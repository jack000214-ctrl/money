"""Read announced earnings dates from explicitly registered official IR sources.

This collector never estimates an earnings date. Publication timestamps, accounting
periods, conference appearances and dates without a year are not event dates.
Original HTTP bytes remain in collectors.web's content-addressed snapshots.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import html
from html.parser import HTMLParser
import io
import json
import re
from urllib.parse import urljoin, urlsplit, urldefrag
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

from calculators.contracts import canonical_payload_hash, content_hash, load_json, write_json
from cio.assets import get_asset
from cio.paths import KNOWLEDGE_DIR, ROOT
from collectors.web import snapshot

SCHEMA_VERSION = "us-earnings/1.0.0"
OUTPUT_DIR = KNOWLEDGE_DIR / "us_earnings"
MAX_SOURCES = 6
MAX_FOLLOW = 8
EARNINGS_RE = re.compile(r"\b(?:earnings\s+(?:call|announcement|release|results|webcast)|(?:financial|quarter(?:ly)?|full[- ]year)\s+results|results\s+(?:call|webcast))\b", re.I)
FOLLOW_RE = re.compile(r"earnings|financial[- ]results|quarter.*results|results.*quarter|\.ics(?:$|\?)|rss|atom", re.I)
MONTHS = {name[:3].lower(): index for index, name in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"], 1)}
DATE_RE = re.compile(r"\b(?:(?P<month>Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?\s*,?\s+(?P<year>20\d{2})|(?P<iso>20\d{2}-\d{2}-\d{2}))\b", re.I)
PRELIMINARY_RE = re.compile(r"preliminary|tentative|provisional|subject to (?:change|confirmation)", re.I)
SCHEDULE_RE = re.compile(r"\b(?:will|plans? to|is scheduled to|expects? to)\b.{0,110}\b(?:release|report|announce|hold|host|post|discuss)\b", re.I)
NO_EVENTS_RE = re.compile(r"no\s+upcoming\s+events|no\s+events\s+(?:are\s+)?(?:scheduled|available)", re.I)


def _clean(value):
    return re.sub(r"\s+", " ", html.unescape(str(value))).strip()


def _date(value):
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def _dates(text):
    found = []
    for match in DATE_RE.finditer(text):
        try:
            value = (_date(match["iso"]) if match["iso"] else
                     dt.date(int(match["year"]), MONTHS[match["month"][:3].lower()], int(match["day"])))
        except ValueError:
            continue
        if value:
            found.append((value.isoformat(), match.start(), match.end()))
    return found


class _Node:
    def __init__(self, tag, attrs=None, parent=None):
        self.tag, self.attrs, self.parent = tag, dict(attrs or []), parent
        self.parts = []

    def text(self):
        return _clean(" ".join(part.text() if isinstance(part, _Node) else part for part in self.parts))

    def descendants(self):
        for part in self.parts:
            if isinstance(part, _Node):
                yield part
                yield from part.descendants()


class _HTML(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, body):
        super().__init__(convert_charrefs=True)
        self.root = _Node("root")
        self.stack = [self.root]
        self.feed(body)

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, attrs, self.stack[-1])
        self.stack[-1].parts.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                self.stack = self.stack[:index]
                break

    def handle_data(self, data):
        if not any(node.tag in {"style", "script"} for node in self.stack):
            self.stack[-1].parts.append(data)


def _candidate(date, title, method, excerpt):
    return {"date": date, "title": _clean(title)[:240], "method": method,
            "date_status": "ANNOUNCED_PRELIMINARY" if PRELIMINARY_RE.search(excerpt) else "ANNOUNCED",
            "excerpt": _clean(excerpt)[:800]}


def _prose(text):
    """Only explicit scheduling clauses, never an article's publication date."""
    observations, issues = [], []
    for sentence in re.split(r"(?<=[.!?])\s+(?=[A-Z])|[\r\n]+", text):
        sentence = _clean(sentence)
        schedule = SCHEDULE_RE.search(sentence)
        if not schedule or not EARNINGS_RE.search(sentence):
            continue
        # The event date must follow the scheduling verb and an explicit connector.
        clause = sentence[schedule.start():]
        dates = [date for date, start, _ in _dates(clause)
                 if re.search(r"\b(?:on|for)\s+(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?$", clause[:start], re.I)]
        if len(set(dates)) == 1:
            observations.append(_candidate(dates[0], sentence, "EXPLICIT_SCHEDULE", sentence))
        elif dates:
            issues.append("Conflicting dates in an earnings scheduling sentence")
        else:
            issues.append("Earnings scheduling sentence lacks an unambiguous event date with year")
    return observations, issues


