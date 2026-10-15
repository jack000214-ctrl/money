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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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


def _is_number(value) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


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
    session_start = period.get("start")
    session_end = period.get("end")
    if not all(type(value) is int for value in (market_time, session_end)):
        return "UNKNOWN"

    # Quote time and currentTradingPeriod need not belong to the same session.
    # Before the US open, Yahoo keeps yesterday's closing quote but advances the
    # period to today. Comparing that quote with today's end mislabels yesterday
    # as an open bar. Use the declared session date, in the exchange timezone.
    zone_name = meta.get("exchangeTimezoneName")
    try:
        exchange_zone = ZoneInfo(zone_name) if isinstance(zone_name, str) else None
    except (ZoneInfoNotFoundError, ValueError):
        exchange_zone = None
    if exchange_zone is None:
        offset = period.get("gmtoffset", meta.get("gmtoffset"))
        if type(offset) is not int or not -86400 < offset < 86400:
            return "UNKNOWN"
        exchange_zone = timezone(timedelta(seconds=offset))
    try:
        bar_date = date.fromisoformat(last_date)
        bar_timestamp = meta.get("_collector_last_bar_timestamp")
        if type(bar_timestamp) is int:
            # CSV dates are UTC labels. FX sessions can open before midnight UTC,
            # so only the actual bar timestamp identifies their exchange date.
            if datetime.fromtimestamp(bar_timestamp, timezone.utc).date() != bar_date:
                return "UNKNOWN"
            bar_date = datetime.fromtimestamp(bar_timestamp, exchange_zone).date()
        quote_date = datetime.fromtimestamp(market_time, exchange_zone).date()
        end_date = datetime.fromtimestamp(session_end, exchange_zone).date()
        start_date = datetime.fromtimestamp(session_start, exchange_zone).date() \
            if type(session_start) is int else None
    except (ValueError, OverflowError, OSError):
        return "UNKNOWN"
    if quote_date < bar_date:
        return "UNKNOWN"
    if start_date is not None:
        if session_start >= session_end:
            return "UNKNOWN"
        if bar_date < start_date:
            return "SETTLED"
        if bar_date > end_date:
            return "UNKNOWN"
        return "SETTLED" if market_time >= session_end else "UNSETTLED"

    # Without a start, only an older bar or matching quote/end dates establishes
    # which session the boundary refers to. A future-dated bar is not settled.
    if bar_date < quote_date:
        return "SETTLED"
    if bar_date == quote_date == end_date:
        return "SETTLED" if market_time >= session_end else "UNSETTLED"
    return "UNKNOWN"


def fail(message: str) -> None:
    print(f"오류: {message}", file=sys.stderr)
    raise SystemExit(1)


