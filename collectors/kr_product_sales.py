#!/usr/bin/env python3
"""잠정실적 공시에서 품목군별 분기 매출 시계열을 만든다.

어떤 회사는 정기보고서의 '사업부문별 요약 재무 현황' 대신 잠정실적 공정공시
본문(4. 기타 투자판단에 참고할 사항)에만 품목군별 매출을 적는다. 파마리서치가
그렇다 — 의료기기·화장품·의약품·기타 × 내수·수출 여덟 줄이 분기마다 나오고,
정기보고서에는 이 분해가 없다. `collectors.kr_segments`는 그 회사에서 "표를
특정하지 못했다"로 끝난다.

그래서 판단마다 모델이 원문 서너 건을 손으로 다시 읽었다. 2026-08-09 판단은 그렇게
모은 3개 분기로 믹스 회귀를 돌렸다가 표본이 5개로 늘자 결론이 반증됐고,
2026-10-06 판단의 무효화 신호는 전부 이 숫자에 걸려 있다.

**새 수집을 하지 않는다.** 원문은 `filing_documents` 작업이 이미 받아 둔다
(잠정실적과 '매출액 또는 손익구조 변동' 공시). 이 모듈은 로컬 원문만 읽는다.

**지어내지 않는다.** 가장 위험한 실패는 못 읽는 것이 아니라 다른 분기·다른
회사의 숫자를 그 분기 숫자로 싣는 것이다.

- 여덟 줄의 합을 같은 공시의 매출액과 대조한다. 4분기는 연간 공시라 분기 매출
  칸이 없으므로 연간 매출에서 같은 해 3분기 공시의 누계를 빼서 대조한다
- 원문의 기간 표기(26.2분기)가 공시의 실적기간과 다르면 싣지 않는다
- 단위가 백만원이 아니면 환산하지 않고 거부한다
- 회사는 공시 색인의 접수번호로 맞추고, 색인 창(1년) 밖으로 나간 원문만 DART
  제목의 회사명(첫 '/' 앞)이 등록명과 **정확히** 같을 때 받는다. 어느 규칙으로
  맞췄는지 행마다 남긴다

**판정하지 않는다.** 변화율·비중은 산술이며 둔화·정체 같은 말은 여기서 만들지
않는다. 그 해석은 모델이 한다.
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
from typing import Any, Iterable

from cio.paths import KNOWLEDGE_DIR, ROOT

SCHEMA = "kr-product-sales/1.0.0"
OUT_DIR = KNOWLEDGE_DIR / "product_sales"
UNIT = "백만원"
# 여덟 줄이 각각 백만원 단위로 반올림돼 있고(±0.5씩), 4분기는 연간·누계 두 값의
# 반올림이 더 붙는다. 그보다 크게 벌어지면 반올림이 아니라 다른 숫자다.
RECONCILE_TOLERANCE = 5
QUARTER_END = {1: "03-31", 2: "06-30", 3: "09-30", 4: "12-31"}
PACKET_QUARTERS = 5

BREAKDOWN_RE = re.compile(
    r"품목군별\s*(?P<yy>\d{2})\s*\.\s*(?P<q>[1-4])\s*(?P<suffix>Q|분기)"
    r"(?P<gap>[^|]{0,80}?)단위\s*:\s*(?P<unit>[^)\s]+)\s*\)",
)
ITEM_RE = re.compile(
    r"(?P<name>[가-힣A-Za-z]+)\s*\(\s*(?P<channel>내수|수출)\s*\)\s*:\s*(?P<value>-?[\d,]+)"
)
# 목록은 이 말들 앞에서 끝난다. 뒤의 문장을 항목으로 읽지 않는다.
LIST_END_RE = re.compile(r"상기|본\s*자료|※|\|")
QUARTER_PERIOD_RE = re.compile(
    r"당기실적\s*\|\s*(\d{4}-\d{2}-\d{2})\s*\|\s*~\s*\|\s*(\d{4}-\d{2}-\d{2})"
)
QUARTER_REVENUE_RE = re.compile(r"매출액\s*\|\s*당해실적\s*\|\s*(-?[\d,]+)\s*\|")
CUMULATIVE_RE = re.compile(r"누계실적\s*\|\s*(-?[\d,]+)\s*\|")
QUARTER_UNIT_RE = re.compile(r"단위\s*:\s*백만원")
ANNUAL_HEADER_RE = re.compile(r"매출액\s*또는\s*손익구조\s*변동내용\s*\(\s*단위\s*:\s*원\s*\)")
ANNUAL_REVENUE_RE = re.compile(r"-\s*매출액\s*\|\s*(-?[\d,]+)\s*\|")
ANNUAL_END_RE = re.compile(r"-\s*종료일\s*\|\s*(\d{4}-\d{2}-\d{2})")


def _int(text: str) -> int:
    return int(text.replace(",", ""))


def title_company_matches(title: str | None, company: str) -> bool:
    """DART 제목은 '회사명/보고서명/(날짜)…'이다. 첫 칸이 등록명과 같아야 한다."""
    if not isinstance(title, str) or "/" not in title:
        return False
    return title.split("/", 1)[0].strip() == company.strip()


def parse_breakdown(text: str) -> dict[str, Any] | None:
    """품목군별 매출 목록. 없으면 None, 읽었지만 쓸 수 없으면 ``problem``."""
    match = BREAKDOWN_RE.search(text)
    if not match:
        return None
    year = 2000 + int(match["yy"])
    quarter = int(match["q"])
    label = f"{match['yy']}.{match['q']}{match['suffix']}"
    parsed: dict[str, Any] = {
        "period": f"{year}Q{quarter}",
        "period_end": f"{year}-{QUARTER_END[quarter]}",
        "label_in_source": label,
        "unit": match["unit"],
    }
    if match["unit"] != UNIT:
        parsed["problem"] = "UNSUPPORTED_UNIT"
        return parsed
    tail = text[match.end():match.end() + 1200]
    end = LIST_END_RE.search(tail)
    if end:
        tail = tail[:end.start()]
    items: dict[str, int] = {}
    for item in ITEM_RE.finditer(tail):
        name = f"{item['name']}({item['channel']})"
        if name in items:
            parsed["problem"] = "DUPLICATE_ITEM"
            return parsed
        items[name] = _int(item["value"])
    if not items:
        parsed["problem"] = "NO_ITEMS"
        return parsed
    parsed["items"] = items
    return parsed


def _quarter_facts(text: str) -> dict[str, Any]:
    """분기 잠정공시의 실적기간·매출액·누계. 단위가 백만원일 때만 읽는다."""
    facts: dict[str, Any] = {"form": "QUARTERLY"}
    period = QUARTER_PERIOD_RE.search(text)
    if period:
        facts["period_start"], facts["period_end"] = period.group(1), period.group(2)
    revenue = QUARTER_REVENUE_RE.search(text)
    if revenue and QUARTER_UNIT_RE.search(text[:revenue.start()]):
        facts["revenue"] = _int(revenue.group(1))
        cumulative = CUMULATIVE_RE.search(text, revenue.end())
        if cumulative:
            facts["cumulative_revenue"] = _int(cumulative.group(1))
    return facts


def _annual_facts(text: str) -> dict[str, Any]:
    """'매출액 또는 손익구조 변동' 공시. 연간 매출(원)과 결산 종료일."""
    facts: dict[str, Any] = {"form": "ANNUAL"}
    end = ANNUAL_END_RE.search(text)
    if end:
        facts["period_end"] = end.group(1)
    header = ANNUAL_HEADER_RE.search(text)
    if header:
        revenue = ANNUAL_REVENUE_RE.search(text, header.end())
        if revenue:
            facts["annual_revenue_million"] = round(_int(revenue.group(1)) / 1_000_000)
    return facts


def _record(source: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    text = source["text"]
    parsed = parse_breakdown(text)
    base = {"rcept_no": source["rcept_no"], "doc_id": source["doc_id"]}
    if parsed is None:
        return None, None
    if parsed.get("problem"):
        return None, {**base, "reason": parsed["problem"], "label_in_source": parsed["label_in_source"]}
    facts = _annual_facts(text) if ANNUAL_HEADER_RE.search(text) else _quarter_facts(text)
    if facts.get("period_end") and facts["period_end"] != parsed["period_end"]:
        return None, {**base, "reason": "PERIOD_CONFLICT",
                      "label_in_source": parsed["label_in_source"],
                      "filing_period_end": facts["period_end"]}
    if facts["form"] == "ANNUAL" and not parsed["period"].endswith("Q4"):
        return None, {**base, "reason": "PERIOD_CONFLICT",
                      "label_in_source": parsed["label_in_source"],
                      "filing_period_end": facts.get("period_end")}
    record = {
        "period": parsed["period"],
        "period_end": parsed["period_end"],
        "label_in_source": parsed["label_in_source"],
        **base,
        "filing_date": source["filing_date"],
        "matched_by": source["matched_by"],
        "source_form": facts["form"],
        "items": parsed["items"],
        "item_sum": sum(parsed["items"].values()),
        "reported_revenue": facts.get("revenue"),
        "reported_revenue_basis": "QUARTER_REPORTED" if facts.get("revenue") is not None else None,
        "cumulative_revenue": facts.get("cumulative_revenue"),
        "annual_revenue_million": facts.get("annual_revenue_million"),
    }
    return record, None


def _reconcile(record: dict[str, Any], by_period: dict[str, dict[str, Any]]) -> None:
    if record["source_form"] == "ANNUAL":
        year = record["period"][:4]
        third = by_period.get(f"{year}Q3")
        nine_months = (third or {}).get("cumulative_revenue")
        annual = record.get("annual_revenue_million")
        if annual is not None and nine_months is not None:
            record["reported_revenue"] = annual - nine_months
            record["reported_revenue_basis"] = "ANNUAL_MINUS_9M"
        else:
            record["reported_revenue"] = None
            record["reported_revenue_basis"] = None
            record["unchecked_reason"] = "같은 해 3분기 누계 매출이 없어 4분기 매출을 만들 수 없다"
    reported = record.get("reported_revenue")
    if reported is None:
        record["difference"] = None
        record["reconciliation"] = "UNCHECKED"
        return
    record["difference"] = record["item_sum"] - reported
    record["reconciliation"] = (
        "RECONCILED" if abs(record["difference"]) <= RECONCILE_TOLERANCE else "MISMATCH"
    )


def build_series(
    asset_id: str,
    documents: Iterable[dict[str, Any]],
    *,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """원문에서 분기 행을 만들고, 이전 저장본의 분기와 합친다.

    같은 분기 공시가 둘이면(정정) 늦은 공시가 이긴다. 이전 저장본에만 있는 분기는
    남긴다 — 공시 색인은 1년 창이라 전년동기 원문이 빠져 나가는데, 바로 그때
    전년동기 대비가 필요하기 때문이다.
    """
    candidates: dict[str, list[dict[str, Any]]] = {}
    unparsed: list[dict[str, Any]] = []
    for source in documents:
        record, problem = _record(source)
        if problem:
            unparsed.append(problem)
        if record:
            candidates.setdefault(record["period"], []).append(record)
    chosen: dict[str, dict[str, Any]] = {}
    superseded: list[dict[str, Any]] = []
    for period, rows in candidates.items():
        rows.sort(key=lambda row: (row["filing_date"] or "", row["rcept_no"]))
        chosen[period] = rows[-1]
        superseded.extend(
            {"period": period, "rcept_no": row["rcept_no"], "superseded_by": rows[-1]["rcept_no"]}
            for row in rows[:-1]
        )
    for row in (previous or {}).get("quarters") or []:
        if row.get("period") and row["period"] not in chosen:
            chosen[row["period"]] = {**row, "retained_from_previous": True}
    for period in sorted(chosen):
        if chosen[period].get("retained_from_previous"):
            continue
        _reconcile(chosen[period], chosen)
    quarters = [chosen[period] for period in sorted(chosen)]
    return {
        "schema_version": SCHEMA,
        "asset_id": asset_id,
        "source": "DART_PROVISIONAL_RESULT_TEXT",
        "collected_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "unit": UNIT,
        "available": bool(quarters),
        "quarters": quarters,
        "superseded": superseded,
        "unparsed": unparsed,
        "_note": (
            "잠정실적 공정공시(4분기는 '매출액 또는 손익구조 변동' 공시) 본문의 품목군별 "
            "매출을 그대로 옮겼다. 외부감사 전 잠정치다. reconciliation 은 여덟 줄의 합과 "
            "같은 공시의 매출액(4분기는 연간 − 3분기 누계)의 대조이며 허용 차이는 "
            f"{RECONCILE_TOLERANCE}백만원(반올림)이다"
        ),
    }


def change_pct(value: float | None, base: float | None) -> float | None:
    """기준이 없거나 0 이하면 변화율을 만들지 않는다."""
    if value is None or base is None or base <= 0:
        return None
    return round((value / base - 1) * 100, 1)


def _previous_period(period: str, quarters_back: int) -> str:
    year, quarter = int(period[:4]), int(period[-1])
    index = year * 4 + (quarter - 1) - quarters_back
    return f"{index // 4}Q{index % 4 + 1}"


def packet_summary(data: dict[str, Any], *, quarters: int = PACKET_QUARTERS) -> dict[str, Any] | None:
    """packet 용 압축본. 표는 한 줄 순서에 맞춘 값 배열이고 최신 분기만 펼친다."""
    rows = [row for row in data.get("quarters") or [] if row.get("items")]
    if not rows:
        return None
    usable = {row["period"]: row for row in rows if row.get("reconciliation") != "MISMATCH"}
    recent = rows[-quarters:]
    order = list(rows[-1]["items"])
    for row in recent:
        order.extend(name for name in row["items"] if name not in order)
    table = [
        {
            "period": row["period"],
            "values": [row["items"].get(name) for name in order],
            "sum": row["item_sum"],
            "reconciliation": row.get("reconciliation"),
            "rcept_no": row.get("rcept_no"),
        }
        for row in recent
    ]
    latest = rows[-1]
    detail = None
    if latest["period"] in usable:
        prior = usable.get(_previous_period(latest["period"], 1))
        year_ago = usable.get(_previous_period(latest["period"], 4))
        total = latest["item_sum"]
        detail = {
            "period": latest["period"],
            "compared_with": {
                "qoq": prior["period"] if prior else None,
                "yoy": year_ago["period"] if year_ago else None,
            },
            "items": {
                name: {
                    "value": value,
                    "share_pct": round(value / total * 100, 1) if total > 0 else None,
                    "qoq_pct": change_pct(value, (prior or {}).get("items", {}).get(name)),
                    "yoy_pct": change_pct(value, (year_ago or {}).get("items", {}).get(name)),
                }
                for name, value in latest["items"].items()
            },
        }
    return {
        "unit": data.get("unit", UNIT),
        "latest_period": latest["period"],
        "items": order,
        "quarters": table,
        "latest": detail,
    }


# ------------------------------------------------------------------ 원문 찾기


def local_documents(asset: dict[str, Any]) -> list[dict[str, Any]]:
    """이 회사의 로컬 DART 원문 중 품목군별 매출 문장이 있는 것."""
    from cio.documents import build_index, document_text, documents_for

    linked = {
        entry["rcept_no"]: entry
        for entry in documents_for(asset["asset_id"])["available"]
        if entry.get("kind") == "DART_DOCUMENT" and entry.get("rcept_no")
    }
    found = []
    for entry in build_index()["documents"]:
        if entry.get("kind") != "DART_DOCUMENT" or not entry.get("rcept_no"):
            continue
        rcept_no = entry["rcept_no"]
        if rcept_no in linked:
            matched_by = "FILING_INDEX"
            filing_date = linked[rcept_no].get("filing_date")
        elif title_company_matches(entry.get("title"), asset.get("name") or ""):
            matched_by = "TITLE_COMPANY_NAME"
            filing_date = None
        else:
            continue
        text = document_text(entry)
        if "품목군별" not in text:
            continue
        found.append({
            "rcept_no": rcept_no,
            "doc_id": f"DARTDOC:{rcept_no}",
            "title": entry.get("title"),
            # DART 접수번호의 앞 8자리는 접수일이다.
            "filing_date": filing_date or f"{rcept_no[:4]}-{rcept_no[4:6]}-{rcept_no[6:8]}",
            "matched_by": matched_by,
            "text": text,
        })
    return found


def save(packet: dict[str, Any]) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{packet['asset_id']}.json"
    temporary_name = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=OUT_DIR, delete=False) as handle:
            json.dump(packet, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            temporary_name = handle.name
        os.replace(temporary_name, path)
    finally:
        if temporary_name and Path(temporary_name).exists():
            Path(temporary_name).unlink()
    return path


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python3.13 -m collectors.kr_product_sales",
        description="잠정실적 공시 원문(로컬)에서 품목군별 분기 매출 시계열을 만든다",
    )
    parser.add_argument("asset_id")
    args = parser.parse_args()
    from cio.assets import get_asset

    asset = get_asset(args.asset_id)
    path = OUT_DIR / f"{asset['asset_id']}.json"
    previous = None
    if path.exists():
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            previous = None
    packet = build_series(asset["asset_id"], local_documents(asset), previous=previous)
    saved = save(packet)
    print(f"{asset['asset_id']}  품목군별 매출 ({UNIT})")
    print(f"저장: {saved.relative_to(ROOT)}")
    print("-" * 72)
    if not packet["available"]:
        # 실패가 아니라 관측이다. 0 이 아닌 코드로 끝내면 refresh 가 수집 실패로
        # 세고 같은 원문으로 계속 다시 돌린다.
        print("  로컬 원문에서 품목군별 매출 문장을 찾지 못했다")
    for row in packet["quarters"]:
        flag = " (이전 저장본)" if row.get("retained_from_previous") else ""
        print(f"  {row['period']}  합 {row['item_sum']:>9,}  대조 {row.get('reconciliation')}"
              f"  차이 {row.get('difference')}  {row['rcept_no']} [{row['matched_by']}]{flag}")
    for problem in packet["unparsed"]:
        print(f"  ⚠️ {problem['rcept_no']} {problem['reason']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
