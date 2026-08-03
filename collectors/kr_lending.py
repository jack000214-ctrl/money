#!/usr/bin/env python3
"""한국 상장주식의 종목별 대차잔고를 수집한다.

대차잔고는 빌려간 주식이 아직 상환되지 않고 남아 있는 수량이다. 공매도는
빌린 주식을 팔아야 성립하므로 대차잔고는 공매도 포지션의 **재료** 이지
공매도 잔고 그 자체가 아니다. 둘을 같은 것으로 읽으면 안 된다 — 대차는
공매도 외에 결제·차익거래·의결권 목적으로도 일어난다.

**공매도 잔고는 이 저장소가 받을 수 없다.** 2026-08-03 확인 기준
`data.krx.co.kr` 과 `short.krx.co.kr` 이 모두 로그인 벽으로 바뀌었고,
네이버 금융의 공매도 페이지는 삭제됐으며, KRX Open API 에도 공매도 서비스가
없다. data.go.kr 에도 종목별 공매도 잔고 API 는 없다. 그래서 이 수집기는
공매도를 대신하지 않고 **그 공백을 명시한 채로** 대차만 싣는다.

출처는 data.go.kr 금융위원회_주식대차거래정보(15124865)이며 원천은
한국예탁결제원 집계다. 정부 공식 개방데이터이므로 네이버 재배포와 달리
`MARKET_DATA` 로 싣는다.

사용:
    python3.13 -m collectors.kr_lending probe --code 005930
    python3.13 -m collectors.kr_lending stock 005930 --days 90
    python3.13 -m collectors.kr_lending show 005930 --days 20
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LENDING_DIR = ROOT / "knowledge" / "lending"
RAW_DIR = ROOT / "data" / "raw" / "datagokr"

BASE = "https://apis.data.go.kr/1160100/service/GetCMStckLnbInfoService"
DETAIL = "getStckLnbDetail"
SIGNUP_URL = "https://www.data.go.kr/data/15124865/openapi.do"

# 종목코드·종목명·시장은 파일 전체에서 같은 값이라 헤더에만 적는다. 열로 두면
# 90행 내내 같은 문자열을 반복해 파일의 19%를 먹는다 (collectors.kr_flow 와 같은 관례).
FIELDS = ("date", "contracted_shares", "redeemed_shares",
          "balance_shares", "balance_amount")

# 응답 필드명은 swagger 로 확정했다. 추측하지 않는다.
RESP = {
    "date": "basDt",
    "code": "stckItmsCd",
    "name": "stckItmsNm",
    "market": "mrktClsfNm",
    "contracted_shares": "cclStckCnt",
    "redeemed_shares": "rdptStckCnt",
    "balance_shares": "balnStckCnt",
    "balance_amount": "balnStckAmt",
}

SOURCE_NOTE = ("data.go.kr 금융위원회_주식대차거래정보(15124865) · 원천 한국예탁결제원. "
               "대차잔고는 공매도 잔고가 아니다")

# 종목 필터로 쓸 파라미터 이름 후보. data.go.kr 금융위 API 는 파라미터를
# 공개 문서에 싣지 않는 경우가 많고, **모르는 파라미터를 조용히 무시하고
# 전 종목을 돌려준다.** 무시된 줄 모르고 저장하면 다른 종목 숫자를 이 종목
# 것으로 읽는다 — 빈 결과보다 나쁘다. 그래서 후보를 순서대로 시도하되
# 반드시 `verify_rows` 로 실제 필터링 여부를 확인한다.
CODE_PARAMS = ("stckItmsCd", "likeStckItmsCd", "itmsCd", "likeItmsCd")


def fail(msg: str) -> None:
    print(f"오류: {msg}", file=sys.stderr)
    raise SystemExit(1)


def load_key() -> str:
    """data.go.kr 은 같은 키를 Encoding·Decoding 두 벌로 내준다.

    Encoding 쪽은 이미 `%2F`·`%3D` 로 인코딩돼 있어서, 그대로 urlencode 에
    넣으면 `%` 가 다시 `%25` 로 바뀌어 **등록되지 않은 서비스키**가 된다.
    어느 쪽을 넣었든 먼저 풀어서 한 벌로 만든 뒤 한 번만 인코딩한다.
    """
    key = os.environ.get("DATA_GO_KR_API_KEY")
    if not key:
        env = ROOT / ".env"
        if env.exists():
            for line in env.read_text(encoding="utf-8").splitlines():
                if line.startswith("DATA_GO_KR_API_KEY="):
                    key = line.split("=", 1)[1]
                    break
    if not key:
        fail("DATA_GO_KR_API_KEY 가 없다. data.go.kr 에서 발급한 일반 인증키를\n"
             f"  .env 에 넣는다. 활용신청: {SIGNUP_URL}")
        return ""
    return urllib.parse.unquote(key.strip())


def request(params: dict, key: str) -> dict:
    """data.go.kr 은 오류를 HTTP 200 안에 담아 보내기도 한다. 본문을 봐야 안다."""
    query = urllib.parse.urlencode({**params, "serviceKey": key,
                                    "resultType": "json"})
    url = f"{BASE}/{DETAIL}?{query}"
    req = urllib.request.Request(url, headers={"User-Agent": "money/cio"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        body = e.read()
    except urllib.error.URLError as e:
        fail(f"연결 실패 — {e.reason}")
        return {}

    text = body.decode("utf-8", errors="replace")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        fail(f"JSON 이 아닌 응답이다. 앞부분:\n  {text[:300]}")
        return {}

    # 인증·한도 오류는 별도 봉투로 온다.
    envelope = data.get("OpenAPI_ServiceResponse")
    if envelope:
        head = envelope.get("cmmMsgHeader", {})
        err = head.get("errMsg", "")
        reason = head.get("returnAuthMsg", "")
        if "SERVICE_KEY_IS_NOT_REGISTERED" in err:
            fail(f"인증키가 등록되지 않았다 ({reason}).\n"
                 f"  키 자체가 틀렸거나, 이 서비스에 활용신청이 안 돼 있다:\n  {SIGNUP_URL}")
        if "LIMITED_NUMBER_OF_SERVICE_REQUESTS" in err:
            fail(f"일일 호출 한도를 넘었다 ({reason}). 내일 다시 시도한다")
        if "NO_OPENAPI_SERVICE" in err:
            fail(f"서비스 경로가 폐기됐다 ({reason}). BASE 를 다시 확인한다:\n  {SIGNUP_URL}")
        fail(f"data.go.kr 오류: {err} / {reason}")

    header = data.get("response", {}).get("header", {})
    code = str(header.get("resultCode") or "")
    if code and code not in {"00", "0"}:
        fail(f"data.go.kr 응답 {code}: {header.get('resultMsg')}")
    return data


def items_of(data: dict) -> list[dict]:
    body = data.get("response", {}).get("body", {}) or {}
    items = body.get("items") or {}
    if isinstance(items, list):
        return items
    item = items.get("item") if isinstance(items, dict) else None
    if item is None:
        return []
    return item if isinstance(item, list) else [item]


def total_count(data: dict) -> int:
    body = data.get("response", {}).get("body", {}) or {}
    try:
        return int(body.get("totalCount") or 0)
    except (TypeError, ValueError):
        return 0


def save_raw(name: str, payload) -> Path:
    day = dt.date.today().isoformat()
    out = RAW_DIR / day
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{name}_{dt.datetime.now().strftime('%H%M%S')}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return path


def num(value) -> float | int | None:
    """주식수·금액은 원래 정수다. 파이썬 float 로 두면 `.0` 이 파일에 그대로 쓰인다."""
    if value in (None, "", "-"):
        return None
    try:
        f = float(str(value).replace(",", ""))
    except ValueError:
        return None
    return int(f) if f.is_integer() else f


def to_row(item: dict) -> dict:
    raw_date = str(item.get(RESP["date"]) or "")
    date = (f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
            if len(raw_date) == 8 and raw_date.isdigit() else raw_date)
    return {
        "date": date,
        "code": str(item.get(RESP["code"]) or ""),
        "name": item.get(RESP["name"]) or "",
        "market": item.get(RESP["market"]) or "",
        "contracted_shares": num(item.get(RESP["contracted_shares"])),
        "redeemed_shares": num(item.get(RESP["redeemed_shares"])),
        "balance_shares": num(item.get(RESP["balance_shares"])),
        "balance_amount": num(item.get(RESP["balance_amount"])),
    }


def verify_rows(rows: list[dict], code: str, param: str) -> list[dict]:
    """필터가 실제로 걸렸는지 확인한다. 안 걸렸으면 저장하지 않고 멈춘다.

    API 가 모르는 파라미터를 무시하고 전 종목을 돌려주면, 그대로 저장할 경우
    다른 종목의 대차잔고를 이 종목 것으로 읽게 된다. 그래서 '요청한 종목이
    아닌 행이 하나라도 있으면' 실패로 본다.
    """
    others = sorted({r["code"] for r in rows if r["code"] and r["code"] != code})
    if others:
        sample = ", ".join(others[:5])
        fail(f"'{param}' 로 걸러지지 않았다. 응답에 다른 종목이 섞여 있다 "
             f"({len(others)}종목, 예: {sample}).\n"
             f"  이 응답을 {code} 의 대차잔고로 저장하면 틀린 숫자를 읽게 되므로 멈춘다.\n"
             f"  probe 로 실제 파라미터 이름을 먼저 확인한다:\n"
             f"    python3.13 -m collectors.kr_lending probe --code {code}")
    return rows


def fetch_stock(code: str, days: int, key: str) -> tuple[list[dict], str]:
    """종목 필터 파라미터를 순서대로 시도하고, 실제로 걸린 것만 쓴다."""
    end = dt.date.today()
    begin = end - dt.timedelta(days=int(days * 1.6) + 10)  # 거래일 기준 여유
    window = {"beginBasDt": begin.strftime("%Y%m%d"),
              "endBasDt": end.strftime("%Y%m%d")}

    last_error = ""
    for param in CODE_PARAMS:
        params = {**window, param: code, "numOfRows": "500", "pageNo": "1"}
        data = request(params, key)
        save_raw(f"lending_{code}_{param}", data)
        items = items_of(data)
        if not items:
            last_error = f"'{param}' 로는 행이 0개다"
            continue
        rows = [to_row(i) for i in items]
        rows = verify_rows(rows, code, param)
        if total_count(data) > len(items):
            rows += fetch_more(code, param, window, key,
                               total_count(data), len(items))
        rows = [r for r in rows if r["date"]]
        rows.sort(key=lambda r: r["date"])
        return rows[-days:], param

    fail(f"{code}: 어떤 파라미터로도 행을 받지 못했다 ({last_error}).\n"
         f"  probe 로 응답 원문을 확인한다:\n"
         f"    python3.13 -m collectors.kr_lending probe --code {code}")
    return [], ""


def fetch_more(code: str, param: str, window: dict, key: str,
               total: int, got: int) -> list[dict]:
    rows: list[dict] = []
    page = 2
    while got < total and page <= 20:
        data = request({**window, param: code, "numOfRows": "500",
                        "pageNo": str(page)}, key)
        items = items_of(data)
        if not items:
            break
        rows += verify_rows([to_row(i) for i in items], code, param)
        got += len(items)
        page += 1
        time.sleep(0.3)
    return rows


def save(code: str, rows: list[dict]) -> Path:
    LENDING_DIR.mkdir(parents=True, exist_ok=True)
    path = LENDING_DIR / f"{code}.csv"
    name = next((r["name"] for r in rows if r.get("name")), "")
    market = next((r["market"] for r in rows if r.get("market")), "")
    with path.open("w", newline="", encoding="utf-8") as f:
        f.write(f"# {code} {name} ({market}) 대차거래 / 단위 주·원\n")
        f.write(f"# 수집일 {dt.date.today().isoformat()} / 출처 {SOURCE_NOTE}\n")
        f.write("# 공매도 잔고 아님 — 대차는 결제·차익거래·의결권 목적으로도 일어난다\n")
        # extrasaction='ignore' — 행에는 검증용 code/name/market 이 남아 있지만
        # 파일에는 쓰지 않는다. 셋 다 파일 전체에서 같은 값이라 헤더에 있다.
        w = csv.DictWriter(f, fieldnames=list(FIELDS), extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return path


def header_name(path: Path) -> str:
    """첫 헤더 줄에서 종목명을 되읽는다. 열로 저장하지 않으므로 여기서 온다."""
    first = path.read_text(encoding="utf-8").splitlines()[0]
    body = first.lstrip("# ").split(" 대차거래")[0]
    return body.strip() or path.stem


def load(code: str) -> list[dict]:
    path = LENDING_DIR / f"{code}.csv"
    if not path.exists():
        fail(f"{path.relative_to(ROOT)} 가 없다. 먼저 stock 으로 수집한다")
    with path.open(newline="", encoding="utf-8") as f:
        lines = [ln for ln in f if not ln.lstrip().startswith("#")]
    return list(csv.DictReader(lines))


def cmd_stock(a):
    key = load_key()
    rows, param = fetch_stock(a.code, a.days, key)
    if not rows:
        fail(f"{a.code}: 수집된 행이 없다")
    path = save(a.code, rows)
    print(f"{a.code}  {len(rows)}거래일  {rows[0]['date']} ~ {rows[-1]['date']}"
          f"  (필터 파라미터 {param})")
    print(f"저장: {path.relative_to(ROOT)}")


def cmd_probe(a):
    """키와 파라미터 이름을 확인한다. 저장하지 않는다."""
    key = load_key()
    print(f"키 길이 {len(key)} · 앞 6자 {key[:6]}…")
    end = dt.date.today()
    begin = end - dt.timedelta(days=30)
    window = {"beginBasDt": begin.strftime("%Y%m%d"),
              "endBasDt": end.strftime("%Y%m%d")}

    base = request({**window, "numOfRows": "5", "pageNo": "1"}, key)
    print(f"\n필터 없이 호출 → totalCount {total_count(base)}, "
          f"받은 행 {len(items_of(base))}")
    for item in items_of(base)[:3]:
        print("   ", json.dumps(item, ensure_ascii=False)[:150])

    if not a.code:
        return
    print(f"\n종목 필터 후보 {len(CODE_PARAMS)}개를 {a.code} 로 시험한다")
    for param in CODE_PARAMS:
        data = request({**window, param: a.code, "numOfRows": "50",
                        "pageNo": "1"}, key)
        rows = [to_row(i) for i in items_of(data)]
        codes = {r["code"] for r in rows if r["code"]}
        if not rows:
            verdict = "행 0개 — 이 이름은 아니다"
        elif codes == {a.code}:
            verdict = f"걸림 ✅  ({len(rows)}행 전부 {a.code})"
        else:
            verdict = f"무시됨 ❌  ({len(codes)}종목 섞임)"
        print(f"   {param:<16} totalCount {total_count(data):>8}  {verdict}")


def cmd_show(a):
    rows = load(a.code)[-a.days:]
    if not rows:
        fail(f"{a.code}: 표시할 행이 없다")
    identity = header_name(LENDING_DIR / f"{a.code}.csv")

    print("=" * 74)
    print(f"대차거래 — {identity}   {rows[0]['date']} ~ {rows[-1]['date']} "
          f"({len(rows)}거래일)")
    print("=" * 74)
    print(f"{'날짜':<12}{'체결':>14}{'상환':>14}{'잔고':>16}{'잔고금액(억)':>14}")
    for r in rows[-15:]:
        amount = float(r["balance_amount"]) / 1e8 if r.get("balance_amount") else 0
        print(f"{r['date']:<12}"
              f"{float(r['contracted_shares'] or 0):>14,.0f}"
              f"{float(r['redeemed_shares'] or 0):>14,.0f}"
              f"{float(r['balance_shares'] or 0):>16,.0f}"
              f"{amount:>14,.0f}")

    first = float(rows[0]["balance_shares"] or 0)
    last = float(rows[-1]["balance_shares"] or 0)
    print("-" * 74)
    change = last - first
    pct = (change / first * 100) if first else 0
    print(f"기간 잔고 변화  {first:,.0f} → {last:,.0f}주  "
          f"({change:+,.0f}주 / {pct:+.1f}%)")

    print(f"\n※ 출처 {SOURCE_NOTE}")
    print("※ 대차잔고는 '빌려간 뒤 아직 갚지 않은 주식'이다. 공매도 잔고가 아니다.")
    print("※ 잔고가 늘었다는 것은 누가 왜 빌렸는지를 말하지 않는다.")


def main():
    p = argparse.ArgumentParser(
        description="종목별 대차잔고 수집 (data.go.kr 금융위원회)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("stock", help="종목 대차거래·잔고")
    s.add_argument("code", help="005930 / 214450")
    s.add_argument("--days", type=int, default=90)

    b = sub.add_parser("probe", help="키와 파라미터 이름 확인 (저장하지 않음)")
    b.add_argument("--code", default="", help="종목 필터를 시험할 코드")

    v = sub.add_parser("show", help="수집된 대차잔고 표시")
    v.add_argument("code")
    v.add_argument("--days", type=int, default=20)

    a = p.parse_args()
    {"stock": cmd_stock, "probe": cmd_probe, "show": cmd_show}[a.cmd](a)


if __name__ == "__main__":
    main()
