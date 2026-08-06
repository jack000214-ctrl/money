"""직전 약속은 위조할 수 없어야 한다.

`prior_signal_checks` 는 나쁜 일이 일어나기 전에 한 약속을 나중의 판단이
마주보게 하려고 만든 것이다. 그런데 초판 검증기는 모델이 **쓴 값**만 보고
packet 이 **실어 보낸 값**과 대조하지 않았다. 그래서 세 가지가 통과했다.

  1. 전혀 다른 가짜 신호 두 줄로 정산한다
  2. 같은 신호를 두 번 적어 개수만 맞춘다
  3. 직전 약속이 EXIT 이었는데 committed_action 을 REASK 로 낮춰 적는다

3번이 가장 나쁘다. 미래의 내가 과거의 내 약속을 다시 쓸 수 있다는 뜻이고,
그러면 사전 약속이라는 장치 전체가 무의미하다 — 손절가 10,000원짜리 판단이
정작 10,000원에서 20,000원부터 분할매도하라는 답으로 바뀌는 것을 막으려고
만든 장치인데, 그 답을 내면서 "직전 약속은 원래 REASK 였다"고 적으면 된다.

그리고 화면에는 `■ 직전 약속 정산` 이 뜬다. 작동하지 않는 안전장치가 작동하는
것처럼 보이는 것은 안전장치가 없는 것보다 나쁘다. 이 파일이 그 경로를 막는다.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cio.decision import context_id_for, validate_decision
from test_cio_core import valid_context, valid_decision

CARRIED = [
    {
        "index": 0,
        "signal": "분기 영업이익률이 8.0% 아래로 2개 분기 연속 내려간다",
        "action": "EXIT",
        "rationale": "정상화 이익 앵커가 그 범위 위에 서 있다",
    },
    {
        "index": 1,
        "signal": "수주잔고가 두 분기 연속 감소한다",
        "action": "REASK",
        "rationale": "단일 수주의 시점 차이로 흔들릴 수 있다",
    },
]


def _packet(carried=CARRIED):
    context = valid_context()
    context["context"] = {
        "prior_assessments": {
            "live_count": 1,
            "state": "CARRIED_FORWARD",
            "live": [
                {
                    "assessment_id": "CIO-" + "a" * 20,
                    "carried_forward": {"invalidation_signals": carried},
                }
            ],
        }
    }
    context["context_id"] = context_id_for(context)
    return context


def _decision(context, checks):
    data = valid_decision()
    data["context_id"] = context["context_id"]
    data["prior_signal_checks"] = checks
    return data


def _honest(index, state="NOT_FIRED", **overrides):
    row = {
        "signal_index": index,
        "signal": CARRIED[index]["signal"],
        "state": state,
        "committed_action": CARRIED[index]["action"],
        "taken_action": None,
        "override_reason": None,
        "note": "확인함",
    }
    row.update(overrides)
    return row


class TestForgedReconciliationIsRejected(unittest.TestCase):
    def test_fabricated_signals_are_rejected(self):
        """개수만 맞춘 가짜 신호 두 줄로 정산할 수 없다."""
        context = _packet()
        checks = [
            {
                "signal_index": 0,
                "signal": "아무 말이나 적은 신호",
                "state": "NOT_FIRED",
                "committed_action": "EXIT",
                "taken_action": None,
                "override_reason": None,
                "note": "n",
            },
            {
                "signal_index": 1,
                "signal": "이것도 아무 말",
                "state": "NOT_FIRED",
                "committed_action": "REASK",
                "taken_action": None,
                "override_reason": None,
                "note": "n",
            },
        ]
        errors = validate_decision(_decision(context, checks), context=context)
        self.assertTrue(
            any(".signal:" in error for error in errors),
            f"packet 이 실어 보낸 신호와 다른 문장이 통과했다: {errors}",
        )

    def test_the_same_signal_twice_is_rejected(self):
        """같은 신호를 두 번 적어 개수만 맞출 수 없다."""
        context = _packet()
        checks = [_honest(0), _honest(0)]
        errors = validate_decision(_decision(context, checks), context=context)
        self.assertTrue(
            any("중복" in error or "닫지 않은" in error for error in errors),
            f"같은 신호를 두 번 닫은 정산이 통과했다: {errors}",
        )

    def test_downgrading_a_prior_commitment_is_rejected(self):
        """직전 약속이 EXIT 인데 REASK 였다고 다시 쓸 수 없다.

        이것이 막히지 않으면 사전 약속 장치 전체가 무의미하다.
        """
        context = _packet()
        checks = [
            _honest(
                0,
                state="FIRED",
                committed_action="REASK",
                taken_action="REASK",
            ),
            _honest(1),
        ]
        errors = validate_decision(_decision(context, checks), context=context)
        self.assertTrue(
            any("committed_action" in error for error in errors),
            f"약속을 낮춰 적은 정산이 통과했다: {errors}",
        )

    def test_an_index_outside_the_packet_is_rejected(self):
        context = _packet()
        checks = [_honest(0), {**_honest(1), "signal_index": 7}]
        errors = validate_decision(_decision(context, checks), context=context)
        self.assertTrue(
            any("signal_index" in error for error in errors), errors
        )

    def test_closing_signals_that_were_never_carried_is_rejected(self):
        """실어 보낸 것이 없는데 닫았다고 적을 수 없다."""
        context = _packet(carried=[])
        errors = validate_decision(
            _decision(context, [_honest(0)]), context=context
        )
        self.assertTrue(errors, "닫을 대상이 없는데 정산이 통과했다")


class TestHonestReconciliationStillPasses(unittest.TestCase):
    def test_closing_every_signal_faithfully_passes(self):
        context = _packet()
        errors = validate_decision(
            _decision(context, [_honest(0), _honest(1)]), context=context
        )
        self.assertEqual(errors, [])

    def test_breaking_a_commitment_with_a_reason_still_passes(self):
        """뒤집는 것은 여전히 허용한다. 막는 것은 조용히 뒤집는 것이다."""
        context = _packet()
        checks = [
            _honest(
                0,
                state="FIRED",
                taken_action="TRIM_HALF",
                override_reason="일회성 충당금이며 영업현금흐름은 유지됐다",
            ),
            _honest(1),
        ]
        errors = validate_decision(_decision(context, checks), context=context)
        self.assertEqual(errors, [])

    def test_clipped_signal_text_still_matches(self):
        """packet 은 긴 신호를 잘라 싣는다. 잘린 표시를 그대로 되돌려줘도 통과한다."""
        long_signal = "가" * 260
        carried = [
            {
                "index": 0,
                "signal": long_signal[:200]
                + "…〔260자 중 200자. 전문은 assessment 파일에 있다〕",
                "action": "EXIT",
                "rationale": "r",
            }
        ]
        context = _packet(carried=carried)
        checks = [
            {
                "signal_index": 0,
                "signal": carried[0]["signal"],
                "state": "NOT_FIRED",
                "committed_action": "EXIT",
                "taken_action": None,
                "override_reason": None,
                "note": "n",
            }
        ]
        errors = validate_decision(_decision(context, checks), context=context)
        self.assertEqual(errors, [])

    def test_legacy_carried_signal_without_action_skips_the_action_check(self):
        """action 이 없는 옛 형식이 실려 오면 약속 대조를 요구하지 않는다."""
        carried = [{"index": 0, "signal": "옛 형식 신호", "action": None, "rationale": None}]
        context = _packet(carried=carried)
        checks = [
            {
                "signal_index": 0,
                "signal": "옛 형식 신호",
                "state": "NOT_FIRED",
                "committed_action": None,
                "taken_action": None,
                "override_reason": None,
                "note": "n",
            }
        ]
        errors = validate_decision(_decision(context, checks), context=context)
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
