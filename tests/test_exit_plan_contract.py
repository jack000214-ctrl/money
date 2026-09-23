"""Future sell stages are bounded advice, not proof that an order filled."""

from __future__ import annotations

from copy import deepcopy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from calculators.contracts import content_hash
from cio.decision import assessment_id_for, context_id_for, validate_decision
from cio.exit_plan import plan_summary, validate
from test_cio_core import valid_context, valid_decision


def sell_fixture():
    context = valid_context()
    context["context"] = {"portfolio": {"existing_position": {"held": True, "quantity": 2350}}}
    context["context_id"] = context_id_for(context)
    decision = valid_decision()
    decision["context_id"] = context["context_id"]
    decision["action"] = {
        "direction": "TRIM", "quantity": 1175, "limit_price": 3.95,
        "sell_reason": "VALUATION", "rationale": "현재 1175주를 먼저 줄인다",
    }
    decision["exit_plan"] = {
        "currency": "USD", "reference_date": "2026-01-01",
        "reference_basis": "질문 시점의 보통주 1주",
        "stages": [
            {"stage_id": "higher", "name": "추가 상승 시 첫 매도", "quantity": 600, "limit_price": 4.70,
             "requires_review": True, "depends_on": "CURRENT_ACTION",
             "condition": "현재 매도가 체결되고 새 중요 공시가 없을 때",
             "cancel_if": "새 임상 또는 규제 공시", "valid_from": "2026-01-02", "valid_until": "2026-12-31"},
            {"stage_id": "highest", "name": "추가 상승 시 잔여 매도", "quantity": 575, "limit_price": 5.10,
             "requires_review": True, "depends_on": "higher",
             "condition": "앞 단계 체결 후 논지가 유지될 때",
             "cancel_if": "새 임상 또는 규제 공시", "valid_from": "2026-01-02", "valid_until": "2026-12-31"},
        ],
    }
    return decision, context


class ExitPlanContractTests(unittest.TestCase):
    def test_rxrx_quantity_bridge_and_full_decision_validation(self):
        decision, context = sell_fixture()
        summary = plan_summary(decision["exit_plan"], decision, context)
        self.assertEqual((summary["current_sale_quantity"], summary["future_sale_quantity"]), (1175, 1175))
        self.assertEqual(summary["remaining_after_all"], 0)
        self.assertEqual(summary["actual_execution"], "UNKNOWN")
        self.assertEqual(validate(decision, context), [])
        self.assertEqual(validate_decision(decision, context=context), [])

    def test_oversell_duplicate_dependency_currency_and_dates_fail(self):
        mutations = [
            lambda d: d["exit_plan"]["stages"][1].update(quantity=576),
            lambda d: d["exit_plan"]["stages"][1].update(stage_id="higher"),
            lambda d: d["exit_plan"]["stages"][0].update(depends_on="highest"),
            lambda d: d["exit_plan"]["stages"][1].update(depends_on="not_a_stage"),
            lambda d: d["exit_plan"].update(currency="KRW"),
            lambda d: d["exit_plan"].update(reference_date="2026-01-02"),
            lambda d: d["exit_plan"]["stages"][0].update(valid_until="2025-12-31"),
            lambda d: (d["exit_plan"]["stages"][0].update(valid_from="2026-09-01"),
                       d["exit_plan"]["stages"][1].update(valid_until="2026-08-31")),
            lambda d: d["exit_plan"]["stages"][0].update(limit_price=0),
            lambda d: d["exit_plan"]["stages"][0].update(requires_review="yes"),
        ]
        for mutate in mutations:
            decision, context = sell_fixture()
            mutate(decision)
            with self.subTest(decision=decision["exit_plan"]):
                self.assertTrue(validate(decision, context))

    def test_plan_requires_a_real_position_and_reviewable_action(self):
        for direction in ("EXIT", "UNDETERMINED", "NOT_APPLICABLE", "ADD"):
            decision, context = sell_fixture()
            decision["action"]["direction"] = direction
            with self.subTest(direction=direction):
                self.assertTrue(validate(decision, context))
        decision, context = sell_fixture()
        decision["verdict"] = "INSUFFICIENT"
        self.assertTrue(validate(decision, context))
        decision, context = sell_fixture()
        context["context"]["portfolio"]["existing_position"] = {"held": False, "quantity": 0}
        self.assertTrue(validate(decision, context))

    def test_hold_can_start_independently_but_later_stage_needs_a_dependency(self):
        decision, context = sell_fixture()
        decision["action"] = {"direction": "HOLD", "quantity": None, "limit_price": None,
                              "sell_reason": None, "rationale": "현재는 보유한다"}
        decision["exit_plan"]["stages"][0]["depends_on"] = None
        self.assertEqual(validate(decision, context), [])
        decision["exit_plan"]["stages"][1]["depends_on"] = None
        self.assertTrue(validate(decision, context))
        decision["exit_plan"]["stages"][1]["depends_on"] = "higher"
        decision["action"]["quantity"] = 1
        self.assertTrue(validate(decision, context))

    def test_old_decisions_do_not_need_plan_fields(self):
        decision, context = sell_fixture()
        decision.pop("exit_plan")
        self.assertEqual(validate(decision, context), [])

    def test_checks_bind_to_exact_prior_stage_condition_and_evidence(self):
        decision, context = sell_fixture()
        plan = decision.pop("exit_plan")
        old_id = "CIO-" + "a" * 20
        context["context"]["prior_assessments"] = {"live": [
            {"assessment_id": old_id, "carried_forward": {"exit_plan": plan}}
        ]}
        check = {"assessment_id": old_id, "stage_id": "higher",
                 "condition": plan["stages"][0]["condition"], "state": "SATISFIED",
                 "note": "원문을 확인했다", "evidence_refs": ["PRICE:TSLA"]}
        decision["exit_plan_checks"] = [check]
        self.assertEqual(validate(decision, context), [])
        for change in (
            lambda d: d["exit_plan_checks"][0].update(condition="다른 조건"),
            lambda d: d["exit_plan_checks"].append(deepcopy(d["exit_plan_checks"][0])),
            lambda d: d["exit_plan_checks"][0].update(evidence_refs=[]),
            lambda d: d["exit_plan_checks"][0].update(stage_id="invented"),
        ):
            tampered = deepcopy(decision)
            change(tampered)
            self.assertTrue(validate(tampered, context))

    def test_referenced_prior_plan_requires_the_saved_assessment_hash(self):
        decision, context = sell_fixture()
        prior_decision = deepcopy(decision)
        prior_decision["assessment_id"] = assessment_id_for(prior_decision)
        decision.pop("exit_plan")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            saved = root / "assessment.json"
            saved.write_text(json.dumps(prior_decision), encoding="utf-8")
            context["context"]["prior_assessments"] = {"latest_exit_plan": {
                "assessment_id": prior_decision["assessment_id"],
                "exit_plan_ref": {"path": "assessment.json", "content_sha256": content_hash(saved)},
            }}
            decision["exit_plan_checks"] = [{
                "assessment_id": prior_decision["assessment_id"], "stage_id": "higher",
                "condition": prior_decision["exit_plan"]["stages"][0]["condition"],
                "state": "UNVERIFIABLE", "note": "체결 여부는 아직 모른다", "evidence_refs": [],
            }]
            with patch("cio.exit_plan.ROOT", root):
                self.assertEqual(validate(decision, context), [])
                saved.write_text(saved.read_text(encoding="utf-8") + " ", encoding="utf-8")
                self.assertTrue(validate(decision, context))


if __name__ == "__main__":
    unittest.main()
