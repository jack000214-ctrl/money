"""한도 위반·판단 노후화·실적 임박 대조. 미설정 항목은 감시하지 않는다."""
from __future__ import annotations

import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cio import watch_review as wr

NOW = "2026-10-19T10:15:00+09:00"
TODAY = dt.date(2026, 10, 19)

HOLDINGS = [
    {"ticker": "A", "name": "에이", "qty": 100.0},
    {"ticker": "B", "name": "비이", "qty": 100.0},
]


def quotes(a=100000.0, b=100000.0, comparable=True):
    return {
        "A": {"price": a, "comparable": comparable},
        "B": {"price": b, "comparable": comparable},
    }


def policy(**concentration_and_risk):
    return {
        "total_assets_krw": 40_000_000,
        "cash_krw": 0,
        "concentration": {
            "max_single_position_pct_of_total": concentration_and_risk.get("single"),
            "max_sector_pct_of_total": concentration_and_risk.get("sector"),
        },
        "risk": {"max_loss_per_position_krw": concentration_and_risk.get("loss")},
    }


SECTORS = {"A": "TECH", "B": "TECH"}


def run_policy(q, p, costs=None):
    return wr.policy_conditions(HOLDINGS, q, p, SECTORS, costs or {}, NOW)


class TestPolicy(unittest.TestCase):
    def test_single_position_over_limit(self):
        # A 평가액 10,000,000 / 총자산 40,000,000 = 25%
        result = run_policy(quotes(), policy(single=20))
        keys = sorted(result["conditions"])
        self.assertEqual(keys, ["REV|POLICY|single|A", "REV|POLICY|single|B"])
        event = result["conditions"]["REV|POLICY|single|A"]
        self.assertEqual(event["measured"]["value"], 25.0)
        self.assertEqual(event["limit"]["value"], 20)

    def test_at_the_limit_is_not_a_breach(self):
        self.assertEqual(run_policy(quotes(), policy(single=25))["conditions"], {})

    def test_sector_sum_uses_both_holdings(self):
        result = run_policy(quotes(), policy(sector=40))
        self.assertEqual(list(result["conditions"]), ["REV|POLICY|sector|TECH"])
        self.assertEqual(result["conditions"]["REV|POLICY|sector|TECH"]["measured"]["value"], 50.0)

    def test_unset_limits_are_not_monitored_and_reported(self):
        result = run_policy(quotes(), policy())
        self.assertEqual(result["conditions"], {})
        reported = {row["item"] for row in result["not_monitored"]}
        self.assertEqual(
            reported,
            {"concentration.max_single_position_pct_of_total",
             "concentration.max_sector_pct_of_total",
             "risk.max_loss_per_position_krw"},
        )

    def test_missing_policy_file_is_reported_not_defaulted(self):
        result = run_policy(quotes(), None)
        self.assertFalse(result["evaluated"])
        self.assertEqual(result["not_monitored"][0]["item"], "policy")

    def test_not_comparable_quote_skips_the_whole_group(self):
        result = run_policy(quotes(comparable=False), policy(single=1))
        self.assertFalse(result["evaluated"])
        self.assertEqual(result["conditions"], {})

    def test_loss_limit_needs_ledger_cost(self):
        result = run_policy(quotes(a=90000.0), policy(loss=500_000), costs={"A": 100000.0})
        # (100000-90000)*100 = 1,000,000 > 500,000
        self.assertEqual(list(result["conditions"]), ["REV|POLICY|max_loss|A"])
        self.assertTrue(any("B" in r["item"] for r in result["not_monitored"]))

    def test_event_has_no_instruction_fields(self):
        event = run_policy(quotes(), policy(single=20))["conditions"]["REV|POLICY|single|A"]
        self.assertFalse({"action", "recommendation", "score"} & set(event))


def assessment(created="2026-10-01T10:00:00+09:00"):
    return {"assessment_id": "CIO-" + "a" * 20, "created_at": created}


class TestStaleness(unittest.TestCase):
    def run_it(self, review, assessments=None, asked=None, q=None):
        return wr.staleness_conditions(
            HOLDINGS[:1], q or quotes(), assessments if assessments is not None else {"A": assessment()},
            asked or {"A": 100000.0}, review, TODAY, NOW,
        )

    review_unset = {"stale_after_days": None, "price_drift_pct": None,
                    "earnings_within_business_days": None}

    def test_no_assessment_is_reported_without_any_threshold(self):
        result = self.run_it(self.review_unset, assessments={"A": None})
        self.assertEqual(list(result["conditions"]), ["REV|STALE|none|A"])

    def test_unset_thresholds_do_not_fire_and_are_reported(self):
        result = self.run_it(self.review_unset, q=quotes(a=1.0))
        self.assertEqual(result["conditions"], {})
        self.assertEqual(
            {r["item"] for r in result["not_monitored"]},
            {"review.stale_after_days", "review.price_drift_pct"},
        )

    def test_age_threshold(self):
        # 2026-10-01 -> 2026-10-19 = 18일
        fired = self.run_it({**self.review_unset, "stale_after_days": 18})
        self.assertEqual(len(fired["conditions"]), 1)
        self.assertEqual(self.run_it({**self.review_unset, "stale_after_days": 19})["conditions"], {})

    def test_drift_threshold_both_directions(self):
        up = self.run_it({**self.review_unset, "price_drift_pct": 10}, q=quotes(a=111000.0))
        down = self.run_it({**self.review_unset, "price_drift_pct": 10}, q=quotes(a=89000.0))
        quiet = self.run_it({**self.review_unset, "price_drift_pct": 10}, q=quotes(a=105000.0))
        self.assertEqual(len(up["conditions"]), 1)
        self.assertEqual(len(down["conditions"]), 1)
        self.assertEqual(quiet["conditions"], {})

    def test_drift_ignored_when_quote_not_comparable(self):
        result = self.run_it({**self.review_unset, "price_drift_pct": 1},
                             q=quotes(a=200000.0, comparable=False))
        self.assertEqual(result["conditions"], {})


