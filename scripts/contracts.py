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

SCHEMA_VERSION = "1.1.0"
IPS_STATUSES = {"DRAFT", "APPROVED", "SUPERSEDED"}
# EXPLORATORY = 뉴스·리포트에서 봤고 원문으로 확인하지 못한 주장. company-research
# SKILL 은 처음부터 이 등급을 요구했는데 여기 없어서, 뉴스를 분석에 넣으려면
# HYPOTHESIS 로 올려 적어야 했다 (2026-07-28 CXMT claim 이 그랬다). 등급을
# 세탁해야 통과하는 계약은 등급 체계를 스스로 무너뜨린다.
#
# HYPOTHESIS 와 다른 점: HYPOTHESIS 는 "내가 세웠고 검증하려는 가설"이고,
# EXPLORATORY 는 "남이 말했고 내가 확인 못 한 것"이다. 전자는 검증 계획이
# 있고 후자는 출처의 신뢰도 문제다. 섞으면 무엇을 파야 하는지 알 수 없다.
CLAIM_TYPES = {"FACT", "DERIVED", "INFERENCE", "EXPLORATORY", "HYPOTHESIS", "UNKNOWN"}
# 원문으로 확인되지 않은 등급. 논지·반대논지의 뼈대를 이것만으로 세우면
# "뉴스에서 봤다"가 매매 근거가 된다.
UNVERIFIED_CLAIM_TYPES = {"EXPLORATORY", "HYPOTHESIS", "UNKNOWN"}
THESIS_STATUSES = {"DRAFT", "REVIEWED", "APPROVED", "INVALIDATED"}
THESIS_CHANGES = {"STRENGTHEN", "NEUTRAL", "WEAKEN", "POSSIBLY_INVALIDATE", "INVALIDATE"}
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")

