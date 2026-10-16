#!/usr/bin/env python3
"""사이클 종목의 분기 실측값으로 정상화 FCF 후보를 계산한다.

전기간 평균 TTM, 중앙값 TTM, 평균 FCF 마진을 현재 매출에 적용한 값을 함께
제공하며 어느 값을 밸류에이션에 쓸지는 결정하지 않는다. 산출물은 원문
fundamentals와 분리해 저장한다.

사용:
    python -m calculators.normalized_earnings 005930 --years 2018 2019 2020 2021 2022 2023 2024 2025 2026
    python -m calculators.normalized_earnings 214450 --years 2021 2022 2023 2024 2025 2026
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics as st
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

from calculators.contracts import stable_hash
from collectors.dart import (
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


def normalized_id_for(payload: dict) -> str:
    """수집 시각처럼 내용과 무관한 메타데이터를 제외한 안정 ID."""
    identity = {
        key: value
        for key, value in payload.items()
        if key not in {"created_at", "normalized_id"}
    }
    return stable_hash(identity, "NORM-")


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


def quarterly_series_us(ticker: str, years: list[int]):
    """미국 종목의 같은 시계열. EDGAR companyfacts 에서 복원한다.

    사이클 위치는 시장의 성질이 아니라 회사의 성질이다. 그런데 이 계산기가
    DART 전용이라 미국 종목은 사이클을 볼 수단 없이 단일 TTM 으로만 판단됐다 —
    직전 세션에서 결론을 뒤집은 입력이 정확히 이것이었는데 미국 종목에는
    아예 없었다.

    분기 복원은 EDGAR 수집기의 `fiscal_quarters` 를 그대로 쓴다. 같은 값을
    두 곳에서 만들면 어느 쪽이 맞는지 판단해야 하고 그건 이 계산기의 일이 아니다.
    """
    from collectors.edgar import (
        CAPEX_TAGS, OCF_TAGS, REVENUE_TAGS, concept, fiscal_quarters, resolve,
        save_raw as save_edgar_raw, user_agent,
    )

    ua = user_agent()
    info = resolve(ticker.upper(), ua)
    cik = info["cik"]
    wanted = set(years)
    series: dict[str, dict[tuple[int, int], float]] = {}
    ends: dict[tuple[int, int], str] = {}
    raw: dict[str, object] = {}
    for label, tags in (
        ("ocf", OCF_TAGS), ("capex", CAPEX_TAGS), ("revenue", REVENUE_TAGS)
    ):
        tag, points = concept(cik, tags, ua)
        raw[f"{label}_{tag}"] = points
        rows = {}
        for row in fiscal_quarters(points):
            if row["fy"] not in wanted:
                continue
            rows[(row["fy"], row["quarter"])] = row["val"]
            ends[(row["fy"], row["quarter"])] = row["end"]
        series[label] = rows
    save_edgar_raw(f"normalized_{ticker.upper()}", raw)
    return info["name"], series["ocf"], series["capex"], series["revenue"], ends


def ttm_series(ocf, capex, revenue, ends=None) -> list[dict]:
    """연속 4분기가 확보된 구간만 TTM 을 만든다. 빈 분기는 건너뛰지 않고 버린다."""
    out = []
    ends = ends or {}
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
            # 결산월이 12월이 아닌 회사가 있어 분기 번호를 달력으로 되돌리지
            # 않는다. 수집기가 준 실제 종료일이 있으면 그것을 쓴다.
            "period_end": ends.get(key) or f"{fy}-{QUARTER_END[q]}",
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


def filing_source_for(asset_id: str) -> str:
    """이 종목의 원문이 어디서 오는가. registry 가 답을 갖고 있다."""
    try:
        from cio.assets import get_asset

        return get_asset(asset_id)["filing_source"]
    except Exception:  # noqa: BLE001 - 등록되지 않은 코드도 받는다
        return "DART" if asset_id.isdigit() else "SEC_EDGAR"


def main():
    p = argparse.ArgumentParser(
        description="사이클 종목의 정상화 이익 (KR=OpenDART / US=SEC EDGAR)"
    )
    p.add_argument("stock_code")
    p.add_argument("--years", type=int, nargs="+", required=True,
                   help="사이클 한 바퀴 이상을 담는 연도 목록")
    p.add_argument("--fs", default="CFS", choices=["CFS", "OFS"])
    a = p.parse_args()

    filing_source = filing_source_for(a.stock_code)
    ends: dict = {}
    if filing_source == "SEC_EDGAR":
        name, ocf, capex, revenue, ends = quarterly_series_us(
            a.stock_code, a.years
        )
        source_note = (
            "SEC EDGAR companyfacts 현금흐름표·손익계산서 (분기 복원)"
        )
    else:
        key = load_key()
        name, ocf, capex, revenue = quarterly_series(
            a.stock_code, a.years, a.fs, key
        )
        source_note = "OpenDART fnlttSinglAcntAll 현금흐름표·손익계산서 (분기 복원)"
    rows = ttm_series(ocf, capex, revenue, ends)
    if len(rows) < 2:
        fail(f"{name}: 연속 4분기 TTM 을 2개 이상 만들지 못했다 "
             f"(분기 확보 {len(ocf)}개). 연도를 넓히거나 --fs 를 바꾼다")

    norm = normalize(rows)
    render(name, a.stock_code, rows, norm)

    payload = {
        "schema_version": SCHEMA,
        "instrument_id": a.stock_code,
        "name": name,
        "fs": a.fs if filing_source == "DART" else "US_GAAP_CONSOLIDATED",
        "created_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": source_note,
        "grade": "DERIVED",
        "_not_a_valuation_input": (
            "fundamentals 스냅샷과 달리 이 값은 실측이 아니라 가공이다. "
            "DCF 에 쓰려면 사용자가 명시적으로 고르고 그 선택이 분석에 남아야 한다"),
        "ttm_series": rows,
        "normalization": norm,
    }
    payload["normalized_id"] = normalized_id_for(payload)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{a.stock_code}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    print(f"\n작성: {path.relative_to(ROOT)}  ({payload['normalized_id']})")


if __name__ == "__main__":
    main()
