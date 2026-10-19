"""색인에 있는데 안 받아서 못 본 것은 데이터가 없는 것이 아니다.

2026-08-07 판단이 답하지 못한 질문 — 영업이익률 37.2% 가 믹스인가 판가 경쟁인가 —
의 근거는 품목군별 매출 시계열이었다. 그 숫자는 잠정실적 공정공시 본문의
"기타 투자판단에 참고할 사항" 에 들어 있고, 해당 공시 두 건은 그날 이미
`knowledge/filings/005930.json` 색인에 접수번호까지 실려 있었다. 원문을 받는 데
걸린 시간은 10초였다.

그런데 판단은 그것을 사용자 대기열(`research_requests`)로 넘겼다. refresh 가
색인만 받고 원문은 모델이 직접 받는 설계였기 때문이고, 모델이 안 받으면 아무도
안 받는다.

잠정실적 공정공시는 이 배선에 딱 맞는 문서다 — 2,285자로 짧고, 분기당 한 건이며,
정기보고서보다 먼저 나와서 분기 사이의 공백을 메우는 유일한 1차 자료다. 파마리서치의
품목군별 매출은 **정기보고서에도 없고** 여기에만 있다.

여기서 고정하는 성질은 셋이다. 이미 받은 것은 다시 받지 않는다. 실적을 말하지
않는 공시까지 긁어오지 않는다. 그리고 한 번에 받는 건수에 상한이 있다 — 색인
400일치를 통째로 도는 작업이 refresh 안에 있으면 안 된다.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import helpers  # noqa: F401  (저장소 루트를 sys.path 에 올린다)
from collectors import dart


FILINGS = {
    "asset_id": "005930",
    "source": "DART_LIST",
    "filings": [
        {
            "filing_date": "2026-08-07",
            "form": "PROVISIONAL_RESULT",
            "accession_number": "20260807900190",
            "description": "연결재무제표기준영업(잠정)실적(공정공시)",
        },
        {
            "filing_date": "2026-05-08",
            "form": "PROVISIONAL_RESULT",
            "accession_number": "20260508900237",
            "description": "연결재무제표기준영업(잠정)실적(공정공시)",
        },
        {
            "filing_date": "2026-08-06",
            "form": "OTHER",
            "accession_number": "20260806900128",
            "description": "기업설명회(IR)개최",
        },
        {
            "filing_date": "2026-05-15",
            "form": "QUARTER_REPORT",
            "accession_number": "20260515001997",
            "description": "분기보고서 (2026.03)",
        },
    ],
}


class ProvisionalCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.knowledge = self.root / "knowledge"
        (self.knowledge / "filings").mkdir(parents=True)
        (self.knowledge / "filings" / "005930.json").write_text(
            json.dumps(FILINGS), encoding="utf-8"
        )
        self.fetched: list[str] = []
        patcher = patch.multiple(
            dart, ROOT=self.root, KNOWLEDGE_DIR=self.knowledge
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_sync(self, stored=(), limit=8):
        def fake_fetch(rcept_no, key):
            self.fetched.append(rcept_no)
            return {"x.xml": "본문"}, self.root / "obj" / f"{rcept_no}.json"

        with patch.object(dart, "fetch_document", side_effect=fake_fetch), \
                patch.object(dart, "stored_filing_keys", return_value=set(stored)):
            return dart.sync_provisional_documents(
                "005930", key="k", limit=limit
            )


class TestWhatItFetches(ProvisionalCase):
    def test_it_fetches_provisional_results_from_the_index(self):
        self.run_sync()
        self.assertEqual(
            sorted(self.fetched), ["20260508900237", "20260807900190"]
        )

    def test_filings_that_do_not_state_results_are_left_alone(self):
        # IR 개최까지 긁어오면 refresh 마다 색인 전체를 도는 작업이 된다.
        self.run_sync()
        self.assertNotIn("20260806900128", self.fetched)

    def test_periodic_reports_are_left_alone_too(self):
        # 분기보고서는 100만 자다. 이 배선이 감당할 문서가 아니고, 필요한
        # 자리만 `cio search` 로 읽는 것이 이 저장소의 방식이다.
        self.run_sync()
        self.assertNotIn("20260515001997", self.fetched)

    def test_newest_first(self):
        # 상한에 걸리면 최신부터 남아야 한다. 오래된 것부터 받으면 이번 분기
        # 실적이 상한 밖으로 밀린다.
        self.run_sync(limit=1)
        self.assertEqual(self.fetched, ["20260807900190"])


class TestItDoesNotRefetch(ProvisionalCase):
    def test_already_stored_documents_are_skipped(self):
        self.run_sync(stored={"20260807900190"})
        self.assertEqual(self.fetched, ["20260508900237"])

    def test_nothing_missing_means_no_network_call(self):
        result = self.run_sync(
            stored={"20260807900190", "20260508900237"}
        )
        self.assertEqual(self.fetched, [])
        self.assertEqual(result["fetched"], [])


class TestTheMarker(ProvisionalCase):
    """산출물이 없으면 refresh 가 매번 다시 돈다."""

    def test_a_marker_is_written_with_what_was_fetched(self):
        self.run_sync()
        marker = json.loads(
            (self.knowledge / "filings" / "_documents" / "005930.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(marker["asset_id"], "005930")
        self.assertTrue(marker["collected_at"])
        self.assertEqual(
            sorted(marker["fetched"]), ["20260508900237", "20260807900190"]
        )

    def test_the_marker_is_written_even_when_nothing_was_missing(self):
        # 안 쓰면 "받을 것이 없었다"가 "안 돌았다"와 구분되지 않고, refresh 는
        # 산출물이 없다는 이유로 영원히 다시 돈다.
        self.run_sync(stored={"20260807900190", "20260508900237"})
        self.assertTrue(
            (self.knowledge / "filings" / "_documents" / "005930.json").exists()
        )

    def test_it_records_what_it_could_not_read(self):
        # 원문을 못 받은 것을 조용히 넘기면 다음 refresh 가 같은 실패를
        # 반복하면서도 마커는 성공으로 남는다.
        def boom(rcept_no, key):
            raise OSError("네트워크")

        with patch.object(dart, "fetch_document", side_effect=boom), \
                patch.object(dart, "stored_filing_keys", return_value=set()):
            result = dart.sync_provisional_documents("005930", key="k")
        self.assertEqual(result["fetched"], [])
        self.assertEqual(len(result["failed"]), 2)


class TestItIsWiredIntoRefresh(unittest.TestCase):
    """수집기가 있어도 배선이 없으면 아무도 안 돌린다 — 그게 원래 문제였다."""

    def plan(self):
        from cio.refresh import tasks_for

        return {task.name: task for task in tasks_for("005930")}

    def test_the_task_exists_for_a_dart_asset(self):
        self.assertIn("filing_documents:005930", self.plan())

    def test_it_reruns_when_the_index_changes(self):
        # 새 잠정실적이 색인에 들어오면 원문도 따라 들어와야 한다.
        task = self.plan()["filing_documents:005930"]
        self.assertIn("knowledge/filings/005930.json", task.dependencies)

    def test_a_missing_document_does_not_block_the_packet(self):
        # required 면 원문 하나를 못 받았다고 판단 자체가 멈춘다. 있으면 좋은
        # 입력 때문에 BLOCKED 가 되는 것은 이 저장소가 피하려는 실패다.
        self.assertFalse(self.plan()["filing_documents:005930"].required)

    def test_it_is_not_planned_for_a_us_asset(self):
        from cio.refresh import tasks_for

        names = {task.name for task in tasks_for("0167A0")}
        self.assertNotIn("filing_documents:0167A0", names)


class TestWithoutAnIndex(ProvisionalCase):
    def test_a_missing_index_is_not_a_crash(self):
        (self.knowledge / "filings" / "005930.json").unlink()
        result = self.run_sync()
        self.assertEqual(result["fetched"], [])
        self.assertEqual(result["missing_index"], True)


if __name__ == "__main__":
    unittest.main()
