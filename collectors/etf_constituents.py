#!/usr/bin/env python3
"""ETF 구성종목마다 가격 이력과 배수를 받아 들여다보기(look-through) 재료를 만든다.

ETF 가격만 보면 "3개월에 34% 내렸다"까지만 보인다. 그 34%가 종목이 내린 것인지
환율이 내린 것인지, 내려온 뒤의 가격표가 구성종목 이익의 몇 배인지는 구성종목을
들여다봐야 나온다. 2026-10-09 판단에서 이 두 가지가 결론을 정했는데, 저장소에
경로가 없어 세션 임시 스크립트로 냈고 그 숫자는 어디에도 남지 않았다. 조건부
매수 계획이 "흑자 묶음 forward PER 22배 이하" 같은 문턱에 걸려 있으므로 같은
방식으로 다시 계산할 수 있어야 한다.

이 파일은 **재료만 모은다.** 가중 배수와 수익률 분해의 산술은
`calculators/etf_lookthrough.py` 에 있다.

- 가격: Yahoo chart API(일봉, 조정종가). 종목별 창 수익률과 12개월 고점 대비
  낙폭만 남기고 일봉은 저장하지 않는다 — 구성종목 수십 개의 5년 일봉을
  저장소에 쌓으면 판단에 쓰이지 않는 행이 대부분이 된다.
- 배수: stockanalysis.com 통계 페이지. **2차 출처다.** forward 추정의 기준연도와
  표본 수를 이 페이지가 밝히지 않는다. 원문은 `collectors.web` 과 같은 내용
  주소 저장소에 보존하고 여기에는 읽은 값만 싣는다.
- 환율: 구성종목 통화 → ETF 통화. 달러를 거친 교차환율로 통일한다. Yahoo 의
  직접 교차쌍(CNYKRW=X 등)은 이력이 한 행뿐인 것이 있어 쌍마다 품질이 다르다.

못 받은 값은 null 이고 `unresolved` 에 사유를 남긴다. 한 종목이 실패해도
나머지는 계속 받는다.

사용:
    python3.13 -m collectors.etf_constituents 0053L0
    python3.13 -m collectors.etf_constituents 0053L0 --no-valuation
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import datetime as dt
import html
import io
import re
import sys
import time
from pathlib import Path
from typing import Any

from calculators.contracts import content_hash, write_json
from cio.assets import get_asset
from cio.paths import KNOWLEDGE_DIR, ROOT
from collectors import price, web

KST = dt.timezone(dt.timedelta(hours=9))
ETF_DIR = KNOWLEDGE_DIR / "etfs"
SCHEMA_VERSION = "etf-constituents/1.0.0"
# 달력 일수로 잰 되돌아보기 창. 거래일 수로 재면 시장마다 휴장일이 달라 같은
# "3개월"이 종목마다 다른 기간이 된다. 종목·환율·ETF 를 같은 창으로 맞추는
# 것이 분해의 전제다.
WINDOWS_DAYS = {"1m": 30, "3m": 91, "6m": 182, "12m": 365}
# 창 시작일에 봉이 없으면 그 이전 가장 가까운 봉을 쓴다. 그 봉이 이보다 멀면
# 이력이 비어 있는 것이므로 수익률을 내지 않는다(상장 직후, 장기 거래정지).
MAX_REFERENCE_GAP_DAYS = 10
MAX_HOLDINGS = 60
PRICE_RANGE = "2y"
REQUEST_PAUSE_SECONDS = 0.4
VALUATION_PROVIDER = "stockanalysis.com"
STATS_BASE = "https://stockanalysis.com"

# 통계 페이지의 표 라벨 → 이 파일의 키. 값은 셀의 title 속성(원자료 정밀값)을
# 먼저 읽고 없으면 셀 텍스트를 읽는다.
STAT_LABELS = {
    "trailing_pe": "PE Ratio",
    "forward_pe": "Forward PE",
    "ps": "PS Ratio",
    "pb": "PB Ratio",
    "profit_margin_pct": "Profit Margin",
}
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
CELL_RE = re.compile(r"<td\b([^>]*)>(.*?)</td>", re.DOTALL)
TITLE_ATTR_RE = re.compile(r'\btitle="([^"]*)"')
COMMENT_OR_TAG_RE = re.compile(r"<!--.*?-->|<[^>]+>", re.DOTALL)


def fail(message: str) -> None:
    print(f"오류: {message}", file=sys.stderr)
    raise SystemExit(1)


def route(holding_id: str, market: str) -> dict[str, Any] | None:
    """holding_id 를 가격 심볼과 통계 페이지 주소로 바꾼다.

    확인한 거래소만 다룬다. 모르는 표기에 접미사를 추측해 붙이지 않는다.
    """
    holding_id = holding_id.strip()
    upper = holding_id.upper()
    if upper.endswith(".HK"):
        code = upper[:-3].zfill(4)
        return {"yahoo": [f"{code}.HK"], "stats": f"/quote/hkg/{code}/statistics/", "code": code}
    for suffix, slug in ((".SZ", "she"), (".SS", "sha"), (".T", "tyo")):
        if upper.endswith(suffix):
            code = upper[: -len(suffix)]
            return {"yahoo": [upper], "stats": f"/quote/{slug}/{code}/statistics/", "code": code}
    if market == "KR" and re.fullmatch(r"[0-9][0-9A-Z]{5}", upper):
        return {
            "yahoo": [f"{upper}.KS", f"{upper}.KQ"],
            "stats": f"/quote/krx/{upper}/statistics/",
            "code": upper,
        }
    if market == "US" and re.fullmatch(r"[A-Z][A-Z0-9.]{0,9}", upper):
        return {
            "yahoo": [upper.replace(".", "-")],
            "stats": f"/stocks/{upper.lower()}/statistics/",
            "code": upper,
        }
    return None


def window_returns(
    points: list[tuple[str, float]], windows: dict[str, int] | None = None
) -> dict[str, float | None]:
    """마지막 봉 기준 달력 창 수익률(%). 낼 수 없으면 null."""
    windows = windows or WINDOWS_DAYS
    result: dict[str, float | None] = {name: None for name in windows}
    if len(points) < 2:
        return result
    last_date = dt.date.fromisoformat(points[-1][0])
    last_value = points[-1][1]
    for name, days in windows.items():
        cutoff = last_date - dt.timedelta(days=days)
        reference = None
        for date_text, value in points:
            if dt.date.fromisoformat(date_text) <= cutoff:
                reference = (date_text, value)
            else:
                break
        if reference is None or reference[1] <= 0:
            continue
        gap = (cutoff - dt.date.fromisoformat(reference[0])).days
        if gap > MAX_REFERENCE_GAP_DAYS:
            continue
        result[name] = round((last_value / reference[1] - 1) * 100, 2)
    return result


def drawdown_12m(points: list[tuple[str, float]]) -> float | None:
    if len(points) < 2:
        return None
    last_date = dt.date.fromisoformat(points[-1][0])
    start = last_date - dt.timedelta(days=365)
    window = [value for date_text, value in points
              if dt.date.fromisoformat(date_text) >= start]
    high = max(window)
    return round((points[-1][1] / high - 1) * 100, 2) if high > 0 else None


def cross_rate(
    quote_per_usd: list[tuple[str, float]] | None,
    base_per_usd: list[tuple[str, float]] | None,
) -> list[tuple[str, float]]:
    """base 통화 1단위의 quote 통화 값. 한쪽이 달러면 그쪽은 None 이다."""
    if quote_per_usd is None and base_per_usd is None:
        return []
    if base_per_usd is None:
        return list(quote_per_usd or [])
    if quote_per_usd is None:
        return [(date, 1 / value) for date, value in base_per_usd if value > 0]
    base = dict(base_per_usd)
    return [
        (date, value / base[date])
        for date, value in quote_per_usd
        if date in base and base[date] > 0
    ]


def _number(text: str | None) -> float | None:
    if text is None:
        return None
    cleaned = html.unescape(text).replace(",", "").replace("%", "").strip()
    if not cleaned or cleaned.lower() in {"n/a", "-", "na"}:
        return None
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return value if value == value and abs(value) != float("inf") else None


def parse_statistics(body: str) -> dict[str, float | None]:
    """통계 페이지에서 배수와 순이익률을 읽는다. 못 읽은 값은 null."""
    values: dict[str, float | None] = {}
    for key, label in STAT_LABELS.items():
        values[key] = None
        needle = f">{label}<"
        start = 0
        while True:
            index = body.find(needle, start)
            if index < 0:
                break
            start = index + len(needle)
            # 같은 라벨이 내비게이션 링크로도 나온다. 그 자리는 값이 아니다.
            if body[start - 1:start + 4].startswith("</a>"):
                continue
            match = CELL_RE.search(body[index:index + 700])
            if not match:
                continue
            # 속성 순서에 기대지 않는다. `class` 뒤에 `title` 이 오는 셀에서
            # 한 정규식으로 둘을 같이 잡으려다 title 을 통째로 놓친 적이 있다.
            title = TITLE_ATTR_RE.search(match.group(1))
            raw = title.group(1) if title else COMMENT_OR_TAG_RE.sub("", match.group(2))
            values[key] = _number(raw)
            break
    return values


def page_matches(body: str, code: str) -> bool:
    """받은 페이지가 그 종목의 것인가. 제목의 `(거래소:코드)` 로 확인한다.

    주소를 잘못 만들면 다른 종목 페이지가 200 으로 올 수 있고, 그 배수는
    형식상 완전히 정상이라 어디서도 걸리지 않는다.
    """
    match = TITLE_RE.search(body)
    if not match:
        return False
    title = html.unescape(match.group(1)).upper()
    return f":{code.upper()})" in title


def load_holdings(asset_id: str) -> tuple[list[dict[str, str]], Path]:
    from cio.refresh import dated_artifacts

    paths = dated_artifacts(ETF_DIR / asset_id, "holdings-", ".csv")
    if not paths:
        fail(f"knowledge/etfs/{asset_id}/holdings-<기준일>.csv 가 없다. 구성종목을 먼저 받는다")
    path = paths[-1]
    with path.open(encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(
            line for line in handle if not line.lstrip().startswith("#")
        ))
    if not rows:
        fail(f"{path.relative_to(ROOT)}: 구성종목 행이 없다")
    return rows, path


def _quiet_price_fetch(symbol: str) -> tuple[dict | None, list[dict], str | None]:
    """`collectors.price.fetch` 는 실패하면 종료한다. 한 종목의 실패가 전체를
    끝내지 않도록 가로채고 사유를 돌려준다."""
    captured = io.StringIO()
    try:
        with contextlib.redirect_stderr(captured):
            meta, rows = price.fetch(symbol, PRICE_RANGE)
    except SystemExit:
        return None, [], captured.getvalue().strip() or "가격 수집 실패"
    return meta, rows, None


def _series(rows: list[dict]) -> list[tuple[str, float]]:
    points = []
    for row in rows:
        value = row.get("adjusted_close")
        if isinstance(value, (int, float)) and value > 0:
            points.append((row["date"], float(value)))
    return points


def _local_series(symbol: str) -> list[tuple[str, float]]:
    """로컬 가격 파일의 조정종가. ETF 자신의 수익률은 여기서 읽는다 —
    packet 의 가격과 같은 파일이어야 분해의 왼쪽과 오른쪽이 맞는다."""
    path = KNOWLEDGE_DIR / "prices" / f"{price.safe_name(symbol)}.csv"
    if not path.exists():
        return []
    points = []
    with path.open(encoding="utf-8-sig") as handle:
        for row in csv.DictReader(
            line for line in handle if not line.lstrip().startswith("#")
        ):
            # 조정종가가 없는 봉을 원가격으로 대신하지 않는다. 한 수익률 안에서
            # 배당이 반영된 값과 안 된 값이 섞이면 배당만큼의 가짜 손익이 생긴다.
            try:
                value = float(row.get("adjusted_close") or "")
            except ValueError:
                continue
            if row.get("date") and value > 0:
                points.append((row["date"], value))
    return points


def _price_block(candidates: list[str]) -> tuple[dict[str, Any] | None, str | None]:
    reason = None
    for symbol in candidates:
        meta, rows, reason = _quiet_price_fetch(symbol)
        points = _series(rows)
        if meta is None or len(points) < 2:
            reason = reason or "가격 이력이 2행 미만이다"
            continue
        return (
            {
                "symbol": symbol,
                "currency": meta.get("currency"),
                "as_of": points[-1][0],
                "close": rows[-1]["close"],
                "bars": len(points),
                "return_pct": window_returns(points),
                "drawdown_12m_pct": drawdown_12m(points),
            },
            None,
        )
    return None, reason


def _valuation_block(
    asset_id: str, routed: dict[str, Any]
) -> tuple[dict[str, Any] | None, str | None]:
    url = STATS_BASE + routed["stats"]
    captured = io.StringIO()
    try:
        with contextlib.redirect_stderr(captured):
            payload = web.fetch(url)
    except SystemExit:
        return None, captured.getvalue().strip() or "통계 페이지 수집 실패"
    if not page_matches(payload["body"], routed["code"]):
        return None, f"받은 페이지의 제목이 {routed['code']} 의 것이 아니다 — 값을 쓰지 않는다"
    label = "stats-" + re.sub(r"[^a-z0-9]+", "-", routed["code"].lower()).strip("-")
    object_path, digest = web.save_snapshot(payload, label=label)
    web.record_index(
        asset_id, label=label, digest=digest,
        locator=str(object_path.relative_to(ROOT)), payload=payload,
        note=f"구성종목 배수 관측(2차 출처: {VALUATION_PROVIDER})",
    )
    return (
        {"source_id": f"WEBSNAP:{digest[:12]}", "url": payload["final_url"],
         **parse_statistics(payload["body"])},
        None,
    )


def collect(asset_id: str, *, with_valuation: bool = True,
            limit: int = MAX_HOLDINGS) -> dict[str, Any]:
    asset = get_asset(asset_id)
    if asset["asset_type"] != "ETF":
        fail(f"{asset_id}: ETF 가 아니다")
    rows, holdings_path = load_holdings(asset["asset_id"])
    as_of_values = {row.get("as_of") for row in rows}
    if len(as_of_values) != 1 or None in as_of_values:
        fail(f"{holdings_path.relative_to(ROOT)}: 구성종목 기준일이 행마다 다르다")
    ordered = sorted(rows, key=lambda row: float(row["weight_pct"]), reverse=True)
    holdings: list[dict[str, Any]] = []
    unresolved: list[str] = []
    for row in ordered[:limit]:
        entry: dict[str, Any] = {
            "holding_id": row["holding_id"],
            "holding_name": row.get("holding_name"),
            "weight_pct": float(row["weight_pct"]),
            "market": row.get("market") or None,
            "price": None,
            "valuation": None,
        }
        routed = route(row["holding_id"], row.get("market") or "")
        if routed is None:
            unresolved.append(f"{row['holding_id']}: 가격·배수 경로를 모르는 표기다")
            holdings.append(entry)
            continue
        entry["price"], reason = _price_block(routed["yahoo"])
        if reason:
            unresolved.append(f"{row['holding_id']}: 가격 — {reason}")
        time.sleep(REQUEST_PAUSE_SECONDS)
        if with_valuation:
            entry["valuation"], reason = _valuation_block(asset["asset_id"], routed)
            if reason:
                unresolved.append(f"{row['holding_id']}: 배수 — {reason}")
            time.sleep(REQUEST_PAUSE_SECONDS)
        holdings.append(entry)
    if len(ordered) > limit:
        unresolved.append(
            f"구성종목 {len(ordered)}개 중 비중 상위 {limit}개만 받았다"
        )

    quote = asset["currency"]
    currencies = sorted({
        (item["price"] or {}).get("currency")
        for item in holdings
        if (item["price"] or {}).get("currency")
    })
    per_usd: dict[str, list[tuple[str, float]] | None] = {"USD": None}
    for currency in sorted({quote, *currencies}):
        if currency in per_usd:
            continue
        _, fx_rows, reason = _quiet_price_fetch(f"{currency}=X")
        per_usd[currency] = _series(fx_rows) or []
        if reason or not per_usd[currency]:
            unresolved.append(f"환율 USD/{currency}: {reason or '이력이 없다'}")
        time.sleep(REQUEST_PAUSE_SECONDS)
    fx: dict[str, Any] = {}
    for currency in currencies:
        if currency == quote:
            fx[currency] = {
                "basis": "SAME_CURRENCY", "as_of": None, "rate": 1.0,
                "return_pct": {name: 0.0 for name in WINDOWS_DAYS},
            }
            continue
        if per_usd.get(currency) == [] or per_usd.get(quote) == []:
            fx[currency] = {"basis": "UNAVAILABLE", "as_of": None, "rate": None,
                            "return_pct": {name: None for name in WINDOWS_DAYS}}
            continue
        points = cross_rate(per_usd[quote], per_usd[currency])
        fx[currency] = {
            "basis": "CROSS_VIA_USD" if currency != "USD" and quote != "USD" else "USD_PAIR",
            "as_of": points[-1][0] if points else None,
            "rate": round(points[-1][1], 6) if points else None,
            "return_pct": window_returns(points),
        }

    etf_points = _local_series(asset["price_symbol"])
    if len(etf_points) < 2:
        unresolved.append(
            "ETF 가격 파일(knowledge/prices)이 없거나 짧다 — 분해의 왼쪽(ETF 수익률)이 비어 있다"
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "asset_id": asset["asset_id"],
        "collected_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "holdings_as_of": next(iter(as_of_values)),
        "holdings_path": str(holdings_path.relative_to(ROOT)),
        "holdings_sha256": content_hash(holdings_path),
        "holdings_total": len(rows),
        "quote_currency": quote,
        "windows_days": WINDOWS_DAYS,
        "valuation_source": {
            "provider": VALUATION_PROVIDER if with_valuation else None,
            "grade": "SECONDARY",
        },
        "etf": {
            "symbol": asset["price_symbol"],
            "as_of": etf_points[-1][0] if etf_points else None,
            "close_adjusted": etf_points[-1][1] if etf_points else None,
            "return_pct": window_returns(etf_points),
        },
        "fx": fx,
        "holdings": holdings,
        "unresolved": unresolved,
    }


def validate_artifact(data: Any, asset_id: str) -> list[str]:
    """`refresh` 가 부르는 계약 검사. 내용의 좋고 나쁨이 아니라 모양만 본다."""
    if not isinstance(data, dict):
        return ["구성종목 재료는 JSON 객체여야 한다"]
    errors = []
    if data.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"{SCHEMA_VERSION}이 필요하다")
    if data.get("asset_id") != asset_id:
        errors.append("구성종목 재료의 asset_id가 요청 자산과 다르다")
    try:
        collected = dt.datetime.fromisoformat(str(data.get("collected_at")))
        if collected.tzinfo is None:
            errors.append("구성종목 재료의 수집시각에 시간대가 없다")
    except ValueError:
        errors.append("구성종목 재료의 수집시각 형식이 잘못됐다")
    holdings = data.get("holdings")
    if not isinstance(holdings, list) or not holdings or any(
        not isinstance(item, dict)
        or not item.get("holding_id")
        or not isinstance(item.get("weight_pct"), (int, float))
        for item in holdings
    ):
        errors.append("구성종목 재료에 holding_id·weight_pct 행이 없다")
    if not isinstance(data.get("holdings_as_of"), str):
        errors.append("구성종목 재료에 구성 기준일이 없다")
    return errors


def latest_path(asset_id: str) -> Path | None:
    paths = sorted((ETF_DIR / asset_id).glob("constituents-*.json"))
    return paths[-1] if paths else None


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python3.13 -m collectors.etf_constituents",
        description="ETF 구성종목별 가격·배수·환율 수집 (look-through 재료)",
    )
    parser.add_argument("asset_id")
    parser.add_argument("--no-valuation", action="store_true",
                        help="배수(2차 출처) 없이 가격·환율만 받는다")
    parser.add_argument("--limit", type=int, default=MAX_HOLDINGS,
                        help="비중 상위 몇 종목까지 받을지")
    args = parser.parse_args()
    data = collect(args.asset_id, with_valuation=not args.no_valuation,
                   limit=args.limit)
    day = dt.datetime.now(KST).date().isoformat()
    path = ETF_DIR / data["asset_id"] / f"constituents-{day}.json"
    write_json(path, data)
    priced = sum(1 for item in data["holdings"] if item["price"])
    valued = sum(1 for item in data["holdings"] if item["valuation"])
    print(
        f"{data['asset_id']} 구성 {len(data['holdings'])}종목 · 가격 {priced} · 배수 {valued} · "
        f"구성 기준일 {data['holdings_as_of']}"
    )
    for line in data["unresolved"]:
        print(f"※ {line}")
    print(f"저장: {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
