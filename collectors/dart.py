#!/usr/bin/env python3
"""OpenDART 원문 수집기.

DART 공시검색 화면은 JavaScript 기반이라 웹 도구로 목록을 가져올 수 없다.
이 스크립트는 그 지점을 대체한다 - 원문 API에서 직접 받아 시점과 함께 보존한다.

모든 응답은 data/raw/dart/ 아래에 받은 그대로 저장한다.
정규화 전 원본을 남겨 재현과 감사의 기준으로 삼는다.

인증키는 절대 출력하지 않는다.
"""

import argparse
import contextlib
import io
import itertools
import json
import math
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from xml.etree import ElementTree as ET
from datetime import date, datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

from calculators.contracts import change_pct, ratio_pct
from cio.display import cell, pad
from cio.documents import archive_filing_rows, stored_filing_keys

BASE = "https://opendart.fss.or.kr/api"
ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw" / "dart"
KNOWLEDGE_DIR = ROOT / "knowledge"
CACHE_DIR = ROOT / "data" / "cache"

from calculators.contracts import (
    SCHEMA_VERSION,
    ContractError,
    canonical_payload_hash,
    fundamentals_differences,
    fundamentals_id_for,
    load_raw_snapshot,
    save_validated_fundamentals,
)

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

    전체계정 API가 빈 응답이면 주요계정 API로 폴백한다.
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
    """원본을 내용 주소로 한 번만 보존한다."""
    stamp = datetime.now(timezone.utc)
    digest = canonical_payload_hash(payload)
    out = RAW_DIR / "objects"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{digest}.json"
    if path.exists():
        load_raw_snapshot(
            path,
            expected_digest=digest,
            expected_source="opendart.fss.or.kr",
        )
        return path
    document = {
        "fetched_at": stamp.isoformat(timespec="seconds"),
        "source": "opendart.fss.or.kr",
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


def xbrl_facts(rcept_no, reprt, key, concept, want_dims=("ConsolidatedMember",),
               exclude_dims=("OrdinarySharesMember", "PreferredSharesMember")):
    """정기보고서의 XBRL 원본에서 개념 값을 뽑는다.

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


FILING_DIR = ROOT / "knowledge" / "filings"
FILING_INDEX_LIMIT = 60
DART_VIEWER = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"

# 공시유형을 지정하지 않으면 임원·주요주주 소유상황보고서 같은 지분공시가
# 목록을 뒤덮는다. 삼성전자는 그것만 하루 십수 건이라 정기보고서와 잠정실적이
# 첫 페이지에서 밀려난다. 필요한 갈래만 따로 받아 합친다.
DART_FILING_TYPES = (
    ("A", "정기공시"),
    ("B", "주요사항보고"),
    ("C", "발행공시"),
    ("I", "거래소공시"),
    ("F", "외부감사관련"),
)

# DART 는 SEC 의 form 코드 같은 것이 없고 보고서 이름만 준다. 이름으로 최소한의
# 갈래만 나눈다. 분류에 실패하면 OTHER 로 두고, 판단은 원래 이름(description)을
# 읽는 모델이 한다 — 여기서 뜻을 지어내지 않는다.
DART_FORM_RULES = (
    ("ANNUAL_REPORT", ("사업보고서",)),
    ("HALF_REPORT", ("반기보고서",)),
    ("QUARTER_REPORT", ("분기보고서",)),
    ("AUDIT_REPORT", ("감사보고서",)),
    ("MATERIAL_EVENT", ("주요사항보고서",)),
    ("MATERIAL_EVENT", ("증권신고서", "투자설명서")),
    ("PROVISIONAL_RESULT", ("잠정실적", "영업(잠정)실적", "손익구조")),
    ("FAIR_DISCLOSURE", ("공정공시",)),
    ("EQUITY_CHANGE", ("유상증자", "무상증자", "전환사채", "신주인수권", "자기주식")),
    ("GOVERNANCE", ("합병", "분할", "영업양수", "영업양도", "주주총회")),
)
DART_PRIMARY_REPORT_FORMS = {
    "ANNUAL_REPORT",
    "HALF_REPORT",
    "QUARTER_REPORT",
    "AUDIT_REPORT",
}
PERIOD_MARK = re.compile(r"\((\d{4})\.(\d{2})\)")
LAST_DAY = {1: 31, 2: 29, 3: 31, 4: 30, 5: 31, 6: 30,
            7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31}


def classify_dart_form(report_name):
    for form, keywords in DART_FORM_RULES:
        if any(keyword in report_name for keyword in keywords):
            return form
    return "OTHER"


def dart_report_date(report_name):
    """정기보고서 이름의 (YYYY.MM) 표기에서 대상 기간말을 읽는다.

    표기가 없으면 None 이다. 접수일에서 기간을 역산하지 않는다 — 그것은
    추측이고, 이 색인은 추측을 담지 않는다.
    """
    match = PERIOD_MARK.search(report_name or "")
    if not match:
        return None
    year, month = int(match.group(1)), int(match.group(2))
    if not 1 <= month <= 12:
        return None
    day = LAST_DAY[month]
    if month == 2 and not (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)):
        day = 28
    return f"{year:04d}-{month:02d}-{day:02d}"


def filing_rows_from_list(payload, *, stock_code, limit=FILING_INDEX_LIMIT):
    """DART list.json 원문에서 공시 색인 행을 만든다.

    수집기와 검증기가 같은 함수를 쓴다. 그래야 저장된 색인이 원문에서 다시
    나오는지 기계로 확인할 수 있다.
    """
    if not isinstance(payload, dict):
        raise ContractError(["DART list.json 원문이 객체가 아니다"])
    rows = payload.get("list")
    if not isinstance(rows, list):
        raise ContractError(["DART list.json 에 list 배열이 없다"])
    result = []
    for row in rows:
        if not isinstance(row, dict):
            raise ContractError(["DART 공시 행이 객체가 아니다"])
        if str(row.get("stock_code") or "").strip() != stock_code:
            continue
        rcept_no = str(row.get("rcept_no") or "").strip()
        rcept_dt = str(row.get("rcept_dt") or "").strip()
        report_name = str(row.get("report_nm") or "").strip()
        if not re.fullmatch(r"\d{14}", rcept_no):
            raise ContractError([f"DART 접수번호 형식이 아니다: {rcept_no}"])
        if not re.fullmatch(r"\d{8}", rcept_dt):
            raise ContractError([f"DART 접수일 형식이 아니다: {rcept_dt}"])
        if not report_name:
            raise ContractError([f"DART 보고서 이름이 없다: {rcept_no}"])
        result.append({
            "filing_date": f"{rcept_dt[:4]}-{rcept_dt[4:6]}-{rcept_dt[6:]}",
            "report_date": dart_report_date(report_name),
            "form": classify_dart_form(report_name),
            "accession_number": rcept_no,
            "description": report_name,
            "filer": str(row.get("flr_nm") or "").strip() or None,
            "remark": str(row.get("rm") or "").strip() or None,
            "document_url": DART_VIEWER.format(rcept_no=rcept_no),
        })
    return merge_filing_rows([result], limit=limit)


def merge_filing_rows(row_groups, *, limit=FILING_INDEX_LIMIT):
    """공시유형별로 받은 목록을 접수번호 기준으로 합친다."""
    merged = {}
    for rows in row_groups:
        for row in rows:
            merged[row["accession_number"]] = row
    ordered = sorted(
        merged.values(),
        key=lambda item: (item["filing_date"], item["accession_number"]),
        reverse=True,
    )
    return ordered[:limit]


def fetch_document(rcept_no, key):
    """접수번호로 공시 원문을 받아 내용 주소로 보존하고 (문서, 경로) 를 준다.

    DART 뷰어 주소(dsaf001/main.do)는 iframe 껍데기라 그대로 저장해도 본문이
    없다. OpenDART document.xml 은 원문 XML 을 zip 으로 준다 — 근거로 인용할
    수 있는 것은 이쪽이다.
    """
    rcept_no = str(rcept_no).strip()
    if not re.fullmatch(r"\d{14}", rcept_no):
        fail("접수번호는 14자리 숫자다")
    blob = request("document.xml", {"rcept_no": rcept_no}, key, binary=True)
    try:
        archive = zipfile.ZipFile(io.BytesIO(blob))
        names = [n for n in archive.namelist() if not n.endswith("/")]
        if not names:
            fail(f"{rcept_no}: 원문 zip 이 비어 있다")
        documents = {}
        for name in names:
            raw = archive.read(name)
            for charset in ("utf-8", "euc-kr", "cp949"):
                try:
                    documents[name] = raw.decode(charset)
                    break
                except UnicodeDecodeError:
                    continue
            else:
                documents[name] = raw.decode("utf-8", errors="replace")
    except zipfile.BadZipFile:
        # 조회 실패는 zip 이 아니라 상태 XML 로 돌아온다.
        detail = blob[:400].decode("utf-8", errors="replace")
        fail(f"{rcept_no}: 원문을 받지 못했다 — {detail}")
    payload = {"rcept_no": rcept_no, "documents": documents}
    return documents, save_raw(f"document_{rcept_no}", payload)


def cmd_document(a, key):
    rcept_no = a.rcept_no.strip()
    documents, path = fetch_document(rcept_no, key)
    total = sum(len(text) for text in documents.values())
    print(f"접수번호 {rcept_no}  문서 {len(documents)}개  {total:,}자")
    print(f"원문 저장: {path.relative_to(ROOT)}")
    print(f"인용 ID: DARTDOC:{rcept_no}")
    for name in sorted(documents):
        print(f"  {name}  {len(documents[name]):,}자")


# 색인에서 원문까지 자동으로 받아 둘 공시. **잠정실적만이다.**
#
# 정기보고서는 100만 자라 이 배선이 감당할 문서가 아니고, 필요한 자리만
# `cio search` 로 읽는 것이 이 저장소의 방식이다. 잠정실적 공정공시는 2,285자에
# 분기당 한 건이며 정기보고서보다 먼저 나와 분기 사이의 공백을 메운다 —
# 파마리서치의 품목군별 매출은 정기보고서에도 없고 여기에만 있다.
AUTO_DOCUMENT_FORMS = ("PROVISIONAL_RESULT",)
AUTO_DOCUMENT_LIMIT = 8


def sync_provisional_documents(asset_id, *, key, limit=AUTO_DOCUMENT_LIMIT):
    """색인에 있는 잠정실적 공시 중 원문을 아직 안 받은 것을 받는다.

    색인에 있는데 안 받아서 못 본 것은 데이터가 없는 것이 아니다. 2026-08-07
    판단은 접수번호까지 알면서 원문 수집을 사용자 대기열로 넘겼고, 그 안에 있던
    품목군별 매출이 이틀 뒤에야 논지를 바꿨다.

    이미 받은 것은 다시 받지 않는다. 최신부터 받는 것은 상한에 걸릴 때 이번
    분기 실적이 먼저 남아야 하기 때문이다.
    """
    index_path = KNOWLEDGE_DIR / "filings" / f"{asset_id}.json"
    stamp = datetime.now(timezone.utc).astimezone()
    result = {
        "asset_id": asset_id,
        "collected_at": stamp.isoformat(timespec="seconds"),
        "forms": list(AUTO_DOCUMENT_FORMS),
        "fetched": [],
        "skipped": [],
        "failed": [],
        "missing_index": False,
    }
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        result["missing_index"] = True
        return result

    rows = []
    for filing in index.get("filings") or []:
        if not isinstance(filing, dict):
            continue
        if filing.get("form") not in AUTO_DOCUMENT_FORMS:
            continue
        rcept_no = str(filing.get("accession_number") or "").strip()
        if not re.fullmatch(r"\d{14}", rcept_no):
            continue
        rows.append((str(filing.get("filing_date") or ""), rcept_no))
    rows.sort(reverse=True)

    # 원문 보유 여부를 모르면(None) 아무것도 받지 않는다. 모르는 것을 "없다"로
    # 읽으면 refresh 마다 색인 전체를 다시 받는다.
    stored = stored_filing_keys(asset_id)
    if stored is None:
        result["skipped"] = [rcept_no for _, rcept_no in rows]
        _write_document_marker(asset_id, result)
        return result

    for _, rcept_no in rows:
        if rcept_no in stored:
            result["skipped"].append(rcept_no)
            continue
        if len(result["fetched"]) >= limit:
            result["skipped"].append(rcept_no)
            continue
        try:
            fetch_document(rcept_no, key)
        except (OSError, SystemExit, ValueError) as exc:
            result["failed"].append({"rcept_no": rcept_no, "reason": str(exc)})
            continue
        result["fetched"].append(rcept_no)
    _write_document_marker(asset_id, result)
    return result


def _write_document_marker(asset_id, result):
    """무엇을 받았고 무엇을 못 받았는지의 표식.

    안 쓰면 "받을 것이 없었다"와 "안 돌았다"가 구분되지 않고, refresh 는
    산출물이 없다는 이유로 영원히 다시 돈다.
    """
    out = KNOWLEDGE_DIR / "filings" / "_documents"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{asset_id}.json"
    temporary_name = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=out, delete=False
        ) as temporary:
            json.dump(result, temporary, ensure_ascii=False, indent=2)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_name = temporary.name
        os.replace(temporary_name, path)
    finally:
        if temporary_name and Path(temporary_name).exists():
            Path(temporary_name).unlink()
    return path


def cmd_documents(a, key):
    result = sync_provisional_documents(a.stock_code, key=key)
    if result["missing_index"]:
        print(f"{a.stock_code}: 공시 색인이 없다 — 먼저 `filings` 를 받는다")
        return 1
    print(
        f"{a.stock_code} 잠정실적 원문  "
        f"받음 {len(result['fetched'])} / 이미 있음 {len(result['skipped'])} / "
        f"실패 {len(result['failed'])}"
    )
    for rcept_no in result["fetched"]:
        print(f"  받음   DARTDOC:{rcept_no}")
    for row in result["failed"]:
        print(f"  실패   {row['rcept_no']}  {row['reason']}")
    return 1 if result["failed"] and not result["fetched"] else 0


def _indexed_rows(index_path):
    """지금 저장돼 있는 공시 색인의 행. 없거나 못 읽으면 빈 목록."""
    try:
        rows = json.loads(Path(index_path).read_text(encoding="utf-8")).get("filings")
    except (OSError, ValueError, AttributeError):
        return []
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def cmd_filings(a, key):
    info = resolve(a.stock_code, key)
    since = (a.since or "").replace("-", "")
    until = (a.until or date.today().strftime("%Y%m%d")).replace("-", "")
    raw_sources = []
    row_groups = []
    for type_code, type_name in DART_FILING_TYPES:
        data = request(
            "list.json",
            {
                "corp_code": info["corp_code"],
                "bgn_de": since,
                "end_de": until,
                "pblntf_ty": type_code,
                "page_count": "100",
            },
            key,
            # 해당 유형의 공시가 하나도 없는 것은 정상이다.
            allow_empty=True,
        )
        if data is None:
            # 이 기간에 해당 유형 공시가 없다. 보존할 원문이 없으므로
            # 비어 있었다는 사실만 남긴다.
            raw_sources.append({
                "pblntf_ty": type_code,
                "pblntf_ty_name": type_name,
                "empty": True,
            })
            continue
        path = save_raw(f"filings_{a.stock_code}_{type_code}", data)
        raw_sources.append({
            "pblntf_ty": type_code,
            "pblntf_ty_name": type_name,
            "raw_payload_sha256": path.stem,
            "raw_locator": str(path.relative_to(ROOT)),
        })
        try:
            row_groups.append(
                filing_rows_from_list(data, stock_code=a.stock_code)
            )
        except ContractError as exc:
            fail(f"{type_name}: " + "; ".join(exc.errors))
    rows = merge_filing_rows(row_groups)
    if not rows:
        fail(f"{a.stock_code}: {a.since} 이후 이 종목의 공시가 없다")
    snapshot = {
        "schema_version": "filing-index/1.0.0",
        "asset_id": a.stock_code,
        "corp_code": info["corp_code"],
        "collected_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "DART_LIST",
        "since": a.since,
        "until": until,
        "pblntf_types": [code for code, _ in DART_FILING_TYPES],
        "raw_sources": raw_sources,
        "filings": rows,
    }
    FILING_DIR.mkdir(parents=True, exist_ok=True)
    index_path = FILING_DIR / f"{a.stock_code}.json"
    # 색인은 60건에서 잘린다. 덮어쓰기 전에, 곧 창 밖으로 밀릴 행까지 자산별
    # 보관 목록에 남긴다 — 받아 둔 원문과 자산의 연결이 색인과 함께 사라지지 않게.
    archive_filing_rows(a.stock_code, [*_indexed_rows(index_path), *rows])
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=FILING_DIR, delete=False
        ) as handle:
            json.dump(snapshot, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            tmp = handle.name
        os.replace(tmp, index_path)
    finally:
        if tmp and Path(tmp).exists():
            Path(tmp).unlink()

    print(f"{info['corp_name']} 공시 {len(rows)}건  ({a.since} ~ {a.until or '오늘'})")
    print(f"원본 저장: {path.relative_to(ROOT)}")
    print(f"색인 저장: {index_path.relative_to(ROOT)}")
    print("-" * 88)
    for r in rows:
        note = f"  [{r['remark']}]" if r.get("remark") else ""
        print(
            f"{r['filing_date']}  {r['form']:<19}"
            f"{r['description'][:44]:<46} {r['accession_number']}{note}"
        )


REV_NAMES = ["매출액", "수익(매출액)", "영업수익", "순이자이익"]
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
    """공시 금액 문자열 → 정수. 못 읽으면 None.

    예전에는 `int(str(v))` 하나였다. 현재 코퍼스(계정행 6,224건)에서는 전부
    쉼표 섞인 정수라 문제가 없었지만, 그 말은 **다른 형태가 오면 조용히 None
    이 된다**는 뜻이다. None 은 "계정을 못 찾았다"와 구분되지 않으므로 값이
    있는데도 결측으로 처리되고, 그 자리는 화면에서 `—` 로만 남는다.

    막는 형태는 셋이다. 소수점(주당이익은 원 단위 소수로 올 수 있다),
    지수 표기(XBRL 직렬화기가 내기도 한다), 괄호 음수(회계 관행 표기).
    """
    if v is None:
        return None
    s = str(v).replace(",", "").replace(" ", "").strip()
    if not s or s in ("-", "—"):
        return None
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1].strip()
    try:
        return int(s)
    except ValueError:
        pass
    try:
        f = float(s)
    except ValueError:
        return None
    return round(f) if math.isfinite(f) else None


# ---------------------------------------------------------------------------
# 역산 DCF 입력 스냅샷 (fundamentals)
# ---------------------------------------------------------------------------
#
# EDGAR fundamentals와 같은 계약을 만들지만 두 가지가 다르다.
#
# 1) 손익 thstrm_amount는 해당 분기, 현금흐름은 연초 누계이므로 현금흐름만
#    차감 복원한다.
# 2) K-IFRS 비용 표시는 회사마다 달라 비용 라인을 순서 있는 목록으로 보존하고
#    매출총이익 − 비용 = 영업이익 항등식으로 검증한다.
#
# 계정은 account_nm 보다 account_id(IFRS/DART 표준코드)를 먼저 본다.
# 표기는 회사마다 바뀌지만 표준코드는 안 바뀐다.

REPORT_SEQUENCE = [("1q", 1), ("half", 2), ("3q", 3), ("annual", 4)]

# 현금흐름 — 누계로 들어오므로 차감 복원한다
CF_OCF_IDS = ["ifrs-full_CashFlowsFromUsedInOperatingActivities"]
CF_CAPEX_IDS = [
    "ifrs-full_PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"]
CF_CAPEX_NAMES = [
    "유형자산의 취득",
    "기타의 유형자산의 취득",
    "기타유형자산의 취득",
]
CF_INTANGIBLE_IDS = [
    "ifrs-full_PurchaseOfIntangibleAssetsClassifiedAsInvestingActivities"]

# 손익 — 이미 3개월이다
IS_REVENUE_IDS = [
    "ifrs-full_Revenue",
    # 금융업 은행·보험지주 fallback: 순이자이익
    "ifrs-full_InterestRevenueExpense",
]
IS_GROSS_IDS = [
    "ifrs-full_GrossProfit",
    # 금융업 fallback: 매출총이익 개념이 없어 영업이익이 그대로다.
    # gross = op 이면 항등식에서 비용 0 조합이 성립한다.
    "dart_OperatingIncomeLoss",
    "ifrs-full_ProfitLossFromOperatingActivities",
]
IS_OPINC_IDS = ["dart_OperatingIncomeLoss", "ifrs-full_ProfitLossFromOperatingActivities"]
IS_NET_IDS = ["ifrs-full_ProfitLoss"]
IS_NET_PARENT_IDS = ["ifrs-full_ProfitLossAttributableToOwnersOfParent"]
IS_DILUTED_EPS_IDS = ["ifrs-full_DilutedEarningsLossPerShare"]
IS_BASIC_EPS_IDS = ["ifrs-full_BasicEarningsLossPerShare"]
# 매출총이익에서 영업이익까지 빼는 비용 라인. 회사마다 개수가 다르다.
IS_EXPENSE_IDS = [
    ("dart_TotalSellingGeneralAdministrativeExpenses", "판매비와관리비"),
    # 회사마다 표준코드와 IS/CIS 위치가 달라 후보를 넓게 잡고 항등식으로 검증한다.
    ("ifrs-full_SellingGeneralAndAdministrativeExpense", "판매비와관리비"),
    ("ifrs-full_ResearchAndDevelopmentExpense", "연구개발비"),
    ("dart_OrdinaryDevelopmentExpense", "연구개발비"),
    ("dart_SellingExpenses", "판매비"),
    ("dart_AdministrativeExpenses", "관리비"),
]

# 재무상태 — 시점값
BS_CASH_IDS = ["ifrs-full_CashAndCashEquivalents"]
# 순부채에서 현금과 함께 빼는 단기 금융투자. 차입 쪽과 같은 이유로 표준코드와
# 계정명을 둘 다 본다(_account_code 로 접두사를 걷어낸 코드).
#
# 예전 목록은 코드 셋뿐이었고, 그 밖의 코드로 내는 회사는 단기투자가 0 으로
# 잡혔다. SK하이닉스의 단기투자자산 14.9조(CurrentInvestments)와 파마리서치의
# 유동성금융자산 3,753억(OtherCurrentFinancialAssets — 단기금융상품 3,178억 +
# 유동 당기손익금융자산 575억)이 그렇게 빠졌다. 파마리서치는 판단이 이 누락을
# 여섯 번 요청했고 회사의 자본관리 주석도 그 금융상품을 현금과 함께 차감한다.
# 차입만 고치고 이쪽을 두면 순부채가 반대 방향으로 더 틀린다.
BS_SHORT_INV_CODES = (
    "ShorttermDepositsNotClassifiedAsCashEquivalents",
    "CurrentFinancialAssetsAtFairValueThroughProfitOrLoss",
    "ShortTermTradingFinancialAssets",
    "CurrentInvestments",
    "CurrentFinancialAssetsAtAmortisedCost",
)
# 코드가 뭉뚱그린 것(OtherCurrentFinancialAssets 등)이거나 표준코드를 안 써도
# 이름이 단기 금융투자임을 말하면 센다. 공백은 지우고 대조한다. 측정 범주가 붙은
# 이름(유동성상각후원가측정금융자산, 단기당기손익-공정가치금융자산)도 같은 줄이다.
BS_SHORT_INV_NAME = re.compile(
    r"^(?:유동성?|단기)(?:금융상품|투자자산|금융자산)$"
    r"|^(?:유동성?|단기)\S*(?:상각후원가|당기손익)\S*(?:금융자산|유가증권)$")
# 현금성일 수 있는데 위에 없는 유동 금융자산을 찾는 그물. **합산에는 쓰지 않는다**
# — '기타금융자산'에는 대여금·보증금이 섞이기 때문이다. 걸린 행은
# short_term_investments_unclassified 로 값과 함께 남긴다.
BS_SHORT_INV_CANDIDATE_CODE = re.compile(
    r"^(?:Other)?Current\w*(?:FinancialAssets|Investments)\w*$")
BS_SHORT_INV_CANDIDATE_NAME = re.compile(r"(?:유동|단기)\S*(?:금융자산|금융상품|투자자산)")
# 파생상품자산·채권(받을 돈)·대여금은 현금성이 아니고, 비유동·장기는 단기투자가
# 아니다.
BS_NOT_SHORT_INV_NAME = re.compile(r"파생|비유동|장기|채권|대여")
BS_NOT_SHORT_INV_CODE = re.compile(r"Derivative|Noncurrent|NonCurrent|LongTerm")
# 총차입으로 세는 계정. 분류마다 (표준계정코드, 계정명) 을 둘 다 둔다.
#
# 코드만 보면 안 되는 이유: 같은 계정이 회사마다 ifrs-full_ / ifrs_ / dart_ 로
# 들어오고, 아예 '-표준계정코드 미사용-' 인 회사도 있다. 이름만 보면 안 되는
# 이유: 같은 뜻을 유동리스부채·유동 리스부채·유동금융리스부채로 달리 쓴다.
# 한쪽만 보면 차입이 통째로 빠지는데, 그러면 순부채가 조용히 틀린다 —
# 파마리서치 2026 1Q 는 이 누락으로 4.3억으로 잡혔고 실제는 92억이었다.
#
# 코드는 접두사를 걷어내고 대조한다(_account_code). 그래야 ifrs-full_ 과
# ifrs_ 가 같은 계정으로 잡힌다.
BS_DEBT_ACCOUNTS = (
    # 'CurrentBorrowingsAndCurrentPortionOfNoncurrentBorrowings' 와
    # 'LongtermBorrowings' 는 IFRS 표준 개념인데 오래 빠져 있었다. SK하이닉스가
    # 차입금을 이 두 코드에 계정명 '차입금' 으로만 내서, 2026 반기 차입금 18.6조가
    # 통째로 빠지고 리스부채 2.5조만 총차입으로 저장됐다(순현금이 그만큼 부풀었다).
    ("BORROWING",
     ("ShorttermBorrowings", "LoansReceived", "LongTermBorrowingsGross",
      "NoncurrentPortionOfNoncurrentLoans",
      "NoncurrentPortionOfNoncurrentLoansReceived",
      "CurrentPortionOfLongtermBorrowings",
      "CurrentLoansReceivedAndCurrentPortionOfNoncurrentLoansReceived",
      "CurrentBorrowingsAndCurrentPortionOfNoncurrentBorrowings",
      "LongtermBorrowings", "CurrentLoansReceived",
      "CurrentPortionOfNoncurrentLoansReceived"),
     ("단기차입금", "장기차입금", "유동성장기차입금", "유동성장기부채",
      "유동성장기채무", "차입금", "유동 차입금(사채 포함)")),
    ("BOND",
     ("Bonds", "BondsIssued", "NoncurrentPortionOfNoncurrentBonds",
      "NoncurrentPortionOfNoncurrentBondsIssued", "CurrentPortionOfBonds"),
     ("사채", "유동성사채")),
    # 전환사채·신주인수권부사채·교환사채는 상환의무가 있으므로 차입이다. 전환되면
    # 주식수가 늘어 주당가치가 희석되므로, 부채로도 주식수로도 잡히지 않는 구간이
    # 있으면 그 회사는 양쪽에서 싸 보인다.
    ("CONVERTIBLE",
     ("ConvertibleBonds", "CurrentPortionOfConvertibleBonds",
      "BondWithWarrant", "CurrentPortionOfBondWithWarrant",
      "ExchangeableBonds", "CurrentPortionOfExchangeableBond"),
     ("전환사채", "전환사채(유동)", "유동성전환사채",
      "신주인수권부사채", "신주인수권부사채(유동)", "유동성신주인수권부사채",
      "교환사채", "유동성교환사채", "장기교환사채")),
    # 부채로 분류된 전환상환우선주(RCPS)도 같은 이유로 여기 든다 — 상환청구권이
    # 있어 재무상태표의 금융부채이고, 전환되면 주식수가 는다. 전환사채와 섞지
    # 않고 따로 두는 것은 금액이 크고(파마리서치 2026 반기 2,073억 — 그동안 총차입
    # 137억에 통째로 빠져 있었다) 자본으로 볼지 부채로 볼지가 방법론에 따라
    # 갈리기 때문이다. 빼고 보려면 debt_by_category 에서 덜어내면 된다.
    ("REDEEMABLE_PREFERRED",
     ("ConvertibleRedeemablePreferredStockLiabilities",
      "ConvertibleRedeemablePreferredStockLiabilitiesNet",
      "CurrentPortionOfConvertibleRedeemablePreferredStockLiabilities",
      "RedeemableConvertiblePreferredStockLiabilities",
      "RedeemablePreferredStockLiabilities"),
     ("전환상환우선주부채", "전환상환우선주부채(유동)",
      "유동성전환상환우선주부채", "비유동전환상환우선주부채",
      "상환전환우선주부채", "유동성상환전환우선주부채",
      "비유동상환전환우선주부채", "상환우선주부채")),
    # IFRS16 이후 리스부채는 차입이다. 이 저장소가 쓰는 영업현금흐름이 이미
    # IFRS16 기준(리스료 원금이 재무활동으로 빠진 뒤)이라, 현금흐름은 그 기준을
    # 쓰면서 부채에서만 리스를 빼면 리스가 많은 회사가 조용히 싸 보인다.
    # 빼고 보고 싶으면 debt_by_category 에서 LEASE 를 덜어내면 된다.
    ("LEASE",
     ("CurrentLeaseLiabilities", "NoncurrentLeaseLiabilities",
      "LeaseLiabilities", "FinanceLeaseLiabilities",
      "CurrentPortionOfFinanceLeaseLiabilities",
      # DART 원문의 오타다. 고쳐 쓰면 안 잡힌다.
      "CurentPortionOfFinanceLeaseLiabilities",
      "NonCurrentFinanceLeaseLiabilities"),
     ("리스부채", "유동리스부채", "비유동리스부채",
      "금융리스부채", "유동금융리스부채", "비유동금융리스부채")),
)
# 계정명은 공백을 지우고 대조한다 ('유동 리스부채' 와 '유동리스부채'는 같다).
BS_DEBT_NAME_TO_CATEGORY = {
    name.replace(" ", ""): category
    for category, _, names in BS_DEBT_ACCOUNTS
    for name in names
}
BS_DEBT_CODE_TO_CATEGORY = {
    code: category
    for category, codes, _ in BS_DEBT_ACCOUNTS
    for code in codes
}
# 차입처럼 보이는데 위 목록에 없는 행을 찾는 그물이다. **합산에는 쓰지 않는다** —
# 합산행인지 구성항목인지, 자본인지 부채인지를 이름만으로 정할 수 없어서다.
# 이 그물의 일은 하나다: 몇 줄을 찾았다는 사실이 전부 찾았다는 뜻으로 바뀌지
# 않게 하는 것. 걸린 행은 debt_unclassified 로 값과 함께 남고 debt_basis 가
# 달라지므로, 목록에 없는 계정을 쓰는 회사를 만나도 조용히 순현금이 되지 않는다.
BS_DEBT_CANDIDATE_NAME = re.compile(r"차입|사채|리스부채|상환우선주|유동성장기")
BS_DEBT_CANDIDATE_CODE = re.compile(
    r"Borrowing|LoansReceived|Bond|Debenture|LeaseLiabilit|RedeemablePreferred"
    r"|NotesPayable|CommercialPaper")
# 이름에 같은 낱말이 들어가도 자산인 것들 (금융리스채권·회사채 투자·대여금).
BS_NOT_DEBT_NAME = re.compile(r"자산|채권|대여|투자")
BS_NOT_DEBT_CODE = re.compile(r"Asset|Receivable|Investment")


# 전체계정 API가 빈 보고서는 필요한 계정을 XBRL 개념명으로 보충한다.
XBRL_SUPPLEMENT = [
    ("CF", "ifrs-full_CashFlowsFromUsedInOperatingActivities",
     "영업활동현금흐름", "CashFlowsFromUsedInOperatingActivities"),
    ("CF", "ifrs-full_PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
     "유형자산의 취득", "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"),
    ("CF", "ifrs-full_PurchaseOfIntangibleAssetsClassifiedAsInvestingActivities",
     "무형자산의 취득", "PurchaseOfIntangibleAssetsClassifiedAsInvestingActivities"),
    ("IS", "ifrs-full_Revenue", "매출액", "Revenue"),
    ("IS", "ifrs-full_InterestRevenueExpense", "순이자이익", "InterestRevenueExpense"),
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


# 보고서별 연초 누계 구간의 길이(일). 1/1~3/31 은 89~90일, 1/1~12/31 은 364~365일이다.
XBRL_YTD_DAYS = {"1q": (80, 100), "half": (170, 190),
                 "3q": (260, 285), "annual": (350, 380)}
XBRL_QUARTER_DAYS = (80, 100)


def xbrl_period_values(facts, year, report):
    """한 개념의 사실들에서 (단독 3개월 값, 연초 누계 값) 을 고른다.

    반기·3분기 XBRL 에는 같은 기말로 끝나는 구간이 둘 있다 — 3개월과 연초 누계.
    예전에는 가장 긴 구간 하나를 골라 두 자리에 모두 넣었고, 손익 복원이 누계를
    단독 분기로 읽어 2분기 매출이 반기 매출이 됐다(Q2 15, 반기 25 → Q2 25).
    구간 길이로 둘을 따로 고르고, 없는 쪽은 비워 둔다.
    """
    at_end = {}
    for fact in facts:
        if not fact.get("start") or not fact.get("end") or fact.get("value") is None:
            continue
        if not fact["end"].startswith(str(year)):
            continue
        days = (date.fromisoformat(fact["end"]) - date.fromisoformat(fact["start"])).days
        at_end.setdefault(fact["end"], []).append((days, fact["value"]))
    if not at_end:
        return None, None
    spans = at_end[max(at_end)]

    def pick(low, high):
        matched = [value for days, value in spans if low <= days <= high]
        return matched[0] if matched else None

    ytd = pick(*XBRL_YTD_DAYS[report])
    standalone = ytd if report in ("1q", "annual") else pick(*XBRL_QUARTER_DAYS)
    return standalone, ytd


def xbrl_rows(corp_code, year, report, key):
    """013 인 보고서를 XBRL 원본으로 보충해 fnlttSinglAcntAll 모양으로 돌려준다.

    fnlttSinglAcntAll 의 관습을 그대로 따른다 — 손익은 thstrm_amount 가 그 보고서의
    단독 기간(분기 보고서면 3개월, 사업보고서면 연간)이고 thstrm_add_amount 가
    연초 누계다. 현금흐름은 누계만 있으므로 thstrm_amount 에 누계를 둔다.
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
        standalone, ytd = xbrl_period_values(
            by_concept.get(concept, []), year, report)
        current = ytd if sj_div == "CF" else standalone
        if current is None and ytd is None:
            continue
        row = {"sj_div": sj_div, "account_id": account_id,
               "account_nm": account_nm, "_source": XBRL_ROW_SOURCE}
        if current is not None:
            row["thstrm_amount"] = str(current)
        if ytd is not None:
            row["thstrm_add_amount"] = str(ytd)
        rows.append(row)
    return rows


# 예전 보충 행("xbrl")은 반기·3분기 손익의 thstrm_amount 에 누계가 들어 있다.
# 저장된 원문을 다시 읽을 때 그 칸을 단독 분기로 쓰지 않게 판을 구분한다.
XBRL_ROW_SOURCE = "xbrl/2"


def _normalize_xbrl_row(row, report):
    if (row.get("_source") == "xbrl" and report in ("half", "3q")
            and row.get("sj_div") in ("IS", "CIS")):
        return {k: v for k, v in row.items() if k != "thstrm_amount"}
    return row


def reports_by_year(raw):
    """저장된 원문 payload 에서 {연도: {보고서: 행 목록}} 을 다시 만든다.

    수집 직후와 나중의 재생 검사가 같은 함수로 같은 구조를 읽는다. 보고서 순서는
    REPORT_SEQUENCE 를 따른다 — 아래 도출이 '그 해의 첫 보고서'를 쓰는 자리가 있다.
    """
    years = sorted({int(key[:4]) for key in raw
                    if re.fullmatch(r"\d{4}_(1q|half|3q|annual)(_xbrl)?", key)})
    by_year = {}
    for year in years:
        by_report = {}
        for report, _ in REPORT_SEQUENCE:
            data = raw.get(f"{year}_{report}")
            if isinstance(data, dict) and data.get("list"):
                by_report[report] = data["list"]
                continue
            rows = raw.get(f"{year}_{report}_xbrl")
            if isinstance(rows, list) and rows:
                by_report[report] = [_normalize_xbrl_row(r, report) for r in rows]
        if by_report:
            by_year[year] = by_report
    return by_year


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


def declared_currency(rows):
    """공시가 스스로 말한 통화. 여러 개면 섞인 것이므로 세우지 않는다.

    예전에는 `"currency": "KRW"` 하드코딩이었다. 실제로 확인해 보니 지금
    코퍼스에서는 통화를 밝힌 5,011 행이 전부 KRW 라 값은 맞았다 — 그러나
    **맞는 것과 확인된 것은 다르다.** 통화가 다른 재무제표가 한 번 들어오면
    원 단위로 저장된 값이 그대로 억원으로 환산돼 화면에 찍히고, 그 숫자는
    자릿수까지 그럴듯하다.

    XBRL 로 합성한 행은 통화 칸이 없다(1,213 행). 그건 결측이지 위반이
    아니므로 밝힌 것들만 본다.
    """
    seen = {(r.get("currency") or "").strip() for r in rows}
    seen.discard("")
    if not seen:
        return "KRW"
    if len(seen) > 1:
        fail(f"한 보고서에 통화가 섞여 있다: {sorted(seen)}. "
             "원 단위 가정이 깨지므로 저장하지 않는다")
    return seen.pop()


def fetch_all(corp_code, year, report, fs, key):
    """정기보고서 전체 계정. 없으면 None."""
    return request(
        "fnlttSinglAcntAll.json",
        {"corp_code": corp_code, "bsns_year": str(year),
         "reprt_code": REPORTS[report], "fs_div": fs},
        key, allow_empty=True)


def restore_cf_quarters(by_report, ids, label, names=()):
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
        row = acct(rows, "CF", ids, names)
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
    """손익 3개월 값.

    보고서가 단독 분기 값을 주면 그대로 쓴다. 누계만 있는 보고서(XBRL 로 보충한
    것 등)는 누계 차감으로 만든다 — 누계를 단독 분기 자리에 넣지 않는다.
    4분기는 언제나 연간 − 3분기누계다.
    """
    out, cumulative = {}, {}
    for report, q in REPORT_SEQUENCE[:3]:
        rows = by_report.get(report)
        if not rows:
            continue
        row = acct(rows, ("IS", "CIS"), ids, names)
        value = amount(row)
        ytd = amount(row, "thstrm_add_amount")
        if q == 1 and ytd is None:
            ytd = value
        if ytd is not None:
            cumulative[q] = ytd
        if value is not None:
            out[q] = {"val": float(value), "derived": False}
    for q in (2, 3):
        if q not in out and q in cumulative and q - 1 in cumulative:
            out[q] = {"val": float(cumulative[q] - cumulative[q - 1]),
                      "derived": True}

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


def _account_code(account_id):
    """표준계정코드에서 접두사를 걷어낸다.

    같은 계정이 회사마다 ifrs-full_ / ifrs_ / dart_ 로 들어온다. 접두사를 둔 채
    대조하면 한 글자 차이로 차입이 통째로 빠진다.
    """
    code = (account_id or "").strip()
    for prefix in ("ifrs-full_", "ifrs_", "dart_"):
        if code.startswith(prefix):
            return code[len(prefix):]
    return code


def classify_debt_row(row):
    """재무상태표 한 행이 어떤 차입인지 낸다. 차입이 아니면 None.

    코드를 먼저 본다 — 계정명은 회사가 자유롭게 쓰지만 표준코드는 뜻이 고정돼
    있다. '금융리스채권'처럼 이름에 리스가 들어가도 코드가 다르면 자산이다.
    """
    category = BS_DEBT_CODE_TO_CATEGORY.get(_account_code(row.get("account_id")))
    if category:
        return category
    name = (row.get("account_nm") or "").replace(" ", "")
    return BS_DEBT_NAME_TO_CATEGORY.get(name)


def _is_short_investment(row):
    # 코드는 접두 일치로 본다 — 당기손익 금융자산은 뒤에 측정 방식이 붙은 변형
    # (…MandatorilyMeasuredAtFairValue)으로도 온다.
    if _account_code(row.get("account_id")).startswith(BS_SHORT_INV_CODES):
        return True
    name = (row.get("account_nm") or "").replace(" ", "")
    return bool(BS_SHORT_INV_NAME.match(name)) and not BS_NOT_SHORT_INV_NAME.search(name)


def resolve_short_investments(bs_report):
    """단기 금융투자와 그 근거를 낸다. → (합, 센 행, 세지 않은 후보)

    센 행은 계정 하나씩 남긴다 — 무엇을 현금성으로 봤는지가 값 옆에 있어야
    모델이 덜어내고 다시 셀 수 있다. 후보는 현금성일 수 있지만 이름만으로 정할
    수 없어 세지 않은 유동 금융자산이며, 잔액이 0 이면 내지 않는다.
    """
    total, detail, candidates, seen = 0.0, [], [], set()
    for row in bs_report:
        if row.get("sj_div") != "BS":
            continue
        value = amount(row)
        if value is None:
            continue
        code = _account_code(row.get("account_id"))
        name = (row.get("account_nm") or "").replace(" ", "")
        marker = (row.get("account_id"), row.get("account_nm"),
                  row.get("thstrm_amount"), row.get("ord"))
        if marker in seen:
            continue
        item = {"label": row.get("account_nm"),
                "account_id": row.get("account_id"), "value": float(value)}
        if _is_short_investment(row):
            seen.add(marker)
            total += float(value)
            detail.append(item)
        elif value and not (
                BS_NOT_SHORT_INV_NAME.search(name)
                or BS_NOT_SHORT_INV_CODE.search(code)) and (
                BS_SHORT_INV_CANDIDATE_CODE.match(code)
                or BS_SHORT_INV_CANDIDATE_NAME.search(name)):
            seen.add(marker)
            candidates.append(item)
    if detail:
        # 단기투자가 유동자산을 넘으면 합계 줄과 그 구성 줄을 같이 센 것이다.
        current_assets = amount(
            acct(bs_report, "BS", ["ifrs-full_CurrentAssets"], ["유동자산"]))
        if current_assets is not None and total > float(current_assets) + 1.0:
            fail(f"단기투자 {total:,.0f} 이 유동자산 {float(current_assets):,.0f} 을 "
                 "넘는다. 합산행과 구성항목을 같이 세었을 가능성이 크므로 "
                 "순부채를 내지 않는다")
    return total, detail, candidates


def unclassified_debt_rows(bs_report):
    """차입처럼 보이지만 분류하지 못한 재무상태표 행. → [{label, account_id, value}]

    잔액이 0 인 행은 내지 않는다 — 총차입을 바꾸지 않는 것을 경고로 올리면
    정말 빠진 것이 그 사이에 묻힌다.
    """
    out, seen = [], set()
    for row in bs_report:
        if row.get("sj_div") != "BS" or classify_debt_row(row) is not None:
            continue
        code = _account_code(row.get("account_id"))
        name = (row.get("account_nm") or "").replace(" ", "")
        if BS_NOT_DEBT_NAME.search(name) or BS_NOT_DEBT_CODE.search(code):
            continue
        if not (BS_DEBT_CANDIDATE_NAME.search(name)
                or BS_DEBT_CANDIDATE_CODE.search(code)):
            continue
        value = amount(row)
        if not value:
            continue
        marker = (row.get("account_id"), row.get("account_nm"),
                  row.get("thstrm_amount"), row.get("ord"))
        if marker in seen:
            continue
        seen.add(marker)
        out.append({"label": row.get("account_nm"),
                    "account_id": row.get("account_id"),
                    "value": float(value)})
    return out


def debt_by_category(debt_detail):
    """분류별 차입 합.

    모델이 리스를 빼고 순부채를 다시 셀 수 있어야 하므로 합계만 주지 않는다.
    """
    totals = {}
    for item in debt_detail:
        category = item.get("category") or "UNCLASSIFIED"
        totals[category] = totals.get(category, 0.0) + float(item["value"])
    return totals


def resolve_debt(bs_report):
    """총차입과 그 근거를 낸다. → (debt, debt_detail, debt_basis)

    차입 계정이 하나도 안 잡히는 경우는 두 가지이고, 둘을 섞으면 안 된다 —
    재무상태표를 제대로 못 읽은 것과 회사가 실제로 무차입인 것이다. 앞을 뒤로
    착각하면 순부채가 조용히 틀리고, 뒤를 앞으로 착각하면 순현금 회사가 영원히
    스냅샷을 못 만든다 (삼성E&A 2026 1Q: 부채 5.7조가 전부 매입채무·
    초과청구공사이고 차입은 0원이다).

    부채총계와 유동·비유동 구분이 모두 잡혔다면 표는 읽힌 것이고, 그 안에 차입
    계정이 없다는 사실 자체가 관측이다. 그때만 0 으로 내려간다.

    값이 0 인 차입 계정은 남긴다. '계정은 있고 잔액이 0' 과 '계정 자체가 없다'
    는 다른 관측이다.

    basis 는 셋이다. MATCHED_LINE_ITEMS 는 **찾은 줄을 합했다**는 뜻이지 전부
    찾았다는 뜻이 아니었다 — 리스부채 두 줄만 잡고 차입금 18.6조를 놓친 것이
    그 이름으로 통과했다. 그래서 차입처럼 보이는데 분류하지 못한 행이 남아
    있으면 UNCLASSIFIED_CANDIDATES 로 내려 합계가 하한임을 말한다.
    """
    unclassified = unclassified_debt_rows(bs_report)
    debt, debt_detail, seen = 0.0, [], set()
    for row in bs_report:
        if row.get("sj_div") != "BS":
            continue
        category = classify_debt_row(row)
        if category is None or amount(row) is None:
            continue
        # 같은 행이 두 번 실려 와도 한 번만 센다. 계정명만으로 묶으면 유동·비유동이
        # 같은 이름·같은 금액일 때 한쪽이 사라지므로 코드와 행 순번도 넣는다.
        marker = (row.get("account_id"), row.get("account_nm"),
                  row.get("thstrm_amount"), row.get("ord"))
        if marker in seen:
            continue
        seen.add(marker)
        debt += float(amount(row))
        debt_detail.append({"label": row.get("account_nm"),
                            "account_id": row.get("account_id"),
                            "category": category,
                            "value": float(amount(row))})
    if debt_detail:
        # 차입 합이 부채총계를 넘으면 같은 돈을 두 번 셌다는 뜻이다 (합산행과 그
        # 구성항목을 같이 잡는 경우). 틀린 순부채를 쓰느니 멈춘다.
        total_liabilities = amount(
            acct(bs_report, "BS", ["ifrs-full_Liabilities"], ["부채총계"])
        )
        if total_liabilities is not None and debt > float(total_liabilities) + 1.0:
            fail(f"총차입 {debt:,.0f} 이 부채총계 {float(total_liabilities):,.0f} 을 "
                 "넘는다. 합산행과 구성항목을 같이 세었을 가능성이 크므로 "
                 "순부채를 내지 않는다")
        return debt, debt_detail, (
            "UNCLASSIFIED_CANDIDATES" if unclassified else "MATCHED_LINE_ITEMS")

    anchors = [
        acct(bs_report, "BS", ["ifrs-full_Liabilities"], ["부채총계"]),
        acct(bs_report, "BS", ["ifrs-full_CurrentLiabilities"], ["유동부채"]),
        acct(bs_report, "BS", ["ifrs-full_NoncurrentLiabilities"], ["비유동부채"]),
    ]
    if any(row is None or amount(row) is None for row in anchors):
        fail("차입금 계정을 하나도 찾지 못했고 부채총계·유동·비유동 구분도 "
             "잡히지 않았다. 재무상태표를 못 읽은 것이므로 무차입이라고 "
             "단정하지 않는다")
    if unclassified:
        # 표는 읽혔지만 차입일 수 있는 줄이 남아 있다. 무차입은 관측이 아니다.
        return 0.0, [], "UNCLASSIFIED_CANDIDATES"
    return 0.0, [], "NO_DEBT_LINE_ITEMS"


def _total_eps(rows, basis):
    """Total EPS, or the sum of explicitly disclosed continuing/discontinued EPS.

    A missing discontinued component is unknown, not zero. Never divide total
    parent income by continuing-only EPS (or mix basic and diluted components).
    """
    ids = IS_DILUTED_EPS_IDS if basis == "DILUTED" else IS_BASIC_EPS_IDS
    names = (["희석주당이익(손실)"] if basis == "DILUTED" else
             ["기본주당순이익", "기본주당이익(손실)"])
    value = amount(acct(rows, ("IS", "CIS"), ids, names))
    if value is not None:
        return value
    stem = "Diluted" if basis == "DILUTED" else "Basic"
    components = [amount(acct(rows, ("IS", "CIS"), [
        f"ifrs-full_{stem}EarningsLossPerShareFrom{scope}Operations"
    ])) for scope in ("Continuing", "Discontinued")]
    if all(value is not None for value in components):
        return sum(components)
    return None


def resolve_shares(bs_report, *, fs_div="CFS"):
    """지배주주 순이익 ÷ 주당이익으로 주식수를 낸다. → (shares, eps_basis)

    희석증권이 없는 회사는 희석주당이익을 아예 공시하지 않는다 (삼성E&A는
    기본주당순이익만 낸다). 그때 기본으로 내려오되 **어느 쪽을 썼는지 남긴다** —
    기본을 희석이라고 부르면 전환사채가 있는 회사에서 주식수가 조용히 적게
    잡히고, 그 오차는 주당가치 전체에 곱해진다.
    """
    # The generic owner label can also denote comprehensive income in CIS.
    # Use the profit-attribution concept, never that ambiguous label.
    parent_row = acct(bs_report, ("IS", "CIS"), IS_NET_PARENT_IDS)
    # 별도재무제표에는 지배기업 소유주지분 행이 존재하지 않는다. 그 경우에만
    # 동일 기간의 당기순이익을 EPS 분자로 사용한다. 연결재무에서는 총당기순이익과
    # 지배주주 EPS를 섞지 않도록 기존 지배주주 행을 계속 우선한다.
    if (parent_row is None or amount(parent_row) is None) and fs_div == "OFS":
        parent_row = acct(bs_report, ("IS", "CIS"), IS_NET_IDS,
                          ["당기순이익", "분기순이익", "반기순이익"])
    eps = _total_eps(bs_report, "DILUTED")
    eps_basis = "DILUTED"
    if eps is None:
        eps = _total_eps(bs_report, "BASIC")
        eps_basis = "BASIC_NO_DILUTED_DISCLOSED"
    parent_ni = amount(parent_row)
    if not parent_ni or not eps:
        fail("주당이익(희석·기본 모두) 또는 지배기업 소유주지분 순이익을 찾지 "
             "못했다. 주식수를 기억으로 채우지 않는다")
    # 분기 EPS 이므로 분기 지배주주 순이익으로 나눈다. 같은 기간이라 상쇄된다.
    return float(parent_ni) / float(eps), eps_basis


def derive_fundamentals(raw, *, fs, period_end=None, income_quarters=8):
    """저장된 원문 payload 하나에서 재무 스냅샷의 숫자를 전부 만든다.

    네트워크를 타지 않는다. 수집 직후의 저장과 나중의 재생 검사
    (`replay_fundamentals`)가 이 함수 하나를 쓴다 — 그래야 "스냅샷이 원문에서
    그대로 다시 나오는가"가 지문 대조가 아니라 값 대조가 된다. 지문은 파일이
    보존됐다는 증거일 뿐 그 안의 숫자가 원문과 같다는 증거가 아니다.
    """
    by_year = reports_by_year(raw)
    if not by_year:
        fail("원문 payload 에 재무제표가 없다")

    # --- 현금흐름 4분기 ---
    ocf, capex, intangible = {}, {}, {}
    for year, by_report in by_year.items():
        try:
            for q, row in restore_cf_quarters(by_report, CF_OCF_IDS, "영업현금흐름").items():
                ocf[(year, q)] = row
        except LookupError:
            pass
        try:
            for q, row in restore_cf_quarters(
                    by_report, CF_CAPEX_IDS, "capex", CF_CAPEX_NAMES).items():
                capex[(year, q)] = row
        except LookupError:
            pass
        try:
            for q, row in restore_cf_quarters(
                    by_report, CF_INTANGIBLE_IDS, "무형자산취득").items():
                intangible[(year, q)] = row
        except LookupError:
            pass  # 무형자산 취득이 없는 회사도 있다. capex 정의에 영향 없음

    common_cf = [k for k in ocf if k in capex]
    if not common_cf:
        fail("영업현금흐름과 capex가 함께 있는 분기를 하나도 확보하지 못했다. "
             f"영업현금흐름 확보: {sorted(ocf)} / capex 확보: {sorted(capex)}")
    latest = max(common_cf)
    if period_end:
        want = [k for k in sorted(ocf)
                if f"{k[0]}-{QUARTER_END[k[1]]}" == period_end]
        if not want:
            fail(f"--period-end {period_end} 에 해당하는 분기가 없다")
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
    # base_plan(revenue/gross/op/net)을 먼저 수집한 뒤 비용 후보 전체를 탐색하고
    # itertools.combinations로 항등식 잔차가 최소인 최적 조합 선택
    income, expense_lines = {}, []
    base_plan = [("revenue", IS_REVENUE_IDS, REV_NAMES),
                 ("gross_profit", IS_GROSS_IDS, ["매출총이익"]),
                 ("operating_income", IS_OPINC_IDS, OP_NAMES),
                 ("net_income", IS_NET_IDS, ["당기순이익", "분기순이익", "반기순이익"])]

    # base_plan 먼저 수집
    for key_name, ids, names in base_plan:
        per_year = {}
        for year, by_report in by_year.items():
            for q, row in restore_is_quarters(by_report, ids, names).items():
                per_year[(year, q)] = row
        rows = series_rows(per_year, income_quarters, latest)
        if rows is None:
            fail(f"손익 {key_name}: {latest} 기준 연속 {income_quarters}분기를 "
                 f"확보하지 못했다 (확보: {sorted(per_year)})")
        for row in rows:
            row["end"] = f"{row['fy']}-{QUARTER_END[row['quarter']]}"
            row["filed"] = None
        income[key_name] = {"tag": ids[0], "ttm": sum(r["val"] for r in rows[-4:]),
                            "quarters": rows}

    # IS_EXPENSE_IDS 후보 전체 수집 (데이터가 있는 것만)
    candidate_expenses = []
    ref_report = (by_year[latest[0]].get("1q") or
                  next(iter(by_year[latest[0]].values())))
    for aid, label in IS_EXPENSE_IDS:
        if not acct(ref_report, ("IS", "CIS"), [aid]):
            continue
        per_year = {}
        for year, by_report in by_year.items():
            for q, row in restore_is_quarters(by_report, [aid], [label]).items():
                per_year[(year, q)] = row
        rows = series_rows(per_year, income_quarters, latest)
        if rows is None:
            continue
        for row in rows:
            row["end"] = f"{row['fy']}-{QUARTER_END[row['quarter']]}"
            row["filed"] = None
        key_name = f"expense_{len(candidate_expenses)}"
        candidate_expenses.append({
            "key": key_name, "label": label, "aid": aid,
            "rows": rows,
        })

    # itertools.combinations로 항등식 잔차 최소 조합 선택 (k=0 포함 — 금융업 지원)
    gross = income["gross_profit"]["quarters"][-1]["val"]
    op = income["operating_income"]["quarters"][-1]["val"]
    best_combo: tuple = ()
    best_residual: float = float("inf")
    for k in range(0, len(candidate_expenses) + 1):
        for combo in itertools.combinations(range(len(candidate_expenses)), k):
            expenses_vals = [candidate_expenses[i]["rows"][-1]["val"] for i in combo]
            residual, _ = income_identity_residual(gross, expenses_vals, op)
            if abs(residual) < best_residual:
                best_residual = residual
                best_combo = combo

    # best_combo 반영
    expense_lines = []
    plan = list(base_plan)
    for i in best_combo:
        cand = candidate_expenses[i]
        key_name = cand["key"]
        expense_lines.append({"key": key_name, "label": cand["label"]})
        income[key_name] = {
            "tag": cand["aid"],
            "ttm": sum(r["val"] for r in cand["rows"][-4:]),
            "quarters": cand["rows"],
        }

    # 항등식 최종 검증
    expenses_final = [income[e["key"]]["quarters"][-1]["val"] for e in expense_lines]
    residual, tolerance = income_identity_residual(gross, expenses_final, op)
    spend = sum(expenses_final)
    if abs(residual) > tolerance:
        detail = (", ".join(f"{e['label']} {income[e['key']]['quarters'][-1]['val']/1e8:,.0f}억"
                            for e in expense_lines)
                  or "찾은 비용 라인 없음")
        fail("손익 항등식이 맞지 않는다: 매출총이익 "
             f"{gross/1e8:,.0f}억 − ({detail}) = {(gross-spend)/1e8:,.0f}억 인데 "
             f"영업이익은 {op/1e8:,.0f}억 이다 (차이 {residual/1e8:,.0f}억).\n"
             "  IS_EXPENSE_IDS 에 이 회사의 비용 계정 표준코드를 추가한다. "
             "원문 계정 목록: python3.13 -m collectors.dart financials 로 확인")

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

    short_inv, short_inv_detail, short_inv_unclassified = (
        resolve_short_investments(bs_report))

    debt, debt_detail, debt_basis = resolve_debt(bs_report)
    debt_unclassified = unclassified_debt_rows(bs_report)

    # --- 주식수: 지배주주 순이익 ÷ 주당이익 ---
    shares, eps_basis = resolve_shares(bs_report, fs_div=fs)

    # isu_stock_totqy 는 **발행할** 주식의 총수(수권주식)다 — 삼성전자는 250억주로
    # 실제 발행주식과 4배 가까이 다르다. 합계 행을 그대로 쓰면 대조가 무의미하다.
    # 실제 유통주식은 종류주식별 distb_stock_co 의 합이다.
    share_cross = None
    total = raw.get("stockTotqySttus")
    if total:
        outstanding = 0.0
        for r in total.get("list", []):
            if (r.get("se") or "").strip() in ("합계", "비고"):
                continue
            value = to_int(r.get("distb_stock_co"))
            if value:
                outstanding += float(value)
        share_cross = outstanding or None

    quarter_keys = ("fy", "quarter", "end", "filed", "val", "derived")
    return {
        "period_end": period_end,
        "currency": declared_currency(bs_report),
        "quarters": {
            "ocf": [{k: r[k] for k in quarter_keys} for r in ocf_rows],
            "capex": [{k: r[k] for k in quarter_keys} for r in capex_rows],
        },
        "values": {
            "ocf_ttm": ocf_ttm,
            "capex_ttm": capex_ttm,
            "fcf_ttm": ocf_ttm - capex_ttm,
            "shares_diluted": shares,
            "cash_and_equivalents": cash,
            "short_term_investments": short_inv,
            "long_term_debt": debt,
            "net_debt": debt - cash - short_inv,
        },
        "intangible_capex_ttm": (
            sum(intangible[k]["val"] for k in [(r["fy"], r["quarter"]) for r in ocf_rows]
                if k in intangible)
            if all((r["fy"], r["quarter"]) in intangible for r in ocf_rows) else None),
        "short_term_investments_detail": short_inv_detail,
        "short_term_investments_unclassified": short_inv_unclassified,
        "debt_detail": debt_detail,
        "debt_by_category": debt_by_category(debt_detail),
        "debt_basis": debt_basis,
        "debt_unclassified": debt_unclassified,
        "eps_basis": eps_basis,
        "expense_lines": expense_lines,
        "income_statement": income,
        "share_count_cross_check": share_cross,
    }


# 재생 검사가 대조하는 필드. 스냅샷에만 있는 설명·식별자·시각은 비교하지 않는다.
REPLAYED_FIELDS = ("currency", "quarters", "values", "debt_by_category",
                   "debt_basis", "income_statement", "eps_basis")


def replay_fundamentals(snapshot, raw):
    """스냅샷의 숫자가 봉인된 원문에서 지금 코드로 그대로 다시 나오는가.

    → 어긋난 것을 설명하는 문장 목록(같으면 빈 목록).

    두 가지를 함께 잡는다. 스냅샷만 고쳐진 경우(값이 원문과 다르다)와, 추출
    규칙이 바뀌어 옛 규칙으로 만든 스냅샷이 낡은 경우(SK하이닉스의 총차입은 옛
    분류로 2.5조, 지금 분류로 21.8조다). 뒤의 경우 재수집이 답이다.
    """
    income = snapshot.get("income_statement") or {}
    quarters = len((income.get("revenue") or {}).get("quarters") or []) or 8
    try:
        with contextlib.redirect_stderr(io.StringIO()) as captured:
            derived = derive_fundamentals(
                raw, fs=snapshot.get("fs_div") or "CFS",
                period_end=snapshot.get("period_end"), income_quarters=quarters)
    except SystemExit:
        reason = captured.getvalue().strip().splitlines()
        return ["원문에서 재무를 다시 만들 수 없다"
                + (f": {reason[0][:160]}" if reason else "")]
    except (KeyError, TypeError, ValueError, StopIteration) as exc:
        return [f"원문에서 재무를 다시 만들 수 없다: {type(exc).__name__}"]
    differences = fundamentals_differences(snapshot, derived, REPLAYED_FIELDS)
    for field, label in (
            ("debt_unclassified", "분류하지 못한 차입 후보"),
            ("short_term_investments_unclassified", "세지 않은 단기 금융자산 후보")):
        if derived[field] and snapshot.get(field) != derived[field]:
            differences.append(f"{field}: 원문에 {label}가 있는데 스냅샷에 없다")
    return differences


def cmd_fundamentals(a, key):
    info = resolve(a.stock_code, key)
    corp, name = info["corp_code"], info["corp_name"]
    years = list(range(a.year - 2, a.year + 1))

    raw, supplemented = {}, []
    for year in years:
        for report, _ in REPORT_SEQUENCE:
            data = fetch_all(corp, year, report, a.fs, key)
            if data:
                raw[f"{year}_{report}"] = data
                continue
            # 013 — XBRL 원본으로 보충한다. 없는 채로 두면 4분기 복원이 막힌다.
            rows = xbrl_rows(corp, year, report, key)
            if rows:
                raw[f"{year}_{report}_xbrl"] = rows
                supplemented.append(f"{year} {report}")
    if supplemented:
        print(f"※ fnlttSinglAcntAll 이 013 이라 XBRL 원본으로 보충: "
              f"{', '.join(supplemented)}", file=sys.stderr)

    if not reports_by_year(raw):
        fail(f"{name}: {years} 구간에서 재무제표를 받지 못했다")

    total = request("stockTotqySttus.json",
                    {"corp_code": corp, "bsns_year": str(a.year - 1),
                     "reprt_code": REPORTS["annual"]}, key, allow_empty=True)
    if total:
        raw["stockTotqySttus"] = total

    derived = derive_fundamentals(raw, fs=a.fs, period_end=a.period_end,
                                  income_quarters=a.income_quarters)
    period_end, values = derived["period_end"], derived["values"]
    ocf_rows, capex_rows = derived["quarters"]["ocf"], derived["quarters"]["capex"]
    ocf_ttm, capex_ttm = values["ocf_ttm"], values["capex_ttm"]
    shares, cash = values["shares_diluted"], values["cash_and_equivalents"]
    short_inv, debt = values["short_term_investments"], values["long_term_debt"]
    income, expense_lines = derived["income_statement"], derived["expense_lines"]
    debt_detail, debt_basis = derived["debt_detail"], derived["debt_basis"]
    debt_unclassified = derived["debt_unclassified"]
    short_inv_unclassified = derived["short_term_investments_unclassified"]
    eps_basis, share_cross = derived["eps_basis"], derived["share_count_cross_check"]
    if short_inv_unclassified:
        print("⚠ 현금성일 수 있는데 세지 않은 유동 금융자산이 있다 — 단기투자는 "
              "하한이고 순부채는 그만큼 크게 잡혀 있을 수 있다: "
              + ", ".join(f"{d['label']} {d['value']/1e8:,.0f}억"
                          for d in short_inv_unclassified), file=sys.stderr)
    if debt_unclassified:
        print("⚠ 차입처럼 보이는데 분류하지 못한 계정이 있다 — 총차입에 넣지 않았고 "
              "합계는 하한이다: "
              + ", ".join(f"{d['label']} {d['value']/1e8:,.0f}억"
                          for d in debt_unclassified), file=sys.stderr)
    gap_pct = shares_cross_check_pct(shares, share_cross)
    if gap_pct is not None and gap_pct > 5.0:
        print(f"⚠ 희석주식수 {shares:,.0f} 와 유통주식수 합 {share_cross:,.0f} 가 "
              f"{gap_pct:.1f}% 어긋난다. "
              "우선주·전환증권 취급을 확인할 것", file=sys.stderr)

    raw["_instrument"] = {
        "instrument_id": a.stock_code,
        "corp_code": corp,
    }
    raw_path = save_raw(f"fundamentals_{a.stock_code}", raw)

    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "instrument_id": a.stock_code,
        "company_name": name,
        "corp_code": corp,
        "currency": derived["currency"],
        "period_end": period_end,
        "created_at": datetime.now(timezone.utc).astimezone().isoformat(
            timespec="seconds"),
        "source": "opendart_fnlttSinglAcntAll",
        "fs_div": a.fs,
        "definitions": {
            # 이 두 줄이 없으면 다른 시장 스냅샷과 말없이 다른 것을 비교하게 된다.
            "capex": "유형자산의 취득만. 무형자산 취득은 intangible_capex_ttm 에 따로 둔다"
                     " (EDGAR 판의 PaymentsToAcquirePropertyPlantAndEquipment 와 같은 기준)",
            "long_term_debt": "총차입. 차입금·사채·전환사채·교환사채·부채로 "
                              "분류된 전환상환우선주·리스부채를 모두 합했다 "
                              "(분류별 합은 debt_by_category, 계정명은 "
                              "debt_detail). 리스를 뺀 기준을 쓰려면 LEASE 를 "
                              "덜어내고 다시 센다",
            "debt_by_category": "BORROWING=차입금 BOND=사채 "
                                "CONVERTIBLE=전환사채·신주인수권부사채·교환사채 "
                                "REDEEMABLE_PREFERRED=부채로 분류된 "
                                "전환상환우선주 LEASE=리스부채. 포함 여부는 "
                                "방법론에 따라 갈리므로 합계만 주지 않고 분류를 "
                                "함께 낸다. 희석주식수가 전환을 이미 반영했다면 "
                                "전환증권은 부채와 주식수 양쪽에 잡힌다",
            "debt_basis": "MATCHED_LINE_ITEMS=차입 계정을 실제로 찾아 합했고 "
                          "차입처럼 보이는 미분류 계정이 없다. "
                          "NO_DEBT_LINE_ITEMS=재무상태표는 읽혔는데 차입 계정이 "
                          "없어 0으로 뒀다(무차입, 0은 관측이다). "
                          "UNCLASSIFIED_CANDIDATES=차입처럼 보이는데 분류하지 "
                          "못한 계정이 debt_unclassified 에 있다. 총차입은 그것을 "
                          "뺀 하한이고 순부채는 그만큼 작게 잡혀 있을 수 있다",
            "debt_unclassified": "차입일 수 있지만 합산행인지·자본인지 이름만으로 "
                                 "정할 수 없어 총차입에 넣지 않은 재무상태표 행",
            "short_term_investments": "순부채에서 현금과 함께 뺀 단기 금융투자 "
                                      "(단기금융상품·단기투자자산·유동 상각후원가/"
                                      "당기손익 금융자산). 센 계정은 "
                                      "short_term_investments_detail 에 있다. "
                                      "장기금융상품은 넣지 않았다",
            "short_term_investments_unclassified": "현금성일 수 있지만 대여금·"
                                                   "보증금이 섞였을 수 있어 세지 "
                                                   "않은 유동 금융자산. 현금성이면 "
                                                   "순부채는 그만큼 작다",
            "shares_diluted": "지배기업 소유주지분 순이익 ÷ 주당이익. 어느 "
                              "주당이익을 썼는지는 eps_basis 참조 — "
                              "BASIC_NO_DILUTED_DISCLOSED 면 희석 공시가 없어 "
                              "기본으로 계산한 값이라 희석증권이 생기면 틀린다",
            "cash_flow_period": "현금흐름표는 연초 누계로 들어오므로 차감 복원했다 "
                                "(derived=true 인 분기)",
            "income_period": "손익계산서 thstrm_amount 는 이미 3개월이다. "
                             "4분기만 연간 − 3분기누계로 만들었다",
        },
        "quarters": derived["quarters"],
        "values": values,
        "intangible_capex_ttm": derived["intangible_capex_ttm"],
        "short_term_investments_detail": derived["short_term_investments_detail"],
        "short_term_investments_unclassified": short_inv_unclassified,
        "debt_detail": debt_detail,
        "debt_by_category": derived["debt_by_category"],
        "debt_basis": debt_basis,
        "debt_unclassified": debt_unclassified,
        "eps_basis": eps_basis,
        "expense_lines": expense_lines,
        "income_statement": income,
        "share_count_cross_check": share_cross,
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
    print(f"{'희석주식수':<22}{shares/1e6:>14,.1f}M"
          f"{'   ※ 희석 공시 없어 기본주당이익 기준' if eps_basis != 'DILUTED' else ''}")
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
        print(f"  {d['label']:<24}{d['value']/unit:>12,.0f}억  {d['category']}")
    if debt_detail:
        totals = debt_by_category(debt_detail)
        print("  " + "  ".join(f"{k} {v/unit:,.0f}억" for k, v in sorted(totals.items())))
    for d in debt_unclassified:
        print(f"  {d['label']:<24}{d['value']/unit:>12,.0f}억  "
              "미분류 — 총차입에 넣지 않음")
    if debt_basis == "NO_DEBT_LINE_ITEMS":
        print("  차입 계정 없음 — 재무상태표는 읽혔고 그 안에 차입이 없다(무차입).")
        print("  ※ 리스부채가 독립 계정이 아니라 기타부채에 묶여 있으면 여기 "
              "잡히지 않는다.")
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

    for default_label, field in (("당기", "thstrm"), ("전기", "frmtrm"),
                                 ("전전기", "bfefrmtrm")):
        print(financial_period_row(
            revenue.get(f"{field}_nm", default_label),
            to_int(revenue.get(f"{field}_amount")),
            to_int(profit.get(f"{field}_amount"))))


def financial_period_row(period_label, rev, op):
    """기간 한 줄. 값이 없어도 **줄을 지우지 않는다.**

    예전에는 매출이나 영업이익 하나만 없어도 `return` 으로 줄이 통째로
    사라졌다. 사라진 줄은 "그 기간 공시가 없다"로 읽히지만 실제로는 계정명을
    못 찾은 것일 수 있고, 둘은 완전히 다른 사건이다. 게다가 `quarter_metrics`
    는 같은 상황에서 매출을 살리고 마진만 비운다 — 한 저장소 안에서 두 함수가
    정반대로 굴면 어느 쪽이 맞는지 알 수 없다.

    OPM 은 `ratio_pct` 에 맡긴다. 직접 나누면 매출 0 에서 터지고, 정정 공시로
    매출이 음수인 기간에서는 부호가 뒤집힌 마진이 진짜처럼 찍힌다.

    기간명은 회사가 쓴 문자열 그대로다 ("제 57 기 반기"). 한글 폭이 섞이므로
    `pad` 로 맞춘다 — `:<22` 는 한글을 한 칸으로 세어 열이 밀린다.
    """
    if rev is None and op is None:
        return pad(period_label, 22, align="<") + "해당 기간 값이 공시에 없다"
    return (pad(period_label, 22, align="<")
            + "매출 " + cell(rev, 13, ",.0f", "억", scale=1e8)
            + "   영업이익 " + cell(op, 11, ",.0f", "억", scale=1e8)
            + "   OPM " + cell(ratio_pct(op, rev), 7, ".2f", "%"))


def income_identity_residual(gross, expenses, operating):
    """매출총이익 − 비용라인 = 영업이익 인가. (잔차, 허용오차)를 낸다.

    이 검사가 하는 일은 **비용 계정을 못 찾았다는 사실을 드러내는 것**이다.
    표준코드에 없는 비용 라인이 있으면 그 회사의 영업이익은 매출총이익에서
    일부만 뺀 값이 되고, 그대로 저장되면 마진이 과대계상된다. 숫자 자체는
    그럴듯하게 나오므로 항등식 없이는 아무도 눈치채지 못한다.

    허용오차를 영업이익에 비례시키되 하한을 두는 이유는 규모 차이다 — 조 단위
    회사의 반올림 오차가 중소형주의 실제 누락보다 크다.
    """
    residual = gross - sum(expenses) - operating
    tolerance = max(abs(operating) * 0.005, 1e8)
    return residual, tolerance


def shares_cross_check_pct(diluted, outstanding):
    """희석주식수와 유통주식수 합의 괴리(%). 둘 중 하나가 없으면 내지 않는다.

    괴리가 크면 우선주·전환증권 취급이 다르다는 뜻이고, 주식수는 주당 지표
    전부의 분모다 — 여기가 어긋나면 EPS·BPS·시가총액이 함께 어긋난다.
    """
    if not diluted or not outstanding:
        return None
    return abs(diluted / outstanding - 1) * 100


def back_derive_q4(annual, quarters):
    """연간에서 1~3분기를 빼 4분기를 복원한다. 세 분기가 다 있어야 성립한다.

    **음수가 나오면 값을 내지 않는다.** 이 뺄셈은 "thstrm_amount 가 누계가 아니라
    해당 3개월 금액"이라는 전제 위에 서 있는데, 그 전제가 깨진 회사에서는 결과가
    음수로 나온다. 음수 매출은 관측이 아니라 전제 위반이다 — 그대로 내면 화면에
    진짜 실적처럼 찍히고, 그것을 읽는 쪽은 4분기에 매출이 마이너스였다고 믿는다.

    영업이익은 실제로 음수일 수 있으므로 매출만 본다.
    """
    if len(quarters) != 3:
        return None
    values = list(quarters.values())
    if any(v[0] is None or v[1] is None for v in values):
        return None
    if annual[0] is None or annual[1] is None:
        return None
    revenue = annual[0] - sum(v[0] for v in values)
    if revenue < 0:
        return None
    return revenue, annual[1] - sum(v[1] for v in values)


def quarter_metrics(series):
    """분기 시리즈에 영업이익률과 전년동기 대비를 붙인다.

    **기저가 0 이하면 비율을 만들지 않는다.** 적자에서 흑자로 돌아선 해의
    전년동기 대비는 부호가 뒤집힌 숫자가 나오고, 그것은 성장률이 아니라
    나눗셈의 부산물이다. 0 이면 나눗셈 자체가 성립하지 않는다.
    """
    rows = []
    for label, (revenue, operating, source) in series.items():
        prior = series.get(f"{label[:2]}{int(label[2:]) - 1:02d}")
        margin = ratio_pct(operating, revenue)
        growth = change_pct(revenue, prior[0] if prior else None)
        rows.append({
            "label": label,
            "revenue": revenue,
            "operating_income": operating,
            "source": source,
            "operating_margin_pct": round(margin, 2) if margin is not None else None,
            "revenue_yoy_pct": round(growth, 1) if growth is not None else None,
        })
    return rows


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
            fourth = back_derive_q4((rev_y, op_y), vals)
            if fourth:
                vals["4Q"] = (fourth[0], fourth[1], f"{src_y}-역산")
        for q in ["1Q", "2Q", "3Q", "4Q"]:
            if q in vals:
                series[f"{q}{str(year)[2:]}"] = vals[q]

    for row in quarter_metrics(series):
        print(f"{row['label']:<10}"
              + cell(row["revenue"], 12, ",.0f", scale=1e8)
              + cell(row["operating_income"], 12, ",.0f", scale=1e8)
              + cell(row["operating_margin_pct"], 9, ".2f", unit="%")
              + cell(row["revenue_yoy_pct"], 11, "+.1f", unit="%")
              + f"{row['source']:>8}")

    print("-" * 66)
    print(f"원본 {len(raw_saved)}건 저장: data/raw/dart/")
    print("※ 4분기는 별도 보고서가 없어 연간에서 1~3분기를 뺀 값이다. "
          "뺀 결과가 음수면 분기 금액이 누계라는 뜻이므로 값을 내지 않는다.")


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

    doc = sub.add_parser("document", help="접수번호로 공시 원문 받기")
    doc.add_argument("rcept_no", help="14자리 접수번호")

    docs = sub.add_parser(
        "documents", help="색인의 잠정실적 공시 중 안 받은 원문을 받기"
    )
    docs.add_argument("stock_code")

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
        "document": cmd_document,
        "documents": cmd_documents,
        "financials": cmd_financials,
        "quarters": cmd_quarters,
        "fundamentals": cmd_fundamentals,
    }[a.cmd](a, key)


if __name__ == "__main__":
    sys.exit(main() or 0)
