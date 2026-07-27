"""주장·출처·스냅샷 저장 계약 테스트."""

import json
import math
import tempfile
import unittest
from pathlib import Path

from contracts import (ContractError, calculation_id_for, save_validated_analysis,
                       validate_analysis, validate_ips)

SHA = "a" * 64

# 계산 산출물 픽스처. calculation_id 는 모델과 입력의 지문이므로 손으로 적지 않고
# 계약과 같은 함수로 만든다 — 그래야 지문 검사가 실제로 검사가 된다.
CALC_MODEL = "reverse_dcf"
CALC_MODEL_VERSION = "1.0.0"
CALC_INPUTS = {
    "price": 330.0, "shares": 3.5e9, "fcf": 3.6e9, "net_debt": -30e9,
    "discount_rate_pct": 10.0, "terminal_growth_pct": 2.5, "years": 10,
    "horizon_months": 12,
    "scenario_growth_pct": {"bear": 15.0, "base": 30.0, "bull": 50.0},
    # 입력이 원문 스냅샷에서 왔는지 손으로 쳤는지. 승인 IPS 가
    # require_verified_inputs 를 걸면 이 기록이 없는 계산은 통과하지 못한다.
    "input_provenance": {
        "verified": True,
        "source": "edgar_fundamentals",
        "fundamentals_id": "FUND-" + "0" * 20,
        "period_end": "2026-06-30",
        "fundamentals_sha256": "b" * 64,
    },
}
CALC_OUTPUTS = {
    "implied_growth_pct": 46.8,
    "scenarios": {
        "bear": {"return_pct": -20.0},
        "base": {"return_pct": 15.0},
        "bull": {"return_pct": 40.0},
    },
}


def valid_calculation(scenario_returns=None):
    """계산 산출물 픽스처.

    scenario_returns 를 주면 그 값으로 산출물을 만들고 지문을 다시 계산한다.
    ID 가 모델·입력·산출값 전체의 지문이라 손으로 적을 수 없다.
    """
    inputs = json.loads(json.dumps(CALC_INPUTS))
    outputs = json.loads(json.dumps(CALC_OUTPUTS))
    if scenario_returns:
        for name, value in scenario_returns.items():
            outputs["scenarios"][name]["return_pct"] = value
    return {
        "schema_version": "1.1.0",
        "calculation_id": calculation_id_for(
            CALC_MODEL, CALC_MODEL_VERSION, inputs, outputs),
        "model": CALC_MODEL,
        "model_version": CALC_MODEL_VERSION,
        "instrument_id": "AAA",
        "created_at": "2026-07-26T00:00:00+09:00",
        "source_ids": ["SRC-1"],
        "inputs": inputs,
        "outputs": outputs,
    }


CALC_ID = valid_calculation()["calculation_id"]


def valid_analysis():
    return {
        "schema_version": "1.1.0",
        "analysis_id": "ANALYSIS-AAA-1",
        "instrument_id": "AAA",
        "company_id": "COMPANY-AAA",
        "as_of": "2026-07-26T00:00:00+09:00",
        "snapshot": {
            "snapshot_id": "SNAPSHOT-AAA-1",
            "created_at": "2026-07-26T00:00:00+09:00",
            "source_ids": ["SRC-1"],
            "input_hashes": {"filing": SHA},
        },
        "sources": [{
            "source_id": "SRC-1",
            "source_type": "FILING",
            "locator": "data/raw/filing.json",
            "published_at": "2026-07-25T18:00:00+09:00",
            "retrieved_at": "2026-07-26T00:00:00+09:00",
            "content_hash": SHA,
        }],
        "claims": [{
            "claim_id": "CLAIM-1",
            "type": "FACT",
            "text": "원문에서 확인한 사실",
            "source_ids": ["SRC-1"],
        }, {
            "claim_id": "CLAIM-BEAR",
            "type": "FACT",
            "text": "하방을 뒷받침하는 관측 사실",
            "source_ids": ["SRC-1"],
        }],
        "thesis": {
            "status": "APPROVED",
            "change": "NEUTRAL",
            "invalidation_triggered": False,
            "approved_by": "USER",
            "approved_at": "2026-07-25T21:00:00+09:00",
            "claim_ids": ["CLAIM-1"],
        },
        "counter_thesis": {
            "text": "경쟁사 진입으로 단가가 무너질 수 있다",
            "invalidation_signal": "분기 OPM YoY 2개 분기 연속 하락",
            "claim_ids": ["CLAIM-BEAR"],
        },
        "expected_return": {
            "horizon_months": 12,
            "required_return_pct": 10.0,
            "methodology": "2단계 FCF 역산 DCF",
            "calculation_id": CALC_ID,
            "probability_weighted_pct": 12.5,
            "scenarios": {
                "bear": {"return_pct": -20.0, "probability": 0.25,
                         "claim_ids": ["CLAIM-BEAR"]},
                "base": {"return_pct": 15.0, "probability": 0.5,
                         "claim_ids": ["CLAIM-1"]},
                "bull": {"return_pct": 40.0, "probability": 0.25,
                         "claim_ids": ["CLAIM-1"]},
            },
        },
        "alternatives": [{
            "instrument_id": "CORE-ETF",
            "expected_return_pct": 8.0,
            "measure": "PROBABILITY_WEIGHTED",
            "snapshot_id": "SNAPSHOT-CORE-1",
            "claim_ids": ["CLAIM-1"],
        }],
    }


