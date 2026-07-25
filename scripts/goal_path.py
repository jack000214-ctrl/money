#!/usr/bin/env python3
"""목표 자산까지의 경로를 계산한다.

수익률·저축액·기간 중 무엇이 실제로 목표를 좌우하는지 보기 위한 것이다.
예측이 아니라 산수다. 세금·인플레이션은 반영하지 않으며 명목 금액 기준이다.
"""

import argparse

EOK = 100_000_000  # 1억


def future_value(principal, monthly_saving, annual_rate, years):
    """월 적립을 월복리로 계산한다. 연이율을 12로 나눈 월이율을 쓴다."""
    r = annual_rate / 12
    n = int(round(years * 12))
    if r == 0:
        return principal + monthly_saving * n
    grown = principal * (1 + r) ** n
    contributed = monthly_saving * (((1 + r) ** n - 1) / r)
    return grown + contributed


def years_to_target(principal, monthly_saving, annual_rate, target, cap=60):
    for m in range(1, cap * 12 + 1):
        if future_value(principal, monthly_saving, annual_rate, m / 12) >= target:
            return m / 12
    return None


def required_rate(principal, monthly_saving, target, years):
    lo, hi = -0.5, 1.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if future_value(principal, monthly_saving, mid, years) < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def main():
    p = argparse.ArgumentParser(description="목표 자산 도달 경로 계산")
    p.add_argument("--principal", type=float, required=True, help="현재 자산 (원)")
    p.add_argument("--target", type=float, required=True, help="목표 자산 (원)")
    p.add_argument("--monthly", type=float, nargs="+", required=True,
                   help="월 저축액 시나리오 (원, 여러 개 가능)")
    p.add_argument("--rates", type=float, nargs="+",
                   default=[0.05, 0.07, 0.10, 0.12],
                   help="연 수익률 시나리오 (소수)")
    p.add_argument("--horizons", type=float, nargs="+", default=[10, 15, 20, 25],
                   help="검토 기간 (년)")
    a = p.parse_args()

    print(f"현재 자산 {a.principal/EOK:.2f}억 → 목표 {a.target/EOK:.0f}억")
    print("=" * 72)

    print("\n[ 목표 도달까지 걸리는 기간 (년) ]")
    header = "월 저축".ljust(12) + "".join(f"{r*100:>13.0f}%" for r in a.rates)
    print(header)
    print("-" * len(header))
    for m in a.monthly:
        row = f"{m/10_000:>6.0f}만원".ljust(14)
        for r in a.rates:
            y = years_to_target(a.principal, m, r, a.target)
            row += f"{y:>13.1f}" if y else f"{'60년+':>13}"
        print(row)

    print("\n[ 기간별 필요 연수익률 ]")
    header = "월 저축".ljust(12) + "".join(f"{int(h):>12}년" for h in a.horizons)
    print(header)
    print("-" * len(header))
    for m in a.monthly:
        row = f"{m/10_000:>6.0f}만원".ljust(14)
        for h in a.horizons:
            rr = required_rate(a.principal, m, a.target, h)
            row += f"{rr*100:>12.1f}%" if rr < 0.99 else f"{'불가':>13}"
        print(row)

    print("\n[ 20년 후 자산 (억원) ]")
    header = "월 저축".ljust(12) + "".join(f"{r*100:>13.0f}%" for r in a.rates)
    print(header)
    print("-" * len(header))
    for m in a.monthly:
        row = f"{m/10_000:>6.0f}만원".ljust(14)
        for r in a.rates:
            row += f"{future_value(a.principal, m, r, 20)/EOK:>13.1f}"
        print(row)

    print("\n※ 명목 금액이며 세금과 물가상승을 반영하지 않는다.")
    print("※ 수익률은 가정이지 예측이 아니다. 실제로는 매년 다르고 순서도 결과를 바꾼다.")


if __name__ == "__main__":
    main()
