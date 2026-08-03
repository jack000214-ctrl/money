from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any

from calculators.contracts import (
    ContractError,
    load_json,
    stable_hash,
    write_json,
)
from cio.assets import ASSET_ID_RE, get_asset
from cio.decision import (
    CONSTRAINT_SELL_REASONS,
    JUDGMENT_SELL_REASONS,
    OUTCOME_METRIC,
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
    write_json(out, normalized)
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
        write_json(path, payload)
    return path


def _safe_memory_asset_id(value: Any) -> str:
    if isinstance(value, str) and ASSET_ID_RE.fullmatch(value):
        return value
    return "unknown"


def _memory_asset_dir(kind: str, asset_id: str) -> Path:
    if kind not in {"assessments", "quarantine", "evaluations", "retired"}:
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


# 모델이 무엇을 예측하든 상관없이 항상 같은 자로 재는 기간.
#
# 지금 승패는 모델이 스스로 정한 `horizon_months` 와 승리 문턱으로 판정된다.
# 그러면 모델마다 **다른 사건**을 예측하게 되어 Brier score 를 나란히 놓을 수
# 없고, 쉬운 문턱을 고르는 것만으로 점수가 좋아진다. 모델이 정한 기간은
# 설명용으로 그대로 두고, 승격 비교는 여기 고정된 자로 한다.
FIXED_HORIZON_MONTHS = (1, 3, 6, 12, 24)


def _return_at(
    rows: list[dict[str, Any]],
    *,
    start: dt.date,
    months: int,
    adjusted_cost: float,
) -> tuple[float | None, bool, str | None]:
    """기준일에서 N개월 뒤까지의 총수익률. 아직 안 지났으면 미완으로 남긴다."""
    target = _add_months(start, months)
    window = [row for row in rows if row["date"] <= target.isoformat()]
    if not window or adjusted_cost <= 0:
        return None, False, None
    last = window[-1]
    # 목표일이 아직 안 왔으면 값은 내되 complete=False 로 못박는다. 미완 값을
    # 완료된 것처럼 집계하면 최근 판단이 유리하거나 불리하게 치우친다.
    complete = rows and rows[-1]["date"] >= target.isoformat()
    return (
        round((last["adjusted_close"] / adjusted_cost - 1) * 100, 4),
        bool(complete),
        last["date"],
    )


def _fixed_horizon_returns(
    *,
    rows: list[dict[str, Any]],
    start: dt.date,
    adjusted_cost: float,
    benchmarks: list[str],
) -> dict[str, Any]:
    """모델의 자기 선언과 무관한 고정 기간 수익률과 기준지수 대비 초과수익.

    **초과수익은 beta=1 가정의 단순 차감이다.** 위험조정 수익이 아니며,
    원인 비중은 더더욱 아니다.
    """
    usable = [row for row in rows if row["date"] > start.isoformat()]
    horizons: dict[str, Any] = {}
    for months in FIXED_HORIZON_MONTHS:
        value, complete, observed = _return_at(
            usable, start=start, months=months, adjusted_cost=adjusted_cost
        )
        horizons[f"m{months}"] = {
            "return_pct": value,
            "complete": complete,
            "observed_date": observed,
        }

    benchmark_rows: dict[str, Any] = {}
    for symbol in benchmarks[:2]:
        try:
            series = _price_rows(symbol)
        except ContractError:
            continue
        series = [
            {
                **row,
                "adjusted_close": float(row.get("adjusted_close") or row["close"]),
            }
            for row in series
        ]
        # 기준점은 **판단일 시점의 지수**다. 판단일 이후 첫 봉을 기준으로 잡으면
        # 그날의 지수 움직임이 통째로 빠져 초과수익이 부풀거나 줄어든다.
        at_start = [row for row in series if row["date"] <= start.isoformat()]
        base = [row for row in series if row["date"] > start.isoformat()]
        if not at_start or not base:
            continue
        base_cost = at_start[-1]["adjusted_close"]
        for months in FIXED_HORIZON_MONTHS:
            value, _complete, _observed = _return_at(
                base, start=start, months=months, adjusted_cost=base_cost
            )
            own = horizons[f"m{months}"]["return_pct"]
            benchmark_rows.setdefault(symbol, {})[f"m{months}"] = {
                "benchmark_return_pct": value,
                "excess_pct": (
                    round(own - value, 4)
                    if own is not None and value is not None
                    else None
                ),
            }
    return {
        "months": horizons,
        "vs_benchmark": benchmark_rows,
        "_role": (
            "모델이 스스로 정한 기간·문턱과 무관하게 항상 같은 자로 잰 값이다. "
            "모델마다 다른 사건을 예측하면 Brier score 를 나란히 놓을 수 없고, "
            "쉬운 문턱을 고르는 것으로 점수가 좋아진다. 모델 비교와 승격은 "
            "이쪽을 쓴다"
        ),
        "_warning": (
            "excess_pct 는 beta=1 가정의 단순 차감이다. 위험조정 수익이 아니고 "
            "원인 비중은 더더욱 아니다. complete=false 인 값을 완료된 것과 같은 "
            "통에 넣지 않는다"
        ),
    }


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
    fixed = _fixed_horizon_returns(
        rows=rows,
        start=start,
        adjusted_cost=adjusted_cost,
        benchmarks=asset.get("benchmarks") or [],
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
        "fixed_horizons": fixed,
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


# 채점에 실제로 필요한 필드. **인용 계약과 채점 계약은 다르다.**
#
# 판단을 다음 판단의 근거로 인용하려면 현재 계약을 전부 통과해야 한다 — 안
# 그러면 모델이 낡은 의미론을 물려받는다. 그러나 채점은 뒤를 보는 측정이다.
# 그때 그 가격에서 무엇을 예측했는지가 남아 있으면 맞았는지 틀렸는지는 계산할
# 수 있고, 그 계산에 현재 계약의 필드가 전부 필요하지는 않다.
#
# 둘을 하나로 묶으면 계약을 조일 때마다 track record 가 통째로 사라진다.
# 그것은 "모델이 좋아질수록 강해지는 시스템"의 정반대다 — 계약을 개선할수록
# 과거를 잃는다.
SCORING_REQUIRED = (
    "assessment_id",
    "context_id",
    "asset_id",
    "verdict",
    "outcome_metric",
    "horizon_months",
    "win_return_threshold_pct",
    "failure_return_threshold_pct",
    "expected_return_pct",
    "price_plan",
)


def scoring_contract_errors(assessment: dict[str, Any]) -> list[str]:
    """이 판단이 **측정 가능한가**만 본다. 좋은 판단인지는 보지 않는다."""
    errors: list[str] = []
    for field in SCORING_REQUIRED:
        if assessment.get(field) is None:
            errors.append(f"{field}: 채점에 필요한 값이 없다")
    if assessment.get("outcome_metric") != OUTCOME_METRIC:
        errors.append(
            f"outcome_metric: {OUTCOME_METRIC}가 아니면 같은 자로 잴 수 없다"
        )
    horizon = assessment.get("horizon_months")
    if not isinstance(horizon, int) or isinstance(horizon, bool) or horizon < 1:
        errors.append("horizon_months: 1 이상의 정수여야 한다")
    for field in ("win_return_threshold_pct", "failure_return_threshold_pct"):
        value = assessment.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            errors.append(f"{field}: 숫자여야 한다")
    plan = assessment.get("price_plan")
    if not isinstance(plan, dict):
        errors.append("price_plan: 객체여야 한다")
    return errors


def _score_one(
    assessment: dict[str, Any],
    *,
    filename: str,
    rows: list[dict[str, Any]],
    retired: dict[str, Any] | None,
) -> dict[str, Any]:
    """한 건을 채점한다. 변조는 계약 변화와 다르므로 여기서 멈춘다."""
    stored_id = assessment.get("assessment_id")
    try:
        if stored_id != assessment_id_for(assessment):
            raise ContractError(
                [
                    f"{filename}: 저장된 판단이 자기 내용 지문과 다르다 — "
                    "기록이 변조됐다"
                ]
            )
    except (TypeError, ValueError) as exc:
        raise ContractError([f"{filename}: 판단을 지문화할 수 없다"]) from exc

    # **인용 가능성과 측정 가능성을 따로 판정한다.**
    #
    # 현재 계약을 통과하면 다음 판단의 근거로 인용할 수 있다. 통과하지 못해도
    # 그때 무엇을 예측했는지가 남아 있으면 맞았는지는 계산할 수 있다. 둘을
    # 하나로 묶으면 계약을 조일 때마다 track record 가 사라진다.
    contract_gap: list[str] = []
    if retired is None:
        try:
            context, _ = find_context(str(assessment.get("context_id") or ""))
            assessment = require_valid_decision(assessment, context=context)
        except ContractError as exc:
            contract_gap = exc.errors
    if retired is not None or contract_gap:
        errors = scoring_contract_errors(assessment)
        if errors:
            # 측정조차 불가능하다. 이때만 건너뛴다.
            raise ContractError(errors)
    evaluation = evaluate_assessment(assessment, rows=rows)
    evaluation["source"] = "RETIRED" if retired else "LIVE"
    evaluation["citable"] = retired is None and not contract_gap
    if contract_gap:
        evaluation["contract_gap"] = {
            "errors": contract_gap[:4],
            "_meaning": (
                "현재 판단 계약을 통과하지 못한다. 근거로 인용하지 않지만 "
                "그때 실제로 내린 예측이므로 채점은 한다. 계약을 조일 때마다 "
                "과거를 잃지 않기 위한 구분이다"
            ),
        }
    if retired:
        # 폐기된 판단의 평가는 집계에서 반드시 구분돼야 한다. 그때의 계약과
        # prompt 가 지금과 달랐으므로, 모델 비교의 같은 통에 넣으면 안 된다.
        evaluation["retired"] = {
            "retired_at": retired.get("retired_at"),
            "class": retired.get("class"),
            "reason": retired.get("reason"),
            "contract_error": retired.get("contract_error"),
            "_meaning": (
                "폐기된 판단의 사후평가다. 그때 실제로 내린 예측이므로 "
                "채점하지만, 근거로 인용하지 않으며 현재 계약의 판단과 같은 "
                "통에 넣어 모델을 비교하지 않는다"
            ),
        }
    return evaluation


def score_asset(asset_id: str) -> dict[str, Any]:
    """저장된 판단을 이후 가격으로 사후평가한다.

    한 건이 현재 계약으로 재검증되지 않는다고 해서 그 종목의 평가 전체가
    멈추면 안 된다. 계약은 앞으로도 바뀔 것이고, 그때마다 축적된 평가가
    통째로 막히면 사후평가는 영영 돌지 않는다. 평가할 수 없는 건은 이유와
    함께 건너뛰고 보고한다.

    **폐기된 판단도 채점한다.** 폐기의 뜻은 "근거로 인용하지 않는다"이지
    "그런 예측을 한 적이 없다"가 아니다. 실제 날짜에 실제 가격에서 내린
    예측이므로 맞았는지 틀렸는지는 여전히 측정 가능하고, 측정하지 않으면
    계약을 조일 때마다 track record 가 사라진다.

    다만 **변조는 건너뛰지 않는다.** 저장된 판단이 자기 내용 지문과 다르면
    그것은 계약 변화가 아니라 기억이 손상된 것이므로 즉시 멈춘다.
    """
    asset = get_asset(asset_id)
    assessment_dir = _memory_asset_dir("assessments", asset["asset_id"])
    retired_dir = _memory_asset_dir("retired", asset["asset_id"])
    if not assessment_dir.exists() and not retired_dir.exists():
        return {
            "asset_id": asset["asset_id"],
            "evaluated": [],
            "scored_but_not_citable": [],
            "retired_evaluated": [],
            "skipped": [],
        }
    rows = _price_rows(asset["price_symbol"])
    evaluated: list[dict[str, Any]] = []
    retired_evaluated: list[dict[str, Any]] = []
    not_citable: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    targets: list[tuple[Path, bool]] = []
    if assessment_dir.exists():
        targets += [(path, False) for path in sorted(assessment_dir.glob("CIO-*.json"))]
    if retired_dir.exists():
        targets += [(path, True) for path in sorted(retired_dir.glob("CIO-*.json"))]

    for assessment_path, is_retired in targets:
        assessment = load_json(assessment_path)
        # 폐기 표식은 폐기 시점에 덧붙인 것이라 원래 지문에 들어 있지 않다.
        marker = assessment.pop("_retired", None) if is_retired else None
        if is_retired and not isinstance(marker, dict):
            marker = {}
        stored_id = assessment.get("assessment_id")
        try:
            evaluation = _score_one(
                assessment,
                filename=assessment_path.name,
                rows=rows,
                retired=marker,
            )
        except ContractError as exc:
            if "변조" in "".join(exc.errors) or "지문화할 수 없다" in "".join(
                exc.errors
            ):
                raise
            skipped.append(
                {
                    "assessment_id": stored_id,
                    "source": "RETIRED" if is_retired else "LIVE",
                    "reason": "; ".join(exc.errors[:2]),
                }
            )
            continue
        out = _memory_asset_dir(
            "evaluations", asset["asset_id"]
        ) / f"{evaluation['evaluation_id']}.json"
        if not out.exists():
            write_json(out, evaluation)
        row = {
            "assessment_id": stored_id,
            "evaluation_id": evaluation["evaluation_id"],
            "path": str(out.relative_to(ROOT)),
        }
        if is_retired:
            retired_evaluated.append(row)
        elif not evaluation["citable"]:
            # 채점은 됐지만 인용할 수 없다. 이 목록이 비어 있지 않다는 것은
            # 사용자가 retire 로 처분할지 결정해야 할 건이 있다는 뜻이다.
            not_citable.append(
                {**row, "reason": evaluation["contract_gap"]["errors"][0]}
            )
        else:
            evaluated.append(row)
    return {
        "asset_id": asset["asset_id"],
        "evaluated": evaluated,
        "scored_but_not_citable": not_citable,
        "retired_evaluated": retired_evaluated,
        "skipped": skipped,
    }
