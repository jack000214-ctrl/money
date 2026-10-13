"""모델의 수집 요청 대기열 회귀 테스트.

`research_requests`는 계약의 필수 필드인데 오래도록 읽는 코드가 없었다. 여기서
지키는 것은 요청이 사라지지 않는다는 것, 같은 요청이 묶여 반복 횟수가 보인다는
것, 그리고 해결 판정을 사용자만 한다는 것이다.
"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from calculators.contracts import ContractError
from cio import requests as requests_module
from cio.requests import (
    RequestError,
    format_requests,
    collect_requests,
    open_requests_for_context,
    request_id,
    request_state,
    resolve_request,
)

ASSET = "005930"


def write_assessment(root, name, requests, created_at="2026-07-29T10:00:00+09:00"):
    directory = root / "assessments" / ASSET
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.json").write_text(
        json.dumps(
            {
                "assessment_id": name,
                "asset_id": ASSET,
                "context_id": "CTX-missing",
                "created_at": created_at,
                "verdict": "WAIT",
                "research_requests": requests,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


class RequestLedgerTest(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        patcher = patch.object(requests_module, "KNOWLEDGE_DIR", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)


class TestRequestIdentity(unittest.TestCase):
    def test_whitespace_does_not_change_identity(self):
        self.assertEqual(
            request_id("DRAM 판가를  모아  달라"),
            request_id("  DRAM 판가를 모아 달라  "),
        )

    def test_different_text_is_a_different_request(self):
        self.assertNotEqual(request_id("가"), request_id("나"))

    def test_id_shape_is_stable(self):
        identifier = request_id("무엇이든")
        self.assertTrue(identifier.startswith("REQ-"))
        self.assertEqual(len(identifier), 16)


class TestCollect(RequestLedgerTest):
    def test_no_assessments_is_empty(self):
        self.assertEqual(collect_requests(ASSET), [])

    def test_repeated_request_is_grouped_with_count(self):
        write_assessment(self.root, "CIO-a", ["같은 요청"], "2026-07-01T09:00:00+09:00")
        write_assessment(self.root, "CIO-b", ["같은 요청"], "2026-07-20T09:00:00+09:00")
        entries = collect_requests(ASSET)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["asked_count"], 2)
        self.assertEqual(entries[0]["first_asked_at"][:10], "2026-07-01")
        self.assertEqual(entries[0]["last_asked_at"][:10], "2026-07-20")

    def test_request_survives_an_assessment_that_no_longer_validates(self):
        # 계약이 바뀌어 재검증에 실패해도 요청 자체는 남아야 한다.
        # 여기서 버리면 가장 오래된 미해결 요청부터 사라진다.
        write_assessment(self.root, "CIO-a", ["살아남아야 한다"])
        entries = collect_requests(ASSET)
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0]["from_unverified_assessment"])

    def test_blank_and_non_string_requests_are_dropped(self):
        write_assessment(self.root, "CIO-a", ["   ", 42, None, "진짜 요청"])
        entries = collect_requests(ASSET)
        self.assertEqual([entry["text"] for entry in entries], ["진짜 요청"])


class TestResolve(RequestLedgerTest):
    def setUp(self):
        super().setUp()
        write_assessment(self.root, "CIO-a", ["첫 요청", "둘째 요청"])

    def test_resolved_request_leaves_the_open_list(self):
        identifier = request_id("첫 요청")
        resolve_request(ASSET, identifier, state="DONE", note="만들었다")
        state = request_state(ASSET)
        self.assertEqual([item["text"] for item in state["open"]], ["둘째 요청"])
        self.assertEqual(len(state["resolved"]), 1)
        self.assertEqual(state["resolved"][0]["resolution"]["note"], "만들었다")

    def test_prefix_resolves(self):
        identifier = request_id("첫 요청")
        resolve_request(ASSET, identifier[:10], state="DONE")
        self.assertEqual(len(request_state(ASSET)["resolved"]), 1)

    def test_unknown_request_is_refused(self):
        with self.assertRaises(RequestError):
            resolve_request(ASSET, "REQ-doesnotexist", state="DONE")

    def test_bad_state_is_refused(self):
        with self.assertRaises(RequestError):
            resolve_request(ASSET, request_id("첫 요청"), state="MAYBE")

    def test_resolution_is_always_attributed_to_the_user(self):
        # 모델이 자기 요청을 닫을 수 있으면 "요청했는데 아직 없다"는
        # 신호가 의미를 잃는다.
        resolve_request(ASSET, request_id("첫 요청"), state="DONE")
        ledger = json.loads(
            (self.root / "requests" / f"{ASSET}.json").read_text(encoding="utf-8")
        )
        self.assertEqual(ledger["resolutions"][0]["resolved_by"], "USER")

    def test_resolving_twice_does_not_duplicate(self):
        identifier = request_id("첫 요청")
        resolve_request(ASSET, identifier, state="DONE")
        resolve_request(ASSET, identifier, state="DECLINED", note="생각이 바뀜")
        state = request_state(ASSET)
        self.assertEqual(len(state["resolved"]), 1)
        self.assertEqual(state["resolved"][0]["resolution"]["state"], "DECLINED")

    def test_broken_ledger_is_reported_not_ignored(self):
        path = self.root / "requests" / f"{ASSET}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"schema_version": "wrong"}', encoding="utf-8")
        with self.assertRaises(ContractError):
            request_state(ASSET)


class TestClustering(RequestLedgerTest):
    """모델은 같은 요청을 매번 조금씩 다르게 쓴다. 마침표 하나, 어미 하나가
    달라 목록이 같은 말로 채워지면 asked_count 가 언제나 1이 되어 반복
    신호가 죽는다."""

    HALF_REPORT_A = (
        "2026년 8월 반기보고서 공시 직후 fundamentals와 kr_inventory를 "
        "재수집해 이 판단을 다시 돌려 달라 — 재고·DIO가 이 결론의 축이다"
    )
    HALF_REPORT_B = (
        "2026년 8월 반기보고서 공시 직후 fundamentals와 kr_inventory를 "
        "재수집하고 이 판단을 다시 돌려 달라 — 재고·DIO가 이 결론의 축이고, "
        "지금 미해결 요청 중 가장 시급하다."
    )
    PREFERRED_A = "우선주(005935) 가격을 수집해 시가총액 계산을 정확히 할 수 있게 해 달라"
    PREFERRED_B = "우선주(005935) 가격을 수집해 시가총액 계산을 정확히 할 수 있게 해 달라."

    def test_trailing_punctuation_does_not_split_a_request(self):
        write_assessment(self.root, "CIO-a", [self.PREFERRED_A], "2026-07-01T09:00:00+09:00")
        write_assessment(self.root, "CIO-b", [self.PREFERRED_B], "2026-07-30T09:00:00+09:00")
        state = request_state(ASSET)
        self.assertEqual(len(state["open"]), 1)
        self.assertEqual(state["open"][0]["asked_count"], 2)

    def test_reworded_request_joins_the_same_cluster(self):
        write_assessment(self.root, "CIO-a", [self.HALF_REPORT_A], "2026-07-01T09:00:00+09:00")
        write_assessment(self.root, "CIO-b", [self.HALF_REPORT_B], "2026-07-30T09:00:00+09:00")
        state = request_state(ASSET)
        self.assertEqual(len(state["open"]), 1)
        self.assertEqual(state["open"][0]["asked_count"], 2)
        self.assertEqual(len(state["open"][0]["variants"]), 1)

    def test_different_requests_are_not_merged(self):
        write_assessment(
            self.root, "CIO-a", [self.PREFERRED_A, self.HALF_REPORT_A]
        )
        state = request_state(ASSET)
        self.assertEqual(len(state["open"]), 2)

    def test_short_texts_are_never_fuzzy_matched(self):
        # 짧은 문자열은 유사도가 부풀려진다. '요청 0'과 '요청 1'은 세 글자 중
        # 둘이 같아 0.8이 나오는데 전혀 다른 요청이다.
        write_assessment(self.root, "CIO-a", ["요청 0", "요청 1", "요청 2"])
        self.assertEqual(len(request_state(ASSET)["open"]), 3)

    def test_long_requests_differing_only_in_one_clause_still_merge(self):
        self.assertGreaterEqual(
            requests_module._similar(
                requests_module._compare_key(self.HALF_REPORT_A),
                requests_module._compare_key(self.HALF_REPORT_B),
            ),
            requests_module.SIMILARITY_THRESHOLD,
        )

    def test_representative_is_the_earliest_so_the_id_is_stable(self):
        write_assessment(self.root, "CIO-a", [self.PREFERRED_A], "2026-07-01T09:00:00+09:00")
        first = request_state(ASSET)["open"][0]["request_id"]
        write_assessment(self.root, "CIO-b", [self.PREFERRED_B], "2026-07-30T09:00:00+09:00")
        self.assertEqual(request_state(ASSET)["open"][0]["request_id"], first)

    def test_variants_are_shown_so_a_wrong_merge_is_visible(self):
        write_assessment(self.root, "CIO-a", [self.HALF_REPORT_A], "2026-07-01T09:00:00+09:00")
        write_assessment(self.root, "CIO-b", [self.HALF_REPORT_B], "2026-07-30T09:00:00+09:00")
        rendered = format_requests(request_state(ASSET))
        self.assertIn("↔", rendered)


class TestANewPeriodIsANewRequest(RequestLedgerTest):
    """문자열 유사도는 "1분기 실적 수집"과 "2분기 실적 수집"을 같은 요청으로 본다.

    두 문장의 유사도는 0.98 이다. 한 묶음이 되면 1분기 요청 하나를 DONE 으로
    닫는 순간 아직 받지 않은 2분기 요청이 대기열에서 사라진다.
    """

    TEMPLATE = (
        "2026년 {}분기 최신 실적과 품목별 수출 매출을 공식 공시에서 수집하고 "
        "연결 기준으로 검증해 주세요"
    )

    def setUp(self):
        super().setUp()
        self.first, self.second = self.TEMPLATE.format(1), self.TEMPLATE.format(2)
        write_assessment(self.root, "CIO-a", [self.first], "2026-05-01T09:00:00+09:00")
        write_assessment(self.root, "CIO-b", [self.second], "2026-08-01T09:00:00+09:00")

    def test_two_quarters_are_two_requests(self):
        self.assertEqual(len(request_state(ASSET)["open"]), 2)

    def test_closing_one_quarter_leaves_the_other_open(self):
        resolve_request(ASSET, request_id(self.first), state="DONE", note="1분기 수집")
        state = request_state(ASSET)
        self.assertEqual([entry["text"] for entry in state["open"]], [self.second])
        self.assertEqual(len(state["resolved"]), 1)

    def test_a_re_request_carrying_an_id_and_a_count_still_joins(self):
        """재요청은 "(REQ-…, 네 번째 요청)"을 달고 온다. 숫자 전부를 기간으로
        읽으면 같은 요청의 반복 횟수가 갈라져 반복 신호가 죽는다."""
        again = (
            "2026년 1분기 최신 실적과 품목별 수출 매출을 공식 공시에서 수집하고 "
            "연결 기준으로 검증해 주세요(REQ-a68c073fec83, 4번째 요청, 538,089건)"
        )
        write_assessment(self.root, "CIO-c", [again], "2026-09-01T09:00:00+09:00")
        state = request_state(ASSET)
        self.assertEqual(len(state["open"]), 2)
        first = next(e for e in state["open"] if e["text"] == self.first)
        self.assertEqual(first["asked_count"], 2)

    def test_period_tokens(self):
        signature = requests_module._scope_signature
        self.assertEqual(signature("2026년 1분기 실적"), ("Q1", "Y2026"))
        self.assertEqual(signature("2Q 잠정실적"), ("Q2",))
        self.assertEqual(signature("8월 반기보고서 공시 직후"), ("M8", "반기"))
        self.assertNotEqual(signature("상반기 실적"), signature("하반기 실적"))
        self.assertEqual(signature("REQ-a68c073fec83 재요청(6번째). 538,089,875"), ())


class TestClusterResolution(RequestLedgerTest):
    A = "동종 배수의 시계열을 쌓아 달라. 지금은 스냅샷 비교라 시간축을 볼 수 없다"
    B = "동종 배수의 시계열을 쌓아 달라. 지금은 스냅샷 비교라 시간축을 볼 수 없다."

    def setUp(self):
        super().setUp()
        write_assessment(self.root, "CIO-a", [self.A], "2026-07-01T09:00:00+09:00")

    def test_resolving_covers_a_later_rewording(self):
        # 이미 만든 것을 모델이 다시 요청한다고 미해결로 돌아오면 안 된다.
        resolve_request(ASSET, request_id(self.A), state="DONE", note="만들었다")
        write_assessment(self.root, "CIO-b", [self.B], "2026-07-30T09:00:00+09:00")
        state = request_state(ASSET)
        self.assertEqual(state["open"], [])
        self.assertEqual(len(state["resolved"]), 1)
        self.assertEqual(state["resolved"][0]["asked_count"], 2)

    def test_can_resolve_by_a_member_id_not_just_the_representative(self):
        write_assessment(self.root, "CIO-b", [self.B], "2026-07-30T09:00:00+09:00")
        resolve_request(ASSET, request_id(self.B), state="DONE")
        self.assertEqual(request_state(ASSET)["open"], [])

    def test_resolution_is_stored_against_the_stable_representative(self):
        write_assessment(self.root, "CIO-b", [self.B], "2026-07-30T09:00:00+09:00")
        resolve_request(ASSET, request_id(self.B), state="DONE")
        ledger = json.loads(
            (self.root / "requests" / f"{ASSET}.json").read_text(encoding="utf-8")
        )
        self.assertEqual(len(ledger["resolutions"]), 1)
        self.assertEqual(ledger["resolutions"][0]["request_id"], request_id(self.A))

    def test_context_block_reports_the_cluster_count(self):
        write_assessment(self.root, "CIO-b", [self.B], "2026-07-30T09:00:00+09:00")
        block = open_requests_for_context(ASSET)
        self.assertEqual(block[0]["asked_count"], 2)


class TestContextBlock(RequestLedgerTest):
    def test_most_repeated_request_comes_first(self):
        write_assessment(self.root, "CIO-a", ["한 번만", "여러 번"])
        write_assessment(self.root, "CIO-b", ["여러 번"], "2026-07-30T09:00:00+09:00")
        block = open_requests_for_context(ASSET)
        self.assertEqual(block[0]["text"], "여러 번")
        self.assertEqual(block[0]["asked_count"], 2)

    def test_block_is_capped_and_says_how_many_are_open(self):
        write_assessment(self.root, "CIO-a", [f"요청 {index}" for index in range(9)])
        block = open_requests_for_context(ASSET)
        self.assertEqual(len(block), requests_module.MAX_CONTEXT_REQUESTS + 1)
        self.assertEqual(block[-1]["total_open"], 9)

    def test_long_request_is_clipped(self):
        write_assessment(self.root, "CIO-a", ["가" * 500])
        block = open_requests_for_context(ASSET)
        self.assertLessEqual(
            len(block[0]["text"]), requests_module.MAX_CONTEXT_REQUEST_CHARS + 1
        )

    def test_empty_when_nothing_open(self):
        self.assertEqual(open_requests_for_context(ASSET), [])


if __name__ == "__main__":
    unittest.main()
