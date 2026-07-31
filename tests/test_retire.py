"""판단 폐기와 그 폐기가 패킷에 보이는 방식."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cio import context as context_module
from cio import retire as retire_module


def write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


class TempKnowledge(unittest.TestCase):
    """retire 와 context 가 같은 임시 knowledge 를 보도록 갈아끼운다."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.knowledge = Path(self.tmp.name) / "knowledge"
        for module, name in (
            (retire_module, "KNOWLEDGE_DIR"),
            (context_module, "KNOWLEDGE_DIR"),
        ):
            original = getattr(module, name)
            setattr(module, name, self.knowledge)
            self.addCleanup(setattr, module, name, original)
        original_retired = retire_module.RETIRED_DIR
        retire_module.RETIRED_DIR = self.knowledge / "retired"
        self.addCleanup(
            setattr, retire_module, "RETIRED_DIR", original_retired
        )

    def add_assessment(self, asset_id: str, assessment_id: str, **extra):
        write(
            self.knowledge / "assessments" / asset_id / f"{assessment_id}.json",
            {
                "assessment_id": assessment_id,
                "created_at": "2026-07-29T10:00:00+09:00",
                "verdict": "WAIT",
                "context_id": "CTX-00000000000000000000",
                "price_plan": {"stop_price": 305000},
                **extra,
            },
        )


class TestRetiredSummary(TempKnowledge):
    def test_empty_when_nothing_retired(self):
        self.assertEqual(
            retire_module.retired_summary("214450"), {"count": 0, "batches": []}
        )

    def test_summary_carries_no_citable_content(self):
        """폐기의 뜻은 '인용하지 않는다'다.

        개수·시점·사유만 낸다. verdict 나 가격계획을 함께 실어 보내면 그
        폐기는 지켜지지 않는다.
        """
        write(
            self.knowledge / "retired" / "214450" / "CIO-a.json",
            {
                "assessment_id": "CIO-a",
                "created_at": "2026-07-29T10:00:00+09:00",
                "verdict": "WAIT",
                "price_plan": {"stop_price": 305000},
                "thesis": "인용되면 안 되는 논지",
                "_retired": {
                    "retired_at": "2026-07-31T09:00:00+09:00",
                    "class": retire_module.CONTRACT_FAILURE,
                    "reason": "구버전 산물",
                },
            },
        )
        summary = retire_module.retired_summary("214450")
        self.assertEqual(summary["count"], 1)
        blob = json.dumps(summary, ensure_ascii=False)
        for leaked in ("WAIT", "305000", "인용되면 안 되는 논지"):
            self.assertNotIn(leaked, blob)

    def test_batches_group_by_date_and_reason(self):
        for index in range(3):
            write(
                self.knowledge / "retired" / "214450" / f"CIO-{index}.json",
                {
                    "assessment_id": f"CIO-{index}",
                    "created_at": f"2026-07-2{index}T10:00:00+09:00",
                    "_retired": {
                        "retired_at": "2026-07-31T09:00:00+09:00",
                        "class": retire_module.USER_DECLARED,
                        "reason": "구버전 산물",
                    },
                },
            )
        summary = retire_module.retired_summary("214450")
        self.assertEqual(summary["count"], 3)
        self.assertEqual(len(summary["batches"]), 1)
        self.assertEqual(summary["batches"][0]["count"], 3)

    def test_file_without_retirement_marker_is_not_counted(self):
        write(
            self.knowledge / "retired" / "214450" / "CIO-a.json",
            {"assessment_id": "CIO-a"},
        )
        self.assertEqual(retire_module.retired_summary("214450")["count"], 0)


class TestRetireRequiresReason(TempKnowledge):
    def test_blank_reason_is_refused(self):
        with self.assertRaises(retire_module.RetireError):
            retire_module.retire("214450", reason="   ")


class TestMemoryDigest(TempKnowledge):
    def test_no_assessments_says_zero_not_silence(self):
        digest = context_module._memory_digest("214450")
        self.assertEqual(digest["live_count"], 0)
        self.assertEqual(digest["state"], "NO_LIVE_JUDGMENT")
        self.assertIn("처음부터 판단한다", digest["_directive"])

    def test_contract_failure_is_reported_without_citable_values(self):
        """계약 미달은 사고다. 사고를 숨기지도, 인용 가능하게 두지도 않는다."""
        self.add_assessment("214450", "CIO-dead")
        digest = context_module._memory_digest("214450")
        self.assertEqual(digest["live_count"], 0)
        self.assertEqual(digest["not_carried"]["count"], 1)
        self.assertEqual(
            digest["not_carried"]["class"], retire_module.CONTRACT_FAILURE
        )
        item = digest["not_carried"]["items"][0]
        self.assertEqual(item["assessment_id"], "CIO-dead")
        # 판정과 가격은 싣지 않는다.
        self.assertNotIn("verdict", item)
        self.assertNotIn("305000", json.dumps(digest, ensure_ascii=False))

    def test_retired_batch_appears_as_history(self):
        write(
            self.knowledge / "retired" / "214450" / "CIO-a.json",
            {
                "assessment_id": "CIO-a",
                "created_at": "2026-07-29T10:00:00+09:00",
                "_retired": {
                    "retired_at": "2026-07-31T09:00:00+09:00",
                    "class": retire_module.USER_DECLARED,
                    "reason": "구버전 산물이라 신뢰할 수 없다",
                },
            },
        )
        digest = context_module._memory_digest("214450")
        self.assertEqual(digest["retired"]["count"], 1)
        self.assertIn(
            "구버전", digest["retired"]["batches"][0]["reason"]
        )


if __name__ == "__main__":
    unittest.main()
