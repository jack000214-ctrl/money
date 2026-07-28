#!/usr/bin/env python3
"""OpenDART 원문 수집기.

DART 공시검색 화면은 JavaScript 기반이라 웹 도구로 목록을 가져올 수 없다.
이 스크립트는 그 지점을 대체한다 - 원문 API에서 직접 받아 시점과 함께 보존한다.

모든 응답은 data/raw/dart/ 아래에 받은 그대로 저장한다.
정규화 전 원본을 남기는 것이 백테스트와 감사의 기준이다 (docs/03-data-architecture.md).

인증키는 절대 출력하지 않는다.
"""

import argparse
import hashlib
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from contracts import (SCHEMA_VERSION, ContractError,  # noqa: E402
                       fundamentals_id_for, save_validated_fundamentals)

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
    canonical = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        allow_nan=False,
    )
    out = RAW_DIR / stamp.strftime("%Y-%m-%d")
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{name}_{stamp.strftime('%H%M%S')}.json"
    path.write_text(
        json.dumps(
            {
                "fetched_at": stamp.isoformat(timespec="seconds"),
                "source": "opendart.fss.or.kr",
                "payload_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
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


def xbrl_facts_many(rcept_no, reprt, key, concepts,
                    want_dims=("ConsolidatedMember",),
                    exclude_dims=("OrdinarySharesMember", "PreferredSharesMember")):
    """XBRL 원본을 한 번 받아 여러 개념을 동시에 뽑는다.

    xbrl_facts 는 개념 하나에 zip 하나를 받는다. 보충용으로 9개 개념을 쓰면
    같은 파일을 9번 받게 되어 보고서당 수 분이 걸린다.
    """
    blob = request("fnlttXbrl.xml", {"rcept_no": rcept_no, "reprt_code": reprt},
                   key, binary=True)
    if blob[:1] != b"P":
        return {}
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        names = [n for n in z.namelist() if n.endswith(".xbrl")]
        if not names:
            return {}
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

    wanted = set(concepts)
    out = {c: {} for c in concepts}
    for el in root:
        name = el.tag.split("}")[-1]
        if name not in wanted:
            continue
        start, end, dims = ctx.get(el.get("contextRef"), (None, None, []))
        if want_dims and not all(d in dims for d in want_dims):
            continue
        if any(d in dims for d in exclude_dims):
            continue
        out[name][(start, end)] = {"start": start, "end": end,
                                   "value": to_int(el.text), "dims": dims}
    return {c: sorted(v.values(), key=lambda r: r["end"] or "")
            for c, v in out.items()}


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


# ---------------------------------------------------------------------------
# 역산 DCF 입력 스냅샷 (fundamentals)
# ---------------------------------------------------------------------------
#
# EDGAR 판(edgar_fetch.py fundamentals)과 같은 계약을 만들지만 두 가지가 다르다.
#
# 1) **기간 관습이 표마다 다르다.** 같은 fnlttSinglAcntAll 응답 안에서
#    손익계산서의 thstrm_amount 는 해당 3개월인데 현금흐름표의 thstrm_amount 는
#    연초 누계다 (삼성전자 2025 영업활동현금흐름: 1Q 165,809 → 반기 339,410 →
#    3Q 565,155 → 연간 853,151 억). 실측으로 확인했다. 그래서 현금흐름만
#    차감 복원하고 손익은 그대로 쓴다. 이걸 뒤집으면 capex 가 조용히 배로 뛴다.
# 2) **손익 항등식이 K-IFRS 모양이다.** 삼성전자 손익계산서에는 연구개발비
#    항목이 없고 판매비와관리비 총액만 있다 (R&D 는 주석). 실제로
#    매출총이익 819,132 − 판관비 246,804 = 영업이익 572,328 으로 정확히 맞는다.
#    US-GAAP 처럼 R&D 를 따로 빼면 항등식이 깨지므로, 비용 라인은 고정 키가
#    아니라 **순서 있는 목록**으로 담고 화면이 그대로 그린다.
#
# 계정은 account_nm 보다 account_id(IFRS/DART 표준코드)를 먼저 본다.
# 표기는 회사마다 바뀌지만 표준코드는 안 바뀐다.

REPORT_SEQUENCE = [("1q", 1), ("half", 2), ("3q", 3), ("annual", 4)]

# 현금흐름 — 누계로 들어오므로 차감 복원한다
CF_OCF_IDS = ["ifrs-full_CashFlowsFromUsedInOperatingActivities"]
CF_CAPEX_IDS = [
    "ifrs-full_PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"]
CF_INTANGIBLE_IDS = [
    "ifrs-full_PurchaseOfIntangibleAssetsClassifiedAsInvestingActivities"]

# 손익 — 이미 3개월이다
IS_REVENUE_IDS = ["ifrs-full_Revenue"]
IS_GROSS_IDS = ["ifrs-full_GrossProfit"]
IS_OPINC_IDS = ["dart_OperatingIncomeLoss", "ifrs-full_ProfitLossFromOperatingActivities"]
IS_NET_IDS = ["ifrs-full_ProfitLoss"]
IS_NET_PARENT_IDS = ["ifrs-full_ProfitLossAttributableToOwnersOfParent"]
IS_DILUTED_EPS_IDS = ["ifrs-full_DilutedEarningsLossPerShare"]
# 매출총이익에서 영업이익까지 빼는 비용 라인. 회사마다 개수가 다르다.
IS_EXPENSE_IDS = [
    ("dart_TotalSellingGeneralAdministrativeExpenses", "판매비와관리비"),
    # 파마리서치는 ifrs-full 쪽 표준코드를 쓰고 CIS 에 둔다. 회사마다 다르므로
    # 후보를 넓게 잡고, 아래 항등식 검사가 실제로 맞는 조합만 통과시킨다.
    ("ifrs-full_SellingGeneralAndAdministrativeExpense", "판매비와관리비"),
    ("ifrs-full_ResearchAndDevelopmentExpense", "연구개발비"),
    ("dart_SellingExpenses", "판매비"),
    ("dart_AdministrativeExpenses", "관리비"),
]

# 재무상태 — 시점값
BS_CASH_IDS = ["ifrs-full_CashAndCashEquivalents"]
BS_SHORT_INV_IDS = [
    "ifrs-full_ShorttermDepositsNotClassifiedAsCashEquivalents",
    "ifrs-full_CurrentFinancialAssetsAtFairValueThroughProfitOrLoss",
    "dart_ShortTermTradingFinancialAssets",
]
# 총차입 기준으로 잡는다. 삼성전자는 단기차입금(195,463억)이 장기차입금
# (73,938억)보다 크다. EDGAR 판처럼 장기만 보면 순부채가 12조 넘게 틀린다.
BS_DEBT_IDS = [
    ("-단기차입금-", "단기차입금"),
    ("ifrs-full_ShorttermBorrowings", "단기차입금"),
    ("ifrs-full_NoncurrentPortionOfNoncurrentBonds", "사채"),
    ("ifrs-full_NoncurrentPortionOfNoncurrentLoans", "장기차입금"),
    ("ifrs-full_CurrentPortionOfLongtermBorrowings", "유동성장기부채"),
    ("ifrs-full_Bonds", "사채"),
]
BS_DEBT_NAMES = ["단기차입금", "사채", "장기차입금", "유동성장기부채",
                 "유동성사채", "장기성미지급금"]


# fnlttSinglAcntAll 이 013 을 내는 회사·보고서 조합이 있다 (파마리서치 2025
# 사업보고서). 그 보고서에서도 XBRL 원본은 정상적으로 받아진다. 아래 표는
# 필요한 계정만 XBRL 개념명으로 되짚어, 013 인 보고서를 보충한다.
# 이게 없으면 4분기 복원(연간 − 3분기)이 막혀 해당 종목의 최신 TTM 이 안 만들어진다.
XBRL_SUPPLEMENT = [
    ("CF", "ifrs-full_CashFlowsFromUsedInOperatingActivities",
     "영업활동현금흐름", "CashFlowsFromUsedInOperatingActivities"),
    ("CF", "ifrs-full_PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
     "유형자산의 취득", "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"),
    ("CF", "ifrs-full_PurchaseOfIntangibleAssetsClassifiedAsInvestingActivities",
     "무형자산의 취득", "PurchaseOfIntangibleAssetsClassifiedAsInvestingActivities"),
    ("IS", "ifrs-full_Revenue", "매출액", "Revenue"),
    ("IS", "ifrs-full_GrossProfit", "매출총이익", "GrossProfit"),
    ("IS", "dart_OperatingIncomeLoss", "영업이익", "OperatingIncomeLoss"),
    ("IS", "ifrs-full_ProfitLoss", "당기순이익", "ProfitLoss"),
    ("IS", "dart_TotalSellingGeneralAdministrativeExpenses",
     "판매비와관리비", "TotalSellingGeneralAdministrativeExpenses"),
    ("IS", "ifrs-full_SellingGeneralAndAdministrativeExpense",
     "판매비와관리비", "SellingGeneralAndAdministrativeExpense"),
    ("IS", "ifrs-full_ResearchAndDevelopmentExpense",
     "연구개발비", "ResearchAndDevelopmentExpense"),
]


def periodic_rcept(corp_code, year, report, key):
    """정기보고서 접수번호. report 는 REPORTS 의 키."""
    want = {"1q": ".03", "half": ".06", "3q": ".09", "annual": ".12"}[report]
    prefix = {"1q": "분기보고서", "half": "반기보고서",
              "3q": "분기보고서", "annual": "사업보고서"}[report]
    for y in (year, year + 1):
        data = request("list.json", {
            "corp_code": corp_code, "bgn_de": f"{y}0101", "end_de": f"{y}1231",
            "pblntf_ty": "A", "page_count": "100"}, key, allow_empty=True)
        if not data:
            continue
        for r in data.get("list", []):
            if r["report_nm"].startswith(prefix) and f"({year}{want})" in r["report_nm"]:
                return r["rcept_no"]
    return None


def xbrl_rows(corp_code, year, report, key):
    """013 인 보고서를 XBRL 원본으로 보충해 fnlttSinglAcntAll 모양으로 돌려준다.

    연간 보고서의 사실은 1/1~12/31 구간이다. 손익은 그 구간값을
    thstrm_amount 로, 현금흐름은 누계이므로 마찬가지로 thstrm_amount 로 둔다 —
    아래 복원 로직이 기대하는 관습과 같다.
    """
    rcept = periodic_rcept(corp_code, year, report, key)
    if not rcept:
        return []
    # zip 을 한 번만 받는다. 개념마다 xbrl_facts 를 부르면 같은 압축파일을
    # 9번 내려받아 한 보고서에 수 분이 걸린다.
    try:
        by_concept = xbrl_facts_many(
            rcept, REPORTS[report], key,
            [c for _, _, _, c in XBRL_SUPPLEMENT])
    except SystemExit:
        return []

    rows = []
    for sj_div, account_id, account_nm, concept in XBRL_SUPPLEMENT:
        facts = by_concept.get(concept, [])
        # 해당 사업연도의 가장 긴 구간(= 연초 누계 또는 연간)을 고른다
        best, best_days = None, -1
        for f in facts:
            if not f.get("start") or not f.get("end"):
                continue
            if not f["end"].startswith(str(year)):
                continue
            days = (date.fromisoformat(f["end"]) - date.fromisoformat(f["start"])).days
            if days > best_days:
                best, best_days = f, days
        if best is None or best["value"] is None:
            continue
        rows.append({"sj_div": sj_div, "account_id": account_id,
                     "account_nm": account_nm,
                     "thstrm_amount": str(best["value"]),
                     "thstrm_add_amount": str(best["value"]),
                     "_source": "xbrl"})
    return rows


def acct(rows, sj_divs, ids=(), names=()):
    """표준코드 우선, 계정명 폴백으로 계정 한 건을 찾는다."""
    if isinstance(sj_divs, str):
        sj_divs = (sj_divs,)
    pool = [r for r in rows if r.get("sj_div") in sj_divs]
    for want in ids:
        for r in pool:
            if (r.get("account_id") or "").strip() == want:
                return r
    for want in names:
        w = want.replace(" ", "")
        for r in pool:
            if (r.get("account_nm") or "").replace(" ", "") == w:
                return r
    return None


def amount(row, field="thstrm_amount"):
    return None if row is None else to_int(row.get(field))


def fetch_all(corp_code, year, report, fs, key):
    """정기보고서 전체 계정. 없으면 None."""
    return request(
        "fnlttSinglAcntAll.json",
        {"corp_code": corp_code, "bsns_year": str(year),
         "reprt_code": REPORTS[report], "fs_div": fs},
        key, allow_empty=True)


def restore_cf_quarters(by_report, ids, label):
    """누계 현금흐름을 3개월로 되돌린다.

    Q1 = 1분기, Q2 = 반기 − 1분기, Q3 = 3분기 − 반기, Q4 = 연간 − 3분기.
    한 구간이라도 빠지면 그 분기는 만들지 않는다. 없는 값을 0 으로 때우면
    분기 합이 TTM 과 맞아 계약을 통과하면서 내용이 틀린다.
    """
    cumulative = {}
    for report, q in REPORT_SEQUENCE:
        rows = by_report.get(report)
        if not rows:
            continue
        row = acct(rows, "CF", ids)
        value = amount(row)
        if value is not None:
            cumulative[q] = value

    out = {}
    for q in (1, 2, 3, 4):
        if q not in cumulative:
            continue
        if q == 1:
            out[1] = {"val": float(cumulative[1]), "derived": False}
        elif q - 1 in cumulative:
            out[q] = {"val": float(cumulative[q] - cumulative[q - 1]),
                      "derived": True}
    if not out:
        raise LookupError(f"{label}: 현금흐름 계정을 찾지 못했다 ({ids})")
    return out


def restore_is_quarters(by_report, ids, names=()):
    """손익 3개월 값. 4분기만 연간 − 3분기누계로 만든다."""
    out = {}
    for report, q in REPORT_SEQUENCE[:3]:
        rows = by_report.get(report)
        if not rows:
            continue
        row = acct(rows, ("IS", "CIS"), ids, names)
        value = amount(row)
        if value is not None:
            out[q] = {"val": float(value), "derived": False}

    annual_rows, third_rows = by_report.get("annual"), by_report.get("3q")
    if annual_rows and third_rows:
        year_row = acct(annual_rows, ("IS", "CIS"), ids, names)
        ytd_row = acct(third_rows, ("IS", "CIS"), ids, names)
        year_val = amount(year_row)
        # 3분기 보고서의 누계 필드. 3개월 값(thstrm_amount)과 다른 칸이다.
        ytd_val = amount(ytd_row, "thstrm_add_amount")
        if year_val is not None and ytd_val is not None:
            out[4] = {"val": float(year_val - ytd_val), "derived": True}
    return out


def series_rows(per_year, want, end_key):
    """{(fy, q): {...}} 를 계약이 요구하는 정렬된 목록으로 자른다."""
    keys = sorted(per_year)
    if end_key not in keys:
        return None
    stop = keys.index(end_key) + 1
    picked = keys[max(0, stop - want):stop]
    if len(picked) < want:
        return None
    # 연속성 확인. 사이가 비면 TTM 창이 실제로 12개월이 아니다.
    idx = [fy * 4 + q for fy, q in picked]
    if idx != list(range(idx[0], idx[0] + want)):
        return None
    return [{"fy": fy, "quarter": q, **per_year[(fy, q)]} for fy, q in picked]


QUARTER_END = {1: "03-31", 2: "06-30", 3: "09-30", 4: "12-31"}


def cmd_fundamentals(a, key):
    info = resolve(a.stock_code, key)
    corp, name = info["corp_code"], info["corp_name"]
    years = list(range(a.year - 2, a.year + 1))

    raw, by_year, supplemented = {}, {}, []
    for year in years:
        by_report = {}
        for report, _ in REPORT_SEQUENCE:
            data = fetch_all(corp, year, report, a.fs, key)
            if data:
                by_report[report] = data["list"]
                raw[f"{year}_{report}"] = data
                continue
            # 013 — XBRL 원본으로 보충한다. 없는 채로 두면 4분기 복원이 막힌다.
            rows = xbrl_rows(corp, year, report, key)
            if rows:
                by_report[report] = rows
                raw[f"{year}_{report}_xbrl"] = rows
                supplemented.append(f"{year} {report}")
        if by_report:
            by_year[year] = by_report
    if supplemented:
        print(f"※ fnlttSinglAcntAll 이 013 이라 XBRL 원본으로 보충: "
              f"{', '.join(supplemented)}", file=sys.stderr)

    if not by_year:
        fail(f"{name}: {years} 구간에서 재무제표를 받지 못했다")

    # --- 현금흐름 4분기 ---
    ocf, capex, intangible = {}, {}, {}
    for year, by_report in by_year.items():
        try:
            for q, row in restore_cf_quarters(by_report, CF_OCF_IDS, "영업현금흐름").items():
                ocf[(year, q)] = row
            for q, row in restore_cf_quarters(by_report, CF_CAPEX_IDS, "capex").items():
                capex[(year, q)] = row
        except LookupError as exc:
            fail(str(exc))
        try:
            for q, row in restore_cf_quarters(
                    by_report, CF_INTANGIBLE_IDS, "무형자산취득").items():
                intangible[(year, q)] = row
        except LookupError:
            pass  # 무형자산 취득이 없는 회사도 있다. capex 정의에 영향 없음

    latest = max(k for k in ocf if k in capex)
    if a.period_end:
        want = [k for k in sorted(ocf)
                if f"{k[0]}-{QUARTER_END[k[1]]}" == a.period_end]
        if not want:
            fail(f"--period-end {a.period_end} 에 해당하는 분기가 없다")
        latest = want[0]

    ocf_rows = series_rows(ocf, 4, latest)
    capex_rows = series_rows(capex, 4, latest)
    if not ocf_rows or not capex_rows:
        fail(f"{latest} 기준 연속 4분기를 확보하지 못했다. 추정하지 않는다.\n"
             f"  영업현금흐름 확보: {sorted(ocf)}\n  capex 확보: {sorted(capex)}")

    period_end = f"{latest[0]}-{QUARTER_END[latest[1]]}"
    for row in ocf_rows + capex_rows:
        row["end"] = f"{row['fy']}-{QUARTER_END[row['quarter']]}"
        row["filed"] = None

    ocf_ttm = sum(r["val"] for r in ocf_rows)
    capex_ttm = sum(r["val"] for r in capex_rows)

    # --- 손익 8분기 (전년 동기 비교용) ---
    income, expense_lines = {}, []
    plan = [("revenue", IS_REVENUE_IDS, REV_NAMES),
            ("gross_profit", IS_GROSS_IDS, ["매출총이익"]),
            ("operating_income", IS_OPINC_IDS, OP_NAMES),
            ("net_income", IS_NET_IDS, ["당기순이익", "분기순이익", "반기순이익"])]

    present = [(aid, label) for aid, label in IS_EXPENSE_IDS
               if acct(by_year[latest[0]].get("1q") or
                       next(iter(by_year[latest[0]].values())),
                       ("IS", "CIS"), [aid])]
    for aid, label in present:
        key_name = f"expense_{len(expense_lines)}"
        expense_lines.append({"key": key_name, "label": label})
        plan.append((key_name, [aid], [label]))

    for key_name, ids, names in plan:
        per_year = {}
        for year, by_report in by_year.items():
            for q, row in restore_is_quarters(by_report, ids, names).items():
                per_year[(year, q)] = row
        rows = series_rows(per_year, a.income_quarters, latest)
        if rows is None:
            fail(f"손익 {key_name}: {latest} 기준 연속 {a.income_quarters}분기를 "
                 f"확보하지 못했다 (확보: {sorted(per_year)})")
        for row in rows:
            row["end"] = f"{row['fy']}-{QUARTER_END[row['quarter']]}"
            row["filed"] = None
        income[key_name] = {"tag": ids[0], "ttm": sum(r["val"] for r in rows[-4:]),
                            "quarters": rows}

    # 손익 항등식 확인 — 매출총이익 − 비용라인 = 영업이익
    #
    # 이 검사를 `if expense_lines:` 안에 두면 **비용 라인을 하나도 못 찾았을 때
    # 조용히 통과한다** — 정작 가장 크게 틀린 경우다. 파마리서치에서 실제로
    # 매출총이익 1,129억 = 영업이익 573억 이라는 화면이 나왔다 (556억 누락).
    # 그래서 라인이 없어도 항등식을 검사한다.
    gross = income["gross_profit"]["quarters"][-1]["val"]
    spend = sum(income[e["key"]]["quarters"][-1]["val"] for e in expense_lines)
    op = income["operating_income"]["quarters"][-1]["val"]
    residual = gross - spend - op
    if abs(residual) > max(abs(op) * 0.005, 1e8):
        detail = (", ".join(f"{e['label']} {income[e['key']]['quarters'][-1]['val']/1e8:,.0f}억"
                            for e in expense_lines)
                  or "찾은 비용 라인 없음")
        fail("손익 항등식이 맞지 않는다: 매출총이익 "
             f"{gross/1e8:,.0f}억 − ({detail}) = {(gross-spend)/1e8:,.0f}억 인데 "
             f"영업이익은 {op/1e8:,.0f}억 이다 (차이 {residual/1e8:,.0f}억).\n"
             "  IS_EXPENSE_IDS 에 이 회사의 비용 계정 표준코드를 추가한다. "
             "원문 계정 목록: dart_fetch.py financials 로 확인")

    # --- 재무상태 시점값 ---
    bs_report = None
    for report, q in REPORT_SEQUENCE:
        if q == latest[1] and by_year.get(latest[0], {}).get(report):
            bs_report = by_year[latest[0]][report]
    if bs_report is None:
        fail(f"{period_end} 재무상태표를 찾지 못했다")

    cash_row = acct(bs_report, "BS", BS_CASH_IDS, ["현금및현금성자산"])
    if cash_row is None:
        fail("현금및현금성자산을 찾지 못했다. 0 으로 때우면 순부채가 틀린다")
    cash = float(amount(cash_row))

    short_inv, short_inv_detail = 0.0, []
    for aid in BS_SHORT_INV_IDS:
        row = acct(bs_report, "BS", [aid])
        if row is not None and amount(row) is not None:
            short_inv += float(amount(row))
            short_inv_detail.append({"label": row.get("account_nm"),
                                     "account_id": aid, "value": float(amount(row))})
    if not short_inv_detail:
        row = acct(bs_report, "BS", (), ["단기금융상품"])
        if row is not None and amount(row) is not None:
            short_inv = float(amount(row))
            short_inv_detail.append({"label": "단기금융상품", "account_id": None,
                                     "value": short_inv})

    debt, debt_detail, seen = 0.0, [], set()
    for aid, label in BS_DEBT_IDS:
        row = acct(bs_report, "BS", [aid]) if not aid.startswith("-") else None
        if row is None:
            row = acct(bs_report, "BS", (), [label])
        if row is None:
            continue
        marker = (row.get("account_nm"), row.get("thstrm_amount"))
        if marker in seen or amount(row) is None:
            continue
        seen.add(marker)
        debt += float(amount(row))
        debt_detail.append({"label": row.get("account_nm"),
                            "account_id": row.get("account_id"),
                            "value": float(amount(row))})
    if not debt_detail:
        fail("차입금 계정을 하나도 찾지 못했다. 무차입이라고 단정하지 않는다")

    # --- 희석주식수: 지배주주 순이익 ÷ 희석주당이익 ---
    parent_row = acct(bs_report, ("IS", "CIS"), IS_NET_PARENT_IDS, ["지배기업 소유주지분"])
    eps_row = acct(bs_report, ("IS", "CIS"), IS_DILUTED_EPS_IDS, ["희석주당이익(손실)"])
    parent_ni, diluted_eps = amount(parent_row), amount(eps_row)
    if not parent_ni or not diluted_eps:
        fail("희석주당이익 또는 지배기업 소유주지분 순이익을 찾지 못했다. "
             "주식수를 기억으로 채우지 않는다")
    # 분기 EPS 이므로 분기 지배주주 순이익으로 나눈다. 같은 기간이라 상쇄된다.
    shares = float(parent_ni) / float(diluted_eps)

    total = request("stockTotqySttus.json",
                    {"corp_code": corp, "bsns_year": str(a.year - 1),
                     "reprt_code": REPORTS["annual"]}, key, allow_empty=True)
    # isu_stock_totqy 는 **발행할** 주식의 총수(수권주식)다 — 삼성전자는 250억주로
    # 실제 발행주식과 4배 가까이 다르다. 합계 행을 그대로 쓰면 대조가 무의미하다.
    # 실제 유통주식은 종류주식별 distb_stock_co 의 합이다.
    share_cross = None
    if total:
        raw["stockTotqySttus"] = total
        outstanding = 0.0
        for r in total.get("list", []):
            if (r.get("se") or "").strip() in ("합계", "비고"):
                continue
            value = to_int(r.get("distb_stock_co"))
            if value:
                outstanding += float(value)
        share_cross = outstanding or None
    if share_cross and abs(shares / share_cross - 1) > 0.05:
        print(f"⚠ 희석주식수 {shares:,.0f} 와 유통주식수 합 {share_cross:,.0f} 가 "
              f"{abs(shares/share_cross-1)*100:.1f}% 어긋난다. "
              "우선주·전환증권 취급을 확인할 것", file=sys.stderr)

    raw_path = save_raw(f"fundamentals_{a.stock_code}", raw)

    values = {
        "ocf_ttm": ocf_ttm,
        "capex_ttm": capex_ttm,
        "fcf_ttm": ocf_ttm - capex_ttm,
        "shares_diluted": shares,
        "cash_and_equivalents": cash,
        "short_term_investments": short_inv,
        "long_term_debt": debt,
        "net_debt": debt - cash - short_inv,
    }

    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "instrument_id": a.stock_code,
        "company_name": name,
        "corp_code": corp,
        "currency": "KRW",
        "period_end": period_end,
        "created_at": datetime.now(timezone.utc).astimezone().isoformat(
            timespec="seconds"),
        "source": "opendart_fnlttSinglAcntAll",
        "fs_div": a.fs,
        "definitions": {
            # 이 두 줄이 없으면 다른 시장 스냅샷과 말없이 다른 것을 비교하게 된다.
            "capex": "유형자산의 취득만. 무형자산 취득은 intangible_capex_ttm 에 따로 둔다"
                     " (EDGAR 판의 PaymentsToAcquirePropertyPlantAndEquipment 와 같은 기준)",
            "long_term_debt": "총차입 (단기차입금·사채·장기차입금 합). 계정명은 "
                              "debt_detail 참조. 장기만 세면 순부채가 틀린다",
            "shares_diluted": "지배기업 소유주지분 순이익 ÷ 희석주당이익",
            "cash_flow_period": "현금흐름표는 연초 누계로 들어오므로 차감 복원했다 "
                                "(derived=true 인 분기)",
            "income_period": "손익계산서 thstrm_amount 는 이미 3개월이다. "
                             "4분기만 연간 − 3분기누계로 만들었다",
        },
        "quarters": {
            "ocf": [{k: r[k] for k in ("fy", "quarter", "end", "filed", "val", "derived")}
                    for r in ocf_rows],
            "capex": [{k: r[k] for k in ("fy", "quarter", "end", "filed", "val", "derived")}
                      for r in capex_rows],
        },
        "values": values,
        "intangible_capex_ttm": (
            sum(intangible[k]["val"] for k in [(r["fy"], r["quarter"]) for r in ocf_rows]
                if k in intangible)
            if all((r["fy"], r["quarter"]) in intangible for r in ocf_rows) else None),
        "short_term_investments_detail": short_inv_detail,
        "debt_detail": debt_detail,
        "expense_lines": expense_lines,
        "income_statement": income,
        "share_count_cross_check": share_cross,
        "raw_locator": str(raw_path.relative_to(ROOT)),
    }
    snapshot["fundamentals_id"] = fundamentals_id_for(snapshot)

    try:
        path = save_validated_fundamentals(a.out_dir, snapshot)
    except ContractError as exc:
        for message in exc.errors:
            print(f"INVALID: {message}", file=sys.stderr)
        return 1

    unit = 1e8
    print(f"{name} ({a.stock_code}) — 기준 {period_end} / 단위 억원 / "
          f"{'연결' if a.fs == 'CFS' else '별도'}")
    print("=" * 74)
    print(f"{'영업현금흐름 TTM':<22}{ocf_ttm/unit:>14,.0f}")
    print(f"{'capex TTM (유형)':<22}{capex_ttm/unit:>14,.0f}")
    print(f"{'FCF TTM':<22}{values['fcf_ttm']/unit:>14,.0f}   = 영업현금흐름 − capex")
    if snapshot["intangible_capex_ttm"]:
        print(f"{'  (무형자산 취득 TTM)':<22}"
              f"{snapshot['intangible_capex_ttm']/unit:>14,.0f}   ※ capex 에 미포함")
    print(f"{'희석주식수':<22}{shares/1e6:>14,.1f}M")
    print(f"{'순부채':<22}{values['net_debt']/unit:>14,.0f}   "
          f"(차입 {debt/unit:,.0f} − 현금 {cash/unit:,.0f} − 단기투자 {short_inv/unit:,.0f})")
    print(f"\n[ 현금흐름 분기 복원 — 누계 차감 ]")
    for o, c in zip(ocf_rows, capex_rows):
        mark = "차감" if o["derived"] else "직접"
        print(f"  FY{o['fy']} Q{o['quarter']}  {o['end']}  "
              f"영업 {o['val']/unit:>9,.0f}  capex {c['val']/unit:>9,.0f}  "
              f"FCF {(o['val']-c['val'])/unit:>9,.0f}  ({mark})")
    print(f"\n[ 손익 항등식 — 최근 분기 ]")
    print(f"  매출총이익 {income['gross_profit']['quarters'][-1]['val']/unit:,.0f}", end="")
    for e in expense_lines:
        print(f" − {e['label']} {income[e['key']]['quarters'][-1]['val']/unit:,.0f}",
              end="")
    print(f" = 영업이익 {income['operating_income']['quarters'][-1]['val']/unit:,.0f}")
    print(f"\n[ 차입 내역 ]")
    for d in debt_detail:
        print(f"  {d['label']:<24}{d['value']/unit:>12,.0f}억")
    def show(target):
        # --out-dir 이 저장소 밖일 수 있다 (재현 테스트는 임시 디렉터리를 쓴다).
        try:
            return target.relative_to(ROOT)
        except ValueError:
            return target

    print(f"\nfundamentals_id  {snapshot['fundamentals_id']}")
    print(f"저장: {show(path)}")
    print(f"원본: {show(raw_path)}")
    return 0


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

    d = sub.add_parser("fundamentals",
                       help="역산 DCF 입력 스냅샷 (TTM FCF·희석주식수·순부채)")
    d.add_argument("stock_code")
    d.add_argument("--year", type=int, required=True,
                   help="가장 최근 분기가 속한 사업연도. 앞 2년까지 함께 받는다")
    d.add_argument("--period-end", help="기준 분기말 YYYY-MM-DD (기본: 확보된 최근 분기)")
    d.add_argument("--fs", choices=["CFS", "OFS"], default="CFS")
    d.add_argument("--income-quarters", type=int, default=8,
                   help="손익 시계열 분기 수. 전년 동기 비교에 최소 5개가 필요하다")
    d.add_argument("--out-dir", default=str(ROOT / "knowledge" / "fundamentals"))

    a = p.parse_args()
    key = load_key()
    return {
        "resolve": cmd_resolve,
        "filings": cmd_filings,
        "financials": cmd_financials,
        "quarters": cmd_quarters,
        "fundamentals": cmd_fundamentals,
    }[a.cmd](a, key)


if __name__ == "__main__":
    sys.exit(main() or 0)
