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

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from calculators.contracts import canonical_payload_hash, content_hash
from cio import documents as documents_module
from cio.decision import (
    context_id_for,
    validate_context_integrity,
    validate_decision,
)
from test_cio_core import valid_context, valid_decision


class PreservedStore(unittest.TestCase):
    """임시 폴더에 실제 모양의 보존 원문을 만든다.

    결속 검사를 흉내 내지 않고 진짜 파일 위에서 돌린다 — 검사 대상인 함수를
    패치해 놓고 통과를 확인하면 그 함수가 무엇을 보는지는 시험되지 않는다.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.dart = self.root / "data" / "raw" / "dart" / "objects"
        self.web = self.root / "data" / "raw" / "web" / "objects"
        self.dart.mkdir(parents=True)
        self.web.mkdir(parents=True)
        for target, value in (
            ("ROOT", self.root),
            ("INDEX_PATH", self.root / "data" / "raw" / "document-index.json"),
            ("TEXT_CACHE_DIR", self.root / "data" / "raw" / "text"),
            ("OBJECT_DIRS", (("DART", self.dart), ("WEB", self.web))),
        ):
            patcher = patch.object(documents_module, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        documents_module._PRESERVED_LOCATORS.clear()
        documents_module._VERIFIED_ORIGINALS.clear()
        self.addCleanup(documents_module._PRESERVED_LOCATORS.clear)
        self.addCleanup(documents_module._VERIFIED_ORIGINALS.clear)

    def _store(self, directory, payload, fetched_at, source):
        digest = canonical_payload_hash(payload)
        path = directory / f"{digest}.json"
        path.write_text(json.dumps({
            "fetched_at": fetched_at, "source": source,
            "payload_sha256": digest, "payload": payload}), encoding="utf-8")
        documents_module._PRESERVED_LOCATORS.clear()
        return digest, path

    def preserve_filing(self, rcept_no="20260730800077",
                        fetched_at="2026-07-28T00:30:00+00:00"):
        payload = {"rcept_no": rcept_no, "documents": {
            f"{rcept_no}.xml": "<DOCUMENT-NAME>영업(잠정)실적</DOCUMENT-NAME>"
                               "<P>영업이익 100</P>"}}
        _digest, path = self._store(self.dart, payload, fetched_at, "opendart.fss.or.kr")
        return f"DARTDOC:{rcept_no}", path

    def preserve_page(self, url="https://example.org/ir/q2", *, body=None,
                      fetched_at="2026-07-28T00:30:00+00:00"):
        payload = {"requested_url": url, "final_url": url, "http_status": 200,
                   "content_type": "text/html", "content_length": 80,
                   "body_encoding": "utf-8",
                   "body": body or "<html><title>Q2</title><p>" + "영업이익 " * 300 + "</p></html>"}
        digest, path = self._store(self.web, payload, fetched_at, "web")
        return f"WEBSNAP:{digest[:12]}", path

    @staticmethod
    def source(source_id, locator, *, retrieved_at="2026-07-28T09:30:00+09:00"):
        return {"source_id": source_id, "type": "PRESERVED_ORIGINAL",
                "locator": locator, "published_at": None,
                "retrieved_at": retrieved_at}

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


class TestACitedOriginalIsBoundToItsPreservedDocument(PreservedStore):
    """인용 주소·보존한 본문·그 지문·회수시각이 한 문서를 가리켜야 한다.

    예전에는 ID 가 색인에 있는지만 봤다. 실제 저장 판단 8건의 보존 출처 87개를
    전부 다른 주소로 바꿔도 통과했고, 본문이 지문과 맞지 않는 파일도 이름만
    맞으면 1차 원문(`PRIMARY_DOCUMENT`)이었다. 파일의 존재가 출처 결속으로
    읽히고 있었다.
    """

    DART_URL = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260730800077"

    def cite(self, source):
        data = valid_decision()
        context = _context()
        data["context_id"] = context["context_id"]
        data["sources"] = [source]
        data["user_assertion_checks"] = [_check(evidence_refs=[source["source_id"]])]
        return validate_decision(data, context=context)

    def test_a_preserved_filing_cited_at_its_own_address_can_confirm_earnings(self):
        """공시 원문을 로컬에 보존해 인용하는 것이 1차 출처다."""
        source_id, _ = self.preserve_filing()
        self.assertEqual(self.cite(self.source(source_id, self.DART_URL)), [])

    def test_an_original_that_was_never_preserved_is_only_a_name(self):
        """`DARTDOC:` 라는 이름은 주장이다. 가리키는 원문이 로컬에 없으면 직접
        조사한 출처와 다를 것이 없고, 그것으로 실적을 확인했다고 할 수 없다."""
        errors = self.cite(self.source("DARTDOC:20260730800077", self.DART_URL))
        self.assertEqual(len(errors), 1)
        self.assertIn("MODEL_RESEARCH", errors[0])

    def test_a_preserved_filing_cited_at_another_address_is_refused(self):
        source_id, _ = self.preserve_filing()
        for locator in ("https://example.invalid/replaced-citation",
                        "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260101000001",
                        "https://evil.example/dsaf001/main.do?rcpNo=20260730800077"):
            with self.subTest(locator=locator):
                errors = self.cite(self.source(source_id, locator))
                self.assertTrue(any("locator가" in error for error in errors), errors)
                # 결속되지 않은 출처로는 실적 주장도 닫히지 않는다.
                self.assertTrue(any("MODEL_RESEARCH" in error for error in errors))

    def test_a_preserved_page_must_be_cited_at_the_address_it_was_fetched_from(self):
        source_id, _ = self.preserve_page("https://example.org/ir/q2")
        self.assertEqual(self.cite(self.source(source_id, "https://example.org/ir/q2")), [])
        errors = self.cite(self.source(source_id, "https://example.org/ir/q3"))
        self.assertTrue(any("https://example.org/ir/q2" in error for error in errors), errors)

    def test_a_fragment_or_host_case_is_the_same_document(self):
        source_id, _ = self.preserve_page("https://example.org/ir/q2")
        self.assertEqual(
            self.cite(self.source(source_id, "https://EXAMPLE.org/ir/q2#table-3")), [])

    def test_a_redirected_page_may_be_cited_by_either_address(self):
        payload = {"requested_url": "https://example.org/short",
                   "final_url": "https://example.org/ir/q2", "http_status": 200,
                   "content_type": "text/html", "content_length": 80,
                   "body_encoding": "utf-8",
                   "body": "<html><p>" + "영업이익 " * 300 + "</p></html>"}
        digest, _ = self._store(self.web, payload, "2026-07-28T00:30:00+00:00", "web")
        for locator in ("https://example.org/short", "https://example.org/ir/q2"):
            with self.subTest(locator=locator):
                self.assertEqual(
                    self.cite(self.source(f"WEBSNAP:{digest[:12]}", locator)), [])

    def test_a_file_whose_body_no_longer_matches_its_fingerprint_is_not_an_original(self):
        """파일 이름이 정체성을 만들면 안 된다 — 본문이 바뀌면 그 ID 의 원문이 아니다."""
        source_id, path = self.preserve_filing()
        document = json.loads(path.read_text(encoding="utf-8"))
        document["payload"]["documents"]["20260730800077.xml"] = "<P>영업이익 999</P>"
        path.write_text(json.dumps(document), encoding="utf-8")
        documents_module._PRESERVED_LOCATORS.clear()
        errors = self.cite(self.source(source_id, self.DART_URL))
        self.assertTrue(any("내용 지문과 맞지 않는다" in error for error in errors), errors)

    def test_a_body_filed_under_another_filings_name_is_not_that_filing(self):
        """본문은 온전하지만 다른 접수번호의 것이다."""
        _, path = self.preserve_filing("20260730800077")
        document = json.loads(path.read_text(encoding="utf-8"))
        document["payload"]["rcept_no"] = "20991231000001"
        digest = canonical_payload_hash(document["payload"])
        document["payload_sha256"] = digest
        moved = path.with_name(f"{digest}.json")
        moved.write_text(json.dumps(document), encoding="utf-8")
        path.unlink()
        # 낡은 색인이 여전히 옛 ID 로 이 파일을 가리킨다고 해 보자.
        documents_module._PRESERVED_LOCATORS.clear()
        with patch.object(documents_module, "_preserved_locators", return_value={
                "DARTDOC:20260730800077": (str(moved.relative_to(self.root)),)}):
            errors = self.cite(self.source("DARTDOC:20260730800077", self.DART_URL))
        self.assertTrue(any("내용 지문과 맞지 않는다" in error for error in errors), errors)

    def test_an_original_preserved_after_the_decision_cannot_support_it(self):
        """판단은 2026-07-28 10:00 KST. 그 뒤에 받은 원문은 그때 없던 자료다."""
        source_id, _ = self.preserve_page(fetched_at="2030-01-01T00:00:00+00:00")
        errors = self.cite(self.source(source_id, "https://example.org/ir/q2"))
        self.assertTrue(any("그때 없던 원문" in error for error in errors), errors)

    def test_a_frame_shell_is_kept_as_a_citation_but_is_not_a_primary_document(self):
        """프레임 껍데기는 보존했다는 기록만 있고 내용이 없다."""
        shell = '<html><frameset><frame src="viewer.do"></frameset></html>'
        source_id, _ = self.preserve_page("https://example.org/viewer", body=shell)
        errors = self.cite(self.source(source_id, "https://example.org/viewer"))
        self.assertFalse(any("locator가" in error for error in errors))
        self.assertEqual(len(errors), 1)
        self.assertIn("MODEL_RESEARCH", errors[0])

    def test_the_same_citation_is_valid_whatever_else_is_in_the_store(self):
        """무관한 원문이 늘어도 결속 결과는 같다."""
        source_id, _ = self.preserve_filing()
        before = self.cite(self.source(source_id, self.DART_URL))
        self.preserve_filing("20260801000123")
        self.preserve_page("https://example.org/unrelated")
        self.assertEqual(self.cite(self.source(source_id, self.DART_URL)), before)


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


class TestAModelCannotBorrowALocalEvidenceName(unittest.TestCase):
    """출처의 갈래는 ID 접두사가 아니라 그 ID 가 실제로 가리키는 것이 정한다.

    예전에는 접두사만 봤다. 그래서 뉴스 기사에 `FUNDAMENTALS:…` 라는 이름을
    붙이면 재무 자료로 분류돼 EARNINGS 주장을 CONFIRMED 로 닫을 수 있었다 —
    바꾼 것은 ID 문자열 하나뿐이다.
    """

    def news(self, source_id):
        return {
            "source_id": source_id,
            "type": "NEWS",
            "locator": "https://example.invalid/not-a-financial-statement",
            "published_at": "2026-07-27T00:00:00+00:00",
            "retrieved_at": "2026-07-27T01:00:00+00:00",
        }

    def errors(self, source_id):
        context = _context()
        data = valid_decision()
        data["context_id"] = context["context_id"]
        data["sources"] = [self.news(source_id)]
        data["user_assertion_checks"] = [_check(evidence_refs=[source_id])]
        return validate_decision(data, context=context)

    def test_a_news_source_named_like_financials_is_refused(self):
        errors = self.errors("FUNDAMENTALS:UNPRESERVED_NEWS")
        self.assertTrue(any("로컬 근거의 이름" in error for error in errors))
        self.assertTrue(any("MODEL_RESEARCH" in error for error in errors))

    def test_the_same_source_under_an_honest_name_fails_the_same_way(self):
        errors = self.errors("NEWS-1")
        self.assertFalse(any("로컬 근거의 이름" in error for error in errors))
        self.assertTrue(any("MODEL_RESEARCH" in error for error in errors))

    def test_a_calculation_id_that_points_at_nothing_is_not_a_calculation(self):
        errors = self.errors("CALC-00000000000000000000")
        self.assertTrue(any("MODEL_RESEARCH" in error for error in errors))


class TestLocalEvidenceCannotPostDateTheContext(unittest.TestCase):
    """모델이 조사한 출처는 회수시각이 판단보다 늦을 수 없다. 로컬 근거도 같다.

    그 순서 검사가 로컬 쪽에는 없어서, 기준일이 context 보다 뒤인 자료를 과거
    판단이 인용해도 통과했다.
    """

    def integrity(self, data_as_of, kind="FILING_DERIVED"):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "evidence.json"
            path.write_text("{}", encoding="utf-8")
            context = valid_context()
            context["evidence_index"].append({
                "source_id": "FUNDAMENTALS:TSLA", "kind": kind,
                "path": path.name, "content_sha256": content_hash(path),
                "data_as_of": data_as_of})
            context["context_id"] = context_id_for(context)
            with patch("cio.decision.ROOT", Path(tmp)):
                return [error for error in validate_context_integrity(context)
                        if "FUNDAMENTALS:TSLA" in error]

    def test_evidence_dated_after_the_context_is_refused(self):
        errors = self.integrity("2026-12-31")
        self.assertEqual(len(errors), 1)
        self.assertIn("그때 없던 자료", errors[0])

    def test_evidence_from_the_past_and_the_same_day_passes(self):
        self.assertEqual(self.integrity("2026-06-30"), [])
        self.assertEqual(self.integrity("2026-07-28T08:59:00+09:00"), [])

    def test_a_value_that_is_not_a_date_is_not_judged(self):
        self.assertEqual(self.integrity("policy-v3"), [])
        self.assertEqual(self.integrity(None), [])

    def test_the_users_own_ledger_is_dated_by_the_user(self):
        """원장의 기준일은 사용자가 적은 거래의 날짜(결제일)다. 수집한 관측이
        아니므로 이 검사로 재지 않는다 — 재면 그 원장을 실은 과거 판단이 전부
        채점에서 떨어진다. 실제로 폐기 판단 5건이 그렇게 빠졌다."""
        self.assertEqual(self.integrity("2026-08-01", kind="USER_DATA"), [])


if __name__ == "__main__":
    unittest.main()
