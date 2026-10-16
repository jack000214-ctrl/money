"""대기열·목록 화면이 무엇을 빠뜨리지 않는가.

`cio requests`·`docs`·`search`·`retire`·`pipeline` 은 **화면이 유일한 전달
경로**인 자리다. packet 에 실리지 않거나 요약만 실리므로, 화면에서 빠진 항목은
사용자에게도 모델에게도 존재하지 않는다. 빠졌다는 사실 자체가 관측되지 않는
것이 이 계층의 위험이다.

그래서 여기서 고정하는 것은 문구가 아니라 **누락**이다 — 건수와 항목이
빠짐없이 나오는가, 그리고 경고가 조용히 사라지지 않는가.
"""

import unittest

import helpers  # noqa: F401  (저장소 루트를 sys.path 에 올린다)
from cio.documents import format_documents, format_search
from cio.requests import format_requests
from cio.retire import format_survey


def request_entry(request_id, text, count=1, variants=None, unverified=False):
    return {
        "request_id": request_id,
        "text": text,
        "asked_count": count,
        "last_asked_at": "2026-08-07T10:00:00+09:00",
        "from_unverified_assessment": unverified,
        "variants": variants or [],
    }


class TestRequestQueue(unittest.TestCase):
    def state(self, **overrides):
        base = {
            "asset_id": "000660",
            "open": [request_entry("REQ-1", "DRAM 고정거래가격 수집기")],
            "resolved": [],
            "orphan_resolutions": [],
        }
        base.update(overrides)
        return base

    def test_every_open_request_is_listed_with_its_text(self):
        """건수만 맞고 본문이 빠지면 사용자는 무엇을 요청받았는지 모른다."""
        text = format_requests(self.state(open=[
            request_entry("REQ-1", "DRAM 고정거래가격 수집기"),
            request_entry("REQ-2", "부문별 매출 재시도"),
        ]))
        self.assertIn("미해결 2건", text)
        self.assertIn("DRAM 고정거래가격 수집기", text)
        self.assertIn("부문별 매출 재시도", text)

    def test_repeated_requests_sort_before_one_off_ones(self):
        """세 번 요청된 것이 목록 아래에 묻히면 대기열의 뜻이 사라진다."""
        text = format_requests(self.state(open=[
            request_entry("REQ-once", "한 번 요청", count=1),
            request_entry("REQ-thrice", "세 번 요청", count=3),
        ]))
        self.assertLess(text.index("REQ-thrice"), text.index("REQ-once"))

    def test_merged_variants_are_shown_so_a_bad_merge_is_visible(self):
        text = format_requests(self.state(open=[
            request_entry("REQ-1", "대표 문장", variants=["다르게 쓴 같은 요청"])
        ]))
        self.assertIn("다르게 쓴 같은 요청", text)

    def test_a_request_from_an_unverified_assessment_is_flagged(self):
        """계약 미달 판단이 낸 요청을 유효한 것과 같은 줄로 보여주면 안 된다."""
        text = format_requests(self.state(open=[
            request_entry("REQ-1", "요청", unverified=True)
        ]))
        self.assertIn("재검증되지 않음", text)

    def test_resolved_requests_keep_their_note(self):
        text = format_requests(self.state(open=[], resolved=[{
            "request_id": "REQ-9", "text": "닫힌 요청",
            "resolution": {"state": "DONE", "resolved_at": "2026-08-01",
                           "note": "collectors/dart.py 배선"},
        }]))
        self.assertIn("해결 1건", text)
        self.assertIn("collectors/dart.py 배선", text)

    def test_an_empty_queue_says_zero_rather_than_nothing(self):
        """빈 화면과 '0건'은 다르다 — 앞은 고장으로도 읽힌다."""
        text = format_requests(self.state(open=[]))
        self.assertIn("미해결 0건", text)


