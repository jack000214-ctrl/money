"""모델이 낸 수집 요청을 추적한다.

`research_requests`는 판단 계약의 필수 필드이고 모델은 매번 진지하게 채운다 —
"DRAM 고정거래가격 수집기를 만들어 달라", "반기보고서 나오면 재수집해 달라".
그런데 그 요청이 assessment 파일 안에만 남으면 아무도 읽지 않는 편지가 된다.
여기서 요청을 모아 미해결/해결로 나누고, 다음 context에 다시 실어 보낸다.

요청의 신원은 정규화한 본문의 sha256이다. 같은 요청을 다른 판단에서 다시 내면
같은 ID로 묶여 `asked_count`가 올라간다 — 몇 번이나 요청했는데 아직 없는지가
사용자와 모델 모두에게 가장 중요한 신호이기 때문이다.

요청 본문은 모델이 쓴 자유 문장이다. 여기서 검증하는 것은 "이 문자열이 언제
몇 번 나왔는가"뿐이며, 요청 내용이 옳은지는 사용자가 판단한다.
"""

from __future__ import annotations

import datetime as dt
import difflib
import hashlib
import re
from pathlib import Path
from typing import Any

from calculators.contracts import ContractError, load_json, write_json
from cio.assets import get_asset
from cio.paths import KNOWLEDGE_DIR

KST = dt.timezone(dt.timedelta(hours=9))
LEDGER_SCHEMA = "research-ledger/1.0.0"
RESOLUTION_STATES = ("DONE", "DECLINED", "SUPERSEDED")
MAX_CONTEXT_REQUESTS = 4
MAX_CONTEXT_REQUEST_CHARS = 160
# 같은 뜻의 요청은 0.69 이상, 서로 다른 요청은 0.45 미만에 몰린다. 그 골짜기다.
SIMILARITY_THRESHOLD = 0.62
# 짧은 문자열은 유사도가 부풀려진다 — "요청 0"과 "요청 1"은 세 글자 중 둘이
# 같아 0.8이 나온다. 실제 요청은 문장이라 30자를 넘으므로, 그 아래는 묶지
# 않고 정확히 같을 때만 하나로 본다.
MIN_CLUSTER_CHARS = 30


class RequestError(ValueError):
    pass


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def request_id(text: str) -> str:
    normalized = _normalize(text)
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"REQ-{digest[:12]}"


def _compare_key(text: str) -> str:
    """비교용 표기. 구두점과 공백을 버리고 글자만 남긴다."""
    return re.sub(r"[^0-9A-Za-z가-힣]", "", text)


# 요청이 **어느 기간**을 가리키는지 드러내는 토큰만 본다. 숫자 전부를 보면 안
# 된다 — 재요청은 "(REQ-a68c…, 네 번째 요청)"이나 갱신된 금액을 달고 오므로,
# 그것까지 다르다고 치면 같은 요청의 반복 횟수가 갈라져 반복 신호가 죽는다.
PERIOD_TOKEN_RES = (
    (re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)"), "Y{}"),
    (re.compile(r"(?<!\d)([1-4])\s*분기"), "Q{}"),
    (re.compile(r"(?<![0-9A-Za-z])([1-4])Q|Q([1-4])(?![0-9A-Za-z])"), "Q{}"),
    (re.compile(r"(?<!\d)(1[0-2]|0?[1-9])\s*월"), "M{}"),
    (re.compile(r"(상반기|하반기|반기|연간)"), "{}"),
)


def _scope_signature(text: str) -> tuple[str, ...]:
    """요청 문장에 적힌 기간(연·분기·월·반기).

    문자열 유사도는 "2026년 1분기 실적 수집"과 "2026년 2분기 실적 수집"을 같은
    요청으로 본다(0.98). 그러면 1분기 요청 하나를 DONE 으로 닫는 순간 아직 받지
    않은 2분기 요청이 대기열에서 사라진다. 새 기간은 새 요청이다 — 기간이
    하나라도 다르면 문장이 아무리 닮아도 묶지 않는다. 덜 묶여 반복 횟수가
    갈리는 쪽이, 잘못 묶여 미해결 요청이 닫히는 쪽보다 싸다.
    """
    tokens: set[str] = set()
    for pattern, label in PERIOD_TOKEN_RES:
        for match in pattern.finditer(text):
            value = next(group for group in match.groups() if group)
            tokens.add(label.format(value.lstrip("0") or value))
    # '상반기'·'하반기'를 센 문장에서 그 안의 '반기'를 따로 세지 않는다.
    if tokens & {"상반기", "하반기"}:
        tokens.discard("반기")
    return tuple(sorted(tokens))


def _similar(left: str, right: str) -> float:
    if len(left) < MIN_CLUSTER_CHARS or len(right) < MIN_CLUSTER_CHARS:
        return 1.0 if left == right else 0.0
    return difflib.SequenceMatcher(None, left, right).ratio()


