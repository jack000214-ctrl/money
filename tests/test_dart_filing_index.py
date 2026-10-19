"""한국 공시 색인과 결산 캘린더 신선도의 회귀 테스트.

DART 수집기는 오래전부터 있었지만 색인이 컨텍스트까지 배선되지 않아, 모델은
한국 종목의 공시로 들어갈 입구가 없었다. 잠정실적을 언론 보도로 확인하는 일이
거기서 나왔다.
"""

import unittest

from calculators.contracts import ContractError
from cio.context import _reporting_lag
from collectors.dart import (
    DART_FILING_TYPES,
    classify_dart_form,
    dart_report_date,
    filing_rows_from_list,
    merge_filing_rows,
)


def payload(*rows):
    return {"status": "000", "list": list(rows)}


def row(**overrides):
    base = {
        "corp_code": "00126380",
        "corp_name": "삼성전자",
        "stock_code": "005930",
        "report_nm": "분기보고서 (2026.03)",
        "rcept_no": "20260515002181",
        "flr_nm": "삼성전자",
        "rcept_dt": "20260515",
        "rm": "",
    }
    base.update(overrides)
    return base


class TestFormClassification(unittest.TestCase):
    def test_issuance_disclosures_are_collected_and_material(self):
        self.assertIn("C", [code for code, _ in DART_FILING_TYPES])
        for name in ("[발행조건확정]증권신고서(지분증권)", "투자설명서"):
            self.assertEqual(classify_dart_form(name), "MATERIAL_EVENT")

    def test_periodic_reports(self):
        self.assertEqual(classify_dart_form("사업보고서 (2025.12)"), "ANNUAL_REPORT")
        self.assertEqual(classify_dart_form("반기보고서 (2026.06)"), "HALF_REPORT")
        self.assertEqual(classify_dart_form("분기보고서 (2026.03)"), "QUARTER_REPORT")

    def test_provisional_result_is_its_own_kind(self):
        self.assertEqual(
            classify_dart_form("연결재무제표기준영업(잠정)실적(공정공시)"),
            "PROVISIONAL_RESULT",
        )

    def test_amended_filing_keeps_its_kind(self):
        self.assertEqual(
            classify_dart_form("[기재정정]연결재무제표기준영업(잠정)실적(공정공시)"),
            "PROVISIONAL_RESULT",
        )

    def test_unknown_name_is_other_not_a_guess(self):
        self.assertEqual(
            classify_dart_form("임원ㆍ주요주주특정증권등소유상황보고서"), "OTHER"
        )


class TestReportDate(unittest.TestCase):
    def test_period_marker_becomes_period_end(self):
        self.assertEqual(dart_report_date("반기보고서 (2026.06)"), "2026-06-30")
        self.assertEqual(dart_report_date("사업보고서 (2025.12)"), "2025-12-31")

    def test_leap_year_february(self):
        self.assertEqual(dart_report_date("분기보고서 (2024.02)"), "2024-02-29")
        self.assertEqual(dart_report_date("분기보고서 (2026.02)"), "2026-02-28")

    def test_no_marker_is_none_not_inferred_from_filing_date(self):
        self.assertIsNone(dart_report_date("주요사항보고서(자기주식취득결정)"))

    def test_impossible_month_is_none(self):
        self.assertIsNone(dart_report_date("이상한보고서 (2026.13)"))


class TestFilingRows(unittest.TestCase):
    def test_maps_dart_fields_to_the_shared_shape(self):
        rows = filing_rows_from_list(payload(row()), stock_code="005930")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["filing_date"], "2026-05-15")
        self.assertEqual(rows[0]["report_date"], "2026-03-31")
        self.assertEqual(rows[0]["form"], "QUARTER_REPORT")
        self.assertEqual(rows[0]["accession_number"], "20260515002181")
        self.assertEqual(rows[0]["description"], "분기보고서 (2026.03)")
        self.assertIn("20260515002181", rows[0]["document_url"])

    def test_other_companies_in_the_response_are_dropped(self):
        rows = filing_rows_from_list(
            payload(row(), row(stock_code="000660", rcept_no="20260515000001")),
            stock_code="005930",
        )
        self.assertEqual(len(rows), 1)

    def test_newest_first(self):
        rows = filing_rows_from_list(
            payload(
                row(rcept_dt="20260101", rcept_no="20260101000001"),
                row(rcept_dt="20260601", rcept_no="20260601000001"),
            ),
            stock_code="005930",
        )
        self.assertEqual(rows[0]["filing_date"], "2026-06-01")

    def test_limit_keeps_the_newest(self):
        rows = filing_rows_from_list(
            payload(
                *[
                    row(rcept_dt=f"202601{day:02d}", rcept_no=f"202601{day:02d}000001")
                    for day in range(1, 11)
                ]
            ),
            stock_code="005930",
            limit=3,
        )
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["filing_date"], "2026-01-10")

    def test_malformed_receipt_number_is_refused(self):
        with self.assertRaises(ContractError):
            filing_rows_from_list(payload(row(rcept_no="abc")), stock_code="005930")

    def test_missing_list_is_refused(self):
        with self.assertRaises(ContractError):
            filing_rows_from_list({"status": "000"}, stock_code="005930")


