"""An explicit holding must survive prepare without rewriting account history."""

import unittest
from unittest.mock import patch

import helpers  # noqa: F401
from cio.assets import get_asset
from cio.context import ContextError, _user_position_summary
from cio.decision import validate_decision
from test_intent_contract import _build_packet
from test_sell_quantity_contract import _decision


class TestUserPosition(unittest.TestCase):
    def packet(self, **kwargs):
        options = dict(intent="HOLD_REVIEW", amount=None, held_quantity=100,
                       average_cost=40000, price_value=50000)
        options.update(kwargs)
        return _build_packet(**options)

    def test_explicit_position_survives_a_conflicting_ledger(self):
        packet = self.packet()
        portfolio = packet["context"]["portfolio"]
        position = portfolio["existing_position"]
        self.assertEqual(position["quantity"], 100)
        self.assertEqual(position["cost_basis_krw"], 4000000)
        self.assertEqual(position["value_at_review_price_krw"], 5000000)
        self.assertEqual(position["unrealized_pnl_krw"], 1000000)
        self.assertEqual(position["unrealized_pnl_pct"], 25)
        self.assertFalse(position["verified"])
        self.assertEqual(position["authority"], "USER_INPUT")
        self.assertFalse(portfolio["local_position_comparison"]["quantity_matches"])
        self.assertEqual(packet["request"]["position_input"]["quantity"], 100)
        self.assertIn("100주", packet["request"]["question"])
        self.assertNotIn("ledger", position)
        self.assertNotIn("add_ladder", portfolio)
        self.assertIsNone(portfolio["cash_krw"])
        self.assertIsNone(portfolio["total_assets_krw"])
        self.assertIsNone(position["weight_at_review_price_pct"])

    def test_scenario_does_not_attach_or_mutate_trade_history(self):
        from copy import deepcopy
        local = {"available": True, "existing_position": {"quantity": 30, "average_cost": 30000}}
        original = deepcopy(local)
        packet = self.packet(portfolio=local, stale_ledger=True)
        self.assertEqual(local, original)
        self.assertEqual(packet["validation"]["readiness"]["position_action"]["status"], "PASS")
        self.assertIsNone(packet["context"]["portfolio"]["existing_position"].get("ledger"))

    def test_missing_ledger_does_not_block_explicit_conditions(self):
        packet = self.packet(portfolio=None)
        self.assertEqual(packet["validation"]["readiness"]["position_action"]["status"], "PASS")

    def test_no_average_cost_means_no_invented_pnl(self):
        position = self.packet(average_cost=None)["context"]["portfolio"]["existing_position"]
        self.assertIsNone(position["average_cost"])
        self.assertIsNone(position["unrealized_pnl_krw"])

    def test_sell_arithmetic_uses_explicit_holding_and_no_account_totals(self):
        trade = self.packet(target_quantity=60)["context"]["portfolio"]["proposed_trade"]
        self.assertEqual(trade["review_quantity"], 40)
        self.assertEqual(trade["remaining_quantity"], 60)
        self.assertEqual(trade["estimated_proceeds_krw"], 2000000)
        self.assertIsNone(trade["cash_after_krw"])
        self.assertIsNone(trade["post_trade_weight_total_assets_pct"])

    def test_selling_too_many_is_still_blocked(self):
        packet = self.packet(review_quantity=101)
        self.assertEqual(packet["validation"]["readiness"]["position_action"]["status"], "BLOCKED")

    def test_record_contract_checks_the_explicit_quantity(self):
        packet = self.packet()
        for size, accepted in ((100, True), (30, False), (101, False)):
            decision = _decision(packet, "EXIT", size)
            errors = [e for e in validate_decision(decision, context=packet)
                      if e.startswith("action")]
            self.assertEqual(not errors, accepted, errors)

    def test_invalid_inputs_do_not_silently_fall_back(self):
        for options in (dict(held_quantity=-1), dict(held_quantity=0),
                        dict(average_cost=-1), dict(held_quantity=None),
                        dict(intent="ADD", amount=1000000)):
            with self.subTest(options=options):
                self.assertEqual(self.packet(**options)["validation"]["status"], "BLOCKED")
        for field in ("held_quantity", "average_cost"):
            with self.subTest(field=field), self.assertRaises(ContextError):
                self.packet(**{field: float("nan")})

    def test_missing_fx_is_not_a_krw_usd_conversion(self):
        with patch("cio.context._fx_rate", return_value=(None, None)):
            portfolio = _user_position_summary(
                get_asset("TSLA"), quantity=2.5, average_cost=100, price=200,
                review_quantity=1, target_quantity=None, local_portfolio={},
            )
        position = portfolio["existing_position"]
        self.assertEqual(position["value_at_review_price"], 500)
        self.assertEqual(position["unrealized_pnl"], 250)
        self.assertIsNone(position["value_at_review_price_krw"])
        self.assertIsNone(portfolio["proposed_trade"]["estimated_proceeds_krw"])


if __name__ == "__main__":
    unittest.main()
