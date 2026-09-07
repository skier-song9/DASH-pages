# D-Day feed pipeline

Generates the static JSON the DASH apps read for the **D-Day** sidebar (holidays and events per
country). Output lives in `data/v1/` and is served by GitHub Pages at
`https://dash-worklife.org/data/v1/`.

```
data/v1/manifest.json        which countries and years exist
data/v1/{CC}/{YYYY}.json     one file per country and year
```

## Running

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r tools/dday/requirements.txt

python tools/dday/generate.py                 # this year + next, all countries -> data/v1
python tools/dday/generate.py --years 2026 2027 --countries KR --today 2026-09-08
python tools/dday/generate.py --check          # validate what is on disk against the contract
python -m unittest discover -s tools/dday      # unit tests
```

`--today` fixes the date stamp for reproducible runs. `updated` fields only move when a file's
events actually change, so re-running on another day produces no diff; that is what lets the weekly
workflow commit only when something changed and lets clients get `304 Not Modified` otherwise.

`DATA_GO_KR_KEY` (environment variable; GitHub secret in CI) enables the Korean official
override described below. Without it the run logs a line and continues with the library data.

## Sources and tiers

| tier | what | where it comes from |
|------|------|---------------------|
| 1 | public holidays | [`holidays`](https://pypi.org/project/holidays/) 0.104 (`language` per `countries.yaml`, English from `en_US`), optionally corrected by an official source |
| 2 | recurring cultural days | `curated/{CC}.yaml` - `rule: "MM-DD"` (client expands) or `nth_weekday` (computed here, e.g. Black Friday) |
| 3 | dated one-off events | `curated/{CC}.yaml` - `start` plus a mandatory `src` URL where the date was verified |

Tier-1 processing:

- Consecutive companion days merge into one ranged event: 설날 전날/설날/설날 다음날 becomes
  `kr-korean-new-year-2026` with `start`/`end`. Patterns live in `countries.yaml` (`family_patterns`).
- Substitute holidays get `substitute: true`, the id suffix `-sub` and a display label from
  `substitute_label` ("개천절 대체공휴일" / "National Foundation Day (substitute)"). Japan's bare
  振替休日 keeps its name but its id is linked to the Sunday holiday it replaces
  (`jp-constitution-day-sub-2026`).
- Names the library marks "(estimated)" / "(추정)" lose the suffix and get `tentative: true`.
- `not_dayoff` lists public days that are not days off (KR 제헌절 -> `dayoff: false`). 노동절 is
  kept with `dayoff: true` on purpose.
- `display_names` swaps the library's statutory wording for what people actually say (KR 신정연휴 ->
  신정, 기독탄신일 -> 크리스마스; a substitute built from a renamed base becomes 크리스마스 대체공휴일).
  It runs last, after the official override, and touches `name` only: `name_en` and ids never move.
- Ids are `{cc}-{slug-of-English-name}[-sub]-{year}` and never depend on the local-language text.
  A rare collision (two 国民の休日 in one year) inserts the month-day into the slug.

Tier 2/3 ids are written out in full in the YAML and must never change: 👍/👎 reactions are keyed
by them. Recurring ids carry no year (`kr-pepero-day`) and are copied into every year file, so a
client that merges two year files must dedupe by id.

### Korean official override (`DATA_GO_KR_KEY`)

When the key is present, `generate.py` calls 한국천문연구원 특일정보 `getRestDeInfo`
(`https://apis.data.go.kr/B090041/openapi/service/SpcdeInfoService/getRestDeInfo`) for each year and
merges the answer into tier 1:

- dates the library does not know are added (임시공휴일 -> `temporary: true`, 대체공휴일 -> `substitute: true`);
  English names come from `official_names_en` in `countries.yaml`;
- dates the API lists with `isHoliday: N` get `dayoff: false`;
- library days the API omits (노동절) are kept and logged.

Store the key in the GitHub repository secret **`DATA_GO_KR_KEY`**. Either form issued by
data.go.kr works: a key containing `%` is treated as already URL-encoded. Network or API failures are
logged and the run continues with library data only, so a portal outage never blocks the feed.

## Adding or changing things

- **New country**: add it to `countries.yaml` (it must exist in the `holidays` library) and,
  optionally, `curated/{CC}.yaml`. The manifest lists countries in `countries.yaml` order.
- **New recurring day**: one line in `curated/{CC}.yaml` with a new, permanent id.
- **New dated event**: only with a `src` URL you actually checked; the generator refuses tier 3
  without one. Entries that could not be verified at authoring time are listed as comments at the
  bottom of `curated/KR.yaml` for re-checking (2027 TOEIC dates, 2027-1 국가장학금, 2027 9급 공채).
- **Upgrading `holidays`**: bump `requirements.txt`, regenerate, and read the `data/v1` diff -
  renamed English names change ids, which orphans reactions. The China `WORKDAY` category is not
  supported in 0.104 and is deliberately not attempted.

## Contract checks (`--check`)

`generate.py --check` validates `manifest.json` and every listed file: key sets, `id` regex
`^[a-z]{2}-[a-z0-9-]+$` with the country prefix, `tier` 1-3, `cat` holiday|event, exactly one of
`start`/`rule`, ISO dates inside the file's year, `end >= start`, `rule` as `MM-DD` that exists
every year (no 02-29), boolean flags, `interests` only on events and only from the seven known
values, `src` as an http(s) URL and mandatory on tier 3, unique ids, tier-1 ids ending in the year,
recurring ids without one, and manifest `updated` stamps not older than their files. The same
validator runs inside generation, so a bad curated entry fails the run before anything is written.

## Schedule

`.github/workflows/dday-feed.yml` runs every Monday 03:00 KST (Sunday 18:00 UTC) and on
`workflow_dispatch`, regenerates, validates, runs the tests and commits to the default branch only
when `data/v1` changed. Scheduled workflows run on the default branch, so the feed a client
downloads always comes from what is deployed.
