#!/usr/bin/env python3
"""단일 종목 비중 한도를 목표에서 역산한다.

"10%가 관행"은 근거가 아니다. 한도는 이렇게 정해야 한다.

    이 종목이 반토막(또는 -80%) 났을 때
    목표 도달이 몇 년 늦어지는가.
    내가 그 지연을 받아들일 수 있는가.

관행을 인용하는 대신 사용자의 목표·저축·수익률에서 직접 도출한다.
"""

import argparse

from goal_path import future_value, years_to_target  # 같은 폴더

EOK = 100_000_000


def main():
    p = argparse.ArgumentParser(description="목표 기반 단일 종목 한도 역산")
    p.add_argument("--assets", type=float, required=True, help="총자산 (원)")
    p.add_argument("--target", type=float, required=True, help="목표 자산 (원)")
    p.add_argument("--monthly", type=float, required=True, help="월 투자액 (원)")
    p.add_argument("--rate", type=float, required=True, help="가정 연수익률 (소수)")
    p.add_argument("--drawdowns", type=float, nargs="+", default=[0.5, 0.8],
                   help="종목 하락 시나리오 (소수)")
    p.add_argument("--weights", type=float, nargs="+",
                   default=[5, 10, 15, 20, 25, 30, 35],
                   help="검토할 비중 (%%)")
    p.add_argument("--tolerable-delay", type=float, default=2.0,
                   help="감내 가능한 목표 지연 (년)")
    a = p.parse_args()

    base_years = years_to_target(a.assets, a.monthly, a.rate, a.target)
    if base_years is None:
        raise SystemExit("현재 조건으로는 60년 내 목표에 도달하지 않는다. 가정을 조정한다.")

    print("=" * 74)
    print("단일 종목 한도 — 목표에서 역산")
    print("=" * 74)
    print(f"총자산 {a.assets/EOK:.2f}억  목표 {a.target/EOK:.0f}억  "
          f"월 {a.monthly/10_000:,.0f}만원  연 {a.rate*100:.1f}%")
    print(f"손실 없을 때 도달 기간: **{base_years:.1f}년**")
    print(f"감내 기준: 목표 지연 {a.tolerable_delay:.1f}년 이내")

    for dd in a.drawdowns:
        print(f"\n[ 해당 종목이 {dd*100:.0f}% 하락하는 경우 ]")
        print(f"{'비중':>6}{'손실액':>14}{'남는 자산':>13}{'도달기간':>10}{'지연':>9}   판정")
        print("-" * 74)
        recommended = None
        for w in a.weights:
            loss = a.assets * (w / 100) * dd
            after = a.assets - loss
            yrs = years_to_target(after, a.monthly, a.rate, a.target)
            if yrs is None:
                print(f"{w:>5.0f}%{loss:>14,.0f}{after/EOK:>12.2f}억{'60년+':>10}"
                      f"{'—':>9}   ❌ 도달 불가")
                continue
            delay = yrs - base_years
            ok = delay <= a.tolerable_delay
            if ok:
                recommended = w
            mark = "✅" if ok else "❌"
            print(f"{w:>5.0f}%{loss:>14,.0f}{after/EOK:>12.2f}억{yrs:>9.1f}년"
                  f"{delay:>+8.1f}년   {mark}")
        if recommended:
            print(f"→ {dd*100:.0f}% 하락 기준 한도: **{recommended:.0f}%**")

    print("\n" + "=" * 74)
    print("읽는 법")
    print("=" * 74)
    print("• 이 표는 '얼마나 잃느냐'가 아니라 '몇 년이 늦어지느냐'로 위험을 잰다.")
    print("• 목표가 기간으로 정의돼 있으므로 손실의 진짜 비용도 기간이다.")
    print("• 확신이 크면 한도를 올릴 수 있다. 단 그때 늘어나는 것은")
    print("  기대수익이 아니라 **틀렸을 때의 지연**이다.")
    print("• 이 계산은 해당 종목만 하락하고 나머지는 가정 수익률을 낸다고 본다.")
    print("  실제 폭락장에서는 함께 빠지므로 지연은 더 커진다.")


if __name__ == "__main__":
    main()
