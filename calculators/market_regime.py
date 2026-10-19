#!/usr/bin/env python3
"""Measure current market stress without prescribing an investment action."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import statistics
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRICE_DIR = ROOT / "knowledge" / "prices"
CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{s}?range={r}&interval=1d"
UA = {"User-Agent": "Mozilla/5.0"}

MARKETS = {
    "US": {
        "indices": [("^GSPC", "S&P 500"), ("^IXIC", "NASDAQ")],
        "volatility": ("^VIX", "VIX"),
        "volatility_in_score": True,
        "macro": [("KRW=X", "KRW/USD"), ("^TNX", "US 10Y yield")],
    },
    "KR": {
        "indices": [("^KS11", "KOSPI"), ("^KQ11", "KOSDAQ")],
        "volatility": ("^VIX", "VIX (global reference)"),
        "volatility_in_score": False,
        "macro": [],
    },
}


def safe_name(symbol: str) -> str:
    return symbol.replace(".", "_").replace("^", "").replace("=", "")


def local_series(symbol: str, days: int | None = None) -> list[tuple[str, float]]:
    return local_series_with_fingerprint(symbol, days)[0]


def local_series_with_fingerprint(
    symbol: str, days: int | None = None
) -> tuple[list[tuple[str, float]], str | None]:
    """로컬 가격 파일의 종가와, **읽은 그 바이트**의 지문.

    지문은 "이 계산이 어떤 입력 위에서 나왔는가"의 기록이다. 가격 파일은 회수시각을
    바꾸지 않고도 내용이 바뀐다 — 거래소 백필이 빠진 봉을 채우면 `retrieved_at` 은
    그대로다. 시각만 비교하는 의존성 검사는 그 변화를 못 보고, 그래서 입력이
    바뀌었는데 옛 시장 상태가 신선하다고 남았다(`cio/refresh.py:inspect_task`).
    읽은 바이트와 지문 낸 바이트가 같도록 파일을 한 번만 읽는다.
    """
    path = PRICE_DIR / f"{safe_name(symbol)}.csv"
    if not path.exists():
        return [], None
    raw = path.read_bytes()
    rows = list(
        csv.DictReader(
            line
            for line in raw.decode("utf-8").splitlines()
            if not line.lstrip().startswith("#")
        )
    )
    points = [
        (row["date"], float(row["close"]))
        for row in rows
        if row.get("date") and row.get("close")
    ]
    if days and len(points) > days:
        points = points[-days:]
    return points, hashlib.sha256(raw).hexdigest()


def source_row(symbol: str, mode: str, points: list[tuple[str, float]],
               fingerprint: str | None) -> dict:
    """입력 한 줄. 로컬 파일을 읽었으면 그 경로와 내용 지문을 함께 남긴다."""
    row = {
        "symbol": symbol,
        "mode": mode,
        "data_as_of": points[-1][0] if points else None,
    }
    if mode == "LOCAL_SNAPSHOT" and fingerprint:
        path = PRICE_DIR / f"{safe_name(symbol)}.csv"
        try:
            row["path"] = path.relative_to(ROOT).as_posix()
        except ValueError:
            row["path"] = str(path)
        row["content_sha256"] = fingerprint
    return row


def remote_series(symbol: str, period: str) -> list[tuple[str, float]]:
    try:
        request = urllib.request.Request(
            CHART.format(s=symbol, r=period), headers=UA
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
        result = data["chart"]["result"][0]
        timestamps = result["timestamp"]
        closes = result["indicators"]["quote"][0]["close"]
        return [
            (
                dt.datetime.fromtimestamp(timestamp, dt.timezone.utc)
                .date()
                .isoformat(),
                float(close),
            )
            for timestamp, close in zip(timestamps, closes)
            if close is not None
        ]
    except (urllib.error.URLError, KeyError, IndexError, TypeError, ValueError):
        return []


def get_series(
    symbol: str, period: str, days: int, *, local_only: bool
) -> tuple[list[tuple[str, float]], str]:
    points, mode, _ = get_series_with_fingerprint(
        symbol, period, days, local_only=local_only
    )
    return points, mode


def get_series_with_fingerprint(
    symbol: str, period: str, days: int, *, local_only: bool
) -> tuple[list[tuple[str, float]], str, str | None]:
    local, fingerprint = local_series_with_fingerprint(symbol, days)
    if local:
        return local, "LOCAL_SNAPSHOT", fingerprint
    if local_only:
        return [], "MISSING", None
    remote = remote_series(symbol, period)
    return remote, "REMOTE_EPHEMERAL" if remote else "MISSING", None


def _rounded(value: float | None) -> float | None:
    """없는 값은 없는 채로 둔다. round(None or 0) 은 0 을 지어낸다."""
    return None if value is None else round(value, 2)


def percentile(values: list[float], value: float) -> float | None:
    if not values:
        return None
    return sum(1 for item in values if item < value) / len(values) * 100


def classify(
    vix: float | None, worst_drawdown_pct: float
) -> tuple[str, str, int, list[str]]:
    score = 0
    notes: list[str] = []
    if vix is not None:
        if vix >= 40:
            score += 3
            notes.append("VIX >= 40")
        elif vix >= 30:
            score += 2
            notes.append("VIX >= 30")
        elif vix >= 22:
            score += 1
            notes.append("VIX >= 22")
    if worst_drawdown_pct <= -20:
        score += 3
        notes.append(f"주요 지수 고점 대비 {worst_drawdown_pct:.1f}% — 약세장")
    elif worst_drawdown_pct <= -10:
        score += 2
        notes.append(f"주요 지수 고점 대비 {worst_drawdown_pct:.1f}% — 조정")
    elif worst_drawdown_pct <= -5:
        score += 1
        notes.append(f"주요 지수 고점 대비 {worst_drawdown_pct:.1f}%")
    if score >= 5:
        return "CRISIS", "🔴", score, notes
    if score >= 3:
        return "STRESS", "🟠", score, notes
    if score >= 1:
        return "ELEVATED", "🟡", score, notes
    return "NORMAL", "🟢", score, notes


def analyse(
    market: str,
    period: str,
    days: int,
    *,
    local_only: bool,
) -> tuple[dict, list[dict]]:
    config = MARKETS[market]
    sources = []
    index_rows = []
    for symbol, name in config["indices"]:
        points, mode, fingerprint = get_series_with_fingerprint(
            symbol, period, days, local_only=local_only
        )
        sources.append(source_row(symbol, mode, points, fingerprint))
        if not points:
            continue
        closes = [value for _, value in points]
        close = closes[-1]
        high = max(closes)
        index_rows.append(
            {
                "symbol": symbol,
                "name": name,
                "as_of": points[-1][0],
                "close": round(close, 4),
                "high": round(high, 4),
                "drawdown_pct": round((close / high - 1) * 100, 2),
            }
        )

    volatility_symbol, volatility_name = config["volatility"]
    volatility_points, mode, fingerprint = get_series_with_fingerprint(
        volatility_symbol, period, days, local_only=local_only
    )
    sources.append(
        source_row(volatility_symbol, mode, volatility_points, fingerprint)
    )
    volatility = None
    volatility_now = None
    if volatility_points:
        values = [value for _, value in volatility_points]
        volatility_now = values[-1]
        volatility = {
            "symbol": volatility_symbol,
            "name": volatility_name,
            "as_of": volatility_points[-1][0],
            "value": round(volatility_now, 2),
            "median": round(statistics.median(values), 2),
            # `or 0` 이었다. 백분위가 없는 상태(빈 분포)가 0 백분위,
            # 곧 "역대 최저"라는 관측으로 바뀐다. 없으면 없다고 낸다.
            "percentile": _rounded(percentile(values, volatility_now)),
            "in_score": config["volatility_in_score"],
        }

    if not index_rows:
        return (
            {
                "market": market,
                "regime": "UNKNOWN",
                "score": None,
                "notes": ["지수 데이터가 없어 시장 상태를 분류하지 않았다"],
                "indices": [],
                "volatility": volatility,
                "worst_drawdown_pct": None,
            },
            sources,
        )

    worst = min(row["drawdown_pct"] for row in index_rows)
    score_vix = volatility_now if config["volatility_in_score"] else None
    regime, mark, score, notes = classify(score_vix, worst)
    return (
        {
            "market": market,
            "regime": regime,
            "mark": mark,
            "score": score,
            "notes": notes,
            "indices": index_rows,
            "volatility": volatility,
            "worst_drawdown_pct": worst,
        },
        sources,
    )


def macro(
    market: str, period: str, days: int, *, local_only: bool
) -> tuple[list[dict], list[dict]]:
    rows = []
    sources = []
    for symbol, name in MARKETS[market]["macro"]:
        points, mode, fingerprint = get_series_with_fingerprint(
            symbol, period, days, local_only=local_only
        )
        sources.append(source_row(symbol, mode, points, fingerprint))
        if not points:
            continue
        values = [value for _, value in points]
        rows.append(
            {
                "symbol": symbol,
                "name": name,
                "as_of": points[-1][0],
                "value": round(values[-1], 4),
                "median": round(statistics.median(values), 4),
                "percentile": _rounded(percentile(values, values[-1])),
            }
        )
    return rows, sources


def main() -> int:
    parser = argparse.ArgumentParser(description="중립적인 시장 스트레스 측정")
    parser.add_argument("--market", choices=["KR", "US"])
    parser.add_argument("--range", default="2y")
    parser.add_argument("--days", type=int, default=500)
    parser.add_argument("--out")
    parser.add_argument("--local-only", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    markets = [args.market] if args.market else sorted(MARKETS)
    by_market = {}
    input_sources = []
    macro_rows = {}
    for market_name in markets:
        block, sources = analyse(
            market_name,
            args.range,
            args.days,
            local_only=args.local_only,
        )
        by_market[market_name] = block
        input_sources.extend(sources)
        rows, sources = macro(
            market_name,
            args.range,
            args.days,
            local_only=args.local_only,
        )
        macro_rows[market_name] = rows
        input_sources.extend(sources)

    snapshot = {
        "schema_version": "market-regime/2.0.0",
        "collected_at": dt.datetime.now().astimezone().isoformat(
            timespec="seconds"
        ),
        "lookback_days": args.days,
        "by_market": by_market,
        "macro": {"by_market": macro_rows},
        "input_sources": input_sources,
        # 이 계산이 읽은 로컬 파일과 그 내용 지문. refresh 가 지금 파일과 대조해
        # 입력이 바뀌었으면 다시 계산한다 — 시각이 아니라 내용으로 본다.
        "input_fingerprints": {
            source["path"]: source["content_sha256"]
            for source in input_sources
            if source.get("path") and source.get("content_sha256")
        },
        "_note": "측정값이며 투자 행동을 지시하지 않는다",
    }
    if args.out:
        out = Path(args.out)
    elif args.market:
        out = ROOT / "knowledge" / "market" / f"regime-{args.market}.json"
    else:
        out = ROOT / "knowledge" / "market" / "regime.json"
    if not out.is_absolute():
        out = ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if args.json:
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    else:
        for market_name, block in by_market.items():
            print(
                f"{market_name}: {block['regime']} "
                f"(score={block['score']}, drawdown={block['worst_drawdown_pct']})"
            )
        print(f"saved: {out}")
    return 1 if any(block["regime"] == "UNKNOWN" for block in by_market.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
