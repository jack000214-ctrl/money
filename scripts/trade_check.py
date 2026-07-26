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
    load_json,
    parse_datetime,
    stable_hash,
    validate_analysis,
    validate_ips,
)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_HOLDINGS = ROOT / "knowledge" / "portfolio" / "holdings.csv"
DEFAULT_IPS = ROOT / "knowledge" / "portfolio" / "ips.json"
ENGINE_VERSION = "2.0.0"
RESULT_SCHEMA_VERSION = "1.0.0"

EQUITY_TYPES = {"STOCK", "ETF", "REIT"}
NON_RISK_TYPES = {"CASH", "CASH_EQUIVALENT", "BOND"}
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
    if not ips or ips.get("status") != "APPROVED":
        return {
            "commission_krw": None,
            "tax_krw": None,
            "slippage_krw": None,
            "fx_cost_krw": None,
            "total_krw": None,
        }
    policy = ips["costs"]
    commission_bps = policy["buy_commission_bps"] if side == "buy" else policy["sell_commission_bps"]
    tax_bps = 0 if side == "buy" else policy["sell_tax_bps"]
    fx_bps = policy["fx_cost_bps"] if market and market != "KR" else 0
    values = {
        "commission_krw": gross * commission_bps / 10000,
        "tax_krw": gross * tax_bps / 10000,
        "slippage_krw": gross * policy["slippage_bps"] / 10000,
        "fx_cost_krw": gross * fx_bps / 10000,
    }
    values["total_krw"] = sum(values.values())
    return values


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

    if price_as_of is None:
        approved_ips = ips is not None and not validate_ips(ips) \
            and ips.get("status") == "APPROVED"
        _issue(issues, "BLOCKED" if approved_ips else "RESEARCH_REQUIRED",
               "PRICE_TIMESTAMP_MISSING",
               "예정가의 기준시각이 없어 신선도를 판단할 수 없다")
        parsed_price_at = None
    else:
        price_errors: list[str] = []
        parsed_price_at = parse_datetime(price_as_of, "price_as_of", price_errors)
        for message in price_errors:
            _issue(issues, "BLOCKED", "PRICE_TIMESTAMP_INVALID", message)

    if ips and not validate_ips(ips) and ips.get("status") == "APPROVED":
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

    gross = quantity * price if quantity_valid and price_valid else 0.0
    trade_costs = _costs(ips, side, gross, target["market"])
    cost_total = trade_costs["total_krw"] or 0.0
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

    if ips and not validate_ips(ips) and ips.get("status") == "APPROVED":
        limit_map = {
            "total_assets": ips["position_limits"]["total_assets_pct"],
            "equity_sleeve": ips["position_limits"]["equity_sleeve_pct"],
            "risk_assets": ips["position_limits"]["risk_assets_pct"],
        }
        for denominator, limit in limit_map.items():
            after_pct = after_weights[denominator]["position_pct"]
            if after_pct is None:
                _issue(issues, "BLOCKED", "DENOMINATOR_ZERO",
                       f"{denominator} 분모가 0이라 비중을 검증할 수 없다")
            elif after_pct > limit + 1e-9:
                _issue(issues, "BLOCKED", f"POSITION_LIMIT_{denominator.upper()}",
                       f"거래 후 {denominator} 기준 비중 {after_pct:.2f}%가 "
                       f"승인 한도 {limit:.2f}%를 초과한다")

    decision_basis = {
        "thesis": None,
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
        decision_basis["expected_return"] = expected
        decision_basis["alternatives"] = alternatives

        if side == "buy" and thesis["invalidation_triggered"]:
            _issue(issues, "BLOCKED", "THESIS_INVALIDATED_FOR_BUY",
                   "무효화 조건이 발생한 종목은 신규·추가 매수할 수 없다")

        if ips and not validate_ips(ips) and ips.get("status") == "APPROVED":
            policy = ips["decision_policy"]
            required = policy["required_expected_return_pct"]
            if abs(expected["required_return_pct"] - required) > 1e-9:
                _issue(issues, "BLOCKED", "RETURN_HURDLE_MISMATCH",
                       "분석의 요구수익률과 승인 IPS의 요구수익률이 다르다")
            if side == "buy" and expected["base_pct"] < required:
                severity = "BLOCKED" if policy["block_buy_below_required_return"] else "CAUTION"
                _issue(issues, severity, "EXPECTED_RETURN_BELOW_HURDLE",
                       f"기준 시나리오 기대수익 {expected['base_pct']:.1f}%가 "
                       f"요구수익률 {required:.1f}%보다 낮다")

            best_alternative = max(
                alternatives,
                key=lambda item: item["base_expected_return_pct"],
                default=None,
            )
            advantage = policy["alternative_min_advantage_pct"]
            if best_alternative:
                gap = best_alternative["base_expected_return_pct"] - expected["base_pct"]
                if side == "buy" and gap >= advantage:
                    _issue(issues, "CAUTION", "ALTERNATIVE_DOMINATES",
                           f"대안 {best_alternative['instrument_id']}의 기준 기대수익이 "
                           f"{gap:.1f}%p 높다")

            if side == "sell":
                sell_drivers = []
                if thesis["invalidation_triggered"] or thesis["change"] in {
                    "POSSIBLY_INVALIDATE", "INVALIDATE"
                }:
                    sell_drivers.append("논지 훼손")
                if expected["base_pct"] < required:
                    sell_drivers.append("기대수익 하한 미달")
                if any(
                    before_weights[key]["position_pct"] is not None
                    and before_weights[key]["position_pct"] > ips["position_limits"][f"{key}_pct"]
                    and (
                        after_weights[key]["position_pct"] is None
                        or after_weights[key]["position_pct"] < before_weights[key]["position_pct"]
                    )
                    for key in ("total_assets", "equity_sleeve", "risk_assets")
                ):
                    sell_drivers.append("집중도 축소")
                if best_alternative and (
                    best_alternative["base_expected_return_pct"] - expected["base_pct"] >= advantage
                ):
                    sell_drivers.append("대안 우위")
                decision_basis["sell_drivers"] = sell_drivers
                if not sell_drivers:
                    _issue(issues, "CAUTION", "SELL_DRIVER_NOT_ESTABLISHED",
                           "논지·기대수익·집중도·대안에서 매도 근거가 확인되지 않았다")

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
                if quantity_valid and side == "buy" and held_quantity is not None
                else held_quantity - quantity
                if quantity_valid and held_quantity is not None
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
    add(f"판정          {decision['verdict']} — {decision['meaning']}")
    add(f"거래          {trade['side']} {quantity_display}주 @ {price_display} KRW")
    add(f"보유수량      {trade['held_quantity_before']} → {trade['held_quantity_after']}")
    add("")
    add("[ 비중 — 분모를 섞지 않는다 ]")
    labels = {
        "total_assets": "총자산",
        "equity_sleeve": "주식 슬리브",
        "risk_assets": "위험자산",
    }
    for key, label in labels.items():
        before = result["weights"]["before"][key]
        after = result["weights"]["after"][key]
        bp = "N/A" if before["position_pct"] is None else f"{before['position_pct']:.2f}%"
        ap = "N/A" if after["position_pct"] is None else f"{after['position_pct']:.2f}%"
        add(f"  {label:<12}{bp:>9} → {ap:>9}  "
            f"(분모 {before['denominator_krw']:,.0f} → {after['denominator_krw']:,.0f} KRW)")
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
    p.add_argument("--name", help="신규 종목명")
    p.add_argument("--asset-type", default="STOCK")
    p.add_argument("--market", default="KR")
    p.add_argument("--evaluated-at", help="재현 테스트용 평가시각 ISO-8601")
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
        price_as_of=a.price_as_of,
        evaluated_at=evaluated_at,
        new_asset_name=a.name,
        new_asset_type=a.asset_type,
        new_asset_market=a.market,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2) if a.json else render_text(result))
    return exit_code_for_verdict(result["decision"]["verdict"])


if __name__ == "__main__":
    raise SystemExit(main())
