"""매매 검증 회귀 테스트.

핵심 성질: **실현손익이 전부 플러스여도 매매가 손해일 수 있다.**
파마리서치는 매도 7건 전부 실현이익(+2,126,033원)이었지만
한 번도 팔지 않았을 경우와 비교하면 -2,412,500원이었다.
이동평균 평단이 낮아지면 어떤 가격에 팔아도 '이익'으로 찍히기 때문이다.
이 성질이 깨지면 도구가 착시를 그대로 재생산한다.
"""

import unittest

from helpers import ROOT, write_csv  # noqa: F401
from trade_review import load_trades, run


class TestMovingAverageCost(unittest.TestCase):
    def test_average_cost_after_buys(self):
        trades = load_trades(write_csv("""
date,side,quantity,price_krw
2026-01-01,BUY,10,1000
2026-01-02,BUY,10,2000
"""))
        r = run(trades, price_now=1500)
        self.assertEqual(r["qty"], 20)
        self.assertAlmostEqual(r["avg"], 1500.0)

    def test_sell_uses_average_not_fifo(self):
        trades = load_trades(write_csv("""
date,side,quantity,price_krw
2026-01-01,BUY,10,1000
2026-01-02,BUY,10,2000
2026-01-03,SELL,10,1800
"""))
        r = run(trades, price_now=1800)
        # 평단 1500 에서 1800 매도 → (1800-1500)*10 = 3000
        self.assertAlmostEqual(r["realized"], 3000.0)
        self.assertAlmostEqual(r["avg"], 1500.0, msg="매도해도 평단은 변하지 않는다")


class TestCounterfactual(unittest.TestCase):
    def test_all_gains_but_trading_lost_money(self):
        """실현손익이 플러스인데 매매 순효과는 마이너스인 경우."""
        trades = load_trades(write_csv("""
date,side,quantity,price_krw
2026-01-01,BUY,100,1000
2026-02-01,BUY,100,600
2026-03-01,SELL,100,900
"""))
        r = run(trades, price_now=1500)
        self.assertGreater(r["realized"], 0, "매도는 실현이익이었다")
        self.assertLess(r["trading_effect"], 0, "그러나 안 팔았으면 더 벌었다")
        # 200주 보유 시 300,000 - 160,000 = +140,000
        self.assertAlmostEqual(r["hold_pnl"], 140000.0)
        # 실제: 실현 (900-800)*100=10,000 + 평가 (1500-800)*100=70,000 = 80,000
        self.assertAlmostEqual(r["total"], 80000.0)
        self.assertAlmostEqual(r["trading_effect"], -60000.0)

    def test_selling_high_helps(self):
        """고점에 판 경우에는 매매 순효과가 플러스여야 한다."""
        trades = load_trades(write_csv("""
date,side,quantity,price_krw
2026-01-01,BUY,100,1000
2026-02-01,SELL,50,2000
"""))
        r = run(trades, price_now=1000)
        self.assertGreater(r["trading_effect"], 0)

    def test_hold_quantity_is_total_bought(self):
        trades = load_trades(write_csv("""
date,side,quantity,price_krw
2026-01-01,BUY,100,1000
2026-02-01,SELL,40,1200
"""))
        r = run(trades, price_now=1000)
        self.assertEqual(r["hold_qty"], 100)
        self.assertEqual(r["qty"], 60)


class TestInputValidation(unittest.TestCase):
    def test_oversell_is_rejected(self):
        trades = load_trades(write_csv("""
date,side,quantity,price_krw
2026-01-01,BUY,10,1000
2026-01-02,SELL,20,1200
"""))
        with self.assertRaises(SystemExit):
            run(trades, price_now=1000)

    def test_trades_sorted_by_date(self):
        trades = load_trades(write_csv("""
date,side,quantity,price_krw
2026-03-01,BUY,10,3000
2026-01-01,BUY,10,1000
"""))
        self.assertEqual([t["date"] for t in trades],
                         ["2026-01-01", "2026-03-01"])


if __name__ == "__main__":
    unittest.main()
