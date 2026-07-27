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


class TestRealTradeFiles(unittest.TestCase):
    """실제 knowledge/portfolio/trades/ 파일을 읽는다.

    이 클래스가 없어서 도구가 전 종목에서 죽어 있는 걸 아무도 못 잡았다.
    픽스처만 검사하면 '픽스처를 읽는 능력'만 검증된다. 회귀 테스트는
    사용자가 실제로 넣는 파일을 읽어야 한다.
    """

    def _files(self):
        d = ROOT / "knowledge" / "portfolio" / "trades"
        # _closed.csv 는 다종목 혼합이라 단일 종목 도구의 입력이 아니다
        return sorted(p for p in d.glob("*.csv") if p.name != "_closed.csv")

    def test_every_trade_file_parses(self):
        for path in self._files():
            with self.subTest(path.name):
                trades = load_trades(str(path))
                self.assertGreater(
                    len(trades), 0,
                    f"{path.name}: 0건으로 읽혔다. settle_date 컬럼을 못 읽으면 "
                    "전 행이 무음 skip 된다")

    def test_every_trade_file_runs(self):
        """파싱만 되고 run() 에서 죽으면 의미가 없다."""
        for path in self._files():
            with self.subTest(path.name):
                r = run(load_trades(str(path)), price_now=100000)
                self.assertGreaterEqual(r["qty"], 0)

    def test_tsla_receive_rows_are_loaded(self):
        """TSLA 는 무상입고(RECEIVE) 33건을 포함한다."""
        trades = load_trades(
            str(ROOT / "knowledge" / "portfolio" / "trades" / "TSLA.csv"))
        sides = [t["side"] for t in trades]
        self.assertIn("RECEIVE", sides)
        self.assertEqual(len(trades), 36)

    def test_tsla_quantity_matches_holdings(self):
        """복원한 보유수량이 holdings.csv 와 일치해야 한다."""
        trades = load_trades(
            str(ROOT / "knowledge" / "portfolio" / "trades" / "TSLA.csv"))
        r = run(trades, price_now=462925)
        self.assertAlmostEqual(r["qty"], 10.004156, places=6)


class TestReceiveHandling(unittest.TestCase):
    """무상입고는 매매 결정이 아니다.

    이 도구의 질문은 '매매가 안 하느니만 못했나'다. 이벤트로 받은 주식을
    매수 통계에 넣으면 내가 내리지 않은 결정이 매매 성적에 섞인다.
    """

    SAMPLE = """
settle_date,side,quantity,price_krw
2026-01-01,BUY,10,1000
2026-01-02,RECEIVE,2,1500
"""

    def test_receive_excluded_from_buy_stats(self):
        r = run(load_trades(write_csv(self.SAMPLE)), price_now=2000)
        self.assertEqual(r["bought_qty"], 10, "RECEIVE 는 매수 통계에서 빠진다")
        self.assertEqual(r["bought_amt"], 10000)

    def test_receive_included_in_position_and_cost(self):
        r = run(load_trades(write_csv(self.SAMPLE)), price_now=2000)
        self.assertEqual(r["qty"], 12, "받은 주식도 보유수량이다")
        self.assertEqual(r["cost"], 10000 + 3000, "기재가로 원가에 산입한다")

    def test_receive_counted_in_counterfactual_baseline(self):
        """'한 번도 팔지 않았다면' 기준에는 받은 주식도 들어간다."""
        r = run(load_trades(write_csv(self.SAMPLE)), price_now=2000)
        self.assertEqual(r["hold_qty"], 12)
        self.assertEqual(r["hold_pnl"], 12 * 2000 - 13000)

    def test_receive_only_file_does_not_crash(self):
        """매수가 0건이면 평균단가 분모가 0이 된다 (ZeroDivisionError 재현)."""
        r = run(load_trades(write_csv("""
settle_date,side,quantity,price_krw
2026-01-01,RECEIVE,2,1500
""")), price_now=2000)
        self.assertEqual(r["bought_qty"], 0)
        self.assertIsNone(r["bought_avg"], "매수가 없으면 평균단가는 None 이다")


class TestSchemaGuards(unittest.TestCase):
    """무음 skip 은 크래시보다 나쁘다. 빈 리포트가 그럴듯하게 나오기 때문이다."""

    def test_settle_date_column_is_accepted(self):
        trades = load_trades(write_csv("""
settle_date,side,quantity,price_krw
2026-01-01,BUY,10,1000
"""))
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["date"], "2026-01-01")

    def test_missing_date_column_fails_loudly(self):
        with self.assertRaises(SystemExit):
            load_trades(write_csv("""
trade_day,side,quantity,price_krw
2026-01-01,BUY,10,1000
"""))

    def test_unknown_side_fails(self):
        with self.assertRaises(SystemExit):
            load_trades(write_csv("""
settle_date,side,quantity,price_krw
2026-01-01,TRANSFER,10,1000
"""))

    def test_multi_ticker_file_is_rejected(self):
        """_closed.csv 처럼 여러 종목이 섞인 파일을 하나의 평단으로 합치면 안 된다."""
        with self.assertRaises(SystemExit):
            load_trades(write_csv("""
settle_date,name,side,quantity,price_krw
2026-01-01,삼성전자,BUY,10,1000
2026-01-02,LG전자,BUY,10,2000
"""))

    def test_closed_csv_is_rejected(self):
        with self.assertRaises(SystemExit):
            load_trades(
                str(ROOT / "knowledge" / "portfolio" / "trades" / "_closed.csv"))


if __name__ == "__main__":
    unittest.main()
