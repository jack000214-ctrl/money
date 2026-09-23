"""Announced earnings are issuer event dates, not release publication dates."""
import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import helpers  # noqa: F401
from calculators.contracts import canonical_payload_hash, content_hash
from collectors import us_earnings as ue

TODAY = dt.date(2026, 9, 22)
URL = "https://ir.example.com/events"


def source(body, url=URL):
    return {"source_id": "WEBSNAP:abc", "url": url, "extraction": ue.extract(body, url)}


def card(date="October 21, 2026", name="Company 3Q 2026 Earnings Announcement"):
    return f"<html><main><article><h3>{name}</h3><p>{date}</p></article></main></html>"


class ExtractionTests(unittest.TestCase):
    def test_event_card_preserves_issuer_preliminary_date(self):
        result = ue.extract(card(name="IBM 3Q 2026 Earnings Announcement (preliminary date)"), URL)
        self.assertEqual(result["observations"][0]["date"], "2026-10-21")
        self.assertEqual(result["observations"][0]["date_status"], "ANNOUNCED_PRELIMINARY")

    def test_publication_date_is_not_a_scheduled_event(self):
        body = card("September 22, 2026", "Company to announce third quarter financial results")
        result = ue.extract(body, "https://ir.example.com/news/news-details/earnings")
        self.assertEqual(result["observations"], [])

    def test_prose_event_date_is_separate_from_publication_and_period_date(self):
        body = "<p>September 22, 2026 — Company will release its financial results for the quarter ended September 30, 2026 on October 21, 2026 after market close.</p>"
        result = ue.extract(body, "https://ir.example.com/press/earnings")
        self.assertEqual([r["date"] for r in result["observations"]], ["2026-10-21"])

    def test_year_is_not_inferred_from_fiscal_year_or_collection(self):
        body = "<p>Company will report its full-year 2025 financial results on January 28.</p>"
        result = ue.extract(body, URL)
        self.assertEqual(result["observations"], [])
        self.assertTrue(result["issues"])

    def test_conference_is_not_earnings(self):
        result = ue.extract(card(name="Global Technology Conference 2026"), URL)
        self.assertEqual(result["observations"], [])

    def test_past_earnings_do_not_invent_next_date(self):
        sources = [source(card("July 22, 2026") + " There are no upcoming events available at this time.")]
        result = ue._derive(sources, [], TODAY)
        self.assertEqual(result["status"], "NO_ANNOUNCEMENT")
        self.assertIsNone(result["next_earnings"])

    def test_json_ld_uses_start_date_never_date_published(self):
        event = {"@type": "Event", "name": "Third quarter earnings call", "startDate": "2026-10-21T17:00:00-04:00", "datePublished": "2026-09-22"}
        body = '<script type="application/ld+json">' + json.dumps(event) + '</script>'
        result = ue.extract(body, URL)
        self.assertEqual(result["observations"][0]["date"], "2026-10-21")
        del event["startDate"]
        body = '<script type="application/ld+json">' + json.dumps(event) + '</script>'
        result = ue.extract(body, URL)
        self.assertEqual(result["observations"], [])
        self.assertTrue(result["issues"])

    def test_json_ld_utc_start_date_uses_us_market_date(self):
        event = {"@type": "Event", "name": "Third quarter earnings call", "startDate": "2026-10-22T00:00:00Z"}
        body = '<script type="application/ld+json">' + json.dumps(event) + '</script>'
        self.assertEqual(ue.extract(body, URL)["observations"][0]["date"], "2026-10-21")

    def test_ics_unfolding_and_cancelled_events(self):
        body = "BEGIN:VCALENDAR\nBEGIN:VEVENT\nDTSTART;TZID=America/New_York:20261021T170000\nSUMMARY:Third quarter earnings \n call\nEND:VEVENT\nEND:VCALENDAR"
        result = ue.extract(body, URL)
        self.assertEqual(result["observations"][0]["date"], "2026-10-21")
        result = ue.extract(body.replace("SUMMARY:", "STATUS:CANCELLED\nSUMMARY:"), URL)
        self.assertEqual(result["observations"], [])
        self.assertTrue(result["issues"])

    def test_ics_explicit_utc_and_local_zones_use_us_market_date(self):
        template = "BEGIN:VCALENDAR\nBEGIN:VEVENT\nDTSTART{params}:{value}\nSUMMARY:Third quarter earnings call\nEND:VEVENT\nEND:VCALENDAR"
        utc = ue.extract(template.format(params="", value="20261022T000000Z"), URL)
        self.assertEqual(utc["observations"][0]["date"], "2026-10-21")
        pacific = ue.extract(template.format(params=";TZID=America/Los_Angeles", value="20261021T210000"), URL)
        self.assertEqual(pacific["observations"][0]["date"], "2026-10-22")
        unsupported = ue.extract(template.format(params=";TZID=Unknown/Zone", value="20261021T170000"), URL)
        self.assertEqual(unsupported["observations"], [])
        self.assertTrue(unsupported["issues"])

    def test_official_cancellation_suppresses_stale_announced_date(self):
        cancelled = "BEGIN:VCALENDAR\nBEGIN:VEVENT\nDTSTART:20261021T170000Z\nSUMMARY:Third quarter earnings call\nSTATUS:CANCELLED\nEND:VEVENT\nEND:VCALENDAR"
        result = ue._derive([source(card(name="Third quarter earnings call")), source(cancelled)], [], TODAY)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertIsNone(result["next_earnings"])
        self.assertEqual(result["cancelled_dates"], ["2026-10-21"])

    def test_cancelled_call_does_not_erase_same_day_results_release(self):
        cancelled = "BEGIN:VCALENDAR\nBEGIN:VEVENT\nDTSTART:20261021T170000Z\nSUMMARY:Third quarter earnings call\nSTATUS:CANCELLED\nEND:VEVENT\nEND:VCALENDAR"
        release = card(name="Company Q3 Financial Results Announcement")
        result = ue._derive([source(release), source(cancelled)], [], TODAY)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["next_earnings"]["date"], "2026-10-21")
        self.assertEqual(result["next_earnings"]["event_kind"], "RESULTS")

    def test_rss_pubdate_never_becomes_earnings_date(self):
        body = '<rss><channel><item><title>Third quarter earnings call</title><pubDate>Wed, 21 Oct 2026 17:00:00 GMT</pubDate><link>https://ir.example.com/earnings</link></item></channel></rss>'
        result = ue.extract(body, URL)
        self.assertTrue(result["interpretable"])
        self.assertEqual(result["observations"], [])
        self.assertEqual(result["links"], ["https://ir.example.com/earnings"])

    def test_rss_prose_is_supported(self):
        body = '<rss><channel><item><title>Company financial results</title><description>Company will announce financial results on October 21, 2026.</description></item></channel></rss>'
        self.assertEqual(ue.extract(body, URL)["observations"][0]["date"], "2026-10-21")

    def test_conflicting_future_dates_are_not_cherry_picked(self):
        sources = [source(card()), source(card("October 22, 2026"))]
        result = ue._derive(sources, [], TODAY)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertIsNone(result["next_earnings"])
        self.assertEqual(result["conflicting_dates"], ["2026-10-21", "2026-10-22"])

    def test_js_shell_is_parse_failure_not_no_announcement(self):
        result = ue._derive([source('<html><title>Investor Relations</title><div id="app"></div><script>loadEvents()</script></html>')], [], TODAY)
        self.assertEqual(result["status"], "PARSE_ERROR")

    def test_one_source_failure_preserves_partial_coverage(self):
        result = ue._derive([source('There are no upcoming events available at this time.')], [{"kind": "NETWORK_ERROR"}], TODAY)
        self.assertEqual(result["status"], "PARTIAL")

    def test_network_errors_are_not_no_announcement(self):
        result = ue._derive([], [{"kind": "NETWORK_ERROR"}], TODAY)
        self.assertEqual(result["status"], "NETWORK_ERROR")

    def test_explicit_earnings_date_column(self):
        body = '<table><tr><th>Year</th><th>Quarter</th><th>Earnings Date</th></tr><tr><td>2026</td><td>Q3</td><td>Oct 21, 2026</td></tr></table>'
        row = ue.extract(body, 'https://ir.example.com/')["observations"][0]
        self.assertEqual(row["date"], "2026-10-21")
        self.assertEqual(row["method"], "EARNINGS_DATE_COLUMN")

    def test_following_keeps_source_document_order(self):
        body = '<a href="/earnings-3q26">Earnings call</a><a href="/earnings-1q22">Earnings call</a>'
        self.assertEqual(ue.extract(body, URL)["links"][0], 'https://ir.example.com/earnings-3q26')


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.asset = {"market": "US", "asset_type": "STOCK", "ir_sources": [URL]}
        self.patchers = [patch.object(ue, "ROOT", self.root), patch.object(ue, "get_asset", return_value=self.asset)]
        for p in self.patchers:
            p.start()
        self.calls = []

    def tearDown(self):
        for p in reversed(self.patchers):
            p.stop()
        self.temp.cleanup()

    def snapshot(self, url, **kwargs):
        self.calls.append(url)
        body = card() + '<a href="https://attacker.example/earnings">Earnings release</a><a href="/earnings-detail">Earnings release</a>'
        if url.endswith('/earnings-detail'):
            body = '<p>Company will release financial results on October 21, 2026.</p>'
        payload = {"requested_url": url, "final_url": url, "body": body, "body_encoding": "utf-8", "content_length": len(body.encode()), "content_type": "text/html"}
        digest = canonical_payload_hash(payload)
        path = self.root / f'{digest}.json'
        path.write_text(json.dumps({"payload_sha256": digest, "payload": payload}))
        return {"source_id": f"WEBSNAP:{digest[:12]}", "path": path.name, "content_sha256": digest}

    def build(self):
        with patch.object(ue, "snapshot", side_effect=self.snapshot):
            return ue.build("TEST", TODAY)

    def test_originals_and_reextraction_are_verified(self):
        artifact = self.build()
        self.assertEqual(ue.validate_artifact(artifact, "TEST"), [])
        self.assertEqual(artifact["next_earnings"]["calendar_days_remaining"], 29)
        self.assertEqual(self.calls, [URL, 'https://ir.example.com/earnings-detail'])
        altered = copy.deepcopy(artifact)
        altered["next_earnings"]["date"] = '2026-10-22'
        self.assertTrue(ue.validate_artifact(altered, "TEST"))

    def test_tampered_raw_bytes_fail_even_if_summary_unchanged(self):
        artifact = self.build()
        path = self.root / artifact["sources"][0]["path"]
        path.write_text(path.read_text().replace('October 21', 'October 22'))
        self.assertTrue(ue.validate_artifact(artifact, "TEST"))

    def test_tampered_extraction_fails(self):
        artifact = self.build()
        artifact["sources"][0]["extraction"]["observations"] = []
        self.assertTrue(ue.validate_artifact(artifact, "TEST"))

    def test_changed_configuration_is_stale(self):
        artifact = self.build()
        self.asset["ir_sources"] = ['https://ir.example.com/new-events']
        self.assertTrue(ue.validate_artifact(artifact, "TEST"))

    def test_redirect_to_unconfigured_host_is_not_trusted(self):
        original = self.snapshot
        def redirected(url, **kwargs):
            result = original(url, **kwargs)
            p = self.root / result["path"]
            doc = json.loads(p.read_text())
            doc["payload"]["final_url"] = 'https://attacker.example/earnings'
            p.write_text(json.dumps(doc))
            return result
        with patch.object(ue, "snapshot", side_effect=redirected):
            artifact = ue.build("TEST", TODAY)
        self.assertEqual(artifact["status"], "PARSE_ERROR")
        self.assertEqual(artifact["sources"], [])


if __name__ == '__main__':
    unittest.main()
