"""배당 정산 회귀 테스트.

발생한 오판: 파마리서치가 주당 3,700원을 배당한다는 사실을 보고
"반사실 계산에 배당이 빠졌다"고 결론냈다. 그러나 배당락일이 2025-12-29 이고
첫 매수는 2026-02-05 이라 **한 푼도 받지 않았다.**

종목의 배당률을 보유자의 수령액으로 착각하면 성과가 과대평가된다.
"""

import unittest

from helpers import ROOT, write_csv  # noqa: F401
from dividend_ledger import shares_on
from trade_review import load_trades

PHARMA = write_csv("""
date,side,quantity,price_krw
2026-02-05,BUY,5,372500
2026-02-06,BUY,10,317000
""")

IBM = write_csv("""
date,side,quantity,price_krw
2026-01-09,BUY,1,437082
2026-02-18,BUY,2,373984
2026-02-24,BUY,2,326638
2026-05-22,SELL,4,393420
2026-07-14,BUY,19,340000
""")


class TestSharesOnExDate(unittest.TestCase):
    def test_zero_before_first_purchase(self):
        """파마리서치 2025-12-29 배당락일에는 보유가 없었다."""
        self.assertEqual(shares_on(load_trades(PHARMA), "2025-12-29"), 0)

    def test_ex_date_same_day_purchase_excluded(self):
        """배당락일 당일 매수는 배당을 받지 못한다."""
        self.assertEqual(shares_on(load_trades(PHARMA), "2026-02-05"), 0)

    def test_day_after_purchase_counts(self):
        self.assertEqual(shares_on(load_trades(PHARMA), "2026-02-06"), 5)

    def test_ibm_holdings_at_each_ex_date(self):
        trades = load_trades(IBM)
        self.assertEqual(shares_on(trades, "2026-02-10"), 1)
        self.assertEqual(shares_on(trades, "2026-05-08"), 5)

    def test_sell_reduces_later_entitlement(self):
        trades = load_trades(IBM)
        self.assertEqual(shares_on(trades, "2026-06-01"), 1, "5주 중 4주 매도 후")

    def test_after_all_trades(self):
        self.assertEqual(shares_on(load_trades(IBM), "2026-08-01"), 20)


class TestSettlement(unittest.TestCase):
    def test_pharma_receives_nothing(self):
        trades = load_trades(PHARMA)
        divs = [("2023-12-27", 950.0), ("2024-12-27", 1100.0), ("2025-12-29", 3700.0)]
        total = sum(shares_on(trades, d) * amt for d, amt in divs)
        self.assertEqual(total, 0.0, "배당락일 전부 첫 매수 이전이다")

    def test_ibm_total(self):
        trades = load_trades(IBM)
        divs = [("2026-02-10", 1.68), ("2026-05-08", 1.69)]
        total = sum(shares_on(trades, d) * amt for d, amt in divs)
        self.assertAlmostEqual(total, 1.68 * 1 + 1.69 * 5, places=2)


if __name__ == "__main__":
    unittest.main()
