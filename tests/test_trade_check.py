"""구조화 매매 점검 엔진의 안전 회귀 테스트."""

import json
import math
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from contracts import calculation_id_for, save_validated_calculation
from test_contracts import valid_analysis, valid_calculation, valid_ips
from trade_check import evaluate_trade, exit_code_for_verdict, render_text

EVALUATED_AT = datetime(2026, 7, 26, 3, 0, tzinfo=timezone.utc)
PRICE_AT = "2026-07-26T12:00:00+09:00"


class TradeEngineCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.holdings = root / "holdings.csv"
        self.ips = root / "ips.json"
        self.analysis = root / "analysis.json"
        self.calculations = root / "calculations"
        self.write_holdings("2026-07-25")
        self.write_json(self.ips, valid_ips())
        self.write_json(self.analysis, valid_analysis())
        save_validated_calculation(self.calculations, valid_calculation())

    def tearDown(self):
        self.tmp.cleanup()

    def write_holdings(self, as_of):
        self.holdings.write_text(
            f"""# 보유 내역 / 기준일 {as_of} / 기준통화 KRW
asset_id,name,market,asset_type,risk_class,value_krw,quantity,sector
AAA,알파,KR,STOCK,STOCK,10000000,100,헬스케어
ETF1,주식ETF,KR,ETF,BROAD_ETF,20000000,200,
CASH,예수금,KR,CASH,CASH,70000000,,
""",
            encoding="utf-8",
        )

    @staticmethod
    def write_json(path, data):
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def evaluate(self, side="buy", quantity=10, price=100_000, **kwargs):
        kwargs.setdefault("calculations_dir", self.calculations)
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
        ips["position_limits"]["by_risk_class"]["STOCK"]["standard_pct"] = 10.5
        self.write_json(self.ips, ips)
        result = self.evaluate(quantity=10, price=100_000)
        self.assertEqual(result["decision"]["verdict"], "BLOCKED")
        self.assertIn("POSITION_LIMIT_TOTAL_ASSETS", self.codes(result))

    def test_sell_that_still_violates_limit_is_blocked(self):
        ips = valid_ips()
        ips["position_limits"]["by_risk_class"]["STOCK"]["standard_pct"] = 5.0
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
            calculations_dir=self.calculations,
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

    def rewrite_scenarios(self, returns, probabilities):
        """시나리오 수익률·확률을 바꾸고 계산 산출물까지 같이 맞춘다.

        분석만 고치면 CALCULATION_MISMATCH 로 막힌다 — 그게 정상이다.
        """
        analysis = valid_analysis()
        scenarios = analysis["expected_return"]["scenarios"]
        for name in ("bear", "base", "bull"):
            scenarios[name]["return_pct"] = returns[name]
            scenarios[name]["probability"] = probabilities[name]
        analysis["expected_return"]["probability_weighted_pct"] = sum(
            returns[n] * probabilities[n] for n in ("bear", "base", "bull"))
        self.write_json(self.analysis, analysis)
        save_validated_calculation(
            self.calculations, valid_calculation(scenario_returns=returns))
        analysis["expected_return"]["calculation_id"] = valid_calculation(
            scenario_returns=returns)["calculation_id"]
        self.write_json(self.analysis, analysis)
        return analysis

    def test_deep_downside_is_blocked_even_when_the_mean_clears_the_hurdle(self):
        """base 만 보던 시절에는 하방 -80%가 아무 저항 없이 통과했다.

        약세 손실이 총자산의 8.8%로 승인 하방 예산 8%를 넘는다.
        """
        self.rewrite_scenarios(
            {"bear": -80.0, "base": 15.0, "bull": 40.0},
            {"bear": 0.1, "base": 0.5, "bull": 0.4},
        )
        result = self.evaluate(quantity=10, price=100_000)
        self.assertIn("BEAR_LOSS_EXCEEDS_BUDGET", self.codes(result))
        self.assertEqual(result["decision"]["verdict"], "BLOCKED")

    def test_shallower_downside_at_the_same_size_stays_within_budget(self):
        """같은 수량, 같은 확률가중(둘 다 요구수익률 통과). 하방 깊이만 다르다.

        -80%는 총자산의 8.8%, -70%는 7.7%. 승인 예산 8%를 사이에 두고 갈린다.
        판정을 가르는 것이 하방이라는 사실을 이 짝이 고정한다.
        """
        self.rewrite_scenarios(
            {"bear": -70.0, "base": 15.0, "bull": 40.0},
            {"bear": 0.1, "base": 0.5, "bull": 0.4},
        )
        result = self.evaluate(quantity=10, price=100_000)
        self.assertNotIn("BEAR_LOSS_EXCEEDS_BUDGET", self.codes(result))
        self.assertEqual(result["decision"]["verdict"], "PROCEED")
        self.assertAlmostEqual(
            result["decision_basis"]["bear_loss_contribution_pct"], 7.7, delta=0.05)

    def test_probability_weighted_return_decides_not_the_base_case(self):
        """base 는 요구수익률을 넘지만 확률가중은 못 넘는 경우.

        base 15% > 요구 10% 인데 하방이 크고 확률이 높아 가중은 3.5%다.
        """
        self.rewrite_scenarios(
            {"bear": -30.0, "base": 15.0, "bull": 40.0},
            {"bear": 0.45, "base": 0.45, "bull": 0.1},
        )
        result = self.evaluate(quantity=1, price=100_000)
        self.assertIn("EXPECTED_RETURN_BELOW_HURDLE", self.codes(result))

    def test_base_measure_ignores_the_downside_by_design(self):
        """IPS가 BASE 기준을 고르면 같은 분석이 통과한다.

        어느 자로 재는지가 판정을 바꾼다는 사실 자체를 고정해 둔다.
        """
        ips = valid_ips()
        ips["decision_policy"]["return_measure"] = "BASE"
        self.write_json(self.ips, ips)
        self.rewrite_scenarios(
            {"bear": -30.0, "base": 15.0, "bull": 40.0},
            {"bear": 0.45, "base": 0.45, "bull": 0.1},
        )
        result = self.evaluate(quantity=1, price=100_000)
        self.assertNotIn("EXPECTED_RETURN_BELOW_HURDLE", self.codes(result))

    def test_analysis_that_disagrees_with_its_calculation_is_blocked(self):
        """기대수익을 손으로 고쳐 요구수익률을 넘기는 경로를 막는다."""
        analysis = valid_analysis()
        # 계약 자체는 통과하도록 내부 정합성은 맞춘다. 그래야 막는 것이
        # 스키마가 아니라 계산 산출물과의 대조라는 점이 드러난다.
        analysis["expected_return"]["scenarios"]["base"]["return_pct"] = 30.0
        analysis["expected_return"]["probability_weighted_pct"] = 20.0
        self.write_json(self.analysis, analysis)
        result = self.evaluate()
        self.assertIn("CALCULATION_MISMATCH", self.codes(result))
        self.assertEqual(result["decision"]["verdict"], "BLOCKED")

    def test_missing_calculation_store_blocks_the_trade(self):
        result = self.evaluate(calculations_dir=self.calculations / "nope")
        self.assertIn("CALCULATION_MISMATCH", self.codes(result))
        self.assertEqual(result["decision"]["verdict"], "BLOCKED")

    def test_alternative_on_another_measure_is_not_silently_compared(self):
        analysis = valid_analysis()
        analysis["alternatives"] = [{
            "instrument_id": "ETF-BETTER",
            "expected_return_pct": 20.0,
            "measure": "BASE",
            "snapshot_id": "SNAP-ALT",
            "claim_ids": ["CLAIM-1"],
        }]
        self.write_json(self.analysis, analysis)
        result = self.evaluate()
        self.assertIn("ALTERNATIVE_MEASURE_MISMATCH", self.codes(result))
        self.assertNotIn("ALTERNATIVE_DOMINATES", self.codes(result))

    def test_reducing_a_position_over_the_bear_budget_is_a_sell_driver(self):
        self.rewrite_scenarios(
            {"bear": -90.0, "base": 15.0, "bull": 40.0},
            {"bear": 0.1, "base": 0.5, "bull": 0.4},
        )
        result = self.evaluate(side="sell", quantity=50, price=100_000)
        self.assertIn("하방 예산 초과 축소", result["decision_basis"]["sell_drivers"])

    def test_better_alternative_causes_caution(self):
        analysis = valid_analysis()
        analysis["alternatives"] = [{
            "instrument_id": "ETF-BETTER",
            "expected_return_pct": 20.0,
            "measure": "PROBABILITY_WEIGHTED",
            "snapshot_id": "SNAP-ALT",
            "claim_ids": ["CLAIM-1"],
        }]
        self.write_json(self.analysis, analysis)
        result = self.evaluate()
        self.assertEqual(result["decision"]["verdict"], "CAUTION")
        self.assertIn("ALTERNATIVE_DOMINATES", self.codes(result))

    def test_understated_price_cannot_slip_past_position_limit(self):
        """과소입력 한 견적 하나로 한도 검사를 통째로 무력화할 수 없다.

        같은 수량을 실제가로 넣으면 한도 초과로 BLOCKED 인데, 가격만 1원으로
        낮추면 모든 비중이 작아 보여 PROCEED 가 나오던 문제.
        """
        at_real_price = self.evaluate(quantity=800, price=100_000)
        self.assertEqual(at_real_price["decision"]["verdict"], "BLOCKED")

        understated = self.evaluate(quantity=800, price=1)
        self.assertEqual(understated["decision"]["verdict"], "BLOCKED")
        self.assertIn("PRICE_DEVIATION", self.codes(understated))

    def test_price_within_band_still_proceeds(self):
        result = self.evaluate(price=110_000)
        self.assertEqual(result["decision"]["verdict"], "PROCEED")
        self.assertAlmostEqual(result["price_reference"]["deviation_pct"], 10.0)
        self.assertEqual(result["price_reference"]["source"], "HOLDINGS_IMPLIED")

    def test_price_reference_uses_holdings_implied_unit_price(self):
        result = self.evaluate()
        self.assertAlmostEqual(result["price_reference"]["price_krw"], 100_000)

    def evaluate_new_asset(self, price=50_000, **kwargs):
        return evaluate_trade(
            "NEWCO", "buy", 10, price,
            holdings_path=self.holdings,
            ips_path=self.ips,
            analysis_path=None,
            price_as_of=PRICE_AT,
            evaluated_at=EVALUATED_AT,
            price_dir=Path(self.tmp.name) / "prices",
            **kwargs,
        )

    def write_snapshot(self, symbol, currency, close, last_date="2026-07-25"):
        price_dir = Path(self.tmp.name) / "prices"
        price_dir.mkdir(exist_ok=True)
        (price_dir / f"{symbol}.csv").write_text(
            f"# {symbol} 일별 종가 / 통화 {currency}\n"
            "# 수집일 2026-07-26 / 출처 Yahoo Finance chart API\n"
            "date,close,high,low,volume\n"
            f"2026-07-24,{close * 0.99},0,0,0\n"
            f"{last_date},{close},0,0,0\n",
            encoding="utf-8",
        )

    def test_unverifiable_price_is_blocked_under_approved_ips(self):
        result = self.evaluate_new_asset()
        self.assertEqual(result["decision"]["verdict"], "BLOCKED")
        self.assertIn("PRICE_UNVERIFIED", self.codes(result))

    def test_unverifiable_price_can_be_accepted_with_a_recorded_reason(self):
        result = self.evaluate_new_asset(
            price_unverified_reason="증권사 호가창 직접 확인")
        self.assertNotIn("PRICE_UNVERIFIED", self.codes(result))
        self.assertIn("PRICE_UNVERIFIED_ACCEPTED", self.codes(result))
        self.assertIn("증권사 호가창 직접 확인",
                      result["price_reference"]["override_reason"])

    def test_reason_never_rescues_a_price_that_contradicts_the_reference(self):
        result = self.evaluate(quantity=10, price=1,
                               price_unverified_reason="그냥 통과시켜줘")
        self.assertEqual(result["decision"]["verdict"], "BLOCKED")
        self.assertIn("PRICE_DEVIATION", self.codes(result))

    def test_price_snapshot_is_used_when_position_is_not_held(self):
        self.write_snapshot("NEWCO", "KRW", 50_000)
        result = self.evaluate_new_asset(price=50_000)
        self.assertEqual(result["price_reference"]["source"], "PRICE_SNAPSHOT")
        self.assertNotIn("PRICE_UNVERIFIED", self.codes(result))

    def test_foreign_currency_snapshot_is_not_compared_without_an_fx_rate(self):
        self.write_snapshot("NEWCO", "USD", 220)
        result = self.evaluate_new_asset(price=50_000)
        self.assertEqual(result["price_reference"]["source"],
                         "SNAPSHOT_CURRENCY_MISMATCH")
        self.assertIn("PRICE_UNVERIFIED", self.codes(result))

    def test_projected_rows_keep_the_denominator_consistent(self):
        """거래 후 행에서 현금을 함께 움직이지 않으면 통화 비중이 100%를 넘는다."""
        for side in ("buy", "sell"):
            with self.subTest(side=side):
                result = self.evaluate(side=side, quantity=10)
                after = result["concentration"]["after"]
                self.assertAlmostEqual(sum(after["currency_pct"].values()), 100.0, places=6)

    def test_sector_concentration_blocks_a_trade_that_breaches_it(self):
        ips = valid_ips()
        ips["portfolio_limits"]["sector_total_assets_pct"] = 10.5
        self.write_json(self.ips, ips)
        result = self.evaluate(quantity=10, price=100_000)
        self.assertEqual(result["decision"]["verdict"], "BLOCKED")
        self.assertIn("SECTOR_CONCENTRATION", self.codes(result))

    def test_top10_concentration_blocks_a_trade_that_breaches_it(self):
        ips = valid_ips()
        ips["portfolio_limits"]["top10_total_assets_pct"] = 30.5
        self.write_json(self.ips, ips)
        result = self.evaluate(quantity=10, price=100_000)
        self.assertEqual(result["decision"]["verdict"], "BLOCKED")
        self.assertIn("TOP10_CONCENTRATION", self.codes(result))

    def test_unclassified_concentration_bounds_the_lookthrough_blind_spot(self):
        ips = valid_ips()
        ips["portfolio_limits"]["unclassified_total_assets_pct"] = 15.0
        self.write_json(self.ips, ips)
        result = self.evaluate(quantity=10, price=100_000)
        self.assertIn("UNCLASSIFIED_CONCENTRATION_STILL_OVER", self.codes(result))

    def test_existing_breach_does_not_lock_the_whole_portfolio(self):
        """이미 초과한 지표 때문에 위험을 줄이는 거래까지 막히면 안 된다."""
        ips = valid_ips()
        ips["portfolio_limits"]["currency_total_assets_pct"] = 50.0
        self.write_json(self.ips, ips)

        neutral = self.evaluate(side="sell", quantity=10, price=100_000)
        self.assertNotEqual(neutral["decision"]["verdict"], "BLOCKED")
        self.assertIn("CURRENCY_CONCENTRATION_STILL_OVER", self.codes(neutral))

    def test_worsening_an_existing_breach_is_blocked(self):
        ips = valid_ips()
        ips["portfolio_limits"]["unclassified_total_assets_pct"] = 9.0
        self.write_json(self.ips, ips)
        self.holdings.write_text(
            """# 보유 내역 / 기준일 2026-07-25 / 기준통화 KRW
asset_id,name,market,asset_type,risk_class,value_krw,quantity,sector
AAA,알파,KR,STOCK,STOCK,10000000,100,
ETF1,주식ETF,KR,ETF,BROAD_ETF,20000000,200,헬스케어
CASH,예수금,KR,CASH,CASH,70000000,,
""",
            encoding="utf-8",
        )
        result = self.evaluate(quantity=10, price=100_000)
        self.assertEqual(result["decision"]["verdict"], "BLOCKED")
        self.assertIn("UNCLASSIFIED_CONCENTRATION_WORSENED", self.codes(result))

    def test_impossible_order_reports_no_after_projection(self):
        """-905주, 970조 같은 숫자가 차단 사유보다 먼저 읽히면 안 된다."""
        result = self.evaluate(side="sell", quantity=1000, price=100_000)
        self.assertEqual(result["decision"]["verdict"], "BLOCKED")
        self.assertFalse(result["projection_reliable"])
        self.assertIsNone(result["trade"]["held_quantity_after"])
        self.assertNotIn("-905", render_text(result))
        self.assertIn("산출 불가", render_text(result))

    def test_feasible_order_still_reports_the_projection(self):
        result = self.evaluate()
        self.assertTrue(result["projection_reliable"])
        self.assertEqual(result["trade"]["held_quantity_after"], 110)

    def test_standard_limit_can_be_exceeded_with_a_recorded_reason(self):
        ips = valid_ips()
        ips["position_limits"]["by_risk_class"]["STOCK"] = {
            "standard_pct": 10.5, "hard_ceiling_pct": 50.0,
        }
        self.write_json(self.ips, ips)

        blocked = self.evaluate(quantity=10, price=100_000)
        self.assertEqual(blocked["decision"]["verdict"], "BLOCKED")

        allowed = self.evaluate(
            quantity=10, price=100_000,
            concentration_override_reason="드문 저평가 구간이라 의도적으로 집중")
        self.assertNotEqual(allowed["decision"]["verdict"], "BLOCKED")
        self.assertIn("POSITION_LIMIT_OVERRIDDEN", self.codes(allowed))

    def test_hard_ceiling_is_never_released_by_a_reason(self):
        ips = valid_ips()
        ips["position_limits"]["by_risk_class"]["STOCK"] = {
            "standard_pct": 10.0, "hard_ceiling_pct": 10.5,
        }
        self.write_json(self.ips, ips)
        result = self.evaluate(
            quantity=10, price=100_000,
            concentration_override_reason="그냥 통과시켜줘")
        self.assertEqual(result["decision"]["verdict"], "BLOCKED")
        self.assertIn("POSITION_HARD_CEILING", self.codes(result))

    def test_derivative_etf_does_not_inherit_the_broad_etf_limit(self):
        """ETF 껍데기가 같다고 같은 한도를 주면 가장 위험한 자산이 가장 느슨해진다."""
        ips = valid_ips()
        ips["position_limits"]["by_risk_class"]["DERIVATIVE_ETF"] = {
            "standard_pct": 5.0, "hard_ceiling_pct": 5.0,
        }
        self.write_json(self.ips, ips)
        self.holdings.write_text(
            """# 보유 내역 / 기준일 2026-07-25 / 기준통화 KRW
asset_id,name,market,asset_type,risk_class,value_krw,quantity,sector
DERIV,파생ETF,US,ETF,DERIVATIVE_ETF,8000000,100,
ETF1,주식ETF,KR,ETF,BROAD_ETF,22000000,200,
CASH,예수금,KR,CASH,CASH,70000000,,
""",
            encoding="utf-8",
        )
        result = evaluate_trade(
            "DERIV", "buy", 10, 80_000,
            holdings_path=self.holdings, ips_path=self.ips, analysis_path=None,
            price_as_of=PRICE_AT, evaluated_at=EVALUATED_AT,
        )
        self.assertEqual(result["risk_class"], "DERIVATIVE_ETF")
        self.assertIn("POSITION_HARD_CEILING", self.codes(result))

    def test_unclassified_risk_class_is_blocked_not_defaulted(self):
        result = evaluate_trade(
            "NEWCO", "buy", 10, 50_000,
            holdings_path=self.holdings, ips_path=self.ips, analysis_path=None,
            price_as_of=PRICE_AT, evaluated_at=EVALUATED_AT,
            new_asset_risk_class="UNKNOWN_CLASS",
            price_unverified_reason="테스트",
        )
        self.assertEqual(result["decision"]["verdict"], "BLOCKED")
        self.assertIn("RISK_CLASS_LIMIT_MISSING", self.codes(result))

    def test_missing_cost_model_for_a_market_is_blocked(self):
        """국내 비용 모형을 해외 거래에 재사용하면 세금·환전이 통째로 틀린다."""
        result = evaluate_trade(
            "USSTOCK", "buy", 1, 100_000,
            holdings_path=self.holdings, ips_path=self.ips, analysis_path=None,
            price_as_of=PRICE_AT, evaluated_at=EVALUATED_AT,
            new_asset_market="US", new_asset_risk_class="STOCK",
            price_unverified_reason="테스트",
        )
        self.assertEqual(result["decision"]["verdict"], "BLOCKED")
        self.assertIn("COST_MODEL_MISSING", self.codes(result))

    def test_stale_price_reference_is_blocked(self):
        self.write_snapshot("NEWCO", "KRW", 50_000, last_date="2026-06-01")
        result = self.evaluate_new_asset(price=50_000)
        self.assertEqual(result["decision"]["verdict"], "BLOCKED")
        self.assertIn("PRICE_REFERENCE_STALE", self.codes(result))

    def test_all_weight_denominators_are_reported(self):
        result = self.evaluate()
        before = result["weights"]["before"]
        self.assertAlmostEqual(before["total_assets"]["position_pct"], 10.0)
        self.assertAlmostEqual(before["equity_sleeve"]["position_pct"], 100 / 3)
        self.assertAlmostEqual(before["risk_assets"]["position_pct"], 100 / 3)


