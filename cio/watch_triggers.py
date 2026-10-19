"""저장된 판단의 가격 문턱과 현재가를 **부등호로만** 대조한다.

이 모듈은 판정하지 않는다. "문턱 X에 현재가 Y가 닿았다"는 사실을 이벤트로 남길 뿐이며,
그 문턱이 매수·매도 신호인지는 모델이 다음 질문에서 정한다. 점수·등급·추천 필드는
이벤트에 없다.

방향 규칙 (`cio/memory.py` 의 사후 관측과 같은 부등호 방향)
  - 이하(down):  stop_price, reask_below
  - 이상(up):    scale_out_start, reask_above, exit_plan.stages[].limit_price
  - 범위 안:     entry_range_low ≤ 현재가 ≤ entry_range_high
  경계값과 같은 가격은 닿은 것으로 본다.

`exit_plan` 단계는 `valid_from ~ valid_until`(거래소 날짜) 안에서만 대조한다.

중복 제거: `(종목, 판단 ID, 문턱 이름)` 키가 `active` 에 있으면 이미 알린 것이다.
문턱 밖으로 나가면 키를 지우고, 다시 닿으면 새 이벤트를 만든다. 시세가 대조 불가
(`comparable = False`)이면 닿음도 벗어남도 판단하지 않고 상태를 그대로 둔다.

`invalidation_signals[].signal` 은 자유 문장이라 자동 대조하지 않는다. 가격 문턱
이벤트에 원문 그대로 실어 사용자와 모델이 읽게 한다.
"""
from __future__ import annotations

from typing import Any, Callable

PRICE_TRIGGER = "PRICE_TRIGGER"

DOWN = "down"
UP = "up"
INSIDE = "inside"

# 이름 -> (방향, 사람이 읽는 이름). 이름은 이벤트 중복 제거 키의 일부다.
PRICE_PLAN_THRESHOLDS: dict[str, tuple[str, str]] = {
    "stop_price": (DOWN, "손절가"),
    "reask_below": (DOWN, "재질문 하단"),
    "scale_out_start": (UP, "분할매도 시작가"),
    "reask_above": (UP, "재질문 상단"),
}
# 기계 판독 가능한 문턱 이름의 전체 집합. 이 밖의 이름은 이벤트에 나오지 않는다.
SCALE_OUT_NAMES = {"scale_out_start"}


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value == value and abs(value) != float("inf") else None


def thresholds_of(assessment: dict[str, Any], today: str) -> list[dict[str, Any]]:
    """판단 JSON 에서 숫자 문턱을 뽑는다. `today` 는 YYYY-MM-DD(거래소 날짜)."""
    found: list[dict[str, Any]] = []
    plan = assessment.get("price_plan") or {}
    for name, (direction, label) in PRICE_PLAN_THRESHOLDS.items():
        value = _num(plan.get(name))
        if value is not None and value > 0:
            found.append(
                {"name": name, "label": label, "direction": direction,
                 "value": value, "currency": plan.get("currency")}
            )
    low, high = _num(plan.get("entry_range_low")), _num(plan.get("entry_range_high"))
    if low is not None and high is not None and 0 < low <= high:
        found.append(
            {"name": "entry_range", "label": "진입 범위", "direction": INSIDE,
             "value": low, "value_high": high, "currency": plan.get("currency")}
        )
    exit_plan = assessment.get("exit_plan") or {}
    for stage in exit_plan.get("stages") or []:
        if not isinstance(stage, dict):
            continue
        limit = _num(stage.get("limit_price"))
        stage_id = stage.get("stage_id")
        valid_from, valid_until = stage.get("valid_from"), stage.get("valid_until")
        if limit is None or limit <= 0 or not isinstance(stage_id, str):
            continue
        if not (isinstance(valid_from, str) and isinstance(valid_until, str)):
            continue
        if not valid_from <= today <= valid_until:
            continue
        found.append(
            {"name": f"exit_stage:{stage_id}", "label": f"매도 단계 {stage_id}",
             "direction": UP, "value": limit, "currency": exit_plan.get("currency"),
             "stage": {
                 "stage_id": stage_id,
                 "name": stage.get("name"),
                 "quantity": stage.get("quantity"),
                 "condition": stage.get("condition"),
                 "requires_review": stage.get("requires_review"),
                 "valid_from": valid_from,
                 "valid_until": valid_until,
             }}
        )
    return found


def touched(threshold: dict[str, Any], price: float) -> bool:
    direction = threshold["direction"]
    if direction == DOWN:
        return price <= threshold["value"]
    if direction == UP:
        return price >= threshold["value"]
    return threshold["value"] <= price <= threshold["value_high"]


