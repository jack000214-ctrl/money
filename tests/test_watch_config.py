"""감시 설정과 장 시간 게이트."""
from __future__ import annotations

import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cio import watch_config as wc

KST = ZoneInfo("Asia/Seoul")


def at(day: str, clock: str) -> dt.datetime:
    return dt.datetime.fromisoformat(f"{day}T{clock}:00").replace(tzinfo=KST)


class TestGate(unittest.TestCase):
    config = wc._fallback_defaults()
    # 2026-10-19 월요일, 2026-10-17 토요일, 2026-10-18 일요일

    def test_window_boundaries(self):
        cases = {
            "07:59": wc.SKIP,
            "08:00": wc.RUN,
            "19:59": wc.RUN,
            "20:00": wc.SKIP,
        }
        for clock, expected in cases.items():
            with self.subTest(clock):
                self.assertEqual(
                    wc.gate(at("2026-10-19", clock), self.config)["decision"], expected
                )

    def test_skip_reasons(self):
        self.assertEqual(wc.gate(at("2026-10-19", "07:00"), self.config)["reason"], "BEFORE_WINDOW")
        self.assertEqual(wc.gate(at("2026-10-19", "21:00"), self.config)["reason"], "AFTER_WINDOW")

    def test_weekend_is_skipped_even_inside_the_window(self):
        for day in ("2026-10-17", "2026-10-18"):
            with self.subTest(day):
                result = wc.gate(at(day, "12:00"), self.config)
                self.assertEqual(result["decision"], wc.SKIP)
                self.assertEqual(result["reason"], "WEEKEND")

    def test_other_timezone_is_converted_to_the_configured_zone(self):
        utc_noon = dt.datetime(2026, 10, 19, 3, 0, tzinfo=dt.timezone.utc)  # 12:00 KST
        self.assertEqual(wc.gate(utc_noon, self.config)["decision"], wc.RUN)
        utc_late = dt.datetime(2026, 10, 19, 12, 0, tzinfo=dt.timezone.utc)  # 21:00 KST
        self.assertEqual(wc.gate(utc_late, self.config)["decision"], wc.SKIP)

    def test_seconds_do_not_move_the_boundary(self):
        late = dt.datetime(2026, 10, 19, 19, 59, 59, tzinfo=KST)
        self.assertEqual(wc.gate(late, self.config)["decision"], wc.RUN)

    def test_interval_change_needs_one_value(self):
        config = json.loads(json.dumps(self.config))
        config["interval_minutes"] = 10
        self.assertEqual(wc.validate_config(config), [])


class TestValidation(unittest.TestCase):
    def valid(self):
        return json.loads(json.dumps(wc._fallback_defaults()))

    def test_default_is_valid(self):
        self.assertEqual(wc.validate_config(self.valid()), [])

    def test_rejects_bad_time_format(self):
        config = self.valid()
        config["window"]["start"] = "8:00"
        self.assertTrue(wc.validate_config(config))

    def test_start_must_precede_end(self):
        config = self.valid()
        config["window"]["start"] = "20:00"
        config["window"]["end"] = "08:00"
        self.assertTrue(any("이르다" in e for e in wc.validate_config(config)))

    def test_interval_must_be_positive_integer(self):
        for bad in (0, -5, 10.5, True, "10", 2000):
            with self.subTest(bad):
                config = self.valid()
                config["interval_minutes"] = bad
                self.assertTrue(wc.validate_config(config))

    def test_us_market_is_refused(self):
        config = self.valid()
        config["markets"] = ["KR", "US"]
        self.assertTrue(wc.validate_config(config))

    def test_unknown_timezone_is_refused(self):
        config = self.valid()
        config["timezone"] = "Mars/Base"
        self.assertTrue(wc.validate_config(config))

    def test_recipient_address_cannot_inject_headers(self):
        config = self.valid()
        config["email"]["recipients"] = [{"address": "a@b.com\r\nBcc: x@y.com", "enabled": True}]
        self.assertTrue(wc.validate_config(config))

    def test_review_null_is_allowed_but_zero_is_not(self):
        config = self.valid()
        config["review"]["stale_after_days"] = 0
        self.assertTrue(wc.validate_config(config))
        config["review"]["stale_after_days"] = None
        self.assertEqual(wc.validate_config(config), [])


class TestLoadSave(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "watch.json"

    def test_missing_file_gives_defaults_and_unset_review_is_reported(self):
        config = wc.load_config(self.path)
        self.assertEqual(config["interval_minutes"], 60)
        self.assertFalse(config["channels"]["email"])
        self.assertEqual(
            wc.unset_review_fields(config), list(wc.REVIEW_FIELDS),
            "미정 항목에 기본값을 채우면 안 된다",
        )

    def test_save_changes_only_what_was_given(self):
        saved = wc.save_config({"interval_minutes": 10}, self.path)
        self.assertEqual(saved["interval_minutes"], 10)
        self.assertEqual(wc.load_config(self.path)["window"]["start"], "08:00")

    def test_invalid_save_writes_nothing(self):
        with self.assertRaises(wc.WatchConfigError):
            wc.save_config({"interval_minutes": 0}, self.path)
        self.assertFalse(self.path.exists())

    def test_recipients_are_replaced_not_merged(self):
        wc.save_config(
            {"email": {"recipients": [{"name": "a", "address": "a@b.com", "enabled": True},
                                      {"name": "c", "address": "c@d.com", "enabled": True}]}},
            self.path,
        )
        saved = wc.save_config(
            {"email": {"recipients": [{"name": "c", "address": "c@d.com", "enabled": True}]}},
            self.path,
        )
        self.assertEqual([r["address"] for r in saved["email"]["recipients"]], ["c@d.com"])

    def test_unknown_keys_are_ignored(self):
        saved = wc.save_config({"smtp_password": "secret"}, self.path)
        self.assertNotIn("smtp_password", saved)
        self.assertNotIn("secret", self.path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
