"""CIO 경계, 자산 라우팅, 결정 기록의 핵심 회귀 테스트."""

import json
import unittest
from dataclasses import dataclass
from unittest.mock import patch

from cio.assets import AssetRegistryError, validate_registry
from cio.context import build_context
from cio.decision import (
    OUTCOME_METRIC,
    context_id_for,
    normalize_decision,
    validate_decision,
)
from cio.memory import evaluate_assessment
from cio.refresh import tasks_for


def valid_context(asset_id="TSLA"):
    data = {
        "schema_version": "cio-context/1.0.0",
        "context_id": None,
        "created_at": "2026-07-28T09:00:00+09:00",
        "request": {
            "asset_id": asset_id,
            "price": 100.0,
            "price_currency": "USD",
            "amount": 1000.0,
            "amount_currency": "USD",
            "as_of": "2026-01-01T00:00:00+00:00",
        },
        "validation": {"status": "PASS", "problems": [], "optional_gaps": []},
        "evidence_index": [{"source_id": f"PRICE:{asset_id}"}],
        "model_contract": {"prompt_version": "1.0"},
    }
    data["context_id"] = context_id_for(data)
    return data


def valid_decision():
    context = valid_context()
    return {
        "schema_version": "cio-decision/1.0.0",
        "assessment_id": None,
        "context_id": context["context_id"],
        "asset_id": "TSLA",
        "created_at": "2026-07-28T10:00:00+09:00",
        "model": {
            "provider": "fixture",
            "name": "cio-test",
            "version": "1",
            "prompt_version": "1.0",
        },
        "verdict": "WAIT",
        "outcome_metric": OUTCOME_METRIC,
        "horizon_months": 1,
        "win_definition": "한 달 뒤 총수익률이 10% 이상",
        "failure_definition": "한 달 뒤 총수익률이 -15% 이하",
        "win_return_threshold_pct": 10.0,
        "failure_return_threshold_pct": -15.0,
        "win_probability_pct": 55.0,
        "confidence_pct": 60.0,
        "expected_return_pct": {
            "bear": -20.0,
            "base": 20.0,
            "bull": 60.0,
            "probability_weighted": 20.0,
        },
        "scenario_probability_pct": {
            "bear": 25.0,
            "base": 50.0,
            "bull": 25.0,
        },
        "price_plan": {
            "currency": "USD",
            "preferred_entry": 95.0,
            "entry_range_low": 90.0,
            "entry_range_high": 100.0,
            "stop_price": 80.0,
            "scale_out_start": 140.0,
            "reask_below": 85.0,
            "reask_above": 130.0,
            "reask_events": ["분기 실적"],
        },
        "proposed_trade": {
            "amount": 0.0,
            "currency": "USD",
            "rationale": "현재는 기다리므로 집행하지 않는다",
        },
        "action": {
            "direction": "HOLD",
            "quantity": None,
            "sell_reason": None,
            "rationale": "보유분을 그대로 둔다",
        },
        "drivers": [
            {
                "name": "자동차 판매량과 마진",
                "weight_pct": 70,
                "basis": "매출의 대부분",
                "researched": True,
                "finding": "분기 인도량이 둔화됐다",
            },
            {
                "name": "에너지 저장",
                "weight_pct": 30,
                "basis": "성장률은 높으나 규모가 작다",
                "researched": False,
                "skipped_reason": "규모가 작아 이 가격 판단을 바꾸지 못한다",
            },
        ],
        "method": {
            "valuation_family": "DCF",
            "why_fit": "양의 현금흐름이 있다",
            "assumptions": ["할인율은 추정치"],
        },
        "thesis": "현재 가격보다 낮은 진입가에서 보상비가 낫다",
        "strongest_counter_thesis": {
            "text": "성장률이 예상보다 높으면 기다리는 비용이 크다",
            "evidence_refs": ["PRICE:TSLA"],
        },
        "invalidation_signals": ["현금흐름 악화"],
        "unknowns": ["다음 분기 마진"],
        "research_requests": [],
        "sources": [],
        "evidence_refs": ["PRICE:TSLA"],
    }


