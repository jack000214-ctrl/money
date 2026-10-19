"""러너: 게이트 SKIP, 이벤트 → 큐 → 알림 흐름, 중복 억제, 종목별 오류 격리, 겹침 방지.

시세·판단·정책·알림은 전부 가짜로 주입한다. 네트워크와 knowledge 에 의존하지 않는다.
"""
from __future__ import annotations

import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cio import paths, watch_config, watch_queue, watch_runner as wr, watch_store as ws

KST = dt.timezone(dt.timedelta(hours=9))
MON_10 = dt.datetime(2026, 10, 19, 10, 0, tzinfo=KST)
SAT_10 = dt.datetime(2026, 10, 17, 10, 0, tzinfo=KST)
MON_07 = dt.datetime(2026, 10, 19, 7, 0, tzinfo=KST)

HOLDINGS = [
    {"ticker": "005930", "name": "삼성전자", "qty": 10.0, "market": "KR", "currency": "KRW", "price_symbol": "005930.KS"},
    {"ticker": "000660", "name": "SK하이닉스", "qty": 5.0, "market": "KR", "currency": "KRW", "price_symbol": "000660.KS"},
]


def quote(price):
    return {"price": price, "currency": "KRW", "quote_time": "2026-10-19T10:00:00+09:00",
            "price_kind": "REGULAR_LIVE", "comparable": True, "status": "OK", "previous_close": price}


def assessment(ticker):
    return {"assessment_id": f"CIO-{ticker}", "created_at": "2026-10-18T09:00:00+09:00",
            "verdict": "HOLD", "action": {"direction": "HOLD"},
            "price_plan": {"currency": "KRW", "stop_price": 200000.0}}


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        original = paths.WATCH_DIR
        paths.WATCH_DIR = Path(self.tmp.name) / "watch"
        self.addCleanup(setattr, paths, "WATCH_DIR", original)
        self.config = watch_config.default_config()
        self.collects = 0
        self.dispatched = []
        self.prices = {"005930": 199000.0, "000660": 150000.0}

    def collect(self, holdings, now):
        self.collects += 1
        return {"quotes": {t: quote(p) for t, p in self.prices.items()}, "ok": len(self.prices), "failed": 0}

    def deps(self, **over):
        def prepare(events, holdings, now=None):
            return watch_queue.prepare_for_events(
                events, holdings, now=now,
                build=lambda asset_id, **kw: ({"context_id": "CTX-" + "a" * 20,
                                               "validation": {"status": "PASS", "problems": []}}, None),
            )

        def dispatch(events, config):
            self.dispatched.append(list(events))
            return {"dashboard": {"ok": True, "reason": ""}}

        base = dict(
            holdings=lambda markets: list(HOLDINGS),
            collect=self.collect,
            assessment=lambda t: assessment(t) if t == "005930" else {**assessment(t), "price_plan": {"currency": "KRW", "stop_price": 100000.0}},
            asked_price=lambda t, a: None,
            average_cost=lambda t: None,
            sectors=lambda: {},
            policy=lambda: None,
            calendar=lambda t: None,
            business_days_until=lambda target, today: None,
            prepare=prepare,
            dispatch=dispatch,
            dispatch_message=lambda s, b, c: {},
        )
        base.update(over)
        return wr.Deps(**base)  # pyright: ignore[reportArgumentType]


class TestGate(Harness):
    def test_weekend_skips_without_collecting(self):
        cycle = wr.run_cycle(SAT_10, self.deps(), self.config)
        self.assertEqual((cycle["result"], cycle["reason"]), ("SKIPPED", "WEEKEND"))
        self.assertEqual(self.collects, 0)
        self.assertEqual(ws.load_state()["last_cycle"]["reason"], "WEEKEND")

    def test_before_window_skips(self):
        cycle = wr.run_cycle(MON_07, self.deps(), self.config)
        self.assertEqual(cycle["reason"], "BEFORE_WINDOW")
        self.assertEqual(self.collects, 0)


