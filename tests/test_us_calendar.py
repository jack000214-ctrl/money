"""미국 실적 캘린더 추정의 계약.

미국에는 결산실적 공시예고 제도가 없다. 그래서 이 모듈은 과거 (결산기말 →
10-Q/10-K 제출) 간격의 중앙값으로 다음 제출일을 **추정한다**. 추정을 확정일처럼
쓰면 판단이 없는 이벤트에 걸리므로, 지켜야 할 것은 정확도가 아니라 **정직성**이다.

  1. 관측이 2건 미만인 부류는 추정하지 않고 그 이유를 남긴다
  2. 10-Q(INTERIM)와 10-K(ANNUAL)는 법정 기한이 달라 따로 센다
  3. 정정·지연 제출로 보이는 이상치는 표본에서 뺀다
"""

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import helpers  # noqa: F401  (저장소 루트를 sys.path 에 올린다)
from collectors import us_calendar
from collectors.us_calendar import MAX_LAG_DAYS, _quarter_end_after, filing_lag


def filing(form, period_end, filed):
    return {"form": form, "report_date": period_end, "filing_date": filed}


class TestQuarterEndAfter(unittest.TestCase):
    def test_next_quarter_end_is_the_last_day_of_the_month(self):
        cases = {
            "2026-03-31": "2026-06-30",
            "2026-06-30": "2026-09-30",
            "2026-09-30": "2026-12-31",
            "2026-12-31": "2027-03-31",
        }
        for given, expected in cases.items():
            with self.subTest(given):
                result = _quarter_end_after(dt.date.fromisoformat(given))
                self.assertEqual(result.isoformat(), expected)

    def test_february_follows_the_leap_rule(self):
        self.assertEqual(
            _quarter_end_after(dt.date(2023, 11, 30)).isoformat(), "2024-02-29"
        )
        self.assertEqual(
            _quarter_end_after(dt.date(2024, 11, 30)).isoformat(), "2025-02-28"
        )

    def test_century_years_follow_the_400_rule(self):
        """2100년은 윤년이 아니다 — 4로 나눠떨어진다고 넣으면 없는 날짜가 된다."""
        self.assertEqual(
            _quarter_end_after(dt.date(2099, 11, 30)).isoformat(), "2100-02-28"
        )


class TestFilingLag(unittest.TestCase):
    def test_interim_and_annual_are_counted_separately(self):
        """법정 기한이 달라 섞으면 둘 다 틀린 중앙값이 나온다."""
        lag = filing_lag([
            filing("10-Q", "2026-03-31", "2026-05-10"),   # 40일
            filing("10-Q", "2026-06-30", "2026-08-09"),   # 40일
            filing("10-K", "2025-12-31", "2026-03-01"),   # 60일
            filing("10-K", "2024-12-31", "2025-03-01"),   # 60일
        ])
        self.assertEqual(lag["median_lag_days_by_class"]["INTERIM"], 40)
        self.assertEqual(lag["median_lag_days_by_class"]["ANNUAL"], 60)
        self.assertEqual(lag["sample_size_by_class"], {"ANNUAL": 2, "INTERIM": 2})

    def test_non_periodic_forms_are_ignored(self):
        """8-K 는 실적 요약이지 정기보고서가 아니다."""
        lag = filing_lag([filing("8-K", "2026-03-31", "2026-04-20")])
        self.assertEqual(lag["observation_count"], 0)

    def test_an_absurdly_late_filing_is_dropped_from_the_sample(self):
        late = (dt.date(2026, 3, 31) + dt.timedelta(days=MAX_LAG_DAYS + 1)).isoformat()
        lag = filing_lag([filing("10-Q", "2026-03-31", late)])
        self.assertEqual(lag["observation_count"], 0)

    def test_a_filing_before_its_period_end_is_dropped(self):
        """음수 간격은 관측이 아니라 데이터 오류다."""
        lag = filing_lag([filing("10-Q", "2026-06-30", "2026-05-01")])
        self.assertEqual(lag["observation_count"], 0)

    def test_unparseable_dates_are_skipped_rather_than_guessed(self):
        lag = filing_lag([
            filing("10-Q", None, "2026-05-10"),
            filing("10-Q", "2026-03-31", "날짜아님"),
        ])
        self.assertEqual(lag["observation_count"], 0)

    def test_only_the_last_six_observations_are_carried(self):
        rows = [
            filing("10-Q", f"{year}-03-31", f"{year}-05-01")
            for year in range(2015, 2026)
        ]
        lag = filing_lag(rows)
        self.assertEqual(lag["observation_count"], 11)
        self.assertEqual(len(lag["observations"]), 6)

    def test_duplicate_periods_do_not_manufacture_a_sample(self):
        row = filing("10-Q", "2026-03-31", "2026-05-10")
        lag = filing_lag([row, dict(row)])
        self.assertEqual(lag["sample_size_by_class"]["INTERIM"], 1)


