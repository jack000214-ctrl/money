"""감시 배치의 저장소: 시세·이벤트·상태·대기열·브리핑 파일 입출력.

경로는 호출 시점에 `WATCH_DIR` 을 다시 읽는다. 테스트가 이 상수 하나만 갈아끼워도
모든 파일이 함께 따라가야 한다(`cio/retire.py` 와 같은 이유).

이벤트 로그는 추가 전용이다. 읽음 표시는 로그를 고치지 않고 `state.json` 의
`events_read_through` 에 따로 둔다 — 로그를 고치면 "그때 무엇을 알렸는가"의 기록이
사라진다.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import threading
from pathlib import Path
from typing import Any

from calculators.contracts import ContractError, load_json, write_json
from cio import paths

KST = dt.timezone(dt.timedelta(hours=9))
_LOCK = threading.Lock()


def watch_dir() -> Path:
    return paths.WATCH_DIR


def quotes_path() -> Path:
    return watch_dir() / "quotes.json"


def events_path() -> Path:
    return watch_dir() / "events.jsonl"


def state_path() -> Path:
    return watch_dir() / "state.json"


def queue_path() -> Path:
    return watch_dir() / "queue.json"


def briefings_dir() -> Path:
    return watch_dir() / "briefings"


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return json.loads(json.dumps(default))
    try:
        data = load_json(path)
    except (ContractError, OSError, ValueError):
        return json.loads(json.dumps(default))
    return data if isinstance(data, dict) else json.loads(json.dumps(default))


# ── 시세 ────────────────────────────────────────────────────────────────

def load_quotes() -> dict[str, Any]:
    return _read_json(quotes_path(), {"quotes": {}})


def save_quotes(data: dict[str, Any]) -> None:
    write_json(quotes_path(), data)


# ── 상태(중복 제거·루프·브리핑·읽음) ───────────────────────────────────

_STATE_DEFAULT: dict[str, Any] = {
    "active_triggers": {},
    "last_cycle": None,
    "briefing_sent_dates": [],
    "events_read_through": 0,
}


def load_state() -> dict[str, Any]:
    state = _read_json(state_path(), _STATE_DEFAULT)
    for key, value in _STATE_DEFAULT.items():
        state.setdefault(key, json.loads(json.dumps(value)))
    return state


def save_state(state: dict[str, Any]) -> None:
    write_json(state_path(), state)


# ── 이벤트 로그 ─────────────────────────────────────────────────────────

def append_event(event: dict[str, Any]) -> dict[str, Any]:
    """이벤트를 추가한다. 순번(seq)은 줄 수 + 1 이다."""
    with _LOCK:
        path = events_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        seq = len(read_events()) + 1
        row = {"seq": seq, **event}
        line = json.dumps(row, ensure_ascii=False, allow_nan=False)
        with open(path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    return row


def read_events(since_seq: int = 0, limit: int | None = None) -> list[dict[str, Any]]:
    path = events_path()
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and int(row.get("seq") or 0) > since_seq:
            rows.append(row)
    if limit is not None:
        rows = rows[-limit:]
    return rows


def update_event(seq: int, changes: dict[str, Any]) -> bool:
    """이미 쓴 이벤트에 후속 사실(context_id 연결)만 덧붙인다.

    추가 전용 원칙의 유일한 예외다. 이벤트 자체(문턱·가격·시각)는 바꾸지 않고
    `context_id`·`queue_note` 같은 후속 연결 필드만 허용한다.
    """
    allowed = {"context_id", "queue_status", "queue_note"}
    if not set(changes) <= allowed:
        raise ValueError(f"이벤트에 덧붙일 수 있는 필드는 {sorted(allowed)} 뿐이다")
    with _LOCK:
        rows = read_events()
        found = False
        for row in rows:
            if row.get("seq") == seq:
                row.update(changes)
                found = True
        if not found:
            return False
        path = events_path()
        payload = "".join(
            json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n" for row in rows
        )
        temporary = path.with_suffix(".tmp")
        temporary.write_text(payload, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    return True


def unread_count() -> int:
    state = load_state()
    return len(read_events(since_seq=int(state.get("events_read_through") or 0)))


def mark_read_through(seq: int | None = None) -> int:
    rows = read_events()
    latest = int(rows[-1]["seq"]) if rows else 0
    target = latest if seq is None else min(int(seq), latest)
    state = load_state()
    state["events_read_through"] = max(int(state.get("events_read_through") or 0), target)
    save_state(state)
    return state["events_read_through"]


# ── 대기열 ──────────────────────────────────────────────────────────────

def load_queue() -> dict[str, Any]:
    return _read_json(queue_path(), {"items": []})


def save_queue(data: dict[str, Any]) -> None:
    write_json(queue_path(), data)