def valid_ips(status="APPROVED"):
    approved = status == "APPROVED"
    return {
        "schema_version": "1.1.0",
        "ips_id": "IPS-1",
        "status": status,
        "base_currency": "KRW",
        "approved_by": "USER" if approved else None,
        "approved_at": "2026-07-25T00:00:00+09:00" if approved else None,
        "position_limits": {
            "by_risk_class": {
                "STOCK": {
                    "standard_pct": 30.0 if approved else None,
                    "hard_ceiling_pct": 50.0 if approved else None,
                },
                "BROAD_ETF": {
                    "standard_pct": 40.0 if approved else None,
                    "hard_ceiling_pct": 50.0 if approved else None,
                },
            },
            "equity_sleeve_pct": 70.0 if approved else None,
            "risk_assets_pct": 70.0 if approved else None,
        },
        "portfolio_limits": {
            "top10_total_assets_pct": 70.0,
            "sector_total_assets_pct": 40.0,
            "currency_total_assets_pct": 100.0,
            "unclassified_total_assets_pct": 20.0,
        },
        "data_freshness": {
            "holdings_max_age_days": 1 if approved else None,
            "analysis_max_age_days": 7 if approved else None,
            "price_max_age_minutes": 60 if approved else None,
        },
        "data_quality": {
            "price_max_deviation_pct": 30.0 if approved else None,
            "price_reference_max_age_days": 3 if approved else None,
        },
        "costs": {
            "by_market": {
                "KR": {
                    "buy_commission_bps": 0.0 if approved else None,
                    "sell_commission_bps": 0.0 if approved else None,
                    "sell_tax_bps": 0.0 if approved else None,
                    "fx_cost_bps": 0.0 if approved else None,
                    "slippage_bps": 0.0 if approved else None,
                },
            },
        },
        "valuation_assumptions": {
            "by_risk_class": {
                "STOCK": {
                    "discount_rate_pct_min": 9.0 if approved else None,
                    "discount_rate_pct_max": 14.0 if approved else None,
                },
                "BROAD_ETF": {
                    "discount_rate_pct_min": 7.0 if approved else None,
                    "discount_rate_pct_max": 11.0 if approved else None,
                },
            },
            "terminal_growth_pct_max": 3.0 if approved else None,
            "stage_one_years_max": 10 if approved else None,
            "horizon_months_max": 36 if approved else None,
            "require_verified_inputs": True if approved else None,
        },
        "decision_policy": {
            "required_expected_return_pct": 10.0 if approved else None,
            "alternative_min_advantage_pct": 3.0 if approved else None,
            "block_buy_below_required_return": True if approved else None,
            "return_measure": "PROBABILITY_WEIGHTED" if approved else None,
            "max_bear_loss_contribution_pct": 8.0 if approved else None,
        },
    }


