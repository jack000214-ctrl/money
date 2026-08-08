#!/usr/bin/env python3
"""Collect KR/US prices with adjusted close for return measurement."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRICE_DIR = ROOT / "knowledge" / "prices"
CHART = (
    "https://query1.finance.yahoo.com/v8/finance/chart/"
    "{symbol}?range={range}&interval=1d&events=div%2Csplits"
)
USER_AGENT = "Mozilla/5.0"
FIELDS = [
    "date",
    "close",
    "adjusted_close",
    "split_adjustment_factor",
    "high",
    "low",
    "volume",
]


def safe_name(symbol: str) -> str:
    return symbol.replace(".", "_").replace("^", "").replace("=", "")


# Yahoo 의 조정종가는 float32 로 오고, 같은 날짜라도 호출할 때마다 1 ulp 씩
# 흔들린다(86994.5234375 ↔ 86994.5078125). 그대로 저장하면 수집할 때마다
# 400 행이 바뀐 것처럼 보여 진짜 변경이 diff 에 묻힌다. float32 유효자릿수가
# 7.2 이므로 6 자리에서 끊으면 지터만 사라지고 값은 그대로다 — 반올림 단위
# 1e-6 은 지터 1.2e-7 보다 크고, 가격 판단이 요구하는 정밀도보다 훨씬 작다.
ADJUSTED_CLOSE_SIGNIFICANT_DIGITS = 6


def _stable(value):
    """조정종가에서 float32 지터를 걷어낸다. 원시 호가(close/high/low)는 건드리지 않는다."""
    if not isinstance(value, (int, float)):
        return value
    return float(f"%.{ADJUSTED_CLOSE_SIGNIFICANT_DIGITS}g" % value)


# 이 파일이 저장하는 것은 일봉이며, 그것이 하류 계약의 전제다. 변동성·이동평균·
# 거래량 배수·급변 판정이 전부 한 행 = 하루를 가정한다.
DAILY_GRANULARITY = "1d"

# 일봉의 행 간격 중앙값. 주말이 끼면 3일, 연휴가 끼면 더 벌어지지만 중앙값은
# 1~2일에 머문다. 주봉(7)과 월봉(30)은 이 문턱 위에 있다.
MAX_DAILY_GAP_DAYS = 4


def granularity(meta: dict, rows: list[dict]) -> str:
    """이 응답이 실제로 일봉인가.

    `meta.dataGranularity` 가 있으면 그것이 권위다. 없으면 행 간격의 중앙값으로
    센다. 관측이 한 행뿐이면 간격이 없으므로 `UNKNOWN` 이다 — 모르는 것을
    일봉으로 단정하지 않고, 아래 `fetch` 도 모른다는 이유로 막지 않는다.
    막는 것은 **일봉이 아님이 확인된** 경우뿐이다.
    """
    declared = meta.get("dataGranularity")
    if isinstance(declared, str) and declared:
        return declared
    if len(rows) < 2:
        return "UNKNOWN"
    days = [date.fromisoformat(row["date"]) for row in rows]
    gaps = sorted((days[i] - days[i - 1]).days for i in range(1, len(days)))
    median_gap = gaps[len(gaps) // 2]
    return DAILY_GRANULARITY if median_gap <= MAX_DAILY_GAP_DAYS else f"{median_gap}d"


def session_state(meta: dict, last_date: str) -> str:
    """마지막 봉이 확정 종가인지 판별한다.

    장중에 수집하면 진행 중인 봉이 확정 일봉과 똑같은 형태로 저장된다. 하류의
    거래량 배수·급변 판정은 그 행을 하루치로 가정하므로, 확정 여부를 파일에
    남기지 않으면 개장 1시간짜리 거래량이 '평소 대비 0.3배'로 읽힌다.
    장 시간을 하드코딩하지 않고 거래소가 선언한 세션 경계를 그대로 쓴다.
    """
    market_time = meta.get("regularMarketTime")
    period = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
    session_end = period.get("end")
    offset = meta.get("gmtoffset")
    if not all(
        isinstance(value, int)
        for value in (market_time, session_end, offset)
    ):
        return "UNKNOWN"
    local_date = (
        datetime.fromtimestamp(market_time + offset, timezone.utc)
        .date()
        .isoformat()
    )
    if local_date != last_date:
        # 마지막 봉이 이전 세션이면 이미 닫힌 봉이다.
        return "SETTLED"
    return "SETTLED" if market_time >= session_end else "UNSETTLED"


def fail(message: str) -> None:
    print(f"오류: {message}", file=sys.stderr)
    raise SystemExit(1)


def fetch(symbol: str, range_name: str) -> tuple[dict, list[dict]]:
    request = urllib.request.Request(
        CHART.format(symbol=symbol, range=range_name),
        headers={"User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        fail(f"HTTP {exc.code} — {symbol}")
    except urllib.error.URLError as exc:
        fail(f"연결 실패 — {exc.reason}")

    results = (data.get("chart") or {}).get("result")
    if not results:
        fail(f"{symbol}: 데이터 없음 {(data.get('chart') or {}).get('error')}")
    result = results[0]
    timestamps = result.get("timestamp") or []
    quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    adjusted_blocks = (result.get("indicators") or {}).get("adjclose") or []
    adjusted = adjusted_blocks[0].get("adjclose") if adjusted_blocks else None
    split_events: list[tuple[str, float]] = []
    for event in ((result.get("events") or {}).get("splits") or {}).values():
        try:
            split_date = datetime.fromtimestamp(
                int(event["date"]), timezone.utc
            ).date().isoformat()
            numerator = float(event["numerator"])
            denominator = float(event["denominator"])
        except (KeyError, TypeError, ValueError):
            continue
        if (
            numerator > 0
            and denominator > 0
            and math.isfinite(numerator)
            and math.isfinite(denominator)
        ):
            split_events.append((split_date, denominator / numerator))

    def quote_value(field: str, index: int):
        values = quote.get(field) or []
        return values[index] if index < len(values) else None

    rows: list[dict] = []
    for index, timestamp in enumerate(timestamps):
        row_date = datetime.fromtimestamp(timestamp, timezone.utc).date().isoformat()
        close = quote_value("close", index)
        if close is None:
            continue
        adjusted_close = _stable(
            adjusted[index]
            if adjusted and index < len(adjusted) and adjusted[index] is not None
            else close
        )
        split_adjustment_factor = math.prod(
            factor
            for split_date, factor in split_events
            if split_date > row_date
        )
        rows.append(
            {
                "date": row_date,
                "close": close,
                "adjusted_close": adjusted_close,
                "split_adjustment_factor": split_adjustment_factor,
                "high": quote_value("high", index),
                "low": quote_value("low", index),
                "volume": quote_value("volume", index),
            }
        )
    if not rows:
        fail(f"{symbol}: 유효한 가격 행이 없다")

    # Yahoo 는 interval=1d 로 요청해도 range 가 크면 조용히 봉을 굵게 만든다
    # (range=max → dataGranularity=1mo). 그대로 저장하면 2,447행짜리 일봉
    # 파일이 320행짜리 월봉으로 덮이는데 헤더에는 그 사실이 남지 않는다.
    # 실제로 그랬고, 그 뒤의 변동성·이동평균·급변 판정은 전부 월봉 위에서
    # 조용히 계산된다. 저장 전에 여기서 멈춰야 파일이 오염되지 않는다.
    meta = result.get("meta") or {}
    actual = granularity(meta, rows)
    if actual not in (DAILY_GRANULARITY, "UNKNOWN"):
        fail(
            f"{symbol}: 일봉을 요청했는데 {actual} 봉이 왔다 (range={range_name}). "
            "Yahoo 는 긴 range 에서 interval 을 조용히 낮춘다 — 저장하지 않고 "
            "멈춘다. 긴 이력이 필요하면 --range 10y 를 쓴다"
        )
    return meta, rows


def save(symbol: str, meta: dict, rows: list[dict]) -> Path:
    PRICE_DIR.mkdir(parents=True, exist_ok=True)
    path = PRICE_DIR / f"{safe_name(symbol)}.csv"
    retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            newline="",
            encoding="utf-8",
            dir=PRICE_DIR,
            delete=False,
        ) as temporary:
            temporary.write("# schema_version price-series/2.1.0\n")
            temporary.write(
                f"# symbol {symbol} / currency {meta.get('currency') or 'UNKNOWN'}\n"
            )
            temporary.write(
                f"# retrieved_at {retrieved_at} / source Yahoo Finance chart API\n"
            )
            temporary.write(
                f"# last_bar {rows[-1]['date']} / "
                f"session {session_state(meta, rows[-1]['date'])}\n"
            )
            writer = csv.DictWriter(temporary, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_name = temporary.name
        os.replace(temporary_name, path)
    finally:
        if temporary_name and Path(temporary_name).exists():
            Path(temporary_name).unlink()
    return path


def load(symbol: str) -> list[dict[str, float | str]]:
    path = PRICE_DIR / f"{safe_name(symbol)}.csv"
    if not path.exists():
        fail(f"{path.relative_to(ROOT)} 가 없다. 먼저 history로 수집한다")
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = csv.DictReader(
            line for line in handle if not line.lstrip().startswith("#")
        )
        return [
            {"date": row["date"], "close": float(row["close"])}
            for row in rows
            if row.get("date") and row.get("close")
        ]


def cmd_history(args) -> None:
    meta, rows = fetch(args.symbol, args.range)
    state = session_state(meta, rows[-1]["date"])
    path = save(args.symbol, meta, rows)
    print(
        f"{args.symbol} {meta.get('currency') or 'UNKNOWN'} {len(rows)}일 "
        f"{rows[0]['date']} ~ {rows[-1]['date']}"
    )
    if state != "SETTLED":
        print(
            f"※ 마지막 봉 {rows[-1]['date']} 은 {state} — 확정 종가가 아니다. "
            "거래량 배수와 급변 판정에서 제외된다"
        )
    print(f"저장: {path.relative_to(ROOT)}")


def band(rows: list[dict], symbol: str, cost: float | None = None) -> dict:
    """구간 내 위치와 고점 대비 낙폭.

    렌더링에서 떼어 낸 이유는 이 값들이 판단에 그대로 인용되기 때문이다 —
    "52주 고점 대비 -51.3%" 같은 문장이 화면에서 바로 나온다. print 안에서만
    계산되면 저장되지도 검증되지도 않는다.

    고가와 저가가 같은 구간(거래 정지·상장 직후)에서는 위치를 **지어내지 않고**
    50 으로 둔다. 0 이나 100 으로 두면 "바닥"·"천장"이라는 없는 정보가 생긴다.
    """
    closes = [float(row["close"]) for row in rows]
    current, high, low = closes[-1], max(closes), min(closes)
    payload = {
        "symbol": symbol,
        "from": rows[0]["date"],
        "to": rows[-1]["date"],
        "close": current,
        "high": high,
        "low": low,
        "position_pct": (
            round((current - low) / (high - low) * 100, 2) if high > low else 50.0
        ),
        "drawdown_pct": round((current / high - 1) * 100, 2) if high else None,
    }
    if cost:
        payload["return_from_cost_pct"] = round((current / cost - 1) * 100, 2)
    return payload


def cmd_band(args) -> None:
    rows = load(args.symbol)
    if args.days:
        rows = rows[-args.days :]
    print(json.dumps(band(rows, args.symbol, args.cost),
                     ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="KR/US 가격 시계열 수집")
    subparsers = parser.add_subparsers(dest="command", required=True)
    history = subparsers.add_parser("history")
    history.add_argument("symbol")
    history.add_argument("--range", default="5y")
    history.set_defaults(function=cmd_history)
    band = subparsers.add_parser("band")
    band.add_argument("symbol")
    band.add_argument("--days", type=int)
    band.add_argument("--cost", type=float)
    band.set_defaults(function=cmd_band)
    args = parser.parse_args()
    args.function(args)


if __name__ == "__main__":
    main()
