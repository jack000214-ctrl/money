"""SEC 누계 현금흐름의 분기·TTM 복원 테스트."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from collectors import edgar
from collectors.edgar import fiscal_quarters, trailing_four_quarters
from helpers import point


class TestFiscalCashFlow(unittest.TestCase):
    def setUp(self):
        self.points = [
            point(2025, "Q1", "2025-01-01", "2025-03-31", 10, "10-Q", "2025-05-01"),
            point(2025, "Q2", "2025-01-01", "2025-06-30", 25, "10-Q", "2025-08-01"),
            point(2025, "Q3", "2025-01-01", "2025-09-30", 45, "10-Q", "2025-11-01"),
            point(2025, "FY", "2025-01-01", "2025-12-31", 70, "10-K", "2026-02-01"),
        ]

    def test_ytd_values_are_differenced_into_quarters(self):
        quarters = fiscal_quarters(self.points)
        self.assertEqual([p["val"] for p in quarters], [10, 15, 20, 25])

    def test_ttm_requires_four_consecutive_quarters(self):
        value, quarters = trailing_four_quarters(self.points)
        self.assertEqual(value, 70)
        self.assertEqual([(p["fy"], p["quarter"]) for p in quarters],
                         [(2025, 1), (2025, 2), (2025, 3), (2025, 4)])

    def test_missing_q3_prevents_fake_ttm(self):
        points = [p for p in self.points if p["fp"] != "Q3"]
        value, _ = trailing_four_quarters(points)
        self.assertIsNone(value)

    def test_latest_amendment_wins(self):
        points = self.points + [
            point(2025, "Q2", "2025-01-01", "2025-06-30", 27, "10-Q", "2025-08-15")
        ]
        quarters = fiscal_quarters(points)
        self.assertEqual([p["val"] for p in quarters], [10, 17, 18, 25])

    def test_comparative_restatement_keeps_its_actual_year(self):
        # 2026 Q2에 함께 나온 2025 비교표는 fy=2026이지만 실제 기간은 2025다.
        points = self.points + [
            point(2026, "Q1", "2025-01-01", "2025-03-31", 12, "10-Q", "2026-05-01"),
            point(2026, "Q1", "2026-01-01", "2026-03-31", 20, "10-Q", "2026-05-01"),
            point(2026, "Q2", "2025-01-01", "2025-06-30", 30, "10-Q", "2026-08-01"),
            point(2026, "Q2", "2026-01-01", "2026-06-30", 50, "10-Q", "2026-08-01"),
        ]
        expected = fiscal_quarters(points)
        self.assertEqual([p["val"] for p in expected], [12, 18, 15, 25, 20, 30])
        self.assertEqual(fiscal_quarters(list(reversed(points))), expected)
        self.assertEqual(trailing_four_quarters(points)[0], 90)

    def test_amended_form_updates_the_original_period(self):
        points = self.points + [
            point(2025, "Q2", "2025-01-01", "2025-06-30", 27, "10-Q/A", "2025-08-15")
        ]
        self.assertEqual([p["val"] for p in fiscal_quarters(points)], [10, 17, 18, 25])

    def test_fiscal_year_does_not_have_to_end_in_december(self):
        points = [
            point(2025, "Q1", "2024-07-01", "2024-09-30", 10),
            point(2025, "Q2", "2024-07-01", "2024-12-31", 25),
            point(2025, "Q3", "2024-07-01", "2025-03-31", 45),
            point(2025, "FY", "2024-07-01", "2025-06-30", 70, "10-K"),
        ]
        value, quarters = trailing_four_quarters(points)
        self.assertEqual(value, 70)
        self.assertEqual({p["fy"] for p in quarters}, {2025})

    def test_mismatched_ytd_starts_cannot_be_subtracted(self):
        points = self.points + [
            point(2025, "Q2", "2025-01-02", "2025-06-30", 27, "10-Q/A", "2025-08-15")
        ]
        self.assertIsNone(trailing_four_quarters(points)[0])


class TestFactSelection(unittest.TestCase):
    def test_as_of_is_applied_before_choosing_a_replacement_tag(self):
        responses = [
            {"units": {"USD": [{"end": "2025-12-31", "filed": "2026-02-01", "val": 10}]}},
            {"units": {"USD": [{"end": "2026-03-31", "filed": "2026-05-01", "val": 20}]}},
        ]
        with patch.object(edgar, "get", side_effect=responses), patch.object(edgar.time, "sleep"):
            tag, points = edgar.concept("0001", ["Old", "New"], "test", as_of="2026-03-01")
        self.assertEqual(tag, "Old")
        self.assertEqual(points[0]["val"], 10)

    def test_instant_and_quarterly_latest_filing_do_not_depend_on_array_order(self):
        current = point(2025, "Q1", "2025-01-01", "2025-03-31", 12, filed="2025-05-10")
        prior = {**current, "val": 10, "filed": "2025-05-01"}
        self.assertEqual(edgar.quarterly([current, prior])[0]["val"], 12)
        self.assertEqual(edgar.instant([{**current, "start": None}, {**prior, "start": None}])[0]["val"], 12)


class TestTickerResolution(unittest.TestCase):
    def test_cache_miss_forces_one_registry_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            cache_dir.joinpath("edgar_tickers.json").write_text(
                json.dumps(
                    {"OLD": {"cik": "0000000001", "name": "Old Corp"}}
                ),
                encoding="utf-8",
            )
            refreshed = {
                "0": {
                    "ticker": "NEW",
                    "cik_str": 2,
                    "title": "New Corp",
                }
            }
            with (
                patch.object(edgar, "CACHE_DIR", cache_dir),
                patch.object(edgar, "get", return_value=refreshed) as fetch,
            ):
                result = edgar.resolve("NEW", "Fixture fixture@example.com")
        self.assertEqual(result["cik"], "0000000002")
        fetch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
