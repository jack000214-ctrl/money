#!/usr/bin/env python3
"""SEC EDGAR 원문 수집기 (미국 상장사).

DART 수집기와 같은 역할을 미국 시장에서 한다. 인증키는 필요 없지만
SEC 는 User-Agent 에 연락처를 요구한다 (.env 의 SEC_USER_AGENT).

XBRL 태그는 회사·기간마다 다르게 쓰이므로 후보를 여러 개 시도한다.
찾지 못하면 추측하지 않고 UNKNOWN 으로 남긴다.
"""

import argparse
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn

ROOT = Path(__file__).resolve().parent.parent
from calculators.contracts import (
    SCHEMA_VERSION,
    ContractError,
    canonical_payload_hash,
    fundamentals_id_for,
    load_raw_snapshot,
    save_validated_fundamentals,
)

RAW_DIR = ROOT / "data" / "raw" / "edgar"
CACHE_DIR = ROOT / "data" / "cache"
FUNDAMENTALS_DIR = ROOT / "knowledge" / "fundamentals"
FILING_DIR = ROOT / "knowledge" / "filings"
CACHE_TTL_SECONDS = 24 * 60 * 60

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik}.json"
CONCEPT = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/us-gaap/{tag}.json"

REVENUE_TAGS = [
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "SalesRevenueNet",
]
OPINC_TAGS = ["OperatingIncomeLoss"]
CASH_TAGS = ["CashAndCashEquivalentsAtCarryingValue"]
SHORT_INV_TAGS = ["ShortTermInvestments", "MarketableSecuritiesCurrent",
                  "AvailableForSaleSecuritiesDebtSecuritiesCurrent"]
OCF_TAGS = ["NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"]
# 장기차입금 태그는 회사·시기별로 갈린다. LongTermDebtNoncurrent 만 보면
# 테슬라처럼 2014년에서 멈춘 태그를 최신값으로 착각한다. LongTermDebt 를
# 먼저 시도하고, 채택한 값의 기준일을 반드시 함께 기록한다.
DEBT_TAGS = ["LongTermDebt", "LongTermDebtNoncurrent"]
CAPEX_TAGS = ["PaymentsToAcquirePropertyPlantAndEquipment",
              "PaymentsToAcquireProductiveAssets"]
INCOME_QUARTERS = 8   # 전년 동기 비교에 필요한 최소 분기 수
SHARES_TAGS = ["WeightedAverageNumberOfDilutedSharesOutstanding",
               "WeightedAverageNumberOfDilutedSharesOutstandingBasic"]


def fail(msg) -> NoReturn:
    print(f"오류: {msg}", file=sys.stderr)
    sys.exit(1)


def user_agent():
    ua = os.environ.get("SEC_USER_AGENT")
    if not ua:
        env = ROOT / ".env"
        if env.exists():
            for line in env.read_text(encoding="utf-8").splitlines():
                if line.startswith("SEC_USER_AGENT="):
                    ua = line.split("=", 1)[1].strip()
    if not ua:
        fail("SEC_USER_AGENT 가 없다. .env 에 '이름 이메일' 형식으로 넣는다. "
             "SEC 는 연락처 없는 요청을 차단한다.")
    return ua


def get(url, ua, tolerate_404=False):
    req = urllib.request.Request(url, headers={
        "User-Agent": ua, "Accept-Encoding": "gzip, deflate", "Host": url.split("/")[2]})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    import gzip
                    raw = gzip.decompress(raw)
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404 and tolerate_404:
                return None
            if e.code == 429 and attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            fail(f"HTTP {e.code} — {url}")
        except urllib.error.URLError as e:
            fail(f"연결 실패 — {e.reason}")
    return None


def save_raw(name, payload):
    stamp = datetime.now(timezone.utc)
    digest = canonical_payload_hash(payload)
    out = RAW_DIR / "objects"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{digest}.json"
    if path.exists():
        load_raw_snapshot(
            path,
            expected_digest=digest,
            expected_source="sec.gov/edgar",
        )
        return path
    document = {
        "fetched_at": stamp.isoformat(timespec="seconds"),
        "source": "sec.gov/edgar",
        "request_name": name,
        "payload_sha256": digest,
        "payload": payload,
    }
    temporary_name = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=out, delete=False
        ) as temporary:
            json.dump(document, temporary, ensure_ascii=False, indent=2)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_name = temporary.name
        os.replace(temporary_name, path)
    finally:
        if temporary_name and Path(temporary_name).exists():
            Path(temporary_name).unlink()
    return path


