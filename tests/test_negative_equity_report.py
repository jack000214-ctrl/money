"""주당가치가 음수(순부채 > 현금흐름 가치)여도 리포트 계산이 복소수로 깨지지 않는다.

하이트진로(000080): 순부채 8,264억 vs FCF 551억 → bear 주당가치 < 0.
음수에 소수 지수(12/horizon)를 쓰면 Python 은 complex 를 돌려주고
`round()` 에서 TypeError 가 났다. 주식 손실은 -100% 까지다.
"""
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_spec = importlib.util.spec_from_file_location(
    "build_decision_report", ROOT / "dashboard" / "scripts" / "build_decision_report.py")
assert _spec is not None and _spec.loader is not None
bdr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bdr)


class TestNegativeEquityValue(unittest.TestCase):
    def test_annualized_floors_at_minus_100(self):
        self.assertEqual(bdr.annualized(-5.0, 100.0, 24), -100.0)

    def test_hurdle_price_is_real(self):
        price = bdr.entry_price_for_hurdle(
            {"bear": -5.0, "base": 100.0, "bull": 200.0},
            {"bear": 0.3, "base": 0.4, "bull": 0.3}, 24, 10.0)
        self.assertIsInstance(price, float)
        self.assertGreater(price, 0)

    def test_sensitivity_grid_survives_negative_bear(self):
        calc_inputs = {"discount_rate_pct": 10.0, "terminal_growth_pct": 2.0,
                       "years": 10, "net_debt": 8.3e11, "shares": 6.96e7, "fcf": 5.5e10,
                       "scenario_growth_pct": {"bear": -5.0, "base": 3.0, "bull": 20.0}}
        grid = bdr.build_sensitivity(
            calc_inputs, {"bear": -5000.0, "base": 8000.0, "bull": 20000.0},
            {"bear": 1 / 3, "base": 1 / 3, "bull": 1 / 3}, 24, 10.0, 17000.0,
            5.5e10, "KRW")
        self.assertIn("axes", grid)


if __name__ == "__main__":
    unittest.main()
