from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import math
import re
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

# 새 평가가 쓰는 판. 1.1.0 에서 `counterfactual` 이 붙었다. 1.2.0 에서 완료
# 판정이 확정 종가에 묶였고 벤치마크의 관측일·완료 여부가 함께 실린다. 1.3.0 에서
# 완료 판정이 **종료일 관측의 존재**까지 본다(`window.end_observation_gaps`) —
# 그 전의 평가는 종료일 봉이 없어도 뒤 날짜의 봉만 있으면 완료였다.
EVALUATION_SCHEMA = "cio-evaluation/1.3.0"
# 읽을 때는 옛 판도 받는다. 여기서 하나로 못 박으면 판을 올릴 때마다 이전
# 평가가 packet 에서 조용히 사라진다 — 읽는 쪽은 `continue` 로 건너뛸 뿐이라
# 무엇이 없어졌는지 아무도 모른다. 추가된 필드는 읽는 쪽이 안 보면 그만이다.
READABLE_EVALUATION_SCHEMAS = frozenset(
    {"cio-evaluation/1.0.0", "cio-evaluation/1.1.0", "cio-evaluation/1.2.0",
     "cio-evaluation/1.3.0"}
)

# 지문에서 빼는 키. **라이터와 리더가 반드시 같은 집합을 써야 한다.**
# evaluation_id 는 자기 자신이고 evaluated_at 은 채점을 다시 돌릴 때마다
# 바뀐다. 나머지 넷은 지문을 계산한 **뒤에** 붙는 저장 시점의 분류 라벨이며
# 예측 내용이 아니다 — source(LIVE/RETIRED), citable, contract_gap, retired.
#
# 두 집합이 어긋나면 리더가 모든 평가를 변조로 보고 `continue` 한다. 실제로
# 그랬다: 라이터는 이 넷을 붙이기 전에 지문을 냈고 리더는 붙은 뒤에 다시
# 계산해서, 116건 중 일치 0건으로 packet 의 latest_evaluation 이 언제나
# null 이었다. 판단이 자기 직전 판단의 성적을 한 번도 못 본 것이다. 조용히
# 건너뛰므로 score 출력만 보면 멀쩡해 보인다.
EVALUATION_ID_EXCLUDED_KEYS = frozenset(
    {
        "evaluation_id",
        "evaluated_at",
        "source",
        "citable",
        "contract_gap",
        "retired",
    }
)


def _require_timely_record(
    decision: dict[str, Any], *, now: dt.datetime | None
) -> None:
    """판단을 만든 시각과 기록하는 시각이 한참 벌어졌으면 받지 않는다.

    `created_at` 은 모델이 적는 값이고 기록 시각은 어디에도 남지 않는다. 그래서
    어제 만든 초안을 오늘 기록해도 판단은 어제의 것으로 저장되고, 사후 평가는
    packet 의 기준일부터 잰다 — 기록할 때 이미 본 가격 변동이 예측의 성적이 된다.
    재검증에는 쓰지 않는다. 저장된 판단은 시간이 지나도 같은 판단이다.
    """
    from cio.refresh import PRICE_MAX_AGE_HOURS

    now = now or dt.datetime.now(dt.timezone.utc)
    created = dt.datetime.fromisoformat(
        str(decision["created_at"]).replace("Z", "+00:00")
    )
    delay_hours = (now - created).total_seconds() / 3600
    if delay_hours > PRICE_MAX_AGE_HOURS:
        raise ContractError([
            f"created_at: 판단을 만든 지 {delay_hours:.1f}시간 뒤에 기록하려 한다. "
            f"판단은 만든 뒤 {PRICE_MAX_AGE_HOURS}시간 안에 기록한다 — 그 뒤에 "
            "기록하면 그 사이의 가격을 이미 본 판단이 예측으로 채점된다. "
            "`cio prepare` 로 새 context 를 만들어 다시 판단한다"
        ])


def record_decision_file(
    path: str | Path, *, now: dt.datetime | None = None
) -> Path:
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
        asset_id = _safe_memory_asset_id(normalized["asset_id"])
        out = (
            _memory_asset_dir("assessments", asset_id)
            / f"{normalized['assessment_id']}.json"
        )
        # 이미 기록된 판단을 다시 넣는 것은 새 기록이 아니다.
        if not out.exists():
            _require_timely_record(normalized, now=now)
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


def _price_session(symbol: str) -> dict[str, Any] | None:
    """가격 파일 헤더에서 마지막 봉의 확정 여부를 읽는다.

    `_price_rows` 는 `#` 로 시작하는 줄을 전부 버리므로 이 사실이 채점까지
    오지 못했다. 장중에 `score` 를 돌리면 진행 중인 봉이 확정 종가와 똑같은
    모양으로 `price.latest` 에 들어가고, 저장된 평가만 봐서는 그것이 종가인지
    14시의 값인지 구분할 수 없다. packet 은 이 구분을 갖고 있는데
    (`price.last_bar.is_settled_close`) 평가 기록에는 없었다.
    """
    path = KNOWLEDGE_DIR / "prices" / f"{safe_symbol(symbol)}.csv"
    if not path.exists():
        return None
    with path.open(encoding="utf-8-sig") as handle:
        for line in handle:
            if not line.lstrip().startswith("#"):
                break
            match = re.fullmatch(
                r"#\s*last_bar\s+(\d{4}-\d{2}-\d{2})\s*/\s*session\s+"
                r"(SETTLED|UNSETTLED|UNKNOWN)\s*",
                line.rstrip("\n"),
            )
            if match:
                return {"last_bar": match.group(1), "session": match.group(2)}
    return None


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