def intent_for(threshold_name: str) -> str:
    """어떤 질문을 준비하는가. 어떤 답이 맞는지는 정하지 않는다."""
    if threshold_name in SCALE_OUT_NAMES or threshold_name.startswith("exit_stage:"):
        return "TRIM_REVIEW"
    return "HOLD_REVIEW"


def _key(ticker: str, assessment_id: str, name: str) -> str:
    return f"{ticker}|{assessment_id}|{name}"


def evaluate_asset(
    *,
    ticker: str,
    name: str,
    assessment: dict[str, Any] | None,
    quote: dict[str, Any] | None,
    active: dict[str, Any],
    today: str,
    now_iso: str,
) -> dict[str, Any]:
    """종목 하나를 대조한다. `active` 는 바꾸지 않고 새 사본을 돌려준다."""
    events: list[dict[str, Any]] = []
    errors: list[str] = []
    new_active = dict(active)
    prefix = f"{ticker}|"

    if assessment is None:
        # 살아 있는 판단이 없으면 대조할 문턱이 없다. 이전 접촉 상태는 의미를 잃는다.
        for key in [k for k in new_active if k.startswith(prefix)]:
            del new_active[key]
        return {"events": events, "errors": errors, "active": new_active, "skipped": "NO_LIVE_ASSESSMENT"}

    if not quote or quote.get("price") is None or not quote.get("comparable"):
        return {"events": events, "errors": errors, "active": new_active, "skipped": "QUOTE_NOT_COMPARABLE"}

    assessment_id = str(assessment.get("assessment_id") or "")
    plan_currency = (assessment.get("price_plan") or {}).get("currency")
    quote_currency = quote.get("currency")
    if plan_currency and quote_currency and plan_currency != quote_currency:
        errors.append(
            f"{ticker}: 판단 통화({plan_currency})와 시세 통화({quote_currency})가 달라 대조하지 않았다"
        )
        return {"events": events, "errors": errors, "active": new_active, "skipped": "CURRENCY_MISMATCH"}

    price = float(quote["price"])
    current = thresholds_of(assessment, today)
    current_keys = {_key(ticker, assessment_id, t["name"]) for t in current}
    # 판단이 바뀌었거나 단계 유효기간이 끝난 문턱의 상태는 지운다.
    for key in [k for k in new_active if k.startswith(prefix) and k not in current_keys]:
        del new_active[key]

    signals = [
        {"signal": s.get("signal"), "action": s.get("action")}
        for s in (assessment.get("invalidation_signals") or [])
        if isinstance(s, dict) and s.get("signal")
    ]
    for threshold in current:
        key = _key(ticker, assessment_id, threshold["name"])
        is_touched = touched(threshold, price)
        if not is_touched:
            new_active.pop(key, None)
            continue
        if key in new_active:
            continue
        new_active[key] = {"since": now_iso, "price": price}
        stage = threshold.get("stage")
        events.append(
            {
                "type": PRICE_TRIGGER,
                "ticker": ticker,
                "name": name,
                "assessment_id": assessment_id,
                "assessment_created_at": assessment.get("created_at"),
                "recorded_verdict": assessment.get("verdict"),
                "recorded_action": (assessment.get("action") or {}).get("direction"),
                "threshold": {
                    "name": threshold["name"],
                    "label": threshold["label"],
                    "direction": threshold["direction"],
                    "value": threshold["value"],
                    **({"value_high": threshold["value_high"]} if "value_high" in threshold else {}),
                },
                "price": price,
                "currency": quote_currency or plan_currency,
                "quote_time": quote.get("quote_time"),
                "price_kind": quote.get("price_kind"),
                "detected_at": now_iso,
                "intent": intent_for(threshold["name"]),
                "promised_action": stage,
                # 가격 문턱 중 손절·재질문 계열에만 원문 약속을 붙인다.
                "invalidation_signals": signals if threshold["name"] in (
                    "stop_price", "reask_below", "reask_above"
                ) and signals else None,
            }
        )
    return {"events": events, "errors": errors, "active": new_active, "skipped": None}


def latest_live_assessment(
    ticker: str,
    *,
    survey_fn: Callable[[str], dict[str, Any]] | None = None,
    load_fn: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """계약을 통과하는 살아 있는 판단 중 최신 하나. 없으면 None.

    계약 미달·폐기 판단은 `survey()['live']` 에 없으므로 대조 대상이 아니다.
    """
    if survey_fn is None or load_fn is None:
        from calculators.contracts import load_json
        from cio.paths import ROOT
        from cio.retire import survey

        survey_fn = survey_fn or survey
        load_fn = load_fn or (lambda rel: load_json(ROOT / rel))
    state = survey_fn(ticker)
    live = state.get("live") or []
    if not live:
        return None
    newest = max(live, key=lambda row: str(row.get("created_at") or ""))
    return load_fn(newest["path"])
