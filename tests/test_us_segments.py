"""SEC segment facts keep their axis, reported period and currency boundaries."""

from __future__ import annotations

from copy import deepcopy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from calculators.contracts import canonical_payload_hash
from collectors import us_segments as segments


REPORT_DATE = "2026-06-30"
REVENUE = "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax"
BUSINESS = "us-gaap:StatementBusinessSegmentsAxis"
PRODUCT = "srt:ProductOrServiceAxis"
GEOGRAPHY = "srt:StatementGeographicalAxis"


def _context(name, start, end, axis, member):
    return (
        f'<xbrli:context id="{name}"><xbrli:entity>'
        '<xbrli:identifier>123</xbrli:identifier><xbrli:segment>'
        f'<xbrldi:explicitMember dimension="{axis}">{member}</xbrldi:explicitMember>'
        '</xbrli:segment></xbrli:entity><xbrli:period>'
        f'<xbrli:startDate>{start}</xbrli:startDate><xbrli:endDate>{end}</xbrli:endDate>'
        '</xbrli:period></xbrli:context>'
    )


def _fact(context, value, *, unit="usd", concept=REVENUE):
    return (f'<ix:nonFraction name="{concept}" contextRef="{context}" '
            f'unitRef="{unit}" scale="6" format="ixt:num-dot-decimal">'
            f'{value}</ix:nonFraction>')


def _fixture():
    contexts = [
        _context("cloud_q", "2026-04-01", REPORT_DATE, BUSINESS, "issuer:CloudMember"),
        _context("consulting_q", "2026-04-01", REPORT_DATE, BUSINESS, "issuer:ConsultingMember"),
        _context("cloud_ytd", "2026-01-01", REPORT_DATE, BUSINESS, "issuer:CloudMember"),
        _context("product_q", "2026-04-01", REPORT_DATE, PRODUCT, "issuer:SoftwareMember"),
        _context("geography_q", "2026-04-01", REPORT_DATE, GEOGRAPHY, "country:US"),
        _context("future_q", "2026-07-01", "2026-09-30", BUSINESS, "issuer:CloudMember"),
    ]
    facts = [
        _fact("cloud_q", "10"),
        _fact("consulting_q", "20"),
        _fact("cloud_ytd", "18"),
        _fact("product_q", "30"),
        _fact("geography_q", "30"),
        _fact("cloud_q", "7", unit="eur"),
        _fact("cloud_q", "2", concept="us-gaap:OperatingIncomeLoss"),
        _fact("future_q", "99"),
    ]
    return (
        '<html><ix:header>'
        '<xbrli:unit id="usd"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>'
        '<xbrli:unit id="eur"><xbrli:measure>iso4217:EUR</xbrli:measure></xbrli:unit>'
        '<xbrli:unit id="shares"><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unit>'
        + ''.join(contexts) + '</ix:header>' + ''.join(facts) + '</html>'
    )


def _document(body):
    return {
        "source_id": "WEBSNAP:fixture", "path": "raw.json", "content_sha256": "fixture",
        "url": "https://www.sec.gov/example", "filing_date": "2026-08-01",
        "report_date": REPORT_DATE, "form": "10-Q", "accession_number": "0000000123-26-000001",
        "body": body,
    }


def _index():
    return {
        "raw_payload_sha256": "filings-fixture",
        "filings": [{"report_date": REPORT_DATE, "filing_date": "2026-08-01",
                     "form": "10-Q", "accession_number": "0000000123-26-000001"}],
    }


def _retained_document(root, body):
    url = "https://www.sec.gov/example"
    payload = {
        "requested_url": url, "final_url": url, "body": body,
        "body_encoding": "utf-8", "content_length": len(body.encode("utf-8")),
    }
    digest = canonical_payload_hash(payload)
    relative_path = Path("data/raw/web/objects") / f"{digest}.json"
    path = root / relative_path
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"source": "web", "payload_sha256": digest, "payload": payload}), encoding="utf-8")
    document = _document(body)
    document.update(source_id=f"WEBSNAP:{digest[:12]}", path=str(relative_path), content_sha256=digest)
    return document, path


