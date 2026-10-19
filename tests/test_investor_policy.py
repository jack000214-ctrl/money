"""투자정책 계약과, PC 가 넘지 말아야 할 선.

정책은 사용자의 것이다. PC 는 그것을 packet 에 싣고 산술로 대조할 뿐,
비어 있는 한도를 채우거나 초과했을 때 무엇을 하라고 정하지 않는다. 여기 있는
테스트는 그 경계가 코드에서 무너졌는지 본다.
"""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from cio.paths import ROOT
from cio.policy import (
    FIELDS,
    deliberate_gaps,
    load_policy,
    policy_for_context,
    unset_fields,
    validate_policy,
)


def valid_policy():
    return {
        "schema_version": "cio-investor-policy/1.0.0",
        "policy_version": "2026-08-01",
        "base_currency": "KRW",
        "_role": "테스트 고정물",
        "objective": {"primary_goal": None, "target_annual_return_pct": None},
        "horizon": {
            "typical_holding_months": None,
            "liquidity_need_within_months": None,
        },
        "risk": {
            "stop_style": None,
            "max_loss_per_position_krw": None,
            "max_portfolio_drawdown_pct": None,
        },
        "concentration": {
            "max_single_position_pct_of_total": None,
            "max_sector_pct_of_total": None,
        },
        "liquidity": {"min_cash_krw": 0, "monthly_net_income_krw": None},
        "instruments": {
            "leverage_allowed": None,
            "derivative_etf_allowed": None,
            "notes": None,
        },
        "tax": {"account_type": None, "notes": None},
        "intentionally_unset": [],
        "denominator": {"included": ["주식"], "excluded": ["퇴직연금"], "note": ""},
        "default_alternatives": [
            {"id": "CASH_KRW", "label": "현금", "why": "기준선"}
        ],
    }


def portfolio_fixture():
    """`_portfolio_summary` 출력에서 정책이 읽는 필드만 추린 것."""
    return {
        "available": True,
        "total_assets_krw": 147_000_000.0,
        "cash_krw": 40_000_000.0,
        "existing_position": {
            "held": True,
            "value_at_review_price_krw": 37_050_000.0,
            "weight_at_review_price_pct": 25.18,
        },
        "proposed_trade": {
            "estimated_deployed_krw": 4_680_000.0,
            "cash_after_krw": 35_320_000.0,
            "post_trade_weight_total_assets_pct": 26.81,
        },
    }


def check_named(section, limit):
    return next(row for row in section["checks"] if row["limit"] == limit)


