"""An explicit holding must survive prepare without rewriting account history."""

import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

import helpers  # noqa: F401
from cio.assets import get_asset
from cio.context import ContextError, _portfolio_summary, _user_position_summary
from cio.__main__ import build_parser, cmd_prepare
from calculators.position import position_value_by_currency
from cio.decision import validate_decision
from test_intent_contract import _build_packet
from test_sell_quantity_contract import _decision


class TestUserPosition(unittest.TestCase):
    def packet(self, **kwargs):
        options = dict(intent="HOLD_REVIEW", amount=None, held_quantity=100,
                       average_cost=40000, price_value=50000)
        options.update(kwargs)
        return _build_packet(**options)  # pyright: ignore[reportArgumentType]

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

    def test_explicit_average_cost_currency_reaches_saved_request(self):
        with patch("cio.context._fx_rate", return_value=(1400, "2026-09-18")):
            packet = self.packet(asset_id="RXRX", held_quantity=100,
                                 average_cost=6000, average_cost_currency="KRW",
                                 price_value=5)
        request = packet["request"]["position_input"]
        self.assertEqual(request["average_cost"], 6000)
        self.assertEqual(request["currency"], "KRW")
        position = packet["context"]["portfolio"]["existing_position"]
        self.assertEqual(position["average_cost_currency"], "KRW")
        self.assertEqual(position["cost_basis_krw"], 600000)
        self.assertEqual(position["value_at_review_price_krw"], 700000)
        self.assertEqual(position["unrealized_pnl_krw"], 100000)
        self.assertIsNone(position["average_cost_asset_currency"])
        self.assertIsNone(position["unrealized_pnl"])
        self.assertEqual(packet["validation"]["readiness"]["position_action"]["status"], "PASS")
        self.assertIsNone(packet["context"]["portfolio"]["proposed_trade"]["review_quantity"])

    def test_different_cost_currencies_cannot_match(self):
        with patch("cio.context._fx_rate", return_value=(1400, "2026-09-18")):
            portfolio = _user_position_summary(
                get_asset("RXRX"), quantity=100, average_cost=5,
                average_cost_currency="KRW", price=5,
                review_quantity=None, target_quantity=None,
                local_portfolio={"available": True, "existing_position": {
                    "quantity": 100, "average_cost": 5, "average_cost_currency": "USD"}},
            )
        comparison = portfolio["local_position_comparison"]
        self.assertFalse(comparison["average_cost_comparable"])
        self.assertIsNone(comparison["average_cost_matches"])

    def test_currency_without_cost_or_unknown_currency_is_blocked(self):
        for options in (dict(average_cost=None, average_cost_currency="KRW"),
                        dict(average_cost_currency="EUR")):
            with self.subTest(options=options):
                self.assertEqual(self.packet(**options)["validation"]["status"], "BLOCKED")

    def test_cli_passes_explicit_cost_currency(self):
        args = build_parser().parse_args([
            "prepare", "RXRX", "--price", "5", "--intent", "HOLD_REVIEW",
            "--held-quantity", "100", "--average-cost", "6000",
            "--average-cost-currency", "KRW", "--no-save", "--json",
        ])
        with patch("cio.__main__.build_context", return_value=(self.packet(), None)) as build, patch("builtins.print"):
            cmd_prepare(args)
        self.assertEqual(build.call_args.kwargs["average_cost_currency"], "KRW")


class TestCurrencyBasis(unittest.TestCase):
    def calculate(self, **kwargs):
        inputs = dict(quantity=10, price=5, asset_currency="USD", average_cost=6000,
                      cost_currency="KRW", fx_krw_per_usd=1400, fx_as_of="2026-09-18")
        inputs.update(kwargs)
        return position_value_by_currency(**inputs)  # pyright: ignore[reportArgumentType]

    def test_changing_fx_changes_krw_gain_without_rewriting_acquisition_cost(self):
        low = self.calculate(fx_krw_per_usd=1000)
        high = self.calculate(fx_krw_per_usd=2000)
        self.assertEqual(low["cost_basis_krw"], high["cost_basis_krw"])
        self.assertEqual(low["unrealized_pnl_krw"], -10000)
        self.assertEqual(high["unrealized_pnl_krw"], 40000)
        self.assertIsNone(low["cost_basis"])
        self.assertIsNone(high["cost_basis"])

    def test_usd_cost_does_not_invent_historical_krw_pnl(self):
        result = self.calculate(average_cost=4, cost_currency="USD")
        self.assertEqual(result["unrealized_pnl"], 10)
        self.assertEqual(result["usd_pnl_at_current_fx_krw"], 14000)
        self.assertIsNone(result["cost_basis_krw"])
        self.assertIsNone(result["unrealized_pnl_krw"])

    def test_missing_undated_or_invalid_fx_preserves_only_known_cost(self):
        for kwargs in (dict(fx_krw_per_usd=None), dict(fx_as_of=None),
                       dict(fx_krw_per_usd=0), dict(fx_krw_per_usd=-1),
                       dict(fx_krw_per_usd=float("nan")), dict(fx_as_of="invalid")):
            with self.subTest(kwargs=kwargs):
                result = self.calculate(**kwargs)
                self.assertEqual(result["cost_basis_krw"], 60000)
                self.assertIsNone(result["value_at_review_price_krw"])
                self.assertIsNone(result["unrealized_pnl_krw"])
                self.assertIsNone(result["break_even_price_at_current_fx"])

    def test_snapshot_krw_cost_never_becomes_historical_usd_average(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "portfolio" / "holdings.csv"
            path.parent.mkdir()
            path.write_text("# 기준일 2026-09-18\nasset_id,risk_class,value_krw,quantity,pnl_krw\n"
                            "RXRX,STOCK,70000,10,10000\n", encoding="utf-8")
            with patch("cio.context.KNOWLEDGE_DIR", root), patch("cio.context.ROOT", root), \
                    patch("cio.context._fx_rate", return_value=(1400, "2026-09-18")):
                portfolio, _ = _portfolio_summary(get_asset("RXRX"), amount=None,
                                                   amount_currency="KRW", price=5, intent="HOLD_REVIEW")
        position = portfolio["existing_position"]
        self.assertEqual(position["average_cost"], 6000)
        self.assertEqual(position["average_cost_currency"], "KRW")
        self.assertIsNone(position["average_cost_asset_currency"])
        self.assertEqual(position["unrealized_pnl_krw"], 10000)


if __name__ == "__main__":
    unittest.main()
