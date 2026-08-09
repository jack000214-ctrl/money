"""직전 판단을 이어받는 경로의 회귀 테스트.

한 종목을 여러 번 묻는 것이 정상 사용법인데, 컨텍스트가 verdict와 확률만
넘기면 모델은 자기가 세운 손절가·무효화 신호를 확인할 수 없다. 여기서 지키는
것은 세 가지다 — 계획이 넘어가는가, 문턱 대조가 산술적으로 맞는가, 계약이
바뀌었을 때 기억이 조용히 사라지지 않는가.
"""

import unittest

from cio.context import (
    CONTEXT_ID_RESERVE_BYTES,
    _bars_since,
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


class TestIntradayTouch(unittest.TestCase):
    """장중에 문턱을 스친 것을 종가만 보면 못 본다.

    2026-08-07 파마리서치가 그랬다 — 직전 계획의 재검토 하단이 340,000원이었고
    그날 저가가 333,000원이었는데, 종가 378,500원만 대조하니 `reask_below_reached`
    가 false 로 남았다. **다시 물어봐야 할 자리를 시스템이 못 본 것이다.**

    고가·저가는 이미 일봉 CSV 에 들어 있다. 새로 수집하는 것이 없고 분봉도
    필요 없다 — 갖고 있던 두 칸을 읽지 않았을 뿐이다.

    종가 기준 `triggered` 는 건드리지 않는다. 스친 것과 닫힌 것은 다른 사실이고,
    둘을 한 칸에 합치면 "종가로 뚫렸다"와 "장중에 스쳤다"가 구분되지 않는다.
    """

    # (날짜, 고가, 저가) — 판단일부터의 일봉
    BARS = [
        ("2026-08-05", 232000.0, 226000.0),
        ("2026-08-06", 240000.0, 231000.0),
        ("2026-08-07", 268000.0, 174000.0),  # 저가가 재검토 하단 195,000 아래
    ]

    def check(self, close, bars=None):
        return _prior_plan_check(
            PLAN,
            latest_close=close,
            close_date="2026-08-07",
            close_is_settled=True,
            bars=self.BARS if bars is None else bars,
        )

    def test_close_based_triggers_are_unchanged_by_bars(self):
        # 회귀 방지: 봉을 넘겨도 종가 기준 결과는 그대로여야 한다.
        with_bars = self.check(228000)
        without = _prior_plan_check(
            PLAN,
            latest_close=228000,
            close_date="2026-08-07",
            close_is_settled=True,
        )
        for key in (
            "triggered",
            "stop_breached",
            "reask_below_reached",
            "reask_above_reached",
            "scale_out_reached",
            "in_entry_range",
        ):
            self.assertEqual(with_bars[key], without[key], key)

    def test_low_that_pierced_reask_below_is_reported(self):
        result = self.check(228000)["intraday"]
        # 종가 228,000 은 재검토 하단 195,000 위인데 장중 저가는 174,000 이었다.
        self.assertIn("reask_below", result["touched"])
        self.assertEqual(result["reask_below_touched_on"], "2026-08-07")

    def test_low_above_the_stop_does_not_count_as_touching_it(self):
        # 저가 174,000 은 재검토 하단 195,000 은 뚫지만 손절 152,000 에는
        # 닿지 않는다. 하나가 스쳤다고 아래 것까지 켜지면 안 된다.
        result = self.check(228000)["intraday"]
        self.assertNotIn("stop_price", result["touched"])
        self.assertIsNone(result["stop_touched_on"])

    def test_low_that_pierced_stop_is_reported(self):
        bars = [("2026-08-07", 268000.0, 150000.0)]
        result = self.check(228000, bars=bars)["intraday"]
        self.assertIn("stop_price", result["touched"])
        self.assertEqual(result["stop_touched_on"], "2026-08-07")

    def test_entry_range_touched_when_bar_overlaps_the_band(self):
        # 174,000~268,000 봉은 175,000~195,000 진입 범위를 통과한다.
        result = self.check(228000)["intraday"]
        self.assertIn("entry_range", result["touched"])
        self.assertEqual(result["entry_range_touched_on"], "2026-08-07")

    def test_first_touch_date_is_the_earliest_not_the_latest(self):
        bars = [
            ("2026-08-05", 300000.0, 290000.0),  # 분할매도 285,000 을 먼저 스침
            ("2026-08-06", 291000.0, 286000.0),
        ]
        result = self.check(240000, bars=bars)["intraday"]
        self.assertEqual(result["scale_out_touched_on"], "2026-08-05")

    def test_untouched_thresholds_stay_out_of_the_list(self):
        bars = [("2026-08-07", 240000.0, 230000.0)]
        result = self.check(235000, bars=bars)["intraday"]
        self.assertEqual(result["touched"], [])
        self.assertIsNone(result["stop_touched_on"])
        self.assertIsNone(result["reask_above_touched_on"])

    def test_range_and_session_count_are_reported(self):
        result = self.check(228000)["intraday"]
        self.assertEqual(result["from"], "2026-08-05")
        self.assertEqual(result["to"], "2026-08-07")
        self.assertEqual(result["sessions"], 3)
        self.assertEqual(result["high"], 268000.0)
        self.assertEqual(result["low"], 174000.0)

    def test_no_bars_means_no_intraday_block_not_a_false_one(self):
        # 봉이 없으면 "안 스쳤다"가 아니라 "모른다"다. false 로 채우면
        # 관측하지 않은 것이 관측된 음성으로 둔갑한다.
        self.assertIsNone(
            _prior_plan_check(
                PLAN,
                latest_close=228000,
                close_date="2026-08-07",
                close_is_settled=True,
                bars=[],
            )["intraday"]
        )
        self.assertIsNone(
            _prior_plan_check(
                PLAN,
                latest_close=228000,
                close_date="2026-08-07",
                close_is_settled=True,
            )["intraday"]
        )

    def test_null_levels_are_skipped_without_crashing(self):
        plan = {**PLAN, "stop_price": None, "scale_out_start": None}
        result = _prior_plan_check(
            plan,
            latest_close=228000,
            close_date="2026-08-07",
            close_is_settled=True,
            bars=self.BARS,
        )["intraday"]
        self.assertIsNone(result["stop_touched_on"])
        self.assertNotIn("stop_price", result["touched"])
        self.assertIn("reask_below", result["touched"])


class TestBarsSince(unittest.TestCase):
    """어느 봉을 문턱 대조에 쓸 것인가."""

    RANGES = [
        ("2026-08-05", 232000.0, 226000.0, 1.0),
        ("2026-08-06", 240000.0, 231000.0, 1.0),
        ("2026-08-07", 268000.0, 174000.0, 1.0),
    ]

    def test_decision_day_is_included(self):
        # 2026-08-07 11:16 판단의 재검토 하단은 **같은 날** 저가에 스쳤다.
        # 판단일을 빼면 이 기능이 존재하는 이유였던 사건이 잡히지 않는다.
        bars = _bars_since(self.RANGES, "2026-08-07T11:16:55+09:00")
        self.assertEqual([row[0] for row in bars], ["2026-08-07"])

    def test_earlier_bars_are_dropped(self):
        bars = _bars_since(self.RANGES, "2026-08-06T09:00:00+09:00")
        self.assertEqual(
            [row[0] for row in bars], ["2026-08-06", "2026-08-07"]
        )

    def test_split_inside_the_window_yields_nothing(self):
        # 분할이 끼면 원가격끼리의 비교가 성립하지 않는다. 그 구간에서
        # 스쳤다·안 스쳤다를 말하는 것은 관측이 아니라 오답이다.
        ranges = [
            ("2026-08-05", 232000.0, 226000.0, 2.0),
            ("2026-08-06", 120000.0, 115000.0, 1.0),
        ]
        self.assertEqual(_bars_since(ranges, "2026-08-05"), [])

    def test_missing_inputs_yield_nothing(self):
        self.assertEqual(_bars_since(None, "2026-08-05"), [])
        self.assertEqual(_bars_since(self.RANGES, None), [])
        self.assertEqual(_bars_since(self.RANGES, "2026-09-01"), [])

    def test_dropped_columns_do_not_leak_into_the_check(self):
        # 분할계수는 대조에 쓰이지 않으므로 넘기지 않는다.
        bars = _bars_since(self.RANGES, "2026-08-05")
        self.assertTrue(all(len(row) == 3 for row in bars))


def packet_with_carried(thesis_chars, signals=3):
    return {
        "validation": {"optional_gaps": []},
        "context": {
            "prior_assessments": {
                "live_count": 1,
                "state": "CARRIED_FORWARD",
                "live": [
                {
                    "assessment_id": "CIO-x",
                    "carried_forward": {
                        "thesis": "가" * thesis_chars,
                        "strongest_counter_thesis": "나" * thesis_chars,
                        "price_plan": PLAN,
                        "invalidation_signals": [
                            {
                                "signal": "다" * 100,
                                "action": "EXIT",
                                "rationale": "라" * 40,
                            }
                        ]
                        * signals,
                    },
                }
                ],
            },
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
        carried = packet["context"]["prior_assessments"]["live"][0]["carried_forward"]
        self.assertIsNotNone(carried["thesis"])

    def test_narrative_goes_before_price_plan(self):
        packet = packet_with_carried(600)
        context_module.MAX_CONTEXT_JSON_BYTES = (
            _compact_size(packet) - 1000 + CONTEXT_ID_RESERVE_BYTES
        )
        _fit_context_budget(packet)
        carried = packet["context"]["prior_assessments"]["live"][0]["carried_forward"]
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
