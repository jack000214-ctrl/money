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
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
from calculators.contracts import (
    SCENARIO_NAMES,
    SCHEMA_VERSION,
    ContractError,
    calculation_id_for,
    content_hash,
    load_fundamentals,
    save_validated_calculation,
)

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
    """주당가치와 현재가의 격차를 수렴기간 기준 연율로 바꾼다."""
    if value <= 0:
        return -100.0
    total = value / price
    annual = total ** (12.0 / horizon_months) - 1.0
    return max(-100.0, annual * 100.0)


def scenario_outputs(*, fcf, price, growth, horizon_months, **common):
    """시나리오별 주당가치와 **두 단위의** 수익률을 낸다.

    이름이 단위를 말하게 한다. 예전 출력에는 `total_return_pct` 와 `return_pct`
    가 있었는데 뒤엣것이 연율이었고, CLI 는 그것을 판단 계약의 기대수익 자리에
    넣으라고 안내했다. 계약의 `outcome_metric` 은
    `TERMINAL_TOTAL_RETURN_ASSET_CURRENCY` — 평가 기간 **말의 누적** 총수익률이다.
    그대로 따르면 24개월 누적 +44.4% 가 +20.2% 로 들어가고 승패 문턱과의 비교가
    통째로 어긋난다. 계산은 맞는데 배선이 틀린 종류의 오류라 눈에 띄지 않는다.

    `return_pct` 라는 이름은 어느 단위인지 말해 주지 않아서 그 오배선을 만들었다.
    그래서 이름을 없애고 `annualized_return_pct` 로 바꿨다.
    """
    scenarios = {}
    for name in SCENARIO_NAMES:
        value = equity_value_per_share(fcf, growth[name] / 100.0, **common)
        scenarios[name] = {
            "growth_pct": growth[name],
            "value_per_share": value,
            # 판단 계약의 expected_return_pct 로 가는 값은 이쪽이다.
            # 주식의 손실은 -100% 까지다. 주당가치가 음수여도 하한을 둔다.
            "total_return_pct": max(-100.0, (value / price - 1) * 100),
            "annualized_return_pct": annualized_pct(value, price, horizon_months),
        }
    return {
        "scenarios": scenarios,
        "_units": (
            "total_return_pct 는 수렴기간 말의 누적 총수익률이고 판단 계약의 "
            "expected_return_pct 로 가는 값이다. annualized_return_pct 는 같은 "
            "값을 연율로 바꾼 참고치이며 계약의 어느 자리에도 들어가지 않는다"
        ),
    }


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


def build_inputs(a, fcf, provenance):
    """지문에 들어갈 입력. 산출물에 영향을 주는 값은 빠짐없이 넣는다.

    provenance 도 여기 들어간다. 같은 FCF 라도 원문에서 읽은 것과 손으로 친
    것은 신뢰도가 다르므로, 계산 ID 자체가 달라져야 한다.
    """
    inputs = {
        "price": a.price,
        "shares": a.shares,
        "fcf": fcf,
        "net_debt": a.net_debt,
        "discount_rate_pct": a.discount_rate,
        "terminal_growth_pct": a.terminal_growth,
        "years": a.years,
        "input_provenance": provenance,
    }
    if a.revenue is not None:
        inputs["revenue"] = a.revenue
    if a.fcf_margin is not None:
        inputs["fcf_margin_pct"] = a.fcf_margin
    return inputs


def validate_common(a):
    if a.price <= 0:
        fail("--price 는 0보다 커야 한다")
    if a.shares is None or a.shares <= 0:
        fail("--shares 는 0보다 커야 한다")
    if a.years < 1:
        fail("--years 는 1 이상이어야 한다")
    if a.discount_rate <= a.terminal_growth:
        fail(f"할인율 {a.discount_rate}% 가 영구성장률 {a.terminal_growth}% 보다 "
             "높아야 한다. 그렇지 않으면 잔존가치가 발산한다")