def _on_one_return_basis(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    """총수익률에 쓸 가격을 한 기준으로 맞춘다. → (행, 조정종가 기준인가)

    조정종가가 없는 봉이 하나라도 있으면 그 구간 전부를 원가격으로 센다. 예전에는
    봉마다 있는 쪽을 골랐다 — 판단일 봉에만 조정종가가 없으면 원가(배당 미반영)와
    기간 말 가격(배당 반영)이 한 수익률 안에서 섞여 배당만큼의 가짜 손익이 생기고,
    기준 표시만 `CLOSE_FALLBACK` 으로 바뀐 채 숫자는 어느 기준도 아니었다.
    """
    if all(row.get("has_adjusted_close") for row in rows):
        return rows, True
    return [{**row, "adjusted_close": row["close"]} for row in rows], False


# 자산의 거래일 달력과 같은 달력을 쓰는 지수. 변동성 지수나 해외 벤치마크는
# 휴장일이 달라 넣지 않는다 — 넣으면 없는 구멍이 생긴다. packet 의 세션 구멍
# 대조(`cio/context.py:_session_gaps`)와 사후 평가가 같은 표를 쓴다.
SESSION_CALENDAR_REFERENCES = {
    "KR": ("^KS11", "^KQ11"),
    "US": ("^GSPC", "^IXIC"),
}


def _exchange_confirmed_absent(symbol: str) -> set[str]:
    """거래소 원문에도 봉이 없다고 확인된 날. 그날은 이 시계열의 구멍이 아니다.

    `collectors.krx` 의 백필 기록에서 사유 없이 `unfilled` 로 남은 날이다 — 거래소에
    물었고 그 종목·지수의 행이 없었다(휴장·거래정지). 사유가 붙은 날은 원문은
    있는데 기준을 정하지 못해 못 메운 것이라 여전히 구멍이다.
    """
    path = KNOWLEDGE_DIR / "prices" / "_backfill" / f"{safe_symbol(symbol)}.json"
    try:
        data = load_json(path)
    except (ContractError, OSError):
        return set()
    if (
        data.get("schema_version") != "price-backfill/1.0.0"
        or data.get("target") != f"{safe_symbol(symbol)}.csv"
        or not isinstance(data.get("unfilled"), list)
    ):
        return set()
    reasons = data.get("unfilled_reasons")
    return {
        day for day in data["unfilled"]
        if isinstance(day, str)
        and day not in (reasons if isinstance(reasons, dict) else {})
    }


def _known_sessions(symbol: str, market: str | None = None) -> set[str]:
    """이 시계열의 달력에 **있었다고 알려진** 거래일.

    한 파일은 자기 구멍을 볼 수 없다. 봉이 저장됐다가 사라진 날의 기록
    (`# missing_sessions`)과, 같은 거래소 달력을 쓰는 지수에 봉이 있는 날을 합친다.
    `market` 을 주지 않으면 그 심볼이 달력 지수 자신일 때만 짝 지수와 대조한다 —
    해외 지수·환율은 대조할 달력이 없어 자기 기록만 본다.

    판정이 아니라 대조다. 모든 파일이 같은 날을 함께 잃고 기록도 없으면 알 수 없다.
    """
    from collectors.price import missing_sessions

    path = KNOWLEDGE_DIR / "prices" / f"{safe_symbol(symbol)}.csv"
    known = set(missing_sessions(path))
    if market not in SESSION_CALENDAR_REFERENCES:
        market = next(
            (name for name, references in SESSION_CALENDAR_REFERENCES.items()
             if symbol in references),
            None,
        )
    for reference in SESSION_CALENDAR_REFERENCES.get(market or "", ()):
        if reference != symbol:
            known.update(_price_dates(reference))
    return known - _exchange_confirmed_absent(symbol)


def _price_dates(symbol: str) -> list[str]:
    """가격 파일에 봉이 있는 날짜. 파일이 없거나 못 읽으면 빈 목록이다."""
    path = KNOWLEDGE_DIR / "prices" / f"{safe_symbol(symbol)}.csv"
    try:
        with path.open(encoding="utf-8-sig") as handle:
            return [
                row["date"]
                for row in csv.DictReader(
                    line for line in handle if not line.lstrip().startswith("#")
                )
                if row.get("date")
            ]
    except OSError:
        return []


def _sessions_missing_before(
    known_sessions: set[str] | None, *, after: str, through: str
) -> list[str]:
    """마지막 관측(`after`) 뒤, 목표일(`through`)까지에 봉이 없는 알려진 거래일."""
    return sorted(
        day for day in known_sessions or () if after < day <= through
    )


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
    start_factor: float,
) -> dict[str, Any] | None:
    """판단 시점 호가로 적은 문턱이 이 경로에서 처음 닿은 날.

    **저장된 close·high·low 는 이미 오늘 기준으로 분할이 반영돼 있다.** Yahoo 가
    소급 조정해서 주기 때문이다 — MSTR 10:1 분할 전날 종가는 1,246 이 아니라
    124.68 로 저장돼 있고 분할 다음 날(135.99)과 끊김 없이 이어진다. TSLA·SCHD·
    IBM 모두 같고, 분할 전 구간의 `adjusted_close / close` 도 1 이다(배당분만
    남는다). 그러니 **시계열 쪽은 손대지 않는다.**

    바꿔야 하는 것은 문턱이다. 손절가는 판단하던 날의 호가라 그 뒤에 분할이
    있었으면 오늘 기준이 아니다. `split_adjustment_factor` 는 그 행 **이후**의
    분할계수를 모두 곱한 값이고 `저장값 = 그날 호가 × 그 행의 계수` 이므로,
    문턱에 판단일 행의 계수를 곱하면 저장된 시계열과 같은 기준이 된다.

    이전에는 시계열 값에도 같은 계수를 곱했다. 이미 조정된 값을 한 번 더
    조정하는 것이라, 판단 뒤에 2:1 분할이 한 번만 있어도 분할 전 구간의 저가가
    절반으로 읽혀 **닿은 적 없는 손절가가 닿았다고 기록된다.** 채점은 뒤늦게
    돌아보는 계산이라 이런 오차는 아무도 눈치채지 못한 채 track record 에 남는다.

    `start_factor` 는 **판단일 봉**의 계수이고 호출하는 쪽이 넘긴다. `rows` 는
    판단 다음 거래일부터라 그 첫 행의 계수를 쓰면 하루가 어긋난다 — 판단 바로
    다음 날 2:1 분할이 있으면 계수가 0.5 가 아니라 1 로 읽혀, 손절 80(비교값
    40)을 80 그대로 저가 49 와 견줘 닿지 않은 손절을 닿았다고 적었다.
    """
    if level is None:
        return None
    comparable_level = level * start_factor
    for row in rows:
        value = float(row[field])
        if (direction == "down" and value <= comparable_level) or (
            direction == "up" and value >= comparable_level
        ):
            return {
                "date": row["date"],
                "observed": value,
                "level": level,
                "split_adjusted_level": round(comparable_level, 8),
                "split_adjustment_applied": round(start_factor, 10),
            }
    return None


