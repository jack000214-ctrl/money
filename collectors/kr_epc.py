"""Extract scoped EPC contract-note facts, not assessments of earnings quality."""

from __future__ import annotations

import argparse
import datetime as dt
from decimal import Decimal, InvalidOperation
import html
import re

from calculators.contracts import canonical_payload_hash, content_hash, load_json, write_json
from cio.assets import get_asset
from cio.paths import KNOWLEDGE_DIR, ROOT
from collectors.kr_segments import PERIODIC_FORMS, TABLE_RE, ROW_RE, _fetched_document

SCHEMA = "epc-contract-notes/1.0.0"
TITLE_RE = re.compile(r"<TITLE\b[^>]*>(.*?)</TITLE>", re.I | re.S)
CELL_RE = re.compile(r"<T[DHE]\b[^>]*>.*?</T[DHE]>", re.I | re.S)
ATTR_RE = re.compile(r'([A-Z]+)="([^"]*)"', re.I)
UNIT_RE = re.compile(r"단위\s*:\s*(백만원|천원|억원|원)")
UNITS = {"원": 1, "천원": 1000, "백만원": 1000000, "억원": 100000000}
# Labels are lookup keys, not a claim about whether the amount is recurring.
LABELS = {
    "나머지수행의무에배분된거래가격": ("remaining_performance_obligations", "BALANCE"),
    "단기미청구공사": ("unbilled_balance", "BALANCE"),
    "단기미청구공사,총액": ("unbilled_balance", "BALANCE"),
    "단기초과청구공사": ("overbilled_balance", "BALANCE"),
    "한시점에이행하는수행의무에대한계약부채를나타내는선수금": ("customer_advances_balance", "BALANCE"),
    "선수금": ("customer_advances_balance", "BALANCE"),
    "유동공사손실충당부채": ("loss_provision_balance", "BALANCE"),
    "총계약수익과원가의추정변동이당기손익에미치는영향": ("estimate_change_profit_ytd", "YEAR_TO_DATE"),
    "총계약수익과원가의추정변동이미래손익에미치는영향": ("estimate_change_profit_future", "FUTURE_REMAINING"),
    "공사손실충당부채전입액": ("loss_provision_reported_charge", "UNRESOLVED"),
}
METRICS = sorted({metric for metric, _ in LABELS.values()})
LIABILITY_BALANCES = {"overbilled_balance", "customer_advances_balance", "loss_provision_balance"}


def clean(value):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value))).strip()


def compact(value):
    return re.sub(r"\s+", "", value)


def grid(table):
    """Preserve DART TE cells and multirow total headers; never pick the last number."""
    occupied = {}
    header_end = table.upper().find("</THEAD>")
    headers = []
    body = []
    for ri, match in enumerate(ROW_RE.finditer(table)):
        col = 0
        for fragment in CELL_RE.findall(match.group()):
            while (ri, col) in occupied:
                col += 1
            attrs = {key.lower(): html.unescape(value) for key, value in ATTR_RE.findall(fragment.split(">", 1)[0])}
            cell = {"text": clean(fragment), "attrs": attrs}
            try:
                rs, cs = int(attrs.get("rowspan", 1)), int(attrs.get("colspan", 1))
            except ValueError as exc:
                raise ValueError("잘못된 표 병합 속성") from exc
            if not 1 <= rs <= 128 or not 1 <= cs <= 128:
                raise ValueError("지원 범위 밖의 표 병합")
            for dr in range(rs):
                for dc in range(cs):
                    occupied[(ri + dr, col + dc)] = cell
            col += cs
        row = [occupied.get((ri, c), {"text": "", "attrs": {}}) for c in range(max((c for r, c in occupied if r == ri), default=-1) + 1)]
        (headers if match.start() < header_end else body).append(row)
    return headers, body


def amount(value, unit):
    raw = value.strip().replace(",", "")
    if raw in {"", "-", "—"} or unit not in UNITS:
        return None
    negative = raw.startswith(("△", "▲", "("))
    if negative:
        raw = raw.strip("△▲()")
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", raw):
        return None
    try:
        result = Decimal(raw) * UNITS[unit] * (-1 if negative else 1)
    except InvalidOperation:
        return None
    return int(result) if result == result.to_integral_value() else float(result)


