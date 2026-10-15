#!/usr/bin/env python3
"""화면이 쓸 판정 리포트를 엔진·원문에서 만든다.

이 스크립트가 없을 때의 문제: `data/decision-report.json` 을 손으로 관리하면
그 파일이 **두 번째 진실의 원천**이 된다. 실제로 그렇게 해서 아래가 틀렸다.

  - PER 325x / PSR 11.9x  → 급락 전 주가 $349 로 계산된 값 (실제 293.8x / 10.8x)
  - 판정 "추가 매수 보류" → 엔진의 실제 판정은 BLOCKED
  - 매수가 $282.46      → 강세 시나리오 단독 기준. IPS 는 확률가중이라
                          그 가격에서도 기대수익 -47.2% 로 여전히 미달이다

그래서 규칙은 하나다. **숫자는 여기서 만들고, 산문은 narrative.json 에 쓴다.**
화면은 둘 다 그대로 그리기만 한다.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

DASHBOARD = Path(__file__).resolve().parent.parent
ROOT = DASHBOARD.parent
sys.path.insert(0, str(ROOT / "calculators"))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from contracts import load_calculation, load_json  # noqa: E402
from reverse_dcf import equity_value_per_share  # noqa: E402
from trade_check import evaluate_trade  # noqa: E402

SCHEMA = "decision-dashboard/0.6.0"
# 한 번 틀렸던 값들. 산문에 다시 나타나면 생성기가 만든 값과 화면에서 충돌한다.
# 낡은 주가($349)로 계산된 배수와, 측정 기준을 섞어 만든 매수가가 그것이다.
# 헤드라인은 36px 로 커진다. 엔진 메시지를 그대로 넣으면
# "기대수익 -50.0%(PROBABILITY_WEIGHTED)가..." 가 대문자로 화면을 덮는다.
# 짧은 라벨만 여기서 붙이고, 원문 문장과 코드는 issue 목록에 그대로 둔다.
# 모르는 코드는 지어내지 않고 코드를 그대로 보여준다.
ISSUE_HEADLINES = {
    "EXPECTED_RETURN_BELOW_HURDLE": "목표 수익률 미달",
    "BEAR_LOSS_EXCEEDS_BUDGET": "하방 예산 초과",
    "POSITION_HARD_CEILING": "비중 절대 상한 초과",
    "POSITION_LIMIT_TOTAL_ASSETS": "단일 종목 한도 초과",
    "PRICE_DEVIATION": "예정가가 기준가와 어긋남",
    "PRICE_STALE": "가격 데이터가 오래됨",
    "PRICE_REFERENCE_STALE": "기준가가 오래됨",
    "INSUFFICIENT_CASH": "현금 부족",
    "THESIS_INVALIDATED_FOR_BUY": "논지 무효화 상태",
    "CALCULATION_MISMATCH": "계산 산출물 불일치",
    "VALUATION_INPUT_UNVERIFIED": "밸류에이션 입력 미검증",
    "DISCOUNT_RATE_OUT_OF_POLICY": "할인율이 승인 범위 밖",
    "OVERSELL": "보유량 초과 매도",
}

STALE_IN_NARRATIVE = ("325.0x", "325배", "11.9x", "11.9배", "$282.46", "282.46",
                      # 엔진 판정을 완화한 옛 문구. 화면은 차단인데 산문만 보류로
                      # 남아 있으면 같은 페이지가 두 말을 한다.
                      "매수 보류", "보류 판정", "보류인가")


def fail(msg: str) -> None:
    print(f"오류: {msg}", file=sys.stderr)
    raise SystemExit(1)


# 통화별 표시 규약. 화면은 이 문자열을 그대로 그리므로 자릿수·단위·기호가
# 전부 여기서 정해진다. USD 는 백만 단위, KRW 는 조 단위가 읽기 쉽다 —
# 삼성전자 영업현금흐름을 "₩109,008,400M" 로 쓰면 아무도 못 읽는다.
CURRENCY = {
    "USD": {
        "money": lambda v: f"${v / 1e6:,.0f}M",
        "signed": lambda v: f"{'−' if v < 0 else '+'}${abs(v) / 1e6:,.0f}M",
        "price": lambda v: f"${v:,.2f}",
        "unit_note": "백만 USD",
    },
    "KRW": {
        "money": lambda v: f"{v / 1e12:,.2f}조",
        "signed": lambda v: f"{'−' if v < 0 else '+'}{abs(v) / 1e12:,.2f}조",
        "price": lambda v: f"{v:,.0f}원",
        "unit_note": "조 KRW",
    },
}
_fmt = CURRENCY["USD"]


def money_m(value: float) -> str:
    return _fmt["money"](value)


def signed_m(value: float) -> str:
    return _fmt["signed"](value)


def price_str(value: float) -> str:
    return _fmt["price"](value)


def pct(value: float, digits: int = 1) -> str:
    return f"{value:+.{digits}f}%"


def entry_price_for_hurdle(scenarios, probabilities, horizon_months, required_pct):
    """IPS 측정 기준으로 요구수익률을 만족하는 가격을 푼다.

        Σ pᵢ (Vᵢ/P)^k − 1 = r   →   P = (Σ pᵢ Vᵢ^k / (1+r))^(1/k),  k = 12/h

    강세 시나리오 하나만 쓰면 훨씬 높은 가격이 나오지만, 그건 IPS 가 정한
    자가 아니다. 측정 기준을 섞으면 화면이 엔진과 다른 답을 낸다.
    """
    k = 12.0 / horizon_months
    total = sum(probabilities[name] * scenarios[name] ** k for name in scenarios)
    return (total / (1 + required_pct / 100.0)) ** (1 / k)


def annualized(value: float, price: float, horizon_months: int) -> float:
    return ((value / price) ** (12.0 / horizon_months) - 1) * 100.0


def nice_ceiling(value: float) -> tuple[float, float]:
    """축 상한과 눈금 간격을 값에서 만든다.

    상한을 고정하면 다음 분기에 capex 가 튀는 순간 막대가 축을 뚫는다.
    """
    step = 10 ** math.floor(math.log10(value)) / 2
    while value / step > 6:
        step *= 2
    return math.ceil(value / step) * step, step


def build_cashflow_chart(ocf_q, capex_q):
    """현금흐름 막대차트를 SVG 좌표로 만든다.

    CSS 고정 픽셀(막대 30px, 스테이지 190px)로 그렸더니 폭이 좁아질 때
    값 라벨이 컬럼 밖으로 잘렸다. viewBox 는 폭에 상관없이 같은 비율로
    줄어들므로 어떤 해상도에서도 잘리지 않는다.
    """
    width, height = 1000, 300
    left, right, top, bottom = 58, 16, 26, 58
    plot_w = width - left - right
    plot_h = height - top - bottom

    peak = max(max(q["val"] for q in ocf_q), max(q["val"] for q in capex_q))
    axis_max, step = nice_ceiling(peak)

    def y_of(value):
        return top + plot_h * (1 - value / axis_max)

    ticks = []
    value = 0.0
    while value <= axis_max + 1:
        ticks.append({"label": money_m(value) if value else "0",
                      "y": f"{y_of(value):.1f}"})
        value += step

    slot = plot_w / len(ocf_q)
    bar_w = min(slot * 0.22, 46)
    gap = bar_w * 0.24
    groups, rows = [], []
    for index, (o, c) in enumerate(zip(ocf_q, capex_q)):
        center = left + slot * (index + 0.5)
        fcf = o["val"] - c["val"]
        groups.append({
            "quarter": f"FY{o['fy']} Q{o['quarter']}",
            "labelX": f"{center:.1f}",
            "ocf": {"x": f"{center - gap / 2 - bar_w:.1f}", "y": f"{y_of(o['val']):.1f}",
                    "height": f"{plot_h * o['val'] / axis_max:.1f}",
                    "label": money_m(o["val"]),
                    "labelX": f"{center - gap / 2 - bar_w / 2:.1f}",
                    "labelY": f"{y_of(o['val']) - 7:.1f}"},
            "capex": {"x": f"{center + gap / 2:.1f}", "y": f"{y_of(c['val']):.1f}",
                      "height": f"{plot_h * c['val'] / axis_max:.1f}",
                      "label": money_m(c["val"]),
                      "labelX": f"{center + gap / 2 + bar_w / 2:.1f}",
                      "labelY": f"{y_of(c['val']) - 7:.1f}"},
            "fcf": signed_m(fcf),
            "signal": "bad" if fcf < 0 else "good",
        })
        rows.append({
            "quarter": f"FY{o['fy']} Q{o['quarter']}",
            "operatingCashFlow": money_m(o["val"]),
            "capex": money_m(c["val"]),
            "freeCashFlow": signed_m(fcf),
            "signal": "bad" if fcf < 0 else "good",
        })

    chart = {
        "viewBox": f"0 0 {width} {height}",
        "barWidth": f"{bar_w:.1f}",
        "baselineY": f"{y_of(0):.1f}",
        "plotLeft": f"{left}",
        "plotRight": f"{width - right}",
        "quarterLabelY": f"{y_of(0) + 20:.1f}",
        "fcfLabelY": f"{y_of(0) + 40:.1f}",
        "ticks": ticks,
        "groups": groups,
    }
    return rows, chart


def build_sensitivity(calc_inputs, per_share, probabilities, horizon,
                      required, price_native, fcf, currency):
    """"무엇이 바뀌어야 통과하는가" 격자를 미리 계산한다.

    화면에서 이 값을 다시 만들면 안 된다 — 브라우저가 두 번째 계산 주체가
    되는 순간 엔진과 다른 답을 낼 수 있다. 그래서 가능한 조합을 전부 여기서
    풀어 두고, UI 는 셀을 **조회만** 한다.

    축은 셋이다. 강세 성장률(강세 시나리오가 얼마나 좋은가), 강세 확률
    (얼마나 그렇게 믿는가), 가격. 확률을 움직이면 약세·기준은 원래 비율을
    유지한 채 나머지를 나눠 갖는다.
    """
    common = dict(
        discount_rate=calc_inputs["discount_rate_pct"] / 100.0,
        terminal_growth=calc_inputs["terminal_growth_pct"] / 100.0,
        years=calc_inputs["years"],
        net_debt=calc_inputs["net_debt"],
        shares=calc_inputs["shares"],
    )
    k = 12.0 / horizon
    base_bear, base_base = probabilities["bear"], probabilities["base"]
    rest_total = base_bear + base_base

    growths = list(range(10, 81, 5))                     # 10% ~ 80%
    probs = [round(v / 100, 2) for v in range(0, 101, 5)]  # 0% ~ 100%
    prices = [round(price_native * (0.2 + 0.05 * i), 2) for i in range(0, 25)]

    cells = []
    for growth in growths:
        bull_value = equity_value_per_share(fcf, growth / 100.0, **common)
        plane = []
        for p_bull in probs:
            rest = 1 - p_bull
            p_bear = rest * base_bear / rest_total
            p_base = rest * base_base / rest_total
            row = []
            for price in prices:
                weighted = (
                    p_bear * ((per_share["bear"] / price) ** k - 1)
                    + p_base * ((per_share["base"] / price) ** k - 1)
                    + p_bull * ((bull_value / price) ** k - 1)
                ) * 100
                gap = weighted - required
                row.append([round(weighted, 1), 1 if weighted >= required else 0,
                            round(gap, 1)])
            plane.append(row)
        cells.append(plane)

    # 현재 설정이 격자의 어느 칸인지
    def nearest(values, target):
        return min(range(len(values)), key=lambda i: abs(values[i] - target))

    return {
        # values 는 로직용 숫자, labels 는 화면에 그대로 찍는 문자열.
        # 브라우저가 포맷하면 $315.7 처럼 자릿수가 흔들린다.
        "axes": {
            "growth": {"label": "강세 FCF 성장률", "unit": "%/년",
                       "values": growths,
                       "labels": [f"{v}%/년" for v in growths]},
            "probability": {"label": "강세 확률", "unit": "%",
                            "values": [round(v * 100) for v in probs],
                            "labels": [f"{round(v * 100)}%" for v in probs]},
            "price": {"label": "주가", "unit": currency, "values": prices,
                      "labels": [price_str(v) for v in prices]},
        },
        "current": {
            "growth": nearest(growths, calc_inputs["scenario_growth_pct"]["bull"]),
            "probability": nearest([round(v * 100) for v in probs],
                                   round(probabilities["bull"] * 100)),
            "price": nearest(prices, price_native),
        },
        "requiredReturn": required,
        "cells": cells,
        "note": "약세·기준 확률은 원래 비율(40:45)을 유지한 채 나머지를 나눠 갖는다. "
                "할인율·영구성장률·1단계 기간은 승인 IPS 범위 안에서 고정이다.",
    }


def build(args) -> dict:
    narrative = load_json(args.narrative)
    blob = json.dumps(narrative, ensure_ascii=False)
    for needle in STALE_IN_NARRATIVE:
        if needle in blob:
            fail(f"narrative.json 에 낡은 수치 {needle!r} 가 있다. "
                 "이 값은 급락 전 주가 또는 다른 측정 기준으로 만들어진 값이라 "
                 "생성기가 만드는 값과 화면에서 충돌한다")

    ips = load_json(args.ips)
    fundamentals = load_json(args.fundamentals)
    values = fundamentals["values"]

    # 통화는 스냅샷이 선언한다. 인자로 받으면 스냅샷과 어긋날 수 있다.
    global _fmt
    currency = fundamentals.get("currency", "USD")
    if currency not in CURRENCY:
        fail(f"통화 {currency} 의 표시 규약이 없다. CURRENCY 에 추가한다")
    _fmt = CURRENCY[currency]

    result = evaluate_trade(
        args.ticker, "buy", args.quantity, args.price_krw,
        holdings_path=args.holdings, ips_path=args.ips,
        analysis_path=args.analysis, price_as_of=args.price_as_of,
        calculations_dir=args.calculations,
        # 보유하지 않은 종목도 리포트를 만들 수 있어야 한다. 이 인자가 없으면
        # 엔진이 기본값(KR/STOCK)으로 때워 위험등급 한도가 잘못 걸린다.
        new_asset_name=args.name,
        new_asset_market=args.market,
        new_asset_risk_class=args.risk_class,
    )
    analysis = load_json(args.analysis)
    expected = analysis["expected_return"]
    calculation_id = expected["calculation_id"]
    # 하드코딩된 ID 를 그냥 찍지 않는다. 실제로 열리는지 확인한다.
    calculation = load_calculation(calculation_id, args.calculations)
    calc_inputs = calculation["inputs"]
    calc_scen = calculation["outputs"]["scenarios"]

    provenance = calc_inputs.get("input_provenance") or {}
    if not provenance.get("verified"):
        fail(f"{calculation_id}: 손입력 계산으로는 리포트를 만들지 않는다")
    if provenance.get("fundamentals_id") != fundamentals["fundamentals_id"]:
        fail("계산이 참조한 스냅샷과 --fundamentals 가 다르다")

    horizon = int(expected["horizon_months"])
    required = float(expected["required_return_pct"])
    probabilities = {n: expected["scenarios"][n]["probability"] for n in calc_scen}
    per_share = {n: calc_scen[n]["value_per_share"] for n in calc_scen}
    price_native = float(calc_inputs["price"])

    hurdle_price = entry_price_for_hurdle(per_share, probabilities, horizon, required)
    bull_only_price = per_share["bull"] / (1 + required / 100.0) ** (horizon / 12.0)

    income = fundamentals["income_statement"]
    net_income_ttm = income["net_income"]["ttm"]
    revenue_ttm = income["revenue"]["ttm"]
    mcap = values["shares_diluted"] * price_native
    per = mcap / net_income_ttm if net_income_ttm else None
    psr = mcap / revenue_ttm if revenue_ttm else None

    decision = result["decision"]
    weights = result["weights"]
    basis = result["decision_basis"]
    issues = [
        {"severity": i["severity"], "code": i["code"], "message": i["message"]}
        for i in result["issues"]
    ]

    denominator_labels = {
        "total_assets": "총자산",
        "equity_sleeve": "주식 슬리브",
        "risk_assets": "위험자산",
    }
    portfolio = {
        "quantity": args.quantity,
        "priceKrw": f"₩{args.price_krw:,.0f}",
        "tradeValueKrw": f"₩{args.quantity * args.price_krw:,.0f}",
        "weights": [
            {
                "label": denominator_labels[key],
                "before": f"{weights['before'][key]['position_pct']:.2f}%",
                "after": f"{weights['after'][key]['position_pct']:.2f}%",
                "denominator": f"₩{weights['after'][key]['denominator_krw']:,.0f}",
                "definition": result["denominators"]["definitions"][key],
            }
            for key in denominator_labels
            if weights["before"].get(key, {}).get("position_pct") is not None
        ],
        "bearBudget": {
            "used": f"{basis.get('bear_loss_contribution_pct', 0):.2f}%",
            "limit": f"{ips['decision_policy']['max_bear_loss_contribution_pct']:.2f}%",
            "note": "약세 시나리오가 실현될 때 이 포지션 하나가 총자산에 입히는 손실",
            "withinBudget": basis.get("bear_loss_contribution_pct", 0)
            <= ips["decision_policy"]["max_bear_loss_contribution_pct"],
        },
        "costs": {
            "총계": f"₩{basis['costs_and_tax'].get('total_krw', 0):,.0f}",
            "내역": (f"수수료 ₩{basis['costs_and_tax'].get('commission_krw', 0):,.0f} · "
                     f"슬리피지 ₩{basis['costs_and_tax'].get('slippage_krw', 0):,.0f} · "
                     f"환전 ₩{basis['costs_and_tax'].get('fx_cost_krw', 0):,.0f}"),
        } if isinstance(basis.get("costs_and_tax"), dict) else None,
        "snapshotId": result["metadata"]["decision_snapshot_id"],
    }

    # 원문 출처에 따라 재수집 명령과 라벨이 달라진다. "EDGAR" 를 박아두면
    # DART 에서 만든 스냅샷에 EDGAR 라고 적힌 화면이 나온다.
    snapshot_source = fundamentals.get("source", "edgar")
    fetcher = "dart_fetch" if "dart" in snapshot_source else "edgar_fetch"
    source_label = "DART" if "dart" in snapshot_source else "EDGAR"

    ocf_q = fundamentals["quarters"]["ocf"]
    capex_q = fundamentals["quarters"]["capex"]
    cash_rows, cash_chart = build_cashflow_chart(ocf_q, capex_q)

    # --- 손익계산서: 최신 분기와 4분기 전(전년 동기)을 비교한다 ---
    def q_pair(key):
        """최신 분기와 **전년 동기**(4분기 전). 창 끝과 비교하면 YoY 가 아니다."""
        rows = income[key]["quarters"]
        if len(rows) < 5:
            fail(f"손익 {key}: 전년 동기 비교에 분기가 모자란다 ({len(rows)}개)")
        latest, year_ago = rows[-1], rows[-5]
        if latest["quarter"] != year_ago["quarter"] or latest["fy"] - year_ago["fy"] != 1:
            fail(f"손익 {key}: 전년 동기가 아니다 "
                 f"(FY{latest['fy']} Q{latest['quarter']} vs "
                 f"FY{year_ago['fy']} Q{year_ago['quarter']})")
        return latest["val"], year_ago["val"]

    rev_now, rev_yoy = q_pair("revenue")
    gp_now, gp_yoy = q_pair("gross_profit")
    op_now, op_yoy = q_pair("operating_income")
    interp = narrative["interpretations"]["incomeStatement"]

    # 매출총이익에서 영업이익까지 빼는 비용 라인은 회계기준마다 다르다.
    # US-GAAP 은 R&D·SG&A 로 갈라지지만 K-IFRS 는 판매비와관리비 한 줄이고
    # R&D 는 그 안에 있다 (주석에만 분리). 고정 키로 쓰면 삼성전자에서
    # 항등식이 깨지거나, 맞추려고 없는 R&D 를 지어내게 된다.
    _expense_lines_raw = fundamentals.get("expense_lines")
    expense_lines = _expense_lines_raw if _expense_lines_raw is not None else [
        {"key": "rnd", "label": "R&D"},
        {"key": "sgna", "label": "SG&A"},
    ]

    def growth(now, before):
        return f"전년 동기 대비 {(now / before - 1) * 100:+.1f}%"

    expenses = []
    for line in expense_lines:
        now, yoy = q_pair(line["key"])
        expenses.append({"label": line["label"], "now": now, "yoy": yoy})

    # 항등식을 화면에 그리기 전에 검사한다. 어긋나면 라인이 중복·누락됐다.
    residual = gp_now - sum(e["now"] for e in expenses) - op_now
    if abs(residual) > max(abs(op_now) * 0.005, 1.0):
        fail(f"손익 항등식이 맞지 않는다 — 매출총이익 {money_m(gp_now)} − "
             f"비용 {money_m(sum(e['now'] for e in expenses))} ≠ 영업이익 "
             f"{money_m(op_now)} (차이 {money_m(residual)}). "
             "스냅샷의 expense_lines 를 확인한다")

    income_rows = [
        {"metric": "매출", "value": money_m(rev_now),
         "comparison": growth(rev_now, rev_yoy),
         "interpretation": interp.get("매출", ""), "signal": "good"},
        {"metric": "매출총이익률", "value": f"{gp_now / rev_now * 100:.2f}%",
         "comparison": f"전년 동기 {gp_yoy / rev_yoy * 100:.2f}% · "
                       f"{(gp_now / rev_now - gp_yoy / rev_yoy) * 100:+.2f}%p",
         "interpretation": interp.get("매출총이익률", ""), "signal": "good"},
    ] + [
        {"metric": e["label"], "value": money_m(e["now"]),
         "comparison": growth(e["now"], e["yoy"]),
         "interpretation": interp.get(e["label"], ""), "signal": "watch"}
        for e in expenses
    ] + [
        {"metric": "영업이익", "value": money_m(op_now),
         "comparison": growth(op_now, op_yoy),
         "interpretation": interp.get("영업이익", ""), "signal": "bad"},
    ]

    # --- 가격 근거: 시세 CSV 에서 직접 만든다 ---
    import csv as _csv
    with open(args.prices, encoding="utf-8") as f:
        price_rows = list(_csv.DictReader(
            line for line in f if not line.startswith("#")))
    closes = [(r["date"], float(r["close"])) for r in price_rows]
    # 시세 파일은 10년치다. 판단용 고점·저점은 최근 창에서 잡는다 —
    # 10년 고점 대비 −70% 는 이 거래에 대해 아무것도 말해주지 않는다.
    if args.price_lookback_days:
        import datetime as _dt
        cutoff = (_dt.date.fromisoformat(closes[-1][0])
                  - _dt.timedelta(days=args.price_lookback_days)).isoformat()
        windowed = [c for c in closes if c[0] >= cutoff]
        if len(windowed) > 2:
            closes = windowed
    hi = max(closes, key=lambda x: x[1])
    lo = min(closes, key=lambda x: x[1])
    two_ago = closes[-3][1]
    price_evidence = [
        {"label": "최근 2거래일", "value": pct((closes[-1][1] / two_ago - 1) * 100),
         "explanation": f"{price_str(two_ago)} → {price_str(closes[-1][1])}. 최근 2거래일 움직임입니다.",
         "signal": "bad"},
        # "52주" 를 라벨에 박으면 안 된다. 이 계산은 CSV 전 구간에서 하므로
        # 시세 파일이 2년이면 라벨이 거짓말을 한다 (삼성전자 저점은 2024-11-14
        # 로 52주 밖이었다). 구간을 라벨에 그대로 적는다.
        {"label": f"기간 고점 대비 ({closes[0][0]}~)",
         "value": pct((price_native / hi[1] - 1) * 100),
         "explanation": f"고점 {price_str(hi[1])} ({hi[0]}) 대비.", "signal": "watch"},
        {"label": f"기간 저점 대비 ({closes[0][0]}~)",
         "value": pct((price_native / lo[1] - 1) * 100),
         "explanation": f"저점 {price_str(lo[1])} ({lo[0]}) 대비.", "signal": "good"},
        {"label": f"요구수익률 {required:.0f}% 만족 가격",
         "value": price_str(hurdle_price),
         "explanation": "IPS 측정 기준으로 푼 값. 현재가의 "
                        f"{hurdle_price / price_native * 100:.0f}% 수준이라 "
                        "가격 조정으로 해소될 수준이 아닙니다.",
         "signal": "bad"},
    ]

    # --- 핵심 지표: 엔진과 원문에서 만든다 ---
    latest_fcf = ocf_q[-1]["val"] - capex_q[-1]["val"]
    highlights = [
        {"meaning": "목표 수익률에 못 미칩니다",
         "value": f"{basis.get('position_return_pct', 0):.1f}% < 목표 {required:.1f}%",
         "context": "세 시나리오를 확률로 섞은 값입니다. 강세 시나리오만 "
                    f"따로 봐도 {calc_scen['bull']['return_pct']:+.1f}%로 "
                    "목표에 못 미칩니다.",
         "confidence": "투자정책 판정", "confidenceLevel": "high", "signal": "bad"},
        # 부호를 보고 문장을 고른다. "현금이 회수되지 않았다"를 박아두면
        # 분기 FCF 가 양수인 종목에서 화면이 사실과 반대로 말한다.
        ({"meaning": "현금은 아직 회수되지 않았습니다",
          "value": f"분기 FCF {signed_m(latest_fcf)}",
          "context": f"영업현금 {money_m(ocf_q[-1]['val'])}보다 "
                     f"CapEx {money_m(capex_q[-1]['val'])}가 큽니다.",
          "confidence": "공시 확정", "confidenceLevel": "high", "signal": "bad"}
         if latest_fcf < 0 else
         {"meaning": "현금은 실제로 남고 있습니다",
          "value": f"분기 FCF {signed_m(latest_fcf)}",
          "context": f"영업현금 {money_m(ocf_q[-1]['val'])}에서 "
                     f"CapEx {money_m(capex_q[-1]['val'])}를 쓰고 남은 금액입니다.",
          "confidence": "공시 확정", "confidenceLevel": "high", "signal": "good"}),
        {"meaning": "성장은 가격 부담을 지우지 못합니다",
         "value": f"PER {(f'{per:.2f}x' if per < 10 else f'{per:.1f}x') if per else '—'} · PSR {(f'{psr:.2f}x' if psr < 10 else f'{psr:.1f}x') if psr else '—'}",
         "context": "현재가 기준입니다. 동종사 대비 프리미엄이 이미 반영돼 있습니다.",
         "confidence": "시장·공시 데이터", "confidenceLevel": "high", "signal": "watch"},
        {"meaning": ("본업의 매출은 성장 중입니다" if rev_now >= rev_yoy
                     else "본업의 매출이 줄었습니다"),
         "value": f"매출 {(rev_now / rev_yoy - 1) * 100:+.1f}%",
         "context": f"매출총이익률도 {gp_now / rev_now * 100:.2f}%로 "
                    f"전년 동기 {gp_yoy / rev_yoy * 100:.2f}%입니다.",
         "confidence": "공시 확정", "confidenceLevel": "high",
         "signal": "good" if rev_now >= rev_yoy else "bad"},
    ]

    sensitivity = build_sensitivity(
        calc_inputs, per_share, probabilities, horizon, required, price_native,
        float(calc_inputs["fcf"]), currency)

    report = {
        "schemaVersion": SCHEMA,
        "sensitivity": sensitivity,
        "meta": {
            "asOf": args.as_of,
            "priceAsOf": args.price_note,
            "financialAsOf": fundamentals["period_end"],
            "calculationId": calculation_id,
            "fundamentalsId": fundamentals["fundamentals_id"],
            "engineVersion": result["metadata"]["engine_version"],
            "generatedBy": "dashboard/scripts/build_decision_report.py",
        },
        "asset": {
            "name": args.name,
            "ticker": args.ticker_label,
            # 통화 이름을 키에 박지 않는다. currentUsd 라는 필드가 있으면
            # KRW 종목은 그 칸을 비워두거나 거짓말을 해야 한다.
            "currency": currency,
            "current": price_str(price_native),
            "currentBase": f"₩{args.price_krw:,.0f}",
            "baseNote": ("기준통화(KRW) 환산값 — 사용자 제공" if currency != "KRW"
                         else "기준통화와 동일"),
        },
        "priceEvidence": price_evidence,
        "incomeStatement": {
            **narrative["sectionTitles"]["incomeStatement"],
            # 매출총이익 − R&D − SG&A = 영업이익. 항등식이 어긋나면 위에서 막힌다.
            "equation": [
                {"label": "매출총이익", "value": money_m(gp_now)},
                *[{"label": e["label"], "value": f"−{money_m(e['now'])}",
                   "sign": "minus"} for e in expenses],
                {"label": "영업이익", "value": money_m(op_now), "sign": "equals"},
            ],
            "rows": income_rows,
        },
        "decision": {
            # 엔진 판정을 자연어로 완화하지 않는다. SKILL 이 금지한다.
            "verdict": decision["verdict"],
            "status": {
                "BLOCKED": "추가 매수 차단",
                "RESEARCH_REQUIRED": "추가 조사 필요",
                "CAUTION": "주의 — 근거 재검토",
                "PROCEED": "차단 사유 없음",
            }[decision["verdict"]],
            "verdictNote": "PROCEED 는 수익 보장이 아니라 정책·근거 계약상 "
                           "차단 사유가 없다는 뜻이다.",
            "primaryReason": next(
                (ISSUE_HEADLINES.get(i["code"], i["code"])
                 for i in issues if i["severity"] == "BLOCKED"),
                "차단 사유 없음"),
            "headline": narrative["decision"]["headline"],
            "reason": narrative["decision"]["reason"],
            "issues": issues,
            "measure": basis.get("return_measure", "?"),
            "positionReturn": f"{basis.get('position_return_pct', 0):.1f}%",
            "requiredReturn": f"{required:.1f}%",
            "hurdleResult": (
                f"연 수익률 {basis.get('position_return_pct', 0):.1f}% · "
                f"목표 {required:.1f}%"
            ),
            "entryCeiling": price_str(hurdle_price),
            "entryCeilingNote": (
                f"세 시나리오를 확률로 섞어 계산했을 때 목표 {required:.1f}%를 "
                f"만족하는 가격이다. 현재가의 {hurdle_price / price_native * 100:.0f}% "
                "수준이라, 가격이 내려온다고 풀릴 문제가 아니다. "
                "FCF 궤적이 바뀌어야 한다."
            ),
            "bullOnlyCeiling": price_str(bull_only_price),
            "bullOnlyNote": (
                "강세 시나리오 하나만 쓰면 이 가격이 나온다. IPS 가 정한 자가 "
                "아니므로 매수 기준으로 쓰지 않는다 — 이 가격에서도 세 시나리오를 "
                f"섞은 연 수익률은 "
                f"{sum(annualized(per_share[n], bull_only_price, horizon) * probabilities[n] for n in per_share):.1f}%"
                f"로 요구수익률 {required:.1f}%에 미달한다."
            ),
            "actionNow": "추가 매수하지 않는다. 다음 분기 FCF 를 확인한다.",
            "highlights": highlights,
        },
        "reviewMap": [
            {"when": f"지금 · {price_str(price_native)}", "state": decision["verdict"],
             "action": "가격만 보고 진입하지 않습니다.",
             "why": f"연 수익률 {basis.get('position_return_pct', 0):.1f}%가 "
                    f"목표 {required:.1f}%에 못 미칩니다.", "signal": "bad"},
            {"when": ("다음 분기보고서" if source_label == "DART" else "다음 분기 10-Q"),
             "state": "강제 재검토",
             "action": f"{fetcher} fundamentals 로 스냅샷을 다시 만들고 "
                       "reverse_dcf 를 재실행합니다.",
             "why": "가격보다 투자 회수 여부가 더 중요한 판정 변수입니다.",
             "signal": "watch"},
            {"when": "FCF 2개 분기 연속 음수", "state": "논지 훼손 점검",
             "action": "강세 시나리오 확률을 낮추고 밸류에이션을 다시 계산합니다.",
             "why": "투자가 회수되지 않고 현금 소모 구조로 바뀌는 신호입니다.",
             "signal": "bad"},
        ],
        "portfolio": portfolio,
        "valuation": {
            **narrative["sectionTitles"]["valuation"],
            "impliedGrowth": f"10년 FCF 연 "
                             f"{calculation['outputs']['implied_growth_pct']:.1f}%",
            "modelInputs": (
                f"TTM FCF {money_m(values['fcf_ttm'])} · 할인율 "
                f"{calc_inputs['discount_rate_pct']:.0f}% · 영구성장률 "
                f"{calc_inputs['terminal_growth_pct']:.0f}% · 순현금 "
                f"{money_m(-values['net_debt'])} · {horizon}개월 수렴"
            ),
            "multiples": [
                {"label": "PER", "value": (f"{per:.2f}x" if per < 10 else f"{per:.1f}x") if per else "—",
                 "basis": f"TTM 순이익 {money_m(net_income_ttm)} · 현재가 기준"},
                {"label": "PSR", "value": (f"{psr:.2f}x" if psr < 10 else f"{psr:.1f}x") if psr else "—",
                 "basis": f"TTM 매출 {money_m(revenue_ttm)} · 현재가 기준"},
                # 역산 DCF 가 실제로 푸는 배수. PER·PSR 보다 이 판정에 직결된다.
                {"label": "EV/FCF",
                 "value": f"{(mcap + values['net_debt']) / values['fcf_ttm']:.1f}x",
                 "basis": f"EV {money_m(mcap + values['net_debt'])} ÷ "
                          f"TTM FCF {money_m(values['fcf_ttm'])}"},
            ],
            "scenarios": [
                {
                    "name": {"bear": "약세", "base": "기준", "bull": "강세"}[name],
                    "probability": f"{probabilities[name] * 100:.0f}%",
                    "fcfGrowth": f"10년간 {calc_inputs['scenario_growth_pct'][name]:.0f}%",
                    "valuePerShare": price_str(per_share[name]),
                    "totalGap": f"현재가 대비 {calc_scen[name]['total_return_pct']:+.1f}%",
                    "annualizedReturn": f"{calc_scen[name]['return_pct']:+.1f}% p.a.",
                    "signal": "bad" if calc_scen[name]["return_pct"] < 0 else "watch",
                }
                for name in ("bear", "base", "bull")
            ],
        },
        "cashFlow": {**narrative["sectionTitles"]["cashFlow"],
                     "rows": cash_rows, "chart": cash_chart},
        "peersTable": {
            **narrative["sectionTitles"]["peers"],
            "basisWarning": "동종사 행은 수집 시점이 서로 다르다. basis 열을 "
                            "함께 읽지 않으면 다른 기간을 비교하게 된다.",
            "rows": [{
                "ticker": args.ticker,
                "company": args.name,
                "basis": f"{fundamentals['period_end']} TTM",
                "revenue": money_m(revenue_ttm),
                "per": (f"{per:.2f}x" if per < 10 else f"{per:.1f}x") if per else "—",
                "psr": (f"{psr:.2f}x" if psr < 10 else f"{psr:.1f}x") if psr else "—",
                "focus": True,
            }] + narrative["peers"]["rows"],
        },
        "marketPositioning": narrative["marketPositioning"],
        "peers": {**narrative["sectionTitles"]["peers"], **narrative["peers"]},
        "detailedAnalysis": narrative["detailedAnalysis"],
        "sources": narrative["sources"] + [
            {"label": "밸류에이션 입력",
             "detail": f"{source_label} 스냅샷 {fundamentals['fundamentals_id']} · "
                       f"{fundamentals['period_end']} 기준"},
            {"label": "판정 엔진",
             "detail": f"trade_check {result['metadata']['engine_version']} · "
                       f"{result['metadata']['decision_snapshot_id']}"},
        ],
    }
    return report


def main() -> None:
    p = argparse.ArgumentParser(description="엔진 산출물 → 화면용 판정 리포트")
    # 기본값을 TSLA 로 두면 인자를 빠뜨렸을 때 다른 종목 리포트에 TSLA 가
    # 섞인다. 전부 필수로 받는다.
    p.add_argument("--ticker", required=True, help="엔진에 넘길 asset_id")
    p.add_argument("--ticker-label", required=True, help="화면 표시용 (예: 005930 · KOSPI)")
    p.add_argument("--name", required=True)
    p.add_argument("--quantity", type=float, required=True)
    p.add_argument("--price-krw", type=float, required=True)
    p.add_argument("--price-as-of", required=True)
    p.add_argument("--price-note", required=True)
    p.add_argument("--as-of", required=True)
    p.add_argument("--holdings", default=str(ROOT / "knowledge/portfolio/holdings.csv"))
    p.add_argument("--ips", default=str(ROOT / "knowledge/portfolio/ips.json"))
    p.add_argument("--analysis", required=True)
    p.add_argument("--calculations", default=str(ROOT / "knowledge/calculations"))
    p.add_argument("--fundamentals", required=True)
    p.add_argument("--market", default="KR", help="보유하지 않은 종목의 시장")
    p.add_argument("--risk-class", default="STOCK",
                   help="보유하지 않은 종목의 위험등급 (IPS 한도가 여기 걸린다)")
    p.add_argument("--prices", required=True)
    p.add_argument("--price-lookback-days", type=int, default=365,
                   help="고점·저점을 잡을 최근 일수 (0 이면 전체)")
    p.add_argument("--narrative", required=True)
    p.add_argument("--output", required=True)
    args = p.parse_args()

    report = build(args)
    Path(args.output).write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"판정 {report['decision']['verdict']} · "
          f"{len(report['decision']['issues'])}건")
    print(f"매수 상한 {report['decision']['entryCeiling']} "
          f"(강세 단독 기준이면 {report['decision']['bullOnlyCeiling']})")
    print(f"작성: {args.output}")


if __name__ == "__main__":
    main()
