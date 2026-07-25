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
    out = RAW_DIR / stamp.strftime("%Y-%m-%d")
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{name}_{stamp.strftime('%H%M%S')}.json"
    path.write_text(json.dumps(
        {"fetched_at": stamp.isoformat(timespec="seconds"),
         "source": "sec.gov/edgar", "payload": payload},
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
    q = quarterly(ocf)[:4]
    if not q:
        print("\n3개월 구간의 영업현금흐름이 없어 소진율을 계산할 수 없다.")
        return

    # 10-Q 의 현금흐름표는 연초부터의 누계다. 따라서 3개월 구간은 대개 1분기에만 존재하고
    # 아래 목록은 "최근 4개 분기"가 아니라 "구간이 잡힌 연도별 분기"가 된다.
    same_quarter = len({p["end"][5:] for p in q}) == 1
    quarter_no = (int(q[0]["end"][5:7]) + 2) // 3  # 종료 월 → 분기
    header = f"연도별 {quarter_no}분기" if same_quarter else "분기"
    print(f"\n{header} 영업현금흐름 (3개월 구간):")
    for p in q:
        print(f"  {p['start']}~{p['end']}  {p['val']/1e6:>+12,.0f}")

    latest = q[0]["val"]
    avg = sum(p["val"] for p in q) / len(q)
    print(f"  {'평균':<24}{avg/1e6:>+12,.0f}")
    if same_quarter:
        print(f"  ※ 10-Q 현금흐름표는 누계라 3개월 구간이 {header}에만 잡힌다.")
        print(f"     따라서 위 '평균'은 연도 간 비교이지 최근 4분기 평균이 아니다.")

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

    if len(q) >= 2 and q[0]["val"] > q[1]["val"]:
        print(f"↗ 소진율 개선: {q[1]['end'][:4]} {q[1]['val']/1e6:+,.0f} "
              f"→ {q[0]['end'][:4]} {q[0]['val']/1e6:+,.0f}")

    print("\n※ 영업현금흐름 기준이며 투자·재무활동은 제외했다.")
    print("※ 파이프라인 진행과 파트너십 수령액에 따라 소진율은 크게 달라진다.")


def cmd_balance(a, ua):
    info = resolve(a.ticker, ua)
    print(f"{info['name']} ({a.ticker.upper()}) 재무 상태 — 단위 백만 USD")
    print("=" * 60)
    for label, tags in [("현금성자산", CASH_TAGS), ("단기투자자산", SHORT_INV_TAGS),
                        ("장기차입금", DEBT_TAGS)]:
        tag, pts = concept(info["cik"], tags, ua)
        if not pts:
            print(f"{label:<16}{'UNKNOWN':>14}   (태그 없음)")
            continue
        p = instant(pts)[0]
        print(f"{label:<16}{p['val']/1e6:>14,.0f}   {p['end']}  [{tag}]")
    tag, pts = concept(info["cik"], OCF_TAGS, ua)
    if pts:
        q = quarterly(pts)[:4]
        if q:
            print(f"{'영업현금흐름':<16}{sum(p['val'] for p in q)/1e6:>14,.0f}   최근 4분기 합")


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
