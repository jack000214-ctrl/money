"""Future sell stages are price observations, never assumed executions."""

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cio.memory import _exit_plan_observations, evaluate_assessment
from cio.report import evaluations_for, format_assessment, format_evaluations
from test_cio_core import valid_context, valid_decision


def exit_plan():
    return {
        "currency": "USD", "reference_date": "2026-01-01",
        "reference_basis": "질문 시점 보통주 1주 기준",
        "stages": [
            {"stage_id": "first", "name": "첫 매도", "quantity": 10.004156,
             "limit_price": 10.25, "requires_review": True, "depends_on": None,
             "condition": "실적 확인", "cancel_if": "논지 훼손",
             "valid_from": "2026-01-02", "valid_until": "2026-01-04"},
            {"stage_id": "second", "name": "다음 매도", "quantity": 5,
             "limit_price": 11.00, "requires_review": True, "depends_on": "first",
             "condition": "첫 매도 체결 확인", "cancel_if": "첫 매도 미체결",
             "valid_from": "2026-01-05", "valid_until": "2026-01-06"},
        ],
    }


def bars():
    return [
        {"date": "2026-01-01", "high": 20.0, "low": 4.8, "close": 5.0,
         "adjusted_close": 5.0, "has_adjusted_close": True, "split_adjustment_factor": 0.5},
        {"date": "2026-01-02", "high": 5.10, "low": 4.9, "close": 5.0,
         "adjusted_close": 5.0, "has_adjusted_close": True, "split_adjustment_factor": 1.0},
        {"date": "2026-01-03", "high": 5.20, "low": 5.0, "close": 5.1,
         "adjusted_close": 5.1, "has_adjusted_close": True, "split_adjustment_factor": 1.0},
        {"date": "2026-01-04", "high": 5.30, "low": 5.0, "close": 5.2,
         "adjusted_close": 5.2, "has_adjusted_close": True, "split_adjustment_factor": 1.0},
        {"date": "2026-01-05", "high": 5.60, "low": 5.0, "close": 5.3,
         "adjusted_close": 5.3, "has_adjusted_close": True, "split_adjustment_factor": 1.0},
        {"date": "2026-01-07", "high": 20.0, "low": 5.0, "close": 5.4,
         "adjusted_close": 5.4, "has_adjusted_close": True, "split_adjustment_factor": 1.0},
    ]