# 시나리오는 셋 다 있어야 한다. bear 를 생략할 수 있으면 아무도 쓰지 않는다.
SCENARIO_NAMES = ("bear", "base", "bull")
# 기대수익을 무엇으로 측정하는가. 포지션과 대안은 같은 자로 재야 한다.
RETURN_MEASURES = {"BASE", "PROBABILITY_WEIGHTED"}
CALCULATION_ID_PREFIX = "CALC-"
CALCULATION_ID_RE = re.compile(r"^CALC-[0-9a-f]{20}$")
# 밸류에이션 입력 스냅샷. 계산기의 첫 숫자가 어디서 왔는지 고정한다.
FUNDAMENTALS_ID_PREFIX = "FUND-"
FUNDAMENTALS_ID_RE = re.compile(r"^FUND-[0-9a-f]{20}$")
# 항등식 허용 오차. 부동소수 반올림은 통과시키되 손으로 고친 값은 걸러낸다.
IDENTITY_TOLERANCE = 1.0
# 확률 합과 가중평균의 허용 오차. 반올림해서 적은 값을 통과시키되
# 손으로 지어낸 값은 걸러낼 만큼은 좁게 잡는다.
PROBABILITY_TOLERANCE = 1e-6
WEIGHTED_RETURN_TOLERANCE = 0.01


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

    # 밸류에이션 가정. 할인율·영구성장률은 판정을 가장 크게 흔드는 값인데
    # 여기 없으면 분석가가 마음대로 고를 수 있다. 요구수익률만 못박고 그것을
    # 계산하는 할인율을 풀어두면 앞뒤가 맞지 않는다.
    valuation = data.get("valuation_assumptions")
    if not isinstance(valuation, dict):
        if status == "APPROVED":
            errors.append("valuation_assumptions: 승인 IPS는 할인율·영구성장률 "
                          "범위를 정의해야 한다")
        valuation = {}
    else:
        v_by_class = valuation.get("by_risk_class")
        if not isinstance(v_by_class, dict):
            errors.append("valuation_assumptions.by_risk_class: 위험등급별 객체가 필요하다")
        elif status == "APPROVED" and not v_by_class:
            errors.append("valuation_assumptions.by_risk_class: 승인 IPS는 등급을 "
                          "하나 이상 정의해야 한다")
        else:
            for name, entry in v_by_class.items():
                field = f"valuation_assumptions.by_risk_class.{name}"
                if not isinstance(entry, dict):
                    errors.append(f"{field}: 객체가 필요하다")
                    continue
                low = _number(entry.get("discount_rate_pct_min"),
                              f"{field}.discount_rate_pct_min", errors,
                              minimum=0, maximum=100,
                              allow_null=status != "APPROVED")
                high = _number(entry.get("discount_rate_pct_max"),
                               f"{field}.discount_rate_pct_max", errors,
                               minimum=0, maximum=100,
                               allow_null=status != "APPROVED")
                if low is not None and high is not None and low > high:
                    errors.append(f"{field}: min 이 max 보다 클 수 없다")
        terminal_max = _number(valuation.get("terminal_growth_pct_max"),
                               "valuation_assumptions.terminal_growth_pct_max",
                               errors, minimum=0, maximum=100,
                               allow_null=status != "APPROVED")
        for key in ("stage_one_years_max", "horizon_months_max"):
            number = _number(valuation.get(key), f"valuation_assumptions.{key}",
                             errors, minimum=1, allow_null=status != "APPROVED")
            if number is not None and not number.is_integer():
                errors.append(f"valuation_assumptions.{key}: 정수여야 한다")
        verified = valuation.get("require_verified_inputs")
        if not isinstance(verified, bool) and not (
                status != "APPROVED" and verified is None):
            errors.append("valuation_assumptions.require_verified_inputs: "
                          "boolean이 필요하다")
        # 영구성장률이 어떤 등급의 할인율 하한보다 높으면 잔존가치가 발산한다.
        v_by_class = valuation.get("by_risk_class")
        if terminal_max is not None and isinstance(v_by_class, dict):
            for name, entry in v_by_class.items():
                if not isinstance(entry, dict):
                    continue
                low = entry.get("discount_rate_pct_min")
                if isinstance(low, (int, float)) and terminal_max >= low:
                    errors.append(
                        f"valuation_assumptions: terminal_growth_pct_max "
                        f"{terminal_max}%가 {name}의 할인율 하한 {low}% 이상이다. "
                        "잔존가치가 발산한다")

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

    # 기대수익을 base 한 값으로 잴지, 확률가중으로 잴지. 승인 IPS는 골라야 한다.
    measure = policy.get("return_measure")
    if measure not in RETURN_MEASURES and not (
            status != "APPROVED" and measure is None):
        errors.append("decision_policy.return_measure: "
                      f"{', '.join(sorted(RETURN_MEASURES))} 중 하나여야 한다")
    # 하방 예산. 없으면 약세 시나리오가 판정에서 조용히 빠진다. 구속하지 않으려면
    # null 이 아니라 큰 값을 적는다 — 꺼져 있는 검사와 느슨한 검사는 다르다.
    _number(policy.get("max_bear_loss_contribution_pct"),
            "decision_policy.max_bear_loss_contribution_pct", errors,
            minimum=0, maximum=100, allow_null=status != "APPROVED")

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


def probability_weighted_return(expected: dict[str, Any]) -> float | None:
    """시나리오 확률가중 기대수익. 계약을 통과한 분석에만 쓴다.

    base 하나만 보면 하방이 -50%인 종목과 -5%인 종목이 똑같이 통과한다.
    """
    scenarios = expected.get("scenarios")
    if not isinstance(scenarios, dict):
        return None
    total = 0.0
    for name in SCENARIO_NAMES:
        entry = scenarios.get(name)
        if not isinstance(entry, dict):
            return None
        ret, prob = entry.get("return_pct"), entry.get("probability")
        if isinstance(ret, bool) or isinstance(prob, bool):
            return None
        if not isinstance(ret, (int, float)) or not isinstance(prob, (int, float)):
            return None
        total += float(ret) * float(prob)
    return total