def save_json(path, payload):
    """구조화 산출물을 원자적으로 저장한다."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False
        ) as temporary:
            json.dump(payload, temporary, ensure_ascii=False, indent=2, allow_nan=False)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_name = temporary.name
        os.replace(temporary_name, path)
    finally:
        if temporary_name and Path(temporary_name).exists():
            Path(temporary_name).unlink()
    return path


def ticker_map(ua, refresh=False):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / "edgar_tickers.json"
    cache_is_fresh = (
        cache.exists()
        and time.time() - cache.stat().st_mtime <= CACHE_TTL_SECONDS
    )
    if cache_is_fresh and not refresh:
        return json.loads(cache.read_text(encoding="utf-8"))
    data = get(TICKERS_URL, ua)
    m = {v["ticker"].upper(): {"cik": str(v["cik_str"]).zfill(10), "name": v["title"]}
         for v in data.values()}
    save_json(cache, m)
    return m


def resolve(ticker, ua, refresh=False):
    m = ticker_map(ua, refresh)
    t = ticker.upper()
    if t not in m and not refresh:
        m = ticker_map(ua, refresh=True)
    if t not in m:
        fail(f"티커 {t} 를 EDGAR 에서 찾을 수 없다")
    return m[t]


def concept(cik, tags, ua, unit="USD"):
    """회사별 태그 교체를 고려해 가장 최근 사실이 있는 후보를 고른다."""
    best_tag, best_points, best_end = None, [], ""
    for tag in tags:
        data = get(CONCEPT.format(cik=cik, tag=tag), ua, tolerate_404=True)
        time.sleep(0.12)  # SEC 권장 10 req/s 이하
        points = (data or {}).get("units", {}).get(unit)
        if not points:
            continue
        latest = max((p.get("end") or "") for p in points)
        if latest > best_end:
            best_tag, best_points, best_end = tag, points, latest
    return best_tag, best_points


def as_of_filter(points, as_of):
    """as_of까지 제출된 XBRL 사실만 남겨 미래정보 편향을 막는다."""
    if not as_of:
        return points
    return [p for p in points if (p.get("filed") or "9999-99-99") <= as_of]


def quarterly(points):
    """10-Q/10-K 중 3개월 구간만 골라 최신순으로 돌려준다."""
    out = {}
    for p in points:
        if not p.get("start") or not p.get("end") or p.get("form") not in ("10-Q", "10-K"):
            continue
        days = (datetime.fromisoformat(p["end"]) - datetime.fromisoformat(p["start"])).days
        if not 80 <= days <= 100:  # 3개월 구간
            continue
        out[p["end"]] = p  # 같은 종료일이면 나중 제출본이 이긴다
    return sorted(out.values(), key=lambda p: p["end"], reverse=True)


def instant(points):
    out = {}
    for p in points:
        if p.get("start") or not p.get("end"):
            continue
        out[p["end"]] = p
    return sorted(out.values(), key=lambda p: p["end"], reverse=True)


def fiscal_quarters(points):
    """SEC duration fact에서 연속된 실제 분기값을 복원한다.

    현금흐름표의 10-Q 값은 대개 연초 누계다. 단순히 80~100일 구간 네 개를
    더하면 Q4가 빠지거나 서로 다른 연도의 Q1만 합칠 수 있다. FY/FP와
    제출일을 기준으로 최신 사실을 고른 뒤 누계 차감으로 Q1~Q4를 만든다.
    """
    selected = {}
    for point in points:
        fy, fp = point.get("fy"), point.get("fp")
        if not fy or fp not in {"Q1", "Q2", "Q3", "FY"}:
            continue
        if not point.get("start") or not point.get("end"):
            continue
        if point.get("form") not in ("10-Q", "10-K"):
            continue
        days = (datetime.fromisoformat(point["end"]) -
                datetime.fromisoformat(point["start"])).days
        wanted = (
            80 <= days <= 110 if fp == "Q1" else
            160 <= days <= 210 if fp == "Q2" else
            250 <= days <= 310 if fp == "Q3" else
            340 <= days <= 390
        )
        if not wanted:
            continue
        key = (int(fy), fp)
        filed = point.get("filed") or ""
        if key not in selected or filed >= (selected[key].get("filed") or ""):
            selected[key] = point

    out = []
    for fy in sorted({key[0] for key in selected}):
        q1 = selected.get((fy, "Q1"))
        q2 = selected.get((fy, "Q2"))
        q3 = selected.get((fy, "Q3"))
        annual = selected.get((fy, "FY"))
        values = {}
        if q1:
            values[1] = q1["val"]
        if q1 and q2:
            values[2] = q2["val"] - q1["val"]
        if q2 and q3:
            values[3] = q3["val"] - q2["val"]
        if q3 and annual:
            values[4] = annual["val"] - q3["val"]
        sources = {1: q1, 2: q2, 3: q3, 4: annual}
        for quarter, value in values.items():
            source = sources[quarter]
            out.append({
                "fy": fy,
                "quarter": quarter,
                "end": source["end"],
                "filed": source.get("filed"),
                "val": value,
                "derived": quarter in {2, 3, 4},
            })
    return sorted(out, key=lambda p: (p["fy"], p["quarter"]))


def trailing_four_quarters(points):
    """연속된 최신 4개 분기 합. 연속성이 없으면 UNKNOWN."""
    quarters = fiscal_quarters(points)
    if len(quarters) < 4:
        return None, []
    latest = quarters[-4:]
    indices = [p["fy"] * 4 + p["quarter"] for p in latest]
    if indices[-1] - indices[0] != 3 or len(set(indices)) != 4:
        return None, latest
    return sum(p["val"] for p in latest), latest


def cmd_resolve(a, ua):
    info = resolve(a.ticker, ua, a.refresh)
    print(f"{a.ticker.upper()} → CIK {info['cik']} ({info['name']})")


def filing_rows_from_submissions(data, *, cik, forms=None, limit=20):
    """SEC submissions 원문에서 계약된 순서 그대로 작은 제출 목록을 만든다."""
    if not isinstance(limit, int) or limit <= 0:
        raise ContractError(["filing index limit은 1 이상이어야 한다"])
    if not isinstance(cik, str) or not cik.isdigit():
        raise ContractError(["filing index CIK가 숫자 문자열이 아니다"])
    try:
        recent = data["filings"]["recent"]
    except (KeyError, TypeError) as exc:
        raise ContractError(["SEC submissions recent 구조가 없다"]) from exc
    field_names = {
        "form": "form",
        "filing_date": "filingDate",
        "report_date": "reportDate",
        "accession_number": "accessionNumber",
        "primary_document": "primaryDocument",
        "description": "primaryDocDescription",
    }
    arrays = {}
    for output_name, raw_name in field_names.items():
        values = recent.get(raw_name) if isinstance(recent, dict) else None
        if not isinstance(values, list):
            raise ContractError([f"SEC submissions recent.{raw_name} 배열이 없다"])
        arrays[output_name] = values
    count = len(arrays["form"])
    if any(len(values) != count for values in arrays.values()):
        raise ContractError(["SEC submissions recent 배열 길이가 서로 다르다"])
    wanted = {str(form).upper() for form in forms} if forms else None
    normalized_cik = str(int(cik))
    rows = []
    for index, raw_form in enumerate(arrays["form"]):
        form = str(raw_form).upper()
        if wanted and form not in wanted:
            continue
        accession = arrays["accession_number"][index]
        primary_document = arrays["primary_document"][index]
        if not isinstance(accession, str) or not isinstance(
            primary_document, str
        ):
            raise ContractError(["SEC submissions accession 또는 문서명이 문자열이 아니다"])
        description = arrays["description"][index]
        rows.append(
            {
                "filing_date": arrays["filing_date"][index],
                "report_date": arrays["report_date"][index],
                "form": form,
                "accession_number": accession,
                "primary_document": primary_document,
                "description": description or None,
                "document_url": (
                    "https://www.sec.gov/Archives/edgar/data/"
                    f"{normalized_cik}/{accession.replace('-', '')}/"
                    f"{primary_document}"
                ),
            }
        )
        if len(rows) >= limit:
            break
    return rows


def cmd_filings(a, ua):
    if a.limit <= 0:
        fail("--limit은 1 이상이어야 한다")
    info = resolve(a.ticker, ua)
    data = get(SUBMISSIONS.format(cik=info["cik"]), ua)
    path = save_raw(f"filings_{a.ticker.upper()}", data)
    wanted = {f.upper() for f in a.forms} if a.forms else None
    try:
        filings = filing_rows_from_submissions(
            data,
            cik=info["cik"],
            forms=wanted,
            limit=a.limit,
        )
    except ContractError as exc:
        fail("; ".join(exc.errors))
    snapshot = {
        "schema_version": "filing-index/1.0.0",
        "asset_id": a.ticker.upper(),
        "cik": info["cik"],
        "collected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "SEC_EDGAR_SUBMISSIONS",
        "forms_filter": sorted(wanted) if wanted else None,
        "raw_payload_sha256": path.stem,
        "raw_locator": str(path.relative_to(ROOT)),
        "filings": filings,
    }
    index_path = save_json(FILING_DIR / f"{a.ticker.upper()}.json", snapshot)

    print(f"{info['name']} ({a.ticker.upper()}) — CIK {info['cik']}")
    print(f"원본 저장: {path.relative_to(ROOT)}")
    print(f"색인 저장: {index_path.relative_to(ROOT)}")
    print("-" * 84)
    for filing in filings:
        print(
            f"{filing['filing_date']}  {filing['form']:<10}"
            f"{(filing['description'] or '')[:38]:<40}"
            f"{filing['accession_number']}"
        )


def cmd_quarters(a, ua):
    info = resolve(a.ticker, ua)
    rev_tag, rev = concept(info["cik"], REVENUE_TAGS, ua)
    op_tag, op = concept(info["cik"], OPINC_TAGS, ua)
    if not rev:
        fail(f"{a.ticker}: 매출 태그를 찾지 못했다 (시도: {', '.join(REVENUE_TAGS)})")

    save_raw(f"concept_{a.ticker.upper()}_revenue", {"tag": rev_tag, "units": rev})
    if op:
        save_raw(f"concept_{a.ticker.upper()}_opinc", {"tag": op_tag, "units": op})

    rq = quarterly(rev)[: a.limit]
    oq = {p["end"]: p["val"] for p in quarterly(op)} if op else {}

    print(f"{info['name']} ({a.ticker.upper()}) 분기 실적 — 단위 백만 USD")
    print(f"매출 태그: {rev_tag}" + (f" / 영업이익 태그: {op_tag}" if op else ""))
    print("=" * 76)
    print(f"{'기간':<26}{'매출':>14}{'영업이익':>14}{'OPM':>9}{'YoY':>10}")
    print("-" * 76)
    by_end = {p["end"]: p["val"] for p in rq}
    for p in rq:
        v = p["val"]
        o = oq.get(p["end"])
        prior = next((by_end[e] for e in by_end
                      if e[5:] == p["end"][5:] and e[:4] == str(int(p["end"][:4]) - 1)), None)
        yoy = f"{(v/prior-1)*100:>+9.1f}%" if prior else f"{'—':>10}"
        opm = f"{o/v*100:>8.2f}%" if o and v else f"{'—':>9}"
        opv = f"{o/1e6:>14,.0f}" if o else f"{'—':>14}"
        print(f"{p['start']}~{p['end']:<15}{v/1e6:>14,.0f}{opv}{opm}{yoy}")
    print("\n※ 같은 종료일에 정정 제출이 있으면 최신 제출본을 쓴다.")


def cmd_balance(a, ua):
    info = resolve(a.ticker, ua)
    print(f"{info['name']} ({a.ticker.upper()}) 재무 상태 — 단위 백만 USD")
    print("=" * 60)
    raw = {}
    for label, tags in [("현금성자산", CASH_TAGS), ("단기투자자산", SHORT_INV_TAGS),
                        ("장기차입금", DEBT_TAGS)]:
        tag, pts = concept(info["cik"], tags, ua)
        raw[label] = {"tag": tag, "units": pts}
        if not pts:
            print(f"{label:<16}{'UNKNOWN':>14}   (태그 없음)")
            continue
        p = instant(pts)[0]
        print(f"{label:<16}{p['val']/1e6:>14,.0f}   {p['end']}  [{tag}]")
    tag, pts = concept(info["cik"], OCF_TAGS, ua)
    raw["영업현금흐름"] = {"tag": tag, "units": pts}
    if pts:
        value, q = trailing_four_quarters(pts)
        if value is not None:
            period = f"FY{q[0]['fy']} Q{q[0]['quarter']}~FY{q[-1]['fy']} Q{q[-1]['quarter']}"
            print(f"{'영업현금흐름':<16}{value/1e6:>14,.0f}   최근 연속 4분기 ({period})")
        else:
            print(f"{'영업현금흐름':<16}{'UNKNOWN':>14}   연속 4분기 부족")
    path = save_raw(f"balance_{a.ticker.upper()}", raw)
    print(f"\n원본 저장: {path.relative_to(ROOT)}")


def cmd_fundamentals(a, ua):
    """역산 DCF 입력을 EDGAR 원문에서 만들어 스냅샷으로 남긴다."""
    info = resolve(a.ticker, ua)
    cik, ticker = info["cik"], a.ticker.upper()
    raw = {"_instrument": {"instrument_id": ticker, "cik": cik}}

    def flow(label, tags):
        tag, pts = concept(cik, tags, ua)
        pts = as_of_filter(pts, a.as_of)
        raw[label] = {"tag": tag, "units": pts}
        if not pts:
            fail(f"{label}: 태그를 찾지 못했다 ({', '.join(tags)})")
        total, quarters = trailing_four_quarters(pts)
        if total is None:
            fail(f"{label}: 연속 4분기를 확보하지 못했다. 추정하지 않는다")
        return tag, total, quarters

    ocf_tag, ocf_ttm, ocf_q = flow("영업현금흐름", OCF_TAGS)
    capex_tag, capex_ttm, capex_q = flow("capex", CAPEX_TAGS)

    # 손익계산서 분기값. 화면과 분석이 같은 원문을 보게 하려고 함께 담는다.
    #
    # DCF 필수값과 표시용 손익을 구분한다. 찾지 못한 표시값은 근사하지 않고
    # null과 누락 사유를 남긴다.
    REQUIRED_INCOME = {"revenue", "net_income"}
    income_series = {}
    income_missing = {}
    for key, tags in (("revenue", REVENUE_TAGS), ("gross_profit", ["GrossProfit"]),
                      ("rnd", ["ResearchAndDevelopmentExpense"]),
                      ("sgna", ["SellingGeneralAndAdministrativeExpense"]),
                      ("operating_income", OPINC_TAGS),
                      ("net_income", ["NetIncomeLoss"])):
        tag, pts = concept(cik, tags, ua)
        pts = as_of_filter(pts, a.as_of)
        raw[f"손익_{key}"] = {"tag": tag, "units": pts}

        reason = None
        total = None
        recent: list = []
        if not pts:
            reason = f"태그를 찾지 못했다 (시도: {', '.join(tags)})"
        else:
            total, _ = trailing_four_quarters(pts)
            if total is None:
                reason = "연속 4분기를 확보하지 못했다"
            else:
                # 전년 동기 비교에는 8분기가 필요하다. TTM 창(4분기)만 담으면
                # rows[0] 이 1년 전이 아니라 3분기 전이라 YoY 가 조용히 틀린다.
                recent = fiscal_quarters(pts)[-INCOME_QUARTERS:]
                if len(recent) < INCOME_QUARTERS:
                    reason = (f"전년 동기 비교에 필요한 {INCOME_QUARTERS}분기를 "
                              f"확보하지 못했다 ({len(recent)}개)")

        if reason:
            if key in REQUIRED_INCOME:
                fail(f"손익 {key}: {reason}")
            income_missing[key] = reason
            income_series[key] = {"tag": tag, "ttm": None, "quarters": [],
                                  "unknown_reason": reason}
            continue
        income_series[key] = {"tag": tag, "ttm": float(total), "quarters": recent}

    if income_missing:
        for key, reason in income_missing.items():
            print(f"※ 손익 {key}: {reason} → UNKNOWN 으로 남긴다 "
                  "(DCF 입력에는 쓰이지 않는 표시용 항목이다)", file=sys.stderr)

    if [(q["fy"], q["quarter"]) for q in ocf_q] != [(q["fy"], q["quarter"]) for q in capex_q]:
        fail("영업현금흐름과 capex 의 분기 구간이 다르다. 서로 다른 기간을 "
             "빼면 FCF 가 아니다")

    def point(label, tags, unit="USD"):
        tag, pts = concept(cik, tags, ua, unit=unit)
        pts = as_of_filter(pts, a.as_of)
        raw[label] = {"tag": tag, "units": pts}
        if not pts:
            return None, None, None
        p = instant(pts)[0] if unit == "USD" and not pts[0].get("start") else None
        if p is None:
            rows = instant(pts)
            if rows:
                p = rows[0]
        if p is None:
            return tag, None, None
        return tag, p["val"], p["end"]

    cash_tag, cash, cash_at = point("현금성자산", CASH_TAGS)
    inv_tag, short_inv, inv_at = point("단기투자자산", SHORT_INV_TAGS)
    debt_tag, debt, debt_at = point("장기차입금", DEBT_TAGS)

    shares_tag, shares_pts = concept(cik, SHARES_TAGS, ua, unit="shares")
    shares_pts = as_of_filter(shares_pts, a.as_of)
    raw["희석주식수"] = {"tag": shares_tag, "units": shares_pts}
    shares_q = quarterly(shares_pts) if shares_pts else []
    if not shares_q:
        fail("희석주식수를 찾지 못했다")
    shares, shares_at = shares_q[0]["val"], shares_q[0]["end"]

    for label, value in (("현금성자산", cash), ("단기투자자산", short_inv),
                         ("장기차입금", debt)):
        if value is None:
            fail(f"{label}: 값을 찾지 못했다. 0 으로 때우면 순부채가 틀린다")

    period_end = ocf_q[-1]["end"]
    # 차입금 기준일이 손익 기준일과 크게 어긋나면 순부채가 다른 시점 값이다.
    for label, at in (("현금성자산", cash_at), ("단기투자자산", inv_at),
                      ("장기차입금", debt_at)):
        if at != period_end:
            print(f"⚠ {label} 기준일 {at} 이 손익 기준일 {period_end} 과 다르다",
                  file=sys.stderr)

    values = {
        "ocf_ttm": float(ocf_ttm),
        "capex_ttm": float(capex_ttm),
        "fcf_ttm": float(ocf_ttm - capex_ttm),
        "shares_diluted": float(shares),
        "cash_and_equivalents": float(cash),
        "short_term_investments": float(short_inv),
        "long_term_debt": float(debt),
        "net_debt": float(debt - cash - short_inv),
    }
    raw_path = save_raw(f"fundamentals_{ticker}", raw)

    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "instrument_id": ticker,
        "company_name": info["name"],
        "cik": cik,
        "currency": "USD",
        "period_end": period_end,
        "created_at": datetime.now(timezone.utc).astimezone().isoformat(
            timespec="seconds"),
        "tags": {"ocf": ocf_tag, "capex": capex_tag, "shares": shares_tag,
                 "cash": cash_tag, "short_term_investments": inv_tag,
                 "long_term_debt": debt_tag},
        "as_of": {"cash": cash_at, "short_term_investments": inv_at,
                  "long_term_debt": debt_at, "shares": shares_at},
        "quarters": {
            "ocf": [{k: q[k] for k in ("fy", "quarter", "end", "filed", "val",
                                       "derived")} for q in ocf_q],
            "capex": [{k: q[k] for k in ("fy", "quarter", "end", "filed", "val",
                                         "derived")} for q in capex_q],
        },
        "values": values,
        "income_statement": {
            key: {
                "tag": series["tag"],
                "ttm": series["ttm"],
                "quarters": [
                    {k: q[k] for k in ("fy", "quarter", "end", "filed", "val", "derived")}
                    for q in series["quarters"]
                ],
            }
            for key, series in income_series.items()
        },
        "raw_locator": str(raw_path.relative_to(ROOT)),
        "raw_payload_sha256": raw_path.stem,
    }
    snapshot["fundamentals_id"] = fundamentals_id_for(snapshot)

    try:
        path = save_validated_fundamentals(a.out_dir, snapshot)
    except ContractError as exc:
        for message in exc.errors:
            print(f"INVALID: {message}", file=sys.stderr)
        return 1

    print(f"{info['name']} ({ticker}) — 기준 {period_end} / 단위 백만 USD")
    print("=" * 66)
    print(f"{'영업현금흐름 TTM':<20}{ocf_ttm/1e6:>14,.0f}   [{ocf_tag}]")
    print(f"{'capex TTM':<20}{capex_ttm/1e6:>14,.0f}   [{capex_tag}]")
    print(f"{'FCF TTM':<20}{values['fcf_ttm']/1e6:>14,.0f}   = 영업현금흐름 − capex")
    print(f"{'희석주식수':<20}{shares/1e6:>14,.0f}M  {shares_at}")
    print(f"{'순부채':<20}{values['net_debt']/1e6:>14,.0f}   "
          f"(차입 {debt/1e6:,.0f} − 현금 {cash/1e6:,.0f} − 단기투자 {short_inv/1e6:,.0f})")
    print("\n[ 분기 복원 — 누계 차감 ]")
    for o, c in zip(ocf_q, capex_q):
        mark = "차감" if o["derived"] else "직접"
        print(f"  FY{o['fy']} Q{o['quarter']}  {o['end']}  "
              f"영업 {o['val']/1e6:>9,.0f}  capex {c['val']/1e6:>9,.0f}  "
              f"FCF {(o['val']-c['val'])/1e6:>9,.0f}  ({mark})")
    print(f"\nfundamentals_id  {snapshot['fundamentals_id']}")
    def show(target):
        # --out-dir 이 저장소 밖일 수 있다 (재현 테스트는 임시 디렉터리를 쓴다).
        try:
            return target.relative_to(ROOT)
        except ValueError:
            return target

    print(f"저장: {show(path)}")
    print(f"원본: {show(raw_path)}")
    print("\n※ reverse_dcf 에 --fundamentals 로 넘기면 이 지문이 계산 산출물에 박힌다.")
    return 0


def main():
    p = argparse.ArgumentParser(description="SEC EDGAR 원문 수집")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("resolve", help="티커 → CIK")
    r.add_argument("ticker")
    r.add_argument("--refresh", action="store_true")

    f = sub.add_parser("filings", help="제출 목록")
    f.add_argument("ticker")
    f.add_argument("--forms", nargs="*", help="예: 10-Q 10-K 8-K")
    f.add_argument("--limit", type=int, default=20)

    q = sub.add_parser("quarters", help="분기 매출·영업이익")
    q.add_argument("ticker")
    q.add_argument("--limit", type=int, default=12)

    b = sub.add_parser("balance", help="현금·차입금 요약")
    b.add_argument("ticker")

    fd = sub.add_parser("fundamentals",
                        help="역산 DCF 입력 스냅샷 (TTM FCF·주식수·순부채)")
    fd.add_argument("ticker")
    fd.add_argument("--out-dir", default=str(FUNDAMENTALS_DIR))
    fd.add_argument("--as-of",
                    help="YYYY-MM-DD. 이 날짜까지 제출된 사실만 쓴다 (재현 테스트용). "
                         "생략하면 최신 전부")

    a = p.parse_args()
    ua = user_agent()
    return {"resolve": cmd_resolve, "filings": cmd_filings, "quarters": cmd_quarters,
            "balance": cmd_balance,
            "fundamentals": cmd_fundamentals}[a.cmd](a, ua)


if __name__ == "__main__":
    raise SystemExit(main() or 0)
