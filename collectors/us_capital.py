"""Collect scoped US share counts and capital disclosures from retained SEC reports.

These are observations, not an estimate of fully diluted capital or an investment
judgment. Cover-page shares, period-end outstanding shares, EPS denominators,
authorized issuance and potential awards remain separate.
"""
from __future__ import annotations

import argparse
import datetime as dt
from decimal import Decimal, InvalidOperation
import html
import json
import re

from calculators.contracts import write_json
from cio.paths import KNOWLEDGE_DIR

SCHEMA = "us-capital/1.0.0"
SOURCE_FIELDS = ("source_id", "path", "content_sha256", "url", "filing_date", "report_date", "form", "accession_number")
FACT_FIELDS = ("concept", "value", "unit", "context_id", "start", "end", "instant", "dimensions", "raw_text", "decimals", "scale", "sign", "state", "nil")
CLASS_AXIS = "us-gaap:StatementClassOfStockAxis"
GROUPS = {
    "authorized_issued_treasury_shares": {"CommonStockSharesAuthorized", "CommonStockSharesIssued", "TreasuryStockShares"},
    "weighted_average_shares": {
        "WeightedAverageNumberOfSharesOutstandingBasic",
        "WeightedAverageNumberOfDilutedSharesOutstanding",
        "WeightedAverageNumberOfSharesOutstandingBasicAndDiluted",
    },
    "stock_compensation_expense": {"ShareBasedCompensation", "AllocatedShareBasedCompensationExpense"},
    "issuance_proceeds": {
        "ProceedsFromStockIssuance", "ProceedsFromIssuanceOfCommonStock",
        "ProceedsFromStockOptionsExercised", "ProceedsFromIssuanceOfSharesUnderIncentiveAndShareBasedCompensationPlansIncludingStockOptions",
        "ProceedsFromIssuanceOfSharesUnderIncentiveAndShareBasedCompensationPlansIncludingStockOptionsNetOfTaxes",
        "ProceedsFromIssuanceOfPreferredStockAndPreferenceStock",
    },
    "issued_during_period": {
        "StockIssuedDuringPeriodSharesNewIssues", "StockIssuedDuringPeriodValueNewIssues",
        "StockIssuedDuringPeriodSharesShareBasedCompensation", "StockIssuedDuringPeriodValueShareBasedCompensation",
        "StockIssuedDuringPeriodSharesStockOptionsExercised", "StockIssuedDuringPeriodSharesConversionOfConvertibleSecurities",
        "StockIssuedDuringPeriodSharesEmployeeStockPurchasePlan",
    },
    "potential_awards": {
        "ShareBasedCompensationArrangementByShareBasedPaymentAwardOptionsOutstandingNumber",
        "ShareBasedCompensationArrangementByShareBasedPaymentAwardOptionsExercisableNumber",
        "ShareBasedCompensationArrangementByShareBasedPaymentAwardEquityInstrumentsOtherThanOptionsNonvestedNumber",
        "ShareBasedCompensationArrangementByShareBasedPaymentAwardNumberOfSharesAvailableForGrant",
        "AntidilutiveSecuritiesExcludedFromComputationOfEarningsPerShareAmount",
        "EmployeeServiceShareBasedCompensationNonvestedAwardsTotalCompensationCostNotYetRecognized",
        "EmployeeServiceShareBasedCompensationNonvestedAwardsTotalCompensationCostNotYetRecognizedShareBasedAwardsOtherThanOptions",
        "EmployeeServiceShareBasedCompensationNonvestedAwardsTotalCompensationCostNotYetRecognizedStockOptions",
    },
    "convertible_debt": {
        "ConvertibleNotesPayable", "ConvertibleNotesPayableCurrent", "ConvertibleNotesPayableNoncurrent",
        "ConvertibleDebtCurrent", "ConvertibleDebtNoncurrent", "ConvertibleLongTermNotesPayable",
    },
    "repurchases": {"PaymentsForRepurchaseOfCommonStock", "StockRepurchasedDuringPeriodShares", "StockRepurchasedDuringPeriodValue"},
}
NOTES = [
    "Cover-page shares can describe one listed class even without a class dimension; they are not automatically the all-class valuation denominator.",
    "Only an explicitly reported dimensionless period-end common share total is treated as a reported total. Class components are reconciled only at that same date and never added to an already reported total.",
    "Weighted-average EPS shares are period denominators, not current outstanding shares. Issued shares, unvested awards and options are separate observations; no automatic fully diluted total is constructed.",
    "Missing observations mean NOT_EXTRACTED, never zero. ATM remaining capacity is reported only when an explicit amount and the report date occur together; capacity is not actual issuance or a current-day balance.",
]
PACKET_UNDIMENSIONED_FIRST = {
    "authorized_issued_treasury_shares", "stock_compensation_expense", "issued_during_period",
}


