"""Event prices are explicit model estimates; their arithmetic is reproducible."""

from copy import deepcopy
import json
import unittest

import helpers  # noqa: F401
from calculators.assumptions import AssumptionError, analyze, calculate, parameter_values
from calculators.contracts import write_json
from cio.assumptions import analysis_id, create_analysis, load_analysis, report_path, validate_review
from cio.decision import validate_decision
import test_assumption_analysis as persistence
from test_assumption_analysis import spec_fixture


def event_spec():
    spec = spec_fixture()
    spec["inputs"].update(method="EVENT_PRICE", horizon_months=3)
    for name, terminal, distribution in [("bear", 50, 0), ("base", 120, 5), ("bull", 200, 0)]:
        row = spec["inputs"]["scenarios"][name]
        for field in ("eps", "multiple", "cash_distribution"):
            row.pop(field)
        row.update(terminal_price=terminal, cash_distribution=distribution)
    spec["bases"][0].update(
        parameters=list(parameter_values(spec["inputs"])),
        rationale="보존된 발표 근거로 추정한 사건 이후 가격이며 확률은 모델 추정이다.",
        revisit_when="실제 사건 결과가 조건과 다르거나 추가 발표가 나오면 재평가",
    )
    spec["tests"] = [
        {"id": "lower_repricing", "changes": {"scenarios.base.terminal_price": 90},
         "rationale": "좋은 결과에도 주가 재평가가 약한 경우"},
        {"id": "risk_shift", "changes": {"scenarios.bear.probability_pct": 35,
                                         "scenarios.base.probability_pct": 40},
         "rationale": "불리한 사건 결과의 가능성을 더 높게 볼 경우"},
    ]
    spec["boundaries"] = [{"id": "required_price", "parameter": "scenarios.base.terminal_price",
                           "low": 0, "high": 200}]
    return spec


class TestEventPriceMath(unittest.TestCase):
    def test_prices_distributions_alternatives_and_boundary(self):
        result = analyze(event_spec(), 10)
        baseline = result["baseline"]
        base = baseline["scenarios"]["base"]
        self.assertEqual(base["value_per_share"], 120)
        self.assertEqual(base["total_return_pct"], 25)
        self.assertAlmostEqual(base["annualized_return_pct"], (1.25 ** 4 - 1) * 100)
        self.assertAlmostEqual(baseline["expected_return_pct"], 25)
        self.assertAlmostEqual(baseline["win_probability_pct"], 50)
        self.assertAlmostEqual(result["tests"][0]["expected_return_pct"], 10)
        self.assertEqual(result["tests"][0]["comparison_relation"], "EQUAL")
        self.assertAlmostEqual(result["tests"][1]["expected_return_pct"], 17.5)
        self.assertAlmostEqual(result["tests"][1]["win_probability_pct"], 45.5)
        self.assertAlmostEqual(result["boundaries"][0]["required_value"], 90)
        self.assertNotIn('"verdict"', json.dumps(result))
        self.assertNotIn('"action"', json.dumps(result))

    def test_total_loss_and_cash_recovery_are_distinct(self):
        inputs = event_spec()["inputs"]
        inputs["scenarios"]["bear"].update(terminal_price=0)
        loss = calculate(inputs)["scenarios"]["bear"]
        self.assertEqual(loss["value_per_share"], 0)
        self.assertEqual(loss["total_return_pct"], -100)
        self.assertEqual(loss["annualized_return_pct"], -100)
        # A zero terminal share price does not erase cash already distributed.
        inputs["scenarios"]["bear"]["cash_distribution"] = 20
        recovery = calculate(inputs)["scenarios"]["bear"]
        self.assertEqual(recovery["value_per_share"], 0)
        self.assertEqual(recovery["total_return_pct"], -80)
        self.assertAlmostEqual(recovery["annualized_return_pct"], (0.2 ** 4 - 1) * 100)

    def test_invalid_prices_probabilities_and_mixed_methods_are_rejected(self):
        mutations = [
            lambda s: s["inputs"]["scenarios"]["base"].update(terminal_price=-1),
            lambda s: s["inputs"]["scenarios"]["base"].update(terminal_price=float("inf")),
            lambda s: s["inputs"]["scenarios"]["base"].update(terminal_price=True),
            lambda s: s["inputs"]["scenarios"]["base"].update(cash_distribution=-1),
            lambda s: s["inputs"]["scenarios"]["base"].update(probability_pct=40),
            lambda s: s["inputs"]["scenarios"]["base"].update(conditional_win_probability_pct=101),
            lambda s: s["inputs"]["scenarios"]["base"].update(eps=1),
            lambda s: s["inputs"].update(dcf={}),
            lambda s: s["tests"][1]["changes"].pop("scenarios.base.probability_pct"),
        ]
        for mutate in mutations:
            spec = event_spec()
            mutate(spec)
            with self.subTest(mutation=mutate), self.assertRaises(AssumptionError):
                analyze(spec, 10)


