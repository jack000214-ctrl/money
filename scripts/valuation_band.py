#!/usr/bin/env python3
"""밸류에이션 밴드 — 가격 기준 매매 규칙을 밸류에이션 기준으로 바꾼다.

"35만원 이상 매도"는 회사가 성장해도 고정돼 있어서 **성장하는 회사를 계속 팔게 만든다.**
2026-06-15 매도(306,500원)가 그 사례다. "PER 20배 이상 매도"로 바꾸면
이익이 늘 때 매도 기준가도 따라 올라간다.

시점 보존: 각 시점의 PER 은 **그때 공시돼 있던 TTM EPS** 로 계산한다.
지금 아는 이익으로 과거 PER 을 만들면 미래정보 편향이 된다 (ADR-003).

EPS 출처
  한국: DART 기본주당이익 (분기 3개월 값, 4분기는 연간에서 역산)
  미국: EDGAR EarningsPerShareBasic
"""

import argparse
import csv
import json
import statistics
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from dart_fetch import (annual_report_rcept, load_key, request as dart_request,  # noqa: E402
                        resolve as dart_resolve, xbrl_facts)
from edgar_fetch import CONCEPT, user_agent  # noqa: E402

PRICE_DIR = ROOT / "knowledge" / "prices"
EPS_NAMES = ["기본주당이익", "기본주당순이익", "기본주당이익(손실)"]
QUARTERS = [("1Q", "11013"), ("2Q", "11012"), ("3Q", "11014")]


def fail(msg):
    print(f"오류: {msg}", file=sys.stderr)
    sys.exit(1)


def load_prices(symbol):
    path = PRICE_DIR / f"{symbol.replace('.', '_')}.csv"
    if not path.exists():
        fail(f"{path.relative_to(ROOT)} 가 없다. price_fetch.py history 를 먼저 실행한다.")
    with open(path, newline="", encoding="utf-8") as f:
        lines = [ln for ln in f if not ln.lstrip().startswith("#")]
    return [(r["date"], float(r["close"])) for r in csv.DictReader(lines) if r.get("close")]


def num(v):
    try:
        return float(str(v).replace(",", "").strip())
    except (ValueError, AttributeError, TypeError):
        return None


# ---------------------------------------------------------------- 한국 (DART)

def kr_filing_dates(corp_code, key):
    """정기보고서 접수일. 시점 보존을 위해 '언제 공시됐는지'가 필요하다."""
    out = {}
    for year in range(2023, datetime.now().year + 1):
        data = dart_request("list.json", {
            "corp_code": corp_code, "bgn_de": f"{year}0101", "end_de": f"{year}1231",
            "pblntf_ty": "A", "page_count": "100"}, key)
        for r in data.get("list", []):
            nm = r["report_nm"]
            if "분기보고서" in nm or "반기보고서" in nm or "사업보고서" in nm:
                # 보고서명의 (YYYY.MM) 이 대상 기간이다
                if "(" in nm and ")" in nm:
                    period = nm[nm.index("(") + 1:nm.index(")")].strip()
                    out[period] = r["rcept_dt"]
    return out


def kr_eps_series(stock_code, key, years):
    info = dart_resolve(stock_code, key)
    filed = kr_filing_dates(info["corp_code"], key)
    quarterly = {}

    for year in years:
        vals = {}
        for label, rc in QUARTERS:
            eps = kr_fetch_eps(info["corp_code"], year, rc, key)
            if eps is not None:
                vals[label] = eps
            time.sleep(0.1)
        annual = kr_fetch_eps(info["corp_code"], year, "11011", key)
        if annual is not None and len(vals) == 3:
            vals["4Q"] = annual - sum(vals.values())
        for label, eps in vals.items():
            q = int(label[0])
            end_month = q * 3
            key_period = f"{year}.{end_month:02d}"
            quarterly[f"{year}-{end_month:02d}"] = {
                "eps": eps,
                "filed": filed.get(key_period),
                "period": key_period,
            }
    return quarterly