def _validate_scenarios(expected: dict[str, Any], errors: list[str]) -> list[str]:
    """약세·기준·강세 각각의 수익률·확률·근거를 검증하고 주장 ID를 모은다."""
    scenarios = expected.get("scenarios")
    if not isinstance(scenarios, dict):
        errors.append("expected_return.scenarios: "
                      f"{', '.join(SCENARIO_NAMES)} 객체가 필요하다")
        return []

    for unknown in sorted(set(scenarios) - set(SCENARIO_NAMES)):
        errors.append(f"expected_return.scenarios: 알 수 없는 시나리오 {unknown}")

    returns: dict[str, float | None] = {}
    probabilities: list[float] = []
    claim_ids: list[str] = []
    for name in SCENARIO_NAMES:
        field = f"expected_return.scenarios.{name}"
        entry = scenarios.get(name)
        if not isinstance(entry, dict):
            errors.append(f"{field}: 객체가 필요하다")
            returns[name] = None
            continue
        returns[name] = _number(entry.get("return_pct"), f"{field}.return_pct",
                                errors, minimum=-100)
        probability = _number(entry.get("probability"), f"{field}.probability",
                              errors, minimum=0, maximum=1)
        if probability is not None:
            if probability == 0:
                errors.append(f"{field}.probability: 0보다 커야 한다. "
                              "일어나지 않는 시나리오는 시나리오가 아니다")
            probabilities.append(probability)
        # 근거 없는 시나리오는 숫자일 뿐이다. 특히 bear 가 그렇다 — 하방을
        # 뒷받침하는 주장이 없으면 아무 값이나 적어도 통과해 버린다.
        claim_ids.extend(_id_list(entry.get("claim_ids"), f"{field}.claim_ids", errors))

    bear, base, bull = returns.get("bear"), returns.get("base"), returns.get("bull")
    if bear is not None and base is not None and bull is not None:
        if not bear <= base <= bull:
            errors.append("expected_return.scenarios: "
                          "bear ≤ base ≤ bull 순서여야 한다")

    if len(probabilities) == len(SCENARIO_NAMES):
        total = sum(probabilities)
        if abs(total - 1.0) > PROBABILITY_TOLERANCE:
            errors.append(f"expected_return.scenarios: 확률 합이 1이어야 한다 "
                          f"(현재 {total:.6f})")

    stated = _number(expected.get("probability_weighted_pct"),
                     "expected_return.probability_weighted_pct", errors, minimum=-100)
    computed = probability_weighted_return(expected)
    if stated is not None and computed is not None:
        if abs(stated - computed) > WEIGHTED_RETURN_TOLERANCE:
            errors.append("expected_return.probability_weighted_pct: "
                          f"시나리오에서 계산한 값 {computed:.4f}%와 다르다 "
                          f"(적힌 값 {stated:.4f}%)")
    return claim_ids


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
    claim_types: dict[str, Any] = {}
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
            claim_types[claim_id] = claim.get("type")

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

    counter = data.get("counter_thesis")
    if not isinstance(counter, dict):
        errors.append("counter_thesis: 가장 강한 반대 근거 객체가 필요하다")
        counter = {}
    _nonempty(counter.get("text"), "counter_thesis.text", errors)
    # 반증 조건은 관측 가능해야 한다. "성장이 둔화되면"은 조건이 아니다.
    _nonempty(counter.get("invalidation_signal"),
              "counter_thesis.invalidation_signal", errors)
    counter_claim_ids = _id_list(
        counter.get("claim_ids"), "counter_thesis.claim_ids", errors)

    expected = data.get("expected_return")
    if not isinstance(expected, dict):
        errors.append("expected_return: 객체가 필요하다")
        expected = {}
    horizon = _number(expected.get("horizon_months"), "expected_return.horizon_months",
                      errors, minimum=1)
    if horizon is not None and not horizon.is_integer():
        errors.append("expected_return.horizon_months: 정수여야 한다")
    _number(expected.get("required_return_pct"), "expected_return.required_return_pct",
            errors, minimum=-100)
    _nonempty(expected.get("methodology"), "expected_return.methodology", errors)
    calculation_id = _nonempty(
        expected.get("calculation_id"), "expected_return.calculation_id", errors)
    # 계산 ID 는 입력의 지문이다. 형식이 맞지 않으면 손으로 적은 값이다.
    if calculation_id and not CALCULATION_ID_RE.fullmatch(calculation_id):
        errors.append("expected_return.calculation_id: "
                      "reverse_dcf 등 승인된 계산기가 발급한 CALC-<20자리 16진수> "
                      "여야 한다. 기대수익은 손으로 적을 수 없다")
    expected_claim_ids = _validate_scenarios(expected, errors)

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
        _number(alternative.get("expected_return_pct"),
                f"{field}.expected_return_pct", errors, minimum=-100)
        # 대안을 다른 자로 재면 비교가 성립하지 않는다.
        if alternative.get("measure") not in RETURN_MEASURES:
            errors.append(f"{field}.measure: "
                          f"{', '.join(sorted(RETURN_MEASURES))} 중 하나여야 한다")
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
        ("counter_thesis.claim_ids", counter_claim_ids),
        ("expected_return.scenarios.claim_ids", expected_claim_ids),
        ("alternatives.claim_ids", alternative_claim_ids),
    ):
        for claim_id in ids:
            if claim_id not in claim_ids:
                errors.append(f"{field}: 존재하지 않는 주장 ID {claim_id}")

    # EXPLORATORY 를 추가하면 뉴스를 등급 세탁 없이 넣을 수 있다. 그 대가로
    # 새 구멍이 열린다 — 논지 전체를 뉴스로만 세우는 것이다. 논지와 반대 논지는
    # 매매를 직접 움직이므로, 각각 원문에서 확인됐거나 코드가 계산한 주장을
    # 최소 하나는 딛고 서야 한다. 시나리오까지 강제하지는 않는다: 강세 시나리오가
    # 본래 가설인 종목이 있고, 그건 정직한 표기다.
    for field, ids in (("thesis.claim_ids", thesis_claim_ids),
                       ("counter_thesis.claim_ids", counter_claim_ids)):
        known = [claim_types.get(cid) for cid in ids if cid in claim_ids]
        if known and not any(t in {"FACT", "DERIVED"} for t in known):
            errors.append(
                f"{field}: 전부 미검증 등급이다 ({', '.join(sorted(set(map(str, known))))}). "
                "FACT 또는 DERIVED 주장을 최소 하나 딛어야 한다 — "
                "뉴스만으로 세운 논지는 매매 근거가 아니다")
    return errors


