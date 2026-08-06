"""무효화 신호는 관측이 아니라 **약속**이다.

예전 계약에서 `invalidation_signals` 는 문자열 배열이었다. 신호는 전부 "X가
일어난다"로 끝났고 "그러면 무엇을 한다"가 한 줄도 없었는데, 화면과 문서는
그것을 **유일한 매도 규율**이라고 불렀다. 계약과 설명이 어긋나 있었다.

행동을 나중에 정하면 늦는다. 신호가 실제로 켜지는 날은 가격이 이미 무너진
날이고, 그날 다시 돌린 역산은 낮아진 가격에서 시작하므로 "더 싸졌다"가
기계적으로 도출된다. 그래서 손절가 10,000원짜리 판단이 정작 10,000원에
도달했을 때 20,000원부터 분할매도하라는 답으로 바뀔 수 있다. 이 파일이 고정하는
것은 그 경로다.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cio.context import _pace_since_decision
from cio.decision import context_id_for, validate_decision
from test_cio_core import valid_context, valid_decision


def _packet(*, policy=None, prior_signals=None):
    """investor_policy 와 직전 판단을 실은 packet 을 만든다."""
    context = valid_context()
    body: dict = {}
    if policy is not None:
        body["investor_policy"] = policy
    if prior_signals is not None:
        body["prior_assessments"] = {
            "live_count": 1,
            "state": "CARRIED_FORWARD",
            "live": [
                {
                    "assessment_id": "CIO-" + "a" * 20,
                    "carried_forward": {"invalidation_signals": prior_signals},
                }
            ],
        }
    context["context"] = body
    context["context_id"] = context_id_for(context)
    return context


def _decision(context, **overrides):
    data = valid_decision()
    data["context_id"] = context["context_id"]
    data.update(overrides)
    return data


THESIS_POLICY = {"risk": {"stop_style": "THESIS"}}
PRICE_POLICY = {"risk": {"stop_style": "PRICE"}}


def signal(text="분기 영업이익률이 8% 아래로 내려간다", action="EXIT"):
    return {
        "signal": text,
        "action": action,
        "rationale": "정상화 이익을 세운 근거가 그 범위였다",
    }


class TestSignalsCarryAnAction(unittest.TestCase):
    def test_a_bare_string_is_no_longer_a_signal(self):
        """관측만 있고 행동이 없는 신호는 매도 규율이 되지 못한다."""
        context = _packet()
        errors = validate_decision(
            _decision(context, invalidation_signals=["현금흐름 악화"]),
            context=context,
        )
        self.assertTrue(
            any("signal·action·rationale" in error for error in errors), errors
        )

    def test_an_unknown_action_is_rejected(self):
        """TRIM 처럼 크기를 정하지 않은 약속은 나중에 1%로도 지킬 수 있다."""
        context = _packet()
        errors = validate_decision(
            _decision(context, invalidation_signals=[signal(action="TRIM")]),
            context=context,
        )
        self.assertTrue(any(".action" in error for error in errors), errors)

    def test_a_signal_without_rationale_is_rejected(self):
        """EXIT 과 TRIM_HALF 를 가른 근거가 없으면 나중의 판단이 읽을 수 없다."""
        context = _packet()
        broken = signal()
        broken.pop("rationale")
        errors = validate_decision(
            _decision(context, invalidation_signals=[broken]), context=context
        )
        self.assertTrue(errors)


class TestThesisStopNeedsASellRule(unittest.TestCase):
    """PC 는 어디서 팔지 정하지 않는다. 팔 자리가 있는지만 센다."""

    def test_all_reask_under_thesis_stop_is_rejected(self):
        context = _packet(policy=THESIS_POLICY)
        errors = validate_decision(
            _decision(
                context, invalidation_signals=[signal(action="REASK")]
            ),
            context=context,
        )
        self.assertTrue(
            any("stop_style=THESIS" in error for error in errors), errors
        )

    def test_one_selling_signal_is_enough(self):
        context = _packet(policy=THESIS_POLICY)
        errors = validate_decision(
            _decision(
                context,
                invalidation_signals=[
                    signal("수주잔고가 두 분기 연속 감소한다", "REASK"),
                    signal(action="TRIM_HALF"),
                ],
            ),
            context=context,
        )
        self.assertEqual(errors, [])

    def test_price_stop_users_may_use_reask_only(self):
        """가격 손절을 쓰는 사용자는 매도 규율이 여기 없어도 된다."""
        context = _packet(policy=PRICE_POLICY)
        errors = validate_decision(
            _decision(
                context, invalidation_signals=[signal(action="REASK")]
            ),
            context=context,
        )
        self.assertEqual(errors, [])

    def test_no_policy_means_no_such_check(self):
        """한도를 모르면 대조하지 않는다. 없는 정책을 지어내지 않는다."""
        context = _packet()
        errors = validate_decision(
            _decision(
                context, invalidation_signals=[signal(action="REASK")]
            ),
            context=context,
        )
        self.assertEqual(errors, [])

    def test_insufficient_is_not_asked_to_promise(self):
        """논지를 못 세운 판단에 매도 약속을 요구하면 근거 없는 약속을 짓는다.

        그 약속은 다음 판단이 지켜야 할 것으로 실려 나가므로 없는 것보다 나쁘다.
        """
        context = _packet(policy=THESIS_POLICY)
        data = _decision(
            context,
            verdict="INSUFFICIENT",
            invalidation_signals=[signal(action="REASK")],
        )
        data["win_probability_pct"] = None
        data["confidence_pct"] = None
        data["expected_return_pct"] = dict.fromkeys(
            ("bear", "base", "bull", "probability_weighted")
        )
        data["scenario_probability_pct"] = dict.fromkeys(("bear", "base", "bull"))
        data["price_plan"] = {**data["price_plan"]}
        for key in data["price_plan"]:
            if key not in {"currency", "reask_events"}:
                data["price_plan"][key] = None
        data["proposed_trade"] = {**data["proposed_trade"], "amount": None}
        data["action"] = {
            **data["action"],
            "direction": "UNDETERMINED",
            "undetermined_reason": "INSUFFICIENT_EVIDENCE",
            "quantity": None,
            "sell_reason": None,
        }
        errors = validate_decision(data, context=context)
        self.assertFalse(
            [error for error in errors if "stop_style=THESIS" in error], errors
        )


class TestPriorCommitmentsMustBeClosed(unittest.TestCase):
    def test_carried_signals_must_all_be_closed(self):
        context = _packet(prior_signals=[signal(), signal("수주잔고 감소")])
        errors = validate_decision(_decision(context), context=context)
        self.assertTrue(
            any("prior_signal_checks" in error for error in errors), errors
        )

    def test_closing_every_signal_passes(self):
        context = _packet(prior_signals=[signal()])
        errors = validate_decision(
            _decision(
                context,
                prior_signal_checks=[
                    {
                        "signal_index": 0,
                        "signal": signal()["signal"],
                        "state": "NOT_FIRED",
                        "committed_action": "EXIT",
                        "taken_action": None,
                        "override_reason": None,
                        "note": "2Q26 영업이익률 9.5%로 범위 안이다",
                    }
                ],
            ),
            context=context,
        )
        self.assertEqual(errors, [])

    def test_breaking_a_commitment_without_a_reason_is_rejected(self):
        """뒤집는 것은 허용한다. 조용히 뒤집는 것은 허용하지 않는다."""
        context = _packet(prior_signals=[signal()])
        errors = validate_decision(
            _decision(
                context,
                prior_signal_checks=[
                    {
                        "signal_index": 0,
                        "signal": signal()["signal"],
                        "state": "FIRED",
                        "committed_action": "EXIT",
                        "taken_action": "REASK",
                        "override_reason": None,
                        "note": "2Q26 영업이익률 7.4%",
                    }
                ],
            ),
            context=context,
        )
        self.assertTrue(
            any("override_reason" in error for error in errors), errors
        )

    def test_breaking_a_commitment_with_a_reason_is_allowed(self):
        context = _packet(prior_signals=[signal()])
        errors = validate_decision(
            _decision(
                context,
                prior_signal_checks=[
                    {
                        "signal_index": 0,
                        "signal": signal()["signal"],
                        "state": "FIRED",
                        "committed_action": "EXIT",
                        "taken_action": "TRIM_HALF",
                        "override_reason": "일회성 충당금이며 현금 유출이 아니다",
                        "note": "2Q26 영업이익률 7.4%",
                    }
                ],
            ),
            context=context,
        )
        self.assertEqual(errors, [])

    def test_closing_fewer_than_carried_is_rejected(self):
        context = _packet(prior_signals=[signal(), signal("수주잔고 감소")])
        errors = validate_decision(
            _decision(
                context,
                prior_signal_checks=[
                    {
                        "signal_index": 0,
                        "signal": signal()["signal"],
                        "state": "NOT_FIRED",
                        "committed_action": "EXIT",
                        "taken_action": None,
                        "override_reason": None,
                        "note": "확인함",
                    }
                ],
            ),
            context=context,
        )
        self.assertTrue(
            any("닫지 않은" in error and "index 1" in error for error in errors),
            errors,
        )

    def test_dropped_signals_are_not_demanded(self):
        """packet 예산이 신호를 버렸으면 닫을 대상이 없다.

        계약이 packet 예산에 종속되면, 컨텍스트가 길어졌다는 이유로 판단이
        저장되지 않는다.
        """
        context = _packet(prior_signals=None)
        errors = validate_decision(_decision(context), context=context)
        self.assertEqual(errors, [])


class TestPaceIsMeasured(unittest.TestCase):
    """수익이 얼마나 **빨리** 왔는지가 packet 에 없으면 아무 일도 없는 것이 된다."""

    def _pace(self, **overrides):
        kwargs = {
            "decided_at": "2026-08-01T22:43:48+09:00",
            "asked_price": 43700,
            "horizon_months": 24,
            "win_threshold_pct": 25.4,
            "latest_close": 49050.0,
            "close_date": "2026-08-05",
            "close_is_settled": False,
            "target_annual_return_pct": 12,
        }
        kwargs.update(overrides)
        return _pace_since_decision(**kwargs)

    def test_a_fast_gain_is_visible_even_when_no_threshold_is_touched(self):
        pace = self._pace()
        self.assertEqual(pace["elapsed_days"], 4)
        self.assertAlmostEqual(pace["return_since_decision_pct"], 12.2426, places=3)
        # 24개월치 승리 문턱의 절반 가까이를 나흘에 받았다.
        self.assertGreater(pace["progress_to_win_threshold_pct"], 45)
        self.assertGreater(pace["pace_progress_over_elapsed_x"], 50)

    def test_it_is_measured_against_the_users_own_objective(self):
        """앵커는 모델 자기 출력이 아니라 사용자가 정한 연 목표수익률이다."""
        pace = self._pace()
        self.assertAlmostEqual(pace["objective_annual_return_pct"], 12)
        self.assertGreater(pace["realized_over_objective_x"], 50)

    def test_annualising_is_not_used(self):
        """이틀 +10% 를 연율화하면 천문학적 숫자가 나와 아무 의미가 없다."""
        self.assertNotIn("annualized_return_pct", self._pace())

    def test_same_day_question_has_no_pace(self):
        """경과 0일이면 속도라는 개념이 성립하지 않는다."""
        pace = self._pace(close_date="2026-08-01")
        self.assertEqual(pace["elapsed_days"], 0)
        self.assertIsNone(pace["pace_progress_over_elapsed_x"])
        self.assertIsNone(pace["realized_over_objective_x"])

    def test_a_loss_reads_as_behind_schedule(self):
        pace = self._pace(latest_close=39000.0)
        self.assertLess(pace["return_since_decision_pct"], 0)
        self.assertLess(pace["pace_progress_over_elapsed_x"], 0)

    def test_missing_inputs_yield_nothing_rather_than_a_guess(self):
        self.assertIsNone(self._pace(asked_price=None))
        self.assertIsNone(self._pace(latest_close=None))
        self.assertIsNone(self._pace(close_date=None))
        self.assertIsNone(self._pace(decided_at=None))

    def test_it_does_not_tell_anyone_to_sell(self):
        """PC 는 몇 배부터 무엇을 하라는 문턱을 갖고 있지 않다."""
        pace = self._pace()
        self.assertIn("_not_a_judgment", pace)
        for key in pace:
            self.assertNotIn("should", key)
            self.assertNotIn("action", key)


if __name__ == "__main__":
    unittest.main()
