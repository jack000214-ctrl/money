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


def change_pct(current: Any, base: Any) -> float | None:
    """전기 대비 변화율. **기저가 0 이하면 값을 내지 않는다.**

    기저가 0 이면 나눗셈이 성립하지 않고, 음수면 부호가 뒤집힌다 — 적자에서
    흑자로 돌아선 해의 "전년 대비 -250%" 는 성장률이 아니라 나눗셈의 부산물이다.
    둘 다 그럴듯한 숫자로 나오기 때문에 화면에서 걸러지지 않는다.
    """
    if current is None or base is None:
        return None
    if not isinstance(current, (int, float)) or not isinstance(base, (int, float)):
        return None
    if base <= 0:
        return None
    return (current / base - 1) * 100


def ratio_pct(part: Any, whole: Any) -> float | None:
    """전체 대비 비율(마진 등). 분모가 0 이하면 값을 내지 않는다.

    분자는 음수일 수 있다 — 영업손실은 실제로 일어나는 관측이다.
    """
    if part is None or whole is None:
        return None
    if not isinstance(part, (int, float)) or not isinstance(whole, (int, float)):
        return None
    if whole <= 0:
        return None
    return part / whole * 100


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


def fundamentals_differences(
    snapshot: dict[str, Any],
    derived: dict[str, Any],
    fields: tuple[str, ...],
    *,
    limit: int = 5,
    ignore_keys: tuple[str, ...] = ("filed",),
) -> list[str]:
    """저장된 재무 스냅샷과 원문에서 다시 만든 값의 차이를 문장으로 낸다.

    내부 항등식(분기 합 = TTM, 차입 − 현금 = 순부채)은 값이 **서로** 맞는지만
    본다. 원문의 영업현금흐름 40 을 400 으로 일관되게 바꾸면 항등식도 지문도
    통과한다. 그래서 수집기의 도출 함수를 봉인된 원문에 다시 돌린 결과와 필드
    단위로 대조한다. 숫자는 허용오차 안에서 같다고 본다.

    `filed`(그 값을 실은 제출일)는 대조하지 않는다. 같은 분기 값이 이듬해 비교표에
    다시 실리면 제출일만 바뀌고 값은 그대로인데, 그것으로 스냅샷을 낡았다고 하면
    매년 모든 스냅샷이 이유 없이 재수집된다. 값이 재작성됐다면 val 이 걸린다.
    """
    out: list[str] = []

    def walk(path: str, left: Any, right: Any) -> None:
        if len(out) >= limit:
            return
        numeric = (int, float)
        if (
            isinstance(left, numeric)
            and isinstance(right, numeric)
            and not isinstance(left, bool)
            and not isinstance(right, bool)
        ):
            if abs(left - right) > max(IDENTITY_TOLERANCE, abs(right) * 1e-9):
                out.append(
                    f"{path}: 스냅샷 {left:,.0f} / 원문 재생 {right:,.0f}"
                )
            return
        if isinstance(left, dict) and isinstance(right, dict):
            for key in sorted(set(left) | set(right)):
                if key not in ignore_keys:
                    walk(f"{path}.{key}", left.get(key), right.get(key))
            return
        if isinstance(left, list) and isinstance(right, list):
            if len(left) != len(right):
                out.append(
                    f"{path}: 스냅샷 {len(left)}건 / 원문 재생 {len(right)}건"
                )
                return
            for index, (a, b) in enumerate(zip(left, right)):
                walk(f"{path}[{index}]", a, b)
            return
        if left != right:
            out.append(f"{path}: 스냅샷 {left!r} / 원문 재생 {right!r}")

    for name in fields:
        walk(name, snapshot.get(name), derived.get(name))
    return out


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


# 모델이 판단마다 통째로 읽는 JSON 의 배치. 사람이 읽기 좋은 들여쓰기는 값 하나를
# 한 줄에 놓고 깊이만큼 공백을 붙인다 — 내용이 아닌 줄바꿈·공백이 packet 파일의
# 25%, 스키마의 45% 였다. 더 나쁜 것은 줄 수다. 예산은 압축 크기로 80KB 를 재는데
# 저장된 파일은 2,100줄을 넘었고, 파일을 2,000줄씩 읽는 도구에서는 뒤쪽(근거 색인·
# 모델 계약)이 잘린 채 읽혔다.
#
# 한 줄로 다 붙이지는 않는다. 긴 줄을 자르는 도구가 있어서(2,000자) 그러면 앞부분만
# 읽힌다. 그래서 줄 길이 상한 안에서 채워 쓴다.
#
#   width        한 줄의 글자 수 상한. 문자열 하나가 그보다 길면 그 줄만 넘는다
#   indent       깊이마다 붙이는 공백. 0 이면 괄호도 앞뒤 줄에 붙인다
#   break_depth  이 깊이보다 얕은 곳의 항목은 한 줄에 여럿을 채우지 않는다 —
#                packet 의 최상위 키와 context 의 섹션이 언제나 줄 머리에서 시작한다
PACKET_LAYOUT = {"width": 1000, "indent": 0, "break_depth": 2}
# 스키마는 사람이 고치는 원본이라 구조가 보이게 들여쓰기를 한 칸 남긴다.
SCHEMA_LAYOUT = {"width": 120, "indent": 1, "break_depth": 0}
SCHEMA_FILES = ("schemas/cio-decision.schema.json", "schemas/cio-assumptions.schema.json")


