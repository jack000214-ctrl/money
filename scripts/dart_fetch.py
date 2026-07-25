#!/usr/bin/env python3
"""OpenDART 원문 수집기.

DART 공시검색 화면은 JavaScript 기반이라 웹 도구로 목록을 가져올 수 없다.
이 스크립트는 그 지점을 대체한다 - 원문 API에서 직접 받아 시점과 함께 보존한다.

모든 응답은 data/raw/dart/ 아래에 받은 그대로 저장한다.
정규화 전 원본을 남기는 것이 백테스트와 감사의 기준이다 (docs/03-data-architecture.md).

인증키는 절대 출력하지 않는다.
"""

import argparse
import io
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from xml.etree import ElementTree as ET
from datetime import date, datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

BASE = "https://opendart.fss.or.kr/api"
ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw" / "dart"
CACHE_DIR = ROOT / "data" / "cache"

# reprt_code: 정기보고서 종류
REPORTS = {
    "1q": "11013",   # 1분기보고서
    "half": "11012",  # 반기보고서
    "3q": "11014",   # 3분기보고서
    "annual": "11011",  # 사업보고서
}

STATUS_MESSAGES = {
    "000": "정상",
    "010": "등록되지 않은 키",
    "011": "사용할 수 없는 키",
    "012": "접근할 수 없는 IP",
    "013": "조회된 데이터가 없음",
    "014": "파일이 존재하지 않음",
    "020": "요청 제한을 초과 (일일 한도)",
    "021": "조회 가능한 회사 개수 초과",
    "100": "필드의 부적절한 값",
    "101": "부적절한 접근",
    "800": "시스템 점검 중",
    "900": "정의되지 않은 오류",
    "901": "사용자 계정의 개인정보보유기간 만료",
}


def fail(msg):
    print(f"오류: {msg}", file=sys.stderr)
    sys.exit(1)


def load_key():
    key = os.environ.get("DART_API_KEY")
    if key:
        return key.strip()
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("DART_API_KEY="):
                return line.split("=", 1)[1].strip()
    fail("DART_API_KEY 가 없다. .env 에 넣거나 환경변수로 지정한다.")


def redact(text, key):
    """어떤 경로로도 인증키가 출력되지 않게 한다."""
    return text.replace(key, "***REDACTED***") if key else text


def request(path, params, key, binary=False, allow_empty=False):
    params = {**params, "crtfc_key": key}
    url = f"{BASE}/{path}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        fail(redact(f"HTTP {e.code} — {path}", key))
    except urllib.error.URLError as e:
        fail(redact(f"연결 실패 — {e.reason}", key))

    if binary:
        return body

    data = json.loads(body.decode("utf-8"))
    status = data.get("status")
    if status == "013" and allow_empty:
        return None
    if status != "000":
        detail = STATUS_MESSAGES.get(status, data.get("message", ""))
        fail(f"DART 응답 {status}: {detail}")
    return data


def fetch_is(corp_code, year, reprt, fs, key):
    """손익 주요항목을 (매출, 영업이익) 으로 돌려준다.

    fnlttSinglAcntAll 이 013 을 내는 조합이 있어(예: 파마리서치 2025 사업보고서)
    주요계정 API 로 폴백한다. 두 API 는 같은 원문에서 나오지만 커버리지가 다르다.
    """
    data = request(
        "fnlttSinglAcntAll.json",
        {"corp_code": corp_code, "bsns_year": year, "reprt_code": reprt, "fs_div": fs},
        key,
        allow_empty=True,
    )
    if data:
        rows = data["list"]
        rev = pick(rows, "IS", REV_NAMES) or pick(rows, "CIS", REV_NAMES)
        op = pick(rows, "IS", OP_NAMES) or pick(rows, "CIS", OP_NAMES)
        if rev and op:
            return to_int(rev["thstrm_amount"]), to_int(op["thstrm_amount"]), "All", data

    data = request(
        "fnlttSinglAcnt.json",
        {"corp_code": corp_code, "bsns_year": year, "reprt_code": reprt},
        key,
        allow_empty=True,
    )
    if not data:
        return None, None, None, None
    want = "연결재무제표" if fs == "CFS" else "재무제표"
    rev = op = None
    for r in data["list"]:
        if r.get("fs_nm") != want:
            continue
        if r.get("account_nm") in REV_NAMES:
            rev = to_int(r["thstrm_amount"])
        elif r.get("account_nm") in OP_NAMES:
            op = to_int(r["thstrm_amount"])
    return rev, op, "주요계정", data


