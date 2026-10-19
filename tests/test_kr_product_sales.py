"""잠정실적 공시의 품목군별 매출 시계열 회귀 테스트.

파마리서치의 품목군별 매출(의료기기·화장품·의약품 × 내수·수출)은 정기보고서에
없고 잠정실적 공정공시 본문에만 있다. 그래서 판단마다 모델이 원문 서너 건을
손으로 다시 읽었다 — 2026-08-09 판단은 그렇게 모은 3개 분기로 회귀를 돌렸다가
표본이 5개로 늘자 결론이 반증됐고, 2026-10-06 판단의 무효화 신호는 전부 이
숫자에 걸려 있다.

가장 위험한 실패는 못 읽는 것이 아니라 **다른 분기나 다른 회사의 숫자를 그
분기 숫자로 싣는 것**이다. 그래서 같은 공시의 매출액과 합을 대조하고, 기간
표기가 공시의 실적기간과 다르면 싣지 않는다.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from collectors import kr_product_sales as ps


def quarterly_text(label, period_start, period_end, revenue, cumulative, items,
                   unit="백만원", colon=": "):
    body = " ".join(f"{name}{colon}{value:,}" for name, value in items)
    return (
        "연결재무제표 기준 영업(잠정)실적(공정공시)\n"
        f"실적기간 | \n당기실적 | \n{period_start} | \n~ | \n{period_end} | \n"
        "1. 연결실적내용 | \n구분(단위 : 백만원, %) | \n당기실적 | \n"
        f"매출액 | \n당해실적 | \n{revenue:,} | \n100 | \n1.0 | \n- | \n"
        f"누계실적 | \n{cumulative:,} | \n- | \n"
        "영업이익 | \n당해실적 | \n66,470 | \n"
        "4. 기타 투자판단에 참고할 사항 | \n"
        f"- 당사의 품목군별 {label} 매출은 아래와 같습니다(단위: {unit}) {body} "
        "- 상기 정보는 외부감사인의 감사를 받지 아니한 자료로 향후 변경될 수 있습니다. |"
    )


def annual_text(label, year_end, annual_revenue_won, items):
    body = " ".join(f"{name} : {value:,}" for name, value in items)
    return (
        "매출액 또는 손익구조 30%(대규모법인은 15%)이상 변동\n"
        "2. 결산기간| 당해사업연도| 직전사업연도|   - 시작일| 2025-01-01| 2024-01-01|"
        f"   - 종료일| {year_end}| 2024-12-31|"
        "   3. 매출액 또는 손익구조변동내용(단위: 원)| 당해사업연도| 직전사업연도|"
        f"   - 매출액| {annual_revenue_won:,}| 350,115,068,641|"
        "   7. 기타 투자판단에 참고할 사항|"
        f" - 당사의 품목군별 {label} 잠정 매출은 아래와 같습니다. (단위: 백만원) {body}"
        " - 본 자료는 외부감사인의 회계감사가 완료되지 않은 상태에서 작성된 자료로 |"
    )


# 실제 공시 값이다 (20260807900190 / 20251111900180 / 20260204900782).
Q2_26 = [("의료기기(내수)", 64497), ("의료기기(수출)", 32186), ("화장품(내수)", 16584),
         ("화장품(수출)", 43587), ("의약품(내수)", 10695), ("의약품(수출)", 7704),
         ("기타(내수)", 2912), ("기타(수출)", 540)]
Q3_25 = [("의약품(내수)", 13348), ("의약품(수출)", 8639), ("의료기기(내수)", 57165),
         ("의료기기(수출)", 19585), ("화장품(내수)", 13246), ("화장품(수출)", 21937),
         ("기타(내수)", 1140), ("기타(수출)", 366)]
Q4_25 = [("의약품(내수)", 13733), ("의약품(수출)", 9192), ("의료기기(내수)", 59691),
         ("의료기기(수출)", 23553), ("화장품(내수)", 11988), ("화장품(수출)", 25647),
         ("기타(내수)", -1889), ("기타(수출)", 865)]


def doc(rcept_no, text, *, matched_by="FILING_INDEX", filing_date=None, title="파마리서치/잠정실적"):
    return {"rcept_no": rcept_no, "doc_id": f"DARTDOC:{rcept_no}", "text": text,
            "matched_by": matched_by, "title": title,
            "filing_date": filing_date or f"{rcept_no[:4]}-{rcept_no[4:6]}-{rcept_no[6:8]}"}


class TestParsing(unittest.TestCase):
    def test_reads_the_quarter_label_in_both_spellings(self):
        for label, period in (("26.2분기", "2026Q2"), ("25.2Q", "2025Q2")):
            parsed = ps.parse_breakdown(quarterly_text(
                label, "2026-04-01", "2026-06-30", 178705, 324805, Q2_26))
            assert parsed is not None
            self.assertEqual(parsed["period"], period)
            self.assertEqual(parsed["label_in_source"], label)

    def test_keeps_every_item_in_source_order_with_spaced_colons(self):
        parsed = ps.parse_breakdown(annual_text("25.4Q", "2025-12-31", 535722606417, Q4_25))
        assert parsed is not None
        self.assertEqual(list(parsed["items"]), [name for name, _ in Q4_25])
        self.assertEqual(parsed["items"]["의료기기(내수)"], 59691)

    def test_a_negative_item_keeps_its_sign(self):
        # 4Q25 기타(내수)는 -1,889 다. 부호를 놓치면 합이 3,778 어긋난다.
        parsed = ps.parse_breakdown(annual_text("25.4Q", "2025-12-31", 535722606417, Q4_25))
        assert parsed is not None
        self.assertEqual(parsed["items"]["기타(내수)"], -1889)

    def test_text_after_the_list_is_not_read_as_an_item(self):
        parsed = ps.parse_breakdown(quarterly_text(
            "26.2분기", "2026-04-01", "2026-06-30", 178705, 324805, Q2_26))
        assert parsed is not None
        self.assertEqual(len(parsed["items"]), 8)

    def test_a_unit_other_than_million_won_is_refused_not_rescaled(self):
        parsed = ps.parse_breakdown(quarterly_text(
            "26.2분기", "2026-04-01", "2026-06-30", 178705, 324805, Q2_26, unit="억원"))
        assert parsed is not None
        self.assertEqual(parsed["problem"], "UNSUPPORTED_UNIT")

    def test_a_document_without_the_breakdown_is_none(self):
        self.assertIsNone(ps.parse_breakdown("연결재무제표 기준 영업(잠정)실적 매출액 | 당해실적 | 1 |"))


class TestReconciliation(unittest.TestCase):
    def build(self, docs, previous=None):
        return ps.build_series("214450", docs, previous=previous)

    def test_items_must_add_up_to_the_same_filings_revenue(self):
        data = self.build([doc("20260807900190", quarterly_text(
            "26.2분기", "2026-04-01", "2026-06-30", 178705, 324805, Q2_26))])
        row = data["quarters"][0]
        self.assertEqual(row["item_sum"], 178705)
        self.assertEqual(row["reported_revenue"], 178705)
        self.assertEqual(row["reconciliation"], "RECONCILED")

    def test_a_sum_that_misses_revenue_is_flagged_not_hidden(self):
        data = self.build([doc("20260807900190", quarterly_text(
            "26.2분기", "2026-04-01", "2026-06-30", 190000, 324805, Q2_26))])
        row = data["quarters"][0]
        self.assertEqual(row["reconciliation"], "MISMATCH")
        self.assertEqual(row["difference"], 178705 - 190000)

    def test_fourth_quarter_is_checked_against_annual_minus_nine_months(self):
        # 4Q 는 연간 공시로 나와 분기 매출 칸이 없다. 3Q 공시의 누계를 빼서 대조한다.
        data = self.build([
            doc("20251111900180", quarterly_text(
                "25.3Q", "2025-07-01", "2025-09-30", 135426, 392944, Q3_25)),
            doc("20260204900782", annual_text("25.4Q", "2025-12-31", 535722606417, Q4_25)),
        ])
        q4 = next(r for r in data["quarters"] if r["period"] == "2025Q4")
        self.assertEqual(q4["reported_revenue_basis"], "ANNUAL_MINUS_9M")
        self.assertEqual(q4["reported_revenue"], 535723 - 392944)
        self.assertEqual(q4["reconciliation"], "RECONCILED")

    def test_fourth_quarter_without_nine_month_total_is_unchecked(self):
        data = self.build([
            doc("20260204900782", annual_text("25.4Q", "2025-12-31", 535722606417, Q4_25)),
        ])
        self.assertEqual(data["quarters"][0]["reconciliation"], "UNCHECKED")
        self.assertIsNone(data["quarters"][0]["reported_revenue"])

    def test_a_label_that_contradicts_the_filing_period_is_not_stored(self):
        data = self.build([doc("20260807900190", quarterly_text(
            "26.1분기", "2026-04-01", "2026-06-30", 178705, 324805, Q2_26))])
        self.assertEqual(data["quarters"], [])
        self.assertEqual(data["unparsed"][0]["reason"], "PERIOD_CONFLICT")

    def test_a_later_filing_for_the_same_quarter_supersedes(self):
        corrected = [(n, v + (10 if n == "기타(수출)" else 0)) for n, v in Q2_26]
        data = self.build([
            doc("20260807900190", quarterly_text(
                "26.2분기", "2026-04-01", "2026-06-30", 178705, 324805, Q2_26)),
            doc("20260810900001", quarterly_text(
                "26.2분기", "2026-04-01", "2026-06-30", 178715, 324815, corrected)),
        ])
        self.assertEqual(len(data["quarters"]), 1)
        self.assertEqual(data["quarters"][0]["rcept_no"], "20260810900001")
        self.assertEqual(data["superseded"][0]["rcept_no"], "20260807900190")

    def test_a_quarter_whose_filing_left_the_index_is_kept(self):
        # 공시 색인은 1년 창이라 전년동기 공시가 빠져 나간다. 바로 그때 YoY 가 필요하다.
        first = self.build([doc("20251111900180", quarterly_text(
            "25.3Q", "2025-07-01", "2025-09-30", 135426, 392944, Q3_25))])
        later = self.build([doc("20260807900190", quarterly_text(
            "26.2분기", "2026-04-01", "2026-06-30", 178705, 324805, Q2_26))], previous=first)
        periods = [r["period"] for r in later["quarters"]]
        self.assertEqual(periods, ["2025Q3", "2026Q2"])
        kept = later["quarters"][0]
        self.assertTrue(kept["retained_from_previous"])

    def test_title_match_requires_the_exact_company_name(self):
        self.assertTrue(ps.title_company_matches("파마리서치/연결재무제표 기준 영업(잠정)실적", "파마리서치"))
        self.assertFalse(ps.title_company_matches("파마리서치바이오/영업(잠정)실적", "파마리서치"))
        self.assertFalse(ps.title_company_matches(None, "파마리서치"))


class TestPacketSummary(unittest.TestCase):
    def series(self):
        q2_25 = [("의약품(내수)", 13423), ("의약품(수출)", 7029), ("의료기기(내수)", 60717),
                 ("의료기기(수출)", 24269), ("화장품(내수)", 11281), ("화장품(수출)", 19489),
                 ("기타(내수)", 3294), ("기타(수출)", 1105)]
        q1_26 = [("의료기기(내수)", 58404), ("의료기기(수출)", 21145), ("화장품(내수)", 15300),
                 ("화장품(수출)", 26855), ("의약품(내수)", 11174), ("의약품(수출)", 10244),
                 ("기타(내수)", 2445), ("기타(수출)", 533)]
        return ps.build_series("214450", [
            doc("20250808900097", quarterly_text("25.2Q", "2025-04-01", "2025-06-30", 140607, 257517, q2_25),
                matched_by="TITLE_COMPANY_NAME"),
            doc("20260508900237", quarterly_text("26.1분기", "2026-01-01", "2026-03-31", 146100, 146100, q1_26)),
            doc("20260807900190", quarterly_text("26.2분기", "2026-04-01", "2026-06-30", 178705, 324805, Q2_26)),
        ])

    def test_change_rates_are_arithmetic_against_the_right_quarters(self):
        latest = ps.packet_summary(self.series())["latest"]  # pyright: ignore[reportOptionalSubscript]
        domestic = latest["items"]["의료기기(내수)"]
        self.assertEqual(domestic["value"], 64497)
        self.assertEqual(domestic["qoq_pct"], round((64497 / 58404 - 1) * 100, 1))
        self.assertEqual(domestic["yoy_pct"], round((64497 / 60717 - 1) * 100, 1))
        self.assertEqual(domestic["share_pct"], round(64497 / 178705 * 100, 1))

    def test_a_missing_or_non_positive_base_gives_no_rate(self):
        data = self.series()
        data["quarters"] = [r for r in data["quarters"] if r["period"] != "2025Q2"]
        latest = ps.packet_summary(data)["latest"]  # pyright: ignore[reportOptionalSubscript]
        self.assertIsNone(latest["items"]["의료기기(내수)"]["yoy_pct"])
        self.assertEqual(ps.change_pct(10, -5), None)

    def test_the_table_is_aligned_to_one_item_order(self):
        summary = ps.packet_summary(self.series())
        assert summary is not None
        order = summary["items"]
        for row in summary["quarters"]:  # pyright: ignore[reportOptionalSubscript]
            self.assertEqual(len(row["values"]), len(order))
        q2_25 = next(r for r in summary["quarters"] if r["period"] == "2025Q2")
        self.assertEqual(q2_25["values"][order.index("의료기기(내수)")], 60717)

    def test_no_judgment_words_reach_the_packet(self):
        text = json.dumps(ps.packet_summary(self.series()), ensure_ascii=False)
        for word in ("둔화", "정체", "피크", "BUY", "SELL", "trend", "signal"):
            self.assertNotIn(word, text)


class TestWiring(unittest.TestCase):
    def test_evidence_counts_as_financials(self):
        from cio.decision import _evidence_family
        self.assertEqual(_evidence_family("KR_PRODUCT_SALES:214450", "FILING_DERIVED"), "FINANCIALS")

    def test_registered_collector_is_allowed_for_korean_stocks(self):
        from cio.assets import OPTIONAL_COLLECTORS_BY_ROUTE
        self.assertIn("KR_PRODUCT_SALES", OPTIONAL_COLLECTORS_BY_ROUTE[("KR", "STOCK")])

    def test_refresh_plans_the_task_after_the_filing_documents(self):
        from cio import refresh
        from cio.assets import get_asset
        asset = {**get_asset("214450"), "optional_collectors": ["KR_PRODUCT_SALES"]}
        with patch.object(refresh, "get_asset", return_value=asset):
            tasks = refresh.tasks_for("214450", include_optional=True)
        task = next(t for t in tasks if t.name == "product_sales:214450")
        self.assertIn("knowledge/filings/_documents/214450.json", task.dependencies)
        self.assertFalse(task.required)

    def test_calendar_says_this_company_reports_product_sales_in_the_provisional(self):
        # 일반 문구는 '부문별 매출은 잠정실적에 없다'이다. 이 회사는 정반대라, 그대로
        # 두면 모델이 품목군 문턱의 확인을 정기보고서 날짜에 걸어 둔다.
        from cio import context
        calendar = {"schema_version": "earnings-calendar/1.0.0", "asset_id": "214450",
                    "next_provisional_result": {"disclosure_date": "2026-11-11",
                                                "_content": "재고자산·부문별 매출은 담지 않는다"}}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            knowledge = root / "knowledge"
            (knowledge / "calendar").mkdir(parents=True)
            (knowledge / "calendar" / "214450.json").write_text(json.dumps(calendar), encoding="utf-8")
            with patch.object(context, "KNOWLEDGE_DIR", knowledge), patch.object(context, "ROOT", root):
                plain, _ = context._earnings_calendar_section(
                    {"asset_id": "214450", "asset_type": "STOCK", "optional_collectors": []})
                wired, _ = context._earnings_calendar_section(
                    {"asset_id": "214450", "asset_type": "STOCK", "optional_collectors": ["KR_PRODUCT_SALES"]})
        assert plain is not None
        self.assertNotIn("_product_sales_note", plain["next_provisional_result"])
        assert wired is not None
        self.assertIn("kr_product_sales", wired["next_provisional_result"]["_product_sales_note"])

    def test_packet_carries_the_series_and_coverage_says_available(self):
        from cio import context
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            knowledge = root / "knowledge"
            (knowledge / "product_sales").mkdir(parents=True)
            data = TestPacketSummary().series()
            (knowledge / "product_sales" / "214450.json").write_text(
                json.dumps(data, ensure_ascii=False), encoding="utf-8")
            asset = {"asset_id": "214450", "market": "KR", "asset_type": "STOCK",
                     "optional_collectors": ["KR_PRODUCT_SALES"]}
            with patch.object(context, "KNOWLEDGE_DIR", knowledge), patch.object(context, "ROOT", root):
                found, sources = context._optional_collection_summaries(asset)
                coverage = context._data_coverage(asset, structured_optional=found, fundamentals=None)
        self.assertEqual(found["kr_product_sales"]["latest_period"], "2026Q2")
        self.assertIn("KR_PRODUCT_SALES:214450", [s["source_id"] for s in sources])
        self.assertEqual(coverage["segment_revenue"]["state"], "AVAILABLE")


if __name__ == "__main__":
    unittest.main()
