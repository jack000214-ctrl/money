#!/usr/bin/env python3
"""SEC XBRL과 시장가격으로 미국 동종기업의 수치상 상대 위치를 계산한다.

출력은 중앙값 차이와 순위이며 투자 매력도 판정은 CIO 모델의 책임이다.
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
from collectors.edgar import (
    CONCEPT,
    OPINC_TAGS,
    REVENUE_TAGS,
    resolve,
    trailing_four_quarters,
    user_agent,
)

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
            key = (p["start"], p["end"])
            if key not in out or (p.get("filed") or "") >= (out[key].get("filed") or ""):
                out[key] = p
    return sorted(out.values(), key=lambda p: p["end"])


def ttm(points):
    """연속 최근 4분기 합. 복원할 수 없으면 최신 FY로 명확히 폴백한다."""
    value, quarters = trailing_four_quarters(points)
    if value is not None:
        return value, quarters[-1]["end"], "TTM"
    fy = periods(points, 350, 380)
    return (fy[-1]["val"], fy[-1]["end"], "FY") if fy else (None, None, None)


def align_period(value, end, basis, reference_end, reference_basis):
    """서로 다른 기간·산출기준의 손익 항목을 한 비율에 섞지 않는다."""
    if end != reference_end or basis != reference_basis:
        return None
    return value


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
    row = {
        "ticker": ticker.upper(),
        "name": info["name"][:22],
        # 적자·주식수·시세 데이터 부재는 정상적인 비교 결과다. 일부 값이 없더라도
        # 표 전체를 중단하지 않고 UNKNOWN 으로 남겨야 한다.
        "mcap": None,
        "per": None,
        "psr": None,
        "pbr": None,
    }

    _, rev_pts = concept_ns(cik, REVENUE_TAGS, ua)
    _, op_pts = concept_ns(cik, OPINC_TAGS, ua)
    _, ni_pts = concept_ns(cik, NETINC_TAGS, ua)
    _, sh_pts = concept_ns(cik, SHARES_TAGS, ua, ns="dei")
    _, eq_pts = concept_ns(cik, EQUITY_TAGS, ua)
    _, gp_pts = concept_ns(cik, GROSS_TAGS, ua)

    row["revenue"], row["as_of"], row["basis"] = ttm(rev_pts)
    opinc, op_as_of, op_basis = ttm(op_pts)
    gross, gross_as_of, gross_basis = ttm(gp_pts)
    netinc, net_as_of, net_basis = ttm(ni_pts)
    row["opinc"] = align_period(
        opinc, op_as_of, op_basis, row["as_of"], row["basis"])
    row["gross"] = align_period(
        gross, gross_as_of, gross_basis, row["as_of"], row["basis"])
    row["netinc"] = align_period(
        netinc, net_as_of, net_basis, row["as_of"], row["basis"])
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

    print("=" * 116)
    print(f"동종업계 비교 — 기준 {a.target.upper()}   (재무: SEC EDGAR / 주가: Yahoo)")
    print("=" * 116)
    print(f"{'티커':<7}{'회사':<24}{'매출(TTM/FY,$M)':>17}{'기준일':>12}{'방식':>6}"
          f"{'GPM':>8}{'OPM':>8}{'매출YoY':>9}{'PER':>8}{'PSR':>7}{'PBR':>7}")
    print("-" * 116)
    for r in rows:
        mark = "▶ " if r["ticker"] == a.target.upper() else "  "
        print(f"{mark}{r['ticker']:<5}{r['name']:<24}"
              f"{fmt(r['revenue'] / 1e6 if r['revenue'] else None, '>17,.0f', width=17)}"
              f"{(r['as_of'] or '—'):>12}{(r['basis'] or '—'):>6}"
              f"{fmt(r['gpm'], '>7.1f', width=8)}{'%' if r['gpm'] is not None else ''}"
              f"{fmt(r['opm'], '>7.1f', width=8)}{'%' if r['opm'] is not None else ''}"
              f"{fmt(r['growth'], '>+8.1f', width=9)}{'%' if r['growth'] is not None else ''}"
              f"{fmt(r['per'], '>8.1f', width=8)}{fmt(r['psr'], '>7.1f', width=7)}"
              f"{fmt(r['pbr'], '>7.1f', width=7)}")

    tgt = next((r for r in rows if r["ticker"] == a.target.upper()), None)
    peers = [r for r in rows if r is not tgt]
    if not tgt or not peers:
        return

    print("\n" + "=" * 116)
    print("상대 위치")
    print("=" * 116)
    for label, key in [("PER", "per"), ("PSR", "psr"),
                       ("PBR", "pbr"), ("매출총이익률", "gpm"),
                       ("영업이익률", "opm"), ("매출성장률", "growth")]:
        vals = [r[key] for r in peers if r.get(key) is not None]
        mine = tgt.get(key)
        if mine is None or not vals:
            print(f"{label:<12} 비교 불가 (데이터 부족)")
            continue
        med = sorted(vals)[len(vals) // 2]
        rank = sum(1 for v in vals if v < mine) + 1
        gap = (mine / med - 1) * 100 if med else 0
        position = "중앙값보다 낮음" if gap < -10 else "중앙값보다 높음" if gap > 10 else "중앙값 근접"
        print(f"{label:<12}{mine:>8.1f}   중앙값 {med:>7.1f}   "
              f"{gap:>+7.1f}%   {rank}/{len(vals)+1}위   {position}")

    print("\n※ TTM = 복원 가능한 연속 최근 4분기 합. 불가능하면 최신 FY를 별도 표시한다.")
    print("※ 매출과 기간·방식이 다른 이익 항목은 마진·PER 계산에 섞지 않고 '—'로 둔다.")
    print("※ 회계연도가 다른 회사끼리는 기준일이 어긋난다. '기준일' 열을 확인한다.")
    print("※ 중앙값 차이와 순위는 측정치이며 투자 매력도 판정이 아니다.")


if __name__ == "__main__":
    main()