def calculation_id_for(model: str, model_version: str, inputs: dict[str, Any],
                       outputs: dict[str, Any]) -> str:
    """계산 ID 는 모델·입력·산출값 전체의 지문이다.

    입력만 지문화하면 가정을 그대로 두고 결과 숫자만 고칠 수 있다. 그것이
    바로 막으려던 구멍이므로 산출값도 함께 넣는다. 결과적으로 이 파일에서
    무엇을 고치든 ID 가 어긋나고, 계약 검증에서 걸린다.
    """
    return stable_hash(
        {"model": model, "model_version": model_version,
         "inputs": inputs, "outputs": outputs},
        CALCULATION_ID_PREFIX,
    )


def validate_calculation(data: dict[str, Any]) -> list[str]:
    """승인된 계산기가 남긴 산출물의 계약."""
    errors: list[str] = []
    if data.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version: {SCHEMA_VERSION} 이어야 한다")
    calculation_id = _nonempty(data.get("calculation_id"), "calculation_id", errors)
    model = _nonempty(data.get("model"), "model", errors)
    model_version = _nonempty(data.get("model_version"), "model_version", errors)
    _nonempty(data.get("instrument_id"), "instrument_id", errors)
    parse_datetime(data.get("created_at"), "created_at", errors)

    inputs = data.get("inputs")
    if not isinstance(inputs, dict) or not inputs:
        errors.append("inputs: 하나 이상의 입력이 필요하다")
        inputs = None
    # 입력을 뒷받침하는 출처. 숫자를 어디서 가져왔는지 없으면 계산도 추측이다.
    _id_list(data.get("source_ids"), "source_ids", errors)

    outputs = data.get("outputs")
    if not isinstance(outputs, dict):
        errors.append("outputs: 객체가 필요하다")
        outputs = None

    if calculation_id and not CALCULATION_ID_RE.fullmatch(calculation_id):
        errors.append("calculation_id: CALC-<20자리 16진수> 형식이어야 한다")
    elif calculation_id and inputs is not None and outputs is not None \
            and model and model_version:
        try:
            expected_id = calculation_id_for(model, model_version, inputs, outputs)
        except (TypeError, ValueError):
            errors.append("calculation_id: 입력·산출값을 표준 JSON으로 만들 수 없어 "
                          "지문을 계산할 수 없다")
        else:
            if calculation_id != expected_id:
                errors.append(f"calculation_id: 모델·입력·산출값의 지문과 다르다 "
                              f"(이 파일에서 계산하면 {expected_id}). "
                              "산출물을 손으로 고쳤거나 계산기를 다시 돌려야 한다")

    if outputs is None:
        return errors
    scenarios = outputs.get("scenarios")
    if not isinstance(scenarios, dict):
        errors.append("outputs.scenarios: bear·base·bull 객체가 필요하다")
        return errors
    for name in SCENARIO_NAMES:
        entry = scenarios.get(name)
        if not isinstance(entry, dict):
            errors.append(f"outputs.scenarios.{name}: 객체가 필요하다")
            continue
        _number(entry.get("return_pct"), f"outputs.scenarios.{name}.return_pct",
                errors, minimum=-100)
    return errors