class TestRegistryAndRouting(unittest.TestCase):
    def test_only_kr_us_stock_etf_are_supported(self):
        invalid = {
            "schema_version": "asset-registry/1.0.0",
            "assets": {
                "BTC": {
                    "name": "Bitcoin",
                    "market": "GLOBAL",
                    "exchange": "CRYPTO",
                    "asset_type": "CRYPTO",
                    "currency": "USD",
                    "price_symbol": "BTC-USD",
                    "sector_code": "CRYPTO",
                    "analysis_lens": "CRYPTO",
                    "valuation_family": ["OTHER"],
                    "benchmarks": ["BTC-USD"],
                }
            },
        }
        with self.assertRaises(AssetRegistryError):
            validate_registry(invalid)

    def test_kr_stock_routes_to_dart_and_kr_events(self):
        tasks = tasks_for("005930")
        modules = [task.command[2] for task in tasks if task.command]
        self.assertIn("collectors.dart", modules)
        self.assertIn("collectors.kr_events", modules)
        self.assertNotIn("collectors.edgar", modules)

    def test_us_stock_routes_to_edgar_without_dart_events(self):
        tasks = tasks_for("TSLA")
        modules = [task.command[2] for task in tasks if task.command]
        capabilities = {task.capability for task in tasks}
        self.assertIn("collectors.edgar", modules)
        self.assertIn("FILINGS", capabilities)
        self.assertNotIn("collectors.dart", modules)
        self.assertNotIn("collectors.kr_events", modules)

    def test_both_markets_get_an_event_packet(self):
        """급변일 ↔ 공시 대조는 시장을 가리지 않는다.

        미국에 없던 시절 packet 의 `events` 는 통째로 null 이었고, 그것은
        "급변이 없었다" 와 구분되지 않았다.
        """
        for asset_id, module in (
            ("005930", "collectors.kr_events"),
            ("TSLA", "collectors.us_events"),
            ("IBM", "collectors.us_events"),
        ):
            with self.subTest(asset_id=asset_id):
                tasks = tasks_for(asset_id)
                events = [t for t in tasks if t.capability == "EVENTS"]
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0].command[2], module)

    def test_us_event_packet_depends_on_the_filing_index(self):
        """미국판은 SEC 를 다시 부르지 않고 색인을 읽는다.

        색인이 갱신됐는데 대조를 다시 하지 않으면, 새 공시가 난 급변일이
        '근접 공시 없음' 으로 남는다.
        """
        events = next(
            t for t in tasks_for("TSLA") if t.capability == "EVENTS"
        )
        self.assertIn("knowledge/filings/TSLA.json", events.dependencies)

    def test_etf_routes_to_holdings_not_company_fundamentals(self):
        tasks = tasks_for("SCHD")
        capabilities = {task.capability for task in tasks}
        self.assertIn("ETF_HOLDINGS", capabilities)
        self.assertNotIn("FUNDAMENTALS", capabilities)
        self.assertFalse(
            any(
                task.command
                and task.command[2] in {"collectors.dart", "collectors.edgar"}
                for task in tasks
            )
        )


@dataclass
class FreshTask:
    name: str
    required: bool = True
    state: str = "FRESH"


class TestCompactContext(unittest.TestCase):
    def test_price_and_amount_are_first_class_and_packet_is_small(self):
        price = {
            "symbol": "TSLA",
            "currency": "USD",
            "as_of": "2026-07-27",
            "close": 99.0,
            "returns_pct": {"1m": 1.0, "3m": 2.0, "12m": 3.0},
        }
        source = {
            "source_id": "PRICE:TSLA",
            "kind": "MARKET_DATA",
            "path": "knowledge/prices/TSLA.csv",
            "data_as_of": "2026-07-27",
        }
        with (
            patch("cio.context._price_summary", return_value=(price, source)),
            patch(
                "cio.context._latest_fundamentals",
                return_value=(
                    {"fundamentals_id": "FUND-x", "period_end": "2026-06-30"},
                    {
                        "source_id": "FUNDAMENTALS:TSLA",
                        "kind": "FILING_DERIVED",
                        "path": "knowledge/fundamentals/TSLA.json",
                        "data_as_of": "2026-06-30",
                    },
                ),
            ),
            patch(
                "cio.context._filing_summary",
                return_value=(
                    {
                        "collected_at": "2026-07-28T00:00:00+00:00",
                        "latest": [{"form": "10-Q"}],
                    },
                    {
                        "source_id": "FILINGS:TSLA",
                        "kind": "REGULATOR_FILING_INDEX",
                        "path": None,
                        "data_as_of": "2026-07-28T00:00:00+00:00",
                    },
                ),
            ),
            patch("cio.context._latest_calculation", return_value=(None, None)),
            patch(
                "cio.context._market_summary",
                return_value=(
                    {"market": "US", "regime": "NORMAL"},
                    {
                        "source_id": "MARKET_STATE:US",
                        "kind": "DERIVED",
                        "path": "knowledge/market/regime-US.json",
                        "data_as_of": "2026-07-27",
                    },
                ),
            ),
            patch("cio.context._event_summary", return_value=(None, None)),
            patch("cio.context._etf_summary", return_value=(None, None)),
            patch(
                "cio.context._portfolio_summary",
                return_value=({"available": False}, None),
            ),
            patch("cio.context._memory_digest", return_value=[]),
            patch(
                "cio.context.inspect_plan",
                return_value=[FreshTask("price:TSLA")],
            ),
        ):
            packet, path = build_context(
                "TSLA",
                price=100.0,
                amount=10_000_000,
                amount_currency="KRW",
                save=False,
            )
        self.assertIsNone(path)
        self.assertEqual(packet["request"]["price"], 100.0)
        self.assertEqual(packet["request"]["amount"], 10_000_000)
        self.assertEqual(packet["validation"]["status"], "PASS")
        size = len(
            json.dumps(packet, ensure_ascii=False, separators=(",", ":")).encode()
        )
        self.assertLess(size, 30_000)