def kr_fetch_eps(corp_code, year, reprt, key):
    data = dart_request("fnlttSinglAcntAll.json", {
        "corp_code": corp_code, "bsns_year": str(year),
        "reprt_code": reprt, "fs_div": "CFS"}, key, allow_empty=True)
    if data:
        for r in data["list"]:
            if (r.get("account_nm") or "").replace(" ", "") in EPS_NAMES:
                return num(r.get("thstrm_amount"))

    # 폴백: 사업보고서가 fnlttSinglAcntAll 에서 조회되지 않는 경우가 있다
    # (파마리서치 2025). XBRL 원본에는 정상적으로 들어 있다.
    if reprt != "11011":
        return None
    rcept = annual_report_rcept(corp_code, year, key)
    if not rcept:
        return None
    facts = xbrl_facts(rcept, reprt, key, "BasicEarningsLossPerShare")
    for f in facts:
        if f["end"] == f"{year}-12-31" and f["start"] == f"{year}-01-01":
            return float(f["value"])
    return None


# ---------------------------------------------------------------- 미국 (EDGAR)

def us_eps_series(ticker, ua):
    from edgar_fetch import resolve as edgar_resolve
    info = edgar_resolve(ticker, ua)
    url = CONCEPT.format(cik=info["cik"], tag="EarningsPerShareBasic")
    req = urllib.request.Request(url, headers={"User-Agent": ua})
    try:
        data = json.loads(urllib.request.urlopen(req, timeout=40).read())
    except Exception as e:
        fail(f"{ticker}: EPS 조회 실패 — {e}")
    pts = data.get("units", {}).get("USD/shares", [])
    quarterly = {}
    for p in pts:
        if not p.get("start") or not p.get("end") or not p.get("filed"):
            continue
        days = (datetime.fromisoformat(p["end"]) - datetime.fromisoformat(p["start"])).days
        if 80 <= days <= 100:
            quarterly[p["end"][:7]] = {"eps": p["val"], "filed": p["filed"],
                                       "period": p["end"]}
    return quarterly


# ---------------------------------------------------------------- 공통

def q_index(key):
    """'2026-03' → 분기 일련번호. 연속성 검사에 쓴다."""
    y, m = int(key[:4]), int(key[5:7])
    return y * 4 + (m - 1) // 3


def ttm_series(quarterly):
    """분기 EPS 를 TTM 으로 누적.

    **연속 4분기여야 한다.** 한 분기라도 비면 건너뛰고 더하는 것이 아니라 그 구간을 버린다.
    (파마리서치 2025 사업보고서가 fnlttSinglAcntAll 에서 조회되지 않아 4Q25 가 비는데,
     이를 건너뛰면 1Q25+2Q25+3Q25+1Q26 을 TTM 이라 부르게 된다.)
    """
    keys = sorted(quarterly)
    out = []
    skipped = []
    for i in range(3, len(keys)):
        window = keys[i - 3:i + 1]
        idx = [q_index(k) for k in window]
        if idx[-1] - idx[0] != 3 or len(set(idx)) != 4:
            skipped.append(window[-1])
            continue
        if any(quarterly[k]["eps"] is None for k in window):
            skipped.append(window[-1])
            continue
        filed = quarterly[keys[i]].get("filed")
        if not filed:
            continue
        out.append({
            "as_of": keys[i],
            "eps_ttm": sum(quarterly[k]["eps"] for k in window),
            "known_from": filed.replace(".", "-") if "." in filed else
                          f"{filed[:4]}-{filed[4:6]}-{filed[6:8]}" if len(filed) == 8 else filed,
        })
    if skipped:
        print(f"※ 연속 4분기가 확보되지 않아 제외한 구간: {', '.join(skipped)}",
              file=sys.stderr)
    return out


def per_history(prices, ttm):
    """각 거래일에 그 시점 공시돼 있던 TTM EPS 로 PER 을 만든다."""
    ttm = sorted(ttm, key=lambda t: t["known_from"])
    out = []
    for date, close in prices:
        usable = [t for t in ttm if t["known_from"] <= date]
        if not usable:
            continue
        eps = usable[-1]["eps_ttm"]
        if eps and eps > 0:
            out.append((date, close, close / eps, eps, usable[-1]["as_of"]))
    return out


