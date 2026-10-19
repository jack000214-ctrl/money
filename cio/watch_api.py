"""`/api/watch/*` 요청 처리. HTTP 서버와 분리해 직접 호출·테스트할 수 있다.

`route()` 는 감시 경로가 아니면 `None` 을, 맞으면 `(HTTP 상태, 응답 객체)` 를 낸다.
응답에는 비밀번호 값을 싣지 않고(설정 여부 불리언만), 내부 예외 문자열도 싣지 않는다.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from cio import watch_config, watch_notify, watch_runner, watch_store

PREFIX = "/api/watch/"
DEFAULT_EVENTS = 50
MAX_EVENTS = 200
TEST_EMAIL_COOLDOWN_SECONDS = 30.0

_LOG = logging.getLogger(__name__)
_last_test_email = 0.0

Response = tuple[int, dict[str, Any]]

_NOT_FOUND: Response = (404, {"error": "Not Found"})
_INTERNAL: Response = (500, {"error": "서버 내부 오류로 처리하지 못했다"})


def route(method: str, path: str, query: dict[str, list[str]], body: Any) -> Response | None:
    if not path.startswith(PREFIX):
        return None
    name = path[len(PREFIX):]
    handler = _ROUTES.get((method.upper(), name))
    if handler is None:
        return _NOT_FOUND
    try:
        return handler(query, body)
    except watch_config.WatchConfigError as exc:
        return 400, {"error": "설정이 올바르지 않다", "errors": exc.errors}
    except Exception as exc:  # noqa: BLE001 - 예외 문자열은 응답·로그에 싣지 않는다
        _LOG.error("watch api failure: %s %s (%s)", method, path, type(exc).__name__)
        return _INTERNAL


# ── 조회 ────────────────────────────────────────────────────────────────

def _status(_query, _body) -> Response:
    payload = watch_runner.status()
    payload["not_monitored"] = ((payload.get("last_cycle") or {}).get("not_monitored")) or []
    return 200, payload


def _limit(query: dict[str, list[str]]) -> int:
    raw = (query.get("limit") or [""])[0]
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_EVENTS
    if value < 1:
        return DEFAULT_EVENTS
    return min(value, MAX_EVENTS)


def _text(event: dict[str, Any]) -> dict[str, str]:
    try:
        return watch_notify.format_event(event)
    except Exception:  # noqa: BLE001 - 알 수 없는 이벤트도 목록에는 남긴다
        return {"title": str(event.get("type") or "이벤트"), "body": ""}


def _events(query, _body) -> Response:
    read_through = int(watch_store.load_state().get("events_read_through") or 0)
    rows = watch_store.read_events(limit=_limit(query))
    events = [
        {**row, "read": int(row.get("seq") or 0) <= read_through, "text": _text(row)}
        for row in reversed(rows)
    ]
    return 200, {"events": events, "unread": watch_store.unread_count()}


def _queue(_query, _body) -> Response:
    from cio import watch_queue

    return 200, {"items": watch_queue.list_items()}


# ── 읽음 ────────────────────────────────────────────────────────────────

def _mark_read(_query, body) -> Response:
    seq = (body or {}).get("through_seq") if isinstance(body, dict) else None
    if seq is not None and (not isinstance(seq, int) or isinstance(seq, bool) or seq < 0):
        return 400, {"error": "through_seq는 0 이상의 정수여야 한다"}
    through = watch_store.mark_read_through(seq)
    return 200, {"read_through": through, "unread": watch_store.unread_count()}


# ── 설정 ────────────────────────────────────────────────────────────────

def _prune(changes: Any, template: Any) -> Any:
    """설정에 없는 키(예: 화면이 실수로 보낸 비밀번호 필드)를 걸러낸다."""
    if isinstance(changes, dict) and isinstance(template, dict):
        return {k: _prune(v, template[k]) for k, v in changes.items() if k in template}
    return changes


def _get_config(_query, _body) -> Response:
    password_set = watch_notify.smtp_password_present()
    try:
        config = watch_config.load_config()
    except watch_config.WatchConfigError as exc:
        return 200, {"config": None, "errors": exc.errors, "smtp_password_set": password_set}
    return 200, {"config": config, "errors": [], "smtp_password_set": password_set}


def _put_config(_query, body) -> Response:
    if not isinstance(body, dict):
        raise watch_config.WatchConfigError(["설정 본문은 객체여야 한다"])
    saved = watch_config.save_config(_prune(body, watch_config.default_config()))
    return 200, {"config": saved, "errors": [], "smtp_password_set": watch_notify.smtp_password_present()}


# ── 투자자 정책 ─────────────────────────────────────────────────────────

def _get_investor_policy(_query, _body) -> Response:
    from cio.policy import load_policy, POLICY_PATH
    raw_exists = POLICY_PATH.exists()
    data, errors = load_policy()
    return 200, {"policy": data, "errors": errors, "exists": raw_exists}


def _put_investor_policy(_query, body) -> Response:
    import json
    from cio.policy import load_policy, validate_policy, POLICY_PATH, POLICY_SCHEMA
    if not isinstance(body, dict):
        return 400, {"error": "본문은 JSON 객체여야 한다"}
    existing_data, _ = load_policy()
    merged = {**(existing_data or {}), **body}
    merged["schema_version"] = POLICY_SCHEMA
    errors = validate_policy(merged)
    if errors:
        return 400, {"error": "정책 형식이 올바르지 않다", "errors": errors}
    POLICY_PATH.parent.mkdir(parents=True, exist_ok=True)
    POLICY_PATH.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    data, _ = load_policy()
    return 200, {"policy": data, "errors": []}


# ── 시험 메일 · 수동 실행 ───────────────────────────────────────────────

def _test_email(_query, _body) -> Response:
    global _last_test_email
    now = time.time()
    if now - _last_test_email < TEST_EMAIL_COOLDOWN_SECONDS:
        return 429, {"error": "시험 메일은 잠시 후 다시 보낼 수 있다"}
    config = watch_config.load_config()
    _last_test_email = now
    try:
        ok, reason = watch_notify.send_email(
            "시험 메일",
            "감시 알림 이메일 설정을 시험하는 메일이다. 어떤 종목에 대한 내용도 담지 않았다.",
            config,
        )
    except Exception as exc:  # noqa: BLE001 - 접속 정보가 섞인 예외 문자열을 숨긴다
        _LOG.error("test email failure (%s)", type(exc).__name__)
        return _INTERNAL
    return 200, {"ok": ok, "reason": reason}


def _run_once(_query, _body) -> Response:
    return 200, {"cycle": watch_runner.run_cycle()}


_ROUTES = {
    ("GET", "status"): _status,
    ("GET", "events"): _events,
    ("GET", "queue"): _queue,
    ("POST", "events/read"): _mark_read,
    ("GET", "config"): _get_config,
    ("PUT", "config"): _put_config,
    ("POST", "config"): _put_config,
    ("POST", "test-email"): _test_email,
    ("POST", "run-once"): _run_once,
    ("GET", "investor-policy"): _get_investor_policy,
    ("PUT", "investor-policy"): _put_investor_policy,
}