def same_instrument(requested: str, responded: str) -> bool:
    """요청한 이름과 응답이 말한 이름이 같은 종목인가.

    Yahoo 의 달러 기준 환율에는 이름이 둘이다 — `KRW=X` 는 `USDKRW=X` 의
    약칭이다. 같은 요청에도 조회 기간에 따라 응답의 `meta.symbol` 이 둘 중
    하나로 바뀌어 온다(2026-10-15 실측: range=1mo·5y 는 KRW=X, 2y 는 USDKRW=X).
    이것을 다른 종목으로 막자 달러/원 시계열이 이틀 동안 갱신되지 않았다.
    동치로 보는 것은 이 약칭 하나뿐이다. 교차쌍(`CNYKRW=X`)과 다른 통화
    (`JPY=X`)는 여전히 다른 종목이다.
    """
    def canonical(symbol: str) -> str:
        upper = symbol.upper()
        if len(upper) == 5 and upper.endswith("=X") and upper[:3].isalpha():
            return f"USD{upper[:3]}=X"
        return upper

    return canonical(requested) == canonical(responded)


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
    # 파일 이름이 정체성을 만들면 안 된다. 요청한 종목과 다른 종목의 응답이 와도
    # 그대로 저장하면 헤더에는 요청한 이름이 찍히고, 그 뒤로는 아무도 구분할 수
    # 없다. 응답이 종목을 말하지 않으면 대조할 수 없으므로 막지 않는다 — 막는
    # 것은 다르다고 **확인된** 경우다(아래 일봉 판정과 같은 규칙).
    response_symbol = (result.get("meta") or {}).get("symbol")
    if (
        isinstance(response_symbol, str)
        and response_symbol
        and not same_instrument(symbol, response_symbol)
    ):
        fail(
            f"{symbol}: 응답은 {response_symbol} 의 시세다. 다른 종목의 가격을 "
            "요청한 이름으로 저장하지 않는다"
        )
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

    def optional_number(field: str, index: int):
        value = quote_value(field, index)
        return value if _is_number(value) else None

    rows: list[dict] = []
    last_bar_timestamp = None
    for index, timestamp in enumerate(timestamps):
        row_date = datetime.fromtimestamp(timestamp, timezone.utc).date().isoformat()
        close = quote_value("close", index)
        if close is None:
            continue
        # 값이 없는 날은 건너뛰지만(휴장일에도 날짜만 실려 온다), 값이 **있는데
        # 숫자가 아니면** 응답이 깨진 것이다. 그 행만 버리면 마지막 봉이 조용히
        # 하루 전으로 물러나고 헤더의 날짜와 본문이 어긋난다.
        if not _is_number(close):
            fail(
                f"{symbol}: {row_date} 종가가 숫자가 아니다 ({close!r}). "
                "깨진 응답을 저장하지 않는다"
            )
        adjusted_value = (
            adjusted[index] if adjusted and index < len(adjusted) else None
        )
        # 조정종가가 없으면 **비워 둔다.** 예전에는 원가격을 대신 넣었다. 그러면
        # 파일의 모든 행에 조정종가가 있는 것처럼 보여, 읽는 쪽이 원가격을 배당이
        # 반영된 값으로 읽고 `returns_basis: ADJUSTED_CLOSE` 라고 싣는다 — 원가격
        # 100→90, 배당 10 이면 총수익률 0% 가 −10% 로 나갔다. 결측을 메우는 순간
        # 그 값이 무엇이었는지가 사라진다. 비어 있으면 읽는 쪽이 기준을 낮춘다.
        adjusted_close = _stable(adjusted_value) if _is_number(adjusted_value) else None
        # Yahoo 가 주는 close·high·low 는 **응답 시점 기준으로 분할이 이미
        # 반영된** 값이다. MSTR 10:1 분할 직전 종가가 1,246 이 아니라 124.68 로
        # 오고 분할 다음 날과 끊김 없이 이어지며, 배당이 없는 종목은 분할 전
        # 구간에서도 adjusted_close 가 close 와 같다. 그러니 이 계수는 저장된
        # 값을 다시 고치라는 뜻이 아니다.
        #
        # 계수의 뜻은 하나뿐이다 — `저장된 값 = 그날 실제 호가 × 이 계수`.
        # 그래서 쓰는 곳도 하나다. **그때의 호가로 적힌 값**(과거 판단의 손절가,
        # 그때 물어본 가격)을 이 시계열과 맞출 때 그 값 쪽에 곱한다.
        # 시계열 쪽에 곱하면 두 번 조정된다 (`cio/memory.py:_first_trigger`).
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
                "high": optional_number("high", index),
                "low": optional_number("low", index),
                "volume": optional_number("volume", index),
            }
        )
        last_bar_timestamp = timestamp
    if not rows:
        fail(f"{symbol}: 유효한 가격 행이 없다")
    # 시계가 틀렸거나 응답이 틀렸다. 어느 쪽이든 오늘보다 뒤의 봉은 관측이 아니다.
    horizon = datetime.now(timezone.utc).date() + timedelta(days=1)
    if date.fromisoformat(rows[-1]["date"]) > horizon:
        fail(
            f"{symbol}: 마지막 봉 {rows[-1]['date']} 이 오늘보다 뒤다. "
            "미래 날짜의 시세를 저장하지 않는다"
        )

    # Yahoo 는 interval=1d 로 요청해도 range 가 크면 조용히 봉을 굵게 만든다
    # (range=max → dataGranularity=1mo). 그대로 저장하면 2,447행짜리 일봉
    # 파일이 320행짜리 월봉으로 덮이는데 헤더에는 그 사실이 남지 않는다.
    # 실제로 그랬고, 그 뒤의 변동성·이동평균·급변 판정은 전부 월봉 위에서
    # 조용히 계산된다. 저장 전에 여기서 멈춰야 파일이 오염되지 않는다.
    meta = dict(result.get("meta") or {})
    meta["_collector_last_bar_timestamp"] = last_bar_timestamp
    actual = granularity(meta, rows)
    if actual not in (DAILY_GRANULARITY, "UNKNOWN"):
        fail(
            f"{symbol}: 일봉을 요청했는데 {actual} 봉이 왔다 (range={range_name}). "
            "Yahoo 는 긴 range 에서 interval 을 조용히 낮춘다 — 저장하지 않고 "
            "멈춘다. 긴 이력이 필요하면 --range 10y 를 쓴다"
        )
    return meta, rows


