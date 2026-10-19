"""`cio record` 로 저장한 판단의 손절가·재질문가가 감시 문턱으로 읽히는지.

기존 `test_watch_triggers` 는 `survey_fn`/`load_fn` 을 가짜로 주입해 저장 → 조회
경로를 건너뛴다. 여기서는 임시 저장소에 실제 `record_decision_file` 로 판단을 쓰고,
기본 조회 경로(`retire.survey` → `load_json`)로 읽어 `evaluate_asset` 까지 잇는다.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cio import watch_triggers as wt
from cio.memory import record_decision_file
from test_cio_core import valid_context, valid_decision

TODAY = "2026-07-28"
NOW = "2026-07-28T10:15:00+09:00"
RECORD_AT = dt.datetime.fromisoformat("2026-07-28T10:05:00+09:00")


def quote(price):
    return {"price": price, "comparable": True, "currency": "KRW",
            "quote_time": NOW, "price_kind": "REGULAR_LIVE", "status": "OK"}


class TestRecordedAssessmentFeedsWatch(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.knowledge = self.root / "knowledge"
        context = valid_context()
        context_path = (
            self.knowledge / "contexts" / "005930" / f"{context['context_id']}.json")
        context_path.parent.mkdir(parents=True)
        context_path.write_text(json.dumps(context), encoding="utf-8")
        self.decision_path = self.root / "decision.json"
        self.decision_path.write_text(json.dumps(valid_decision()), encoding="utf-8")

        for target in (
            "cio.paths.ROOT",
            "cio.memory.ROOT", "cio.memory.KNOWLEDGE_DIR",
            "cio.decision.ROOT", "cio.decision.KNOWLEDGE_DIR",
            "cio.retire.ROOT", "cio.retire.KNOWLEDGE_DIR",
        ):
            value = self.root if target.endswith("ROOT") else self.knowledge
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def record(self):
        return record_decision_file(self.decision_path, now=RECORD_AT)

    def evaluate(self, price, active=None):
        assessment = wt.latest_live_assessment("005930")
        return wt.evaluate_asset(
            ticker="005930", name="삼성전자", assessment=assessment,
            quote=quote(price), active=active or {}, today=TODAY, now_iso=NOW,
        )

    def test_nothing_recorded_means_nothing_to_compare(self):
        self.assertIsNone(wt.latest_live_assessment("005930"))
        self.assertEqual(self.evaluate(100000)["skipped"], "NO_LIVE_ASSESSMENT")

    def test_recorded_assessment_is_found_with_its_plan(self):
        saved = json.loads(self.record().read_text(encoding="utf-8"))
        found = wt.latest_live_assessment("005930")
        self.assertEqual(found["assessment_id"], saved["assessment_id"])
        self.assertEqual(found["price_plan"]["stop_price"], 160000.0)

    def test_recorded_stop_and_reask_become_thresholds(self):
        self.record()
        result = self.evaluate(165000)  # reask_below(170000) 이하, stop(160000) 초과
        self.assertEqual(
            [e["threshold"]["name"] for e in result["events"]], ["reask_below"])
        event = result["events"][0]
        self.assertEqual(event["threshold"]["value"], 170000.0)
        self.assertEqual(event["intent"], "HOLD_REVIEW")
        self.assertEqual(event["recorded_verdict"], "WAIT")

    def test_stop_price_touch_is_reported_once(self):
        self.record()
        first = self.evaluate(159000)
        self.assertEqual(
            sorted(e["threshold"]["name"] for e in first["events"]),
            ["reask_below", "stop_price"])
        again = self.evaluate(158000, active=first["active"])
        self.assertEqual(again["events"], [])

    def test_price_between_thresholds_raises_nothing(self):
        self.record()
        self.assertEqual(self.evaluate(220000)["events"], [])


if __name__ == "__main__":
    unittest.main()