class TestValuationAssumptions(unittest.TestCase):
    """할인율이 지배되지 않으면 요구수익률 한도는 우회 가능한 장식이다.

    요구수익률 12%를 0.1% 단위로 못박아 놓고 그 12%를 계산하는 할인율을
    분석가가 고르게 하면, 다이얼 하나로 어떤 종목이든 통과시킬 수 있다.
    """

    def test_approved_ips_must_define_valuation_assumptions(self):
        ips = valid_ips()
        del ips["valuation_assumptions"]
        errors = validate_ips(ips)
        self.assertTrue(any("valuation_assumptions" in e for e in errors), errors)

    def test_draft_ips_may_omit_them(self):
        ips = valid_ips(status="DRAFT")
        del ips["valuation_assumptions"]
        self.assertEqual(validate_ips(ips), [])

    def test_min_above_max_is_rejected(self):
        ips = valid_ips()
        ips["valuation_assumptions"]["by_risk_class"]["STOCK"] = {
            "discount_rate_pct_min": 15.0, "discount_rate_pct_max": 9.0}
        self.assertTrue(validate_ips(ips))

    def test_terminal_growth_above_discount_floor_is_rejected(self):
        """영구성장률이 할인율 하한 이상이면 잔존가치가 발산한다."""
        ips = valid_ips()
        ips["valuation_assumptions"]["terminal_growth_pct_max"] = 9.0
        errors = validate_ips(ips)
        self.assertTrue(any("발산" in e for e in errors), errors)

    def test_require_verified_inputs_must_be_boolean(self):
        ips = valid_ips()
        ips["valuation_assumptions"]["require_verified_inputs"] = "yes"
        self.assertTrue(validate_ips(ips))


class TestAnalysisContract(unittest.TestCase):
    def test_valid_analysis_passes(self):
        self.assertEqual(validate_analysis(valid_analysis()), [])

    def test_unknown_source_reference_fails(self):
        data = valid_analysis()
        data["claims"][0]["source_ids"] = ["SRC-MISSING"]
        errors = validate_analysis(data)
        self.assertTrue(any("존재하지 않는 출처 ID" in error for error in errors))

    def test_duplicate_claim_id_fails(self):
        data = valid_analysis()
        data["claims"].append(dict(data["claims"][0]))
        errors = validate_analysis(data)
        self.assertTrue(any("중복 ID" in error for error in errors))

    def test_inverted_return_range_fails(self):
        data = valid_analysis()
        data["expected_return"]["scenarios"]["bear"]["return_pct"] = 30.0
        errors = validate_analysis(data)
        self.assertTrue(any("bear ≤ base ≤ bull" in error for error in errors))

    def test_non_finite_return_fails(self):
        data = valid_analysis()
        data["expected_return"]["scenarios"]["base"]["return_pct"] = math.nan
        self.assertTrue(any("유한한 숫자" in error for error in validate_analysis(data)))

    def test_scenario_without_evidence_fails(self):
        """근거 없는 시나리오는 숫자일 뿐이다. 특히 bear 가 그렇다."""
        data = valid_analysis()
        data["expected_return"]["scenarios"]["bear"]["claim_ids"] = []
        errors = validate_analysis(data)
        self.assertTrue(any("scenarios.bear.claim_ids" in error for error in errors))

    def test_probabilities_must_sum_to_one(self):
        data = valid_analysis()
        data["expected_return"]["scenarios"]["bull"]["probability"] = 0.4
        errors = validate_analysis(data)
        self.assertTrue(any("확률 합이 1이어야 한다" in error for error in errors))

    def test_zero_probability_scenario_fails(self):
        data = valid_analysis()
        data["expected_return"]["scenarios"]["bear"]["probability"] = 0.0
        data["expected_return"]["scenarios"]["base"]["probability"] = 0.75
        data["expected_return"]["probability_weighted_pct"] = 21.25
        errors = validate_analysis(data)
        self.assertTrue(any("0보다 커야 한다" in error for error in errors))

    def test_weighted_return_must_match_the_scenarios(self):
        """가중평균을 손으로 적고 시나리오는 따로 노는 분석을 막는다."""
        data = valid_analysis()
        data["expected_return"]["probability_weighted_pct"] = 30.0
        errors = validate_analysis(data)
        self.assertTrue(any("시나리오에서 계산한 값" in error for error in errors))

    def test_handwritten_calculation_id_fails(self):
        """기대수익은 승인된 계산기가 만든 값이어야 한다."""
        data = valid_analysis()
        data["expected_return"]["calculation_id"] = "CALC-001"
        errors = validate_analysis(data)
        self.assertTrue(any("손으로 적을 수 없다" in error for error in errors))

    def test_missing_counter_thesis_fails(self):
        data = valid_analysis()
        del data["counter_thesis"]
        errors = validate_analysis(data)
        self.assertTrue(any("counter_thesis" in error for error in errors))

    def test_counter_thesis_needs_an_observable_signal(self):
        data = valid_analysis()
        data["counter_thesis"]["invalidation_signal"] = "  "
        errors = validate_analysis(data)
        self.assertTrue(
            any("counter_thesis.invalidation_signal" in error for error in errors))

    def test_alternative_measured_differently_fails(self):
        data = valid_analysis()
        data["alternatives"][0]["measure"] = "MADE_UP"
        errors = validate_analysis(data)
        self.assertTrue(any("alternatives[0].measure" in error for error in errors))

    def test_unknown_scenario_name_fails(self):
        data = valid_analysis()
        data["expected_return"]["scenarios"]["moon"] = {
            "return_pct": 900.0, "probability": 0.1, "claim_ids": ["CLAIM-1"],
        }
        errors = validate_analysis(data)
        self.assertTrue(any("알 수 없는 시나리오 moon" in error for error in errors))

    def test_empty_alternatives_fails(self):
        data = valid_analysis()
        data["alternatives"] = []
        self.assertTrue(any("하나 이상의 대안" in error for error in validate_analysis(data)))

    def test_approved_thesis_requires_approval_metadata(self):
        data = valid_analysis()
        data["thesis"]["approved_by"] = None
        self.assertTrue(any("thesis.approved_by" in error for error in validate_analysis(data)))

    def test_invalidated_thesis_state_must_be_consistent(self):
        data = valid_analysis()
        data["thesis"]["status"] = "INVALIDATED"
        errors = validate_analysis(data)
        self.assertTrue(any("change=INVALIDATE" in error for error in errors))

    def test_claim_source_must_belong_to_snapshot(self):
        data = valid_analysis()
        extra = dict(data["sources"][0])
        extra["source_id"] = "SRC-2"
        data["sources"].append(extra)
        data["claims"][0]["source_ids"] = ["SRC-2"]
        errors = validate_analysis(data)
        self.assertTrue(any("스냅샷에 연결되지 않은 출처" in error for error in errors))

    def test_invalid_analysis_is_never_saved(self):
        data = valid_analysis()
        data["snapshot"]["input_hashes"] = {}
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "analysis.json"
            with self.assertRaises(ContractError):
                save_validated_analysis(target, data)
            self.assertFalse(target.exists())

    def test_invalid_analysis_does_not_overwrite_existing_file(self):
        data = valid_analysis()
        data["alternatives"] = []
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "analysis.json"
            target.write_text('{"preserved": true}\n', encoding="utf-8")
            with self.assertRaises(ContractError):
                save_validated_analysis(target, data)
            self.assertEqual(
                json.loads(target.read_text(encoding="utf-8")),
                {"preserved": True},
            )

    def test_unknown_nan_field_is_not_saved(self):
        data = valid_analysis()
        data["unexpected"] = math.nan
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "analysis.json"
            with self.assertRaises(ContractError):
                save_validated_analysis(target, data)
            self.assertFalse(target.exists())

    def test_valid_analysis_is_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "analysis.json"
            save_validated_analysis(target, valid_analysis())
            self.assertEqual(json.loads(target.read_text())["analysis_id"], "ANALYSIS-AAA-1")


