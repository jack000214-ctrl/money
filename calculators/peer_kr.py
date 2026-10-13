#!/usr/bin/env python3
"""한국 종목 동종업계 비교 (DART + Yahoo).

  - 재무는 주요계정(fnlttSinglAcnt) 연간 기준으로 통일한다.
    fnlttSinglAcntAll 은 회사·연도에 따라 013 을 내서 비교표가 구멍난다.
  - 주식수는 stockTotqySttus. 구분 표기가 회사마다 달라 정규화가 필요하다.

시가총액은 보통주 기준이다. 우선주가 상장된 회사는 실제 시총이 더 크다.
"""

import argparse
import json
import statistics
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
from calculators.contracts import change_pct
from cio.display import cell
from collectors.dart import load_key, request as dart_request, resolve as dart_resolve

CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{s}?range=5d&interval=1d"
QUARTERS = [("1Q", "11013"), ("2Q", "11012"), ("3Q", "11014")]
EQUITY_NAMES = ["자본총계"]


def num(v):
    try:
        return float(str(v).replace(",", "").strip())
    except (ValueError, AttributeError, TypeError):
        return None


def price(stock_code):
    """코스닥·코스피 중 응답하는 쪽을 쓴다."""
    for suffix in (".KQ", ".KS"):
        try:
            req = urllib.request.Request(CHART.format(s=stock_code + suffix),
                                         headers={"User-Agent": "Mozilla/5.0"})
            d = json.loads(urllib.request.urlopen(req, timeout=25).read())
            m = d["chart"]["result"][0]["meta"]
            if m.get("regularMarketPrice"):
                return m["regularMarketPrice"], suffix
        except Exception:
            continue
    return None, None


def fs_rows(corp_code, year, reprt, key):
    d = dart_request("fnlttSinglAcntAll.json", {
        "corp_code": corp_code, "bsns_year": str(year),
        "reprt_code": reprt, "fs_div": "CFS"}, key, allow_empty=True)
    return d["list"] if d else []


def pick(rows, names, divs=("IS", "CIS")):
    for want in names:
        for r in rows:
            if r.get("sj_div") in divs and \
               (r.get("account_nm") or "").replace(" ", "") == want.replace(" ", ""):
                return num(r.get("thstrm_amount"))
    return None


def fy_main(corp_code, year, key):
    """주요계정 API 로 연간 재무를 가져온다.

    fnlttSinglAcntAll 은 회사·연도에 따라 013(데이터 없음)을 내지만 주요계정은 안정적이다.
    비교표는 전 종목이 같은 기준이어야 의미가 있으므로 연간(FY)으로 통일한다.
    """
    for y in (year, year - 1):
        d = dart_request("fnlttSinglAcnt.json", {
            "corp_code": corp_code, "bsns_year": str(y), "reprt_code": "11011"},
            key, allow_empty=True)
        if not d:
            continue
        vals, seen = {}, set()
        for r in d.get("list", []):
            if r.get("fs_nm") != "연결재무제표":
                continue
            nm = (r.get("account_nm") or "").replace(" ", "")
            if nm in seen:
                continue
            seen.add(nm)
            vals[nm] = num(r.get("thstrm_amount"))
        if vals.get("매출액"):
            return {
                "revenue": vals.get("매출액"),
                "opinc": vals.get("영업이익"),
                "netinc": vals.get("당기순이익(손실)") or vals.get("당기순이익"),
                "equity": vals.get("자본총계"),
            }, str(y)
    return None, None


def shares(corp_code, year, key):
    """발행 보통주 수.

    구분(se) 표기가 회사마다 다르다: '보통주' / '의결권있는주식' / '합계'.
    공백과 줄바꿈도 섞여 들어오므로 정규화 후 우선순위대로 찾는다.
    """
    priority = ["보통주", "의결권있는주식", "합계"]
    for y in (year, year - 1):
        for rc in ("11013", "11014", "11012", "11011"):
            d = dart_request("stockTotqySttus.json", {
                "corp_code": corp_code, "bsns_year": str(y), "reprt_code": rc},
                key, allow_empty=True)
            if not d:
                continue
            found = {}
            for r in d.get("list", []):
                se = "".join((r.get("se") or "").split())
                v = num(r.get("istc_totqy"))
                if v and v > 0:
                    found[se] = v
            for want in priority:
                if want in found:
                    return found[want]
    return None


def equity(corp_code, year, key):
    for rc in ("11013", "11011"):
        rows = fs_rows(corp_code, year, rc, key)
        v = pick(rows, EQUITY_NAMES, divs=("BS",))
        if v:
            return v
    return None


def gather(stock_code, key, year):
    info = dart_resolve(stock_code, key)
    row = {"code": stock_code, "name": info["corp_name"][:14]}
    fin, basis = fy_main(info["corp_code"], year, key)
    row["basis"] = basis
    if fin:
        row.update(fin)
    row["shares"] = shares(info["corp_code"], year, key)
    row["price"], row["board"] = price(stock_code)

    row["opm"] = (row.get("opinc") / row["revenue"] * 100
                  if row.get("opinc") is not None and row.get("revenue") else None)
    row["mcap"] = row["price"] * row["shares"] if row["price"] and row["shares"] else None
    row["per"] = (row["mcap"] / row["netinc"]
                  if row.get("mcap") and row.get("netinc") and row["netinc"] > 0 else None)
    row["psr"] = row["mcap"] / row["revenue"] if row.get("mcap") and row.get("revenue") else None
    row["pbr"] = row["mcap"] / row["equity"] if row.get("mcap") and row.get("equity") else None
    return row


