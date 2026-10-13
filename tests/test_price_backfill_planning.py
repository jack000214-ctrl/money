"""한국 시계열의 구멍을 거래소 원문으로 메우는 작업이 계획에 들어가는 자리.

지수만 메우던 때에는 종목 쪽 구멍을 아무도 보지 않았다. 2026-10-13 수집에서
파마리서치의 10-12 봉이 사라졌고 packet 은 그대로 `PASS` 였다.

지키는 성질은 넷이다.
  1. 한국 종목·ETF 의 계획에는 자기 시계열 백필이 들어간다. 미국 자산에는 없다
  2. 그 작업은 **필수가 아니다** — 거래소 API 가 막혀도 판단을 세우지 않는다
  3. 달력이 한국 시계열의 합집합이므로, 지수 백필은 함께 수집되는 다른 한국
     시계열에도 의존한다. 자기 파일만 보면 "구멍 없음" 기록이 낡는다
  4. 급변 대조는 종목 백필 표식이 **있을 때만** 그것에 의존한다 — 없는 입력을
     의존성으로 걸면 통째로 막힌다
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import helpers  # noqa: F401
from cio import refresh
from cio.refresh import _asset_backfill_task, tasks_for

STOCK = {
    "asset_id": "214450", "name": "파마리서치", "market": "KR", "exchange": "KOSDAQ",
    "asset_type": "STOCK", "currency": "KRW", "price_symbol": "214450.KQ",
    "filing_source": "DART", "filing_profile": "DART_KIFRS", "benchmarks": ["^KQ11"],
    "optional_collectors": [],
}
US_STOCK = {
    "asset_id": "RXRX", "market": "US", "exchange": "NASDAQ", "asset_type": "STOCK",
    "currency": "USD", "price_symbol": "RXRX", "benchmarks": ["^IXIC"],
}


def plan(asset, root=None):
    with patch.object(refresh, "get_asset", return_value=asset), \
            patch.object(refresh, "ROOT", root or refresh.ROOT):
        return {task.name: task for task in tasks_for(asset["asset_id"])}


class TestAssetBackfillTask(unittest.TestCase):
    def test_a_korean_stock_fills_its_own_series_from_its_own_market(self):
        task = _asset_backfill_task(STOCK)
        self.assertEqual(task.name, "price_backfill:214450.KQ")
        self.assertEqual(task.artifact, "knowledge/prices/_backfill/214450_KQ.json")
        self.assertEqual(task.command[-4:], ["backfill-stock", "214450", "--market", "KOSDAQ"])

    def test_an_etf_uses_the_etf_service_even_when_listed_on_kospi(self):
        etf = {**STOCK, "asset_id": "0053L0", "asset_type": "ETF",
               "exchange": "KOSPI", "price_symbol": "0053L0.KS"}
        self.assertEqual(_asset_backfill_task(etf).command[-2:], ["--market", "ETF"])

    def test_it_never_blocks_a_judgment(self):
        self.assertFalse(_asset_backfill_task(STOCK).required)

    def test_a_us_asset_has_no_exchange_backfill(self):
        self.assertIsNone(_asset_backfill_task(US_STOCK))

    def test_an_unknown_exchange_is_not_guessed(self):
        self.assertIsNone(_asset_backfill_task({**STOCK, "exchange": "UNKNOWN"}))


class TestPlanWiring(unittest.TestCase):
    def test_the_stock_backfill_is_in_a_korean_plan(self):
        self.assertIn("price_backfill:214450.KQ", plan(STOCK))

    def test_usd_krw_is_collected_for_a_korean_asset_but_not_required(self):
        task = plan(STOCK)["price:KRW=X"]
        self.assertFalse(task.required)

    def test_index_backfill_depends_on_every_korean_series_in_the_plan(self):
        dependencies = plan(STOCK)["benchmark_backfill:^KQ11"].dependencies
        self.assertEqual(set(dependencies), {
            "knowledge/prices/KQ11.csv",
            "knowledge/prices/KS11.csv",
            "knowledge/prices/214450_KQ.csv",
        })

    def test_stock_backfill_depends_on_the_indices_that_define_the_calendar(self):
        dependencies = plan(STOCK)["price_backfill:214450.KQ"].dependencies
        self.assertEqual(set(dependencies), {
            "knowledge/prices/214450_KQ.csv",
            "knowledge/prices/KS11.csv",
            "knowledge/prices/KQ11.csv",
        })

    def test_events_wait_for_the_stock_backfill_only_once_it_has_run(self):
        marker = "knowledge/prices/_backfill/214450_KQ.json"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            before = plan(STOCK, root)["events:214450"].dependencies
            (root / "knowledge" / "prices" / "_backfill").mkdir(parents=True)
            (root / marker).write_text("{}", encoding="utf-8")
            after = plan(STOCK, root)["events:214450"].dependencies
        self.assertNotIn(marker, before, "없는 표식에 의존하면 급변 대조가 막힌다")
        self.assertIn(marker, after)


if __name__ == "__main__":
    unittest.main()