class TestExitPlanObservations(unittest.TestCase):
    def test_touch_uses_decision_day_split_basis_validity_and_next_day_review(self):
        assessment = {"assessment_id": "CIO-old", "exit_plan": exit_plan()}
        review = {"assessment_id": "CIO-old", "stage_id": "first", "state": "SATISFIED",
                  "recorded_at": "2026-01-03T10:00:00-05:00", "review_assessment_id": "CIO-review"}
        result = _exit_plan_observations(assessment, bars(), start_factor=0.5,
                                         decision_date="2026-01-01", reviews=[review])
        first, second = result["stages"]
        self.assertEqual(first["split_adjusted_limit_price"], 5.125)
        self.assertEqual(first["price_touch_date"], "2026-01-03")
        self.assertEqual(first["reviewed_price_touch_date"], "2026-01-04")
        self.assertTrue(first["share_basis_changed_at_touch"])
        self.assertEqual(second["price_touch_date"], "2026-01-05")
        self.assertEqual(second["dependency_execution"], "UNKNOWN")
        self.assertEqual(second["execution_eligibility"], "UNKNOWN")
        self.assertEqual(result["actual_execution"], "UNKNOWN")
        self.assertIsNone(result["strategy_return_pct"])

    def test_unobserved_window_and_cancellation_do_not_create_execution(self):
        plan = exit_plan()
        plan["stages"][0]["valid_from"] = "2026-02-01"
        plan["stages"][0]["valid_until"] = "2026-02-03"
        assessment = {"assessment_id": "CIO-old", "exit_plan": plan}
        result = _exit_plan_observations(assessment, bars(), start_factor=0.5,
                                         decision_date="2026-01-01")
        self.assertEqual(result["stages"][0]["price_observation"], "NOT_YET_OBSERVED")
        self.assertIsNone(result["stages"][0]["price_touch_date"])
        self.assertIsNone(result["stages"][0]["reviewed_price_touch_date"])

        plan["stages"][0]["valid_from"] = "2026-01-02"
        plan["stages"][0]["valid_until"] = "2026-01-04"
        cancellation = {"assessment_id": "CIO-old", "stage_id": "first", "state": "CANCELLED",
                        "recorded_at": "2026-01-02T10:00:00-05:00",
                        "review_assessment_id": "CIO-review"}
        result = _exit_plan_observations(assessment, bars(), start_factor=0.5,
                                         decision_date="2026-01-01", reviews=[cancellation])
        self.assertEqual(result["stages"][0]["price_touch_date"], "2026-01-03")
        self.assertIsNone(result["stages"][0]["reviewed_price_touch_date"])
        self.assertEqual(result["stages"][0]["condition_review"], "CANCELLED")

    def test_expired_window_without_price_rows_is_not_called_future(self):
        plan = exit_plan()
        plan["stages"][0]["valid_from"] = "2026-01-06"
        plan["stages"][0]["valid_until"] = "2026-01-06"
        assessment = {"assessment_id": "CIO-old", "exit_plan": plan}
        result = _exit_plan_observations(assessment, bars(), start_factor=0.5,
                                         decision_date="2026-01-01")
        self.assertEqual(result["stages"][0]["price_observation"], "NO_PRICE_ROWS_IN_VALIDITY")
        self.assertIsNone(result["stages"][0]["price_touch_date"])

    def test_cancelled_stage_is_not_revived_by_later_satisfied_check(self):
        assessment = {"assessment_id": "CIO-old", "exit_plan": exit_plan()}
        checks = [
            {"assessment_id": "CIO-old", "stage_id": "first", "state": "CANCELLED",
             "recorded_at": "2026-01-02T10:00:00-05:00", "review_assessment_id": "CIO-cancel"},
            {"assessment_id": "CIO-old", "stage_id": "first", "state": "SATISFIED",
             "recorded_at": "2026-01-03T10:00:00-05:00", "review_assessment_id": "CIO-later"},
        ]
        stage = _exit_plan_observations(assessment, bars(), start_factor=0.5,
                                        decision_date="2026-01-01", reviews=checks)["stages"][0]
        self.assertEqual(stage["price_touch_date"], "2026-01-03")
        self.assertIsNone(stage["reviewed_price_touch_date"])
        self.assertEqual(stage["condition_review"], "CANCELLED")

    def test_dst_fallback_checks_are_sorted_by_instant(self):
        plan = exit_plan()
        plan["reference_date"] = "2026-10-31"
        plan["stages"][0]["valid_from"] = "2026-11-01"
        plan["stages"][0]["valid_until"] = "2026-11-03"
        checks = [
            {"assessment_id": "CIO-old", "stage_id": "first", "state": "SATISFIED",
             "recorded_at": "2026-11-01T01:30:00-04:00", "review_assessment_id": "CIO-before"},
            {"assessment_id": "CIO-old", "stage_id": "first", "state": "CANCELLED",
             "recorded_at": "2026-11-01T01:15:00-05:00", "review_assessment_id": "CIO-after"},
        ]
        rows = [{"date": "2026-11-02", "high": 11.0, "split_adjustment_factor": 1.0}]
        stage = _exit_plan_observations({"assessment_id": "CIO-old", "exit_plan": plan}, rows,
                                        start_factor=1.0, decision_date="2026-10-31",
                                        reviews=checks)["stages"][0]
        self.assertEqual(stage["latest_condition_check"]["state"], "CANCELLED")
        self.assertEqual(stage["condition_review"], "CANCELLED")
        self.assertIsNone(stage["reviewed_price_touch_date"])

    def test_evaluation_attaches_exit_plan_without_strategy_return(self):
        assessment = valid_decision()
        assessment["assessment_id"] = "CIO-" + "1" * 20
        assessment["created_at"] = "2026-01-01T12:00:00-05:00"
        assessment["exit_plan"] = exit_plan()
        context = valid_context()
        with tempfile.TemporaryDirectory() as tmp, \
             patch("cio.memory.KNOWLEDGE_DIR", Path(tmp)), \
             patch("cio.memory.get_asset", return_value={"market": "US", "asset_id": "TSLA", "price_symbol": "TSLA", "benchmarks": []}), \
             patch("cio.memory.find_context", return_value=(context, None)), \
             patch("cio.memory._price_session", return_value=None), \
             patch("cio.memory._fixed_horizon_returns", return_value={}):
            result = evaluate_assessment(assessment, rows=bars())
        self.assertEqual(result["exit_plan"]["stages"][0]["price_touch_date"], "2026-01-03")
        self.assertEqual(result["exit_plan"]["actual_execution"], "UNKNOWN")
        self.assertIsNone(result["exit_plan"]["strategy_return_pct"])

    def test_korean_calendar_date_does_not_drop_first_us_session(self):
        assessment = valid_decision()
        assessment["assessment_id"] = "CIO-" + "1" * 20
        assessment["created_at"] = "2026-09-23T11:30:00-04:00"
        plan = exit_plan()
        plan["reference_date"] = "2026-09-24"
        plan["stages"][0].update(valid_from="2026-09-24", valid_until="2026-09-26")
        assessment["exit_plan"] = plan
        context = valid_context()
        context["request"]["as_of"] = "2026-09-24T00:30:00+09:00"
        price_rows = [
            {"date": "2026-09-23", "high": 10.0, "low": 9.0, "close": 9.5,
             "adjusted_close": 9.5, "has_adjusted_close": True, "split_adjustment_factor": 1.0},
            {"date": "2026-09-24", "high": 10.50, "low": 9.5, "close": 10.0,
             "adjusted_close": 10.0, "has_adjusted_close": True, "split_adjustment_factor": 1.0},
            {"date": "2026-09-25", "high": 10.0, "low": 9.5, "close": 9.8,
             "adjusted_close": 9.8, "has_adjusted_close": True, "split_adjustment_factor": 1.0},
        ]
        with patch("cio.memory.get_asset", return_value={"market": "US", "asset_id": "TSLA",
                                                      "price_symbol": "TSLA", "benchmarks": []}), \
             patch("cio.memory.find_context", return_value=(context, None)), \
             patch("cio.memory._price_session", return_value=None), \
             patch("cio.memory._fixed_horizon_returns", return_value={}):
            evaluation = evaluate_assessment(assessment, rows=price_rows)
        self.assertEqual(evaluation["window"]["start_date"], "2026-09-23")
        self.assertEqual(evaluation["exit_plan"]["stages"][0]["price_touch_date"], "2026-09-24")

    def test_evaluation_reads_later_model_check_without_backdating_it(self):
        assessment = valid_decision()
        assessment["assessment_id"] = "CIO-" + "1" * 20
        assessment["created_at"] = "2026-01-01T12:00:00-05:00"
        assessment["exit_plan"] = exit_plan()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = root / "assessments" / "TSLA"
            directory.mkdir(parents=True)
            (directory / ("CIO-" + "2" * 20 + ".json")).write_text(json.dumps({
                "assessment_id": "CIO-" + "2" * 20,
                "created_at": "2026-01-03T10:00:00-05:00",
                "exit_plan_checks": [{"assessment_id": assessment["assessment_id"],
                                      "stage_id": "first", "state": "SATISFIED"}],
            }), encoding="utf-8")
            with patch("cio.memory.KNOWLEDGE_DIR", root), \
                 patch("cio.memory.get_asset", return_value={"market": "US", "asset_id": "TSLA", "price_symbol": "TSLA", "benchmarks": []}), \
                 patch("cio.memory.find_context", return_value=(valid_context(), None)), \
                 patch("cio.memory._price_session", return_value=None), \
                 patch("cio.memory._fixed_horizon_returns", return_value={}), \
                 patch("cio.memory.require_valid_decision", side_effect=lambda value: value):
                result = evaluate_assessment(assessment, rows=bars())
        first = result["exit_plan"]["stages"][0]
        self.assertEqual(first["price_touch_date"], "2026-01-03")
        self.assertEqual(first["reviewed_price_touch_date"], "2026-01-04")
        self.assertEqual(first["condition_review"], "SATISFIED")
        self.assertEqual(first["execution_eligibility"], "UNKNOWN")