def _exit_plan_observations(
    assessment: dict[str, Any],
    price_rows: list[dict[str, Any]],
    *,
    start_factor: float,
    decision_date: str,
    reviews: list[dict[str, Any]] | tuple[()] = (),
) -> dict[str, Any]:
    """Observe sell limits without inferring a condition, fill, or sale return."""
    plan = assessment["exit_plan"]
    stages = []
    for stage in plan["stages"]:
        window = [
            row for row in price_rows
            # The plan reference date is the user's calendar date; for a US
            # security that can be one day ahead of the exchange session.
            if decision_date < row["date"]
            and stage["valid_from"] <= row["date"] <= stage["valid_until"]
        ]
        adjusted_limit = float(stage["limit_price"]) * start_factor
        touched = next((row for row in window if float(row["high"]) >= adjusted_limit), None)
        checks = sorted(
            (check for check in reviews
             if check["assessment_id"] == assessment["assessment_id"]
             and check["stage_id"] == stage["stage_id"]
             and check["recorded_at"][:10] <= stage["valid_until"]),
            key=lambda check: dt.datetime.fromisoformat(check["recorded_at"]).astimezone(dt.timezone.utc),
        )
        cancellations = [check for check in checks if check["state"] == "CANCELLED"]
        first_cancellation_day = cancellations[0]["recorded_at"][:10] if cancellations else None
        reviewed_touch = None
        for row in window:
            # A cancelled plan cannot be revived by a later check. On the
            # cancellation day the intraday order is unknown, so do not call
            # that day's high a reviewed opportunity either.
            if first_cancellation_day and first_cancellation_day <= row["date"]:
                break
            prior_checks = [check for check in checks if check["recorded_at"][:10] < row["date"]]
            if (prior_checks and prior_checks[-1]["state"] == "SATISFIED"
                    and float(row["high"]) >= adjusted_limit):
                reviewed_touch = row
                break
        latest_check = checks[-1] if checks else None
        stages.append({
            "stage_id": stage["stage_id"],
            "name": stage["name"],
            "quantity": stage["quantity"],
            "limit_price": stage["limit_price"],
            "currency": plan["currency"],
            "depends_on": stage["depends_on"],
            "requires_review": stage["requires_review"],
            "valid_from": stage["valid_from"],
            "valid_until": stage["valid_until"],
            "split_adjusted_limit_price": round(adjusted_limit, 8),
            "split_adjustment_applied": round(start_factor, 10),
            "observed_from": window[0]["date"] if window else None,
            "observed_through": window[-1]["date"] if window else None,
            "price_touch_date": touched["date"] if touched else None,
            "price_touch_high": float(touched["high"]) if touched else None,
            "reviewed_price_touch_date": reviewed_touch["date"] if reviewed_touch else None,
            "latest_condition_check": (
                {"state": latest_check["state"], "recorded_at": latest_check["recorded_at"],
                 "review_assessment_id": latest_check["review_assessment_id"]}
                if latest_check else None
            ),
            "condition_review": (
                "CANCELLED" if first_cancellation_day else
                latest_check["state"] if latest_check else
                "UNREVIEWED" if stage["requires_review"] else "NOT_REQUIRED"
            ),
            "cancellation_recorded_on": first_cancellation_day,
            "share_basis_changed_at_touch": (
                not math.isclose(float(touched.get("split_adjustment_factor") or 1.0),
                                 start_factor, rel_tol=1e-8)
                if touched else None
            ),
            "price_observation": (
                "PRICE_TOUCHED" if touched else
                "NO_TOUCH_IN_OBSERVED_WINDOW" if window else
                "NO_PRICE_ROWS_IN_VALIDITY" if price_rows and price_rows[-1]["date"] > stage["valid_until"] else
                "NOT_YET_OBSERVED"
            ),
            "dependency_execution": "UNKNOWN" if stage["depends_on"] is not None else "NOT_APPLICABLE",
            "execution_eligibility": "UNKNOWN",
            "actual_execution": "UNKNOWN",
        })
    return {
        "stages": stages,
        "actual_execution": "UNKNOWN",
        "strategy_return_pct": None,
        "_note": (
            "유효기간 안에서 판단일 다음 거래일부터 일봉 고가가 분할 보정한 지정가에 닿았는지만 본다. "
            "조건 확인은 기록 다음 날부터 대조하지만 앞 단계·현재 매도의 실제 체결과 이번 단계의 "
            "집행 가능 여부는 알 수 없다. 분할 뒤 수량·지정가를 자동 환산하지 않으며, "
            "실제 매도와 전략 수익률을 계산하지 않는다."
        ),
    }


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


