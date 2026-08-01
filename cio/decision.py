from __future__ import annotations

import datetime as dt
import json
import math
import re
import urllib.parse
from pathlib import Path
from typing import Any

from calculators.contracts import ContractError, content_hash, load_json, stable_hash
from cio.assets import ASSET_ID_RE
from cio.paths import KNOWLEDGE_DIR, ROOT

SCHEMA_VERSION = "cio-decision/1.0.0"
VERDICTS = {"BUY_NOW", "WAIT", "AVOID", "INSUFFICIENT"}
# UNDETERMINED 는 "행동을 지시할 수 없다"이지 "가만히 있으라"가 아니다. 보유
# 중인 종목에서 HOLD 는 빈 값이 아니라 계속 보유하라는 지시이므로, 모른다는
# 상태를 HOLD 로 접으면 시스템이 '모른다'를 '보유하라'로 번역하게 된다.
ACTION_DIRECTIONS = {"ADD", "HOLD", "TRIM", "EXIT", "UNDETERMINED"}
# 무엇이 없어서 정할 수 없는지. 이 구분이 있어야 "데이터가 없어서 막힌 비율"과
# "사용자 정책이 없어서 막힌 비율"을 따로 집계할 수 있다. 둘은 해결 주체가
# 다르다 — 앞은 수집기가, 뒤는 사용자가 푼다.
UNDETERMINED_REASONS = {
    "INSUFFICIENT_EVIDENCE",
    "NO_INVESTOR_POLICY",
    "NO_POSITION_DATA",
    "USER_INPUT_REQUIRED",
}
# 판단 기반 — 자산 자체가 나쁘다는 주장이라 verdict와 같은 편에 서야 한다.
JUDGMENT_SELL_REASONS = {"THESIS_BROKEN", "VALUATION", "BETTER_OPPORTUNITY"}
# 제약 기반 — 자산이 좋아도 낼 수 있다. 대신 제약임을 밝혀야 하고, 사후 평가는
# 이 둘을 절대 같은 통에 넣지 않는다. 가격 예측력과 위험 관리 비용은 다른 것이다.
CONSTRAINT_SELL_REASONS = {"CONCENTRATION", "RISK_BUDGET", "LIQUIDITY_NEED"}
SELL_REASONS = JUDGMENT_SELL_REASONS | CONSTRAINT_SELL_REASONS
OUTCOME_METRIC = "TERMINAL_TOTAL_RETURN_ASSET_CURRENCY"
CONTEXT_ID_RE = re.compile(r"^CTX-[0-9a-f]{20}$")
ASSESSMENT_ID_RE = re.compile(r"^CIO-[0-9a-f]{20}$")
EXPECTED_KEYS = {"bear", "base", "bull", "probability_weighted"}
PROBABILITY_KEYS = {"bear", "base", "bull"}
PRICE_KEYS = {
    "currency",
    "preferred_entry",
    "entry_range_low",
    "entry_range_high",
    "stop_price",
    "scale_out_start",
    "reask_below",
    "reask_above",
    "reask_events",
}
TOP_LEVEL_KEYS = {
    "schema_version",
    "assessment_id",
    "context_id",
    "asset_id",
    "created_at",
    "model",
    "verdict",
    "outcome_metric",
    "horizon_months",
    "win_definition",
    "failure_definition",
    "win_return_threshold_pct",
    "failure_return_threshold_pct",
    "win_probability_pct",
    "confidence_pct",
    "expected_return_pct",
    "scenario_probability_pct",
    "price_plan",
    "proposed_trade",
    "action",
    # 적립매수 프로그램. required 에 넣지 않은 것은 의도다 — 필수로 만들면
    # 이 필드가 없는 기존 판단이 전부 계약 미달로 끊긴다. 계약을 조일 때마다
    # 기억을 잃는 구조를 막기 위해 선택 필드로 둔다.
    "recurring_plan",
    # 수급·포지셔닝. 같은 이유로 선택 필드지만, packet 이 수급을 실어 보냈으면
    # 그때는 필수가 된다 (아래 _validate_positioning).
    "positioning",
    # 사용자 주장의 확인 결과. packet 에 user_assertions 가 있으면 필수가 된다.
    "user_assertion_checks",
    "drivers",
    "method",
    "thesis",
    "strongest_counter_thesis",
    "invalidation_signals",
    "unknowns",
    "research_requests",
    "sources",
    "evidence_refs",
}


DRIVER_KEYS = {"name", "weight_pct", "basis", "researched", "finding", "skipped_reason"}
DRIVER_REQUIRED = {"name", "weight_pct", "basis", "researched"}

# undetermined_reason 을 required 에 넣지 않은 것은 recurring_plan 과 같은 이유다 —
# 필수로 만들면 이 필드가 없는 기존 판단이 전부 계약 미달로 끊긴다.
ACTION_KEYS = {
    "direction", "quantity", "sell_reason", "rationale", "undetermined_reason",
}
ACTION_REQUIRED = {"direction", "quantity", "sell_reason", "rationale"}

RECURRING_KEYS = {
    "state", "amount_per_period", "currency", "period", "rationale",
}
RECURRING_REQUIRED = {"state", "rationale"}
RECURRING_STATES = {"START", "CONTINUE", "PAUSE", "STOP", "NONE"}
RECURRING_PERIODS = {"DAILY", "WEEKLY", "MONTHLY"}