FUNDAMENTALS_QUARTER_COUNT = 4
FUNDAMENTALS_VALUE_KEYS = (
    "ocf_ttm", "capex_ttm", "fcf_ttm", "shares_diluted",
    "cash_and_equivalents", "short_term_investments", "long_term_debt", "net_debt",
)


def fundamentals_id_for(data: dict[str, Any]) -> str:
    """스냅샷의 지문. ID 계산에서 자기 자신은 뺀다."""
    payload = {k: v for k, v in data.items()
               if k not in ("fundamentals_id", "created_at", "raw_locator")}
    return stable_hash(payload, FUNDAMENTALS_ID_PREFIX)


def validate_fundamentals(data: dict[str, Any]) -> list[str]:
    """밸류에이션 입력 스냅샷의 계약.

    핵심은 **내부 항등식**이다. 스키마만 검사하면 숫자 하나를 손으로 고쳐도
    통과한다. FCF = 영업현금흐름 − capex 이고 각 TTM 은 4개 분기의 합이라는
    관계를 함께 검사하면, 값 하나만 바꾼 파일은 반드시 어긋난다.
    """
    errors: list[str] = []
    if data.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version: {SCHEMA_VERSION} 이어야 한다")
    _nonempty(data.get("instrument_id"), "instrument_id", errors)
    _nonempty(data.get("period_end"), "period_end", errors)
    parse_datetime(data.get("created_at"), "created_at", errors)

    fundamentals_id = _nonempty(data.get("fundamentals_id"), "fundamentals_id", errors)
    if fundamentals_id and not FUNDAMENTALS_ID_RE.fullmatch(fundamentals_id):
        errors.append("fundamentals_id: FUND-<20자리 16진수> 형식이어야 한다")

    values = data.get("values")
    if not isinstance(values, dict):
        errors.append("values: 객체가 필요하다")
        return errors
    numbers: dict[str, float] = {}
    for key in FUNDAMENTALS_VALUE_KEYS:
        value = values.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool) \
                or not math.isfinite(value):
            errors.append(f"values.{key}: 유한한 숫자가 필요하다")
        else:
            numbers[key] = float(value)

    quarters = data.get("quarters")
    if not isinstance(quarters, dict):
        errors.append("quarters: ocf·capex 분기 내역이 필요하다")
        quarters = {}

    for name, ttm_key in (("ocf", "ocf_ttm"), ("capex", "capex_ttm")):
        rows = quarters.get(name)
        if not isinstance(rows, list) or len(rows) != FUNDAMENTALS_QUARTER_COUNT:
            errors.append(f"quarters.{name}: 분기 {FUNDAMENTALS_QUARTER_COUNT}개가 "
                          "필요하다. 모자란 채로 TTM 을 만들면 기간이 어긋난다")
            continue
        total = 0.0
        ok = True
        for idx, row in enumerate(rows):
            if not isinstance(row, dict) or not isinstance(row.get("val"), (int, float)):
                errors.append(f"quarters.{name}[{idx}].val: 숫자가 필요하다")
                ok = False
                continue
            total += float(row["val"])
        # 분기 구간이 연속인지 — 같은 분기를 두 번 더하거나 한 해를 건너뛰면 안 된다
        keys = [(r.get("fy"), r.get("quarter")) for r in rows
                if isinstance(r, dict)]
        if all(isinstance(fy, int) and isinstance(q, int) for fy, q in keys):
            indices = [fy * 4 + q for fy, q in keys]
            if sorted(indices) != list(range(min(indices), min(indices) + 4)):
                errors.append(f"quarters.{name}: 연속된 4분기가 아니다 {keys}")
        if ok and ttm_key in numbers and abs(total - numbers[ttm_key]) > IDENTITY_TOLERANCE:
            errors.append(
                f"values.{ttm_key}: 분기 합 {total:,.0f} 과 {numbers[ttm_key]:,.0f} 이 "
                "다르다. 손으로 고친 값이다")

    if {"ocf_ttm", "capex_ttm", "fcf_ttm"} <= numbers.keys():
        derived = numbers["ocf_ttm"] - numbers["capex_ttm"]
        if abs(derived - numbers["fcf_ttm"]) > IDENTITY_TOLERANCE:
            errors.append(
                f"values.fcf_ttm: 영업현금흐름 − capex = {derived:,.0f} 인데 "
                f"{numbers['fcf_ttm']:,.0f} 로 적혀 있다")
    if {"long_term_debt", "cash_and_equivalents",
            "short_term_investments", "net_debt"} <= numbers.keys():
        derived = (numbers["long_term_debt"] - numbers["cash_and_equivalents"]
                   - numbers["short_term_investments"])
        if abs(derived - numbers["net_debt"]) > IDENTITY_TOLERANCE:
            errors.append(
                f"values.net_debt: 차입 − 현금 − 단기투자 = {derived:,.0f} 인데 "
                f"{numbers['net_debt']:,.0f} 로 적혀 있다")
    if "shares_diluted" in numbers and numbers["shares_diluted"] <= 0:
        errors.append("values.shares_diluted: 0보다 커야 한다")

    if fundamentals_id and not errors:
        expected = fundamentals_id_for(data)
        if expected != fundamentals_id:
            errors.append(
                f"fundamentals_id: 내용의 지문은 {expected} 인데 {fundamentals_id} "
                "라고 적혀 있다. 저장 후 편집된 파일이다")
    return errors


