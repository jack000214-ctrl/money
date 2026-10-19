"""`/api/watch/*` 라우팅: 상태·이벤트·대기열·읽음·설정 GET/PUT·시험 메일.

핸들러 로직(`cio.watch_api.route`)을 직접 호출한다. 서버·네트워크·knowledge 에 의존하지 않는다.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cio import paths, watch_api, watch_config, watch_notify, watch_runner, watch_store as ws


def event(n: int) -> dict:
    return {
        "type": "PRICE_TRIGGER", "ticker": "005930", "name": "삼성전자", "assessment_id": f"CIO-{n}",
        "threshold": {"name": "stop_price", "label": "손절가", "direction": "AT_OR_BELOW", "value": 200000.0},
        "price": 199000.0, "currency": "KRW", "quote_time": "2026-10-19T10:00:00+09:00",
        "price_kind": "REGULAR_LIVE", "detected_at": "2026-10-19T10:00:00+09:00",
        "intent": "HOLD_REVIEW", "promised_action": None, "invalidation_signals": [],
    }


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        original_dir = paths.WATCH_DIR
        paths.WATCH_DIR = Path(self.tmp.name) / "watch"
        self.addCleanup(setattr, paths, "WATCH_DIR", original_dir)
        original_cfg = watch_config.CONFIG_PATH
        watch_config.CONFIG_PATH = Path(self.tmp.name) / "watch.json"
        self.addCleanup(setattr, watch_config, "CONFIG_PATH", original_cfg)
        patcher = patch.object(watch_notify, "smtp_password_present", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        watch_api._last_test_email = 0.0

    def call(self, method, path, query=None, body=None):
        return watch_api.route(method, path, query or {}, body if body is not None else {})

    def call_ok(self, method, path, query=None, body=None):
        result = self.call(method, path, query, body)
        assert result is not None
        return result


class TestRouting(Harness):
    def test_non_watch_path_is_not_handled(self):
        self.assertIsNone(self.call("GET", "/api/assets"))

    def test_unknown_watch_path_is_404(self):
        status, payload = self.call_ok("GET", "/api/watch/nope")
        self.assertEqual(status, 404)
        self.assertIn("error", payload)

    def test_wrong_method_is_404(self):
        status, _ = self.call_ok("POST", "/api/watch/status")
        self.assertEqual(status, 404)


class TestStatus(Harness):
    def test_status_has_password_flag_only(self):
        status, payload = self.call_ok("GET", "/api/watch/status")
        self.assertEqual(status, 200)
        self.assertIs(payload["smtp_password_set"], True)
        self.assertEqual(payload["unread"], 0)
        self.assertEqual(payload["not_monitored"], [])
        self.assertNotIn("SMTP_PASSWORD", json.dumps(payload))

    def test_status_surfaces_not_monitored_from_last_cycle(self):
        ws.save_state({"last_cycle": {"at": "t", "result": "OK", "not_monitored": [{"item": "review.x", "reason": "미정"}]}})
        _, payload = self.call_ok("GET", "/api/watch/status")
        self.assertEqual(payload["not_monitored"][0]["item"], "review.x")

    def test_status_reports_invalid_config_without_crashing(self):
        watch_config.CONFIG_PATH.write_text(json.dumps({"interval_minutes": 0}), encoding="utf-8")
        status, payload = self.call_ok("GET", "/api/watch/status")
        self.assertEqual(status, 200)
        self.assertTrue(payload["config_errors"])


class TestEvents(Harness):
    def test_events_newest_first_with_read_flag_and_text(self):
        for n in (1, 2, 3):
            ws.append_event(event(n))
        ws.mark_read_through(1)
        status, payload = self.call_ok("GET", "/api/watch/events")
        self.assertEqual(status, 200)
        self.assertEqual([e["seq"] for e in payload["events"]], [3, 2, 1])
        self.assertEqual([e["read"] for e in payload["events"]], [False, False, True])
        self.assertEqual(payload["unread"], 2)
        self.assertIn("title", payload["events"][0]["text"])

    def test_limit_is_applied_and_clamped(self):
        for n in range(1, 6):
            ws.append_event(event(n))
        _, payload = self.call_ok("GET", "/api/watch/events", {"limit": ["2"]})
        self.assertEqual([e["seq"] for e in payload["events"]], [5, 4])
        for bad in ("abc", "0", "-3", "999999"):
            status, payload = self.call_ok("GET", "/api/watch/events", {"limit": [bad]})
            self.assertEqual(status, 200)
            self.assertLessEqual(len(payload["events"]), watch_api.MAX_EVENTS)

    def test_unformattable_event_still_listed(self):
        ws.append_event({"type": "UNKNOWN_FUTURE_TYPE"})
        _, payload = self.call_ok("GET", "/api/watch/events")
        self.assertEqual(len(payload["events"]), 1)


class TestMarkRead(Harness):
    def test_mark_all_read(self):
        ws.append_event(event(1))
        ws.append_event(event(2))
        status, payload = self.call_ok("POST", "/api/watch/events/read")
        self.assertEqual(status, 200)
        self.assertEqual(payload["unread"], 0)
        self.assertEqual(payload["read_through"], 2)

    def test_mark_through_seq(self):
        for n in (1, 2, 3):
            ws.append_event(event(n))
        _, payload = self.call_ok("POST", "/api/watch/events/read", body={"through_seq": 2})
        self.assertEqual(payload["unread"], 1)

    def test_bad_seq_is_400(self):
        for bad in ("x", -1, True, 1.5):
            status, _ = self.call_ok("POST", "/api/watch/events/read", body={"through_seq": bad})
            self.assertEqual(status, 400, bad)


class TestQueue(Harness):
    def test_queue_lists_items(self):
        items = [{"context_id": "CTX-1", "ticker": "005930", "status": "PENDING"}]
        with patch("cio.watch_queue.list_items", return_value=items):
            status, payload = self.call_ok("GET", "/api/watch/queue")
        self.assertEqual(status, 200)
        self.assertEqual(payload["items"], items)


class TestConfig(Harness):
    def test_get_config_without_password(self):
        status, payload = self.call_ok("GET", "/api/watch/config")
        self.assertEqual(status, 200)
        self.assertEqual(payload["config"]["interval_minutes"], watch_config.default_config()["interval_minutes"])
        self.assertIs(payload["smtp_password_set"], True)
        self.assertNotIn("smtp_password", json.dumps(payload).lower().replace("smtp_password_set", ""))

    def test_get_invalid_config_returns_errors(self):
        watch_config.CONFIG_PATH.write_text(json.dumps({"interval_minutes": 0}), encoding="utf-8")
        status, payload = self.call_ok("GET", "/api/watch/config")
        self.assertEqual(status, 200)
        self.assertIsNone(payload["config"])
        self.assertTrue(payload["errors"])

    def test_put_saves_and_returns_config(self):
        status, payload = self.call_ok("PUT", "/api/watch/config", body={"interval_minutes": 10})
        self.assertEqual(status, 200)
        self.assertEqual(payload["config"]["interval_minutes"], 10)
        self.assertEqual(watch_config.load_config()["interval_minutes"], 10)

    def test_put_post_both_accepted(self):
        status, _ = self.call_ok("POST", "/api/watch/config", body={"interval_minutes": 15})
        self.assertEqual(status, 200)

    def test_put_invalid_is_400_with_errors_and_nothing_written(self):
        self.call_ok("PUT", "/api/watch/config", body={"interval_minutes": 10})
        status, payload = self.call_ok("PUT", "/api/watch/config", body={"interval_minutes": 0})
        self.assertEqual(status, 400)
        self.assertTrue(payload["errors"])
        self.assertEqual(watch_config.load_config()["interval_minutes"], 10)

    def test_put_non_object_is_400(self):
        status, _ = self.call_ok("PUT", "/api/watch/config", body=["x"])
        self.assertEqual(status, 400)

    def test_put_ignores_password_field(self):
        self.call_ok("PUT", "/api/watch/config", body={"email": {"smtp_password": "secret-value"}})
        self.assertNotIn("secret-value", watch_config.CONFIG_PATH.read_text(encoding="utf-8")
                         if watch_config.CONFIG_PATH.exists() else "")

    def test_put_replaces_recipients(self):
        rows = [{"name": "a", "address": "a@example.com", "enabled": True}]
        _, payload = self.call_ok("PUT", "/api/watch/config", body={"email": {"recipients": rows}})
        self.assertEqual(payload["config"]["email"]["recipients"], rows)
        _, payload = self.call_ok("PUT", "/api/watch/config", body={"email": {"recipients": []}})
        self.assertEqual(payload["config"]["email"]["recipients"], [])

    def test_internal_error_hides_exception_text(self):
        with patch.object(watch_config, "save_config", side_effect=RuntimeError("C:\\secret\\path")):
            status, payload = self.call_ok("PUT", "/api/watch/config", body={"interval_minutes": 10})
        self.assertEqual(status, 500)
        self.assertNotIn("secret", json.dumps(payload, ensure_ascii=False))


class TestTestEmail(Harness):
    def test_success_and_failure_pass_through_reason(self):
        with patch.object(watch_notify, "send_email", return_value=(True, "발송했다")) as sent:
            status, payload = self.call_ok("POST", "/api/watch/test-email")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"ok": True, "reason": "발송했다"})
        subject, body = sent.call_args.args[:2]
        self.assertIn("시험", subject)
        self.assertNotIn("매수", subject + body)

        watch_api._last_test_email = 0.0
        with patch.object(watch_notify, "send_email", return_value=(False, "수신자가 없다")):
            status, payload = self.call_ok("POST", "/api/watch/test-email")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"ok": False, "reason": "수신자가 없다"})

    def test_cooldown_blocks_rapid_repeats(self):
        with patch.object(watch_notify, "send_email", return_value=(True, "발송했다")) as sent:
            self.call_ok("POST", "/api/watch/test-email")
            status, payload = self.call_ok("POST", "/api/watch/test-email")
        self.assertEqual(status, 429)
        self.assertEqual(sent.call_count, 1)
        self.assertIn("error", payload)

    def test_sender_exception_is_generic(self):
        with patch.object(watch_notify, "send_email", side_effect=RuntimeError("smtp://user:pw@host")):
            status, payload = self.call_ok("POST", "/api/watch/test-email")
        self.assertEqual(status, 500)
        self.assertNotIn("pw@host", json.dumps(payload, ensure_ascii=False))


class TestRunOnce(Harness):
    def test_run_once_returns_cycle(self):
        cycle = {"at": "t", "result": "SKIPPED", "reason": "WEEKEND"}
        with patch.object(watch_runner, "run_cycle", return_value=cycle) as ran:
            status, payload = self.call_ok("POST", "/api/watch/run-once")
        self.assertEqual(status, 200)
        self.assertEqual(payload["cycle"], cycle)
        ran.assert_called_once()


if __name__ == "__main__":
    unittest.main()