class TestRetireSurvey(unittest.TestCase):
    def state(self, **overrides):
        base = {
            "asset_id": "000660",
            "live": [{"created_at": "2026-08-07", "assessment_id": "CIO-live"}],
            "contract_failing": [],
            "retired": {"count": 0, "batches": []},
        }
        base.update(overrides)
        return base

    def test_all_three_states_are_always_counted(self):
        """살아 있음·계약 미달·폐기됨은 서로 다른 상태다. 하나가 빠지면 오독된다."""
        text = format_survey(self.state())
        for label in ("살아 있음", "계약 미달", "폐기됨"):
            with self.subTest(label):
                self.assertIn(label, text)

    def test_a_contract_failing_judgement_shows_its_reason(self):
        text = format_survey(self.state(contract_failing=[{
            "created_at": "2026-07-01", "assessment_id": "CIO-old",
            "reason": "drivers 비중 합이 100이 아니다",
        }]))
        self.assertIn("CIO-old", text)
        self.assertIn("drivers 비중 합이 100이 아니다", text)

    def test_the_trap_warning_appears_when_a_judgement_is_contract_failing(self):
        """파일은 남아 있어 직접 열면 살아 있는 판단처럼 보인다 — 그 경고가 핵심이다."""
        text = format_survey(self.state(contract_failing=[{
            "created_at": "2026-07-01", "assessment_id": "CIO-old", "reason": "x",
        }]))
        self.assertIn("패킷에 실리지 않는다", text)

    def test_retired_batches_carry_their_reason(self):
        text = format_survey(self.state(retired={
            "count": 2,
            "batches": [{"retired_on": "2026-08-01", "count": 2,
                         "class": "MANUAL", "reason": "구버전 산물"}],
        }))
        self.assertIn("폐기됨         2건", text)
        self.assertIn("구버전 산물", text)


class TestDocumentSearchRendering(unittest.TestCase):
    def result(self, **overrides):
        base = {
            "asset_id": "000660",
            "pattern": "영업이익",
            "searched_documents": 3,
            "total_matches": 1,
            "returned": 1,
            "truncated": False,
            "treated_as_literal": False,
            "hits": [{"doc_id": "DARTDOC:1", "filing_date": "2026-07-29",
                      "title": "잠정실적", "excerpt": "영업이익 60,542,608"}],
            "not_fetched": [],
            "not_fetched_total": 0,
        }
        base.update(overrides)
        return base

    def test_the_excerpt_is_shown_not_just_the_document_id(self):
        """어느 문서인지만 알려주면 원문을 다시 열어야 한다."""
        text = format_search(self.result())
        self.assertIn("영업이익 60,542,608", text)
        self.assertIn("DARTDOC:1", text)

    def test_a_literal_fallback_is_announced(self):
        """정규식이 아니라 문자 그대로 찾았다는 사실이 0건 원인일 수 있다."""
        text = format_search(self.result(treated_as_literal=True))
        self.assertIn("문자 그대로", text)

    def test_truncation_is_announced_so_zero_extra_is_not_assumed(self):
        text = format_search(self.result(truncated=True, returned=5,
                                         total_matches=40))
        self.assertIn("5건만 표시", text)

    def test_unfetched_filings_are_listed_as_a_known_gap(self):
        """받지 않은 공시를 감추면 '검색해봤는데 없다'가 된다."""
        text = format_search(self.result(not_fetched_total=51, not_fetched=[{
            "accession_number": "20260807800613", "filing_date": "2026-08-07",
            "description": "신규시설투자등",
        }]))
        self.assertIn("51건", text)
        self.assertIn("20260807800613", text)

    def test_zero_matches_still_reports_how_many_documents_were_searched(self):
        text = format_search(self.result(total_matches=0, returned=0, hits=[]))
        self.assertIn("문서 3건에서 0건", text)


class TestDocumentCatalogRendering(unittest.TestCase):
    def catalog(self, **overrides):
        base = {
            "asset_id": "000660",
            "available": [{"doc_id": "DARTDOC:1", "filing_date": "2026-07-29",
                           "description": "잠정실적", "text_chars": 2145}],
            "missing": [{"accession_number": "20260807800613",
                         "filing_date": "2026-08-07",
                         "description": "신규시설투자등"}],
            "fetch_hint": "python -m collectors.dart document <접수번호>",
        }
        base.update(overrides)
        return base

    def test_available_and_unfetched_are_both_reported(self):
        """받은 것만 보여주면 '이 종목 공시는 이게 전부'로 읽힌다."""
        text = format_documents(self.catalog())
        self.assertIn("DARTDOC:1", text)
        self.assertIn("잠정실적", text)
        self.assertIn("20260807800613", text)
        self.assertIn("받지 않은 공시 1건", text)

    def test_the_recipe_to_fetch_is_included(self):
        text = format_documents(self.catalog())
        self.assertIn("collectors.dart document", text)

    def test_a_long_missing_list_is_capped_but_the_count_is_not(self):
        """15건만 찍더라도 '51건 있다'는 사실은 남아야 한다."""
        missing = [{"accession_number": f"2026080780{i:04d}",
                    "filing_date": "2026-08-07", "description": "공시"}
                   for i in range(51)]
        text = format_documents(self.catalog(missing=missing))
        self.assertIn("받지 않은 공시 51건", text)
        self.assertEqual(text.count("2026080780"), 15)


if __name__ == "__main__":
    unittest.main()
