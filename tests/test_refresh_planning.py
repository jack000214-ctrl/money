"""무엇을 수집할지 정하고, 수집이 실제로 됐는지 판정하는 자리.

`_append_optional` 은 자산마다 어떤 선택 수집기를 돌릴지 정한다. 여기가 새면 그
종목만 조용히 수집기 하나를 잃고, 다른 종목은 멀쩡하므로 눈치채기 어렵다.

`run_refresh` 는 수집기를 돌린 뒤 **산출물이 실제로 생겼는지 다시 확인한다.**
종료코드만 믿으면 안 되기 때문이다 — 수집기가 0 으로 끝나고도 파일을 못 쓰는
경우가 있고, 그것이 `FAILED_ARTIFACT` 다. 이 구분이 없으면 "수집 성공"이라고
찍힌 뒤 packet 이 옛 파일을 싣는다.

`_dart_filing_index_error` 는 공시 색인이 **저장된 원문에서 그대로 다시
나오는지** 본다. 색인은 원문의 파생물이어야 하고, 손으로 고칠 수 있는 별개의
사실이 되면 안 된다.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import helpers  # noqa: F401  (저장소 루트를 sys.path 에 올린다)
from cio import refresh
from cio.refresh import (
    DART_FILING_TYPES,
    RefreshTask,
    _append_optional,
    _dart_filing_index_error,
    inspect_task,
    run_refresh,
)


class TestOptionalCollectorPlanning(unittest.TestCase):
    """등록한 수집기만, 그러나 등록한 것은 빠짐없이."""

    def plan(self, *collectors, asset_id="000660"):
        tasks = []
        _append_optional(tasks, {"asset_id": asset_id,
                                 "optional_collectors": list(collectors)})
        return tasks

    def test_nothing_registered_plans_nothing(self):
        self.assertEqual(self.plan(), [])

    def test_each_registered_collector_becomes_one_task(self):
        tasks = self.plan("KR_FLOW", "KR_LENDING")
        self.assertEqual({task.capability for task in tasks},
                         {"KR_FLOW", "KR_LENDING"})

    def test_an_unregistered_collector_is_not_planned(self):
        """등록하지 않은 수집기가 돌면 없는 근거가 packet 에 생긴다."""
        tasks = self.plan("KR_FLOW")
        self.assertNotIn("KR_LENDING", {task.capability for task in tasks})

    def test_every_optional_task_is_marked_optional(self):
        """선택 수집기의 실패가 판단 전체를 막으면 안 된다."""
        for task in self.plan("KR_FLOW", "KR_LENDING", "KR_INVENTORY"):
            with self.subTest(task.capability):
                self.assertFalse(task.required)

    def test_the_artifact_path_carries_the_asset(self):
        """경로에 종목이 안 들어가면 두 종목이 같은 파일을 덮어쓴다."""
        for task in self.plan("KR_FLOW", "KR_LENDING", asset_id="005930"):
            with self.subTest(task.capability):
                self.assertIn("005930", task.artifact)

    def test_the_command_targets_the_same_asset(self):
        """산출물 경로와 명령의 종목이 어긋나면 남의 데이터를 제 파일에 쓴다."""
        for task in self.plan("KR_FLOW", "KR_LENDING", asset_id="005930"):
            with self.subTest(task.capability):
                self.assertIn("005930", task.command)

    def test_task_names_are_unique(self):
        tasks = self.plan("KR_FLOW", "KR_LENDING", "KR_INVENTORY",
                          "NORMALIZED_EARNINGS", "CONSENSUS")
        names = [task.name for task in tasks]
        self.assertEqual(len(names), len(set(names)))

    def test_flow_and_lending_are_refreshed_daily(self):
        """수급·대차는 매일 바뀐다 — 월 단위로 두면 옛 숫자를 읽는다."""
        for task in self.plan("KR_FLOW", "KR_LENDING"):
            with self.subTest(task.capability):
                self.assertLessEqual(task.max_age_hours, 24)


class TestRunRefreshOutcomes(unittest.TestCase):
    """돌린 뒤에 실제로 생겼는지 다시 본다."""

    def task(self, **overrides):
        base = dict(name="price:000660", capability="PRICE",
                    artifact="knowledge/prices/000660.csv",
                    max_age_hours=24, command=["true"], required=True)
        base.update(overrides)
        return RefreshTask(**base)  # pyright: ignore[reportArgumentType]

    def execute(self, planned, *, states, returncode=0, force=False):
        """`inspect_task` 가 돌려줄 상태를 순서대로 준다.

        `run` 이라는 이름은 `TestCase.run` 을 가려서 테스트가 아예 실행되지
        않는다 — 통과도 실패도 아닌 상태가 된다.

        `DATA_DIR` 도 임시로 돌린다. `run_refresh` 는 로그를 남기므로, 그대로
        두면 테스트가 실제 저장소에 파일을 쓴다 — gitignore 라 눈에 안 띌 뿐
        부작용은 부작용이다.
        """
        queue = list(states)

        def inspect(task, **kwargs):
            task.state = queue.pop(0)
            return task

        completed = type("P", (), {"returncode": returncode,
                                   "stdout": "out", "stderr": ""})()
        with tempfile.TemporaryDirectory() as tmp, \
                patch("cio.refresh.DATA_DIR", Path(tmp)), \
                patch("cio.refresh.ROOT", Path(tmp)), \
                patch("cio.refresh.tasks_for", return_value=[planned]), \
                patch("cio.refresh.inspect_task", side_effect=inspect), \
                patch("cio.refresh.subprocess.run", return_value=completed):
            return run_refresh("000660", force=force)

    def test_a_fresh_artifact_is_not_collected_again(self):
        results, code = self.execute(self.task(), states=["FRESH"])
        self.assertEqual(results[0]["status"], "SKIPPED_FRESH")
        self.assertEqual(code, 0)

    def test_force_collects_even_when_fresh(self):
        results, _ = self.execute(self.task(), states=["FRESH", "FRESH"],
                              force=True)
        self.assertEqual(results[0]["status"], "UPDATED")

    def test_a_stale_artifact_is_collected_and_verified(self):
        results, code = self.execute(self.task(), states=["STALE", "FRESH"])
        self.assertEqual(results[0]["status"], "UPDATED")
        self.assertEqual(code, 0)

    def test_collector_output_is_read_as_utf8(self):
        """Windows 기본(cp949)으로 읽으면 한글 출력에서 읽기 스레드가 죽는다."""
        completed = type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        with tempfile.TemporaryDirectory() as tmp, \
                patch("cio.refresh.DATA_DIR", Path(tmp)), \
                patch("cio.refresh.ROOT", Path(tmp)), \
                patch("cio.refresh.tasks_for", return_value=[self.task()]), \
                patch("cio.refresh.inspect_task",
                      side_effect=lambda task, **kw: setattr(task, "state", "STALE") or task), \
                patch("cio.refresh.subprocess.run", return_value=completed) as run:
            run_refresh("000660")
        self.assertEqual(run.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(run.call_args.kwargs["errors"], "replace")

    def test_exit_zero_without_a_usable_artifact_is_a_failure(self):
        """수집기가 0 으로 끝나고도 파일을 못 쓰는 경우가 있다.

        종료코드만 믿으면 '수집 성공'으로 찍히고 packet 은 옛 파일을 싣는다.
        """
        results, code = self.execute(self.task(), states=["STALE", "STALE"])
        self.assertEqual(results[0]["status"], "FAILED_ARTIFACT")
        self.assertEqual(results[0]["artifact_state"], "STALE")
        self.assertEqual(code, 1)

    def test_a_nonzero_exit_is_a_failure(self):
        results, code = self.execute(self.task(), states=["STALE", "STALE"],
                                 returncode=1)
        self.assertEqual(results[0]["status"], "FAILED")
        self.assertEqual(code, 1)

    def test_a_blocked_dependency_does_not_run_the_command(self):
        results, code = self.execute(self.task(), states=["BLOCKED"])
        self.assertEqual(results[0]["status"], "DEPENDENCY_BLOCKED")
        self.assertEqual(code, 1)

    def test_a_task_without_a_command_says_a_human_must_do_it(self):
        """포트폴리오 원장처럼 사람이 채우는 것이 있다."""
        results, code = self.execute(self.task(command=None), states=["MISSING"])
        self.assertEqual(results[0]["status"], "MANUAL_REQUIRED")
        self.assertEqual(code, 1)

    def test_an_optional_task_failing_does_not_fail_the_run(self):
        """선택 수집기 하나가 없다고 판단 전체를 막으면 안 된다."""
        results, code = self.execute(self.task(command=None, required=False),
                                 states=["MISSING"])
        self.assertEqual(results[0]["status"], "OPTIONAL_MISSING")
        self.assertEqual(code, 0)

    def test_an_optional_failure_is_still_reported(self):
        _results, code = self.execute(
            self.task(required=False), states=["STALE", "STALE"], returncode=1)
        self.assertEqual(code, 0)

    def test_every_run_leaves_a_log(self):
        results, _ = self.execute(self.task(), states=["STALE", "FRESH"])
        self.assertIn("log", results[0])
        self.assertIn("price-000660", results[0]["log"])


class TestDartFilingIndexContract(unittest.TestCase):
    """색인이 원문에서 다시 나오는가. 앞단 검사만 본다 — 뒤는 실제 원문이 필요하다."""

    TYPES = [code for code, _label in DART_FILING_TYPES]

    def index(self, **overrides):
        data = {
            "corp_code": "00164779",
            "since": "20250101",
            "pblntf_types": list(self.TYPES),
            "raw_sources": [{"pblntf_ty": code, "empty": True}
                            for code in self.TYPES],
            "filings": [{"form": "QUARTER_REPORT"}],
        }
        data.update(overrides)
        return data

    def check(self, **overrides):
        return _dart_filing_index_error(self.index(**overrides), "000660")

    def test_a_malformed_corp_code_is_refused(self):
        """DART 고유번호는 8자리다. 종목코드와 헷갈리면 남의 공시를 받는다."""
        self.assertIn("고유번호", self.check(corp_code="000660"))  # pyright: ignore[reportArgumentType]
        self.assertIn("고유번호", self.check(corp_code=None))  # pyright: ignore[reportArgumentType]

    def test_a_missing_since_date_is_refused(self):
        """조회 시작일이 없으면 색인이 무엇을 덮는지 알 수 없다."""
        self.assertIn("조회 시작일", self.check(since=None))  # pyright: ignore[reportArgumentType]
        self.assertIn("조회 시작일", self.check(since="2025-01-01"))  # pyright: ignore[reportArgumentType]

    def test_a_different_filing_type_filter_is_refused(self):
        """필터가 다르면 같은 이름의 색인이 다른 범위를 덮는다."""
        self.assertIn("공시유형 필터",
                      self.check(pblntf_types=["A"]))  # pyright: ignore[reportArgumentType]

    def test_a_raw_source_count_mismatch_is_refused(self):
        self.assertIn("원문 목록이 공시유형 수와 다르다",
                      self.check(raw_sources=[{"pblntf_ty": "A"}]))  # pyright: ignore[reportArgumentType]

    def test_a_raw_source_order_mismatch_is_refused(self):
        """순서가 다르면 어느 원문이 어느 유형인지 대응이 깨진다."""
        reversed_sources = [{"pblntf_ty": code, "empty": True}
                            for code in reversed(self.TYPES)]
        self.assertIn("공시유형 순서와 다르다",
                      self.check(raw_sources=reversed_sources))  # pyright: ignore[reportArgumentType]

    def test_a_malformed_digest_is_refused(self):
        sources = [{"pblntf_ty": code, "empty": True} for code in self.TYPES]
        sources[0] = {"pblntf_ty": self.TYPES[0],
                      "raw_payload_sha256": "짧은지문"}
        self.assertIn("원문 지문 형식", self.check(raw_sources=sources))  # pyright: ignore[reportArgumentType]

    def test_a_missing_raw_file_is_refused(self):
        """지문만 있고 원문이 없으면 색인을 재현할 수 없다."""
        sources = [{"pblntf_ty": code, "empty": True} for code in self.TYPES]
        sources[0] = {"pblntf_ty": self.TYPES[0],
                      "raw_payload_sha256": "0" * 64,
                      "raw_locator": "data/raw/dart/objects/" + "0" * 64
                                     + ".json"}
        self.assertIn("원문 위치가 유효하지 않다",
                      self.check(raw_sources=sources))  # pyright: ignore[reportArgumentType]

    def test_an_empty_filing_list_is_refused(self):
        self.assertIn("제출 문서 수가 유효하지 않다", self.check(filings=[]))  # pyright: ignore[reportArgumentType]

    def test_an_oversized_filing_list_is_refused(self):
        """상한을 넘은 색인은 수집 계약과 다른 방법으로 만들어진 것이다."""
        self.assertIn("제출 문서 수가 유효하지 않다",
                      self.check(filings=[{"form": "OTHER"}] * 200))  # pyright: ignore[reportArgumentType]

    def test_an_index_that_matches_the_empty_sources_is_checked_further(self):
        """원문이 전부 비었으면 색인도 비어 있어야 한다."""
        self.assertIsNotNone(self.check())


class TestDatedArtifactsAreChosenByOneRule(unittest.TestCase):
    """"가장 최근 것"을 고르는 규칙이 곳마다 달랐다.

    refresh 는 파일 속 기준일로, packet 과 수집기는 파일 이름 순으로 골랐다.
    이름과 기준일이 어긋난 파일이 하나 끼면 refresh 가 신선하다고 본 파일과
    packet 이 실은 파일이 달라진다. 그리고 `IBM-*.json` 은 다른 자산
    (`IBM-B-…json`)의 파일까지 집어, 무관한 파일 하나로 검사 결과가 바뀌었다.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def touch(self, relative):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
        return path

    def test_a_longer_asset_id_with_the_same_prefix_is_another_asset(self):
        own = self.touch("knowledge/fundamentals/IBM-2026-03-31.json")
        self.touch("knowledge/fundamentals/IBM-B-2026-06-30.json")
        self.assertEqual(refresh.dated_artifacts(
            self.root / "knowledge" / "fundamentals", "IBM-", ".json"), [own])

    def test_a_name_without_a_date_is_not_a_candidate(self):
        dated = self.touch("knowledge/etfs/SCHD/holdings-2026-07-23.csv")
        self.touch("knowledge/etfs/SCHD/holdings-latest.csv")
        self.touch("knowledge/etfs/SCHD/holdings-2026-07.csv")
        self.assertEqual(refresh.dated_artifacts(
            self.root / "knowledge" / "etfs" / "SCHD", "holdings-", ".csv"), [dated])

    def test_candidates_come_back_oldest_to_newest(self):
        for day in ("2026-10-08", "2025-12-31", "2026-07-22"):
            self.touch(f"knowledge/etfs/X/holdings-{day}.csv")
        names = [path.name for path in refresh.dated_artifacts(
            self.root / "knowledge" / "etfs" / "X", "holdings-", ".csv")]
        self.assertEqual(names, sorted(names))

    def test_refresh_inspects_this_assets_snapshot_not_the_other_assets(self):
        self.touch("knowledge/fundamentals/IBM-2026-03-31.json")
        self.touch("knowledge/fundamentals/IBM-B-2026-06-30.json")
        task = RefreshTask(
            name="fundamentals:IBM", capability="FUNDAMENTALS",
            artifact="knowledge/fundamentals/IBM-*.json", max_age_hours=720,
            command=None)
        with patch.object(refresh, "ROOT", self.root):
            inspected = inspect_task(task)
        self.assertEqual(inspected.resolved_path,
                         "knowledge/fundamentals/IBM-2026-03-31.json")

    def test_a_holdings_file_that_cannot_be_ordered_by_name_is_reported(self):
        self.touch("knowledge/etfs/SCHD/holdings-latest.csv")
        task = RefreshTask(
            name="holdings:SCHD", capability="ETF_HOLDINGS",
            artifact="knowledge/etfs/SCHD/holdings-*.csv", max_age_hours=168,
            command=None)
        with patch.object(refresh, "ROOT", self.root):
            inspected = inspect_task(task)
        self.assertEqual(inspected.state, "STALE")
        self.assertIn("holdings-<기준일>.csv", inspected.reason)  # pyright: ignore[reportArgumentType]


if __name__ == "__main__":
    unittest.main()