def cluster_requests(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """같은 뜻의 요청을 한 묶음으로 만든다.

    요청의 신원은 본문 해시라 문장이 한 글자만 달라도 다른 ID가 된다. 모델은
    매 판단에서 같은 요청을 조금씩 다르게 쓰기 때문에 — 마침표 하나, '재수집해'
    와 '재수집하고' — 그대로 두면 목록이 사실상 같은 말로 채워지고 asked_count
    가 언제나 1이 되어 반복 신호가 죽는다.

    실제 데이터에서 같은 뜻의 요청은 유사도 0.69 이상, 서로 다른 요청은 0.45
    미만에 몰려 있어 그 사이가 비어 있다. 임계값은 그 골짜기 한가운데다.
    묶인 문장은 variants 로 함께 보여 잘못 묶였으면 눈에 띄게 한다.
    """
    ordered = sorted(entries, key=lambda item: item["first_asked_at"])
    clusters: list[list[dict[str, Any]]] = []
    keys: list[list[str]] = []
    scopes: list[tuple[str, ...]] = []
    for entry in ordered:
        key = _compare_key(entry["text"])
        scope = _scope_signature(entry["text"])
        for index, group_keys in enumerate(keys):
            if scopes[index] == scope and any(
                _similar(key, other) >= SIMILARITY_THRESHOLD
                for other in group_keys
            ):
                clusters[index].append(entry)
                group_keys.append(key)
                break
        else:
            clusters.append([entry])
            keys.append([key])
            scopes.append(scope)

    result = []
    for members in clusters:
        # 대표는 가장 먼저 요청된 것이다. 나중 문장이 추가돼도 ID 가 바뀌지
        # 않아야 해결 기록이 살아남는다.
        head = members[0]
        result.append(
            {
                "request_id": head["request_id"],
                "text": head["text"],
                "member_ids": [member["request_id"] for member in members],
                "variants": [
                    member["text"] for member in members[1:]
                ],
                "first_asked_at": min(
                    member["first_asked_at"] for member in members
                ),
                "last_asked_at": max(
                    member["last_asked_at"] for member in members
                ),
                "asked_count": sum(member["asked_count"] for member in members),
                "asked_by": [
                    identifier
                    for member in members
                    for identifier in member["asked_by"]
                ],
                "from_unverified_assessment": all(
                    member["from_unverified_assessment"] for member in members
                ),
            }
        )
    return sorted(
        result, key=lambda item: (item["last_asked_at"], item["request_id"])
    )


def ledger_path(asset_id: str) -> Path:
    return KNOWLEDGE_DIR / "requests" / f"{asset_id}.json"


def load_ledger(asset_id: str) -> dict[str, Any]:
    path = ledger_path(asset_id)
    if not path.exists():
        return {
            "schema_version": LEDGER_SCHEMA,
            "asset_id": asset_id,
            "updated_at": None,
            "resolutions": [],
        }
    data = load_json(path)
    if data.get("schema_version") != LEDGER_SCHEMA:
        raise ContractError([f"{path}: {LEDGER_SCHEMA}가 아니다"])
    if data.get("asset_id") != asset_id:
        raise ContractError([f"{path}: 요청 원장의 asset_id가 다르다"])
    if not isinstance(data.get("resolutions"), list):
        raise ContractError([f"{path}: resolutions 배열이 없다"])
    return data


def collect_requests(asset_id: str) -> list[dict[str, Any]]:
    """저장된 판단에서 요청을 모아 같은 요청끼리 묶는다.

    계약이 바뀌어 재검증에 실패하는 판단의 요청도 읽는다. 요청은 모델이 쓴
    자유 문장이라 스키마 변화와 무관하게 뜻이 유지되고, 여기서 버리면 가장
    오래된 미해결 요청부터 사라지기 때문이다. 대신 출처 판단의 재검증
    상태를 `from_unverified_assessment`로 함께 남긴다.

    **폐기된 판단의 요청도 같은 이유로 읽는다.** 처음에는 살아 있는 판단만
    훑었는데, 그러면 새 판단이 옛 판단을 대체하고 옛것을 `retire` 하는 정상
    흐름에서 미해결 요청이 통째로 증발했다. 실제로 그랬다 — 세 건이 사라지고
    `resolved` 쪽에는 어느 요청도 가리키지 않는 해결 기록 8건만 남았다.
    `asked_count` 는 "몇 번이나 요청했는데 아직 없는가"를 세라고 만든 값인데,
    폐기를 빼면 살아 있는 판단 수를 넘을 수 없어 신호가 죽는다.

    폐기가 뜻하는 것은 "그 결론을 근거로 인용하지 않는다"이지 "그 데이터가
    이제 필요 없다"가 아니다. 요청은 결론이 아니라 아직 메워지지 않은 공백을
    가리키며, 그 공백은 판단을 폐기해도 그대로 있다. `score` 가 폐기된 판단도
    채점하는 것과 같은 구분이다.
    """
    directories = [
        KNOWLEDGE_DIR / "assessments" / asset_id,
        KNOWLEDGE_DIR / "retired" / asset_id,
    ]
    paths = [
        path
        for directory in directories
        if directory.exists()
        for path in sorted(directory.glob("CIO-*.json"))
    ]
    if not paths:
        return []
    from cio.decision import require_valid_decision, validate_context_integrity

    grouped: dict[str, dict[str, Any]] = {}
    for path in paths:
        try:
            data = load_json(path)
        except (ContractError, OSError):
            continue
        if data.get("asset_id") != asset_id:
            continue
        raw = data.get("research_requests")
        if not isinstance(raw, list):
            continue
        verified = False
        try:
            context_id = str(data.get("context_id") or "")
            context = load_json(
                KNOWLEDGE_DIR / "contexts" / asset_id / f"{context_id}.json"
            )
            if not validate_context_integrity(context, expected_id=context_id):
                verified = require_valid_decision(data, context=context) == data
        except (ContractError, OSError):
            verified = False
        created_at = str(data.get("created_at") or "")
        for item in raw:
            if not isinstance(item, str) or not _normalize(item):
                continue
            identifier = request_id(item)
            entry = grouped.setdefault(
                identifier,
                {
                    "request_id": identifier,
                    "text": _normalize(item),
                    "first_asked_at": created_at,
                    "last_asked_at": created_at,
                    "asked_count": 0,
                    "asked_by": [],
                    "from_unverified_assessment": True,
                },
            )
            entry["asked_count"] += 1
            entry["asked_by"].append(data.get("assessment_id") or path.stem)
            if created_at and created_at < entry["first_asked_at"]:
                entry["first_asked_at"] = created_at
            if created_at > entry["last_asked_at"]:
                entry["last_asked_at"] = created_at
            if verified:
                entry["from_unverified_assessment"] = False
    return sorted(
        grouped.values(),
        key=lambda item: (item["last_asked_at"], item["request_id"]),
    )


def request_state(asset_id: str) -> dict[str, Any]:
    """미해결과 해결된 요청을 나눠 돌려준다."""
    get_asset(asset_id)
    ledger = load_ledger(asset_id)
    resolved = {}
    for item in ledger["resolutions"]:
        if isinstance(item, dict) and isinstance(item.get("request_id"), str):
            resolved[item["request_id"]] = item
    known = cluster_requests(collect_requests(asset_id))
    open_requests = []
    closed_requests = []
    for entry in known:
        # 묶음 안의 어느 문장이든 해결됐으면 그 뜻은 해결된 것이다. 모델이
        # 같은 요청을 다시 쓴다고 해서 이미 만든 것이 미해결로 돌아오면 안 된다.
        resolution = next(
            (
                resolved[member]
                for member in entry["member_ids"]
                if member in resolved
            ),
            None,
        )
        if resolution:
            closed_requests.append({**entry, "resolution": resolution})
        else:
            open_requests.append(entry)
    clustered_ids = {
        member for entry in known for member in entry["member_ids"]
    }
    orphan_resolutions = [
        item
        for request_id_value, item in resolved.items()
        if request_id_value not in clustered_ids
    ]
    return {
        "asset_id": asset_id,
        "open": open_requests,
        "resolved": closed_requests,
        "orphan_resolutions": orphan_resolutions,
    }


def resolve_request(
    asset_id: str,
    request_id_value: str,
    *,
    state: str,
    note: str | None = None,
) -> Path:
    get_asset(asset_id)
    if state not in RESOLUTION_STATES:
        raise RequestError(
            f"state는 {' | '.join(RESOLUTION_STATES)} 중 하나여야 한다"
        )
    # 묶음 대표 ID 로도, 묶인 개별 문장의 ID 로도 닫을 수 있어야 한다.
    known = {}
    for cluster in cluster_requests(collect_requests(asset_id)):
        for member in cluster["member_ids"]:
            known[member] = cluster
    matches = [
        identifier
        for identifier in known
        if identifier == request_id_value
        or identifier.lower().startswith(request_id_value.lower())
    ]
    if not matches:
        raise RequestError(
            f"{asset_id}에 {request_id_value} 요청이 없다. "
            "`python3.13 -m cio requests <asset_id>`로 확인한다"
        )
    if len(matches) > 1:
        raise RequestError(
            f"{request_id_value}가 {len(matches)}개 요청과 일치한다: "
            + ", ".join(sorted(matches))
        )
    cluster = known[matches[0]]
    # 해결은 묶음 대표에 건다. 대표는 가장 먼저 요청된 문장이라 나중에 비슷한
    # 요청이 더 들어와도 바뀌지 않는다.
    identifier = cluster["request_id"]
    ledger = load_ledger(asset_id)
    now = dt.datetime.now(KST).isoformat(timespec="seconds")
    member_ids = set(cluster["member_ids"])
    resolutions = [
        item
        for item in ledger["resolutions"]
        if not (
            isinstance(item, dict) and item.get("request_id") in member_ids
        )
    ]
    resolutions.append(
        {
            "request_id": identifier,
            "text": cluster["text"],
            "state": state,
            "note": note,
            # 해결 판정은 사람의 것이다. 모델이 자기 요청을 스스로 닫으면
            # "요청했는데 아직 없다"는 신호가 의미를 잃는다.
            "resolved_by": "USER",
            "resolved_at": now,
        }
    )
    resolutions.sort(key=lambda item: (item["resolved_at"], item["request_id"]))
    ledger["resolutions"] = resolutions
    ledger["updated_at"] = now
    path = ledger_path(asset_id)
    write_json(path, ledger)
    return path


def open_requests_for_context(asset_id: str) -> list[dict[str, Any]]:
    """context에 실을 만큼만 줄인 미해결 요청 목록.

    여러 번 요청했는데 아직 없는 것이 가장 중요하므로 요청 횟수를 먼저 본다.
    """
    try:
        state = request_state(asset_id)
    except (ContractError, ValueError, OSError):
        return []
    ranked = sorted(
        state["open"],
        key=lambda item: (-item["asked_count"], item["last_asked_at"]),
    )
    result = []
    for entry in ranked[:MAX_CONTEXT_REQUESTS]:
        text = entry["text"]
        if len(text) > MAX_CONTEXT_REQUEST_CHARS:
            text = text[:MAX_CONTEXT_REQUEST_CHARS].rstrip() + "…"
        result.append(
            {
                "request_id": entry["request_id"],
                "text": text,
                "asked_count": entry["asked_count"],
                "first_asked_at": entry["first_asked_at"],
                "last_asked_at": entry["last_asked_at"],
            }
        )
    if result:
        result.append(
            {
                "_role": (
                    "네가 이전 판단에서 로컬에 만들어 달라고 요청했으나 아직 "
                    "반영되지 않은 것들이다. 같은 요청을 또 내는 대신, 이번에는 "
                    "그 공백을 직접 조사하거나 결론에 어떤 한계를 남기는지 "
                    "unknowns에 명시한다. 여전히 필요하면 다시 요청해도 되며 "
                    "asked_count가 올라가 우선순위 신호가 된다"
                ),
                "total_open": len(state["open"]),
                "_note": (
                    "미해결 요청이 total_open 건이고 그중 일부만 실려 있다. "
                    "전체는 `python3.13 -m cio requests <asset_id>`로 본다"
                ),
            }
        )
    return result


def format_requests(state: dict[str, Any]) -> str:
    lines = [f"{state['asset_id']} 수집 요청"]
    lines.append("")
    lines.append(f"미해결 {len(state['open'])}건")
    for entry in sorted(
        state["open"], key=lambda item: (-item["asked_count"], item["last_asked_at"])
    ):
        flag = " [출처 판단이 현재 계약으로 재검증되지 않음]" if entry[
            "from_unverified_assessment"
        ] else ""
        lines.append(
            f"  {entry['request_id']}  x{entry['asked_count']}  "
            f"최근 {entry['last_asked_at'][:10]}{flag}"
        )
        lines.append(f"      {entry['text']}")
        # 묶인 문장을 보여줘야 잘못 묶였을 때 눈에 띈다.
        for variant in entry.get("variants") or []:
            lines.append(f"      ↔ {variant[:100]}")
    lines.append("")
    lines.append(f"해결 {len(state['resolved'])}건")
    for entry in state["resolved"]:
        resolution = entry["resolution"]
        lines.append(
            f"  {entry['request_id']}  {resolution['state']}  "
            f"{str(resolution.get('resolved_at') or '')[:10]}"
        )
        lines.append(f"      {entry['text']}")
        if resolution.get("note"):
            lines.append(f"      → {resolution['note']}")
    if state["orphan_resolutions"]:
        lines.append("")
        lines.append(
            f"어느 판단에도 없는 해결 기록 {len(state['orphan_resolutions'])}건 "
            "(요청 본문이 바뀌었거나 판단이 삭제됐다)"
        )
        for item in state["orphan_resolutions"]:
            lines.append(f"  {item.get('request_id')}  {item.get('state')}")
    return "\n".join(lines)


__all__ = [
    "RequestError",
    "collect_requests",
    "format_requests",
    "ledger_path",
    "open_requests_for_context",
    "request_id",
    "request_state",
    "resolve_request",
]