# `WAIT` 는 "이 가격에는 안 산다"는 판단이다. 그런데 채점은 질문가에서 시작하는
# 수익률 하나만 내므로, **안 샀는데 산 것처럼** 잰다. 그러면 WAIT 뒤 오른 건은
# 전부 실패로, 내린 건은 전부 성공으로 기록된다 — 정작 모델이 건 것은 "질문가보다
# 낮은 어딘가에서 산다"이지 "주가가 내린다"가 아닌데도 그렇다.
#
# 실제로 그 차이가 결과를 뒤집은 적이 있다. 005930 2026-07-29 판단은 WAIT 에
# 진입범위 175,000~195,000 이었고 그날 저가가 189,200 이었다. 질문가(210,000)
# 기준으로는 손실로 잡히지만 **계획은 체결됐고** 이틀 뒤 종가는 251,500 이었다.
#
# 그래서 세 갈래를 따로 낸다.
#   1. 질문가에 즉시 샀다면             (기존 realized 와 같은 값)
#   2. 제안 진입가에 걸어두고 기다렸다면 (체결 여부부터가 결과다)
#   3. 현금으로 들고 있었다면            (명목 0%. 지수 대안은 fixed_horizons)
#
# **어느 쪽이 옳았는지는 여기서 정하지 않는다.** 차이만 내고 판정은 회고에서 한다.
def _counterfactual(
    *,
    plan: dict[str, Any] | None,
    path_until_horizon: list[dict[str, Any]],
    end: dict[str, Any],
    latest: dict[str, Any],
    start_split_factor: float,
    realized: float,
    latest_return: float,
) -> dict[str, Any]:
    plan = plan or {}

    def _level(name: str) -> float | None:
        value = plan.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value) if value > 0 else None

    # 모델이 스스로 고른 값을 쓴다. `preferred_entry` 가 그 약속이고, 없을 때만
    # 범위 상단으로 내려간다 — 범위 상단이 먼저 체결되므로 더 관대한 기준이며,
    # 어느 쪽을 썼는지 모르면 관대한 값을 엄격한 값과 같은 통에 넣게 된다.
    entry, basis = _level("preferred_entry"), "PREFERRED_ENTRY"
    if entry is None:
        entry, basis = _level("entry_range_high"), "ENTRY_RANGE_HIGH"
    if entry is None:
        basis = None

    waited: dict[str, Any] = {
        "entry_price": entry,
        "entry_price_basis": basis,
        "filled": None,
        "fill": None,
        "return_to_end_pct": None,
        "return_to_latest_pct": None,
        "advantage_pct": None,
    }
    if entry is not None:
        # 손절·분할매도와 같은 방식으로 관측한다(분할 보정 후 저가 대조).
        fill = _first_trigger(
            path_until_horizon, field="low", level=entry, direction="down",
            start_factor=start_split_factor,
        )
        waited["filled"] = fill is not None
        waited["fill"] = fill
        if fill is not None:
            # 진입가는 질문가와 같은 시점의 호가라 분할 기준은 판단일 것을 쓴다.
            # 배당은 다르다 — **체결된 날부터** 받는다. 판단일의 배당 조정계수를
            # 진입 원가에 쓰면 체결 전에 지급된 배당이 수익으로 들어간다(판단가
            # 100, 배당락 뒤 90 에 체결, 이후 90 유지면 수익은 0% 인데 +11.1% 로
            # 적혔다). 체결일 봉의 조정종가/종가 비율이 그날 이후의 배당분이다.
            fill_bar = next(
                row for row in path_until_horizon if row["date"] == fill["date"]
            )
            dividend_factor_at_fill = (
                fill_bar["adjusted_close"] / fill_bar["close"]
                if fill_bar["close"] > 0 else 1.0
            )
            adjusted_entry = entry * start_split_factor * dividend_factor_at_fill
            to_end = (end["adjusted_close"] / adjusted_entry - 1) * 100
            waited["return_to_end_pct"] = round(to_end, 4)
            waited["return_to_latest_pct"] = round(
                (latest["adjusted_close"] / adjusted_entry - 1) * 100, 4
            )
            waited["advantage_pct"] = round(to_end - realized, 4)
        else:
            # 체결되지 않았다 = 지평까지 현금이었다. 그래서 이득은 현금과 같다.
            waited["advantage_pct"] = round(-realized, 4)

    return {
        "bought_at_ask": {
            "return_to_end_pct": round(realized, 4),
            "return_to_latest_pct": round(latest_return, 4),
            "_note": "질문가에 즉시 샀다면. price.return_to_end_pct 와 같은 값이다",
        },
        "waited_for_entry": {
            **waited,
            "_note": (
                "제안 진입가에 걸어두고 기다렸다면. filled=false 는 계획이 "
                "실행되지 못했다는 뜻이며 그 자체가 결과다 — 수익률이 없는 것이 "
                "아니라 포지션이 없었다. advantage_pct 는 bought_at_ask 대비 "
                "차이이며 미체결이면 현금(0%) 대비로 계산된다"
            ),
        },
        "held_cash": {
            "return_pct": 0.0,
            "advantage_pct": round(-realized, 4),
            "_note": (
                "자산 통화 명목 0%. 이자·인플레이션은 반영하지 않는다. "
                "지수 대안은 fixed_horizons.vs_benchmark 에 있다"
            ),
        },
        "_role": (
            "같은 판단을 세 가지 대안 행동으로 나눠 잰다. **`WAIT`·`AVOID` 는 "
            "이쪽이 본 채점이다** — price.return_to_end_pct 는 사지도 않은 "
            "포지션의 수익률이므로 그것만으로 승패를 읽으면 안 된다"
        ),
        "_warning": (
            "체결은 일봉 저가로만 관측한다. 그날 그 가격에 실제로 채워졌는지, "
            "몇 주가 채워졌는지는 알 수 없다. 어느 대안이 옳았는지도 여기서 "
            "정하지 않는다 — 차이만 낸다"
        ),
    }


# 모델이 무엇을 예측하든 상관없이 항상 같은 자로 재는 기간.
#
# 지금 승패는 모델이 스스로 정한 `horizon_months` 와 승리 문턱으로 판정된다.
# 그러면 모델마다 **다른 사건**을 예측하게 되어 Brier score 를 나란히 놓을 수
# 없고, 쉬운 문턱을 고르는 것만으로 점수가 좋아진다. 모델이 정한 기간은
# 설명용으로 그대로 두고, 승격 비교는 여기 고정된 자로 한다.
FIXED_HORIZON_MONTHS = (1, 3, 6, 12, 24)


def _settled_through(
    rows: list[dict[str, Any]], session: dict[str, Any] | None
) -> str | None:
    """확정 종가로 관측이 끝난 마지막 날짜.

    마지막 봉이 장중 수집분이거나 확정 여부를 모르면 그 봉은 세지 않는다. 날짜가
    목표일에 닿았다는 것과 그날의 종가를 봤다는 것은 다르다 — 평가 종료일 14시의
    값으로 완료를 선언하면 Brier score 가 종가가 아닌 값 위에서 굳는다.
    세션 정보가 아예 없으면(헤더 없는 시계열) 가를 수 없으므로 마지막 행을 쓴다.
    """
    if not rows:
        return None
    if (
        session is not None
        and session.get("last_bar") == rows[-1]["date"]
        and session.get("session") != "SETTLED"
    ):
        return rows[-2]["date"] if len(rows) >= 2 else None
    return rows[-1]["date"]


