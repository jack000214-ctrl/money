"""Reported US business/product/geographic facts from saved SEC inline XBRL.

Dimensions are separate slices, never a summable hierarchy. Exact reported
concepts and periods survive extraction; custom concepts require model review.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re

from calculators.contracts import write_json
from cio.paths import KNOWLEDGE_DIR
from collectors.sec_inline import (DOCUMENT_FIELDS, load_reports, load_filings_index,
                                   parse_inline_xbrl, read_document, select_reports,
                                   validate_document_sources)

SCHEMA = "us-segments/1.0.0"
METRICS = {
    "RevenueFromContractWithCustomerExcludingAssessedTax": "revenue",
    "RevenueFromContractWithCustomerIncludingAssessedTax": "revenue",
    "Revenues": "revenue", "SalesRevenueNet": "revenue", "SalesRevenueGoodsNet": "revenue",
    "SalesRevenueServicesNet": "revenue", "OperatingIncomeLoss": "operating_income",
    "SegmentReportingInformationOperatingIncomeLoss": "operating_income",
    "SegmentReportingInformationProfitLoss": "segment_profit_loss",
    "GrossProfit": "gross_profit",
}
AXES = {
    "StatementBusinessSegmentsAxis": "BUSINESS", "BusinessSegmentsAxis": "BUSINESS",
    "ReportableSegmentsAxis": "BUSINESS", "OperatingSegmentsAxis": "BUSINESS",
    "ProductOrServiceAxis": "PRODUCT_SERVICE", "ProductsAndServicesAxis": "PRODUCT_SERVICE",
    "ProductAxis": "PRODUCT_SERVICE", "StatementGeographicalAxis": "GEOGRAPHY",
    "GeographicalAreasAxis": "GEOGRAPHY", "GeographicAreaAxis": "GEOGRAPHY", "CountryAxis": "GEOGRAPHY",
}
NOTE = ("Reported dimensional facts, not additive segments. Product, business and geographic slices overlap; "
        "do not add them or equate revenue with profit contribution. Periods are reported quarter/YTD/year, "
        "never silently subtracted. Custom QNames and segment profit measures need model interpretation. "
        "Missing tags, dimensions and unsupported transforms remain unknown; untagged tables require original review.")


def _axis_kind(axis):
    local = axis.split(":")[-1]
    if local in AXES:
        return AXES[local]
    if not axis.startswith(("us-gaap:", "srt:")):
        if re.fullmatch(r"(?:Statement)?(?:Business|Reportable|Operating)Segments?Axis", local):
            return "BUSINESS"
        if re.fullmatch(r"(?:Product|Service|ProductAndService|ProductOrService)s?Axis", local):
            return "PRODUCT_SERVICE"
        if re.fullmatch(r"(?:Geograph\w*|Countries|Country)Axis", local):
            return "GEOGRAPHY"
    return None


def _metric(concept):
    prefix, _, local = (concept or "").partition(":")
    if prefix == "us-gaap" and local in METRICS:
        return METRICS[local], "STANDARD_CONCEPT"
    if prefix not in {"us-gaap", "dei", "srt", ""}:
        if re.fullmatch(r"(?:Segment|External|Net)?(?:Revenues?|SalesRevenue|NetSales)(?:Net)?", local):
            return "revenue", "CUSTOM_NAME_MATCH_REQUIRES_REVIEW"
        if re.fullmatch(r"(?:Segment)?Operating(?:Income|Profit)(?:Loss)?", local):
            return "operating_income", "CUSTOM_NAME_MATCH_REQUIRES_REVIEW"
        if re.fullmatch(r"Segment(?:Profit|Income)(?:Loss)?", local):
            return "segment_profit_loss", "CUSTOM_NAME_MATCH_REQUIRES_REVIEW"
    return None, None


def extract(body, report_date):
    parsed = parse_inline_xbrl(body)
    observations, groups = [], {}
    for fact in parsed["facts"]:
        metric, mapping = _metric(fact["concept"])
        axes = {axis: _axis_kind(axis) for axis in fact["dimensions"] if _axis_kind(axis)}
        if not metric or not axes or not fact["numeric"]:
            continue
        if not fact["end"] or fact["end"] > report_date:
            continue
        state = fact["state"]
        if not re.fullmatch(r"[A-Z]{3}", fact["unit"] or ""):
            state = "UNIT_UNRESOLVED"
        row = {"metric": metric, "concept": fact["concept"], "concept_mapping": mapping,
               "state": state, "amount": fact["value"] if state == "AVAILABLE" else None,
               "currency": fact["unit"], "period_start": fact["start"], "period_end": fact["end"],
               "dimensions": fact["dimensions"], "axes": axes, "context_ids": [fact["context_id"]],
               "raw_texts": [fact["raw_text"]], "issue": fact["issue"]}
        key = (row["concept"], row["period_start"], row["period_end"], row["currency"], tuple(sorted(row["dimensions"].items())))
        if key in groups:
            prior = groups[key]
            prior["context_ids"] = sorted(set(prior["context_ids"] + row["context_ids"]))
            prior["raw_texts"] = sorted(set(prior["raw_texts"] + row["raw_texts"]))
            if prior["amount"] != row["amount"] or prior["state"] != row["state"]:
                prior["state"], prior["amount"] = "CONFLICTING_VALUES", None
        else:
            groups[key] = row
            observations.append(row)
    observations.sort(key=lambda r: (r["period_end"], r["period_start"], r["concept"], sorted(r["dimensions"].items())), reverse=True)
    counts = []
    for fact in parsed["facts"]:
        if fact["concept"] in {"us-gaap:NumberOfOperatingSegments", "us-gaap:NumberOfReportableSegments"} and not fact["dimensions"]:
            if (fact["instant"] or fact["end"] or "") > report_date:
                continue
            item = {k: fact[k] for k in ("concept", "state", "value", "start", "end", "instant", "context_id", "raw_text")}
            if item not in counts:
                counts.append(item)
    available = sum(r["state"] == "AVAILABLE" for r in observations)
    state = ("PARTIAL" if available != len(observations) else "AVAILABLE") if available else ("UNAVAILABLE" if parsed["errors"] or observations else "NOT_TAGGED")
    return {"state": state, "observations": observations, "segment_count_observations": counts,
            "parser_errors": parsed["errors"], "fact_count": len(parsed["facts"])}


def build(asset_id, limit=3, fetch=True):
    bundle = load_reports(asset_id, limit=limit, fetch=fetch)
    reports = [{**{k: document[k] for k in DOCUMENT_FIELDS}, **extract(document["body"], document["report_date"])} for document in bundle["documents"]]
    available = any(r["state"] in {"AVAILABLE", "PARTIAL"} for r in reports)
    state = ("PARTIAL" if bundle["errors"] or any(r["state"] != "AVAILABLE" for r in reports) else "AVAILABLE") if available else ("UNAVAILABLE" if bundle["errors"] or not reports else "NOT_TAGGED")
    return {"schema_version": SCHEMA, "asset_id": asset_id,
            "collected_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "state": state, "report_limit": limit,
            "filings_source_sha256": bundle["filings_index"]["raw_payload_sha256"],
            "selected_accessions": [r["accession_number"] for r in select_reports(bundle["filings_index"], limit)],
            "reports": reports, "collection_errors": bundle["errors"], "_note": NOTE}


collect = build


def validate_artifact(data, asset_id):
    try:
        if data.get("schema_version") != SCHEMA or data.get("asset_id") != asset_id:
            return ["US segment schema/asset mismatch"]
        if dt.datetime.fromisoformat(data["collected_at"]).tzinfo is None:
            return ["US segment timestamp requires timezone"]
        index = load_filings_index(asset_id)
        selected = [r["accession_number"] for r in select_reports(index, data["report_limit"])]
        if data.get("selected_accessions") != selected or data.get("filings_source_sha256") != index["raw_payload_sha256"]:
            return ["US segment filing selection is stale"]
        reports = data["reports"]
        if not isinstance(reports, list) or len(reports) > 8:
            return ["US segment reports must be a bounded array"]
        problems = validate_document_sources(reports, asset_id)
        if problems:
            return problems
        if any(r["accession_number"] not in selected for r in reports):
            return ["US segment unexpected source filing"]
        expected_errors = {r["accession_number"] for r in data["collection_errors"] if r.get("accession_number")}
        if {r["accession_number"] for r in reports} | expected_errors != set(selected):
            return ["US segment missing source coverage"]
        for report in reports:
            expected = extract(read_document(report)["body"], report["report_date"])
            if any(report.get(key) != value for key, value in expected.items()):
                return ["US segment amounts, dimensions or periods differ from original re-extraction"]
        available = any(r["state"] in {"AVAILABLE", "PARTIAL"} for r in reports)
        state = ("PARTIAL" if data["collection_errors"] or any(r["state"] != "AVAILABLE" for r in reports) else "AVAILABLE") if available else ("UNAVAILABLE" if data["collection_errors"] or not reports else "NOT_TAGGED")
        if data.get("state") != state or data.get("_note") != NOTE:
            return ["US segment state/meaning differs from extraction"]
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return [f"US segments cannot be verified: {exc}"]
    return []


contract_errors = validate_artifact


def summary(data):
    """Small reported slices; truncation is explicit and detailed source stays local."""
    result = {"state": data["state"], "_note": NOTE, "reports": [], "collection_errors": data["collection_errors"]}
    remaining = 6500 - len(json.dumps(result))
    for report in data["reports"]:
        item = {"report_date": report["report_date"], "filing_date": report["filing_date"], "form": report["form"],
                "source_id": report["source_id"], "state": report["state"],
                "segment_count_observations": report["segment_count_observations"][:4], "slices": []}
        # The newest report carries comparative periods. Group by exact concept,
        # duration and axis set rather than letting axes overlap in a single sum.
        groups = {}
        for row in report["observations"]:
            key = (row["metric"], row["concept"], row["period_start"], row["period_end"], row["currency"], tuple(sorted(row["dimensions"])))
            groups.setdefault(key, []).append(row)
        keys = sorted(groups, key=lambda k: (k[3], "StatementBusinessSegmentsAxis" in " ".join(k[-1]), k[0] == "operating_income", k[2]), reverse=True)
        included = 0
        for key in keys:
            rows = groups[key]
            metric, concept, start, end, currency, axes = key
            sliced = {"metric": metric, "concept": concept, "period_start": start, "period_end": end,
                      "currency": currency, "axes": list(axes),
                      "members": [{"names": [r["dimensions"][a] for a in axes], "amount": r["amount"], "state": r["state"]} for r in rows]}
            size = len(json.dumps(sliced))
            if size > remaining - 500:
                continue
            item["slices"].append(sliced)
            remaining -= size
            included += len(rows)
        item["omitted_observations"] = len(report["observations"]) - included
        result["reports"].append(item)
        remaining -= len(json.dumps({k: v for k, v in item.items() if k != "slices"}))
        if remaining < 400:
            break
    result["omitted_reports"] = len(data["reports"]) - len(result["reports"])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("asset_id")
    parser.add_argument("--reports", type=int, choices=range(1, 5), default=3)
    args = parser.parse_args()
    data = build(args.asset_id, args.reports)
    errors = validate_artifact(data, args.asset_id)
    if errors:
        raise ValueError("; ".join(errors))
    path = write_json(KNOWLEDGE_DIR / "us_segments" / f"{args.asset_id}.json", data)
    print(path)
    print(json.dumps(summary(data), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
