"""한도 위반과 판단 노후화를 사실로 대조한다 (이벤트 유형 POLICY_BREACH · STALE_ASSESSMENT · EARNINGS_NEAR).

판정하지 않는다. "비중 X%가 사용자가 정한 한도 Y%를 넘었다"처럼 측정치와 사용자가 정한
값을 나란히 놓을 뿐이다. 무엇을 할지는 말하지 않는다.

미정은 감시하지 않는다
  `investor-policy.json` 의 한도나 `watch.json` 의 `review` 임계값이 null 이면 그
  항목은 대조하지 않는다. 기본값을 채우지 않는다. 감시하지 못한 항목은 `not_monitored`
  로 이유와 함께 돌려주어 상태 화면과 브리핑에 보고된다.

정책 한도 입력 경로
  `get_portfolio_status()` 는 `tickers.json` 의 `priceKrw`(일봉 기준)를 쓴다. 장중 현재가를
  반영하려고 이 모듈은 `quotes.json` 의 가격으로 평가액을 다시 계산한다. 분모는
  `get_portfolio_status()` 와 같은 규칙이다: 총자산이 정책에 있으면 max(총자산, 주식 평가액),
  없으면 주식 평가액 + 정책 현금. ETF 관통(look-through)은 하지 않는다 — 장중 비중은
  보유 종목 단위의 근사이며 이벤트에 `basis` 로 그 사실을 적는다.

중복 제거는 `watch_triggers` 와 같은 상태(`active_triggers`)를 쓰되 키 접두사가 `REV|` 다.
조건이 풀리면 키를 지우고 다시 성립하면 새 이벤트를 만든다.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

POLICY_BREACH = "POLICY_BREACH"
STALE_ASSESSMENT = "STALE_ASSESSMENT"
EARNINGS_NEAR = "EARNINGS_NEAR"
MISSING_COST = "MISSING_COST"
PREFIX = "REV|"

BASIS = "보유 종목 단위 장중 평가액 근사(ETF 관통 미반영, 평가액 = 수량 × 현재가)"


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value == value and abs(value) != float("inf") else None


def _get(policy: dict[str, Any] | None, group: str, leaf: str) -> float | None:
    if not isinstance(policy, dict):
        return None
    block = policy.get(group)
    return _num(block.get(leaf)) if isinstance(block, dict) else None


def policy_conditions(
    holdings: list[dict[str, Any]],
    quotes: dict[str, Any],
    policy: dict[str, Any] | None,
    sectors: dict[str, str],
    average_costs: dict[str, float],
    now_iso: str,
) -> dict[str, Any]:
    """정책 한도 대조. 반환: {conditions, not_monitored, evaluated}."""
    not_monitored: list[dict[str, str]] = []
    conditions: dict[str, dict[str, Any]] = {}

    single = _get(policy, "concentration", "max_single_position_pct_of_total")
    sector_limit = _get(policy, "concentration", "max_sector_pct_of_total")
    max_loss = _get(policy, "risk", "max_loss_per_position_krw")
    if policy is None:
        not_monitored.append({"item": "policy", "reason": "investor-policy.json 이 없다"})
    else:
        for item, value in (
            ("concentration.max_single_position_pct_of_total", single),
            ("concentration.max_sector_pct_of_total", sector_limit),
            ("risk.max_loss_per_position_krw", max_loss),
        ):
            if value is None:
                not_monitored.append({"item": item, "reason": "정하지 않음(null)"})

    values: dict[str, float] = {}
    for holding in holdings:
        quote = quotes.get(holding["ticker"]) or {}
        if quote.get("price") is None or not quote.get("comparable"):
            not_monitored.append(
                {"item": f"policy:{holding['ticker']}", "reason": "대조 가능한 현재가가 없다"}
            )
            return {"conditions": {}, "not_monitored": not_monitored, "evaluated": False}
        values[holding["ticker"]] = holding["qty"] * float(quote["price"])
    if not values or policy is None:
        return {"conditions": {}, "not_monitored": not_monitored, "evaluated": False}

    stock_value = sum(values.values())
    total_assets = _num(policy.get("total_assets_krw")) or 0.0
    cash = _num(policy.get("cash_krw")) or 0.0
    total = max(total_assets, stock_value) if total_assets > 0 else stock_value + cash
    if total <= 0:
        return {"conditions": {}, "not_monitored": not_monitored, "evaluated": False}

    names = {h["ticker"]: h["name"] for h in holdings}
    if single is not None:
        for ticker, value in values.items():
            pct = value / total * 100
            if pct > single:
                conditions[f"{PREFIX}POLICY|single|{ticker}"] = {
                    "type": POLICY_BREACH, "ticker": ticker, "name": names[ticker],
                    "limit": {"name": "max_single_position_pct_of_total", "value": single, "unit": "PCT"},
                    "measured": {"value": round(pct, 2), "unit": "PCT"},
                    "value_krw": round(value), "total_krw": round(total),
                    "basis": BASIS, "detected_at": now_iso,
                }
    if sector_limit is not None:
        by_sector: dict[str, float] = {}
        for ticker, value in values.items():
            sector = sectors.get(ticker)
            if sector:
                by_sector[sector] = by_sector.get(sector, 0.0) + value
        for sector, value in by_sector.items():
            pct = value / total * 100
            if pct > sector_limit:
                conditions[f"{PREFIX}POLICY|sector|{sector}"] = {
                    "type": POLICY_BREACH, "ticker": None, "name": sector,
                    "limit": {"name": "max_sector_pct_of_total", "value": sector_limit, "unit": "PCT"},
                    "measured": {"value": round(pct, 2), "unit": "PCT"},
                    "sector": sector, "value_krw": round(value), "total_krw": round(total),
                    "basis": BASIS, "detected_at": now_iso,
                }
    if max_loss is not None:
        for holding in holdings:
            ticker = holding["ticker"]
            cost = average_costs.get(ticker)
            if cost is None:
                not_monitored.append(
                    {"item": f"risk.max_loss_per_position_krw:{ticker}",
                     "reason": "직접 입력한 평단이 없어 평가손실을 계산할 수 없다"}
                )
                continue
            loss = (cost - float(quotes[ticker]["price"])) * holding["qty"]
            if loss > max_loss:
                conditions[f"{PREFIX}POLICY|max_loss|{ticker}"] = {
                    "type": POLICY_BREACH, "ticker": ticker, "name": names[ticker],
                    "limit": {"name": "max_loss_per_position_krw", "value": max_loss, "unit": "KRW"},
                    "measured": {"value": round(loss), "unit": "KRW"},
                    "average_cost": cost,
                    "basis": "직접 입력한 평단 대비 평가손실(수수료·세금 제외)",
                    "detected_at": now_iso,
                }
    return {"conditions": conditions, "not_monitored": not_monitored, "evaluated": True}


def missing_cost_conditions(
    holdings: list[dict[str, Any]],
    average_costs: dict[str, float],
    now_iso: str,
) -> dict[str, dict[str, Any]]:
    """보유 수량이 0 이 아닌데 평단이 입력되지 않은 종목.

    임계값이 필요 없는 사실이다. 평단이 비어 있으면 손익과 손실 한도를 계산할 수
    없다는 것만 알린다.
    """
    conditions: dict[str, dict[str, Any]] = {}
    for holding in holdings:
        qty = _num(holding.get("qty"))
        if not qty or qty <= 0:
            continue
        ticker = holding["ticker"]
        if average_costs.get(ticker) is not None:
            continue
        conditions[f"{PREFIX}AVGCOST|{ticker}"] = {
            "type": MISSING_COST, "ticker": ticker, "name": holding["name"],
            "qty": qty, "detected_at": now_iso,
        }
    return conditions


def staleness_conditions(
    holdings: list[dict[str, Any]],
    quotes: dict[str, Any],
    assessments: dict[str, dict[str, Any] | None],
    asked_prices: dict[str, float | None],
    review: dict[str, Any],
    today: dt.date,
    now_iso: str,
) -> dict[str, Any]:
    """판단 노후화 대조. 반환: {conditions, not_monitored}."""
    conditions: dict[str, dict[str, Any]] = {}
    not_monitored: list[dict[str, str]] = []
    stale_days = _num(review.get("stale_after_days"))
    drift = _num(review.get("price_drift_pct"))
    if stale_days is None:
        not_monitored.append({"item": "review.stale_after_days", "reason": "정하지 않음(null)"})
    if drift is None:
        not_monitored.append({"item": "review.price_drift_pct", "reason": "정하지 않음(null)"})

    for holding in holdings:
        ticker = holding["ticker"]
        assessment = assessments.get(ticker)
        base = {"type": STALE_ASSESSMENT, "ticker": ticker, "name": holding["name"],
                "detected_at": now_iso}
        if assessment is None:
            # 임계값이 필요 없는 사실: 보유 중인데 유효한 판단이 없다.
            conditions[f"{PREFIX}STALE|none|{ticker}"] = {
                **base, "reason": "NO_LIVE_ASSESSMENT",
                "qty": holding.get("qty"),
            }
            continue
        assessment_id = assessment.get("assessment_id")
        created_text = str(assessment.get("created_at") or "")
        if stale_days is not None and created_text:
            try:
                created = dt.datetime.fromisoformat(created_text).date()
            except ValueError:
                created = None
            if created is not None:
                age = (today - created).days
                if age >= stale_days:
                    conditions[f"{PREFIX}STALE|age|{ticker}|{assessment_id}"] = {
                        **base, "reason": "AGE", "assessment_id": assessment_id,
                        "assessment_created_at": created_text,
                        "measured": {"value": age, "unit": "DAYS"},
                        "limit": {"name": "stale_after_days", "value": stale_days, "unit": "DAYS"},
                    }
        asked = asked_prices.get(ticker)
        quote = quotes.get(ticker) or {}
        if drift is not None and asked and quote.get("comparable") and quote.get("price"):
            pct = (float(quote["price"]) - asked) / asked * 100
            if abs(pct) >= drift:
                conditions[f"{PREFIX}STALE|drift|{ticker}|{assessment_id}"] = {
                    **base, "reason": "PRICE_DRIFT", "assessment_id": assessment_id,
                    "assessment_created_at": created_text,
                    "asked_price": asked, "price": float(quote["price"]),
                    "measured": {"value": round(pct, 2), "unit": "PCT"},
                    "limit": {"name": "price_drift_pct", "value": drift, "unit": "PCT"},
                }
    return {"conditions": conditions, "not_monitored": not_monitored}


def earnings_conditions(
    holdings: list[dict[str, Any]],
    calendars: dict[str, dict[str, Any] | None],
    review: dict[str, Any],
    today: dt.date,
    now_iso: str,
    business_days_until,
) -> dict[str, Any]:
    """실적발표 임박 대조. 발표일은 회사가 바꿀 수 있는 예정일이며 확정이 아니다."""
    limit = _num(review.get("earnings_within_business_days"))
    if limit is None:
        return {
            "conditions": {},
            "not_monitored": [
                {"item": "review.earnings_within_business_days", "reason": "정하지 않음(null)"}
            ],
        }
    conditions: dict[str, dict[str, Any]] = {}
    not_monitored: list[dict[str, str]] = []
    for holding in holdings:
        ticker = holding["ticker"]
        calendar = calendars.get(ticker)
        if not calendar:
            not_monitored.append(
                {"item": f"earnings:{ticker}", "reason": "실적 캘린더 산출물이 없다"}
            )
            continue
        for field, label in (
            ("next_provisional_result", "잠정실적 공시 예정"),
            ("expected_periodic_report", "정기보고서 제출 예상"),
        ):
            block = calendar.get(field)
            if not isinstance(block, dict):
                continue
            date_text = block.get("disclosure_date") if field == "next_provisional_result" else block.get("date")
            if not isinstance(date_text, str):
                continue
            days = business_days_until(date_text, today)
            if days is None or days > limit:
                continue
            conditions[f"{PREFIX}EARN|{field}|{ticker}|{date_text}"] = {
                "type": EARNINGS_NEAR, "ticker": ticker, "name": holding["name"],
                "event": label, "event_date": date_text,
                "estimated": bool(block.get("estimated", field != "next_provisional_result")),
                "measured": {"value": days, "unit": "BUSINESS_DAYS"},
                "limit": {"name": "earnings_within_business_days", "value": limit, "unit": "BUSINESS_DAYS"},
                "basis": "주말만 제외한 근사(공휴일 미반영). 예정일은 회사가 바꿀 수 있다",
                "detected_at": now_iso,
            }
    return {"conditions": conditions, "not_monitored": not_monitored}


def apply_dedup(
    active: dict[str, Any],
    conditions: dict[str, dict[str, Any]],
    evaluated_prefixes: list[str],
    now_iso: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """성립 중인 조건 중 새로 성립한 것만 이벤트로 돌려준다.

    `evaluated_prefixes` 에 해당하는 키만 갱신한다. 이번에 평가하지 못한 그룹의 상태는
    그대로 둔다 — 평가하지 못한 것을 '해소됨'으로 읽으면 같은 알림이 다시 나온다.
    """
    new_active = {
        key: value for key, value in active.items()
        if not any(key.startswith(prefix) for prefix in evaluated_prefixes)
    }
    events: list[dict[str, Any]] = []
    for key, event in conditions.items():
        previous = active.get(key)
        new_active[key] = previous or {"since": now_iso}
        if previous is None:
            events.append(event)
    return events, new_active


def load_calendar(ticker: str, knowledge_dir: Path) -> dict[str, Any] | None:
    """`collectors/kr_calendar.py` 산출물을 읽는다. 없으면 None(그 모듈의 load 는 종료한다)."""
    path = knowledge_dir / "calendar" / f"{ticker}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None
