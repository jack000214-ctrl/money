from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from calculators.contracts import ContractError, load_json, stable_hash
from cio.assets import ASSET_ID_RE, get_asset
from cio.decision import (
    CONSTRAINT_SELL_REASONS,
    JUDGMENT_SELL_REASONS,
    assessment_id_for,
    find_context,
    require_valid_decision,
)
from cio.paths import KNOWLEDGE_DIR, ROOT
from cio.refresh import safe_symbol

KST = dt.timezone(dt.timedelta(hours=9))


def record_decision_file(path: str | Path) -> Path:
    input_path = Path(path)
    try:
        raw_output = input_path.read_text(encoding="utf-8")
    except UnicodeError:
        raw_output = input_path.read_bytes().decode("utf-8", errors="replace")
    try:
        data = load_json(input_path)
    except (ContractError, UnicodeError) as exc:
        parse_errors = (
            exc.errors
            if isinstance(exc, ContractError)
            else [f"{input_path}: UTF-8로 해석할 수 없는 모델 출력"]
        )
        quarantine = quarantine_decision(
            None,
            parse_errors,
            raw_output=raw_output,
            source_name=input_path.name,
        )
        raise ContractError(
            [
                *parse_errors,
                f"격리 저장: {quarantine.resolve().relative_to(ROOT.resolve())}",
            ]
        ) from exc
    context = None
    try:
        context, _ = find_context(str(data.get("context_id") or ""))
        normalized = require_valid_decision(data, context=context)
    except ContractError as exc:
        quarantine = quarantine_decision(
            data,
            exc.errors,
            raw_output=raw_output,
            source_name=input_path.name,
        )
        raise ContractError(
            [
                *exc.errors,
                f"격리 저장: {quarantine.resolve().relative_to(ROOT.resolve())}",
            ]
        ) from exc
    asset_id = _safe_memory_asset_id(normalized["asset_id"])
    out = (
        _memory_asset_dir("assessments", asset_id)
        / f"{normalized['assessment_id']}.json"
    )
    if out.exists():
        existing = load_json(out)
        if existing != normalized:
            raise ContractError([f"{out}: 같은 assessment_id의 내용이 다르다"])
        return out
    _atomic_json(out, normalized)
    return out


def quarantine_decision(
    data: dict[str, Any] | None,
    errors: list[str],
    *,
    raw_output: str | None = None,
    source_name: str | None = None,
) -> Path:
    if raw_output is None:
        try:
            raw_output = json.dumps(
                data, ensure_ascii=False, sort_keys=True, allow_nan=False
            )
        except (TypeError, ValueError):
            raw_output = repr(data)
    payload = {
        "schema_version": "cio-quarantine/1.0.0",
        "quarantined_at": dt.datetime.now(KST).isoformat(timespec="seconds"),
        "errors": errors,
        "source_name": source_name,
        "model_output_raw": raw_output,
    }
    if data is not None:
        try:
            json.dumps(data, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError):
            pass
        else:
            payload["model_output"] = data
    identifier = "INVALID-" + hashlib.sha256(
        raw_output.encode("utf-8")
    ).hexdigest()[:20]
    asset_id = _safe_memory_asset_id(
        data.get("asset_id") if isinstance(data, dict) else None
    )
    path = _memory_asset_dir("quarantine", asset_id) / f"{identifier}.json"
    if not path.exists():
        _atomic_json(path, payload)
    return path


def _safe_memory_asset_id(value: Any) -> str:
    if isinstance(value, str) and ASSET_ID_RE.fullmatch(value):
        return value
    return "unknown"


def _memory_asset_dir(kind: str, asset_id: str) -> Path:
    if kind not in {"assessments", "quarantine", "evaluations"}:
        raise ValueError(f"지원하지 않는 memory kind: {kind}")
    root = (KNOWLEDGE_DIR / kind).resolve()
    path = (root / _safe_memory_asset_id(asset_id)).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ContractError(["memory 저장 경로가 허용 범위를 벗어났다"]) from exc
    return path


