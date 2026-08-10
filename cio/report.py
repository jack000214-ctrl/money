"""기록된 판단을 사람이 읽을 수 있게 렌더링한다.

**이 파일은 아무것도 판정하지 않는다.** 모델이 쓴 값을 자리에 놓을 뿐이고,
임계값·등급·"싸다/비싸다" 같은 값을 여기서 만들지 않는다. 화면이 판정을 내기
시작하면 다음 판단의 모델이 그것을 근거로 받아들이고, 그 순간 판단의 주체가
조용히 바뀐다 — `calculators/position.py` 가 지키는 경계와 같은 경계다.

이 명령이 있는 이유는 하나다. `cio record` 뒤에 판단을 읽을 방법이 raw JSON
밖에 없었다. 그런데 사용자의 `stop_style` 이 `THESIS` 면 **무효화 신호가 유일한
매도 규율**이다 — 가격 손절이 없으므로 그 신호를 아무도 안 보면 안전장치가
통째로 없는 것과 같다. 그래서 무효화 신호와 반대 논지를 결론과 같은 크기로
놓는다.

같은 이유로 승률·확신도를 verdict 옆에 붙이고 `unknowns` 를 접지 않는다.
잘 정리된 화면은 45% 승률을 확신처럼 보이게 하는데, 그것이 이 저장소가
경계하는 "사용자의 확신을 정교하게 합리화하는" 실패의 표시 계층 판본이다.
"""

from __future__ import annotations

from typing import Any

from cio.display import display_width
from calculators.contracts import ContractError, load_json
from cio.assets import get_asset
from cio.memory import READABLE_EVALUATION_SCHEMAS
from cio.paths import KNOWLEDGE_DIR
from cio.retire import survey

WIDTH = 78

# 약속을 한국어로 편다. 라벨을 붙일 뿐 등급을 매기지 않는다 — EXIT 이 TRIM_HALF
# 보다 옳다는 표시는 어디에도 넣지 않는다.
INVALIDATION_ACTION_LABELS = {
    "EXIT": "전량 매도",
    "TRIM_HALF": "절반 축소",
    "REASK": "다시 묻는다 (행동 아님)",
}


# 터미널 표시 폭. 공용 구현을 쓰되 이름은 유지한다 — 이 화면의 접기 로직이
# 이 이름에 걸려 있고, 테스트도 이 경계를 이 이름으로 고정한다.
cell_width = display_width


def _wrap(text: str, indent: str = "  ") -> list[str]:
    """문단 구분을 살려 접는다. 모델의 논지는 빈 줄로 문단을 나눈다."""
    lines: list[str] = []
    for index, block in enumerate(str(text).split("\n")):
        if not block.strip():
            if index and lines and lines[-1] != "":
                lines.append("")
            continue
        current = indent
        for word in block.strip().split():
            candidate = word if current.strip() == "" else f"{current} {word}"
            if current.strip() and cell_width(candidate) > WIDTH:
                lines.append(current.rstrip())
                current = f"{indent}{word}"
            else:
                current = candidate if current.strip() else f"{indent}{word}"
        if current.strip():
            lines.append(current.rstrip())
    return lines


def _pad(text: str, width: int) -> str:
    """한글 라벨을 칸 기준으로 맞춘다. f-string 의 `<` 는 글자 수로 세어 어긋난다."""
    return text + " " * max(0, width - cell_width(text))


def _num(value: Any, *, unit: str = "", digits: int = 1) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return f"{value:,.{digits}f}{unit}"
    return str(value)


def _signed(value: Any) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "—"
    return f"{value:+,.1f}%"


def _section(title: str, note: str | None = None) -> list[str]:
    lines = ["", f"■ {title}"]
    if note:
        lines.extend(_wrap(note))
    return lines


def _find_evaluation(asset_id: str, assessment_id: str) -> dict[str, Any] | None:
    """이 판단의 최신 평가. 없으면 None — 없는 것도 정보다(아직 안 재봤다)."""
    directory = KNOWLEDGE_DIR / "evaluations" / asset_id
    if not directory.exists():
        return None
    best: dict[str, Any] | None = None
    for path in sorted(directory.glob("EVAL-*.json")):
        try:
            data = load_json(path)
        except (ContractError, OSError):
            continue
        if data.get("assessment_id") != assessment_id:
            continue
        if data.get("schema_version") not in READABLE_EVALUATION_SCHEMAS:
            continue
        if best is None or str(data.get("evaluated_at") or "") > str(
            best.get("evaluated_at") or ""
        ):
            best = data
    return best


