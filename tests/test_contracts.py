"""주장·출처·스냅샷 저장 계약 테스트."""

import json
import math
import tempfile
import unittest
from pathlib import Path

from contracts import ContractError, save_validated_analysis, validate_analysis, validate_ips

SHA = "a" * 64


def valid_analysis():
    return {
        "schema_version": "1.0.0",
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
        }],
        "thesis": {
            "status": "APPROVED",
            "change": "NEUTRAL",
            "invalidation_triggered": False,
            "approved_by": "USER",
            "approved_at": "2026-07-25T21:00:00+09:00",
            "claim_ids": ["CLAIM-1"],
        },
        "expected_return": {
            "horizon_months": 12,
            "bear_pct": -20.0,
            "base_pct": 15.0,
            "bull_pct": 40.0,
            "required_return_pct": 10.0,
            "methodology": "시나리오 가치평가",
            "calculation_id": "CALC-1",
            "claim_ids": ["CLAIM-1"],
        },
        "alternatives": [{
            "instrument_id": "CORE-ETF",
            "base_expected_return_pct": 8.0,
            "snapshot_id": "SNAPSHOT-CORE-1",
            "claim_ids": ["CLAIM-1"],
        }],
    }


def valid_ips(status="APPROVED"):
    approved = status == "APPROVED"
    return {
        "schema_version": "1.0.0",
        "ips_id": "IPS-1",
        "status": status,
        "base_currency": "KRW",
        "approved_by": "USER" if approved else None,
        "approved_at": "2026-07-25T00:00:00+09:00" if approved else None,
        "position_limits": {
            "total_assets_pct": 30.0 if approved else None,
            "equity_sleeve_pct": 70.0 if approved else None,
            "risk_assets_pct": 70.0 if approved else None,
        },
        "portfolio_limits": {
            "top10_total_assets_pct": 70.0,
            "sector_total_assets_pct": 40.0,
            "currency_total_assets_pct": 80.0,
            "unclassified_total_assets_pct": 20.0,
        },
        "data_freshness": {
            "holdings_max_age_days": 1 if approved else None,
            "analysis_max_age_days": 7 if approved else None,
            "price_max_age_minutes": 60 if approved else None,
        },
        "costs": {
            "buy_commission_bps": 0.0 if approved else None,
            "sell_commission_bps": 0.0 if approved else None,
            "sell_tax_bps": 0.0 if approved else None,
            "fx_cost_bps": 0.0 if approved else None,
            "slippage_bps": 0.0 if approved else None,
        },
        "decision_policy": {
            "required_expected_return_pct": 10.0 if approved else None,
            "alternative_min_advantage_pct": 3.0 if approved else None,
            "block_buy_below_required_return": True if approved else None,
        },
    }


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
        data["expected_return"]["bear_pct"] = 30.0
        errors = validate_analysis(data)
        self.assertTrue(any("bear_pct <= base_pct" in error for error in errors))

    def test_non_finite_return_fails(self):
        data = valid_analysis()
        data["expected_return"]["base_pct"] = math.nan
        self.assertTrue(any("유한한 숫자" in error for error in validate_analysis(data)))

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
        data["position_limits"]["total_assets_pct"] = None
        self.assertTrue(validate_ips(data))

    def test_draft_ips_can_hold_null_decisions(self):
        self.assertEqual(validate_ips(valid_ips("DRAFT")), [])


if __name__ == "__main__":
    unittest.main()