def _existing_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        with path.open(encoding="utf-8-sig") as handle:
            return list(
                csv.DictReader(
                    line for line in handle if not line.lstrip().startswith("#")
                )
            )
    except OSError:
        return []


def _unsettled_date(path: Path) -> str | None:
    """저장된 파일의 마지막 봉이 미확정이면 그 날짜.

    미확정 봉은 장중 수집분이라 다시 받으면 값이 **당연히** 다르다. 이것을
    겹침 대조에 넣으면 재수집마다 "분할이 있었다"로 오판한다. 실제로 그렇게
    잘못 만들었고, 첫 실행에서 2026-08-10 하나 때문에 병합이 거부돼 이력이
    그대로 날아갔다.
    """
    if not path.exists():
        return None
    try:
        with path.open(encoding="utf-8-sig") as handle:
            for line in handle:
                if not line.lstrip().startswith("#"):
                    return None
                parts = line.strip().split()
                if len(parts) >= 5 and parts[1] == "last_bar":
                    return parts[2] if parts[-1] == "UNSETTLED" else None
    except OSError:
        return None
    return None


def _same_bar(left: dict, right: dict) -> bool:
    """겹치는 날의 봉이 같은 기준의 같은 사실을 말하는가.

    `close` 는 **수집 시점 기준으로 분할이 반영된** 값이라(위 `fetch` 의 설명)
    분할도 정정도 없으면 변하지 않고, 있으면 `split_adjustment_factor` 와 함께
    변한다. 그래서 둘이 맞으면 같은 기준의 시계열이고, 어긋나면 저장된 옛 행이
    옛 기준이라는 뜻이라 이어 붙일 수 없다. `adjusted_close` 는 재수집마다
    말단 자릿수가 흔들려(96241.3 vs 96241.2) 판정에 쓰지 않는다.
    """
    for key, tolerance in (("close", 1e-6), ("split_adjustment_factor", 1e-9)):
        try:
            a, b = float(left[key]), float(right[key])
        except (KeyError, TypeError, ValueError):
            return False
        if abs(a - b) > tolerance * max(1.0, abs(a)):
            return False
    return True


MISSING_SESSIONS_PREFIX = "# missing_sessions "
BACKFILLED_PREFIX = "# backfilled "
# 기록이 끝없이 자라지 않게 한다. 오래 메워지지 않은 구멍은 packet 의 최근
# 세션 대조에서 이미 벗어났고, 남는 것은 가장 최근 것들이다.
MAX_RECORDED_MISSING_SESSIONS = 20
# 옛 행을 이어 붙이려면 값이 있어야 하는 열. 거래량은 뺀다 — KRX 로 메운
# 지수 행은 거래량 단위가 Yahoo 와 달라 일부러 비워 둔다(`collectors/krx.py`).
# 조정종가도 뺀다 — 출처가 주지 않으면 비워 두는 열이고(`fetch`), 그 봉의 종가와
# 고가·저가는 여전히 관측이라 조정종가가 없다는 이유로 이력을 버리지 않는다.
CARRIED_ROW_FIELDS = tuple(
    field for field in FIELDS if field not in ("volume", "adjusted_close")
)


