"""진입가는 "그 가격이면 산다"로 읽힌다 — 가격 말고 기다리는 것이 있으면 적는다.

2026-10-13 파마리서치 판단은 WAIT 이면서 선호 진입가 262,000원을 냈다. 논지에는
"3분기 발표 전에는 더 사지 않는다"고 적었지만 그 조건은 가격 계획 어디에도 없었다.
화면은 `선호 진입 262,000` 만 보여 줬고, 이틀 뒤 주가가 그 가격에 오자 사용자는
"262,000원이면 다시 찾아오라고 했다"며 매수를 물었다. 같은 날의 판단도 같은 숫자를
같은 WAIT 로 다시 냈다. 진입가가 사건 조건부라는 사실이 서술에만 있으면 사용자는
그 가격마다 같은 질문을 하고 같은 답을 듣는다.

PC 가 보는 것은 판정이 아니라 부등호다 — 검토 가격이 자기 진입 범위 상단 이하인데
새 자금을 넣지 않는다면, 가격 말고 무엇을 기다리는지가 출력에 있어야 한다. 무엇을
기다릴지는 전부 모델이 정한다. 이 요구는 그것을 선언한 새 context 에만 걸린다.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cio.decision import context_id_for, validate_decision
from test_cio_core import valid_context, valid_decision


def _context(*, required=True, price=100.0):
    context = valid_context()
    context["request"]["price"] = price
    if required:
        context["model_contract"]["entry_conditions_required"] = True
    context["context_id"] = context_id_for(context)
    return context


def _errors(context, *, verdict="WAIT", conditions=None, amount=0.0):
    data = valid_decision()
    data["context_id"] = context["context_id"]
    data["verdict"] = verdict
    data["proposed_trade"]["amount"] = amount
    if conditions is not None:
        data["price_plan"]["entry_conditions"] = conditions
    return [
        error for error in validate_decision(data, context=context)
        if "entry_conditions" in error or "price_plan" in error
    ]


class TestWaitingInsideYourOwnEntryRangeNeedsAReason(unittest.TestCase):
    """fixture 의 진입 범위는 90~100 이고 검토 가격은 100 이다."""

    def test_wait_at_the_top_of_the_entry_range_without_a_condition_is_refused(self):
        errors = _errors(_context())
        self.assertTrue(errors)
        self.assertIn("entry_conditions", errors[0])

    def test_wait_below_the_entry_range_without_a_condition_is_refused(self):
        self.assertTrue(_errors(_context(price=88.0)))

    def test_avoid_inside_the_entry_range_is_refused_the_same_way(self):
        self.assertTrue(_errors(_context(), verdict="AVOID"))

    def test_naming_what_is_awaited_passes(self):
        self.assertEqual(
            _errors(_context(), conditions=["3분기 잠정실적에서 내수 매출이 전년동기를 넘는다"]),
            [],
        )

    def test_wait_above_the_entry_range_is_waiting_for_the_price(self):
        """가격이 진입 범위 위면 기다리는 것이 가격 자체다. 조건이 없어도 된다."""
        self.assertEqual(_errors(_context(price=105.0)), [])

    def test_buy_now_is_not_asked_for_a_condition(self):
        self.assertEqual(_errors(_context(), verdict="BUY_NOW", amount=500.0), [])

    def test_a_context_that_did_not_declare_the_rule_is_not_held_to_it(self):
        """저장된 판단은 당시 context 로 재검증된다. 소급하면 살아 있는 판단이
        계약 미달로 끊기고 그 판단의 사전 약속까지 다음 packet 에서 사라진다."""
        self.assertEqual(_errors(_context(required=False)), [])


class TestTheFieldItself(unittest.TestCase):
    def test_conditions_are_optional_everywhere_else(self):
        self.assertEqual(_errors(_context(required=False), conditions=None), [])

    def test_conditions_must_be_sentences(self):
        self.assertTrue(_errors(_context(), conditions=[""]))
        self.assertTrue(_errors(_context(), conditions="3분기 실적"))

    def test_an_empty_list_is_no_condition(self):
        self.assertTrue(_errors(_context(), conditions=[]))
        self.assertEqual(_errors(_context(required=False), conditions=[]), [])

    def test_unknown_price_plan_fields_are_still_refused(self):
        data = valid_decision()
        context = _context(required=False)
        data["context_id"] = context["context_id"]
        data["price_plan"]["entry_note"] = "x"
        self.assertTrue(
            any("price_plan" in error for error in validate_decision(data, context=context))
        )


if __name__ == "__main__":
    unittest.main()
