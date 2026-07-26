"""구조화 매매 점검 엔진의 안전 회귀 테스트."""

import json
import math
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from test_contracts import valid_analysis, valid_ips
from trade_check import evaluate_trade, exit_code_for_verdict

EVALUATED_AT = datetime(2026, 7, 26, 3, 0, tzinfo=timezone.utc)
PRICE_AT = "2026-07-26T12:00:00+09:00"


class TradeEngineCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.holdings = root / "holdings.csv"
        self.ips = root / "ips.json"
        self.analysis = root / "analysis.json"
        self.write_holdings("2026-07-25")
        self.write_json(self.ips, valid_ips())
        self.write_json(self.analysis, valid_analysis())

    def tearDown(self):
        self.tmp.cleanup()

    def write_holdings(self, as_of):
        self.holdings.write_text(
            f"""# 보유 내역 / 기준일 {as_of} / 기준통화 KRW
asset_id,name,market,asset_type,value_krw,quantity
AAA,알파,KR,STOCK,10000000,100
ETF1,주식ETF,KR,ETF,20000000,200
CASH,예수금,KR,CASH,70000000,
""",
            encoding="utf-8",
        )

    @staticmethod
    def write_json(path, data):
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def evaluate(self, side="buy", quantity=10, price=100_000, **kwargs):
        return evaluate_trade(
            "AAA", side, quantity, price,
            holdings_path=self.holdings,
            ips_path=self.ips,
            analysis_path=self.analysis,
            price_as_of=PRICE_AT,
            evaluated_at=EVALUATED_AT,
            **kwargs,
        )

    def codes(self, result):
        return {issue["code"] for issue in result["issues"]}

    def test_valid_buy_proceeds(self):
        result = self.evaluate()
        self.assertEqual(result["decision"]["verdict"], "PROCEED")

    def test_only_proceed_has_success_exit_code(self):
        self.assertEqual(exit_code_for_verdict("PROCEED"), 0)
        for verdict in ("CAUTION", "RESEARCH_REQUIRED", "BLOCKED"):
            self.assertNotEqual(exit_code_for_verdict(verdict), 0)

    def test_oversell_is_blocked_even_when_price_is_high(self):
        result = self.evaluate(side="sell", quantity=1000, price=500_000)
        self.assertEqual(result["decision"]["verdict"], "BLOCKED")
        self.assertIn("OVERSELL", self.codes(result))

    def test_negative_quantity_and_price_are_blocked(self):
        result = self.evaluate(quantity=-1, price=-100)
        self.assertEqual(result["decision"]["verdict"], "BLOCKED")
        self.assertIn("INVALID_QUANTITY", self.codes(result))
        self.assertIn("INVALID_PRICE", self.codes(result))

    def test_non_finite_quantity_and_price_are_blocked(self):
        for quantity, price in ((math.nan, 100_000), (10, math.inf)):
            with self.subTest(quantity=quantity, price=price):
                result = self.evaluate(quantity=quantity, price=price)
                self.assertEqual(result["decision"]["verdict"], "BLOCKED")
                self.assertTrue(
                    {"INVALID_QUANTITY", "INVALID_PRICE"} & self.codes(result)
                )
                json.dumps(result, allow_nan=False)

    def test_insufficient_cash_is_blocked(self):
        result = self.evaluate(quantity=800, price=100_000)
        self.assertEqual(result["decision"]["verdict"], "BLOCKED")
        self.assertIn("INSUFFICIENT_CASH", self.codes(result))

    def test_stale_holdings_are_blocked(self):
        self.write_holdings("2026-07-20")
        result = self.evaluate()
        self.assertEqual(result["decision"]["verdict"], "BLOCKED")
        self.assertIn("HOLDINGS_STALE", self.codes(result))

    def test_stale_price_is_blocked(self):
        result = evaluate_trade(
            "AAA", "buy", 10, 100_000,
            holdings_path=self.holdings,
            ips_path=self.ips,
            analysis_path=self.analysis,
            price_as_of="2026-07-26T09:00:00+09:00",
            evaluated_at=EVALUATED_AT,
        )
        self.assertEqual(result["decision"]["verdict"], "BLOCKED")
        self.assertIn("PRICE_STALE", self.codes(result))

    def test_stale_analysis_is_blocked(self):
        analysis = valid_analysis()
        analysis["as_of"] = "2026-07-01T00:00:00+09:00"
        analysis["snapshot"]["created_at"] = "2026-07-01T00:00:00+09:00"
        analysis["sources"][0]["retrieved_at"] = "2026-07-01T00:00:00+09:00"
        analysis["sources"][0]["published_at"] = "2026-06-30T18:00:00+09:00"
        self.write_json(self.analysis, analysis)
        result = self.evaluate()
        self.assertEqual(result["decision"]["verdict"], "BLOCKED")
        self.assertIn("ANALYSIS_STALE", self.codes(result))

    def test_missing_price_timestamp_is_blocked_with_approved_ips(self):
        result = evaluate_trade(
            "AAA", "buy", 10, 100_000,
            holdings_path=self.holdings,
            ips_path=self.ips,
            analysis_path=self.analysis,
            price_as_of=None,
            evaluated_at=EVALUATED_AT,
        )
        self.assertEqual(result["decision"]["verdict"], "BLOCKED")
        self.assertIn("PRICE_TIMESTAMP_MISSING", self.codes(result))

    def test_limit_violation_is_blocked(self):
        ips = valid_ips()
        ips["position_limits"]["total_assets_pct"] = 10.5
        self.write_json(self.ips, ips)
        result = self.evaluate(quantity=10, price=100_000)
        self.assertEqual(result["decision"]["verdict"], "BLOCKED")
        self.assertIn("POSITION_LIMIT_TOTAL_ASSETS", self.codes(result))

    def test_sell_that_still_violates_limit_is_blocked(self):
        ips = valid_ips()
        ips["position_limits"]["total_assets_pct"] = 5.0
        self.write_json(self.ips, ips)
        result = self.evaluate(side="sell", quantity=10, price=100_000)
        self.assertEqual(result["decision"]["verdict"], "BLOCKED")
        self.assertIn("POSITION_LIMIT_TOTAL_ASSETS", self.codes(result))

    def test_full_sale_never_creates_negative_position_from_price_change(self):
        result = self.evaluate(side="sell", quantity=100, price=500_000)
        self.assertNotIn("NEGATIVE_POSITION_VALUE", self.codes(result))
        self.assertEqual(result["weights"]["after"]["total_assets"]["position_pct"], 0.0)

    def test_draft_ips_requires_research(self):
        self.write_json(self.ips, valid_ips("DRAFT"))
        result = self.evaluate()
        self.assertEqual(result["decision"]["verdict"], "RESEARCH_REQUIRED")
        self.assertIn("IPS_NOT_APPROVED", self.codes(result))

    def test_missing_ips_requires_research(self):
        result = evaluate_trade(
            "AAA", "buy", 10, 100_000,
            holdings_path=self.holdings,
            ips_path=None,
            analysis_path=self.analysis,
            price_as_of=PRICE_AT,
            evaluated_at=EVALUATED_AT,
        )
        self.assertEqual(result["decision"]["verdict"], "RESEARCH_REQUIRED")
        self.assertIn("IPS_MISSING", self.codes(result))

    def test_missing_analysis_requires_research(self):
        result = evaluate_trade(
            "AAA", "buy", 10, 100_000,
            holdings_path=self.holdings,
            ips_path=self.ips,
            analysis_path=None,
            price_as_of=PRICE_AT,
            evaluated_at=EVALUATED_AT,
        )
        self.assertEqual(result["decision"]["verdict"], "RESEARCH_REQUIRED")
        self.assertIn("ANALYSIS_MISSING", self.codes(result))

    def test_invalid_analysis_is_blocked(self):
        analysis = valid_analysis()
        analysis["claims"][0]["source_ids"] = ["MISSING"]
        self.write_json(self.analysis, analysis)
        result = self.evaluate()
        self.assertEqual(result["decision"]["verdict"], "BLOCKED")
        self.assertIn("ANALYSIS_SCHEMA_INVALID", self.codes(result))

    def test_invalidated_thesis_blocks_buy(self):
        analysis = valid_analysis()
        analysis["thesis"].update(
            status="INVALIDATED", change="INVALIDATE", invalidation_triggered=True
        )
        self.write_json(self.analysis, analysis)
        result = self.evaluate()
        self.assertEqual(result["decision"]["verdict"], "BLOCKED")
        self.assertIn("THESIS_INVALIDATED_FOR_BUY", self.codes(result))

    def test_invalidated_thesis_supports_sell(self):
        analysis = valid_analysis()
        analysis["thesis"].update(
            status="INVALIDATED", change="INVALIDATE", invalidation_triggered=True
        )
        self.write_json(self.analysis, analysis)
        result = self.evaluate(side="sell", quantity=10)
        self.assertEqual(result["decision"]["verdict"], "PROCEED")
        self.assertIn("논지 훼손", result["decision_basis"]["sell_drivers"])

    def test_better_alternative_causes_caution(self):
        analysis = valid_analysis()
        analysis["alternatives"] = [{
            "instrument_id": "ETF-BETTER",
            "base_expected_return_pct": 20.0,
            "snapshot_id": "SNAP-ALT",
            "claim_ids": ["CLAIM-1"],
        }]
        self.write_json(self.analysis, analysis)
        result = self.evaluate()
        self.assertEqual(result["decision"]["verdict"], "CAUTION")
        self.assertIn("ALTERNATIVE_DOMINATES", self.codes(result))

    def test_all_weight_denominators_are_reported(self):
        result = self.evaluate()
        before = result["weights"]["before"]
        self.assertAlmostEqual(before["total_assets"]["position_pct"], 10.0)
        self.assertAlmostEqual(before["equity_sleeve"]["position_pct"], 100 / 3)
        self.assertAlmostEqual(before["risk_assets"]["position_pct"], 100 / 3)


if __name__ == "__main__":
    unittest.main()
