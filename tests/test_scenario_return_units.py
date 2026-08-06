"""시나리오 수익률의 단위가 계약과 어긋나면 승패 판정이 조용히 왜곡된다.

`outcome_metric` 은 `TERMINAL_TOTAL_RETURN_ASSET_CURRENCY` 다 — 평가 기간 **말의
누적** 총수익률이다. 그런데 reverse_dcf 는 두 값을 함께 낸다.

    total_return_pct        주당가치 대비 누적 격차      +44.4%
    return_pct(옛 이름)      수렴기간 기준 연율          +20.2%

그리고 CLI 는 "'연율' 이 분석 계약의 scenarios[].return_pct 로 들어간다"고 안내했다.
그대로 따르면 24개월 누적 +44.4% 가 +20.2% 로 들어가고, `win_return_threshold_pct`
와의 비교가 통째로 어긋난다. 계산은 맞는데 배선이 틀린 종류의 오류라 눈에 띄지 않는다.

그래서 두 가지를 고정한다.

  * 계산기 출력의 이름이 단위를 말한다 (`annualized_return_pct`)
  * 인용한 계산과 대조해 **연율을 누적 자리에 넣은 것**을 PC 가 잡는다

PC 는 기대수익이 얼마여야 하는지 정하지 않는다. 모델이 앵커를 바꾸거나 확률을
다시 잡아 계산기와 다른 값을 쓰는 것은 판단이며 막지 않는다. 잡는 것은 **자기가
인용한 계산의 연율 칸을 누적 칸에 옮겨 적은 경우** 하나뿐이다.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from calculators.reverse_dcf import annualized_pct, equity_value_per_share


class TestCalculatorNamesItsUnits(unittest.TestCase):
    def test_scenario_output_carries_both_units_by_name(self):
        from calculators.reverse_dcf import scenario_outputs

        outputs = scenario_outputs(
            fcf=7.156e11,
            price=43700.0,
            growth={"bear": -3.0, "base": 5.0, "bull": 11.0},
            horizon_months=24,
            discount_rate=0.11,
            terminal_growth=0.02,
            years=10,
            net_debt=-2.378254112064e12,
            shares=195967468.5465995,
        )
        base = outputs["scenarios"]["base"]
        self.assertIn("total_return_pct", base)
        self.assertIn("annualized_return_pct", base)
        # 24개월이면 누적이 연율보다 크다. 두 값이 같으면 배선이 무너진 것이다.
        self.assertGreater(base["total_return_pct"], base["annualized_return_pct"])
        self.assertAlmostEqual(base["total_return_pct"], 44.44, places=1)
        self.assertAlmostEqual(base["annualized_return_pct"], 20.18, places=1)

    def test_the_ambiguous_old_name_is_gone(self):
        """`return_pct` 는 어느 단위인지 이름이 말해 주지 않아 오배선을 만들었다."""
        from calculators.reverse_dcf import scenario_outputs

        outputs = scenario_outputs(
            fcf=1e11,
            price=1000.0,
            growth={"bear": 0.0, "base": 5.0, "bull": 10.0},
            horizon_months=24,
            discount_rate=0.10,
            terminal_growth=0.02,
            years=10,
            net_debt=0.0,
            shares=1e8,
        )
        self.assertNotIn("return_pct", outputs["scenarios"]["base"])

    def test_twelve_month_horizon_makes_the_two_equal(self):
        value = equity_value_per_share(
            1e11, 0.05, discount_rate=0.10, terminal_growth=0.02,
            years=10, net_debt=0.0, shares=1e8,
        )
        total = (value / 1000.0 - 1) * 100
        self.assertAlmostEqual(total, annualized_pct(value, 1000.0, 12), places=6)


class TestMisusingAnnualisedAsTerminalIsCaught(unittest.TestCase):
    def test_annualised_value_in_expected_return_is_rejected(self):
        from cio.decision import _scenario_unit_mismatch

        calculation = {
            "model": "reverse_dcf",
            "inputs": {"price": 43700.0, "horizon_months": 24},
            "outputs": {
                "scenarios": {
                    "bear": {"total_return_pct": -4.77, "annualized_return_pct": -2.42},
                    "base": {"total_return_pct": 44.44, "annualized_return_pct": 20.18},
                    "bull": {"total_return_pct": 106.04, "annualized_return_pct": 43.54},
                }
            },
        }
        annualised = {"bear": -2.42, "base": 20.18, "bull": 43.54}
        terminal = {"bear": -4.77, "base": 44.44, "bull": 106.04}
        self.assertTrue(_scenario_unit_mismatch(calculation, annualised))
        self.assertFalse(_scenario_unit_mismatch(calculation, terminal))

    def test_a_different_judgement_is_not_flagged(self):
        """앵커를 바꾸거나 확률을 다시 잡는 것은 판단이다. 막지 않는다."""
        from cio.decision import _scenario_unit_mismatch

        calculation = {
            "model": "reverse_dcf",
            "inputs": {"price": 43700.0, "horizon_months": 24},
            "outputs": {
                "scenarios": {
                    "bear": {"total_return_pct": -4.77, "annualized_return_pct": -2.42},
                    "base": {"total_return_pct": 44.44, "annualized_return_pct": 20.18},
                    "bull": {"total_return_pct": 106.04, "annualized_return_pct": 43.54},
                }
            },
        }
        self.assertFalse(
            _scenario_unit_mismatch(
                calculation, {"bear": -23.7, "base": 14.2, "bull": 61.6}
            )
        )

    def test_a_twelve_month_calculation_is_never_flagged(self):
        """12개월이면 두 단위가 같아 구분할 수 없다. 구분되지 않으면 잡지 않는다."""
        from cio.decision import _scenario_unit_mismatch

        calculation = {
            "model": "reverse_dcf",
            "inputs": {"price": 1000.0, "horizon_months": 12},
            "outputs": {
                "scenarios": {
                    "bear": {"total_return_pct": -10.0, "annualized_return_pct": -10.0},
                    "base": {"total_return_pct": 20.0, "annualized_return_pct": 20.0},
                    "bull": {"total_return_pct": 50.0, "annualized_return_pct": 50.0},
                }
            },
        }
        self.assertFalse(
            _scenario_unit_mismatch(
                calculation, {"bear": -10.0, "base": 20.0, "bull": 50.0}
            )
        )


if __name__ == "__main__":
    unittest.main()