def _json_events(value):
    if isinstance(value, list):
        for child in value:
            yield from _json_events(child)
    elif isinstance(value, dict):
        types = value.get("@type", [])
        if isinstance(types, str):
            types = [types]
        if any(str(kind).rsplit("/", 1)[-1] in {"Event", "BusinessEvent"} for kind in types):
            yield value
        for key, child in value.items():
            if isinstance(child, (list, dict)):
                yield from _json_events(child)


def extract(body, url, content_type=""):
    """Pure extraction, replayed by validate_artifact against preserved originals."""
    observations, issues, links = [], [], []
    interpretable = False
    if body.lstrip().startswith("BEGIN:VCALENDAR"):
        interpretable = True
        unfolded = re.sub(r"\r?\n[ \t]", "", body)
        for event in re.findall(r"BEGIN:VEVENT\s*(.*?)END:VEVENT", unfolded, re.S):
            fields = {}
            for line in event.splitlines():
                key, sep, value = line.partition(":")
                if sep:
                    fields[key.split(";", 1)[0].upper()] = value
            title = fields.get("SUMMARY", "")
            if not EARNINGS_RE.search(title):
                continue
            raw = fields.get("DTSTART", "")
            date = _date(f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}") if re.match(r"20\d{6}(?:T|$)", raw) else None
            if fields.get("STATUS") == "CANCELLED":
                issues.append("An earnings event is explicitly cancelled")
            elif date:
                observations.append(_candidate(date.isoformat(), title, "ICS_DTSTART", title + " " + raw))
            else:
                issues.append("Earnings calendar event has no supported DTSTART")
    elif body.lstrip().startswith("<?xml") or re.match(r"\s*<(?:rss|feed)\b", body):
        try:
            xml = ET.fromstring(body)
            items = [node for node in xml.iter() if node.tag.split("}")[-1] in {"item", "entry"}]
            interpretable = xml.tag.split("}")[-1] in {"rss", "feed"}
            for item in items:
                fields = {node.tag.split("}")[-1]: node for node in item}
                title = _clean("".join(fields["title"].itertext())) if "title" in fields else ""
                text = " ".join("".join(fields[key].itertext()) for key in ("description", "summary", "content") if key in fields)
                clean_text = _HTML(text).root.text()
                rows, problems = _prose(clean_text)
                observations.extend(rows); issues.extend(problems)
                if FOLLOW_RE.search(title):
                    for node in item:
                        if node.tag.split("}")[-1] == "link":
                            links.append(node.attrib.get("href") or (node.text or "").strip())
        except ET.ParseError:
            issues.append("Malformed RSS/Atom XML")
    else:
        tree = _HTML(body)
        nodes = list(tree.root.descendants())
        text = tree.root.text()
        # A JS shell, access-denied page or navigation alone is not evidence of absence.
        interpretable = bool(NO_EVENTS_RE.search(text) or (len(text) > 100 and EARNINGS_RE.search(text) and _dates(text)))
        for script in re.findall(r'<script\b[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', body, re.I | re.S):
            try:
                value = json.loads(script)
            except (ValueError, TypeError):
                issues.append("Malformed JSON-LD")
                continue
            for event in _json_events(value):
                title = str(event.get("name", ""))
                if not EARNINGS_RE.search(title):
                    continue
                interpretable = True
                if str(event.get("eventStatus", "")).endswith("EventCancelled"):
                    issues.append("An earnings event is explicitly cancelled")
                    continue
                date = _date(event.get("startDate"))
                if date:
                    observations.append(_candidate(date.isoformat(), title, "JSON_LD_START_DATE", title + " " + str(event["startDate"])))
                else:
                    issues.append("Earnings JSON-LD event has no explicit startDate with year")
        event_page = bool(re.search(r"/(?:events?|event-details)(?:/|$)", urlsplit(url).path))
        for node in nodes:
            if node.tag == "a" and node.attrs.get("href"):
                href = node.attrs["href"]
                if FOLLOW_RE.search(node.text() + " " + href):
                    links.append(urljoin(url, href))
            if node.tag == "link" and node.attrs.get("type") in {"application/rss+xml", "application/atom+xml"}:
                links.append(urljoin(url, node.attrs.get("href", "")))
            # On an events page, pair a named event with a date inside the smallest
            # enclosing card. A news article title + publication date is not a card.
            is_heading = node.tag in {"h1", "h2", "h3", "h4", "h5", "c4d-content-item-heading"}
            if event_page and is_heading and EARNINGS_RE.search(node.text()):
                parent = node.parent
                for _ in range(3):
                    if parent is None:
                        break
                    content = parent.text()
                    dates = {item[0] for item in _dates(content)}
                    if len(content) <= 1600 and len(dates) == 1:
                        observations.append(_candidate(next(iter(dates)), node.text(), "EVENT_CARD", content))
                        break
                    if len(content) > 1600 or len(dates) > 1:
                        break
                    parent = parent.parent
            if node.tag in {"p", "li"}:
                rows, problems = _prose(node.text())
                observations.extend(rows); issues.extend(problems)
            # Tesla-style disclosures tables explicitly identify their date column.
            if node.tag == "table":
                rows = [n for n in node.descendants() if n.tag == "tr"]
                header = next((r for r in rows if "earnings date" in r.text().lower()), None)
                if header:
                    cells = [n for n in header.descendants() if n.tag in {"th", "td"}]
                    date_index = next((i for i, n in enumerate(cells) if n.text().lower() == "earnings date"), None)
                    if date_index is not None:
                        interpretable = True
                        for row in rows[rows.index(header) + 1:]:
                            cells = [n for n in row.descendants() if n.tag in {"th", "td"}]
                            if len(cells) <= date_index:
                                continue
                            dates = _dates(cells[date_index].text())
                            if len(dates) == 1:
                                observations.append(_candidate(dates[0][0], "Quarterly earnings: " + row.text(), "EARNINGS_DATE_COLUMN", row.text()))
    unique = {(row["date"], row["title"], row["method"], row["date_status"]): row for row in observations}
    return {"interpretable": interpretable, "observations": sorted(unique.values(), key=lambda r: (r["date"], r["title"], r["method"])),
            "issues": sorted(set(issues)), "links": list(dict.fromkeys(link for link in links if link))}


