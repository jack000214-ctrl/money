"""감시 이벤트를 사용자에게 전달한다: 단일 포매터 + 채널 어댑터(콘솔·파일·이메일).

문구는 **포매터 하나**가 만든다. 모든 채널이 같은 문구를 쓰므로 채널마다 표현이
갈라지지 않는다. 문구는 사실 진술이다 — "문턱 X에 현재가 Y가 닿았다", "기록된 판단이
약속한 행동은 Z다". 매수·매도 지시, 추천, 순위는 만들지 않으며 그 금지는
`tests/test_watch_notify.py` 가 고정한다.

채널 하나의 실패는 다른 채널과 이벤트 기록을 막지 않는다. 오류 메시지에는 비밀번호나
스택 추적을 싣지 않고 짧은 분류 사유만 돌려준다.

이메일
  - 호스트·계정·발신자·수신자: `config/watch.json` (설정 화면에서 바꾼다. 환경변수
    `SMTP_HOST`/`SMTP_PORT`/`SMTP_USER`/`SMTP_FROM`/`NOTIFY_EMAIL_TO` 는 빈 값의 대체)
  - 비밀번호: `.env` 의 `SMTP_PASSWORD` **에서만** 읽는다. 설정 파일과 API 응답에 싣지 않는다.
  - 전송은 STARTTLS(587 등) 또는 SSL(465)이며 인증서 검증을 끄지 않는다.
"""
from __future__ import annotations

import os
import smtplib
import socket
import ssl
import sys
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable

from cio import watch_store
from cio.paths import ROOT

SMTP_TIMEOUT_SECONDS = 20
SSL_PORT = 465

# 포매터 출력에 나오면 안 되는 표현. 테스트가 이 목록으로 고정한다.
FORBIDDEN_PHRASES = (
    "매수하세요", "매도하세요", "사세요", "파세요", "사라", "팔아", "팔라",
    "매수 권", "매도 권", "추천", "지금 사", "지금 팔",
)

_INTENT_LABEL = {
    "HOLD_REVIEW": "보유 검토(HOLD_REVIEW)",
    "TRIM_REVIEW": "분할매도 검토(TRIM_REVIEW)",
}
_DIRECTION_LABEL = {"down": "이하", "up": "이상", "inside": "범위 안"}
_SIGNAL_ACTION = {"EXIT": "EXIT", "TRIM_HALF": "TRIM_HALF", "REASK": "REASK"}


# ── 포매터 ──────────────────────────────────────────────────────────────

def _money(value: Any, currency: str | None) -> str:
    if not isinstance(value, (int, float)):
        return str(value)
    if currency in (None, "KRW"):
        return f"{value:,.0f}원"
    return f"{value:,.2f} {currency}"


def _title(event: dict[str, Any]) -> str:
    kind = event.get("type")
    who = f"{event.get('name') or ''}({event['ticker']})" if event.get("ticker") else str(event.get("name") or "")
    labels = {
        "PRICE_TRIGGER": "가격 문턱 접촉",
        "POLICY_BREACH": "정책 한도 초과",
        "STALE_ASSESSMENT": "판단 재검토 필요 사실",
        "EARNINGS_NEAR": "실적 발표 임박",
    }
    return f"{labels.get(kind, '감시 이벤트')} — {who}".strip(" —")


