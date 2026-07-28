"""시장 체제 판정 회귀 테스트.

발생한 버그: 낙폭 라벨의 조건 순서가 뒤집혀 있었다.
    flag = "조정" if dd <= -10 else ("약세장" if dd <= -20 else "")
-39% 는 첫 조건에서 True 가 되어 '조정'으로 찍혔다. 약세장 분기는 도달 불가였다.
"""

import unittest

from helpers import ROOT  # noqa: F401
from calculators.market_regime import classify, drawdown_label, percentile


class TestDrawdownLabel(unittest.TestCase):
    def test_bear_market_not_mislabeled_as_correction(self):
        """실제로 발생한 버그: -39% 가 '조정'으로 표시됐다."""
        self.assertEqual(drawdown_label(-39.0).strip(), "← 약세장")
        self.assertEqual(drawdown_label(-26.6).strip(), "← 약세장")

    def test_correction_range(self):
        self.assertEqual(drawdown_label(-12.0).strip(), "← 조정")
        self.assertEqual(drawdown_label(-10.0).strip(), "← 조정")

    def test_boundary_20_is_bear(self):
        self.assertEqual(drawdown_label(-20.0).strip(), "← 약세장")
        self.assertEqual(drawdown_label(-19.99).strip(), "← 조정")

    def test_normal_has_no_label(self):
        self.assertEqual(drawdown_label(-2.6), "")
        self.assertEqual(drawdown_label(0.0), "")


class TestClassify(unittest.TestCase):
    def test_calm_market_is_normal(self):
        regime, _, score, _ = classify(vix=14.0, worst_drawdown_pct=-2.0)
        self.assertEqual(regime, "NORMAL")
        self.assertEqual(score, 0)

    def test_large_drawdown_is_stress(self):
        regime, _, score, notes = classify(vix=18.58, worst_drawdown_pct=-39.0)
        self.assertEqual(regime, "STRESS")
        self.assertEqual(score, 3)
        self.assertTrue(any("약세장" in n for n in notes))

    def test_high_vix_plus_bear_is_crisis(self):
        regime, _, score, _ = classify(vix=45.0, worst_drawdown_pct=-35.0)
        self.assertEqual(regime, "CRISIS")
        self.assertGreaterEqual(score, 5)

    def test_missing_vix_still_classifies(self):
        """VIX 수집이 실패해도 낙폭만으로 판정할 수 있어야 한다."""
        regime, _, score, _ = classify(vix=None, worst_drawdown_pct=-25.0)
        self.assertEqual(regime, "STRESS")
        self.assertEqual(score, 3)

    def test_monotonic_in_severity(self):
        """상황이 나빠지면 점수가 줄어들면 안 된다."""
        scores = [classify(20.0, dd)[2] for dd in (0.0, -6.0, -12.0, -25.0)]
        self.assertEqual(scores, sorted(scores))


class TestPercentile(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(percentile([1, 2, 3, 4], 3), 50.0)

    def test_empty_returns_none(self):
        self.assertIsNone(percentile([], 3))


if __name__ == "__main__":
    unittest.main()