def extract(document, report_date):
    dt.date.fromisoformat(report_date)
    period = re.search(r"사업연도\s+(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일\s*부터\s*(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일\s*까지", clean(document[:20000]))
    period_start = None
    if period:
        start = dt.date(*map(int, period.groups()[:3]))
        end = dt.date(*map(int, period.groups()[3:]))
        if end.isoformat() != report_date or start > end:
            raise ValueError("표지의 사업연도와 보고서 기준일이 다르다")
        period_start = start.isoformat()
    titles = list(TITLE_RE.finditer(document))
    notes = []
    for index, title in enumerate(titles):
        label = clean(title.group(1))
        if "(연결)" in compact(label) and any(word in label for word in ("도급공사", "건설계약", "공사계약")):
            end = titles[index + 1].start() if index + 1 < len(titles) else len(document)
            notes.append((label, document[title.end():end]))
    observations, issues = [], []
    for title, section in notes:
        for ti, match in enumerate(TABLE_RE.finditer(section)):
            table = match.group()
            if not any(label in compact(clean(table)) for label in LABELS):
                continue
            header, body = grid(table)
            units = list(UNIT_RE.finditer(clean(section[max(0, match.start() - 5000):match.start()])))
            unit = units[-1].group(1) if units else None
            width = max((len(r) for r in header), default=0)
            totals = [col for col in range(1, width) if any(col < len(r) and compact(r[col]["text"]) in {"부문합계", "합계"} for r in header)]
            if not totals and width == 2 and any(len(r) == 2 and compact(r[1]["text"]) == "도급공사" for r in header):
                totals = [1]
            for row in body:
                if not row or compact(row[0]["text"]) not in LABELS:
                    continue
                metric, time_kind = LABELS[compact(row[0]["text"])]
                if len(totals) != 1 or totals[0] >= len(row):
                    issues.append({"metric": metric, "reason": "TOTAL_COLUMN_UNRESOLVED", "table_index": ti})
                    continue
                cell = row[totals[0]]; attrs = cell["attrs"]
                context = attrs.get("acontext", "")
                if "_SeparateMember" in context or "_ConsolidatedMember" not in context:
                    issues.append({"metric": metric, "reason": "SCOPE_UNRESOLVED", "table_index": ti})
                    continue
                # Only the current reporting period. Previous-year rows remain in the raw source.
                if not context.startswith(f"CFY{report_date[:4]}"):
                    continue
                value = amount(cell["text"], unit)
                state = "AVAILABLE" if value is not None else "UNIT_OR_VALUE_UNRESOLVED"
                if time_kind == "UNRESOLVED":
                    state = "STOCK_FLOW_UNRESOLVED"
                elif time_kind == "YEAR_TO_DATE" and period_start is None:
                    state = "PERIOD_START_UNRESOLVED"
                observations.append({
                    "metric": metric, "state": state, "label": row[0]["text"],
                    "reported_amount_krw": value, "raw_amount": cell["text"], "display_unit": unit,
                    "scope": "CONSOLIDATED", "time_kind": time_kind,
                    "period_start": period_start if time_kind == "YEAR_TO_DATE" else None,
                    "period_end": report_date if time_kind in {"BALANCE", "YEAR_TO_DATE"} else None,
                    "observed_at": report_date, "note_title": title, "table_index": ti,
                    "column": " / ".join(dict.fromkeys(r[totals[0]]["text"] for r in header if totals[0] < len(r) and r[totals[0]]["text"])),
                    "tag": attrs.get("acode"), "source_context": context,
                    "source_decimal_attribute": attrs.get("adecimal"),
                })
    values = {}
    for metric in METRICS:
        matches = [r for r in observations if r["metric"] == metric]
        available = [r for r in matches if r["state"] == "AVAILABLE"]
        distinct = {abs(r["reported_amount_krw"]) if metric in LIABILITY_BALANCES else r["reported_amount_krw"] for r in available}
        if len(distinct) == 1 and len(available) == len(matches):
            representative = available[0]
            values[metric] = {key: representative[key] for key in ("state", "reported_amount_krw", "scope", "time_kind", "period_start", "period_end", "observed_at")}
            values[metric]["amount_krw"] = next(iter(distinct))
            values[metric]["amount_basis"] = "LIABILITY_MAGNITUDE" if metric in LIABILITY_BALANCES else "AS_REPORTED"
        else:
            values[metric] = {"state": "CONFLICTING_VALUES" if len(distinct) > 1 else (matches[0]["state"] if matches else "NOT_EXTRACTED"), "reported_amount_krw": None, "amount_krw": None}
    return {"available": any(v["state"] == "AVAILABLE" for v in values.values()),
            "values": values, "observations": observations, "issues": issues,
            "note_found": bool(notes), "report_period_start": period_start}


