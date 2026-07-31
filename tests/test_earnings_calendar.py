"""결산실적 공시예고 파싱과 정기보고서 예상일 도출."""
from __future__ import annotations

import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from collectors.kr_calendar import (
    business_days_until,
    expected_report_class,
    parse_notice,
    periodic_lag,
    strip_markup,
)

NOTICE_XML = """
<table><tbody>
 <tr><td><span>1. 회사명</span></td>
     <td><span class="xforms_input">주식회사 파마리서치</span></td></tr>
 <tr><td><span>2. 결산대상기간</span></td><td><span>시작일</span></td>
     <td><span class="xforms_input">2026-01-01</span></td></tr>
 <tr><td><span>종료일</span></td>
     <td><span class="xforms_input">2026-06-30</span></td></tr>
 <tr><td><span>3. 결산실적 공시예정일</span></td>
     <td><span class="xforms_input">2026-08-07</span></td></tr>
 <tr><td><span>4. 기타 투자판단에 참고할 사항</span></td>
     <td><span>- 상기 결산실적 공시예정일은 당사의 사정에 따라
     변경될 수 있습니다.</span></td></tr>
</tbody></table>
"""


class TestParseNotice(unittest.TestCase):
    def test_reads_disclosure_date_and_period(self):
        parsed = parse_notice({"a.xml": NOTICE_XML})
        self.assertEqual(parsed["disclosure_date"], "2026-08-07")
        self.assertEqual(parsed["period_start"], "2026-01-01")
        self.assertEqual(parsed["period_end"], "2026-06-30")

    def test_never_reports_the_date_as_confirmed(self):
        # 원문이 스스로 바뀔 수 있다고 말한다. 확정으로 올리면 안 된다.
        parsed = parse_notice({"a.xml": NOTICE_XML})
        self.assertFalse(parsed["is_confirmed"])
        self.assertIn("변경될 수 있습니다", str(parsed["source_caveat"]))

    def test_korean_date_format_is_read(self):
        xml = NOTICE_XML.replace("2026-08-07", "2026년 8월 7일")
        self.assertEqual(
            parse_notice({"a.xml": xml})["disclosure_date"], "2026-08-07"
        )

    def test_missing_field_raises_instead_of_guessing(self):
        xml = NOTICE_XML.replace("3. 결산실적 공시예정일", "3. 삭제됨")
        with self.assertRaises(LookupError):
            parse_notice({"a.xml": xml})

    def test_label_window_does_not_reach_across_the_document(self):
        # 라벨 뒤에 값이 없으면 멀리 있는 날짜를 주워오지 않는다.
        xml = (
            "<span>3. 결산실적 공시예정일</span>"
            + "<span>x</span>" * 60
            + "<span>2026-08-07</span>"
        )
        with self.assertRaises(LookupError):
            parse_notice({"a.xml": xml})

    def test_strip_markup_collapses_whitespace(self):
        self.assertEqual(strip_markup("<b>가</b>\n\n <i>나</i>"), "가 나")


class TestBusinessDays(unittest.TestCase):
    def test_counts_weekdays_only(self):
        # 2026-07-31 금 → 2026-08-07 금 = 월화수목금 5영업일
        self.assertEqual(
            business_days_until("2026-08-07", dt.date(2026, 7, 31)), 5
        )

    def test_past_date_is_none_not_negative(self):
        self.assertIsNone(
            business_days_until("2026-07-01", dt.date(2026, 7, 31))
        )

    def test_same_day_is_zero(self):
        self.assertEqual(
            business_days_until("2026-07-31", dt.date(2026, 7, 31)), 0
        )

    def test_malformed_date_is_none(self):
        self.assertIsNone(business_days_until("아무말", dt.date(2026, 7, 31)))


