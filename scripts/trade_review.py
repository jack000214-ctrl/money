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


# 거래내역서 기준 파일은 settle_date(결제일)를 쓴다. date 는 이전 형식.
DATE_COLUMNS = ("settle_date", "date")
# RECEIVE = 이벤트 무상입고. 내가 내린 매매 결정이 아니다.
SIDES = ("BUY", "SELL", "RECEIVE")


def load_trades(path):
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            lines = [ln for ln in f if not ln.lstrip().startswith("#")]
    except FileNotFoundError:
        fail(f"파일을 찾을 수 없다: {path}")

    reader = csv.DictReader(lines)
    fields = reader.fieldnames or []
    # 컬럼명이 어긋나면 전 행이 조용히 빠지고 빈 리포트가 그럴듯하게 나온다.
    # 크래시보다 나쁘므로 여기서 막는다.
    date_col = next((c for c in DATE_COLUMNS if c in fields), None)
    if date_col is None:
        fail(f"{path}: 날짜 컬럼이 없다. {' 또는 '.join(DATE_COLUMNS)} 가 필요하다 "
             f"(읽은 헤더: {', '.join(fields) or '없음'})")

    raw = [r for r in reader if (r.get(date_col) or "").strip()]

    # 종목별 이동평균 평단을 계산하는 도구다. 여러 종목을 한 원가에 합치면
    # 나오는 평단은 아무 의미가 없다.
    names = {(r.get("name") or "").strip() for r in raw if (r.get("name") or "").strip()}
    if len(names) > 1:
        fail(f"{path}: 여러 종목이 섞여 있다 ({', '.join(sorted(names))}). "
             "이 도구는 한 종목 파일을 받는다")

    rows = []
    for i, r in enumerate(raw, start=2):
        side = (r.get("side") or "").strip().upper()
        if side not in SIDES:
            fail(f"{i}행: side 는 {', '.join(SIDES)} 중 하나여야 한다 ({side!r})")
        rows.append({
            "date": r[date_col].strip(),
            "side": side,
            "qty": float(r["quantity"]),
            "price": float(r["price_krw"]),
        })
    if not rows:
        fail(f"{path}: 읽은 거래가 0건이다")
    rows.sort(key=lambda t: t["date"])
    return rows


def run(trades, price_now):
    qty = 0.0          # 보유수량
    cost = 0.0         # 취득원가 총액 (이동평균)
    realized = 0.0     # 실현손익
    bought_qty = bought_amt = sold_qty = sold_amt = 0.0
    received_qty = received_amt = 0.0
    log = []

    for t in trades:
        if t["side"] in ("BUY", "RECEIVE"):
            qty += t["qty"]
            # 무상입고도 기재가로 원가에 넣는다. 받은 시점의 평가액이
            # 취득가액이 되므로 평가손익이 이만큼 부풀지 않는다.
            cost += t["qty"] * t["price"]
            if t["side"] == "BUY":
                bought_qty += t["qty"]
                bought_amt += t["qty"] * t["price"]
            else:
                received_qty += t["qty"]
                received_amt += t["qty"] * t["price"]
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

    # 반사실: 매도를 한 번도 하지 않았다면.
    # 무상입고분도 그대로 갖고 있었을 테니 기준에 포함한다.
    acquired_qty = bought_qty + received_qty
    acquired_amt = bought_amt + received_amt
    hold_qty = acquired_qty
    hold_value = hold_qty * price_now
    hold_pnl = hold_value - acquired_amt

    return {
        "log": log,
        "qty": qty, "avg": avg_now, "cost": cost,
        "value_now": value_now, "unrealized": unrealized, "realized": realized,
        "total": realized + unrealized,
        "bought_qty": bought_qty, "bought_amt": bought_amt,
        # 매수가 0건이면 평균단가는 존재하지 않는다. 0 으로 때우지 않는다.
        "bought_avg": (bought_amt / bought_qty) if bought_qty else None,
        "sold_qty": sold_qty, "sold_amt": sold_amt,
        "sold_avg": (sold_amt / sold_qty) if sold_qty else None,
        "received_qty": received_qty, "received_amt": received_amt,
        "acquired_qty": acquired_qty, "acquired_amt": acquired_amt,
        "hold_qty": hold_qty, "hold_value": hold_value, "hold_pnl": hold_pnl,
        "trading_effect": (realized + unrealized) - hold_pnl,
        "sold_worth_now": sold_qty * price_now,
    }


def qty_str(q, width=0):
    """소수 수량(무상입고 0.000105주)을 0 으로 뭉개지 않는다."""
    s = f"{q:,.0f}" if abs(q - round(q)) < 1e-9 else f"{q:,.6f}"
    return f"{s:>{width}}" if width else s


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

    print(f"\n{'일자':<12}{'구분':<9}{'수량':>12}{'단가':>10}{'평단':>10}"
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
        print(f"{t['date']:<12}{t['side']:<9}{qty_str(t['qty'], 12)}"
              f"{t['price']:>10,.0f}{avg:>10,.0f}{pnl_s}  {mark}")

    print("\n" + "-" * 78)
    if r["bought_avg"] is None:
        print(f"총 매수  {qty_str(0, 12)}주  (매수 거래 없음)")
    else:
        print(f"총 매수  {qty_str(r['bought_qty'], 12)}주  {r['bought_amt']:>14,.0f}원  "
              f"평균 {r['bought_avg']:>9,.0f}원")
    if r["received_qty"]:
        print(f"무상입고 {qty_str(r['received_qty'], 12)}주  {r['received_amt']:>14,.0f}원  "
              f"← 매매 결정이 아니므로 매매 성적에서 제외")
    if r["sold_qty"]:
        print(f"총 매도  {qty_str(r['sold_qty'], 12)}주  {r['sold_amt']:>14,.0f}원  "
              f"평균 {r['sold_avg']:>9,.0f}원")
    print(f"현재보유 {qty_str(r['qty'], 12)}주  {r['value_now']:>14,.0f}원  "
          f"평단 {r['avg']:>9,.0f}원")

    print(f"\n실현손익   {r['realized']:>+16,.0f}원")
    print(f"평가손익   {r['unrealized']:>+16,.0f}원")
    print(f"합계       {r['total']:>+16,.0f}원")

    print("\n" + "=" * 78)
    print("[ 반사실 검증 — 한 번도 팔지 않았다면 ]")
    print("=" * 78)
    print(f"보유수량     {qty_str(r['hold_qty'], 10)}주 (실제 {qty_str(r['qty'])}주)")
    print(f"평가금액     {r['hold_value']:>14,.0f}원")
    print(f"손익         {r['hold_pnl']:>+14,.0f}원")
    print(f"실제 손익    {r['total']:>+14,.0f}원")
    print("-" * 78)
    eff = r["trading_effect"]
    verdict = "매매가 도움이 됐다" if eff > 0 else "매매가 손해였다"
    print(f"**매매의 순효과  {eff:>+14,.0f}원**   → {verdict}")
    print(f"\n매도한 {qty_str(r['sold_qty'])}주를 지금까지 보유했다면 "
          f"{r['sold_worth_now']:,.0f}원")
    print(f"실제 매도 대금은                      {r['sold_amt']:,.0f}원")
    print(f"차이                                  {r['sold_amt']-r['sold_worth_now']:+,.0f}원")
    print("\n※ 거래세·수수료는 반영하지 않았다. 반영하면 매매 쪽이 더 불리해진다.")
    print("※ 매도 대금을 다른 곳에 더 잘 굴렸다면 이 비교는 달라진다.")


if __name__ == "__main__":
    main()