class TestMerge(unittest.TestCase):
    def test_same_filing_from_two_types_appears_once(self):
        group = filing_rows_from_list(payload(row()), stock_code="005930")
        merged = merge_filing_rows([group, group])
        self.assertEqual(len(merged), 1)

    def test_merge_orders_newest_first(self):
        early = filing_rows_from_list(
            payload(row(rcept_dt="20260101", rcept_no="20260101000001")),
            stock_code="005930",
        )
        late = filing_rows_from_list(
            payload(row(rcept_dt="20260701", rcept_no="20260701000001")),
            stock_code="005930",
        )
        merged = merge_filing_rows([early, late])
        self.assertEqual(merged[0]["filing_date"], "2026-07-01")


class TestReportingLag(unittest.TestCase):
    def filings(self, primary=(), events=()):
        return {
            "latest_primary_reports": list(primary),
            "latest_material_events": list(events),
        }

    def test_provisional_result_after_local_period_is_surfaced(self):
        lag = _reporting_lag(
            {"period_end": "2026-03-31"},
            self.filings(
                events=[
                    {
                        "form": "PROVISIONAL_RESULT",
                        "filing_date": "2026-07-30",
                        "description": "연결재무제표기준영업(잠정)실적(공정공시)",
                        "document_url": "https://example.invalid/1",
                    }
                ]
            ),
        )
        assert lag is not None
        self.assertEqual(len(lag["disclosures_after_local_period"]), 1)
        self.assertFalse(lag["newer_periodic_report_filed"])

    def test_newer_periodic_report_is_flagged(self):
        lag = _reporting_lag(
            {"period_end": "2026-03-31"},
            self.filings(
                primary=[
                    {
                        "form": "HALF_REPORT",
                        "report_date": "2026-06-30",
                        "filing_date": "2026-08-14",
                        "description": "반기보고서 (2026.06)",
                    }
                ]
            ),
        )
        assert lag is not None
        self.assertTrue(lag["newer_periodic_report_filed"])

    def test_older_disclosures_are_not_counted(self):
        lag = _reporting_lag(
            {"period_end": "2026-03-31"},
            self.filings(
                primary=[
                    {
                        "form": "QUARTER_REPORT",
                        "report_date": "2026-03-31",
                        "filing_date": "2026-05-15",
                    }
                ],
                events=[
                    {"form": "PROVISIONAL_RESULT", "filing_date": "2026-01-08"}
                ],
            ),
        )
        assert lag is not None
        self.assertFalse(lag["newer_periodic_report_filed"])
        self.assertEqual(lag["disclosures_after_local_period"], [])

    def test_a_disclosure_about_the_local_quarter_is_not_a_gap(self):
        """공시는 이미 끝난 기간만 보고한다.

        3월 31일 기준 재무에 5월 8일 1분기 잠정실적을 "미반영 공시"로 세면,
        로컬이 이미 담은 분기를 두고 매번 없는 공백이 켜진다. 경고가 늘 켜져
        있으면 아무도 읽지 않는다.
        """
        lag = _reporting_lag(
            {"period_end": "2026-03-31"},
            self.filings(
                events=[
                    {
                        "form": "PROVISIONAL_RESULT",
                        "filing_date": "2026-05-08",
                        "description": "연결재무제표기준영업(잠정)실적(공정공시)",
                    }
                ]
            ),
        )
        assert lag is not None
        self.assertEqual(lag["disclosures_after_local_period"], [])

    def test_no_filings_means_no_claim(self):
        self.assertIsNone(_reporting_lag({"period_end": "2026-03-31"}, None))
        self.assertIsNone(_reporting_lag(None, self.filings()))


if __name__ == "__main__":
    unittest.main()