class ValuationPolicyCase(TradeEngineCase):
    """할인율·영구성장률이 IPS 지배 아래 있는지.

    이 검사가 없으면 요구수익률 12%는 장식이다. 분석가가 할인율만 10%→8%로
    낮추면 같은 종목의 요구 성장률이 38.2%→31.9%로 내려가고, 시나리오를
    조금만 올리면 어떤 종목이든 한도를 통과한다. 승인 IPS 가 못박는 값과
    분석가가 고르는 값이 분리돼 있으면 한도는 우회 가능하다.
    """

    def rebuild(self, **input_overrides):
        """입력을 바꾼 계산 산출물로 갈아끼운다. 지문은 자동으로 다시 계산된다."""
        calculation = valid_calculation()
        calculation["inputs"].update(input_overrides)
        calculation["calculation_id"] = calculation_id_for(
            calculation["model"], calculation["model_version"],
            calculation["inputs"], calculation["outputs"])
        save_validated_calculation(self.calculations, calculation)
        analysis = valid_analysis()
        analysis["expected_return"]["calculation_id"] = calculation["calculation_id"]
        self.write_json(self.analysis, analysis)

    def test_baseline_still_proceeds(self):
        """기준 픽스처는 승인 범위 안이라 통과해야 한다."""
        self.assertEqual(self.evaluate()["decision"]["verdict"], "PROCEED")

    def test_discount_rate_below_policy_is_blocked(self):
        self.rebuild(discount_rate_pct=8.0)   # STOCK 승인 범위 9~14%
        result = self.evaluate()
        self.assertEqual(result["decision"]["verdict"], "BLOCKED")
        self.assertIn("DISCOUNT_RATE_OUT_OF_POLICY", self.codes(result))

    def test_discount_rate_above_policy_is_blocked(self):
        self.rebuild(discount_rate_pct=20.0)
        self.assertIn("DISCOUNT_RATE_OUT_OF_POLICY", self.codes(self.evaluate()))

    def test_terminal_growth_above_cap_is_blocked(self):
        self.rebuild(terminal_growth_pct=5.0)  # 상한 3.0
        self.assertIn("TERMINAL_GROWTH_PCT_OUT_OF_POLICY",
                      self.codes(self.evaluate()))

    def test_horizon_beyond_cap_is_blocked(self):
        self.rebuild(horizon_months=60)        # 상한 36
        self.assertIn("HORIZON_MONTHS_OUT_OF_POLICY", self.codes(self.evaluate()))

    def test_hand_typed_inputs_are_blocked(self):
        """손입력은 사유를 적어도 승인 IPS 아래서는 통과하지 못한다."""
        self.rebuild(input_provenance={
            "verified": False, "source": "manual", "reason": "직접 계산"})
        result = self.evaluate()
        self.assertEqual(result["decision"]["verdict"], "BLOCKED")
        self.assertIn("VALUATION_INPUT_UNVERIFIED", self.codes(result))

    def test_missing_provenance_is_blocked(self):
        calculation = valid_calculation()
        del calculation["inputs"]["input_provenance"]
        calculation["calculation_id"] = calculation_id_for(
            calculation["model"], calculation["model_version"],
            calculation["inputs"], calculation["outputs"])
        save_validated_calculation(self.calculations, calculation)
        analysis = valid_analysis()
        analysis["expected_return"]["calculation_id"] = calculation["calculation_id"]
        self.write_json(self.analysis, analysis)
        self.assertIn("VALUATION_INPUT_PROVENANCE_MISSING",
                      self.codes(self.evaluate()))

    def test_ips_without_valuation_policy_blocks(self):
        """정책이 없으면 기본값으로 때우지 않는다. 위험등급 한도와 같은 원칙."""
        ips = valid_ips()
        del ips["valuation_assumptions"]
        self.write_json(self.ips, ips)
        result = self.evaluate()
        self.assertEqual(result["decision"]["verdict"], "BLOCKED")

    def test_unknown_risk_class_has_no_discount_range(self):
        ips = valid_ips()
        del ips["valuation_assumptions"]["by_risk_class"]["STOCK"]
        self.write_json(self.ips, ips)
        self.assertIn("VALUATION_RISK_CLASS_MISSING", self.codes(self.evaluate()))

    def test_valuation_inputs_are_reported(self):
        basis = self.evaluate()["decision_basis"]["valuation_inputs"]
        self.assertEqual(basis["discount_rate_pct"], 10.0)
        self.assertTrue(basis["input_provenance"]["verified"])


if __name__ == "__main__":
    unittest.main()
