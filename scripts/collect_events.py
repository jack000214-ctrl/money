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


def closes(symbol: str) -> list[tuple[str, float, int]]:
    path = ROOT / "knowledge" / "prices" / f"{symbol.replace('.', '_')}.csv"
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
    p.add_argument("--threshold", type=float, default=5.0,
                   help="급변으로 볼 일간 변동률 %")
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
    moves = []
    for i in range(1, len(window)):
        prev, cur = window[i - 1], window[i]
        change = (cur[1] / prev[1] - 1) * 100
        if abs(change) < a.threshold:
            continue
        moves.append({
            "date": cur[0],
            "close": cur[1],
            "change_pct": round(change, 2),
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
        "threshold_pct": a.threshold,
        "latest": {"date": series[-1][0], "close": series[-1][1]},
        "moves": moves,
        "filings_in_window": filings,
        "_note": "candidate_filings 는 시간적으로 가까운 공시일 뿐 원인이 아니다. "
                 "여러 건이면 다 넣었다 — 하나를 고르는 것이 인과 판단이고 "
                 "그건 모델의 몫이다",
        "_gaps": ([] if not unattributed else [
            f"공시로 설명되지 않는 급변 {len(unattributed)}건 "
            f"({', '.join(m['date'] for m in unattributed[:6])}"
            f"{' 외' if len(unattributed) > 6 else ''}) — 산업 뉴스·동종사·지수 "
            "movement 가 원인일 수 있다. 뉴스 수집기가 아직 없다"]),
    }

    out = Path(a.out) if a.out else OUT_DIR / f"{a.stock_code}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(packet, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")

    print(f"{a.stock_code} — {since} ~ {last_date} · 급변 {len(moves)}건 "
          f"(±{a.threshold}%) · 공시 {len(filings)}건 · "
          f"미설명 {len(unattributed)}건")
    for m in moves:
        names = ", ".join(f["name"][:34] for f in m["candidate_filings"]) or "—"
        print(f"  {m['date']}  {m['change_pct']:+6.2f}%  거래량 "
              f"{m['volume_vs_avg']:>4.1f}x  {names}")
    print(f"작성: {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
