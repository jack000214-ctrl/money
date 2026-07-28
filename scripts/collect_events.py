#!/usr/bin/env python3
"""급변일과 그 무렵 공시를 나란히 놓는다 — 사건 패킷의 수집기.

오늘(2026-07-28) 삼성전자를 볼 때 내가 손으로 한 일이다. 07-24 −7.59% 옆에
자사주 해명공시를 놓고, 07-16 −8.77% 옆에 CXMT 공모 소식을 놓았다. 그 대조가
"단일전자 논문 때문에 폭락한다"는 가설을 깨뜨린 결정적 재료였다.

**인과는 붙이지 않는다.** 프로그램은 시간적 근접성만 제공한다. 원인을 프로그램이
단정하면 그게 곧 결론 유도이고, 이 프로젝트는 모델에게 결론을 주지 않는다.
같은 날 공시가 세 건이면 세 건을 다 놓고 모델이 고르게 한다.

공시 목록에서 정기보고서와 임원·주요주주 소유상황보고서는 제외한다 — 전자는
예정된 일정이고 후자는 하루에 수십 건씩 나와서 신호를 덮는다.

**급변 판정은 두 축이다.**

1. **적응형 임계값** — 고정 ±5% 를 쓰면 변동성이 큰 국면에서 거의 매일이 "사건"이
   된다 (삼성전자 2026년 6~7월은 60일에 21건이었다). 종목 자신의 최근
   표준편차 배수로 잡아야 국면이 바뀌어도 같은 뜻을 유지한다.
2. **지수 대비 초과변동** — "시장이 빠져서 빠진 것"과 "이 종목 이유로 빠진 것"은
   원인이 전혀 다르다. 지수와 같이 움직인 날을 종목 고유 사건으로 넘기면
   모델이 엉뚱한 곳을 파게 된다. 초과변동이 사건성의 핵심 신호다.

사용:
    python3 scripts/collect_events.py 005930 --symbol 005930.KS
    python3 scripts/collect_events.py 005930 --symbol 005930.KS --threshold 3
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dart_fetch import load_key, request, resolve  # noqa: E402

OUT_DIR = ROOT / "knowledge" / "events"
SCHEMA = "event-packet/1.0.0"

# 신호를 덮는 공시. 임원 소유상황은 하루 수십 건, 정기보고서는 예정된 일정이다.
NOISE_PREFIX = ("임원ㆍ주요주주특정증권등소유상황보고서",)
SCHEDULED_PREFIX = ("사업보고서", "분기보고서", "반기보고서")


def safe_name(symbol: str) -> str:
    # 지수(^KS11)·환율(KRW=X)의 특수문자를 뗀다. 파일명에 두면 셸 글롭·
    # 리다이렉션 사고가 난다.
    return symbol.replace(".", "_").replace("^", "").replace("=", "")


BENCHMARKS = {"KS": "^KS11", "KQ": "^KQ11", "US": "^IXIC"}


def benchmark_for(symbol: str) -> str:
    suffix = symbol.rsplit(".", 1)[-1].upper() if "." in symbol else "US"
    return BENCHMARKS.get(suffix, BENCHMARKS["US"])


def daily_returns(series: list[tuple[str, float, int]]) -> dict[str, float]:
    out = {}
    for i in range(1, len(series)):
        out[series[i][0]] = (series[i][1] / series[i - 1][1] - 1) * 100
    return out


def stdev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return (sum((v - mean) ** 2 for v in values) / (len(values) - 1)) ** 0.5


def decompose(series: list[tuple[str, float, int]],
              bench: list[tuple[str, float, int]] | None) -> dict:
    """구간 수익률을 시장과 종목 고유로 분해한다 — 상시 필드.

    이게 없으면 시장이 33% 빠져서 빠진 것을 종목 고유 원인으로 착각한다.
    실제로 그런 일이 있었다: 삼성전자 2026-06-18~07-28 −38.1% 중 KOSPI 가
    −33.4% 였고 종목 초과는 −7.1% 뿐이었는데, CXMT·자사주를 종목 고유
    드라이버로 설명했다. 분해를 항상 눈에 보이게 두면 그 착각을 막는다.
    """
    if not bench:
        return {"available": False,
                "_note": "벤치마크 시세가 없어 분해할 수 없다"}
    bench_at = {d: c for d, c, _ in bench}
    aligned = [(d, c) for d, c, _ in series if d in bench_at]
    if len(aligned) < 2:
        return {"available": False, "_note": "겹치는 거래일이 부족하다"}

    def leg(start_date: str, end_date: str, label: str) -> dict | None:
        a = next((c for d, c in aligned if d == start_date), None)
        b = next((c for d, c in aligned if d == end_date), None)
        if not a or not b:
            return None
        asset = (b / a - 1) * 100
        market = (bench_at[end_date] / bench_at[start_date] - 1) * 100
        # 초과는 차이가 아니라 비율로 낸다. 큰 변동에서 산술차는 왜곡된다.
        excess = ((b / a) / (bench_at[end_date] / bench_at[start_date]) - 1) * 100
        return {
            "label": label, "from": start_date, "to": end_date,
            "asset_pct": round(asset, 1),
            "market_pct": round(market, 1),
            "excess_pct": round(excess, 1),
            # 이 한 줄이 착각을 막는다
            "reading": (f"종목 {asset:+.1f}% 중 시장이 {market:+.1f}%, "
                        f"종목 고유는 {excess:+.1f}%"),
        }

    last_date = aligned[-1][0]
    high_date = max(aligned, key=lambda x: x[1])[0]
    low_date = min(aligned, key=lambda x: x[1])[0]
    legs = [
        leg(high_date, last_date, "구간 고점 → 현재"),
        leg(low_date, last_date, "구간 저점 → 현재"),
        leg(aligned[0][0], last_date, "구간 전체"),
    ]
    if high_date > low_date:
        legs.insert(0, leg(low_date, high_date, "구간 저점 → 고점"))
    legs = [x for x in legs if x]

    down = next((x for x in legs if x["label"] == "구간 고점 → 현재"), None)
    verdict = None
    if down:
        share = (abs(down["market_pct"]) / abs(down["asset_pct"]) * 100
                 if down["asset_pct"] else None)
        if share is not None:
            verdict = (f"고점 대비 하락의 약 {share:.0f}% 가 시장 요인이다. "
                       + ("종목 고유 원인을 찾기 전에 시장 하락을 먼저 설명해야 한다"
                          if share > 60 else
                          "종목 고유 요인이 지배적이다"))
    return {"available": True, "benchmark_symbol": None,
            "legs": legs, "verdict": verdict}


def closes(symbol: str) -> list[tuple[str, float, int]]:
    path = ROOT / "knowledge" / "prices" / f"{safe_name(symbol)}.csv"
    if not path.exists():
        print(f"오류: 시세 파일이 없다 — {path}", file=sys.stderr)
        raise SystemExit(1)
    with path.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(l for l in f if not l.startswith("#")))
    return [(r["date"], float(r["close"]), int(r["volume"])) for r in rows]


def main() -> int:
    p = argparse.ArgumentParser(description="급변일 ↔ 공시 대조 (인과는 붙이지 않음)")
    p.add_argument("stock_code")
    p.add_argument("--symbol", required=True)
    p.add_argument("--days", type=int, default=90, help="돌아볼 기간")
    p.add_argument("--threshold", default="auto",
                   help="급변 기준. 'auto' 면 최근 변동성의 --sigma 배, "
                        "숫자면 고정 변동률 %")
    p.add_argument("--sigma", type=float, default=2.0,
                   help="적응형 임계값의 표준편차 배수")
    p.add_argument("--vol-window", type=int, default=60,
                   help="표준편차를 재는 거래일 수")
    p.add_argument("--benchmark", help="지수 심볼. 생략하면 시장에서 추정 "
                                      "(KS→^KS11, KQ→^KQ11, US→^IXIC)")
    p.add_argument("--lag-days", type=int, default=2,
                   help="공시가 반영될 수 있는 거래일 범위. 장 마감 후 공시는 "
                        "다음 거래일에 반영된다")
    p.add_argument("--out")
    a = p.parse_args()

    series = closes(a.symbol)
    last_date = dt.date.fromisoformat(series[-1][0])
    since = last_date - dt.timedelta(days=a.days)
    window = [c for c in series if c[0] >= since.isoformat()]

    avg_volume = sum(c[2] for c in window) / len(window) if window else 0

    # 벤치마크 — 없으면 초과변동 없이 계속한다 (있는 게 훨씬 낫지만 필수는 아니다)
    bench_symbol = a.benchmark or benchmark_for(a.symbol)
    bench_path = ROOT / "knowledge" / "prices" / f"{safe_name(bench_symbol)}.csv"
    bench_returns: dict[str, float] = {}
    if bench_path.exists():
        bench_returns = daily_returns(closes(bench_symbol))
    else:
        print(f"※ 벤치마크 {bench_symbol} 시세가 없어 초과변동을 못 낸다 — "
              f"price_fetch.py history '{bench_symbol}' --range 10y", file=sys.stderr)

    returns = daily_returns(series)
    dates = [c[0] for c in series]
    index_of = {d: i for i, d in enumerate(dates)}

    # 판정에 쓰는 계열의 표준편차를 써야 한다. 초과변동으로 판정하면서 임계값을
    # **원시** 수익률 σ 로 잡으면 단위가 안 맞아 전부 걸러진다 (삼성전자 2026:
    # 원시 σ 6.15% vs 초과 σ 2.43% — 2배 이상 차이라 아무것도 안 걸렸다).
    judged_series = ({d: returns[d] - bench_returns[d]
                      for d in returns if d in bench_returns}
                     if bench_returns else returns)

    # 적응형 임계값. 종목 고유 변동성이 국면마다 다르므로 고정값은 뜻이 달라진다.
    fixed = None
    if str(a.threshold).lower() != "auto":
        fixed = float(a.threshold)

    moves = []
    for i in range(1, len(window)):
        prev, cur = window[i - 1], window[i]
        change = (cur[1] / prev[1] - 1) * 100
        bench = bench_returns.get(cur[0])
        excess = change - bench if bench is not None else None

        # 이 날짜 직전 vol_window 거래일의 표준편차 — 미래를 쓰지 않는다
        pos = index_of[cur[0]]
        prior = [judged_series[d] for d in dates[max(1, pos - a.vol_window):pos]
                 if d in judged_series]
        sigma = stdev(prior)
        limit = fixed if fixed is not None else max(a.sigma * sigma, 1.0)

        # 초과변동이 있으면 그것으로 판정한다. 지수와 같이 움직인 날은
        # 종목 고유 사건이 아니다.
        judged = abs(excess) if excess is not None else abs(change)
        if judged < limit:
            continue
        moves.append({
            "date": cur[0],
            "close": cur[1],
            "change_pct": round(change, 2),
            "benchmark_pct": round(bench, 2) if bench is not None else None,
            "excess_pct": round(excess, 2) if excess is not None else None,
            "threshold_pct": round(limit, 2),
            "sigma_used": round(sigma, 2),
            "moved_with_market": (bench is not None and excess is not None
                                  and abs(excess) < abs(change) / 2),
            "direction": "down" if change < 0 else "up",
            "volume": cur[2],
            "volume_vs_avg": round(cur[2] / avg_volume, 2) if avg_volume else None,
        })

    key = load_key()
    corp = resolve(a.stock_code, key)["corp_code"]
    data = request("list.json", {
        "corp_code": corp,
        "bgn_de": since.strftime("%Y%m%d"),
        "end_de": last_date.strftime("%Y%m%d"),
        "page_count": "100",
    }, key, allow_empty=True)

    filings = []
    for row in (data or {}).get("list", []):
        name = row["report_nm"]
        if name.startswith(NOISE_PREFIX):
            continue
        filings.append({
            "filed": f"{row['rcept_dt'][:4]}-{row['rcept_dt'][4:6]}-{row['rcept_dt'][6:]}",
            "name": name,
            "rcept_no": row["rcept_no"],
            "scheduled": name.startswith(SCHEDULED_PREFIX),
        })

    # 급변일마다 lag 범위 안의 공시를 붙인다. 여러 건이면 여러 건 다 붙인다 —
    # 하나를 골라주면 그게 인과 판단이 된다.
    trading_days = [c[0] for c in window]
    for move in moves:
        idx = trading_days.index(move["date"])
        earliest = trading_days[max(0, idx - a.lag_days)]
        move["candidate_filings"] = [
            f for f in filings if earliest <= f["filed"] <= move["date"]
        ]

    unattributed = [m for m in moves if not m["candidate_filings"]]
    packet = {
        "schema_version": SCHEMA,
        "asset_id": a.stock_code,
        "symbol": a.symbol,
        "collected_at": dt.datetime.now(
            dt.timezone(dt.timedelta(hours=9))).isoformat(timespec="seconds"),
        "window": {"since": since.isoformat(), "until": last_date.isoformat(),
                   "trading_days": len(window)},
        "threshold": ("적응형 — 직전 "
                      f"{a.vol_window}거래일 "
                      f"{'초과변동' if bench_returns else '변동률'} "
                      f"표준편차 × {a.sigma}"
                      if fixed is None else f"고정 {fixed}%"),
        "benchmark": {"symbol": bench_symbol, "available": bool(bench_returns)},
        "market_decomposition": {
            **decompose(window, [c for c in closes(bench_symbol)
                                 if c[0] >= since.isoformat()]
                        if bench_returns else None),
            "benchmark_symbol": bench_symbol,
        },
        "judged_on": ("지수 대비 초과변동" if bench_returns else "종목 변동률"),
        "latest": {"date": series[-1][0], "close": series[-1][1]},
        "moves": moves,
        "filings_in_window": filings,
        "_note": "candidate_filings 는 시간적으로 가까운 공시일 뿐 원인이 아니다. "
                 "여러 건이면 다 넣었다 — 하나를 고르는 것이 인과 판단이고 "
                 "그건 모델의 몫이다",
        # 공시로 설명 안 되는 날은 뉴스 쪽을 본다. 뉴스가 이미 수집돼 있으면
        # build_packet 이 헤드라인을 붙이므로 여기서 다시 요구하지 않는다.
        "_gaps": ([] if not unattributed else [
            f"공시로 설명되지 않는 급변 {len(unattributed)}건 "
            f"({', '.join(m['date'] for m in unattributed[:6])}"
            f"{' 외' if len(unattributed) > 6 else ''}) — 산업 뉴스·동종사·지수 "
            "movement 가 원인일 수 있다."
            + ("" if (ROOT / "knowledge" / "news" / f"{a.stock_code}.json").exists()
               else f" news_fetch.py {a.stock_code} --query <회사명> 로 헤드라인을 붙인다")]),
    }

    out = Path(a.out) if a.out else OUT_DIR / f"{a.stock_code}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(packet, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")

    with_market = [m for m in moves if m.get("moved_with_market")]
    print(f"{a.stock_code} — {since} ~ {last_date} · 급변 {len(moves)}건 · "
          f"공시 {len(filings)}건 · 미설명 {len(unattributed)}건")
    print(f"  판정 기준: {packet['threshold']} / {packet['judged_on']} "
          f"(벤치마크 {bench_symbol}{'' if bench_returns else ' 없음'})")
    if with_market:
        print(f"  ※ {len(with_market)}건은 지수와 함께 움직였다 — "
              "종목 고유 사건이 아닐 수 있다")
    print(f"\n  {'날짜':11s}{'종목':>8s}{'지수':>8s}{'초과':>8s}{'기준':>7s}"
          f"{'거래량':>7s}  후보 공시")
    for m in moves:
        names = ", ".join(f["name"][:30] for f in m["candidate_filings"]) or "—"
        bench = (f"{m['benchmark_pct']:+7.2f}" if m["benchmark_pct"] is not None
                 else f"{'—':>7s}")
        excess = (f"{m['excess_pct']:+7.2f}" if m["excess_pct"] is not None
                  else f"{'—':>7s}")
        mark = "≈" if m.get("moved_with_market") else " "
        print(f"  {m['date']:11s}{m['change_pct']:+7.2f}%{bench}%{excess}%{mark}"
              f"{m['threshold_pct']:>6.1f}%{m['volume_vs_avg']:>6.1f}x  {names}")
    print("  ≈ = 지수와 동행 (초과변동이 종목 변동의 절반 미만)")

    dec = packet["market_decomposition"]
    if dec.get("available"):
        print(f"\n[ 시장 대비 분해 — 벤치마크 {bench_symbol} ]")
        print(f"  {'구간':18s}{'종목':>9s}{'시장':>9s}{'고유':>9s}")
        for leg in dec["legs"]:
            print(f"  {leg['label']:18s}{leg['asset_pct']:>8.1f}%"
                  f"{leg['market_pct']:>8.1f}%{leg['excess_pct']:>8.1f}%")
        if dec.get("verdict"):
            print(f"  → {dec['verdict']}")
    print(f"작성: {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
