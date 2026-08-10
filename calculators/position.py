#!/usr/bin/env python3
"""보유 포지션의 결정론적 산술.

거래 원장에서 평단과 실현손익을 계산하고, 검토 가격 기준 평가손익·배수 환산·
세 분모 비중·수량 사다리를 낸다. 지금까지 이 계산은 모델이 추론 시점에 했다.
결정론적 계산을 확률적 장치에 맡기면 재현되지 않고 기록도 남지 않는다 — 같은
질문에 다음 턴에 다른 값이 나와도 아무도 잡지 못한다.

## 이 계산기가 하지 않는 것

**판정을 내지 않는다.** 싸다·비싸다, 사도 된다·줄여라, 비중이 과하다 같은 값은
출력에 없고 앞으로도 없어야 한다. 임계값도 여기서 정하지 않는다 — 모델이 세운
문턱을 받아 대조만 한다. 계산기가 판정을 내기 시작하면 모델은 그 판정을 근거로
받아들이고, 그 순간 판단의 주체가 조용히 바뀐다.

평단보다 위인지 아래인지 같은 값은 판정이 아니라 정의다. 그것을 "물타기인가"로
읽는 것은 모델의 몫이다.

## 원장과 스냅샷

평단은 두 경로로 나온다 — 거래 원장의 이동평균과, 증권사 스냅샷의
(평가액 − 평가손익) ÷ 수량이다. 둘을 합치지 않고 **각각 내고 차이를 보고한다.**
어느 쪽이 옳은지는 이 계산기가 정할 문제가 아니며, 벌어졌다는 사실 자체가
원장이나 스냅샷 중 하나가 낡았다는 신호다.

사용:
    python3.13 -m calculators.position 214450 --price 331000
    python3.13 -m calculators.position 214450 --price 331000 --add 5 10 20
    python3.13 -m calculators.position TSLA --price 460 --json
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import math
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cio.paths import KNOWLEDGE_DIR

SCHEMA_VERSION = "position-math/1.0.0"
COMPONENT = "position/ledger"

TRADES_DIR = KNOWLEDGE_DIR / "portfolio" / "trades"
HOLDINGS = KNOWLEDGE_DIR / "portfolio" / "holdings.csv"

# calculators/portfolio.py 와 같은 정의를 쓴다. 두 계산기가 서로 다른 분모로
# 같은 종목의 비중을 말하면 그 숫자는 비교할 수 없게 된다.
EQUITY_TYPES = {"STOCK", "ETF", "REIT", "EQUITY"}
NON_RISK_TYPES = {"CASH", "CASH_EQUIVALENT", "BOND", "UNCLASSIFIED"}

BUY_SIDES = {"BUY"}
SELL_SIDES = {"SELL"}
# 무상입고. 수량은 늘지만 지불한 돈이 없다. 원장이 단가를 적어 두므로 그 값으로
# 원가에 넣되, 몇 건이 그렇게 들어왔는지 따로 보고한다.
RECEIVE_SIDES = {"RECEIVE"}

# 원장과 스냅샷이 이만큼 벌어지면 보고에서 눈에 띄게 표시한다.
RECONCILE_TOLERANCE_PCT = 0.5

# 적립매수(정기 자동매수) 판별. 증권사 내역서가 note 에 남기는 표시를 쓴다 —
# 간격을 통계로 추정하면 우연히 규칙적인 수동 매수를 적립으로 오인한다.
RECURRING_MARK = "적립"
RECURRING_MIN_TRADES = 5

# 평단의 권위는 증권사 스냅샷이다 (사용자 확정, 2026-07-31).
#
# 두 값이 벌어졌을 때 어느 쪽이 옳은지를 처음에는 열어 뒀는데, 사용자가
# 파마리서치 298,894원·TSLA 480,707원으로 확정했고 둘 다 스냅샷 역산값과
# 같다. 즉 **원장이 재현하지 못하는 것이 있다.** TSLA 에서 -0.887% 벌어진
# 것이 그 증거다 — 원장 헤더의 "평단·실현손익을 이 파일만으로 계산할 수
# 있다"는 주석은 적어도 TSLA 에 대해서는 사실이 아니다.
#
# 그래도 원장을 계속 읽는 이유는 둘이다. 실현손익은 스냅샷에 아예 없고,
# 벌어지는 폭 자체가 원장이나 스냅샷 중 하나가 낡았다는 알람이다.
AVERAGE_COST_AUTHORITY = "BROKER_SNAPSHOT"
AVERAGE_COST_AUTHORITY_NOTE = (
    "평단의 권위는 증권사 스냅샷이다(사용자 확정 2026-07-31). 원장 평단은 "
    "교차검증용이며, 벌어지면 원장이 재현하지 못하는 항목이 있다는 신호다"
)


class PositionError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _display(path: Path) -> str:
    """보고용 경로. 저장소 밖이면 절대경로 그대로 쓴다.

    표시 문자열을 만들다가 계산이 죽는 것은 어떤 경우에도 옳지 않다.
    """
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _iso_date(value: Any) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(value).strip())
    except (ValueError, TypeError, AttributeError):
        return None


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig") as handle:
        lines = [line for line in handle if not line.lstrip().startswith("#")]
    return list(csv.DictReader(lines))


def _number(raw: Any, default: float | None = None) -> float | None:
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = float(str(raw).replace(",", ""))
    except ValueError:
        return default
    return value if math.isfinite(value) else default


def read_ledger(asset_id: str, currency_field: str) -> dict[str, Any]:
    """거래 원장을 이동평균법으로 접는다.

    매도는 그 시점의 평단으로 원가를 덜어내고 차액을 실현손익에 넣는다.
    수수료는 매수 시 원가에 더하고 매도 시 실현손익에서 뺀다 — 실제로 나간
    돈이기 때문이다.

    수수료·세금은 원장에 **항상 원화로** 적혀 있다(`fee_krw`·`tax_krw`).
    달러 기준으로 접을 때 그 값을 그대로 더하면 통화가 섞여 평단이 조용히
    부풀어 오른다. 그 행의 `fx_rate` 로 환산하며, 환율이 없는데 수수료가
    있으면 멈춘다 — 환율을 가정하는 것보다 계산을 거부하는 편이 낫다.
    """
    path = TRADES_DIR / f"{asset_id}.csv"
    if not path.exists():
        raise PositionError(
            f"{_display(path)} 가 없다. 이 종목의 거래 원장이 없으면 "
            "평단을 원장에서 계산할 수 없다"
        )
    rows = _rows(path)
    if not rows:
        raise PositionError(f"{_display(path)} 에 거래 행이 없다")

    quantity = 0.0
    cost = 0.0
    realized = 0.0
    fees = 0.0
    taxes = 0.0
    received_quantity = 0.0
    received_events = 0
    unknown_sides: set[str] = set()
    dates: list[str] = []

    for index, row in enumerate(rows, start=1):
        side = str(row.get("side") or "").strip().upper()
        count = _number(row.get("quantity"))
        price = _number(row.get(currency_field))
        fee_krw = _number(row.get("fee_krw"), 0.0) or 0.0
        tax_krw = _number(row.get("tax_krw"), 0.0) or 0.0
        settle = str(row.get("settle_date") or "").strip()
        if count is None or price is None or count <= 0 or price <= 0:
            raise PositionError(
                f"{path.name} {index}행: quantity 또는 {currency_field} 가 "
                "유효한 양수가 아니다"
            )
        if settle:
            dates.append(settle)
        fees += fee_krw
        taxes += tax_krw
        fee, tax = fee_krw, tax_krw
        if currency_field != "price_krw":
            rate = _number(row.get("fx_rate"))
            if fee_krw or tax_krw:
                if not rate or rate <= 0:
                    raise PositionError(
                        f"{path.name} {index}행: 수수료·세금이 원화로 적혀 "
                        "있는데 fx_rate 가 없다. 환율을 가정하면 평단이 "
                        "조용히 틀리므로 여기서 멈춘다"
                    )
                fee, tax = fee_krw / rate, tax_krw / rate
            else:
                fee = tax = 0.0
        if side in BUY_SIDES:
            cost += count * price + fee
            quantity += count
        elif side in RECEIVE_SIDES:
            cost += count * price
            quantity += count
            received_quantity += count
            received_events += 1
        elif side in SELL_SIDES:
            if quantity <= 0:
                raise PositionError(
                    f"{path.name} {index}행: 보유 수량이 0인데 매도가 있다"
                )
            unit = cost / quantity
            realized += count * price - fee - tax - count * unit
            cost -= count * unit
            quantity -= count
        else:
            unknown_sides.add(side or "(빈값)")
    if unknown_sides:
        raise PositionError(
            f"{path.name}: 알 수 없는 side {sorted(unknown_sides)} — "
            "처리 규칙이 없는 거래를 조용히 무시하면 평단이 틀린다"
        )
    # 부동소수 잔차로 남은 미세 수량을 0 으로 보지 않는다. 값을 그대로 낸다.
    average = cost / quantity if quantity > 0 else None
    return {
        "source": _display(path),
        "method": "MOVING_AVERAGE",
        "price_field": currency_field,
        "trade_count": len(rows),
        "first_settle_date": min(dates) if dates else None,
        "last_settle_date": max(dates) if dates else None,
        "quantity": round(quantity, 6),
        "cost_basis": round(cost, 2),
        "average_cost": round(average, 4) if average is not None else None,
        "realized_pnl": round(realized, 2),
        "fees_paid_krw": round(fees, 2),
        "taxes_paid_krw": round(taxes, 2),
        "free_receipts": {
            "events": received_events,
            "quantity": round(received_quantity, 6),
            "_note": (
                "무상입고분이다. 원장에 적힌 단가로 원가에 넣었다 — 실제로 "
                "지불한 돈은 없으므로 평단 해석에 영향을 준다"
            ),
        } if received_events else None,
        "_note": (
            "settle_date 는 결제 기준일이지 체결일이 아니다. 체결일 기준 "
            "손익과는 시점이 다르다"
        ),
    }


def recurring_pattern(asset_id: str, today: dt.date | None = None) -> dict[str, Any] | None:
    """원장에서 적립매수(정기 자동매수) 흔적을 읽는다.

    사용자가 자동 적립을 걸어 두면 원장에 같은 수량의 작은 매수가 매 영업일
    쌓인다. RXRX 는 142건 중 129건이 그것이었다. 그런데 판단 계약에는 그런
    개념이 없어서 — `action.direction` 은 ADD/HOLD/TRIM/EXIT 뿐이다 — 모델은
    **이미 돌아가고 있는 매수 프로그램을 모른 채** "지금 살까"에 답하게 된다.

    적립을 멈추거나 재개하는 것도 판단이다. 실제로 사용자는 2026-05 에
    멈췄는데 그 결정은 어디에도 기록되지 않았다.

    여기서는 흔적만 센다. 계속할지 멈출지는 모델이 정한다.
    """
    path = TRADES_DIR / f"{asset_id}.csv"
    if not path.exists():
        return None
    today = today or dt.date.today()
    rows = _rows(path)
    marked = [
        row for row in rows
        if RECURRING_MARK in str(row.get("note") or "")
        and str(row.get("side") or "").strip().upper() in BUY_SIDES
    ]
    if len(marked) < RECURRING_MIN_TRADES:
        return None
    dates = sorted(
        date for row in marked
        if (date := _iso_date(row.get("settle_date"))) is not None
    )
    if not dates:
        return None
    # 읽힌 수량만 모은다. 빈 칸이나 숫자가 아닌 칸은 `_number` 가 None 을 내는데,
    # 그것까지 집합에 넣으면 전 행이 안 읽힌 원장에서 원소가 {None} 하나가 되어
    # "수량이 하나로 모였다"로 통과한 뒤 빈 목록의 첫 원소를 꺼내다 죽는다.
    readable_quantities = [
        quantity
        for row in marked
        if (quantity := _number(row.get("quantity"))) is not None
    ]
    days_since_last = (today - dates[-1]).days
    return {
        "detected": True,
        "marker": RECURRING_MARK,
        "trade_count": len(marked),
        "share_of_all_trades_pct": round(len(marked) / len(rows) * 100, 2),
        "first_date": dates[0].isoformat(),
        "last_date": dates[-1].isoformat(),
        "days_since_last": days_since_last,
        # 모든 행의 수량을 읽었고 그 값이 하나로 모일 때만 '몇 주씩'이라고
        # 말한다. 한 행이라도 못 읽었으면 모르는 것이지 같은 것이 아니다.
        "quantity_per_trade": (
            readable_quantities[0]
            if len(readable_quantities) == len(marked)
            and len(set(readable_quantities)) == 1
            else None
        ),
        "_role": (
            "원장에 남은 정기 자동매수 흔적이다. 원장에는 체결된 거래만 있고 "
            "프로그램 설정은 없다 — 지금 켜져 있는지 꺼져 있는지는 여기서 알 "
            "수 없으며 마지막 적립일과 경과일이 전부다"
        ),
    }


def read_snapshot(asset_id: str) -> dict[str, Any] | None:
    """증권사 스냅샷에서 같은 값을 역산한다."""
    if not HOLDINGS.exists():
        return None
    as_of = None
    with HOLDINGS.open(encoding="utf-8-sig") as handle:
        for line in handle:
            if line.lstrip().startswith("#") and "기준일" in line:
                for token in line.replace("/", " ").split():
                    if len(token) == 10 and token[4] == "-":
                        as_of = token
                        break
            if as_of:
                break
    for row in _rows(HOLDINGS):
        if str(row.get("asset_id") or "").strip() != asset_id:
            continue
        value = _number(row.get("value_krw"))
        quantity = _number(row.get("quantity"))
        pnl = _number(row.get("pnl_krw"))
        cost = value - pnl if value is not None and pnl is not None else None
        return {
            "as_of": as_of,
            "value_krw": value,
            "quantity": quantity,
            "cost_basis_krw": round(cost, 2) if cost is not None else None,
            "average_cost_krw": (
                round(cost / quantity, 4)
                if cost is not None and quantity else None
            ),
            "_method": "(평가액 − 평가손익) ÷ 수량",
        }
    return None


def reconcile(
    ledger: dict[str, Any], snapshot: dict[str, Any] | None, is_krw: bool
) -> dict[str, Any]:
    """원장과 스냅샷을 대조한다. 어느 쪽이 옳은지는 정하지 않는다."""
    if not snapshot:
        return {
            "available": False,
            "comparable": False,
            "average_cost_diff": None,
            "within_tolerance": None,
            "_reason": "holdings.csv 에 이 자산이 없다",
        }
    quantity_diff = None
    if snapshot.get("quantity") is not None:
        quantity_diff = round(ledger["quantity"] - snapshot["quantity"], 6)
    average_diff = None
    average_diff_pct = None
    comparable = is_krw and ledger.get("average_cost") is not None
    if comparable and snapshot.get("average_cost_krw"):
        average_diff = round(
            ledger["average_cost"] - snapshot["average_cost_krw"], 4
        )
        average_diff_pct = round(
            average_diff / snapshot["average_cost_krw"] * 100, 4
        )
    return {
        "available": True,
        "snapshot_as_of": snapshot.get("as_of"),
        "quantity_ledger": ledger["quantity"],
        "quantity_snapshot": snapshot.get("quantity"),
        "quantity_diff": quantity_diff,
        "average_cost_ledger": ledger.get("average_cost"),
        "average_cost_snapshot": snapshot.get("average_cost_krw"),
        "average_cost_diff": average_diff,
        "average_cost_diff_pct": average_diff_pct,
        "comparable": comparable,
        "within_tolerance": (
            None
            if average_diff_pct is None
            else abs(average_diff_pct) <= RECONCILE_TOLERANCE_PCT
        ),
        "tolerance_pct": RECONCILE_TOLERANCE_PCT,
        "authority": AVERAGE_COST_AUTHORITY,
        "_note": (
            "통화가 다르면 평단은 비교하지 않는다. 원장의 자산통화 평단과 "
            "스냅샷의 원화 평단은 같은 값이 아니다"
            if not comparable else AVERAGE_COST_AUTHORITY_NOTE
        ),
    }


def at_price(
    ledger: dict[str, Any], price: float, currency: str
) -> dict[str, Any]:
    quantity = ledger["quantity"]
    cost = ledger["cost_basis"]
    value = quantity * price
    average = ledger.get("average_cost")
    position = None
    if average:
        if price > average:
            position = "ABOVE_AVERAGE_COST"
        elif price < average:
            position = "BELOW_AVERAGE_COST"
        else:
            position = "AT_AVERAGE_COST"
    return {
        "review_price": price,
        "currency": currency,
        "market_value": round(value, 2),
        "unrealized_pnl": round(value - cost, 2),
        "unrealized_pnl_pct": (
            round((value / cost - 1) * 100, 4) if cost > 0 else None
        ),
        "price_vs_average_cost": position,
        "price_vs_average_cost_pct": (
            round((price / average - 1) * 100, 4) if average else None
        ),
        "_role": (
            "검토 가격에서의 평가 상태다. 평단 위/아래는 사실이며 그것을 "
            "물타기·불타기로 읽는 것은 판단이다 — 여기서 하지 않는다"
        ),
    }


def multiples(
    asset_id: str, price: float, *, estimates_only: bool = False
) -> dict[str, Any] | None:
    """검토 가격을 컨센서스 EPS로 나눈다. 그 배수의 높낮이는 말하지 않는다.

    `estimates_only` 는 확정연도를 뺀다. 오늘 주가를 2023년 EPS로 나눈 값은
    호기심이지 판단 입력이 아니다 — 과거 배수 밴드는 그때의 주가로 계산한
    consensus 의 `per_band_actual` 이 답한다. packet 예산이 있는 자리에서는
    그 구분이 곧 자리값이다.
    """
    path = KNOWLEDGE_DIR / "consensus" / f"{asset_id}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    annual = ((data.get("estimates") or {}).get("annual")) or []
    rows = []
    for entry in annual:
        eps = _number(entry.get("eps"))
        if not eps or eps <= 0:
            continue
        if estimates_only and entry.get("actual"):
            continue
        rows.append({
            "period": entry.get("period"),
            "actual": entry.get("actual"),
            "eps": eps,
            "per_at_review_price": round(price / eps, 4),
        })
    if not rows:
        return None
    return {
        "source": _display(path),
        "consensus_as_of": data.get("as_of"),
        "source_grade": data.get("source_grade"),
        "rows": rows,
        "_note": (
            "검토 가격 ÷ 컨센서스 EPS 다. 컨센서스는 2차 출처이며 이 배수가 "
            "높은지 낮은지는 동종·자체 밴드와 비교해야 하고 그 비교는 모델이 한다"
        ),
    }


def _denominators() -> dict[str, float] | None:
    if not HOLDINGS.exists():
        return None
    total = 0.0
    equity = 0.0
    risk = 0.0
    for row in _rows(HOLDINGS):
        value = _number(row.get("value_krw"))
        if value is None:
            continue
        asset_type = str(row.get("asset_type") or "").strip().upper()
        total += value
        if asset_type in EQUITY_TYPES:
            equity += value
        if asset_type not in NON_RISK_TYPES:
            risk += value
    return {"total_assets": total, "equity_sleeve": equity, "risk_assets": risk}


def weights(
    asset_id: str, value_krw: float, added_krw: float = 0.0
) -> dict[str, Any] | None:
    """세 분모로 비중을 낸다. 한도와의 비교는 하지 않는다.

    `added_krw` 는 이 포지션을 늘리는 데 쓴 현금이고, `value_krw` 는 그 결과인
    포지션 전체의 검토가 평가액이다.

    **현금으로 주식을 사면 총자산은 변하지 않는다** — 현금이 준 만큼 주식이
    늘 뿐이다. 그래서 총자산 분모에서는 쓴 현금을 빼야 한다. 반면 슬리브와
    위험자산 분모는 현금을 포함하지 않으므로 그 이동만큼 실제로 커지고, 그
    증가는 이미 `value_krw` 안에 들어 있다. 여기에 `added_krw` 를 또 더하면
    이중 계상이 되어 비중이 실제보다 낮게 나온다.
    """
    base = _denominators()
    if not base:
        return None
    snapshot = read_snapshot(asset_id) or {}
    existing = snapshot.get("value_krw") or 0.0
    total = base["total_assets"] - existing + value_krw - added_krw
    equity = base["equity_sleeve"] - existing + value_krw
    risk = base["risk_assets"] - existing + value_krw
    return {
        "value_krw": round(value_krw, 2),
        "total_assets_pct": round(value_krw / total * 100, 4) if total > 0 else None,
        "equity_sleeve_pct": (
            round(value_krw / equity * 100, 4) if equity > 0 else None
        ),
        "risk_assets_pct": round(value_krw / risk * 100, 4) if risk > 0 else None,
        "denominators_krw": {
            "total_assets": round(total, 2),
            "equity_sleeve": round(equity, 2),
            "risk_assets": round(risk, 2),
        },
        "_definitions": {
            "equity_sleeve": "STOCK·ETF·REIT 평가자산",
            "risk_assets": "CASH·CASH_EQUIVALENT·BOND를 제외한 평가자산",
            "_same_as": "calculators/portfolio.py 와 같은 정의다",
        },
        "_staleness": (
            "이 종목 외 보유분은 holdings.csv 스냅샷 평가액 그대로다. 그 사이 "
            "시장이 움직였다면 분모가 낡았고 비중은 그만큼 틀린다"
        ),
        "_role": "노출 측정치다. 한도 판정이나 매도 사유가 아니다",
    }


def ladder(
    asset_id: str,
    ledger: dict[str, Any],
    price: float,
    steps: list[float],
    integer_shares: bool,
) -> list[dict[str, Any]]:
    """추가 수량별로 평단과 비중이 어디로 가는지 낸다.

    기본 단계는 **현재 보유 수량의 비율**이다. "얼마를 사야 하는가"에 대한
    제안이 아니라, 가진 것을 기준으로 한 눈금이다.
    """
    cash = 0.0
    for row in _rows(HOLDINGS) if HOLDINGS.exists() else []:
        if str(row.get("asset_type") or "").strip().upper() == "CASH":
            cash += _number(row.get("value_krw")) or 0.0
    rows = []
    for step in steps:
        count = math.floor(step) if integer_shares else step
        if count <= 0:
            continue
        added_cost = count * price
        new_quantity = ledger["quantity"] + count
        new_cost = ledger["cost_basis"] + added_cost
        new_average = new_cost / new_quantity if new_quantity else None
        rows.append({
            "add_quantity": count,
            "add_cost": round(added_cost, 2),
            "cash_after": round(cash - added_cost, 2),
            # 보유 현금보다 큰 눈금인지의 산술이다. 사라·사지 말라가 아니다.
            "exceeds_available_cash": added_cost > cash,
            "new_quantity": round(new_quantity, 6),
            "new_average_cost": round(new_average, 4) if new_average else None,
            "average_cost_change": (
                round(new_average - ledger["average_cost"], 4)
                if new_average and ledger.get("average_cost") else None
            ),
            "weights_after": weights(
                asset_id, new_quantity * price, added_krw=added_cost
            ),
        })
    return rows


def build(
    asset_id: str,
    price: float,
    *,
    currency: str = "KRW",
    add_steps: list[float] | None = None,
) -> dict[str, Any]:
    is_krw = currency == "KRW"
    ledger = read_ledger(asset_id, "price_krw" if is_krw else "price_usd")
    snapshot = read_snapshot(asset_id)
    value_krw = ledger["quantity"] * price if is_krw else None
    if add_steps is None:
        # 보유 수량의 10·25·50·100%. 절대 수량을 제안하지 않으려는 눈금이다 —
        # "얼마를 사라"가 아니라 "가진 것의 몇 배인가"로 눈금을 잡는다.
        add_steps = [ledger["quantity"] * ratio for ratio in (0.1, 0.25, 0.5, 1.0)]
        ladder_basis = "현재 보유 수량의 10·25·50·100%"
    else:
        ladder_basis = "요청한 수량"
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "component": COMPONENT,
        "asset_id": asset_id,
        "computed_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "currency": currency,
        "ledger": ledger,
        "snapshot": snapshot,
        "reconciliation": reconcile(ledger, snapshot, is_krw),
        "at_review_price": at_price(ledger, price, currency),
        "recurring_buy": recurring_pattern(asset_id),
        "multiples": multiples(asset_id, price),
        "_not_a_judgment": (
            "전부 결정론적 산술이다. 싸다·비싸다, 사도 된다·줄여라 같은 값은 "
            "여기 없고 앞으로도 없어야 한다. 임계값도 이 계산기가 정하지 "
            "않는다 — 모델이 세운 문턱을 받아 대조할 뿐이다"
        ),
    }
    if is_krw and value_krw is not None:
        result["weights"] = weights(asset_id, value_krw)
        result["ladder_basis"] = ladder_basis
        result["ladder"] = ladder(
            asset_id, ledger, price, add_steps, integer_shares=True
        )
    return result


def _fmt(value: float | None, currency: str, *, signed: bool = False) -> str:
    """통화에 맞는 자릿수로 찍는다.

    원화 자릿수를 달러에 그대로 쓰면 $3.75 평단이 "4달러"가 된다. 3달러짜리
    주식에서 그 반올림은 손익 판단을 통째로 지운다.
    """
    if value is None:
        return "-"
    digits = 0 if currency == "KRW" else 2
    sign = "+" if signed else ""
    return f"{value:{sign},.{digits}f}"


def render(result: dict[str, Any]) -> None:
    ledger = result["ledger"]
    currency = result["currency"]
    unit = "원" if currency == "KRW" else "달러"
    print("=" * 76)
    print(f"포지션 산술 — {result['asset_id']}   검토가 "
          f"{result['at_review_price']['review_price']:,.0f}{unit}")
    print("=" * 76)
    print(f"원장 {ledger['trade_count']}건 "
          f"({ledger['first_settle_date']} ~ {ledger['last_settle_date']}, "
          f"{ledger['method']})")
    print(f"  보유 수량      {ledger['quantity']:,.6f}")
    print(f"  취득원가       {_fmt(ledger['cost_basis'], currency):>18}{unit}")
    print(f"  평단           {_fmt(ledger['average_cost'], currency):>18}{unit}")
    print(f"  실현손익       {_fmt(ledger['realized_pnl'], currency, signed=True):>18}{unit}")
    print(f"  낸 수수료·세금 {ledger['fees_paid_krw']:,.0f} / "
          f"{ledger['taxes_paid_krw']:,.0f}원")
    if ledger.get("free_receipts"):
        free = ledger["free_receipts"]
        print(f"  무상입고       {free['events']}건 {free['quantity']:,.6f}주")

    rep = result.get("recurring_buy")
    if rep:
        per = (f"{rep['quantity_per_trade']:g}주씩 "
               if rep.get("quantity_per_trade") else "")
        print(f"\n적립매수  {rep['trade_count']}건 "
              f"({rep['share_of_all_trades_pct']}% of 전체) {per}"
              f"{rep['first_date']} ~ {rep['last_date']}")
        print(f"  마지막 적립 이후 {rep['days_since_last']}일 "
              f"(프로그램 켜짐/꺼짐 여부는 원장에 없다)")

    rec = result["reconciliation"]
    print("\n원장 ↔ 증권사 스냅샷")
    if not rec.get("available"):
        print(f"  대조 불가 — {rec.get('_reason')}")
    elif not rec.get("comparable"):
        print(f"  평단 비교 불가 — {rec['_note']}")
    else:
        mark = "일치" if rec["within_tolerance"] else "⚠️ 벌어짐"
        print(f"  평단 원장 {rec['average_cost_ledger']:,.0f} vs "
              f"스냅샷 {rec['average_cost_snapshot']:,.0f} "
              f"(차이 {rec['average_cost_diff']:+,.0f}, "
              f"{rec['average_cost_diff_pct']:+.3f}%) — {mark}")
        print(f"  수량 차이 {rec['quantity_diff']:+,.6f}  "
              f"(스냅샷 기준일 {rec['snapshot_as_of']})")

    at = result["at_review_price"]
    print(f"\n검토가 평가")
    print(f"  평가액         {_fmt(at['market_value'], currency):>18}{unit}")
    print(f"  평가손익       {_fmt(at['unrealized_pnl'], currency, signed=True):>18}{unit} "
          f"({at['unrealized_pnl_pct']:+.2f}%)")
    print(f"  평단 대비      {at['price_vs_average_cost']} "
          f"({at['price_vs_average_cost_pct']:+.2f}%)")

    mult = result.get("multiples")
    if mult:
        print(f"\n배수 환산 (컨센서스 {mult['consensus_as_of']}, "
              f"{mult['source_grade']})")
        for row in mult["rows"]:
            tag = "확정" if row["actual"] else "추정"
            print(f"  {row['period']} {tag}  EPS {row['eps']:>10,.0f}  "
                  f"PER {row['per_at_review_price']:>6.2f}배")

    weight = result.get("weights")
    if weight:
        print("\n비중 (검토가 기준)")
        print(f"  총자산 {weight['total_assets_pct']:.2f}%  /  "
              f"주식슬리브 {weight['equity_sleeve_pct']:.2f}%  /  "
              f"위험자산 {weight['risk_assets_pct']:.2f}%")

    rungs = result.get("ladder")
    if rungs:
        print(f"\n추가 매수 눈금 ({result.get('ladder_basis')})")
        print(f"  {'추가':>6}{'금액':>15}{'새 평단':>13}{'평단변화':>11}"
              f"{'총자산':>8}{'슬리브':>8}{'잔여현금':>15}")
        for row in rungs:
            after = row["weights_after"]
            over = "!" if row["exceeds_available_cash"] else " "
            print(f"  {row['add_quantity']:>6,.0f}{row['add_cost']:>15,.0f}"
                  f"{row['new_average_cost']:>13,.0f}"
                  f"{row['average_cost_change']:>+11,.0f}"
                  f"{after['total_assets_pct']:>7.2f}%"
                  f"{after['equity_sleeve_pct']:>7.2f}%"
                  f"{row['cash_after']:>14,.0f}{over}")
        if any(row["exceeds_available_cash"] for row in rungs):
            print("  ! = 보유 현금을 넘는 눈금이다 (산술이며 제약 판정이 아니다)")
    print("-" * 76)
    print(f"※ {result['_not_a_judgment']}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="보유 포지션의 결정론적 산술 (판정은 하지 않는다)"
    )
    parser.add_argument("asset_id")
    parser.add_argument("--price", type=float, required=True, help="검토 가격")
    parser.add_argument("--currency", default="KRW", choices=["KRW", "USD"])
    parser.add_argument(
        "--add", type=float, nargs="+",
        help="추가 수량 눈금. 없으면 보유 수량의 10·25·50·100%%",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    try:
        result = build(
            args.asset_id, args.price,
            currency=args.currency, add_steps=args.add,
        )
    except PositionError as exc:
        print(f"오류: {exc.message}", file=sys.stderr)
        raise SystemExit(1)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        render(result)


if __name__ == "__main__":
    main()