class TestIpsContract(unittest.TestCase):
    def test_approved_ips_requires_complete_values(self):
        data = valid_ips()
        data["position_limits"]["by_risk_class"]["STOCK"]["standard_pct"] = None
        self.assertTrue(validate_ips(data))

    def test_standard_limit_cannot_exceed_the_hard_ceiling(self):
        data = valid_ips()
        data["position_limits"]["by_risk_class"]["STOCK"] = {
            "standard_pct": 60.0, "hard_ceiling_pct": 50.0,
        }
        errors = validate_ips(data)
        self.assertTrue(any("hard_ceiling_pct보다 클 수 없다" in error for error in errors))

    def test_approved_ips_must_define_at_least_one_risk_class(self):
        data = valid_ips()
        data["position_limits"]["by_risk_class"] = {}
        self.assertTrue(validate_ips(data))

    def test_draft_ips_can_hold_null_decisions(self):
        self.assertEqual(validate_ips(valid_ips("DRAFT")), [])

    def test_approved_ips_must_set_a_price_deviation_limit(self):
        """승인 IPS에 예정가 허용 괴리가 없으면 가격 검증이 조용히 꺼진다."""
        data = valid_ips()
        data["data_quality"]["price_max_deviation_pct"] = None
        self.assertTrue(validate_ips(data))

        del data["data_quality"]
        self.assertTrue(validate_ips(data))


if __name__ == "__main__":
    unittest.main()