class TestExitPlanReport(unittest.TestCase):
    def test_show_preserves_usd_limit_and_unknown_execution(self):
        decision = valid_decision()
        decision["exit_plan"] = exit_plan()
        context = valid_context()
        context["context"] = {"portfolio": {"existing_position": {"held": True, "quantity": 100}}}
        evaluation = {"window": {"start_date": "2026-01-01", "latest_available_date": "2026-01-05", "complete": False},
                      "price": {"return_to_end_pct": 0},
                      "exit_plan": _exit_plan_observations({"assessment_id": "CIO-old", "exit_plan": exit_plan()},
                                                            bars(), start_factor=0.5, decision_date="2026-01-01")}
        with patch("cio.report.get_asset", return_value={"asset_id": "TSLA", "name": "Tesla"}), \
             patch("cio.report._find_evaluation", return_value=evaluation), \
             patch("cio.decision.find_context", return_value=(context, None)):
            output = format_assessment(decision, status="LIVE", warnings=[])
        self.assertIn("조건부 미래 매도 계획", output)
        self.assertIn("first (첫 매도): 10.004156주 · 지정가 10.25 USD", output)
        self.assertIn("second (다음 매도): 5주 · 지정가 11.00 USD", output)
        self.assertIn("실제 집행 UNKNOWN", output)
        self.assertIn("수익률은 산출하지 않았다", output)

    def test_buy_counterfactual_is_labeled_as_hypothetical_beside_sell_plan(self):
        decision = valid_decision()
        decision["exit_plan"] = exit_plan()
        context = valid_context()
        context["context"] = {"portfolio": {"existing_position": {"held": True, "quantity": 100}}}
        evaluation = {
            "window": {"start_date": "2026-01-01", "latest_available_date": "2026-01-05", "complete": False},
            "price": {"return_to_end_pct": 0},
            "exit_plan": _exit_plan_observations({"assessment_id": "CIO-old", "exit_plan": exit_plan()},
                                                    bars(), start_factor=0.5, decision_date="2026-01-01"),
            "counterfactual": {"waited_for_entry": {"filled": True, "entry_price": 9.5,
                                                       "fill": {"date": "2026-01-03"},
                                                       "return_to_end_pct": 10.0}},
        }
        with patch("cio.report.get_asset", return_value={"asset_id": "TSLA", "name": "Tesla"}), \
             patch("cio.report._find_evaluation", return_value=evaluation), \
             patch("cio.decision.find_context", return_value=(context, None)):
            output = format_assessment(decision, status="LIVE", warnings=[])
        self.assertIn("별도 매수 반사실", output)
        self.assertIn("실제 체결 미확인", output)
        self.assertNotIn("진입가 9.5 체결됨", output)

    def test_evals_reads_stored_sell_stages_and_observations(self):
        assessment_id = "CIO-" + "1" * 20
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = root / "assessments" / "TSLA"
            directory.mkdir(parents=True)
            (directory / f"{assessment_id}.json").write_text(json.dumps({
                "assessment_id": assessment_id, "created_at": "2026-01-01T12:00:00-05:00",
                "verdict": "WAIT", "action": {"direction": "HOLD"},
                "exit_plan": exit_plan(),
            }), encoding="utf-8")
            evaluation = {"price": {"start": 9.95}, "exit_plan": {"actual_execution": "UNKNOWN",
                "stages": [{"stage_id": "first", "price_touch_date": "2026-01-03",
                            "reviewed_price_touch_date": None}]}}
            with patch("cio.report.KNOWLEDGE_DIR", root), \
                 patch("cio.report.get_asset", return_value={"asset_id": "TSLA", "name": "Tesla"}), \
                 patch("cio.report._find_evaluation", return_value=evaluation):
                report = evaluations_for("TSLA")
        row = report["rows"][0]["exit_plan"]
        self.assertEqual(row["stages"][0]["quantity"], 10.004156)
        self.assertEqual(row["stages"][0]["limit_price"], 10.25)
        self.assertEqual(row["stages"][0]["price_touch_date"], "2026-01-03")
        self.assertEqual(row["actual_execution"], "UNKNOWN")

    def test_evals_plan_preserves_future_sell_prices_and_quantity(self):
        row = {"created_at": "2026-01-01T12:00:00-05:00", "assessment_id": "CIO-" + "1" * 20,
               "source": "LIVE", "asked_price": 9.95, "verdict": "WAIT", "action": "HOLD",
               "return_to_latest_pct": None, "triggered": [],
               "price_plan": {"preferred_entry": None, "stop_price": None, "scale_out_start": None},
               "exit_plan": {"currency": "USD", "actual_execution": "UNKNOWN", "stages": [
                   {"stage_id": "first", "name": "첫 매도", "quantity": 10.004156,
                    "limit_price": 10.25, "price_touch_date": "2026-01-03",
                    "reviewed_price_touch_date": None}]}}
        report = {"asset_id": "TSLA", "name": "Tesla", "assessment_count": 1,
                  "scored_count": 1, "unscored_count": 0, "rows": [row]}
        with patch("cio.report.evaluations_for", return_value=report):
            output = format_evaluations("TSLA", with_plan=True)
        self.assertIn("10.004156주 @ 10.25 USD", output)
        self.assertIn("가격 2026-01-03", output)
        self.assertIn("실제 집행 미확인", output)

    def test_evals_keeps_the_longest_decision_apart_from_the_return(self):
        """결론 칸이 14칸 고정이라 `WAIT/UNDETERMINED-33.6%` 로 붙어 찍혔다."""
        rows = [{"created_at": "2026-01-01T12:00:00-05:00",
                 "assessment_id": "CIO-" + str(index) * 20, "source": "LIVE",
                 "asked_price": 100.0, "verdict": verdict, "action": action,
                 "return_to_latest_pct": -33.6, "triggered": []}
                for index, (verdict, action) in enumerate(
                    (("WAIT", "HOLD"), ("INSUFFICIENT", "UNDETERMINED")))]
        report = {"asset_id": "TSLA", "name": "Tesla", "assessment_count": 2,
                  "scored_count": 2, "unscored_count": 0, "rows": rows}
        with patch("cio.report.evaluations_for", return_value=report):
            lines = format_evaluations("TSLA").splitlines()
        short, long = (next(line for line in lines if text in line)
                       for text in ("WAIT/HOLD", "INSUFFICIENT/UNDETERMINED"))
        self.assertIn("INSUFFICIENT/UNDETERMINED  -33.6%", long)
        self.assertEqual(short.index("-33.6%"), long.index("-33.6%"))


if __name__ == "__main__":
    unittest.main()