def format_event(event: dict[str, Any]) -> dict[str, str]:
    """이벤트 하나를 {title, body} 사실 진술로 바꾼다."""
    kind = event.get("type")
    lines: list[str] = []
    currency = event.get("currency")

    if kind == "PRICE_TRIGGER":
        th = event["threshold"]
        if th["direction"] == "inside":
            level = f"{_money(th['value'], currency)}~{_money(th['value_high'], currency)}"
        else:
            level = _money(th["value"], currency)
        lines.append(
            f"{th['label']} {level}에 현재가 {_money(event['price'], currency)}이(가) 닿았다"
            f"({_DIRECTION_LABEL.get(th['direction'], th['direction'])})."
        )
        lines.append(
            f"시세 시각 {event.get('quote_time')} · 가격 종류 {event.get('price_kind')}."
        )
        lines.append(
            f"기록된 판단 {event.get('assessment_id')} (verdict {event.get('recorded_verdict')}"
            f", action {event.get('recorded_action')})."
        )
        stage = event.get("promised_action")
        if stage:
            lines.append(
                f"이 문턱에 연결된 기록된 단계 {stage.get('stage_id')}: 수량 {stage.get('quantity')}"
                f", 유효 {stage.get('valid_from')}~{stage.get('valid_until')}"
                f", 조건: {stage.get('condition')}"
                + (" (재검토 필요)" if stage.get("requires_review") else "")
            )
        for signal in event.get("invalidation_signals") or []:
            lines.append(
                f"판단이 적어 둔 무효화 신호(원문): {signal.get('signal')}"
                f" → 약속 행동 {_SIGNAL_ACTION.get(signal.get('action'), signal.get('action'))}."
                " 이 신호가 켜졌는지는 자동으로 판정하지 않았다."
            )
    elif kind == "POLICY_BREACH":
        limit, measured = event["limit"], event["measured"]
        unit = "%" if measured.get("unit") == "PCT" else "원"
        lines.append(
            f"{limit['name']} 한도 {limit['value']:,}{unit}에 대해 측정값은 {measured['value']:,}{unit}이다."
        )
        lines.append(f"산정 기준: {event.get('basis')}.")
    elif kind == "STALE_ASSESSMENT":
        reason = event.get("reason")
        if reason == "NO_LIVE_ASSESSMENT":
            lines.append("보유 중인데 계약을 통과하는 판단이 없다.")
        elif reason == "AGE":
            lines.append(
                f"최신 판단 {event.get('assessment_id')}이(가) {event['measured']['value']}일 지났다"
                f"(사용자가 정한 기준 {event['limit']['value']}일)."
            )
        elif reason == "PRICE_DRIFT":
            lines.append(
                f"판단 시점 가격 {_money(event['asked_price'], 'KRW')}에서 현재가 "
                f"{_money(event['price'], 'KRW')}까지 {event['measured']['value']:+}% 벗어났다"
                f"(사용자가 정한 기준 ±{event['limit']['value']}%)."
            )
    elif kind == "EARNINGS_NEAR":
        lines.append(
            f"{event.get('event')} {event.get('event_date')}까지 "
            f"{event['measured']['value']}영업일 남았다(사용자가 정한 기준 {event['limit']['value']}영업일)."
        )
        lines.append(f"{event.get('basis')}.")

    context_id = event.get("context_id")
    if context_id:
        intent = _INTENT_LABEL.get(event.get("intent"), event.get("intent"))
        lines.append(
            f"재질문 패킷 {context_id} 을(를) {intent} 질문으로 준비했다. 새 판단은 모델이 낸다."
        )
    elif event.get("queue_status") == "BLOCKED":
        lines.append(
            f"데이터 준비 필요: 재질문 패킷을 만들지 못했다 ({event.get('queue_note') or '사유 미상'})."
        )
    elif event.get("queue_status") == "PENDING_EXISTS":
        lines.append("같은 종목의 미처리 재질문 패킷이 이미 있어 새로 만들지 않았다.")
    lines.append("이 알림은 사실 진술이며 매매 판단이 아니다.")
    return {"title": _title(event), "body": "\n".join(lines)}


def format_digest(events: list[dict[str, Any]]) -> dict[str, str]:
    formatted = [format_event(event) for event in events]
    if len(formatted) == 1:
        return formatted[0]
    body = "\n\n".join(f"■ {item['title']}\n{item['body']}" for item in formatted)
    return {"title": f"감시 이벤트 {len(formatted)}건", "body": body}


# ── 환경변수 (.env) ─────────────────────────────────────────────────────

def read_env(name: str, env_file: Path | None = None) -> str:
    """환경변수를 읽고, 없으면 `.env` 를 직접 파싱한다 (`scripts/stock_service.load_env_file` 와 같은 규칙)."""
    value = os.environ.get(name)
    if value:
        return value
    path = env_file or (ROOT / ".env")
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            if key.strip() == name:
                return val.strip().strip("'\"")
    except OSError:
        pass
    return ""


def smtp_password_present(env_file: Path | None = None) -> bool:
    return bool(read_env("SMTP_PASSWORD", env_file))


# ── 채널 ────────────────────────────────────────────────────────────────

def resolve_email_settings(config: dict[str, Any], env_file: Path | None = None) -> dict[str, Any]:
    email = config.get("email") or {}
    host = email.get("smtp_host") or read_env("SMTP_HOST", env_file)
    port_text = email.get("smtp_port") or read_env("SMTP_PORT", env_file) or 587
    user = email.get("smtp_user") or read_env("SMTP_USER", env_file)
    sender = email.get("smtp_from") or read_env("SMTP_FROM", env_file) or user
    recipients = [
        row["address"] for row in email.get("recipients") or []
        if isinstance(row, dict) and row.get("enabled", True) and row.get("address")
    ]
    if not recipients:
        recipients = [a.strip() for a in read_env("NOTIFY_EMAIL_TO", env_file).split(",") if a.strip()]
    try:
        port = int(port_text)
    except (TypeError, ValueError):
        port = 587
    return {"host": host, "port": port, "user": user, "from": sender, "recipients": recipients}