class TestEventPriceBinding(unittest.TestCase):
    # Reuse fixture construction, without inheriting and rerunning its test suite.
    setUp = persistence.TestAssumptionPersistence.setUp

    def decision(self, report):
        decision = persistence.TestAssumptionPersistence.decision(self, report)
        decision["method"]["valuation_family"] = "EVENT_REPRICING"
        decision["horizon_months"] = 3
        return decision

    def test_event_report_records_and_loads_with_same_arithmetic(self):
        from cio.memory import record_decision_file

        report = create_analysis(event_spec(), self.context)
        loaded = load_analysis("TSLA", report["analysis_id"], self.context)
        self.assertEqual(loaded["outputs"], report["outputs"])
        decision = self.decision(loaded)
        self.assertEqual(validate_decision(decision, context=self.context), [])
        write_json(self.knowledge / "contexts" / "TSLA" / f"{self.context['context_id']}.json", self.context)
        path = record_decision_file(write_json(self.root / "event-decision.json", decision))
        saved = json.loads(path.read_text())
        self.assertEqual(saved["method"]["valuation_family"], "EVENT_REPRICING")
        self.assertEqual(saved["assumption_review"]["analysis_id"], report["analysis_id"])

    def test_method_binding_and_unsupported_escape_hatch(self):
        report = create_analysis(event_spec(), self.context)
        decision = self.decision(report)
        self.assertEqual(validate_review(decision, self.context), [])
        for family in ("DCF", "EQUITY_BRIDGE", "EARNINGS_MULTIPLE"):
            wrong = deepcopy(decision)
            wrong["method"]["valuation_family"] = family
            with self.subTest(family=family):
                self.assertTrue(validate_review(wrong, self.context))
        decision["assumption_review"].update(status="UNSUPPORTED_METHOD", analysis_id=None, responses=[])
        self.assertTrue(validate_review(decision, self.context))
        # Binding must also hold in the opposite direction.
        earnings = create_analysis(spec_fixture(), self.context)
        wrong = persistence.TestAssumptionPersistence.decision(self, earnings)
        wrong["method"]["valuation_family"] = "EVENT_REPRICING"
        self.assertTrue(validate_review(wrong, self.context))

    def test_prior_engine_reports_remain_recomputable(self):
        original = create_analysis(spec_fixture(), self.context)
        for version in ("1.0.0", "1.1.0"):
            report = deepcopy(original)
            report["engine_version"] = version
            report["analysis_id"] = analysis_id(report)
            write_json(report_path("TSLA", report["analysis_id"]), report)
            with self.subTest(version=version):
                loaded = load_analysis("TSLA", report["analysis_id"], self.context)
                self.assertEqual(loaded["outputs"], original["outputs"])
        report["engine_version"] = "99.0.0"
        report["analysis_id"] = analysis_id(report)
        write_json(report_path("TSLA", report["analysis_id"]), report)
        with self.assertRaisesRegex(AssumptionError, "지원하지 않는"):
            load_analysis("TSLA", report["analysis_id"], self.context)


if __name__ == "__main__":
    unittest.main()
