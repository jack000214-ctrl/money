"""판단을 명시적으로 폐기한다.

계약이 바뀌면 과거 판단이 재검증에 실패한다. 그 상태로 두면 파일은
`knowledge/assessments/` 에 그대로 남아 살아 있는 판단처럼 보이는데, 패킷은
그것을 싣지 않는다. **디렉터리를 직접 읽는 사람과 패킷을 읽는 모델이 서로
다른 것을 본다.** 실제로 그렇게 해서 죽은 판단의 손절가가 두 턴 동안 근거로
인용됐다.

폐기는 자동으로 하지 않는다. 계약 미달은 사고이지 결론이 아니고, 사고를
조용히 치우면 계약이 언제 무엇을 무효화했는지 아무도 모른다. 사용자가
`cio retire` 로 내리는 명시적 행동으로만 옮긴다.

옮겨진 판단은 `knowledge/retired/<asset_id>/` 에 폐기 사유와 함께 남는다.
지우지 않는 이유는 "그때 이렇게 판단했다"가 기록이기 때문이고, 옮기는 이유는
그 기록이 **근거로 재사용되면 안 되기** 때문이다.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from calculators.contracts import ContractError, load_json
from cio.assets import get_asset
from cio.decision import find_context, require_valid_decision
from cio.paths import KNOWLEDGE_DIR, ROOT

RETIRED_DIR = KNOWLEDGE_DIR / "retired"

CONTRACT_FAILURE = "CONTRACT_FAILURE"
USER_DECLARED = "USER_DECLARED"


class RetireError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _assessment_dir(asset_id: str) -> Path:
    return KNOWLEDGE_DIR / "assessments" / asset_id


def _retired_dir(asset_id: str) -> Path:
    # 호출 시점에 KNOWLEDGE_DIR 을 다시 읽는다. 테스트가 KNOWLEDGE_DIR 하나만
    # 갈아끼워도 폐기 보관소가 함께 따라가야 두 곳이 어긋나지 않는다.
    return KNOWLEDGE_DIR / "retired" / asset_id


def _contract_state(path: Path) -> tuple[str, str | None]:
    """판단 하나가 현재 계약을 통과하는지 본다.

    패킷이 쓰는 판정과 같은 경로(context 무결성 → 판단 재검증)를 밟는다.
    여기서 통과한 것만 패킷에 실리므로, 두 곳의 답이 갈리면 안 된다.
    """
    try:
        data = load_json(path)
    except (ContractError, OSError) as exc:
        return CONTRACT_FAILURE, f"판단 JSON을 읽을 수 없다: {exc}"
    context_id = str(data.get("context_id") or "")
    try:
        context, _ = find_context(context_id)
    except (ContractError, OSError, LookupError) as exc:
        return CONTRACT_FAILURE, f"당시 context를 찾을 수 없다: {exc}"
    try:
        require_valid_decision(data, context=context)
    except ContractError as exc:
        return CONTRACT_FAILURE, "; ".join(exc.errors[:2])
    return "LIVE", None


def survey(asset_id: str) -> dict[str, Any]:
    """이 자산의 판단이 현재 계약에서 어떤 상태인지 센다."""
    asset = get_asset(asset_id)
    directory = _assessment_dir(asset["asset_id"])
    live: list[dict[str, Any]] = []
    failing: list[dict[str, Any]] = []
    for path in sorted(directory.glob("CIO-*.json")) if directory.exists() else []:
        state, reason = _contract_state(path)
        try:
            data = load_json(path)
        except (ContractError, OSError):
            data = {}
        row = {
            "assessment_id": data.get("assessment_id") or path.stem,
            "created_at": data.get("created_at"),
            "verdict": data.get("verdict"),
            "path": str(path.relative_to(ROOT)),
            "reason": reason,
        }
        (live if state == "LIVE" else failing).append(row)
    return {
        "asset_id": asset["asset_id"],
        "live": live,
        "contract_failing": failing,
        "retired": retired_summary(asset["asset_id"]),
    }


def retired_summary(asset_id: str) -> dict[str, Any]:
    """폐기된 판단의 존재만 요약한다.

    개수와 시점과 사유만 낸다. verdict·가격계획·논지는 내지 않는다 — 폐기의
    뜻이 "인용하지 않는다"인데 인용할 수 있는 값을 실어 보내면 폐기가 아니다.
    """
    directory = _retired_dir(asset_id)
    if not directory.exists():
        return {"count": 0, "batches": []}
    batches: dict[tuple[str, str, str], dict[str, Any]] = {}
    count = 0
    for path in sorted(directory.glob("CIO-*.json")):
        try:
            data = load_json(path)
        except (ContractError, OSError):
            continue
        marker = data.get("_retired")
        if not isinstance(marker, dict):
            continue
        count += 1
        key = (
            str(marker.get("retired_at") or "")[:10],
            str(marker.get("class") or ""),
            str(marker.get("reason") or ""),
        )
        entry = batches.setdefault(
            key,
            {
                "retired_on": key[0],
                "class": key[1],
                "reason": key[2],
                "count": 0,
                "created_range": [None, None],
            },
        )
        entry["count"] += 1
        created = str(data.get("created_at") or "")[:10]
        if created:
            low, high = entry["created_range"]
            entry["created_range"] = [
                created if low is None else min(low, created),
                created if high is None else max(high, created),
            ]
    return {
        "count": count,
        "batches": sorted(batches.values(), key=lambda item: item["retired_on"]),
    }


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False
        ) as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            tmp = handle.name
        os.replace(tmp, path)
    finally:
        if tmp and Path(tmp).exists():
            Path(tmp).unlink()


def retire(
    asset_id: str,
    *,
    reason: str,
    include_live: bool = False,
    confirm: bool = False,
) -> dict[str, Any]:
    """계약 미달 판단(또는 전부)을 폐기 보관소로 옮긴다.

    `confirm=False` 면 아무것도 옮기지 않고 대상만 돌려준다. 판단 이력을
    지우는 것은 되돌리기 어려우므로 기본값을 실행이 아니라 조회로 둔다.
    """
    reason = (reason or "").strip()
    if not reason:
        raise RetireError(
            "폐기 사유가 필요하다. 왜 못 믿는지가 기록에 남아야 나중에 "
            "'그때 왜 버렸지'에 답할 수 있다"
        )
    state = survey(asset_id)
    targets = list(state["contract_failing"])
    if include_live:
        targets += state["live"]
    if not confirm:
        return {
            **state,
            "would_retire": targets,
            "confirmed": False,
            "_note": (
                "아무것도 옮기지 않았다. 실제로 폐기하려면 --confirm 을 붙인다"
            ),
        }

    retired_at = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    moved: list[dict[str, Any]] = []
    for row in targets:
        source = ROOT / row["path"]
        if not source.exists():
            continue
        data = load_json(source)
        marker_class = CONTRACT_FAILURE if row.get("reason") else USER_DECLARED
        data["_retired"] = {
            "retired_at": retired_at,
            "class": marker_class,
            "reason": reason,
            "contract_error": row.get("reason"),
            "original_path": row["path"],
            "_meaning": (
                "폐기된 판단이다. 기록으로 남기지만 근거로 인용하지 않는다. "
                "이 파일의 가격계획·논지·확률은 현재 판단에 쓰지 않는다"
            ),
        }
        destination = (
            _retired_dir(state["asset_id"]) / f"{row['assessment_id']}.json"
        )
        _write(destination, data)
        source.unlink()
        moved.append(
            {
                "assessment_id": row["assessment_id"],
                "created_at": row.get("created_at"),
                "class": marker_class,
                "to": str(destination.relative_to(ROOT)),
            }
        )
    after = survey(asset_id)
    return {
        "asset_id": state["asset_id"],
        "confirmed": True,
        "retired_at": retired_at,
        "reason": reason,
        "moved": moved,
        "live_remaining": after["live"],
        "contract_failing_remaining": after["contract_failing"],
    }


def format_survey(state: dict[str, Any]) -> str:
    lines = [f"판단 상태 — {state['asset_id']}"]
    lines.append(f"  살아 있음      {len(state['live'])}건")
    for row in state["live"]:
        lines.append(f"    {row['created_at']}  {row['assessment_id']}")
    lines.append(f"  계약 미달      {len(state['contract_failing'])}건")
    for row in state["contract_failing"]:
        lines.append(
            f"    {row['created_at']}  {row['assessment_id']}  — {row['reason']}"
        )
    retired = state.get("retired") or {}
    lines.append(f"  폐기됨         {retired.get('count', 0)}건")
    for batch in retired.get("batches", []):
        lines.append(
            f"    {batch['retired_on']}  {batch['count']}건  "
            f"[{batch['class']}]  {batch['reason']}"
        )
    if state.get("contract_failing") and not state.get("confirmed"):
        lines.append("")
        lines.append(
            "※ 계약 미달 판단은 패킷에 실리지 않는다. 그런데 파일은 "
            "assessments/ 에 남아 있어 직접 열면 살아 있는 판단처럼 보인다."
        )
    return "\n".join(lines)
