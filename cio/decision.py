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
    "method",
    "thesis",
    "strongest_counter_thesis",
    "invalidation_signals",
    "unknowns",
    "research_requests",
    "sources",
    "evidence_refs",
}


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
