"""보유 종목의 장중 현재가를 조회해 `knowledge/watch/quotes.json` 에 둔다.

일봉 저장소(`collectors/price.py`, `knowledge/prices/`)와 **섞지 않는다.** 장중에 받은
마지막 봉을 일봉 CSV 에 넣으면 개장 1시간짜리 거래량이 하루치로 읽힌다
(`price.session_state()` 주석). 이 모듈은 일봉 CSV 를 읽지도 쓰지도 않는다.

가격 출처 실측 (2026-10-19 10:14 KST, 005930.KS · 0167A0.KS)
  - Yahoo chart `meta` 는 `regularMarketPrice`·`regularMarketTime` 과
    `currentTradingPeriod.{pre,regular,post}` 시각 경계를 준다.
  - `preMarketPrice`·`postMarketPrice` 는 **없다(null).** 한국 종목은 정규장 가격만 받는다.
  따라서 08:00~09:00 과 15:30~20:00 에는 "마지막 정규장 가격"이 오며, 이 값을
  `price_kind = LAST_REGULAR` 로 명시한다. 시간외 단일가·NXT 가격은 받지 못한다.

대조 가능 여부(`comparable`)
  시세 시각이 오늘(거래소 시간대)이 아니면 전 거래일 가격이다. 개장 직후 첫 사이클이나
  평일 휴장일에 그 가격으로 문턱을 대조하면 이미 알린 사실을 다시 알리거나 잘못된
  접촉을 만든다. 그래서 `fresh_today = False` 인 시세는 `comparable = False` 이고
  트리거 대조에서 제외한다. 평일 휴장일은 별도 캘린더 없이 이 규칙으로 감지한다.

조회 실패는 이전 값을 덮어쓰지 않는다. 이전 시세는 그대로 두고 `status = FAILED` 와
오류 사유만 덧붙인다.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import urllib.error
import urllib.request
from typing import Any, Callable, TypeGuard
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cio import watch_store
from cio.paths import CONFIG_DIR, ROOT
from collectors.price import USER_AGENT, same_instrument

CHART = (
    "https://query1.finance.yahoo.com/v8/finance/chart/"
    "{symbol}?range=1d&interval=1d"
)
TICKERS_FILE = ROOT / "dashboard" / "scripts" / "tickers.json"
ASSETS_FILE = CONFIG_DIR / "assets.json"
SOURCE = "YAHOO_CHART_META"
TIMEOUT_SECONDS = 15

OK = "OK"
FAILED = "FAILED"


class QuoteError(Exception):
    pass


def _is_number(value: Any) -> TypeGuard[int | float]:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def load_holdings(
    markets: tuple[str, ...] | list[str] = ("KR",),
    *,
    tickers_file=None,
    assets_file=None,
) -> list[dict[str, Any]]:
    """`tickers.json` 의 `qty > 0` 종목에 `assets.json` 의 `price_symbol` 을 붙인다.

    registry 전체 검증(`cio.assets.get_asset`)을 거치지 않고 JSON 을 직접 읽는다. 다른
    종목의 등록 오류가 시세 조회를 막으면 안 된다.
    """
    tickers = _read_json(tickers_file or TICKERS_FILE)
    assets = (_read_json(assets_file or ASSETS_FILE).get("assets")) or {}
    rows: list[dict[str, Any]] = []
    for ticker, row in tickers.items():
        if not isinstance(row, dict):
            continue
        try:
            qty = float(row.get("qty") or 0)
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue
        meta = assets.get(ticker) or {}
        market = str(meta.get("market") or row.get("market") or "KR").upper()
        if market not in markets:
            continue
        symbol = meta.get("price_symbol")
        if not isinstance(symbol, str) or not symbol:
            continue
        rows.append(
            {
                "ticker": ticker,
                "name": row.get("name") or meta.get("name") or ticker,
                "qty": qty,
                "market": market,
                "currency": str(meta.get("currency") or row.get("currency") or "KRW"),
                "price_symbol": symbol,
            }
        )
    return rows


def _read_json(path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def fetch_meta(symbol: str) -> dict[str, Any]:
    """Yahoo chart 응답의 `meta` 를 돌려준다. 실패는 `QuoteError` 다."""
    request = urllib.request.Request(
        CHART.format(symbol=symbol), headers={"User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise QuoteError(f"HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise QuoteError(f"연결 실패: {type(exc).__name__}") from exc
    results = (data.get("chart") or {}).get("result")
    if not results:
        raise QuoteError("응답에 데이터가 없다")
    meta = results[0].get("meta")
    if not isinstance(meta, dict):
        raise QuoteError("응답에 meta가 없다")
    return meta


def parse_meta(
    symbol: str, meta: dict[str, Any], now: dt.datetime, expected_currency: str
) -> dict[str, Any]:
    """meta 한 개를 시세 행으로 접는다. 검증에 실패하면 `QuoteError` 다."""
    responded = meta.get("symbol")
    if isinstance(responded, str) and responded and not same_instrument(symbol, responded):
        raise QuoteError(f"응답은 {responded} 의 시세다")
    price = meta.get("regularMarketPrice")
    if not _is_number(price) or price <= 0:
        raise QuoteError("regularMarketPrice가 양의 숫자가 아니다")
    currency = meta.get("currency")
    if isinstance(currency, str) and currency and currency.upper() != expected_currency.upper():
        raise QuoteError(
            f"시세 통화({currency})가 등록 통화({expected_currency})와 다르다"
        )
    market_time = meta.get("regularMarketTime")
    if type(market_time) is not int:
        raise QuoteError("regularMarketTime이 없다")

    zone_name = meta.get("exchangeTimezoneName")
    try:
        zone = ZoneInfo(zone_name) if isinstance(zone_name, str) else ZoneInfo("Asia/Seoul")
    except ZoneInfoNotFoundError:
        zone = ZoneInfo("Asia/Seoul")
    quote_dt = dt.datetime.fromtimestamp(market_time, zone)
    today = now.astimezone(zone).date()
    if quote_dt > now.astimezone(zone) + dt.timedelta(minutes=5):
        raise QuoteError("시세 시각이 현재보다 미래다")
    fresh_today = quote_dt.date() == today

    periods = meta.get("currentTradingPeriod") or {}
    regular = periods.get("regular") or {}
    start, end = regular.get("start"), regular.get("end")
    now_ts = int(now.timestamp())
    if type(start) is int and type(end) is int and start < end:
        if start <= now_ts < end:
            session = "REGULAR"
        elif now_ts < start:
            session = "BEFORE_REGULAR"
        else:
            session = "AFTER_REGULAR"
    else:
        session = "UNKNOWN"

    live = session == "REGULAR" and fresh_today
    previous = meta.get("chartPreviousClose")
    return {
        "price": float(price),
        "currency": (currency or expected_currency).upper(),
        "quote_time": quote_dt.isoformat(timespec="seconds"),
        "session": session,
        "price_kind": "REGULAR_LIVE" if live else "LAST_REGULAR",
        "fresh_today": fresh_today,
        "comparable": fresh_today,
        "previous_close": float(previous) if _is_number(previous) and previous > 0 else None,
        "source": SOURCE,
        "_note": (
            "정규장 가격이다. 시간외·NXT 가격은 받지 못한다"
            if not live
            else "정규장 진행 중 가격이다(지연될 수 있다)"
        ),
    }


def collect(
    holdings: list[dict[str, Any]],
    *,
    fetcher: Callable[[str], dict[str, Any]] = fetch_meta,
    now: dt.datetime | None = None,
    save: bool = True,
) -> dict[str, Any]:
    """보유 종목 전체를 조회해 quotes.json 을 갱신하고 결과를 돌려준다."""
    now = now or dt.datetime.now().astimezone()
    stored = watch_store.load_quotes()
    quotes: dict[str, Any] = dict(stored.get("quotes") or {})
    fetched_at = now.isoformat(timespec="seconds")
    ok = failed = 0
    for holding in holdings:
        ticker = holding["ticker"]
        try:
            meta = fetcher(holding["price_symbol"])
            row = parse_meta(
                holding["price_symbol"], meta, now, holding["currency"]
            )
        except QuoteError as exc:
            previous = dict(quotes.get(ticker) or {})
            previous.update(
                {
                    "status": FAILED,
                    "last_error": str(exc),
                    "last_error_at": fetched_at,
                    "name": holding["name"],
                    "price_symbol": holding["price_symbol"],
                }
            )
            # 이전 값은 보존하되 오래된 값이 새 관측처럼 대조되지 않게 막는다.
            if "price" in previous:
                previous["comparable"] = False
            quotes[ticker] = previous
            failed += 1
            continue
        row.update(
            {
                "status": OK,
                "fetched_at": fetched_at,
                "name": holding["name"],
                "price_symbol": holding["price_symbol"],
                "last_error": None,
            }
        )
        quotes[ticker] = row
        ok += 1
    # 보유에서 빠진 종목의 시세는 남기지 않는다.
    held = {h["ticker"] for h in holdings}
    quotes = {t: q for t, q in quotes.items() if t in held}
    result = {"updated_at": fetched_at, "quotes": quotes}
    if save:
        watch_store.save_quotes(result)
    result["ok"] = ok
    result["failed"] = failed
    return result