def _local(concept):
    return concept.split(":", 1)[-1]


def _clean(text):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text))).strip()


def _fact(fact):
    return {key: fact.get(key) for key in FACT_FIELDS}


def _identity(fact):
    return (fact.get("concept"), fact.get("unit"), fact.get("start"), fact.get("end"), fact.get("instant"),
            tuple(sorted((fact.get("dimensions") or {}).items())))


def _deduplicate(facts):
    """Keep conflicting facts visible; repeated table/narrative facts are one fact."""
    result, seen = [], set()
    for fact in facts:
        key = (_identity(fact), fact.get("value"), fact.get("state"), fact.get("nil"))
        if key not in seen:
            result.append(_fact(fact)); seen.add(key)
    return sorted(result, key=lambda f: (f.get("instant") or f.get("end") or "", f.get("start") or "", f["concept"], json.dumps(f.get("dimensions") or {}, sort_keys=True), str(f.get("value"))))


def _numeric(fact, unit=None):
    return (isinstance(fact.get("value"), (int, float)) and not isinstance(fact.get("value"), bool)
            and not fact.get("nil") and (unit is None or fact.get("unit") == unit))


def _conflicts(facts):
    groups = {}
    for fact in facts:
        if _numeric(fact):
            groups.setdefault(_identity(fact), []).append(fact)
    return [_deduplicate(rows) for rows in groups.values() if len({f["value"] for f in rows}) > 1]


def _single(facts):
    facts = _deduplicate(facts)
    values = {f["value"] for f in facts if _numeric(f, "shares") and f["value"] >= 0}
    if len(values) == 1 and len(values) and all(_numeric(f, "shares") and f["value"] >= 0 for f in facts):
        return {"state": "AVAILABLE", "value": next(iter(values)), "unit": "shares", "facts": facts}
    return {"state": "CONFLICTING_VALUES" if len(values) > 1 else "NOT_EXTRACTED", "value": None, "unit": "shares", "facts": facts}


_ATM_REMAINING_RE = re.compile(
    r"\bAs of\s+(?P<month>January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+(?P<day>\d{1,2}),?\s+(?P<year>20\d{2}),?\s+"
    r"(?:an amount of\s+)?\$\s*(?P<amount>[\d,]+(?:\.\d+)?)\s*"
    r"(?P<scale>thousand|million|billion)?\s+remained\s+available\s+for\s+(?:future\s+)?sales",
    re.I,
)
_ATM_SALES_TAIL_RE = re.compile(
    r"\s+under\s+the\s+(?:[\w-]+\s+){0,6}(?:Sales Agreement|ATM Program|ATM Offering)\b"
    r"|\s+of\s+(?:its\s+)?(?:Class [A-C]\s+)?common stock\b",
    re.I,
)


def _explicit_atm_remaining(excerpts, report_date):
    """Keep only a stated remaining amount at this filing's balance date."""
    candidates = []
    for index, excerpt in enumerate(excerpts):
        for match in _ATM_REMAINING_RE.finditer(excerpt["text"]):
            # The excerpt can include adjacent customer-contract prose. The
            # remaining amount must identify an equity sales agreement itself.
            preceding = excerpt["text"][max(0, match.start() - 700):match.start()]
            if (not re.search(r"\b(?:at.the.market|ATM offering|ATM program)\b", preceding, re.I)
                    or not re.search(r"\b(?:common stock|shares|equity securities)\b", preceding, re.I)):
                continue
            if not _ATM_SALES_TAIL_RE.match(excerpt["text"][match.end():match.end() + 100]):
                continue
            try:
                as_of = dt.datetime.strptime(
                    f"{match['month'].title()} {match['day']} {match['year']}", "%B %d %Y"
                ).date().isoformat()
                amount = Decimal(match["amount"].replace(",", "")) * {
                    None: 1, "thousand": 1000, "million": 1_000_000, "billion": 1_000_000_000,
                }[match["scale"].lower() if match["scale"] else None]
            except (ValueError, InvalidOperation):
                continue
            if as_of == report_date:
                candidates.append((int(amount) if amount == amount.to_integral_value() else float(amount), index))
    values = sorted({amount for amount, _ in candidates})
    if not values:
        return None
    if len(values) > 1:
        return {"state": "MULTIPLE_AMOUNTS_UNRESOLVED", "amount": None, "currency": "USD",
                "as_of": report_date, "scope": "REPORTED_AGREEMENT_NOT_ISSUER_TOTAL",
                "candidate_amounts": values}
    return {"state": "AVAILABLE", "amount": values[0], "currency": "USD",
            "as_of": report_date, "scope": "REPORTED_AGREEMENT_NOT_ISSUER_TOTAL",
            "excerpt_indices": sorted({index for _, index in candidates})}


