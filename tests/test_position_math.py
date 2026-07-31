"""포지션 산술과, 그 계산기가 넘지 말아야 할 경계.

이 파일의 절반은 값이 맞는지 보는 테스트이고, 나머지 절반은 **계산기가
판정을 내지 않는지** 보는 테스트다. 후자가 없으면 편의 필드가 하나씩 늘다가
어느 순간 계산기가 "비중이 과하다"를 말하게 되고, 모델은 그것을 근거로
받아들인다. 그 시점에 판단의 주체가 조용히 바뀐다.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from calculators import position as position_module
from calculators.position import PositionError, at_price, read_ledger, weights

LEDGER_KR = """# 주석은 무시된다
settle_date,side,quantity,price_krw,fee_krw,tax_krw,note
2026-02-09,BUY,10,300000,0,0,
2026-03-09,BUY,10,200000,0,0,
2026-04-09,SELL,5,400000,0,500,
"""

LEDGER_US = """settle_date,side,quantity,price_krw,price_usd,fx_rate,fee_krw,note
2025-01-02,BUY,10,140000,100,1400,14000,
"""

HOLDINGS = """# 기준일 2026-07-27
asset_id,name,market,asset_type,risk_class,value_krw,fx_exposure,sector,quantity,pnl_krw,pnl_pct,note
AAA,테스트주,KR,STOCK,STOCK,5000000,,,15,500000,11.1,
BBB,테스트ETF,KR,ETF,BROAD_ETF,3000000,,,10,0,0,
USD1,테스트미국주,US,STOCK,STOCK,1400000,,,10,0,0,
CASH_KRW,원화,KR,CASH,CASH,2000000,KRW,,,,,
"""


class TempPortfolio(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        trades = root / "portfolio" / "trades"
        trades.mkdir(parents=True)
        (trades / "AAA.csv").write_text(LEDGER_KR, encoding="utf-8")
        (trades / "USD1.csv").write_text(LEDGER_US, encoding="utf-8")
        (root / "portfolio" / "holdings.csv").write_text(
            HOLDINGS, encoding="utf-8"
        )
        for name, value in (
            ("TRADES_DIR", trades),
            ("HOLDINGS", root / "portfolio" / "holdings.csv"),
            ("KNOWLEDGE_DIR", root),
        ):
            original = getattr(position_module, name)
            setattr(position_module, name, value)
            self.addCleanup(setattr, position_module, name, original)


class TestLedgerMath(TempPortfolio):
    def test_moving_average_survives_a_sale(self):
        """매도는 그 시점 평단으로 원가를 덜어낼 뿐 평단을 바꾸지 않는다."""
        ledger = read_ledger("AAA", "price_krw")
        # 10주@30만 + 10주@20만 = 20주, 평단 25만
        # 5주 매도 후에도 평단은 25만이고 수량만 15주다
        self.assertEqual(ledger["quantity"], 15.0)
        self.assertEqual(ledger["average_cost"], 250000.0)
        self.assertEqual(ledger["cost_basis"], 3750000.0)

    def test_realized_pnl_is_net_of_fees_and_tax(self):
        ledger = read_ledger("AAA", "price_krw")
        # 5주 × (40만 − 25만) − 세금 500
        self.assertEqual(ledger["realized_pnl"], 749500.0)
        self.assertEqual(ledger["taxes_paid_krw"], 500.0)

    def test_krw_fee_is_converted_before_touching_a_usd_cost_basis(self):
        """수수료는 원장에 항상 원화로 적힌다.

        달러 원가에 원화 수수료를 그대로 더하면 평단이 조용히 부풀어 오른다.
        실제로 TSLA 평단이 325달러 대신 800달러로 나왔던 버그다.
        """
        ledger = read_ledger("USD1", "price_usd")
        # 10주 × $100 + 14,000원/1400 = $1,010 → 평단 $101
        self.assertEqual(ledger["average_cost"], 101.0)
        # 실제로 부과된 수수료는 원화 그대로 보고한다
        self.assertEqual(ledger["fees_paid_krw"], 14000.0)

    def test_missing_fx_rate_stops_instead_of_guessing(self):
        path = position_module.TRADES_DIR / "USD1.csv"
        path.write_text(
            LEDGER_US.replace(",1400,14000,", ",,14000,"), encoding="utf-8"
        )
        with self.assertRaises(PositionError):
            read_ledger("USD1", "price_usd")

    def test_unknown_side_is_refused(self):
        path = position_module.TRADES_DIR / "AAA.csv"
        path.write_text(
            LEDGER_KR.replace("2026-04-09,SELL", "2026-04-09,SPLIT"),
            encoding="utf-8",
        )
        with self.assertRaises(PositionError):
            read_ledger("AAA", "price_krw")


class TestWeights(TempPortfolio):
    def test_buying_with_cash_leaves_total_assets_unchanged(self):
        """현금으로 주식을 사면 총자산은 그대로다.

        현금이 준 만큼 주식이 늘 뿐이다. 이 항등식이 깨지면 추가 매수 시
        비중이 실제보다 낮게 나오고, 집중도를 과소평가하게 된다.
        """
        before = weights("AAA", 5000000.0)
        after = weights("AAA", 6000000.0, added_krw=1000000.0)
        self.assertEqual(
            before["denominators_krw"]["total_assets"],
            after["denominators_krw"]["total_assets"],
        )

    def test_sleeve_denominator_grows_by_the_purchase(self):
        """슬리브에는 현금이 없으므로 현금→주식 이동만큼 실제로 커진다."""
        before = weights("AAA", 5000000.0)
        after = weights("AAA", 6000000.0, added_krw=1000000.0)
        self.assertAlmostEqual(
            after["denominators_krw"]["equity_sleeve"]
            - before["denominators_krw"]["equity_sleeve"],
            1000000.0,
        )

    def test_added_amount_is_not_double_counted_in_the_sleeve(self):
        # 슬리브 9,400,000 중 AAA 5,000,000 → 53.19%
        self.assertAlmostEqual(
            weights("AAA", 5000000.0)["equity_sleeve_pct"], 53.1915, places=3
        )
        # 100만 추가 → AAA 600만 / 슬리브 1,040만 = 57.69%
        after = weights("AAA", 6000000.0, added_krw=1000000.0)
        self.assertAlmostEqual(after["equity_sleeve_pct"], 57.6923, places=3)

    def test_cash_is_excluded_from_sleeve_but_not_from_total(self):
        result = weights("AAA", 5000000.0)
        self.assertEqual(result["denominators_krw"]["total_assets"], 11400000.0)
        self.assertEqual(result["denominators_krw"]["equity_sleeve"], 9400000.0)


class TestAtPrice(unittest.TestCase):
    LEDGER = {"quantity": 10.0, "cost_basis": 1000000.0,
              "average_cost": 100000.0}

    def test_above_and_below_average_cost_are_stated_as_facts(self):
        self.assertEqual(
            at_price(self.LEDGER, 110000, "KRW")["price_vs_average_cost"],
            "ABOVE_AVERAGE_COST",
        )
        self.assertEqual(
            at_price(self.LEDGER, 90000, "KRW")["price_vs_average_cost"],
            "BELOW_AVERAGE_COST",
        )
        self.assertEqual(
            at_price(self.LEDGER, 100000, "KRW")["price_vs_average_cost"],
            "AT_AVERAGE_COST",
        )


class TestJudgmentBoundary(TempPortfolio):
    """계산기는 측정만 한다. 판정 어휘가 출력에 들어오면 실패한다."""

    FORBIDDEN = (
        "싸다", "비싸다", "저평가", "고평가", "매수", "매도", "추천",
        "권장", "줄여", "늘려", "과도", "적정", "위험하다", "BUY", "SELL",
        "OVERWEIGHT", "UNDERWEIGHT", "RECOMMEND",
    )

    @staticmethod
    def _data_only(node):
        """`_` 로 시작하는 설명 필드를 뺀 데이터만 남긴다.

        `_not_a_judgment` 같은 산문은 경계를 **설명하려고** 판정 어휘를 쓴다.
        금지해야 하는 것은 산문이 아니라 계산기가 내는 값이다.
        """
        if isinstance(node, dict):
            return {
                key: TestJudgmentBoundary._data_only(value)
                for key, value in node.items()
                if not key.startswith("_")
            }
        if isinstance(node, list):
            return [TestJudgmentBoundary._data_only(item) for item in node]
        return node

    def test_output_contains_no_verdict_vocabulary(self):
        result = position_module.build("AAA", 300000.0, currency="KRW")
        blob = json.dumps(self._data_only(result), ensure_ascii=False)
        for word in self.FORBIDDEN:
            self.assertNotIn(
                word, blob,
                f"계산기 출력에 판정 어휘 '{word}' 가 들어왔다 — "
                "측정과 판단의 경계가 무너지고 있다",
            )

    def test_output_declares_that_it_is_not_a_judgment(self):
        result = position_module.build("AAA", 300000.0, currency="KRW")
        self.assertIn("_not_a_judgment", result)

    def test_no_recommended_quantity_field(self):
        """'얼마를 사라'는 값은 없어야 한다. 사다리는 눈금이지 제안이 아니다."""
        result = position_module.build("AAA", 300000.0, currency="KRW")
        blob = json.dumps(result, ensure_ascii=False)
        for field in ("recommended", "suggested", "target_quantity", "verdict"):
            self.assertNotIn(field, blob)
        self.assertIn("보유 수량", result["ladder_basis"])

    def test_no_threshold_is_invented(self):
        """임계값은 모델이 정한다. 계산기가 손절가·목표가를 만들지 않는다."""
        result = position_module.build("AAA", 300000.0, currency="KRW")
        blob = json.dumps(result, ensure_ascii=False)
        for field in ("stop_price", "target_price", "limit_pct", "breach"):
            self.assertNotIn(field, blob)


class TestReconciliation(TempPortfolio):
    def test_reports_the_gap_and_names_the_authority(self):
        """평단의 권위는 증권사 스냅샷이다 (사용자 확정 2026-07-31).

        원장 평단은 교차검증용이며, 벌어지는 폭은 원장이 재현하지 못하는
        항목이 있다는 알람으로만 쓴다.
        """
        result = position_module.build("AAA", 300000.0, currency="KRW")
        rec = result["reconciliation"]
        # 원장 평단 250,000 vs 스냅샷 (5,000,000-500,000)/15 = 300,000
        self.assertEqual(rec["average_cost_ledger"], 250000.0)
        self.assertEqual(rec["average_cost_snapshot"], 300000.0)
        self.assertFalse(rec["within_tolerance"])
        self.assertEqual(rec["authority"], "BROKER_SNAPSHOT")

    def test_ledger_still_carries_what_the_snapshot_cannot(self):
        """실현손익은 스냅샷에 없다. 권위가 스냅샷이어도 원장을 계속 읽는 이유다."""
        result = position_module.build("AAA", 300000.0, currency="KRW")
        self.assertEqual(result["ledger"]["realized_pnl"], 749500.0)
        self.assertIsNone(result["snapshot"].get("realized_pnl"))

    def test_currency_mismatch_refuses_to_compare(self):
        result = position_module.build("USD1", 120.0, currency="USD")
        self.assertFalse(result["reconciliation"]["comparable"])
        self.assertIsNone(result["reconciliation"]["average_cost_diff"])


if __name__ == "__main__":
    unittest.main()
