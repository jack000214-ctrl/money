"""Small advisory-plan contract and retrospective observations. No order state."""
import datetime as dt
import math

from calculators.assumptions import keys, number, rows, text, ID
from calculators.staged_entry import calculate
from calculators.contracts import content_hash, load_json
from cio.paths import ROOT

STATES = {"SATISFIED", "NOT_SATISFIED", "UNVERIFIABLE", "CANCELLED"}


def day(value):
    if not isinstance(value, str):
        raise ValueError("계획 날짜가 필요하다")
    parsed = dt.date.fromisoformat(value)
    if value != parsed.isoformat():
        raise ValueError("계획 날짜는 YYYY-MM-DD 형식이다")
    return value


def plan_calculation(plan, decision, context):
    keys(plan, {"currency", "reference_basis", "reference_date", "stages"}, label="entry_plan")
    text(plan["reference_basis"], "reference_basis")
    request = context["request"]
    if plan["currency"] != request.get("amount_currency") or plan["currency"] != request.get("price_currency"):
        raise ValueError("분할계획은 같은 통화의 예산·가격을 사용해야 한다. 먼저 날짜가 있는 FX 환산이 필요하다")
    if day(plan["reference_date"]) != request["as_of"][:10]:
        raise ValueError("계획의 주식 기준일은 이 질문의 가격 기준일이어야 한다")
    budget = number(request.get("amount"), "사용자 예산", positive=True)
    current = number((decision.get("proposed_trade") or {}).get("amount"), "현재 제안 금액", low=0)
    if current >= budget:
        raise ValueError("현재 제안액을 제외하면 미래 단계에 배분할 예산이 없다")
    capital = (context.get("context") or {}).get("capital_changes") or {}
    identifiers = set()
    for stage in rows(plan["stages"], "stages", maximum=8):
        keys(stage, {"stage_id", "name", "budget", "reference_price", "share_multiplier", "requires_review", "condition", "cancel_if", "valid_from", "valid_until"}, {"capital_source_id"}, "stage")
        identifier = stage["stage_id"]
        if not isinstance(identifier, str) or not ID.fullmatch(identifier) or identifier in identifiers:
            raise ValueError("단계 ID가 잘못됐거나 중복됐다")
        identifiers.add(identifier)
        for field in ("name", "condition", "cancel_if"):
            text(stage[field], field)
        for field in ("budget", "reference_price", "share_multiplier"):
            number(stage[field], field, positive=True)
        if not isinstance(stage["requires_review"], bool):
            raise ValueError("requires_review는 true/false다")
        if day(stage["valid_from"]) < plan["reference_date"] or day(stage["valid_until"]) < stage["valid_from"]:
            raise ValueError("단계의 유효기간이 가격 기준일보다 이르거나 뒤집혔다")
        if stage["share_multiplier"] != 1:
            matches = [e for e in capital.get("events", []) if e.get("kind") == "BONUS" and e.get("share_multiplier") == stage["share_multiplier"]]
            if capital.get("state") != "ANNOUNCED" or stage.get("capital_source_id") != capital.get("source_id") or not matches:
                raise ValueError("주식단위 환산배수가 packet의 증자 원문과 연결되지 않았다")
            if not stage["requires_review"]:
                raise ValueError("예정 무상증자 가격은 실제 권리 반영 확인이 필요하다")
    return calculate({**plan, "budget": budget - current})


def carried_plans(context):
    prior = (context.get("context") or {}).get("prior_assessments") or {}
    records = prior.get("live", []) if isinstance(prior, dict) else prior
    if isinstance(prior, dict) and prior.get("latest_entry_plan"):
        records = [*records, prior["latest_entry_plan"]]
    result = {}
    for record in records or []:
        carried = record.get("carried_forward") or {}
        plan = carried.get("entry_plan")
        ref = carried.get("entry_plan_ref") or record.get("entry_plan_ref")
        if plan is None and ref:
            path = (ROOT / ref["path"]).resolve()
            if not path.is_relative_to(ROOT.resolve()) or content_hash(path) != ref["content_sha256"]:
                raise ValueError("이어받은 분할계획 파일의 경로/지문이 다르다")
            prior = load_json(path)
            from cio.decision import assessment_id_for
            if prior.get("assessment_id") != record["assessment_id"] or prior["assessment_id"] != assessment_id_for(prior) or prior.get("asset_id") != context["request"]["asset_id"]:
                raise ValueError("이어받은 분할계획이 과거 판단과 다른 기록이다")
            plan = prior.get("entry_plan")
        if plan:
            for stage in plan.get("stages", []):
                result[(record["assessment_id"], stage["stage_id"])] = stage
    return result


