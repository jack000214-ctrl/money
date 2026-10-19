"""가격 문턱 대조: 방향, 경계, 중복 억제, 재진입, 만료 단계."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cio import watch_triggers as wt

TODAY = "2026-10-19"
NOW = "2026-10-19T10:15:00+09:00"


def assessment(**plan):
    base = {
        "currency": "KRW",
        "preferred_entry": None, "entry_range_low": None, "entry_range_high": None,
        "stop_price": None, "scale_out_start": None,
        "reask_below": None, "reask_above": None, "reask_events": [],
    }
    base.update(plan)
    return {
        "assessment_id": "CIO-" + "a" * 20,
        "created_at": "2026-10-15T10:00:00+09:00",
        "verdict": "HOLD",
        "action": {"direction": "HOLD"},
        "price_plan": base,
        "invalidation_signals": [
            {"signal": "분기 영업이익이 3조 아래", "action": "EXIT", "rationale": "x"}
        ],
    }


def quote(price, comparable=True, currency="KRW"):
    return {"price": price, "comparable": comparable, "currency": currency,
            "quote_time": NOW, "price_kind": "REGULAR_LIVE", "status": "OK"}


def run(a, q, active=None):
    return wt.evaluate_asset(
        ticker="005930", name="삼성전자", assessment=a, quote=q,
        active=active or {}, today=TODAY, now_iso=NOW,
    )


def names(result):
    return [e["threshold"]["name"] for e in result["events"]]


class TestDirections(unittest.TestCase):
    def test_down_thresholds_fire_at_or_below(self):
        a = assessment(stop_price=200000.0, reask_below=210000.0)
        self.assertEqual(names(run(a, quote(215000))), [])
        self.assertEqual(names(run(a, quote(210000))), ["reask_below"])
        self.assertEqual(sorted(names(run(a, quote(199999)))), ["reask_below", "stop_price"])

    def test_up_thresholds_fire_at_or_above(self):
        a = assessment(scale_out_start=230000.0, reask_above=250000.0)
        self.assertEqual(names(run(a, quote(229999))), [])
        self.assertEqual(names(run(a, quote(230000))), ["scale_out_start"])
        self.assertEqual(sorted(names(run(a, quote(251000)))), ["reask_above", "scale_out_start"])

    def test_entry_range_fires_only_inside_inclusive(self):
        a = assessment(entry_range_low=190000.0, entry_range_high=200000.0)
        for price, expected in ((189999, []), (190000, ["entry_range"]),
                                (195000, ["entry_range"]), (200000, ["entry_range"]),
                                (200001, [])):
            with self.subTest(price):
                self.assertEqual(names(run(a, quote(price))), expected)

    def test_null_thresholds_are_ignored(self):
        self.assertEqual(run(assessment(), quote(1))["events"], [])


class TestFactsOnly(unittest.TestCase):
    def test_event_has_no_score_or_recommendation_field(self):
        event = run(assessment(stop_price=200000.0), quote(199000))["events"][0]
        forbidden = {"score", "rating", "grade", "recommendation", "rank", "confidence"}
        self.assertFalse(forbidden & set(event))

    def test_stop_event_carries_signal_text_verbatim_without_parsing(self):
        event = run(assessment(stop_price=200000.0), quote(199000))["events"][0]
        self.assertEqual(
            event["invalidation_signals"],
            [{"signal": "분기 영업이익이 3조 아래", "action": "EXIT"}],
        )

    def test_scale_out_event_does_not_attach_signals(self):
        event = run(assessment(scale_out_start=230000.0), quote(231000))["events"][0]
        self.assertIsNone(event["invalidation_signals"])

    def test_intent_mapping_only_chooses_the_question(self):
        self.assertEqual(wt.intent_for("stop_price"), "HOLD_REVIEW")
        self.assertEqual(wt.intent_for("reask_below"), "HOLD_REVIEW")
        self.assertEqual(wt.intent_for("scale_out_start"), "TRIM_REVIEW")
        self.assertEqual(wt.intent_for("exit_stage:s1"), "TRIM_REVIEW")


class TestDedup(unittest.TestCase):
    def test_same_threshold_is_reported_once(self):
        a = assessment(stop_price=200000.0)
        first = run(a, quote(199000))
        self.assertEqual(len(first["events"]), 1)
        second = run(a, quote(198000), first["active"])
        self.assertEqual(second["events"], [])
        self.assertEqual(len(second["active"]), 1)

    def test_leaving_and_touching_again_fires_again(self):
        a = assessment(stop_price=200000.0)
        first = run(a, quote(199000))
        left = run(a, quote(205000), first["active"])
        self.assertEqual(left["active"], {})
        again = run(a, quote(199500), left["active"])
        self.assertEqual(len(again["events"]), 1)

    def test_not_comparable_quote_changes_nothing(self):
        a = assessment(stop_price=200000.0)
        first = run(a, quote(199000))
        stale = run(a, quote(250000, comparable=False), first["active"])
        self.assertEqual(stale["events"], [])
        self.assertEqual(stale["active"], first["active"], "대조 불가 시세가 상태를 지우면 안 된다")
        self.assertEqual(stale["skipped"], "QUOTE_NOT_COMPARABLE")

    def test_new_assessment_resets_old_state(self):
        old = assessment(stop_price=200000.0)
        first = run(old, quote(199000))
        new = assessment(stop_price=190000.0)
        new["assessment_id"] = "CIO-" + "b" * 20
        result = run(new, quote(199000), first["active"])
        self.assertEqual(result["events"], [])
        self.assertEqual(result["active"], {})

    def test_no_live_assessment_clears_state(self):
        a = assessment(stop_price=200000.0)
        first = run(a, quote(199000))
        result = run(None, quote(199000), first["active"])
        self.assertEqual(result["active"], {})
        self.assertEqual(result["skipped"], "NO_LIVE_ASSESSMENT")


class TestExitStages(unittest.TestCase):
    def with_stage(self, valid_from, valid_until, limit=240000.0):
        a = assessment()
        a["exit_plan"] = {
            "currency": "KRW", "reference_date": "2026-10-15", "reference_basis": "x",
            "stages": [{
                "stage_id": "first_cut", "name": "1차", "quantity": 10,
                "limit_price": limit, "requires_review": True, "depends_on": None,
                "condition": "컨센서스 상향", "cancel_if": "없음",
                "valid_from": valid_from, "valid_until": valid_until,
            }],
        }
        return a

    def test_stage_inside_validity_fires_with_its_commitment(self):
        result = run(self.with_stage("2026-10-01", "2026-12-31"), quote(241000))
        self.assertEqual(names(result), ["exit_stage:first_cut"])
        event = result["events"][0]
        self.assertEqual(event["intent"], "TRIM_REVIEW")
        self.assertEqual(event["promised_action"]["quantity"], 10)
        self.assertEqual(event["promised_action"]["condition"], "컨센서스 상향")

    def test_expired_stage_is_excluded(self):
        result = run(self.with_stage("2026-09-01", "2026-10-18"), quote(250000))
        self.assertEqual(result["events"], [])

    def test_future_stage_is_excluded(self):
        result = run(self.with_stage("2026-10-20", "2026-12-31"), quote(250000))
        self.assertEqual(result["events"], [])

    def test_validity_boundaries_are_inclusive(self):
        for edge in ("2026-10-19",):
            with self.subTest(edge):
                self.assertEqual(
                    names(run(self.with_stage(edge, edge), quote(250000))),
                    ["exit_stage:first_cut"],
                )


class TestCurrency(unittest.TestCase):
    def test_mismatch_is_recorded_as_error_not_event(self):
        result = run(assessment(stop_price=200000.0), quote(100, currency="USD"))
        self.assertEqual(result["events"], [])
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(result["skipped"], "CURRENCY_MISMATCH")


class TestLatestLive(unittest.TestCase):
    def test_picks_newest_live_and_none_when_empty(self):
        rows = [
            {"assessment_id": "old", "created_at": "2026-10-01", "path": "old.json"},
            {"assessment_id": "new", "created_at": "2026-10-10", "path": "new.json"},
        ]
        got = wt.latest_live_assessment(
            "005930",
            survey_fn=lambda _t: {"live": rows},
            load_fn=lambda rel: {"assessment_id": rel},
        )
        self.assertEqual(got, {"assessment_id": "new.json"})
        self.assertIsNone(
            wt.latest_live_assessment(
                "005930", survey_fn=lambda _t: {"live": []}, load_fn=lambda _r: {}
            )
        )


if __name__ == "__main__":
    unittest.main()