def _disclosures(body, report_date):
    """Retain bounded original text and explicitly dated ATM remaining amounts."""
    # Hidden XBRL contexts are machine metadata, not prose disclosures.
    visible = re.sub(r"<ix:header\b.*?</ix:header>", " ", body, flags=re.I | re.S)
    text = _clean(visible)
    patterns = {
        "atm": r"\bat.the.market\b|\bsales agreement\b|\bATM (?:program|offering)",
        "convertible": r"\bconvertible (?:senior |unsecured )?(?:notes|debt|securities)\b|\bexchangeable shares\b",
        "share_class": r"\bClass [A-C] (?:common )?(?:stock|shares)\b",
    }
    result = {}
    for label, pattern in patterns.items():
        excerpts, last_end = [], -1
        for match in re.finditer(pattern, text, re.I):
            start, end = max(0, match.start() - 250), min(len(text), match.end() + 950)
            if start <= last_end:
                continue
            excerpts.append({"text": text[start:end], "text_offset": start})
            last_end = end
            if len(excerpts) == 4:
                break
        result[label] = {"state": "REVIEW_ORIGINAL" if excerpts else "NOT_EXTRACTED", "excerpts": excerpts}
    result["atm"].update(capacity=None, used=None,
                         remaining=_explicit_atm_remaining(result["atm"]["excerpts"], report_date),
                         _note="Only an explicitly stated report-date remaining amount is extracted. It is not current-day capacity or issued proceeds; other amounts need original review.")
    return result


def extract(body, report_date):
    from collectors.sec_inline import parse_inline_xbrl
    parsed = parse_inline_xbrl(body)
    facts = parsed["facts"]
    shares = [f for f in facts if f.get("concept") in {"dei:EntityCommonStockSharesOutstanding", "us-gaap:CommonStockSharesOutstanding"}]
    cover_facts = [f for f in shares if f["concept"].startswith("dei:") and f.get("instant")]
    cover_date = max((f["instant"] for f in cover_facts), default=None)
    # An undimensioned DEI cover fact may still be Class A only; do not call it a total.
    latest_cover_facts = _deduplicate([f for f in cover_facts if f["instant"] == cover_date])
    cover_state = ("CONFLICTING_VALUES" if _conflicts(latest_cover_facts) else
                   "AVAILABLE" if latest_cover_facts and all(_numeric(f, "shares") and f["value"] >= 0 for f in latest_cover_facts)
                   else "NOT_EXTRACTED")
    cover = {"as_of": cover_date, "state": cover_state,
             "facts": latest_cover_facts,
             "scope": "AS_DESCRIBED_ON_COVER_NOT_ASSUMED_ALL_CLASSES"}
    balance_facts = [f for f in shares if f["concept"] == "us-gaap:CommonStockSharesOutstanding"
                     and f.get("instant") == report_date and not f.get("dimensions")]
    balance = {"as_of": report_date, **_single(balance_facts), "scope": "REPORTED_DIMENSIONLESS_COMMON_STOCK_TOTAL"}
    by_class = {}
    for fact in shares:
        dims = fact.get("dimensions") or {}
        if fact["concept"] == "us-gaap:CommonStockSharesOutstanding" and fact.get("instant") == report_date and set(dims) == {CLASS_AXIS}:
            by_class.setdefault(dims[CLASS_AXIS], []).append(fact)
    classes = [{"class_member": member, "as_of": report_date, **_single(rows)} for member, rows in sorted(by_class.items())]
    component_sum = sum(row["value"] for row in classes) if classes and all(row["state"] == "AVAILABLE" for row in classes) else None
    reconciliation = {"state": "NOT_COMPARABLE", "component_sum": component_sum, "reported_total": balance["value"], "difference": None,
                      "_note": "The sum of observed classes alone does not establish completeness or equal economic rights."}
    if component_sum is not None and balance["value"] is not None:
        reconciliation.update(state="MATCH" if component_sum == balance["value"] else "MISMATCH", difference=component_sum-balance["value"])
    groups = {}
    for group, concepts in GROUPS.items():
        selected = [f for f in facts if f.get("concept", "").startswith("us-gaap:") and _local(f["concept"]) in concepts]
        # Standard tagged values are retained for historical/current periods. We do
        # not merge periods, sum nested dimensions, or treat a duration as an instant.
        observations = _deduplicate(selected)
        conflicts = _conflicts(observations)
        groups[group] = {"state": "CONFLICTING_VALUES" if conflicts else "AVAILABLE" if any(_numeric(f) for f in observations) else "NOT_EXTRACTED",
                         "facts": observations, "conflicts": conflicts}
    custom = _deduplicate([f for f in facts if f.get("concept", "").split(":")[0] not in {"us-gaap", "dei", "srt"}
                           and f.get("numeric") and re.search(r"StockIssued|ShareBased|Convertible|AtTheMarket|IssuanceOfCommonStock", _local(f["concept"]), re.I)])
    return {"cover_shares": cover, "period_end_shares": balance, "share_classes": classes,
            "class_reconciliation": reconciliation, "share_observations": _deduplicate(shares),
            "metrics": groups, "issuer_extension_candidates": custom,
            "disclosures": _disclosures(body, report_date), "parse_errors": parsed.get("errors", [])}


