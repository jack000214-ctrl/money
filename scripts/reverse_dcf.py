#!/usr/bin/env python3
"""역산 밸류에이션 — 현재 가격이 요구하는 성장률과 마진을 계산한다.

이 도구가 답하는 질문은 "이 주식이 얼마짜리냐"가 아니다.
**"지금 가격이 맞으려면 회사가 무엇을 해내야 하는가"** 다.

    현재가 330달러 → 향후 10년 FCF 연 22.4% 성장을 요구한다.
    → 사용자가 가져온 근거가 22.4%를 정당화하는가?

이렇게 물으면 논지를 반박할 수 있다. "타점이 좋아 보인다"는 반박할 수 없다.

모델: 2단계 FCF 할인 (1단계 N년 정률성장 → 고든 영구성장)

    EV  = Σ FCF₀(1+g)ᵗ/(1+r)ᵗ  +  FCF₀(1+g)ᴺ(1+gₜ)/((r−gₜ)(1+r)ᴺ)
    주당가치 = (EV − 순부채) / 주식수

산출물은 `knowledge/calculations/CALC-*.json` 에 저장된다.
`calculation_id` 는 모델과 입력의 SHA-256 지문이므로, 가정을 바꾸면 ID 가
바뀌고 산출물을 손으로 고치면 계약 검증에서 걸린다 (contracts.py).

확률은 계산하지 않는다. 수익률은 계산기가, 확률은 분석가가 낸다.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from contracts import (SCENARIO_NAMES, SCHEMA_VERSION, ContractError,  # noqa: E402
                       calculation_id_for, save_validated_calculation)

CALCULATIONS_DIR = ROOT / "knowledge" / "calculations"
MODEL = "reverse_dcf"
MODEL_VERSION = "1.0.0"

# 1단계 성장률 탐색 범위. 유한합이라 할인율을 넘어도 발산하지 않는다.
GROWTH_BOUNDS = (-0.95, 2.0)
# FCF 마진 탐색 범위. 음수 마진으로는 이 모델이 성립하지 않는다.
MARGIN_BOUNDS = (0.0001, 0.95)
BISECTION_STEPS = 200


def fail(msg):
    print(f"오류: {msg}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------- 모델

def equity_value_per_share(fcf, growth, *, discount_rate, terminal_growth,
                           years, net_debt, shares):
    """2단계 FCF 할인의 주당 가치."""
    r, g, gt = discount_rate, growth, terminal_growth
    stage_one = 0.0
    for t in range(1, years + 1):
        stage_one += fcf * (1 + g) ** t / (1 + r) ** t
    terminal_fcf = fcf * (1 + g) ** years * (1 + gt)
    terminal_value = terminal_fcf / (r - gt) / (1 + r) ** years
    return (stage_one + terminal_value - net_debt) / shares


def solve(target_price, lo, hi, value_of):
    """value_of(x) == target_price 인 x 를 이분법으로 찾는다.

    value_of 는 x 에 대해 단조증가여야 한다 (FCF>0 인 성장률·마진 모두 그렇다).
    """
    if value_of(lo) > target_price:
        return lo, "BELOW_RANGE"
    if value_of(hi) < target_price:
        return hi, "ABOVE_RANGE"
    for _ in range(BISECTION_STEPS):
        mid = (lo + hi) / 2
        if value_of(mid) < target_price:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2, "SOLVED"


def annualized_pct(value, price, horizon_months):
    """주당가치와 현재가의 격차를 연율로 바꾼다.

    IPS 요구수익률이 연율이므로 시나리오 수익률도 연율이어야 비교가 성립한다.
    가격이 horizon_months 안에 가치로 수렴한다는 가정이 들어간다.
    """
    if value <= 0:
        return -100.0
    total = value / price
    annual = total ** (12.0 / horizon_months) - 1.0
    return max(-100.0, annual * 100.0)


# ---------------------------------------------------------------- 입력

def parse_pairs(items, field, cast=float):
    out = {}
    for item in items or []:
        if "=" not in item:
            fail(f"--{field}: 'bear=5' 형식이어야 한다 (받은 값: {item!r})")
        key, _, raw = item.partition("=")
        key = key.strip()
        if key not in SCENARIO_NAMES:
            fail(f"--{field}: 시나리오는 {', '.join(SCENARIO_NAMES)} 뿐이다 "
                 f"(받은 값: {key!r})")
        try:
            out[key] = cast(raw)
        except ValueError:
            fail(f"--{field} {key}: 숫자가 아니다 ({raw!r})")
    return out


def build_inputs(a, fcf):
    """지문에 들어갈 입력. 산출물에 영향을 주는 값은 빠짐없이 넣는다."""
    inputs = {
        "price": a.price,
        "shares": a.shares,
        "fcf": fcf,
        "net_debt": a.net_debt,
        "discount_rate_pct": a.discount_rate,
        "terminal_growth_pct": a.terminal_growth,
        "years": a.years,
    }
    if a.revenue is not None:
        inputs["revenue"] = a.revenue
    if a.fcf_margin is not None:
        inputs["fcf_margin_pct"] = a.fcf_margin
    return inputs


def validate_common(a):
    if a.price <= 0:
        fail("--price 는 0보다 커야 한다")
    if a.shares <= 0:
        fail("--shares 는 0보다 커야 한다")
    if a.years < 1:
        fail("--years 는 1 이상이어야 한다")
    if a.discount_rate <= a.terminal_growth:
        fail(f"할인율 {a.discount_rate}% 가 영구성장률 {a.terminal_growth}% 보다 "
             "높아야 한다. 그렇지 않으면 잔존가치가 발산한다")


def resolve_fcf(a):
    """FCF 를 직접 받거나 매출×마진으로 만든다."""
    if a.fcf is not None:
        fcf = a.fcf
    elif a.revenue is not None and a.fcf_margin is not None:
        fcf = a.revenue * a.fcf_margin / 100.0
    else:
        fail("--fcf 또는 (--revenue 와 --fcf-margin) 이 필요하다")
    if fcf <= 0:
        fail(f"FCF 가 {fcf:,.0f} 다. 잉여현금흐름이 음수인 회사에 정률성장 DCF 를 "
             "쓰면 성장할수록 가치가 낮아지는 값이 나온다. 흑자 전환 경로를 "
             "가정한 별도 모델이 필요하다 (07-strategy-and-decision-policy.md §10)")
    return fcf


# ---------------------------------------------------------------- implied

def cmd_implied(a):
    validate_common(a)
    fcf = resolve_fcf(a)
    common = dict(discount_rate=a.discount_rate / 100.0,
                  terminal_growth=a.terminal_growth / 100.0,
                  years=a.years, net_debt=a.net_debt, shares=a.shares)

    if a.solve_for == "growth":
        value_of = lambda g: equity_value_per_share(fcf, g, **common)  # noqa: E731
        lo, hi = GROWTH_BOUNDS
        answer, status = solve(a.price, lo, hi, value_of)
        label, unit, shown = "요구 FCF 성장률", "%", answer * 100
    else:
        if a.revenue is None:
            fail("--solve-for margin 에는 --revenue 가 필요하다")
        if a.growth_assumption is None:
            fail("--solve-for margin 에는 --growth-assumption 이 필요하다")
        g = a.growth_assumption / 100.0
        value_of = lambda m: equity_value_per_share(  # noqa: E731
            a.revenue * m, g, **common)
        lo, hi = MARGIN_BOUNDS
        answer, status = solve(a.price, lo, hi, value_of)
        label, unit, shown = "요구 FCF 마진", "%", answer * 100

    if a.json:
        print(json.dumps({"solve_for": a.solve_for, "value": shown,
                          "status": status}, ensure_ascii=False))
        return 0

    print("=" * 74)
    print(f"역산 밸류에이션 — {a.instrument}")
    print("=" * 74)
    print(f"현재가        {a.price:>16,.2f}")
    print(f"FCF           {fcf:>16,.0f}")
    print(f"주식수        {a.shares:>16,.0f}")
    print(f"순부채        {a.net_debt:>16,.0f}")
    print(f"할인율        {a.discount_rate:>16,.2f}%")
    print(f"영구성장      {a.terminal_growth:>16,.2f}%   1단계 {a.years}년")
    if a.solve_for == "margin":
        print(f"성장률 가정   {a.growth_assumption:>16,.2f}%")

    print(f"\n[ 현재 가격이 요구하는 것 ]")
    print(f"  {label}   {shown:>10,.2f}{unit}")
    if status == "ABOVE_RANGE":
        print(f"  ⚠ 탐색 상한({hi * 100:,.0f}{unit})으로도 현재가에 닿지 않는다. "
              "이 모델로는 설명되지 않는 가격이다")
    elif status == "BELOW_RANGE":
        print(f"  ⚠ 탐색 하한({lo * 100:,.0f}{unit})에서도 현재가를 넘는다. "
              "가격이 모델 가치보다 크게 낮다")

    print(f"\n※ 이 값은 예측이 아니라 **현재가에 내재된 시장의 요구**다.")
    print(f"※ 다음 질문은 하나다 — 매수 근거가 이 수치를 정당화하는가?")
    print(f"※ 할인율·영구성장률은 가정이다. 바꾸면 답이 바뀐다.")
    return 0


# ---------------------------------------------------------------- scenarios

def cmd_scenarios(a):
    validate_common(a)
    fcf = resolve_fcf(a)
    growth = parse_pairs(a.growth, "growth")
    missing = [n for n in SCENARIO_NAMES if n not in growth]
    if missing:
        fail(f"--growth 에 {', '.join(missing)} 가 없다. "
             "세 시나리오가 모두 있어야 한다")
    if not (growth["bear"] <= growth["base"] <= growth["bull"]):
        fail(f"성장률이 bear ≤ base ≤ bull 순서가 아니다 "
             f"({growth['bear']} / {growth['base']} / {growth['bull']})")
    if a.horizon_months < 1:
        fail("--horizon-months 는 1 이상이어야 한다")

    common = dict(discount_rate=a.discount_rate / 100.0,
                  terminal_growth=a.terminal_growth / 100.0,
                  years=a.years, net_debt=a.net_debt, shares=a.shares)

    outputs = {"scenarios": {}}
    for name in SCENARIO_NAMES:
        value = equity_value_per_share(fcf, growth[name] / 100.0, **common)
        outputs["scenarios"][name] = {
            "growth_pct": growth[name],
            "value_per_share": value,
            "total_return_pct": (value / a.price - 1) * 100,
            "return_pct": annualized_pct(value, a.price, a.horizon_months),
        }
    implied, status = solve(
        a.price, *GROWTH_BOUNDS,
        lambda g: equity_value_per_share(fcf, g, **common))
    outputs["implied_growth_pct"] = implied * 100
    outputs["implied_growth_status"] = status

    inputs = build_inputs(a, fcf)
    inputs["horizon_months"] = a.horizon_months
    inputs["scenario_growth_pct"] = {n: growth[n] for n in SCENARIO_NAMES}

    calculation = {
        "schema_version": SCHEMA_VERSION,
        "calculation_id": calculation_id_for(MODEL, MODEL_VERSION, inputs, outputs),
        "model": MODEL,
        "model_version": MODEL_VERSION,
        "instrument_id": a.instrument,
        "created_at": datetime.now(timezone.utc).astimezone().isoformat(
            timespec="seconds"),
        "source_ids": a.source_id,
        "inputs": inputs,
        "outputs": outputs,
    }

    if a.json:
        print(json.dumps(calculation, ensure_ascii=False, indent=2))
    else:
        render_scenarios(a, fcf, calculation)

    if a.save:
        try:
            path = save_validated_calculation(a.calculations_dir, calculation)
        except ContractError as exc:
            for message in exc.errors:
                print(f"INVALID: {message}", file=sys.stderr)
            return 1
        print(f"\nSAVED {path}")
    return 0


def render_scenarios(a, fcf, calculation):
    outputs = calculation["outputs"]
    print("=" * 74)
    print(f"시나리오 밸류에이션 — {a.instrument}")
    print("=" * 74)
    print(f"현재가        {a.price:>16,.2f}")
    print(f"FCF           {fcf:>16,.0f}")
    print(f"할인율 {a.discount_rate:.2f}%   영구성장 {a.terminal_growth:.2f}%   "
          f"1단계 {a.years}년   수렴가정 {a.horizon_months}개월")

    print(f"\n[ 현재가에 내재된 요구 성장률 ]")
    print(f"  {outputs['implied_growth_pct']:>10,.2f}%")

    print(f"\n[ 시나리오 ]")
    print(f"  {'':<8}{'성장률':>10}{'주당가치':>16}{'격차':>10}{'연율':>10}")
    print("  " + "-" * 54)
    for name in SCENARIO_NAMES:
        s = outputs["scenarios"][name]
        print(f"  {name:<8}{s['growth_pct']:>9,.1f}%{s['value_per_share']:>16,.2f}"
              f"{s['total_return_pct']:>+9,.1f}%{s['return_pct']:>+9,.1f}%")

    print(f"\n  calculation_id  {calculation['calculation_id']}")
    print(f"\n※ '연율' 이 분석 계약의 scenarios[].return_pct 로 들어간다.")
    print(f"※ 확률은 계산하지 않는다. 세 시나리오의 확률은 분석가가 정하고, "
          "그 근거를 claim 으로 남긴다.")
    print(f"※ 가격이 {a.horizon_months}개월 안에 가치로 수렴한다는 가정이 들어 있다.")


# ---------------------------------------------------------------- CLI

def main():
    p = argparse.ArgumentParser(
        description="역산 밸류에이션 — 현재가가 요구하는 성장률·마진")
    sub = p.add_subparsers(dest="cmd", required=True)

    def shared(sp):
        sp.add_argument("--instrument", required=True, help="종목 ID (TSLA / 214450)")
        sp.add_argument("--price", type=float, required=True, help="주당 현재가")
        sp.add_argument("--shares", type=float, required=True, help="희석 주식수")
        # 지수표기 음수(-30e9)는 argparse 가 옵션으로 읽는다. '=' 로 붙여야 한다.
        sp.add_argument("--net-debt", type=float, default=0.0,
                        help="순부채. 순현금이면 음수 (--net-debt=-30e9 처럼 = 로 붙인다)")
        sp.add_argument("--fcf", type=float, help="직전 12개월 잉여현금흐름")
        sp.add_argument("--revenue", type=float, help="직전 12개월 매출")
        sp.add_argument("--fcf-margin", type=float, help="FCF 마진 %%")
        sp.add_argument("--discount-rate", type=float, required=True,
                        help="할인율 %% (자기자본비용)")
        sp.add_argument("--terminal-growth", type=float, required=True,
                        help="영구성장률 %%")
        sp.add_argument("--years", type=int, default=10, help="1단계 기간 (기본 10년)")
        sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("implied", help="현재가가 요구하는 성장률 또는 마진")
    shared(sp)
    sp.add_argument("--solve-for", choices=["growth", "margin"], default="growth")
    sp.add_argument("--growth-assumption", type=float,
                    help="--solve-for margin 일 때 가정할 성장률 %%")

    sp = sub.add_parser("scenarios", help="시나리오별 가치와 기대수익, 계산 산출물 저장")
    shared(sp)
    sp.add_argument("--growth", nargs="+", required=True,
                    metavar="NAME=PCT", help="bear=5 base=15 bull=25")
    sp.add_argument("--horizon-months", type=int, required=True,
                    help="가격이 가치로 수렴한다고 보는 기간")
    sp.add_argument("--source-id", nargs="+", required=True,
                    help="입력 숫자의 출처 ID. 분석의 sources 와 맞춘다")
    sp.add_argument("--save", action="store_true", help="계산 산출물을 저장한다")
    sp.add_argument("--calculations-dir", default=str(CALCULATIONS_DIR))

    a = p.parse_args()
    return cmd_implied(a) if a.cmd == "implied" else cmd_scenarios(a)


if __name__ == "__main__":
    raise SystemExit(main())
