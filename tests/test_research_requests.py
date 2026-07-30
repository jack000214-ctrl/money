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