def load_fundamentals(path: str | Path) -> dict[str, Any]:
    data = load_json(path)
    errors = validate_fundamentals(data)
    if errors:
        raise ContractError([f"{Path(path).name}: {m}" for m in errors])
    return data


def save_validated_fundamentals(out_dir: str | Path, data: dict[str, Any]) -> Path:
    errors = validate_fundamentals(data)
    if errors:
        raise ContractError(errors)
    name = f"{data['instrument_id']}-{data['period_end']}.json"
    return _atomic_write_json(Path(out_dir) / name, data)


def load_calculation(calculation_id: str, calculations_dir: str | Path) -> dict[str, Any]:
    path = Path(calculations_dir) / f"{calculation_id}.json"
    if not path.exists():
        raise ContractError([
            f"expected_return.calculation_id: 계산 산출물 {path} 가 없다. "
            "기대수익은 승인된 계산기가 만든 값이어야 한다"
        ])
    data = load_json(path)
    errors = validate_calculation(data)
    if errors:
        raise ContractError([f"{path.name}: {message}" for message in errors])
    return data


def verify_expected_return_calculation(
    analysis: dict[str, Any], calculations_dir: str | Path
) -> list[str]:
    """분석에 적힌 시나리오 수익률이 계산 산출물과 일치하는지 확인한다.

    이 검사가 없으면 계약은 `calculation_id` 라는 문자열이 있는지만 보고,
    정작 매매를 결정하는 숫자는 여전히 손으로 적을 수 있다.
    """
    expected = analysis.get("expected_return")
    if not isinstance(expected, dict):
        return ["expected_return: 객체가 필요하다"]
    calculation_id = expected.get("calculation_id")
    if not isinstance(calculation_id, str) or not calculation_id:
        return ["expected_return.calculation_id: 계산 ID 가 필요하다"]
    try:
        calculation = load_calculation(calculation_id, calculations_dir)
    except ContractError as exc:
        return exc.errors

    errors: list[str] = []
    if calculation.get("instrument_id") != analysis.get("instrument_id"):
        errors.append(
            f"expected_return.calculation_id: 계산은 "
            f"{calculation.get('instrument_id')} 것인데 분석은 "
            f"{analysis.get('instrument_id')} 이다"
        )
    stored = calculation.get("outputs", {}).get("scenarios", {})
    stated = expected.get("scenarios")
    if not isinstance(stated, dict):
        return errors
    for name in SCENARIO_NAMES:
        left = stated.get(name, {})
        right = stored.get(name, {})
        if not isinstance(left, dict) or not isinstance(right, dict):
            continue
        a, b = left.get("return_pct"), right.get("return_pct")
        if isinstance(a, (int, float)) and isinstance(b, (int, float)):
            if abs(float(a) - float(b)) > WEIGHTED_RETURN_TOLERANCE:
                errors.append(
                    f"expected_return.scenarios.{name}.return_pct: "
                    f"분석은 {float(a):.4f}%인데 계산 산출물은 {float(b):.4f}% 다"
                )
    return errors


