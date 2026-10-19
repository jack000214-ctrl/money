"""로컬 재무보다 새 실적이 나왔는가 — 그 판정이 조용히 통과하던 길 넷.

이 판정은 fail-closed 여야 하는 자리다. 로컬 재무가 낡았는데 새 실적 원문도
없으면 모델에게는 최신 실적을 확인할 경로가 packet 안에 없다(삼성전자 TTM
영업이익이 로컬 94.1조 vs 공시 179.0조로 벌어진 채 통과한 적이 있다).

· **요약을 줄인 뒤에 판정했다.** 요약은 정기보고서 4건·사건 8건에서 잘린다.
  실적 공시 뒤에 다른 공시가 8건 쌓이면 실적이 요약에서 밀려나 판정이 놓쳤다.
· **8-K 를 서식으로만 봤다.** 임원 교체(Item 5.02)가 최신 실적으로 세어지고,
  실적을 정정한 8-K/A(Item 2.02)는 빠졌다.
· **확인 실패가 통과였다.** 보존 원문 목록을 읽지 못하면 "미수집 없음"으로
  내려갔다.
· **본체만 받고 원문을 가졌다고 했다.** 실적 8-K 의 숫자는 Exhibit 99.1 에 있다.
"""

import copy
import datetime as dt
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from helpers import ROOT  # noqa: F401
from cio import context, documents
from cio.assets import get_asset
from cio.context import _is_result_disclosure, _reporting_lag

ASSET = get_asset("TSLA")
FOLDER = "https://www.sec.gov/Archives/edgar/data/1318605/000131860526000999/"
PRIMARY_URL = FOLDER + "earnings.htm"
LOCAL = {"period_end": "2026-03-31"}


def periodic(report_date="2026-03-31", filed="2026-05-01", form="10-Q", number=1):
    return {"filing_date": filed, "report_date": report_date, "form": form,
            "accession_number": f"0001318605-26-{number:06d}", "description": form,
            "document_url": "https://www.sec.gov/Archives/edgar/data/1318605/base.htm"}


def current(item="2.02", form="8-K", filed="2026-07-15", number=999):
    return {"filing_date": filed, "report_date": "2026-06-30", "form": form,
            "accession_number": f"0001318605-26-{number:06d}", "description": form,
            "items": [item] if item else None,
            "item_labels": [f"{item} 항목"] if item else None,
            "document_url": PRIMARY_URL}


class TestWhatCountsAsAResultDisclosure(unittest.TestCase):
    def test_an_8k_is_read_by_its_item_not_its_form(self):
        self.assertTrue(_is_result_disclosure(current("2.02")))
        self.assertFalse(_is_result_disclosure(current("5.02")))

    def test_an_amended_earnings_8k_is_still_earnings(self):
        self.assertTrue(_is_result_disclosure(current("2.02", form="8-K/A")))

    def test_an_8k_with_no_item_information_is_not_assumed_harmless(self):
        """옛 색인 행에는 Item 이 없다. 모르는 것을 '실적 아님'으로 넘기지 않는다."""
        row = current(None)  # pyright: ignore[reportArgumentType]
        self.assertTrue(_is_result_disclosure(row))

    def test_the_compact_row_carries_labels_instead_of_codes(self):
        self.assertTrue(_is_result_disclosure(
            {"form": "8-K", "item_labels": ["2.02 실적 발표", "9.01 첨부서류"]}))
        self.assertFalse(_is_result_disclosure(
            {"form": "8-K", "item_labels": ["5.02 임원·이사 변동"]}))

    def test_korean_forms_are_unchanged(self):
        self.assertTrue(_is_result_disclosure({"form": "PROVISIONAL_RESULT"}))
        self.assertFalse(_is_result_disclosure({"form": "OTHER"}))


class TestTheScanSeesTheWholeIndex(unittest.TestCase):
    def summary(self, rows):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "knowledge" / "filings" / "TSLA.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({
                "schema_version": "filing-index/1.0.0", "asset_id": "TSLA",
                "source": "SEC_EDGAR_SUBMISSIONS", "cik": "0001318605",
                "collected_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "filings": rows}), encoding="utf-8")
            with patch.object(context, "KNOWLEDGE_DIR", root / "knowledge"), \
                    patch.object(context, "ROOT", root):
                return context._filing_summary(ASSET)[0]

    def test_results_pushed_out_by_eight_later_filings_are_still_found(self):
        noise = [current(None, form="S-3", filed=f"2026-08-{day:02d}", number=100 + day)  # pyright: ignore[reportArgumentType]
                 for day in range(20, 12, -1)]
        summary = self.summary([*noise, current(), periodic()])
        assert summary is not None
        self.assertEqual(len(summary["latest_material_events"]), 8)
        self.assertNotIn(current()["accession_number"], [
            row["accession_number"] for row in summary["latest_material_events"]])
        lag = _reporting_lag(LOCAL, summary)
        assert lag is not None
        self.assertEqual(len(lag["disclosures_after_local_period"]), 1)

    def test_a_newer_period_pushed_out_by_four_amendments_is_still_found(self):
        amendments = [periodic("2025-12-31", f"2026-09-{day:02d}", "10-K/A", 200 + day)
                      for day in range(20, 16, -1)]
        summary = self.summary(
            [*amendments, periodic("2026-06-30", "2026-08-01", number=300), periodic()])
        assert summary is not None
        self.assertEqual({row["report_date"] for row in
                          summary["latest_primary_reports"]}, {"2025-12-31"})
        self.assertTrue(_reporting_lag(LOCAL, summary)["newer_periodic_report_filed"])  # pyright: ignore[reportOptionalSubscript]

    def test_an_officer_change_is_not_unreflected_earnings(self):
        summary = self.summary([current("5.02"), periodic()])
        assert summary is not None
        self.assertEqual(
            _reporting_lag(LOCAL, summary)["disclosures_after_local_period"], [])  # pyright: ignore[reportOptionalSubscript]

    def test_a_summary_without_the_scan_still_works(self):
        lag = _reporting_lag(LOCAL, {
            "latest_primary_reports": [periodic()],
            "latest_material_events": [current("2.02", form="8-K/A")]})
        assert lag is not None
        self.assertEqual(len(lag["disclosures_after_local_period"]), 1)