def same_basis_peers(target, peers):
    """기준 종목과 같은 회계연도의 동종만 비교에 남긴다. → (남긴 것, 뺀 것)

    회계연도를 모르는 줄(재무를 못 받은 줄)은 어느 해의 값인지 말할 수 없으므로
    뺀 쪽이다. 기준 종목의 회계연도를 모르면 아무것도 비교하지 않는다.
    """
    basis = target.get("basis")
    kept = [row for row in peers if basis and row.get("basis") == basis]
    return kept, [row for row in peers if row not in kept]


def fmt(v, spec, width, dash="—"):
    return f"{v:{spec}}" if v is not None else f"{dash:>{width}}"


def main():
    p = argparse.ArgumentParser(description="한국 종목 동종업계 비교")
    p.add_argument("target", help="기준 종목코드 (예: 214450)")
    p.add_argument("peers", nargs="+", help="비교 종목코드")
    p.add_argument("--year", type=int, default=2026)
    a = p.parse_args()

    key = load_key()
    rows = []
    for code in [a.target] + a.peers:
        try:
            rows.append(gather(code, key, a.year))
        except SystemExit:
            print(f"  {code}: 수집 실패", file=sys.stderr)
        except Exception as e:
            print(f"  {code}: 수집 실패 — {type(e).__name__} {e}", file=sys.stderr)

    print("=" * 92)
    print(f"동종업계 비교 — 기준 {a.target}   (재무: DART 연결 / 주가: Yahoo)")
    print("=" * 92)
    print(f"{'코드':<8}{'회사':<16}{'매출(억)':>12}{'영업이익(억)':>13}{'OPM':>8}"
          f"{'주가':>11}{'PER':>8}{'PSR':>7}{'PBR':>7}{'기준':>6}")
    print("-" * 92)
    for r in rows:
        mark = "▶" if r["code"] == a.target else " "
        print(f"{mark}{r['code']:<7}{r['name']:<16}"
              f"{fmt(r.get('revenue') / 1e8 if r.get('revenue') else None, '>12,.0f', 12)}"
              f"{fmt(r.get('opinc') / 1e8 if r.get('opinc') else None, '>13,.0f', 13)}"
              f"{fmt(r.get('opm'), '>7.1f', 8)}{'%' if r.get('opm') is not None else ''}"
              f"{fmt(r.get('price'), '>11,.0f', 11)}"
              f"{fmt(r.get('per'), '>8.1f', 8)}{fmt(r.get('psr'), '>7.1f', 7)}"
              f"{fmt(r.get('pbr'), '>7.1f', 7)}{r.get('basis') or '—':>6}")

    tgt = next((r for r in rows if r["code"] == a.target), None)
    peers = [r for r in rows if r is not tgt]
    if not tgt or not peers:
        return

    # `fy_main` 은 요청 연도가 없으면 한 해 전으로 내려간다. 그래서 '기준' 열이
    # 종목마다 다를 수 있는데, 예전에는 그대로 중앙값에 넣으면서 "전 종목 같은
    # 기준으로 통일했다"고 적었다. 다른 해의 이익으로 낸 배수는 섞지 않는다.
    peers, other_year = same_basis_peers(tgt, peers)
    print("\n" + "=" * 92)
    print(f"상대 위치 — 회계연도 {tgt.get('basis') or '미상'} 끼리만 비교")
    print("=" * 92)
    if other_year:
        print("※ 회계연도가 달라 뺀 종목: "
              + ", ".join(f"{r['code']}({r.get('basis') or '미상'})" for r in other_year))
    for label, k in [("PER", "per"), ("PSR", "psr"),
                     ("PBR", "pbr"), ("영업이익률", "opm")]:
        vals = [r[k] for r in peers if r.get(k) is not None]
        mine = tgt.get(k)
        if mine is None or not vals:
            print(f"{label:<12} 비교 불가 (데이터 부족)")
            continue
        med = statistics.median(vals)
        # 중앙값이 0 이하면 차이를 비율로 말할 수 없다. 0 으로 두면
        # 비교 불가가 "중앙값 근접"으로 읽힌다 — peer_us 와 같은 자리다.
        gap = change_pct(mine, med)
        position = (
            "비교 불가 (중앙값이 0 이하)" if gap is None
            else "중앙값보다 낮음" if gap < -10
            else "중앙값보다 높음" if gap > 10
            else "중앙값 근접"
        )
        print(f"{label:<12}{mine:>8.1f}   중앙값 {med:>7.1f}   "
              + cell(gap, 7, "+.1f", "%") + f"   {position}")

    print("\n※ 시가총액은 보통주 기준. 우선주 상장사는 실제 시총이 더 크다.")
    print("※ 연간(FY) 연결 기준이다 — '기준' 열이 회계연도이며 종목마다 다를 수 "
          "있다. 상대 위치는 기준 종목과 같은 회계연도끼리만 냈다.")
    print("※ PER = 시가총액 ÷ 당기순이익. 분자는 보통주 시가총액인데 분모는 "
          "비지배지분을 포함한 연결 순이익이다 — 비지배지분이 큰 회사는 PER 이 "
          "낮게 나온다. 최신 분기는 반영돼 있지 않다.")
    print("※ 사업 구성이 다르면 마진 비교가 왜곡된다 (제품 믹스 확인 필요).")


if __name__ == "__main__":
    main()