def require_valid_analysis(
    data: dict[str, Any], calculations_dir: str | Path | None = None
) -> None:
    errors = validate_analysis(data)
    if not errors and calculations_dir is not None:
        errors = verify_expected_return_calculation(data, calculations_dir)
    if errors:
        raise ContractError(errors)


def save_validated_calculation(
    calculations_dir: str | Path, data: dict[str, Any]
) -> Path:
    """검증된 계산 산출물만 저장한다. 파일명은 calculation_id 다."""
    errors = validate_calculation(data)
    if errors:
        raise ContractError(errors)
    return _atomic_write_json(
        Path(calculations_dir) / f"{data['calculation_id']}.json", data)


def save_validated_analysis(
    path: str | Path, data: dict[str, Any],
    calculations_dir: str | Path | None = None,
) -> Path:
    """검증된 분석만 원자적으로 저장한다."""
    require_valid_analysis(data, calculations_dir)
    return _atomic_write_json(path, data)


def _atomic_write_json(path: str | Path, data: dict[str, Any]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        payload = json.dumps(
            data, ensure_ascii=False, indent=2, allow_nan=False,
        ) + "\n"
    except (TypeError, ValueError) as exc:
        raise ContractError([f"표준 JSON으로 직렬화할 수 없다: {exc}"]) from exc
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