class TestDecisionBoundary(unittest.TestCase):
    def test_valid_provider_neutral_decision(self):
        normalized = normalize_decision(valid_decision())
        self.assertEqual(validate_decision(normalized, context=valid_context()), [])
        self.assertTrue(normalized["assessment_id"].startswith("CIO-"))

    def test_pc_rejects_arithmetic_not_investment_opinion(self):
        data = valid_decision()
        data["verdict"] = "BUY_NOW"
        data["expected_return_pct"]["probability_weighted"] = 99.0
        data["proposed_trade"]["amount"] = 2000.0
        errors = validate_decision(data, context=valid_context())
        self.assertTrue(any("가중값" in error for error in errors))
        self.assertTrue(any("제시한 금액" in error for error in errors))
        self.assertFalse(any("BUY_NOW" in error for error in errors))

    def test_currency_boundary_is_enforced(self):
        data = valid_decision()
        data["price_plan"]["currency"] = "KRW"
        errors = validate_decision(data, context=valid_context())
        self.assertTrue(any("입력 가격 통화" in error for error in errors))

    def test_evaluation_observes_return_and_price_triggers(self):
        assessment = normalize_decision(valid_decision())
        rows = [
            {
                "date": "2026-01-02",
                "close": 100.0,
                "high": 101.0,
                "low": 99.0,
            },
            {
                "date": "2026-01-15",
                "close": 82.0,
                "high": 105.0,
                "low": 79.0,
            },
            {
                "date": "2026-01-30",
                "close": 115.0,
                "high": 141.0,
                "low": 81.0,
            },
            {
                "date": "2026-02-02",
                "close": 116.0,
                "high": 120.0,
                "low": 114.0,
            },
        ]
        with (
            patch(
                "cio.memory.get_asset",
                return_value={"asset_id": "TSLA", "price_symbol": "TSLA"},
            ),
            patch("cio.memory.find_context", return_value=(valid_context(), None)),
        ):
            result = evaluate_assessment(assessment, rows=rows)
        self.assertTrue(result["window"]["complete"])
        self.assertTrue(result["thresholds"]["win_observed"])
        self.assertIsNotNone(result["triggers"]["stop"])
        self.assertIsNotNone(result["triggers"]["scale_out_start"])
        self.assertIsNotNone(result["calibration"]["brier_score"])