def _header_notes(path: Path, prefix: str) -> tuple[list[str], str]:
    """머리말에서 `prefix 날짜,날짜 / 설명` 줄을 읽는다. (날짜들, 설명)."""
    if not path.exists():
        return [], ""
    try:
        with path.open(encoding="utf-8-sig") as handle:
            for line in handle:
                if not line.lstrip().startswith("#"):
                    break
                if line.startswith(prefix):
                    body = line[len(prefix):].strip()
                    dates, _, tail = body.partition(" / ")
                    return [d for d in dates.split(",") if d], tail
    except OSError:
        return [], ""
    return [], ""


def missing_sessions(path: Path) -> list[str]:
    """봉이 한 번 저장됐다가 확정값으로 재확인되지 못하고 사라진 거래일.

    그 날짜에 봉이 있었다는 것은 그날 거래가 있었다는 뜻이다. 값은 믿을 수
    없어 남기지 않지만 **세션이 있었다는 사실**은 남긴다. 이것이 없으면 같은
    날을 모든 시계열이 함께 잃었을 때 구멍을 볼 방법이 없다.
    """
    return _header_notes(path, MISSING_SESSIONS_PREFIX)[0]


def merge_with_existing(
    path: Path, rows: list[dict]
) -> tuple[list[dict], list[str], list[str]]:
    """이미 있는 봉을 보존한 채 새 수집분을 얹는다. (행, 알림, 사라진 세션).

    수집은 `--range` 만큼만 받아 오는데 저장이 통째로 덮어쓰기였다. 그래서
    `refresh` 가 고정으로 쓰는 `5y` 때문에 파일이 **롤링 5년 창**이 됐고,
    돌릴 때마다 가장 오래된 하루가 조용히 떨어졌다. 실제로 000660 은
    2016-08-08 부터 2,448 일이 있었는데 한 번의 refresh 로 1,220 일
    (2021-08-10 부터) 이 됐다 — 1,229 일이 사라졌고 아무 경고도 없었다.

    가격 이력은 재수집으로 복구되는 것처럼 보이지만 그렇지 않다. 이 저장소는
    3년 실측 PER 밴드(`per_band_actual`)·12개월 수익률·고점 대비 낙폭을 전부
    여기서 뽑고, 무엇보다 **과거 판단의 채점**이 이 시계열 위에 선다. 창이
    깎이면 track record 가 앞에서부터 지워진다.

    **응답에 없는 날짜의 봉도 지우지 않는다.** 처음 고칠 때는 수집 범위보다
    앞선 행만 보존했다. 그런데 Yahoo 는 범위 **안의** 하루를 빼고도 정상 응답을
    준다 — 2026-10-13 수집에서 214450.KQ 와 ^KQ11 의 2026-10-12 가 그렇게
    사라졌고 packet 의 세션 수익률이 한 세션을 건너뛰었다. 응답에 없다는 것은
    "그날이 없었다"가 아니라 "이번에 안 실었다"다.

    **저장된 마지막 봉만은 잠정값으로 다룬다.** 다음 수집이 다시 실어 준 적이
    없는 유일한 봉이다. 장중 수집분이면 종가가 아니고, 헤더에 `SETTLED` 로
    적혔어도 장 마감 직후의 값은 뒤에 바뀐다(같은 날 ^KS11 의 10-12 는
    6,399.43 → 6,384.24). 그래서 (1) 분할 판정의 대조에서 빼고 — 그 하루의
    정정을 분할로 읽어 40거래일을 버린 적이 있다 — (2) 응답에 없으면 값을
    보존하지 않고 날짜만 `missing_sessions` 로 남긴다. 장중 값을 종가로 굳히는
    것보다 비어 있다고 말하는 쪽이 낫고, 한국 시계열은 `collectors.krx` 가 그
    날짜를 거래소 원문으로 메운다.

    **겹치는 구간이 어긋나면 병합하지 않는다.** 액면분할은 그 이전의 모든
    봉을 바꾸므로 마지막 봉 하나를 빼도 드러난다. 그때 옛 행을 남기면 두 기준이
    한 파일에 섞인다. 새 수집분만 쓰고 사실을 알린다 — 조용히 고르지 않는다.
    """
    previous = _existing_rows(path)
    if not previous or not rows:
        return rows, [], []
    fresh = {row["date"]: row for row in rows}
    last_stored = previous[-1].get("date")
    # 이번 수집의 마지막 봉도 장중일 수 있어 같이 뺀다.
    volatile = {rows[-1]["date"], last_stored}
    overlap = [
        row
        for row in previous
        if row.get("date") in fresh and row["date"] not in volatile
    ]
    if not overlap:
        # 겹치는 날이 하나도 없으면 이어 붙일 근거가 없다. 잘못 이으면
        # 사이가 빈 시계열이 되고 수익률이 조용히 틀린다.
        return rows, [], []
    mismatched = [
        row["date"] for row in overlap if not _same_bar(row, fresh[row["date"]])
    ]
    if mismatched:
        return rows, [
            f"겹치는 {len(overlap)}일 중 {len(mismatched)}일의 원가격·분할계수가 "
            f"다르다 (예: {mismatched[0]}). 액면분할이나 정정으로 보이며 "
            "옛 이력을 이어 붙이지 않았다 — 두 기준이 섞이면 수익률이 틀린다"
        ], []

    notes: list[str] = []
    was_unsettled = _unsettled_date(path) == last_stored
    earliest = rows[0]["date"]
    older: list[dict] = []
    holes: list[dict] = []
    dropped_unconfirmed = None
    for row in previous:
        day = row.get("date")
        if not day or day in fresh:
            continue
        if day == last_stored:
            dropped_unconfirmed = day
            continue
        if any(row.get(field) in (None, "") for field in CARRIED_ROW_FIELDS):
            continue
        (older if day < earliest else holes).append(row)

    if older:
        notes.append(
            f"기존 이력 {len(older)}일을 보존했다 ({older[0]['date']} 부터). "
            f"이번 수집은 {earliest} 부터라 그대로 저장하면 그만큼 잘린다"
        )
    if holes:
        days = ", ".join(row["date"] for row in holes[:6])
        notes.append(
            f"이번 응답에 없는 거래일 {len(holes)}일의 봉을 보존했다 "
            f"({days}{' 외' if len(holes) > 6 else ''}) — 응답이 날짜를 뺀 것이지 "
            "그날이 없었던 것이 아니다"
        )
    if last_stored in fresh and not was_unsettled and not _same_bar(
        previous[-1], fresh[last_stored]
    ):
        notes.append(
            f"확정으로 적혀 있던 마지막 봉 {last_stored} 의 종가가 "
            f"{previous[-1].get('close')} → {fresh[last_stored]['close']} 로 바뀌었다. "
            "새 값으로 바꿨다 — 장 마감 직후의 값은 확정이 아니었다"
        )

    merged = sorted(older + holes + rows, key=lambda row: row["date"])
    present = {row["date"] for row in merged}
    missing = [
        day
        for day in sorted({*missing_sessions(path), *filter(None, [dropped_unconfirmed])})
        if day not in present
    ][-MAX_RECORDED_MISSING_SESSIONS:]
    if dropped_unconfirmed:
        notes.append(
            f"저장돼 있던 마지막 봉 {dropped_unconfirmed} 이 이번 응답에 없다. "
            + ("장중 수집분이라" if was_unsettled else "재확인된 적 없는 값이라")
            + " 종가로 보존하지 않고 사라진 세션으로 기록했다 (한국 종목·지수면 "
            "collectors.krx 가 거래소 원문으로 메운다)"
        )
    return merged, notes, missing