def save_raw(name, payload):
    """원본을 수집시각과 함께 보존한다."""
    stamp = datetime.now(timezone.utc)
    out = RAW_DIR / stamp.strftime("%Y-%m-%d")
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{name}_{stamp.strftime('%H%M%S')}.json"
    path.write_text(
        json.dumps(
            {
                "fetched_at": stamp.isoformat(timespec="seconds"),
                "source": "opendart.fss.or.kr",
                "payload": payload,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def xbrl_facts(rcept_no, reprt, key, concept, want_dims=("ConsolidatedMember",),
               exclude_dims=("OrdinarySharesMember", "PreferredSharesMember")):
    """정기보고서의 XBRL 원본에서 개념 값을 뽑는다.

    fnlttSinglAcntAll 이 013 을 내는 보고서(예: 파마리서치 2025 사업보고서)에서도
    XBRL 원본은 정상적으로 받아진다. 이 경로가 최후의 수단이다.

    주의: 우선주가 있는 회사는 같은 개념이 차원별로 여러 값을 갖는다.
    보통주 기준(OrdinarySharesMember)과 전체 기준이 다르므로 섞으면 안 된다.
    기본값은 전체 기준 - fnlttSinglAcntAll 의 분기값과 같은 기준이다.
    """
    blob = request("fnlttXbrl.xml", {"rcept_no": rcept_no, "reprt_code": reprt},
                   key, binary=True)
    if blob[:1] != b"P":
        return []
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        names = [n for n in z.namelist() if n.endswith(".xbrl")]
        if not names:
            return []
        root = ET.fromstring(z.read(names[0]))

    ctx = {}
    for c in root:
        if not c.tag.endswith("}context"):
            continue
        start = end = None
        dims = []
        for e in c.iter():
            tag = e.tag.split("}")[-1]
            if tag == "startDate":
                start = e.text
            elif tag == "endDate":
                end = e.text
            elif tag == "instant":
                start, end = None, e.text
            elif tag == "explicitMember":
                dims.append((e.text or "").split(":")[-1])
        ctx[c.get("id")] = (start, end, dims)

    out = []
    for el in root:
        if el.tag.split("}")[-1] != concept:
            continue
        start, end, dims = ctx.get(el.get("contextRef"), (None, None, []))
        if want_dims and not all(d in dims for d in want_dims):
            continue
        if any(d in dims for d in exclude_dims):
            continue
        out.append({"start": start, "end": end, "value": to_int(el.text), "dims": dims})
    # 같은 기간 중복 제거
    dedup = {}
    for r in out:
        dedup[(r["start"], r["end"])] = r
    return sorted(dedup.values(), key=lambda r: r["end"] or "")


def annual_report_rcept(corp_code, year, key):
    """해당 사업연도 사업보고서의 접수번호."""
    for y in (year + 1, year):
        data = request("list.json", {
            "corp_code": corp_code, "bgn_de": f"{y}0101", "end_de": f"{y}1231",
            "pblntf_ty": "A", "page_count": "100"}, key, allow_empty=True)
        if not data:
            continue
        for r in data.get("list", []):
            if r["report_nm"].startswith("사업보고서") and f"({year}." in r["report_nm"]:
                return r["rcept_no"]
    return None


def corp_code_map(key, refresh=False):
    """고유번호 전체 목록. ZIP 안의 XML이며 크므로 캐시한다."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / "dart_corp_codes.json"
    if cache.exists() and not refresh:
        return json.loads(cache.read_text(encoding="utf-8"))

    blob = request("corpCode.xml", {}, key, binary=True)
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        xml = z.read(z.namelist()[0])

    mapping = {}
    for el in ElementTree.fromstring(xml).iter("list"):
        stock = (el.findtext("stock_code") or "").strip()
        if stock:  # 상장사만
            mapping[stock] = {
                "corp_code": (el.findtext("corp_code") or "").strip(),
                "corp_name": (el.findtext("corp_name") or "").strip(),
                "modify_date": (el.findtext("modify_date") or "").strip(),
            }
    cache.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
    return mapping


def resolve(stock_code, key, refresh=False):
    m = corp_code_map(key, refresh)
    if stock_code not in m:
        fail(f"종목코드 {stock_code} 를 DART 고유번호로 찾을 수 없다")
    return m[stock_code]


def cmd_resolve(a, key):
    info = resolve(a.stock_code, key, a.refresh)
    print(f"종목코드 {a.stock_code} → 고유번호 {info['corp_code']} ({info['corp_name']})")
    print(f"최종변경일 {info['modify_date']}  상장사 총 {len(corp_code_map(key)):,}개")


def cmd_filings(a, key):
    info = resolve(a.stock_code, key)
    data = request(
        "list.json",
        {
            "corp_code": info["corp_code"],
            "bgn_de": a.since,
            "end_de": a.until or date.today().strftime("%Y%m%d"),
            "page_count": "100",
        },
        key,
    )
    path = save_raw(f"filings_{a.stock_code}", data)
    rows = data.get("list", [])

    print(f"{info['corp_name']} 공시 {len(rows)}건  ({a.since} ~ {a.until or '오늘'})")
    print(f"원본 저장: {path.relative_to(ROOT)}")
    print("-" * 88)
    for r in rows:
        note = f"  [{r['rm']}]" if r.get("rm") else ""
        print(f"{r['rcept_dt']}  {r['report_nm'][:52]:<54} {r['rcept_no']}{note}")


REV_NAMES = ["매출액", "수익(매출액)", "영업수익"]
OP_NAMES = ["영업이익", "영업이익(손실)"]


def pick(rows, sj_div, names):
    """계정명으로 찾는다. 회사마다 표기가 달라 후보를 여러 개 받는다."""
    for want in names:
        for r in rows:
            if r.get("sj_div") != sj_div:
                continue
            nm = (r.get("account_nm") or "").replace(" ", "")
            if nm == want.replace(" ", ""):
                return r
    return None


def to_int(v):
    try:
        return int(str(v).replace(",", "").strip())
    except (ValueError, AttributeError):
        return None


def cmd_financials(a, key):
    info = resolve(a.stock_code, key)
    reprt = REPORTS[a.report]
    data = request(
        "fnlttSinglAcntAll.json",
        {
            "corp_code": info["corp_code"],
            "bsns_year": a.year,
            "reprt_code": reprt,
            "fs_div": a.fs,
        },
        key,
    )
    path = save_raw(f"fs_{a.stock_code}_{a.year}_{a.report}_{a.fs}", data)
    rows = data.get("list", [])

    label = {"CFS": "연결", "OFS": "별도"}[a.fs]
    print(f"{info['corp_name']} {a.year}년 {a.report} ({label}) — 계정 {len(rows)}건")
    print(f"원본 저장: {path.relative_to(ROOT)}")
    print("-" * 76)

    revenue = pick(rows, "IS", ["매출액", "수익(매출액)", "영업수익"]) \
        or pick(rows, "CIS", ["매출액", "수익(매출액)", "영업수익"])
    profit = pick(rows, "IS", ["영업이익", "영업이익(손실)"]) \
        or pick(rows, "CIS", ["영업이익", "영업이익(손실)"])

    if not revenue or not profit:
        print("매출액 또는 영업이익 계정을 찾지 못했다. 원본 JSON을 직접 확인한다.")
        print("사용 가능한 손익 계정:")
        for r in rows:
            if r.get("sj_div") in ("IS", "CIS"):
                print(f"  {r.get('account_nm')}")
        return

    def show(period_label, rev_field, op_field):
        rev, op = to_int(revenue.get(rev_field)), to_int(profit.get(op_field))
        if rev is None or op is None or rev == 0:
            return
        print(f"{period_label:<22}매출 {rev/1e8:>12,.0f}억   "
              f"영업이익 {op/1e8:>10,.0f}억   OPM {op/rev*100:>6.2f}%")

    show(revenue.get("thstrm_nm", "당기"), "thstrm_amount", "thstrm_amount")
    show(revenue.get("frmtrm_nm", "전기"), "frmtrm_amount", "frmtrm_amount")
    show(revenue.get("bfefrmtrm_nm", "전전기"), "bfefrmtrm_amount", "bfefrmtrm_amount")


def cmd_quarters(a, key):
    """분기별 3개월 실적 시리즈를 만든다.

    정기보고서의 thstrm_amount 는 분기·반기·3분기 모두 해당 3개월 금액이다
    (누계가 아니다). 4분기는 별도 보고서가 없으므로 연간에서 1~3분기를 뺀다.
    """
    info = resolve(a.stock_code, key)
    print(f"{info['corp_name']} ({a.stock_code}) 분기별 실적 — "
          f"{'연결' if a.fs == 'CFS' else '별도'}, 단위 억원")
    print("=" * 66)
    print(f"{'분기':<10}{'매출':>12}{'영업이익':>12}{'OPM':>9}{'YoY 매출':>11}{'출처':>8}")
    print("-" * 66)

    series, raw_saved = {}, []
    for year in a.years:
        vals = {}
        for q, rc in [("1Q", "11013"), ("2Q", "11012"), ("3Q", "11014")]:
            rev, op, src, raw = fetch_is(info["corp_code"], year, rc, a.fs, key)
            if rev is not None:
                vals[q] = (rev, op, src)
                raw_saved.append(save_raw(f"fs_{a.stock_code}_{year}_{q}", raw))
        rev_y, op_y, src_y, raw_y = fetch_is(info["corp_code"], year, "11011", a.fs, key)
        if rev_y is not None:
            raw_saved.append(save_raw(f"fs_{a.stock_code}_{year}_FY", raw_y))
            if len(vals) == 3:
                q4r = rev_y - sum(v[0] for v in vals.values())
                q4o = op_y - sum(v[1] for v in vals.values())
                vals["4Q"] = (q4r, q4o, f"{src_y}-역산")
        for q in ["1Q", "2Q", "3Q", "4Q"]:
            if q in vals:
                series[f"{q}{str(year)[2:]}"] = vals[q]

    for label, (rev, op, src) in series.items():
        prior = series.get(f"{label[:2]}{int(label[2:]) - 1:02d}")
        yoy = f"{(rev / prior[0] - 1) * 100:>+10.1f}%" if prior else f"{'—':>11}"
        opm = f"{op / rev * 100:>8.2f}%" if rev else f"{'—':>9}"
        print(f"{label:<10}{rev/1e8:>12,.0f}{op/1e8:>12,.0f}{opm}{yoy}{src:>8}")

    print("-" * 66)
    print(f"원본 {len(raw_saved)}건 저장: data/raw/dart/")
    print("※ 4분기는 별도 보고서가 없어 연간에서 1~3분기를 뺀 값이다.")


def main():
    p = argparse.ArgumentParser(description="OpenDART 원문 수집")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("resolve", help="종목코드 → DART 고유번호")
    r.add_argument("stock_code")
    r.add_argument("--refresh", action="store_true", help="고유번호 캐시 갱신")

    f = sub.add_parser("filings", help="공시 목록")
    f.add_argument("stock_code")
    f.add_argument("--since", required=True, help="YYYYMMDD")
    f.add_argument("--until", help="YYYYMMDD (기본: 오늘)")

    s = sub.add_parser("financials", help="재무제표")
    s.add_argument("stock_code")
    s.add_argument("--year", required=True)
    s.add_argument("--report", choices=list(REPORTS), default="annual")
    s.add_argument("--fs", choices=["CFS", "OFS"], default="CFS", help="연결/별도")

    q = sub.add_parser("quarters", help="분기별 3개월 실적 시리즈 (4분기는 역산)")
    q.add_argument("stock_code")
    q.add_argument("--years", nargs="+", required=True, help="예: 2024 2025 2026")
    q.add_argument("--fs", choices=["CFS", "OFS"], default="CFS")

    a = p.parse_args()
    key = load_key()
    {
        "resolve": cmd_resolve,
        "filings": cmd_filings,
        "financials": cmd_financials,
        "quarters": cmd_quarters,
    }[a.cmd](a, key)


if __name__ == "__main__":
    main()
