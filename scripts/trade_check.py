#!/usr/bin/env python3
"""매매 전 점검 — "이거 사도 돼?" / "지금 팔까?" 에 답한다.

이 스크립트는 주가를 예측하지 않는다. 예측 대신 다음을 계산한다.

  1. 이 매매 후 비중이 한도를 넘는가
  2. 내가 정한 매수·매도 구간의 어디인가
  3. 같은 종목에서 이 가격대에 했던 과거 매매는 결과가 어땠는가

근거: 파마리서치 검증(2026-07-26)에서 손실의 91%가 예측 실패가 아니라
**본인이 정한 규칙을 어긴 매도**에서 나왔다. 규칙 위반만 막아도 대부분이 걸러진다.
"""

import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOLDINGS = ROOT / "knowledge" / "portfolio" / "holdings.csv"
TRADES = ROOT / "knowledge" / "portfolio" / "trades"


def read_csv(path, required=None):
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8-sig") as f:
        lines = [ln for ln in f if not ln.lstrip().startswith("#")]
    rows = [r for r in csv.DictReader(lines) if any((v or "").strip() for v in r.values())]
    if required and rows and (missing := required - set(rows[0])):
        sys.exit(f"오류: {path} 에 열이 없다: {', '.join(sorted(missing))}")
    return rows


def num(v):
    try:
        return float(str(v).replace(",", "").strip())
    except (ValueError, AttributeError):
        return None