class TestBuild(unittest.TestCase):
    def build_with(self, filings, today="2026-08-08"):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "TSLA.json").write_text(
                json.dumps({"filings": filings}), encoding="utf-8"
            )
            with patch.object(us_calendar, "FILING_DIR", directory):
                return us_calendar.build("TSLA", today=dt.date.fromisoformat(today))

    def test_a_single_observation_refuses_to_estimate_and_says_why(self):
        """관측 1건으로 중앙값을 내면 그 하나가 곧 예측이 된다."""
        packet = self.build_with([
            filing("10-K", "2025-12-31", "2026-03-01"),
            filing("10-Q", "2026-03-31", "2026-05-10"),
        ])
        self.assertIsNone(packet["expected_periodic_report"])
        self.assertIn("2건 미만", packet["expected_periodic_report_missing_reason"])

    def test_two_observations_produce_an_estimate_marked_as_estimated(self):
        packet = self.build_with([
            filing("10-K", "2025-12-31", "2026-03-01"),
            filing("10-Q", "2025-09-30", "2025-11-09"),   # 40일
            filing("10-Q", "2026-03-31", "2026-05-10"),   # 40일
        ], today="2026-11-06")
        expected = packet["expected_periodic_report"]
        self.assertTrue(expected["estimated"])
        self.assertEqual(expected["report_class"], "INTERIM")
        self.assertEqual(expected["period_end"], "2026-06-30")
        self.assertEqual(expected["date"], "2026-08-09")
        self.assertEqual(expected["sample_size"], 2)

    def test_a_december_period_end_is_classified_as_annual(self):
        packet = self.build_with([
            filing("10-K", "2024-12-31", "2025-03-01"),
            filing("10-K", "2025-12-31", "2026-03-01"),
            filing("10-Q", "2026-09-30", "2026-11-05"),
        ], today="2026-11-06")
        self.assertEqual(
            packet["expected_periodic_report"]["report_class"], "ANNUAL"
        )

    def test_provisional_result_is_absent_with_a_stated_reason(self):
        """비어 있는 것과 '제도가 없다'는 다르다. 빈칸을 '해당 없음'으로 읽지 않는다."""
        packet = self.build_with([filing("10-Q", "2026-03-31", "2026-05-10")])
        self.assertIsNone(packet["next_provisional_result"])
        self.assertIn("별도 확인", packet["next_provisional_result_missing_reason"])

    def test_june_fiscal_year_end_uses_annual_lag(self):
        packet = self.build_with([
            filing("10-K", "2024-06-30", "2024-08-29"),
            filing("10-K", "2025-06-30", "2025-08-29"),
            filing("10-Q", "2026-03-31", "2026-05-10"),
        ])
        expected = packet["expected_periodic_report"]
        self.assertEqual(expected["report_class"], "ANNUAL")
        self.assertEqual(expected["date"], "2026-08-29")

    def test_unknown_fiscal_year_end_is_not_assumed_to_be_december(self):
        packet = self.build_with([
            filing("10-Q", "2025-09-30", "2025-11-09"),
            filing("10-Q", "2025-12-31", "2026-02-09"),
        ])
        self.assertIsNone(packet["expected_periodic_report"])
        self.assertIn("10-K", packet["expected_periodic_report_missing_reason"])

    def test_week_based_calendar_does_not_create_a_month_end_date(self):
        packet = self.build_with([
            filing("10-K", "2025-01-26", "2025-03-01"),
            filing("10-Q", "2025-04-27", "2025-05-28"),
            filing("10-Q", "2025-07-27", "2025-08-27"),
        ])
        self.assertIsNone(packet["expected_periodic_report"])
        self.assertIn("52/53주", packet["expected_periodic_report_missing_reason"])

    def test_future_filing_does_not_move_the_reported_period(self):
        packet = self.build_with([
            filing("10-K", "2025-12-31", "2026-03-01"),
            filing("10-Q", "2025-09-30", "2025-11-09"),
            filing("10-Q", "2026-03-31", "2026-05-10"),
            filing("10-Q", "2026-06-30", "2026-08-09"),
        ], today="2026-08-08")
        self.assertEqual(packet["expected_periodic_report"]["period_end"], "2026-06-30")

    def test_missing_filing_index_stops_with_a_recipe(self):
        """색인이 없으면 빈 캘린더를 만들지 않고 멈춘다.

        임시 폴더가 아니라 저장소 안의 없는 경로를 쓴다 — 오류 문구가
        `relative_to(ROOT)` 로 만들어지므로 저장소 밖 경로는 실제로 일어나지
        않는 상황이고, 그걸 흉내 내면 엉뚱한 예외를 검증하게 된다.
        """
        absent = helpers.ROOT / "knowledge" / "filings" / "없는하위폴더"
        with patch.object(us_calendar, "FILING_DIR", absent):
            with self.assertRaises(SystemExit):
                us_calendar.build("TSLA", today=dt.date(2026, 8, 8))


if __name__ == "__main__":
    unittest.main()
