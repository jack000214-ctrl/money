"""ETF 관통 계산 회귀 테스트.

발생한 버그
  1. 섹터 노출이 ETF 구성종목만 집계하고 **직접 보유 종목을 빠뜨렸다.**
     반도체가 8.43% 로 나왔는데 실제는 41.76% 였다.
  2. knowledge/ 의 CSV 는 출처·기준일을 '#' 주석으로 갖는데 파서가 이를 헤더로 읽었다.
"""

import unittest

from helpers import ROOT, write_csv  # noqa: F401
from calculators.portfolio import analyze, load_constituents, load_holdings

LIMITS = {"single": 10.0, "top10": 50.0, "sector": 30.0,
          "currency": 70.0, "unclassified": 20.0}


class TestCsvParsing(unittest.TestCase):
    def test_comment_lines_are_skipped(self):
        path = write_csv("""
# 보유 내역 / 기준일 2026-07-25
# 현금 2억은 대기자금
asset_id,name,market,asset_type,value_krw
AAA,알파,KR,STOCK,1000000
""")
        rows = load_holdings(path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["asset_id"], "AAA")

    def test_non_finite_value_is_rejected(self):
        path = write_csv("""
asset_id,name,market,asset_type,value_krw
AAA,알파,KR,STOCK,nan
""")
        with self.assertRaises(SystemExit):
            load_holdings(path)

    def test_duplicate_asset_id_is_rejected(self):
        path = write_csv("""
asset_id,name,market,asset_type,value_krw
AAA,알파,KR,STOCK,100
AAA,알파중복,KR,STOCK,200
""")
        with self.assertRaises(SystemExit):
            load_holdings(path)


class TestLookThrough(unittest.TestCase):
    def setUp(self):
        self.holdings = load_holdings(write_csv("""
asset_id,name,market,asset_type,value_krw,fx_exposure,sector
AAA,알파,KR,STOCK,5000000,,반도체
ETF1,지수펀드,KR,ETF,5000000,,
"""))
        self.cons = load_constituents([write_csv("""
etf_id,holding_id,holding_name,weight_pct,market,sector
ETF1,AAA,알파,20.0,KR,반도체
ETF1,BBB,베타,30.0,KR,자동차
""")])

    def test_effective_weight_adds_direct_and_etf_paths(self):
        r = analyze(self.holdings, self.cons, LIMITS)
        aaa = next(e for e in r["exposures"] if e["asset_id"] == "AAA")
        # 직접 5,000,000 + ETF 경유 5,000,000*0.20 = 1,000,000 → 6,000,000 / 10,000,000
        self.assertAlmostEqual(aaa["effective_pct"], 60.0, places=2)
        self.assertAlmostEqual(aaa["nominal_pct"], 50.0, places=2)
        self.assertAlmostEqual(aaa["hidden_pct"], 10.0, places=2)
        self.assertEqual(aaa["path_count"], 2)

    def test_sector_includes_directly_held_positions(self):
        """버그: 직접 보유 종목의 섹터가 집계에서 빠졌다."""
        r = analyze(self.holdings, self.cons, LIMITS)
        # 반도체 = 직접 5,000,000 + ETF 경유 1,000,000 = 60%
        self.assertAlmostEqual(r["sector_exposure"]["반도체"], 60.0, places=2)

    def test_unclassified_remainder_tracked(self):
        r = analyze(self.holdings, self.cons, LIMITS)
        # ETF 커버리지 50% → 나머지 50% × 비중 50% = 25%
        self.assertAlmostEqual(r["unclassified_pct"], 25.0, places=2)
        remainder = next(
            e for e in r["exposures"]
            if e["asset_id"].startswith("UNCLASSIFIED::")
        )
        self.assertAlmostEqual(remainder["equity_sleeve_pct"], 25.0)
        self.assertAlmostEqual(remainder["risk_assets_pct"], 25.0)

    def test_top10_excludes_unclassified_rows(self):
        r = analyze(self.holdings, self.cons, LIMITS)
        ids = [e["asset_id"] for e in r["exposures"]]
        self.assertTrue(any(i.startswith("UNCLASSIFIED::") for i in ids))
        # 상위 10 합계에 미분류가 섞이면 100% 를 넘길 수 있다
        self.assertLessEqual(r["top10_effective_pct"], 100.0)

    def test_currency_inferred_from_market(self):
        r = analyze(self.holdings, self.cons, LIMITS)
        self.assertAlmostEqual(r["currency_exposure"]["KRW"], 100.0, places=2)

    def test_weights_sum_to_100(self):
        r = analyze(self.holdings, self.cons, LIMITS)
        total = sum(e["effective_pct"] for e in r["exposures"])
        self.assertAlmostEqual(total, 100.0, places=1)

    def test_all_three_denominators_are_explicit(self):
        r = analyze(self.holdings, self.cons, LIMITS)
        aaa = next(e for e in r["exposures"] if e["asset_id"] == "AAA")
        self.assertEqual(r["denominators_krw"]["total_assets"], 10_000_000)
        self.assertEqual(r["denominators_krw"]["equity_sleeve"], 10_000_000)
        self.assertAlmostEqual(aaa["total_assets_pct"], 60.0)
        self.assertAlmostEqual(aaa["equity_sleeve_pct"], 60.0)
        self.assertAlmostEqual(aaa["risk_assets_pct"], 60.0)


class TestWarnings(unittest.TestCase):
    def test_single_name_limit_breach_detected(self):
        holdings = load_holdings(write_csv("""
asset_id,name,market,asset_type,value_krw
BIG,대형,KR,STOCK,9000000
SMALL,소형,KR,STOCK,1000000
"""))
        r = analyze(holdings, {}, LIMITS)
        codes = [w["code"] for w in r["warnings"]]
        self.assertIn("SINGLE_NAME_CONCENTRATION", codes)

    def test_cash_is_not_a_single_name_or_top10_position(self):
        holdings = load_holdings(write_csv("""
asset_id,name,market,asset_type,value_krw
CASH,예수금,KR,CASH,9000000
AAA,알파,KR,STOCK,1000000
"""))
        r = analyze(holdings, {}, LIMITS)
        single_ids = [
            w.get("asset_id") for w in r["warnings"]
            if w["code"] == "SINGLE_NAME_CONCENTRATION"
        ]
        self.assertNotIn("CASH", single_ids)
        self.assertAlmostEqual(r["top10_effective_pct"], 10.0)
        cash = next(e for e in r["exposures"] if e["asset_id"] == "CASH")
        self.assertIsNone(cash["equity_sleeve_pct"])
        self.assertIsNone(cash["risk_assets_pct"])


if __name__ == "__main__":
    unittest.main()