FILINGS = [
    {"filing_date": "2025-08-08", "form": "PROVISIONAL_RESULT"},
    {"filing_date": "2025-08-14", "form": "HALF_REPORT"},
    {"filing_date": "2025-11-11", "form": "PROVISIONAL_RESULT"},
    {"filing_date": "2025-11-14", "form": "QUARTER_REPORT"},
    {"filing_date": "2026-02-04", "form": "PROVISIONAL_RESULT"},
    {"filing_date": "2026-03-19", "form": "ANNUAL_REPORT"},
    {"filing_date": "2026-05-08", "form": "PROVISIONAL_RESULT"},
    {"filing_date": "2026-05-15", "form": "QUARTER_REPORT"},
]


class TestPeriodicLag(unittest.TestCase):
    def test_annual_is_not_pooled_with_interim(self):
        """사업보고서는 법정 기한이 +90일, 분기·반기는 +45일이다.

        섞어서 중앙값을 내면 어느 쪽도 예측하지 못하는 값이 나온다.
        """
        lag = periodic_lag(FILINGS)
        medians = lag["median_gap_days_by_class"]
        self.assertEqual(medians["INTERIM"], 6.0)
        # 사업보고서는 관측 1건이라 중앙값을 내지 않는다.
        self.assertIsNone(medians["ANNUAL"])
        self.assertEqual(lag["sample_size_by_class"]["INTERIM"], 3)

    def test_single_observation_yields_no_median(self):
        lag = periodic_lag(FILINGS[:2])
        self.assertIsNone(lag["median_gap_days_by_class"]["INTERIM"])

    def test_unpaired_provisional_is_not_matched_to_next_quarter(self):
        # 정기보고서가 아예 없으면 짝이 생기지 않는다.
        lag = periodic_lag([{"filing_date": "2026-05-08",
                             "form": "PROVISIONAL_RESULT"}])
        self.assertEqual(lag["observation_count"], 0)

    def test_far_apart_filings_are_not_paired(self):
        rows = [
            {"filing_date": "2026-01-05", "form": "PROVISIONAL_RESULT"},
            {"filing_date": "2026-09-30", "form": "QUARTER_REPORT"},
        ]
        self.assertEqual(periodic_lag(rows)["observation_count"], 0)

    def test_repeated_provisionals_count_the_report_once(self):
        """같은 결산에 잠정·정정·손익구조변동을 여러 건 내는 회사가 있다.

        잠정 쪽에서 짝지으면 같은 정기보고서가 여러 번 계상돼 표본이 부풀고
        중앙값이 흔들린다. 삼성전자에서 실제로 그렇게 나왔다.
        """
        rows = [
            {"filing_date": "2026-01-29", "form": "PROVISIONAL_RESULT"},
            {"filing_date": "2026-01-29", "form": "PROVISIONAL_RESULT"},
            {"filing_date": "2026-01-29", "form": "PROVISIONAL_RESULT"},
            {"filing_date": "2026-03-10", "form": "ANNUAL_REPORT"},
        ]
        lag = periodic_lag(rows)
        self.assertEqual(lag["observation_count"], 1)
        self.assertEqual(lag["sample_size_by_class"]["ANNUAL"], 1)

    def test_nearest_preceding_provisional_is_used(self):
        rows = [
            {"filing_date": "2026-04-07", "form": "PROVISIONAL_RESULT"},
            {"filing_date": "2026-04-30", "form": "PROVISIONAL_RESULT"},
            {"filing_date": "2026-05-15", "form": "QUARTER_REPORT"},
        ]
        lag = periodic_lag(rows)
        self.assertEqual(lag["observation_count"], 1)
        self.assertEqual(lag["observations"][0]["gap_days"], 15)


class TestExpectedReportClass(unittest.TestCase):
    def test_period_end_month_picks_the_class(self):
        self.assertEqual(expected_report_class("2026-06-30"), "INTERIM")
        self.assertEqual(expected_report_class("2026-03-31"), "INTERIM")
        self.assertEqual(expected_report_class("2026-12-31"), "ANNUAL")

    def test_unknown_period_end_is_none(self):
        self.assertIsNone(expected_report_class(None))
        self.assertIsNone(expected_report_class("2026-07-31"))


if __name__ == "__main__":
    unittest.main()
