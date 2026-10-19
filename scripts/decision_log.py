#!/usr/bin/env python3
"""매매 판정 기록 보조 모듈.

trade_check.py 가 PROCEED/BLOCKED/… 판정을 낸 뒤 결과를 JSON Lines 로 남긴다.
기록 실패가 판정을 막아서는 안 되므로 호출 측은 예외를 try/except 로 삼킨다.
"""
from __future__ import annotations

import datetime as dt
import json
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "knowledge" / "decision_log"


def entry_from_result(result: dict, *, origin: str = "live") -> dict:
    """evaluate_trade() 결과 dict 에서 로그 항목을 만든다."""
    decision = result.get("decision") or {}
    return {
        "decision_snapshot_id": str(uuid.uuid4()),
        "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "origin": origin,
        "asset_id": result.get("asset_id", ""),
        "side": result.get("side", ""),
        "verdict": decision.get("verdict", ""),
        "issues": result.get("issues", []),
        "evaluated_at": result.get("evaluated_at", ""),
    }


def append(asset_id: str, entry: dict) -> Path:
    """LOG_DIR/<asset_id>.jsonl 에 항목을 한 줄 추가하고 경로를 반환한다."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    safe_id = asset_id.replace("/", "_").replace("\\", "_")
    log_path = LOG_DIR / f"{safe_id}.jsonl"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return log_path
