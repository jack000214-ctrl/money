#!/usr/bin/env python3
"""SEC EDGAR 원문 수집기 (미국 상장사).

DART 수집기와 같은 역할을 미국 시장에서 한다. 인증키는 필요 없지만
SEC 는 User-Agent 에 연락처를 요구한다 (.env 의 SEC_USER_AGENT).

XBRL 태그는 회사·기간마다 다르게 쓰이므로 후보를 여러 개 시도한다.
찾지 못하면 추측하지 않고 UNKNOWN 으로 남긴다.
"""

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw" / "edgar"
CACHE_DIR = ROOT / "data" / "cache"

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
DEBT_TAGS = ["LongTermDebtNoncurrent", "LongTermDebt"]


def fail(msg):
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
    canonical = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        allow_nan=False,
    )
    out = RAW_DIR / stamp.strftime("%Y-%m-%d")
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{name}_{stamp.strftime('%H%M%S')}.json"
    path.write_text(json.dumps(
        {"fetched_at": stamp.isoformat(timespec="seconds"),
         "source": "sec.gov/edgar",
         "payload_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
         "payload": payload},
        ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def ticker_map(ua, refresh=False):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / "edgar_tickers.json"
    if cache.exists() and not refresh:
        return json.loads(cache.read_text(encoding="utf-8"))
    data = get(TICKERS_URL, ua)
    m = {v["ticker"].upper(): {"cik": str(v["cik_str"]).zfill(10), "name": v["title"]}
         for v in data.values()}
    cache.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    return m


def resolve(ticker, ua, refresh=False):
    m = ticker_map(ua, refresh)
    t = ticker.upper()
    if t not in m:
        fail(f"티커 {t} 를 EDGAR 에서 찾을 수 없다")
    return m[t]


def concept(cik, tags, ua):
    """태그 후보를 순서대로 시도한다. 회사마다 쓰는 태그가 다르다."""
    for tag in tags:
        data = get(CONCEPT.format(cik=cik, tag=tag), ua, tolerate_404=True)
        if data and data.get("units", {}).get("USD"):
            return tag, data["units"]["USD"]
        time.sleep(0.12)  # SEC 권장 10 req/s 이하
    return None, []


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


def cmd_filings(a, ua):
    info = resolve(a.ticker, ua)
    data = get(SUBMISSIONS.format(cik=info["cik"]), ua)
    path = save_raw(f"filings_{a.ticker.upper()}", data)
    r = data["filings"]["recent"]
    wanted = {f.upper() for f in a.forms} if a.forms else None

    print(f"{info['name']} ({a.ticker.upper()}) — CIK {info['cik']}")
    print(f"원본 저장: {path.relative_to(ROOT)}")
    print("-" * 84)
    shown = 0
    for i, form in enumerate(r["form"]):
        if wanted and form.upper() not in wanted:
            continue
        print(f"{r['filingDate'][i]}  {form:<10}{(r['primaryDocDescription'][i] or '')[:38]:<40}"
              f"{r['accessionNumber'][i]}")
        shown += 1
        if shown >= a.limit:
            break


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


def cmd_runway(a, ua):
    """현금 소진 기업의 잔여 개월수. 임상단계 바이오 판단의 핵심 지표."""
    info = resolve(a.ticker, ua)
    _, cash = concept(info["cik"], CASH_TAGS, ua)
    _, inv = concept(info["cik"], SHORT_INV_TAGS, ua)
    _, ocf = concept(info["cik"], OCF_TAGS, ua)
    if not cash:
        fail(f"{a.ticker}: 현금 태그를 찾지 못했다")

    c = instant(cash)[0]
    i = next((x for x in instant(inv) if x["end"] == c["end"]), None) if inv else None
    liquid = c["val"] + (i["val"] if i else 0)

    print(f"{info['name']} ({a.ticker.upper()}) 런웨이 — 단위 백만 USD")
    print("=" * 66)
    print(f"기준일             {c['end']}")
    print(f"현금성자산         {c['val']/1e6:>12,.0f}")
    if i:
        print(f"단기투자자산       {i['val']/1e6:>12,.0f}")
    print(f"유동성 합계        {liquid/1e6:>12,.0f}")

    if not ocf:
        print("\n영업현금흐름 태그를 찾지 못해 소진율을 계산할 수 없다.")
        return
    q = fiscal_quarters(ocf)[-4:]
    if not q:
        print("\n3개월 구간의 영업현금흐름이 없어 소진율을 계산할 수 없다.")
        return

    print("\n최근 분기 영업현금흐름 (누계 차감 복원):")
    for p in q:
        suffix = " DERIVED" if p["derived"] else ""
        print(f"  FY{p['fy']} Q{p['quarter']}  {p['val']/1e6:>+12,.0f}{suffix}")

    latest = q[-1]["val"]
    avg = sum(p["val"] for p in q) / len(q)
    print(f"  {'평균':<24}{avg/1e6:>+12,.0f}")

    if latest >= 0:
        print("\n최근 분기 영업현금흐름이 플러스다 — 런웨이 개념이 적용되지 않는다.")
        return

    def show(label, burn_q):
        months = liquid / (-burn_q / 3)
        print(f"  {label:<22}{months:>6.1f}개월  (분기 소진 {burn_q/1e6:>+8,.0f})")
        return months

    print(f"\n런웨이 = 유동성 {liquid/1e6:,.0f} ÷ 월 소진")
    m_latest = show("최근 분기 기준", latest)
    if avg < 0:
        show("평균 기준", avg)

    worst = min(m_latest, liquid / (-avg / 3) if avg < 0 else m_latest)
    if worst < 12:
        print("\n⚠ 12개월 미만 — 유상증자로 인한 희석 위험이 높다.")
    elif worst < 24:
        print("\n⚠ 24개월 미만 — 자금조달 시점을 주시한다.")

    if len(q) >= 2 and q[-1]["val"] > q[-2]["val"]:
        print(f"↗ 소진율 개선: FY{q[-2]['fy']} Q{q[-2]['quarter']} "
              f"{q[-2]['val']/1e6:+,.0f} → FY{q[-1]['fy']} Q{q[-1]['quarter']} "
              f"{q[-1]['val']/1e6:+,.0f}")

    print("\n※ 영업현금흐름 기준이며 투자·재무활동은 제외했다.")
    print("※ 파이프라인 진행과 파트너십 수령액에 따라 소진율은 크게 달라진다.")


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

    w = sub.add_parser("runway", help="현금 런웨이 (소진 기업용)")
    w.add_argument("ticker")

    b = sub.add_parser("balance", help="현금·차입금 요약")
    b.add_argument("ticker")

    a = p.parse_args()
    ua = user_agent()
    {"resolve": cmd_resolve, "filings": cmd_filings, "quarters": cmd_quarters,
     "runway": cmd_runway, "balance": cmd_balance}[a.cmd](a, ua)


if __name__ == "__main__":
    main()