def _allowed(url, hosts):
    parsed = urlsplit(url)
    return parsed.scheme == "https" and parsed.hostname in hosts and not parsed.username and not parsed.password and parsed.port in {None, 443}


def _derive(sources, errors, today):
    future = []
    for source in sources:
        for row in source["extraction"]["observations"]:
            if _date(row["date"]) >= today:
                future.append({**row, "source_id": source["source_id"], "url": source["url"]})
    dates = sorted({row["date"] for row in future})
    partial = bool(errors or any(source["extraction"]["issues"] for source in sources))
    interpretable = any(source["extraction"]["interpretable"] for source in sources)
    next_event = None
    if len(dates) > 1:
        status, reason = "PARTIAL", "Conflicting future earnings dates remain unresolved; no date selected"
    elif dates:
        selected = [row for row in future if row["date"] == dates[0]]
        preliminary = any(row["date_status"] == "ANNOUNCED_PRELIMINARY" for row in selected)
        next_event = {"date": dates[0], "date_status": "ANNOUNCED_PRELIMINARY" if preliminary else "ANNOUNCED", "estimated": False,
                      "title": selected[0]["title"], "source_ids": sorted({row["source_id"] for row in selected}),
                      "source_urls": sorted({row["url"] for row in selected}), "calendar_days_remaining": (_date(dates[0]) - today).days}
        status, reason = ("PARTIAL" if partial else "AVAILABLE"), "Issuer-published earnings date; preliminary qualifiers are preserved"
    elif interpretable:
        status = "PARTIAL" if partial else "NO_ANNOUNCEMENT"
        reason = "No upcoming earnings date extracted from the checked official sources; bounded coverage is not proof that no announcement exists elsewhere"
    else:
        status = "NETWORK_ERROR" if errors and not sources and all(e["kind"] == "NETWORK_ERROR" for e in errors) else "PARSE_ERROR"
        reason = "Official sources could not be fetched or interpreted; absence of an announcement is not established"
    return {"status": status, "reason": reason, "next_earnings": next_event,
            "conflicting_dates": dates if len(dates) > 1 else []}