def _price_rows(symbol: str) -> list[dict[str, Any]]:
    path = KNOWLEDGE_DIR / "prices" / f"{safe_symbol(symbol)}.csv"
    if not path.exists():
        raise ContractError([f"평가할 가격 시계열이 없다: {path.relative_to(ROOT)}"])
    with path.open(encoding="utf-8-sig") as handle:
        rows = list(
            csv.DictReader(
                line for line in handle if not line.lstrip().startswith("#")
            )
        )
    result = []
    for row in rows:
        try:
            close = float(row["close"])
            high = float(row.get("high") or close)
            low = float(row.get("low") or close)
            adjusted_close = float(row.get("adjusted_close") or close)
            split_adjustment_factor = float(
                row.get("split_adjustment_factor") or 1.0
            )
        except (KeyError, TypeError, ValueError):
            continue
        if split_adjustment_factor <= 0:
            continue
        result.append(
            {
                "date": row["date"],
                "close": close,
                "adjusted_close": adjusted_close,
                "has_adjusted_close": bool(row.get("adjusted_close")),
                "split_adjustment_factor": split_adjustment_factor,
                "high": high,
                "low": low,
            }
        )
    if not result:
        raise ContractError([f"가격 시계열이 비어 있다: {path.relative_to(ROOT)}"])
    return result


def _add_months(value: dt.date, months: int) -> dt.date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    month_days = (
        dt.date(year + (month == 12), 1 if month == 12 else month + 1, 1)
        - dt.timedelta(days=1)
    ).day
    return dt.date(year, month, min(value.day, month_days))


def _first_trigger(
    rows: list[dict[str, Any]],
    *,
    field: str,
    level: float | None,
    direction: str,
) -> dict[str, Any] | None:
    if level is None:
        return None
    start_factor = float(rows[0].get("split_adjustment_factor") or 1.0)
    comparable_level = level * start_factor
    for row in rows:
        value = float(row[field])
        row_factor = float(row.get("split_adjustment_factor") or 1.0)
        comparable_value = value * row_factor
        if (direction == "down" and comparable_value <= comparable_level) or (
            direction == "up" and comparable_value >= comparable_level
        ):
            return {
                "date": row["date"],
                "observed": value,
                "level": level,
                "split_adjusted_observed": round(comparable_value, 8),
                "split_adjusted_level": round(comparable_level, 8),
            }
    return None


def _action_legs(
    assessment: dict[str, Any],
    realized: float,
    complete: bool,
) -> dict[str, Any]:
    """보유분 행동을 두 다리로 나눠 관측한다.

    분할매도는 하나의 판단이 아니라 두 개의 베팅이다 — 판 수량은 '앞으로 안 오를
    것'에, 남긴 수량은 '오를 것'에 걸었다. 하나로 합치면 어느 쪽이 맞았는지
    영원히 알 수 없다.

    매도 다리는 부호가 반대다. `realized`(판단가에 샀다면 얻었을 수익률)가 양수면
    판 쪽은 기회를 놓친 것이고, 음수면 손실을 피한 것이다. 이걸 뒤집지 않고
    그대로 쓰면 좋은 매도가 전부 패배로 기록된다.

    승패를 문턱으로 확정하지 않는 이유는 매도에 쓸 문턱이 계약에 없기 때문이다.
    임의의 기준선을 만드는 대신 방향과 크기만 남기고, 사유별 집계는 회고에서 한다.
    """
    action = assessment.get("action") or {}
    direction = action.get("direction")
    reason = action.get("sell_reason")
    legs: dict[str, Any] = {
        "direction": direction,
        "sell_reason": reason,
        "sell_reason_tier": (
            "JUDGMENT"
            if reason in JUDGMENT_SELL_REASONS
            else "CONSTRAINT"
            if reason in CONSTRAINT_SELL_REASONS
            else None
        ),
        "quantity": action.get("quantity"),
        "sold_leg": None,
        "retained_leg": None,
        "_note": (
            "판단 기반과 제약 기반 매도를 절대 같은 통에 넣지 않는다. 가격 "
            "예측력과 위험 관리 비용은 다른 것이며, 섞으면 둘 다 못 읽는다"
        ),
    }
    if direction not in {"TRIM", "EXIT"}:
        return legs
    # 판 수량 기준: 판단가 대비 이후 가격이 오르면 기회비용, 내리면 회피한 손실.
    forgone = round(realized, 4)
    legs["sold_leg"] = {
        "quantity": action.get("quantity"),
        "forgone_return_pct": forgone,
        "outcome": None
        if not complete
        else "AVOIDED_LOSS"
        if forgone < 0
        else "MISSED_GAIN"
        if forgone > 0
        else "NEUTRAL",
        "_note": "판 수량이 지평까지 냈을 수익률. 음수면 매도가 유리했다",
    }
    if direction == "TRIM":
        legs["retained_leg"] = {
            "return_pct": round(realized, 4),
            "outcome": None
            if not complete
            else "GAIN"
            if realized > 0
            else "LOSS"
            if realized < 0
            else "NEUTRAL",
            "_note": "남긴 수량이 지평까지 낸 수익률. 보유 판단의 결과다",
        }
    return legs