def _share_history(reports):
    """Same concept/unit/dimensions only; last filed restatement supersedes prior."""
    history = {}
    for report in sorted(reports, key=lambda r: (r["filing_date"], r["accession_number"])):
        for fact in report["share_observations"]:
            if fact["concept"] != "us-gaap:CommonStockSharesOutstanding" or fact.get("dimensions") or not fact.get("instant"):
                continue
            date = fact["instant"]
            rows = [f for f in report["share_observations"] if f["concept"] == fact["concept"] and not f.get("dimensions") and f.get("instant") == date]
            value = _single(rows)
            history[date] = {"as_of": date, "value": value["value"], "state": value["state"], "source_id": report["source_id"], "unit": "shares"}
    ordered = [history[date] for date in sorted(history)]
    for i, row in enumerate(ordered):
        prior = ordered[i-1] if i else None
        row["change_from_previous"] = None
        if prior and row["state"] == prior["state"] == "AVAILABLE":
            row["change_from_previous"] = {"from": prior["as_of"], "shares": row["value"] - prior["value"],
                                            "pct": (row["value"] / prior["value"] - 1) * 100 if prior["value"] > 0 else None,
                                            "_note": "Net change in reported outstanding shares, not gross issuance or a split-adjusted dilution rate."}
    return ordered


def build(asset_id, documents, *, collection_errors=None, limit=3):
    reports = []
    for doc in documents:
        reports.append({**{key: doc[key] for key in SOURCE_FIELDS}, **extract(doc["body"], doc["report_date"])})
    reports.sort(key=lambda r: (r["report_date"], r["filing_date"], r["accession_number"]), reverse=True)
    available = any(r["cover_shares"]["state"] == "AVAILABLE" or r["period_end_shares"]["state"] == "AVAILABLE"
                    or any(g["state"] == "AVAILABLE" for g in r["metrics"].values()) for r in reports)
    partial = collection_errors or any(r["parse_errors"] for r in reports)
    return {"schema_version": SCHEMA, "asset_id": asset_id,
            "collected_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "report_limit": limit, "state": ("PARTIAL" if partial else "AVAILABLE") if available else "NOT_EXTRACTED",
            "reports": reports, "share_history": _share_history(reports),
            "collection_errors": collection_errors or [], "_notes": NOTES}


def collect(asset_id, limit=3, *, fetch=True):
    from cio.assets import get_asset
    from collectors.sec_inline import load_reports
    asset = get_asset(asset_id)
    if (asset.get("market"), asset.get("asset_type"), asset.get("filing_source")) != ("US", "STOCK", "SEC_EDGAR"):
        raise ValueError("US SEC domestic stock reports only")
    loaded = load_reports(asset_id, limit=limit, fetch=fetch)
    return build(asset_id, loaded["documents"], collection_errors=loaded.get("errors", []), limit=limit)