class TestEarnings(unittest.TestCase):
    @staticmethod
    def bdays(target, today):
        end = dt.date.fromisoformat(target)
        if end < today:
            return None
        days, cursor = 0, today
        while cursor < end:
            cursor += dt.timedelta(days=1)
            if cursor.weekday() < 5:
                days += 1
        return days

    def run_it(self, limit, calendar):
        return wr.earnings_conditions(
            HOLDINGS[:1], {"A": calendar}, {"earnings_within_business_days": limit},
            TODAY, NOW, self.bdays,
        )

    calendar = {"next_provisional_result": {"disclosure_date": "2026-10-22"}}

    def test_unset_limit_is_not_monitored(self):
        result = self.run_it(None, self.calendar)
        self.assertEqual(result["conditions"], {})
        self.assertEqual(result["not_monitored"][0]["item"], "review.earnings_within_business_days")

    def test_within_limit_fires_beyond_does_not(self):
        self.assertEqual(len(self.run_it(3, self.calendar)["conditions"]), 1)
        self.assertEqual(self.run_it(2, self.calendar)["conditions"], {})

    def test_missing_calendar_is_reported(self):
        result = self.run_it(5, None)
        self.assertEqual(result["conditions"], {})
        self.assertEqual(result["not_monitored"][0]["item"], "earnings:A")

    def test_event_marks_date_as_changeable(self):
        event = next(iter(self.run_it(5, self.calendar)["conditions"].values()))
        self.assertIn("바꿀 수 있다", event["basis"])


class TestDedup(unittest.TestCase):
    cond = {"REV|POLICY|single|A": {"type": wr.POLICY_BREACH}}

    def test_fires_once_then_clears_then_fires_again(self):
        events, active = wr.apply_dedup({}, self.cond, ["REV|POLICY|"], NOW)
        self.assertEqual(len(events), 1)
        events, active = wr.apply_dedup(active, self.cond, ["REV|POLICY|"], NOW)
        self.assertEqual(events, [])
        events, active = wr.apply_dedup(active, {}, ["REV|POLICY|"], NOW)
        self.assertEqual((events, active), ([], {}))
        events, _ = wr.apply_dedup(active, self.cond, ["REV|POLICY|"], NOW)
        self.assertEqual(len(events), 1)

    def test_unevaluated_group_state_is_preserved(self):
        _, active = wr.apply_dedup({}, self.cond, ["REV|POLICY|"], NOW)
        events, kept = wr.apply_dedup(active, {}, ["REV|STALE|"], NOW)
        self.assertEqual(events, [])
        self.assertIn("REV|POLICY|single|A", kept)

    def test_price_trigger_keys_are_untouched(self):
        events, kept = wr.apply_dedup({"005930|CIO-x|stop_price": {}}, {}, ["REV|"], NOW)
        self.assertIn("005930|CIO-x|stop_price", kept)


class TestMissingCost(unittest.TestCase):
    def test_held_quantity_without_cost_is_reported(self):
        found = wr.missing_cost_conditions(HOLDINGS, {"A": 50000.0}, NOW)
        self.assertEqual(list(found), ["REV|AVGCOST|B"])
        event = found["REV|AVGCOST|B"]
        self.assertEqual(event["type"], "MISSING_COST")
        self.assertEqual(event["qty"], 100.0)

    def test_all_costs_present_reports_nothing(self):
        self.assertEqual(
            wr.missing_cost_conditions(HOLDINGS, {"A": 1.0, "B": 2.0}, NOW), {}
        )

    def test_zero_quantity_is_not_reported(self):
        holdings = [{"ticker": "A", "name": "에이", "qty": 0.0}]
        self.assertEqual(wr.missing_cost_conditions(holdings, {}, NOW), {})

    def test_event_text_says_what_is_missing(self):
        from cio import watch_notify

        event = wr.missing_cost_conditions(HOLDINGS, {}, NOW)["REV|AVGCOST|A"]
        text = watch_notify.format_event(event)
        self.assertIn("평단", text["body"])
        self.assertIn("평단 미입력", text["title"])


if __name__ == "__main__":
    unittest.main()