class TestCycle(Harness):
    def test_trigger_flows_to_event_queue_and_notification(self):
        cycle = wr.run_cycle(MON_10, self.deps(), self.config)
        self.assertEqual(cycle["result"], "OK")
        self.assertEqual(cycle["counts"]["events"], 1)
        event = ws.read_events()[0]
        self.assertEqual(event["type"], "PRICE_TRIGGER")
        self.assertEqual(event["ticker"], "005930")
        self.assertEqual(event["queue_status"], "PREPARED")
        self.assertEqual(len(ws.load_queue()["items"]), 1)
        self.assertEqual(len(self.dispatched), 1)
        self.assertEqual(self.dispatched[0][0]["context_id"], "CTX-" + "a" * 20)

    def test_same_touch_is_not_notified_twice(self):
        wr.run_cycle(MON_10, self.deps(), self.config)
        second = wr.run_cycle(MON_10 + dt.timedelta(hours=1), self.deps(), self.config)
        self.assertEqual(second["counts"]["events"], 0)
        self.assertEqual(len(ws.read_events()), 1)
        self.assertEqual(len(self.dispatched), 1)

    def test_retouch_after_leaving_notifies_again(self):
        wr.run_cycle(MON_10, self.deps(), self.config)
        self.prices["005930"] = 210000.0
        wr.run_cycle(MON_10 + dt.timedelta(hours=1), self.deps(), self.config)
        self.prices["005930"] = 199000.0
        third = wr.run_cycle(MON_10 + dt.timedelta(hours=2), self.deps(), self.config)
        self.assertEqual(third["counts"]["events"], 1)

    def test_no_event_when_not_touched(self):
        self.prices["005930"] = 250000.0
        cycle = wr.run_cycle(MON_10, self.deps(), self.config)
        self.assertEqual(cycle["counts"]["events"], 0)
        self.assertEqual(self.dispatched, [])

    def test_unset_review_items_are_reported_not_monitored(self):
        cycle = wr.run_cycle(MON_10, self.deps(), self.config)
        items = {row["item"] for row in cycle["not_monitored"]}
        self.assertIn("review.stale_after_days", items)
        self.assertIn("review.price_drift_pct", items)
        self.assertIn("review.earnings_within_business_days", items)

    def test_one_ticker_failure_is_isolated(self):
        def assessment_fn(ticker):
            if ticker == "000660":
                raise RuntimeError("C:\\secret\\path")
            return assessment(ticker)
        cycle = wr.run_cycle(MON_10, self.deps(assessment=assessment_fn), self.config)
        self.assertEqual(cycle["result"], "OK")
        self.assertEqual(cycle["counts"]["events"], 1)
        self.assertTrue(any("000660" in e for e in cycle["errors"]))
        self.assertNotIn("secret", " ".join(cycle["errors"]))
        self.assertIn("assessment:000660", {r["item"] for r in cycle["not_monitored"]})

    def test_missing_assessment_reports_a_fact_event_without_packet(self):
        cycle = wr.run_cycle(MON_10, self.deps(assessment=lambda t: None), self.config)
        events = ws.read_events()
        self.assertEqual({e["type"] for e in events}, {"STALE_ASSESSMENT"})
        self.assertEqual(cycle["counts"]["events"], 2)
        self.assertEqual(ws.load_queue()["items"], [])

    def test_collect_failure_records_error(self):
        def boom(holdings, now):
            raise RuntimeError("x")
        cycle = wr.run_cycle(MON_10, self.deps(collect=boom), self.config)
        self.assertEqual((cycle["result"], cycle["reason"]), ("ERROR", "QUOTES_FAILED"))

    def test_no_holdings(self):
        cycle = wr.run_cycle(MON_10, self.deps(holdings=lambda m: []), self.config)
        self.assertEqual(cycle["reason"], "NO_HOLDINGS")
        self.assertEqual(self.collects, 0)

    def test_notification_failure_does_not_lose_events(self):
        def dispatch(events, config):
            raise RuntimeError("smtp")
        cycle = wr.run_cycle(MON_10, self.deps(dispatch=dispatch), self.config)
        self.assertEqual(len(ws.read_events()), 1)
        self.assertTrue(any("알림 전달 실패" in e for e in cycle["errors"]))

    def test_stale_age_event_when_user_set_the_threshold(self):
        self.config["review"]["stale_after_days"] = 1
        self.prices["005930"] = 250000.0
        self.prices["000660"] = 250000.0
        old = {**assessment("005930"), "created_at": "2026-10-10T09:00:00+09:00"}
        cycle = wr.run_cycle(MON_10, self.deps(assessment=lambda t: old), self.config)
        reasons = {e.get("reason") for e in ws.read_events()}
        self.assertEqual(reasons, {"AGE"})
        self.assertEqual(cycle["counts"]["events"], 2)


