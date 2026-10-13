"""환율 일봉을 결산 분기에 맞춰 접는다. 산술만 하고 판정하지 않는다.

원화로 보고하는 수출 회사의 매출은 **분기 평균** 환율로, 외화 화폐성 자산은
**기말** 환율로 환산된다. 그래서 한 분기에 대해 두 숫자가 다 필요하고, 그 분기가
이미 실적에 들어 있는지 아닌지가 함께 있어야 한다 — 끝났지만 아직 발표되지 않은
분기의 환율 변화는 재무 숫자 어디에도 없다.

2026-10-06 판단은 수출 비중 47% 회사에서 이 값을 통째로 빠뜨렸다. packet 에
환율이 없었고, 일주일 뒤 모델이 스크래치 스크립트로 분기 평균을 직접 냈다.
추론 시점의 산술은 재현되지 않고 기록에도 남지 않는다.

이 모듈은 환율이 실적을 얼마나 움직이는지 말하지 않는다. 수출 비중·통화 구성·
헤지·이익 전이율은 회사마다 다르고 그것을 정하는 것은 모델이다.
"""

from __future__ import annotations

import calendar
import datetime as dt
from typing import Any

# 한 분기는 대략 62~65 거래일이다. 절반도 안 되는 봉으로 낸 평균은 분기 평균이
# 아니라 그 분기 일부의 평균이므로 내지 않는다.
MIN_BARS_PER_QUARTER = 32


def _shift_months(day: dt.date, months: int) -> dt.date:
    index = day.year * 12 + (day.month - 1) + months
    year, month = divmod(index, 12)
    month += 1
    return dt.date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def quarter_start(end: dt.date) -> dt.date:
    """기말이 `end` 인 석 달 분기의 첫날."""
    return _shift_months(end + dt.timedelta(days=1), -3)


def _pct(new: float | None, old: float | None) -> float | None:
    if new is None or old is None or old <= 0:
        return None
    return round((new / old - 1) * 100, 2)


def _stats(series: list[tuple[str, float]], end: dt.date) -> dict[str, Any] | None:
    start = quarter_start(end).isoformat()
    closes = [close for day, close in series if start <= day <= end.isoformat()]
    if len(closes) < MIN_BARS_PER_QUARTER:
        return None
    return {
        "average": round(sum(closes) / len(closes), 1),
        "period_end": round(closes[-1], 1),
        "bars": len(closes),
    }


def quarter_table(
    series: list[tuple[str, float]],
    quarter_ends: list[str],
    *,
    reported_through: str | None,
) -> dict[str, Any] | None:
    """분기별 평균·기말 환율과 직전 분기·전년 동기 대비 변화율.

    `series` 는 날짜순 (날짜, 종가). `quarter_ends` 는 표에 올릴 분기의 기말
    날짜이고, 아직 끝나지 않은 분기와 봉이 모자란 분기는 뺀다. `reported_through`
    이후에 끝난 분기는 `reported: false` 다 — 그 분기의 실적이 아직 재무 숫자에
    없다는 뜻이다.

    마지막 봉은 장중일 수 있다. `latest` 는 수집 시점의 값이다.
    """
    series = sorted((day, close) for day, close in series if close and close > 0)
    if not series:
        return None
    latest_day, latest_close = series[-1]
    rows: list[dict[str, Any]] = []
    previous_end: dt.date | None = None
    previous_stats: dict[str, Any] | None = None
    for text in sorted(set(quarter_ends)):
        end = dt.date.fromisoformat(text)
        if text > latest_day:
            continue
        stats = _stats(series, end)
        if stats is None:
            previous_end, previous_stats = None, None
            continue
        row: dict[str, Any] = {"end": text, **stats}
        # 표에서 바로 앞 줄이 실제로 직전 분기일 때만 전분기 대비를 낸다.
        if previous_stats and previous_end == quarter_start(end) - dt.timedelta(days=1):
            row["average_qoq_pct"] = _pct(stats["average"], previous_stats["average"])
            row["period_end_qoq_pct"] = _pct(
                stats["period_end"], previous_stats["period_end"])
        year_ago = _stats(series, _shift_months(end, -12))
        if year_ago:
            row["average_yoy_pct"] = _pct(stats["average"], year_ago["average"])
        if reported_through is not None:
            row["reported"] = text <= reported_through
        rows.append(row)
        previous_end, previous_stats = end, stats
    if not rows:
        return None
    reported = [row for row in rows if row.get("reported")]
    basis = reported[-1] if reported else None
    return {
        "latest": {"date": latest_day, "close": round(latest_close, 1)},
        "quarters": rows,
        "latest_vs_last_reported_quarter": (
            {
                "end": basis["end"],
                "vs_average_pct": _pct(latest_close, basis["average"]),
                "vs_period_end_pct": _pct(latest_close, basis["period_end"]),
            }
            if basis
            else None
        ),
    }
