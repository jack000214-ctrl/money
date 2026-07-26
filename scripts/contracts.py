#!/usr/bin/env python3
"""투자정책과 리서치 분석의 구조화 계약.

외부 패키지 없이도 저장 직전에 필수 필드, ID 참조, 시간, 숫자 범위를
검증한다. 검증에 실패한 분석은 파일로 쓰지 않는다.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0.0"
IPS_STATUSES = {"DRAFT", "APPROVED", "SUPERSEDED"}
CLAIM_TYPES = {"FACT", "DERIVED", "INFERENCE", "HYPOTHESIS", "UNKNOWN"}
THESIS_STATUSES = {"DRAFT", "REVIEWED", "APPROVED", "INVALIDATED"}
THESIS_CHANGES = {"STRENGTHEN", "NEUTRAL", "WEAKEN", "POSSIBLY_INVALIDATE", "INVALIDATE"}
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class ContractError(ValueError):
    """구조화 계약 위반."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


def _duplicate_guard(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ContractError([f"중복 JSON 키: {key}"])
        out[key] = value
    return out


def load_json(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f, object_pairs_hook=_duplicate_guard)
    except FileNotFoundError:
        raise
    except json.JSONDecodeError as exc:
        raise ContractError([f"{path}: JSON 파싱 실패 ({exc.msg}, {exc.lineno}행)"]) from exc
    if not isinstance(data, dict):
        raise ContractError([f"{path}: 최상위 값은 객체여야 한다"])
    return data


def content_hash(path: str | Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def stable_hash(value: Any, prefix: str = "") -> str:
    payload = json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        allow_nan=False,
    )
    return prefix + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def parse_date(value: Any, field: str, errors: list[str]) -> date | None:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{field}: YYYY-MM-DD 문자열이 필요하다")
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        errors.append(f"{field}: 올바른 날짜가 아니다 ({value!r})")
        return None


def parse_datetime(value: Any, field: str, errors: list[str]) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{field}: ISO-8601 시각 문자열이 필요하다")
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        errors.append(f"{field}: 올바른 ISO-8601 시각이 아니다 ({value!r})")
        return None
    if parsed.tzinfo is None:
        errors.append(f"{field}: 시간대가 포함돼야 한다")
        return None
    return parsed.astimezone(timezone.utc)


def _number(value: Any, field: str, errors: list[str], *,
            minimum: float | None = None, maximum: float | None = None,
            allow_null: bool = False) -> float | None:
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


def _nonempty(value: Any, field: str, errors: list[str]) -> str | None:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{field}: 비어 있지 않은 문자열이 필요하다")
        return None
    return value.strip()


def validate_ips(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if data.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version: {SCHEMA_VERSION} 이어야 한다")
    _nonempty(data.get("ips_id"), "ips_id", errors)
    status = data.get("status")
    if status not in IPS_STATUSES:
        errors.append(f"status: {', '.join(sorted(IPS_STATUSES))} 중 하나여야 한다")
    _nonempty(data.get("base_currency"), "base_currency", errors)

    limits = data.get("position_limits")
    if not isinstance(limits, dict):
        errors.append("position_limits: 객체가 필요하다")
        limits = {}
    for key in ("equity_sleeve_pct", "risk_assets_pct"):
        _number(limits.get(key), f"position_limits.{key}", errors,
                minimum=0, maximum=100, allow_null=True)

    by_class = limits.get("by_risk_class")
    if not isinstance(by_class, dict):
        errors.append("position_limits.by_risk_class: 위험등급별 한도 객체가 필요하다")
    elif status == "APPROVED" and not by_class:
        errors.append("position_limits.by_risk_class: 승인 IPS는 위험등급을 "
                      "하나 이상 정의해야 한다")
    else:
        for name, entry in by_class.items():
            field = f"position_limits.by_risk_class.{name}"
            if not _nonempty(name, f"{field}: 등급명", errors):
                continue
            if not isinstance(entry, dict):
                errors.append(f"{field}: 객체가 필요하다")
                continue
            standard = _number(entry.get("standard_pct"), f"{field}.standard_pct",
                               errors, minimum=0, maximum=100,
                               allow_null=status != "APPROVED")
            ceiling = _number(entry.get("hard_ceiling_pct"), f"{field}.hard_ceiling_pct",
                              errors, minimum=0, maximum=100,
                              allow_null=status != "APPROVED")
            if standard is not None and ceiling is not None and standard > ceiling:
                errors.append(f"{field}: standard_pct는 hard_ceiling_pct보다 클 수 없다")

    portfolio = data.get("portfolio_limits")
    if not isinstance(portfolio, dict):
        errors.append("portfolio_limits: 객체가 필요하다")
        portfolio = {}
    for key in ("top10_total_assets_pct", "sector_total_assets_pct",
                "currency_total_assets_pct", "unclassified_total_assets_pct"):
        _number(portfolio.get(key), f"portfolio_limits.{key}", errors,
                minimum=0, maximum=100, allow_null=True)

    freshness = data.get("data_freshness")
    if not isinstance(freshness, dict):
        errors.append("data_freshness: 객체가 필요하다")
        freshness = {}
    for key in ("holdings_max_age_days", "analysis_max_age_days", "price_max_age_minutes"):
        value = freshness.get(key)
        number = _number(value, f"data_freshness.{key}", errors,
                         minimum=0, allow_null=status != "APPROVED")
        if number is not None and not number.is_integer():
            errors.append(f"data_freshness.{key}: 정수여야 한다")

    quality = data.get("data_quality")
    if not isinstance(quality, dict):
        errors.append("data_quality: 객체가 필요하다")
        quality = {}
    _number(quality.get("price_max_deviation_pct"),
            "data_quality.price_max_deviation_pct", errors,
            minimum=0, maximum=100, allow_null=status != "APPROVED")
    reference_age = _number(quality.get("price_reference_max_age_days"),
                            "data_quality.price_reference_max_age_days", errors,
                            minimum=0, allow_null=status != "APPROVED")
    if reference_age is not None and not reference_age.is_integer():
        errors.append("data_quality.price_reference_max_age_days: 정수여야 한다")

    costs = data.get("costs")
    if not isinstance(costs, dict):
        errors.append("costs: 객체가 필요하다")
        costs = {}
    by_market = costs.get("by_market")
    if not isinstance(by_market, dict):
        errors.append("costs.by_market: 시장별 비용 객체가 필요하다")
    elif status == "APPROVED" and not by_market:
        errors.append("costs.by_market: 승인 IPS는 시장을 하나 이상 정의해야 한다")
    else:
        for market, entry in by_market.items():
            field = f"costs.by_market.{market}"
            if not _nonempty(market, f"{field}: 시장코드", errors):
                continue
            if not isinstance(entry, dict):
                errors.append(f"{field}: 객체가 필요하다")
                continue
            for key in ("buy_commission_bps", "sell_commission_bps", "sell_tax_bps",
                        "fx_cost_bps", "slippage_bps"):
                _number(entry.get(key), f"{field}.{key}", errors,
                        minimum=0, allow_null=status != "APPROVED")

    policy = data.get("decision_policy")
    if not isinstance(policy, dict):
        errors.append("decision_policy: 객체가 필요하다")
        policy = {}
    _number(policy.get("required_expected_return_pct"),
            "decision_policy.required_expected_return_pct", errors,
            minimum=-100, allow_null=status != "APPROVED")
    _number(policy.get("alternative_min_advantage_pct"),
            "decision_policy.alternative_min_advantage_pct", errors,
            minimum=0, allow_null=status != "APPROVED")
    block_below = policy.get("block_buy_below_required_return")
    if not isinstance(block_below, bool) and not (
            status != "APPROVED" and block_below is None):
        errors.append("decision_policy.block_buy_below_required_return: boolean이 필요하다")

    if status == "APPROVED":
        _nonempty(data.get("approved_by"), "approved_by", errors)
        parse_datetime(data.get("approved_at"), "approved_at", errors)
    return errors


def require_valid_ips(data: dict[str, Any]) -> None:
    errors = validate_ips(data)
    if errors:
        raise ContractError(errors)


def _id_list(value: Any, field: str, errors: list[str], *, allow_empty=False) -> list[str]:
    if not isinstance(value, list):
        errors.append(f"{field}: ID 배열이 필요하다")
        return []
    result = []
    seen = set()
    for idx, item in enumerate(value):
        identifier = _nonempty(item, f"{field}[{idx}]", errors)
        if identifier:
            if identifier in seen:
                errors.append(f"{field}[{idx}]: 중복 ID {identifier}")
            seen.add(identifier)
            result.append(identifier)
    if not allow_empty and not result:
        errors.append(f"{field}: 하나 이상의 ID가 필요하다")
    return result


def validate_analysis(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if data.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version: {SCHEMA_VERSION} 이어야 한다")
    _nonempty(data.get("analysis_id"), "analysis_id", errors)
    _nonempty(data.get("instrument_id"), "instrument_id", errors)
    _nonempty(data.get("company_id"), "company_id", errors)
    analysis_at = parse_datetime(data.get("as_of"), "as_of", errors)

    snapshot = data.get("snapshot")
    if not isinstance(snapshot, dict):
        errors.append("snapshot: 객체가 필요하다")
        snapshot = {}
    _nonempty(snapshot.get("snapshot_id"), "snapshot.snapshot_id", errors)
    snapshot_at = parse_datetime(snapshot.get("created_at"), "snapshot.created_at", errors)
    snapshot_source_ids = _id_list(
        snapshot.get("source_ids"), "snapshot.source_ids", errors)
    input_hashes = snapshot.get("input_hashes")
    if not isinstance(input_hashes, dict) or not input_hashes:
        errors.append("snapshot.input_hashes: 하나 이상의 입력 해시가 필요하다")
    else:
        for key, digest in input_hashes.items():
            if not isinstance(key, str) or not key.strip():
                errors.append("snapshot.input_hashes: 키는 비어 있지 않은 문자열이어야 한다")
            if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
                errors.append("snapshot.input_hashes: 값은 64자리 16진수 SHA-256이어야 한다")
                break
    if analysis_at and snapshot_at and snapshot_at < analysis_at:
        errors.append("snapshot.created_at: analysis.as_of보다 빠를 수 없다")

    sources = data.get("sources")
    if not isinstance(sources, list) or not sources:
        errors.append("sources: 하나 이상의 출처가 필요하다")
        sources = []
    source_ids: set[str] = set()
    for idx, source in enumerate(sources):
        field = f"sources[{idx}]"
        if not isinstance(source, dict):
            errors.append(f"{field}: 객체가 필요하다")
            continue
        source_id = _nonempty(source.get("source_id"), f"{field}.source_id", errors)
        if source_id:
            if source_id in source_ids:
                errors.append(f"{field}.source_id: 중복 ID {source_id}")
            source_ids.add(source_id)
        _nonempty(source.get("source_type"), f"{field}.source_type", errors)
        _nonempty(source.get("locator"), f"{field}.locator", errors)
        retrieved_at = parse_datetime(
            source.get("retrieved_at"), f"{field}.retrieved_at", errors)
        published_at = None
        if source.get("published_at") is not None:
            published_at = parse_datetime(
                source.get("published_at"), f"{field}.published_at", errors)
        if published_at and retrieved_at and published_at > retrieved_at:
            errors.append(f"{field}.published_at: retrieved_at보다 늦을 수 없다")
        if retrieved_at and snapshot_at and retrieved_at > snapshot_at:
            errors.append(f"{field}.retrieved_at: snapshot.created_at보다 늦을 수 없다")
        digest = source.get("content_hash")
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            errors.append(f"{field}.content_hash: 64자리 16진수 SHA-256이 필요하다")

    claims = data.get("claims")
    if not isinstance(claims, list) or not claims:
        errors.append("claims: 하나 이상의 주장이 필요하다")
        claims = []
    claim_ids: set[str] = set()
    claim_source_refs: dict[str, list[str]] = {}
    for idx, claim in enumerate(claims):
        field = f"claims[{idx}]"
        if not isinstance(claim, dict):
            errors.append(f"{field}: 객체가 필요하다")
            continue
        claim_id = _nonempty(claim.get("claim_id"), f"{field}.claim_id", errors)
        if claim_id:
            if claim_id in claim_ids:
                errors.append(f"{field}.claim_id: 중복 ID {claim_id}")
            claim_ids.add(claim_id)
        if claim.get("type") not in CLAIM_TYPES:
            errors.append(f"{field}.type: {', '.join(sorted(CLAIM_TYPES))} 중 하나여야 한다")
        _nonempty(claim.get("text"), f"{field}.text", errors)
        refs = _id_list(claim.get("source_ids"), f"{field}.source_ids", errors)
        if claim_id:
            claim_source_refs[claim_id] = refs

    thesis = data.get("thesis")
    if not isinstance(thesis, dict):
        errors.append("thesis: 객체가 필요하다")
        thesis = {}
    if thesis.get("status") not in THESIS_STATUSES:
        errors.append(f"thesis.status: {', '.join(sorted(THESIS_STATUSES))} 중 하나여야 한다")
    if thesis.get("change") not in THESIS_CHANGES:
        errors.append(f"thesis.change: {', '.join(sorted(THESIS_CHANGES))} 중 하나여야 한다")
    thesis_status = thesis.get("status")
    thesis_change = thesis.get("change")
    invalidation_triggered = thesis.get("invalidation_triggered")
    if not isinstance(invalidation_triggered, bool):
        errors.append("thesis.invalidation_triggered: boolean이 필요하다")
    if thesis_status in {"APPROVED", "INVALIDATED"}:
        _nonempty(thesis.get("approved_by"), "thesis.approved_by", errors)
        parse_datetime(thesis.get("approved_at"), "thesis.approved_at", errors)
    if thesis_status == "INVALIDATED" and (
            invalidation_triggered is not True or thesis_change != "INVALIDATE"):
        errors.append(
            "thesis: INVALIDATED 상태는 change=INVALIDATE와 "
            "invalidation_triggered=true가 필요하다"
        )
    if invalidation_triggered is True and thesis_status != "INVALIDATED":
        errors.append("thesis: 무효화 조건 발생 시 status=INVALIDATED여야 한다")
    thesis_claim_ids = _id_list(thesis.get("claim_ids"), "thesis.claim_ids", errors)

    expected = data.get("expected_return")
    if not isinstance(expected, dict):
        errors.append("expected_return: 객체가 필요하다")
        expected = {}
    horizon = _number(expected.get("horizon_months"), "expected_return.horizon_months",
                      errors, minimum=1)
    if horizon is not None and not horizon.is_integer():
        errors.append("expected_return.horizon_months: 정수여야 한다")
    bear = _number(expected.get("bear_pct"), "expected_return.bear_pct", errors,
                   minimum=-100)
    base = _number(expected.get("base_pct"), "expected_return.base_pct", errors,
                   minimum=-100)
    bull = _number(expected.get("bull_pct"), "expected_return.bull_pct", errors,
                   minimum=-100)
    if None not in (bear, base, bull) and not bear <= base <= bull:
        errors.append("expected_return: bear_pct <= base_pct <= bull_pct 여야 한다")
    _number(expected.get("required_return_pct"), "expected_return.required_return_pct",
            errors, minimum=-100)
    _nonempty(expected.get("methodology"), "expected_return.methodology", errors)
    _nonempty(expected.get("calculation_id"), "expected_return.calculation_id", errors)
    expected_claim_ids = _id_list(
        expected.get("claim_ids"), "expected_return.claim_ids", errors)

    alternatives = data.get("alternatives")
    if not isinstance(alternatives, list) or not alternatives:
        errors.append("alternatives: 현금·코어자산을 포함해 하나 이상의 대안이 필요하다")
        alternatives = []
    alternative_claim_ids: list[str] = []
    for idx, alternative in enumerate(alternatives):
        field = f"alternatives[{idx}]"
        if not isinstance(alternative, dict):
            errors.append(f"{field}: 객체가 필요하다")
            continue
        _nonempty(alternative.get("instrument_id"), f"{field}.instrument_id", errors)
        _number(alternative.get("base_expected_return_pct"),
                f"{field}.base_expected_return_pct", errors, minimum=-100)
        _nonempty(alternative.get("snapshot_id"), f"{field}.snapshot_id", errors)
        alternative_claim_ids.extend(_id_list(
            alternative.get("claim_ids"), f"{field}.claim_ids", errors))

    for source_id in snapshot_source_ids:
        if source_id not in source_ids:
            errors.append(f"snapshot.source_ids: 존재하지 않는 출처 ID {source_id}")
    for source_id in source_ids:
        if source_id not in snapshot_source_ids:
            errors.append(f"sources: 스냅샷에 연결되지 않은 출처 ID {source_id}")
    for claim_id, refs in claim_source_refs.items():
        for source_id in refs:
            if source_id not in source_ids:
                errors.append(f"{claim_id}: 존재하지 않는 출처 ID {source_id}")
            elif source_id not in snapshot_source_ids:
                errors.append(f"{claim_id}: 스냅샷에 연결되지 않은 출처 ID {source_id}")
    for field, ids in (
        ("thesis.claim_ids", thesis_claim_ids),
        ("expected_return.claim_ids", expected_claim_ids),
        ("alternatives.claim_ids", alternative_claim_ids),
    ):
        for claim_id in ids:
            if claim_id not in claim_ids:
                errors.append(f"{field}: 존재하지 않는 주장 ID {claim_id}")
    return errors


def require_valid_analysis(data: dict[str, Any]) -> None:
    errors = validate_analysis(data)
    if errors:
        raise ContractError(errors)


def save_validated_analysis(path: str | Path, data: dict[str, Any]) -> Path:
    """검증된 분석만 원자적으로 저장한다."""
    require_valid_analysis(data)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        payload = json.dumps(
            data, ensure_ascii=False, indent=2, allow_nan=False,
        ) + "\n"
    except (TypeError, ValueError) as exc:
        raise ContractError([f"분석을 표준 JSON으로 직렬화할 수 없다: {exc}"]) from exc
    tmp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False
        ) as tmp:
            tmp.write(payload)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_name = tmp.name
        os.replace(tmp_name, path)
    finally:
        if tmp_name and Path(tmp_name).exists():
            Path(tmp_name).unlink()
    return path