def _carried_backfill_line(path: Path, rows: list[dict], fresh: set[str]) -> str | None:
    """KRX 로 메운 봉이 이번 저장에도 남아 있으면 그 출처 표기를 이어 쓴다.

    예전에는 수집할 때마다 메운 봉이 지워졌으니 표기도 같이 사라지는 것이
    맞았다. 이제는 봉이 남으므로 표기가 사라지면 Yahoo 값처럼 읽힌다.
    """
    dates, tail = _header_notes(path, BACKFILLED_PREFIX)
    present = {row["date"] for row in rows}
    kept = [day for day in dates if day in present and day not in fresh]
    if not kept:
        return None
    return f"{BACKFILLED_PREFIX}{','.join(kept)}" + (f" / {tail}" if tail else "")


def save(
    symbol: str, meta: dict, rows: list[dict], *, merge: bool = True
) -> Path:
    PRICE_DIR.mkdir(parents=True, exist_ok=True)
    path = PRICE_DIR / f"{safe_name(symbol)}.csv"
    missing: list[str] = []
    backfill_line = None
    if merge:
        fetched = {row["date"] for row in rows}
        rows, notes, missing = merge_with_existing(path, rows)
        backfill_line = _carried_backfill_line(path, rows, fetched)
        for note in notes:
            print(f"※ {note}", file=sys.stderr)
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
            if type(meta.get("_collector_last_bar_timestamp")) is int:
                temporary.write(
                    f"# last_bar_timestamp {meta['_collector_last_bar_timestamp']} / source Yahoo timestamp\n"
                )
            # 여기부터는 있을 때만 쓰는 줄이다. 위 네 줄은 packet 조립이 자리로
            # 읽으므로(`cio/context.py:_price_summary`) 그 사이에 끼우지 않는다.
            if backfill_line:
                temporary.write(backfill_line + "\n")
            if missing:
                temporary.write(
                    f"{MISSING_SESSIONS_PREFIX}{','.join(missing)} / 봉이 저장됐다가 "
                    "응답에서 사라졌고 확정값으로 재확인되지 않았다\n"
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
    fetched = len(rows)
    unadjusted = [row["date"] for row in rows if row["adjusted_close"] is None]
    if unadjusted:
        print(
            f"※ 조정종가가 없는 봉 {len(unadjusted)}일 "
            f"({unadjusted[0]} ~ {unadjusted[-1]}) — 원가격으로 채우지 않고 비워 "
            "뒀다. 그 봉이 걸친 수익률은 배당이 빠진 가격 수익률로 실린다",
            file=sys.stderr,
        )
    path = save(args.symbol, meta, rows)
    # 저장된 파일을 그대로 읽어 보고한다. 받아 온 행수를 그대로 찍으면
    # 병합으로 보존한 이력이 화면에서 사라지고, 사용자는 파일이 잘린 줄 안다.
    saved = _existing_rows(path)
    print(
        f"{args.symbol} {meta.get('currency') or 'UNKNOWN'} {len(saved)}일 "
        f"{saved[0]['date']} ~ {saved[-1]['date']}"
        + (f" (이번 수집 {fetched}일)" if len(saved) != fetched else "")
    )
    if state != "SETTLED":
        certainty = "확정 종가가 아니다" if state == "UNSETTLED" else "확정 여부를 확인할 수 없다"
        print(
            f"※ 마지막 봉 {rows[-1]['date']} 은 {state} — {certainty}. "
            "거래량 배수와 급변 판정에서 제외된다"
        )
    print(f"저장: {path.relative_to(ROOT)}")


def band(rows: list[dict], symbol: str, cost: float | None = None) -> dict:
    """구간 내 위치와 고점 대비 낙폭.

    렌더링에서 떼어 낸 이유는 이 값들이 판단에 그대로 인용되기 때문이다 —
    "52주 고점 대비 -51.3%" 같은 문장이 화면에서 바로 나온다. print 안에서만
    계산되면 저장되지도 검증되지도 않는다.

    고가와 저가가 같은 구간(거래 정지·상장 직후)에서는 위치를 **내지 않는다.**
    0 이나 100 은 "바닥"·"천장"이라는 없는 정보를 만들고, 50 도 마찬가지로
    "구간 중간"이라는 없는 정보를 만든다. 예전에는 50 을 넣으면서 그것을
    "지어내지 않는 것"이라 적어 뒀는데, 세 값 다 지어낸 것이고 답은 넷째다 —
    범위가 없으면 위치도 없다.
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
            round((current - low) / (high - low) * 100, 2) if high > low else None
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
