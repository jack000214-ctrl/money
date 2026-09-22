"""Bounded SEC originals and a conservative, stdlib-only inline-XBRL reader.

Raw web objects are immutable. Facts retain their exact QName, dimensions, dates,
units and display metadata. Unsupported transforms never become guessed numbers.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
import re

from calculators.contracts import canonical_payload_hash, load_json, load_raw_snapshot
from cio.assets import get_asset
from cio.paths import KNOWLEDGE_DIR, ROOT
from collectors import web

PERIODIC_FORMS = {"10-K", "10-K/A", "10-Q", "10-Q/A"}
DOCUMENT_FIELDS = ("source_id", "path", "content_sha256", "url", "filing_date", "report_date", "form", "accession_number")
VOID = {"br", "hr", "img", "meta", "link", "input", "wbr", "source", "area", "base", "col", "embed", "param", "track"}


def _local(name):
    return name.split(":")[-1].lower()


class _Node:
    def __init__(self, tag, attrs):
        self.tag, self.attrs, self.children = tag, dict(attrs), []

    def text(self):
        if _local(self.tag) == "exclude":
            return ""
        return "".join(c if isinstance(c, str) else c.text() for c in self.children)

    def all(self, name):
        for child in self.children:
            if isinstance(child, _Node):
                if _local(child.tag) == name:
                    yield child
                yield from child.all(name)


class _InlineReader(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.roots, self.stack = [], []

    def handle_starttag(self, tag, attrs):
        if not self.stack and _local(tag) not in {"context", "unit", "nonfraction", "nonnumeric", "continuation"}:
            return
        node = _Node(tag, attrs)
        (self.stack[-1].children if self.stack else self.roots).append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i].tag == tag:
                self.stack[i:] = []
                break

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data):
        if self.stack:
            self.stack[-1].children.append(data)


def _nodes(roots, name):
    for node in roots:
        if _local(node.tag) == name:
            yield node
        yield from node.all(name)


def _first_text(node, name):
    return next((n.text().strip() for n in node.all(name)), None)


def _date(text):
    try:
        return dt.date.fromisoformat(text).isoformat()
    except (TypeError, ValueError):
        return None


def _number(text, attrs):
    fmt = _local(attrs.get("format") or "")
    raw = re.sub(r"\s+", "", text).replace("\u2212", "-")
    if fmt in {"zerodash", "numdash", "fixed-zero", "fixedzero"}:
        # XBRL Transformation Registry 4/5 fixed-zero explicitly accepts any text.
        if fmt not in {"fixed-zero", "fixedzero"} and raw not in {"-", "—", "–"}:
            raise ValueError("unsupported zero transform content")
        value = Decimal(0)
    elif fmt == "numwordsen":
        # SEC's English-number transform is broader; unsupported phrases stay
        # unresolved. These exact words cover reported segment counts.
        words = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty".split()
        if raw.lower() not in words:
            raise ValueError("unsupported English numeric phrase")
        value = Decimal(words.index(raw.lower()))
    else:
        if fmt in {"num-comma-decimal", "numcommadecimal"}:
            raw = raw.replace(".", "").replace(",", ".")
        elif fmt in {"num-dot-decimal", "numdotdecimal", "num-dot-decimal-in", "numdotdecimalin"}:
            raw = raw.replace(",", "")
        elif fmt:
            raise ValueError("unsupported numeric transform: " + fmt)
        if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", raw):
            raise ValueError("numeric text is not a decimal")
        value = Decimal(raw)
    scale = int(attrs.get("scale", "0"))
    if not -18 <= scale <= 18 or attrs.get("sign", "") not in {"", "-"}:
        raise ValueError("invalid scale or sign")
    value *= Decimal(10) ** scale
    if attrs.get("sign") == "-":
        value = -value
    if not value.is_finite():
        raise ValueError("non-finite number")
    return int(value) if value == value.to_integral_value() else float(value)


def parse_inline_xbrl(body: str) -> dict:
    reader = _InlineReader()
    reader.feed(body)
    contexts, units, errors = {}, {}, []
    for node in _nodes(reader.roots, "context"):
        key = node.attrs.get("id")
        dims, context_errors = {}, []
        for member in list(node.all("explicitmember")) + list(node.all("typedmember")):
            axis = member.attrs.get("dimension")
            value = member.text().strip()
            if not axis or not value or axis in dims:
                context_errors.append("invalid/duplicate dimension")
            else:
                dims[axis] = value
        start, end, instant = (_date(_first_text(node, f)) for f in ("startdate", "enddate", "instant"))
        if not (instant and not start and not end or start and end and not instant and start <= end):
            context_errors.append("unresolved period")
        context = {"start": start, "end": end, "instant": instant, "dimensions": dims,
                   "entity_identifier": _first_text(node, "identifier"), "errors": context_errors}
        if not key or key in contexts:
            errors.append("invalid/duplicate context id: " + str(key))
            if key in contexts:
                contexts[key]["errors"].append("duplicate context id")
        else:
            contexts[key] = context
    for node in _nodes(reader.roots, "unit"):
        key = node.attrs.get("id")
        numerator = next(node.all("unitnumerator"), None)
        denominator = next(node.all("unitdenominator"), None)
        def measures(n):
            return "*".join(m.text().strip().split(":")[-1] for m in n.all("measure"))
        unit = (measures(numerator) + "/" + measures(denominator)) if numerator and denominator else measures(node)
        if not key or not unit or key in units:
            errors.append("invalid/duplicate unit id: " + str(key))
            if key in units:
                units[key] = None
        else:
            units[key] = unit
    continuation_nodes = list(_nodes(reader.roots, "continuation"))
    continuations = {n.attrs.get("id"): n for n in continuation_nodes}
    facts = []
    for kind in ("nonfraction", "nonnumeric"):
        for node in _nodes(reader.roots, kind):
            attrs, raw = node.attrs, node.text().strip()
            continuation, seen = attrs.get("continuedat"), set()
            issue = None
            while continuation:
                if continuation in seen or continuation not in continuations:
                    issue = "unresolved continuation"
                    break
                seen.add(continuation)
                part = continuations[continuation]
                raw += " " + part.text().strip()
                continuation = part.attrs.get("continuedat")
            context = contexts.get(attrs.get("contextref"), {})
            nil = attrs.get("xsi:nil", "").lower() in {"true", "1"}
            numeric = kind == "nonfraction"
            unit = units.get(attrs.get("unitref")) if numeric else None
            value = None
            state = "NIL" if nil else "AVAILABLE"
            if not nil and not issue:
                try:
                    value = _number(raw, attrs) if numeric else raw
                except (ValueError, InvalidOperation, OverflowError) as exc:
                    issue = str(exc)
            if not context or context.get("errors"):
                issue = "unresolved context"
            if numeric and not unit:
                issue = "unresolved unit"
            if issue:
                state, value = "UNRESOLVED", None
            facts.append({"concept": attrs.get("name"), "value": value, "unit": unit,
                          "context_id": attrs.get("contextref"), "start": context.get("start"),
                          "end": context.get("end"), "instant": context.get("instant"),
                          "dimensions": context.get("dimensions", {}), "entity_identifier": context.get("entity_identifier"),
                          "raw_text": raw, "decimals": attrs.get("decimals"), "scale": attrs.get("scale"),
                          "sign": attrs.get("sign"), "format": attrs.get("format"),
                          "numeric": numeric, "nil": nil, "state": state, "issue": issue})
    if not facts:
        errors.append("NO_INLINE_XBRL_FACTS")
    return {"facts": facts, "contexts": contexts, "units": units, "errors": errors}


def load_filings_index(asset_id):
    asset = get_asset(asset_id)
    if asset.get("market") != "US" or asset.get("asset_type") != "STOCK" or asset.get("filing_profile") != "SEC_DOMESTIC_US_GAAP":
        raise ValueError("SEC domestic US-GAAP stock required")
    index = load_json(KNOWLEDGE_DIR / "filings" / f"{asset_id}.json")
    if index.get("schema_version") != "filing-index/1.0.0" or index.get("asset_id") != asset_id or index.get("source") != "SEC_EDGAR_SUBMISSIONS":
        raise ValueError("SEC filing index identity mismatch")
    digest = index.get("raw_payload_sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) or index.get("raw_locator") != f"data/raw/edgar/objects/{digest}.json":
        raise ValueError("SEC filing index source locator mismatch")
    raw = load_raw_snapshot(ROOT / index["raw_locator"], expected_digest=digest, expected_source="sec.gov/edgar")["payload"]
    if str(raw.get("cik", "")).zfill(10) != index.get("cik") or asset_id not in raw.get("tickers", []):
        raise ValueError("SEC filing index CIK/ticker mismatch")
    from collectors.edgar import filing_rows_from_submissions
    if not index.get("filings") or index["filings"] != filing_rows_from_submissions(raw, cik=index["cik"], forms=index.get("forms_filter"), limit=len(index["filings"])):
        raise ValueError("SEC filing index differs from saved submissions")
    return index


def select_reports(index, limit=3):
    """At most limit periods, latest annual included; amendments keep the base."""
    if not isinstance(limit, int) or not 1 <= limit <= 4:
        raise ValueError("report limit must be 1..4")
    rows = sorted((r for r in index.get("filings", []) if r.get("form") in PERIODIC_FORMS and _date(r.get("report_date"))), key=lambda r: (r["report_date"], r["filing_date"], r["accession_number"]), reverse=True)
    groups = {}
    for row in rows:
        groups.setdefault((row["report_date"], row["form"].split("/")[0]), []).append(row)
    keys = list(groups)
    selected = keys[:limit]
    annual = next((k for k in keys if k[1] == "10-K"), None)
    if limit > 1 and annual and annual not in selected:
        selected[-1] = annual
    result = []
    for key in selected:
        group = groups[key]
        result.append(group[0])
        if group[0]["form"].endswith("/A"):
            base = next((r for r in group if not r["form"].endswith("/A")), None)
            if base:
                result.append(base)
    return result


def filing_url(row, cik):
    accession = row.get("accession_number", "")
    name = row.get("primary_document", "")
    if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession) or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise ValueError("invalid SEC accession/document name")
    url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{name}"
    # The accession prefix can be a filing agent CIK, not the issuer CIK.
    if row.get("document_url") != url:
        raise ValueError("SEC document URL CIK/accession mismatch")
    return url


def read_document(metadata):
    digest = metadata.get("content_sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) or metadata.get("path") != f"data/raw/web/objects/{digest}.json":
        raise ValueError("SEC original locator mismatch")
    original = load_json(ROOT / metadata["path"])
    payload = original["payload"]
    if original.get("source") != "web" or original.get("payload_sha256") != digest or canonical_payload_hash(payload) != digest:
        raise ValueError("SEC original fingerprint mismatch")
    if metadata.get("source_id") != f"WEBSNAP:{digest[:12]}" or payload.get("requested_url") != metadata.get("url") or payload.get("final_url") != metadata.get("url"):
        raise ValueError("SEC original source URL mismatch")
    body = payload.get("body")
    if not isinstance(body, str) or web.shell_page_warning(payload):
        raise ValueError("SEC original has no usable body")
    raw = body.encode(payload.get("body_encoding") or "utf-8", errors="strict")
    if len(raw) != payload.get("content_length"):
        raise ValueError("SEC original byte length mismatch")
    return {**metadata, "body": body}


def load_reports(asset_id, limit=3, fetch=True):
    index = load_filings_index(asset_id)
    documents, errors = [], []
    stored = web.load_index(asset_id)["snapshots"]
    selected = select_reports(index, limit)
    for row in selected:
        url = filing_url(row, index["cik"])
        match = next((r for r in reversed(stored) if r.get("url") == url and r.get("requested_url", url) == url), None)
        try:
            if not match and fetch:
                match = web.snapshot(url, asset_id=asset_id, label="sec-report-" + row["accession_number"].replace("-", ""), note="Automatic SEC periodic original for scoped capital and segment facts")
            if not match:
                raise ValueError("ORIGINAL_NOT_COLLECTED")
            metadata = {**{k: match[k] for k in ("source_id", "path", "content_sha256")}, "url": url,
                        **{k: row[k] for k in ("filing_date", "report_date", "form", "accession_number")}}
            document = read_document(metadata)
            parsed = parse_inline_xbrl(document["body"])
            entities = {str(c.get("entity_identifier") or "").lstrip("0") for c in parsed["contexts"].values()}
            if entities and entities != {str(int(index["cik"]))}:
                raise ValueError("inline XBRL entity CIK differs from filing issuer")
            documents.append(document)
        except (OSError, ValueError, KeyError, SystemExit) as exc:
            errors.append({"accession_number": row["accession_number"], "url": url, "error": str(exc)})
    if not selected:
        errors.append({"error": "NO_PERIODIC_FILINGS"})
    return {"filings_index": index, "documents": documents, "errors": errors}


def validate_document_sources(documents, asset_id):
    try:
        index = load_filings_index(asset_id)
        rows = {r["accession_number"]: r for r in index["filings"]}
        seen = set()
        for document in documents:
            accession = document["accession_number"]
            if accession in seen or accession not in rows:
                raise ValueError("duplicate or unknown accession")
            seen.add(accession)
            row = rows[accession]
            if row["form"] not in PERIODIC_FORMS or document["url"] != filing_url(row, index["cik"]) or any(document[k] != row[k] for k in ("filing_date", "report_date", "form")):
                raise ValueError("SEC report metadata differs from filing index")
            loaded = read_document(document)
            parsed = parse_inline_xbrl(loaded["body"])
            entities = {str(c.get("entity_identifier") or "").lstrip("0") for c in parsed["contexts"].values()}
            if entities and entities != {str(int(index["cik"]))}:
                raise ValueError("SEC report entity CIK mismatch")
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return [str(exc)]
    return []
