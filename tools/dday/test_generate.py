"""Unit tests for the D-Day feed generator.

Run with the pinned environment: ``python -m unittest discover -s tools/dday``. The tests that
touch the ``holidays`` library pin their expectations to the 0.104 output for 2026/2027, so a
library upgrade that changes a name shows up here before it reaches the published feed.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import generate as g  # noqa: E402

HERE = Path(__file__).resolve().parent
CONFIG = g.load_yaml(HERE / "countries.yaml")["countries"]


def by_id(events: list[dict]) -> dict[str, dict]:
    return {e["id"]: e for e in events}


class SlugAndLabelTests(unittest.TestCase):
    def test_slugify_drops_apostrophes_and_collapses_punctuation(self):
        self.assertEqual(g.slugify("New Year's Day"), "new-years-day")
        self.assertEqual(g.slugify("Martin Luther King Jr. Day"), "martin-luther-king-jr-day")
        self.assertEqual(g.slugify("  Chuseok "), "chuseok")
        with self.assertRaises(g.FeedError):
            g.slugify("추석")  # non-Latin text has no slug; ids come from English names only

    def test_label_pattern_recovers_base_name(self):
        pattern = g.label_pattern("%s (observed)")
        self.assertIsNotNone(pattern)
        self.assertEqual(pattern.match("Independence Day (observed)").group("base"), "Independence Day")
        self.assertIsNone(g.label_pattern("%s"))
        self.assertIsNone(g.label_pattern(None))

    def test_classify_name_flags(self):
        labels = g.Labels(
            observed=g.label_pattern("%s (observed)"),
            estimated=g.label_pattern("%s (estimated)"),
            observed_estimated=g.label_pattern("%s (observed, estimated)"),
        )
        self.assertEqual(g.classify_name("Eid al-Fitr (estimated)", labels), ("Eid al-Fitr", False, True))
        self.assertEqual(g.classify_name("Eid al-Fitr (observed, estimated)", labels), ("Eid al-Fitr", True, True))
        self.assertEqual(g.classify_name("Labor Day (observed)", labels), ("Labor Day", True, False))
        self.assertEqual(g.classify_name("Labor Day", labels), ("Labor Day", False, False))

    def test_nth_weekday(self):
        self.assertEqual(g.nth_weekday(2026, 11, g.WEEKDAYS["thu"], 4), dt.date(2026, 11, 26))
        self.assertEqual(g.nth_weekday(2026, 11, g.WEEKDAYS["thu"], 4, offset=1), dt.date(2026, 11, 27))
        self.assertEqual(g.nth_weekday(2027, 11, g.WEEKDAYS["thu"], 4, offset=4), dt.date(2027, 11, 29))
        with self.assertRaises(g.FeedError):
            g.nth_weekday(2026, 2, g.WEEKDAYS["mon"], 5)


class KoreaHolidayTests(unittest.TestCase):
    def setUp(self):
        self.events = by_id(g.holiday_events("KR", 2026, CONFIG["KR"]))

    def test_consecutive_companion_days_merge_into_one_range(self):
        chuseok = self.events["kr-chuseok-2026"]
        self.assertEqual((chuseok["start"], chuseok["end"]), ("2026-09-24", "2026-09-26"))
        self.assertEqual((chuseok["name"], chuseok["name_en"]), ("추석", "Chuseok"))
        seollal = self.events["kr-korean-new-year-2026"]
        self.assertEqual((seollal["start"], seollal["end"]), ("2026-02-16", "2026-02-18"))
        self.assertEqual(seollal["name"], "설날")
        self.assertNotIn("kr-the-day-preceding-chuseok-2026", self.events)

    def test_substitute_holiday_naming_and_id(self):
        sub = self.events["kr-national-foundation-day-sub-2026"]
        self.assertEqual(sub["start"], "2026-10-05")
        self.assertTrue(sub["substitute"])
        self.assertEqual(sub["name"], "개천절 대체공휴일")
        self.assertEqual(sub["name_en"], "National Foundation Day (substitute)")
        self.assertTrue(sub["dayoff"])
        # The English base must be recovered from the translated template, not the Korean one.
        self.assertFalse(any("alternative-holiday" in i for i in self.events))

    def test_constitution_day_is_not_a_day_off_and_labor_day_is(self):
        self.assertFalse(self.events["kr-constitution-day-2026"]["dayoff"])
        self.assertTrue(self.events["kr-labor-day-2026"]["dayoff"])

    def test_expected_2026_set(self):
        # 22 library dates collapse to 18 events once 설날 and 추석 are ranged.
        self.assertEqual(len(self.events), 18)
        self.assertEqual(
            sorted(self.events),
            sorted([
                "kr-new-years-day-2026", "kr-korean-new-year-2026", "kr-independence-movement-day-2026",
                "kr-independence-movement-day-sub-2026", "kr-labor-day-2026", "kr-childrens-day-2026",
                "kr-buddhas-birthday-2026", "kr-buddhas-birthday-sub-2026", "kr-local-election-day-2026",
                "kr-memorial-day-2026", "kr-constitution-day-2026", "kr-liberation-day-2026",
                "kr-liberation-day-sub-2026", "kr-chuseok-2026", "kr-national-foundation-day-2026",
                "kr-national-foundation-day-sub-2026", "kr-hangul-day-2026", "kr-christmas-day-2026",
            ]),
        )

    def test_2027_substitute_after_a_range_stays_separate(self):
        events = by_id(g.holiday_events("KR", 2027, CONFIG["KR"]))
        self.assertEqual(events["kr-korean-new-year-2027"]["end"], "2027-02-08")
        self.assertEqual(events["kr-korean-new-year-sub-2027"]["start"], "2027-02-09")

    def test_ids_are_stable_across_runs(self):
        again = by_id(g.holiday_events("KR", 2026, CONFIG["KR"]))
        self.assertEqual(self.events, again)
        for ident in self.events:
            self.assertRegex(ident, g.ID_RE)

    def test_display_names_swap_statutory_wording_but_keep_ids_and_english(self):
        # 2027 has a Christmas substitute, which exercises the rebuilt substitute label.
        raw = g.holiday_events("KR", 2027, CONFIG["KR"])
        before = {e["id"]: (e["name_en"], e["start"], e.get("end")) for e in raw}
        events = by_id(g.rename_for_display(raw, CONFIG["KR"]))
        self.assertEqual(events["kr-new-years-day-2027"]["name"], "신정")
        self.assertEqual(events["kr-christmas-day-2027"]["name"], "크리스마스")
        self.assertEqual(events["kr-christmas-day-sub-2027"]["name"], "크리스마스 대체공휴일")
        # Names that are not listed pass through untouched, ranges included.
        self.assertEqual(events["kr-korean-new-year-2027"]["name"], "설날")
        self.assertEqual(events["kr-chuseok-2027"]["name"], "추석")
        self.assertEqual(events["kr-buddhas-birthday-2027"]["name"], "부처님오신날")
        self.assertEqual(events["kr-independence-movement-day-2027"]["name"], "삼일절")
        # Every id, English name and date is exactly what it was before the pass.
        self.assertEqual({i: (e["name_en"], e["start"], e.get("end")) for i, e in events.items()}, before)

    def test_display_names_only_touch_tier_one_and_are_a_no_op_without_config(self):
        raw = g.holiday_events("KR", 2026, CONFIG["KR"])
        self.assertEqual(g.rename_for_display([dict(e) for e in raw], {}), raw)
        curated = {"id": "kr-x", "tier": 2, "cat": "event", "name": "기독탄신일", "name_en": "x", "rule": "12-25", "interests": []}
        self.assertEqual(g.rename_for_display([dict(curated)], CONFIG["KR"]), [curated])
        with self.assertRaises(g.FeedError):
            g.rename_for_display(list(raw), {"display_names": {"신정연휴": ""}})


class OtherCountryHolidayTests(unittest.TestCase):
    def test_us_observed_holidays_are_substitutes(self):
        events = by_id(g.holiday_events("US", 2026, CONFIG["US"]))
        observed = events["us-independence-day-sub-2026"]
        self.assertEqual(observed["start"], "2026-07-03")
        self.assertTrue(observed["substitute"])
        self.assertEqual(observed["name_en"], "Independence Day (observed)")
        self.assertEqual(events["us-independence-day-2026"]["start"], "2026-07-04")
        self.assertNotIn("substitute", events["us-independence-day-2026"])

    def test_jp_substitute_links_to_the_sunday_holiday(self):
        events = by_id(g.holiday_events("JP", 2026, CONFIG["JP"]))
        sub = events["jp-constitution-day-sub-2026"]  # 憲法記念日 fell on Sunday 2026-05-03
        self.assertEqual(sub["start"], "2026-05-06")
        self.assertTrue(sub["substitute"])
        self.assertEqual((sub["name"], sub["name_en"]), ("振替休日", "Substitute Holiday"))
        events_2027 = by_id(g.holiday_events("JP", 2027, CONFIG["JP"]))
        self.assertEqual(events_2027["jp-vernal-equinox-day-sub-2027"]["start"], "2027-03-22")

    def test_disambiguate_ids_uses_the_date(self):
        events = [
            {"id": "jp-national-holiday-2026", "start": "2026-05-04"},
            {"id": "jp-national-holiday-2026", "start": "2026-09-22"},
            {"id": "jp-showa-day-sub-2026", "start": "2026-04-30"},
            {"id": "jp-showa-day-sub-2026", "start": "2026-05-06"},
            {"id": "jp-culture-day-2026", "start": "2026-11-03"},
        ]
        g.disambiguate_ids(events)
        self.assertEqual(
            [e["id"] for e in events],
            ["jp-national-holiday-0504-2026", "jp-national-holiday-0922-2026",
             "jp-showa-day-0430-sub-2026", "jp-showa-day-0506-sub-2026", "jp-culture-day-2026"],
        )


class CuratedTests(unittest.TestCase):
    def test_rule_computed_and_dated_shapes(self):
        doc = {
            "country": "US",
            "events": [
                {"id": "us-tax-day", "tier": 2, "name": "Tax Day", "name_en": "Tax Day", "rule": "04-15", "interests": ["worker"]},
                {"id": "us-black-friday", "tier": 2, "name": "Black Friday", "name_en": "Black Friday",
                 "nth_weekday": {"month": 11, "weekday": "thu", "n": 4, "offset": 1}},
                {"id": "us-super-bowl-2027", "tier": 2, "name": "Super Bowl LXI", "name_en": "Super Bowl LXI",
                 "start": dt.date(2027, 2, 14), "src": "https://example.com/sb"},
            ],
        }
        events_2026 = by_id(g.curated_events("US", 2026, doc))
        self.assertEqual(events_2026["us-tax-day"]["rule"], "04-15")
        self.assertEqual(events_2026["us-black-friday-2026"]["start"], "2026-11-27")
        self.assertEqual(events_2026["us-black-friday-2026"]["interests"], [])
        self.assertNotIn("us-super-bowl-2027", events_2026)  # dated entries only land in their year
        events_2027 = by_id(g.curated_events("US", 2027, doc))
        self.assertEqual(events_2027["us-super-bowl-2027"]["start"], "2027-02-14")
        self.assertEqual(events_2027["us-black-friday-2027"]["start"], "2027-11-26")

    def test_curated_shape_errors(self):
        with self.assertRaises(g.FeedError):
            g.curated_events("KR", 2026, {"country": "KR", "events": [
                {"id": "kr-x-2026", "name": "x", "name_en": "x", "rule": "01-01"}]})  # rule id with a year
        with self.assertRaises(g.FeedError):
            g.curated_events("KR", 2026, {"country": "KR", "events": [
                {"id": "kr-x", "name": "x", "name_en": "x", "start": "2026-01-01"}]})  # dated id without its year
        with self.assertRaises(g.FeedError):
            g.curated_events("KR", 2026, {"country": "KR", "events": [
                {"id": "us-x", "name": "x", "name_en": "x", "rule": "01-01"}]})  # wrong country prefix
        with self.assertRaises(g.FeedError):
            g.curated_events("KR", 2026, {"country": "US", "events": []})

    def test_shipped_curated_files_are_consistent_with_the_brief(self):
        kr = by_id(g.curated_events("KR", 2026, g.load_yaml(HERE / "curated" / "KR.yaml")))
        self.assertEqual(kr["kr-pepero-day"]["rule"], "11-11")
        self.assertEqual(kr["kr-white-day"]["interests"], ["romance"])
        self.assertEqual(kr["kr-suneung-2026"]["start"], "2026-11-19")
        self.assertTrue(kr["kr-suneung-2026"]["src"].startswith("https://"))
        self.assertTrue(all("src" in e for e in kr.values() if e["tier"] == 3))
        jp = by_id(g.curated_events("JP", 2026, g.load_yaml(HERE / "curated" / "JP.yaml")))
        self.assertEqual(jp["jp-setsubun"]["rule"], "02-03")
        us = by_id(g.curated_events("US", 2027, g.load_yaml(HERE / "curated" / "US.yaml")))
        self.assertEqual(us["us-cyber-monday-2027"]["start"], "2027-11-29")


class OfficialSourceTests(unittest.TestCase):
    def test_parse_handles_the_portal_json_quirks(self):
        multi = {"response": {"header": {"resultCode": "00"}, "body": {"items": {"item": [
            {"dateKind": "01", "dateName": "추석", "isHoliday": "Y", "locdate": 20260925, "seq": 1},
            {"dateKind": "01", "dateName": "제헌절", "isHoliday": "N", "locdate": 20260717, "seq": 1},
        ]}, "totalCount": 2}}}
        days = g.parse_data_go_kr(multi)
        self.assertEqual([(d.date.isoformat(), d.name, d.is_holiday) for d in days],
                         [("2026-09-25", "추석", True), ("2026-07-17", "제헌절", False)])
        single = {"response": {"header": {"resultCode": "00"}, "body": {"items": {"item":
            {"dateKind": "01", "dateName": "임시공휴일", "isHoliday": "Y", "locdate": "20261002", "seq": 1}}}}}
        self.assertEqual(len(g.parse_data_go_kr(single)), 1)
        empty = {"response": {"header": {"resultCode": "00"}, "body": {"items": "", "totalCount": 0}}}
        self.assertEqual(g.parse_data_go_kr(empty), [])
        with self.assertRaises(g.FeedError):
            g.parse_data_go_kr({"response": {"header": {"resultCode": "30", "resultMsg": "SERVICE_KEY_IS_NOT_REGISTERED_ERROR"}}})

    def test_apply_official_adds_missing_days_and_fixes_dayoff(self):
        events = g.holiday_events("KR", 2026, CONFIG["KR"])
        official = [
            g.OfficialDay(dt.date(2026, 9, 24), "추석", True),
            g.OfficialDay(dt.date(2026, 9, 25), "추석", True),
            g.OfficialDay(dt.date(2026, 9, 26), "추석", True),
            g.OfficialDay(dt.date(2026, 10, 2), "임시공휴일", True),
            g.OfficialDay(dt.date(2026, 7, 17), "제헌절", False),
            g.OfficialDay(dt.date(2026, 5, 5), "어린이날", True),
        ]
        merged = by_id(g.apply_official("KR", 2026, events, official, CONFIG["KR"]))
        added = merged["kr-temporary-public-holiday-1002-2026"]
        self.assertEqual(added["start"], "2026-10-02")
        self.assertTrue(added["temporary"])
        self.assertTrue(added["dayoff"])
        self.assertEqual(added["name_en"], "Temporary Public Holiday")
        self.assertFalse(merged["kr-constitution-day-2026"]["dayoff"])
        self.assertEqual(len(merged), len(events) + 1)  # covered dates never duplicate
        self.assertIn("kr-labor-day-2026", merged)  # library-only days are kept


class ValidatorTests(unittest.TestCase):
    def good(self) -> dict:
        return {"country": "KR", "year": 2026, "updated": "2026-09-08", "events": [
            {"id": "kr-chuseok-2026", "tier": 1, "cat": "holiday", "name": "추석", "name_en": "Chuseok",
             "start": "2026-09-24", "end": "2026-09-26", "dayoff": True},
            {"id": "kr-pepero-day", "tier": 2, "cat": "event", "name": "빼빼로데이", "name_en": "Pepero Day",
             "rule": "11-11", "interests": []},
            {"id": "kr-suneung-2026", "tier": 3, "cat": "event", "name": "수능", "name_en": "CSAT",
             "start": "2026-11-19", "interests": ["undergrad", "other"], "src": "https://www.moe.go.kr/x"},
        ]}

    def test_good_document_passes(self):
        self.assertEqual(g.validate_feed(self.good(), "KR", 2026), [])

    def test_each_contract_rule_is_enforced(self):
        cases = {
            "bad id": lambda d: d["events"][0].update(id="KR-Chuseok-2026"),
            "wrong country prefix": lambda d: d["events"][0].update(id="us-chuseok-2026"),
            "start and rule": lambda d: d["events"][0].update(rule="09-24"),
            "neither start nor rule": lambda d: d["events"][0].pop("start"),
            "interests on holiday": lambda d: d["events"][0].update(interests=[]),
            "missing interests on event": lambda d: d["events"][1].pop("interests"),
            "unknown interest": lambda d: d["events"][1].update(interests=["gamer"]),
            "feb 29 rule": lambda d: d["events"][1].update(rule="02-29"),
            "rule id with year": lambda d: d["events"][1].update(id="kr-pepero-day-2026"),
            "tier 3 without src": lambda d: d["events"][2].pop("src"),
            "end before start": lambda d: d["events"][0].update(end="2026-09-23"),
            "start in another year": lambda d: d["events"][2].update(start="2027-11-18"),
            "tier-1 id without year": lambda d: d["events"][0].update(id="kr-chuseok"),
            "unknown key": lambda d: d["events"][0].update(color="red"),
            "non-boolean flag": lambda d: d["events"][0].update(dayoff="yes"),
            "duplicate id": lambda d: d["events"].append(dict(d["events"][1])),
            "bad tier": lambda d: d["events"][0].update(tier=4),
            "bad cat": lambda d: d["events"][0].update(cat="calendar"),
            "bad updated": lambda d: d.update(updated="yesterday"),
            "wrong year": lambda d: d.update(year=2027),
        }
        for label, mutate in cases.items():
            doc = self.good()
            mutate(doc)
            self.assertTrue(g.validate_feed(doc, "KR", 2026), msg=f"{label} should fail")

    def test_manifest_validation(self):
        good = {"version": 1, "updated": "2026-09-08", "countries": {
            "KR": {"name": "대한민국", "name_en": "South Korea", "years": [2026, 2027], "updated": "2026-09-08"}}}
        self.assertEqual(g.validate_manifest(good), [])
        self.assertTrue(g.validate_manifest({**good, "version": 2}))
        self.assertTrue(g.validate_manifest({**good, "countries": {}}))
        self.assertTrue(g.validate_manifest({**good, "countries": {"kr": good["countries"]["KR"]}}))
        self.assertTrue(g.validate_manifest({**good, "countries": {"KR": {**good["countries"]["KR"], "years": []}}}))


class AssemblyTests(unittest.TestCase):
    def test_updated_is_preserved_when_events_are_unchanged(self):
        events = g.holiday_events("KR", 2026, CONFIG["KR"])
        first = g.build_feed("KR", 2026, list(events), None, dt.date(2026, 9, 1))
        self.assertEqual(first["updated"], "2026-09-01")
        same = g.build_feed("KR", 2026, list(events), first, dt.date(2026, 9, 8))
        self.assertEqual(same["updated"], "2026-09-01")
        changed = g.build_feed("KR", 2026, events[:-1], first, dt.date(2026, 9, 8))
        self.assertEqual(changed["updated"], "2026-09-08")

    def test_events_are_sorted_by_calendar_position(self):
        doc = g.build_feed("KR", 2026, [
            {"id": "kr-pepero-day", "tier": 2, "cat": "event", "name": "x", "name_en": "x", "rule": "11-11", "interests": []},
            {"id": "kr-chuseok-2026", "tier": 1, "cat": "holiday", "name": "x", "name_en": "x", "start": "2026-09-24", "dayoff": True},
            {"id": "kr-suneung-2026", "tier": 3, "cat": "event", "name": "x", "name_en": "x", "start": "2026-11-19", "interests": [], "src": "https://x"},
        ], None, dt.date(2026, 9, 8))
        self.assertEqual([e["id"] for e in doc["events"]], ["kr-chuseok-2026", "kr-pepero-day", "kr-suneung-2026"])

    def test_end_to_end_generation_validates_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "v1"
            config = g.load_yaml(HERE / "countries.yaml")
            changed = g.generate(out, config, HERE / "curated", [2026, 2027], None, dt.date(2026, 9, 8), use_official=False)
            self.assertEqual(changed, 7)  # 3 countries x 2 years + manifest
            self.assertEqual(g.check_output(out), [])
            manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(list(manifest["countries"]), ["KR", "US", "JP"])
            self.assertEqual(manifest["countries"]["KR"]["years"], [2026, 2027])
            # A second run on a later day changes nothing: same events, same stamps, no writes.
            again = g.generate(out, config, HERE / "curated", [2026, 2027], None, dt.date(2026, 9, 15), use_official=False)
            self.assertEqual(again, 0)
            kr = json.loads((out / "KR" / "2026.json").read_text(encoding="utf-8"))
            self.assertEqual(kr["updated"], "2026-09-08")
            ids = [e["id"] for e in kr["events"]]
            self.assertIn("kr-chuseok-2026", ids)
            self.assertIn("kr-pepero-day", ids)
            self.assertIn("kr-suneung-2026", ids)
            # The written file carries the colloquial display names, with English untouched.
            names = {e["id"]: (e["name"], e["name_en"]) for e in kr["events"]}
            self.assertEqual(names["kr-new-years-day-2026"], ("신정", "New Year's Day"))
            self.assertEqual(names["kr-christmas-day-2026"], ("크리스마스", "Christmas Day"))

    def test_partial_run_keeps_other_countries_in_the_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "v1"
            config = g.load_yaml(HERE / "countries.yaml")
            g.generate(out, config, HERE / "curated", [2026], None, dt.date(2026, 9, 8), use_official=False)
            g.generate(out, config, HERE / "curated", [2026], ["KR"], dt.date(2026, 9, 9), use_official=False)
            manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(set(manifest["countries"]), {"KR", "US", "JP"})
            self.assertEqual(g.check_output(out), [])


if __name__ == "__main__":
    unittest.main()