def _return_at(
    rows: list[dict[str, Any]],
    *,
    start: dt.date,
    months: int,
    adjusted_cost: float,
    settled_through: str | None = None,
    known_sessions: set[str] | None = None,
) -> tuple[float | None, bool, str | None, list[str]]:
    """기준일에서 N개월 뒤까지의 총수익률. 아직 안 지났으면 미완으로 남긴다.

    → (수익률, 완료 여부, 관측일, 관측일 뒤 목표일까지 봉이 없는 알려진 거래일)

    **목표일 뒤의 봉이 있다는 것은 목표일의 관측이 있다는 뜻이 아니다.** 예전에는
    확정 종가가 목표일을 지났는지만 봤다. 그래서 목표일 2/2 의 봉이 사라진 채
    2/3 봉만 있으면 1/30 의 가격을 기간 말 가격으로 굳혔고, 지수는 2/2 가격을 써서
    서로 다른 구간의 수익률을 뺐다.
    """
    target = _add_months(start, months)
    window = [row for row in rows if row["date"] <= target.isoformat()]
    if not window or adjusted_cost <= 0:
        return None, False, None, []
    last = window[-1]
    # 목표일이 아직 안 왔으면 값은 내되 complete=False 로 못박는다. 미완 값을
    # 완료된 것처럼 집계하면 최근 판단이 유리하거나 불리하게 치우친다.
    boundary = settled_through if settled_through is not None else rows[-1]["date"]
    # 이 시계열이 아직 닿지 않은 날은 구멍이 아니다 — 지수 파일이 하루 먼저
    # 수집됐을 뿐이다. 구멍은 그 뒤의 봉이 이미 있는데 그날만 없는 경우다.
    gaps = _sessions_missing_before(
        known_sessions, after=last["date"],
        through=min(target.isoformat(), rows[-1]["date"]),
    )
    complete = boundary >= target.isoformat() and not gaps
    return (
        round((last["adjusted_close"] / adjusted_cost - 1) * 100, 4),
        bool(complete),
        last["date"],
        gaps,
    )


def _price_currency(symbol: str) -> str | None:
    """가격 파일 머리말의 통화. 못 읽으면 None."""
    path = KNOWLEDGE_DIR / "prices" / f"{safe_symbol(symbol)}.csv"
    try:
        with path.open(encoding="utf-8-sig") as handle:
            for line in handle:
                if not line.startswith("#"):
                    break
                match = re.search(r"/ currency ([A-Z]{3})\b", line)
                if match:
                    return match.group(1)
    except OSError:
        return None
    return None


def _fixed_horizon_returns(
    *,
    rows: list[dict[str, Any]],
    start: dt.date,
    adjusted_cost: float,
    benchmarks: list[str],
    asset_currency: str | None = None,
    session: dict[str, Any] | None = None,
    known_sessions: set[str] | None = None,
) -> dict[str, Any]:
    """모델의 자기 선언과 무관한 고정 기간 수익률과 기준지수 대비 초과수익.

    **초과수익은 beta=1 가정의 단순 차감이다.** 위험조정 수익이 아니며,
    원인 비중은 더더욱 아니다.
    """
    usable = [row for row in rows if row["date"] > start.isoformat()]
    settled_through = _settled_through(usable, session)
    horizons: dict[str, Any] = {}
    for months in FIXED_HORIZON_MONTHS:
        value, complete, observed, gaps = _return_at(
            usable, start=start, months=months, adjusted_cost=adjusted_cost,
            settled_through=settled_through if usable else None,
            known_sessions=known_sessions,
        )
        horizons[f"m{months}"] = {
            "return_pct": value,
            "complete": complete,
            "observed_date": observed,
            # 있을 때만 싣는다. 관측일 뒤 목표일까지에 봉이 없는 거래일이다.
            **({"missing_sessions_before_target": gaps} if gaps else {}),
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
                "has_adjusted_close": bool(
                    row.get("has_adjusted_close", row.get("adjusted_close"))
                ),
            }
            for row in series
        ]
        # 기준점은 **판단일 시점의 지수**다. 판단일 이후 첫 봉을 기준으로 잡으면
        # 그날의 지수 움직임이 통째로 빠져 초과수익이 부풀거나 줄어든다.
        at_start = [row for row in series if row["date"] <= start.isoformat()]
        base = [row for row in series if row["date"] > start.isoformat()]
        if not at_start or not base:
            continue
        in_use, benchmark_adjusted = _on_one_return_basis([at_start[-1], *base])
        base_cost, base = in_use[0]["adjusted_close"], in_use[1:]
        benchmark_settled = _settled_through(base, _price_session(symbol))
        benchmark_known = _known_sessions(symbol)
        for months in FIXED_HORIZON_MONTHS:
            value, benchmark_complete, benchmark_observed, benchmark_gaps = _return_at(
                base, start=start, months=months, adjusted_cost=base_cost,
                settled_through=benchmark_settled,
                known_sessions=benchmark_known,
            )
            own = horizons[f"m{months}"]
            # 두 수익률이 **같은 구간**의 것일 때만 뺀다. 종목은 2/2 까지 +20% 인데
            # 지수 파일이 1/5 에서 멈춰 +1% 면, 차감값 +19%p 는 초과수익이 아니라
            # 28일치 종목 수익이다. 둘 다 목표일까지 끝났거나, 둘 다 진행 중이고
            # 같은 날까지 관측됐을 때만 비교가 성립한다.
            comparable = (
                own["return_pct"] is not None
                and value is not None
                and (
                    (own["complete"] and benchmark_complete)
                    or (
                        not own["complete"]
                        and not benchmark_complete
                        and own["observed_date"] == benchmark_observed
                    )
                )
            )
            benchmark_rows.setdefault(symbol, {})[f"m{months}"] = {
                "benchmark_return_pct": value,
                "benchmark_complete": benchmark_complete,
                "benchmark_observed_date": benchmark_observed,
                **(
                    {"benchmark_missing_sessions_before_target": benchmark_gaps}
                    if benchmark_gaps else {}
                ),
                # 지수 쪽에 조정종가가 없는 봉이 있어 원가격으로 센 경우에만 싣는다.
                **({} if benchmark_adjusted
                   else {"benchmark_return_basis": "CLOSE_FALLBACK"}),
                "excess_pct": (
                    round(own["return_pct"] - value, 4) if comparable else None
                ),
            }
    # 원화로 거래되는 해외 ETF 를 위안·홍콩달러 지수와 견주면 차감값에 환율이
    # 통째로 들어간다. 초과수익처럼 보이는 것의 일부가 원화 강세·약세다.
    currencies = {symbol: _price_currency(symbol) for symbol in benchmark_rows}
    mismatched = sorted(
        symbol for symbol, currency in currencies.items()
        if asset_currency and currency and currency != asset_currency
    )
    return {
        "months": horizons,
        "vs_benchmark": benchmark_rows,
        **(
            {
                "benchmark_currency": currencies,
                "_currency_warning": (
                    f"자산은 {asset_currency} 인데 {', '.join(mismatched)} 는 다른 "
                    "통화의 지수다. excess_pct 에 환율 변동이 그대로 들어 있어 "
                    "종목 선택의 성과로 읽으면 안 된다"
                ),
            }
            if mismatched
            else {}
        ),
        "_role": (
            "모델이 스스로 정한 기간·문턱과 무관하게 항상 같은 자로 잰 값이다. "
            "모델마다 다른 사건을 예측하면 Brier score 를 나란히 놓을 수 없고, "
            "쉬운 문턱을 고르는 것으로 점수가 좋아진다. 모델 비교와 승격은 "
            "이쪽을 쓴다"
        ),
        "_warning": (
            "excess_pct 는 beta=1 가정의 단순 차감이다. 위험조정 수익이 아니고 "
            "원인 비중은 더더욱 아니다. complete=false 인 값을 완료된 것과 같은 "
            "통에 넣지 않는다. excess_pct 가 null 인데 두 수익률이 다 있으면 "
            "종목과 지수의 관측 구간이 달라 뺄 수 없었다는 뜻이다 "
            "(benchmark_complete·benchmark_observed_date 참조)"
        ),
    }


