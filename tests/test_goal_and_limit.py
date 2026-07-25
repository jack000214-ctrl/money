"""목표 경로·비중 한도 회귀 테스트.

한도를 '관행 10%' 로 두지 않고 목표에서 역산하기로 했으므로,
그 계산이 맞는지가 곧 한도의 근거가 된다.
"""

import unittest

from helpers import ROOT  # noqa: F401
from goal_path import future_value, required_rate, years_to_target
from position_limit import main as _limit_main  # noqa: F401  (임포트 가능 여부 확인)

EOK = 100_000_000


class TestFutureValue(unittest.TestCase):
    def test_zero_rate_is_simple_sum(self):
        fv = future_value(principal=1_000_000, monthly_saving=100_000,
                          annual_rate=0.0, years=1)
        self.assertAlmostEqual(fv, 1_000_000 + 100_000 * 12)

    def test_no_saving_is_compound_only(self):
        fv = future_value(principal=1_000_000, monthly_saving=0,
                          annual_rate=0.12, years=1)
        self.assertAlmostEqual(fv, 1_000_000 * (1.01 ** 12), places=2)

    def test_monotonic_in_rate(self):
        vals = [future_value(1e8, 2e6, r, 20) for r in (0.03, 0.05, 0.07, 0.09)]
        self.assertEqual(vals, sorted(vals))

    def test_monotonic_in_years(self):
        vals = [future_value(1e8, 2e6, 0.07, y) for y in (5, 10, 20, 30)]
        self.assertEqual(vals, sorted(vals))


class TestYearsToTarget(unittest.TestCase):
    def test_reaches_target_at_returned_time(self):
        yrs = years_to_target(304_050_636, 2_000_000, 0.089, 5_000_000_000)
        self.assertIsNotNone(yrs)
        fv = future_value(304_050_636, 2_000_000, 0.089, yrs)
        self.assertGreaterEqual(fv, 5_000_000_000)

    def test_just_before_target_is_short(self):
        yrs = years_to_target(304_050_636, 2_000_000, 0.089, 5_000_000_000)
        fv_before = future_value(304_050_636, 2_000_000, 0.089, yrs - 1 / 12)
        self.assertLess(fv_before, 5_000_000_000)

    def test_unreachable_returns_none(self):
        self.assertIsNone(years_to_target(1_000, 0, 0.0, 5_000_000_000))


class TestRequiredRate(unittest.TestCase):
    def test_solves_consistently(self):
        r = required_rate(304_050_636, 2_000_000, 5_000_000_000, 20)
        fv = future_value(304_050_636, 2_000_000, r, 20)
        self.assertAlmostEqual(fv / 5_000_000_000, 1.0, places=2)

    def test_longer_horizon_needs_lower_rate(self):
        r20 = required_rate(304_050_636, 2_000_000, 5_000_000_000, 20)
        r25 = required_rate(304_050_636, 2_000_000, 5_000_000_000, 25)
        self.assertLess(r25, r20)


class TestConcentrationDelay(unittest.TestCase):
    """비중이 클수록 목표 지연이 커져야 한다 — 한도 논리의 근거."""

    def _delay(self, weight_pct, drawdown=0.8):
        assets, target = 304_050_636, 5_000_000_000
        monthly, rate = 2_000_000, 0.089
        base = years_to_target(assets, monthly, rate, target)
        after = assets - assets * (weight_pct / 100) * drawdown
        return years_to_target(after, monthly, rate, target) - base

    def test_delay_increases_with_weight(self):
        delays = [self._delay(w) for w in (5, 10, 20, 35)]
        self.assertEqual(delays, sorted(delays))
        self.assertGreater(delays[0], 0)

    def test_delay_increases_with_drawdown(self):
        self.assertLess(self._delay(20, 0.5), self._delay(20, 0.8))


if __name__ == "__main__":
    unittest.main()
