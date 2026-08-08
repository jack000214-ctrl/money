#!/usr/bin/env python3
"""한국 종목의 급변일과 가까운 DART 공시를 나란히 저장한다.

공시는 시간상 후보일 뿐 원인으로 판정하지 않는다. 급변일은 종목 수익률 또는
벤치마크를 beta=1로 단순 차감한 상대수익률의 적응형 임계값으로 선별한다.
상대수익률은 조사 우선순위용 기술통계이며 시장·종목 요인의 인과 기여도가 아니다.

사용:
    python3.13 -m collectors.kr_events 005930 --symbol 005930.KS
    python3.13 -m collectors.kr_events 005930 --symbol 005930.KS --threshold 3
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

from collectors.dart import load_key, request, resolve

OUT_DIR = ROOT / "knowledge" / "events"
SCHEMA = "event-packet/2.1.0"
KST = dt.timezone(dt.timedelta(hours=9))
# 미국 장 마감 기준 시각. 수집 시각을 UTC 로 적으면 같은 거래일이 날짜가 갈린다.
ET = dt.timezone(dt.timedelta(hours=-4))

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


def daily_returns(
    series: list[tuple[str, float, int]],
) -> dict[str, tuple[float, str]]:
    """날짜별 (변동률, 그 계산에 쓴 직전 거래일).

    직전 거래일을 함께 돌려주지 않으면 두 시계열의 캘린더가 어긋나도 알 수 없다.
    Yahoo 가 벤치마크의 거래일 하나를 빠뜨리면 이틀치 변동이 하루로 합쳐지고,
    종목에서 그 값을 빼는 순간 초과수익 부호까지 뒤집힌다.
    """
    out = {}
    for i in range(1, len(series)):
        out[series[i][0]] = (
            (series[i][1] / series[i - 1][1] - 1) * 100,
            series[i - 1][0],
        )
    return out


def aligned_benchmark(
    bench_returns: dict[str, tuple[float, str]],
    date: str,
    asset_prev: str,
) -> float | None:
    """두 시계열이 같은 직전 거래일을 쓸 때만 벤치마크 변동률을 준다.

    벤치마크에 거래일 구멍이 있으면 그 값은 이틀치라 종목의 하루치에서 뺄 수
    없다. 근사하지 않고 None 을 돌려준다 — 없는 것보다 틀린 것이 훨씬 나쁘다.
    2026-07-28 코스피 -10.84% 폭락일이 Yahoo 에서 통째로 빠졌을 때, 이 확인이
    없어서 다음 거래일 초과수익이 +13.39% 로 뒤집혀 나왔다.
    """
    row = bench_returns.get(date)
    if row is None or row[1] != asset_prev:
        return None
    return row[0]


def stdev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return (sum((v - mean) ** 2 for v in values) / (len(values) - 1)) ** 0.5


def detect_moves(
    window: list[tuple[str, float, int]],
    *,
    judged_series: dict[str, float],
    dates: list[str],
    index_of: dict[str, int],
    aligned,
    avg_volume: float,
    sigma: float,
    fixed: float | None,
    vol_window: int,
) -> list[dict]:
    """어느 날이 '급변'인지 고르고 그 날의 배수를 낸다.

    수집이 아니라 **판정 대상 선별**이다. 여기서 나온 목록이 그대로 packet 의
    `events.moves` 가 되고, 모델은 그것을 "조사할 날"로 읽는다. 문턱이 한 칸
    어긋나면 실제로 무슨 일이 있었던 날이 목록에서 빠지고, 모델은 그날을
    평범한 날로 취급한다. 빠진 것은 화면에도 남지 않는다.

    렌더링에서 떼어 낸 이유가 그것이다 — 이 숫자들은 print 직전에 만들어져
    어디에도 저장되지 않았고, 그래서 대조할 대상조차 없었다.

    임계값은 **그 날 이전** 구간의 표준편차로만 만든다. 미래를 쓰면 나중에
    조용해진 구간의 급변이 사라진다.
    """
    moves = []
    for i in range(1, len(window)):
        prev, cur = window[i - 1], window[i]
        if not prev[1]:
            continue  # 직전 종가가 0 이면 변화율이 성립하지 않는다
        change = (cur[1] / prev[1] - 1) * 100
        bench = aligned(cur[0], prev[0])
        excess = change - bench if bench is not None else None

        # 이 날짜 직전 vol_window 거래일의 표준편차 — 미래를 쓰지 않는다
        pos = index_of[cur[0]]
        prior = [judged_series[d] for d in dates[max(1, pos - vol_window):pos]
                 if d in judged_series]
        deviation = stdev(prior)
        limit = fixed if fixed is not None else max(sigma * deviation, 1.0)

        # 벤치마크가 있으면 beta=1 상대변동으로 조사 우선순위를 정한다.
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
            "sigma_used": round(deviation, 2),
            "direction": "down" if change < 0 else "up",
            "volume": cur[2],
            "volume_vs_avg": round(cur[2] / avg_volume, 2) if avg_volume else None,
        })
    return moves


def compare_with_benchmark(series: list[tuple[str, float, int]],
                           bench: list[tuple[str, float, int]] | None) -> dict:
    """구간별 종목·벤치마크 수익률과 beta=1 상대수익률을 계산한다."""
    if not bench:
        return {"available": False,
                "_note": "벤치마크 시세가 없어 상대수익률을 계산할 수 없다"}
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
        relative = ((b / a) / (bench_at[end_date] / bench_at[start_date]) - 1) * 100
        return {
            "label": label, "from": start_date, "to": end_date,
            "asset_return_pct": round(asset, 1),
            "benchmark_return_pct": round(market, 1),
            "benchmark_relative_pct": round(relative, 1),
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

    return {
        "available": True,
        "benchmark_symbol": None,
        "method": "(1 + asset_return) / (1 + benchmark_return) - 1 "
                  "(beta=1 단순 비교)",
        "legs": legs,
        "_note": "상대수익률은 기술통계이며 시장·종목 요인의 인과 기여도가 아니다",
    }


def closes(symbol: str) -> list[tuple[str, float, int]]:
    path = ROOT / "knowledge" / "prices" / f"{safe_name(symbol)}.csv"
    if not path.exists():
        print(f"오류: 시세 파일이 없다 — {path}", file=sys.stderr)
        raise SystemExit(1)
    with path.open(encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(l for l in f if not l.lstrip().startswith("#")))
    # 거래량은 비어 있을 수 있다 — 원문 백필로 메운 지수 행은 Yahoo 와 거래량
    # 단위가 달라 값을 넣지 않는다. 종가만 있으면 수익률 계산에는 충분하다.
    return [
        (r["date"], float(r["close"]), int(float(r["volume"] or 0)))
        for r in rows
    ]


def last_bar_settled(symbol: str) -> bool:
    """마지막 봉이 확정 종가인지 가격 파일 헤더에서 읽는다."""
    path = ROOT / "knowledge" / "prices" / f"{safe_name(symbol)}.csv"
    with path.open(encoding="utf-8-sig") as f:
        for line in f:
            if not line.lstrip().startswith("#"):
                break
            if line.startswith("# last_bar "):
                # "UNSETTLED" 도 "SETTLED" 로 끝난다 — 토큰으로 정확히 비교한다.
                return line.rstrip().rsplit("session ", 1)[-1] == "SETTLED"
    return True


def build_event_packet(
    asset_id: str,
    symbol: str,
    filings: list[dict],
    *,
    days: int = 90,
    threshold: str = "auto",
    sigma: float = 2.0,
    vol_window: int = 60,
    benchmark: str | None = None,
    lag_days: int = 2,
    timezone: dt.timezone = KST,
    backfill_hint: str | None = None,
) -> dict:
    """급변일을 찾아 시간상 가까운 공시를 나란히 놓는다.

    가격·벤치마크·급변 판정은 시장과 무관하므로 여기 한 곳에 둔다. 시장마다
    다른 것은 **공시를 어디서 가져오는가** 뿐이고, 그래서 `filings` 는 인자로
    받는다 (한국은 DART list.json, 미국은 이미 모아 둔 EDGAR 공시 색인).

    같은 로직을 시장별로 복사하면 한쪽만 고쳐지는 날이 온다. 벤치마크 캘린더
    확인이나 미확정 봉 제외처럼 조용히 부호를 뒤집는 검사가 여기 있어서, 갈리면
    두 시장의 상대수익률이 서로 다른 뜻을 갖게 된다.
    """
    series = closes(symbol)
    # 장중 수집분의 마지막 행은 진행 중인 봉이다. 거래량이 하루치가 아니라
    # 급변 판정과 거래량 배수를 왜곡하므로 판정 대상에서 뺀다.
    unsettled_bar = None if last_bar_settled(symbol) else series[-1][0]
    if unsettled_bar:
        series = series[:-1]
        if len(series) < 2:
            print(f"오류: {symbol} 에 확정된 거래일이 부족하다", file=sys.stderr)
            raise SystemExit(1)
    last_date = dt.date.fromisoformat(series[-1][0])
    since = last_date - dt.timedelta(days=days)
    window = [c for c in series if c[0] >= since.isoformat()]

    avg_volume = sum(c[2] for c in window) / len(window) if window else 0

    # 벤치마크 — 없으면 초과변동 없이 계속한다 (있는 게 훨씬 낫지만 필수는 아니다)
    bench_symbol = benchmark or benchmark_for(symbol)
    bench_path = ROOT / "knowledge" / "prices" / f"{safe_name(bench_symbol)}.csv"
    bench_returns: dict[str, tuple[float, str]] = {}
    if bench_path.exists():
        bench_returns = daily_returns(closes(bench_symbol))
    else:
        print(f"※ 벤치마크 {bench_symbol} 시세가 없어 상대변동을 못 낸다 — "
              f"python3.13 -m collectors.price history '{bench_symbol}' --range 10y",
              file=sys.stderr)

    returns = daily_returns(series)
    dates = [c[0] for c in series]
    index_of = {d: i for i, d in enumerate(dates)}

    def aligned(date: str, asset_prev: str) -> float | None:
        return aligned_benchmark(bench_returns, date, asset_prev)

    # 벤치마크 캘린더가 어긋난 날. 판정에서 빼되 감추지 않고 패킷에 남긴다.
    calendar_gaps = [
        {"date": d, "asset_prev_trading_day": prev,
         "benchmark_prev_trading_day": (bench_returns[d][1]
                                        if d in bench_returns else None),
         "reason": ("벤치마크에 그 거래일이 없다" if d not in bench_returns
                    else "벤치마크의 직전 거래일이 종목과 다르다")}
        for d, (_, prev) in returns.items()
        if bench_returns and aligned(d, prev) is None
    ]
    window_gaps = [g for g in calendar_gaps if g["date"] >= since.isoformat()]

    # 상대변동으로 선별할 때는 임계값도 같은 계열의 표준편차로 계산한다.
    judged_series = (
        {d: value - value_b
         for d, (value, prev) in returns.items()
         for value_b in [aligned(d, prev)]
         if value_b is not None}
        if bench_returns
        else {d: value for d, (value, _) in returns.items()}
    )

    # 적응형 임계값. 관측 변동성이 국면마다 달라 고정값은 의미가 달라진다.
    fixed = None
    if str(threshold).lower() != "auto":
        fixed = float(threshold)

    moves = detect_moves(
        window,
        judged_series=judged_series,
        dates=dates,
        index_of=index_of,
        aligned=aligned,
        avg_volume=avg_volume,
        sigma=sigma,
        fixed=fixed,
        vol_window=vol_window,
    )

    filings = [f for f in filings if since.isoformat() <= f["filed"]
               <= last_date.isoformat()]

    # 급변일마다 lag 범위 안의 공시를 붙인다. 여러 건이면 여러 건 다 붙인다 —
    # 하나를 골라주면 그게 인과 판단이 된다.
    trading_days = [c[0] for c in window]
    for move in moves:
        idx = trading_days.index(move["date"])
        earliest = trading_days[max(0, idx - lag_days)]
        move["candidate_filings"] = [
            f for f in filings if earliest <= f["filed"] <= move["date"]
        ]

    without_filings = [m for m in moves if not m["candidate_filings"]]
    return {
        "schema_version": SCHEMA,
        "asset_id": asset_id,
        "symbol": symbol,
        "collected_at": dt.datetime.now(timezone).isoformat(timespec="seconds"),
        "window": {"since": since.isoformat(), "until": last_date.isoformat(),
                   "trading_days": len(window)},
        "threshold": ("적응형 — 직전 "
                      f"{vol_window}거래일 "
                      f"{'초과변동' if bench_returns else '변동률'} "
                      f"표준편차 × {sigma}"
                      if fixed is None else f"고정 {fixed}%"),
        "benchmark": {
            "symbol": bench_symbol,
            "available": bool(bench_returns),
            "daily_relative_method": "asset_return_pct - benchmark_return_pct "
                                     "(beta=1 조사 우선순위용)",
            # 캘린더가 어긋난 날은 상대변동을 내지 않고 여기에 남긴다. 비어
            # 있으면 구간의 모든 거래일이 두 시계열에서 같은 직전일을 썼다는 뜻이다.
            "calendar_gaps": window_gaps,
        },
        "last_bar": {
            "date": series[-1][0],
            "settled": True,
            "excluded_unsettled_bar": unsettled_bar,
            "_note": ("장중 수집분의 미확정 봉을 판정에서 제외했다"
                      if unsettled_bar else
                      "수집 시점에 마지막 봉이 확정 종가였다"),
        },
        "benchmark_comparison": {
            **compare_with_benchmark(
                window,
                [c for c in closes(bench_symbol) if c[0] >= since.isoformat()]
                if bench_returns else None,
            ),
            "benchmark_symbol": bench_symbol,
        },
        "judged_on": (
            "벤치마크 대비 beta=1 상대변동" if bench_returns else "종목 변동률"
        ),
        "latest": {"date": series[-1][0], "close": series[-1][1]},
        "moves": moves,
        "filings_in_window": filings,
        "_note": "candidate_filings 는 시간적으로 가까운 공시일 뿐 원인이 아니다. "
                 "여러 건이면 다 넣었다 — 하나를 고르는 것이 인과 판단이고 "
                 "그건 모델의 몫이다",
        "_gaps": [
            *([] if not without_filings else [
                f"근접 공시가 없는 급변 {len(without_filings)}건 "
                f"({', '.join(m['date'] for m in without_filings[:6])}"
                f"{' 외' if len(without_filings) > 6 else ''}) — 산업 뉴스·동종사·지수 "
                "원문은 CIO 모델이 직접 추가 조사한다."]),
            *([] if not window_gaps else [
                f"벤치마크 캘린더가 어긋난 거래일 {len(window_gaps)}건 "
                f"({', '.join(g['date'] for g in window_gaps[:6])}"
                f"{' 외' if len(window_gaps) > 6 else ''}) — 그 날의 상대변동은 "
                "계산하지 않고 null 로 뒀다. 상대수익률로 인용하지 말 것."
                + (f" 복구: {backfill_hint}" if backfill_hint else "")]),
            *([] if not unsettled_bar else [
                f"{unsettled_bar} 은 장중 미확정 봉이라 판정에서 제외했다 — "
                "이 패킷의 마지막 거래일은 그 전날이다"]),
        ],
    }


def render_packet(packet: dict, out: Path) -> None:
    """사람이 읽는 요약. 저장은 호출한 쪽이 이미 끝냈다고 본다."""
    moves = packet["moves"]
    window = packet["window"]
    bench_symbol = packet["benchmark"]["symbol"]
    available = packet["benchmark"]["available"]
    window_gaps = packet["benchmark"]["calendar_gaps"]
    unsettled = packet["last_bar"]["excluded_unsettled_bar"]
    without_filings = [m for m in moves if not m["candidate_filings"]]
    print(f"{packet['asset_id']} — {window['since']} ~ {window['until']} · "
          f"급변 {len(moves)}건 · 공시 {len(packet['filings_in_window'])}건 · "
          f"근접 공시 없음 {len(without_filings)}건")
    print(f"  판정 기준: {packet['threshold']} / {packet['judged_on']} "
          f"(벤치마크 {bench_symbol}{'' if available else ' 없음'})")
    if unsettled:
        print(f"  ※ {unsettled} 미확정 봉 제외 — 마지막 확정 거래일은 "
              f"{window['until']}")
    if window_gaps:
        print(f"  ※ 벤치마크 캘린더 불일치 {len(window_gaps)}건 "
              f"({', '.join(g['date'] for g in window_gaps[:6])}) — 그 날 "
              "상대변동은 내지 않았다.")
    print(f"\n  {'날짜':11s}{'종목':>8s}{'지수':>8s}{'상대':>8s}{'기준':>7s}"
          f"{'거래량':>7s}  후보 공시")
    for m in moves:
        names = ", ".join(f["name"][:30] for f in m["candidate_filings"]) or "—"
        bench = (f"{m['benchmark_pct']:+7.2f}" if m["benchmark_pct"] is not None
                 else f"{'—':>7s}")
        excess = (f"{m['excess_pct']:+7.2f}" if m["excess_pct"] is not None
                  else f"{'—':>7s}")
        volume = (f"{m['volume_vs_avg']:>6.1f}x" if m["volume_vs_avg"] is not None
                  else f"{'—':>7s}")
        print(f"  {m['date']:11s}{m['change_pct']:+7.2f}%{bench}%{excess}% "
              f"{m['threshold_pct']:>6.1f}%{volume}  {names}")

    comparison = packet["benchmark_comparison"]
    if comparison.get("available"):
        print(f"\n[ 벤치마크 단순 비교 — {bench_symbol} ]")
        print(f"  {'구간':18s}{'종목':>9s}{'벤치마크':>9s}{'상대':>9s}")
        for leg in comparison["legs"]:
            print(f"  {leg['label']:18s}{leg['asset_return_pct']:>8.1f}%"
                  f"{leg['benchmark_return_pct']:>8.1f}%"
                  f"{leg['benchmark_relative_pct']:>8.1f}%")
        print(f"  ※ {comparison['_note']}")
    print(f"작성: {out.relative_to(ROOT)}")


def save_packet(packet: dict, out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(packet, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return out_path


def main() -> int:
    p = argparse.ArgumentParser(description="급변일 ↔ 공시 대조 (인과는 붙이지 않음)")
    p.add_argument("stock_code")
    p.add_argument("--symbol", required=True)
    p.add_argument("--days", type=int, default=90, help="돌아볼 기간")
    p.add_argument("--threshold", default="auto",
                   help="급변 기준. 'auto' 면 최근 변동성의 --sigma 배, "
                        "숫자면 고정 변동률 %%")
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

    # 한국은 공시 목록을 DART 에서 그때그때 받는다.
    key = load_key()
    corp = resolve(a.stock_code, key)["corp_code"]
    last_close = closes(a.symbol)[-1][0]
    since = dt.date.fromisoformat(last_close) - dt.timedelta(days=a.days)
    data = request("list.json", {
        "corp_code": corp,
        "bgn_de": since.strftime("%Y%m%d"),
        "end_de": last_close.replace("-", ""),
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

    packet = build_event_packet(
        a.stock_code, a.symbol, filings,
        days=a.days, threshold=a.threshold, sigma=a.sigma,
        vol_window=a.vol_window, benchmark=a.benchmark, lag_days=a.lag_days,
        timezone=KST,
        backfill_hint=(
            "python3.13 -m collectors.krx backfill "
            + ("KOSDAQ" if (a.benchmark or benchmark_for(a.symbol)) == "^KQ11"
               else "KOSPI")
        ),
    )
    out = save_packet(packet, Path(a.out) if a.out else
                      OUT_DIR / f"{a.stock_code}.json")
    render_packet(packet, out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
