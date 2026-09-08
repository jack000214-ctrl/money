"""Read a checked capital announcement into the packet; no execution inference."""
from calculators.contracts import load_json
from cio.paths import KNOWLEDGE_DIR, ROOT
from collectors.kr_capital import contract_errors, selected_filing


def section(asset):
    if asset.get("market") != "KR" or asset.get("asset_type") != "STOCK":
        return None, []
    index_path = KNOWLEDGE_DIR / "filings" / f"{asset['asset_id']}.json"
    if not index_path.exists():
        return None, []
    index = load_json(index_path)
    row = selected_filing(index)
    if row is None:
        return None, []
    path = KNOWLEDGE_DIR / "capital" / f"{asset['asset_id']}.json"
    if not path.exists():
        return {"state": "MISSING", "filing": row, "_note": "cio refresh --run 또는 collectors.kr_capital로 증자 원문을 확인한다"}, []
    try:
        report = load_json(path)
        errors = contract_errors(report, asset["asset_id"], index=index)
    except (ValueError, OSError) as exc:
        errors = [str(exc)]
    if errors:
        return {"state": "INVALID", "problems": errors}, []
    source_id = f"CAPITAL:{asset['asset_id']}"
    summary = {k: report[k] for k in ("state", "events", "share_bases", "unresolved")}
    summary.update(source_id=source_id, filing=row, _role="공시된 계획의 산술이다. POST_*는 예정 시나리오이며 현재 잔고·증자 완료가 아니다. ex_date와 completed_at이 null이면 확인되지 않았다. 원문에 없는 CB 상환·전환·청약권 소유를 만들지 않는다")
    sources = [{"source_id": source_id, "kind": "FILING_DERIVED", "path": str(path.relative_to(ROOT)), "data_as_of": row["filing_date"]}]
    if report.get("raw_path"):
        sources.append({"source_id": f"DARTDOC:{row['accession_number']}", "kind": "REGULATOR_FILING_DOCUMENT", "path": report["raw_path"], "data_as_of": row["filing_date"]})
    return summary, sources