def resolve_inputs(a):
    """FCF·주식수·순부채를 확정하고 그 출처를 함께 돌려준다.

    이 프로젝트는 "기대수익은 손으로 적을 수 없다"를 내걸지만, 계산기의 첫
    숫자가 손으로 친 값이면 뒤의 지문은 지어낸 값의 지문일 뿐이다. 그래서
    기본 경로는 원문 스냅샷이고, 손입력은 사유를 남겨야만 허용한다.
    """
    shares_reason = (getattr(a, "shares_reason", None) or "").strip()
    if shares_reason and a.shares is None:
        fail("--shares-reason 은 --shares 와 함께 준다")
    if a.fundamentals:
        for flag, value in (("--fcf", a.fcf), ("--net-debt", a.net_debt)):
            if value is not None:
                fail(f"--fundamentals 와 {flag} 를 함께 줄 수 없다. "
                     "스냅샷 값을 손으로 덮어쓰면 출처 고정이 무의미해진다")
        try:
            snapshot = load_fundamentals(a.fundamentals)
        except ContractError as exc:
            for message in exc.errors:
                print(f"INVALID: {message}", file=sys.stderr)
            sys.exit(1)
        if snapshot["instrument_id"] != a.instrument:
            fail(f"스냅샷은 {snapshot['instrument_id']} 것인데 "
                 f"--instrument 는 {a.instrument} 다")
        values = snapshot["values"]
        selected_shares = a.shares
        share_basis = snapshot.get("share_count_basis") or {}
        share_tag = (snapshot.get("tags") or {}).get("shares")
        # EPS 분모와 가치평가 분모는 다르다. 공시값이라는 이유만으로 과거
        # 가중평균을 현재/미래 주식수로 승격하지 않는다. 구버전 스냅샷에는
        # basis가 없으므로 그 경우도 모델이 기준을 명시적으로 선택한다.
        current_outstanding = (
            share_basis.get("is_current_outstanding") is True
            and not str(share_basis.get("kind", "")).startswith("WEIGHTED_AVERAGE")
            and not str(share_tag or "").startswith("WeightedAverage")
        )
        if selected_shares is None:
            if not current_outstanding:
                fail("스냅샷 주식수는 EPS 분모이거나 현재 발행주식수인지 확인되지 "
                     "않았다. 가치평가에 쓸 주식수를 --shares 로 명시하고 "
                     "--shares-reason 에 출처·기준일·선택 이유를 남긴다")
            a.shares = values["shares_diluted"]
        elif not math.isfinite(selected_shares) or selected_shares <= 0:
            fail("--shares 는 0보다 큰 유한한 숫자여야 한다")
        elif not shares_reason:
            fail("--fundamentals 와 --shares 를 함께 쓰면 --shares-reason 에 "
                 "출처·기준일·가치평가 분모로 선택한 이유가 필요하다")
        a.net_debt = values["net_debt"]
        provenance = {
            "verified": selected_shares is None,
            # 출처를 스냅샷이 스스로 말하게 한다. 여기에 "edgar_fundamentals" 를
            # 박아두면 DART 원문에서 만든 스냅샷도 EDGAR 라고 기록돼, 나중에
            # 어느 원문에서 온 값인지 감사할 수 없다.
            # 새 키를 넣으면 calculation_id 지문이 바뀌어 이미 저장된 계산이
            # 전부 어긋난다. 통화는 스냅샷에 있고 생성기가 거기서 읽는다.
            "source": snapshot.get("source", "edgar_fundamentals"),
            "fundamentals_id": snapshot["fundamentals_id"],
            "period_end": snapshot["period_end"],
            # 파일 해시까지 지문에 넣는다. 스냅샷이 바뀌면 계산 ID 가 바뀐다.
            "fundamentals_sha256": content_hash(a.fundamentals),
            "share_count": {
                "origin": "MODEL_CHOICE" if selected_shares is not None else "SNAPSHOT",
                "selected_shares": a.shares,
                "snapshot_shares": values["shares_diluted"],
                "snapshot_basis": share_basis or None,
                "snapshot_tag": share_tag,
                "reason": shares_reason or None,
                "_note": "공시 스냅샷의 FCF·순부채는 유지한다. 명시적으로 선택한 "
                    "주식수의 출처와 적합성은 모델이 확인하며 PC가 인증하지 않는다",
            },
        }
        return values["fcf_ttm"], provenance

    # --- 손입력 경로 ---
    if a.fcf is not None:
        fcf = a.fcf
    elif a.revenue is not None and a.fcf_margin is not None:
        fcf = a.revenue * a.fcf_margin / 100.0
    else:
        fail("--fundamentals 또는 --fcf 또는 (--revenue 와 --fcf-margin) 이 필요하다")
    if a.shares is None:
        fail("--fundamentals 없이 계산하려면 --shares 가 필요하다")
    if a.net_debt is None:
        a.net_debt = 0.0
    if not a.inputs_unverified_reason:
        fail("손으로 적은 밸류에이션 입력은 그대로 쓸 수 없다.\n"
             "  권장:  python3.13 -m collectors.edgar fundamentals "
             f"{a.instrument}\n"
             "         → --fundamentals knowledge/fundamentals/"
             f"{a.instrument}-<기준일>.json\n"
             "  그래도 손입력이 필요하면 --inputs-unverified-reason "
             '"{확인 경로}" 로 사유를 남긴다. 사유는 기록될 뿐 검증을 대신하지 '
             "않는다")
    provenance = {"verified": False, "source": "manual",
                  "reason": a.inputs_unverified_reason}
    if shares_reason:
        provenance["share_count"] = {
            "origin": "MODEL_CHOICE", "selected_shares": a.shares,
            "reason": shares_reason,
        }
    return fcf, provenance


