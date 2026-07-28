#!/usr/bin/env python3
"""구조화된 매매 전 정책·근거 점검 엔진.

판정 의미:
  BLOCKED           불가능 주문, 안전 위반, 오래된/손상된 핵심 데이터
  RESEARCH_REQUIRED 승인 IPS 또는 검증된 논지·기대수익·대안 분석 부족
  CAUTION           거래는 가능하지만 근거 또는 기회비용을 재검토해야 함
  PROCEED           승인 정책과 검증된 분석 계약상 차단 사유 없음

PROCEED는 수익이나 주가 방향을 보장하지 않는다.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from contracts import (
    ContractError,
    load_calculation,
    load_json,
    parse_datetime,
    probability_weighted_return,
    stable_hash,
    validate_analysis,
    validate_ips,
    verify_expected_return_calculation,
)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HOLDINGS = ROOT / "knowledge" / "portfolio" / "holdings.csv"
DEFAULT_IPS = ROOT / "knowledge" / "portfolio" / "ips.json"
DEFAULT_CALCULATIONS = ROOT / "knowledge" / "calculations"
PRICE_DIR = ROOT / "knowledge" / "prices"
ENGINE_VERSION = "2.2.0"
RESULT_SCHEMA_VERSION = "1.0.0"

EQUITY_TYPES = {"STOCK", "ETF", "REIT"}
NON_RISK_TYPES = {"CASH", "CASH_EQUIVALENT", "BOND"}
MARKET_TO_CURRENCY = {"KR": "KRW", "US": "USD"}
VERDICT_ORDER = {"PROCEED": 0, "CAUTION": 1, "RESEARCH_REQUIRED": 2, "BLOCKED": 3}
VERDICT_EXIT_CODE = {"PROCEED": 0, "CAUTION": 1, "RESEARCH_REQUIRED": 2, "BLOCKED": 3}


def _number(value: Any) -> float | None:
    try:
        number = float(str(value).replace(",", "").strip())
    except (ValueError, TypeError, AttributeError):
        return None
    return number if math.isfinite(number) else None


def _issue(issues: list[dict[str, str]], severity: str, code: str, message: str):
    issues.append({"severity": severity, "code": code, "message": message})


def _verdict(issues: list[dict[str, str]]) -> str:
    if not issues:
        return "PROCEED"
    return max((i["severity"] for i in issues), key=lambda s: VERDICT_ORDER[s])


def exit_code_for_verdict(verdict: str) -> int:
    """자동화가 주의·조사 필요를 성공으로 오인하지 않게 한다."""
    return VERDICT_EXIT_CODE[verdict]


def load_holdings(path: str | Path) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    """보유내역과 주석 메타데이터를 읽되 오류를 숨기지 않는다."""
    path = Path(path)
    errors: list[str] = []
    try:
        raw_lines = path.read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError:
        return [], {}, [f"보유내역 파일을 찾을 수 없다: {path}"]

    comments = [line for line in raw_lines if line.lstrip().startswith("#")]
    data_lines = [line for line in raw_lines if not line.lstrip().startswith("#")]
    metadata: dict[str, Any] = {"path": str(path)}
    for comment in comments:
        match = re.search(r"기준일\s+(\d{4}-\d{2}-\d{2})", comment)
        if match:
            metadata["as_of"] = match.group(1)
            break

    reader = csv.DictReader(data_lines)
    required = {"asset_id", "name", "market", "asset_type", "value_krw"}
    if not reader.fieldnames or required - set(reader.fieldnames):
        missing = sorted(required - set(reader.fieldnames or []))
        return [], metadata, [f"보유내역 필수 열 누락: {', '.join(missing)}"]

    rows = []
    seen = set()
    for line_no, row in enumerate(reader, start=2):
        if not any((value or "").strip() for value in row.values()):
            continue
        asset_id = (row.get("asset_id") or "").strip()
        if not asset_id:
            errors.append(f"{line_no}행: asset_id가 비어 있다")
            continue
        if asset_id in seen:
            errors.append(f"{line_no}행: 중복 asset_id {asset_id}")
            continue
        seen.add(asset_id)
        value = _number(row.get("value_krw"))
        quantity = _number(row.get("quantity"))
        market = (row.get("market") or "").strip().upper()
        asset_type = (row.get("asset_type") or "").strip().upper()
        if not market:
            errors.append(f"{line_no}행 {asset_id}: market이 비어 있다")
        if not asset_type:
            errors.append(f"{line_no}행 {asset_id}: asset_type이 비어 있다")
        if value is None:
            errors.append(f"{line_no}행 {asset_id}: value_krw가 숫자가 아니다")
            continue
        if value < 0:
            errors.append(f"{line_no}행 {asset_id}: 평가금액이 음수다")
        if quantity is not None and quantity < 0:
            errors.append(f"{line_no}행 {asset_id}: 보유수량이 음수다")
        rows.append({
            **row,
            "asset_id": asset_id,
            "name": (row.get("name") or asset_id).strip(),
            "market": market,
            "asset_type": asset_type,
            "value_krw": value,
            "quantity": quantity,
        })
    if not rows:
        errors.append("보유내역에 유효한 데이터 행이 없다")
    return rows, metadata, errors


def portfolio_denominators(rows: list[dict[str, Any]]) -> dict[str, float]:
    total = sum(row["value_krw"] for row in rows)
    equity = sum(row["value_krw"] for row in rows if row["asset_type"] in EQUITY_TYPES)
    risk = sum(row["value_krw"] for row in rows if row["asset_type"] not in NON_RISK_TYPES)
    cash = sum(row["value_krw"] for row in rows if row["asset_type"] in {"CASH", "CASH_EQUIVALENT"})
    return {
        "total_assets": total,
        "equity_sleeve": equity,
        "risk_assets": risk,
        "cash": cash,
    }


def resolve_risk_class(row: dict[str, Any]) -> str:
    """위험등급은 명시값이 우선, 없으면 자산 유형으로 떨어진다.

    ETF라는 껍데기는 위험을 말해주지 않는다. 광역 분산 ETF와 단일 기초자산
    파생 ETF는 같은 `asset_type`을 갖지만 위험이 전혀 다르므로, 한도는
    `asset_type`이 아니라 이 등급으로 건다.
    """
    explicit = (row.get("risk_class") or "").strip().upper()
    return explicit or (row.get("asset_type") or "").strip().upper()


def resolve_currency(market: str, explicit: str | None) -> str:
    """통화는 명시값이 우선, 없으면 시장에서 추론한다.

    lookthrough.py의 같은 이름 함수와 규칙을 맞춘다.
    """
    if explicit:
        return explicit.strip().upper()
    return MARKET_TO_CURRENCY.get(market, "UNKNOWN")


def portfolio_concentration(rows: list[dict[str, Any]], total: float) -> dict[str, Any] | None:
    """총자산 대비 직접 비중 기준 집중도.

    ETF 룩스루는 반영하지 않는다. 따라서 섹터 비중은 하한이며, ETF가 품고
    있는 섹터 노출은 `unclassified_pct`로 격리된다. IPS의
    `unclassified_total_assets_pct`가 이 사각지대의 크기를 제한한다.
    실제 룩스루 집중도는 portfolio-xray SKILL이 계산한다.
    """
    if total <= 0:
        return None
    positions = [r for r in rows if r["asset_type"] not in {"CASH", "CASH_EQUIVALENT"}]
    ranked = sorted(positions, key=lambda r: -r["value_krw"])

    sector: dict[str, float] = {}
    unclassified = 0.0
    for row in positions:
        name = (row.get("sector") or "").strip()
        if name:
            sector[name] = sector.get(name, 0.0) + row["value_krw"]
        else:
            unclassified += row["value_krw"]

    currency: dict[str, float] = {}
    for row in rows:
        code = resolve_currency(row["market"], row.get("fx_exposure"))
        currency[code] = currency.get(code, 0.0) + row["value_krw"]

    return {
        "top10_pct": sum(r["value_krw"] for r in ranked[:10]) / total * 100,
        "sector_pct": {k: v / total * 100 for k, v in sector.items()},
        "currency_pct": {
            k: v / total * 100 for k, v in currency.items() if k != "UNKNOWN"
        },
        "unclassified_pct": unclassified / total * 100,
        "lookthrough_applied": False,
    }


def _weight(value: float, denominator: float) -> float | None:
    return value / denominator * 100 if denominator > 0 else None


def _weights(position_value: float, denominators: dict[str, float]) -> dict[str, dict[str, float | None]]:
    return {
        "total_assets": {
            "position_pct": _weight(position_value, denominators["total_assets"]),
            "denominator_krw": denominators["total_assets"],
        },
        "equity_sleeve": {
            "position_pct": _weight(position_value, denominators["equity_sleeve"]),
            "denominator_krw": denominators["equity_sleeve"],
        },
        "risk_assets": {
            "position_pct": _weight(position_value, denominators["risk_assets"]),
            "denominator_krw": denominators["risk_assets"],
        },
    }


def _costs(ips: dict[str, Any] | None, side: str, gross: float, market: str) -> dict[str, float | None]:
    """시장별 비용 모형. 국내와 해외는 세금·환전 구조가 달라 값을 섞지 않는다."""
    empty = {
        "commission_krw": None,
        "tax_krw": None,
        "slippage_krw": None,
        "fx_cost_krw": None,
        "total_krw": None,
        "market": market,
    }
    if not ips or ips.get("status") != "APPROVED":
        return empty
    policy = (ips["costs"].get("by_market") or {}).get(market)
    if not policy:
        return empty
    commission_bps = policy["buy_commission_bps"] if side == "buy" \
        else policy["sell_commission_bps"]
    tax_bps = 0 if side == "buy" else policy["sell_tax_bps"]
    values = {
        "commission_krw": gross * commission_bps / 10000,
        "tax_krw": gross * tax_bps / 10000,
        "slippage_krw": gross * policy["slippage_bps"] / 10000,
        "fx_cost_krw": gross * policy["fx_cost_bps"] / 10000,
    }
    values["total_krw"] = sum(values.values())
    values["market"] = market
    return values


def _snapshot_candidates(asset_id: str, market: str) -> list[str]:
    """price_fetch.py가 저장하는 심볼 표기를 시장별로 추정한다."""
    if market == "KR":
        return [f"{asset_id}.KQ", f"{asset_id}.KS", asset_id]
    return [asset_id]


def _read_price_snapshot(asset_id: str, market: str, price_dir: Path) -> dict[str, Any] | None:
    """price_fetch.py가 저장한 시세 스냅샷의 최근 종가와 통화를 읽는다."""
    for symbol in _snapshot_candidates(asset_id, market):
        path = Path(price_dir) / f"{symbol.replace('.', '_')}.csv"
        if not path.exists():
            continue
        try:
            lines = path.read_text(encoding="utf-8-sig").splitlines()
        except OSError:
            continue
        currency = None
        for line in lines:
            if line.lstrip().startswith("#"):
                match = re.search(r"통화\s+(\S+)", line)
                if match:
                    currency = match.group(1).upper()
        data_lines = [line for line in lines if not line.lstrip().startswith("#")]
        rows = [row for row in csv.DictReader(data_lines)
                if (row.get("close") or "").strip()]
        if not rows:
            continue
        close = _number(rows[-1].get("close"))
        if close is None or close <= 0:
            continue
        return {
            "symbol": symbol,
            "close": close,
            "currency": currency,
            "date": (rows[-1].get("date") or "").strip(),
        }
    return None


def resolve_price_reference(
    target: dict[str, Any],
    base_currency: str,
    *,
    price_dir: str | Path = PRICE_DIR,
    holdings_as_of: str | None = None,
) -> dict[str, Any]:
    """입력 예정가를 대조할 기준가를 찾는다.

    보유내역의 내재단가는 정의상 기준통화 환산값이라 환율 모호성이 없어
    1순위로 쓴다. 시세 스냅샷은 통화가 기준통화와 같을 때만 대조에 쓴다.
    환율을 임의로 지어내 비교하지 않는다.

    기준가에는 항상 그 근거의 기준일을 함께 싣는다. 기준가가 낡을수록
    허용 괴리는 시세 변동까지 흡수해야 하므로 검증력이 떨어진다.
    """
    value = target.get("value_krw")
    quantity = target.get("quantity")
    if value and quantity and value > 0 and quantity > 0:
        return {
            "source": "HOLDINGS_IMPLIED",
            "price_krw": value / quantity,
            "as_of": holdings_as_of,
            "detail": f"보유 평가금액 {value:,.0f} ÷ 보유수량 {quantity:g}",
        }

    snapshot = _read_price_snapshot(target["asset_id"], target["market"], price_dir)
    if snapshot is None:
        return {
            "source": "NONE",
            "price_krw": None,
            "as_of": None,
            "detail": "보유 내재단가도 시세 스냅샷도 없다",
        }
    if snapshot["currency"] != base_currency.upper():
        return {
            "source": "SNAPSHOT_CURRENCY_MISMATCH",
            "price_krw": None,
            "as_of": snapshot["date"] or None,
            "detail": f"스냅샷 통화 {snapshot['currency'] or '미상'}가 기준통화 "
                      f"{base_currency.upper()}와 달라 환율 없이는 대조할 수 없다",
        }
    return {
        "source": "PRICE_SNAPSHOT",
        "price_krw": snapshot["close"],
        "as_of": snapshot["date"] or None,
        "detail": f"{snapshot['symbol']} {snapshot['date']} 종가",
    }


def _denominator_limits(ips: dict[str, Any], risk_class: str) -> dict[str, float | None]:
    """분모별로 적용되는 단일 종목 한도. 총자산 한도는 위험등급에서 온다."""
    limits = ips["position_limits"]
    class_limit = (limits.get("by_risk_class") or {}).get(risk_class) or {}
    return {
        "total_assets": class_limit.get("standard_pct"),
        "equity_sleeve": limits.get("equity_sleeve_pct"),
        "risk_assets": limits.get("risk_assets_pct"),
    }


def _check_portfolio_limits(
    issues: list[dict[str, str]],
    limits: dict[str, Any],
    before: dict[str, Any],
    after: dict[str, Any],
):
    """포트폴리오 수준 집중도 한도.

    단일 종목 한도와 달리 이 지표들은 거래하지 않은 다른 종목 때문에도
    초과할 수 있다. 이미 초과한 상태를 이유로 모든 거래를 막으면 포트폴리오가
    잠기고 위험을 줄이는 거래마저 불가능해진다. 따라서 이 거래가 한도를
    새로 넘기거나 이미 넘긴 값을 더 악화시킬 때만 BLOCKED로 다룬다.
    """
    def judge(code: str, limit: float | None, before_pct: float,
              after_pct: float, label: str):
        if limit is None or after_pct <= limit + 1e-9:
            return
        if before_pct <= limit + 1e-9:
            _issue(issues, "BLOCKED", code,
                   f"이 거래가 {label}을 {before_pct:.2f}%에서 {after_pct:.2f}%로 "
                   f"올려 승인 한도 {limit:.2f}%를 넘긴다")
        elif after_pct > before_pct + 1e-9:
            _issue(issues, "BLOCKED", f"{code}_WORSENED",
                   f"{label}이 이미 한도 {limit:.2f}%를 넘은 상태에서 "
                   f"{before_pct:.2f}% → {after_pct:.2f}%로 더 악화된다")
        else:
            _issue(issues, "CAUTION", f"{code}_STILL_OVER",
                   f"{label} {after_pct:.2f}%가 아직 한도 {limit:.2f}%를 넘는다 "
                   f"(거래 전 {before_pct:.2f}%, 이 거래가 악화시키지는 않는다)")

    judge("TOP10_CONCENTRATION", limits["top10_total_assets_pct"],
          before["top10_pct"], after["top10_pct"], "상위 10종목 비중")
    judge("UNCLASSIFIED_CONCENTRATION", limits["unclassified_total_assets_pct"],
          before["unclassified_pct"], after["unclassified_pct"], "섹터 미분류 비중")
    for name, pct in sorted(after["sector_pct"].items()):
        judge("SECTOR_CONCENTRATION", limits["sector_total_assets_pct"],
              before["sector_pct"].get(name, 0.0), pct, f"{name} 섹터 비중")
    for code, pct in sorted(after["currency_pct"].items()):
        judge("CURRENCY_CONCENTRATION", limits["currency_total_assets_pct"],
              before["currency_pct"].get(code, 0.0), pct, f"{code} 비중")


def _check_valuation_policy(
    issues: list[dict[str, str]],
    ips: dict[str, Any],
    analysis: dict[str, Any],
    calculations_dir: str | Path,
    risk_class: str | None,
) -> dict[str, Any] | None:
    """계산 산출물의 가정이 승인된 IPS 범위 안인지 본다.

    할인율과 영구성장률은 판정을 가장 크게 흔드는 값이다. 요구수익률만
    못박고 이쪽을 풀어두면, 분석가는 할인율 하나만 낮춰서 어떤 종목이든
    통과시킬 수 있다. 위험등급 한도와 같은 원칙으로 다룬다 — IPS 에
    정의가 없으면 기본값으로 때우지 않고 BLOCKED 다.
    """
    calculation_id = analysis.get("expected_return", {}).get("calculation_id")
    if not calculation_id:
        return None
    try:
        calculation = load_calculation(calculation_id, calculations_dir)
    except ContractError:
        return None  # CALCULATION_MISMATCH 가 이미 잡는다

    policy = ips.get("valuation_assumptions")
    if not isinstance(policy, dict) or not policy:
        _issue(issues, "BLOCKED", "VALUATION_POLICY_MISSING",
               "승인 IPS에 valuation_assumptions가 없다. 할인율·영구성장률이 "
               "지배되지 않으면 요구수익률 한도를 우회할 수 있다")
        return calculation

    inputs = calculation.get("inputs", {})
    provenance = inputs.get("input_provenance")

    if policy.get("require_verified_inputs"):
        if not isinstance(provenance, dict):
            _issue(issues, "BLOCKED", "VALUATION_INPUT_PROVENANCE_MISSING",
                   f"{calculation_id}: 입력 출처 기록이 없다. 원문에서 읽은 값인지 "
                   "손으로 친 값인지 구분할 수 없다")
        elif not provenance.get("verified"):
            _issue(issues, "BLOCKED", "VALUATION_INPUT_UNVERIFIED",
                   f"{calculation_id}: 밸류에이션 입력이 손입력이다 "
                   f"(사유: {provenance.get('reason') or '없음'}). "
                   "edgar_fetch fundamentals 로 원문 스냅샷을 만들어 다시 계산한다")

    by_class = policy.get("by_risk_class")
    entry = by_class.get(risk_class) if isinstance(by_class, dict) else None
    rate = inputs.get("discount_rate_pct")
    if not isinstance(entry, dict):
        _issue(issues, "BLOCKED", "VALUATION_RISK_CLASS_MISSING",
               f"위험등급 {risk_class}의 할인율 범위가 IPS에 없다")
    elif isinstance(rate, (int, float)):
        low, high = entry.get("discount_rate_pct_min"), entry.get("discount_rate_pct_max")
        if isinstance(low, (int, float)) and isinstance(high, (int, float)) \
                and not (low <= rate <= high):
            _issue(issues, "BLOCKED", "DISCOUNT_RATE_OUT_OF_POLICY",
                   f"할인율 {rate:.2f}%가 {risk_class}의 승인 범위 "
                   f"{low:.2f}~{high:.2f}% 밖이다")

    for key, limit_key, label in (
        ("terminal_growth_pct", "terminal_growth_pct_max", "영구성장률"),
        ("years", "stage_one_years_max", "1단계 기간"),
        ("horizon_months", "horizon_months_max", "수렴 가정 기간"),
    ):
        value, limit = inputs.get(key), policy.get(limit_key)
        if isinstance(value, (int, float)) and isinstance(limit, (int, float)) \
                and value > limit:
            _issue(issues, "BLOCKED", f"{key.upper()}_OUT_OF_POLICY",
                   f"{label} {value}이(가) 승인 상한 {limit}을(를) 넘는다")
    return calculation


def _load_contract(path: str | Path | None, kind: str, issues: list[dict[str, str]]):
    if not path:
        _issue(issues, "RESEARCH_REQUIRED", f"{kind.upper()}_MISSING",
               f"{kind} 파일이 지정되지 않았다")
        return None
    try:
        return load_json(path)
    except FileNotFoundError:
        _issue(issues, "RESEARCH_REQUIRED", f"{kind.upper()}_MISSING",
               f"{kind} 파일을 찾을 수 없다: {path}")
    except ContractError as exc:
        for message in exc.errors:
            _issue(issues, "BLOCKED", f"{kind.upper()}_INVALID", message)
    return None


def evaluate_trade(
    asset_id: str,
    side: str,
    quantity: float,
    price: float,
    *,
    holdings_path: str | Path = DEFAULT_HOLDINGS,
    ips_path: str | Path | None = DEFAULT_IPS,
    analysis_path: str | Path | None = None,
    price_as_of: str | None = None,
    evaluated_at: datetime | None = None,
    new_asset_name: str | None = None,
    new_asset_type: str = "STOCK",
    new_asset_market: str = "KR",
    new_asset_risk_class: str | None = None,
    price_unverified_reason: str | None = None,
    concentration_override_reason: str | None = None,
    price_dir: str | Path = PRICE_DIR,
    calculations_dir: str | Path | None = DEFAULT_CALCULATIONS,
) -> dict[str, Any]:
    evaluated_at = (evaluated_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    issues: list[dict[str, str]] = []

    if side not in {"buy", "sell"}:
        _issue(issues, "BLOCKED", "INVALID_SIDE", "side는 buy 또는 sell이어야 한다")
    quantity_valid = (
        isinstance(quantity, (int, float))
        and not isinstance(quantity, bool)
        and math.isfinite(float(quantity))
        and quantity > 0
    )
    price_valid = (
        isinstance(price, (int, float))
        and not isinstance(price, bool)
        and math.isfinite(float(price))
        and price > 0
    )
    if not quantity_valid:
        _issue(
            issues, "BLOCKED", "INVALID_QUANTITY",
            "수량은 0보다 큰 유한한 숫자여야 한다",
        )
    if not price_valid:
        _issue(
            issues, "BLOCKED", "INVALID_PRICE",
            "예정가는 0보다 큰 유한한 숫자여야 한다",
        )

    holdings, holdings_meta, holdings_errors = load_holdings(holdings_path)
    for message in holdings_errors:
        _issue(issues, "BLOCKED", "HOLDINGS_INVALID", message)

    ips = _load_contract(ips_path, "ips", issues)
    if ips is not None:
        ips_errors = validate_ips(ips)
        for message in ips_errors:
            _issue(issues, "BLOCKED", "IPS_SCHEMA_INVALID", message)
        if not ips_errors and ips.get("status") != "APPROVED":
            _issue(issues, "RESEARCH_REQUIRED", "IPS_NOT_APPROVED",
                   f"IPS 상태가 {ips.get('status')}다. 사용자 승인 전에는 거래를 판정하지 않는다")

    analysis = _load_contract(analysis_path, "analysis", issues)
    if analysis is not None:
        analysis_errors = validate_analysis(analysis)
        for message in analysis_errors:
            _issue(issues, "BLOCKED", "ANALYSIS_SCHEMA_INVALID", message)
        if not analysis_errors:
            if analysis.get("instrument_id") != asset_id:
                _issue(issues, "BLOCKED", "ANALYSIS_INSTRUMENT_MISMATCH",
                       f"분석 대상 {analysis.get('instrument_id')}와 거래 대상 {asset_id}가 다르다")
            if analysis.get("thesis", {}).get("status") not in {"APPROVED", "INVALIDATED"}:
                _issue(issues, "RESEARCH_REQUIRED", "THESIS_NOT_APPROVED",
                       "투자논지가 사용자 승인 상태가 아니다")
            # 기대수익이 승인된 계산기의 산출물과 일치하는지 대조한다. 이 검사가
            # 없으면 계약은 calculation_id 라는 문자열이 있는지만 보고, 정작
            # 매매를 결정하는 숫자는 손으로 적을 수 있다.
            if calculations_dir is not None:
                for message in verify_expected_return_calculation(
                        analysis, calculations_dir):
                    _issue(issues, "BLOCKED", "CALCULATION_MISMATCH", message)

    approved_ips = (
        ips is not None and not validate_ips(ips) and ips.get("status") == "APPROVED"
    )

    if price_as_of is None:
        _issue(issues, "BLOCKED" if approved_ips else "RESEARCH_REQUIRED",
               "PRICE_TIMESTAMP_MISSING",
               "예정가의 기준시각이 없어 신선도를 판단할 수 없다")
        parsed_price_at = None
    else:
        price_errors: list[str] = []
        parsed_price_at = parse_datetime(price_as_of, "price_as_of", price_errors)
        for message in price_errors:
            _issue(issues, "BLOCKED", "PRICE_TIMESTAMP_INVALID", message)

    if approved_ips:
        max_holdings_days = ips["data_freshness"]["holdings_max_age_days"]
        holdings_date = holdings_meta.get("as_of")
        if not holdings_date:
            _issue(issues, "BLOCKED", "HOLDINGS_TIMESTAMP_MISSING",
                   "보유내역 기준일이 없어 신선도를 검증할 수 없다")
        else:
            try:
                age_days = (evaluated_at.date() - datetime.fromisoformat(holdings_date).date()).days
                if age_days < 0:
                    _issue(issues, "BLOCKED", "HOLDINGS_FROM_FUTURE",
                           f"보유내역 기준일 {holdings_date}이 평가시각보다 미래다")
                elif age_days > max_holdings_days:
                    _issue(issues, "BLOCKED", "HOLDINGS_STALE",
                           f"보유내역이 {age_days}일 경과했다 (허용 {max_holdings_days}일)")
            except ValueError:
                _issue(issues, "BLOCKED", "HOLDINGS_TIMESTAMP_INVALID",
                       f"보유내역 기준일이 올바르지 않다: {holdings_date}")

        if parsed_price_at:
            age_minutes = (evaluated_at - parsed_price_at).total_seconds() / 60
            if age_minutes < -1:
                _issue(issues, "BLOCKED", "PRICE_FROM_FUTURE",
                       "예정가 기준시각이 평가시각보다 미래다")
            elif age_minutes > ips["data_freshness"]["price_max_age_minutes"]:
                _issue(issues, "BLOCKED", "PRICE_STALE",
                       f"예정가가 {age_minutes:.0f}분 경과했다 "
                       f"(허용 {ips['data_freshness']['price_max_age_minutes']}분)")

        if analysis and not validate_analysis(analysis):
            analysis_time_errors: list[str] = []
            analysis_at = parse_datetime(analysis["as_of"], "analysis.as_of", analysis_time_errors)
            if analysis_at:
                age_days = (evaluated_at - analysis_at).total_seconds() / 86400
                if age_days < -1 / 24:
                    _issue(issues, "BLOCKED", "ANALYSIS_FROM_FUTURE",
                           "분석 기준시각이 평가시각보다 미래다")
                elif age_days > ips["data_freshness"]["analysis_max_age_days"]:
                    _issue(issues, "BLOCKED", "ANALYSIS_STALE",
                           f"분석이 {age_days:.1f}일 경과했다 "
                           f"(허용 {ips['data_freshness']['analysis_max_age_days']}일)")

    target = next((row for row in holdings if row["asset_id"] == asset_id), None)
    if target is None and side == "sell":
        _issue(issues, "BLOCKED", "POSITION_NOT_FOUND",
               f"{asset_id} 보유 포지션이 없어 매도할 수 없다")
    if target is None:
        target = {
            "asset_id": asset_id,
            "name": new_asset_name or asset_id,
            "market": new_asset_market.upper(),
            "asset_type": new_asset_type.upper(),
            "risk_class": (new_asset_risk_class or new_asset_type).upper(),
            "value_krw": 0.0,
            "quantity": 0.0,
        }
    if not target["market"]:
        _issue(issues, "BLOCKED", "MARKET_MISSING",
               "거래 대상의 시장 코드가 없어 환전비용을 검증할 수 없다")
    if not target["asset_type"]:
        _issue(issues, "BLOCKED", "ASSET_TYPE_MISSING",
               "거래 대상의 자산 유형이 없어 비중 분모를 검증할 수 없다")

    held_quantity = target.get("quantity")
    if side == "sell":
        if held_quantity is None:
            _issue(issues, "BLOCKED", "POSITION_QUANTITY_UNKNOWN",
                   "보유수량이 없어 과매도 여부를 검증할 수 없다")
        elif quantity > held_quantity + 1e-9:
            _issue(issues, "BLOCKED", "OVERSELL",
                   f"보유 {held_quantity:g}주보다 많은 {quantity:g}주를 매도할 수 없다")

    # 예정가는 사용자가 주장한 값이다. 대조하지 않으면 수량 한도·현금 검사가
    # 통째로 무력화된다 (과소입력 한 번이면 모든 비중이 작아 보인다).
    base_currency = (ips.get("base_currency") if ips else None) or "KRW"
    price_reference = resolve_price_reference(
        target, base_currency, price_dir=price_dir,
        holdings_as_of=holdings_meta.get("as_of"))
    if approved_ips and price_reference["price_krw"] and price_reference["as_of"]:
        max_reference_age = ips["data_quality"]["price_reference_max_age_days"]
        try:
            reference_age_days = (
                evaluated_at.date()
                - datetime.fromisoformat(price_reference["as_of"]).date()
            ).days
            price_reference["age_days"] = reference_age_days
            if reference_age_days > max_reference_age:
                _issue(issues, "BLOCKED", "PRICE_REFERENCE_STALE",
                       f"기준가가 {reference_age_days}일 경과해 예정가를 검증할 수 없다 "
                       f"(허용 {max_reference_age:.0f}일)")
        except ValueError:
            _issue(issues, "BLOCKED", "PRICE_REFERENCE_TIMESTAMP_INVALID",
                   f"기준가 기준일이 올바르지 않다: {price_reference['as_of']}")
    deviation_pct = None
    if price_valid and price_reference["price_krw"]:
        deviation_pct = abs(price / price_reference["price_krw"] - 1) * 100
        if approved_ips:
            max_deviation = ips["data_quality"]["price_max_deviation_pct"]
            if deviation_pct > max_deviation + 1e-9:
                _issue(issues, "BLOCKED", "PRICE_DEVIATION",
                       f"예정가 {price:,.0f}이 기준가 {price_reference['price_krw']:,.0f}"
                       f"({price_reference['detail']}) 대비 {deviation_pct:.1f}% 벗어났다 "
                       f"(허용 {max_deviation:.1f}%)")
    elif price_valid:
        if price_unverified_reason:
            _issue(issues, "CAUTION", "PRICE_UNVERIFIED_ACCEPTED",
                   f"대조할 기준가가 없으나 사용자가 사유를 명시했다: "
                   f"{price_unverified_reason}")
        else:
            _issue(issues, "BLOCKED" if approved_ips else "RESEARCH_REQUIRED",
                   "PRICE_UNVERIFIED",
                   f"예정가를 대조할 기준가가 없다 ({price_reference['detail']}). "
                   f"price_fetch.py로 시세를 수집하거나 사유를 명시해야 한다")
    price_reference = {
        **price_reference,
        "input_price_krw": price if price_valid else None,
        "deviation_pct": deviation_pct,
        "max_deviation_pct": (
            ips["data_quality"]["price_max_deviation_pct"] if approved_ips else None
        ),
        "override_reason": price_unverified_reason,
    }

    gross = quantity * price if quantity_valid and price_valid else 0.0
    trade_costs = _costs(ips, side, gross, target["market"])
    cost_total = trade_costs["total_krw"] or 0.0
    if approved_ips and trade_costs["total_krw"] is None:
        _issue(issues, "BLOCKED", "COST_MODEL_MISSING",
               f"시장 {target['market'] or '미지정'}의 비용 모형이 IPS에 없다. "
               f"수수료·세금을 0으로 가정하고 판정하지 않는다")
    before_denoms = portfolio_denominators(holdings)

    if side == "buy":
        required_cash = gross + cost_total
        if required_cash > before_denoms["cash"] + 1e-9:
            _issue(issues, "BLOCKED", "INSUFFICIENT_CASH",
                   f"필요 현금 {required_cash:,.0f}원이 보유 현금 "
                   f"{before_denoms['cash']:,.0f}원을 초과한다")

    if side == "sell" and held_quantity is not None and held_quantity > 0:
        sold_fraction = min(quantity / held_quantity, 1.0) if quantity_valid else 0.0
        position_delta = -target["value_krw"] * sold_fraction
    elif side == "buy":
        position_delta = gross
    else:
        position_delta = 0.0
    after_position = target["value_krw"] + position_delta
    if after_position < -1e-6:
        _issue(issues, "BLOCKED", "NEGATIVE_POSITION_VALUE",
               "거래 후 추정 포지션 가치가 음수가 된다")

    after_denoms = dict(before_denoms)
    revaluation_delta = gross + position_delta if side == "sell" else 0.0
    after_denoms["total_assets"] = max(
        0.0, before_denoms["total_assets"] + revaluation_delta - cost_total)
    if target["asset_type"] in EQUITY_TYPES:
        after_denoms["equity_sleeve"] = max(
            0.0, before_denoms["equity_sleeve"] + position_delta)
    if target["asset_type"] not in NON_RISK_TYPES:
        after_denoms["risk_assets"] = max(
            0.0, before_denoms["risk_assets"] + position_delta)
    after_denoms["cash"] = before_denoms["cash"] - gross - cost_total if side == "buy" \
        else before_denoms["cash"] + gross - cost_total

    before_weights = _weights(target["value_krw"], before_denoms)
    after_weights = _weights(max(0.0, after_position), after_denoms)

    risk_class = resolve_risk_class(target)
    if approved_ips:
        position_limits = ips["position_limits"]
        class_limit = (position_limits.get("by_risk_class") or {}).get(risk_class)
        total_pct = after_weights["total_assets"]["position_pct"]
        if total_pct is None:
            _issue(issues, "BLOCKED", "DENOMINATOR_ZERO",
                   "total_assets 분모가 0이라 비중을 검증할 수 없다")
        elif class_limit is None:
            _issue(issues, "BLOCKED", "RISK_CLASS_LIMIT_MISSING",
                   f"위험등급 {risk_class or '미지정'}에 대한 승인 한도가 IPS에 없다. "
                   f"등급을 분류하지 않고 기본값으로 판정하지 않는다")
        else:
            standard = class_limit["standard_pct"]
            ceiling = class_limit["hard_ceiling_pct"]
            if total_pct > ceiling + 1e-9:
                _issue(issues, "BLOCKED", "POSITION_HARD_CEILING",
                       f"거래 후 {risk_class} 비중 {total_pct:.2f}%가 절대 상한 "
                       f"{ceiling:.2f}%를 초과한다. 사유로도 해제되지 않는다")
            elif total_pct > standard + 1e-9:
                if concentration_override_reason:
                    _issue(issues, "CAUTION", "POSITION_LIMIT_OVERRIDDEN",
                           f"{risk_class} 비중 {total_pct:.2f}%가 표준 한도 "
                           f"{standard:.2f}%를 넘지만 사유가 기록됐다: "
                           f"{concentration_override_reason}")
                else:
                    _issue(issues, "BLOCKED", "POSITION_LIMIT_TOTAL_ASSETS",
                           f"거래 후 {risk_class} 비중 {total_pct:.2f}%가 표준 한도 "
                           f"{standard:.2f}%를 초과한다 (절대 상한 {ceiling:.2f}%). "
                           f"넘기려면 사유를 명시해야 한다")

        for denominator in ("equity_sleeve", "risk_assets"):
            limit = position_limits.get(f"{denominator}_pct")
            if limit is None:
                continue
            after_pct = after_weights[denominator]["position_pct"]
            if after_pct is None:
                _issue(issues, "BLOCKED", "DENOMINATOR_ZERO",
                       f"{denominator} 분모가 0이라 비중을 검증할 수 없다")
            elif after_pct > limit + 1e-9:
                _issue(issues, "BLOCKED", f"POSITION_LIMIT_{denominator.upper()}",
                       f"거래 후 {denominator} 기준 비중 {after_pct:.2f}%가 "
                       f"승인 한도 {limit:.2f}%를 초과한다")

    # 거래 후 행을 만들 때 현금을 함께 움직이지 않으면 분자와 분모가 어긋나
    # 통화 비중이 100%를 넘는 값이 나온다.
    cash_delta = -(gross + cost_total) if side == "buy" else (gross - cost_total)
    cash_before = before_denoms["cash"]
    after_rows = []
    for row in holdings:
        projected = dict(row)
        if row["asset_id"] == asset_id:
            projected["value_krw"] = max(0.0, after_position)
        elif row["asset_type"] in {"CASH", "CASH_EQUIVALENT"} and cash_before > 0:
            share = row["value_krw"] / cash_before
            projected["value_krw"] = max(0.0, row["value_krw"] + cash_delta * share)
        after_rows.append(projected)
    if all(row["asset_id"] != asset_id for row in holdings):
        after_rows.append({**target, "value_krw": max(0.0, after_position)})
    before_concentration = portfolio_concentration(holdings, before_denoms["total_assets"])
    after_concentration = portfolio_concentration(after_rows, after_denoms["total_assets"])

    if approved_ips and before_concentration and after_concentration:
        _check_portfolio_limits(
            issues, ips["portfolio_limits"], before_concentration, after_concentration)

    decision_basis = {
        "thesis": None,
        "counter_thesis": None,
        "expected_return": None,
        "portfolio_risk": {
            "before": before_weights,
            "after": after_weights,
        },
        "costs_and_tax": trade_costs,
        "alternatives": [],
    }
    if analysis and not validate_analysis(analysis):
        thesis = analysis["thesis"]
        expected = analysis["expected_return"]
        alternatives = analysis["alternatives"]
        decision_basis["thesis"] = thesis
        decision_basis["counter_thesis"] = analysis["counter_thesis"]
        decision_basis["expected_return"] = expected
        decision_basis["alternatives"] = alternatives

        if side == "buy" and thesis["invalidation_triggered"]:
            _issue(issues, "BLOCKED", "THESIS_INVALIDATED_FOR_BUY",
                   "무효화 조건이 발생한 종목은 신규·추가 매수할 수 없다")

        # 밸류에이션 가정이 승인 범위 안인지, 입력이 원문에서 왔는지.
        # 위험등급을 알아야 할인율 범위를 고를 수 있어 여기서 검사한다.
        if approved_ips and calculations_dir is not None:
            calculation = _check_valuation_policy(
                issues, ips, analysis, calculations_dir, risk_class)
            if calculation is not None:
                decision_basis["valuation_inputs"] = {
                    k: v for k, v in calculation.get("inputs", {}).items()
                    if k in ("discount_rate_pct", "terminal_growth_pct", "years",
                             "horizon_months", "fcf", "shares", "net_debt",
                             "input_provenance")
                }

        if approved_ips:
            policy = ips["decision_policy"]
            required = policy["required_expected_return_pct"]
            measure = policy["return_measure"]
            # 포지션과 대안을 같은 자로 잰다. 어느 자를 쓸지는 IPS가 정한다.
            position_return = (
                probability_weighted_return(expected)
                if measure == "PROBABILITY_WEIGHTED"
                else expected["scenarios"]["base"]["return_pct"]
            )
            bear_return = expected["scenarios"]["bear"]["return_pct"]
            decision_basis["return_measure"] = measure
            decision_basis["position_return_pct"] = position_return

            if abs(expected["required_return_pct"] - required) > 1e-9:
                _issue(issues, "BLOCKED", "RETURN_HURDLE_MISMATCH",
                       "분석의 요구수익률과 승인 IPS의 요구수익률이 다르다")
            if side == "buy" and position_return < required:
                severity = "BLOCKED" if policy["block_buy_below_required_return"] else "CAUTION"
                _issue(issues, severity, "EXPECTED_RETURN_BELOW_HURDLE",
                       f"기대수익 {position_return:.1f}%({measure})가 "
                       f"요구수익률 {required:.1f}%보다 낮다")

            # 안전마진 — 수렴 기간 가정 없이 '가치 대비 가격'만 잰다.
            #
            # 기대수익(연율)은 "몇 개월 안에 가치로 수렴한다"는 가정에 곱해져
            # 있어서, 주가가 가치와 같아도 큰 음수가 나온다. 안전마진은 그
            # 가정을 쓰지 않으므로 계측되지 않은 다이얼이 없다.
            if calculation is not None:
                scenarios = calculation.get("outputs", {}).get("scenarios", {})
                values = {
                    name: scenarios[name].get("value_per_share")
                    for name in ("bear", "base", "bull")
                    if isinstance(scenarios.get(name), dict)
                }
                probs = {
                    name: expected["scenarios"][name]["probability"]
                    for name in values
                    if name in expected.get("scenarios", {})
                }
                calc_price = calculation.get("inputs", {}).get("price")
                if len(values) == 3 and len(probs) == 3 and calc_price:
                    weighted_value = sum(values[n] * probs[n] for n in values)
                    margin = (weighted_value - calc_price) / weighted_value * 100.0
                    # 가치보다 비쌀 때 margin_pct 는 하한이 없다 (가치의 3배면
                    # -200%). 읽히는 값은 '가치의 몇 %에 거래되는가'다.
                    price_to_value = calc_price / weighted_value * 100.0
                    decision_basis["safety_margin"] = {
                        "weighted_value_per_share": round(weighted_value, 4),
                        "price": calc_price,
                        "margin_pct": round(margin, 2),
                        "price_pct_of_value": round(price_to_value, 1),
                        "by_scenario": {
                            n: round(calc_price / values[n] * 100.0, 1)
                            for n in values
                        },
                        "measure": "확률가중 내재가치 대비 가격 (수렴 기간 가정 없음). "
                                   "by_scenario 는 시나리오 가치의 몇 %에 거래되는가",
                    }
                    threshold = (policy.get("safety_margin") or {}).get(
                        "caution_below_pct")
                    if side == "buy" and isinstance(threshold, (int, float)) \
                            and margin < threshold:
                        _issue(issues, "CAUTION", "NO_SAFETY_MARGIN",
                               f"안전마진 없음 — 확률가중 내재가치 "
                               f"{weighted_value:,.0f} 의 {price_to_value:.0f}% 인 "
                               f"{calc_price:,.0f} 에 산다 "
                               f"(가치보다 {price_to_value - 100:.0f}% 비싸다)")

            # 하방 예산 — 약세 시나리오가 실현될 때 이 포지션 하나가 총자산에
            # 입히는 손실. base 만 보면 하방 -50%와 -5%가 똑같이 통과한다.
            bear_budget = policy["max_bear_loss_contribution_pct"]
            after_total = after_denoms["total_assets"]
            if bear_return < 0 and after_total > 0:
                bear_loss_pct = after_position * (-bear_return / 100.0) / after_total * 100.0
                decision_basis["bear_loss_contribution_pct"] = bear_loss_pct
                if bear_loss_pct > bear_budget + 1e-9:
                    _issue(issues, "BLOCKED", "BEAR_LOSS_EXCEEDS_BUDGET",
                           f"거래 후 약세 시나리오({bear_return:.1f}%)에서 이 포지션의 "
                           f"손실이 총자산의 {bear_loss_pct:.2f}%다. "
                           f"승인 하방 예산 {bear_budget:.2f}%를 초과한다")
            else:
                decision_basis["bear_loss_contribution_pct"] = 0.0

            comparable = [
                item for item in alternatives if item["measure"] == measure
            ]
            for item in alternatives:
                if item["measure"] != measure:
                    _issue(issues, "CAUTION", "ALTERNATIVE_MEASURE_MISMATCH",
                           f"대안 {item['instrument_id']}은 {item['measure']} 기준이라 "
                           f"{measure} 기준의 이 포지션과 비교할 수 없다")
            best_alternative = max(
                comparable,
                key=lambda item: item["expected_return_pct"],
                default=None,
            )
            advantage = policy["alternative_min_advantage_pct"]
            if best_alternative:
                gap = best_alternative["expected_return_pct"] - position_return
                if side == "buy" and gap >= advantage:
                    _issue(issues, "CAUTION", "ALTERNATIVE_DOMINATES",
                           f"대안 {best_alternative['instrument_id']}의 기대수익이 "
                           f"{gap:.1f}%p 높다")

            if side == "sell":
                sell_drivers = []
                if thesis["invalidation_triggered"] or thesis["change"] in {
                    "POSSIBLY_INVALIDATE", "INVALIDATE"
                }:
                    sell_drivers.append("논지 훼손")
                if position_return < required:
                    sell_drivers.append("기대수익 하한 미달")
                # 하방 예산을 넘긴 포지션을 줄이는 것은 그 자체로 매도 근거다.
                before_total = before_denoms["total_assets"]
                if bear_return < 0 and before_total > 0:
                    before_bear_loss = (
                        target["value_krw"] * (-bear_return / 100.0)
                        / before_total * 100.0
                    )
                    if before_bear_loss > bear_budget + 1e-9:
                        sell_drivers.append("하방 예산 초과 축소")
                denominator_limits = _denominator_limits(ips, risk_class)
                if any(
                    limit is not None
                    and before_weights[key]["position_pct"] is not None
                    and before_weights[key]["position_pct"] > limit
                    and (
                        after_weights[key]["position_pct"] is None
                        or after_weights[key]["position_pct"] < before_weights[key]["position_pct"]
                    )
                    for key, limit in denominator_limits.items()
                ):
                    sell_drivers.append("집중도 축소")
                if best_alternative and (
                    best_alternative["expected_return_pct"] - position_return >= advantage
                ):
                    sell_drivers.append("대안 우위")
                decision_basis["sell_drivers"] = sell_drivers
                if not sell_drivers:
                    _issue(issues, "CAUTION", "SELL_DRIVER_NOT_ESTABLISHED",
                           "논지·기대수익·집중도·대안에서 매도 근거가 확인되지 않았다")

    # 성립하지 않는 주문의 "거래 후" 숫자는 의미가 없다. -905주나 970조 같은
    # 값을 출력하면 차단 사유보다 그 숫자가 먼저 읽힌다.
    blocking_codes = {issue["code"] for issue in issues}
    projection_reliable = not (
        blocking_codes & {
            "INVALID_QUANTITY", "INVALID_PRICE", "INVALID_SIDE", "OVERSELL",
            "POSITION_NOT_FOUND", "POSITION_QUANTITY_UNKNOWN", "HOLDINGS_INVALID",
        }
    )

    verdict = _verdict(issues)
    snapshot_payload = {
        "holdings": holdings,
        "holdings_meta": holdings_meta,
        "ips": ips,
        "analysis": analysis,
        "trade": {
            "asset_id": asset_id,
            "side": side,
            "quantity": quantity if quantity_valid else str(quantity),
            "price": price if price_valid else str(price),
            "price_as_of": price_as_of,
        },
    }
    result = {
        "metadata": {
            "schema_version": RESULT_SCHEMA_VERSION,
            "engine_version": ENGINE_VERSION,
            "evaluated_at": evaluated_at.isoformat(timespec="seconds"),
            "decision_snapshot_id": stable_hash(snapshot_payload, "TRADE-SNAPSHOT-"),
            "holdings_as_of": holdings_meta.get("as_of"),
            "ips_id": ips.get("ips_id") if ips else None,
            "analysis_id": analysis.get("analysis_id") if analysis else None,
            "analysis_snapshot_id": (
                analysis.get("snapshot", {}).get("snapshot_id") if analysis else None
            ),
        },
        "trade": {
            "asset_id": asset_id,
            "name": target["name"],
            "side": side.upper(),
            "quantity": quantity if quantity_valid else None,
            "quantity_input": str(quantity),
            "price_krw": price if price_valid else None,
            "price_input": str(price),
            "gross_krw": gross,
            "held_quantity_before": held_quantity,
            "held_quantity_after": (
                held_quantity + quantity
                if projection_reliable and quantity_valid and side == "buy"
                and held_quantity is not None
                else held_quantity - quantity
                if projection_reliable and quantity_valid and held_quantity is not None
                else None
            ),
        },
        "decision": {
            "verdict": verdict,
            "meaning": {
                "BLOCKED": "안전·정책·데이터 위반으로 거래 금지",
                "RESEARCH_REQUIRED": "승인 정책 또는 검증 분석이 부족해 판정 보류",
                "CAUTION": "거래 가능성은 있으나 근거·기회비용 재검토 필요",
                "PROCEED": "승인 정책과 검증 분석상 차단 사유 없음",
            }[verdict],
        },
        "denominators": {
            "definitions": {
                "total_assets": "현금을 포함한 전체 평가자산",
                "equity_sleeve": "STOCK·ETF·REIT 평가자산",
                "risk_assets": "CASH·CASH_EQUIVALENT·BOND를 제외한 평가자산",
            },
            "before_krw": before_denoms,
            "after_krw": after_denoms,
        },
        "weights": {"before": before_weights, "after": after_weights},
        "projection_reliable": projection_reliable,
        "concentration": {
            "before": before_concentration,
            "after": after_concentration,
            "note": "ETF 룩스루 미반영 직접 비중. 섹터 비중은 하한이며 "
                    "ETF가 품은 섹터는 미분류로 격리된다",
        },
        "risk_class": risk_class,
        "price_reference": price_reference,
        "decision_basis": decision_basis,
        "issues": sorted(issues, key=lambda i: -VERDICT_ORDER[i["severity"]]),
    }
    return result


def render_text(result: dict[str, Any]) -> str:
    out = []
    add = out.append
    trade = result["trade"]
    decision = result["decision"]
    quantity_display = (
        f"{trade['quantity']:,.4g}" if trade["quantity"] is not None
        else trade["quantity_input"]
    )
    price_display = (
        f"{trade['price_krw']:,.0f}" if trade["price_krw"] is not None
        else trade["price_input"]
    )
    add("=" * 78)
    add(f"매매 점검 — {trade['name']} ({trade['asset_id']})")
    add("=" * 78)
    reliable = result["projection_reliable"]
    after_quantity = (
        trade["held_quantity_after"] if reliable else "산출 불가"
    )
    add(f"판정          {decision['verdict']} — {decision['meaning']}")
    add(f"거래          {trade['side']} {quantity_display}주 @ {price_display} KRW")
    add(f"위험등급      {result['risk_class'] or '미지정'}")
    add(f"보유수량      {trade['held_quantity_before']} → {after_quantity}")
    add("")
    add("[ 비중 — 분모를 섞지 않는다 ]")
    labels = {
        "total_assets": "총자산",
        "equity_sleeve": "주식 슬리브",
        "risk_assets": "위험자산",
    }
    if not reliable:
        add("  주문이 성립하지 않아 거래 후 비중을 산출하지 않는다.")
    for key, label in labels.items():
        before = result["weights"]["before"][key]
        after = result["weights"]["after"][key]
        bp = "N/A" if before["position_pct"] is None else f"{before['position_pct']:.2f}%"
        if not reliable:
            add(f"  {label:<12}{bp:>9} → {'—':>9}  "
                f"(분모 {before['denominator_krw']:,.0f} KRW)")
            continue
        ap = "N/A" if after["position_pct"] is None else f"{after['position_pct']:.2f}%"
        add(f"  {label:<12}{bp:>9} → {ap:>9}  "
            f"(분모 {before['denominator_krw']:,.0f} → {after['denominator_krw']:,.0f} KRW)")
    add("")
    concentration = result["concentration"]
    if concentration["before"] and concentration["after"]:
        add("[ 포트폴리오 집중도 — 총자산 대비 직접 비중 ]")
        before_c, after_c = concentration["before"], concentration["after"]
        pairs = [
            ("상위 10종목", before_c["top10_pct"], after_c["top10_pct"]),
            ("섹터 미분류", before_c["unclassified_pct"], after_c["unclassified_pct"]),
        ]
        for name in sorted(set(before_c["sector_pct"]) | set(after_c["sector_pct"])):
            pairs.append((f"섹터 {name}", before_c["sector_pct"].get(name, 0.0),
                          after_c["sector_pct"].get(name, 0.0)))
        for code in sorted(set(before_c["currency_pct"]) | set(after_c["currency_pct"])):
            pairs.append((f"통화 {code}", before_c["currency_pct"].get(code, 0.0),
                          after_c["currency_pct"].get(code, 0.0)))
        for name, before_pct, after_pct in pairs:
            tail = f"{after_pct:>8.2f}%" if reliable else f"{'—':>9}"
            add(f"  {name:<16}{before_pct:>8.2f}% → {tail}")
        add(f"  ※ {concentration['note']}")
        add("")
    add("[ 예정가 검증 ]")
    reference = result["price_reference"]
    labels_source = {
        "HOLDINGS_IMPLIED": "보유내역 내재단가",
        "PRICE_SNAPSHOT": "시세 스냅샷",
        "SNAPSHOT_CURRENCY_MISMATCH": "대조 불가",
        "NONE": "대조 불가",
    }
    add(f"  기준가 출처  {labels_source.get(reference['source'], reference['source'])}"
        f" — {reference['detail']}")
    if reference["price_krw"] is None:
        add("  기준가       없음 — 예정가를 검증하지 못했다")
    else:
        age = reference.get("age_days")
        add(f"  기준가       {reference['price_krw']:,.0f} KRW"
            + (f"  (기준일 {reference['as_of']}"
               + (f", {age}일 경과)" if age is not None else ")")
               if reference["as_of"] else ""))
        add(f"  괴리         {reference['deviation_pct']:.1f}%"
            + ("" if reference["max_deviation_pct"] is None
               else f"  (허용 {reference['max_deviation_pct']:.1f}%)"))
    add("")
    basis = result["decision_basis"]
    expected = basis.get("expected_return")
    if expected:
        add("[ 기대수익 — 시나리오와 하방 ]")
        scenarios = expected["scenarios"]
        add(f"  {'':<8}{'확률':>8}{'연율':>10}   근거")
        for name in ("bear", "base", "bull"):
            entry = scenarios[name]
            add(f"  {name:<8}{entry['probability']:>8.0%}"
                f"{entry['return_pct']:>+9.1f}%   "
                f"{', '.join(entry['claim_ids'])}")
        measure = basis.get("return_measure")
        position_return = basis.get("position_return_pct")
        if position_return is not None:
            add(f"  판정에 쓴 값  {position_return:+.1f}%  ({measure})"
                f"   요구 {expected['required_return_pct']:+.1f}%")
        bear_loss = basis.get("bear_loss_contribution_pct")
        if bear_loss is not None:
            add(f"  약세 손실     총자산의 {bear_loss:.2f}%")
        add(f"  기간 {expected['horizon_months']}개월 / {expected['methodology']}")
        add(f"  계산 {expected['calculation_id']}")
        add("")
        counter = basis.get("counter_thesis")
        if counter:
            add("[ 가장 강한 반대 근거 ]")
            add(f"  {counter['text']}")
            add(f"  반증 신호  {counter['invalidation_signal']}")
            add(f"  근거       {', '.join(counter['claim_ids'])}")
            add("")
        alternatives = basis.get("alternatives") or []
        if alternatives:
            add("[ 대안 ]")
            for item in alternatives:
                add(f"  {item['instrument_id']:<16}"
                    f"{item['expected_return_pct']:>+8.1f}%  ({item['measure']})")
            add("")
        drivers = basis.get("sell_drivers")
        if drivers is not None:
            add("[ 매도 근거 ]")
            add("  " + (" / ".join(drivers) if drivers else "확인되지 않음"))
            add("")
    add("[ 비용·세금 추정 ]")
    costs = result["decision_basis"]["costs_and_tax"]
    if costs["total_krw"] is None:
        add("  승인 IPS가 없어 계산하지 않음")
    else:
        add(f"  수수료 {costs['commission_krw']:,.0f} / 세금 {costs['tax_krw']:,.0f} / "
            f"슬리피지 {costs['slippage_krw']:,.0f} / 환전 {costs['fx_cost_krw']:,.0f} KRW")
        add(f"  합계 {costs['total_krw']:,.0f} KRW")
    add("")
    add("[ 판정 근거 ]")
    if not result["issues"]:
        add("  차단 또는 주의 사유 없음")
    for issue in result["issues"]:
        add(f"  {issue['severity']:<18}{issue['code']}: {issue['message']}")
    add("")
    add(f"결정 스냅샷  {result['metadata']['decision_snapshot_id']}")
    add("※ PROCEED는 수익 보장이 아니라 정책·근거 계약상 차단 사유가 없다는 뜻이다.")
    return "\n".join(out)


def main():
    p = argparse.ArgumentParser(description="구조화된 매매 전 점검")
    p.add_argument("asset_id")
    p.add_argument("side", choices=["buy", "sell"])
    p.add_argument("quantity", type=float)
    p.add_argument("--price", type=float, required=True, help="예정 체결가, KRW 환산")
    p.add_argument("--price-as-of", help="예정가 기준시각 ISO-8601, 시간대 필수")
    p.add_argument("--holdings", default=str(DEFAULT_HOLDINGS))
    p.add_argument("--ips", default=str(DEFAULT_IPS))
    p.add_argument("--analysis", help="검증된 주장·출처·스냅샷 분석 JSON")
    p.add_argument("--calculations", default=str(DEFAULT_CALCULATIONS),
                   help="계산 산출물 저장소. 기대수익이 여기 산출물과 일치하는지 대조한다")
    p.add_argument("--name", help="신규 종목명")
    p.add_argument("--asset-type", default="STOCK")
    p.add_argument("--market", default="KR")
    p.add_argument("--risk-class",
                   help="신규 종목 위험등급 (STOCK / BROAD_ETF / DERIVATIVE_ETF 등). "
                        "생략하면 --asset-type을 쓴다")
    p.add_argument("--evaluated-at", help="재현 테스트용 평가시각 ISO-8601")
    p.add_argument("--price-unverified-reason",
                   help="대조할 기준가가 없을 때 예정가를 받아들이는 사유. "
                        "괴리 초과는 이 사유로 해제되지 않는다")
    p.add_argument("--concentration-override-reason",
                   help="표준 한도를 넘겨 집중하는 사유. 절대 상한은 해제되지 않는다")
    p.add_argument("--json", action="store_true")
    a = p.parse_args()

    evaluated_at = None
    if a.evaluated_at:
        errors: list[str] = []
        evaluated_at = parse_datetime(a.evaluated_at, "evaluated_at", errors)
        if errors:
            for error in errors:
                print(error, file=sys.stderr)
            return 2

    result = evaluate_trade(
        a.asset_id,
        a.side,
        a.quantity,
        a.price,
        holdings_path=a.holdings,
        ips_path=a.ips,
        analysis_path=a.analysis,
        calculations_dir=a.calculations,
        price_as_of=a.price_as_of,
        evaluated_at=evaluated_at,
        new_asset_name=a.name,
        new_asset_type=a.asset_type,
        new_asset_market=a.market,
        new_asset_risk_class=a.risk_class,
        price_unverified_reason=a.price_unverified_reason,
        concentration_override_reason=a.concentration_override_reason,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2) if a.json else render_text(result))
    return exit_code_for_verdict(result["decision"]["verdict"])


if __name__ == "__main__":
    raise SystemExit(main())
