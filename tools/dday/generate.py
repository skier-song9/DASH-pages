#!/usr/bin/env python3
"""Generate and validate the DASH D-Day feed served from https://dash-worklife.org/data/v1/.

The feed is a set of static JSON files: one ``manifest.json`` plus one ``{CC}/{YYYY}.json`` per
country and year. The iOS/Android apps download them with a conditional GET once a day, so the
output must be deterministic (same inputs -> byte-identical files) and every change must be a
real content change. Three sources feed each country file:

* tier 1 - public holidays from the ``holidays`` library, optionally corrected by the country's
  official source (for KR the 한국천문연구원 특일정보 API when ``DATA_GO_KR_KEY`` is set);
* tier 2 - recurring cultural days curated by hand in ``curated/{CC}.yaml`` (a ``rule`` "MM-DD",
  or a computed nth-weekday date such as Black Friday);
* tier 3 - dated one-off events curated by hand with a verifiable ``src`` URL.

Run ``generate.py`` to (re)build ``data/v1`` and ``generate.py --check`` to validate whatever is
on disk against the feed contract. The contract itself lives in the DASH repo brief; the
validator below is the executable copy of it.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import logging
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import holidays
import yaml

LOG = logging.getLogger("dday")

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent
DEFAULT_OUT = REPO_ROOT / "data" / "v1"
DEFAULT_CONFIG = HERE / "countries.yaml"
DEFAULT_CURATED = HERE / "curated"

FEED_VERSION = 1
# The feed's "today" is Korean time because the product is Korean-first and the weekly cron is
# specified in KST; using one fixed zone also keeps CI and laptops from disagreeing on the date.
FEED_TIMEZONE = ZoneInfo("Asia/Seoul")

ID_RE = re.compile(r"^[a-z]{2}-[a-z0-9-]+$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RULE_RE = re.compile(r"^\d{2}-\d{2}$")
YEAR_SUFFIX_RE = re.compile(r"-(\d{4})$")
COUNTRY_RE = re.compile(r"^[A-Z]{2}$")

CATEGORIES = ("holiday", "event")
TIERS = (1, 2, 3)
INTERESTS = ("festival", "worker", "undergrad", "grad", "jobseeker", "romance", "other")
FLAGS = ("dayoff", "substitute", "temporary", "tentative")
# The canonical key order makes diffs readable and keeps regenerated files byte-identical.
EVENT_KEY_ORDER = (
    "id", "tier", "cat", "name", "name_en", "start", "end", "rule",
    "dayoff", "substitute", "temporary", "tentative", "interests", "region", "src",
)
EVENT_KEYS = frozenset(EVENT_KEY_ORDER)
# Curated entries may carry these straight through to the output; everything else is derived.
CURATED_PASSTHROUGH = ("name", "name_en", "interests", "region", "src", "temporary", "tentative", "cat", "end")

WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}

DATA_GO_KR_URL = "https://apis.data.go.kr/B090041/openapi/service/SpcdeInfoService/getRestDeInfo"
DATA_GO_KR_TIMEOUT = 20


class FeedError(Exception):
    """A configuration or data problem that must stop generation rather than ship a bad feed."""


# --------------------------------------------------------------------------------------------
# Small pure helpers
# --------------------------------------------------------------------------------------------

def slugify(text: str) -> str:
    """Turn an English holiday name into the stable id slug ("New Year's Day" -> "new-years-day").

    Apostrophes are dropped rather than replaced so possessives do not grow a stray hyphen; every
    other non-alphanumeric run becomes a single hyphen. The slug is derived from the English name
    only, because Korean/Japanese text has no canonical romanisation and reactions are keyed by id.
    """
    lowered = text.lower().replace("'", "").replace("’", "")
    slug = re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
    if not slug:
        raise FeedError(f"cannot derive an id slug from {text!r}")
    return slug


def label_pattern(template: str | None) -> re.Pattern[str] | None:
    """Compile a ``holidays`` label template such as ``"%s (observed)"`` into a matcher.

    The library expresses observed/estimated holidays by formatting the base name into a
    per-language template; reversing the template is the only reliable way to recover the base
    name and the flag. A bare ``"%s"`` (Japan) carries no information and yields ``None``.
    """
    if not template or "%s" not in template or template.strip() == "%s":
        return None
    return re.compile("^" + re.escape(template).replace("%s", "(?P<base>.+)") + "$")


@dataclasses.dataclass(frozen=True)
class Labels:
    observed: re.Pattern[str] | None
    estimated: re.Pattern[str] | None
    observed_estimated: re.Pattern[str] | None

    @classmethod
    def from_instance(cls, inst: Any) -> "Labels":
        # The class attributes hold the untranslated source template (Korean for KR); the instance's
        # ``tr`` applies the language it was constructed with, which is what the names went through.
        translate = getattr(inst, "tr", None) or (lambda text: text)

        def template(name: str) -> str | None:
            raw = getattr(type(inst), name, None)
            return translate(raw) if raw else None

        return cls(
            observed=label_pattern(template("observed_label")),
            estimated=label_pattern(template("estimated_label")),
            observed_estimated=label_pattern(template("observed_estimated_label")),
        )


def classify_name(name: str, labels: Labels) -> tuple[str, bool, bool]:
    """Split a library holiday name into (base name, substitute?, tentative?).

    The combined label is tried first because ``"%s (observed, estimated)"`` would otherwise be
    swallowed by the plain observed pattern and lose the estimated flag.
    """
    for pattern, substitute, tentative in (
        (labels.observed_estimated, True, True),
        (labels.observed, True, False),
        (labels.estimated, False, True),
    ):
        if pattern is not None:
            match = pattern.match(name)
            if match:
                return match.group("base"), substitute, tentative
    return name, False, False


def reduce_family(name: str, patterns: Iterable[str]) -> str:
    """Map a companion day ("The day preceding Chuseok") to its family name ("Chuseok")."""
    for raw in patterns:
        match = re.match(raw, name)
        if match:
            return match.group(1)
    return name


def nth_weekday(year: int, month: int, weekday: int, n: int, offset: int = 0) -> dt.date:
    """Return the n-th given weekday of a month plus an offset in days (Black Friday = 4th Thu + 1)."""
    first = dt.date(year, month, 1)
    delta = (weekday - first.weekday()) % 7
    day = first + dt.timedelta(days=delta + 7 * (n - 1))
    if day.month != month:
        raise FeedError(f"{year}-{month:02d} has no {n}th weekday {weekday}")
    return day + dt.timedelta(days=offset)


def as_date(value: Any) -> dt.date:
    """Accept both ISO strings and the ``datetime.date`` PyYAML produces for unquoted dates."""
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str) and DATE_RE.match(value):
        return dt.date.fromisoformat(value)
    raise FeedError(f"not a calendar date: {value!r}")


def order_event(event: dict[str, Any]) -> dict[str, Any]:
    unknown = set(event) - EVENT_KEYS
    if unknown:
        raise FeedError(f"event {event.get('id')!r} has keys outside the contract: {sorted(unknown)}")
    return {key: event[key] for key in EVENT_KEY_ORDER if key in event}


def event_sort_key(year: int, event: dict[str, Any]) -> tuple[str, int, str]:
    """Interleave dated and rule events by calendar position so the file reads like a calendar."""
    when = event.get("start") or f"{year}-{event['rule']}"
    return (when, event["tier"], event["id"])


# --------------------------------------------------------------------------------------------
# Tier 1 - public holidays from the ``holidays`` library
# --------------------------------------------------------------------------------------------

@dataclasses.dataclass
class Row:
    """One library holiday on one date, in both languages, with its flags recovered."""
    date: dt.date
    name_local: str
    name_en: str
    base_local: str
    base_en: str
    substitute: bool
    tentative: bool


@dataclasses.dataclass
class HolidayGroup:
    """One or more consecutive rows that ship as a single (possibly ranged) event."""
    start: dt.date
    end: dt.date
    name_local: str
    name_en: str
    base_local: str
    base_en: str
    substitute: bool
    tentative: bool


def library_rows(cc: str, year: int, cfg: dict[str, Any]) -> list[Row]:
    """Read one year of public holidays in the local language and English, aligned by date.

    Two instances are used because the library translates names at construction time. A date can
    hold several holidays, so names are read with ``get_list`` and zipped; the lists come from the
    same insertion order, and a length mismatch means the translations diverged and must be looked
    at rather than silently mispaired.
    """
    cls = getattr(holidays, cc, None)
    if cls is None:
        raise FeedError(f"holidays library has no country {cc}")
    local = cls(years=year, language=cfg["language"])
    english = cls(years=year, language="en_US")
    labels_local = Labels.from_instance(local)
    labels_en = Labels.from_instance(english)
    bare_substitutes = set(cfg.get("bare_substitute_names_en", []))

    rows: list[Row] = []
    for day in sorted(local.keys()):
        names_local = local.get_list(day)
        names_en = english.get_list(day)
        if len(names_local) != len(names_en):
            raise FeedError(f"{cc} {day}: {len(names_local)} local names vs {len(names_en)} English names")
        for name_local, name_en in zip(names_local, names_en):
            base_local, sub_local, tent_local = classify_name(name_local, labels_local)
            base_en, sub_en, tent_en = classify_name(name_en, labels_en)
            substitute = sub_local or sub_en or base_en in bare_substitutes
            rows.append(Row(
                date=day, name_local=name_local, name_en=name_en,
                base_local=base_local, base_en=base_en,
                substitute=substitute, tentative=tent_local or tent_en,
            ))
    resolve_bare_substitutes(rows, bare_substitutes)
    return rows


def resolve_bare_substitutes(rows: list[Row], bare_names: set[str]) -> None:
    """Link a nameless substitute (Japan's 振替休日) to the Sunday holiday it stands in for.

    Japanese law moves a holiday that falls on Sunday to the next non-holiday weekday, so walking
    back over consecutive holiday dates until a Sunday holiday appears identifies the source. The
    id is derived from that source; the displayed name stays the library's, because Japanese
    calendars print 振替休日 without naming the holiday.
    """
    if not bare_names:
        return
    by_date = {row.date: row for row in rows}
    for row in rows:
        if row.base_en not in bare_names:
            continue
        cursor = row.date - dt.timedelta(days=1)
        source = None
        while cursor in by_date:
            candidate = by_date[cursor]
            if candidate.base_en not in bare_names and cursor.weekday() == 6:
                source = candidate
                break
            cursor -= dt.timedelta(days=1)
        if source is None:
            LOG.warning("%s: could not find the Sunday holiday behind %r; id falls back to the date", row.date, row.name_en)
            continue
        row.base_en = source.base_en
        row.base_local = source.base_local


def group_rows(rows: list[Row], cfg: dict[str, Any]) -> list[HolidayGroup]:
    """Merge consecutive companion days (설날 전날/설날/설날 다음날) into one ranged event.

    Rows are grouped when they are adjacent dates, reduce to the same English family name and
    share the substitute flag. Family names only replace the row names when a merge actually
    happened, so a lone companion day keeps its own label instead of being renamed.
    """
    patterns = cfg.get("family_patterns", {}) or {}
    pat_local = patterns.get("local", []) or []
    pat_en = patterns.get("en", []) or []

    groups: list[HolidayGroup] = []
    members: list[list[Row]] = []
    for row in sorted(rows, key=lambda r: (r.date, r.name_en)):
        family_en = reduce_family(row.base_en, pat_en)
        if groups:
            last = groups[-1]
            adjacent = row.date == last.end + dt.timedelta(days=1)
            if adjacent and family_en == last.base_en and row.substitute == last.substitute:
                last.end = row.date
                last.tentative = last.tentative or row.tentative
                members[-1].append(row)
                continue
        groups.append(HolidayGroup(
            start=row.date, end=row.date,
            name_local=row.name_local, name_en=row.name_en,
            base_local=reduce_family(row.base_local, pat_local), base_en=family_en,
            substitute=row.substitute, tentative=row.tentative,
        ))
        members.append([row])

    for group, rows_in in zip(groups, members):
        if len(rows_in) == 1:
            # Not merged: keep the row's own names, but the id still comes from the row's base.
            group.base_en = rows_in[0].base_en
            group.base_local = rows_in[0].base_local
        else:
            # Merged: the family name is the event name ("추석", "Chuseok").
            group.name_local = group.base_local
            group.name_en = group.base_en
    return groups


def holiday_events(cc: str, year: int, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Build the tier-1 events for one country-year with stable ids and contract flags."""
    groups = group_rows(library_rows(cc, year, cfg), cfg)
    not_dayoff = set(cfg.get("not_dayoff", []) or [])
    sub_label = cfg.get("substitute_label") or {}
    prefix = cc.lower()

    events: list[dict[str, Any]] = []
    for group in groups:
        slug = slugify(group.base_en)
        name_local, name_en = group.name_local, group.name_en
        if group.substitute and sub_label:
            name_local = sub_label["local"].format(base=group.base_local)
            name_en = sub_label["en"].format(base=group.base_en)
        event: dict[str, Any] = {
            "id": f"{prefix}-{slug}{'-sub' if group.substitute else ''}-{year}",
            "tier": 1,
            "cat": "holiday",
            "name": name_local,
            "name_en": name_en,
            "start": group.start.isoformat(),
            "dayoff": group.base_en not in not_dayoff,
        }
        if group.end != group.start:
            event["end"] = group.end.isoformat()
        if group.substitute:
            event["substitute"] = True
        if group.tentative:
            event["tentative"] = True
        events.append(event)

    disambiguate_ids(events)
    return [order_event(e) for e in events]


def disambiguate_ids(events: list[dict[str, Any]]) -> None:
    """Make colliding ids unique by inserting the month-day into the slug.

    Collisions are rare (two "National Holiday" days in one Japanese year) and the date is the
    only stable disambiguator: an ordinal would shift whenever a new holiday is inserted earlier
    in the year, which would silently re-key reactions.
    """
    counts = Counter(e["id"] for e in events)
    for event in events:
        if counts[event["id"]] > 1:
            match = YEAR_SUFFIX_RE.search(event["id"])
            head = event["id"][: match.start()]
            mmdd = event["start"][5:7] + event["start"][8:10]
            if head.endswith("-sub"):
                head = head[:-4] + f"-{mmdd}-sub"
            else:
                head = head + f"-{mmdd}"
            event["id"] = head + match.group(0)


# --------------------------------------------------------------------------------------------
# Tier 1 correction - official source (KR: 한국천문연구원 특일정보 API)
# --------------------------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class OfficialDay:
    date: dt.date
    name: str
    is_holiday: bool


def fetch_data_go_kr(service_key: str, year: int) -> list[OfficialDay] | None:
    """Fetch one year of 국경일·공휴일 from data.go.kr; ``None`` means "no data, keep the library".

    Network and API failures are logged and swallowed on purpose: the weekly job must still
    publish a feed from the library alone rather than leave stale files behind.
    """
    # data.go.kr issues the same key in an "encoded" and a "decoded" form. Percent-encoding an
    # already-encoded key breaks authentication, so a key that contains '%' is used verbatim.
    key_param = service_key if "%" in service_key else urllib.parse.quote(service_key, safe="")
    query = f"serviceKey={key_param}&solYear={year}&numOfRows=100&pageNo=1&_type=json"
    url = f"{DATA_GO_KR_URL}?{query}"
    try:
        with urllib.request.urlopen(url, timeout=DATA_GO_KR_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        LOG.warning("data.go.kr %s: request failed (%s); keeping library holidays", year, exc)
        return None
    try:
        return parse_data_go_kr(payload)
    except FeedError as exc:
        LOG.warning("data.go.kr %s: %s; keeping library holidays", year, exc)
        return None


def parse_data_go_kr(payload: dict[str, Any]) -> list[OfficialDay]:
    """Normalise the portal's XML-flavoured JSON: ``items`` is ``""`` when empty and ``item`` is a
    bare object when there is exactly one row."""
    response = payload.get("response") or {}
    header = response.get("header") or {}
    if str(header.get("resultCode")) != "00":
        raise FeedError(f"resultCode={header.get('resultCode')} ({header.get('resultMsg')})")
    items = (response.get("body") or {}).get("items") or {}
    if not isinstance(items, dict):
        return []
    raw = items.get("item") or []
    if isinstance(raw, dict):
        raw = [raw]
    days: list[OfficialDay] = []
    for item in raw:
        locdate = str(item.get("locdate", "")).strip()
        if not re.fullmatch(r"\d{8}", locdate):
            raise FeedError(f"unexpected locdate {item.get('locdate')!r}")
        days.append(OfficialDay(
            date=dt.date(int(locdate[:4]), int(locdate[4:6]), int(locdate[6:8])),
            name=str(item.get("dateName", "")).strip(),
            is_holiday=str(item.get("isHoliday", "")).strip().upper() == "Y",
        ))
    return days


def apply_official(cc: str, year: int, events: list[dict[str, Any]], official: list[OfficialDay],
                   cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Correct the library's tier-1 list with the official one.

    The library stays the source of bilingual names; the official list adds what it does not know
    (임시공휴일 declared during the year, late substitute decisions) and fixes ``dayoff`` for days
    the government lists without a day off. Library days the official list omits are kept, because
    the API only carries statutory public holidays and the product wants 노동절 shown.
    """
    if not official:
        return events
    covered: dict[dt.date, dict[str, Any]] = {}
    for event in events:
        if event["tier"] != 1:
            continue
        start = dt.date.fromisoformat(event["start"])
        end = dt.date.fromisoformat(event.get("end", event["start"]))
        for offset in range((end - start).days + 1):
            covered[start + dt.timedelta(days=offset)] = event

    names_en = cfg.get("official_names_en", {}) or {}
    added: list[dict[str, Any]] = []
    for day in official:
        if day.date.year != year:
            continue
        known = covered.get(day.date)
        if known is not None:
            if not day.is_holiday and known.get("dayoff", True):
                LOG.info("%s %s: official source says %r is not a day off", cc, day.date, day.name)
                known["dayoff"] = False
            continue
        if not day.is_holiday:
            continue
        name_en = names_en.get(day.name)
        if name_en is None:
            LOG.warning("%s %s: no English name configured for official %r; using the local name", cc, day.date, day.name)
            name_en = day.name
        try:
            slug = slugify(name_en)
        except FeedError:
            slug = "public-holiday"
        substitute = "대체" in day.name
        temporary = "임시" in day.name
        event: dict[str, Any] = {
            "id": f"{cc.lower()}-{slug}-{day.date:%m%d}{'-sub' if substitute else ''}-{year}",
            "tier": 1,
            "cat": "holiday",
            "name": day.name,
            "name_en": name_en,
            "start": day.date.isoformat(),
            "dayoff": True,
        }
        if substitute:
            event["substitute"] = True
        if temporary:
            event["temporary"] = True
        LOG.info("%s %s: adding %r from the official source", cc, day.date, day.name)
        added.append(order_event(event))

    official_dates = {d.date for d in official}
    for day, event in covered.items():
        if day not in official_dates:
            LOG.info("%s %s: %r is not in the official list; kept from the library", cc, day, event["name"])
    return events + added


# --------------------------------------------------------------------------------------------
# Tiers 2 and 3 - curated YAML
# --------------------------------------------------------------------------------------------

def load_yaml(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def curated_events(cc: str, year: int, doc: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Expand the curated entries of one country into the events that belong in a given year.

    Three shapes are accepted: ``rule: "MM-DD"`` (recurring, id without a year, copied into every
    year file for the client to expand), ``nth_weekday`` (computed date, id gains ``-{year}``), and
    ``start`` (a dated one-off whose id must already end in its year). The id is always spelled out
    in YAML rather than derived, because renaming an event must never re-key its reactions.
    """
    if not doc:
        return []
    if doc.get("country") != cc:
        raise FeedError(f"curated file for {cc} declares country {doc.get('country')!r}")
    prefix = f"{cc.lower()}-"
    events: list[dict[str, Any]] = []
    for entry in doc.get("events", []) or []:
        base_id = str(entry.get("id", ""))
        if not base_id.startswith(prefix):
            raise FeedError(f"curated id {base_id!r} must start with {prefix!r}")
        event: dict[str, Any] = {"tier": int(entry.get("tier", 2)), "cat": entry.get("cat", "event")}
        for key in CURATED_PASSTHROUGH:
            if key in entry and key != "end":
                event[key] = entry[key]
        if "end" in entry:
            event["end"] = as_date(entry["end"]).isoformat()

        shapes = [k for k in ("rule", "nth_weekday", "start") if k in entry]
        if len(shapes) != 1:
            raise FeedError(f"curated {base_id!r} needs exactly one of rule / nth_weekday / start")
        shape = shapes[0]
        if shape == "rule":
            if YEAR_SUFFIX_RE.search(base_id):
                raise FeedError(f"recurring {base_id!r} must not carry a year in its id")
            event["id"] = base_id
            event["rule"] = str(entry["rule"])
        elif shape == "nth_weekday":
            spec = entry["nth_weekday"]
            weekday = spec["weekday"]
            weekday = WEEKDAYS[weekday.lower()] if isinstance(weekday, str) else int(weekday)
            day = nth_weekday(year, int(spec["month"]), weekday, int(spec["n"]), int(spec.get("offset", 0)))
            event["id"] = f"{base_id}-{year}"
            event["start"] = day.isoformat()
        else:
            start = as_date(entry["start"])
            if start.year != year:
                continue
            if not base_id.endswith(f"-{year}"):
                raise FeedError(f"dated {base_id!r} must end with -{year} (its start year)")
            event["id"] = base_id
            event["start"] = start.isoformat()

        if event["cat"] == "event":
            event["interests"] = list(entry.get("interests", []) or [])
        events.append(order_event(event))
    return events


# --------------------------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------------------------

def build_feed(cc: str, year: int, events: list[dict[str, Any]], previous: dict[str, Any] | None,
               today: dt.date) -> dict[str, Any]:
    """Wrap events in the file envelope, keeping the previous ``updated`` when nothing changed.

    ``updated`` is a content stamp, not a run stamp: if it moved on every cron run the workflow
    would commit weekly for no reason and every client would re-download an identical file.
    """
    ordered = sorted(events, key=lambda e: event_sort_key(year, e))
    updated = today.isoformat()
    if previous and previous.get("events") == ordered and isinstance(previous.get("updated"), str):
        updated = previous["updated"]
    return {"country": cc, "year": year, "updated": updated, "events": ordered}


def build_manifest(countries_cfg: dict[str, Any], feeds: dict[str, dict[int, dict[str, Any]]]) -> dict[str, Any]:
    """Index the generated files. Countries keep the order of countries.yaml so the app's region
    picker can list the primary market first without sorting on its side."""
    countries: dict[str, Any] = {}
    for cc in countries_cfg:
        if cc not in feeds:
            continue
        per_year = feeds[cc]
        cfg = countries_cfg[cc]
        countries[cc] = {
            "name": cfg["name"],
            "name_en": cfg["name_en"],
            "years": sorted(per_year),
            "updated": max(doc["updated"] for doc in per_year.values()),
        }
    return {
        "version": FEED_VERSION,
        "updated": max(c["updated"] for c in countries.values()) if countries else dt.date.today().isoformat(),
        "countries": countries,
    }


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError) as exc:
        LOG.warning("%s: unreadable, treating as absent (%s)", path, exc)
        return None


def write_json(path: Path, doc: dict[str, Any]) -> bool:
    """Write pretty, UTF-8 JSON and report whether the bytes actually changed."""
    text = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.write_text(text, encoding="utf-8")
    return True


def generate(out_dir: Path, config: dict[str, Any], curated_dir: Path, years: list[int],
             only: list[str] | None, today: dt.date, use_official: bool) -> int:
    countries_cfg: dict[str, Any] = config["countries"]
    selected = [cc for cc in countries_cfg if not only or cc in only]
    if only:
        missing = sorted(set(only) - set(selected))
        if missing:
            raise FeedError(f"countries not in countries.yaml: {missing}")

    kr_key = os.environ.get("DATA_GO_KR_KEY", "").strip()
    feeds: dict[str, dict[int, dict[str, Any]]] = {}
    changed = 0
    for cc in selected:
        cfg = countries_cfg[cc]
        curated_path = curated_dir / f"{cc}.yaml"
        curated_doc = load_yaml(curated_path) if curated_path.exists() else None
        feeds[cc] = {}
        for year in years:
            events = holiday_events(cc, year, cfg)
            if cfg.get("official") == "data_go_kr":
                if not use_official:
                    LOG.info("%s %s: official override disabled by --no-official", cc, year)
                elif not kr_key:
                    LOG.info("%s %s: DATA_GO_KR_KEY not set; skipping the official override", cc, year)
                else:
                    official = fetch_data_go_kr(kr_key, year)
                    if official is not None:
                        events = apply_official(cc, year, events, official, cfg)
            events += curated_events(cc, year, curated_doc)

            path = out_dir / cc / f"{year}.json"
            doc = build_feed(cc, year, events, read_json(path), today)
            errors = validate_feed(doc, cc, year)
            if errors:
                raise FeedError(f"{cc}/{year}.json fails validation:\n  " + "\n  ".join(errors))
            if write_json(path, doc):
                changed += 1
                shown = path.relative_to(REPO_ROOT) if path.is_relative_to(REPO_ROOT) else path
                LOG.info("wrote %s (%d events)", shown, len(doc["events"]))
            feeds[cc][year] = doc

    manifest_path = out_dir / "manifest.json"
    previous_manifest = read_json(manifest_path) or {}
    # Countries that were not regenerated this run keep their previous manifest entry, so a
    # partial run (``--countries KR``) never drops the others from the index.
    merged: dict[str, dict[int, dict[str, Any]]] = {}
    for cc, entry in (previous_manifest.get("countries") or {}).items():
        if cc in countries_cfg and cc not in feeds:
            merged[cc] = {int(y): {"updated": entry.get("updated", today.isoformat())} for y in entry.get("years", [])}
    merged.update(feeds)
    manifest = build_manifest(countries_cfg, merged)
    if write_json(manifest_path, manifest):
        changed += 1
        LOG.info("wrote manifest.json")
    return changed


# --------------------------------------------------------------------------------------------
# Validation (--check) - the executable form of the feed contract
# --------------------------------------------------------------------------------------------

def _is_date(value: Any) -> bool:
    if not isinstance(value, str) or not DATE_RE.match(value):
        return False
    try:
        dt.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def validate_event(event: Any, cc: str, year: int) -> list[str]:
    errors: list[str] = []
    if not isinstance(event, dict):
        return ["event is not an object"]
    ident = event.get("id", "<no id>")

    def err(msg: str) -> None:
        errors.append(f"{ident}: {msg}")

    unknown = set(event) - EVENT_KEYS
    if unknown:
        err(f"unknown keys {sorted(unknown)}")
    for key in ("id", "tier", "cat", "name", "name_en"):
        if key not in event:
            err(f"missing {key}")
    if not isinstance(ident, str) or not ID_RE.match(ident):
        err("id does not match ^[a-z]{2}-[a-z0-9-]+$")
    elif not ident.startswith(cc.lower() + "-"):
        err(f"id does not start with {cc.lower()}-")
    if event.get("tier") not in TIERS:
        err(f"tier must be one of {TIERS}")
    if event.get("cat") not in CATEGORIES:
        err(f"cat must be one of {CATEGORIES}")
    for key in ("name", "name_en"):
        if key in event and (not isinstance(event[key], str) or not event[key].strip()):
            err(f"{key} must be a non-empty string")

    has_start, has_rule = "start" in event, "rule" in event
    if has_start == has_rule:
        err("exactly one of start / rule is required")
    if has_start:
        if not _is_date(event["start"]):
            err("start is not a YYYY-MM-DD date")
        elif int(event["start"][:4]) != year:
            err(f"start is not in {year}")
        if "end" in event:
            if not _is_date(event["end"]):
                err("end is not a YYYY-MM-DD date")
            elif _is_date(event["start"]):
                start, end = dt.date.fromisoformat(event["start"]), dt.date.fromisoformat(event["end"])
                if end < start:
                    err("end is before start")
                if end.year > year + 1:
                    err("end is more than a year out")
        if event.get("tier") == 1 and isinstance(ident, str) and not ident.endswith(f"-{year}"):
            err(f"tier-1 id must end with -{year}")
    if has_rule:
        rule = event["rule"]
        if not isinstance(rule, str) or not RULE_RE.match(rule):
            err("rule is not MM-DD")
        else:
            month, day = int(rule[:2]), int(rule[3:])
            try:
                dt.date(2001, month, day)  # 2001 is not a leap year, so 02-29 is rejected on purpose.
            except ValueError:
                err("rule is not a date that exists every year")
        if "end" in event:
            err("end is only allowed with start")
        if isinstance(ident, str) and YEAR_SUFFIX_RE.search(ident):
            err("recurring (rule) ids must not end with a year")

    for flag in FLAGS:
        if flag in event and not isinstance(event[flag], bool):
            err(f"{flag} must be a boolean")
    if event.get("cat") == "event":
        interests = event.get("interests")
        if not isinstance(interests, list):
            err("events need an interests array (empty = common)")
        else:
            bad = [i for i in interests if i not in INTERESTS]
            if bad:
                err(f"unknown interests {bad}")
            if len(set(interests)) != len(interests):
                err("interests contains duplicates")
    elif "interests" in event:
        err("interests is only allowed on events")
    if "region" in event and (not isinstance(event["region"], str) or not event["region"].strip()):
        err("region must be a non-empty string")
    if "src" in event and (not isinstance(event["src"], str) or not re.match(r"^https?://\S+$", event["src"])):
        err("src must be an http(s) URL")
    if event.get("tier") == 3 and "src" not in event:
        err("tier-3 events must cite a src URL")
    return errors


def validate_feed(doc: Any, cc: str, year: int) -> list[str]:
    if not isinstance(doc, dict):
        return ["feed is not an object"]
    errors: list[str] = []
    unknown = set(doc) - {"country", "year", "updated", "events"}
    if unknown:
        errors.append(f"unknown top-level keys {sorted(unknown)}")
    if doc.get("country") != cc:
        errors.append(f"country is {doc.get('country')!r}, expected {cc!r}")
    if doc.get("year") != year:
        errors.append(f"year is {doc.get('year')!r}, expected {year}")
    if not _is_date(doc.get("updated")):
        errors.append("updated is not a YYYY-MM-DD date")
    events = doc.get("events")
    if not isinstance(events, list):
        return errors + ["events is not an array"]
    for event in events:
        errors.extend(validate_event(event, cc, year))
    ids = [e.get("id") for e in events if isinstance(e, dict)]
    for ident, count in Counter(ids).items():
        if count > 1:
            errors.append(f"{ident}: id appears {count} times")
    return errors


def validate_manifest(doc: Any) -> list[str]:
    if not isinstance(doc, dict):
        return ["manifest is not an object"]
    errors: list[str] = []
    if doc.get("version") != FEED_VERSION:
        errors.append(f"version must be {FEED_VERSION}")
    if not _is_date(doc.get("updated")):
        errors.append("updated is not a YYYY-MM-DD date")
    countries = doc.get("countries")
    if not isinstance(countries, dict) or not countries:
        return errors + ["countries must be a non-empty object"]
    for cc, entry in countries.items():
        if not COUNTRY_RE.match(str(cc)):
            errors.append(f"{cc}: country code must be two upper-case letters")
        if not isinstance(entry, dict):
            errors.append(f"{cc}: entry is not an object")
            continue
        for key in ("name", "name_en"):
            if not isinstance(entry.get(key), str) or not entry[key].strip():
                errors.append(f"{cc}: {key} must be a non-empty string")
        years = entry.get("years")
        if not isinstance(years, list) or not years or not all(isinstance(y, int) for y in years):
            errors.append(f"{cc}: years must be a non-empty array of integers")
        if not _is_date(entry.get("updated")):
            errors.append(f"{cc}: updated is not a YYYY-MM-DD date")
    return errors


def check_output(out_dir: Path) -> list[str]:
    """Validate manifest.json and every country file it lists; used by ``--check`` and CI."""
    manifest_path = out_dir / "manifest.json"
    manifest = read_json(manifest_path)
    if manifest is None:
        return [f"{manifest_path}: missing or unreadable"]
    errors = [f"manifest.json: {e}" for e in validate_manifest(manifest)]
    if errors:
        return errors
    for cc, entry in manifest["countries"].items():
        for year in entry["years"]:
            path = out_dir / cc / f"{year}.json"
            doc = read_json(path)
            if doc is None:
                errors.append(f"{cc}/{year}.json: missing or unreadable")
                continue
            errors.extend(f"{cc}/{year}.json: {e}" for e in validate_feed(doc, cc, year))
            if doc.get("updated", "") > manifest["countries"][cc]["updated"]:
                errors.append(f"{cc}/{year}.json: updated is newer than the manifest entry")
    return errors


# --------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output directory (data/v1)")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="countries.yaml")
    parser.add_argument("--curated", type=Path, default=DEFAULT_CURATED, help="directory of curated {CC}.yaml files")
    parser.add_argument("--years", type=int, nargs="+", help="years to generate (default: this year and next)")
    parser.add_argument("--countries", nargs="+", help="subset of country codes to regenerate")
    parser.add_argument("--today", help="override today's date (YYYY-MM-DD) for reproducible runs")
    parser.add_argument("--no-official", action="store_true", help="skip official-source overrides even if a key is set")
    parser.add_argument("--check", action="store_true", help="validate the files on disk instead of generating")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)

    if args.check:
        errors = check_output(args.out)
        for line in errors:
            print(line, file=sys.stderr)
        print(f"{'FAIL' if errors else 'OK'}: {args.out} ({len(errors)} problem(s))", file=sys.stderr)
        return 1 if errors else 0

    today = dt.date.fromisoformat(args.today) if args.today else dt.datetime.now(FEED_TIMEZONE).date()
    years = args.years or [today.year, today.year + 1]
    config = load_yaml(args.config)
    try:
        changed = generate(args.out, config, args.curated, years, args.countries, today, not args.no_official)
    except FeedError as exc:
        LOG.error("%s", exc)
        return 1
    LOG.info("done: %d file(s) changed", changed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
