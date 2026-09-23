"""Validate optional future sell stages without assuming that any order filled."""

from __future__ import annotations

import datetime as dt
import math

from calculators.assumptions import ID, keys, number, rows, text
from calculators.contracts import content_hash, load_json
from cio.paths import ROOT

CHECK_STATES = {"SATISFIED", "NOT_SATISFIED", "UNVERIFIABLE", "CANCELLED"}


def _day(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label}: YYYY-MM-DD 날짜가 필요하다")
    try:
        parsed = dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label}: YYYY-MM-DD 날짜가 필요하다") from exc
    if value != parsed.isoformat():
        raise ValueError(f"{label}: YYYY-MM-DD 형식이어야 한다")
    return value


def _held_quantity(context: dict) -> float:
    position = ((context.get("context") or {}).get("portfolio") or {}).get("existing_position") or {}
    if position.get("held") is not True:
        raise ValueError("보유 중인 수량이 packet에 있어야 미래 매도 계획을 검산할 수 있다")
    return number(position.get("quantity"), "보유 수량", positive=True)


def plan_summary(plan: dict, decision: dict, context: dict) -> dict:
    """Check share arithmetic and return a displayable plan, never execution state."""
    keys(plan, {"currency", "reference_date", "reference_basis", "stages"}, label="exit_plan")
    text(plan["reference_basis"], "reference_basis")
    request = context["request"]
    if plan["currency"] != request.get("price_currency") or plan["currency"] not in {"KRW", "USD"}:
        raise ValueError("미래 매도 지정가의 통화가 질문 자산의 가격 통화와 다르다")
    try:
        request_date = dt.datetime.fromisoformat(request["as_of"].replace("Z", "+00:00")).date().isoformat()
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ValueError("질문 기준시각이 올바른 ISO 날짜·시각이 아니다") from exc
    if _day(plan["reference_date"], "reference_date") != request_date:
        raise ValueError("미래 매도 주식 기준일은 이번 질문의 기준일이어야 한다")
    held = _held_quantity(context)
    action = decision.get("action") or {}
    direction = action.get("direction")
    if direction not in {"HOLD", "TRIM"} or decision.get("verdict") == "INSUFFICIENT":
        raise ValueError("보유 또는 일부 축소 판단에서만 미래 매도 계획을 낼 수 있다")
    if direction == "HOLD" and action.get("quantity") is not None:
        raise ValueError("HOLD에는 현재 매도 수량이 없어야 한다")
    current = number(action.get("quantity"), "현재 매도 수량", positive=True) if direction == "TRIM" else 0

    seen: set[str] = set()
    earliest_by_stage: dict[str, str] = {}
    future_quantities: list[float] = []
    stages = rows(plan["stages"], "stages", maximum=8)
    for index, stage in enumerate(stages):
        keys(stage, {"stage_id", "name", "quantity", "limit_price", "requires_review", "depends_on",
                     "condition", "cancel_if", "valid_from", "valid_until"}, label="exit_plan.stage")
        identifier = stage["stage_id"]
        if not isinstance(identifier, str) or not ID.fullmatch(identifier) or identifier in seen:
            raise ValueError("미래 매도 단계 ID가 잘못됐거나 중복됐다")
        for field in ("name", "condition", "cancel_if"):
            text(stage[field], field)
        future_quantities.append(number(stage["quantity"], "미래 매도 수량", positive=True))
        number(stage["limit_price"], "미래 매도 지정가", positive=True)
        if not isinstance(stage["requires_review"], bool):
            raise ValueError("requires_review는 true/false여야 한다")
        dependency = stage["depends_on"]
        if dependency is None:
            if direction != "HOLD" or index != 0:
                raise ValueError("독립 단계는 즉시 매도가 없는 HOLD 계획의 첫 단계에만 허용한다")
        elif dependency == "CURRENT_ACTION":
            if direction != "TRIM":
                raise ValueError("CURRENT_ACTION 의존성에는 현재 TRIM 매도가 필요하다")
        elif not isinstance(dependency, str) or dependency not in seen:
            raise ValueError("의존 단계는 앞에 선언한 단계 ID여야 한다")
        start = _day(stage["valid_from"], "valid_from")
        end = _day(stage["valid_until"], "valid_until")
        if start < plan["reference_date"] or end < start:
            raise ValueError("미래 매도 단계의 유효기간이 기준일보다 이르거나 뒤집혔다")
        earliest = max(start, earliest_by_stage[dependency]) if dependency in earliest_by_stage else start
        if end < earliest:
            raise ValueError("의존 단계가 가능해지기 전에 미래 매도 단계의 유효기간이 끝난다")
        earliest_by_stage[identifier] = earliest
        seen.add(identifier)

    future = math.fsum(future_quantities)
    if math.fsum((current, future)) > held + 1e-8:
        raise ValueError("현재 매도와 미래 단계의 총수량이 packet 보유수량을 넘는다")
    return {"currency": plan["currency"], "held_quantity": held,
            "current_sale_quantity": current, "future_sale_quantity": future,
            "remaining_after_all": max(0.0, held - current - future), "stages": stages,
            "actual_execution": "UNKNOWN"}


