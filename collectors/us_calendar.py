#!/usr/bin/env python3
"""미국 자산의 다음 정기보고서 제출일을 추정해 실적 캘린더를 만든다.

한국은 회사가 `결산실적공시예고`로 발표일을 미리 공시하므로 원문을 읽으면
날짜가 나온다(`collectors/kr_calendar.py`). 미국 회사도 IR에서 발표일을 예고하지만
SEC 제출 색인만으로는 그 일정을 알 수 없다. 과거 (결산기말 → 10-Q/10-K 제출)
간격을 재고, 다음 결산기말에 그 간격을 더한다.

법정 기한은 제출자 규모에 따라 다르고(대형가속제출자 10-Q 40일·10-K 60일,
그 아래는 45일·75일) 회사마다 실제 제출은 그보다 이르다. 규정을 가정하지 않고
**이 회사가 실제로 지킨 간격**을 쓴다.

10-Q 와 10-K 는 기한이 다르므로 부류를 나눈다 — kr_calendar 와 같은 이유다.

사용:
    python3.13 -m collectors.us_calendar RXRX
    python3.13 -m collectors.us_calendar RXRX --show
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent

from collectors.kr_calendar import (
    SCHEMA_VERSION, _median, business_days_until, fail, load, save,
)

FILING_DIR = ROOT / "knowledge" / "filings"

PERIODIC_CLASS = {"10-Q": "INTERIM", "10-K": "ANNUAL"}
# 결산기말 → 제출까지 이보다 오래 걸린 것은 정정·지연 제출로 보고 표본에서 뺀다.
MAX_LAG_DAYS = 150


def _date(value: object) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None


def _quarter_end_after(period_end: dt.date) -> dt.date:
    """월말 결산의 다음 분기말. 현재 결산월에서 세 달을 이동한다."""
    month = period_end.month + 3
    year = period_end.year + (month - 1) // 12
    month = (month - 1) % 12 + 1
    last = [31, 29 if year % 4 == 0 and (year % 100 or not year % 400)
            else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
    return dt.date(year, month, last)


def filing_lag(filings: list[dict]) -> dict[str, object]:
    """과거 (결산기말 → 제출) 간격을 부류별로 관측한다."""
    observations = []
    seen = set()
    for row in sorted(filings, key=lambda item: str(item.get("filing_date") or "")):
        report_class = PERIODIC_CLASS.get(str(row.get("form")))
        period_end = _date(row.get("report_date"))
        filed = _date(row.get("filing_date"))
        if not report_class or period_end is None or filed is None:
            continue
        lag = (filed - period_end).days
        if lag <= 0 or lag > MAX_LAG_DAYS:
            continue
        # 같은 결산기말의 중복 행은 별도 관측이 아니다. 최초 제출만 센다.
        key = (row["form"], period_end)
        if key in seen:
            continue
        seen.add(key)
        observations.append({
            "period_end": period_end.isoformat(),
            "filing_date": filed.isoformat(),
            "form": row["form"],
            "report_class": report_class,
            "lag_days": lag,
        })
    by_class: dict[str, list[float]] = {}
    for item in observations:
        by_class.setdefault(str(item["report_class"]), []).append(
            float(item["lag_days"])
        )
    return {
        "observation_count": len(observations),
        "observations": observations[-6:],
        "median_lag_days_by_class": {
            name: _median(values) for name, values in sorted(by_class.items())
        },
        "sample_size_by_class": {
            name: len(values) for name, values in sorted(by_class.items())
        },
        "_note": (
            "회사 IR의 발표 예정일과 별개다. 과거 (결산기말 → 10-Q/10-K "
            "제출) 간격의 중앙값이며 추정이다. 10-Q(INTERIM)와 10-K(ANNUAL)는 "
            "법정 기한이 달라 따로 센다. 관측이 2건 미만인 부류는 내지 않는다"
        ),
    }


def build(ticker: str, today: dt.date | None = None) -> dict:
    today = today or dt.datetime.now(ZoneInfo("America/New_York")).date()
    index_path = FILING_DIR / f"{ticker}.json"
    if not index_path.exists():
        fail(
            f"{index_path.relative_to(ROOT)} 가 없다. 먼저 공시 색인을 모은다: "
            f"python3.13 -m collectors.edgar filings {ticker}"
        )
    index = json.loads(index_path.read_text(encoding="utf-8"))
    filings = [row for row in (index.get("filings") or [])
               if (_date(row.get("filing_date")) or dt.date.max) <= today]
    lag = filing_lag(filings)

    reported = [
        _date(row.get("report_date"))
        for row in filings
        if row.get("form") in PERIODIC_CLASS and _date(row.get("report_date"))
    ]
    latest_reported = max(reported) if reported else None
    expected_date = None
    report_class = None
    median_lag = None
    next_period_end = None
    missing_reason = None
    if latest_reported:
        annual_ends = sorted(
            _date(row["report_date"]) for row in filings
            if row.get("form") == "10-K" and _date(row.get("report_date"))
        )
        month_end = (latest_reported + dt.timedelta(days=1)).month != latest_reported.month
        annual_month_end = bool(annual_ends) and (
            (annual_ends[-1] + dt.timedelta(days=1)).month != annual_ends[-1].month
        )
        if not annual_ends:
            missing_reason = "10-K 결산기말이 없어 다음 보고서의 연간/분기 구분을 알 수 없다"
        elif not month_end or not annual_month_end:
            missing_reason = "월말 결산이 아닌 52/53주 등 회계달력은 자동 추정하지 않는다"
        else:
            next_period_end = _quarter_end_after(latest_reported)
            report_class = (
                "ANNUAL" if next_period_end.month == annual_ends[-1].month else "INTERIM"
            )
            median_lag = (lag["median_lag_days_by_class"] or {}).get(report_class)
            if median_lag is not None:
                expected_date = (
                    next_period_end + dt.timedelta(days=round(float(median_lag)))
                ).isoformat()

    return {
        "schema_version": SCHEMA_VERSION,
        "asset_id": ticker,
        "collected_at": dt.datetime.now().astimezone().isoformat(
            timespec="seconds"
        ),
        "as_of_date": today.isoformat(),
        "source": "SEC_EDGAR_SUBMISSIONS",
        "source_grade": "PRIMARY",
        # 제출 색인에는 회사 IR의 실적발표 일정이 없으므로 미확인으로 둔다.
        "next_provisional_result": None,
        "next_provisional_result_missing_reason": (
            "SEC 제출 색인만으로는 실적 발표 예정일을 확인할 수 없다. "
            "회사가 IR 보도자료·이벤트 페이지에서 사전 공지했는지 별도 확인해야 한다"
        ),
        "expected_periodic_report": (
            {
                "date": expected_date,
                "estimated": True,
                "report_class": report_class,
                "period_end": (
                    next_period_end.isoformat() if next_period_end else None
                ),
                "basis": (
                    f"과거 {report_class} 보고서의 (결산기말 → 제출) 간격 "
                    f"중앙값 {median_lag}일"
                ),
                "sample_size": (lag["sample_size_by_class"] or {}).get(
                    report_class
                ),
                "business_days_remaining": business_days_until(
                    expected_date, today
                ),
                "_content": (
                    "10-Q/10-K 본문이다. 현금·번레이트·희석주식수가 여기서 "
                    "갱신된다. 실적 요약은 보통 같은 날 8-K(Item 2.02)로 먼저 "
                    "나온다"
                ),
            }
            if expected_date else None
        ),
        "expected_periodic_report_missing_reason": (
            None
            if expected_date
            else missing_reason or (
                f"{report_class or '알 수 없는'} 부류의 과거 관측이 2건 "
                "미만이라 제출일을 추정하지 않는다"
            )
        ),
        "periodic_report_lag": lag,
        "business_day_basis": (
            "주말만 제외한 근사다. 미국 휴장일은 반영하지 않았으므로 실제 "
            "영업일 수는 이 값 이하다"
        ),
        "_note": (
            "추정일이며 회사가 언제든 앞뒤로 옮길 수 있다. 지연 제출은 NT 10-Q/"
            "NT 10-K 로 별도 공시되며, 그 서식이 보이면 이 추정은 무효다"
        ),
        "_not_a_judgment": (
            "이벤트까지 남은 날짜는 측정치다. 이벤트 전에 사지 말라는 지시가 "
            "아니라, 판단이 이벤트와 어디에 놓였는지 보이게 하는 값이다"
        ),
    }


def render(packet: dict) -> None:
    print("=" * 74)
    print(f"실적 캘린더 — {packet['asset_id']}   기준 {packet['as_of_date']}")
    print("=" * 74)
    print(f"발표 예고     없음 — {packet['next_provisional_result_missing_reason']}")
    report = packet.get("expected_periodic_report")
    if report:
        days = report.get("business_days_remaining")
        left = f"{days}영업일 뒤" if days is not None else "지났거나 알 수 없음"
        print(f"\n정기보고서 예상   {report['date']}  ({left})  "
              f"※ 추정 (표본 {report.get('sample_size')}건)")
        print(f"  결산기말        {report.get('period_end')}")
        print(f"  근거            {report['basis']}")
        print(f"  담는 것         {report['_content']}")
    elif packet.get("expected_periodic_report_missing_reason"):
        print(f"\n정기보고서 예상   없음 — "
              f"{packet['expected_periodic_report_missing_reason']}")
    lag = packet.get("periodic_report_lag") or {}
    if lag.get("observations"):
        medians = lag.get("median_lag_days_by_class") or {}
        summary = ", ".join(f"{k} {v}일" for k, v in medians.items())
        print(f"\n과거 간격 관측 {lag['observation_count']}건  ({summary})")
        for item in lag["observations"]:
            print(f"  {item['period_end']} 기말 → {item['filing_date']} "
                  f"{item['form']} ({item['lag_days']}일, {item['report_class']})")
    print("-" * 74)
    print(f"※ {packet['_note']}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="미국 정기보고서 제출일 추정 (SEC submissions 기반)"
    )
    parser.add_argument("ticker")
    parser.add_argument("--show", action="store_true")
    args = parser.parse_args()
    ticker = args.ticker.upper()

    if args.show:
        render(load(ticker))
        return
    packet = build(ticker)
    path = save(ticker, packet)
    render(packet)
    print(f"\n저장: {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
