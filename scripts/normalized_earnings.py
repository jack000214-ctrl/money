#!/usr/bin/env python3
"""정상화 이익 — 사이클 종목의 DCF 가 반쪽만 되는 문제.

삼성전자 TTM FCF 는 2024-01 에 **−16.6조**였고 2026-03 에 **+56.5조**다.
같은 회사, 같은 자산, 2년 차이. 어느 쪽을 DCF 에 넣느냐로 내재가치가 몇 배
달라진다. **peak FCF 에 성장률을 걸면 고평가가 나오고, trough FCF 에 걸면
음수 가치가 나온다.** 둘 다 틀렸다. 메모리·조선·화학처럼 사이클이 지배하는
업종에서 "현재 TTM" 은 밸류에이션의 출발점이 될 수 없다.

정상화 이익은 그 자리를 **사이클 한 바퀴의 평균**으로 바꾼다.

## 세 가지 기준을 함께 낸다 — 하나만 내면 그게 정답처럼 보인다

1. **전기간 평균 TTM** — 사이클 전체를 몇 바퀴 도는 창의 산술평균
2. **중앙값 TTM** — 한두 분기의 극단값에 덜 흔들린다
3. **정상화 마진 × 현재 매출** — 회사는 커졌는데 마진만 사이클을 타는 경우.
   과거 평균 FCF 마진을 지금 매출에 곱한다

셋이 크게 다르면 그 자체가 정보다. 회사가 구조적으로 변했다는 뜻이거나,
창이 사이클 한 바퀴를 못 담았다는 뜻이다. **이 스크립트는 어느 것을 쓸지
고르지 않는다.**

## 이것은 밸류에이션 입력이 아니다

`knowledge/fundamentals/*.json` 스냅샷은 **분기 원문에서 나온 실측값**이고
`fundamentals_id` 로 지문이 찍힌다. 정상화 이익은 그 실측값들의 **가공**이므로
스냅샷에 섞으면 "원문에서 온 숫자"라는 보증이 깨진다.

그래서 별도 산출물로 낸다. 이 값을 DCF 에 쓰려면 사용자가 명시적으로 골라야
하고, 그 선택이 분석에 근거로 남아야 한다. **자동으로 밸류에이션을 바꾸지 않는다.**

## 사이클 판정도 하지 않는다

"이 회사가 사이클 종목인가"는 이 스크립트가 답할 수 없다. 대신 **진폭**을 낸다 —
최고 TTM ÷ 최저 TTM. 이 배수가 크면 정상화가 필요하고 작으면 현재 TTM 을
그냥 쓰면 된다. 판단은 그 숫자를 보고 모델과 사용자가 한다.

사용:
    python3 scripts/normalized_earnings.py 005930 --years 2018 2019 2020 2021 2022 2023 2024 2025 2026
    python3 scripts/normalized_earnings.py 214450 --years 2021 2022 2023 2024 2025 2026
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics as st
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from contracts import stable_hash  # noqa: E402
from dart_fetch import (  # noqa: E402
    CF_CAPEX_IDS, CF_OCF_IDS, IS_REVENUE_IDS, QUARTER_END, REPORT_SEQUENCE,
    fetch_all, load_key, resolve, restore_cf_quarters, restore_is_quarters,
    save_raw, xbrl_rows,
)

OUT_DIR = ROOT / "knowledge" / "normalized"
SCHEMA = "normalized-earnings/1.0.0"
TTM_QUARTERS = 4
# 진폭이 이 배수를 넘으면 현재 TTM 을 그대로 쓰는 것이 위험하다는 표시.
# 판정이 아니라 눈금이다 — 값 자체를 함께 출력한다.
CYCLICAL_HINT_RATIO = 3.0


def fail(msg: str) -> None:
    print(f"오류: {msg}", file=sys.stderr)
    raise SystemExit(1)


def quarterly_series(stock_code: str, years: list[int], fs: str, key: str):
    """분기별 영업현금흐름·capex·매출. 전부 DART 원문에서 복원한다."""
    info = resolve(stock_code, key)
    corp, name = info["corp_code"], info["corp_name"]

    ocf: dict[tuple[int, int], float] = {}
    capex: dict[tuple[int, int], float] = {}
    revenue: dict[tuple[int, int], float] = {}
    raw: dict[str, object] = {}

    for year in years:
        by_report = {}
        for report, _ in REPORT_SEQUENCE:
            data = fetch_all(corp, year, report, fs, key)
            rows = data["list"] if data else xbrl_rows(corp, year, report, key)
            if rows:
                by_report[report] = rows
                raw[f"{year}_{report}"] = rows
        if not by_report:
            continue
        try:
            for q, entry in restore_cf_quarters(by_report, CF_OCF_IDS, "영업현금흐름").items():
                ocf[(year, q)] = entry["val"]
            for q, entry in restore_cf_quarters(by_report, CF_CAPEX_IDS, "capex").items():
                capex[(year, q)] = entry["val"]
        except LookupError:
            pass
        try:
            for q, entry in restore_is_quarters(by_report, IS_REVENUE_IDS, ["매출액"]).items():
                revenue[(year, q)] = entry["val"]
        except LookupError:
            pass

    save_raw(f"normalized_{stock_code}", raw)
    return name, ocf, capex, revenue


def ttm_series(ocf, capex, revenue) -> list[dict]:
    """연속 4분기가 확보된 구간만 TTM 을 만든다. 빈 분기는 건너뛰지 않고 버린다."""
    out = []
    for key in sorted(set(ocf) & set(capex)):
        fy, q = key
        window = []
        cursor = fy * 4 + q
        for back in range(TTM_QUARTERS):
            idx = cursor - back
            window.append(((idx - 1) // 4, (idx - 1) % 4 + 1))
        if not all(k in ocf and k in capex for k in window):
            continue
        ocf_ttm = sum(ocf[k] for k in window)
        capex_ttm = sum(capex[k] for k in window)
        rev_ttm = (sum(revenue[k] for k in window)
                   if all(k in revenue for k in window) else None)
        out.append({
            "period_end": f"{fy}-{QUARTER_END[q]}",
            "ocf_ttm": ocf_ttm,
            "capex_ttm": capex_ttm,
            "fcf_ttm": ocf_ttm - capex_ttm,
            "revenue_ttm": rev_ttm,
            "fcf_margin_pct": (round((ocf_ttm - capex_ttm) / rev_ttm * 100, 2)
                               if rev_ttm else None),
        })
    return out


def normalize(rows: list[dict]) -> dict:
    fcf = [r["fcf_ttm"] for r in rows]
    peak_row = max(rows, key=lambda r: r["fcf_ttm"])
    trough_row = min(rows, key=lambda r: r["fcf_ttm"])
    current = rows[-1]

    # 진폭. 저점이 음수면 배수가 의미를 잃으므로 그 사실을 그대로 말한다.
    if trough_row["fcf_ttm"] > 0:
        amplitude = round(peak_row["fcf_ttm"] / trough_row["fcf_ttm"], 2)
        amplitude_note = "최고 TTM ÷ 최저 TTM"
    else:
        amplitude = None
        amplitude_note = ("최저 TTM 이 0 이하라 배수가 성립하지 않는다. "
                          "사이클의 저점에서 FCF 가 음수인 회사다 — "
                          "정상화 없이는 DCF 가 음수 가치를 낸다")

    margins = [r["fcf_margin_pct"] for r in rows if r["fcf_margin_pct"] is not None]
    margin_based = None
    if margins and current.get("revenue_ttm"):
        margin_based = current["revenue_ttm"] * st.mean(margins) / 100.0

    return {
        "window": {"from": rows[0]["period_end"], "to": rows[-1]["period_end"],
                   "ttm_points": len(rows)},
        "current_fcf_ttm": current["fcf_ttm"],
        "peak": {"period_end": peak_row["period_end"], "fcf_ttm": peak_row["fcf_ttm"]},
        "trough": {"period_end": trough_row["period_end"],
                   "fcf_ttm": trough_row["fcf_ttm"]},
        "amplitude_ratio": amplitude,
        "amplitude_note": amplitude_note,
        "bases": {
            "mean_ttm": st.mean(fcf),
            "median_ttm": st.median(fcf),
            "mean_margin_on_current_revenue": margin_based,
        },
        "mean_fcf_margin_pct": round(st.mean(margins), 2) if margins else None,
        "current_vs_mean_x": (round(current["fcf_ttm"] / st.mean(fcf), 2)
                              if st.mean(fcf) > 0 else None),
    }


def scale_for(rows: list[dict]) -> tuple[float, str]:
    """표시 단위를 크기에 맞춘다.

    조원 고정이면 파마리서치처럼 FCF 가 수천억인 회사는 전 구간이 '0.1조' 로
    뭉개져 표가 아무 정보를 주지 않는다. 사이클 진폭을 보자고 만든 표에서
    그건 치명적이다.
    """
    biggest = max(abs(r["fcf_ttm"]) for r in rows) if rows else 0.0
    if biggest >= 1e13:
        return 1e12, "조"
    if biggest >= 1e10:
        return 1e8, "억"
    return 1e6, "백만"


def render(name: str, code: str, rows: list[dict], norm: dict) -> None:
    unit, unit_name = scale_for(rows)
    print("=" * 76)
    print(f"정상화 이익 — {code} {name}   "
          f"{norm['window']['from']} ~ {norm['window']['to']} "
          f"(TTM {norm['window']['ttm_points']}개 구간)")
    print("=" * 76)
    print(f"{'분기말':<13}{f'TTM 영업CF({unit_name})':>17}{f'capex({unit_name})':>14}"
          f"{f'TTM FCF({unit_name})':>16}{'FCF 마진':>10}")
    for r in rows:
        margin = (f"{r['fcf_margin_pct']:>9.1f}%" if r["fcf_margin_pct"] is not None
                  else f"{'-':>10}")
        print(f"{r['period_end']:<13}{r['ocf_ttm']/unit:>17,.1f}"
              f"{r['capex_ttm']/unit:>14,.1f}{r['fcf_ttm']/unit:>16,.1f}{margin}")

    print("-" * 76)
    print(f"  최고 TTM FCF   {norm['peak']['fcf_ttm']/unit:>12,.1f}{unit_name}   "
          f"{norm['peak']['period_end']}")
    print(f"  최저 TTM FCF   {norm['trough']['fcf_ttm']/unit:>12,.1f}{unit_name}   "
          f"{norm['trough']['period_end']}")
    print(f"  현재 TTM FCF   {norm['current_fcf_ttm']/unit:>12,.1f}{unit_name}")
    if norm["amplitude_ratio"]:
        print(f"  진폭           {norm['amplitude_ratio']:>10,.2f}배   "
              f"({norm['amplitude_note']})")
    else:
        print(f"  진폭           {'—':>10}     {norm['amplitude_note']}")

    print(f"\n[ 정상화 기준 — 셋 중 하나를 고르는 것은 이 스크립트의 일이 아니다 ]")
    bases = norm["bases"]
    print(f"  전기간 평균 TTM              {bases['mean_ttm']/unit:>12,.1f}{unit_name}")
    print(f"  전기간 중앙값 TTM            {bases['median_ttm']/unit:>12,.1f}{unit_name}")
    if bases["mean_margin_on_current_revenue"] is not None:
        print(f"  평균 FCF 마진({norm['mean_fcf_margin_pct']:.1f}%) × 현재 매출  "
              f"{bases['mean_margin_on_current_revenue']/unit:>12,.1f}{unit_name}")
    else:
        print("  평균 FCF 마진 × 현재 매출     —  (매출 시계열이 부족하다)")

    if norm["current_vs_mean_x"]:
        print(f"\n  현재 TTM 은 전기간 평균의 {norm['current_vs_mean_x']:.2f}배다.")
    if norm["amplitude_ratio"] and norm["amplitude_ratio"] >= CYCLICAL_HINT_RATIO:
        print(f"  진폭이 {CYCLICAL_HINT_RATIO}배를 넘는다 — 현재 TTM 을 그대로 "
              "DCF 에 넣으면 사이클 위치가 결론을 지배한다.")
    elif norm["amplitude_ratio"] is None:
        print("  저점 FCF 가 음수다 — 현재 TTM 을 그대로 쓰는 것은 특히 위험하다.")

    print("\n※ 이 값은 밸류에이션 입력이 아니다. fundamentals 스냅샷의 실측값과")
    print("  달리 가공된 값이므로, DCF 에 쓰려면 사용자가 명시적으로 고르고")
    print("  그 선택이 분석에 근거로 남아야 한다.")


def main():
    p = argparse.ArgumentParser(description="사이클 종목의 정상화 이익 (OpenDART)")
    p.add_argument("stock_code")
    p.add_argument("--years", type=int, nargs="+", required=True,
                   help="사이클 한 바퀴 이상을 담는 연도 목록")
    p.add_argument("--fs", default="CFS", choices=["CFS", "OFS"])
    a = p.parse_args()

    key = load_key()
    name, ocf, capex, revenue = quarterly_series(a.stock_code, a.years, a.fs, key)
    rows = ttm_series(ocf, capex, revenue)
    if len(rows) < 2:
        fail(f"{name}: 연속 4분기 TTM 을 2개 이상 만들지 못했다 "
             f"(분기 확보 {len(ocf)}개). 연도를 넓히거나 --fs 를 바꾼다")

    norm = normalize(rows)
    render(name, a.stock_code, rows, norm)

    payload = {
        "schema_version": SCHEMA,
        "instrument_id": a.stock_code,
        "name": name,
        "fs": a.fs,
        "created_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": "OpenDART fnlttSinglAcntAll 현금흐름표·손익계산서 (분기 복원)",
        "grade": "DERIVED",
        "_not_a_valuation_input": (
            "fundamentals 스냅샷과 달리 이 값은 실측이 아니라 가공이다. "
            "DCF 에 쓰려면 사용자가 명시적으로 고르고 그 선택이 분석에 남아야 한다"),
        "ttm_series": rows,
        "normalization": norm,
    }
    payload["normalized_id"] = stable_hash(payload, "NORM-")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{a.stock_code}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    print(f"\n작성: {path.relative_to(ROOT)}  ({payload['normalized_id']})")


if __name__ == "__main__":
    main()
