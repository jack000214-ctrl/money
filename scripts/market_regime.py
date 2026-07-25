#!/usr/bin/env python3
"""시장 체제 판정 — 지금이 평소인가, 평소가 아닌가.

팬데믹·전쟁 같은 체제 전환은 개별 종목 분석으로 잡히지 않는다.
그러나 **뉴스로 판단하면 안 된다.** 헤드라인은 매일 무섭고, 뉴스를 보고 매수를 미루면
대부분의 경우 손해다. 시장은 대체로 오른다.

그래서 관측 가능한 지표로 체제를 판정하고, 뉴스는 지표가 이상할 때
"무슨 일인지" 찾는 용도로만 쓴다 (docs/00-product-charter.md 6.1 뉴스=탐색 신호).

**이 판정의 목적은 매수를 막는 것이 아니다.** 위기장에서는 오히려 사야 한다.
목적은 "평소 판단 기준이 안 통하는 구간이니 한 번에 넣지 말라"를 알리는 것이다.
"""

import argparse
import json
import statistics
import sys
import urllib.error
import urllib.request

CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{s}?range={r}&interval=1d"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}

INDICES = [
    ("^GSPC", "S&P 500"),
    ("^IXIC", "나스닥"),
    ("^KS11", "코스피"),
    ("^KQ11", "코스닥"),
]


def series(symbol, rng="2y"):
    try:
        req = urllib.request.Request(CHART.format(s=symbol, r=rng), headers=UA)
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read().decode("utf-8"))
        res = d["chart"]["result"][0]
        closes = [c for c in res["indicators"]["quote"][0]["close"] if c is not None]
        return closes
    except (urllib.error.URLError, KeyError, IndexError, TypeError) as e:
        print(f"  {symbol}: 수집 실패 ({type(e).__name__})", file=sys.stderr)
        return []


def classify(vix, worst_drawdown_pct):
    """VIX 와 최대 낙폭으로 체제를 분류한다. 순수 함수라 테스트할 수 있다.

    낙폭 판정은 **더 심한 조건을 먼저** 본다. 순서를 바꾸면 -39% 도 '조정'이 된다.
    """
    score, notes = 0, []
    if vix is not None:
        if vix >= 40:
            score += 3
            notes.append("VIX 40 이상 — 과거 위기 국면에서 나타난 수준")
        elif vix >= 30:
            score += 2
            notes.append("VIX 30 이상 — 시장이 명확히 불안하다")
        elif vix >= 22:
            score += 1
            notes.append("VIX 22 이상 — 평시보다 높다")

    dd = worst_drawdown_pct
    if dd <= -20:
        score += 3
        notes.append(f"주요 지수가 고점 대비 {dd:.1f}% — 약세장 구간")
    elif dd <= -10:
        score += 2
        notes.append(f"주요 지수가 고점 대비 {dd:.1f}% — 조정 구간")
    elif dd <= -5:
        score += 1

    if score >= 5:
        return "CRISIS", "🔴", score, notes
    if score >= 3:
        return "STRESS", "🟠", score, notes
    if score >= 1:
        return "ELEVATED", "🟡", score, notes
    return "NORMAL", "🟢", score, notes


def drawdown_label(dd):
    """더 심한 조건을 먼저 본다."""
    if dd <= -20:
        return "  ← 약세장"
    if dd <= -10:
        return "  ← 조정"
    return ""


def percentile(values, x):
    if not values:
        return None
    return sum(1 for v in values if v < x) / len(values) * 100


def main():
    p = argparse.ArgumentParser(description="시장 체제 판정")
    p.add_argument("--range", default="2y", help="비교 기간 (1y, 2y, 5y)")
    a = p.parse_args()

    print("=" * 70)
    print(f"시장 체제 판정   (비교 기간 {a.range})")
    print("=" * 70)

    vix_now = None
    vix = series("^VIX", a.range)
    if vix:
        vix_now = vix[-1]
        print(f"\n[ 변동성 ]")
        print(f"  VIX          {vix_now:>8.2f}   중앙값 {statistics.median(vix):>6.2f}   "
              f"{a.range} 백분위 {percentile(vix, vix_now):>5.1f}%")

    # 2. 지수별 고점 대비 낙폭
    print(f"\n[ 최고점 대비 낙폭 ]")
    worst = 0.0
    for sym, name in INDICES:
        s = series(sym, a.range)
        if not s:
            continue
        cur, hi = s[-1], max(s)
        dd = (cur / hi - 1) * 100
        worst = min(worst, dd)
        flag = drawdown_label(dd)
        print(f"  {name:<12}{cur:>12,.2f}   고점 {hi:>12,.2f}   {dd:>+7.1f}%{flag}")

    regime, mark, score, notes = classify(vix_now, worst)

    print("\n" + "=" * 70)
    print(f"{mark} 체제: {regime}   (점수 {score})")
    print("=" * 70)
    for n in notes:
        print(f"  • {n}")
    if not notes:
        print("  • 특이 신호 없음")

    print("\n[ 이 체제에서 할 일 ]")
    guide = {
        "NORMAL": [
            "평소 판단 기준이 통한다. 종목 분석대로 진행한다.",
            "매수는 계획한 분할 횟수대로.",
        ],
        "ELEVATED": [
            "평소보다 변동이 크다. 체결가가 계획과 어긋날 수 있다.",
            "분할 횟수를 한 단계 늘린다.",
        ],
        "STRESS": [
            "개별 종목 논지보다 시장 전체가 가격을 지배하는 구간이다.",
            "**좋은 회사도 같이 빠진다.** 논지가 틀린 것이 아닐 수 있다.",
            "분할 횟수를 2배로 늘리고 기간을 벌린다. 한 번에 넣지 않는다.",
            "지금 무슨 일이 있는지 뉴스를 확인한다 — 판단이 아니라 사실 파악용.",
        ],
        "CRISIS": [
            "체제 전환 국면이다. 과거 밸류에이션 밴드가 기준이 되지 않는다.",
            "**팔지 않는 것이 첫 번째 할 일이다.** 공포 매도가 최대 손실원이다.",
            "적립은 계속한다. 중단하면 복리가 끊긴다.",
            "추가 매수는 최소 4~6회로 나누고 몇 달에 걸친다.",
            "뉴스를 확인하되 헤드라인으로 매매하지 않는다.",
        ],
    }[regime]
    for g in guide:
        print(f"  • {g}")

    print("\n※ 이 판정은 예측이 아니라 현재 상태의 분류다.")
    print("※ 체제는 매수를 막는 신호가 아니라 **분할 횟수를 조절하는** 신호다.")
    print("※ 위기장은 장기 투자자에게 매수 기회였던 경우가 많다. 문제는 한 번에 넣는 것이다.")


if __name__ == "__main__":
    main()
