"""Integrity contracts for deterministic calculation artifacts.

This module deliberately validates facts and arithmetic only. Investment
judgement belongs to the selected CIO model and uses ``cio.decision``.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.1.0"
SCENARIO_NAMES = ("bear", "base", "bull")
CALCULATION_ID_PREFIX = "CALC-"
CALCULATION_ID_RE = re.compile(r"^CALC-[0-9a-f]{20}$")
FUNDAMENTALS_ID_PREFIX = "FUND-"
FUNDAMENTALS_ID_RE = re.compile(r"^FUND-[0-9a-f]{20}$")
FUNDAMENTALS_QUARTER_COUNT = 4
FUNDAMENTALS_VALUE_KEYS = (
    "ocf_ttm",
    "capex_ttm",
    "fcf_ttm",
    "shares_diluted",
    "cash_and_equivalents",
    "short_term_investments",
    "long_term_debt",
    "net_debt",
)
IDENTITY_TOLERANCE = 1.0


class ContractError(ValueError):
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
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle, object_pairs_hook=_duplicate_guard)
    except json.JSONDecodeError as exc:
        raise ContractError(
            [f"{path}: JSON 파싱 실패 ({exc.msg}, {exc.lineno}행)"]
        ) from exc
    if not isinstance(data, dict):
        raise ContractError([f"{path}: 최상위 값은 객체여야 한다"])
    return data


def content_hash(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_payload_hash(payload: Any) -> str:
    try:
        canonical = json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ContractError(["원문 payload가 canonical JSON이 아니다"]) from exc
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_raw_snapshot(
    path: str | Path,
    *,
    expected_digest: str | None = None,
    expected_source: str | None = None,
) -> dict[str, Any]:
    """내용 주소형 원문 wrapper와 내부 payload 지문을 함께 검증한다."""
    path = Path(path)
    document = load_json(path)
    payload = document.get("payload")
    if not isinstance(payload, dict):
        raise ContractError([f"{path}: 원문 wrapper의 payload는 객체여야 한다"])
    observed = canonical_payload_hash(payload)
    errors = []
    if document.get("payload_sha256") != observed:
        errors.append(f"{path}: 원문 payload 지문이 내용과 다르다")
    if path.stem != observed:
        errors.append(f"{path}: 원문 파일명이 내용 지문과 다르다")
    if expected_digest is not None and observed != expected_digest:
        errors.append(f"{path}: 요청한 원문 지문과 다르다")
    if expected_source is not None and document.get("source") != expected_source:
        errors.append(f"{path}: 원문 출처가 다르다")
    if errors:
        raise ContractError(errors)
    return document


def stable_hash(value: Any, prefix: str = "") -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    return prefix + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


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
) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        errors.append(f"{field}: 숫자가 필요하다")
        return None
    number = float(value)
    if not math.isfinite(number):
        errors.append(f"{field}: 유한한 숫자가 필요하다")
        return None
    if minimum is not None and number < minimum:
        errors.append(f"{field}: {minimum} 이상이어야 한다")
    return number


def _datetime(value: Any, field: str, errors: list[str]) -> datetime | None:
    if not isinstance(value, str) or not value:
        errors.append(f"{field}: 시간대가 포함된 ISO-8601 문자열이 필요하다")
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        errors.append(f"{field}: 올바른 ISO-8601 시각이 아니다")
        return None
    if parsed.tzinfo is None:
        errors.append(f"{field}: 시간대가 포함돼야 한다")
        return None
    return parsed.astimezone(timezone.utc)


def _id_list(value: Any, field: str, errors: list[str]) -> list[str]:
    if not isinstance(value, list) or not value:
        errors.append(f"{field}: 하나 이상의 ID가 필요하다")
        return []
    result: list[str] = []
    for index, item in enumerate(value):
        identifier = _nonempty(item, f"{field}[{index}]", errors)
        if identifier:
            result.append(identifier)
    if len(result) != len(set(result)):
        errors.append(f"{field}: 중복 ID가 있다")
    return result


def calculation_id_for(
    model: str,
    model_version: str,
    inputs: dict[str, Any],
    outputs: dict[str, Any],
) -> str:
    return stable_hash(
        {
            "model": model,
            "model_version": model_version,
            "inputs": inputs,
            "outputs": outputs,
        },
        CALCULATION_ID_PREFIX,
    )


def validate_calculation(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if data.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version: {SCHEMA_VERSION} 이어야 한다")
    calculation_id = _nonempty(data.get("calculation_id"), "calculation_id", errors)
    model = _nonempty(data.get("model"), "model", errors)
    model_version = _nonempty(data.get("model_version"), "model_version", errors)
    _nonempty(data.get("instrument_id"), "instrument_id", errors)
    _datetime(data.get("created_at"), "created_at", errors)
    _id_list(data.get("source_ids"), "source_ids", errors)

    inputs = data.get("inputs")
    if not isinstance(inputs, dict) or not inputs:
        errors.append("inputs: 하나 이상의 입력이 필요하다")
        inputs = None
    outputs = data.get("outputs")
    if not isinstance(outputs, dict):
        errors.append("outputs: 객체가 필요하다")
        outputs = None

    if calculation_id and not CALCULATION_ID_RE.fullmatch(calculation_id):
        errors.append("calculation_id: CALC-<20자리 16진수> 형식이어야 한다")
    elif calculation_id and model and model_version and inputs and outputs:
        try:
            expected = calculation_id_for(model, model_version, inputs, outputs)
        except (TypeError, ValueError):
            errors.append("calculation_id: 입력과 산출값을 표준 JSON으로 만들 수 없다")
        else:
            if calculation_id != expected:
                errors.append(
                    f"calculation_id: 내용 지문과 다르다 (예상 {expected})"
                )

    if not outputs:
        return errors
    scenarios = outputs.get("scenarios")
    if not isinstance(scenarios, dict):
        errors.append("outputs.scenarios: bear·base·bull 객체가 필요하다")
        return errors
    for name in SCENARIO_NAMES:
        scenario = scenarios.get(name)
        if not isinstance(scenario, dict):
            errors.append(f"outputs.scenarios.{name}: 객체가 필요하다")
            continue
        # 계약으로 가는 값은 누적이다. 연율은 참고치이며 이름이 단위를 말해야
        # 한다 — 예전 이름 `return_pct` 는 어느 단위인지 말해 주지 않아서
        # 연율이 기대수익 자리에 들어가는 오배선을 만들었다. 이미 저장된
        # 산출물은 옛 이름을 갖고 있으므로 읽을 때는 둘 다 받는다.
        _number(
            scenario.get("total_return_pct"),
            f"outputs.scenarios.{name}.total_return_pct",
            errors,
            minimum=-100,
        )
        annualized = scenario.get("annualized_return_pct")
        if annualized is None:
            annualized = scenario.get("return_pct")
        _number(
            annualized,
            f"outputs.scenarios.{name}.annualized_return_pct",
            errors,
            minimum=-100,
        )
    return errors


def fundamentals_id_for(data: dict[str, Any]) -> str:
    # raw_locator was excluded in schema 1.1 and remains so for compatibility.
    # New collectors should add raw_payload_sha256; that field is fingerprinted.
    payload = {
        key: value
        for key, value in data.items()
        if key not in ("fundamentals_id", "created_at", "raw_locator")
    }
    return stable_hash(payload, FUNDAMENTALS_ID_PREFIX)


def validate_fundamentals(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if data.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version: {SCHEMA_VERSION} 이어야 한다")
    _nonempty(data.get("instrument_id"), "instrument_id", errors)
    _nonempty(data.get("period_end"), "period_end", errors)
    _datetime(data.get("created_at"), "created_at", errors)

    fundamentals_id = _nonempty(
        data.get("fundamentals_id"), "fundamentals_id", errors
    )
    if fundamentals_id and not FUNDAMENTALS_ID_RE.fullmatch(fundamentals_id):
        errors.append("fundamentals_id: FUND-<20자리 16진수> 형식이어야 한다")

    values = data.get("values")
    if not isinstance(values, dict):
        errors.append("values: 객체가 필요하다")
        return errors
    numbers: dict[str, float] = {}
    for key in FUNDAMENTALS_VALUE_KEYS:
        value = _number(values.get(key), f"values.{key}", errors)
        if value is not None:
            numbers[key] = value

    quarters = data.get("quarters")
    if not isinstance(quarters, dict):
        errors.append("quarters: ocf·capex 분기 내역이 필요하다")
        quarters = {}

    for name, ttm_key in (("ocf", "ocf_ttm"), ("capex", "capex_ttm")):
        rows = quarters.get(name)
        if not isinstance(rows, list) or len(rows) != FUNDAMENTALS_QUARTER_COUNT:
            errors.append(f"quarters.{name}: 연속된 분기 4개가 필요하다")
            continue
        total = 0.0
        keys: list[tuple[int, int]] = []
        valid_rows = True
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                errors.append(f"quarters.{name}[{index}]: 객체가 필요하다")
                valid_rows = False
                continue
            value = _number(
                row.get("val"), f"quarters.{name}[{index}].val", errors
            )
            fy, quarter = row.get("fy"), row.get("quarter")
            if (
                isinstance(fy, int)
                and isinstance(quarter, int)
                and 1 <= quarter <= 4
            ):
                keys.append((fy, quarter))
            else:
                errors.append(
                    f"quarters.{name}[{index}]: fy와 quarter(1~4)가 필요하다"
                )
                valid_rows = False
            if value is not None:
                total += value
            else:
                valid_rows = False
        if len(keys) == 4:
            indices = sorted(fy * 4 + quarter for fy, quarter in keys)
            if indices != list(range(indices[0], indices[0] + 4)):
                errors.append(f"quarters.{name}: 연속된 4분기가 아니다 {keys}")
        if (
            valid_rows
            and ttm_key in numbers
            and abs(total - numbers[ttm_key]) > IDENTITY_TOLERANCE
        ):
            errors.append(
                f"values.{ttm_key}: 분기 합 {total:,.0f}과 "
                f"{numbers[ttm_key]:,.0f}이 다르다"
            )

    if {"ocf_ttm", "capex_ttm", "fcf_ttm"} <= numbers.keys():
        expected_fcf = numbers["ocf_ttm"] - numbers["capex_ttm"]
        if abs(expected_fcf - numbers["fcf_ttm"]) > IDENTITY_TOLERANCE:
            errors.append(
                f"values.fcf_ttm: OCF-capex는 {expected_fcf:,.0f}이다"
            )
    debt_keys = {
        "long_term_debt",
        "cash_and_equivalents",
        "short_term_investments",
        "net_debt",
    }
    if debt_keys <= numbers.keys():
        expected_debt = (
            numbers["long_term_debt"]
            - numbers["cash_and_equivalents"]
            - numbers["short_term_investments"]
        )
        if abs(expected_debt - numbers["net_debt"]) > IDENTITY_TOLERANCE:
            errors.append(
                f"values.net_debt: 차입-현금-단기투자는 {expected_debt:,.0f}이다"
            )
    if numbers.get("shares_diluted", 0) <= 0:
        errors.append("values.shares_diluted: 0보다 커야 한다")

    # 손익계산서를 실었으면 각 줄이 값을 갖거나 왜 없는지 말해야 한다.
    # IBM 표본은 operating_income 이 통째로 비어 있는 채 통과했다 — 항등식이
    # OCF·capex·FCF·순부채만 보기 때문이다. 0 이 아니라 결측인데 계약이 아무
    # 말도 하지 않으면 그 공백은 packet 에서 없는 것이 되고, 모델은 이 회사가
    # 영업이익을 공시하지 않는다는 사실 자체를 모른 채 판단한다.
    income = data.get("income_statement")
    if isinstance(income, dict):
        for name, line in income.items():
            if not isinstance(line, dict):
                errors.append(f"income_statement.{name}: 객체가 필요하다")
                continue
            ttm = line.get("ttm")
            has_ttm = (
                not isinstance(ttm, bool)
                and isinstance(ttm, (int, float))
                and math.isfinite(ttm)
            )
            if has_ttm or line.get("quarters"):
                continue
            reason = line.get("absent_reason")
            if not isinstance(reason, str) or not reason.strip():
                errors.append(
                    f"income_statement.{name}: 값도 분기도 없는데 이유가 없다. "
                    "왜 없는지(absent_reason) 없이 비워 두면 결측이 아니라 "
                    "관측된 0 으로 읽힌다"
                )

    structural_errors = [
        error for error in errors if not error.startswith("fundamentals_id:")
    ]
    if fundamentals_id and not structural_errors:
        expected_id = fundamentals_id_for(data)
        if fundamentals_id != expected_id:
            errors.append(
                f"fundamentals_id: 내용 지문은 {expected_id}이다"
            )
    return errors


def load_fundamentals(path: str | Path) -> dict[str, Any]:
    data = load_json(path)
    errors = validate_fundamentals(data)
    if errors:
        raise ContractError([f"{Path(path).name}: {error}" for error in errors])
    return data


def load_calculation(
    calculation_id: str, calculations_dir: str | Path
) -> dict[str, Any]:
    path = Path(calculations_dir) / f"{calculation_id}.json"
    if not path.exists():
        raise ContractError([f"계산 산출물이 없다: {path}"])
    data = load_json(path)
    errors = validate_calculation(data)
    if errors:
        raise ContractError([f"{path.name}: {error}" for error in errors])
    return data


def save_validated_fundamentals(
    out_dir: str | Path, data: dict[str, Any]
) -> Path:
    errors = validate_fundamentals(data)
    if errors:
        raise ContractError(errors)
    return write_json(
        Path(out_dir) / f"{data['instrument_id']}-{data['period_end']}.json",
        data,
    )


def save_validated_calculation(
    calculations_dir: str | Path, data: dict[str, Any]
) -> Path:
    errors = validate_calculation(data)
    if errors:
        raise ContractError(errors)
    return write_json(
        Path(calculations_dir) / f"{data['calculation_id']}.json",
        data,
    )


def write_json(path: str | Path, data: dict[str, Any]) -> Path:
    """JSON 을 원자적으로 쓴다. 쓰기가 끊겨도 반쪽 파일이 남지 않는다.

    저장소 전체가 이 하나를 쓴다. 같은 로직이 모듈마다 복사돼 있으면 한쪽만
    NaN 을 막거나 한쪽만 부모 디렉터리를 만드는 식으로 조용히 갈라진다.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        payload = json.dumps(
            data, ensure_ascii=False, indent=2, allow_nan=False
        ) + "\n"
    except (TypeError, ValueError) as exc:
        raise ContractError([f"표준 JSON으로 직렬화할 수 없다: {exc}"]) from exc
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False
        ) as temporary:
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_name = temporary.name
        os.replace(temporary_name, path)
    finally:
        if temporary_name and Path(temporary_name).exists():
            Path(temporary_name).unlink()
    return path
