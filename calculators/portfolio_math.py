#!/usr/bin/env python3
"""실시간 tickers.json 보유 수량을 바탕으로 포트폴리오를 계산하고 리포팅 데이터를 생성한다.

모든 모듈은 cio/paths.py의 ROOT, CONFIG_DIR, KNOWLEDGE_DIR, DATA_DIR 경로 상수를 사용한다.
"""

import json
import glob
from pathlib import Path
from typing import Any

from cio.paths import ROOT, CONFIG_DIR, KNOWLEDGE_DIR
from calculators.portfolio import analyze, load_constituents

TICKERS_FILE = ROOT / "dashboard" / "scripts" / "tickers.json"
ASSETS_FILE = CONFIG_DIR / "assets.json"
POLICY_FILE = CONFIG_DIR / "investor-policy.json"


def _load_policy() -> dict[str, Any]:
    """investor-policy.json 로드. 없으면 기본값 반환."""
    try:
        with open(POLICY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def save_policy(policy: dict[str, Any]) -> dict[str, Any]:
    """investor-policy.json 저장. 스키마 버전을 보존하고 반환."""
    existing = _load_policy()
    # 기존 _comment 및 schema_version 보존
    merged = {**existing, **policy}
    merged["schema_version"] = existing.get("schema_version", "ips/1.0.0")
    if "_comment" in existing:
        merged["_comment"] = existing["_comment"]
    with open(POLICY_FILE, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    return merged


def get_policy() -> dict[str, Any]:
    """현재 투자자 정책을 UI용으로 정제하여 반환."""
    raw = _load_policy()
    allocation = raw.get("allocation", {})
    concentration = raw.get("concentration", {})
    rebalance = raw.get("rebalance", {})
    return {
        "total_assets_krw": raw.get("total_assets_krw", 0),
        "cash_krw": raw.get("cash_krw", 0),
        "allocation": {
            "safe_asset_target_pct": allocation.get("safe_asset_target_pct", 30),
            "risk_asset_target_pct": allocation.get("risk_asset_target_pct", 70),
        },
        "concentration": {
            "max_single_position_pct_of_total": concentration.get("max_single_position_pct_of_total", 20),
            "max_sector_pct_of_total": concentration.get("max_sector_pct_of_total", 35),
        },
        "rebalance": {
            "tolerance_pct": rebalance.get("tolerance_pct", 5),
        },
    }


def _compute_allocation_status(
    holdings: list[dict],
    policy: dict[str, Any],
) -> dict[str, Any]:
    """현재 보유 자산을 안전/위험으로 분류하고 목표 대비 갭을 계산한다."""
    SAFE_TYPES = {"CASH", "CASH_EQUIVALENT", "BOND"}

    stock_value = sum(h["value_krw"] for h in holdings)
    total_assets_krw = float(policy.get("total_assets_krw", 0))

    # 총자산이 입력되면 현금·예수금 = 총자산 - 보유 주식 평가액 (자동).
    # 총자산이 0이면 수동 입력된 현금 + 주식 평가액으로 총자산을 계산한다.
    if total_assets_krw > 0:
        cash_krw = max(0.0, total_assets_krw - stock_value)
    else:
        cash_krw = float(policy.get("cash_krw", 0))
    total = stock_value + cash_krw

    safe_value = cash_krw + sum(
        h["value_krw"] for h in holdings if h["asset_type"].upper() in SAFE_TYPES
    )
    risk_value = total - safe_value

    safe_pct = (safe_value / total * 100) if total > 0 else 0.0
    risk_pct = (risk_value / total * 100) if total > 0 else 0.0

    alloc = policy.get("allocation", {})
    safe_target = float(alloc.get("safe_asset_target_pct", 30))
    risk_target = float(alloc.get("risk_asset_target_pct", 70))
    tolerance = float(policy.get("rebalance", {}).get("tolerance_pct", 5))

    safe_gap = safe_pct - safe_target    # 양수 = 초과, 음수 = 부족
    risk_gap = risk_pct - risk_target

    def rebalance_action(gap_pct: float, label: str, value: float) -> dict:
        abs_gap = abs(gap_pct)
        if abs_gap <= tolerance:
            return {"status": "OK", "action": "리밸런싱 불필요", "amount_krw": 0}
        amount = abs(gap_pct / 100 * total)
        if gap_pct > 0:
            return {
                "status": "OVERWEIGHT",
                "action": f"{label} {abs_gap:.1f}%p 초과 → 일부 매도 또는 현금 축소",
                "amount_krw": round(amount),
            }
        else:
            return {
                "status": "UNDERWEIGHT",
                "action": f"{label} {abs_gap:.1f}%p 부족 → 추가 매수 또는 현금 투입",
                "amount_krw": round(amount),
            }

    return {
        "total_assets_krw": round(total),
        "stock_value_krw": round(stock_value),
        "cash_krw": round(cash_krw),
        "safe_value_krw": round(safe_value),
        "risk_value_krw": round(risk_value),
        "safe_pct": round(safe_pct, 2),
        "risk_pct": round(risk_pct, 2),
        "safe_target_pct": safe_target,
        "risk_target_pct": risk_target,
        "safe_gap_pct": round(safe_gap, 2),
        "risk_gap_pct": round(risk_gap, 2),
        "tolerance_pct": tolerance,
        "rebalance": {
            "safe": rebalance_action(safe_gap, "안전자산", safe_value),
            "risk": rebalance_action(risk_gap, "위험자산", risk_value),
        },
    }


def get_portfolio_status() -> dict[str, Any]:
    """tickers.json 상의 보유 수량(qty > 0)을 분석하여 포트폴리오 상태 및 한도 검토 결과를 생성한다."""
    # 1. tickers.json 로드
    try:
        with open(TICKERS_FILE, "r", encoding="utf-8") as f:
            tickers_raw = json.load(f)
    except FileNotFoundError:
        tickers_raw = {}

    # 2. assets.json 로드
    try:
        with open(ASSETS_FILE, "r", encoding="utf-8") as f:
            assets_raw = json.load(f).get("assets", {})
    except FileNotFoundError:
        assets_raw = {}

    # 3. holdings 목록 구성 (qty > 0 인 자산)
    holdings = []
    for ticker, val in tickers_raw.items():
        qty = float(val.get("qty", 0))
        if qty <= 0:
            continue

        price = float(val.get("priceKrw", 0))
        value_krw = qty * price

        asset_meta = assets_raw.get(ticker, {})
        market = asset_meta.get("market", val.get("market", "KR"))
        asset_type = asset_meta.get("asset_type", val.get("riskClass", "STOCK"))
        currency = asset_meta.get("currency", val.get("currency", "KRW"))
        sector = asset_meta.get("sector_code", None)
        industry = asset_meta.get("industry", "")

        holdings.append({
            "asset_id": ticker,
            "name": val.get("name", ticker),
            "market": market.upper(),
            "asset_type": asset_type.upper(),
            "value_krw": value_krw,
            "fx_exposure": asset_meta.get("fx_exposure", None),
            "sector": sector,
            "note": industry,
        })

    # 4. investor-policy.json 로드
    policy = _load_policy()

    # 보유 자산이 없는 경우 디폴트 반환
    if not holdings:
        return {
            "total_value_krw": 0,
            "base_currency": "KRW",
            "denominators_krw": {
                "total_assets": 0,
                "equity_sleeve": 0,
                "risk_assets": 0,
            },
            "asset_count": 0,
            "effective_position_count": 0,
            "exposures": [],
            "currency_exposure": {},
            "sector_exposure": {},
            "warnings": [],
            "allocation_status": _compute_allocation_status([], policy),
            "policy": get_policy(),
            "message": "현재 보유 주식이 등록되어 있지 않습니다. [1. 내 종목] 탭에서 수량(qty)을 입력해 주세요."
        }

    # 5. 집중도 한도 설정 (investor-policy.json 우선, 없으면 기본값)
    default_limits = {
        "single": 15.0,
        "top10": 60.0,
        "sector": 30.0,
        "currency": 50.0,
        "unclassified": 20.0,
    }
    concentration = policy.get("concentration", {})
    limits = {
        "single": float(concentration.get("max_single_position_pct_of_total", default_limits["single"])),
        "top10": default_limits["top10"],
        "sector": float(concentration.get("max_sector_pct_of_total", default_limits["sector"])),
        "currency": default_limits["currency"],
        "unclassified": default_limits["unclassified"],
    }
    is_policy_custom = bool(concentration)

    # 6. constituents (ETF 관통 데이터) 로드
    etf_csvs = glob.glob(str(KNOWLEDGE_DIR / "etfs" / "**" / "holdings-*.csv"), recursive=True)
    constituents = load_constituents(etf_csvs) if etf_csvs else {}

    # 7. calculators/portfolio.py 분석 실행
    analysis_result = analyze(holdings, constituents, limits)
    analysis_result["is_policy_custom"] = is_policy_custom

    # 8. 자산배분 현황 및 목표 갭 추가
    analysis_result["allocation_status"] = _compute_allocation_status(holdings, policy)
    analysis_result["policy"] = get_policy()

    return analysis_result


if __name__ == "__main__":
    # 단독 테스트 코드
    print(json.dumps(get_portfolio_status(), ensure_ascii=False, indent=2))
