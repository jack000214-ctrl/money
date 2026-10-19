"""장중 현재가 조회: 네트워크 없이 고정 응답으로 파싱과 실패 시 이전값 보존을 본다."""
from __future__ import annotations

import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cio import paths, watch_store
from collectors import quote

KST = ZoneInfo("Asia/Seoul")
# 2026-10-19 월요일. 정규장 09:00~15:30 KST
REGULAR_START = int(dt.datetime(2026, 10, 19, 9, 0, tzinfo=KST).timestamp())
REGULAR_END = int(dt.datetime(2026, 10, 19, 15, 30, tzinfo=KST).timestamp())


def meta(price=214500.0, quote_at=None, symbol="005930.KS", currency="KRW", **extra):
    quote_at = quote_at or dt.datetime(2026, 10, 19, 10, 14, tzinfo=KST)
    base = {
        "symbol": symbol,
        "currency": currency,
        "exchangeTimezoneName": "Asia/Seoul",
        "regularMarketPrice": price,
        "regularMarketTime": int(quote_at.timestamp()),
        "chartPreviousClose": 215500.0,
        "currentTradingPeriod": {
            "regular": {"start": REGULAR_START, "end": REGULAR_END}
        },
    }
    base.update(extra)
    return base


def at(hour, minute=0, day=19):
    return dt.datetime(2026, 10, day, hour, minute, tzinfo=KST)


class TempWatch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        original = paths.WATCH_DIR
        paths.WATCH_DIR = Path(self.tmp.name) / "watch"
        self.addCleanup(setattr, paths, "WATCH_DIR", original)


class TestParse(unittest.TestCase):
    def test_live_regular_session(self):
        row = quote.parse_meta("005930.KS", meta(), at(10, 15), "KRW")
        self.assertEqual(row["price"], 214500.0)
        self.assertEqual(row["session"], "REGULAR")
        self.assertEqual(row["price_kind"], "REGULAR_LIVE")
        self.assertTrue(row["comparable"])
        self.assertEqual(row["previous_close"], 215500.0)

    def test_after_close_is_last_regular_not_live(self):
        closing = at(15, 30)
        row = quote.parse_meta("005930.KS", meta(quote_at=closing), at(18, 0), "KRW")
        self.assertEqual(row["session"], "AFTER_REGULAR")
        self.assertEqual(row["price_kind"], "LAST_REGULAR")
        self.assertTrue(row["comparable"], "오늘 종가는 오늘 시세다")

    def test_previous_day_quote_before_open_is_not_comparable(self):
        """개장 전 첫 사이클은 어제 종가를 돌려준다. 그것으로 문턱을 대조하면 안 된다."""
        yesterday = dt.datetime(2026, 10, 16, 15, 30, tzinfo=KST)
        row = quote.parse_meta("005930.KS", meta(quote_at=yesterday), at(8, 5), "KRW")
        self.assertEqual(row["session"], "BEFORE_REGULAR")
        self.assertFalse(row["fresh_today"])
        self.assertFalse(row["comparable"])

    def test_weekday_holiday_is_detected_by_stale_quote_time(self):
        stale = dt.datetime(2026, 10, 16, 15, 30, tzinfo=KST)
        row = quote.parse_meta("005930.KS", meta(quote_at=stale), at(11, 0), "KRW")
        self.assertEqual(row["session"], "REGULAR")
        self.assertFalse(row["comparable"])
        self.assertEqual(row["price_kind"], "LAST_REGULAR")

    def test_other_instrument_is_refused(self):
        with self.assertRaises(quote.QuoteError):
            quote.parse_meta("005930.KS", meta(symbol="000660.KS"), at(10), "KRW")

    def test_currency_mismatch_is_refused(self):
        with self.assertRaises(quote.QuoteError):
            quote.parse_meta("005930.KS", meta(currency="USD"), at(10), "KRW")

    def test_non_positive_or_non_numeric_price_is_refused(self):
        for bad in (0, -1, None, "100", float("nan"), True):
            with self.subTest(bad), self.assertRaises(quote.QuoteError):
                quote.parse_meta("005930.KS", meta(price=bad), at(10, 15), "KRW")  # pyright: ignore[reportArgumentType]

    def test_future_quote_time_is_refused(self):
        future = dt.datetime(2026, 10, 19, 12, 0, tzinfo=KST)
        with self.assertRaises(quote.QuoteError):
            quote.parse_meta("005930.KS", meta(quote_at=future), at(10, 15), "KRW")

    def test_extended_hours_prices_are_not_invented(self):
        row = quote.parse_meta("005930.KS", meta(), at(10, 15), "KRW")
        self.assertNotIn("preMarketPrice", row)
        self.assertNotIn("postMarketPrice", row)