def evaluate_assessment(
    assessment: dict[str, Any],
    *,
    rows: list[dict[str, Any]] | None = None,
    known_sessions: set[str] | None = None,
) -> dict[str, Any]:
    """판단 하나를 가격 경로에 대조한다.

    `known_sessions` 는 이 시계열의 달력에 있었다고 알려진 거래일이다(봉이 없는
    날 포함). 시계열을 직접 넘기는 호출부는 그 달력도 함께 넘긴다 — 넘기지 않으면
    넘겨받은 봉만으로 본다. 파일에서 읽을 때는 여기서 함께 읽는다.
    """
    asset = get_asset(assessment["asset_id"])
    session = _price_session(asset["price_symbol"])
    if not rows:
        rows = _price_rows(asset["price_symbol"])
        if known_sessions is None:
            known_sessions = _known_sessions(
                asset["price_symbol"], asset.get("market")
            )
    rows = [
        {
            **row,
            "adjusted_close": float(row.get("adjusted_close") or row["close"]),
            # 파일에서 읽은 행은 이 표시를 갖고 온다(`_price_rows`). 직접 넘긴
            # 행에 표시가 없으면 조정종가 값이 실려 있는지로 본다.
            "has_adjusted_close": bool(
                row.get("has_adjusted_close", row.get("adjusted_close") is not None)
            ),
            "split_adjustment_factor": float(
                row.get("split_adjustment_factor") or 1.0
            ),
        }
        for row in rows
    ]
    context, _ = find_context(assessment["context_id"])
    request = context["request"]
    requested_at = dt.datetime.fromisoformat(str(request["as_of"]).replace("Z", "+00:00"))
    market = asset.get("market")
    if market in {"KR", "US"} and requested_at.tzinfo is not None:
        from zoneinfo import ZoneInfo
        zone = ZoneInfo("Asia/Seoul" if market == "KR" else "America/New_York")
        start = requested_at.astimezone(zone).date()
    else:
        start = requested_at.date()
    horizon = _add_months(start, int(assessment["horizon_months"]))
    # 일봉에는 장중 시각이 없다. 판단일의 고가·저가는 판단 전에 이미 발생했을
    # 수 있으므로 다음 거래일부터만 사후 경로로 인정한다.
    usable = [row for row in rows if row["date"] > start.isoformat()]
    if not usable:
        raise ContractError([f"{start} 다음 거래일부터의 가격이 없다"])
    latest = usable[-1]
    settled_through = _settled_through(usable, session)
    horizon_rows = [row for row in usable if row["date"] <= horizon.isoformat()]
    if not horizon_rows:
        # 사후 봉이 전부 평가 종료일 뒤다(그 사이 시계열이 비었다). 예전에는
        # 종료일 뒤의 첫 봉을 기간 말 가격으로 썼고, 고가·저가를 낼 행이 없어
        # ValueError 로 그 자산의 채점 전체가 죽었다. 기간 안의 관측이 없으면
        # 그 기간의 수익률은 없다 — 이 건만 건너뛴다.
        raise ContractError(
            [f"{start} ~ {horizon} 평가 기간 안에 가격 관측이 없다"]
        )
    end = horizon_rows[-1]
    # 완료는 날짜가 아니라 **확정 종가**로 정한다. 평가 종료일의 봉이 장중
    # 수집분이면 그 값으로 승패·Brier 를 굳히지 않는다.
    #
    # 그리고 **종료일 뒤의 봉이 있다는 것은 종료일의 관측이 있다는 뜻이 아니다.**
    # 기간 말 가격으로 쓰는 봉(`end`)과 종료일 사이에 봉이 없는 거래일이 있으면
    # — 봉이 저장됐다가 사라진 날이거나 같은 달력의 지수에만 있는 날 — 그 `end` 는
    # 기간 말이 아니라 그 전의 어느 날이다. 그 값으로 완료를 선언하지 않는다.
    # 이 시계열이 아직 닿지 않은 날은 구멍이 아니다(진행 중인 평가에서 지수 파일이
    # 하루 먼저 수집된 경우). 그 뒤의 봉이 이미 있는데 그날만 없을 때가 구멍이다.
    end_gaps = _sessions_missing_before(
        known_sessions, after=end["date"],
        through=min(horizon.isoformat(), latest["date"]),
    )
    complete = (
        settled_through is not None
        and settled_through >= horizon.isoformat()
        and not end_gaps
    )
    start_price = float(request["price"])
    first = usable[0]
    at_start = [row for row in rows if row["date"] <= start.isoformat()]
    reference_bar = at_start[-1] if at_start else first
    # 판단일 봉부터 뒤로는 한 기준의 가격으로만 센다. 그 구간에 조정종가가 없는
    # 봉이 있으면 전부 원가격(배당 미반영)이고, 그 사실이 price.return_basis 다.
    # 판단 전의 봉은 수익률에 쓰이지 않으므로 기준을 정하는 데도 쓰지 않는다.
    in_use, adjusted_basis = _on_one_return_basis(
        [reference_bar, *usable] if at_start else usable
    )
    by_date = {row["date"]: row for row in in_use}
    rows = [by_date.get(row["date"], row) for row in rows]
    usable = [by_date[row["date"]] for row in usable]
    horizon_rows = [by_date[row["date"]] for row in horizon_rows]
    latest, end, first = usable[-1], horizon_rows[-1], usable[0]
    reference_bar = by_date[reference_bar["date"]]
    start_split_factor = float(
        reference_bar.get("split_adjustment_factor") or 1.0
    )
    # 질문가는 그날의 호가다. 저장된 시계열은 오늘 기준으로 분할이 반영돼
    # 있으므로(`_first_trigger` 참고) 질문가에 판단일 행의 분할계수를 곱해야
    # 같은 기준이 된다. 조정종가는 거기에 배당까지 반영한 값이라 그 비율을 한
    # 번 더 곱한다 — `adjusted_close / close` 는 두 값이 같은 분할 기준이므로
    # 배당분만 남는다.
    dividend_factor = (
        reference_bar["adjusted_close"] / reference_bar["close"] if reference_bar["close"] > 0 else 1.0
    )
    adjustment_factor = start_split_factor * dividend_factor
    adjusted_cost = start_price * adjustment_factor
    realized = (end["adjusted_close"] / adjusted_cost - 1) * 100
    latest_return = (latest["adjusted_close"] / adjusted_cost - 1) * 100
    plan = assessment["price_plan"]
    path_until_horizon = (
        horizon_rows if complete else usable
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
        asset_currency=asset.get("currency"),
        session=session,
        known_sessions=known_sessions,
    )
    evaluation = {
        "schema_version": EVALUATION_SCHEMA,
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
            "settled_through": settled_through,
            "complete": complete,
            # 기간 말 가격으로 쓴 봉의 날짜와, 그 뒤 종료일까지에 봉이 없는 알려진
            # 거래일. 비어 있지 않으면 price.end 는 기간 말 가격이 아니다.
            "end_observation_date": end["date"],
            "end_observation_gaps": end_gaps,
            **(
                {"_end_gap_note": (
                    f"{', '.join(end_gaps)} 의 봉이 없다 — 봉이 저장됐다가 "
                    "사라졌거나 같은 거래소 달력의 지수에만 있는 거래일이다. "
                    f"price.end 는 {end['date']} 의 값이라 평가 종료일의 관측이 "
                    "아니므로 완료로 세지 않았다. 가격을 다시 수집하거나 "
                    "거래소 원문으로 메운 뒤 다시 채점한다"
                )}
                if end_gaps else {}
            ),
            # 마지막 관측이 확정 종가인가. 장중에 채점하면 진행 중인 봉이
            # price.latest 에 들어가는데, 그 사실이 남지 않으면 나중에 이
            # 평가를 읽는 쪽이 14시의 값을 종가로 인용한다.
            # 세 갈래를 둘로 접지 않는다(`cio/context.py` 의 last_bar 와 같은
            # 규칙). 쓰고 있는 마지막 행이 헤더의 마지막 봉이 아니면 이미 닫힌
            # 봉이라 세션 상태와 무관하게 확정이다. 같은 봉일 때만 상태를 보고,
            # 그 상태가 UNKNOWN 이면 모르는 것이다.
            "latest_is_settled_close": (
                None
                if session is None
                else True
                if session.get("last_bar") != latest["date"]
                else None
                if session.get("session") == "UNKNOWN"
                else session.get("session") == "SETTLED"
            ),
            "_settled_note": (
                None
                if session is None or session.get("last_bar") != latest["date"]
                else "마지막 봉이 확정 종가가 아니다 — 장중 수집분이다. "
                "이 평가의 실현 수익률은 진행 중인 값이다"
                if session.get("session") == "UNSETTLED"
                else "마지막 봉이 확정 종가인지 판별하지 못했다. 이 평가의 "
                "실현 수익률이 확정 종가 기준인지 알 수 없다"
                if session.get("session") == "UNKNOWN"
                else None
            ),
        },
        "price": {
            "start": start_price,
            "end": end["close"],
            "latest": latest["close"],
            "currency": request["price_currency"],
            "return_basis": (
                "ADJUSTED_CLOSE_ASSET_CURRENCY"
                if adjusted_basis
                else "CLOSE_FALLBACK_ASSET_CURRENCY"
            ),
            **(
                {}
                if adjusted_basis
                else {"_return_basis_note": (
                    "판단일 이후 구간에 조정종가가 없는 봉이 있어 수익률을 전부 "
                    "원가격으로 냈다. 배당·분배금이 빠진 가격 수익률이며, 판단이 "
                    "약속한 총수익률(outcome_metric)과 그만큼 다르다"
                )}
            ),
            "excludes": ["FX", "TAX", "FEES", "SLIPPAGE"],
            "adjustment_factor_at_start": round(adjustment_factor, 10),
            "return_to_end_pct": round(realized, 4),
            "return_to_latest_pct": round(latest_return, 4),
            # 고가·저가는 저장된 그대로 쓴다. 분할 보정이 필요한 쪽은 질문가다.
            "max_gain_pct": round(
                (
                    max(row["high"] for row in path_until_horizon)
                    / split_adjusted_start
                    - 1
                )
                * 100,
                4,
            ),
            "max_loss_pct": round(
                (
                    min(row["low"] for row in path_until_horizon)
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
            name: _first_trigger(
                path_until_horizon,
                field=field,
                level=plan.get(level_name),
                direction=direction,
                start_factor=start_split_factor,
            )
            for name, level_name, field, direction in (
                ("stop", "stop_price", "low", "down"),
                ("scale_out_start", "scale_out_start", "high", "up"),
                ("reask_below", "reask_below", "low", "down"),
                ("reask_above", "reask_above", "high", "up"),
            )
        },
        "action": _action_legs(assessment, realized, complete),
        "counterfactual": _counterfactual(
            plan=plan,
            path_until_horizon=path_until_horizon,
            end=end,
            latest=latest,
            start_split_factor=start_split_factor,
            realized=realized,
            latest_return=latest_return,
        ),
        "calibration": {
            "predicted_win_probability_pct": assessment["win_probability_pct"],
            "brier_score": round((probability - float(win_observed)) ** 2, 6)
            if complete and probability is not None and win_observed is not None
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
    review_rows: dict[str, list[dict[str, Any]]] = {
        "entry_plan_checks": [], "exit_plan_checks": [],
    }
    decision_date = ""
    if assessment.get("entry_plan") or assessment.get("exit_plan"):
        from zoneinfo import ZoneInfo
        zone = ZoneInfo("Asia/Seoul" if asset["market"] == "KR" else "America/New_York")
        for path in (KNOWLEDGE_DIR / "assessments" / assessment["asset_id"]).glob("CIO-*.json"):
            try:
                candidate = load_json(path)
                if not candidate.get("entry_plan_checks") and not candidate.get("exit_plan_checks"):
                    continue
                require_valid_decision(candidate)
                recorded = dt.datetime.fromisoformat(candidate["created_at"]).astimezone(zone).isoformat()
                for field in review_rows:
                    review_rows[field].extend(
                        {**check, "recorded_at": recorded,
                         "review_assessment_id": candidate["assessment_id"]}
                        for check in candidate.get(field) or []
                    )
            except (ContractError, ValueError, OSError, KeyError):
                continue
        decision_date = dt.datetime.fromisoformat(assessment["created_at"]).astimezone(zone).date().isoformat()
    if assessment.get("entry_plan"):
        from cio.entry_plan import observations
        evaluation["entry_plan"] = observations(assessment, path_until_horizon,
            start_factor=start_split_factor, end=end,
            reviews=review_rows["entry_plan_checks"], decision_date=decision_date)
        evaluation["counterfactual"]["waited_for_entry"]["_note"] += " 조건부 분할계획의 실행 평가는 entry_plan에 별도로 표시한다. 이 단일 가격 비교를 전체 계획의 성과로 읽지 않는다."
    if assessment.get("exit_plan"):
        evaluation["exit_plan"] = _exit_plan_observations(
            assessment, usable, start_factor=start_split_factor,
            decision_date=decision_date, reviews=review_rows["exit_plan_checks"],
        )
    evaluation["evaluation_id"] = stable_hash(
        {
            key: value
            for key, value in evaluation.items()
            if key not in EVALUATION_ID_EXCLUDED_KEYS
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
    known_sessions: set[str] | None = None,
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
    evaluation = evaluate_assessment(
        assessment, rows=rows, known_sessions=known_sessions
    )
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


def _prune_superseded_evaluations(
    directory: Path, *, assessment_id: str, keep: str
) -> int:
    """같은 판단의 옛 채점을 지운다. 남기는 것은 방금 쓴 한 건뿐이다.

    `evaluation_id` 가 내용 지문이라 가격이 하루만 움직여도 새 파일이 된다.
    그래서 `score` 를 돌릴 때마다 판단 하나에 파일이 하나씩 쌓였고, 실제로
    고유 판단 15건에 파일 116건까지 갔다(판단 하나당 최대 13개). 디스크가
    문제가 아니라 **"이 판단의 평가"가 열세 가지가 되는 것**이 문제다 —
    읽는 쪽이 매번 `evaluated_at` 로 직접 골라야 하고, 고르는 규칙이 읽는
    곳마다 달라질 수 있다.

    지워도 되는 이유는 이것이 **파생 데이터**이기 때문이다. 평가는 판단과
    가격 시계열만 있으면 언제든 다시 만들어진다. 원본(판단·가격)은 그대로
    있으므로 track record 를 잃지 않는다. 반대로 판단 자체는 절대 지우지
    않는다 — 그것은 파생이 아니라 그날 실제로 한 예측이다.
    """
    removed = 0
    for path in sorted(directory.glob("EVAL-*.json")):
        if path.stem == keep:
            continue
        try:
            data = load_json(path)
        except (ContractError, OSError):
            continue
        if data.get("assessment_id") != assessment_id:
            continue
        try:
            path.unlink()
        except OSError:
            continue
        removed += 1
    return removed


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
    known_sessions = _known_sessions(asset["price_symbol"], asset.get("market"))
    evaluated: list[dict[str, Any]] = []
    retired_evaluated: list[dict[str, Any]] = []
    not_citable: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    superseded = 0

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
                known_sessions=known_sessions,
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
        directory = _memory_asset_dir("evaluations", asset["asset_id"])
        out = directory / f"{evaluation['evaluation_id']}.json"
        if not out.exists():
            write_json(out, evaluation)
        superseded += _prune_superseded_evaluations(
            directory,
            assessment_id=evaluation["assessment_id"],
            keep=evaluation["evaluation_id"],
        )
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
        "superseded_removed": superseded,
    }