def validate_artifact(data, asset_id):
    from collectors.sec_inline import (load_filings_index, read_document,
                                       select_reports, validate_document_sources)
    try:
        if data.get("schema_version") != SCHEMA or data.get("asset_id") != asset_id:
            return ["US capital schema or asset mismatch"]
        reports = data["reports"]
        if not isinstance(reports, list) or not reports:
            return ["US capital has no retained periodic reports"]
        errors = validate_document_sources(reports, asset_id)
        if errors:
            return errors
        selected = {row["accession_number"] for row in select_reports(load_filings_index(asset_id), data["report_limit"])}
        reported = {report["accession_number"] for report in reports}
        collection_errors = data.get("collection_errors")
        if not isinstance(collection_errors, list) or any(not isinstance(error, dict) or not error.get("accession_number") for error in collection_errors):
            return ["US capital collection errors must identify selected filings"]
        failed = [error["accession_number"] for error in collection_errors]
        if (len(failed) != len(set(failed)) or reported & set(failed)
                or reported | set(failed) != selected):
            return ["US capital selected filing coverage is incomplete or inconsistent"]
        documents = []
        for report in reports:
            read = read_document(report)
            body = read["body"] if isinstance(read, dict) else read
            documents.append({**{key: report[key] for key in SOURCE_FIELDS}, "body": body})
        expected = build(asset_id, documents, collection_errors=data.get("collection_errors"), limit=data["report_limit"])
        for field in ("state", "reports", "share_history", "_notes"):
            if data.get(field) != expected[field]:
                return [f"US capital {field} differs from retained original extraction"]
        return []
    except (KeyError, TypeError, ValueError, OSError) as exc:
        return [f"US capital validation failed: {exc}"]


contract_errors = validate_artifact


def summary(data):
    reports = data.get("reports") or []
    if not reports:
        return {"state": "NOT_EXTRACTED", "collection_errors": data.get("collection_errors", [])}
    latest = reports[0]
    metrics = {}
    for name, group in latest["metrics"].items():
        current = [f for f in group["facts"] if (f.get("instant") or f.get("end")) == latest["report_date"]]
        # Keep dimensional facts in the retained artifact; they may overlap totals.
        preferred = [f for f in current if not f.get("dimensions")]
        scoped = [f for f in current if f.get("dimensions")]
        # These scoped facts can repeat the same concept/period as a reported
        # company-wide total. Omit only those duplicates from the packet: scoped
        # share-count concepts without a dimensionless equivalent still matter.
        if name in PACKET_UNDIMENSIONED_FIRST and preferred:
            total_keys = {(f["concept"], f.get("unit"), f.get("start"), f.get("end"), f.get("instant"))
                          for f in preferred}
            distinct_scoped = [f for f in scoped
                               if (f["concept"], f.get("unit"), f.get("start"), f.get("end"), f.get("instant"))
                               not in total_keys]
            selected = (preferred + distinct_scoped)[:8]
        else:
            selected = (preferred + scoped)[:8]
        metrics[name] = {"state": "CONFLICTING_VALUES" if _conflicts(current) else "AVAILABLE" if any(_numeric(f) for f in current) else "NOT_EXTRACTED",
                         "facts": [{k: f.get(k) for k in ("concept", "value", "unit", "start", "end", "instant", "dimensions", "state")} for f in selected],
                         "scoped_observation_count": len(scoped), "omitted_current_observations": len(current)-len(selected)}
    cover = {key: value for key, value in latest["cover_shares"].items() if key != "facts"}
    cover["facts"] = [{k: f.get(k) for k in ("concept", "value", "unit", "dimensions", "state")} for f in latest["cover_shares"]["facts"]]
    period_end = {key: value for key, value in latest["period_end_shares"].items() if key != "facts"}
    share_classes = [{key: value for key, value in row.items() if key != "facts"} for row in latest["share_classes"]]
    return {"state": data["state"], "report_date": latest["report_date"], "filing_date": latest["filing_date"], "source_id": latest["source_id"],
            "cover_shares": cover, "period_end_shares": period_end, "share_classes": share_classes,
            "class_reconciliation": latest["class_reconciliation"], "share_history": data["share_history"][-6:], "metrics": metrics,
            "atm": {"state": latest["disclosures"]["atm"]["state"], "capacity": None, "used": None,
                    "remaining": latest["disclosures"]["atm"]["remaining"],
                    "excerpt_count": len(latest["disclosures"]["atm"]["excerpts"])},
            "collection_errors": data.get("collection_errors", []), "_notes": NOTES}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("asset_id")
    parser.add_argument("--reports", type=int, choices=range(1, 5), default=3)
    parser.add_argument("--no-fetch", action="store_true")
    args = parser.parse_args()
    data = collect(args.asset_id, args.reports, fetch=not args.no_fetch)
    errors = validate_artifact(data, args.asset_id)
    if errors:
        raise ValueError("; ".join(errors))
    path = write_json(KNOWLEDGE_DIR / "us_capital" / f"{args.asset_id}.json", data)
    print(path)
    print(json.dumps(summary(data), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
