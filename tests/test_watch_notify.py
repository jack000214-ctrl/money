"""알림 포매터의 금지 표현, 채널 실패 격리, 이메일(가짜 SMTP)."""
from __future__ import annotations

import smtplib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cio import paths, watch_notify as wn

PRICE_EVENT = {
    "type": "PRICE_TRIGGER", "ticker": "005930", "name": "삼성전자",
    "assessment_id": "CIO-" + "a" * 20, "recorded_verdict": "HOLD", "recorded_action": "HOLD",
    "threshold": {"name": "stop_price", "label": "손절가", "direction": "down", "value": 200000.0},
    "price": 199000.0, "currency": "KRW", "quote_time": "2026-10-19T10:14:00+09:00",
    "price_kind": "REGULAR_LIVE", "intent": "HOLD_REVIEW",
    "invalidation_signals": [{"signal": "분기 영업이익이 3조 아래", "action": "EXIT"}],
    "promised_action": None,
}
STAGE_EVENT = {
    **PRICE_EVENT,
    "threshold": {"name": "exit_stage:s1", "label": "매도 단계 s1", "direction": "up", "value": 240000.0},
    "price": 241000.0, "intent": "TRIM_REVIEW", "invalidation_signals": None,
    "promised_action": {"stage_id": "s1", "quantity": 10, "valid_from": "2026-10-01",
                        "valid_until": "2026-12-31", "condition": "컨센서스 상향",
                        "requires_review": True},
    "context_id": "CTX-" + "b" * 20,
}
ALL_EVENTS = [
    PRICE_EVENT, STAGE_EVENT,
    {**PRICE_EVENT, "queue_status": "BLOCKED", "queue_note": "가격 이력이 낡았다"},
    {**PRICE_EVENT, "queue_status": "PENDING_EXISTS"},
    {"type": "POLICY_BREACH", "ticker": "005930", "name": "삼성전자",
     "limit": {"name": "max_single_position_pct_of_total", "value": 20, "unit": "PCT"},
     "measured": {"value": 25.0, "unit": "PCT"}, "basis": "보유 종목 단위 근사"},
    {"type": "STALE_ASSESSMENT", "ticker": "005930", "name": "삼성전자", "reason": "NO_LIVE_ASSESSMENT"},
    {"type": "STALE_ASSESSMENT", "ticker": "005930", "name": "삼성전자", "reason": "AGE",
     "assessment_id": "CIO-x", "measured": {"value": 20, "unit": "DAYS"},
     "limit": {"name": "stale_after_days", "value": 14, "unit": "DAYS"}},
    {"type": "STALE_ASSESSMENT", "ticker": "005930", "name": "삼성전자", "reason": "PRICE_DRIFT",
     "asked_price": 200000, "price": 230000, "measured": {"value": 15.0, "unit": "PCT"},
     "limit": {"name": "price_drift_pct", "value": 10, "unit": "PCT"}},
    {"type": "EARNINGS_NEAR", "ticker": "005930", "name": "삼성전자", "event": "잠정실적 공시 예정",
     "event_date": "2026-10-22", "measured": {"value": 3, "unit": "BUSINESS_DAYS"},
     "limit": {"name": "earnings_within_business_days", "value": 5, "unit": "BUSINESS_DAYS"},
     "basis": "주말만 제외한 근사"},
]


class TestFormatter(unittest.TestCase):
    def test_no_forbidden_phrase_in_any_event_type(self):
        for index, event in enumerate(ALL_EVENTS):
            text = wn.format_event(event)
            combined = text["title"] + "\n" + text["body"]
            for phrase in wn.FORBIDDEN_PHRASES:
                with self.subTest(index=index, phrase=phrase):
                    self.assertNotIn(phrase, combined)

    def test_digest_has_no_forbidden_phrase(self):
        digest = wn.format_digest(ALL_EVENTS)
        for phrase in wn.FORBIDDEN_PHRASES:
            self.assertNotIn(phrase, digest["title"] + digest["body"])

    def test_every_message_states_it_is_not_a_judgment(self):
        for event in ALL_EVENTS:
            self.assertIn("매매 판단이 아니다", wn.format_event(event)["body"])

    def test_price_event_states_facts(self):
        body = wn.format_event(PRICE_EVENT)["body"]
        self.assertIn("손절가 200,000원에 현재가 199,000원", body)
        self.assertIn("원문", body)
        self.assertIn("자동으로 판정하지 않았다", body)

    def test_blocked_queue_is_shown_as_data_state(self):
        body = wn.format_event(ALL_EVENTS[2])["body"]
        self.assertIn("데이터 준비 필요", body)

    def test_context_link_says_model_decides(self):
        body = wn.format_event(STAGE_EVENT)["body"]
        self.assertIn("새 판단은 모델이 낸다", body)
        self.assertIn("TRIM_REVIEW", body)


class TempWatch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        original = paths.WATCH_DIR
        paths.WATCH_DIR = Path(self.tmp.name) / "watch"
        self.addCleanup(setattr, paths, "WATCH_DIR", original)


CONFIG = {
    "channels": {"console": True, "file": True, "dashboard": True, "email": True},
    "email": {"smtp_host": "smtp.example.com", "smtp_port": 587, "smtp_user": "me@example.com",
              "smtp_from": "", "recipients": [{"address": "to@example.com", "enabled": True}]},
}