class TestLock(Harness):
    def test_overlapping_cycle_is_skipped(self):
        lock_path = paths.WATCH_DIR / "runner.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text("123")
        cycle = wr.run_cycle(MON_10, self.deps(), self.config)
        self.assertEqual((cycle["result"], cycle["reason"]), ("SKIPPED", "BUSY"))
        self.assertEqual(self.collects, 0)

    def test_lock_is_released_after_cycle(self):
        wr.run_cycle(MON_10, self.deps(), self.config)
        self.assertFalse((paths.WATCH_DIR / "runner.lock").exists())

    def test_lock_is_released_after_failure(self):
        wr.run_cycle(MON_10, self.deps(collect=lambda h, n: (_ for _ in ()).throw(RuntimeError())), self.config)
        self.assertFalse((paths.WATCH_DIR / "runner.lock").exists())


class TestBriefingAndLoop(Harness):
    def test_briefing_sent_once_when_due(self):
        sent = []
        deps = self.deps(
            dispatch_message=lambda s, b, c: sent.append(s) or {},
            briefing_builders={"open_requests": lambda t: 0,
                               "evaluation_summary": lambda t: {"assessment_count": 0, "scored_count": 0, "unscored_count": 0}},
        )
        now = MON_10.replace(hour=8, minute=45)
        first = wr.maybe_send_briefing(now, deps, self.config)
        second = wr.maybe_send_briefing(now, deps, self.config)
        self.assertTrue(first["sent"])
        self.assertFalse(second["sent"])
        self.assertEqual(len(sent), 1)

    def test_loop_survives_unexpected_error_and_sleeps(self):
        logs, sleeps = [], []
        with patch.object(watch_config, "load_config", side_effect=RuntimeError("x")):
            wr.run_loop(self.deps(), sleep=sleeps.append, max_ticks=2, log=logs.append)
        self.assertEqual(len(sleeps), 2)
        self.assertTrue(all("예상하지 못한" in line for line in logs))

    def test_loop_runs_cycle_then_waits_for_interval(self):
        logs = []
        config = {**self.config, "briefing": {"enabled": False, "time": "08:30"}}
        clock = iter([MON_10, MON_10 + dt.timedelta(seconds=30), MON_10 + dt.timedelta(minutes=61)])
        with patch.object(watch_config, "load_config", return_value=config), \
                patch.object(watch_config, "now_in_zone", side_effect=lambda c: next(clock)):
            wr.run_loop(self.deps(), sleep=lambda s: None, max_ticks=3, log=logs.append)
        # 첫 틱 실행, 둘째 틱(30초 뒤)은 주기 미도래, 셋째 틱(61분 뒤) 실행
        self.assertEqual(self.collects, 2)

    def test_status_never_contains_password(self):
        with patch.object(watch_config, "load_config", return_value=self.config):
            text = str(wr.status())
        self.assertIn("smtp_password_set", text)
        self.assertNotIn("password'", text.replace("smtp_password_set", ""))


if __name__ == "__main__":
    unittest.main()