def main():
    p = argparse.ArgumentParser(description="매매 전 점검")
    p.add_argument("asset_id", help="종목 식별자 (holdings.csv 의 asset_id)")
    p.add_argument("side", choices=["buy", "sell"])
    p.add_argument("quantity", type=float)
    p.add_argument("--price", type=float, required=True, help="예정 체결가")
    p.add_argument("--limit", type=float, default=10.0, help="단일 종목 비중 한도 %%")
    p.add_argument("--buy-below", type=float, help="내 매수 기준가 (이하면 매수 구간)")
    p.add_argument("--sell-above", type=float, help="내 매도 기준가 (이상이면 매도 구간)")
    a = p.parse_args()

    holdings = read_csv(HOLDINGS, {"asset_id", "name", "value_krw"})
    total = sum(num(h["value_krw"]) or 0 for h in holdings)
    cash = sum(num(h["value_krw"]) or 0 for h in holdings
               if (h.get("asset_type") or "").upper() == "CASH")
    me = next((h for h in holdings if h["asset_id"] == a.asset_id), None)
    if not me:
        sys.exit(f"오류: {a.asset_id} 를 holdings.csv 에서 찾을 수 없다")

    held_qty = num(me.get("quantity")) or 0
    held_val = num(me["value_krw"])
    amount = a.quantity * a.price
    signed = amount if a.side == "buy" else -amount

    # 현금이 대기자금이면 총자산에 이미 들어있다. 현금으로 사면 분모가 변하지 않는다.
    # 외부에서 새 돈을 넣는 경우에만 총자산이 늘어난다.
    new_total = total if cash else total + signed
    new_val = held_val + signed
    w_before = held_val / total * 100
    w_after = new_val / new_total * 100 if new_total else 0

    print("=" * 72)
    print(f"매매 점검 — {me['name']} ({a.asset_id})")
    print("=" * 72)
    print(f"{'구분':<14}{a.side.upper()} {a.quantity:,.0f}주 @ {a.price:,.0f}원 "
          f"= {amount:,.0f}원")
    print(f"{'보유':<14}{held_qty:,.0f}주 → {held_qty + (a.quantity if a.side=='buy' else -a.quantity):,.0f}주")
    print(f"{'비중':<14}{w_before:.2f}% → {w_after:.2f}%  (한도 {a.limit:.0f}%)")
    if cash:
        print(f"{'분모':<14}총자산 {total:,.0f}원 (현금 {cash:,.0f}원 포함, 매매해도 불변)")
        if a.side == "buy":
            room = a.limit / 100 * total - held_val
            print(f"{'한도 여유':<14}{room:>+,.0f}원  "
                  f"(비중 {a.limit:.0f}% = {a.limit/100*total:,.0f}원)")
            if amount > cash:
                blocks_pre = f"매수금 {amount:,.0f}원이 보유 현금 {cash:,.0f}원을 초과한다"
                print(f"{'⚠':<14}{blocks_pre}")

    flags, blocks = [], []

    # 1. 비중
    if a.side == "buy" and w_after > a.limit:
        over = w_after - a.limit
        (blocks if over > a.limit else flags).append(
            f"매수 후 비중 {w_after:.2f}% — 한도 {a.limit:.0f}% 를 {over:.2f}%p 초과")
    elif a.side == "buy" and w_after > w_before and w_before > a.limit:
        blocks.append(f"이미 한도 초과({w_before:.2f}%) 상태에서 추가 매수")

    # 2. 내 규칙
    if a.buy_below and a.sell_above:
        if a.side == "sell" and a.price < a.sell_above:
            zone = "매수 구간" if a.price <= a.buy_below else "중립 구간"
            blocks.append(
                f"매도가 {a.price:,.0f}원은 내 매도 기준({a.sell_above:,.0f}원) 미만 — {zone}이다")
        if a.side == "buy" and a.price > a.buy_below:
            flags.append(
                f"매수가 {a.price:,.0f}원은 내 매수 기준({a.buy_below:,.0f}원)보다 높다")

    # 3. 과거 같은 가격대 매매의 사후 결과
    tf = TRADES / f"{a.asset_id}.csv"
    past = read_csv(tf)
    if past:
        band = 0.03
        near = [t for t in past
                if (pr := num(t.get("price_krw"))) and abs(pr / a.price - 1) <= band]
        print(f"\n[ 과거 유사 가격대 매매 ] {a.price*(1-band):,.0f}~{a.price*(1+band):,.0f}원")
        if not near:
            print("  이 가격대에서 매매한 기록 없음")
        for t in near:
            pr = num(t["price_krw"])
            diff = (a.price - pr) / pr * 100
            note = f"  {t.get('note','')}" if t.get("note") else ""
            print(f"  {t['date']}  {t['side']:<5}{num(t['quantity']):>7,.0f}주 "
                  f"@ {pr:>9,.0f}원  (현재 대비 {diff:+.1f}%){note}")
            if t["side"].upper() == "SELL" and pr < a.price:
                flags.append(
                    f"{t['date']} 에 {pr:,.0f}원으로 매도했고 지금은 {a.price:,.0f}원이다 "
                    f"— 같은 판단을 반복하는가")

    print("\n" + "-" * 72)
    if blocks:
        print("🔴 규칙 위반")
        for b in blocks:
            print(f"   • {b}")
    if flags:
        print("🟡 확인 필요")
        for f in flags:
            print(f"   • {f}")
    if not blocks and not flags:
        print("🟢 규칙상 걸리는 항목 없음")

    print("\n" + "=" * 72)
    print("[ 반드시 답해야 할 질문 ]")
    print("=" * 72)
    if a.side == "sell":
        print("  이 매도의 이유는 무엇인가?")
        print("    (A) 논지가 훼손됐다 — 무효화 조건이 켜졌거나 사실이 바뀌었다")
        print("    (B) 가격이 올랐다 / 불안하다 / 이익을 확정하고 싶다")
        print()
        print("  ※ 본인 실적: 논지 기반 매도(리커전) +1,377,621원")
        print("               가격 기반 매도(파마리서치) -2,412,500원")
        print("  (B)라면 근거를 다시 확인한다. 무효화 조건 없이 파는 것은 예측이다.")
    else:
        print("  이 매수의 근거 중 검증 가능한 것은 몇 개인가?")
        print("    검증 가능 = 공시·재무·계약처럼 사실 여부를 확인할 수 있는 것")
        print("    검증 불가 = '시장이 아직 몰라준다', '섹터가 좋아질 것 같다'")
        print()
        print("  ※ 본인 실적: 파마리서치 매수 이유 9개 중 검증 가능은 2개였다.")
        print("  ※ 급락 당일 매수는 재검토 후에 한다 (2026-02-05 어닝쇼크일 매수 사례).")
    print("\n이 스크립트는 주가를 예측하지 않는다. 규칙 위반만 잡는다.")


if __name__ == "__main__":
    main()
