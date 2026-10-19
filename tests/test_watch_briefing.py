"""일일 브리핑: 사실 나열, 금지 표현 없음, 하루 1회, 조회 실패 격리."""
from __future__ import annotations

import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cio import paths, watch_briefing as wb, watch_notify, watch_store as ws

KST = dt.timezone(dt.timedelta(hours=9))
NOW = dt.datetime(2026, 10, 19, 8, 45, tzinfo=KST)  # 월요일
HOLDINGS = [{"ticker": "005930", "name": "삼성전자", "qty": 10.0, "currency": "KRW"}]
QUOTES = {"005930": {"price": 210000.0, "previous_close": 200000.0, "currency": "KRW",
                     "status": "OK", "price_kind": "LAST_REGULAR", "comparable": True}}
CONFIG = {"briefing": {"enabled": True, "time": "08:30"},
          "channels": {"console": False, "file": False, "dashboard": True, "email": False}}
EVENT = {"type": "PRICE_TRIGGER", "ticker": "005930", "name": "삼성전자",
         "assessment_id": "CIO-x", "recorded_verdict": "HOLD", "recorded_action": "HOLD",
         "threshold": {"name": "stop_price", "label": "손절가", "direction": "down", "value": 200000.0},
         "price": 199000.0, "currency": "KRW", "quote_time": "t", "price_kind": "LAST_REGULAR",
         "intent": "HOLD_REVIEW", "detected_at": "2026-10-19T08:10:00+09:00",
         "invalidation_signals": None, "promised_action": None}


def ok_requests(_t):
    return 2


def ok_eval(_t):
    return {"assessment_count": 3, "scored_count": 1, "unscored_count": 2}


def boom(_t):
    raise RuntimeError("C:\\secret")


class TempWatch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        original = paths.WATCH_DIR
        paths.WATCH_DIR = Path(self.tmp.name) / "watch"
        self.addCleanup(setattr, paths, "WATCH_DIR", original)


class TestBuild(unittest.TestCase):
    def build(self, **kw):
        args = dict(holdings=HOLDINGS, quotes=QUOTES, events=[EVENT], queue_items=[],
                    not_monitored=[{"item": "review.stale_after_days", "reason": "정하지 않음(null)"}],
                    today=NOW.date(), open_requests=ok_requests, evaluation_summary=ok_eval)
        args.update(kw)
        return wb.build_briefing(**args)

    def test_contains_facts(self):
        text = self.build()["body"]
        self.assertIn("삼성전자(005930)", text)
        self.assertIn("210,000원", text)
        self.assertIn("+5.00%", text)
        self.assertIn("미해결 자료 요청 2건", text)
        self.assertIn("채점 1건, 미채점 2건", text)
        self.assertIn("review.stale_after_days", text)
        self.assertIn("가격 종류 LAST_REGULAR", text)

    def test_no_forbidden_phrases(self):
        message = self.build()
        for phrase in watch_notify.FORBIDDEN_PHRASES:
            self.assertNotIn(phrase, message["title"] + message["body"])

    def test_old_events_are_not_listed_as_today(self):
        old = {**EVENT, "detected_at": "2026-10-16T10:00:00+09:00"}
        self.assertIn("오늘 감지된 이벤트가 없다", self.build(events=[old])["body"])

    def test_lookup_failure_is_reported_per_line_without_leaking(self):
        text = self.build(open_requests=boom, evaluation_summary=boom)["body"]
        self.assertIn("조회하지 못했다", text)
        self.assertNotIn("secret", text)

    def test_pending_packets_listed_and_failed_quote_reported(self):
        item = {"status": "PENDING", "ticker": "005930", "name": "삼성전자", "intent": "HOLD_REVIEW",
                "context_id": "CTX-1", "expires_at": "2026-10-19T20:00:00+09:00"}
        failed = {"005930": {**QUOTES["005930"], "status": "FAILED", "last_error": "HTTP 500", "comparable": False}}
        text = self.build(queue_items=[item, {**item, "status": "EXPIRED", "context_id": "CTX-2"}], quotes=failed)["body"]
        self.assertIn("CTX-1", text)
        self.assertNotIn("CTX-2", text)
        self.assertIn("시세 조회 실패(HTTP 500)", text)
        self.assertIn("오늘 갱신된 시세가 아니다", text)

    def test_empty_holdings(self):
        self.assertIn("보유 종목이 없다", self.build(holdings=[], events=[])["body"])


class TestDue(unittest.TestCase):
    def test_before_time_not_due(self):
        self.assertFalse(wb.briefing_due(CONFIG, {}, NOW.replace(hour=8, minute=29)))

    def test_at_time_due(self):
        self.assertTrue(wb.briefing_due(CONFIG, {}, NOW.replace(hour=8, minute=30)))

    def test_disabled_not_due(self):
        self.assertFalse(wb.briefing_due({"briefing": {"enabled": False, "time": "08:30"}}, {}, NOW))

    def test_already_sent_today(self):
        self.assertFalse(wb.briefing_due(CONFIG, {"briefing_sent_dates": ["2026-10-19"]}, NOW))

    def test_weekend_not_due(self):
        self.assertFalse(wb.briefing_due(CONFIG, {}, NOW + dt.timedelta(days=5)))


class TestSend(TempWatch):
    def run_send(self, sent):
        def dispatch(subject, body, config):
            sent.append((subject, body))
            return {"dashboard": {"ok": True, "reason": ""}}
        return wb.send_briefing(CONFIG, HOLDINGS, QUOTES, [], NOW, dispatch=dispatch,
                                open_requests=ok_requests, evaluation_summary=ok_eval)

    def test_sends_once_per_day_and_saves_file(self):
        sent = []
        first = self.run_send(sent)
        second = self.run_send(sent)
        self.assertTrue(first["sent"])
        self.assertFalse(second["sent"])
        self.assertEqual(len(sent), 1)
        self.assertTrue((paths.WATCH_DIR / "briefings" / "2026-10-19.md").exists())
        self.assertEqual(ws.load_state()["briefing_sent_dates"], ["2026-10-19"])

    def test_next_day_sends_again(self):
        sent = []
        self.run_send(sent)
        tomorrow = NOW + dt.timedelta(days=1)
        result = wb.send_briefing(CONFIG, HOLDINGS, QUOTES, [], tomorrow,
                                  dispatch=lambda s, b, c: sent.append(1) or {},
                                  open_requests=ok_requests, evaluation_summary=ok_eval)
        self.assertTrue(result["sent"])


if __name__ == "__main__":
    unittest.main()
