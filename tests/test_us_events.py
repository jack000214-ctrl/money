"""미국 급변일 ↔ 공시 대조 계약 테스트.

판정 로직은 한국판과 같은 함수(kr_events.build_event_packet)를 쓴다. 여기서
지키는 것은 **공시를 어떻게 읽어 오는가** 다 — 8-K 는 폼 이름만으로 무엇이
났는지 알 수 없고, Form 4 는 하루 수십 건이라 신호를 덮는다.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers import ROOT  # noqa: F401
from collectors import us_events


def index(*filings):
    return {"schema_version": "sec-filing-index/1.0.0", "filings": list(filings)}


def filing(form, filed, **extra):
    row = {"form": form, "filing_date": filed, "description": form}
    row.update(extra)
    return row


class TestFilingIndexReading(unittest.TestCase):
    def read(self, payload):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "TSLA.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )
            with patch.object(us_events, "FILING_DIR", directory):
                return us_events.load_filings("TSLA")

    def test_insider_forms_are_dropped(self):
        """Form 4 는 하루 수십 건이라 급변 후보를 덮는다."""
        rows = self.read(index(
            filing("4", "2026-07-20"),
            filing("8-K", "2026-07-23"),
        ))
        self.assertEqual([r["form"] for r in rows], ["8-K"])

    def test_periodic_reports_are_marked_scheduled(self):
        """정기보고서는 날짜가 미리 알려져 있어 놀라움이 아니다."""
        rows = self.read(index(filing("10-Q", "2026-07-23")))
        self.assertTrue(rows[0]["scheduled"])

    def test_eight_k_carries_its_item_labels(self):
        """8-K 는 item 코드가 실제 내용이다. 폼 이름만 남기면 뜻이 사라진다."""
        rows = self.read(index(filing(
            "8-K", "2026-07-23",
            items=["2.02", "9.01"],
            item_labels=["2.02 실적 발표", "9.01 재무제표·첨부서류"],
        )))
        self.assertIn("2.02 실적 발표", rows[0]["name"])
        self.assertFalse(rows[0]["scheduled"])

    def test_rows_are_sorted_by_filing_date(self):
        rows = self.read(index(
            filing("8-K", "2026-07-23"),
            filing("8-K", "2026-05-01"),
        ))
        self.assertEqual([r["filed"] for r in rows],
                         ["2026-05-01", "2026-07-23"])

    def test_filing_without_a_date_is_skipped(self):
        """날짜 없는 공시는 급변일에 붙일 수 없다."""
        rows = self.read(index(
            {"form": "8-K", "filing_date": None},
            filing("8-K", "2026-07-23"),
        ))
        self.assertEqual(len(rows), 1)

    def test_missing_index_is_an_error_not_an_empty_list(self):
        """색인이 없는 것과 공시가 없는 것은 다르다.

        빈 목록으로 넘기면 모든 급변이 '근접 공시 없음' 으로 나오고, 그것은
        관측처럼 보인다.
        """
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(us_events, "FILING_DIR", Path(tmp)):
                with self.assertRaises(SystemExit):
                    us_events.load_filings("TSLA")


class TestSharedJudgement(unittest.TestCase):
    def test_us_uses_the_same_packet_builder_as_kr(self):
        """판정 로직이 갈리면 두 시장의 상대수익률이 다른 뜻을 갖는다."""
        from collectors import kr_events
        self.assertIs(us_events.build_event_packet,
                      kr_events.build_event_packet)

    def test_event_packet_reaches_the_model_for_both_markets(self):
        """수집만 하고 packet 에 안 실으면 없는 것과 같다."""
        from cio.assets import get_asset
        from cio.context import _event_summary
        for asset_id in ("005930", "IBM", "TSLA"):
            with self.subTest(asset_id=asset_id):
                summary, source = _event_summary(get_asset(asset_id))
                self.assertIsNotNone(source)
                self.assertIsNotNone(summary)
                self.assertNotIn("contract_errors", summary or {})

    def test_filing_identifier_follows_the_market(self):
        """DART 는 접수번호, SEC 는 accession number 다. 없는 키는 싣지 않는다."""
        from cio.assets import get_asset
        from cio.context import _event_summary
        seen = {}
        for asset_id in ("005930", "IBM"):
            summary, _ = _event_summary(get_asset(asset_id))
            for move in summary["recent_moves"]:
                for row in move["candidate_filings"]:
                    seen.setdefault(asset_id, set()).update(row)
        self.assertIn("rcept_no", seen.get("005930", set()))
        self.assertNotIn("accession_number", seen.get("005930", set()))
        self.assertIn("accession_number", seen.get("IBM", set()))
        self.assertNotIn("rcept_no", seen.get("IBM", set()))


if __name__ == "__main__":
    unittest.main()