def carried_plans(context: dict) -> dict[tuple[str, str], dict]:
    """Resolve only plans supplied by the current packet, checking references."""
    prior = ((context.get("context") or {}).get("prior_assessments") or {})
    records = prior.get("live", []) if isinstance(prior, dict) else prior
    if isinstance(prior, dict) and prior.get("latest_exit_plan"):
        records = [*(records or []), prior["latest_exit_plan"]]
    result: dict[tuple[str, str], dict] = {}
    for record in records or []:
        carried = record.get("carried_forward") or {}
        plan = carried.get("exit_plan")
        ref = carried.get("exit_plan_ref") or record.get("exit_plan_ref")
        if plan is None and ref:
            path = (ROOT / ref["path"]).resolve()
            if not path.is_relative_to(ROOT.resolve()) or content_hash(path) != ref["content_sha256"]:
                raise ValueError("이어받은 미래 매도 계획 파일의 경로/지문이 다르다")
            assessment = load_json(path)
            from cio.decision import assessment_id_for
            if (assessment.get("assessment_id") != record["assessment_id"]
                    or assessment["assessment_id"] != assessment_id_for(assessment)
                    or assessment.get("asset_id") != context["request"]["asset_id"]):
                raise ValueError("이어받은 미래 매도 계획이 과거 판단과 다른 기록이다")
            plan = assessment.get("exit_plan")
        if plan:
            for stage in plan.get("stages", []):
                result[(record["assessment_id"], stage["stage_id"])] = stage
    return result


def validate(decision: dict, context: dict) -> list[str]:
    errors: list[str] = []
    try:
        if decision.get("exit_plan") is not None:
            plan_summary(decision["exit_plan"], decision, context)
        previous = carried_plans(context)
        prior = ((context.get("context") or {}).get("prior_assessments") or {})
        historical = (prior.get("latest_exit_plan") or {}) if isinstance(prior, dict) else {}
        cancelled = {(historical.get("assessment_id"), row.get("stage_id"))
                     for row in (historical.get("stage_reviews") or [])
                     if isinstance(row, dict) and row.get("state") == "CANCELLED"}
        seen: set[tuple[str, str]] = set()
        valid_refs = {s["source_id"] for s in (context.get("evidence_index") or []) if isinstance(s, dict) and s.get("source_id")}
        valid_refs |= {s["source_id"] for s in (decision.get("sources") or []) if isinstance(s, dict) and s.get("source_id")}
        for check in rows(decision.get("exit_plan_checks", []), "exit_plan_checks", minimum=0):
            keys(check, {"assessment_id", "stage_id", "condition", "state", "note", "evidence_refs"}, label="exit_plan_check")
            pair = (check["assessment_id"], check["stage_id"])
            if pair not in previous or pair in seen or check["condition"] != previous[pair]["condition"]:
                raise ValueError("존재하지 않거나 중복·변경된 과거 매도 단계의 조건을 확인했다")
            if pair in cancelled:
                raise ValueError("이미 취소한 매도 단계는 다시 확인해 되살릴 수 없다. 새 exit_plan이 필요하다")
            seen.add(pair)
            if check["state"] not in CHECK_STATES:
                raise ValueError("미래 매도 단계 조건 상태가 잘못됐다")
            text(check["note"], "exit_plan_check.note")
            refs = rows(check["evidence_refs"], "exit_plan_check.evidence_refs", minimum=0)
            if any(not isinstance(ref, str) or ref not in valid_refs for ref in refs) or (check["state"] != "UNVERIFIABLE" and not refs):
                raise ValueError("미래 매도 단계 조건 확인에는 등록된 근거가 필요하다")
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, OSError) as exc:
        errors.append(f"exit_plan: {exc}")
    return errors
