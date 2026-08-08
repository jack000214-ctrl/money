#!/usr/bin/env python3
"""DART에서 재고자산과 매출원가를 읽어 분기 시계열을 만든다.

재고 잔액, DIO, 전분기·전년동기 증감률을 별도 관측 산출물로 저장한다.
수치는 제공하지만 사이클 전환 여부는 판정하지 않는다.

사용:
    python3.13 -m collectors.kr_inventory 005930 --years 2023 2024 2025 2026
    python3.13 -m collectors.kr_inventory 214450 --years 2024 2025 2026
    python3.13 -m collectors.kr_inventory show 005930
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

from collectors.dart import (
    QUARTER_END, REPORT_SEQUENCE, acct, amount, fetch_all, load_key,
    resolve, restore_is_quarters, save_raw, xbrl_rows,
)

OUT_DIR = ROOT / "knowledge" / "inventory"

# 재고자산은 재무상태표 시점값이다. 현금흐름처럼 누계를 차감 복원할 필요가 없다.
BS_INVENTORY_IDS = ["ifrs-full_Inventories"]
BS_INVENTORY_NAMES = ["재고자산"]
IS_COGS_IDS = ["ifrs-full_CostOfSales"]
IS_COGS_NAMES = ["매출원가"]

DAYS_PER_QUARTER = 91.25
FIELDS = ("period_end", "fy", "quarter", "inventories", "inventories_source",
          "cogs_quarter", "dio_days", "qoq_pct", "yoy_pct", "cogs_derived")


def fail(msg: str) -> None:
    print(f"오류: {msg}", file=sys.stderr)
    raise SystemExit(1)


def collect(stock_code: str, years: list[int], fs: str, key: str) -> tuple[str, list[dict]]:
    info = resolve(stock_code, key)
    corp, name = info["corp_code"], info["corp_name"]

    inventories: dict[tuple[int, int], tuple[float, str]] = {}
    cogs: dict[tuple[int, int], dict] = {}
    raw: dict[str, object] = {}

    for year in years:
        by_report = {}
        for report, quarter in REPORT_SEQUENCE:
            data = fetch_all(corp, year, report, fs, key)
            rows = data["list"] if data else xbrl_rows(corp, year, report, key)
            if not rows:
                continue
            by_report[report] = rows
            raw[f"{year}_{report}"] = rows

            # 재무상태표는 그 보고서 시점의 잔액이다. 보고서 하나 = 분기 하나.
            row = acct(rows, ("BS",), BS_INVENTORY_IDS, BS_INVENTORY_NAMES)
            value = amount(row)
            if value is not None:
                inventories[(year, quarter)] = (float(value), "report")

            # 사업보고서가 013(데이터 없음)이면 그 해 4분기가 통째로 빈다.
            # 그런데 재무상태표는 **비교표시**를 한다 — 다음 해 1분기보고서의
            # frmtrm_amount 가 바로 그 전기말 잔액이다. QoQ 사슬이 끊기는 것을
            # 막으려면 이쪽을 쓰는 편이 낫다. 추정이 아니라 같은 공시의 다른 칸이다.
            if report == "1q":
                prior = amount(row, "frmtrm_amount")
                if prior is not None and (year - 1, 4) not in inventories:
                    inventories[(year - 1, 4)] = (float(prior), "comparative")

        if by_report:
            try:
                for quarter, entry in restore_is_quarters(
                        by_report, IS_COGS_IDS, IS_COGS_NAMES).items():
                    cogs[(year, quarter)] = entry
            except LookupError:
                pass  # 매출원가를 못 찾으면 DIO 만 비운다. 재고 잔액은 살린다

    if not inventories:
        fail(f"{name}: 재고자산 계정을 찾지 못했다. 금융업처럼 재고가 없는 "
             "업종이거나 계정명이 다르다")

    save_raw(f"inventory_{stock_code}", raw)
    return name, derive_rows(inventories, cogs, years)


def derive_rows(
    inventories: dict[tuple[int, int], tuple[float, str]],
    cogs: dict[tuple[int, int], dict],
    years: list[int],
) -> list[dict]:
    """잔액과 매출원가에서 DIO·QoQ·YoY 를 낸다.

    수집과 분리해 둔다 — 이 산술이 판단의 무효화 신호 문턱으로 인용되기
    때문이다("DIO 145일 초과하면 절반 축소"). 네트워크 없이 검증할 수 있어야
    그 문턱이 무엇을 뜻하는지 고정된다.

    QoQ 의 직전 분기는 **1분기면 전년 4분기**다. 같은 해 안에서만 찾으면
    매년 1분기의 QoQ 가 비고, 재고 사이클의 연말·연초 구간이 통째로 사라진다.
    """
    rows = []
    # 비교표시로 채운 앞선 해가 요청 구간 밖일 수 있다. 표에는 요청 구간만 남긴다.
    for (fy, quarter) in sorted(k for k in inventories if k[0] in years):
        value, origin = inventories[(fy, quarter)]
        entry = cogs.get((fy, quarter))
        cogs_q = entry["val"] if entry else None
        prev = inventories.get((fy, quarter - 1) if quarter > 1 else (fy - 1, 4))
        prev = prev[0] if prev else None
        year_ago = inventories.get((fy - 1, quarter))
        year_ago = year_ago[0] if year_ago else None
        rows.append({
            "period_end": f"{fy}-{QUARTER_END[quarter]}",
            "fy": fy, "quarter": quarter,
            "inventories": round(value, 0),
            "inventories_source": origin,
            "cogs_quarter": round(cogs_q, 0) if cogs_q else None,
            # DIO — 분기 매출원가 기준 며칠치 재고인가
            "dio_days": (round(value / cogs_q * DAYS_PER_QUARTER, 1)
                         if cogs_q and cogs_q > 0 else None),
            "qoq_pct": (round((value / prev - 1) * 100, 2)
                        if prev and prev > 0 else None),
            "yoy_pct": (round((value / year_ago - 1) * 100, 2)
                        if year_ago and year_ago > 0 else None),
            # 4분기 매출원가는 연간 − 3분기누계 역산이다. 표시할 때 구분한다
            "cogs_derived": bool(entry and entry.get("derived")),
        })
    return rows


def save(stock_code: str, name: str, rows: list[dict], fs: str) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{stock_code}.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        f.write(f"# {stock_code} {name} 재고자산 시계열 / 단위 원 / {fs}\n")
        f.write(f"# 수집일 {dt.date.today().isoformat()} / 출처 OpenDART "
                "fnlttSinglAcntAll 재무상태표·손익계산서\n")
        f.write("# 재고자산은 분기말 잔액이다. 4분기 매출원가는 연간 − 3분기누계 "
                "역산이므로 cogs_derived 로 표시한다\n")
        w = csv.DictWriter(f, fieldnames=list(FIELDS))
        w.writeheader()
        w.writerows(rows)
    return path


def load(stock_code: str) -> list[dict]:
    path = OUT_DIR / f"{stock_code}.csv"
    if not path.exists():
        fail(f"{path.relative_to(ROOT)} 가 없다. 먼저 수집한다")
    with path.open(newline="", encoding="utf-8") as f:
        lines = [ln for ln in f if not ln.lstrip().startswith("#")]
    return list(csv.DictReader(lines))


def render(stock_code: str, rows: list[dict]) -> None:
    print("=" * 78)
    print(f"재고자산 시계열 — {stock_code}   "
          f"{rows[0]['period_end']} ~ {rows[-1]['period_end']} ({len(rows)}분기)")
    print("=" * 78)
    print(f"{'분기말':<13}{'재고자산':>18}{'QoQ':>9}{'YoY':>9}"
          f"{'분기 매출원가':>18}{'DIO(일)':>10}")
    for r in rows:
        def show(key, fmt, width):
            v = r.get(key)
            return f"{float(v):{fmt}}" if v not in (None, "", "None") else f"{'-':>{width}}"
        mark = "*" if str(r.get("cogs_derived")).lower() == "true" else " "
        cmp_mark = "†" if r.get("inventories_source") == "comparative" else " "
        print(f"{r['period_end']:<13}{show('inventories', '>17,.0f', 17)}{cmp_mark}"
              f"{show('qoq_pct', '>+8.1f', 9)}{'%' if r.get('qoq_pct') not in (None,'','None') else ''}"
              f"{show('yoy_pct', '>+8.1f', 9)}{'%' if r.get('yoy_pct') not in (None,'','None') else ''}"
              f"{show('cogs_quarter', '>17,.0f', 18)}{mark}"
              f"{show('dio_days', '>10.1f', 10)}")
    print("-" * 78)
    print("* = 4분기 매출원가는 연간 − 3분기누계 역산값")
    print("† = 사업보고서가 비어 있어 다음 해 1분기보고서의 전기말 비교값을 썼다")
    print("\n※ 재고 금액과 DIO 가 **함께** 오르는 것이 사이클 신호다.")
    print("  하나만 오르면 매출 성장이거나 계절성이다. 이 표는 그 판단을 하지 않는다.")


def main():
    p = argparse.ArgumentParser(description="재고자산 시계열 수집 (OpenDART)")
    p.add_argument("stock_code", help="005930 / 214450")
    p.add_argument("--years", type=int, nargs="+",
                   help="수집 연도. 기본은 최근 4년")
    p.add_argument("--fs", default="CFS", choices=["CFS", "OFS"],
                   help="CFS 연결 / OFS 별도")
    p.add_argument("--show", action="store_true",
                   help="수집하지 않고 저장된 시계열만 표시")
    a = p.parse_args()

    if a.show:
        render(a.stock_code, load(a.stock_code))
        return

    years = a.years or list(range(dt.date.today().year - 3, dt.date.today().year + 1))
    name, rows = collect(a.stock_code, years, a.fs, load_key())
    path = save(a.stock_code, name, rows, a.fs)
    render(a.stock_code, rows)
    print(f"\n저장: {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
