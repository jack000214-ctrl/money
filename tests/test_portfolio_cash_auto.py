"""portfolio_math: 총 자산 입력 시 현금·예수금은 (총 자산 - 보유 주식 평가액)으로 자동 계산된다."""

from __future__ import annotations

import unittest

from calculators.portfolio_math import _compute_allocation_status


def _holdings(*values: float) -> list[dict]:
    return [{"value_krw": v, "asset_type": "STOCK"} for v in values]


class TestCashAutoDerivation(unittest.TestCase):
    def test_cash_derived_from_total_minus_stock(self):
        policy = {"total_assets_krw": 10_000_000, "cash_krw": 123}
        status = _compute_allocation_status(_holdings(3_000_000, 2_000_000), policy)
        self.assertEqual(status["stock_value_krw"], 5_000_000)
        self.assertEqual(status["cash_krw"], 5_000_000)
        self.assertEqual(status["total_assets_krw"], 10_000_000)
        self.assertEqual(status["safe_pct"], 50.0)

    def test_total_below_stock_value_clamps_cash_to_zero(self):
        policy = {"total_assets_krw": 1_000_000}
        status = _compute_allocation_status(_holdings(5_000_000), policy)
        self.assertEqual(status["cash_krw"], 0)
        self.assertEqual(status["total_assets_krw"], 5_000_000)

    def test_total_zero_uses_manual_cash(self):
        policy = {"total_assets_krw": 0, "cash_krw": 2_000_000}
        status = _compute_allocation_status(_holdings(3_000_000), policy)
        self.assertEqual(status["cash_krw"], 2_000_000)
        self.assertEqual(status["total_assets_krw"], 5_000_000)


if __name__ == "__main__":
    unittest.main()
