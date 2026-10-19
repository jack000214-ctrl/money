"""일일 브리핑: 보유 종목의 오늘 상태를 **사실로만** 나열한다.

판단·추천·순위를 만들지 않는다. 시세, 오늘 접촉한 문턱, 모델이 요청해 둔 자료 중
미해결 건수, 대기 중인 재질문 패킷, 기록된 판단의 채점 요약, 감시하지 못하는 항목을 싣는다.
하루 1회만 보낸다(`state["briefing_sent_dates"]`).
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any, Callable

from cio import watch_notify, watch_queue, watch_store

SENT_KEEP_DAYS = 30


def _fmt_price(value: Any, currency: str | None) -> str:
    if not isinstance(value, (int, float)):
        return "—"
    if currency == "KRW" or currency is None:
        return f"{value:,.0f}원"
    return f"{value:,.2f} {currency}"


def _open_requests(ticker: str) -> int | None:
    from cio.requests import request_state

    return len(request_state(ticker).get("open") or [])


def _evaluation_summary(ticker: str) -> dict[str, Any]:
    from cio.report import evaluations_for

    data = evaluations_for(ticker)
    return {
        "assessment_count": data.get("assessment_count", 0),
        "scored_count": data.get("scored_count", 0),
        "unscored_count": data.get("unscored_count", 0),
    }


def build_briefing(
    holdings: list[dict[str, Any]],
    quotes: dict[str, Any],
    events: list[dict[str, Any]],
    queue_items: list[dict[str, Any]],
    not_monitored: list[dict[str, str]],
    today: dt.date,
    *,
    open_requests: Callable[[str], int | None] = _open_requests,
    evaluation_summary: Callable[[str], dict[str, Any]] = _evaluation_summary,
) -> dict[str, str]:
    """{title, body}. 보조 조회가 실패하면 그 줄만 '조회하지 못했다'로 보고한다."""
    lines: list[str] = []
    day = today.isoformat()
    today_events = [e for e in events if str(e.get("detected_at") or "")[:10] == day]

    lines.append("■ 보유 종목 시세")
    if not holdings:
        lines.append("보유 종목이 없다.")
    for holding in holdings:
        ticker = holding["ticker"]
        quote = quotes.get(ticker) or {}
        price = quote.get("price")
        previous = quote.get("previous_close")
        currency = quote.get("currency") or holding.get("currency")
        text = f"- {holding['name']}({ticker}) {holding['qty']:g}주 · 현재가 {_fmt_price(price, currency)}"
        if isinstance(price, (int, float)) and isinstance(previous, (int, float)) and previous:
            text += f" · 전일 종가 {_fmt_price(previous, currency)} 대비 {(price - previous) / previous * 100:+.2f}%"
        if quote.get("status") == "FAILED":
            text += f" · 시세 조회 실패({quote.get('last_error')})"
        elif quote.get("price_kind") and quote.get("price_kind") != "REGULAR_LIVE":
            text += f" · 가격 종류 {quote.get('price_kind')}"
        if quote and quote.get("comparable") is False:
            text += " · 오늘 갱신된 시세가 아니다"
        lines.append(text)

    lines.append("")
    lines.append("■ 오늘 감지된 사실")
    if not today_events:
        lines.append("오늘 감지된 이벤트가 없다.")
    for event in today_events:
        formatted = watch_notify.format_event(event)
        lines.append(f"- {formatted['title']}")

    lines.append("")
    lines.append("■ 모델이 요청해 둔 자료와 기록된 판단")
    for holding in holdings:
        ticker = holding["ticker"]
        parts = []
        try:
            count = open_requests(ticker)
            parts.append(f"미해결 자료 요청 {count}건")
        except Exception:  # noqa: BLE001
            parts.append("자료 요청 조회하지 못했다")
        try:
            summary = evaluation_summary(ticker)
            parts.append(
                f"기록된 판단 {summary['assessment_count']}건"
                f"(채점 {summary['scored_count']}건, 미채점 {summary['unscored_count']}건)"
            )
        except Exception:  # noqa: BLE001
            parts.append("판단 기록 조회하지 못했다")
        lines.append(f"- {holding['name']}({ticker}): " + " · ".join(parts))

    lines.append("")
    lines.append("■ 대기 중인 재질문 패킷")
    pending = [i for i in queue_items if i.get("status") == watch_queue.PENDING]
    if not pending:
        lines.append("대기 중인 패킷이 없다.")
    for item in pending:
        lines.append(
            f"- {item.get('name') or ''}({item.get('ticker')}) {item.get('intent')} "
            f"{item.get('context_id')} · 만료 {item.get('expires_at')}"
        )

    lines.append("")
    lines.append("■ 감시하지 못하는 항목")
    if not not_monitored:
        lines.append("없음.")
    for row in not_monitored:
        lines.append(f"- {row.get('item')}: {row.get('reason')}")

    lines.append("")
    lines.append("이 브리핑은 사실 나열이며 매매 판단이 아니다.")
    return {"title": f"일일 브리핑 {day}", "body": "\n".join(lines)}


def briefing_due(config: dict[str, Any], state: dict[str, Any], now: dt.datetime) -> bool:
    """브리핑을 켜 뒀고, 시각이 지났고, 오늘 아직 안 보냈는가. 주말은 보내지 않는다."""
    brief = config.get("briefing") or {}
    if not brief.get("enabled"):
        return False
    if now.weekday() >= 5:
        return False
    if now.date().isoformat() in (state.get("briefing_sent_dates") or []):
        return False
    hour, minute = int(brief["time"][:2]), int(brief["time"][3:])
    return (now.hour, now.minute) >= (hour, minute)


def send_briefing(
    config: dict[str, Any],
    holdings: list[dict[str, Any]],
    quotes: dict[str, Any],
    not_monitored: list[dict[str, str]],
    now: dt.datetime,
    *,
    dispatch: Callable[..., dict[str, dict[str, Any]]] = watch_notify.dispatch_message,
    **builders: Any,
) -> dict[str, Any]:
    """브리핑을 만들고 저장하고 보낸다. 하루 1회. 반환: {sent, path, channels}."""
    state = watch_store.load_state()
    if not briefing_due(config, state, now):
        return {"sent": False, "reason": "NOT_DUE", "path": None, "channels": {}}
    watch_queue.expire(now)
    message = build_briefing(
        holdings,
        quotes,
        watch_store.read_events(),
        watch_store.load_queue().get("items", []),
        not_monitored,
        now.date(),
        **builders,
    )
    directory = watch_store.briefings_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path: Path = directory / f"{now.date().isoformat()}.md"
    path.write_text(f"# {message['title']}\n\n{message['body']}\n", encoding="utf-8", newline="\n")
    channels = dispatch(message["title"], message["body"], config)
    # 전달 시도 자체를 하루 1회로 센다. 이메일 서버가 죽었다고 매 사이클 재전송하지 않는다.
    state = watch_store.load_state()
    sent = sorted(set(state.get("briefing_sent_dates") or []) | {now.date().isoformat()})
    state["briefing_sent_dates"] = sent[-SENT_KEEP_DAYS:]
    watch_store.save_state(state)
    return {"sent": True, "reason": None, "path": str(path), "channels": channels}