class TestPolicyContract(unittest.TestCase):
    def test_shipped_template_passes_its_own_contract(self):
        """템플릿이 계약과 함께 낡지 않게 한다.

        FIELDS 에 항목을 더하면 템플릿에도 키가 있어야 한다. 없으면 새 clone
        에서 만든 정책이 처음부터 계약 미달이 된다.
        """
        data, errors = load_policy(
            ROOT / "config" / "templates" / "investor-policy.json"
        )
        self.assertIsNotNone(data)
        self.assertEqual(errors, [])

    def test_local_policy_if_present_passes(self):
        """실제 정책은 gitignore 대상이라 없을 수 있다. 있으면 유효해야 한다.

        여기서 `skipTest` 를 쓰지 않는다 — 없는 것은 정상이고, 없을 때 이
        검사가 아무것도 보지 않는다는 사실은 아래 세 갈래 테스트가 대신
        보장한다. `load_policy` 의 분기는 실행 환경과 무관하게 덮인다.
        """
        _data, errors = load_policy()
        self.assertEqual(errors, [])

    def test_absent_policy_is_not_an_error(self):
        """없는 것은 오류가 아니다 — '아직 정하지 않음'과 '읽기 실패'는 다르다."""
        with tempfile.TemporaryDirectory() as tmp:
            data, errors = load_policy(Path(tmp) / "없는파일.json")
        self.assertIsNone(data)
        self.assertEqual(errors, [])

    def test_unreadable_policy_reports_an_error_rather_than_absence(self):
        """깨진 파일을 '정책 없음'으로 접으면 한도가 조용히 사라진다."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "investor-policy.json"
            path.write_text("{ 이건 JSON 이 아니다", encoding="utf-8")
            data, errors = load_policy(path)
        self.assertIsNone(data)
        self.assertTrue(errors)

    def test_present_but_invalid_policy_returns_both_data_and_errors(self):
        """읽히기는 했으나 계약 미달이면 데이터와 오류가 함께 나와야 한다."""
        broken = valid_policy()
        del broken["objective"]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "investor-policy.json"
            path.write_text(json.dumps(broken), encoding="utf-8")
            data, errors = load_policy(path)
        self.assertIsNotNone(data)
        self.assertTrue(errors)

    def test_valid_fixture_passes(self):
        self.assertEqual(validate_policy(valid_policy()), [])

    def test_missing_key_is_not_the_same_as_unset_value(self):
        """키를 빠뜨린 것과 한도를 정하지 않은 것은 다른 사건이다.

        구분하지 않으면 모델은 '정책이 없다'는 사실 자체를 관측할 수 없다.
        """
        data = valid_policy()
        del data["risk"]["max_loss_per_position_krw"]
        errors = validate_policy(data)
        self.assertTrue(any("키가 없다" in error for error in errors), errors)

        data = valid_policy()
        self.assertEqual(validate_policy(data), [])
        self.assertIn("risk.max_loss_per_position_krw", unset_fields(data))

    def test_unknown_field_is_rejected(self):
        data = valid_policy()
        data["risk"]["max_yolo_pct"] = 100
        self.assertTrue(
            any("알 수 없는 필드" in error for error in validate_policy(data))
        )

    def test_out_of_range_limits_are_rejected(self):
        data = valid_policy()
        data["concentration"]["max_single_position_pct_of_total"] = 140
        self.assertTrue(
            any("100을 넘을 수 없다" in e for e in validate_policy(data))
        )
        data = valid_policy()
        data["risk"]["max_loss_per_position_krw"] = -1
        self.assertTrue(
            any("음수일 수 없다" in e for e in validate_policy(data))
        )

    def test_dashboard_fields_are_accepted_but_optional(self):
        """대시보드 '투자 정책' 탭이 저장하는 항목은 계약 위반이 아니다."""
        data = valid_policy()
        data["total_assets_krw"] = 214_000_000
        data["cash_krw"] = 0
        data["allocation"] = {
            "safe_asset_target_pct": 30,
            "risk_asset_target_pct": 70,
        }
        data["rebalance"] = {"tolerance_pct": 5}
        self.assertEqual(validate_policy(data), [])
        self.assertEqual(validate_policy(valid_policy()), [])

    def test_dashboard_fields_are_still_validated(self):
        data = valid_policy()
        data["total_assets_krw"] = -1
        data["allocation"] = {"safe_asset_target_pct": 130, "max_yolo": 1}
        data["rebalance"] = "5"
        errors = validate_policy(data)
        self.assertTrue(any("total_assets_krw" in e for e in errors), errors)
        self.assertTrue(
            any("allocation.safe_asset_target_pct" in e for e in errors), errors
        )
        self.assertTrue(any("allocation: 알 수 없는 필드" in e for e in errors), errors)
        self.assertTrue(any("rebalance: 객체가 필요하다" in e for e in errors), errors)

    def test_missing_file_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            data, errors = load_policy(Path(tmp) / "nope.json")
        self.assertIsNone(data)
        self.assertEqual(errors, [])


class TestPacketExposesTheGap(unittest.TestCase):
    def test_absent_policy_says_so_without_inventing_limits(self):
        section = policy_for_context(None, portfolio_fixture())
        self.assertFalse(section["available"])
        self.assertIn("NO_INVESTOR_POLICY", section["_note"])
        self.assertNotIn("checks", section)

    def test_unset_limit_is_reported_as_unset_not_as_unlimited(self):
        section = policy_for_context(valid_policy(), portfolio_fixture())
        row = check_named(
            section, "concentration.max_single_position_pct_of_total"
        )
        self.assertEqual(row["state"], "LIMIT_UNSET")
        self.assertIsNone(row["limit_value"])
        # 한도가 없다고 breach 를 false 로 접으면 무제한과 구분되지 않는다.
        self.assertIsNone(row["breach_now"])
        self.assertIsNone(row["breach_after_proposed_trade"])
        self.assertIn(
            "concentration.max_single_position_pct_of_total", section["unset"]
        )

    def test_every_registered_field_is_reachable_from_unset(self):
        section = policy_for_context(valid_policy(), portfolio_fixture())
        expected = {path for path in FIELDS if path != "liquidity.min_cash_krw"}
        self.assertEqual(set(section["unset"]), expected)


class TestDeliberateGapIsNotAGap(unittest.TestCase):
    """'아직 안 정했다'와 '두지 않기로 했다'를 구분한다.

    구분하지 않으면 가격 손절을 쓰지 않는 사용자에게 시스템이 영원히
    UNDETERMINED 를 돌려준다 — 이미 답한 질문을 계속 되묻는 셈이다.
    """

    def thesis_policy(self):
        data = valid_policy()
        data["risk"]["stop_style"] = "THESIS"
        data["intentionally_unset"] = [
            {
                "field": "risk.max_loss_per_position_krw",
                "why": "논지가 깨질 때 팔지, 가격이 빠졌다고 팔지 않는다",
            }
        ]
        return data

    def test_deliberate_gap_leaves_unset_list(self):
        data = self.thesis_policy()
        self.assertEqual(validate_policy(data), [])
        self.assertNotIn("risk.max_loss_per_position_krw", unset_fields(data))
        self.assertIn(
            "risk.max_loss_per_position_krw", deliberate_gaps(data)
        )

    def test_check_says_by_choice_not_unset(self):
        section = policy_for_context(self.thesis_policy(), portfolio_fixture())
        row = check_named(section, "risk.max_loss_per_position_krw")
        self.assertEqual(row["state"], "NO_LIMIT_BY_CHOICE")
        self.assertIn("논지", row["reason"])

    def test_cannot_claim_deliberate_while_holding_a_value(self):
        data = self.thesis_policy()
        data["risk"]["max_loss_per_position_krw"] = 5_000_000
        self.assertTrue(
            any("값이 들어 있다" in e for e in validate_policy(data))
        )

    def test_deliberate_gap_needs_a_reason(self):
        data = self.thesis_policy()
        data["intentionally_unset"][0]["why"] = "  "
        self.assertTrue(any("why" in e for e in validate_policy(data)))

    def test_unknown_path_is_rejected(self):
        data = self.thesis_policy()
        data["intentionally_unset"][0]["field"] = "risk.vibes"
        self.assertTrue(
            any("알 수 없는 경로" in e for e in validate_policy(data))
        )

    def test_stop_style_enum_is_enforced(self):
        data = valid_policy()
        data["risk"]["stop_style"] = "YOLO"
        self.assertTrue(
            any("stop_style" in e for e in validate_policy(data))
        )


class TestArithmeticOnly(unittest.TestCase):
    def test_cap_breach_is_a_comparison_not_a_verdict(self):
        data = valid_policy()
        data["concentration"]["max_single_position_pct_of_total"] = 25.0
        section = policy_for_context(data, portfolio_fixture())
        row = check_named(
            section, "concentration.max_single_position_pct_of_total"
        )
        self.assertEqual(row["state"], "OK")
        self.assertTrue(row["breach_now"])
        self.assertTrue(row["breach_after_proposed_trade"])
        # 무엇을 하라는 말은 어디에도 없어야 한다.
        blob = json.dumps(section, ensure_ascii=False)
        for word in ("줄여", "매도하라", "팔아", "사라", "권장"):
            self.assertNotIn(word, blob)

    def test_floor_breach_uses_the_opposite_inequality(self):
        data = valid_policy()
        data["liquidity"]["min_cash_krw"] = 38_000_000
        section = policy_for_context(data, portfolio_fixture())
        row = check_named(section, "liquidity.min_cash_krw")
        self.assertEqual(row["bound"], "MIN")
        self.assertFalse(row["breach_now"])
        self.assertTrue(row["breach_after_proposed_trade"])

    def test_max_loss_converts_to_a_drawdown_percentage(self):
        data = valid_policy()
        data["risk"]["max_loss_per_position_krw"] = 7_410_000
        section = policy_for_context(data, portfolio_fixture())
        row = check_named(section, "risk.max_loss_per_position_krw")
        # 741만 / 3,705만 = 20%
        self.assertAlmostEqual(row["max_drawdown_now_pct"], 20.0, places=2)
        # 매수 후 노출 4,173만에서는 같은 손실 한도가 더 얕은 하락에 걸린다.
        self.assertAlmostEqual(
            row["max_drawdown_after_proposed_trade_pct"], 17.76, places=2
        )

    def test_sector_limit_refuses_to_answer_without_look_through(self):
        """ETF 안의 같은 섹터 노출을 빼고 낸 숫자는 실제보다 작다.

        틀린 여유를 주느니 대조하지 않는다고 말한다.
        """
        data = valid_policy()
        data["concentration"]["max_sector_pct_of_total"] = 30.0
        section = policy_for_context(data, portfolio_fixture())
        row = check_named(section, "concentration.max_sector_pct_of_total")
        self.assertEqual(row["state"], "INPUT_UNAVAILABLE")
        self.assertIn("look-through", row["reason"])
        self.assertIsNone(row["breach_now"])

    def test_missing_exposure_does_not_collapse_to_zero(self):
        """환율 누락 등으로 평가액을 못 내면 0 이 아니라 모른다로 남는다."""
        data = valid_policy()
        data["risk"]["max_loss_per_position_krw"] = 1_000_000
        portfolio = copy.deepcopy(portfolio_fixture())
        portfolio["existing_position"]["value_at_review_price_krw"] = None
        section = policy_for_context(data, portfolio)
        row = check_named(section, "risk.max_loss_per_position_krw")
        self.assertIsNone(row["exposure_after_proposed_trade_krw"])
        self.assertIsNone(row["max_drawdown_after_proposed_trade_pct"])

    def test_concentration_and_drawdown_limits_are_related(self):
        """두 한도가 서로 팽팽한지를 산술로 보여준다.

        단일 종목 50%에 전체 낙폭 한도 35%면, 그 종목이 -70% 갈 때 낙폭 예산이
        한 종목에 통째로 걸린다.
        """
        data = valid_policy()
        data["concentration"]["max_single_position_pct_of_total"] = 50
        data["risk"]["max_portfolio_drawdown_pct"] = 35
        section = policy_for_context(data, portfolio_fixture())
        row = check_named(section, "risk.max_portfolio_drawdown_pct")
        self.assertAlmostEqual(
            row["position_loss_consuming_whole_budget_pct"], 70.0, places=2
        )
        self.assertNotIn("breach_now", row)

    def test_relation_row_is_absent_when_either_limit_is_missing(self):
        data = valid_policy()
        data["concentration"]["max_single_position_pct_of_total"] = 50
        section = policy_for_context(data, portfolio_fixture())
        self.assertFalse(
            any(
                "position_loss_consuming_whole_budget_pct" in row
                for row in section["checks"]
            )
        )

    def test_broken_policy_file_is_surfaced_not_swallowed(self):
        section = policy_for_context(
            valid_policy(), portfolio_fixture(), errors=["base_currency: ..."]
        )
        self.assertFalse(section["available"])
        self.assertIn("contract_errors", section)


if __name__ == "__main__":
    unittest.main()