def _read_source(ref, requested_url, fetched_at, hosts):
    path = ROOT / ref["path"]
    document = load_json(path)
    payload = document["payload"]
    raw = payload["body"].encode(payload["body_encoding"], errors="strict")
    if not _allowed(payload["final_url"], hosts):
        raise ValueError("redirect left the configured official IR hosts")
    return {"source_id": ref["source_id"], "url": payload["final_url"], "requested_url": requested_url,
            "path": ref["path"], "content_sha256": ref["content_sha256"], "raw_file_sha256": content_hash(path),
            "body_sha256": hashlib.sha256(raw).hexdigest(), "body_bytes": len(raw), "fetched_at": fetched_at,
            "extraction": extract(payload["body"], payload["final_url"], payload.get("content_type") or "")}


def build(asset_id, today=None):
    asset = get_asset(asset_id)
    if asset["market"] != "US" or asset["asset_type"] != "STOCK":
        raise ValueError("Official IR earnings dates apply to registered US stocks")
    today = today or dt.datetime.now(ZoneInfo("America/New_York")).date()
    configured = asset.get("ir_sources") or []
    if not configured:
        raise ValueError("ir_sources is not registered; add official issuer IR URLs")
    if not isinstance(configured, list) or len(configured) > MAX_SOURCES or any(not isinstance(url, str) for url in configured):
        raise ValueError("ir_sources must contain at most six official URLs")
    hosts = {urlsplit(url).hostname for url in configured}
    if any(not _allowed(url, hosts) for url in configured):
        raise ValueError("ir_sources must be HTTPS URLs without credentials or custom ports")
    collected_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    sources, errors, visited = [], [], set()
    queue = [(url, 0) for url in configured]
    followed = 0
    while queue:
        url, depth = queue.pop(0)
        url = urldefrag(url)[0]
        if url in visited:
            continue
        visited.add(url)
        stderr = io.StringIO()
        try:
            with contextlib.redirect_stderr(stderr):
                ref = snapshot(url, asset_id=asset_id, label="us-earnings-ir", note="Official issuer earnings schedule; deterministic bounded extraction")
            source = _read_source(ref, url, collected_at, hosts)
            sources.append(source)
            if depth == 0:
                for link in source["extraction"]["links"]:
                    link = urldefrag(urljoin(url, link))[0]
                    if followed < MAX_FOLLOW and _allowed(link, hosts) and link not in visited and all(link != q[0] for q in queue):
                        queue.append((link, 1)); followed += 1
        except SystemExit:
            message = stderr.getvalue().strip() or "HTTP snapshot failed"
            kind = "PARSE_ERROR" if any(word in message for word in ("PDF", "바이너리", "인코딩", "UTF", "본문")) else "NETWORK_ERROR"
            errors.append({"url": url, "kind": kind, "reason": message[:400]})
        except (OSError, ValueError, TypeError, KeyError, UnicodeError) as exc:
            errors.append({"url": url, "kind": "PARSE_ERROR", "reason": str(exc)[:400]})
    return {"schema_version": SCHEMA_VERSION, "asset_id": asset_id, "source_id": f"US_EARNINGS:{asset_id}",
            "source_grade": "PRIMARY", "collected_at": collected_at, "as_of_date": today.isoformat(),
            "configured_sources": configured, "sources": sources, "errors": errors,
            **_derive(sources, errors, today), "_note": "Official announced earnings dates only. SEC report-filing estimates are a separate observation. Calendar days use the America/New_York collection date; event timezone is not inferred."}