def check_fcf_sign(fcf):
    if fcf <= 0:
        fail(f"FCF 가 {fcf:,.0f} 다. 잉여현금흐름이 음수인 회사에 정률성장 DCF 를 "
             "쓰면 성장할수록 가치가 낮아진다. 흑자 전환 경로를 가정한 "
             "별도 모델이 필요하다")
    return fcf


# ---------------------------------------------------------------- implied

def implied_answer(a, fcf):
    """현재가가 요구하는 성장률 또는 마진을 **퍼센트로** 낸다.

    이 함수가 하는 일의 절반은 단위 변환이다 — 사용자는 퍼센트로 넣고, 모델은
    소수로 풀고, 화면은 다시 퍼센트로 읽는다. 그 왕복이 한 번만 어긋나도
    "요구 성장률 12.05%"가 "1205%" 또는 "0.12%"가 되는데, 둘 다 그럴듯해
    보이지 않으므로 눈에 띈다. 정말 위험한 것은 **탐색 경계**다 — 경계도 같이
    변환하지 않으면 "상한 30%로도 안 닿는다"는 경고가 엉뚱한 숫자를 말한다.

    돌려주는 값은 전부 퍼센트 단위이며, 여기서만 곱하고 나눈다.
    """
    common = dict(discount_rate=a.discount_rate / 100.0,
                  terminal_growth=a.terminal_growth / 100.0,
                  years=a.years, net_debt=a.net_debt, shares=a.shares)
    if a.solve_for == "growth":
        value_of = lambda g: equity_value_per_share(fcf, g, **common)  # noqa: E731
        lo, hi = GROWTH_BOUNDS
        label = "요구 FCF 성장률"
    else:
        if a.revenue is None:
            fail("--solve-for margin 에는 --revenue 가 필요하다")
        if a.growth_assumption is None:
            fail("--solve-for margin 에는 --growth-assumption 이 필요하다")
        growth = a.growth_assumption / 100.0
        value_of = lambda m: equity_value_per_share(  # noqa: E731
            a.revenue * m, growth, **common)
        lo, hi = MARGIN_BOUNDS
        label = "요구 FCF 마진"
    answer, status = solve(a.price, lo, hi, value_of)
    return {
        "label": label,
        "unit": "%",
        "value_pct": answer * 100,
        "status": status,
        "lower_bound_pct": lo * 100,
        "upper_bound_pct": hi * 100,
    }


