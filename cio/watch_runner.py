"""감시 러너: 한 사이클(게이트 → 시세 → 대조 → 이벤트 → 재질문 큐 → 알림 → 상태)과 루프.

PC 는 부등호만 대조한다. 매수·매도 판정은 하지 않는다. 외부 의존(시세·판단·정책·캘린더·
알림)은 `Deps` 로 주입해 테스트가 네트워크·knowledge 없이 돈다. 종목 하나의 실패는
`errors` 에 기록하고 나머지를 계속한다.
"""
from __future__ import annotations

import datetime as dt
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from cio import (
    paths,
    watch_briefing,
    watch_config,
    watch_notify,
    watch_queue,
    watch_review,
    watch_store,
    watch_triggers,
)

OK = "OK"
SKIPPED = "SKIPPED"
ERROR = "ERROR"
LOCK_STALE_SECONDS = 2 * 60 * 60
LOOP_TICK_SECONDS = 30


# ── 기본 의존성(실제 시스템) ─────────────────────────────────────────────

def _default_holdings(markets):
    from collectors import quote

    return quote.load_holdings(markets)


def _default_collect(holdings, now):
    from collectors import quote

    return quote.collect(holdings, now=now)


def _default_asked_price(ticker: str, assessment: dict[str, Any]) -> float | None:
    from cio.report import evaluations_for

    wanted = assessment.get("assessment_id")
    for row in evaluations_for(ticker).get("rows", []):
        if row.get("assessment_id") == wanted:
            value = row.get("asked_price")
            return float(value) if isinstance(value, (int, float)) else None
    return None


def _default_average_cost(ticker: str) -> float | None:
    from calculators.position import read_ledger

    value = read_ledger(ticker, "price_krw").get("average_cost")
    return float(value) if isinstance(value, (int, float)) else None


def _default_sectors() -> dict[str, str]:
    from collectors import quote

    assets = quote._read_json(quote.ASSETS_FILE).get("assets") or {}
    return {
        ticker: meta["sector_code"]
        for ticker, meta in assets.items()
        if isinstance(meta, dict) and meta.get("sector_code")
    }


def _default_policy():
    from cio.policy import load_policy

    data, _errors = load_policy()
    return data


def _default_business_days_until(target: str, today: dt.date):
    from collectors.kr_calendar import business_days_until

    return business_days_until(target, today)


def _default_calendar(ticker: str):
    return watch_review.load_calendar(ticker, paths.KNOWLEDGE_DIR)


@dataclass
class Deps:
    holdings: Callable[[Any], list[dict[str, Any]]] = _default_holdings
    collect: Callable[[list[dict[str, Any]], dt.datetime], dict[str, Any]] = _default_collect
    assessment: Callable[[str], dict[str, Any] | None] = watch_triggers.latest_live_assessment
    asked_price: Callable[[str, dict[str, Any]], float | None] = _default_asked_price
    average_cost: Callable[[str], float | None] = _default_average_cost
    sectors: Callable[[], dict[str, str]] = _default_sectors
    policy: Callable[[], dict[str, Any] | None] = _default_policy
    calendar: Callable[[str], dict[str, Any] | None] = _default_calendar
    business_days_until: Callable[[str, dt.date], int | None] = _default_business_days_until
    prepare: Callable[..., list[dict[str, Any]]] = watch_queue.prepare_for_events
    dispatch: Callable[..., dict[str, dict[str, Any]]] = watch_notify.dispatch
    dispatch_message: Callable[..., dict[str, dict[str, Any]]] = watch_notify.dispatch_message
    briefing_builders: dict[str, Any] = field(default_factory=dict)


# ── 겹침 방지 ───────────────────────────────────────────────────────────

class CycleBusy(Exception):
    """다른 사이클이 돌고 있다."""


class _Lock:
    def __init__(self) -> None:
        self.path = watch_store.watch_dir() / "runner.lock"
        self.fd: int | None = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, str(os.getpid()).encode())
                return self
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                except OSError:
                    continue
                if age < LOCK_STALE_SECONDS:
                    raise CycleBusy() from None
                try:
                    self.path.unlink()  # 죽은 프로세스가 남긴 잠금
                except OSError:
                    raise CycleBusy() from None
        raise CycleBusy()

    def __exit__(self, *_exc):
        if self.fd is not None:
            os.close(self.fd)
        try:
            self.path.unlink()
        except OSError:
            pass


# ── 사이클 ──────────────────────────────────────────────────────────────

def _commit(changes: dict[str, Any]) -> None:
    """상태의 일부 키만 바꾼다. API 서버가 쓴 읽음 표시를 덮어쓰지 않도록 다시 읽는다."""
    state = watch_store.load_state()
    state.update(changes)
    watch_store.save_state(state)


def _record(result: str, reason: str | None, at: str, **extra: Any) -> dict[str, Any]:
    cycle = {"at": at, "result": result, "reason": reason, **extra}
    _commit({"last_cycle": cycle})
    return cycle


