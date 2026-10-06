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


class TestRetireOneOfMany(TempKnowledge):
    """살아 있는 판단이 여럿일 때 하나만 찍어 폐기할 수 있어야 한다.

    없으면 선택지가 둘뿐이다 — 전부(`--all`) 폐기하거나 손으로 파일을 옮기거나.
    앞은 방금 기록한 새 판단까지 같이 버리고, 뒤는 폐기 기록이 남지 않는다.
    실제로 그 자리에 걸렸다: 실적 공시로 오전 판단이 대체됐는데 새 판단이
    이미 저장돼 있어 `--all` 을 쓸 수 없었다.
    """

    def setUp(self):
        super().setUp()
        original_root = retire_module.ROOT
        retire_module.ROOT = Path(self.tmp.name)
        self.addCleanup(setattr, retire_module, "ROOT", original_root)
        self.add_assessment("214450", "CIO-old0000000000000000")
        self.add_assessment("214450", "CIO-new0000000000000000")

    def remaining(self):
        state = retire_module.survey("214450")
        return sorted(
            row["assessment_id"]
            for row in state["live"] + state["contract_failing"]
        )

    def test_targeting_one_leaves_the_other_alone(self):
        result = retire_module.retire(
            "214450",
            reason="새 판단으로 대체됨",
            assessment_id="CIO-old0000000000000000",
            confirm=True,
        )
        self.assertEqual(
            [row["assessment_id"] for row in result["moved"]],
            ["CIO-old0000000000000000"],
        )
        self.assertEqual(self.remaining(), ["CIO-new0000000000000000"])

    def test_without_an_id_both_would_go(self):
        """지목이 없으면 둘 다 대상이다 — 그래서 지목이 필요하다."""
        result = retire_module.retire("214450", reason="확인만")
        self.assertEqual(len(result["would_retire"]), 2)

    def test_dry_run_still_shows_only_that_one(self):
        result = retire_module.retire(
            "214450", reason="확인만", assessment_id="CIO-old0000000000000000"
        )
        self.assertFalse(result["confirmed"])
        self.assertEqual(
            [row["assessment_id"] for row in result["would_retire"]],
            ["CIO-old0000000000000000"],
        )
        self.assertEqual(len(self.remaining()), 2)

    def test_unknown_id_is_refused_rather_than_retiring_everything(self):
        with self.assertRaises(retire_module.RetireError):
            retire_module.retire(
                "214450", reason="오타", assessment_id="CIO-typo", confirm=True
            )
        self.assertEqual(len(self.remaining()), 2)


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

    def test_long_retirement_reason_is_clipped_in_the_packet_only(self):
        # 폐기 사유에 옛 판단의 지정가·논지를 길게 적는 관행이 생겼다. 12건이 쌓이자
        # 사유만 17.5KB — packet 예산의 22% — 가 되어 살아 있는 판단의 논지를 밀어냈고,
        # '인용하지 않는다'는 폐기의 뜻과 달리 인용할 수 있는 가격을 실어 보냈다.
        reason = "2026-08-10 CIO-b 로 대체됨. " + "이 판단의 TRIM 30주 지정가 405,000원은 집행됐다. " * 20
        write(
            self.knowledge / "retired" / "214450" / "CIO-b.json",
            {
                "assessment_id": "CIO-b",
                "created_at": "2026-08-09T10:00:00+09:00",
                "_retired": {
                    "retired_at": "2026-08-10T09:00:00+09:00",
                    "class": retire_module.USER_DECLARED,
                    "reason": reason,
                },
            },
        )
        packet_reason = context_module._memory_digest("214450")["retired"]["batches"][0]["reason"]
        self.assertTrue(packet_reason.startswith("2026-08-10 CIO-b 로 대체됨"))
        self.assertLess(len(packet_reason), 200)
        self.assertIn("cio retire", packet_reason)
        # 사람이 보는 화면에는 전문이 그대로 남는다.
        self.assertEqual(retire_module.retired_summary("214450")["batches"][0]["reason"], reason)


if __name__ == "__main__":
    unittest.main()
