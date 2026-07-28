#!/usr/bin/env python3
"""가격 시계열 수집과 밴드 분석.

가격은 그 자체로 매매 근거가 아니다. 그러나 밸류에이션 판단에는 위치 정보가 필요하다 —
"싸다"는 말은 무엇 대비 싼지가 있어야 성립한다.

또 하나의 용도는 **급변동일 자동 탐지**다. 일간 ±5% 이상 움직인 날을 찾아주면
그날 무슨 일이 있었는지 조사할 대상이 나온다 (timeline.md 의 입력).

출처: Yahoo Finance chart API (비공식). 한국·미국 종목 모두 지원한다.
"""

import argparse
import csv
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRICE_DIR = ROOT / "knowledge" / "prices"
CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range={rng}&interval=1d"


def safe_name(symbol):
    """심볼 → 파일명. 지수(^KS11)의 ^ 는 셸에서 사고를 내므로 떼어낸다."""
    # 지수(^KS11)·환율(KRW=X)의 특수문자를 뗀다. 파일명에 두면 셸 글롭·
    # 리다이렉션 사고가 난다.
    return symbol.replace(".", "_").replace("^", "").replace("=", "")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"


def fail(msg):
    print(f"오류: {msg}", file=sys.stderr)
    sys.exit(1)


def fetch(symbol, rng):
    req = urllib.request.Request(CHART.format(sym=symbol, rng=rng),
                                 headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        fail(f"HTTP {e.code} — {symbol}")
    except urllib.error.URLError as e:
        fail(f"연결 실패 — {e.reason}")

    res = (data.get("chart") or {}).get("result")
    if not res:
        err = (data.get("chart") or {}).get("error")
        fail(f"{symbol}: 데이터 없음 {err}")
    r0 = res[0]
    meta, ts = r0["meta"], r0.get("timestamp") or []
    q = r0["indicators"]["quote"][0]
    rows = []
    for i, t in enumerate(ts):
        close = q["close"][i]
        if close is None:
            continue
        rows.append({
            "date": datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d"),
            "close": close,
            "high": q["high"][i], "low": q["low"][i], "volume": q["volume"][i],
        })
    return meta, rows


def save(symbol, meta, rows):
    PRICE_DIR.mkdir(parents=True, exist_ok=True)
    path = PRICE_DIR / f"{safe_name(symbol)}.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        f.write(f"# {symbol} 일별 종가 / 통화 {meta.get('currency')}\n")
        f.write(f"# 수집일 {datetime.now(timezone.utc).strftime('%Y-%m-%d')} "
                f"/ 출처 Yahoo Finance chart API\n")
        w = csv.DictWriter(f, fieldnames=["date", "close", "high", "low", "volume"])
        w.writeheader()
        w.writerows(rows)
    return path


def load(symbol):
    path = PRICE_DIR / f"{safe_name(symbol)}.csv"
    if not path.exists():
        fail(f"{path.relative_to(ROOT)} 가 없다. 먼저 history 로 수집한다.")
    with open(path, newline="", encoding="utf-8") as f:
        lines = [ln for ln in f if not ln.lstrip().startswith("#")]
    return [{"date": r["date"], "close": float(r["close"])}
            for r in csv.DictReader(lines) if r.get("close")]


def cmd_history(a):
    meta, rows = fetch(a.symbol, a.range)
    path = save(a.symbol, meta, rows)
    print(f"{a.symbol}  {meta.get('currency')}  {len(rows)}일  "
          f"{rows[0]['date']} ~ {rows[-1]['date']}")
    print(f"저장: {path.relative_to(ROOT)}")


def cmd_band(a):
    rows = load(a.symbol)
    if a.days:
        rows = rows[-a.days:]
    closes = [r["close"] for r in rows]
    cur = closes[-1]
    hi, lo = max(closes), min(closes)
    hi_row = max(rows, key=lambda r: r["close"])
    lo_row = min(rows, key=lambda r: r["close"])
    pos = (cur - lo) / (hi - lo) * 100 if hi > lo else 50.0

    print("=" * 62)
    print(f"가격 밴드 — {a.symbol}   {rows[0]['date']} ~ {rows[-1]['date']} "
          f"({len(rows)}거래일)")
    print("=" * 62)
    print(f"현재가        {cur:>14,.2f}")
    print(f"기간 최고     {hi:>14,.2f}   {hi_row['date']}")
    print(f"기간 최저     {lo:>14,.2f}   {lo_row['date']}")
    print(f"밴드 내 위치  {pos:>13.1f}%   (0=최저, 100=최고)")
    print(f"고점 대비     {(cur/hi-1)*100:>+13.1f}%")
    print(f"저점 대비     {(cur/lo-1)*100:>+13.1f}%")

    bar = int(pos / 100 * 40)
    print(f"\n  최저 [{'─' * bar}●{'─' * (40 - bar)}] 최고")

    if a.cost:
        print(f"\n매수 평단     {a.cost:>14,.2f}")
        print(f"평단 대비     {(cur/a.cost-1)*100:>+13.1f}%")
        cost_pos = (a.cost - lo) / (hi - lo) * 100 if hi > lo else 50
        print(f"평단의 위치   {cost_pos:>13.1f}%   "
              f"→ {'밴드 상단부에서 샀다' if cost_pos > 60 else '밴드 하단부에서 샀다' if cost_pos < 40 else '밴드 중간에서 샀다'}")

    # 급변동일 — 조사할 날짜를 찾아준다
    shocks = []
    for i in range(1, len(rows)):
        ch = rows[i]["close"] / rows[i - 1]["close"] - 1
        if abs(ch) >= a.shock / 100:
            shocks.append((rows[i]["date"], ch * 100, rows[i]["close"]))
    print(f"\n[ 일간 ±{a.shock:.0f}% 이상 급변동일 ]  {len(shocks)}건")
    if not shocks:
        print("  없음")
    for d, ch, c in shocks[-12:]:
        print(f"  {d}  {ch:>+7.1f}%   종가 {c:>12,.2f}   ← 이날 무슨 일이 있었는지 조사한다")

    print("\n※ 가격은 매매 근거가 아니다. 밸류에이션 판단의 위치 정보이자")
    print("  조사할 이벤트 날짜를 찾는 용도다.")


def main():
    p = argparse.ArgumentParser(description="가격 시계열 수집·밴드 분석")
    sub = p.add_subparsers(dest="cmd", required=True)

    h = sub.add_parser("history", help="일별 종가 수집")
    h.add_argument("symbol", help="IBM / RXRX / 214450.KQ / SCHD")
    h.add_argument("--range", default="2y", help="1y, 2y, 5y, max")

    b = sub.add_parser("band", help="현재가의 밴드 내 위치")
    b.add_argument("symbol")
    b.add_argument("--days", type=int, help="최근 N거래일로 제한")
    b.add_argument("--cost", type=float, help="내 매수 평단")
    b.add_argument("--shock", type=float, default=5.0, help="급변동 기준 %%")

    a = p.parse_args()
    {"history": cmd_history, "band": cmd_band}[a.cmd](a)


if __name__ == "__main__":
    main()
