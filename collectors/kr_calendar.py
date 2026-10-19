#!/usr/bin/env python3
"""결산실적 공시예고에서 다음 실적 발표일을 읽어 실적 캘린더를 만든다.

공시 색인은 `"description": "결산실적공시예고"` 라는 **제목만** 갖는다. 정작
판단을 바꾸는 값 — 공시예정일 — 은 원문 본문에만 있어서, 이벤트까지 며칠
남았는지 알려면 매번 원문을 열어야 했다. 이벤트 전에 살 것인가는 매수 판단의
1차 분기점인데 그 입력이 패킷에 없었다.

잠정실적(공정공시)과 정기보고서는 다른 날 나온다. 잠정은 요약 손익이 중심이고
재고자산·현금흐름은 정기보고서에만 있다(부문별 매출은 회사마다 자리가 다르다 —
파마리서치는 잠정 본문에만 적는다). 재고가 미해결 위험인 종목에서
잠정실적만 보고 판단하면 정작 확인하려던 것을 못 본 채 결론을 낸다. 그래서
두 날짜를 따로 낸다.

정기보고서 예정일은 공시되지 않는다. 과거 (잠정 → 그 다음 정기보고서) 간격의
중앙값으로 추정하며, 관측이 2건 미만이면 내지 않는다. 추정은 추정이라고
표시한다.

사용:
    python -m collectors.kr_calendar 214450
    python -m collectors.kr_calendar 214450 --show
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, NoReturn

ROOT = Path(__file__).resolve().parent.parent

from collectors.dart import (
    DART_VIEWER, FILING_DIR, fetch_document, load_key,
)

OUT_DIR = ROOT / "knowledge" / "calendar"
SCHEMA_VERSION = "earnings-calendar/1.0.0"

NOTICE_KEYWORDS = ("결산실적공시예고", "결산실적 공시예고")

# 정기보고서 제출 기한은 분기·반기가 기간말 +45일, 사업보고서가 +90일이다.
# 둘을 같은 모집단에 넣고 중앙값을 내면 서로 다른 규칙의 평균이 된다.
PERIODIC_CLASS = {
    "QUARTER_REPORT": "INTERIM",
    "HALF_REPORT": "INTERIM",
    "ANNUAL_REPORT": "ANNUAL",
}
# 결산대상기간 종료월 → 이번에 나올 정기보고서의 부류
PERIOD_END_CLASS = {3: "INTERIM", 6: "INTERIM", 9: "INTERIM", 12: "ANNUAL"}
# 짝짓기 상한. 사업보고서(+90일)까지 담되 다음 결산까지 넘어가지는 않는다.
MAX_PAIR_DAYS = 120

TAG = re.compile(r"<[^>]+>")
SPACE = re.compile(r"\s+")
ISO_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
KO_DATE = re.compile(r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일")

# 원문이 스스로 확정이 아니라고 말한다. 그 문장을 계약에 남긴다 — 이 날짜를
# 확정으로 취급하면 안 되는 이유가 추론이 아니라 공시 본문에 있다.
PROVISIONAL_MARK = "공시예정일은 당사의 사정에 따라 변경될 수 있습니다"


def fail(msg: str) -> NoReturn:
    print(f"오류: {msg}", file=sys.stderr)
    raise SystemExit(1)


def strip_markup(text: str) -> str:
    return SPACE.sub(" ", TAG.sub(" ", text)).strip()


def _read_date(text: str) -> str | None:
    match = ISO_DATE.search(text)
    if match:
        year, month, day = (int(g) for g in match.groups())
    else:
        match = KO_DATE.search(text)
        if not match:
            return None
        year, month, day = (int(g) for g in match.groups())
    try:
        return dt.date(year, month, day).isoformat()
    except ValueError:
        return None


def _after(flat: str, label: re.Pattern[str], window: int = 120) -> str | None:
    """라벨 뒤 창에서 첫 날짜를 읽는다.

    공시 서식은 표라서 라벨과 값이 인접한 셀에 있다. 태그를 벗기면 그 인접성이
    문자열 거리로 남는다. 창을 넘겨 멀리서 주워오지 않도록 길이를 제한한다.
    """
    match = label.search(flat)
    if not match:
        return None
    return _read_date(flat[match.end():match.end() + window])


def parse_notice(documents: dict[str, str]) -> dict[str, object]:
    """결산실적 공시예고 원문에서 대상기간과 공시예정일을 읽는다."""
    flat = " ".join(strip_markup(text) for text in documents.values())
    disclosure_date = _after(
        flat, re.compile(r"결산실적\s*공시\s*예정일")
    )
    if not disclosure_date:
        raise LookupError("원문에서 '결산실적 공시예정일' 을 찾지 못했다")
    period_start = _after(flat, re.compile(r"결산대상기간[^가-힣]*시작일"), 60)
    period_end = _after(flat, re.compile(r"종료일"), 60)
    return {
        "disclosure_date": disclosure_date,
        "period_start": period_start,
        "period_end": period_end,
        "is_confirmed": False,
        "source_caveat": (
            PROVISIONAL_MARK
            if PROVISIONAL_MARK.replace(" ", "") in flat.replace(" ", "")
            else None
        ),
    }


def business_days_until(target: str, today: dt.date) -> int | None:
    """오늘부터 대상일까지 남은 영업일.

    주말만 제외한 근사다. 한국 공휴일은 반영하지 않으므로 실제 영업일은 이
    값 이하다. 며칠 남았는지의 자릿수를 보려는 값이지 정확한 거래일 수가 아니다.
    """
    try:
        end = dt.date.fromisoformat(target)
    except ValueError:
        return None
    if end < today:
        return None
    days = 0
    cursor = today
    while cursor < end:
        cursor += dt.timedelta(days=1)
        if cursor.weekday() < 5:
            days += 1
    return days


def _median(values: list[float]) -> float | None:
    """관측 2건 미만이면 내지 않는다. 한 번 본 것은 규칙성이 아니다."""
    if len(values) < 2:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def periodic_lag(filings: list[dict]) -> dict[str, Any]:
    """과거 (잠정실적 → 그 다음 정기보고서) 간격을 부류별로 관측한다.

    정기보고서 제출일은 공시되지 않는다. 유일하게 쓸 수 있는 규칙성은 이
    회사가 잠정실적을 낸 뒤 며칠 만에 정기보고서를 냈는가이고, 그것은 과거
    공시 색인에 이미 들어 있다.

    분기·반기(기간말 +45일)와 사업보고서(+90일)는 법정 기한이 다르므로 따로
    센다. 섞으면 서로 다른 규칙의 평균이 나오고, 그 값은 어느 쪽도 예측하지
    못한다.
    """
    def as_date(row: dict) -> dt.date | None:
        try:
            return dt.date.fromisoformat(str(row.get("filing_date")))
        except (ValueError, TypeError):
            return None

    # 잠정실적 쪽이 아니라 **정기보고서 쪽에서** 짝을 찾는다. 회사는 같은 결산에
    # 잠정실적·정정·손익구조변동을 여러 건 내기도 하는데(삼성전자가 그렇다),
    # 잠정 쪽에서 짝지으면 같은 보고서가 관측으로 여러 번 계상돼 표본이
    # 부풀고 중앙값이 흔들린다. 보고서 하나에 관측 하나다.
    provisionals = sorted(
        (date for row in filings
         if row.get("form") == "PROVISIONAL_RESULT"
         and (date := as_date(row)) is not None)
    )
    observations = []
    for row in sorted(filings, key=lambda item: str(item.get("filing_date") or "")):
        report_class = PERIODIC_CLASS.get(str(row.get("form")))
        report_date = as_date(row)
        if not report_class or report_date is None:
            continue
        earlier = [date for date in provisionals if date < report_date]
        if not earlier:
            continue
        provisional = earlier[-1]
        gap = (report_date - provisional).days
        if gap > MAX_PAIR_DAYS:
            # 이 보고서 앞에는 짝지을 잠정실적이 없다. 그 앞 결산의 것이다.
            continue
        observations.append({
            "provisional_date": provisional.isoformat(),
            "report_date": report_date.isoformat(),
            "report_form": row["form"],
            "report_class": report_class,
            "gap_days": gap,
        })
    by_class: dict[str, list[float]] = {}
    for item in observations:
        by_class.setdefault(str(item["report_class"]), []).append(
            float(item["gap_days"])
        )
    return {
        "observation_count": len(observations),
        "observations": observations[-6:],
        "median_gap_days_by_class": {
            name: _median(values) for name, values in sorted(by_class.items())
        },
        "sample_size_by_class": {
            name: len(values) for name, values in sorted(by_class.items())
        },
        "_note": (
            "정기보고서 제출일은 공시되지 않는다. 과거 (잠정실적 → 그 다음 "
            "정기보고서) 간격의 중앙값이며 추정이다. INTERIM(분기·반기, 기간말 "
            "+45일)과 ANNUAL(사업보고서, +90일)은 법정 기한이 달라 따로 센다. "
            "관측이 2건 미만인 부류는 내지 않는다"
        ),
    }


def expected_report_class(period_end: object) -> str | None:
    """결산대상기간 종료월로 이번에 나올 정기보고서의 부류를 정한다."""
    try:
        month = dt.date.fromisoformat(str(period_end)).month
    except (ValueError, TypeError):
        return None
    return PERIOD_END_CLASS.get(month)


def build(stock_code: str, key: str, today: dt.date | None = None) -> dict:
    today = today or dt.date.today()
    index_path = FILING_DIR / f"{stock_code}.json"
    if not index_path.exists():
        fail(
            f"{index_path.relative_to(ROOT)} 가 없다. 먼저 공시 색인을 모은다: "
            f"python -m collectors.dart filings {stock_code} --since 20250101"
        )
    index = json.loads(index_path.read_text(encoding="utf-8"))
    filings = index.get("filings") or []

    notices = [
        row for row in filings
        if any(word in str(row.get("description") or "")
               for word in NOTICE_KEYWORDS)
    ]
    notices.sort(key=lambda row: str(row.get("filing_date") or ""))

    upcoming: dict[str, object] | None = None
    document_id = None
    if notices:
        latest = notices[-1]
        rcept_no = str(latest["accession_number"])
        documents, _ = fetch_document(rcept_no, key)
        try:
            parsed = parse_notice(documents)
        except LookupError as exc:
            fail(f"{rcept_no}: {exc}")
        document_id = f"DARTDOC:{rcept_no}"
        parsed.update({
            "notice_filing_date": latest.get("filing_date"),
            "accession_number": rcept_no,
            "document_url": DART_VIEWER.format(rcept_no=rcept_no),
            "evidence_ref": document_id,
        })
        upcoming = parsed

    lag = periodic_lag(filings)
    expected_report = None
    report_class = None
    median_gap = None
    if upcoming:
        report_class = expected_report_class(upcoming.get("period_end"))
        median_gap = (lag["median_gap_days_by_class"] or {}).get(report_class)
    if upcoming and median_gap is not None:
        try:
            base = dt.date.fromisoformat(str(upcoming["disclosure_date"]))
            expected_report = (
                base + dt.timedelta(days=round(float(median_gap)))
            ).isoformat()
        except (ValueError, TypeError):
            expected_report = None

    provisional_days = (
        business_days_until(str(upcoming["disclosure_date"]), today)
        if upcoming else None
    )
    report_days = (
        business_days_until(expected_report, today) if expected_report else None
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "asset_id": stock_code,
        "collected_at": dt.datetime.now().astimezone().isoformat(
            timespec="seconds"
        ),
        "as_of_date": today.isoformat(),
        "source": "DART_DOCUMENT",
        "source_grade": "PRIMARY",
        "next_provisional_result": (
            {
                **upcoming,
                "business_days_remaining": provisional_days,
                "_content": (
                    "잠정실적 공정공시다. 매출·영업이익·순이익 요약이 중심이고 "
                    "재고자산·현금흐름은 담지 않는다. 일부 회사는 본문에 품목군별 "
                    "매출을 적는다(kr_product_sales)"
                ),
            }
            if upcoming else None
        ),
        "expected_periodic_report": (
            {
                "date": expected_report,
                "estimated": True,
                "report_class": report_class,
                "basis": (
                    f"과거 {report_class} 정기보고서의 (잠정실적 → 제출) 간격 "
                    f"중앙값 {median_gap}일"
                ),
                "sample_size": (lag["sample_size_by_class"] or {}).get(
                    report_class
                ),
                "business_days_remaining": report_days,
                "_content": (
                    "재고자산·현금흐름·주요 매출처가 여기 나온다. 사업부문표가 "
                    "있는 회사는 부문별 매출도 여기 있다(segments). 잠정실적 "
                    "공정공시에는 재고자산·현금흐름이 없다"
                ),
            }
            if expected_report else None
        ),
        "expected_periodic_report_missing_reason": (
            None
            if expected_report or not upcoming
            else (
                f"{report_class or '알 수 없는'} 부류의 과거 관측이 2건 미만이라 "
                "정기보고서 예상일을 내지 않는다"
            )
        ),
        "periodic_report_lag": lag,
        "business_day_basis": (
            "주말만 제외한 근사다. 한국 공휴일은 반영하지 않았으므로 실제 "
            "영업일 수는 이 값 이하다"
        ),
        "_note": (
            "발표 예정일은 회사가 변경할 수 있다고 원문이 명시한다. 확정일이 "
            "아니며, 날짜가 지나도 공시가 없으면 연기된 것이다"
        ),
        "_not_a_judgment": (
            "이벤트까지 남은 날짜는 측정치다. 이벤트 전에 사지 말라는 지시가 "
            "아니라, 판단이 이벤트와 어디에 놓였는지 보이게 하는 값이다"
        ),
    }


def save(stock_code: str, packet: dict) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{stock_code}.json"
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=OUT_DIR, delete=False
        ) as handle:
            json.dump(packet, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            tmp = handle.name
        os.replace(tmp, path)
    finally:
        if tmp and Path(tmp).exists():
            Path(tmp).unlink()
    return path


def load(stock_code: str) -> dict:
    path = OUT_DIR / f"{stock_code}.json"
    if not path.exists():
        fail(f"{path.relative_to(ROOT)} 가 없다. 먼저 수집한다")
    return json.loads(path.read_text(encoding="utf-8"))


def render(packet: dict) -> None:
    print("=" * 74)
    print(f"실적 캘린더 — {packet['asset_id']}   기준 {packet['as_of_date']}")
    print("=" * 74)
    upcoming = packet.get("next_provisional_result")
    if not upcoming:
        print("결산실적 공시예고가 색인에 없다. 다음 발표일을 알 수 없다.")
    else:
        days = upcoming.get("business_days_remaining")
        left = f"{days}영업일 뒤" if days is not None else "지났거나 알 수 없음"
        print(f"잠정실적 공시예정   {upcoming['disclosure_date']}  ({left})")
        print(f"  결산대상기간      {upcoming.get('period_start')} ~ "
              f"{upcoming.get('period_end')}")
        print(f"  예고 공시일       {upcoming.get('notice_filing_date')}")
        print(f"  근거              {upcoming.get('evidence_ref')}")
        print(f"  담는 것           {upcoming['_content']}")
    report = packet.get("expected_periodic_report")
    if report:
        days = report.get("business_days_remaining")
        left = f"{days}영업일 뒤" if days is not None else "지났거나 알 수 없음"
        print(f"\n정기보고서 예상     {report['date']}  ({left})  "
              f"※ 추정 (표본 {report.get('sample_size')}건)")
        print(f"  근거              {report['basis']}")
        print(f"  담는 것           {report['_content']}")
    elif packet.get("expected_periodic_report_missing_reason"):
        print(f"\n정기보고서 예상     없음 — "
              f"{packet['expected_periodic_report_missing_reason']}")
    lag = packet.get("periodic_report_lag") or {}
    if lag.get("observations"):
        medians = lag.get("median_gap_days_by_class") or {}
        summary = ", ".join(
            f"{name} {value}일" for name, value in medians.items()
        ) or "부류별 중앙값 없음"
        print(f"\n과거 간격 관측 {lag['observation_count']}건  ({summary})")
        for item in lag["observations"]:
            print(f"  {item['provisional_date']} 잠정 → "
                  f"{item['report_date']} {item['report_form']} "
                  f"({item['gap_days']}일, {item['report_class']})")
    print("-" * 74)
    print(f"※ {packet['_note']}")
    print(f"※ {packet['business_day_basis']}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="결산실적 공시예고 기반 실적 캘린더 (OpenDART 원문)"
    )
    parser.add_argument("stock_code", help="005930 / 214450")
    parser.add_argument("--show", action="store_true",
                        help="수집하지 않고 저장된 캘린더만 표시")
    args = parser.parse_args()

    if args.show:
        render(load(args.stock_code))
        return

    packet = build(args.stock_code, load_key())
    path = save(args.stock_code, packet)
    render(packet)
    print(f"\n저장: {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