def periodic_filings(asset_id, limit=2):
    data = load_json(KNOWLEDGE_DIR / "filings" / f"{asset_id}.json")
    result, seen = [], set()
    for row in sorted(data.get("filings", []), key=lambda r: r.get("filing_date", ""), reverse=True):
        if row.get("form") in PERIODIC_FORMS and row.get("report_date") and row["report_date"] not in seen:
            result.append(row); seen.add(row["report_date"])
    return sorted(result, key=lambda r: r["report_date"], reverse=True)[:limit]


def collect(asset_id, limit=2):
    asset = get_asset(asset_id)
    if asset["market"] != "KR" or asset["asset_type"] != "STOCK":
        raise ValueError("한국 주식의 연결 도급공사 주석만 지원한다")
    reports = []
    for filing in periodic_filings(asset_id, limit):
        document, path = _fetched_document(filing["accession_number"])
        result = extract(document, filing["report_date"])
        reports.append({"report_date": filing["report_date"], "filing_date": filing["filing_date"],
                        "rcept_no": filing["accession_number"], "source_id": f"DARTDOC:{filing['accession_number']}",
                        "raw_locator": str(path.relative_to(ROOT)), "raw_file_sha256": content_hash(path), **result})
    if not reports:
        raise ValueError("정기보고서 색인이 없다")
    return {"schema_version": SCHEMA, "asset_id": asset_id, "collected_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "reports": reports,
            "_note": "표의 표시 단위를 원으로 환산했다. ADECIMAL은 표시 단위로 쓰지 않는다. 부채 잔액은 amount_krw에 크기를, reported_amount_krw에 원문 부호를 보존한다. 잔액·올해 누적·미래 잔여기간의 영향을 구분한다. 전입액이라는 라벨만으로 당기 비용을 확정하지 않는다. 이익의 반복성·선수금의 자유로운 사용 가능성은 모델이 판단한다."}


def contract_errors(data, asset_id):
    try:
        if data.get("schema_version") != SCHEMA or data.get("asset_id") != asset_id:
            return ["EPC 계약 또는 종목 ID가 다르다"]
        reports = data.get("reports")
        if not isinstance(reports, list) or not 1 <= len(reports) <= 4:
            return ["EPC 보고서 수가 유효하지 않다"]
        filings = {r["accession_number"]: r for r in periodic_filings(asset_id, 4)}
        if reports[0]["rcept_no"] != next(iter(filings)):
            return ["더 새로운 정기보고서가 있다"]
        seen = set()
        for report in reports:
            key = report["rcept_no"]
            if key in seen or key not in filings:
                return ["중복 또는 공시 색인에 없는 EPC 보고서"]
            seen.add(key)
            if any(report[field] != filings[key][field] for field in ("report_date", "filing_date")):
                return ["EPC 기준기간이 공시 색인과 다르다"]
            path = (ROOT / report["raw_locator"]).resolve()
            if not path.is_relative_to(ROOT.resolve()) or content_hash(path) != report["raw_file_sha256"]:
                return ["EPC 원문 지문 또는 경로가 다르다"]
            document = load_json(path); payload = document["payload"]
            if canonical_payload_hash(payload) != document["payload_sha256"] or payload["rcept_no"] != key:
                return ["EPC 공시 원문 식별자가 다르다"]
            expected = extract(max(payload["documents"].values(), key=len), report["report_date"])
            if any(report.get(field) != value for field, value in expected.items()) or report["source_id"] != f"DARTDOC:{key}":
                return ["EPC 숫자·단위·기간·출처가 원문 재추출과 다르다"]
    except (OSError, ValueError, TypeError, KeyError, StopIteration) as exc:
        return [f"EPC 자료를 재검증할 수 없다: {exc}"]
    return []


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("asset_id")
    parser.add_argument("--reports", type=int, choices=range(1, 5), default=2)
    args = parser.parse_args()
    data = collect(args.asset_id, args.reports)
    errors = contract_errors(data, args.asset_id)
    if errors:
        raise ValueError("; ".join(errors))
    path = write_json(KNOWLEDGE_DIR / "epc" / f"{args.asset_id}.json", data)
    print(path)
    for report in data["reports"]:
        print(report["report_date"], {k: v["reported_amount_krw"] for k, v in report["values"].items()})


if __name__ == "__main__":
    main()