def validate(decision, context):
    errors = []
    try:
        if decision.get("entry_plan") is not None:
            if decision.get("verdict") == "INSUFFICIENT":
                raise ValueError("근거 부족 판단에 숫자 분할계획을 붙일 수 없다")
            plan_calculation(decision["entry_plan"], decision, context)
        previous = carried_plans(context)
        seen = set()
        valid_refs = {s["source_id"] for s in context.get("evidence_index", [])} | {s["source_id"] for s in decision.get("sources", [])}
        for check in rows(decision.get("entry_plan_checks", []), "entry_plan_checks", minimum=0):
            keys(check, {"assessment_id", "stage_id", "state", "condition", "note", "evidence_refs"}, label="entry_plan_check")
            pair = (check["assessment_id"], check["stage_id"])
            if pair not in previous or pair in seen or check["condition"] != previous[pair]["condition"]:
                raise ValueError("존재하지 않거나 중복·변경된 과거 단계의 조건을 확인했다")
            seen.add(pair)
            if check["state"] not in STATES:
                raise ValueError("단계 조건 상태가 잘못됐다")
            text(check["note"], "check.note")
            refs = rows(check["evidence_refs"], "check.evidence_refs", minimum=0)
            if any(ref not in valid_refs for ref in refs) or (check["state"] != "UNVERIFIABLE" and not refs):
                raise ValueError("조건 확인에는 등록된 근거가 필요하다")
    except (ValueError, TypeError, KeyError, OverflowError, OSError) as exc:
        errors.append(f"entry_plan: {exc}")
    return errors


def _dividend_factor(bar):
    """그 봉 이후의 배당분(조정종가/종가). 종가가 없으면 배당 정보가 없는 것이라 1 이다."""
    close, adjusted = bar.get("close"), bar.get("adjusted_close")
    if not close or not adjusted:
        return 1.0
    return float(adjusted) / float(close)


def observations(assessment, price_rows, *, start_factor, end, reviews=(), decision_date=None):
    """Record touches only after the model's confirmation was recorded.

    A daily bar cannot identify a same-day before/after event, so a review becomes
    usable the next day. Never translate a missing review into a successful entry.
    """
    plan = assessment["entry_plan"]
    decision_date = decision_date or assessment.get("created_at", plan["reference_date"])[:10]
    result = []
    for stage in plan["stages"]:
        checks = sorted([r for r in reviews if r["assessment_id"] == assessment["assessment_id"] and r["stage_id"] == stage["stage_id"]], key=lambda r: r["recorded_at"])
        touch, eligible, unknown, eligible_bar = None, None, False, None
        for bar in price_rows:
            if not (max(plan["reference_date"], decision_date) < bar["date"] and stage["valid_from"] <= bar["date"] <= stage["valid_until"]):
                continue
            state = "SATISFIED" if not stage["requires_review"] else "UNVERIFIABLE"
            for check in checks:
                if check["recorded_at"][:10] < bar["date"]:
                    state = check["state"]
            if state == "CANCELLED":
                break
            ratio = float(bar.get("split_adjustment_factor") or 1) / start_factor
            matching_basis = math.isclose(ratio, stage["share_multiplier"], rel_tol=1e-8)
            if bar["low"] <= stage["reference_price"] * start_factor:
                touch = touch or bar["date"]
                if state == "SATISFIED" and matching_basis:
                    eligible, eligible_bar = bar["date"], bar
                    break
                unknown |= state == "UNVERIFIABLE" or not matching_basis
        result.append({"stage_id": stage["stage_id"], "price_touch_date": touch,
                       "eligible_price_touch_date": eligible,
                       "observation": "CONDITIONS_AND_PRICE_OBSERVED" if eligible else "UNVERIFIABLE" if unknown else "NO_ELIGIBLE_TOUCH",
                       # 배당은 접촉한 날부터 받는다. 판단일의 배당 조정계수를 쓰면 그 사이에
                       # 지급된 배당이 이 단계의 수익으로 들어간다 (`cio/memory.py:_counterfactual`).
                       "hypothetical_return_pct": round((end["adjusted_close"] / (
                           stage["reference_price"] * start_factor * _dividend_factor(eligible_bar)
                       ) - 1) * 100, 4) if eligible else None})
    return {"stages": result, "actual_execution": "UNKNOWN", "strategy_return_pct": None,
            "_note": "조건 충족 후 가격 접촉의 관측이다. 실제 체결과 전략 수익률을 인증하지 않으며, 미확인 조건은 체결로 가정하지 않는다. 기존 고정기간 평가는 별도다."}
