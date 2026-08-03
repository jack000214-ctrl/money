"""사용자 투자정책 — 종목이 아니라 **자본**에 대한 제약.

모델은 "이 종목이 이 가격에 좋은가"에 답할 수 있다. 그러나 "그래서 내 돈
얼마를 여기에 쓰는가"에 답하려면 사용자만 아는 것이 필요하다 — 한 종목에서
얼마까지 잃어도 되는지, 한 종목이 자산의 몇 %까지 커져도 되는지, 현금을
얼마나 남겨야 하는지. 이것이 없으면 모델은 수량을 정할 근거 없이 정밀한
숫자를 낸다. 리뷰가 지적한 과잉정밀성이 그것이다.

이 값들은 지금까지 `knowledge/portfolio/holdings.csv` 의 **주석**에만 있었다.
주석은 어떤 모델도 읽지 못한다. 정책을 파일로 옮기고 packet 에 실어야
모델을 교체해도 제약이 따라간다.

## PC 가 여기서 하는 일과 하지 않는 일

하는 일은 둘뿐이다.

1. 사용자가 정한 한도를 packet 에 **그대로** 싣는다.
2. 한도와 현재·매수후 수치를 **산술로 대조**한다.

한도 초과 여부(`breach`)는 계산 결과이지 매도 지시가 아니다. 무엇이 좋은
한도인지, 초과했을 때 무엇을 해야 하는지는 PC 가 정하지 않는다. 비어 있는
한도를 그럴듯한 기본값으로 채우지도 않는다 — 사용자가 정하지 않은 수치를
PC 가 채우면 모델은 그것을 사용자의 정책으로 믿고 그 위에 수량을 쌓는다.

비어 있으면 비어 있다고 말한다. 그 공백이 결론에 필요하면 모델은
`action.direction = UNDETERMINED`, `undetermined_reason = NO_INVESTOR_POLICY`
로 답한다.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from calculators.contracts import load_json
from cio.paths import ROOT

POLICY_SCHEMA = "cio-investor-policy/1.0.0"
POLICY_PATH = ROOT / "config" / "investor-policy.json"

NUMBER = "number"
BOOLEAN = "boolean"
TEXT = "text"

# 점 표기 경로 -> (허용 타입, 단위).
#
# **여기 없는 필드는 계약 위반이고, 여기 있는데 null 이면 '아직 정하지 않음'이다.**
# 둘을 구분하는 것이 이 registry 의 목적이다. 구분하지 않으면 "필드를
# 빠뜨렸다"와 "한도를 정하지 않았다"가 같은 모양이 되고, 모델은 정책이
# 없다는 사실 자체를 관측할 수 없게 된다.
FIELDS: dict[str, tuple[str, str]] = {
    "objective.primary_goal": (TEXT, ""),
    "objective.target_annual_return_pct": (NUMBER, "PCT_PER_YEAR"),
    "horizon.typical_holding_months": (NUMBER, "MONTHS"),
    "horizon.liquidity_need_within_months": (NUMBER, "MONTHS"),
    "risk.stop_style": (TEXT, ""),
    "risk.max_loss_per_position_krw": (NUMBER, "KRW"),
    "risk.max_portfolio_drawdown_pct": (NUMBER, "PCT_OF_TOTAL_ASSETS"),
    "concentration.max_single_position_pct_of_total": (
        NUMBER, "PCT_OF_TOTAL_ASSETS",
    ),
    "concentration.max_sector_pct_of_total": (NUMBER, "PCT_OF_TOTAL_ASSETS"),
    "liquidity.min_cash_krw": (NUMBER, "KRW"),
    "liquidity.monthly_net_income_krw": (NUMBER, "KRW_PER_MONTH"),
    "instruments.leverage_allowed": (BOOLEAN, ""),
    "instruments.derivative_etf_allowed": (BOOLEAN, ""),
    "instruments.notes": (TEXT, ""),
    "tax.account_type": (TEXT, ""),
    "tax.notes": (TEXT, ""),
}

# 무엇이 매도를 촉발하는가. 이것을 고르지 않으면 `max_loss_per_position_krw`
# 가 비어 있는 이유를 알 수 없다 — 아직 안 정한 것인지, 가격 손절을 쓰지
# 않기로 한 것인지가 같은 모양(null)이기 때문이다.
STOP_STYLES = {
    # 가격이 닿으면 판다
    "PRICE",
    # 논지가 깨지면 판다. 가격 문턱은 매도 지시가 아니라 재검토 트리거다
    "THESIS",
    "BOTH",
    # 팔지 않는다
    "NONE",
}

STRUCTURAL_KEYS = {
    "schema_version",
    "policy_version",
    "base_currency",
    "denominator",
    "default_alternatives",
    "intentionally_unset",
    "_role",
}

GROUPS = sorted({path.split(".", 1)[0] for path in FIELDS})


def _get(data: dict[str, Any], path: str) -> Any:
    group, leaf = path.split(".", 1)
    section = data.get(group)
    if not isinstance(section, dict):
        return None
    return section.get(leaf)


def _finite(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
    )


def validate_policy(data: dict[str, Any]) -> list[str]:
    """정책 파일의 형식만 검사한다. 값이 현명한지는 검사하지 않는다."""
    errors: list[str] = []
    if data.get("schema_version") != POLICY_SCHEMA:
        errors.append(f"schema_version: {POLICY_SCHEMA} 이어야 한다")
    version = data.get("policy_version")
    if not isinstance(version, str) or not version.strip():
        errors.append("policy_version: 비어 있지 않은 문자열이어야 한다")
    if data.get("base_currency") not in {"KRW", "USD"}:
        errors.append("base_currency: KRW 또는 USD여야 한다")

    unknown_top = sorted(set(data) - STRUCTURAL_KEYS - set(GROUPS))
    if unknown_top:
        errors.append(f"알 수 없는 최상위 필드: {', '.join(unknown_top)}")

    for group in GROUPS:
        section = data.get(group)
        if not isinstance(section, dict):
            errors.append(f"{group}: 객체가 필요하다")
            continue
        expected = {
            path.split(".", 1)[1]
            for path in FIELDS
            if path.startswith(f"{group}.")
        }
        unknown = sorted(set(section) - expected)
        if unknown:
            errors.append(
                f"{group}: 알 수 없는 필드 {', '.join(unknown)}"
            )
        missing = sorted(expected - set(section))
        if missing:
            # 값을 정하지 않았어도 키는 있어야 한다. 키가 없으면 '정하지
            # 않았다'와 '이 프로그램이 그런 개념을 모른다'가 구분되지 않는다.
            errors.append(
                f"{group}: {', '.join(missing)} 키가 없다 "
                "(정하지 않았으면 null로 둔다)"
            )

    for path, (kind, _unit) in FIELDS.items():
        value = _get(data, path)
        if value is None:
            continue
        if kind == NUMBER and not _finite(value):
            errors.append(f"{path}: 유한한 숫자 또는 null이어야 한다")
        elif kind == BOOLEAN and not isinstance(value, bool):
            errors.append(f"{path}: true·false 또는 null이어야 한다")
        elif kind == TEXT and (not isinstance(value, str) or not value.strip()):
            errors.append(f"{path}: 비어 있지 않은 문자열 또는 null이어야 한다")

    for path in (
        "objective.target_annual_return_pct",
        "risk.max_loss_per_position_krw",
        "risk.max_portfolio_drawdown_pct",
        "concentration.max_single_position_pct_of_total",
        "concentration.max_sector_pct_of_total",
        "liquidity.min_cash_krw",
        "horizon.typical_holding_months",
        "horizon.liquidity_need_within_months",
    ):
        value = _get(data, path)
        if value is not None and _finite(value) and value < 0:
            errors.append(f"{path}: 음수일 수 없다")
    for path in (
        "concentration.max_single_position_pct_of_total",
        "concentration.max_sector_pct_of_total",
        "risk.max_portfolio_drawdown_pct",
    ):
        value = _get(data, path)
        if value is not None and _finite(value) and value > 100:
            errors.append(f"{path}: 100을 넘을 수 없다")

    denominator = data.get("denominator")
    if not isinstance(denominator, dict):
        errors.append("denominator: 객체가 필요하다")
    else:
        unknown = sorted(set(denominator) - {"included", "excluded", "note"})
        if unknown:
            errors.append(f"denominator: 알 수 없는 필드 {', '.join(unknown)}")
        for key in ("included", "excluded"):
            items = denominator.get(key)
            if not isinstance(items, list) or not all(
                isinstance(item, str) and item.strip() for item in items
            ):
                errors.append(f"denominator.{key}: 문자열 배열이 필요하다")

    style = _get(data, "risk.stop_style")
    if style is not None and style not in STOP_STYLES:
        errors.append(
            f"risk.stop_style: {' | '.join(sorted(STOP_STYLES))} 중 하나여야 한다"
        )

    deliberate = data.get("intentionally_unset")
    if not isinstance(deliberate, list):
        errors.append("intentionally_unset: 배열이 필요하다")
    else:
        for index, item in enumerate(deliberate):
            if not isinstance(item, dict) or set(item) - {"field", "why"}:
                errors.append(
                    f"intentionally_unset[{index}]: field·why만 허용한다"
                )
                continue
            field = item.get("field")
            if field not in FIELDS:
                errors.append(
                    f"intentionally_unset[{index}].field: 알 수 없는 경로 {field}"
                )
            elif _get(data, field) is not None:
                # 값을 정해 놓고 '일부러 비웠다'고 적을 수는 없다.
                errors.append(
                    f"intentionally_unset[{index}]: {field}에는 값이 들어 있다"
                )
            if not isinstance(item.get("why"), str) or not item["why"].strip():
                errors.append(
                    f"intentionally_unset[{index}].why: 왜 두지 않는지 밝혀야 한다"
                )

    alternatives = data.get("default_alternatives")
    if not isinstance(alternatives, list):
        errors.append("default_alternatives: 배열이 필요하다")
    else:
        for index, item in enumerate(alternatives):
            if not isinstance(item, dict) or set(item) - {"id", "label", "why"}:
                errors.append(
                    f"default_alternatives[{index}]: id·label·why만 허용한다"
                )
                continue
            for key in ("id", "label"):
                if not isinstance(item.get(key), str) or not item[key].strip():
                    errors.append(f"default_alternatives[{index}].{key}: 비었다")
    return errors


def deliberate_gaps(data: dict[str, Any]) -> dict[str, str]:
    """일부러 두지 않은 항목 -> 그 이유.

    `null` 하나로는 '아직 안 정했다'와 '쓰지 않기로 했다'를 구분할 수 없다.
    구분하지 않으면 손절가를 쓰지 않는 사용자에게 시스템이 영원히
    `UNDETERMINED` 를 돌려주게 된다.
    """
    return {
        item["field"]: item["why"]
        for item in (data.get("intentionally_unset") or [])
        if isinstance(item, dict) and item.get("field") in FIELDS
    }


def _unset_not_in_checks(
    data: dict[str, Any], checks: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """packet 에는 `checks` 가 못 담는 '일부러 비운 항목'만 싣는다.

    한도 대부분은 `checks` 에 `state: NO_LIMIT_BY_CHOICE` + `reason` 으로
    이미 들어간다. 같은 `why` 를 `intentionally_unset` 에도 실으면 매 판단마다
    같은 문장을 두 번 보낸다(실측 244 B). 다만 `horizon.typical_holding_months`
    처럼 산술 대조가 없는 항목은 `checks` 에 자리가 없으므로 여기 남겨야 한다 —
    빠지면 '아직 안 정함'과 '두지 않기로 함'의 구분이 사라진다.

    원본 `config/investor-policy.json` 은 그대로다. 줄이는 것은 전송분뿐이다.
    """
    covered = {check.get("limit") for check in checks}
    return [
        item
        for item in (data.get("intentionally_unset") or [])
        if isinstance(item, dict) and item.get("field") not in covered
    ]


def unset_fields(data: dict[str, Any]) -> list[str]:
    """사용자가 **아직 정하지 않은** 한도의 경로.

    일부러 비운 항목은 여기 넣지 않는다 — 그것은 공백이 아니라 결정이다.
    """
    chosen = deliberate_gaps(data)
    return sorted(
        path
        for path in FIELDS
        if _get(data, path) is None and path not in chosen
    )


def load_policy(path: Path | None = None) -> tuple[dict[str, Any] | None, list[str]]:
    """정책을 읽는다. 없으면 (None, []) — 없는 것은 오류가 아니다."""
    target = Path(path) if path else POLICY_PATH
    if not target.exists():
        return None, []
    try:
        data = load_json(target)
    except Exception as exc:  # noqa: BLE001 - 계약 오류를 그대로 실어 보낸다
        return None, [f"투자정책 파일을 읽을 수 없다: {exc}"]
    return data, validate_policy(data)


def _round(value: Any, digits: int) -> Any:
    return round(value, digits) if _finite(value) else None


def _check(
    *,
    limit_path: str,
    bound: str,
    limit_value: Any,
    now: Any,
    after: Any,
    digits: int = 2,
    unavailable_reason: str | None = None,
    chosen_reason: str | None = None,
) -> dict[str, Any]:
    """한도와 실제 수치의 산술 대조 한 줄.

    `breach` 는 부등호 비교의 결과일 뿐이다. 넘었으니 팔라는 뜻이 아니다.
    """
    unit = FIELDS[limit_path][1]
    row: dict[str, Any] = {
        "limit": limit_path,
        "bound": bound,
        "unit": unit,
        "limit_value": limit_value,
        "now": _round(now, digits),
        "after_proposed_trade": _round(after, digits),
        "breach_now": None,
        "breach_after_proposed_trade": None,
        "state": "OK",
    }
    if limit_value is None:
        # 일부러 두지 않은 것과 아직 정하지 않은 것을 다르게 말한다. 앞은
        # 사용자의 결정이므로 모델이 판단을 진행해야 하고, 뒤는 공백이라
        # 결론이 거기 걸리면 UNDETERMINED 가 정당하다.
        row["state"] = "NO_LIMIT_BY_CHOICE" if chosen_reason else "LIMIT_UNSET"
        if chosen_reason:
            row["reason"] = chosen_reason
        return row
    if unavailable_reason:
        row["state"] = "INPUT_UNAVAILABLE"
        row["reason"] = unavailable_reason
        return row
    if now is None and after is None:
        row["state"] = "INPUT_UNAVAILABLE"
        row["reason"] = "대조할 포트폴리오 수치가 없다"
        return row
    for source_key, target_key in (
        ("now", "breach_now"),
        ("after_proposed_trade", "breach_after_proposed_trade"),
    ):
        value = row[source_key]
        if value is None:
            continue
        row[target_key] = value > limit_value if bound == "MAX" else value < limit_value
    return row


def policy_for_context(
    policy: dict[str, Any] | None,
    portfolio: dict[str, Any] | None,
    *,
    errors: list[str] | None = None,
) -> dict[str, Any]:
    """packet 의 `context.investor_policy` 를 만든다.

    포트폴리오 수치는 다시 계산하지 않고 `_portfolio_summary` 가 이미 낸 값을
    받아 쓴다. 같은 숫자를 두 곳에서 계산하면 어느 쪽이 맞는지 판단해야 하고,
    그 판단은 PC 가 할 일이 아니다.
    """
    guidance = (
        "비어 있는 한도가 결론에 필요하면 action.direction=UNDETERMINED, "
        "undetermined_reason=NO_INVESTOR_POLICY 로 답한다. 없는 한도를 "
        "추측해 채우지 않는다"
    )
    if policy is None:
        return {
            "available": False,
            "reason": "config/investor-policy.json이 없다",
            "_role": (
                "사용자가 정한 자본 제약이다. 없으면 수량·분할매도 판단의 "
                "근거가 없다는 뜻이지 제약이 없다는 뜻이 아니다"
            ),
            "_note": guidance,
        }
    if errors:
        return {
            "available": False,
            "reason": "투자정책 계약 검증에 실패했다",
            "contract_errors": errors,
            "_note": guidance,
        }

    portfolio = portfolio or {}
    position = portfolio.get("existing_position") or {}
    trade = portfolio.get("proposed_trade") or {}

    exposure_now = position.get("value_at_review_price_krw")
    deployed = trade.get("estimated_deployed_krw")
    if not _finite(deployed):
        exposure_after = None
    elif not position.get("held"):
        exposure_after = deployed
    elif _finite(exposure_now):
        exposure_after = exposure_now + deployed
    else:
        # 보유 중인데 검토 가격 기준 평가액을 못 냈다(환율 누락 등). 0으로
        # 접으면 노출을 과소평가하므로 모른다고 남긴다.
        exposure_after = None

    chosen = deliberate_gaps(policy)
    max_loss = _get(policy, "risk.max_loss_per_position_krw")
    checks = [
        _check(
            limit_path="concentration.max_single_position_pct_of_total",
            bound="MAX",
            limit_value=_get(
                policy, "concentration.max_single_position_pct_of_total"
            ),
            now=position.get("weight_at_review_price_pct"),
            after=trade.get("post_trade_weight_total_assets_pct"),
            chosen_reason=chosen.get(
                "concentration.max_single_position_pct_of_total"
            ),
        ),
        _check(
            limit_path="concentration.max_sector_pct_of_total",
            bound="MAX",
            limit_value=_get(policy, "concentration.max_sector_pct_of_total"),
            now=None,
            after=None,
            chosen_reason=chosen.get("concentration.max_sector_pct_of_total"),
            # 직접 보유만으로 섹터 비중을 내면 ETF 안의 같은 섹터 노출이
            # 빠져 실제보다 작게 나온다. 이 사용자의 헬스케어 노출은 상당
            # 부분이 SCHD 안에 있으므로, 그 숫자를 내면 모델은 잘못된 여유가
            # 있다고 믿는다. 틀린 값을 주느니 없다고 말한다.
            unavailable_reason=(
                "ETF look-through가 없어 직접 보유만으로는 실제 섹터 노출을 "
                "낼 수 없다. 이 한도는 아직 기계적으로 대조하지 않는다"
            ),
        ),
        _check(
            limit_path="liquidity.min_cash_krw",
            bound="MIN",
            limit_value=_get(policy, "liquidity.min_cash_krw"),
            now=portfolio.get("cash_krw"),
            after=trade.get("cash_after_krw"),
            chosen_reason=chosen.get("liquidity.min_cash_krw"),
        ),
    ]

    # 두 한도가 서로 팽팽한지를 산술로 보여준다. 단일 종목을 최대 비중까지
    # 채웠을 때 그 종목이 몇 % 빠지면 전체 낙폭 예산을 다 쓰는가. 한도 위반이
    # 아니라 한도끼리의 관계이며, 그래서 수량을 정할 때 쓰는 값이다.
    cap = _get(policy, "concentration.max_single_position_pct_of_total")
    drawdown = _get(policy, "risk.max_portfolio_drawdown_pct")
    if _finite(cap) and _finite(drawdown) and cap > 0:
        checks.append(
            {
                "limit": "risk.max_portfolio_drawdown_pct",
                "unit": "PCT_OF_TOTAL_ASSETS",
                "limit_value": drawdown,
                "single_position_cap_pct": cap,
                "position_loss_consuming_whole_budget_pct": round(
                    min(drawdown / cap * 100, 100), 2
                ),
                "state": "OK",
                "_role": (
                    "한도 위반이 아니라 두 한도의 산술 관계다. 최대 비중까지 "
                    "채운 한 종목이 이만큼 빠지면 전체 낙폭 예산이 그 한 "
                    "종목에 다 걸린다"
                ),
            }
        )

    # 최대 손실 한도는 그 자체로 대조할 상대가 없다. 대신 노출로 나눠
    # "이 포지션에서 몇 % 하락까지가 한도인가"로 환산한다. 손절을 어디 둘지는
    # 여전히 모델이 정하며, 이 값은 그 선택이 한도 안인지 밖인지만 알려준다.
    implied = {
        "limit": "risk.max_loss_per_position_krw",
        "unit": "PCT_OF_POSITION_VALUE",
        "limit_value": max_loss,
        "exposure_now_krw": _round(exposure_now, 2),
        "exposure_after_proposed_trade_krw": _round(exposure_after, 2),
        "max_drawdown_now_pct": None,
        "max_drawdown_after_proposed_trade_pct": None,
        "state": (
            "OK"
            if max_loss is not None
            else "NO_LIMIT_BY_CHOICE"
            if "risk.max_loss_per_position_krw" in chosen
            else "LIMIT_UNSET"
        ),
        "_role": (
            "한도를 노출로 나눈 환산값이다. 손절가를 여기 두라는 뜻이 아니라, "
            "네가 정한 손절이 이 한도 안인지 밖인지 대조하라는 뜻이다"
        ),
    }
    if "risk.max_loss_per_position_krw" in chosen:
        implied["reason"] = chosen["risk.max_loss_per_position_krw"]
    if max_loss is not None:
        for exposure, key in (
            (exposure_now, "max_drawdown_now_pct"),
            (exposure_after, "max_drawdown_after_proposed_trade_pct"),
        ):
            if _finite(exposure) and exposure > 0:
                implied[key] = round(min(max_loss / exposure * 100, 100), 2)
    checks.append(implied)

    missing = unset_fields(policy)
    return {
        "available": True,
        "policy_version": policy.get("policy_version"),
        "base_currency": policy.get("base_currency"),
        "objective": policy.get("objective"),
        "horizon": policy.get("horizon"),
        "risk": policy.get("risk"),
        "concentration": policy.get("concentration"),
        "liquidity": policy.get("liquidity"),
        "instruments": policy.get("instruments"),
        "tax": policy.get("tax"),
        "denominator": policy.get("denominator"),
        "default_alternatives": policy.get("default_alternatives"),
        "intentionally_unset": _unset_not_in_checks(policy, checks),
        "unset": missing,
        "checks": checks,
        "_role": (
            "사용자가 정한 자본 제약이며 이 종목의 좋고 나쁨에 대한 판정이 "
            "아니다. breach는 부등호 비교의 결과일 뿐 매도·매수 지시가 아니다"
        ),
        "_note": guidance,
    }
