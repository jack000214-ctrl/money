#!/usr/bin/env python3
"""미국 섹터 ETF 11종의 SPY 대비 상대강도를 계산한다.

1·3·6·12개월 수익률과 초과수익 추세를 관측값으로 제공하며 자본 배분을
결정하지 않는다. 미국 ETF는 한국 종목의 섹터 대리가 아니고, 상대강도는
밸류에이션 지표가 아니다.

사용:
    python3.13 -m collectors.price history XLV --range 2y
    python3.13 -m calculators.sector_strength
    python3.13 -m calculators.sector_strength --highlight 헬스케어
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from cio.display import cell  # noqa: E402

PRICE_DIR = ROOT / "knowledge" / "prices"
OUT = ROOT / "knowledge" / "market" / "sector-strength.json"
SCHEMA = "sector-strength/1.0.0"

BENCHMARK = ("SPY", "S&P 500")
# SPDR Select Sector — 미국 섹터 자금 흐름의 표준 대리지표
SECTORS = [
    ("XLK", "IT"), ("XLV", "헬스케어"), ("XLF", "금융"),
    ("XLY", "경기소비재"), ("XLC", "통신서비스"), ("XLI", "산업재"),
    ("XLP", "필수소비재"), ("XLE", "에너지"), ("XLU", "유틸리티"),
    ("XLRE", "리츠"), ("XLB", "소재"),
]
WINDOWS = [("1개월", 21), ("3개월", 63), ("6개월", 126), ("12개월", 252)]


def fail(msg: str) -> None:
    print(f"오류: {msg}", file=sys.stderr)
    raise SystemExit(1)


def closes(symbol: str) -> list[tuple[str, float]]:
    path = PRICE_DIR / f"{symbol}.csv"
    if not path.exists():
        fail(f"{path.relative_to(ROOT)} 가 없다. "
             f"먼저 python3.13 -m collectors.price history {symbol} --range 2y")
    with path.open(encoding="utf-8") as f:
        lines = [ln for ln in f if not ln.lstrip().startswith("#")]
    return [(r["date"], float(r["close"]))
            for r in csv.DictReader(lines) if r.get("close")]


def window_return(series: list[tuple[str, float]], days: int) -> float | None:
    """최근 N거래일 수익률. 데이터가 모자라면 지어내지 않고 None."""
    if len(series) < days + 1:
        return None
    return (series[-1][1] / series[-1 - days][1] - 1) * 100


def main():
    p = argparse.ArgumentParser(description="섹터 상대강도 (미국 섹터 ETF 대비 SPY)")
    p.add_argument("--highlight", help="강조할 섹터명 (예: 헬스케어)")
    p.add_argument("--json", action="store_true")
    a = p.parse_args()

    bench = closes(BENCHMARK[0])
    rows = []
    for symbol, label in SECTORS:
        series = closes(symbol)
        entry = {"symbol": symbol, "sector": label, "windows": {}}
        for name, days in WINDOWS:
            r_sec = window_return(series, days)
            r_ben = window_return(bench, days)
            entry["windows"][name] = {
                "return_pct": round(r_sec, 2) if r_sec is not None else None,
                "excess_pct": (round(r_sec - r_ben, 2)
                               if r_sec is not None and r_ben is not None else None),
            }
        rows.append(entry)

    # 12개월 초과수익 기준 정렬 — 자금이 어디로 갔는지가 이 표의 요점이다
    rows.sort(key=lambda e: (e["windows"]["12개월"]["excess_pct"] is not None,
                             e["windows"]["12개월"]["excess_pct"] or -999),
              reverse=True)

    # 벤치마크 이력이 짧아 구간 수익률을 못 내면 비운다. 예전에는 0.0 이
    # 들어가 "SPY 가 12개월간 0% 움직였다"가 knowledge/market/ 에 저장됐고,
    # 그 표를 읽는 것은 모델이다.
    bench_row = {
        name: round(value, 2) if (value := window_return(bench, days)) is not None
        else None
        for name, days in WINDOWS
    }

    payload = {
        "schema_version": SCHEMA,
        "as_of": bench[-1][0],
        "built_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "benchmark": {"symbol": BENCHMARK[0], "name": BENCHMARK[1],
                      "returns_pct": bench_row},
        "source": "Yahoo Finance chart API · SPDR Select Sector ETF",
        "grade": "DERIVED",
        "_not_a_recommendation": (
            "상대강도는 추세지 밸류에이션이 아니다. 약한 섹터가 싸서 오를 수도 "
            "있다. 이 표만으로 매매를 정하면 모멘텀 추종이 된다"),
        "_scope_warning": (
            "미국 섹터 ETF 는 한국 종목의 섹터 대리가 아니다. 파마리서치(국내 "
            "에스테틱)를 XLV(미국 대형 제약·보험)로 재는 것은 근사조차 아니다"),
        "sectors": rows,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")

    if a.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    print("=" * 74)
    print(f"섹터 상대강도 — SPY 대비 초과수익 (기준일 {payload['as_of']})")
    print("=" * 74)
    print(f"{'섹터':<12}{'심볼':<7}" + "".join(f"{n:>14}" for n, _ in WINDOWS))
    print(f"{'':19}" + "".join(f"{'수익 / 초과':>14}" for _ in WINDOWS))
    print("-" * 74)
    for e in rows:
        mark = "▶" if a.highlight and e["sector"] == a.highlight else " "
        line = f"{mark}{e['sector']:<11}{e['symbol']:<7}"
        for name, _ in WINDOWS:
            w = e["windows"][name]
            # 수익률은 있는데 초과수익만 없는 경우가 있다(벤치마크 이력이 그
            # 구간을 못 덮을 때). 예전에는 그 자리에서 f"{None:>+6.1f}" 로
            # 표 전체가 죽었다 — cio/display.cell 이 있는 이유다.
            line += cell(w["return_pct"], 7, "+.1f") + "/" + cell(
                w["excess_pct"], 6, "+.1f"
            )
        print(line)
    print("-" * 74)
    line = f" {'SPY (시장)':<18}"
    for name, _ in WINDOWS:
        line += cell(bench_row[name], 7, "+.1f") + "/" + f"{'—':>6}"
    print(line)

    if a.highlight:
        target = next((e for e in rows if e["sector"] == a.highlight), None)
        if target:
            rank = rows.index(target) + 1
            print(f"\n[ {a.highlight} ] 12개월 초과수익 기준 {rank}위 / {len(rows)}개 섹터")
            for name, _ in WINDOWS:
                w = target["windows"][name]
                if w["excess_pct"] is None:
                    continue
                verdict = "시장 상회" if w["excess_pct"] > 0 else "시장 하회"
                print(f"  {name:<6} {w['return_pct']:>+7.1f}%   "
                      f"초과 {w['excess_pct']:>+7.1f}%p   {verdict}")

    print(f"\n※ {payload['_scope_warning']}")
    print(f"※ {payload['_not_a_recommendation']}")
    print(f"\n작성: {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