def dumps_wrapped(
    data: Any, *, width: int, indent: int = 0, break_depth: int = 0
) -> str:
    """줄 길이 상한 안에서 채워 쓴 JSON. 공백만 다르고 내용은 같다.

    한 줄에 들어가는 값은 압축해서 한 줄에 쓰고, 넘치는 객체·배열만 풀어서 그
    항목들을 줄이 찰 때까지 이어 붙인다. `json.loads` 로 읽으면 원래 값이 그대로
    나온다.
    """

    def compact(value: Any) -> str:
        return json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )

    def render(value: Any, depth: int) -> list[str]:
        room = width - depth * indent
        text = compact(value)
        if not isinstance(value, (dict, list)) or not value or len(text) <= room:
            return [text]
        if isinstance(value, dict):
            opener, closer = "{", "}"
            children = [
                (json.dumps(key, ensure_ascii=False) + ":", child)
                for key, child in value.items()
            ]
        else:
            opener, closer = "[", "]"
            children = [("", child) for child in value]
        inner = room - indent
        body: list[str] = []
        current = ""
        for index, (prefix, child) in enumerate(children):
            comma = "," if index < len(children) - 1 else ""
            lines = render(child, depth + 1)
            if len(lines) == 1:
                piece = prefix + lines[0] + comma
                if current and (
                    depth < break_depth or len(current) + len(piece) > inner
                ):
                    body.append(current)
                    current = ""
                current += piece
                continue
            if current:
                body.append(current)
                current = ""
            body.append(prefix + lines[0])
            body.extend(lines[1:-1])
            body.append(lines[-1] + comma)
        if current:
            body.append(current)
        if indent:
            pad = " " * indent
            return [opener, *(pad + line for line in body), closer]
        if depth < break_depth:
            # 첫 항목도 줄 머리에서 시작하게 여는 괄호를 따로 둔다.
            body.insert(0, opener)
        else:
            body[0] = opener + body[0]
        body[-1] += closer
        return body

    return "\n".join(render(data, 0)) + "\n"


def reformat_schemas(root: str | Path | None = None) -> list[Path]:
    """스키마 원본을 정해진 배치로 다시 쓴다. 내용은 건드리지 않는다.

    편집기가 스키마를 다시 들여쓰면 판단마다 읽는 분량이 조용히 두 배가 된다.
    `tests/test_json_layout.py` 가 그것을 잡고, 고칠 때는 이것을 부른다:

        python3.13 -c "from calculators.contracts import reformat_schemas; reformat_schemas()"
    """
    base = Path(root) if root is not None else Path(__file__).resolve().parent.parent
    changed = []
    for name in SCHEMA_FILES:
        path = base / name
        text = path.read_text(encoding="utf-8")
        wanted = dumps_wrapped(json.loads(text), **SCHEMA_LAYOUT)
        if text != wanted:
            path.write_text(wanted, encoding="utf-8")
            changed.append(path)
    return changed


def write_json(
    path: str | Path, data: dict[str, Any], *, layout: dict[str, int] | None = None
) -> Path:
    """JSON 을 원자적으로 쓴다. 쓰기가 끊겨도 반쪽 파일이 남지 않는다.

    저장소 전체가 이 하나를 쓴다. 같은 로직이 모듈마다 복사돼 있으면 한쪽만
    NaN 을 막거나 한쪽만 부모 디렉터리를 만드는 식으로 조용히 갈라진다.

    `layout` 을 주면 그 배치(`dumps_wrapped`)로 쓴다. 모델이 통째로 읽는 packet
    이 그렇다. 주지 않으면 사람이 열어 보는 산출물이라 들여쓰기로 쓴다.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        payload = (
            dumps_wrapped(data, **layout)
            if layout
            else json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        )
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