class TestCollect(TempWatch):
    holdings = [
        {"ticker": "005930", "name": "삼성전자", "qty": 80, "market": "KR",
         "currency": "KRW", "price_symbol": "005930.KS"},
        {"ticker": "000660", "name": "SK하이닉스", "qty": 15, "market": "KR",
         "currency": "KRW", "price_symbol": "000660.KS"},
    ]

    def test_failed_symbol_keeps_previous_price_but_is_not_comparable(self):
        def good(symbol):
            return meta(symbol=symbol)

        first = quote.collect(self.holdings, fetcher=good, now=at(10, 15))
        self.assertEqual((first["ok"], first["failed"]), (2, 0))

        def flaky(symbol):
            if symbol == "000660.KS":
                raise quote.QuoteError("HTTP 500")
            return meta(symbol=symbol, price=213000.0)

        second = quote.collect(self.holdings, fetcher=flaky, now=at(10, 25))
        self.assertEqual((second["ok"], second["failed"]), (1, 1))
        saved = watch_store.load_quotes()["quotes"]
        self.assertEqual(saved["005930"]["price"], 213000.0)
        failed = saved["000660"]
        self.assertEqual(failed["status"], quote.FAILED)
        self.assertEqual(failed["price"], 214500.0, "실패가 이전 값을 지우면 안 된다")
        self.assertFalse(failed["comparable"], "낡은 값이 새 관측처럼 대조되면 안 된다")
        self.assertEqual(failed["last_error"], "HTTP 500")

    def test_first_failure_has_no_price(self):
        def broken(symbol):
            raise quote.QuoteError("연결 실패")

        quote.collect(self.holdings[:1], fetcher=broken, now=at(10))
        row = watch_store.load_quotes()["quotes"]["005930"]
        self.assertNotIn("price", row)
        self.assertEqual(row["status"], quote.FAILED)

    def test_sold_position_is_dropped_from_quotes(self):
        quote.collect(self.holdings, fetcher=lambda s: meta(symbol=s), now=at(10, 15))
        quote.collect(self.holdings[:1], fetcher=lambda s: meta(symbol=s), now=at(10, 25))
        self.assertEqual(list(watch_store.load_quotes()["quotes"]), ["005930"])

    def test_daily_price_store_is_never_touched(self):
        prices = paths.KNOWLEDGE_DIR / "prices"
        before = sorted(p.name for p in prices.glob("*")) if prices.exists() else []
        quote.collect(self.holdings, fetcher=lambda s: meta(symbol=s), now=at(10, 15))
        after = sorted(p.name for p in prices.glob("*")) if prices.exists() else []
        self.assertEqual(before, after)


class TestHoldings(unittest.TestCase):
    def test_only_positive_quantity_kr_with_price_symbol(self):
        with tempfile.TemporaryDirectory() as tmp:
            tickers = Path(tmp) / "tickers.json"
            assets = Path(tmp) / "assets.json"
            tickers.write_text(json.dumps({
                "005930": {"name": "삼성전자", "qty": 80, "market": "KR"},
                "000660": {"name": "SK하이닉스", "qty": 0, "market": "KR"},
                "TSLA": {"name": "Tesla", "qty": 3, "market": "US"},
                "111111": {"name": "심볼없음", "qty": 1, "market": "KR"},
            }), encoding="utf-8")
            assets.write_text(json.dumps({"assets": {
                "005930": {"market": "KR", "currency": "KRW", "price_symbol": "005930.KS"},
                "000660": {"market": "KR", "currency": "KRW", "price_symbol": "000660.KS"},
                "TSLA": {"market": "US", "currency": "USD", "price_symbol": "TSLA"},
            }}), encoding="utf-8")
            rows = quote.load_holdings(("KR",), tickers_file=tickers, assets_file=assets)
        self.assertEqual([r["ticker"] for r in rows], ["005930"])


if __name__ == "__main__":
    unittest.main()
