"""Budget arithmetic for a model-authored conditional entry plan; no execution.

Prices and shares are expressed in a declared reference share unit. The caller
supplies each stage's share multiplier (e.g. 2 after a 1-for-1 bonus). The module
does not infer eligibility, corporate actions, prices, conditions or probabilities.
"""

import argparse
import json
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from pathlib import Path


def number(value, *, positive=False):
    if isinstance(value, bool):
        raise ValueError("boolean is not a number")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("invalid number") from exc
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise ValueError("expected a finite positive/nonnegative number")
    return result


def calculate(spec):
    currency = spec["currency"]
    if currency not in ("KRW", "USD"):
        raise ValueError("unsupported currency")
    if not spec.get("reference_basis") or not spec.get("stages"):
        raise ValueError("reference_basis and stages are required")
    budget = number(spec["budget"], positive=True)
    stages, allocated, deployed, units = [], Decimal(0), Decimal(0), Decimal(0)
    for stage in spec["stages"]:
        if stage.get("currency", currency) != currency:
            raise ValueError("mixed currencies require a separate dated FX calculation")
        if not stage.get("condition"):
            raise ValueError("each stage needs a model-authored condition")
        cash = number(stage["budget"], positive=True)
        ref_price = number(stage["reference_price"], positive=True)
        multiplier = number(stage["share_multiplier"], positive=True)
        quote = ref_price / multiplier
        qty = int((cash / quote).to_integral_value(rounding=ROUND_FLOOR))
        cost = qty * quote
        ref_units = Decimal(qty) / multiplier
        allocated += cash
        deployed += cost
        units += ref_units
        stages.append({**stage, "quoted_price": float(quote), "quantity": qty,
                       "deployed": float(cost), "unspent": float(cash - cost),
                       "reference_units": float(ref_units)})
    if allocated > budget:
        raise ValueError("sum of stage budgets exceeds total budget")
    average = deployed / units if units else None
    outcomes = {}
    for name, price in spec.get("terminal_reference_prices", {}).items():
        terminal = number(price)
        pnl = terminal * units - deployed
        outcomes[name] = {"terminal_reference_price": float(terminal), "pnl": float(pnl),
                          "return_on_deployed_pct": float(pnl / deployed * 100) if deployed else None}
    return {"schema_version": "staged-entry-calculation/1.0.0", "currency": currency,
            "reference_basis": spec["reference_basis"], "budget": float(budget),
            "stages": stages, "allocated_budget": float(allocated),
            "reserved_budget": float(budget - allocated), "total_deployed": float(deployed),
            "total_unspent": float(budget - deployed), "reference_units": float(units),
            "average_reference_price": float(average) if average is not None else None,
            "all_stages_arithmetic": outcomes,
            "note": "모든 단계의 조건 확인 후 해당 가격에 배분할 때의 산술이며 "
                    "조건 충족·체결 예측이나 주문이 아니다. 세금·수수료·슬리피지 제외."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=Path)
    args = parser.parse_args()
    print(json.dumps(calculate(json.loads(args.spec.read_text())), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
