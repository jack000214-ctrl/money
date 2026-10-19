"""재질문 큐: 패킷 준비, 중복 방지, BLOCKED 보고, 만료. build_context 는 가짜로 대체한다."""
from __future__ import annotations

import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cio import paths, watch_queue as wq, watch_store as ws

NOW = dt.datetime(2026, 10, 19, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=9)))
HOLDINGS = [{"ticker": "005930", "name": "삼성전자", "qty": 10.0}]


def price_event(**extra):
    return {
        "type": "PRICE_TRIGGER", "ticker": "005930", "name": "삼성전자",
        "price": 199000.0, "intent": "HOLD_REVIEW", **extra,
    }


class TempWatch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        original = paths.WATCH_DIR
        paths.WATCH_DIR = Path(self.tmp.name) / "watch"
        self.addCleanup(setattr, paths, "WATCH_DIR", original)
        self.calls = []

    def fake_build(self, status="PASS", problems=None, context_id="CTX-" + "a" * 20):
        def build(asset_id, **kwargs):
            self.calls.append((asset_id, kwargs))
            return {
                "context_id": context_id,
                "validation": {"status": status, "problems": problems or []},
            }, None
        return build

    def stored_event(self, **extra):
        return ws.append_event(price_event(**extra))


class TestPrepare(TempWatch):
    def test_pass_creates_pending_item_and_links_event(self):
        event = self.stored_event()
        out = wq.prepare_for_events([event], HOLDINGS, build=self.fake_build(), now=NOW)
        self.assertEqual(out[0]["queue_status"], "PREPARED")
        self.assertEqual(out[0]["context_id"], "CTX-" + "a" * 20)
        items = ws.load_queue()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["status"], "PENDING")
        self.assertEqual(items[0]["event_seq"], event["seq"])
        saved = ws.read_events()[0]
        self.assertEqual(saved["context_id"], "CTX-" + "a" * 20)

    def test_expiry_is_twelve_hours(self):
        wq.prepare_for_events([self.stored_event()], HOLDINGS, build=self.fake_build(), now=NOW)
        item = ws.load_queue()["items"][0]
        self.assertEqual(
            dt.datetime.fromisoformat(item["expires_at"]) - dt.datetime.fromisoformat(item["created_at"]),
            dt.timedelta(hours=12),
        )

    def test_holding_review_never_receives_an_amount(self):
        """보유 검토에 amount 를 주면 BLOCKED 가 된다(context 규칙)."""
        wq.prepare_for_events([self.stored_event()], HOLDINGS, build=self.fake_build(), now=NOW)
        asset_id, kwargs = self.calls[0]
        self.assertEqual(asset_id, "005930")
        self.assertNotIn("amount", kwargs)
        self.assertEqual(kwargs["intent"], "HOLD_REVIEW")
        self.assertEqual(kwargs["held_quantity"], 10.0)
        self.assertEqual(kwargs["price"], 199000.0)

    def test_blocked_packet_is_reported_not_queued(self):
        event = self.stored_event()
        out = wq.prepare_for_events(
            [event], HOLDINGS,
            build=self.fake_build("BLOCKED", ["가격 이력이 낡았다"]), now=NOW,
        )
        self.assertEqual(out[0]["queue_status"], "BLOCKED")
        self.assertEqual(out[0]["queue_note"], "가격 이력이 낡았다")
        self.assertEqual(ws.load_queue()["items"], [])
        self.assertEqual(ws.read_events()[0]["queue_status"], "BLOCKED")

    def test_builder_exception_is_isolated_and_does_not_leak_text(self):
        def boom(asset_id, **kwargs):
            raise RuntimeError("비밀 경로 C:\\secret")
        first = self.stored_event()
        out = wq.prepare_for_events([first], HOLDINGS, build=boom, now=NOW)
        self.assertEqual(out[0]["queue_status"], "BLOCKED")
        self.assertNotIn("secret", out[0]["queue_note"])

    def test_second_event_for_same_ticker_does_not_build_again(self):
        first = self.stored_event()
        second = self.stored_event()
        out = wq.prepare_for_events([first, second], HOLDINGS, build=self.fake_build(), now=NOW)
        self.assertEqual(out[0]["queue_status"], "PREPARED")
        self.assertEqual(out[1]["queue_status"], "PENDING_EXISTS")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(ws.load_queue()["items"]), 1)

    def test_expired_item_allows_a_new_packet(self):
        wq.prepare_for_events([self.stored_event()], HOLDINGS, build=self.fake_build(), now=NOW)
        later = NOW + dt.timedelta(hours=13)
        out = wq.prepare_for_events(
            [self.stored_event()], HOLDINGS,
            build=self.fake_build(context_id="CTX-" + "b" * 20), now=later,
        )
        self.assertEqual(out[0]["queue_status"], "PREPARED")
        statuses = [i["status"] for i in ws.load_queue()["items"]]
        self.assertEqual(statuses, ["EXPIRED", "PENDING"])

    def test_non_price_events_pass_through_without_packets(self):
        event = {"type": "POLICY_BREACH", "ticker": "005930", "name": "삼성전자"}
        out = wq.prepare_for_events([event], HOLDINGS, build=self.fake_build(), now=NOW)
        self.assertEqual(out, [event])
        self.assertEqual(self.calls, [])

    def test_packet_has_no_verdict_in_queue_item(self):
        wq.prepare_for_events([self.stored_event()], HOLDINGS, build=self.fake_build(), now=NOW)
        item = ws.load_queue()["items"][0]
        self.assertFalse({"verdict", "action", "recommendation"} & set(item))


class TestExpireAndResolve(TempWatch):
    def test_expire_marks_only_overdue_pending(self):
        wq.prepare_for_events([self.stored_event()], HOLDINGS, build=self.fake_build(), now=NOW)
        self.assertEqual(wq.expire(NOW + dt.timedelta(hours=1)), 0)
        self.assertEqual(wq.expire(NOW + dt.timedelta(hours=12)), 1)
        self.assertEqual(ws.load_queue()["items"][0]["status"], "EXPIRED")

    def test_resolve_marks_used(self):
        wq.prepare_for_events([self.stored_event()], HOLDINGS, build=self.fake_build(), now=NOW)
        self.assertTrue(wq.resolve("CTX-" + "a" * 20, "USED"))
        self.assertIsNone(wq.pending_for("005930", NOW))
        self.assertFalse(wq.resolve("CTX-none", "USED"))

    def test_resolve_rejects_unknown_status(self):
        with self.assertRaises(ValueError):
            wq.resolve("x", "DONE")

    def test_format_queue_empty(self):
        self.assertIn("비어", wq.format_queue([]))


if __name__ == "__main__":
    unittest.main()