def validate_artifact(data, asset_id):
    try:
        if data.get("schema_version") != SCHEMA_VERSION or data.get("asset_id") != asset_id or data.get("source_id") != f"US_EARNINGS:{asset_id}":
            return ["US earnings schema, asset or source ID mismatch"]
        today = _date(data["as_of_date"])
        if not today or not isinstance(data["sources"], list) or not isinstance(data["errors"], list):
            return ["US earnings sources/errors/as_of_date are invalid"]
        configured = get_asset(asset_id).get("ir_sources") or []
        if configured != data["configured_sources"]:
            return ["Official IR source registration changed"]
        hosts = {urlsplit(url).hostname for url in configured}
        if len(data["sources"]) + len(data["errors"]) > len(configured) + MAX_FOLLOW:
            return ["US earnings crawl exceeded its bound"]
        for source in data["sources"]:
            path = (ROOT / source["path"]).resolve()
            if not path.is_relative_to(ROOT.resolve()) or content_hash(path) != source["raw_file_sha256"]:
                return ["US earnings raw source path/hash mismatch"]
            document = load_json(path)
            payload = document["payload"]
            digest = canonical_payload_hash(payload)
            if digest != source["content_sha256"] or digest != document["payload_sha256"] or source["source_id"] != f"WEBSNAP:{digest[:12]}":
                return ["US earnings snapshot payload hash mismatch"]
            expected = _read_source(source, source["requested_url"], source["fetched_at"], hosts)
            if source != expected or payload["requested_url"] != source["requested_url"] or source["body_bytes"] != payload["content_length"]:
                return ["US earnings values differ from original bytes/re-extraction"]
        expected = _derive(data["sources"], data["errors"], today)
        if any(data.get(key) != value for key, value in expected.items()):
            return ["US earnings summary differs from preserved observations"]
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, AttributeError) as exc:
        return [f"US earnings artifact cannot be verified: {exc}"]
    return []


def summary(data):
    return {key: data.get(key) for key in ("source_id", "as_of_date", "status", "reason", "next_earnings", "conflicting_dates")} | {
        "source_count": len(data.get("sources") or []), "errors": data.get("errors") or [],
        "issues": sorted({issue for source in data.get("sources", []) for issue in source["extraction"]["issues"]})}


def save(asset_id, data):
    return write_json(OUTPUT_DIR / f"{asset_id}.json", data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("asset_id")
    args = parser.parse_args()
    asset_id = args.asset_id.upper()
    data = build(asset_id)
    errors = validate_artifact(data, asset_id)
    if errors:
        raise ValueError("; ".join(errors))
    save(asset_id, data)
    print(json.dumps(summary(data), ensure_ascii=False, indent=2))
    return 0 if data["status"] in {"AVAILABLE", "NO_ANNOUNCEMENT", "PARTIAL"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