def load_assessment(
    asset_id: str, *, assessment_id: str | None = None
) -> tuple[dict[str, Any], str, list[str]]:
    """판단 하나와 그 계약 상태를 읽는다.

    상태를 같이 내는 이유는 `cio retire` 가 이미 경고하는 함정 때문이다 —
    계약 미달 판단도 파일로는 남아 있어서, 그냥 열면 살아 있는 판단처럼
    보인다. 패킷에 실리지 않는 판단을 근거처럼 읽으면 안 된다.
    """
    asset = get_asset(asset_id)
    state = survey(asset["asset_id"])
    rows = [(row, "LIVE") for row in state["live"]]
    rows += [(row, "CONTRACT_FAILURE") for row in state["contract_failing"]]
    if not rows:
        raise ContractError(
            [
                f"{asset['asset_id']}: 읽을 판단이 없다. "
                "`cio retire` 로 폐기된 것이 있는지 확인한다"
            ]
        )
    if assessment_id:
        picked = [item for item in rows if item[0]["assessment_id"] == assessment_id]
        if not picked:
            raise ContractError([f"{assessment_id}: 이 자산의 판단이 아니다"])
    else:
        live = [item for item in rows if item[1] == "LIVE"]
        pool = live or rows
        picked = [max(pool, key=lambda item: str(item[0].get("created_at") or ""))]
    row, status = picked[0]
    warnings: list[str] = []
    if status == "CONTRACT_FAILURE":
        warnings.append(
            "이 판단은 현재 계약을 통과하지 못한다 — 패킷에 실리지 않으므로 "
            f"다음 판단이 이어받지 않는다. 사유: {row.get('reason')}"
        )
    if len(state["live"]) > 1 and not assessment_id:
        warnings.append(
            f"살아 있는 판단이 {len(state['live'])}건이다. 가장 최근 것을 보여준다 "
            "(--id 로 지정할 수 있다)"
        )
    retired = state.get("retired") or {}
    if retired.get("count"):
        warnings.append(
            f"폐기된 판단이 {retired['count']}건 있다. 폐기의 뜻은 "
            "'인용하지 않는다'이며 그 값은 여기 표시되지 않는다"
        )
    return load_json(row["path"]), status, warnings


