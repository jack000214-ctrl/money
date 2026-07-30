"""직전 판단을 이어받는 경로의 회귀 테스트.

한 종목을 여러 번 묻는 것이 정상 사용법인데, 컨텍스트가 verdict와 확률만
넘기면 모델은 자기가 세운 손절가·무효화 신호를 확인할 수 없다. 여기서 지키는
것은 세 가지다 — 계획이 넘어가는가, 문턱 대조가 산술적으로 맞는가, 계약이
바뀌었을 때 기억이 조용히 사라지지 않는가.
"""

import unittest

from cio.context import (
    CONTEXT_ID_RESERVE_BYTES,
    _clip,
    _compact_size,
    _fit_context_budget,
    _prior_plan_check,
)
from cio import context as context_module


PLAN = {
    "currency": "KRW",
    "preferred_entry": 185000,
    "entry_range_low": 175000,
    "entry_range_high": 195000,
    "stop_price": 152000,
    "scale_out_start": 285000,
    "reask_below": 195000,
    "reask_above": 265000,
    "reask_events": ["반기보고서"],
}


class TestClip(unittest.TestCase):
    def test_short_text_is_untouched(self):
        self.assertEqual(_clip("  짧다  ", 100), "짧다")

    def test_long_text_says_it_was_cut(self):
        clipped = _clip("가" * 300, 100)
        self.assertTrue(clipped.startswith("가" * 100))
        self.assertIn("300자 중 100자", clipped)

    def test_non_string_is_none(self):
        self.assertIsNone(_clip(None, 10))
        self.assertIsNone(_clip(42, 10))


class TestPriorPlanCheck(unittest.TestCase):
    def check(self, close):
        return _prior_plan_check(
            PLAN, latest_close=close, close_date="2026-07-28", close_is_settled=True
        )

    def test_untouched_plan_reports_no_trigger(self):
        result = self.check(228000)
        self.assertEqual(result["triggered"], [])
        self.assertFalse(result["stop_breached"])
        self.assertFalse(result["reask_below_reached"])
        self.assertFalse(result["reask_above_reached"])
        self.assertFalse(result["in_entry_range"])

    def test_stop_and_reask_below_fire_together_below_stop(self):
        result = self.check(150000)
        self.assertEqual(
            sorted(result["triggered"]), ["reask_below", "stop_price"]
        )

    def test_entry_range_is_inclusive(self):
        self.assertTrue(self.check(175000)["in_entry_range"])
        self.assertTrue(self.check(195000)["in_entry_range"])
        self.assertFalse(self.check(195001)["in_entry_range"])

    def test_scale_out_and_reask_above(self):
        result = self.check(290000)
        self.assertEqual(
            sorted(result["triggered"]), ["reask_above", "scale_out_start"]
        )

    def test_percent_against_preferred_entry(self):
        self.assertEqual(
            self.check(203500)["close_vs_preferred_entry_pct"], 10.0
        )

    def test_unsettled_close_is_carried_not_hidden(self):
        result = _prior_plan_check(
            PLAN,
            latest_close=228000,
            close_date="2026-07-29",
            close_is_settled=False,
        )
        self.assertIs(result["close_is_settled"], False)

    def test_missing_inputs_give_none(self):
        self.assertIsNone(
            _prior_plan_check(
                PLAN, latest_close=None, close_date=None, close_is_settled=None
            )
        )
        self.assertIsNone(
            _prior_plan_check(
                None, latest_close=1.0, close_date=None, close_is_settled=None
            )
        )

    def test_null_levels_report_none_not_false(self):
        plan = {**PLAN, "stop_price": None}
        result = _prior_plan_check(
            plan, latest_close=100, close_date=None, close_is_settled=True
        )
        self.assertIsNone(result["stop_breached"])
        self.assertNotIn("stop_price", result["triggered"])


def packet_with_carried(thesis_chars, signals=3):
    return {
        "validation": {"optional_gaps": []},
        "context": {
            "prior_assessments": [
                {
                    "assessment_id": "CIO-x",
                    "carried_forward": {
                        "thesis": "가" * thesis_chars,
                        "strongest_counter_thesis": "나" * thesis_chars,
                        "price_plan": PLAN,
                        "invalidation_signals": ["다" * 100] * signals,
                    },
                }
            ],
            "open_research_requests": [
                {"request_id": f"REQ-{index}", "text": "라" * 150}
                for index in range(4)
            ]
            + [{"_role": "설명", "total_open": 4}],
        },
    }


class TestBudgetFitter(unittest.TestCase):
    def setUp(self):
        self.original = context_module.MAX_CONTEXT_JSON_BYTES
        self.addCleanup(
            setattr, context_module, "MAX_CONTEXT_JSON_BYTES", self.original
        )

    def test_within_budget_changes_nothing(self):
        packet = packet_with_carried(50)
        context_module.MAX_CONTEXT_JSON_BYTES = 100_000
        _fit_context_budget(packet)
        self.assertEqual(packet["validation"]["optional_gaps"], [])
        carried = packet["context"]["prior_assessments"][0]["carried_forward"]
        self.assertIsNotNone(carried["thesis"])

    def test_narrative_goes_before_price_plan(self):
        packet = packet_with_carried(600)
        context_module.MAX_CONTEXT_JSON_BYTES = (
            _compact_size(packet) - 1000 + CONTEXT_ID_RESERVE_BYTES
        )
        _fit_context_budget(packet)
        carried = packet["context"]["prior_assessments"][0]["carried_forward"]
        self.assertIsNone(carried["thesis"])
        # 가격 계획은 결정에 직접 쓰이므로 끝까지 남는다.
        self.assertEqual(carried["price_plan"], PLAN)

    def test_result_fits_including_its_own_record_and_context_id(self):
        packet = packet_with_carried(900)
        limit = _compact_size(packet) - 2000
        context_module.MAX_CONTEXT_JSON_BYTES = limit
        _fit_context_budget(packet)
        # context_id 자리까지 남겨두고 맞춰야 build_context 가 실패하지 않는다.
        self.assertLessEqual(
            _compact_size(packet), limit - CONTEXT_ID_RESERVE_BYTES
        )
        gaps = packet["validation"]["optional_gaps"]
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["state"], "TRIMMED_FOR_BUDGET")
        self.assertTrue(gaps[0]["trimmed"])

    def test_role_marker_survives_request_trimming(self):
        packet = packet_with_carried(900)
        context_module.MAX_CONTEXT_JSON_BYTES = 1200
        _fit_context_budget(packet)
        requests = packet["context"]["open_research_requests"]
        self.assertEqual(requests[-1].get("total_open"), 4)


if __name__ == "__main__":
    unittest.main()