def send_email(
    subject: str,
    body: str,
    config: dict[str, Any],
    *,
    env_file: Path | None = None,
    smtp_factory: Callable[..., Any] | None = None,
) -> tuple[bool, str]:
    """SMTP 로 보낸다. (성공 여부, 사유). 사유에는 비밀번호·스택 추적을 싣지 않는다."""
    settings = resolve_email_settings(config, env_file)
    password = read_env("SMTP_PASSWORD", env_file)
    if not settings["host"] or not settings["user"]:
        return False, "SMTP 호스트와 계정이 설정되지 않았다"
    if not password:
        return False, ".env 에 SMTP_PASSWORD 가 없다"
    if not settings["recipients"]:
        return False, "수신자가 없다"
    message = EmailMessage()
    message["Subject"] = f"[Money 감시] {subject}"
    message["From"] = settings["from"]
    message["To"] = ", ".join(settings["recipients"])
    message.set_content(body, charset="utf-8")
    context = ssl.create_default_context()
    try:
        if smtp_factory is not None:
            client = smtp_factory(settings["host"], settings["port"])
            with client as smtp:
                smtp.login(settings["user"], password)
                smtp.send_message(message)
        elif settings["port"] == SSL_PORT:
            with smtplib.SMTP_SSL(
                settings["host"], settings["port"], context=context, timeout=SMTP_TIMEOUT_SECONDS
            ) as smtp:
                smtp.login(settings["user"], password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(
                settings["host"], settings["port"], timeout=SMTP_TIMEOUT_SECONDS
            ) as smtp:
                smtp.ehlo()
                smtp.starttls(context=context)
                smtp.ehlo()
                smtp.login(settings["user"], password)
                smtp.send_message(message)
    except smtplib.SMTPAuthenticationError:
        return False, "SMTP 인증에 실패했다 (계정 또는 앱 비밀번호 확인)"
    except smtplib.SMTPRecipientsRefused:
        return False, "수신자 주소가 거부됐다"
    except smtplib.SMTPNotSupportedError:
        return False, "서버가 필요한 SMTP 기능(STARTTLS)을 지원하지 않는다"
    except (ssl.SSLError, ssl.CertificateError):
        return False, "TLS 연결 또는 인증서 검증에 실패했다"
    except (socket.timeout, TimeoutError):
        return False, "SMTP 서버 응답 시간이 초과됐다"
    except (smtplib.SMTPException, OSError):
        return False, "SMTP 서버에 연결하거나 전송하지 못했다"
    return True, "발송했다"


def send_console(subject: str, body: str) -> tuple[bool, str]:
    text = f"[감시] {subject}\n{body}\n\n"
    try:
        try:
            sys.stdout.write(text)
        except UnicodeEncodeError:
            # Windows cp949 콘솔은 일부 문자를 인코딩하지 못한다. 채널 실패가 아니라 표시 문제다.
            encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
            sys.stdout.write(text.encode(encoding, errors="replace").decode(encoding))
        sys.stdout.flush()
    except (OSError, ValueError):
        return False, "콘솔에 쓰지 못했다"
    return True, "출력했다"


def send_file(subject: str, body: str) -> tuple[bool, str]:
    path = watch_store.watch_dir() / "notifications.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(f"[{watch_store.now_iso()}] {subject}\n{body}\n\n")
    except OSError:
        return False, "알림 파일에 쓰지 못했다"
    return True, "기록했다"


def dispatch(
    events: list[dict[str, Any]],
    config: dict[str, Any],
    *,
    email_sender: Callable[..., tuple[bool, str]] | None = None,
) -> dict[str, dict[str, Any]]:
    """활성 채널로 보낸다. 채널마다 독립이며 하나가 실패해도 나머지를 계속한다."""
    if not events:
        return {}
    digest = format_digest(events)
    return dispatch_message(digest["title"], digest["body"], config, email_sender=email_sender)


def dispatch_message(
    subject: str,
    body: str,
    config: dict[str, Any],
    *,
    email_sender: Callable[..., tuple[bool, str]] | None = None,
) -> dict[str, dict[str, Any]]:
    channels = config.get("channels") or {}
    results: dict[str, dict[str, Any]] = {}
    sender = email_sender or send_email
    plan: list[tuple[str, Callable[[], tuple[bool, str]]]] = [
        ("console", lambda: send_console(subject, body)),
        ("file", lambda: send_file(subject, body)),
        ("email", lambda: sender(subject, body, config)),
    ]
    for name, action in plan:
        if not channels.get(name):
            continue
        try:
            ok, reason = action()
        except Exception:  # noqa: BLE001 - 어떤 채널 오류도 다른 채널을 막지 않는다
            ok, reason = False, "예상하지 못한 오류로 전달하지 못했다"
        results[name] = {"ok": ok, "reason": reason}
    # 대시보드 채널은 전송이 아니라 이벤트 로그를 읽는 배지다. 켜져 있음만 알린다.
    if channels.get("dashboard"):
        results["dashboard"] = {"ok": True, "reason": "이벤트 로그를 대시보드가 읽는다"}
    return results