def format_assessment(
    data: dict[str, Any],
    *,
    status: str,
    warnings: list[str],
) -> str:
    asset = get_asset(data["asset_id"])
    model = data.get("model") or {}
    plan = data.get("price_plan") or {}
    expected = data.get("expected_return_pct") or {}
    scenario = data.get("scenario_probability_pct") or {}
    action = data.get("action") or {}
    currency = plan.get("currency") or ""

    lines = [
        "=" * WIDTH,
        f"판단 — {data['asset_id']} {asset['name']}",
        f"  {data.get('assessment_id')}",
        f"  {data.get('created_at')} · {model.get('name')} · "
        f"prompt {model.get('prompt_version')} · "
        f"{'살아 있음' if status == 'LIVE' else '계약 미달'}",
        "=" * WIDTH,
    ]
    for warning in warnings:
        lines.extend(_wrap(f"※ {warning}", indent="  "))

    # 결론. 승률·확신도를 verdict 와 같은 자리에 둔다 — 따로 떼면 확신처럼 읽힌다.
    lines.extend(_section("결론"))
    lines.append(f"  verdict   {data.get('verdict'):<14}새 자금을 넣을 것인가")
    lines.append(
        f"  action    {action.get('direction'):<14}가진 것을 어떻게 할 것인가"
    )
    if action.get("undetermined_reason"):
        lines.append(f"            └ 막힌 이유: {action['undetermined_reason']}")
    if action.get("sell_reason"):
        lines.append(f"            └ 매도 사유: {action['sell_reason']}")
    # 수량과 실행 가격은 **화면에 찍혀야 한다.** 2026-08-07 판단은 TRIM 30주를
    # 390,000원에 걸라는 지시였는데 화면에는 둘 다 없었고(수량은 아예 렌더링되지
    # 않았다) 그 숫자들은 논지 문단 안에만 있었다. 읽는 사람이 본 것은 뜻이
    # 다른 scale_out_start 470,000 이었다.
    quantity = action.get("quantity")
    limit_price = action.get("limit_price")
    if quantity is not None or limit_price is not None:
        parts = []
        if quantity is not None:
            parts.append(f"{_num(quantity, digits=0)}주")
        if limit_price is not None:
            parts.append(
                f"지정가 {_num(limit_price, digits=0)} {currency}".rstrip()
            )
        lines.append(f"            └ 주문: {' · '.join(parts)}")
    lines.append(
        f"  승률 {_num(data.get('win_probability_pct'), unit='%')}   "
        f"확신도 {_num(data.get('confidence_pct'), unit='%')}   "
        f"평가 기간 {data.get('horizon_months')}개월"
    )
    trade = data.get("proposed_trade") or {}
    lines.append(
        f"  제안 집행액 {_num(trade.get('amount'), digits=0)} "
        f"{trade.get('currency') or ''}"
    )

    lines.extend(_section("논지"))
    lines.extend(_wrap(data.get("thesis") or "—"))

    # 반대 논지를 결론과 같은 크기로 둔다. 접으면 확증 편향의 편을 드는 화면이 된다.
    counter = data.get("strongest_counter_thesis") or {}
    lines.extend(_section("가장 강한 반대 논지"))
    lines.extend(_wrap(counter.get("text") or "—"))

    signals = data.get("invalidation_signals") or []
    lines.extend(
        _section(
            f"무효화 신호 ({len(signals)})",
            "stop_style 이 THESIS 면 이것이 유일한 매도 규율이다. "
            "가격 손절은 자동 매도선이 아니라 다시 물어볼 자리다",
        )
    )
    # action 을 신호 문장과 같은 줄에 붙인다. 접어 두거나 아래에 몰아 두면
    # 신호는 읽히고 약속은 읽히지 않는데, 정작 매도를 만드는 것은 약속이다.
    for index, row in enumerate(signals, 1):
        signal = row.get("signal") if isinstance(row, dict) else row
        action = row.get("action") if isinstance(row, dict) else None
        rationale = row.get("rationale") if isinstance(row, dict) else None
        body = _wrap(signal or "—", indent="     ")
        body[0] = f"  {index}. " + body[0].lstrip()
        lines.extend(body)
        if action:
            lines.append(f"     → 발생 시 {INVALIDATION_ACTION_LABELS.get(action, action)}")
        if rationale:
            lines.extend(_wrap(rationale, indent="       "))
    if signals and not any(
        isinstance(row, dict) and row.get("action") in {"EXIT", "TRIM_HALF"}
        for row in signals
    ):
        lines.extend(
            _wrap(
                "※ 이 판단에는 보유분을 줄이는 신호가 하나도 없다. 전부 "
                "'다시 묻는다'이며, stop_style 이 THESIS 라면 매도 지시가 나올 "
                "자리가 없다는 뜻이다",
                indent="  ",
            )
        )

    checks = data.get("prior_signal_checks")
    if checks is not None:
        fired = sum(
            1
            for row in checks
            if isinstance(row, dict) and row.get("state") == "FIRED"
        )
        lines.extend(
            _section(
                f"직전 약속 정산 ({len(checks)})",
                f"켜진 신호 {fired}건. 약속과 다르게 행동했다면 사유가 함께 있다",
            )
        )
        for row in checks:
            if not isinstance(row, dict):
                # 헤더가 이미 len(checks) 를 약속했다. 조용히 건너뛰면 "6건"
                # 이라 써놓고 5줄만 찍히고, 빠진 하나는 아무 데도 안 남는다.
                lines.append(f"  —  [손상] 정산 항목이 dict 가 아니다: {row!r:.60}")
                continue
            state = row.get("state") or "—"
            # 직전 판단의 몇 번 신호인지를 보여준다. 정산의 번호가 아니라
            # 약속의 번호라야 사람이 원본과 맞춰 볼 수 있다.
            position = row.get("signal_index")
            marker = "—" if position is None else f"#{position}"
            body = _wrap(row.get("signal") or "—", indent="     ")
            body[0] = f"  {marker} [{state}] " + body[0].lstrip()
            lines.extend(body)
            committed = row.get("committed_action")
            taken = row.get("taken_action")
            if committed or taken:
                mark = " ※ 약속 파기" if committed and taken and committed != taken else ""
                lines.append(
                    f"     약속 {INVALIDATION_ACTION_LABELS.get(committed, committed or '—')}"
                    f" → 이번 {INVALIDATION_ACTION_LABELS.get(taken, taken or '—')}{mark}"
                )
            if row.get("override_reason"):
                lines.extend(_wrap(f"파기 사유: {row['override_reason']}", indent="     "))
            if row.get("note"):
                lines.extend(_wrap(row["note"], indent="       "))

    lines.extend(_section("가격 계획", f"통화 {currency}"))
    for label, key in (
        ("선호 진입", "preferred_entry"),
        ("진입 범위 하단", "entry_range_low"),
        ("진입 범위 상단", "entry_range_high"),
        ("손절", "stop_price"),
        ("분할매도 시작", "scale_out_start"),
        ("재질문 하단", "reask_below"),
        ("재질문 상단", "reask_above"),
    ):
        lines.append(f"  {_pad(label, 22)}{_num(plan.get(key), digits=0):>12}")
    for event in plan.get("reask_events") or []:
        lines.extend(_wrap(f"· {event}", indent="  "))

    lines.extend(_section("기대수익", "확률가중은 모델이 낸 값이며 재계산하지 않는다"))
    lines.append(
        f"  약세 {_signed(expected.get('bear'))}   "
        f"기준 {_signed(expected.get('base'))}   "
        f"강세 {_signed(expected.get('bull'))}"
    )
    lines.append(
        f"  확률 {_num(scenario.get('bear'), unit='%')} / "
        f"{_num(scenario.get('base'), unit='%')} / "
        f"{_num(scenario.get('bull'), unit='%')}"
    )
    lines.append(
        f"  확률가중 {_signed(expected.get('probability_weighted'))}"
    )
    lines.append(
        f"  승리 문턱 {_signed(data.get('win_return_threshold_pct'))}   "
        f"실패 문턱 {_signed(data.get('failure_return_threshold_pct'))}"
    )

    drivers = data.get("drivers") or []
    lines.extend(
        _section("이익 동인", "비중 합 100. 수급·포지셔닝은 이익 동인이 아니라 아래 따로 있다")
    )
    for driver in drivers:
        mark = "조사함" if driver.get("researched") else "건너뜀"
        lines.append(f"  {_num(driver.get('weight_pct'), unit='%', digits=0):>5}  "
                     f"{driver.get('name')}  [{mark}]")
        detail = driver.get("finding") or driver.get("skipped_reason")
        if detail:
            lines.extend(_wrap(detail, indent="         "))

    checks = data.get("user_assertion_checks")
    if checks:
        lines.extend(
            _section("사용자 주장 검증", "REFUTED 는 실패가 아니라 이 시스템이 존재하는 이유다")
        )
        for check in checks:
            lines.append(f"  [{check.get('status')}] {check.get('claim_id')}")
            lines.extend(_wrap(check.get("finding") or "—", indent="     "))

    positioning = data.get("positioning")
    if positioning:
        affects = positioning.get("affects_conclusion")
        lines.extend(
            _section(
                "수급·포지셔닝",
                f"결론에 영향 {'있음' if affects else '없음'}",
            )
        )
        body = positioning.get("finding") or positioning.get("skipped_reason")
        lines.extend(_wrap(body or "—"))
        for item in positioning.get("unobservable") or []:
            lines.extend(_wrap(f"· 관측 불가: {item}", indent="  "))

    unknowns = data.get("unknowns") or []
    lines.extend(_section(f"모르는 것 ({len(unknowns)})", "접지 않는다"))
    for item in unknowns:
        body = _wrap(item, indent="     ")
        body[0] = "  · " + body[0].lstrip()
        lines.extend(body)

    requests = data.get("research_requests") or []
    if requests:
        lines.extend(_section(f"수집 요청 ({len(requests)})", "`cio requests` 로 닫는다"))
        for item in requests:
            body = _wrap(item, indent="     ")
            body[0] = "  · " + body[0].lstrip()
            lines.extend(body)

    sources = data.get("sources") or []
    if sources:
        lines.extend(_section(f"모델이 인용한 출처 ({len(sources)})"))
        for source in sources:
            lines.append(f"  {source.get('source_id')}  [{source.get('type')}]")
            lines.append(f"     {source.get('locator')}")

    evaluation = _find_evaluation(data["asset_id"], data.get("assessment_id") or "")
    lines.extend(_section("사후 평가"))
    if evaluation is None:
        lines.append("  아직 없다. `cio score` 로 채점한다")
    else:
        lines.extend(_format_evaluation(evaluation, verdict=data.get("verdict")))

    lines.append("")
    lines.append("─" * WIDTH)
    lines.extend(
        _wrap(
            "이 화면은 모델이 쓴 값을 그대로 옮긴 것이며 어떤 판정도 여기서 "
            "만들지 않는다. 승률과 확신도는 모델의 자기 추정이고, 실제 성과는 "
            "위 사후 평가가 가격으로 잰다.",
            indent="  ",
        )
    )
    return "\n".join(lines)


