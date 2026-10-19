"""매도 지시에 실행 가격을 담을 자리가 없었다.

2026-08-07 11:16 판단이 그랬다. 계약이 담을 수 있었던 것은 이만큼이다.

    action.direction  TRIM
    action.quantity   30
    price_plan.scale_out_start  470,000

그런데 실제 지시는 "390,000원 이상에서 30주를 줄이면 된다"였고, 그 숫자는
`action.rationale` 문단 안에만 있었다. 화면(`cio show`)은 `scale_out_start`
470,000 을 보여 줬다. **읽는 사람이 본 숫자와 실행해야 할 숫자가 달랐다.**

그날 실적 발표는 몇 분 만에 주가를 15% 움직였고, 문단 안의 가격은 지정가로
걸리지 못했다. 계획이 틀려서가 아니라 실행 가능한 형태가 아니어서 0원이 됐다.

`scale_out_start` 로는 대신할 수 없다. 그것은 "논지가 앞서가면 줄이기 시작할
가격"이라는 **미래 조건**이고, 여기 필요한 것은 "이 수량을 지금 이 가격에
걸어라"는 **현재 지시**다. 둘을 한 칸에 넣으면 어느 쪽인지 사라진다.

여기서 고정하는 성질은 셋이다. 매도 지시에는 실행 가격이 있다. 그 가격은
지시가 아닌 자리에는 없다. 그리고 수량과 가격이 **화면에 찍힌다** — 계약에만
있고 화면에 없으면 8월 7일과 똑같이 문단 안에 묻힌다.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cio.decision import context_id_for, validate_decision
from cio.report import format_assessment
from test_cio_core import valid_context, valid_decision


def _packet(quantity=95.0):
    context = valid_context()
    context["context"] = {
        "portfolio": {
            "existing_position": {
                "held": True,
                "quantity": quantity,
                "value_as_of": "2026-07-27",
            }
        }
    }
    context["context_id"] = context_id_for(context)
    return context


def _decision(context, action):
    data = valid_decision()
    data["context_id"] = context["context_id"]
    data["verdict"] = "WAIT"
    data["proposed_trade"] = {**data["proposed_trade"], "amount": 0}
    data["action"] = action
    return data


def _errors(context, action):
    return [
        error
        for error in validate_decision(_decision(context, action), context=context)
        if error.startswith("action")
    ]


def trim(**overrides):
    action = {
        "direction": "TRIM",
        "quantity": 30.0,
        "limit_price": 390000.0,
        "sell_reason": "VALUATION",
        "rationale": "이유",
    }
    action.update(overrides)
    return action


class TestSellingNeedsAPrice(unittest.TestCase):
    def test_a_trim_with_an_execution_price_is_accepted(self):
        self.assertEqual(_errors(_packet(), trim()), [])

    def test_a_trim_without_an_execution_price_is_rejected(self):
        action = trim()
        del action["limit_price"]
        errors = _errors(_packet(), action)
        self.assertTrue(errors, "실행 가격 없는 매도 지시가 통과했다")
        self.assertIn("limit_price", errors[0])

    def test_a_null_execution_price_is_rejected_too(self):
        # 키만 있고 값이 없는 것은 없는 것과 같다.
        self.assertTrue(_errors(_packet(), trim(limit_price=None)))

    def test_an_exit_needs_one_as_well(self):
        action = trim(direction="EXIT", quantity=95.0)
        del action["limit_price"]
        self.assertTrue(_errors(_packet(), action))

    def test_a_non_positive_price_is_rejected(self):
        self.assertTrue(_errors(_packet(), trim(limit_price=0)))
        self.assertTrue(_errors(_packet(), trim(limit_price=-1)))


class TestWhereItMustNotAppear(unittest.TestCase):
    """지시가 아닌 자리에 가격이 있으면 그것이 지시로 읽힌다."""

    def test_hold_cannot_carry_an_execution_price(self):
        action = {
            "direction": "HOLD",
            "quantity": None,
            "limit_price": 390000.0,
            "sell_reason": None,
            "rationale": "이유",
        }
        errors = _errors(_packet(), action)
        self.assertTrue(errors, "HOLD 에 실행 가격이 붙었는데 통과했다")

    def test_undetermined_cannot_carry_one(self):
        action = {
            "direction": "UNDETERMINED",
            "undetermined_reason": "NO_POSITION_DATA",
            "quantity": None,
            "limit_price": 390000.0,
            "sell_reason": None,
            "rationale": "이유",
        }
        self.assertTrue(_errors(_packet(), action))

    def test_add_may_carry_one_but_is_not_forced_to(self):
        # ADD 의 진입가는 이미 price_plan.preferred_entry 에 있다. 강제하면
        # 같은 뜻의 숫자가 둘이 되어 서로 어긋날 수 있으므로 허용만 한다.
        context = _packet()
        data = _decision(context, {
            "direction": "ADD",
            "quantity": None,
            "sell_reason": None,
            "rationale": "이유",
        })
        data["verdict"] = "BUY_NOW"
        data["proposed_trade"] = {**data["proposed_trade"], "amount": 1000000}
        without = [
            error for error in validate_decision(data, context=context)
            if error.startswith("action")
        ]
        self.assertEqual(without, [])
        data["action"]["limit_price"] = 320000.0
        with_price = [
            error for error in validate_decision(data, context=context)
            if error.startswith("action")
        ]
        self.assertEqual(with_price, [])


class TestItReachesTheScreen(unittest.TestCase):
    """계약에만 있고 화면에 없으면 8월 7일과 똑같이 문단 안에 묻힌다."""

    def render(self, action):
        data = _decision(_packet(), action)
        data["assessment_id"] = "CIO-" + "0" * 20
        return format_assessment(data, status="LIVE", warnings=[])

    def order_line(self, action):
        lines = [
            line for line in self.render(action).splitlines()
            if "주문:" in line
        ]
        return lines[0] if lines else ""

    def test_the_quantity_is_printed(self):
        # 8월 7일 화면에는 30주라는 숫자 자체가 없었다. `_num` 이 확률 55.0 등
        # 다른 곳에도 숫자를 찍으므로 지시 줄에 있는지로 본다.
        self.assertIn("30주", self.order_line(trim()))

    def test_the_execution_price_is_printed_with_its_currency(self):
        line = self.order_line(trim())
        self.assertIn("390,000", line)
        self.assertIn("지정가", line)
        # 통화 없는 가격은 지정가로 옮겨 적을 수 없다.
        self.assertIn("KRW", line)

    def test_an_add_without_a_quantity_still_shows_its_price(self):
        line = self.order_line({
            "direction": "ADD",
            "quantity": None,
            "limit_price": 320000.0,
            "sell_reason": None,
            "rationale": "이유",
        })
        self.assertIn("320,000", line)
        self.assertNotIn("주 ", line)

    def test_a_hold_does_not_invent_a_quantity_line(self):
        text = self.render({
            "direction": "HOLD",
            "quantity": None,
            "sell_reason": None,
            "rationale": "이유",
        })
        self.assertNotIn("지정가", text)


if __name__ == "__main__":
    unittest.main()
