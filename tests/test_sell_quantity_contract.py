"""팔 수 없는 수량을 팔라고 지시할 수 없어야 한다.

초판 검증기는 매도 수량이 양수인지만 봤다. 그래서 다음이 전부 통과했다.

  95주 보유 중 TRIM 999주
  95주 보유 중 EXIT  1주
  미보유 종목에 TRIM / EXIT / HOLD

매도는 되돌릴 수 없는 행동이고, 이 계약은 종목이 좋은지 나쁜지를 판정하지 않는다.
**보유 수량과 지시 수량의 부등호 비교**일 뿐이며 그래서 PC 의 일이다. 어디서 팔지,
얼마나 팔지는 여전히 전부 모델이 정한다.

`EXIT` 은 전량 매도라는 뜻이다. 95주 중 1주를 팔면서 `EXIT` 이라고 적으면 다음
판단의 packet 은 이 종목이 정리됐다고 읽는다 — 수량 오류가 아니라 상태 오류다.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cio.decision import context_id_for, validate_decision
from test_cio_core import valid_context, valid_decision


def _packet(held=True, quantity=95.0):
    context = valid_context()
    context["context"] = {
        "portfolio": {
            "existing_position": {
                "held": held,
                "quantity": quantity if held else None,
                "value_as_of": "2026-07-27",
            }
        }
    }
    context["context_id"] = context_id_for(context)
    return context


def _decision(context, direction, quantity, sell_reason="VALUATION"):
    data = valid_decision()
    data["context_id"] = context["context_id"]
    data["verdict"] = "WAIT"
    data["proposed_trade"] = {**data["proposed_trade"], "amount": 0}
    selling = direction in {"TRIM", "EXIT"}
    data["action"] = {
        "direction": direction,
        "quantity": quantity,
        # 매도 지시에는 얼마에 걸지가 함께 있어야 한다. 이 파일이 보는 것은
        # 수량 대조이므로 가격은 유효한 값을 하나 채워 두고 지나간다.
        "limit_price": 190000.0 if selling else None,
        "sell_reason": sell_reason if selling else None,
        "rationale": "이유",
    }
    return data


def _errors(context, *args, **kwargs):
    return [
        error
        for error in validate_decision(_decision(context, *args, **kwargs), context=context)
        if error.startswith("action")
    ]


class TestSellingMoreThanHeld(unittest.TestCase):
    def test_trimming_more_than_held_is_rejected(self):
        context = _packet(quantity=95)
        self.assertTrue(_errors(context, "TRIM", 999), "95주 보유 중 999주 축소가 통과했다")

    def test_trimming_exactly_all_is_rejected(self):
        """전량이면 TRIM 이 아니라 EXIT 이다. 둘을 섞으면 잔여수량이 흐려진다."""
        context = _packet(quantity=95)
        self.assertTrue(_errors(context, "TRIM", 95))

    def test_partial_trim_passes(self):
        context = _packet(quantity=95)
        self.assertEqual(_errors(context, "TRIM", 40), [])


class TestExitMeansAllOfIt(unittest.TestCase):
    def test_exit_of_one_share_is_rejected(self):
        """95주 중 1주를 팔면서 EXIT 이라 적으면 다음 packet 이 정리됐다고 읽는다."""
        context = _packet(quantity=95)
        self.assertTrue(_errors(context, "EXIT", 1))

    def test_exit_of_everything_passes(self):
        context = _packet(quantity=95)
        self.assertEqual(_errors(context, "EXIT", 95), [])

    def test_fractional_holdings_are_allowed(self):
        """이 사용자의 원장에는 TSLA 10.004156주가 있다. 정수주를 강요하지 않는다."""
        context = _packet(quantity=10.004156)
        self.assertEqual(_errors(context, "EXIT", 10.004156), [])
        self.assertEqual(_errors(context, "TRIM", 5.0), [])


class TestNoPositionMeansNoSellOrder(unittest.TestCase):
    def test_trimming_what_is_not_held_is_rejected(self):
        context = _packet(held=False)
        self.assertTrue(_errors(context, "TRIM", 10))

    def test_exiting_what_is_not_held_is_rejected(self):
        context = _packet(held=False)
        self.assertTrue(_errors(context, "EXIT", 10))

    def test_holding_what_is_not_held_is_rejected(self):
        """HOLD 는 '계속 들고 있어라'는 지시다. 없는 것에 낼 수 없다."""
        context = _packet(held=False)
        self.assertTrue(_errors(context, "HOLD", None))

    def test_not_applicable_is_the_answer_when_nothing_is_held(self):
        context = _packet(held=False)
        self.assertEqual(_errors(context, "NOT_APPLICABLE", None), [])

    def test_not_applicable_while_holding_is_rejected(self):
        """보유 중인데 해당 없음이라고 답하면 판단을 건너뛴 것이다."""
        context = _packet(quantity=95)
        self.assertTrue(_errors(context, "NOT_APPLICABLE", None))


class TestWithoutPositionDataNothingIsAsserted(unittest.TestCase):
    """원장이 없으면 수량 대조를 하지 않는다. 없는 값을 지어내지 않는다."""

    def test_missing_portfolio_leaves_quantity_unchecked(self):
        context = valid_context()
        context["context"] = {}
        context["context_id"] = context_id_for(context)
        self.assertEqual(_errors(context, "TRIM", 40), [])

    def test_held_without_quantity_leaves_quantity_unchecked(self):
        context = _packet(quantity=None)  # pyright: ignore[reportArgumentType]
        self.assertEqual(_errors(context, "TRIM", 40), [])


if __name__ == "__main__":
    unittest.main()