def cmd_implied(a):
    fcf, provenance = resolve_inputs(a)
    validate_common(a)
    check_fcf_sign(fcf)
    result = implied_answer(a, fcf)
    label, unit, shown = result["label"], result["unit"], result["value_pct"]
    status = result["status"]
    lo, hi = result["lower_bound_pct"], result["upper_bound_pct"]

    if a.json:
        print(json.dumps({"solve_for": a.solve_for, "value": shown,
                          "status": status,
                          "inputs": build_inputs(a, fcf, provenance)}, ensure_ascii=False))
        return 0

    print("=" * 74)
    print(f"역산 밸류에이션 — {a.instrument}")
    print("=" * 74)
    print(f"현재가        {a.price:>16,.2f}")
    print(f"FCF           {fcf:>16,.0f}")
    print(f"주식수        {a.shares:>16,.0f}")
    if (provenance.get("share_count") or {}).get("reason"):
        print(f"주식수 근거   {provenance['share_count']['reason']}")
    print(f"순부채        {a.net_debt:>16,.0f}")
    print(f"할인율        {a.discount_rate:>16,.2f}%")
    print(f"영구성장      {a.terminal_growth:>16,.2f}%   1단계 {a.years}년")
    if a.solve_for == "margin":
        print(f"성장률 가정   {a.growth_assumption:>16,.2f}%")

    print(f"\n[ 현재 가격이 요구하는 것 ]")
    print(f"  {label}   {shown:>10,.2f}{unit}")
    if status == "ABOVE_RANGE":
        print(f"  ⚠ 탐색 상한({hi:,.0f}{unit})으로도 현재가에 닿지 않는다. "
              "이 모델로는 설명되지 않는 가격이다")
    elif status == "BELOW_RANGE":
        print(f"  ⚠ 탐색 하한({lo:,.0f}{unit})에서도 현재가를 넘는다. "
              "가격이 모델 가치보다 크게 낮다")

    print(f"\n※ 이 값은 예측이 아니라 **현재가에 내재된 시장의 요구**다.")
    print(f"※ 다음 질문은 하나다 — 매수 근거가 이 수치를 정당화하는가?")
    print(f"※ 할인율·영구성장률은 가정이다. 바꾸면 답이 바뀐다.")
    return 0


# ---------------------------------------------------------------- scenarios

def cmd_scenarios(a):
    fcf, provenance = resolve_inputs(a)
    validate_common(a)
    check_fcf_sign(fcf)
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

    outputs = scenario_outputs(
        fcf=fcf,
        price=a.price,
        growth=growth,
        horizon_months=a.horizon_months,
        **common,
    )
    implied, status = solve(
        a.price, *GROWTH_BOUNDS,
        lambda g: equity_value_per_share(fcf, g, **common))
    outputs["implied_growth_pct"] = implied * 100
    outputs["implied_growth_status"] = status

    inputs = build_inputs(a, fcf, provenance)
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
    print(f"주식수        {a.shares:>16,.0f}")
    share_count = calculation["inputs"]["input_provenance"].get("share_count") or {}
    if share_count.get("reason"):
        print(f"주식수 근거   {share_count['reason']}")
    print(f"할인율 {a.discount_rate:.2f}%   영구성장 {a.terminal_growth:.2f}%   "
          f"1단계 {a.years}년   수렴가정 {a.horizon_months}개월")

    print(f"\n[ 현재가에 내재된 요구 성장률 ]")
    print(f"  {outputs['implied_growth_pct']:>10,.2f}%")

    print(f"\n[ 시나리오 ]")
    print(f"  {'':<8}{'성장률':>10}{'주당가치':>16}{'누적':>10}{'연율':>10}")
    print("  " + "-" * 54)
    for name in SCENARIO_NAMES:
        s = outputs["scenarios"][name]
        print(f"  {name:<8}{s['growth_pct']:>9,.1f}%{s['value_per_share']:>16,.2f}"
              f"{s['total_return_pct']:>+9,.1f}%{s['annualized_return_pct']:>+9,.1f}%")

    print(f"\n  calculation_id  {calculation['calculation_id']}")
    print(f"\n※ 판단 계약의 expected_return_pct 로 가는 값은 '누적' 이다. "
          "'연율' 은 참고치이며 계약의 어느 자리에도 넣지 않는다.")
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
        # FCF·순부채는 스냅샷에 고정하고, 주식수의 기준은 별도로 확인한다.
        sp.add_argument("--fundamentals",
                        help="edgar_fetch fundamentals 가 만든 입력 스냅샷 경로")
        sp.add_argument("--shares", type=float, help="모델이 선택한 가치평가 주식수")
        sp.add_argument("--shares-reason",
                        help="주식수의 출처·기준일·선택 이유. --fundamentals 와 "
                             "--shares 를 함께 쓰면 필수이며 PC 인증을 뜻하지 않는다")
        # 지수표기 음수(-30e9)는 argparse 가 옵션으로 읽는다. '=' 로 붙여야 한다.
        sp.add_argument("--net-debt", type=float,
                        help="순부채. 순현금이면 음수 (--net-debt=-30e9 처럼 = 로 붙인다)")
        sp.add_argument("--fcf", type=float, help="직전 12개월 잉여현금흐름 (손입력 경로)")
        sp.add_argument("--inputs-unverified-reason",
                        help="원문 스냅샷 없이 손으로 입력할 때의 확인 경로. "
                             "기록될 뿐 검증을 대신하지 않는다")
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