class TestTheGateFailsClosed(unittest.TestCase):
    """판정 재료만 실제로 두고 나머지 입력은 고정한 채 build_context 를 돌린다."""

    FILINGS = {"latest_primary_reports": [periodic()],
               "latest_material_events": [current()]}

    def packet(self, *, documents_for, filings=None):
        price = {"currency": "USD", "close": 100, "as_of": "2026-10-12",
                 "last_bar": {"is_settled_close": True}}
        fixed = {
            "_price_summary": (price, None), "_session_gaps": None,
            "_latest_fundamentals": ({"period_end": "2026-03-31", "values": {}}, None),
            "_filing_summary": (copy.deepcopy(filings or self.FILINGS), None),
            "_earnings_calendar_section": (None, None),
            "_latest_calculation": (None, None),
            "_market_summary": ({"input_sources": []}, None),
            "_event_summary": (None, None), "_etf_summary": (None, None),
            "inspect_plan": [], "_optional_collection_summaries": ({}, []),
            "_us_automated_summaries": ({}, []),
            "_portfolio_summary": ({"available": False}, None),
            "_attach_position_math": None, "load_policy": (None, []),
            "position_multiples": None, "_memory_digest": {}, "_daily_ranges": [],
            "open_requests_for_context": [], "documents_for_context": None,
        }
        with ExitStack() as stack:
            for name, value in fixed.items():
                stack.enter_context(patch.object(context, name, return_value=value))
            stack.enter_context(patch("cio.capital.section", return_value=(None, [])))
            stack.enter_context(patch.object(documents, "documents_for", documents_for))
            packet, _ = context.build_context(
                "TSLA", price=100, amount=1000, amount_currency="USD", save=False)
        return packet

    def build(self, *, documents_for):
        return self.packet(documents_for=documents_for)["validation"]

    def catalog(self, body, *, extra=()):
        available = [{"url": PRIMARY_URL, "locator": "primary.json"}, *extra]
        return patch.object(documents, "load_json",
                            return_value={"payload": {"body": body}}), \
            (lambda asset_id: {"available": available, "missing": []})

    def test_an_unreadable_catalog_blocks_instead_of_passing(self):
        def broken(asset_id):
            raise OSError("보존 원문 목록을 읽을 수 없다")

        validation = self.build(documents_for=broken)
        self.assertEqual(validation["status"], "BLOCKED")
        self.assertTrue(any("확인하지 못했다" in p for p in validation["problems"]))

    def test_an_unfetched_original_blocks(self):
        validation = self.build(
            documents_for=lambda asset_id: {"available": [], "missing": []})
        self.assertTrue(any("그 원문이 없다" in p for p in validation["problems"]))

    def test_a_stub_without_its_exhibit_blocks_and_names_the_exhibit(self):
        loader, catalog = self.catalog(
            "<p>Item 2.02. The release is Exhibit 99.1.</p>"
            "<a href='press-release.htm'>Exhibit 99.1</a>")
        with loader:
            validation = self.build(documents_for=catalog)
        self.assertEqual(validation["status"], "BLOCKED")
        problem = next(p for p in validation["problems"] if "Exhibit 99" in p)
        self.assertIn(FOLDER + "press-release.htm", problem)

    def test_a_stub_with_its_exhibit_passes(self):
        loader, catalog = self.catalog(
            "<p>The release is Exhibit 99.1.</p><a href='press-release.htm'>99.1</a>",
            extra=({"url": FOLDER + "press-release.htm", "locator": "exhibit.json"},))
        with loader:
            validation = self.build(documents_for=catalog)
        self.assertEqual(validation["problems"], [])

    def test_the_scan_never_reaches_the_packet(self):
        loader, catalog = self.catalog("<p>Revenue was $25.0 billion.</p>")
        with_scan = {**self.FILINGS, "_lag_scan": {
            "primary_reports": [periodic()], "result_disclosures": [current()]}}
        with loader:
            packet = self.packet(documents_for=catalog, filings=with_scan)
        self.assertNotIn("_lag_scan", packet["context"]["filings"])
        self.assertEqual(len(packet["context"]["fundamentals"]["reporting_lag"][
            "disclosures_after_local_period"]), 1)


if __name__ == "__main__":
    unittest.main()
