"""Extract announced KR cash/bonus issues from DART form fields.

An announcement and its scheduled dates never certify completion or ownership.
No valuation, eligibility, conversion of a CB, or trading decision is inferred.
"""
import argparse
import datetime as dt
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from calculators.contracts import canonical_payload_hash, content_hash, load_json, write_json
from cio.paths import ROOT, KNOWLEDGE_DIR

SCHEMA = "kr-capital-announcement/1.0.0"


def selected_filing(index):
    candidates = [r for r in index.get("filings", [])
                  if "주요사항보고서" in r.get("description", "")
                  and "증자" in r.get("description", "")]
    return max(candidates, key=lambda r: (r["filing_date"], r["accession_number"]), default=None)


def numeric(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9,]+(?:\.[0-9]+)?", value.strip()):
        return None
    return float(value.replace(",", ""))


def date_value(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{8}", value):
        return None
    return dt.datetime.strptime(value, "%Y%m%d").date().isoformat()


def extract(documents, *, corp_code, description):
    if "철회" in description or "취소" in description:
        return {"state": "CANCELLED", "events": [], "share_bases": {}, "unresolved": []}
    events = []
    for xml in documents.values():
        root = ET.fromstring(xml)
        company = root.find(".//COMPANY-NAME")
        if company is None or company.get("AREGCIK") != corp_code:
            raise ValueError("증자 공시의 회사 고유번호가 종목 색인과 다르다")
        # Correction comparisons can contain old values; only the actual form.
        for parent in root.iter():
            for child in list(parent):
                if child.tag == "CORRECTION":
                    parent.remove(child)
        field_groups, fields = [], None
        for table in root.iter("TABLE"):
            table_fields = {}
            for cell in table.iter():
                code = cell.get("ACODE") or cell.get("AUNIT")
                if code:
                    value = cell.get("AUNITVALUE") or "".join(cell.itertext()).strip()
                    if code in table_fields and table_fields[code] != value:
                        raise ValueError("증자 표에 충돌하는 동일 필드가 있다")
                    table_fields[code] = value
            if "CST_CNT" in table_fields:
                fields = {}
                field_groups.append(fields)
            if fields is not None:
                for code, value in table_fields.items():
                    if code in fields and fields[code] != value:
                        raise ValueError("증자 서식 필드가 충돌한다")
                    fields[code] = value
        for fields in field_groups:
            if "CST_CNT" not in fields or "BFR_CST_CNT" not in fields:
                continue
            kind = "BONUS" if "NEW_ASN_CST" in fields else "CASH" if "CI_MTH" in fields else None
            if kind is None:
                continue
            event = {"kind": kind, "state": "ANNOUNCED",
                     "issued_before": numeric(fields["BFR_CST_CNT"]),
                     "new_shares": numeric(fields["CST_CNT"]),
                     "record_date": date_value(fields.get("ALL_BS_DT")),
                     "listing_date": date_value(fields.get("LST_PLN_DT")),
                     "ex_date": None, "completed_at": None,
                     "announcement_date": date_value(fields.get("DRC_DT"))}
            if kind == "CASH":
                event.update(issue_price=numeric(fields.get("CST_ISS_VAL")),
                             indicative_price=numeric(fields.get("PRE_CST_VAL")),
                             price_fix_date=date_value(fields.get("PRE_CST_VAL_DT")),
                             payment_date=date_value(fields.get("PYM_DT")),
                             subscription_start=date_value(fields.get("SH_BGN_DT")),
                             subscription_end=date_value(fields.get("SH_END_DT")),
                             rights_per_share=numeric(fields.get("NEW_ASN_CNT")),
                             offering_method=fields.get("CI_MTH"))
            else:
                event["bonus_per_share"] = numeric(fields.get("NEW_ASN_CST"))
            events.append(event)
    if not events:
        return {"state": "UNSUPPORTED", "events": [], "share_bases": {},
                "unresolved": ["지원하는 DART 보통주 유·무상증자 표를 찾지 못했다"]}
    kinds = [e["kind"] for e in events]
    if len(kinds) != len(set(kinds)):
        raise ValueError("유상 또는 무상 표가 중복되어 한 계획으로 확정할 수 없다")
    bases, unresolved = {}, ["청약권 실제 보유·납입·상장 완료는 확인하지 않았다",
                            "전환증권의 미래 상환/전환은 별도 모델 시나리오가 필요하다"]
    for e in events:
        if any(e[k] is None or e[k] <= 0 or not e[k].is_integer() for k in ("issued_before", "new_shares")):
            raise ValueError("발행주식수는 양의 정수 관측이어야 한다")
        e["issued_after"] = e["issued_before"] + e["new_shares"]
    cash = next((e for e in events if e["kind"] == "CASH"), None)
    bonus = next((e for e in events if e["kind"] == "BONUS"), None)
    if cash:
        bases.update(ISSUED_AT_ANNOUNCEMENT=cash["issued_before"], POST_CASH_ISSUED=cash["issued_after"])
    if bonus:
        ratio = bonus["bonus_per_share"]
        if ratio is None or ratio <= 0:
            raise ValueError("무상 배정비율을 확인하지 못했다")
        eligible = bonus["new_shares"] / ratio
        if not eligible.is_integer() or eligible > bonus["issued_before"]:
            raise ValueError("무상 배정 주식수와 비율이 맞지 않는다")
        bonus.update(eligible_shares=eligible, share_multiplier=1 + ratio)
        bases.update(PRE_BONUS_ELIGIBLE=eligible, POST_BONUS_ELIGIBLE=eligible + bonus["new_shares"])
        if cash:
            if cash["issued_after"] != bonus["issued_before"]:
                raise ValueError("유상 후 주식수와 무상 전 주식수가 다르다")
            bases["POST_CASH_ELIGIBLE"] = eligible
    return {"state": "ANNOUNCED", "events": events, "share_bases": bases, "unresolved": unresolved}


def make_report(asset_id, index, row, raw=None, raw_path=None):
    observations = extract(raw["payload"]["documents"], corp_code=index["corp_code"], description=row["description"]) if row else {
        "state": "NO_EVENTS", "events": [], "share_bases": {}, "unresolved": []}
    return {"schema_version": SCHEMA, "asset_id": asset_id,
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "filing": row, "corp_code": index.get("corp_code"),
            "raw_path": str(raw_path.relative_to(ROOT)) if raw_path else None,
            "raw_sha256": content_hash(raw_path) if raw_path else None, **observations}


def contract_errors(report, asset_id, *, index=None):
    try:
        if report.get("schema_version") != SCHEMA or report.get("asset_id") != asset_id:
            raise ValueError("증자 자료의 계약/종목 불일치")
        if index is not None and report.get("filing") != selected_filing(index):
            raise ValueError("더 최신 증자·정정 공시가 있다")
        row = report.get("filing")
        if row:
            path = (ROOT / report["raw_path"]).resolve()
            if not path.is_relative_to(ROOT.resolve()) or content_hash(path) != report["raw_sha256"]:
                raise ValueError("증자 원문 경로/지문 불일치")
            raw = load_json(path)
            if canonical_payload_hash(raw["payload"]) != raw["payload_sha256"] or raw["payload"].get("rcept_no") != row["accession_number"]:
                raise ValueError("공시 원문의 payload/접수번호 불일치")
            expected = extract(raw["payload"]["documents"], corp_code=report["corp_code"], description=row["description"])
            if any(report.get(k) != v for k, v in expected.items()):
                raise ValueError("증자 추출값이 원문 재계산과 다르다")
        elif report.get("state") != "NO_EVENTS" or report.get("events") or report.get("share_bases"):
            raise ValueError("공시 없이 증자 관측을 만들 수 없다")
    except (ValueError, KeyError, TypeError, OSError, ET.ParseError) as exc:
        return [str(exc)]
    return []


def collect(asset_id):
    from collectors.dart import fetch_document, load_key
    from cio.documents import documents_for
    from cio.assets import get_asset
    asset = get_asset(asset_id)
    if asset.get("market") != "KR" or asset.get("asset_type") != "STOCK":
        raise ValueError("이 수집기는 한국 주식 공시만 지원한다")
    index = load_json(KNOWLEDGE_DIR / "filings" / f"{asset_id}.json")
    row = selected_filing(index)
    raw, path = None, None
    if row:
        catalog = documents_for(asset_id)["available"]
        doc = next((d for d in catalog if d["doc_id"] == f"DARTDOC:{row['accession_number']}"), None)
        path = ROOT / doc["locator"] if doc else fetch_document(row["accession_number"], load_key())[1]
        raw = load_json(path)
    report = make_report(asset_id, index, row, raw, path)
    errors = contract_errors(report, asset_id, index=index)
    if errors:
        raise ValueError("; ".join(errors))
    write_json(KNOWLEDGE_DIR / "capital" / f"{asset_id}.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("asset_id")
    args = parser.parse_args()
    print(json.dumps(collect(args.asset_id), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