class SegmentExtractionTests(unittest.TestCase):
    def test_axes_periods_metrics_and_currencies_remain_separate(self):
        extracted = segments.extract(_fixture(), REPORT_DATE)
        rows = extracted["observations"]
        self.assertEqual(extracted["state"], "AVAILABLE")
        self.assertEqual(len(rows), 7)  # The future quarter is excluded.

        def matching(*, metric="revenue", start="2026-04-01", currency="USD", axis=BUSINESS):
            return [row for row in rows if row["metric"] == metric
                    and row["period_start"] == start and row["currency"] == currency
                    and axis in row["axes"]]

        self.assertEqual({row["amount"] for row in matching()}, {10_000_000, 20_000_000})
        self.assertEqual([row["amount"] for row in matching(start="2026-01-01")], [18_000_000])
        self.assertEqual([row["amount"] for row in matching(currency="EUR")], [7_000_000])
        self.assertEqual([row["amount"] for row in matching(axis=PRODUCT)], [30_000_000])
        self.assertEqual([row["amount"] for row in matching(axis=GEOGRAPHY)], [30_000_000])
        self.assertEqual([row["amount"] for row in matching(metric="operating_income")], [2_000_000])

        report = {key: _document(_fixture())[key] for key in segments.DOCUMENT_FIELDS}
        summary = segments.summary({"state": "AVAILABLE", "reports": [{**report, **extracted}], "collection_errors": []})
        slices = summary["reports"][0]["slices"]
        self.assertEqual(len(slices), 6)
        business_quarter = next(row for row in slices if row["metric"] == "revenue"
                                and row["currency"] == "USD" and row["period_start"] == "2026-04-01"
                                and row["axes"] == [BUSINESS])
        self.assertEqual({member["amount"] for member in business_quarter["members"]}, {10_000_000, 20_000_000})
        self.assertEqual(summary["reports"][0]["omitted_observations"], 0)

    def test_non_currency_units_remain_unresolved(self):
        body = _fixture().replace(_fact("cloud_q", "10"), _fact("cloud_q", "10", unit="shares"))
        extracted = segments.extract(body, REPORT_DATE)
        affected = next(row for row in extracted["observations"]
                        if row["dimensions"].get(BUSINESS) == "issuer:CloudMember"
                        and row["period_start"] == "2026-04-01" and row["currency"] == "shares")
        self.assertEqual(affected["state"], "UNIT_UNRESOLVED")
        self.assertIsNone(affected["amount"])
        self.assertEqual(extracted["state"], "PARTIAL")

    def test_validation_replays_retained_original_and_rejects_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            document, raw_path = _retained_document(root, _fixture())
            index = _index()
            bundle = {"filings_index": index, "documents": [document], "errors": []}
            with patch.object(segments, "load_reports", return_value=bundle):
                artifact = segments.build("TEST", limit=1)
            with patch("collectors.sec_inline.ROOT", root), \
                 patch.object(segments, "load_filings_index", return_value=index), \
                 patch.object(segments, "validate_document_sources", return_value=[]):
                self.assertEqual(segments.validate_artifact(artifact, "TEST"), [])
                tampered = deepcopy(artifact)
                tampered["reports"][0]["observations"][0]["amount"] = 999_000_000
                self.assertIn("re-extraction", segments.validate_artifact(tampered, "TEST")[0])

                raw = json.loads(raw_path.read_text(encoding="utf-8"))
                raw["payload"]["body"] = _fixture().replace(_fact("cloud_q", "10"), _fact("cloud_q", "11"))
                raw_path.write_text(json.dumps(raw), encoding="utf-8")
                self.assertIn("fingerprint", segments.validate_artifact(artifact, "TEST")[0])


if __name__ == "__main__":
    unittest.main()
