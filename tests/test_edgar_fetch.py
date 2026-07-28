"""SEC 누계 현금흐름의 분기·TTM 복원 테스트."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from collectors import edgar
from collectors.edgar import fiscal_quarters, trailing_four_quarters


def point(fy, fp, start, end, value, form, filed):
    return {
        "fy": fy, "fp": fp, "start": start, "end": end, "val": value,
        "form": form, "filed": filed,
    }


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