class TestSellContract(unittest.TestCase):
    """verdict(살 것인가)와 action(가진 것을 어쩔 것인가)의 경계."""

    def _sell(self, **overrides):
        data = valid_decision()
        data["action"] = {
            "direction": "TRIM",
            "quantity": 25.0,
            "sell_reason": "VALUATION",
            "rationale": "고평가 구간이라 줄인다",
            **overrides,
        }
        return normalize_decision(data)

    def test_judgment_sell_cannot_coexist_with_buy_now(self):
        """'이 가격에 사겠다'와 '비싸서 판다'를 동시에 말할 수 없다."""
        data = self._sell()
        data["verdict"] = "BUY_NOW"
        data["proposed_trade"]["amount"] = 1000.0
        errors = validate_decision(normalize_decision(data))
        self.assertTrue(any("BUY_NOW와 함께 쓸 수 없다" in e for e in errors))

    def test_constraint_sell_may_coexist_with_a_favourable_verdict(self):
        """자산이 좋아도 제약 때문에는 팔 수 있다 — 대신 제약이라고 밝혀야 한다."""
        data = self._sell(sell_reason="CONCENTRATION")
        data["verdict"] = "BUY_NOW"
        data["proposed_trade"]["amount"] = 0.0
        errors = validate_decision(normalize_decision(data))
        self.assertFalse(any("sell_reason" in e for e in errors), errors)

    def test_sell_requires_a_reason_and_a_quantity(self):
        missing_reason = self._sell(sell_reason=None)
        self.assertTrue(
            any("매도에는 사유가 필요하다" in e
                for e in validate_decision(missing_reason))
        )
        missing_quantity = self._sell(quantity=None)
        self.assertTrue(
            any("action.quantity" in e
                for e in validate_decision(missing_quantity))
        )

    def test_holding_cannot_carry_a_sell_reason_or_quantity(self):
        data = self._sell(direction="HOLD")
        errors = validate_decision(data)
        self.assertTrue(any("매도가 아니면 null이어야 한다" in e for e in errors))

    def test_selling_while_proposing_to_buy_is_rejected(self):
        data = valid_decision()
        data["verdict"] = "BUY_NOW"
        data["proposed_trade"]["amount"] = 1000.0
        data["action"] = {
            "direction": "EXIT",
            "quantity": 95.0,
            "sell_reason": "LIQUIDITY_NEED",
            "rationale": "현금이 필요하다",
        }
        errors = validate_decision(normalize_decision(data))
        self.assertTrue(
            any("새로 사겠다는 금액이 있다" in e for e in errors), errors
        )


class TestSellScoring(unittest.TestCase):
    """매도는 부호가 반대다. 뒤집지 않으면 좋은 매도가 전부 패배로 남는다."""

    def _evaluate(self, assessment, end_close):
        rows = [
            {
                "date": "2026-01-02",
                "close": 100.0,
                "adjusted_close": 100.0,
                "has_adjusted_close": True,
                "split_adjustment_factor": 1.0,
                "high": 100.0,
                "low": 100.0,
            },
            {
                "date": "2026-01-20",
                "close": end_close,
                "adjusted_close": end_close,
                "has_adjusted_close": True,
                "split_adjustment_factor": 1.0,
                "high": end_close,
                "low": end_close,
            },
            # 지평(2026-02-01) 이후 행이 있어야 평가가 완료로 확정된다.
            {
                "date": "2026-02-05",
                "close": end_close,
                "adjusted_close": end_close,
                "has_adjusted_close": True,
                "split_adjustment_factor": 1.0,
                "high": end_close,
                "low": end_close,
            },
        ]
        with (
            patch(
                "cio.memory.get_asset",
                return_value={"asset_id": "TSLA", "price_symbol": "TSLA"},
            ),
            patch("cio.memory.find_context", return_value=(valid_context(), None)),
        ):
            return evaluate_assessment(assessment, rows=rows)

    def _trim(self, reason="CONCENTRATION"):
        data = valid_decision()
        data["horizon_months"] = 1
        data["action"] = {
            "direction": "TRIM",
            "quantity": 25.0,
            "sell_reason": reason,
            "rationale": "일부만 줄인다",
        }
        return normalize_decision(data)

    def test_a_falling_price_makes_the_sold_leg_an_avoided_loss(self):
        result = self._evaluate(self._trim(), end_close=70.0)
        legs = result["action"]
        self.assertEqual(legs["sold_leg"]["outcome"], "AVOIDED_LOSS")
        self.assertLess(legs["sold_leg"]["forgone_return_pct"], 0)
        self.assertEqual(legs["retained_leg"]["outcome"], "LOSS")

    def test_a_rising_price_makes_the_sold_leg_a_missed_gain(self):
        result = self._evaluate(self._trim(), end_close=140.0)
        legs = result["action"]
        self.assertEqual(legs["sold_leg"]["outcome"], "MISSED_GAIN")
        # 같은 사건이 남긴 수량에는 이득이다 — 두 다리를 분리하는 이유다.
        self.assertEqual(legs["retained_leg"]["outcome"], "GAIN")

    def test_sell_reason_tier_is_recorded_for_split_aggregation(self):
        constraint = self._evaluate(self._trim("CONCENTRATION"), end_close=140.0)
        judgment = self._evaluate(self._trim("VALUATION"), end_close=140.0)
        self.assertEqual(constraint["action"]["sell_reason_tier"], "CONSTRAINT")
        self.assertEqual(judgment["action"]["sell_reason_tier"], "JUDGMENT")

    def test_holding_has_no_legs(self):
        result = self._evaluate(normalize_decision(valid_decision()), 140.0)
        self.assertIsNone(result["action"]["sold_leg"])
        self.assertIsNone(result["action"]["retained_leg"])


if __name__ == "__main__":
    unittest.main()
