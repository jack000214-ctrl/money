#!/usr/bin/env python3
"""동종업계 비교 — "지금 가격이 매력적인가"를 상대 기준으로 본다.

PER 18.68 이 싼지 비싼지는 절대값으로 알 수 없다. 비교 대상이 두 개 필요하다.

  1. 자기 자신의 과거 (밸류에이션 밴드)
  2. **동종업계** ← 이 스크립트

재무는 SEC EDGAR 원문(XBRL), 주가는 Yahoo. 미국 상장사 전용이다.
한국 종목은 DART 기반으로 별도 구현이 필요하다 (주식수 위치가 다르다).

TTM(최근 4분기)은 분기 보고가 3개월 구간을 주지 않는 4분기를 연간에서 역산해 만든다.
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from edgar_fetch import (CONCEPT, OPINC_TAGS, REVENUE_TAGS, resolve, user_agent)  # noqa: E402

NETINC_TAGS = ["NetIncomeLoss", "ProfitLoss"]
GROSS_TAGS = ["GrossProfit"]
# 회사마다 손익 태그가 다르다. IBM 은 OperatingIncomeLoss 를 아예 쓰지 않고
# GrossProfit / CostOfRevenue 만 태깅한다. 그래서 두 마진을 모두 보여준다.
FORMS = ("10-Q", "10-K", "20-F")  # 20-F: SAP 같은 외국 발행인
SHARES_TAGS = ["EntityCommonStockSharesOutstanding"]
EQUITY_TAGS = ["StockholdersEquity",
               "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"]
CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{s}?range=5d&interval=1d"


def get_json(url, headers, timeout=40, tolerate_404=False):
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404 and tolerate_404:
            return None
        raise


def concept_ns(cik, tags, ua, ns="us-gaap"):
    for tag in tags:
        url = CONCEPT.format(cik=cik, tag=tag).replace("/us-gaap/", f"/{ns}/")
        data = get_json(url, {"User-Agent": ua}, tolerate_404=True)
        time.sleep(0.12)
        if not data:
            continue
        units = data.get("units", {})
        key = "USD" if "USD" in units else ("shares" if "shares" in units else None)
        if key and units[key]:
            return tag, units[key]
    return None, []


def periods(points, lo, hi):
    out = {}
    for p in points:
        if not p.get("start") or not p.get("end") or p.get("form") not in FORMS:
            continue
        days = (datetime.fromisoformat(p["end"]) - datetime.fromisoformat(p["start"])).days
        if lo <= days <= hi:
            out[(p["start"], p["end"])] = p
    return sorted(out.values(), key=lambda p: p["end"])


def ttm(points):
    """최근 4분기 합. 4분기 구간이 없으면 연간에서 역산해 채운다."""
    q = {p["end"]: p["val"] for p in periods(points, 80, 100)}
    fy = periods(points, 350, 380)
    for f in fy:
        y_end = f["end"]
        same_year = [e for e in q if e[:4] == y_end[:4] and e <= y_end]
        if len(same_year) == 3:
            q.setdefault(y_end, f["val"] - sum(q[e] for e in same_year))
    if len(q) < 4:
        return (fy[-1]["val"], fy[-1]["end"], "FY") if fy else (None, None, None)
    last4 = sorted(q)[-4:]
    return sum(q[e] for e in last4), last4[-1], "TTM"


def latest_instant(points):
    inst = sorted((p for p in points if not p.get("start") and p.get("end")),
                  key=lambda p: p["end"])
    return (inst[-1]["val"], inst[-1]["end"]) if inst else (None, None)


def price(ticker):
    d = get_json(CHART.format(s=ticker), {"User-Agent": "Mozilla/5.0"}, timeout=25)
    m = d["chart"]["result"][0]["meta"]
    return m.get("regularMarketPrice"), m.get("currency")


def gather(ticker, ua):
    info = resolve(ticker, ua)
    cik = info["cik"]
    row = {"ticker": ticker.upper(), "name": info["name"][:22]}

    _, rev_pts = concept_ns(cik, REVENUE_TAGS, ua)
    _, op_pts = concept_ns(cik, OPINC_TAGS, ua)
    _, ni_pts = concept_ns(cik, NETINC_TAGS, ua)
    _, sh_pts = concept_ns(cik, SHARES_TAGS, ua, ns="dei")
    _, eq_pts = concept_ns(cik, EQUITY_TAGS, ua)
    _, gp_pts = concept_ns(cik, GROSS_TAGS, ua)

    row["revenue"], row["as_of"], row["basis"] = ttm(rev_pts)
    row["opinc"], _, _ = ttm(op_pts)
    row["gross"], _, _ = ttm(gp_pts)
    row["netinc"], _, _ = ttm(ni_pts)
    row["shares"], _ = latest_instant(sh_pts)
    row["equity"], _ = latest_instant(eq_pts)

    # 매출 성장률: 최근 분기 YoY
    qs = periods(rev_pts, 80, 100)
    row["growth"] = None
    if len(qs) >= 5:
        cur = qs[-1]
        prior = next((p for p in qs if p["end"][5:] == cur["end"][5:]
                      and p["end"][:4] == str(int(cur["end"][:4]) - 1)), None)
        if prior and prior["val"]:
            row["growth"] = (cur["val"] / prior["val"] - 1) * 100

    row["price"], row["currency"] = price(ticker)
    if row["price"] and row["shares"]:
        row["mcap"] = row["price"] * row["shares"]
        row["per"] = row["mcap"] / row["netinc"] if row["netinc"] and row["netinc"] > 0 else None
        row["psr"] = row["mcap"] / row["revenue"] if row["revenue"] else None
        row["pbr"] = row["mcap"] / row["equity"] if row["equity"] and row["equity"] > 0 else None
    row["opm"] = (row["opinc"] / row["revenue"] * 100
                  if row["opinc"] is not None and row["revenue"] else None)
    row["gpm"] = (row["gross"] / row["revenue"] * 100
                  if row["gross"] is not None and row["revenue"] else None)
    return row


def fmt(v, spec, dash="—", width=10):
    return f"{v:{spec}}" if v is not None else f"{dash:>{width}}"


def main():
    p = argparse.ArgumentParser(description="동종업계 재무·밸류에이션 비교 (미국 상장사)")
    p.add_argument("target", help="비교 기준 종목")
    p.add_argument("peers", nargs="+", help="비교 대상")
    a = p.parse_args()

    ua = user_agent()
    rows = []
    for t in [a.target] + a.peers:
        try:
            rows.append(gather(t, ua))
        except Exception as e:
            print(f"  {t}: 수집 실패 — {type(e).__name__} {e}", file=sys.stderr)

    print("=" * 96)
    print(f"동종업계 비교 — 기준 {a.target.upper()}   (재무: SEC EDGAR / 주가: Yahoo)")
    print("=" * 96)
    print(f"{'티커':<7}{'회사':<24}{'매출(TTM,$M)':>14}{'GPM':>8}{'OPM':>8}{'매출YoY':>9}"
          f"{'PER':>8}{'PSR':>7}{'PBR':>7}")
    print("-" * 96)
    for r in rows:
        mark = "▶ " if r["ticker"] == a.target.upper() else "  "
        print(f"{mark}{r['ticker']:<5}{r['name']:<24}"
              f"{fmt(r['revenue'] / 1e6 if r['revenue'] else None, '>14,.0f', width=14)}"
              f"{fmt(r['gpm'], '>7.1f', width=8)}{'%' if r['gpm'] is not None else ''}"
              f"{fmt(r['opm'], '>7.1f', width=8)}{'%' if r['opm'] is not None else ''}"
              f"{fmt(r['growth'], '>+8.1f', width=9)}{'%' if r['growth'] is not None else ''}"
              f"{fmt(r['per'], '>8.1f', width=8)}{fmt(r['psr'], '>7.1f', width=7)}"
              f"{fmt(r['pbr'], '>7.1f', width=7)}")

    tgt = next((r for r in rows if r["ticker"] == a.target.upper()), None)
    peers = [r for r in rows if r is not tgt]
    if not tgt or not peers:
        return

    print("\n" + "=" * 96)
    print("상대 위치")
    print("=" * 96)
    for label, key, better_low in [("PER", "per", True), ("PSR", "psr", True),
                                   ("PBR", "pbr", True), ("매출총이익률", "gpm", False),
                                   ("영업이익률", "opm", False),
                                   ("매출성장률", "growth", False)]:
        vals = [r[key] for r in peers if r.get(key) is not None]
        mine = tgt.get(key)
        if mine is None or not vals:
            print(f"{label:<12} 비교 불가 (데이터 부족)")
            continue
        med = sorted(vals)[len(vals) // 2]
        rank = sum(1 for v in vals if v < mine) + 1
        gap = (mine / med - 1) * 100 if med else 0
        if better_low:
            verdict = "동종 대비 싸다" if gap < -10 else "동종 대비 비싸다" if gap > 10 else "동종과 비슷"
        else:
            verdict = "동종 대비 높다" if gap > 10 else "동종 대비 낮다" if gap < -10 else "동종과 비슷"
        print(f"{label:<12}{mine:>8.1f}   중앙값 {med:>7.1f}   "
              f"{gap:>+7.1f}%   {rank}/{len(vals)+1}위   {verdict}")

    print("\n※ TTM = 최근 4분기 합. 4분기 구간이 없는 회사는 연간(FY)에서 역산했다.")
    print("※ 회계연도가 다른 회사끼리는 기준일이 어긋난다. '기준' 열의 날짜를 확인한다.")
    print("※ 밸류에이션이 낮은 데는 이유가 있다. 싸다는 것과 매력적이라는 것은 다르다.")


if __name__ == "__main__":
    main()