def evaluate_assessment(
    assessment: dict[str, Any],
    *,
    rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    asset = get_asset(assessment["asset_id"])
    rows = rows or _price_rows(asset["price_symbol"])
    rows = [
        {
            **row,
            "adjusted_close": float(row.get("adjusted_close") or row["close"]),
            "has_adjusted_close": bool(row.get("has_adjusted_close")),
            "split_adjustment_factor": float(
                row.get("split_adjustment_factor") or 1.0
            ),
        }
        for row in rows
    ]
    context, _ = find_context(assessment["context_id"])
    request = context["request"]
    start = dt.datetime.fromisoformat(
        str(request["as_of"]).replace("Z", "+00:00")
    ).date()
    horizon = _add_months(start, int(assessment["horizon_months"]))
    # 일봉에는 장중 시각이 없다. 판단일의 고가·저가는 판단 전에 이미 발생했을
    # 수 있으므로 다음 거래일부터만 사후 경로로 인정한다.
    usable = [row for row in rows if row["date"] > start.isoformat()]
    if not usable:
        raise ContractError([f"{start} 다음 거래일부터의 가격이 없다"])
    latest = usable[-1]
    complete = latest["date"] >= horizon.isoformat()
    horizon_rows = [row for row in usable if row["date"] <= horizon.isoformat()]
    end = horizon_rows[-1] if horizon_rows else usable[0]
    start_price = float(request["price"])
    first = usable[0]
    adjustment_factor = (
        first["adjusted_close"] / first["close"] if first["close"] > 0 else 1.0
    )
    adjusted_cost = start_price * adjustment_factor
    realized = (end["adjusted_close"] / adjusted_cost - 1) * 100
    latest_return = (latest["adjusted_close"] / adjusted_cost - 1) * 100
    plan = assessment["price_plan"]
    path_until_horizon = (
        horizon_rows if complete else usable
    )
    start_split_factor = float(
        first.get("split_adjustment_factor") or 1.0
    )
    split_adjusted_start = start_price * start_split_factor
    win_threshold = float(assessment["win_return_threshold_pct"])
    failure_threshold = float(assessment["failure_return_threshold_pct"])
    win_observed = realized >= win_threshold if complete else None
    failure_observed = realized <= failure_threshold if complete else None
    probability_value = assessment.get("win_probability_pct")
    probability = (
        float(probability_value) / 100
        if isinstance(probability_value, (int, float))
        else None
    )
    expected_value = assessment["expected_return_pct"].get("probability_weighted")
    weighted_expected = (
        float(expected_value) if isinstance(expected_value, (int, float)) else None
    )
    evaluation = {
        "schema_version": "cio-evaluation/1.0.0",
        "evaluation_id": None,
        "assessment_id": assessment["assessment_id"],
        "context_id": assessment["context_id"],
        "asset_id": assessment["asset_id"],
        "model": assessment["model"],
        "verdict": assessment["verdict"],
        "outcome_metric": assessment["outcome_metric"],
        "evaluated_at": dt.datetime.now(KST).isoformat(timespec="seconds"),
        "window": {
            "start_date": start.isoformat(),
            "first_observation_date": usable[0]["date"],
            "target_date": horizon.isoformat(),
            "latest_available_date": latest["date"],
            "complete": complete,
        },
        "price": {
            "start": start_price,
            "end": end["close"],
            "latest": latest["close"],
            "currency": request["price_currency"],
            "return_basis": (
                "ADJUSTED_CLOSE_ASSET_CURRENCY"
                if all(row.get("has_adjusted_close") for row in usable)
                else "CLOSE_FALLBACK_ASSET_CURRENCY"
            ),
            "excludes": ["FX", "TAX", "FEES", "SLIPPAGE"],
            "adjustment_factor_at_start": round(adjustment_factor, 10),
            "return_to_end_pct": round(realized, 4),
            "return_to_latest_pct": round(latest_return, 4),
            "max_gain_pct": round(
                (
                    max(
                        row["high"] * row["split_adjustment_factor"]
                        for row in path_until_horizon
                    )
                    / split_adjusted_start
                    - 1
                )
                * 100,
                4,
            ),
            "max_loss_pct": round(
                (
                    min(
                        row["low"] * row["split_adjustment_factor"]
                        for row in path_until_horizon
                    )
                    / split_adjusted_start
                    - 1
                )
                * 100,
                4,
            ),
            "price_path_basis": "SPLIT_ADJUSTED_RAW_OHLC",
        },
        "thresholds": {
            "win_return_threshold_pct": win_threshold,
            "failure_return_threshold_pct": failure_threshold,
            "win_observed": win_observed,
            "failure_observed": failure_observed,
        },
        "triggers": {
            "stop": _first_trigger(
                path_until_horizon,
                field="low",
                level=plan.get("stop_price"),
                direction="down",
            ),
            "scale_out_start": _first_trigger(
                path_until_horizon,
                field="high",
                level=plan.get("scale_out_start"),
                direction="up",
            ),
            "reask_below": _first_trigger(
                path_until_horizon,
                field="low",
                level=plan.get("reask_below"),
                direction="down",
            ),
            "reask_above": _first_trigger(
                path_until_horizon,
                field="high",
                level=plan.get("reask_above"),
                direction="up",
            ),
        },
        "action": _action_legs(assessment, realized, complete),
        "calibration": {
            "predicted_win_probability_pct": assessment["win_probability_pct"],
            "brier_score": round((probability - float(win_observed)) ** 2, 6)
            if complete and probability is not None
            else None,
            "expected_return_pct": weighted_expected,
            "return_error_pct_points": round(realized - weighted_expected, 4)
            if complete and weighted_expected is not None
            else None,
        },
        "_note": (
            "가격·문턱·트리거의 기계적 관측이다. verdict의 정성적 타당성은 "
            "여러 평가를 모아 모델·프롬프트별로 회고한다"
        ),
    }
    evaluation["evaluation_id"] = stable_hash(
        {
            key: value
            for key, value in evaluation.items()
            if key not in {"evaluation_id", "evaluated_at"}
        },
        "EVAL-",
    )
    return evaluation


def score_asset(asset_id: str) -> dict[str, Any]:
    """저장된 판단을 이후 가격으로 사후평가한다.

    한 건이 현재 계약으로 재검증되지 않는다고 해서 그 종목의 평가 전체가
    멈추면 안 된다. 계약은 앞으로도 바뀔 것이고, 그때마다 축적된 평가가
    통째로 막히면 사후평가는 영영 돌지 않는다. 평가할 수 없는 건은 이유와
    함께 건너뛰고 보고한다.

    다만 **변조는 건너뛰지 않는다.** 저장된 판단이 자기 내용 지문과 다르면
    그것은 계약 변화가 아니라 기억이 손상된 것이므로 즉시 멈춘다.
    """
    asset = get_asset(asset_id)
    assessment_dir = _memory_asset_dir("assessments", asset["asset_id"])
    if not assessment_dir.exists():
        return {"asset_id": asset["asset_id"], "evaluated": [], "skipped": []}
    rows = _price_rows(asset["price_symbol"])
    evaluated: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for assessment_path in sorted(assessment_dir.glob("CIO-*.json")):
        assessment = load_json(assessment_path)
        stored_id = assessment.get("assessment_id")
        try:
            if stored_id != assessment_id_for(assessment):
                raise ContractError(
                    [
                        f"{assessment_path.name}: 저장된 판단이 자기 내용 지문과 "
                        "다르다 — 기록이 변조됐다"
                    ]
                )
        except (TypeError, ValueError) as exc:
            raise ContractError(
                [f"{assessment_path.name}: 판단을 지문화할 수 없다"]
            ) from exc
        try:
            context, _ = find_context(str(assessment.get("context_id") or ""))
            valid = require_valid_decision(assessment, context=context)
            evaluation = evaluate_assessment(valid, rows=rows)
        except ContractError as exc:
            skipped.append(
                {
                    "assessment_id": stored_id,
                    "reason": "; ".join(exc.errors[:2]),
                }
            )
            continue
        out = _memory_asset_dir(
            "evaluations", asset["asset_id"]
        ) / f"{evaluation['evaluation_id']}.json"
        if not out.exists():
            _atomic_json(out, evaluation)
        evaluated.append(
            {
                "assessment_id": stored_id,
                "evaluation_id": evaluation["evaluation_id"],
                "path": str(out.relative_to(ROOT)),
            }
        )
    return {
        "asset_id": asset["asset_id"],
        "evaluated": evaluated,
        "skipped": skipped,
    }


def _atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
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