class TestDispatchIsolation(TempWatch):
    def test_failing_channel_does_not_block_others(self):
        def boom(*_a, **_k):
            raise RuntimeError("secret-token-123")

        results = wn.dispatch([PRICE_EVENT], CONFIG, email_sender=boom)
        self.assertTrue(results["console"]["ok"])
        self.assertTrue(results["file"]["ok"])
        self.assertFalse(results["email"]["ok"])
        self.assertNotIn("secret-token-123", results["email"]["reason"])
        self.assertTrue((paths.WATCH_DIR / "notifications.log").exists())

    def test_disabled_channels_are_not_used(self):
        config = {**CONFIG, "channels": {"console": False, "file": True, "dashboard": False, "email": False}}
        called = []
        results = wn.dispatch([PRICE_EVENT], config, email_sender=lambda *a, **k: called.append(1) or (True, ""))
        self.assertEqual(set(results), {"file"})
        self.assertEqual(called, [])

    def test_no_events_sends_nothing(self):
        self.assertEqual(wn.dispatch([], CONFIG), {})

    def test_file_failure_is_isolated(self):
        original = wn.send_file
        wn.send_file = lambda *a: (_ for _ in ()).throw(OSError("disk"))
        self.addCleanup(setattr, wn, "send_file", original)
        results = wn.dispatch([PRICE_EVENT], CONFIG, email_sender=lambda *a, **k: (True, "ok"))
        self.assertFalse(results["file"]["ok"])
        self.assertTrue(results["email"]["ok"])


class FakeSMTP:
    instances: list = []

    def __init__(self, host, port):
        self.host, self.port = host, port
        self.logged_in = None
        self.sent = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        self.logged_in = (user, password)

    def send_message(self, message):
        self.sent.append(message)


class TestEmail(unittest.TestCase):
    def setUp(self):
        FakeSMTP.instances = []
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = Path(self.tmp.name) / ".env"

    def write_env(self, text):
        self.env.write_text(text, encoding="utf-8")

    def test_password_comes_only_from_env_file(self):
        self.write_env("SMTP_PASSWORD=app-pass-1234\n")
        ok, _ = wn.send_email("제목", "본문", CONFIG, env_file=self.env, smtp_factory=FakeSMTP)
        self.assertTrue(ok)
        smtp = FakeSMTP.instances[0]
        self.assertEqual(smtp.logged_in, ("me@example.com", "app-pass-1234"))
        message = smtp.sent[0]
        self.assertEqual(message["To"], "to@example.com")
        self.assertEqual(message["From"], "me@example.com", "발신자가 비면 계정을 쓴다")
        self.assertTrue(message["Subject"].startswith("[Money 감시]"))

    def test_config_never_carries_the_password(self):
        self.write_env("SMTP_PASSWORD=app-pass-1234\n")
        settings = wn.resolve_email_settings(CONFIG, self.env)
        self.assertNotIn("app-pass-1234", str(settings))

    def test_missing_password_is_reported_without_sending(self):
        self.write_env("OTHER=1\n")
        ok, reason = wn.send_email("s", "b", CONFIG, env_file=self.env, smtp_factory=FakeSMTP)
        self.assertFalse(ok)
        self.assertIn("SMTP_PASSWORD", reason)
        self.assertEqual(FakeSMTP.instances, [])

    def test_missing_host_or_recipients_are_reported(self):
        self.write_env("SMTP_PASSWORD=x\n")
        no_host = {"email": {**CONFIG["email"], "smtp_host": ""}}
        self.assertFalse(wn.send_email("s", "b", no_host, env_file=self.env, smtp_factory=FakeSMTP)[0])
        no_to = {"email": {**CONFIG["email"], "recipients": []}}
        self.assertFalse(wn.send_email("s", "b", no_to, env_file=self.env, smtp_factory=FakeSMTP)[0])

    def test_disabled_recipient_is_skipped(self):
        self.write_env("SMTP_PASSWORD=x\n")
        config = {"email": {**CONFIG["email"], "recipients": [
            {"address": "off@example.com", "enabled": False},
            {"address": "on@example.com", "enabled": True},
        ]}}
        wn.send_email("s", "b", config, env_file=self.env, smtp_factory=FakeSMTP)
        self.assertEqual(FakeSMTP.instances[0].sent[0]["To"], "on@example.com")

    def test_quotes_in_env_are_stripped_like_stock_service(self):
        self.write_env('SMTP_PASSWORD="quoted-pass"\n')
        self.assertEqual(wn.read_env("SMTP_PASSWORD", self.env), "quoted-pass")

    def test_auth_failure_reason_does_not_leak_password(self):
        self.write_env("SMTP_PASSWORD=leak-me\n")

        class Rejecting(FakeSMTP):
            def login(self, user, password):
                raise smtplib.SMTPAuthenticationError(535, b"bad leak-me")

        ok, reason = wn.send_email("s", "b", CONFIG, env_file=self.env, smtp_factory=Rejecting)
        self.assertFalse(ok)
        self.assertNotIn("leak-me", reason)
        self.assertIn("인증", reason)

    def test_password_presence_check(self):
        self.write_env("SMTP_PASSWORD=x\n")
        self.assertTrue(wn.smtp_password_present(self.env))
        self.write_env("A=1\n")
        self.assertFalse(wn.smtp_password_present(self.env))


if __name__ == "__main__":
    unittest.main()
