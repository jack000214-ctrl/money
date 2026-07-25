#!/usr/bin/env python3
"""매매 내역을 검증한다.

핵심 질문은 "얼마 벌었나"가 아니라 **"매매가 안 하느니만 못했나"**다.
실현손익만 보면 판 것이 전부 이익처럼 보인다. 팔지 않았을 경우와 비교해야
매매 자체의 기여를 알 수 있다.

평단은 이동평균법을 쓴다 (국내 증권사 표기와 동일).
"""

import argparse
import csv
import sys
from pathlib import Path


def fail(msg):
    print(f"오류: {msg}", file=sys.stderr)
    sys.exit(1)


def load_trades(path):
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            lines = [ln for ln in f if not ln.lstrip().startswith("#")]
    except FileNotFoundError:
        fail(f"파일을 찾을 수 없다: {path}")
    rows = []
    for i, r in enumerate(csv.DictReader(lines), start=2):
        if not r.get("date"):
            continue
        side = r["side"].strip().upper()
        if side not in ("BUY", "SELL"):
            fail(f"{i}행: side 는 BUY 또는 SELL 이어야 한다 ({side})")
        rows.append({
            "date": r["date"].strip(),
            "side": side,
            "qty": float(r["quantity"]),
            "price": float(r["price_krw"]),
        })
    rows.sort(key=lambda t: t["date"])
    return rows


def run(trades, price_now):
    qty = 0.0          # 보유수량
    cost = 0.0         # 취득원가 총액 (이동평균)
    realized = 0.0     # 실현손익
    bought_qty = bought_amt = sold_qty = sold_amt = 0.0
    log = []

    for t in trades:
        if t["side"] == "BUY":
            qty += t["qty"]
            cost += t["qty"] * t["price"]
            bought_qty += t["qty"]
            bought_amt += t["qty"] * t["price"]
            log.append((t, None, cost / qty if qty else 0))
        else:
            if t["qty"] > qty + 1e-9:
                fail(f"{t['date']}: 보유({qty:g})보다 많은 수량({t['qty']:g})을 매도했다")
            avg = cost / qty if qty else 0
            pnl = (t["price"] - avg) * t["qty"]
            realized += pnl
            cost -= avg * t["qty"]
            qty -= t["qty"]
            sold_qty += t["qty"]
            sold_amt += t["qty"] * t["price"]
            log.append((t, pnl, avg))

    avg_now = cost / qty if qty else 0
    value_now = qty * price_now
    unrealized = value_now - cost

    # 반사실: 매도를 한 번도 하지 않았다면
    hold_qty = bought_qty
    hold_value = hold_qty * price_now
    hold_pnl = hold_value - bought_amt

    return {
        "log": log,
        "qty": qty, "avg": avg_now, "cost": cost,
        "value_now": value_now, "unrealized": unrealized, "realized": realized,
        "total": realized + unrealized,
        "bought_qty": bought_qty, "bought_amt": bought_amt,
        "sold_qty": sold_qty, "sold_amt": sold_amt,
        "hold_qty": hold_qty, "hold_value": hold_value, "hold_pnl": hold_pnl,
        "trading_effect": (realized + unrealized) - hold_pnl,
        "sold_worth_now": sold_qty * price_now,
    }


def main():
    p = argparse.ArgumentParser(description="매매 내역 검증")
    p.add_argument("--trades", required=True)
    p.add_argument("--price", type=float, required=True, help="현재가")
    p.add_argument("--buy-zone", type=float, nargs=2, metavar=("LOW", "HIGH"),
                   help="본인 규칙의 매수 구간")
    p.add_argument("--sell-above", type=float, help="본인 규칙의 매도 기준가")
    a = p.parse_args()

    trades = load_trades(a.trades)
    r = run(trades, a.price)

    print("=" * 78)
    print(f"매매 검증 — {Path(a.trades).stem}   현재가 {a.price:,.0f}원   거래 {len(trades)}건")
    print("=" * 78)

    print(f"\n{'일자':<12}{'구분':<6}{'수량':>6}{'단가':>10}{'평단':>10}"
          f"{'실현손익':>12}  규칙")
    print("-" * 78)
    for t, pnl, avg in r["log"]:
        mark = ""
        if a.buy_zone and a.sell_above:
            lo, hi = a.buy_zone
            if t["side"] == "SELL" and t["price"] < a.sell_above:
                mark = "⚠ 매수구간에서 매도" if lo <= t["price"] <= hi else "⚠ 기준가 미만 매도"
            elif t["side"] == "BUY" and t["price"] > hi:
                mark = "⚠ 매수구간 위에서 매수"
        pnl_s = f"{pnl:>+12,.0f}" if pnl is not None else " " * 12
        print(f"{t['date']:<12}{t['side']:<6}{t['qty']:>6.0f}{t['price']:>10,.0f}"
              f"{avg:>10,.0f}{pnl_s}  {mark}")

    print("\n" + "-" * 78)
    print(f"총 매수  {r['bought_qty']:>6.0f}주  {r['bought_amt']:>14,.0f}원  "
          f"평균 {r['bought_amt']/r['bought_qty']:>9,.0f}원")
    if r["sold_qty"]:
        print(f"총 매도  {r['sold_qty']:>6.0f}주  {r['sold_amt']:>14,.0f}원  "
              f"평균 {r['sold_amt']/r['sold_qty']:>9,.0f}원")
    print(f"현재보유 {r['qty']:>6.0f}주  {r['value_now']:>14,.0f}원  "
          f"평단 {r['avg']:>9,.0f}원")

    print(f"\n실현손익   {r['realized']:>+16,.0f}원")
    print(f"평가손익   {r['unrealized']:>+16,.0f}원")
    print(f"합계       {r['total']:>+16,.0f}원")

    print("\n" + "=" * 78)
    print("[ 반사실 검증 — 한 번도 팔지 않았다면 ]")
    print("=" * 78)
    print(f"보유수량     {r['hold_qty']:>10.0f}주 (실제 {r['qty']:.0f}주)")
    print(f"평가금액     {r['hold_value']:>14,.0f}원")
    print(f"손익         {r['hold_pnl']:>+14,.0f}원")
    print(f"실제 손익    {r['total']:>+14,.0f}원")
    print("-" * 78)
    eff = r["trading_effect"]
    verdict = "매매가 도움이 됐다" if eff > 0 else "매매가 손해였다"
    print(f"**매매의 순효과  {eff:>+14,.0f}원**   → {verdict}")
    print(f"\n매도한 {r['sold_qty']:.0f}주를 지금까지 보유했다면 {r['sold_worth_now']:,.0f}원")
    print(f"실제 매도 대금은                      {r['sold_amt']:,.0f}원")
    print(f"차이                                  {r['sold_amt']-r['sold_worth_now']:+,.0f}원")
    print("\n※ 거래세·수수료는 반영하지 않았다. 반영하면 매매 쪽이 더 불리해진다.")
    print("※ 매도 대금을 다른 곳에 더 잘 굴렸다면 이 비교는 달라진다.")


if __name__ == "__main__":
    main()