def _validate_recurring_plan(plan: Any, errors: list[str]) -> None:
    """정기 자동매수 프로그램의 처분.

    `action` 이 '지금 가진 것'을 다룬다면 이쪽은 '앞으로 자동으로 사질 것'을
    다룬다. 둘을 나눈 이유는 **적립이 돌아가는 동안에는 판단하지 않아도 매수가
    계속되기 때문**이다. 적립을 걸어 둔 종목에서 침묵은 HOLD 가 아니라
    CONTINUE 다. 그 차이를 계약에 자리 없이 두면 모델은 이미 돌아가는 매수
    프로그램을 모른 채 "지금 살까"에만 답하게 된다.

    선택 필드다 — 없으면 검사하지 않는다.
    """
    if plan is None:
        return
    if not isinstance(plan, dict):
        errors.append("recurring_plan: 객체 또는 null이어야 한다")
        return
    unknown = sorted(set(plan) - RECURRING_KEYS)
    if unknown:
        errors.append(f"recurring_plan: 알 수 없는 필드 {', '.join(unknown)}")
    if not RECURRING_REQUIRED.issubset(plan):
        errors.append("recurring_plan: state·rationale이 필요하다")
        return
    if plan.get("state") not in RECURRING_STATES:
        errors.append(
            f"recurring_plan.state: {' | '.join(sorted(RECURRING_STATES))} "
            "중 하나여야 한다"
        )
    rationale = plan.get("rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        errors.append("recurring_plan.rationale: 비어 있을 수 없다")
    period = plan.get("period")
    if period is not None and period not in RECURRING_PERIODS:
        errors.append(
            f"recurring_plan.period: {' | '.join(sorted(RECURRING_PERIODS))} "
            "또는 null이어야 한다"
        )
    currency = plan.get("currency")
    if currency is not None and currency not in {"KRW", "USD"}:
        errors.append("recurring_plan.currency: KRW · USD 또는 null이어야 한다")
    amount = plan.get("amount_per_period")
    if amount is not None and (
        isinstance(amount, bool)
        or not isinstance(amount, (int, float))
        or not math.isfinite(amount)
        or amount <= 0
    ):
        errors.append("recurring_plan.amount_per_period: 0보다 큰 수 또는 null")
    # 계속·시작하려면 얼마를 얼마 간격으로인지가 있어야 한다. 그것 없이
    # "계속한다"만 쓰면 다음 판단이 무엇을 이어받는지 알 수 없다.
    if plan.get("state") in {"START", "CONTINUE"} and (
        amount is None or period is None
    ):
        errors.append(
            "recurring_plan: START·CONTINUE 는 amount_per_period 와 period 가 "
            "있어야 한다"
        )


POSITIONING_KEYS = {
    "checked", "finding", "skipped_reason", "unobservable", "affects_conclusion",
}

ASSERTION_CHECK_KEYS = {"claim_id", "status", "finding", "evidence_refs"}
# 사용자 주장을 닫는 세 가지 방법. `UNVERIFIABLE` 도 유효한 결말이다 — 확인할
# 수 없다는 것을 아는 것과 확인하지 않은 것은 다르다.
ASSERTION_STATUSES = {"CONFIRMED", "REFUTED", "UNVERIFIABLE"}


def _validate_assertion_checks(
    checks: Any,
    errors: list[str],
    *,
    expected_ids: set[str],
) -> None:
    """사용자가 사실이라고 말한 것을 확인했는지 본다.

    사용자는 이미 사고 싶은 상태에서 묻는다 — "실적 1,860% 나왔더만", "폴드8도
    그렇고". 그 문장을 전제로 받아 분석하면 결론은 질문에 이미 들어 있다.
    강한 모델일수록 그 프레이밍을 더 정교하게 보강한다.

    그래서 각 주장은 **확인·반박·확인불가 중 하나로 닫혀야 한다.** 열어둔 채
    결론을 내면 계약이 거부한다. 반박도 정당한 결말이며, 오히려 그것이 이
    시스템이 존재하는 이유에 가깝다.
    """
    if checks is None:
        if expected_ids:
            errors.append(
                "user_assertion_checks: packet에 사용자 주장이 실려 있다. "
                "각 주장을 확인·반박·확인불가 중 하나로 닫아야 한다"
            )
        return
    if not isinstance(checks, list):
        errors.append("user_assertion_checks: 배열 또는 null이어야 한다")
        return
    seen: set[str] = set()
    for index, row in enumerate(checks):
        label = f"user_assertion_checks[{index}]"
        if not isinstance(row, dict) or set(row) - ASSERTION_CHECK_KEYS:
            errors.append(
                f"{label}: claim_id·status·finding·evidence_refs만 허용한다"
            )
            continue
        claim_id = _nonempty(row.get("claim_id"), f"{label}.claim_id", errors)
        if claim_id:
            if claim_id in seen:
                errors.append(f"{label}.claim_id: 중복 {claim_id}")
            seen.add(claim_id)
            if expected_ids and claim_id not in expected_ids:
                errors.append(
                    f"{label}.claim_id: packet에 없는 주장이다 {claim_id}"
                )
        status = row.get("status")
        if status not in ASSERTION_STATUSES:
            errors.append(
                f"{label}.status: {', '.join(sorted(ASSERTION_STATUSES))} "
                "중 하나여야 한다"
            )
        _nonempty(row.get("finding"), f"{label}.finding", errors)
        refs = _string_list(
            row.get("evidence_refs") or [], f"{label}.evidence_refs", errors
        )
        # 확인했다·반박했다고 말하려면 무엇으로 그랬는지가 있어야 한다.
        # 확인불가는 근거가 없는 것이 정상이다.
        if status in {"CONFIRMED", "REFUTED"} and not refs:
            errors.append(
                f"{label}.evidence_refs: {status}에는 근거가 필요하다. "
                "근거 없이 확인했다고 말할 수 없다"
            )
    unanswered = sorted(expected_ids - seen)
    if unanswered:
        errors.append(
            "user_assertion_checks: 닫지 않은 사용자 주장이 있다 — "
            + ", ".join(unanswered)
        )


def _validate_positioning(positioning: Any, errors: list[str]) -> None:
    """누가 지금 사고 팔아야 하는가 — 이익 동인이 아닌 가격 결정 요인.

    `drivers` 는 **이익** 동인만 100% 로 배분한다. 그래서 수급·포지셔닝은
    배분표에 들어갈 칸이 없고, packet 에 실려도 판단의 무게에서 조용히 빠진다.

    실제로 그 실패가 있었다. 2026-07-29 삼성전자에서 기관이 597만주를
    순매수하고 거래량이 평소의 2.5배였는데(`structured_optional.kr_flow` 로
    packet 에 실려 나갔다), 다음 판단의 논지는 판가 얘기만 하고 수급을 한 줄도
    쓰지 않았다. 이틀 뒤 +20% 였다.

    사이클주에서 "누가 강제로 사야 하는가"는 6개월짜리 이익 추정만큼 가격을
    움직인다. 맞히라는 것이 아니라 **보고도 안 본 척하지 말라**는 것이다.

    선택 필드이되, **packet 이 수급을 실어 보냈으면 필수가 된다.** 받은 데이터를
    다루지 않고 넘어가는 것은 계약이 허용하지 않는다 (그 검사는 context 를
    해석한 뒤에 하므로 아래 validate_decision 의 context 블록에 있다).

    여기서는 형식만 본다.
    """
    if positioning is None:
        return
    if not isinstance(positioning, dict):
        errors.append("positioning: 객체 또는 null이어야 한다")
        return
    unknown = sorted(set(positioning) - POSITIONING_KEYS)
    if unknown:
        errors.append(f"positioning: 알 수 없는 필드 {', '.join(unknown)}")
    checked = positioning.get("checked")
    if not isinstance(checked, bool):
        errors.append("positioning.checked: true 또는 false여야 한다")
        return
    finding = positioning.get("finding")
    skipped = positioning.get("skipped_reason")
    if checked:
        if not isinstance(finding, str) or not finding.strip():
            errors.append(
                "positioning.finding: 확인했다면 무엇을 봤는지 남겨야 한다"
            )
        if skipped is not None:
            errors.append(
                "positioning.skipped_reason: 확인했으면 건너뛴 이유가 없다"
            )
    else:
        if not isinstance(skipped, str) or not skipped.strip():
            errors.append(
                "positioning.skipped_reason: 건너뛰었다면 왜 결론을 바꾸지 "
                "못하는지 밝혀야 한다"
            )
        if finding is not None:
            errors.append(
                "positioning.finding: 확인하지 않았으면 결과가 없다"
            )
    # 관측할 수 없는 것을 적어 두면 그 공백이 다음 판단으로 넘어간다. 공매도
    # 잔고·대차잔고처럼 로컬에 수집기가 없는 항목이 여기 쌓인다.
    _string_list(
        positioning.get("unobservable") or [],
        "positioning.unobservable",
        errors,
    )
    if not isinstance(positioning.get("affects_conclusion"), bool):
        errors.append(
            "positioning.affects_conclusion: true 또는 false여야 한다"
        )


def _validate_drivers(drivers: Any, errors: list[str]) -> None:
    """이익 동인의 배분과, 무엇을 조사하고 무엇을 건너뛰었는지를 검사한다.

    동인이 여럿인 회사에서 조사 순서를 정하지 않으면, 이익에 거의 영향이 없는
    이야기에 판단의 무게가 실린다. 비중의 합을 100으로 강제하는 것은 그래서다 —
    "다 중요하다"는 배분이 아니다. 조사하지 않은 동인은 이유를 남겨야 하며,
    그 이유는 다음 판단으로 넘어가 누적된다.
    """
    if not isinstance(drivers, list) or not drivers:
        errors.append("drivers: 이익 동인을 최소 하나 이상 명시해야 한다")
        return
    if len(drivers) > 8:
        errors.append("drivers: 8개를 넘을 수 없다")
        return
    total = 0.0
    names: list[str] = []
    for index, driver in enumerate(drivers):
        label = f"drivers[{index}]"
        if not isinstance(driver, dict):
            errors.append(f"{label}: 객체여야 한다")
            continue
        unknown = set(driver) - DRIVER_KEYS
        if unknown or not DRIVER_REQUIRED.issubset(driver):
            errors.append(
                f"{label}: name·weight_pct·basis·researched가 필요하다"
            )
            continue
        name = _nonempty(driver.get("name"), f"{label}.name", errors)
        if name:
            names.append(name)
        _nonempty(driver.get("basis"), f"{label}.basis", errors)
        weight = _number(
            driver.get("weight_pct"),
            f"{label}.weight_pct",
            errors,
            minimum=0.000001,
            maximum=100,
        )
        if weight is not None:
            total += float(weight)
        researched = driver.get("researched")
        if not isinstance(researched, bool):
            errors.append(f"{label}.researched: true 또는 false여야 한다")
            continue
        finding = driver.get("finding")
        skipped = driver.get("skipped_reason")
        if researched:
            if not isinstance(finding, str) or not finding.strip():
                errors.append(
                    f"{label}.finding: 조사했다면 무엇을 알아냈는지 남겨야 한다"
                )
            if skipped is not None:
                errors.append(
                    f"{label}.skipped_reason: 조사한 동인에는 건너뛴 이유가 없다"
                )
        else:
            if not isinstance(skipped, str) or not skipped.strip():
                errors.append(
                    f"{label}.skipped_reason: 건너뛰었다면 왜 결론을 바꾸지 "
                    "못하는지 밝혀야 한다"
                )
            if finding is not None:
                errors.append(
                    f"{label}.finding: 조사하지 않은 동인에는 결과가 없다"
                )
    if len(names) != len(set(names)):
        errors.append("drivers: 동인 이름이 중복됐다")
    if names and abs(total - 100) > 0.01:
        errors.append(f"drivers: weight_pct의 합은 100이어야 한다 ({total})")


def _nonempty(value: Any, field: str, errors: list[str]) -> str | None:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{field}: 비어 있지 않은 문자열이 필요하다")
        return None
    return value.strip()


def _number(
    value: Any,
    field: str,
    errors: list[str],
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    allow_null: bool = False,
) -> float | None:
    if value is None and allow_null:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        errors.append(f"{field}: 숫자가 필요하다")
        return None
    number = float(value)
    if not math.isfinite(number):
        errors.append(f"{field}: 유한한 숫자가 필요하다")
        return None
    if minimum is not None and number < minimum:
        errors.append(f"{field}: {minimum} 이상이어야 한다")
    if maximum is not None and number > maximum:
        errors.append(f"{field}: {maximum} 이하여야 한다")
    return number


def _datetime(value: Any, field: str, errors: list[str]) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        errors.append(f"{field}: 시간대가 포함된 ISO-8601 문자열이 필요하다")
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        errors.append(f"{field}: 올바른 ISO-8601 시각이 아니다")
        return None
    if parsed.tzinfo is None:
        errors.append(f"{field}: 시간대가 포함돼야 한다")
        return None
    return parsed


def _string_list(
    value: Any,
    field: str,
    errors: list[str],
    *,
    minimum: int = 0,
) -> list[str]:
    if not isinstance(value, list):
        errors.append(f"{field}: 문자열 배열이 필요하다")
        return []
    result = []
    for index, item in enumerate(value):
        text = _nonempty(item, f"{field}[{index}]", errors)
        if text:
            result.append(text)
    if len(result) < minimum:
        errors.append(f"{field}: 최소 {minimum}개가 필요하다")
    return result


def find_context(context_id: str) -> tuple[dict[str, Any], Path]:
    if not CONTEXT_ID_RE.fullmatch(context_id):
        raise ContractError([f"context_id {context_id}: CTX-<20자리 16진수> 형식이 아니다"])
    matches = list(
        (KNOWLEDGE_DIR / "contexts").glob(f"*/{context_id}.json")
    )
    if len(matches) != 1:
        raise ContractError(
            [f"context_id {context_id}: 컨텍스트를 정확히 하나 찾을 수 없다"]
        )
    data = load_json(matches[0])
    errors = validate_context_integrity(data, expected_id=context_id)
    if errors:
        raise ContractError([f"{matches[0]}: {error}" for error in errors])
    return data, matches[0]


def context_id_for(data: dict[str, Any]) -> str:
    return stable_hash(
        {key: value for key, value in data.items() if key != "context_id"},
        "CTX-",
    )


def validate_context_integrity(
    data: dict[str, Any],
    *,
    expected_id: str | None = None,
) -> list[str]:
    errors: list[str] = []
    context_id = data.get("context_id")
    if not isinstance(context_id, str) or not CONTEXT_ID_RE.fullmatch(context_id):
        errors.append("context_id가 CTX-<20자리 16진수> 형식이 아니다")
        return errors
    if expected_id and context_id != expected_id:
        errors.append("context_id가 요청값 또는 파일명과 다르다")
    if data.get("schema_version") != "cio-context/1.0.0":
        errors.append("지원하지 않는 context schema다")
    try:
        expected_hash = context_id_for(data)
    except (TypeError, ValueError):
        errors.append("context를 표준 JSON으로 지문화할 수 없다")
    else:
        if context_id != expected_hash:
            errors.append(f"context_id 내용 지문은 {expected_hash}이다")

    source_ids: set[str] = set()
    for index, item in enumerate(data.get("evidence_index") or []):
        if not isinstance(item, dict):
            errors.append(f"evidence_index[{index}]가 객체가 아니다")
            continue
        source_id = item.get("source_id")
        if not isinstance(source_id, str) or not source_id:
            errors.append(f"evidence_index[{index}].source_id가 비었다")
        elif source_id in source_ids:
            errors.append(f"evidence_index source_id가 중복됐다: {source_id}")
        else:
            source_ids.add(source_id)
        locator = item.get("path")
        digest = item.get("content_sha256")
        if locator is None and digest is None:
            continue
        if not isinstance(locator, str) or not locator:
            errors.append(f"evidence_index[{index}].path가 비었다")
            continue
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            errors.append(f"evidence_index[{index}].content_sha256 형식이 잘못됐다")
            continue
        path = (ROOT / locator).resolve()
        try:
            path.relative_to(ROOT.resolve())
        except ValueError:
            errors.append(f"evidence_index[{index}].path가 저장소 밖을 가리킨다")
            continue
        if not path.is_file():
            errors.append(f"evidence_index[{index}] 파일이 없다: {locator}")
        elif content_hash(path) != digest:
            errors.append(f"evidence_index[{index}] 파일 지문이 달라졌다: {locator}")

    model_contract = data.get("model_contract") or {}
    for path_key, hash_key in (
        ("prompt_snapshot", "prompt_sha256"),
        ("decision_schema_snapshot", "decision_schema_sha256"),
    ):
        locator = model_contract.get(path_key)
        digest = model_contract.get(hash_key)
        if locator is None:
            continue
        path = (ROOT / str(locator)).resolve()
        try:
            path.relative_to(ROOT.resolve())
        except ValueError:
            errors.append(f"model_contract.{path_key}가 저장소 밖을 가리킨다")
            continue
        if not path.is_file() or content_hash(path) != digest:
            errors.append(f"model_contract.{path_key} 파일이 없거나 지문이 다르다")
    return errors


def assessment_id_for(data: dict[str, Any]) -> str:
    payload = {key: value for key, value in data.items() if key != "assessment_id"}
    return stable_hash(payload, "CIO-")


def normalize_decision(data: dict[str, Any]) -> dict[str, Any]:
    normalized = json.loads(json.dumps(data, ensure_ascii=False, allow_nan=False))
    normalized["assessment_id"] = assessment_id_for(normalized)
    return normalized


def validate_decision(
    data: dict[str, Any],
    *,
    context: dict[str, Any] | None = None,
) -> list[str]:
    errors: list[str] = []
    unknown = sorted(set(data) - TOP_LEVEL_KEYS)
    if unknown:
        errors.append(f"알 수 없는 최상위 필드: {', '.join(unknown)}")
    if data.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version: {SCHEMA_VERSION} 이어야 한다")
    context_id = _nonempty(data.get("context_id"), "context_id", errors)
    asset_id = _nonempty(data.get("asset_id"), "asset_id", errors)
    if asset_id and not ASSET_ID_RE.fullmatch(asset_id):
        errors.append("asset_id: 안전한 자산 ID 형식이 아니다")
    created_at = _datetime(data.get("created_at"), "created_at", errors)
    verdict = data.get("verdict")
    if verdict not in VERDICTS:
        errors.append(f"verdict: {', '.join(sorted(VERDICTS))} 중 하나여야 한다")
    insufficient = verdict == "INSUFFICIENT"
    if data.get("outcome_metric") != OUTCOME_METRIC:
        errors.append(f"outcome_metric: {OUTCOME_METRIC} 이어야 한다")

    assessment_id = data.get("assessment_id")
    if assessment_id is not None:
        if not isinstance(assessment_id, str) or not ASSESSMENT_ID_RE.fullmatch(
            assessment_id
        ):
            errors.append("assessment_id: CIO-<20자리 16진수> 형식이어야 한다")
        else:
            try:
                expected_id = assessment_id_for(data)
            except (TypeError, ValueError):
                errors.append("assessment_id: 출력을 표준 JSON으로 지문화할 수 없다")
            else:
                if assessment_id != expected_id:
                    errors.append(f"assessment_id: 내용 지문은 {expected_id}이다")

    model = data.get("model")
    if not isinstance(model, dict):
        errors.append("model: 객체가 필요하다")
    else:
        allowed = {"provider", "name", "version", "prompt_version"}
        if set(model) - allowed:
            errors.append("model: 알 수 없는 필드가 있다")
        for field in ("provider", "name", "prompt_version"):
            _nonempty(model.get(field), f"model.{field}", errors)
        if model.get("version") is not None:
            _nonempty(model.get("version"), "model.version", errors)

    horizon = data.get("horizon_months")
    horizon_number = _number(
        horizon, "horizon_months", errors, minimum=1, maximum=120
    )
    if horizon_number is not None and not horizon_number.is_integer():
        errors.append("horizon_months: 정수여야 한다")
    _nonempty(data.get("win_definition"), "win_definition", errors)
    _nonempty(data.get("failure_definition"), "failure_definition", errors)
    win_threshold = _number(
        data.get("win_return_threshold_pct"),
        "win_return_threshold_pct",
        errors,
        minimum=-100,
    )
    failure_threshold = _number(
        data.get("failure_return_threshold_pct"),
        "failure_return_threshold_pct",
        errors,
        minimum=-100,
    )
    if (
        win_threshold is not None
        and failure_threshold is not None
        and win_threshold <= failure_threshold
    ):
        errors.append(
            "win_return_threshold_pct는 failure_return_threshold_pct보다 커야 한다"
        )
    _nonempty(data.get("thesis"), "thesis", errors)
    _number(
        data.get("win_probability_pct"),
        "win_probability_pct",
        errors,
        minimum=0,
        maximum=100,
        allow_null=insufficient,
    )
    _number(
        data.get("confidence_pct"),
        "confidence_pct",
        errors,
        minimum=0,
        maximum=100,
        allow_null=insufficient,
    )

    expected = data.get("expected_return_pct")
    expected_values: dict[str, float | None] = {}
    if not isinstance(expected, dict) or set(expected) != EXPECTED_KEYS:
        errors.append("expected_return_pct: bear·base·bull·probability_weighted가 필요하다")
    else:
        for key in EXPECTED_KEYS:
            expected_values[key] = _number(
                expected.get(key),
                f"expected_return_pct.{key}",
                errors,
                minimum=-100,
                allow_null=insufficient,
            )

    probabilities = data.get("scenario_probability_pct")
    probability_values: dict[str, float | None] = {}
    if not isinstance(probabilities, dict) or set(probabilities) != PROBABILITY_KEYS:
        errors.append("scenario_probability_pct: bear·base·bull이 필요하다")
    else:
        for key in PROBABILITY_KEYS:
            probability_values[key] = _number(
                probabilities.get(key),
                f"scenario_probability_pct.{key}",
                errors,
                minimum=0.000001,
                maximum=100,
                allow_null=insufficient,
            )
    complete_probabilities = all(
        probability_values.get(key) is not None for key in PROBABILITY_KEYS
    )
    if complete_probabilities:
        total = sum(float(probability_values[key]) for key in PROBABILITY_KEYS)
        if abs(total - 100) > 0.01:
            errors.append(f"scenario_probability_pct: 합은 100이어야 한다 ({total})")
        if all(expected_values.get(key) is not None for key in PROBABILITY_KEYS):
            weighted = sum(
                float(expected_values[key]) * float(probability_values[key]) / 100
                for key in PROBABILITY_KEYS
            )
            stated = expected_values.get("probability_weighted")
            if stated is not None and abs(weighted - stated) > 0.1:
                errors.append(
                    "expected_return_pct.probability_weighted: "
                    f"시나리오 가중값 {weighted:.2f}와 다르다"
                )
    ordered = [expected_values.get(key) for key in ("bear", "base", "bull")]
    if all(value is not None for value in ordered) and ordered != sorted(ordered):
        errors.append("expected_return_pct: bear <= base <= bull이어야 한다")

    price_plan = data.get("price_plan")
    price_values: dict[str, float | None] = {}
    if not isinstance(price_plan, dict) or set(price_plan) != PRICE_KEYS:
        errors.append("price_plan: 계약 필드가 정확히 필요하다")
    else:
        if price_plan.get("currency") not in {"KRW", "USD"}:
            errors.append("price_plan.currency: KRW 또는 USD여야 한다")
        for key in PRICE_KEYS - {"currency", "reask_events"}:
            price_values[key] = _number(
                price_plan.get(key),
                f"price_plan.{key}",
                errors,
                minimum=0.0000001,
                allow_null=insufficient,
            )
        _string_list(
            price_plan.get("reask_events"),
            "price_plan.reask_events",
            errors,
        )
    low = price_values.get("entry_range_low")
    high = price_values.get("entry_range_high")
    preferred = price_values.get("preferred_entry")
    stop = price_values.get("stop_price")
    scale_out = price_values.get("scale_out_start")
    if low is not None and high is not None and low > high:
        errors.append("price_plan: entry_range_low는 entry_range_high 이하여야 한다")
    if (
        low is not None
        and high is not None
        and preferred is not None
        and not low <= preferred <= high
    ):
        errors.append("price_plan.preferred_entry: 진입 범위 안이어야 한다")
    if stop is not None and low is not None and stop >= low:
        errors.append("price_plan.stop_price: 진입 범위 하단보다 낮아야 한다")
    if scale_out is not None and high is not None and scale_out <= high:
        errors.append("price_plan.scale_out_start: 진입 범위 상단보다 높아야 한다")

    if insufficient:
        numeric_fields = {
            "win_probability_pct": data.get("win_probability_pct"),
            "confidence_pct": data.get("confidence_pct"),
            **{
                f"expected_return_pct.{key}": expected_values.get(key)
                for key in EXPECTED_KEYS
            },
            **{
                f"scenario_probability_pct.{key}": probability_values.get(key)
                for key in PROBABILITY_KEYS
            },
            **{
                f"price_plan.{key}": price_values.get(key)
                for key in PRICE_KEYS - {"currency", "reask_events"}
            },
        }
        for field, value in numeric_fields.items():
            if value is not None:
                errors.append(f"{field}: INSUFFICIENT에서는 null이어야 한다")

    trade = data.get("proposed_trade")
    proposed_amount = None
    if not isinstance(trade, dict) or set(trade) != {"amount", "currency", "rationale"}:
        errors.append("proposed_trade: amount·currency·rationale가 필요하다")
    else:
        proposed_amount = _number(
            trade.get("amount"),
            "proposed_trade.amount",
            errors,
            minimum=0,
            allow_null=insufficient,
        )
        if trade.get("currency") not in {"KRW", "USD"}:
            errors.append("proposed_trade.currency: KRW 또는 USD여야 한다")
        _nonempty(trade.get("rationale"), "proposed_trade.rationale", errors)
    if insufficient and proposed_amount is not None:
        errors.append("proposed_trade.amount: INSUFFICIENT에서는 null이어야 한다")
    if verdict == "BUY_NOW" and proposed_amount is not None and proposed_amount <= 0:
        errors.append("proposed_trade.amount: BUY_NOW에서는 0보다 커야 한다")
    if (
        verdict in {"WAIT", "AVOID"}
        and proposed_amount is not None
        and proposed_amount != 0
    ):
        errors.append(f"proposed_trade.amount: {verdict}의 현재 집행액은 0이어야 한다")

    action = data.get("action")
    if (
        not isinstance(action, dict)
        or (set(action) - ACTION_KEYS)
        or not ACTION_REQUIRED.issubset(action)
    ):
        errors.append("action: direction·quantity·sell_reason·rationale가 필요하다")
    else:
        direction = action.get("direction")
        if direction not in ACTION_DIRECTIONS:
            errors.append(
                f"action.direction: {', '.join(sorted(ACTION_DIRECTIONS))} "
                "중 하나여야 한다"
            )
        undetermined = direction == "UNDETERMINED"
        blocked_by = action.get("undetermined_reason")
        if blocked_by is not None and blocked_by not in UNDETERMINED_REASONS:
            errors.append(
                f"action.undetermined_reason: {', '.join(sorted(UNDETERMINED_REASONS))} "
                "중 하나이거나 null이어야 한다"
            )
        elif undetermined and blocked_by is None:
            errors.append(
                "action.undetermined_reason: 무엇이 없어서 행동을 정할 수 없는지 "
                "밝혀야 한다"
            )
        elif not undetermined and blocked_by is not None:
            errors.append(
                "action.undetermined_reason: UNDETERMINED가 아니면 null이어야 한다"
            )
        # verdict INSUFFICIENT 와 undetermined_reason INSUFFICIENT_EVIDENCE 는
        # 같은 사실의 두 면이다. 하나만 쓰면 "데이터가 없어서 막혔다"와
        # "한도가 없어서 막혔다"가 한 통에 섞여 집계가 읽히지 않는다.
        if blocked_by == "INSUFFICIENT_EVIDENCE" and not insufficient:
            errors.append(
                "action.undetermined_reason: INSUFFICIENT_EVIDENCE는 종목 판단 "
                "자체가 서지 않는 경우이므로 verdict도 INSUFFICIENT여야 한다. "
                "한도가 없어 수량을 못 정하는 것이면 NO_INVESTOR_POLICY다"
            )
        if insufficient and undetermined and blocked_by not in {
            None, "INSUFFICIENT_EVIDENCE",
        }:
            errors.append(
                "action.undetermined_reason: INSUFFICIENT에서는 "
                "INSUFFICIENT_EVIDENCE여야 한다"
            )
        reason = action.get("sell_reason")
        selling = direction in {"TRIM", "EXIT"}
        if reason is not None and reason not in SELL_REASONS:
            errors.append(
                f"action.sell_reason: {', '.join(sorted(SELL_REASONS))} "
                "중 하나이거나 null이어야 한다"
            )
        elif selling and reason is None:
            errors.append("action.sell_reason: 매도에는 사유가 필요하다")
        elif not selling and reason is not None:
            errors.append("action.sell_reason: 매도가 아니면 null이어야 한다")
        quantity = _number(
            action.get("quantity"),
            "action.quantity",
            errors,
            allow_null=not selling,
        )
        if selling and quantity is not None and quantity <= 0:
            errors.append("action.quantity: 매도 수량은 0보다 커야 한다")
        if not selling and quantity is not None:
            errors.append("action.quantity: 매도가 아니면 null이어야 한다")
        _nonempty(action.get("rationale"), "action.rationale", errors)
        # 예전 계약은 여기서 HOLD 를 강제했다. 그러면 시스템이 모델의 "모른다"를
        # "계속 보유하라"로 번역한다 — 보유 중인 종목에서 그것은 빈 값이 아니라
        # 행동 지시다. 판단이 서지 않으면 지시하지 않는 자리가 있어야 한다.
        if insufficient and direction not in {None, "UNDETERMINED"}:
            errors.append(
                "action.direction: INSUFFICIENT에서는 UNDETERMINED여야 한다. "
                "판단이 서지 않는 것을 HOLD로 적으면 보유 지시가 된다"
            )
        # 판단 기반 매도는 자산 자체가 나쁘다는 주장이므로 verdict와 같은 편에
        # 서야 한다. "이 가격에 사겠다"와 "비싸서 판다"를 동시에 말할 수 없다.
        # 제약 기반 매도는 자산이 좋아도 낼 수 있으나, 제약이라고 밝혀야 한다.
        if reason in JUDGMENT_SELL_REASONS and verdict == "BUY_NOW":
            errors.append(
                f"action.sell_reason: {reason}은 자산 판단이라 "
                "BUY_NOW와 함께 쓸 수 없다. 제약 때문이면 "
                f"{', '.join(sorted(CONSTRAINT_SELL_REASONS))} 중에서 고른다"
            )
        if direction == "ADD" and proposed_amount is not None and proposed_amount <= 0:
            errors.append("action.direction: ADD면 제안 금액이 0보다 커야 한다")
        if (
            direction in {"HOLD", "TRIM", "EXIT", "UNDETERMINED"}
            and proposed_amount is not None
            and proposed_amount > 0
        ):
            errors.append(
                f"action.direction: {direction}인데 새로 사겠다는 금액이 있다"
            )

    _validate_drivers(data.get("drivers"), errors)
    _validate_recurring_plan(data.get("recurring_plan"), errors)
    _validate_positioning(data.get("positioning"), errors)
    if context is None and data.get("user_assertion_checks") is not None:
        # context 없는 읽기전용 검증에서는 형식만 본다. 무엇을 닫아야 하는지는
        # packet 이 알려주므로 아래 context 블록에서 다시 검사한다.
        _validate_assertion_checks(
            data.get("user_assertion_checks"), errors, expected_ids=set()
        )

    method = data.get("method")
    if not isinstance(method, dict) or set(method) != {
        "valuation_family",
        "why_fit",
        "assumptions",
    }:
        errors.append("method: valuation_family·why_fit·assumptions가 필요하다")
    else:
        _nonempty(method.get("valuation_family"), "method.valuation_family", errors)
        _nonempty(method.get("why_fit"), "method.why_fit", errors)
        _string_list(method.get("assumptions"), "method.assumptions", errors)

    counter = data.get("strongest_counter_thesis")
    counter_refs: list[str] = []
    if not isinstance(counter, dict) or set(counter) != {"text", "evidence_refs"}:
        errors.append("strongest_counter_thesis: text·evidence_refs가 필요하다")
    else:
        _nonempty(counter.get("text"), "strongest_counter_thesis.text", errors)
        counter_refs = _string_list(
            counter.get("evidence_refs"),
            "strongest_counter_thesis.evidence_refs",
            errors,
            minimum=1,
        )
    _string_list(
        data.get("invalidation_signals"),
        "invalidation_signals",
        errors,
        minimum=1,
    )
    _string_list(data.get("unknowns"), "unknowns", errors)
    _string_list(data.get("research_requests"), "research_requests", errors)

    sources = data.get("sources")
    model_source_ids: set[str] = set()
    source_times: list[
        tuple[str, dt.datetime | None, dt.datetime | None]
    ] = []
    if not isinstance(sources, list):
        errors.append("sources: 배열이 필요하다")
    else:
        for index, source in enumerate(sources):
            field = f"sources[{index}]"
            if not isinstance(source, dict) or set(source) != {
                "source_id",
                "type",
                "locator",
                "published_at",
                "retrieved_at",
            }:
                errors.append(f"{field}: source 계약 필드가 정확히 필요하다")
                continue
            source_id = _nonempty(source.get("source_id"), f"{field}.source_id", errors)
            if source_id:
                if source_id in model_source_ids:
                    errors.append(f"{field}.source_id: 중복 {source_id}")
                model_source_ids.add(source_id)
            _nonempty(source.get("type"), f"{field}.type", errors)
            locator = _nonempty(
                source.get("locator"), f"{field}.locator", errors
            )
            if locator:
                parsed_locator = urllib.parse.urlparse(locator)
                if (
                    parsed_locator.scheme not in {"http", "https"}
                    or not parsed_locator.netloc
                ):
                    errors.append(
                        f"{field}.locator: http(s) 원문 URL이어야 한다"
                    )
            retrieved_at = _datetime(
                source.get("retrieved_at"), f"{field}.retrieved_at", errors
            )
            published_at = None
            if source.get("published_at") is not None:
                published_at = _datetime(
                    source.get("published_at"), f"{field}.published_at", errors
                )
            if (
                published_at is not None
                and retrieved_at is not None
                and published_at > retrieved_at
            ):
                errors.append(f"{field}: published_at은 retrieved_at보다 늦을 수 없다")
            source_times.append((field, published_at, retrieved_at))

    evidence_refs = _string_list(
        data.get("evidence_refs"), "evidence_refs", errors, minimum=1
    )
    if len(evidence_refs) != len(set(evidence_refs)):
        errors.append("evidence_refs: 중복 ID가 있다")
    if len(counter_refs) != len(set(counter_refs)):
        errors.append("strongest_counter_thesis.evidence_refs: 중복 ID가 있다")

    if context is None and context_id:
        try:
            context, _ = find_context(context_id)
        except ContractError as exc:
            errors.extend(exc.errors)
    if context:
        context_errors = validate_context_integrity(context)
        errors.extend(f"context: {error}" for error in context_errors)
        request = context.get("request") or {}
        if context.get("context_id") != context_id:
            errors.append("context_id: 컨텍스트와 결정이 다르다")
        if request.get("asset_id") != asset_id:
            errors.append("asset_id: 컨텍스트와 결정이 다르다")
        context_created = _datetime(
            context.get("created_at"), "context.created_at", errors
        )
        if created_at and context_created and created_at < context_created:
            errors.append("created_at: 컨텍스트보다 앞설 수 없다")
        if created_at and created_at > dt.datetime.now(dt.timezone.utc) + dt.timedelta(
            minutes=5
        ):
            errors.append("created_at: 현재보다 5분 이상 미래일 수 없다")
        for field, _published_at, retrieved_at in source_times:
            if created_at and retrieved_at and retrieved_at > created_at:
                errors.append(f"{field}.retrieved_at: decision created_at보다 늦을 수 없다")
        # 받은 데이터를 다루지 않고 넘어가는 것을 허용하지 않는다. 이익 동인이
        # 아니라는 이유로 수급이 조용히 빠지는 실패가 실제로 있었다.
        packet_body = context.get("context") or {}
        flow = (packet_body.get("structured_optional") or {}).get("kr_flow")
        if flow and data.get("positioning") is None:
            errors.append(
                "positioning: packet에 수급 데이터(structured_optional.kr_flow)가 "
                "실려 있다. 이익 동인이 아니라는 이유로 건너뛰지 말고 확인했는지 "
                "밝혀야 한다"
            )
        _validate_assertion_checks(
            data.get("user_assertion_checks"),
            errors,
            expected_ids={
                str(row.get("claim_id"))
                for row in (request.get("user_assertions") or [])
                if isinstance(row, dict) and row.get("claim_id")
            },
        )
        validation_status = (context.get("validation") or {}).get("status")
        if validation_status not in {"PASS", "BLOCKED"}:
            errors.append("context.validation.status: PASS 또는 BLOCKED여야 한다")
        elif validation_status != "PASS" and not insufficient:
            errors.append("BLOCKED context에서는 INSUFFICIENT만 기록할 수 있다")
        expected_prompt_version = (context.get("model_contract") or {}).get(
            "prompt_version"
        )
        if (
            expected_prompt_version
            and isinstance(model, dict)
            and model.get("prompt_version") != expected_prompt_version
        ):
            errors.append("model.prompt_version: context의 prompt version과 다르다")
        requested_currency = request.get("price_currency")
        if isinstance(price_plan, dict) and price_plan.get("currency") != requested_currency:
            errors.append("price_plan.currency: 입력 가격 통화와 달라선 안 된다")
        requested_price = request.get("price")
        if isinstance(requested_price, (int, float)):
            below = price_values.get("reask_below")
            above = price_values.get("reask_above")
            if below is not None and below >= requested_price:
                errors.append("price_plan.reask_below: 입력 가격보다 낮아야 한다")
            if above is not None and above <= requested_price:
                errors.append("price_plan.reask_above: 입력 가격보다 높아야 한다")
        if isinstance(trade, dict):
            if trade.get("currency") != request.get("amount_currency"):
                errors.append("proposed_trade.currency: 입력 금액 통화와 달라선 안 된다")
            requested_amount = request.get("amount")
            proposed = trade.get("amount")
            if (
                isinstance(requested_amount, (int, float))
                and isinstance(proposed, (int, float))
                and proposed > requested_amount + 1e-9
            ):
                errors.append("proposed_trade.amount: 사용자가 제시한 금액을 넘을 수 없다")
        local_source_ids = {
            item.get("source_id")
            for item in context.get("evidence_index") or []
            if isinstance(item, dict) and item.get("source_id")
        }
        collisions = local_source_ids & model_source_ids
        if collisions:
            errors.append(
                "model source_id가 local evidence와 충돌한다: "
                + ", ".join(sorted(collisions))
            )
        known_refs = local_source_ids | model_source_ids
        for ref in evidence_refs + counter_refs:
            if ref not in known_refs:
                errors.append(f"evidence ref가 source index에 없다: {ref}")
    return errors


def require_valid_decision(
    data: dict[str, Any],
    *,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    errors = validate_decision(data, context=context)
    if errors:
        raise ContractError(errors)
    return normalize_decision(data)