def run_cycle(
    now: dt.datetime | None = None,
    deps: Deps | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """감시 사이클 1회. 반환은 `state["last_cycle"]` 과 같다."""
    deps = deps or Deps()
    try:
        config = config or watch_config.load_config()
    except watch_config.WatchConfigError as exc:
        return _record(ERROR, "CONFIG_INVALID", watch_store.now_iso(), errors=exc.errors[:5])
    now = now or watch_config.now_in_zone(config)
    at = now.isoformat(timespec="seconds")

    gate = watch_config.gate(now, config)
    if gate["decision"] == watch_config.SKIP:
        return _record(SKIPPED, gate["reason"], at, message=gate["message"])

    try:
        with _Lock():
            return _run_locked(now, at, deps, config)
    except CycleBusy:
        return _record(SKIPPED, "BUSY", at, message="다른 감시 사이클이 돌고 있다")


def _run_locked(now: dt.datetime, at: str, deps: Deps, config: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    not_monitored: list[dict[str, str]] = []
    now_text = at
    today = now.date()
    review = config["review"]

    try:
        holdings = deps.holdings(config["markets"])
    except Exception as exc:  # noqa: BLE001
        return _record(ERROR, "HOLDINGS_UNREADABLE", at, errors=[f"보유 종목을 읽지 못했다({type(exc).__name__})"])
    if not holdings:
        return _record(OK, "NO_HOLDINGS", at, counts={"holdings": 0, "events": 0}, errors=[], not_monitored=[])

    try:
        collected = deps.collect(holdings, now)
    except Exception as exc:  # noqa: BLE001
        return _record(ERROR, "QUOTES_FAILED", at, errors=[f"시세를 수집하지 못했다({type(exc).__name__})"])
    quotes = collected.get("quotes") or {}

    state = watch_store.load_state()
    active = dict(state.get("active_triggers") or {})
    events: list[dict[str, Any]] = []

    # 판단 로드(종목별 격리). 읽지 못한 종목은 '판단 없음'과 구별해 대조에서 뺀다.
    assessments: dict[str, dict[str, Any] | None] = {}
    loaded: list[dict[str, Any]] = []
    for holding in holdings:
        ticker = holding["ticker"]
        try:
            assessments[ticker] = deps.assessment(ticker)
            loaded.append(holding)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{ticker}: 판단을 읽지 못했다({type(exc).__name__})")
            not_monitored.append({"item": f"assessment:{ticker}", "reason": "판단을 읽지 못했다"})

    # 1) 가격 문턱
    for holding in loaded:
        ticker = holding["ticker"]
        try:
            outcome = watch_triggers.evaluate_asset(
                ticker=ticker, name=holding["name"], assessment=assessments[ticker],
                quote=quotes.get(ticker), active=active, today=today.isoformat(), now_iso=now_text,
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{ticker}: 문턱 대조 실패({type(exc).__name__})")
            continue
        active = outcome["active"]
        events.extend(outcome["events"])
        errors.extend(outcome["errors"])
        if outcome["skipped"] == "QUOTE_NOT_COMPARABLE":
            not_monitored.append({"item": f"price:{ticker}", "reason": "오늘 갱신된 현재가가 없다"})

    # 2) 정책 한도
    try:
        average_costs: dict[str, float] = {}
        for holding in holdings:
            try:
                cost = deps.average_cost(holding["ticker"])
            except Exception:  # noqa: BLE001 - 원장이 없으면 평가손실만 건너뛴다
                cost = None
            if cost is not None:
                average_costs[holding["ticker"]] = cost
        policy_result = watch_review.policy_conditions(
            holdings, quotes, deps.policy(), deps.sectors(), average_costs, now_text
        )
        not_monitored.extend(policy_result["not_monitored"])
        if policy_result["evaluated"]:
            new, active = watch_review.apply_dedup(
                active, policy_result["conditions"], [watch_review.PREFIX + "POLICY|"], now_text
            )
            events.extend(new)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"정책 한도 대조 실패({type(exc).__name__})")

    # 3) 판단 노후화
    try:
        asked: dict[str, float | None] = {}
        for holding in loaded:
            assessment = assessments[holding["ticker"]]
            if assessment is None:
                continue
            try:
                asked[holding["ticker"]] = deps.asked_price(holding["ticker"], assessment)
            except Exception:  # noqa: BLE001
                asked[holding["ticker"]] = None
        stale_result = watch_review.staleness_conditions(
            loaded, quotes, assessments, asked, review, today, now_text
        )
        not_monitored.extend(stale_result["not_monitored"])
        prefixes: list[str] = []
        for holding in loaded:
            ticker = holding["ticker"]
            prefixes.append(f"{watch_review.PREFIX}STALE|none|{ticker}")
            prefixes.append(f"{watch_review.PREFIX}STALE|age|{ticker}|")
            if (quotes.get(ticker) or {}).get("comparable"):
                prefixes.append(f"{watch_review.PREFIX}STALE|drift|{ticker}|")
        new, active = watch_review.apply_dedup(active, stale_result["conditions"], prefixes, now_text)
        events.extend(new)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"판단 노후화 대조 실패({type(exc).__name__})")

    # 4) 실적 임박
    try:
        calendars: dict[str, dict[str, Any] | None] = {}
        for holding in holdings:
            try:
                calendars[holding["ticker"]] = deps.calendar(holding["ticker"])
            except Exception:  # noqa: BLE001
                calendars[holding["ticker"]] = None
        earn = watch_review.earnings_conditions(
            holdings, calendars, review, today, now_text, deps.business_days_until
        )
        not_monitored.extend(earn["not_monitored"])
        if review.get("earnings_within_business_days") is not None:
            prefixes = [
                f"{watch_review.PREFIX}EARN|{field_name}|{ticker}|"
                for ticker, calendar in calendars.items() if calendar
                for field_name in ("next_provisional_result", "expected_periodic_report")
            ]
            new, active = watch_review.apply_dedup(active, earn["conditions"], prefixes, now_text)
            events.extend(new)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"실적 임박 대조 실패({type(exc).__name__})")

    # 이벤트 기록 → 상태 확정(같은 사실을 다음 사이클에 다시 알리지 않도록 알림 전에 저장)
    stored = [watch_store.append_event(event) for event in events]
    _commit({"active_triggers": active})

    delivered = stored
    try:
        delivered = deps.prepare(stored, holdings, now=now)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"재질문 패킷 준비 실패({type(exc).__name__})")

    channels: dict[str, Any] = {}
    if delivered:
        try:
            channels = deps.dispatch(delivered, config)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"알림 전달 실패({type(exc).__name__})")

    return _record(
        OK, None, at,
        counts={
            "holdings": len(holdings),
            "quotes_ok": collected.get("ok", 0),
            "quotes_failed": collected.get("failed", 0),
            "events": len(stored),
        },
        errors=errors,
        not_monitored=not_monitored,
        channels=channels,
    )


