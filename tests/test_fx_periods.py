"""환율을 결산 분기에 접는 산술의 계약.

2026-10-06 판단은 수출 비중 47% 회사에서 환율을 통째로 빠뜨렸고, 일주일 뒤
모델이 스크래치 스크립트로 분기 평균을 직접 냈다. 그 산술을 PC 로 옮긴 것이다.

지키는 성질은 넷이다.
  1. 평균은 그 분기의 봉만으로 낸다 — 기말 다음 날의 봉이 섞이지 않는다
  2. 봉이 모자란 분기는 근사하지 않고 뺀다
  3. 끝났지만 아직 실적에 없는 분기를 구분한다 (`reported: false`)
  4. 표에서 인접하지 않은 분기끼리 '전분기 대비'를 내지 않는다
"""

import datetime as dt
import unittest

import helpers  # noqa: F401
from calculators.fx_periods import quarter_start, quarter_table


def weekdays(start: str, end: str, close: float):
    day, last = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    out = []
    while day <= last:
        if day.weekday() < 5:
            out.append((day.isoformat(), close))
        day += dt.timedelta(days=1)
    return out


SERIES = (
    weekdays("2025-07-01", "2025-09-30", 1380.0)
    + weekdays("2025-10-01", "2025-12-31", 1450.0)
    + weekdays("2026-01-01", "2026-03-31", 1460.0)
    + weekdays("2026-04-01", "2026-06-29", 1500.0)
    + [("2026-06-30", 1548.0)]
    + weekdays("2026-07-01", "2026-09-29", 1420.0)
    + [("2026-09-30", 1356.0)]
    + weekdays("2026-10-01", "2026-10-13", 1336.0)
)
ENDS = ["2026-03-31", "2026-06-30", "2026-09-30"]


class TestQuarterBoundaries(unittest.TestCase):
    def test_quarter_starts_the_day_after_the_previous_quarter_ends(self):
        self.assertEqual(quarter_start(dt.date(2026, 6, 30)), dt.date(2026, 4, 1))
        self.assertEqual(quarter_start(dt.date(2026, 3, 31)), dt.date(2026, 1, 1))
        self.assertEqual(quarter_start(dt.date(2025, 12, 31)), dt.date(2025, 10, 1))

    def test_average_uses_only_bars_inside_the_quarter(self):
        table = quarter_table(SERIES, ENDS, reported_through="2026-06-30")
        second = next(row for row in table["quarters"] if row["end"] == "2026-06-30")
        # 64개 봉이 1,500 이고 기말 하루가 1,548 이다. 7월 1일의 1,420 은 들어가면 안 된다.
        self.assertEqual(second["bars"], 65)
        self.assertEqual(second["average"], round((1500.0 * 64 + 1548.0) / 65, 1))
        self.assertEqual(second["period_end"], 1548.0)


class TestChanges(unittest.TestCase):
    def setUp(self):
        self.table = quarter_table(SERIES, ENDS, reported_through="2026-06-30")
        self.rows = {row["end"]: row for row in self.table["quarters"]}

    def test_quarter_over_quarter_is_reported_for_both_average_and_period_end(self):
        third = self.rows["2026-09-30"]
        second = self.rows["2026-06-30"]
        self.assertEqual(
            third["average_qoq_pct"],
            round((third["average"] / second["average"] - 1) * 100, 2))
        self.assertEqual(third["period_end_qoq_pct"], round((1356.0 / 1548.0 - 1) * 100, 2))

    def test_year_over_year_uses_the_same_quarter_a_year_earlier(self):
        third = self.rows["2026-09-30"]
        self.assertEqual(
            third["average_yoy_pct"], round((third["average"] / 1380.0 - 1) * 100, 2))

    def test_a_quarter_without_a_year_ago_quarter_has_no_yoy(self):
        self.assertNotIn("average_yoy_pct", self.rows["2026-03-31"])

    def test_the_first_row_has_no_quarter_over_quarter(self):
        self.assertNotIn("average_qoq_pct", self.rows["2026-03-31"])

    def test_rows_that_are_not_adjacent_do_not_get_a_qoq(self):
        table = quarter_table(SERIES, ["2026-03-31", "2026-09-30"],
                              reported_through="2026-03-31")
        third = next(row for row in table["quarters"] if row["end"] == "2026-09-30")
        self.assertNotIn("average_qoq_pct", third)


class TestReportedFlag(unittest.TestCase):
    def test_a_quarter_that_ended_after_coverage_is_marked_unreported(self):
        table = quarter_table(SERIES, ENDS, reported_through="2026-06-30")
        flags = {row["end"]: row["reported"] for row in table["quarters"]}
        self.assertEqual(flags, {
            "2026-03-31": True, "2026-06-30": True, "2026-09-30": False})

    def test_latest_is_compared_with_the_last_reported_quarter(self):
        table = quarter_table(SERIES, ENDS, reported_through="2026-06-30")
        versus = table["latest_vs_last_reported_quarter"]
        second = next(row for row in table["quarters"] if row["end"] == "2026-06-30")
        self.assertEqual(versus["end"], "2026-06-30")
        self.assertEqual(
            versus["vs_average_pct"], round((1336.0 / second["average"] - 1) * 100, 2))
        self.assertEqual(versus["vs_period_end_pct"], round((1336.0 / 1548.0 - 1) * 100, 2))
        self.assertEqual(table["latest"], {"date": "2026-10-13", "close": 1336.0})

    def test_without_a_coverage_date_nothing_is_called_reported(self):
        table = quarter_table(SERIES, ENDS, reported_through=None)
        self.assertTrue(all("reported" not in row for row in table["quarters"]))
        self.assertIsNone(table["latest_vs_last_reported_quarter"])


class TestWhatIsNotComputed(unittest.TestCase):
    def test_a_quarter_still_in_progress_is_left_out(self):
        table = quarter_table(SERIES, [*ENDS, "2026-12-31"], reported_through="2026-06-30")
        self.assertNotIn("2026-12-31", [row["end"] for row in table["quarters"]])

    def test_a_quarter_with_too_few_bars_is_left_out_not_approximated(self):
        thin = [row for row in SERIES if not "2026-04-15" <= row[0] <= "2026-06-20"]
        table = quarter_table(thin, ENDS, reported_through="2026-06-30")
        ends = [row["end"] for row in table["quarters"]]
        self.assertNotIn("2026-06-30", ends)
        third = next(row for row in table["quarters"] if row["end"] == "2026-09-30")
        self.assertNotIn("average_qoq_pct", third, "빠진 분기를 건너뛰어 전분기 대비를 냈다")

    def test_an_empty_series_gives_nothing(self):
        self.assertIsNone(quarter_table([], ENDS, reported_through="2026-06-30"))
        self.assertIsNone(quarter_table(SERIES, [], reported_through="2026-06-30"))


if __name__ == "__main__":
    unittest.main()
