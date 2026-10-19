"""재질문 대기열: 가격 문턱 접촉 이벤트마다 `cio prepare` 와 같은 패킷을 미리 만든다.

PC 는 질문을 **준비**할 뿐이다. 패킷에 verdict 를 넣지 않으며, 패킷이 PASS 인지는
`validation.status`(데이터 준비 상태)로만 읽는다. 새 판단은 모델이 낸다.

`build_context` 는 인자로 주입한다. 테스트가 네트워크·knowledge 없이 대체할 수 있어야 한다.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Callable

from cio import watch_store

PACKET_TTL_HOURS = 12  # README: 패킷 유효 시간
PENDING = "PENDING"
EXPIRED = "EXPIRED"
USED = "USED"
RESOLUTIONS = (USED, EXPIRED)

PREPARED = "PREPARED"
BLOCKED = "BLOCKED"
PENDING_EXISTS = "PENDING_EXISTS"

BuildContext = Callable[..., tuple[dict[str, Any], Any]]


def _default_build() -> BuildContext:
    from cio.context import build_context

    return build_context


def _parse(value: Any) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.astimezone()


def _now(now: dt.datetime | None) -> dt.datetime:
    return now or dt.datetime.now().astimezone()


def expire(now: dt.datetime | None = None) -> int:
    """유효 시간이 지난 PENDING 항목을 EXPIRED 로 표시한다. 바꾼 개수를 돌려준다."""
    current = _now(now)
    queue = watch_store.load_queue()
    changed = 0
    for item in queue.get("items", []):
        if item.get("status") != PENDING:
            continue
        expires = _parse(item.get("expires_at"))
        if expires is None or expires <= current:
            item["status"] = EXPIRED
            changed += 1
    if changed:
        watch_store.save_queue(queue)
    return changed


def pending_for(ticker: str, now: dt.datetime | None = None) -> dict[str, Any] | None:
    """종목의 미만료 PENDING 항목. 없으면 None."""
    current = _now(now)
    for item in watch_store.load_queue().get("items", []):
        if item.get("ticker") != ticker or item.get("status") != PENDING:
            continue
        expires = _parse(item.get("expires_at"))
        if expires is not None and expires > current:
            return item
    return None


def list_items(now: dt.datetime | None = None) -> list[dict[str, Any]]:
    expire(now)
    return list(watch_store.load_queue().get("items", []))


def resolve(context_id: str, status: str) -> bool:
    """사용자가 패킷을 모델에 넘겼다/버렸다고 표시한다. 판정이 아니라 처리 표시다."""
    if status not in RESOLUTIONS:
        raise ValueError(f"status는 {' | '.join(RESOLUTIONS)} 중 하나여야 한다")
    queue = watch_store.load_queue()
    for item in queue.get("items", []):
        if item.get("context_id") == context_id:
            item["status"] = status
            watch_store.save_queue(queue)
            return True
    return False


def prepare_for_events(
    events: list[dict[str, Any]],
    holdings: list[dict[str, Any]],
    *,
    build: BuildContext | None = None,
    now: dt.datetime | None = None,
) -> list[dict[str, Any]]:
    """저장된(`seq` 가 있는) PRICE_TRIGGER 이벤트마다 패킷을 준비한다.

    각 이벤트의 이벤트 로그에 `queue_status` 등을 덧붙이고, 같은 사실을 담은 사본을
    돌려준다(알림이 `context_id` 를 문구에 쓰도록). 종목 하나의 실패가 다른 종목을
    막지 않는다.
    """
    current = _now(now)
    qty_by_ticker = {h["ticker"]: h["qty"] for h in holdings}
    queue = watch_store.load_queue()
    items: list[dict[str, Any]] = queue.setdefault("items", [])
    expire(current)
    items[:] = watch_store.load_queue().get("items", [])
    out: list[dict[str, Any]] = []
    builder = build

    for event in events:
        if event.get("type") != "PRICE_TRIGGER":
            out.append(event)
            continue
        ticker = event["ticker"]
        seq = event.get("seq")
        changes: dict[str, Any]
        existing = next(
            (
                item for item in items
                if item.get("ticker") == ticker and item.get("status") == PENDING
                and (_parse(item.get("expires_at")) or current) > current
            ),
            None,
        )
        if existing is not None:
            changes = {"queue_status": PENDING_EXISTS}
        else:
            try:
                builder = builder or _default_build()
                packet, _path = builder(
                    ticker,
                    price=float(event["price"]),
                    intent=event["intent"],
                    held_quantity=qty_by_ticker.get(ticker),
                    save=True,
                )
            except Exception as exc:  # noqa: BLE001 - 종목 하나의 실패가 사이클을 막지 않는다
                changes = {
                    "queue_status": BLOCKED,
                    "queue_note": f"패킷 준비 실패: {type(exc).__name__}",
                }
            else:
                validation = packet.get("validation") or {}
                if validation.get("status") == "PASS" and packet.get("context_id"):
                    items.append(
                        {
                            "context_id": packet["context_id"],
                            "ticker": ticker,
                            "name": event.get("name"),
                            "event_seq": seq,
                            "intent": event["intent"],
                            "created_at": current.isoformat(timespec="seconds"),
                            "expires_at": (
                                current + dt.timedelta(hours=PACKET_TTL_HOURS)
                            ).isoformat(timespec="seconds"),
                            "status": PENDING,
                        }
                    )
                    changes = {"context_id": packet["context_id"], "queue_status": PREPARED}
                else:
                    problems = validation.get("problems") or []
                    changes = {
                        "queue_status": BLOCKED,
                        "queue_note": str(problems[0]) if problems else "패킷 검증 실패",
                    }
        if seq is not None:
            watch_store.update_event(int(seq), changes)
        out.append({**event, **changes})

    watch_store.save_queue({"items": items})
    return out


def format_queue(items: list[dict[str, Any]]) -> str:
    if not items:
        return "재질문 대기열이 비어 있다."
    lines = []
    for item in items:
        lines.append(
            f"{item.get('status'):<8} {item.get('ticker')} {item.get('name') or ''} "
            f"{item.get('intent')} {item.get('context_id')} (만료 {item.get('expires_at')})"
        )
    return "\n".join(lines)
