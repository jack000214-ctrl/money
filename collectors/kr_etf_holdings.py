#!/usr/bin/env python3
"""운용사 원문에서 한국 상장 ETF의 구성종목(PDF)을 받아 holdings CSV 를 만든다.

ETF 구성은 `refresh` 의 **필수** 입력인데 수집 명령이 없었다. 그래서 7일마다
`MANUAL_REQUIRED` 가 되어 packet 이 BLOCKED 로 멈췄고, 그때마다 모델이 운용사
페이지의 AJAX 주소를 다시 찾아 손으로 CSV 를 적었다. 손으로 옮긴 표는 재현되지
않고, 음수 비중인 현금 행처럼 계약 밖의 행을 어떻게 처리했는지도 남지 않는다.

**운용사마다 주소와 표 모양이 다르다.** 확인한 것만 넣는다 — 지금은 미래에셋
TIGER 하나다. 다른 운용사는 `ISSUERS` 에 어댑터를 더하기 전까지 수동 경로로
남으며, 확인하지 않은 주소를 추측해 넣지 않는다.

받은 원문은 `collectors.web` 과 같은 내용 주소 저장소에 그대로 보존한다. CSV 는
그 원문의 파생물이다. 표가 페이지 단위로 잘려 왔으면(운용사가 선언한 행수와
다르면) 일부만 적지 않고 멈춘다 — 상위 10종목만 담긴 파일은 전체처럼 읽힌다.

사용:
    python3.13 -m collectors.kr_etf_holdings 0053L0
    python3.13 -m collectors.kr_etf_holdings 0053L0 --show
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

from calculators.contracts import write_json
from cio.assets import get_asset
from cio.paths import KNOWLEDGE_DIR, ROOT
from collectors import web

KST = dt.timezone(dt.timedelta(hours=9))
ETF_DIR = KNOWLEDGE_DIR / "etfs"
ISSUER_SCHEMA = "etf-issuer/1.0.0"
HOLDINGS_FIELDS = [
    "etf_id", "holding_id", "holding_name", "weight_pct", "market", "sector",
    "as_of", "issuer_code", "shares_per_cu", "value_krw_per_cu",
]

# 운용사별 주소. `{isin}` 은 registry 의 etf.isin, `{date}` 는 YYYY.MM.DD 다.
# listCnt 를 넉넉히 주는 이유는 기본값이 10이라 그대로 두면 상위 10종목만 온다.
ISSUERS: dict[str, dict[str, str]] = {
    "MIRAE_TIGER": {
        "product_url": (
            "https://investments.miraeasset.com/tigeretf/ko/product/search/"
            "detail/index.do?ksdFund={isin}"
        ),
        "holdings_url": (
            "https://investments.miraeasset.com/tigeretf/ko/product/search/"
            "detail/pdfListAjax.ajax?ksdFund={isin}&pageIndex=1&firstIndex=0"
            "&listCnt=500&fixDate={date}"
        ),
    },
}

ISIN_RE = re.compile(r"KR[0-9A-Z]{10}")
ROW_RE = re.compile(r"<tr\b([^>]*)>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
CELL_RE = re.compile(r"<td\b[^>]*>(.*?)</td>", re.IGNORECASE | re.DOTALL)
TOTAL_RE = re.compile(r'data-tot-cnt="(\d+)"')
TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")

# 블룸버그식 거래소 접미사 → (야후 접미사, 시장). 운용사 PDF 가 쓰는 표기다.
# C1·CG 는 상하이(후강퉁), C2·CS 는 선전(선강퉁)이다.
EXCHANGE_SUFFIX = {
    "HK": (".HK", "HK"),
    "C1": (".SS", "CN"), "CG": (".SS", "CN"),
    "C2": (".SZ", "CN"), "CS": (".SZ", "CN"),
    "JT": (".T", "JP"), "JP": (".T", "JP"),
    "US": ("", "US"), "UW": ("", "US"), "UN": ("", "US"), "UQ": ("", "US"),
    "UA": ("", "US"), "UP": ("", "US"), "UR": ("", "US"),
}
BLOOMBERG_RE = re.compile(r"^(\S+)\s+([A-Z0-9]{2})\s+EQUITY$")
KRX_CODE_RE = re.compile(r"^[0-9][0-9A-Z]{5}$")
# 증권이 아닌 행. 원화예금은 비중이 음수로 오기도 해서(미결제 등) 비중 계약
# (0 이상) 밖이다. 구성종목이 아니므로 CSV 에서 빼고 뺀 사실을 머리말에 남긴다.
CASH_NAME_WORDS = ("예금", "현금", "예수금", "CASH")


def fail(message: str) -> None:
    print(f"오류: {message}", file=sys.stderr)
    raise SystemExit(1)


def _text(fragment: str) -> str:
    return SPACE_RE.sub(" ", html.unescape(TAG_RE.sub(" ", fragment))).strip()


def _number(text: str) -> float | None:
    cleaned = text.replace(",", "").replace("%", "").strip()
    if not cleaned or cleaned in {"-", "n/a"}:
        return None
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return value if value == value and abs(value) != float("inf") else None


def parse_holdings_rows(body: str) -> tuple[list[dict[str, Any]], int | None]:
    """운용사 표의 행과, 운용사가 선언한 전체 행수.

    열 순서는 종목코드·종목명·수량·평가금액·비중(·수익률)이다. 선언 행수는
    `data-tot-cnt` 에서 읽는다 — 받은 행수와 다르면 표가 잘린 것이다.
    """
    rows: list[dict[str, Any]] = []
    declared: int | None = None
    for attributes, inner in ROW_RE.findall(body):
        total = TOTAL_RE.search(attributes)
        cells = [_text(cell) for cell in CELL_RE.findall(inner)]
        if len(cells) < 5:
            continue
        if total:
            declared = int(total.group(1))
        rows.append(
            {
                "issuer_code": cells[0],
                "holding_name": cells[1],
                "shares_per_cu": _number(cells[2]),
                "value_krw_per_cu": _number(cells[3]),
                "weight_pct": _number(cells[4]),
            }
        )
    return rows, declared


def normalize_holding(issuer_code: str, name: str) -> dict[str, str]:
    """운용사 종목코드를 이 저장소의 holding_id·시장으로 바꾼다.

    아는 표기만 바꾼다. 모르는 표기는 **원문 그대로 두고 시장을 비운다** —
    추측한 접미사는 다른 종목의 가격을 조용히 끌어온다.
    """
    code = SPACE_RE.sub(" ", issuer_code).strip()
    upper_name = name.upper()
    if code.upper().startswith(("KRD", "CASH")) or any(
        word in upper_name for word in CASH_NAME_WORDS
    ):
        return {"kind": "CASH", "holding_id": code.replace(" ", "_"), "market": ""}
    match = BLOOMBERG_RE.match(code.upper())
    if match and match.group(2) in EXCHANGE_SUFFIX:
        suffix, market = EXCHANGE_SUFFIX[match.group(2)]
        ticker = match.group(1).replace("/", ".")
        return {"kind": "SECURITY", "holding_id": f"{ticker}{suffix}", "market": market}
    if KRX_CODE_RE.fullmatch(code.upper()):
        return {"kind": "SECURITY", "holding_id": code.upper(), "market": "KR"}
    return {"kind": "SECURITY", "holding_id": code.replace(" ", "_"), "market": ""}


BASE_DATE_RE = re.compile(r"기준일\s*(\d{4})\.(\d{2})\.(\d{2})")
NAV_RE = re.compile(
    r"기준가격\(NAV\).{0,1500}?<span class=\"amount\">\s*([\d,]+(?:\.\d+)?)\s*</span>",
    re.DOTALL,
)
FEE_RE = re.compile(r"총보수</div>.{0,200}?연\s*([\d.]+)\s*(?:%|&#37;)", re.DOTALL)
HEDGE_RE = re.compile(r"환헤지 사항</div>(.{0,400}?)</div>", re.DOTALL)


def parse_product_page(body: str) -> dict[str, Any]:
    """상품 페이지에서 기준일·기준가·총보수·환헤지 문구를 읽는다.

    못 읽은 값은 null 이다. 환헤지는 **운용사 문구를 그대로** 싣는다 — 헤지
    여부를 여기서 참·거짓으로 접으면 '일부 헤지'나 '목표 헤지비율' 같은
    문구가 조용히 둘 중 하나로 바뀐다.
    """
    facts: dict[str, Any] = {
        "as_of": None, "nav": None, "total_fee_pct": None,
        "hedge_statement": None,
    }
    match = BASE_DATE_RE.search(body)
    if match:
        try:
            facts["as_of"] = dt.date(
                int(match.group(1)), int(match.group(2)), int(match.group(3))
            ).isoformat()
        except ValueError:
            facts["as_of"] = None
    match = NAV_RE.search(body)
    if match:
        facts["nav"] = _number(match.group(1))
    match = FEE_RE.search(body)
    if match:
        facts["total_fee_pct"] = _number(match.group(1))
    match = HEDGE_RE.search(body)
    if match:
        facts["hedge_statement"] = _text(match.group(1)) or None
    return facts


def build_holdings(
    asset_id: str, rows: list[dict[str, Any]], *, as_of: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """(구성종목 행, 뺀 행, 오류). 계약을 어기는 표는 행을 내지 않고 오류를 낸다."""
    holdings: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    errors: list[str] = []
    for row in rows:
        normalized = normalize_holding(row["issuer_code"], row["holding_name"])
        weight = row["weight_pct"]
        if normalized["kind"] == "CASH":
            excluded.append({**row, "reason": "CASH"})
            continue
        if weight is None:
            errors.append(f"{row['issuer_code']}: 비중을 읽을 수 없다")
            continue
        if weight < 0:
            errors.append(
                f"{row['issuer_code']}: 증권인데 비중이 음수({weight})다"
            )
            continue
        holdings.append(
            {
                "etf_id": asset_id,
                "holding_id": normalized["holding_id"],
                "holding_name": row["holding_name"],
                "weight_pct": weight,
                "market": normalized["market"],
                "sector": "",
                "as_of": as_of,
                "issuer_code": row["issuer_code"],
                "shares_per_cu": row["shares_per_cu"],
                "value_krw_per_cu": row["value_krw_per_cu"],
            }
        )
    ids = [row["holding_id"] for row in holdings]
    duplicated = sorted({item for item in ids if ids.count(item) > 1})
    if duplicated:
        errors.append("holding_id 가 중복됐다: " + ", ".join(duplicated))
    if not holdings:
        errors.append("구성종목 행이 하나도 없다")
    holdings.sort(key=lambda row: row["weight_pct"], reverse=True)
    return holdings, excluded, errors


def _format(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def write_holdings(
    asset: dict[str, Any],
    holdings: list[dict[str, Any]],
    excluded: list[dict[str, Any]],
    *,
    as_of: str,
    retrieved_at: str,
    sources: dict[str, str],
    url: str,
) -> Path:
    directory = ETF_DIR / asset["asset_id"]
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"holdings-{as_of}.csv"
    coverage = round(sum(row["weight_pct"] for row in holdings), 2)
    lines = [
        f"# {asset['name']} ({asset['asset_id']}) 구성종목 {len(holdings)}종목 — 운용사 원문 자동 수집",
        f"# 조회일 {dt.datetime.fromisoformat(retrieved_at).astimezone(KST).date().isoformat()} / 기준일 {as_of}",
        f"# retrieved_at {retrieved_at} / source {asset['etf']['issuer']}",
        f"# 출처 {url}",
        "# 원문 보존 "
        + ", ".join(f"{name} {source_id}" for name, source_id in sources.items()),
        f"# 비중 합계 {coverage}%",
    ]
    for row in excluded:
        lines.append(
            "# 제외한 행 "
            f"{row['issuer_code']} {row['holding_name']} 비중 {_format(row['weight_pct'])}% "
            "— 증권이 아닌 현금성 항목이라 구성종목 계약(비중 0 이상) 밖이다"
        )
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", newline="", encoding="utf-8", dir=directory, delete=False
        ) as handle:
            handle.write("\n".join(lines) + "\n")
            writer = csv.DictWriter(handle, fieldnames=HOLDINGS_FIELDS)
            writer.writeheader()
            for row in holdings:
                writer.writerow({key: _format(row.get(key)) for key in HOLDINGS_FIELDS})
            handle.flush()
            os.fsync(handle.fileno())
            temporary_name = handle.name
        os.replace(temporary_name, path)
    finally:
        if temporary_name and Path(temporary_name).exists():
            Path(temporary_name).unlink()
    return path


def issuer_path(asset_id: str) -> Path:
    return ETF_DIR / asset_id / "issuer.json"


def _snapshot(url: str, *, asset_id: str, label: str, note: str) -> tuple[dict[str, Any], str]:
    payload = web.fetch(url)
    object_path, digest = web.save_snapshot(payload, label=label)
    web.record_index(
        asset_id, label=label, digest=digest,
        locator=str(object_path.relative_to(ROOT)), payload=payload, note=note,
    )
    return payload, f"WEBSNAP:{digest[:12]}"


def collect(asset_id: str, *, today: dt.date | None = None) -> dict[str, Any]:
    asset = get_asset(asset_id)
    etf = asset.get("etf") or {}
    if asset["asset_type"] != "ETF":
        fail(f"{asset_id}: ETF 가 아니다")
    issuer = etf.get("issuer")
    if issuer not in ISSUERS:
        fail(
            f"{asset_id}: registry 의 etf.issuer({issuer!r})에 맞는 수집 경로가 없다. "
            f"지원: {', '.join(sorted(ISSUERS))}. 다른 운용사는 원문을 직접 확인해 "
            "knowledge/etfs/<asset_id>/holdings-<기준일>.csv 로 저장한다"
        )
    isin = etf.get("isin")
    if not isinstance(isin, str) or not ISIN_RE.fullmatch(isin):
        fail(f"{asset_id}: registry 의 etf.isin 이 없거나 형식이 다르다 (KR + 10자리)")
    today = today or dt.datetime.now(KST).date()
    routes = ISSUERS[issuer]
    product_url = routes["product_url"].format(isin=isin)
    product, product_source = _snapshot(
        product_url, asset_id=asset_id, label="issuer-product-page",
        note="운용사 상품 페이지 — 기준일·기준가·총보수·환헤지 문구",
    )
    facts = parse_product_page(product["body"])
    # 기준일은 운용사가 말한 날짜를 쓴다. 조회일로 대신하면 휴장일에 받은
    # 전 영업일 구성이 오늘 것으로 적힌다.
    as_of = facts["as_of"]
    if not as_of:
        fail(
            f"{asset_id}: 상품 페이지에서 기준일을 읽지 못했다 — 구성종목의 기준일을 "
            "조회일로 대신하지 않는다. 페이지 구조가 바뀌었는지 확인한다"
        )
    holdings_url = routes["holdings_url"].format(
        isin=isin, date=as_of.replace("-", ".")
    )
    listing, holdings_source = _snapshot(
        holdings_url, asset_id=asset_id,
        label=f"issuer-holdings-{as_of.replace('-', '')}",
        note="운용사 구성종목(PDF) 원문 — holdings CSV 의 출처",
    )
    rows, declared = parse_holdings_rows(listing["body"])
    if declared is None:
        fail(f"{asset_id}: 구성종목 표에 운용사가 선언한 행수가 없다 — 표 구조가 바뀌었다")
    if declared != len(rows):
        fail(
            f"{asset_id}: 운용사가 선언한 {declared}행 중 {len(rows)}행만 왔다 — "
            "표가 잘렸다. 일부만 저장하지 않는다"
        )
    holdings, excluded, errors = build_holdings(asset_id, rows, as_of=as_of)
    if errors:
        fail(f"{asset_id}: " + "; ".join(errors[:5]))
    retrieved_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    path = write_holdings(
        asset, holdings, excluded, as_of=as_of, retrieved_at=retrieved_at,
        sources={"구성": holdings_source, "상품": product_source},
        url=holdings_url,
    )
    issuer_facts = {
        "schema_version": ISSUER_SCHEMA,
        "asset_id": asset_id,
        "issuer": issuer,
        "isin": isin,
        "collected_at": retrieved_at,
        "as_of": as_of,
        "nav": facts["nav"],
        "nav_currency": asset["currency"],
        "total_fee_pct": facts["total_fee_pct"],
        "hedge_statement": facts["hedge_statement"],
        "holdings_path": str(path.relative_to(ROOT)),
        "holdings_count": len(holdings),
        "excluded_rows": [
            {
                "issuer_code": row["issuer_code"],
                "holding_name": row["holding_name"],
                "weight_pct": row["weight_pct"],
                "reason": row["reason"],
            }
            for row in excluded
        ],
        "unresolved": [
            key for key in ("nav", "total_fee_pct", "hedge_statement")
            if facts[key] is None
        ],
        "sources": {"product_page": product_source, "holdings": holdings_source},
    }
    write_json(issuer_path(asset_id), issuer_facts)
    return issuer_facts


def render(facts: dict[str, Any]) -> None:
    print(
        f"{facts['asset_id']} 기준일 {facts['as_of']} · 구성 {facts['holdings_count']}종목 · "
        f"기준가 {facts['nav']} {facts['nav_currency']} · 총보수 {facts['total_fee_pct']}%"
    )
    print(f"환헤지 문구: {facts['hedge_statement']}")
    for row in facts["excluded_rows"]:
        print(f"제외: {row['issuer_code']} {row['holding_name']} {row['weight_pct']}%")
    if facts["unresolved"]:
        print("못 읽은 값: " + ", ".join(facts["unresolved"]))
    print(f"저장: {facts['holdings_path']}")


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python3.13 -m collectors.kr_etf_holdings",
        description="운용사 원문 기반 한국 ETF 구성종목 수집 (지원: MIRAE_TIGER)",
    )
    parser.add_argument("asset_id", help="0053L0")
    parser.add_argument("--show", action="store_true",
                        help="수집하지 않고 저장된 운용사 사실만 표시")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.show:
        path = issuer_path(args.asset_id)
        if not path.exists():
            fail(f"{path.relative_to(ROOT)} 가 없다. 먼저 수집한다")
        facts = json.loads(path.read_text(encoding="utf-8"))
    else:
        facts = collect(args.asset_id)
    if args.json:
        print(json.dumps(facts, ensure_ascii=False, indent=2))
    else:
        render(facts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
