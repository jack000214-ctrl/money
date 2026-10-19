#!/usr/bin/env python3
"""미국 종목의 급변일과 가까운 SEC 공시를 나란히 저장한다.

한국판(collectors/kr_events.py)과 **같은 판정 로직**을 쓴다. 급변 선별·벤치마크
정렬·미확정 봉 제외는 시장과 무관하고, 갈리면 두 시장의 상대수익률이 서로 다른
뜻을 갖게 되기 때문이다. 여기서 다른 것은 공시를 어디서 가져오는가 하나다.

공시는 이미 모아 둔 `knowledge/filings/<ticker>.json` 에서 읽는다 — SEC 를 다시
부르지 않는다. 그 색인은 `collectors.edgar filings` 가 만들고 8-K 의 item 코드까지
들고 있어서, 실적 발표(2.02)와 정기보고서와 내부자 신고를 구분할 수 있다.

사용:
    python -m collectors.us_events IBM --symbol IBM --benchmark ^GSPC
    python -m collectors.us_events SCHD --symbol SCHD
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

from collectors.kr_events import (
    ET,
    OUT_DIR,
    benchmark_for,
    build_event_packet,
    render_packet,
    save_packet,
)

FILING_DIR = ROOT / "knowledge" / "filings"

# 예정된 일정이라 급변의 원인 후보로서 값이 다르다. 한국의 사업·분기보고서와
# 같은 자리다 — 날짜가 미리 알려져 있으므로 놀라움이 아니다.
SCHEDULED_FORMS = {"10-Q", "10-K", "10-Q/A", "10-K/A", "DEF 14A", "ARS"}
# 신호를 덮는 공시. Form 3/4/5 는 내부자 거래 신고로 하루 수십 건씩 나온다.
NOISE_FORMS = {"3", "4", "5", "3/A", "4/A", "5/A", "SC 13G/A", "SC 13D/A"}


def load_filings(asset_id: str) -> list[dict]:
    """저장된 EDGAR 공시 색인을 급변 대조용 형태로 바꾼다.

    `filed` 는 공시일이다. SEC 의 `filing_date` 는 접수일이고 장 마감 후 접수는
    다음 거래일에 반영되므로, 그 지연은 build_event_packet 의 lag_days 가 본다.
    """
    path = FILING_DIR / f"{asset_id}.json"
    if not path.exists():
        print(f"오류: 공시 색인이 없다 — {path}\n"
              f"  먼저: python -m collectors.edgar filings {asset_id} "
              "--since YYYYMMDD", file=sys.stderr)
        raise SystemExit(1)
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = []
    for row in data.get("filings", []):
        form = str(row.get("form") or "").strip()
        if form in NOISE_FORMS:
            continue
        filed = row.get("filing_date")
        if not filed:
            continue
        labels = row.get("item_labels") or []
        name = form
        if labels:
            # 8-K 는 폼 이름만으로는 무엇이 났는지 알 수 없다.
            name = f"{form} — {', '.join(labels)}"
        elif row.get("description") and row["description"] != form:
            name = f"{form} — {row['description']}"
        rows.append({
            "filed": filed,
            "name": name,
            "form": form,
            "accession_number": row.get("accession_number"),
            "document_url": row.get("document_url"),
            "items": row.get("items"),
            "scheduled": form in SCHEDULED_FORMS,
        })
    return sorted(rows, key=lambda r: r["filed"])


def main() -> int:
    p = argparse.ArgumentParser(
        description="미국 급변일 ↔ SEC 공시 대조 (인과는 붙이지 않음)")
    p.add_argument("ticker")
    p.add_argument("--symbol", help="시세 심볼. 생략하면 ticker 와 같다")
    p.add_argument("--days", type=int, default=90, help="돌아볼 기간")
    p.add_argument("--threshold", default="auto",
                   help="급변 기준. 'auto' 면 최근 변동성의 --sigma 배, "
                        "숫자면 고정 변동률 %%")
    p.add_argument("--sigma", type=float, default=2.0,
                   help="적응형 임계값의 표준편차 배수")
    p.add_argument("--vol-window", type=int, default=60,
                   help="표준편차를 재는 거래일 수")
    p.add_argument("--benchmark", help="지수 심볼. 생략하면 ^IXIC")
    p.add_argument("--lag-days", type=int, default=2,
                   help="공시가 반영될 수 있는 거래일 범위. 장 마감 후 접수는 "
                        "다음 거래일에 반영된다")
    p.add_argument("--out")
    a = p.parse_args()

    symbol = a.symbol or a.ticker
    packet = build_event_packet(
        a.ticker, symbol, load_filings(a.ticker),
        days=a.days, threshold=a.threshold, sigma=a.sigma,
        vol_window=a.vol_window,
        benchmark=a.benchmark or benchmark_for(symbol),
        lag_days=a.lag_days,
        timezone=ET,
        backfill_hint=(
            f"python -m collectors.price history "
            f"'{a.benchmark or benchmark_for(symbol)}' --range 10y"
        ),
    )
    out = save_packet(packet, Path(a.out) if a.out else
                      OUT_DIR / f"{a.ticker}.json")
    render_packet(packet, out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