def _format_evaluation(
    evaluation: dict[str, Any], *, verdict: str | None
) -> list[str]:
    window = evaluation.get("window") or {}
    price = evaluation.get("price") or {}
    counterfactual = evaluation.get("counterfactual") or {}
    lines = [
        f"  기준일 {window.get('start_date')} → "
        f"{window.get('latest_available_date')}  "
        f"({'완료' if window.get('complete') else '진행 중'})",
        f"  질문가 기준 수익률 {_signed(price.get('return_to_end_pct'))}",
    ]
    waited = counterfactual.get("waited_for_entry") or {}
    if waited:
        filled = waited.get("filled")
        entry = _num(waited.get("entry_price"), digits=0)
        if filled:
            lines.append(
                f"  진입가 {entry} 체결됨 ({(waited.get('fill') or {}).get('date')}) "
                f"→ {_signed(waited.get('return_to_end_pct'))}"
            )
        elif filled is False:
            lines.append(f"  진입가 {entry} 미체결 — 포지션이 없었다")
        if waited.get("advantage_pct") is not None:
            lines.append(
                f"  기다린 것의 차이 {_signed(waited.get('advantage_pct'))} "
                "(즉시 매수 대비)"
            )
    if verdict in {"WAIT", "AVOID"}:
        lines.append(
            "  ※ WAIT·AVOID 는 질문가 수익률이 아니라 위 반사실이 본 채점이다"
        )
    return lines


