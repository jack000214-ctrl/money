"""근거는 주장에 결속돼야 한다 — 개수가 아니라 종류가 맞아야 한다.

초판 검증기는 `evidence_refs` 가 비어 있지 않은지만 봤다. 그래서 다음이
전부 통과했다.

  · "영업이익 1,860%" 주장을 PRICE:TSLA(가격 CSV)로 CONFIRMED
  · 존재하지 않는 GHOST 근거 ID로 CONFIRMED

앞은 **다른 종류의 자료로 확인했다고 말한 것**이고 뒤는 **아무것도 가리키지
않는 문자열**이다. 둘 다 "근거가 있다"는 형식은 통과한다. 형식이 통과하고
의미가 검증되지 않는 자리가 정확히 여기다.

이 계약이 하는 일은 판정이 아니다. 가격 자료로 실적을 확인할 수 없다는 것은
투자 판단이 아니라 자료의 종류에 대한 사실이고, 그래서 PC 의 일이다. 무엇이
확인이고 무엇이 반박인지는 여전히 전부 모델이 정한다.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cio.decision import context_id_for, validate_decision
from test_cio_core import valid_context, valid_decision

BASIS = {
    "metric": "영업이익",
    "period": "2026 회계연도 2분기",
    "comparison": "전년동기 대비",
}


def _context(claims=(("UA-aaa1", "이번 실적발표가 1,860% 나왔다"),), evidence=None):
    context = valid_context()
    context["request"]["user_assertions"] = [
        {"claim_id": claim_id, "claim": text, "status": "USER_ASSERTED"}
        for claim_id, text in claims
    ]
    context["evidence_index"] = evidence or [
        {"source_id": "PRICE:TSLA", "kind": "MARKET_DATA"},
        {"source_id": "FUNDAMENTALS:TSLA", "kind": "FILING_DERIVED"},
        {"source_id": "FILINGS:TSLA", "kind": "REGULATOR_FILING_INDEX"},
        {"source_id": "PORTFOLIO:HOLDINGS", "kind": "USER_DATA"},
    ]
    context["context_id"] = context_id_for(context)
    return context


def _check(claim_id="UA-aaa1", **overrides):
    row = {
        "claim_id": claim_id,
        "status": "CONFIRMED",
        "claim_kind": "EARNINGS",
        "basis": dict(BASIS),
        "finding": "2분기 영업이익 전년동기 +1,810%",
        "evidence_refs": ["FUNDAMENTALS:TSLA"],
    }
    row.update(overrides)
    return row


def _errors(context, *checks, sources=None):
    data = valid_decision()
    data["context_id"] = context["context_id"]
    data["user_assertion_checks"] = list(checks)
    if sources is not None:
        data["sources"] = sources
    return [
        error
        for error in validate_decision(data, context=context)
        if "user_assertion_checks" in error
    ]


class TestEvidenceKindMustMatchClaimKind(unittest.TestCase):
    def test_earnings_confirmed_by_a_price_file_is_rejected(self):
        """가격 CSV 에는 영업이익이 없다. 파일을 열어보면 아는 사실이다."""
        context = _context()
        self.assertTrue(_errors(context, _check(evidence_refs=["PRICE:TSLA"])))

    def test_earnings_confirmed_by_the_financial_snapshot_passes(self):
        self.assertEqual(_errors(_context(), _check()), [])

    def test_price_claim_confirmed_by_the_price_file_passes(self):
        context = _context(claims=(("UA-aaa1", "2일 연속 하락했다"),))
        self.assertEqual(
            _errors(
                context,
                _check(
                    claim_kind="PRICE",
                    basis={"metric": "종가", "period": "직전 2거래일",
                           "comparison": "전일 대비"},
                    finding="직전 거래일은 +21.5% 상승이다",
                    status="REFUTED",
                    evidence_refs=["PRICE:TSLA"],
                ),
            ),
            [],
        )

    def test_price_claim_confirmed_by_the_ledger_is_rejected(self):
        context = _context(claims=(("UA-aaa1", "2일 연속 하락했다"),))
        self.assertTrue(
            _errors(
                context,
                _check(
                    claim_kind="PRICE",
                    basis={"metric": "종가", "period": "직전 2거래일",
                           "comparison": "전일 대비"},
                    evidence_refs=["PORTFOLIO:HOLDINGS"],
                ),
            )
        )

    def test_earnings_confirmed_by_a_filing_title_is_rejected(self):
        """공시 색인에는 제목만 있다. 제목으로 숫자를 확인할 수 없다."""
        context = _context()
        self.assertTrue(_errors(context, _check(evidence_refs=["FILINGS:TSLA"])))

    def test_the_disclosure_date_can_be_confirmed_by_the_index(self):
        """'발표가 났다'는 색인이 답할 수 있는 질문이다."""
        context = _context(claims=(("UA-aaa1", "오늘 실적 발표를 했다"),))
        self.assertEqual(
            _errors(
                context,
                _check(
                    claim_kind="EVENT_DATE",
                    basis={"metric": "잠정실적 공시 접수", "period": "2026-07-30",
                           "comparison": "공시 색인 대조"},
                    evidence_refs=["FILINGS:TSLA"],
                ),
            ),
            [],
        )

    def test_a_preserved_original_document_can_confirm_earnings(self):
        """공시 원문을 로컬에 보존해 인용하는 것이 1차 출처다."""
        context = _context()
        self.assertEqual(
            _errors(
                context,
                _check(evidence_refs=["DARTDOC:20260730800077"]),
                sources=[
                    {
                        "source_id": "DARTDOC:20260730800077",
                        "type": "DART_DOCUMENT",
                        "locator": "https://dart.fss.or.kr/x",
                        "published_at": "2026-07-30T00:00:00+09:00",
                        "retrieved_at": "2026-07-30T01:00:00+09:00",
                    }
                ],
            ),
            [],
        )

    def test_earnings_confirmed_by_a_news_article_is_rejected(self):
        """1차 출처를 두고 2차 출처를 쓰는 것을 막는다."""
        context = _context()
        self.assertTrue(
            _errors(
                context,
                _check(evidence_refs=["NEWS-1"]),
                sources=[
                    {
                        "source_id": "NEWS-1",
                        "type": "NEWS",
                        "locator": "https://example.invalid/a",
                        "published_at": "2026-07-30T00:00:00+09:00",
                        "retrieved_at": "2026-07-30T01:00:00+09:00",
                    }
                ],
            )
        )

    def test_business_claims_may_use_researched_sources(self):
        """제품·경쟁은 로컬에 없는 것이 정상이다. 조사한 출처가 1차다."""
        context = _context(claims=(("UA-aaa1", "폴드8 사전예약이 잘 됐다"),))
        self.assertEqual(
            _errors(
                context,
                _check(
                    claim_kind="BUSINESS",
                    basis={"metric": "사전예약 대수", "period": "출시 첫 주",
                           "comparison": "전작 대비"},
                    evidence_refs=["NEWS-1"],
                ),
                sources=[
                    {
                        "source_id": "NEWS-1",
                        "type": "NEWS",
                        "locator": "https://example.invalid/a",
                        "published_at": "2026-07-30T00:00:00+09:00",
                        "retrieved_at": "2026-07-30T01:00:00+09:00",
                    }
                ],
            ),
            [],
        )


class TestGhostEvidence(unittest.TestCase):
    def test_an_id_that_points_at_nothing_is_rejected(self):
        context = _context()
        self.assertTrue(_errors(context, _check(evidence_refs=["GHOST-1"])))

    def test_the_error_names_the_missing_id(self):
        context = _context()
        errors = _errors(context, _check(evidence_refs=["GHOST-1"]))
        self.assertTrue(any("GHOST-1" in error for error in errors), errors)


class TestClaimMustSayWhatItMeasures(unittest.TestCase):
    """'1,860%' 가 무엇의 몇 퍼센트인지 적지 않으면 확인이 아니다."""

    def test_missing_claim_kind_is_rejected(self):
        context = _context()
        row = _check()
        del row["claim_kind"]
        self.assertTrue(_errors(context, row))

    def test_unknown_claim_kind_is_rejected(self):
        self.assertTrue(_errors(_context(), _check(claim_kind="WHATEVER")))

    def test_confirmed_without_a_basis_is_rejected(self):
        self.assertTrue(_errors(_context(), _check(basis=None)))

    def test_basis_needs_metric_period_and_comparison(self):
        for missing in ("metric", "period", "comparison"):
            basis = dict(BASIS)
            basis[missing] = ""
            with self.subTest(missing=missing):
                self.assertTrue(_errors(_context(), _check(basis=basis)))

    def test_unverifiable_needs_neither_basis_nor_evidence(self):
        context = _context()
        self.assertEqual(
            _errors(
                context,
                _check(
                    status="UNVERIFIABLE",
                    basis=None,
                    evidence_refs=[],
                    finding="어떤 지표의 1,860%인지 사용자에게 확인이 필요하다",
                ),
            ),
            [],
        )

    def test_unverifiable_still_needs_a_claim_kind(self):
        """무엇에 관한 주장인지는 확인 못 해도 말할 수 있다."""
        context = _context()
        row = _check(status="UNVERIFIABLE", basis=None, evidence_refs=[])
        del row["claim_kind"]
        self.assertTrue(_errors(context, row))


if __name__ == "__main__":
    unittest.main()
