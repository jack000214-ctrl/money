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


class TestReportedQuartersBeatSubtraction(unittest.TestCase):
    """직접 보고된 3개월 값이 있으면 그것이 그 분기다.

    2분기 10-Q 안의 사실은 전부 fp=Q2 다 — 반기 누계도, 2분기 3개월 값도, 재작성된
    1분기 값도. 예전에는 fp=Q2 면 반기 누계 길이의 사실만 받아 `새 반기 누계 −
    옛 1분기` 로 2분기를 만들었다. 재작성 1분기 8·2분기 22·반기 30 이 있어도 옛
    1분기 10 을 빼 2분기 20, 전년 대비 +10% 가 0% 가 됐다. 재무 항등식·원문 재생·
    최신성 검사는 전부 통과했다 — 재생은 같은 함수를 다시 돌릴 뿐이다.
    """

    def history(self):
        rows = []
        for year in (2024, 2025):
            rows += [
                point(year, "Q1", f"{year}-01-01", f"{year}-03-31", 10, "10-Q", f"{year}-05-01"),
                point(year, "Q2", f"{year}-01-01", f"{year}-06-30", 30, "10-Q", f"{year}-08-01"),
                point(year, "Q2", f"{year}-04-01", f"{year}-06-30", 20, "10-Q", f"{year}-08-01"),
                point(year, "Q3", f"{year}-01-01", f"{year}-09-30", 60, "10-Q", f"{year}-11-01"),
                point(year, "Q3", f"{year}-07-01", f"{year}-09-30", 30, "10-Q", f"{year}-11-01"),
                point(year, "FY", f"{year}-01-01", f"{year}-12-31", 100, "10-K", f"{year + 1}-02-01"),
            ]
        return rows + [
            point(2026, "Q1", "2026-01-01", "2026-03-31", 10, "10-Q", "2026-05-01"),
            point(2026, "Q2", "2026-01-01", "2026-06-30", 30, "10-Q", "2026-08-01"),
            point(2026, "Q2", "2026-04-01", "2026-06-30", 22, "10-Q", "2026-08-01"),
        ]

    def quarters(self, points):
        return {(row["fy"], row["quarter"]): row for row in fiscal_quarters(points)}

    def test_the_restated_first_quarter_carried_by_the_later_filing_is_used(self):
        """2분기 10-Q 가 재작성된 1분기(8)를 직접 싣는다. fp 는 Q2 지만 기간은 1분기다."""
        points = self.history() + [
            point(2026, "Q2", "2026-01-01", "2026-03-31", 8, "10-Q", "2026-08-01")]
        rows = self.quarters(points)
        self.assertEqual(rows[(2026, 1)]["val"], 8)
        self.assertEqual(rows[(2026, 2)]["val"], 22)
        self.assertFalse(rows[(2026, 2)]["derived"])
        growth = (rows[(2026, 2)]["val"] / rows[(2025, 2)]["val"] - 1) * 100
        self.assertAlmostEqual(growth, 10.0)

    def test_an_unrestated_first_quarter_is_reissued_from_the_later_filing(self):
        """10-Q 는 보통 1분기를 다시 싣지 않는다. 반기 누계 30 과 2분기 22 가 나중
        서류의 값이고 옛 1분기 10 과 맞지 않으면, 1분기는 30 − 22 = 8 이다."""
        rows = self.quarters(self.history())
        self.assertEqual(rows[(2026, 2)]["val"], 22)
        first = rows[(2026, 1)]
        self.assertEqual(first["val"], 8)
        self.assertTrue(first["derived"])
        self.assertEqual(first["restated_from"], 10)
        self.assertEqual(first["end"], "2026-03-31")
        self.assertEqual(first["filed"], "2026-08-01")
        # 재작성이 없던 해에는 표시가 붙지 않는다.
        self.assertNotIn("restated_from", rows[(2025, 1)])
        self.assertFalse(rows[(2025, 1)]["derived"])

    def test_a_rounding_step_between_quarters_and_the_half_is_not_a_restatement(self):
        """백만 단위로 반올림된 값은 1분기 + 2분기가 반기와 한 단위 어긋날 수 있다."""
        points = [
            point(2026, "Q1", "2026-01-01", "2026-03-31", 14_541_000_000, "10-Q", "2026-05-01"),
            point(2026, "Q2", "2026-01-01", "2026-06-30", 31_519_000_000, "10-Q", "2026-08-01"),
            point(2026, "Q2", "2026-04-01", "2026-06-30", 16_977_000_000, "10-Q", "2026-08-01"),
        ]
        rows = self.quarters(points)
        self.assertEqual(rows[(2026, 1)]["val"], 14_541_000_000)
        self.assertNotIn("restated_from", rows[(2026, 1)])
        self.assertEqual(rows[(2026, 2)]["val"], 16_977_000_000)

    def test_a_first_quarter_filed_after_the_half_is_the_newer_figure(self):
        """다음 해 1분기 10-Q 의 비교표가 1분기를 고쳤다면 그쪽이 나중 값이다."""
        points = self.history() + [
            point(2027, "Q1", "2026-01-01", "2026-03-31", 9, "10-Q", "2027-05-01"),
            point(2027, "Q1", "2027-01-01", "2027-03-31", 12, "10-Q", "2027-05-01")]
        first = self.quarters(points)[(2026, 1)]
        self.assertEqual(first["val"], 9)
        self.assertNotIn("restated_from", first)

    def test_a_missing_first_quarter_is_recovered_from_the_half_and_the_second(self):
        points = [row for row in self.history()
                  if not (row["fy"] == 2026 and row["fp"] == "Q1")]
        first = self.quarters(points)[(2026, 1)]
        self.assertEqual((first["val"], first["derived"], first["end"]),
                         (8, True, "2026-03-31"))

    def test_the_prior_year_quarter_in_a_comparative_column_replaces_the_old_one(self):
        """올해 2분기 10-Q 는 전년 2분기 3개월 값도 싣는다(재분류 반영)."""
        points = self.history() + [
            point(2026, "Q2", "2025-04-01", "2025-06-30", 19, "10-Q", "2026-08-01")]
        self.assertEqual(self.quarters(points)[(2025, 2)]["val"], 19)

    def test_series_that_only_report_cumulative_values_are_still_differenced(self):
        """현금흐름표는 대개 누계만 있다. 그때는 예전처럼 차감한다."""
        points = [row for row in self.history()
                  if row["start"].endswith("-01-01")]
        rows = self.quarters(points)
        self.assertEqual(rows[(2025, 2)]["val"], 20)
        self.assertTrue(rows[(2025, 2)]["derived"])
        self.assertEqual(rows[(2025, 4)]["val"], 40)

    def test_a_three_month_span_that_is_not_a_fiscal_quarter_is_ignored(self):
        points = self.history() + [
            point(2026, "Q2", "2026-02-15", "2026-05-15", 999, "10-Q", "2026-08-01")]
        self.assertEqual(fiscal_quarters(points), fiscal_quarters(self.history()))

    def test_the_order_of_facts_does_not_change_the_quarters(self):
        points = self.history() + [
            point(2026, "Q2", "2026-01-01", "2026-03-31", 8, "10-Q", "2026-08-01")]
        self.assertEqual(fiscal_quarters(list(reversed(points))), fiscal_quarters(points))

    def test_the_trailing_total_is_the_sum_of_the_reported_quarters(self):
        value, quarters = trailing_four_quarters(self.history())
        self.assertEqual([(row["fy"], row["quarter"]) for row in quarters],
                         [(2025, 3), (2025, 4), (2026, 1), (2026, 2)])
        self.assertEqual(value, 30 + 40 + 8 + 22)


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