def format_report(asset_id: str, *, assessment_id: str | None = None) -> str:
    data, status, warnings = load_assessment(
        asset_id, assessment_id=assessment_id
    )
    return format_assessment(data, status=status, warnings=warnings)


def _all_assessment_paths(asset_id: str) -> list[tuple[Any, str]]:
    """살아 있는 판단과 폐기된 판단을 모두 모은다.

    폐기된 것을 포함하는 이유는 `cio score` 와 같다 — **채점 계약은 인용
    계약보다 작다.** 폐기의 뜻은 "근거로 인용하지 않는다"이지 "그런 예측을
    한 적이 없다"가 아니며, 둘을 하나로 묶으면 계약을 조일 때마다 track
    record 가 통째로 사라진다.
    """
    rows: list[tuple[Any, str]] = []
    for directory, source in (
        (KNOWLEDGE_DIR / "assessments" / asset_id, "LIVE"),
        (KNOWLEDGE_DIR / "retired" / asset_id, "RETIRED"),
    ):
        if not directory.exists():
            continue
        for path in sorted(directory.glob("CIO-*.json")):
            rows.append((path, source))
    return rows


def evaluations_for(asset_id: str) -> dict[str, Any]:
    """판단별 최신 평가를 모아 track record 로 편다.

    packet 은 살아 있는 판단 최대 3건에 스칼라 다섯 개만 싣는다. 그것이
    맞는 설계다 — 채점 이력 전체를 packet 에 넣으면 예산을 먹고, 대부분의
    판단에서 읽히지도 않는다. 그러나 "내가 지금 같은 실수를 반복하는
    중인가"를 묻고 싶을 때 꺼낼 경로가 없으면, 모델은 파일을 직접 뒤지거나
    (재현되지 않는다) 아예 안 본다. 이 함수가 그 자리다 — `local_documents`
    본문에 대해 `cio search` 가 하는 것과 같은 역할이다.
    """
    asset = get_asset(asset_id)
    resolved = asset["asset_id"]
    rows: list[dict[str, Any]] = []
    unscored = 0
    for path, source in _all_assessment_paths(resolved):
        try:
            data = load_json(path)
        except (ContractError, OSError):
            continue
        assessment_id = data.get("assessment_id") or path.stem
        evaluation = _find_evaluation(resolved, assessment_id)
        if evaluation is None:
            unscored += 1
        plan = data.get("price_plan") or {}
        price = (evaluation or {}).get("price") or {}
        window = (evaluation or {}).get("window") or {}
        thresholds = (evaluation or {}).get("thresholds") or {}
        triggers = (evaluation or {}).get("triggers") or {}
        rows.append(
            {
                "assessment_id": assessment_id,
                "created_at": data.get("created_at"),
                "source": source,
                "citable": (evaluation or {}).get("citable"),
                "verdict": data.get("verdict"),
                "action": (data.get("action") or {}).get("direction"),
                "asked_price": price.get("start"),
                "latest_price": price.get("latest"),
                "return_to_latest_pct": price.get("return_to_latest_pct"),
                "window_complete": window.get("complete") if evaluation else None,
                "win_observed": thresholds.get("win_observed"),
                "failure_observed": thresholds.get("failure_observed"),
                "expected_return_pct": (
                    (data.get("expected_return_pct") or {}).get(
                        "probability_weighted"
                    )
                ),
                "price_plan": {
                    "preferred_entry": plan.get("preferred_entry"),
                    "stop_price": plan.get("stop_price"),
                    "scale_out_start": plan.get("scale_out_start"),
                },
                "triggered": sorted(
                    name
                    for name, value in triggers.items()
                    if isinstance(value, dict)
                ),
                "evaluation_id": (evaluation or {}).get("evaluation_id"),
            }
        )
    rows.sort(key=lambda row: str(row.get("created_at") or ""))
    return {
        "asset_id": resolved,
        "name": asset.get("name"),
        "assessment_count": len(rows),
        "scored_count": len(rows) - unscored,
        "unscored_count": unscored,
        "rows": rows,
        "_role": (
            "저장된 채점 결과의 조회다. 판정하지 않으며 무엇이 좋은 판단인지 "
            "여기서 정하지 않는다. RETIRED 는 근거로 인용하지 않되 채점에는 "
            "남는다 — 폐기는 인용 계약이고 채점 계약은 그보다 작다"
        ),
    }


