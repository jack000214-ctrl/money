#!/usr/bin/env python3
"""배당 정산 — 거래내역 기준으로 **실제로 받은** 배당을 계산한다.

추정하지 않는다. 배당락일마다 그 시점의 보유수량을 거래내역에서 재구성해 곱한다.

이 도구가 필요한 이유
  파마리서치 2025년 배당은 주당 3,700원이지만 배당락일이 2025-12-29 였다.
  사용자의 첫 매수는 2026-02-05 이므로 **한 푼도 받지 않았다.**
  종목의 배당률을 보유자의 수익으로 착각하면 성과가 과대평가된다.

주의: 배당락일에 보유했으면 배당을 받는다. 락일 당일 매수는 받지 못한다.
"""

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from trade_review import load_trades  # noqa: E402

CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{s}?range={r}&interval=1d&events=div"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}


def fetch_dividends(symbol, rng="5y"):
    req = urllib.request.Request(CHART.format(s=symbol, r=rng), headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read().decode("utf-8"))
    except urllib.error.URLError as e:
        sys.exit(f"오류: 배당 조회 실패 — {e.reason}")
    res = d.get("chart", {}).get("result")
    if not res:
        sys.exit(f"오류: {symbol} 데이터 없음")
    meta = res[0]["meta"]
    events = res[0].get("events", {}).get("dividends", {}) or {}
    out = [{
        "date": datetime.fromtimestamp(v["date"], timezone.utc).strftime("%Y-%m-%d"),
        "amount": v["amount"],
    } for v in events.values()]
    return sorted(out, key=lambda x: x["date"]), meta.get("currency")


def shares_on(trades, date):
    """배당락일 **직전**까지의 체결로 보유수량을 만든다.

    락일 당일 매수는 배당을 받지 못하므로 '<' 로 자른다.
    """
    qty = 0.0
    for t in trades:
        if t["date"] >= date:
            break
        qty += t["qty"] if t["side"] == "BUY" else -t["qty"]
    return qty


def main():
    p = argparse.ArgumentParser(description="거래내역 기준 배당 정산")
    p.add_argument("--trades", required=True, help="거래내역 CSV")
    p.add_argument("--symbol", required=True, help="Yahoo 심볼 (IBM / 214450.KQ)")
    p.add_argument("--fx", type=float, help="외화 배당의 원화 환산율")
    p.add_argument("--range", default="5y")
    a = p.parse_args()

    trades = load_trades(a.trades)
    divs, currency = fetch_dividends(a.symbol, a.range)
    first_trade = trades[0]["date"] if trades else None

    print("=" * 78)
    print(f"배당 정산 — {a.symbol}   통화 {currency}")
    print("=" * 78)
    print(f"첫 매수일 {first_trade}   배당 이벤트 {len(divs)}건 (최근 {a.range})")
    print(f"\n{'배당락일':<12}{'주당':>12}{'보유수량':>10}{'수령액':>16}  비고")
    print("-" * 78)

    total = 0.0
    received = 0
    for d in divs:
        qty = shares_on(trades, d["date"])
        amount = qty * d["amount"]
        total += amount
        note = ""
        if qty <= 0:
            note = "미보유 — 수령 없음"
            if first_trade and d["date"] < first_trade:
                note = "첫 매수 이전 — 수령 없음"
        else:
            received += 1
        print(f"{d['date']:<12}{d['amount']:>12,.4f}{qty:>10,.0f}"
              f"{amount:>16,.2f}  {note}")

    print("-" * 78)
    print(f"{'합계':<12}{'':<12}{'':<10}{total:>16,.2f} {currency}")
    if a.fx and currency != "KRW":
        print(f"{'원화 환산':<12}{'':<12}{'':<10}{total * a.fx:>16,.0f} KRW  "
              f"(환율 {a.fx:,.0f})")

    print(f"\n수령한 배당 {received}건 / 전체 {len(divs)}건")
    if received == 0:
        print("⚠ 이 종목에서 받은 배당이 없다. 배당률을 성과에 반영하면 안 된다.")
    print("\n※ 배당락일에 보유 중이어야 받는다. 락일 당일 매수는 제외했다.")
    print("※ 세금(배당소득세, 외국납부세액)은 반영하지 않은 세전 금액이다.")


if __name__ == "__main__":
    main()