# ── 브리핑 ──────────────────────────────────────────────────────────────

def maybe_send_briefing(
    now: dt.datetime | None = None,
    deps: Deps | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    deps = deps or Deps()
    config = config or watch_config.load_config()
    now = now or watch_config.now_in_zone(config)
    state = watch_store.load_state()
    if not watch_briefing.briefing_due(config, state, now):
        return {"sent": False, "reason": "NOT_DUE", "path": None, "channels": {}}
    try:
        holdings = deps.holdings(config["markets"])
    except Exception:  # noqa: BLE001
        return {"sent": False, "reason": "HOLDINGS_UNREADABLE", "path": None, "channels": {}}
    quotes = (watch_store.load_quotes().get("quotes")) or {}
    not_monitored = ((state.get("last_cycle") or {}).get("not_monitored")) or []
    return watch_briefing.send_briefing(
        config, holdings, quotes, not_monitored, now,
        dispatch=deps.dispatch_message, **deps.briefing_builders,
    )


# ── 루프 ────────────────────────────────────────────────────────────────

def run_loop(
    deps: Deps | None = None,
    *,
    sleep: Callable[[float], None] = time.sleep,
    max_ticks: int | None = None,
    log: Callable[[str], None] = print,
) -> None:
    """`interval_minutes` 마다 사이클을, 매 틱마다 브리핑 도래 여부를 본다.

    설정 파일은 틱마다 다시 읽는다 — 화면에서 바꾼 주기·시간이 재시작 없이 반영된다.
    """
    deps = deps or Deps()
    next_cycle: dt.datetime | None = None
    ticks = 0
    while max_ticks is None or ticks < max_ticks:
        ticks += 1
        try:
            config = watch_config.load_config()
            now = watch_config.now_in_zone(config)
            if next_cycle is None or now >= next_cycle:
                cycle = run_cycle(now, deps, config)
                next_cycle = now + dt.timedelta(minutes=config["interval_minutes"])
                log(f"[{cycle['at']}] 사이클 {cycle['result']} {cycle.get('reason') or ''}".rstrip())
            maybe_send_briefing(now, deps, config)
        except watch_config.WatchConfigError as exc:
            log(f"설정 오류: {'; '.join(exc.errors[:3])}")
        except Exception as exc:  # noqa: BLE001 - 루프는 죽지 않는다
            log(f"예상하지 못한 오류({type(exc).__name__})")
        sleep(LOOP_TICK_SECONDS)


def status() -> dict[str, Any]:
    """상태 화면용 요약. 비밀번호 값은 싣지 않는다."""
    try:
        config = watch_config.load_config()
        config_error = None
    except watch_config.WatchConfigError as exc:
        config, config_error = None, exc.errors
    state = watch_store.load_state()
    now = watch_config.now_in_zone(config) if config else None
    return {
        "config_errors": config_error,
        "gate": watch_config.gate(now, config) if config else None,
        "last_cycle": state.get("last_cycle"),
        "unread": watch_store.unread_count(),
        "unset_review_fields": watch_config.unset_review_fields(config) if config else [],
        "smtp_password_set": watch_notify.smtp_password_present(),
    }