def format_evaluations(asset_id: str, *, with_plan: bool = False) -> str:
    report = evaluations_for(asset_id)
    lines = [
        "=" * WIDTH,
        f"평가 — {report['asset_id']} {report.get('name') or ''}".rstrip(),
        f"  판단 {report['assessment_count']}건 · 채점 {report['scored_count']}건"
        f" · 미채점 {report['unscored_count']}건",
        "=" * WIDTH,
    ]
    if not report["rows"]:
        lines.append("  기록된 판단이 없다")
        return "\n".join(lines)

    header = (
        f"  {_pad('판단시각', 17)}{_pad('ID', 10)}{_pad('출처', 9)}"
        f"{_pad('기준가', 10)}{_pad('결론', 14)}{_pad('실현%', 8)}"
    )
    if with_plan:
        header += f"{_pad('선호진입', 10)}{_pad('손절', 10)}{_pad('분할매도', 10)}"
    lines += ["", header, "  " + "-" * (WIDTH - 4)]
    for row in report["rows"]:
        created = str(row.get("created_at") or "")[:16].replace("T", " ")
        decision = f"{row.get('verdict') or '—'}/{row.get('action') or '—'}"
        line = (
            f"  {_pad(created, 17)}"
            f"{_pad(str(row['assessment_id'])[4:12], 10)}"
            f"{_pad(row['source'], 9)}"
            f"{_pad(_num(row.get('asked_price'), digits=0), 10)}"
            f"{_pad(decision, 14)}"
            f"{_pad(_signed(row.get('return_to_latest_pct')), 8)}"
        )
        if with_plan:
            plan = row["price_plan"]
            line += (
                f"{_pad(_num(plan.get('preferred_entry'), digits=0), 10)}"
                f"{_pad(_num(plan.get('stop_price'), digits=0), 10)}"
                f"{_pad(_num(plan.get('scale_out_start'), digits=0), 10)}"
            )
        lines.append(line)
        if row["triggered"]:
            lines.append(f"      발동: {', '.join(row['triggered'])}")

    lines += [
        "",
        "  ※ RETIRED 는 근거로 인용하지 않는다. 채점에는 남기며 그것이 폐기의 뜻이다",
        "  ※ 실현%는 판단 시점 기준가 대비 최신가이며 평가 기간이 끝나지 않았다",
    ]
    if not with_plan:
        lines.append("  ※ --plan 으로 그때의 진입·손절·분할매도 문턱까지 본다")
    return "\n".join(lines)


__all__ = [
    "format_report",
    "format_assessment",
    "load_assessment",
    "evaluations_for",
    "format_evaluations",
]