def main():
    p = argparse.ArgumentParser(description="밸류에이션 밴드")
    p.add_argument("ticker", help="한국: 214450 / 미국: IBM")
    p.add_argument("--market", choices=["KR", "US"], required=True)
    p.add_argument("--price-symbol", help="가격 파일 심볼 (기본: 한국 {코드}.KQ, 미국 티커)")
    p.add_argument("--years", nargs="+", type=int, default=[2023, 2024, 2025, 2026])
    p.add_argument("--targets", nargs="+", type=float, default=[25, 50, 75],
                   help="주가로 환산할 PER 백분위")
    a = p.parse_args()

    symbol = a.price_symbol or (f"{a.ticker}.KQ" if a.market == "KR" else a.ticker)
    prices = load_prices(symbol)

    if a.market == "KR":
        quarterly = kr_eps_series(a.ticker, load_key(), a.years)
    else:
        quarterly = us_eps_series(a.ticker, user_agent())

    ttm = ttm_series(quarterly)
    if not ttm:
        fail("TTM EPS 를 만들 수 없다. 분기 EPS 또는 공시일이 부족하다.")

    hist = per_history(prices, ttm)
    if not hist:
        fail("가격 기간과 EPS 공시 기간이 겹치지 않는다.")

    pers = [h[2] for h in hist]
    cur_date, cur_price, cur_per, cur_eps, cur_asof = hist[-1]

    print("=" * 74)
    print(f"밸류에이션 밴드 — {a.ticker} ({a.market})")
    print("=" * 74)
    print(f"기간          {hist[0][0]} ~ {hist[-1][0]}  ({len(hist)}거래일)")
    print(f"현재가        {cur_price:>14,.2f}")
    print(f"TTM EPS       {cur_eps:>14,.2f}   (기준 {cur_asof}, 공시 반영)")
    print(f"현재 PER      {cur_per:>14,.2f}")

    qs = statistics.quantiles(pers, n=100)
    pct = sum(1 for v in pers if v < cur_per) / len(pers) * 100
    print(f"\n[ PER 분포 ]")
    print(f"  최저 {min(pers):>7.2f}   25% {qs[24]:>7.2f}   중앙 {qs[49]:>7.2f}   "
          f"75% {qs[74]:>7.2f}   최고 {max(pers):>7.2f}")
    print(f"  현재 백분위  {pct:>5.1f}%")
    bar = int(pct / 100 * 40)
    print(f"  저평가 [{'─' * bar}●{'─' * (40 - bar)}] 고평가")

    print(f"\n[ 매매 기준가 — PER 을 주가로 환산 ]")
    print(f"  {'기준':<18}{'PER':>8}{'주가':>16}   현재가 대비")
    print("  " + "-" * 58)
    for t in sorted(a.targets):
        per_t = qs[int(t) - 1] if 1 <= int(t) <= 99 else statistics.median(pers)
        price_t = per_t * cur_eps
        print(f"  {f'{t:.0f}백분위':<18}{per_t:>8.2f}{price_t:>16,.0f}"
              f"{(price_t / cur_price - 1) * 100:>+13.1f}%")

    print(f"\n[ TTM EPS 추이 ]")
    for t in ttm[-6:]:
        print(f"  {t['as_of']}   EPS {t['eps_ttm']:>12,.2f}   공시 {t['known_from']}")

    print("\n※ 각 시점의 PER 은 그때 공시돼 있던 TTM EPS 로 계산했다 (미래정보 배제).")
    print("※ 밴드는 과거 분포다. 사업이 구조적으로 변하면 과거 밴드는 기준이 되지 않는다.")
    print("※ PER 이 낮은 데는 이유가 있을 수 있다. 밴드 하단 = 매수 신호가 아니다.")


if __name__ == "__main__":
    main()
