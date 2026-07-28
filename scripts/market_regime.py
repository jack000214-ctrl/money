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

# 체제는 **시장별로** 판정한다. 전역 최대낙폭을 쓰면 한국 종목을 살 때
# S&P −2.6% 가 섞이고, 미국 종목을 살 때 코스피 −34% 가 섞인다. 2026-07-28 이
# 정확히 그런 국면이었다 — 코스피 −34.1% / 코스닥 −42.9% 인데 S&P 는 −2.6%,
# VIX 는 18.67(평시)로 **한국만 무너진 장세**였다. 섞으면 그 사실이 사라진다.
MARKETS = {
    "US": {
        "indices": [("^GSPC", "S&P 500"), ("^IXIC", "나스닥")],
        "vol": ("^VIX", "VIX"),
    },
    "KR": {
        "indices": [("^KS11", "코스피"), ("^KQ11", "코스닥")],
        # 한국 변동성 지수(VKOSPI)는 야후에 안정적으로 없다. 전역 VIX 를 참고로만
        # 쓰고 점수에는 넣지 않는다 — 오늘처럼 VIX 가 평온한데 코스피만 무너지면
        # VIX 를 점수에 넣는 순간 체제가 과소평가된다.
        "vol": ("^VIX", "VIX (참고 — 전역 지표라 점수 미반영)"),
        "vol_in_score": False,
    },
}
INDICES = [("^GSPC", "S&P 500"), ("^IXIC", "나스닥"),
           ("^KS11", "코스피"), ("^KQ11", "코스닥")]


def safe_name(symbol):
    return symbol.replace(".", "_").replace("^", "")


def local_series(symbol, days):
    """knowledge/prices/ 에 있으면 그것을 쓴다.

    수집된 스냅샷을 쓰면 재현이 가능하고 네트워크 호출도 줄어든다. 없으면
    야후에서 직접 받는다(그 경우 재현 불가라고 표시한다).
    """
    import csv as _csv
    from pathlib import Path as _Path
    path = (_Path(__file__).resolve().parent.parent
            / "knowledge" / "prices" / f"{safe_name(symbol)}.csv")
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as f:
        rows = list(_csv.DictReader(l for l in f if not l.startswith("#")))
    closes = [float(r["close"]) for r in rows]
    return closes[-days:] if days and len(closes) > days else closes


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


def analyse(market, rng, days):
    """한 시장의 체제를 판정하고 구조화해 돌려준다."""
    conf = MARKETS[market]
    sym, vol_label = conf["vol"]
    vol = local_series(sym, days) or series(sym, rng)
    vol_now = vol[-1] if vol else None
    vol_block = None
    if vol:
        vol_block = {
            "symbol": sym, "label": vol_label, "value": round(vol_now, 2),
            "median": round(statistics.median(vol), 2),
            "percentile": round(percentile(vol, vol_now), 1),
            "in_score": conf.get("vol_in_score", True),
        }

    rows, worst = [], 0.0
    for isym, name in conf["indices"]:
        closes = local_series(isym, days) or series(isym, rng)
        if not closes:
            continue
        cur, hi = closes[-1], max(closes)
        dd = (cur / hi - 1) * 100
        worst = min(worst, dd)
        rows.append({"symbol": isym, "name": name, "close": round(cur, 2),
                     "high": round(hi, 2), "drawdown_pct": round(dd, 1)})

    score_vol = vol_now if conf.get("vol_in_score", True) else None
    regime, mark, score, notes = classify(score_vol, worst)
    return {
        "market": market, "regime": regime, "mark": mark, "score": score,
        "notes": notes, "volatility": vol_block, "indices": rows,
        "worst_drawdown_pct": round(worst, 1),
        "guidance": GUIDANCE[regime],
    }


GUIDANCE = {
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
}


def main():
    p = argparse.ArgumentParser(description="시장 체제 판정 (시장별)")
    p.add_argument("--range", default="2y", help="비교 기간 (1y, 2y, 5y)")
    p.add_argument("--days", type=int, default=500,
                   help="로컬 시세를 쓸 때 볼 거래일 수")
    p.add_argument("--out", help="스냅샷 저장 경로 "
                                 "(기본 knowledge/market/regime.json)")
    p.add_argument("--json", action="store_true", help="표 대신 JSON 만 출력")
    a = p.parse_args()

    import datetime as _dt
    from pathlib import Path as _Path
    snapshot = {
        "schema_version": "market-regime/1.0.0",
        "collected_at": _dt.datetime.now(
            _dt.timezone(_dt.timedelta(hours=9))).isoformat(timespec="seconds"),
        "lookback_days": a.days,
        "by_market": {m: analyse(m, a.range, a.days) for m in MARKETS},
        "_note": "체제는 예측이 아니라 현재 상태의 분류다. 매수를 막는 신호가 "
                 "아니라 **분할 횟수를 조절하는** 신호다. 위기장은 장기 투자자에게 "
                 "매수 기회였던 경우가 많고, 문제는 한 번에 넣는 것이다.",
    }
    # 시장별 체제가 갈리면 그 사실 자체가 중요한 정보다
    regimes = {m: v["regime"] for m, v in snapshot["by_market"].items()}
    if len(set(regimes.values())) > 1:
        snapshot["divergence"] = (
            f"시장별 체제가 다르다 — {', '.join(f'{k} {v}' for k, v in regimes.items())}. "
            "전역 위기가 아니라 특정 시장의 문제라는 뜻이므로, 원인을 그 시장에서 "
            "찾아야 한다")

    out = _Path(a.out) if a.out else (_Path(__file__).resolve().parent.parent
                                      / "knowledge" / "market" / "regime.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")

    if a.json:
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
        return

    for market, block in snapshot["by_market"].items():
        print("=" * 70)
        print(f"{block['mark']} {market} 체제: {block['regime']}  (점수 {block['score']})")
        print("=" * 70)
        v = block["volatility"]
        if v:
            note = "" if v["in_score"] else "  ※ 점수 미반영"
            print(f"  {v['label']:<34}{v['value']:>8.2f}   "
                  f"중앙값 {v['median']:>6.2f}  백분위 {v['percentile']:>5.1f}%{note}")
        for row in block["indices"]:
            print(f"  {row['name']:<12}{row['close']:>12,.2f}   "
                  f"고점 {row['high']:>12,.2f}   {row['drawdown_pct']:>+7.1f}%"
                  f"{drawdown_label(row['drawdown_pct'])}")
        for n in block["notes"] or ["특이 신호 없음"]:
            print(f"  • {n}")
        print("  [ 할 일 ]")
        for g in block["guidance"]:
            print(f"    - {g}")
        print()

    if "divergence" in snapshot:
        print("!! " + snapshot["divergence"])
    print(f"\n저장: {out}")


if __name__ == "__main__":
    main()
