#!/usr/bin/env python3
"""KRX Open API — 한국 시세의 **원문** 경로.

지금까지 KR 종목 가격은 Yahoo Finance 에서 왔다. 그건 2차 출처다. KRX 는
거래소 자신이므로 여기서 온 가격·시가총액·상장주식수는 `FACT` 등급이 된다.

## 이 API 가 주지 않는 것 — 먼저 밝힌다

**투자자별 거래실적(개인·외국인·기관 순매수)이 없다.** 2026-07-28 에 서비스
목록 전체를 확인했고, 다음 세 후보 경로가 모두 404 였다:
`sto/inv_trd`, `sto/invst_trd`, `sto/trdvol_by_invstr`.

카탈로그는 지수·주식·증권상품·채권·파생상품·일반상품·ESG 뿐이고 전부
"일별매매정보"(OHLCV·시가총액·거래대금)와 "종목기본정보"다. **수급은 이
API 로 못 받는다.** `flow_fetch.py` 는 계속 네이버 경유(`EXPLORATORY`)로 둔다.

## 인증 — 401 두 종류를 구분한다

- `Unauthorized Key` → 키 자체가 안 먹는다 (`.env` 의 `KRX_API_KEY` 확인)
- `Unauthorized API Call` → 키는 유효한데 **그 서비스에 활용신청이 안 됐다**

후자가 흔하다. KRX Open API 는 서비스마다 따로 신청해야 한다:
https://openapi.krx.co.kr/contents/OPP/MYPG/mypage/OPPMYPG001.cmd

## 호출 비용 — 시계열은 비싸다

이 API 는 **날짜 하나에 전 종목**을 준다(`basDd`). 그래서 한 종목의 250일
시계열을 만들려면 250번 호출해야 한다. 일별 호출 한도가 있으므로 기본 동작을
시계열 수집으로 두지 않았다.

대신 두 가지를 준다.

- `verify` — 특정 날짜의 KRX 원문과 기존 Yahoo CSV 를 **대조**한다. 한 번 호출로
  가격 출처의 등급을 올릴 수 있는지 판단한다. 이게 기본 용도다.
- `history` — 날짜 구간을 실제로 훑는다. 호출 수를 먼저 알려주고 확인을 받는다.

사용:
    python3 scripts/krx_fetch.py verify 005930 --date 2026-07-27
    python3 scripts/krx_fetch.py snapshot --date 2026-07-27 --market KOSPI
    python3 scripts/krx_fetch.py history 005930 --from 2026-07-01 --to 2026-07-27
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
from typing import NoReturn

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw" / "krx"
PRICE_DIR = ROOT / "knowledge" / "prices"

BASE = "https://data-dbg.krx.co.kr/svc/apis"
SUBSCRIBE_URL = "https://openapi.krx.co.kr/contents/OPP/MYPG/mypage/OPPMYPG001.cmd"

# 2026-07-28 에 404/401 판별로 실재를 확인한 경로들.
ENDPOINTS = {
    "KOSPI": "sto/stk_bydd_trd",
    "KOSDAQ": "sto/ksq_bydd_trd",
    "KONEX": "sto/knx_bydd_trd",
    "ETF": "etp/etf_bydd_trd",
}
INDEX_ENDPOINTS = {"KOSPI": "idx/kospi_dd_trd", "KOSDAQ": "idx/kosdaq_dd_trd",
                   "KRX": "idx/krx_dd_trd"}
# 지수 응답은 한 시장에 수십 개 지수를 담는다. 벤치마크로 쓰는 대표지수의
# 정확한 이름과, 대조할 Yahoo 파일. 첫 행은 '코스피 (외국주포함)' 인데
# 지수 레벨이 비어 있어서, 이름으로 정확히 집지 않으면 빈 값을 집는다.
BENCHMARKS = {
    "KOSPI": {"endpoint": "idx/kospi_dd_trd", "index_name": "코스피",
              "yahoo": "KS11.csv", "symbol": "^KS11"},
    "KOSDAQ": {"endpoint": "idx/kosdaq_dd_trd", "index_name": "코스닥",
               "yahoo": "KQ11.csv", "symbol": "^KQ11"},
}
F_IDX_NM = "IDX_NM"
F_IDX_CLOSE, F_IDX_HIGH, F_IDX_LOW = "CLSPRC_IDX", "HGPRC_IDX", "LWPRC_IDX"
BASE_INFO = {"KOSPI": "sto/stk_isu_base_info", "KOSDAQ": "sto/ksq_isu_base_info"}

# 응답 필드명 — 2026-07-28 에 실제 응답에서 확인했다.
# 추측했던 ISU_SRT_CD/ISU_ABBRV 는 없는 필드였고, 그 탓에 종목 검색이 조용히
# 첫 행으로 폴백했다 (삼성전자를 찾는데 AJ네트웍스가 나왔다). 없는 키로 찾으면
# 빈 결과가 아니라 **틀린 결과**가 나오므로 실제 응답으로 확정한다.
F_CODE, F_NAME = "ISU_CD", "ISU_NM"
F_CLOSE, F_OPEN, F_HIGH, F_LOW = "TDD_CLSPRC", "TDD_OPNPRC", "TDD_HGPRC", "TDD_LWPRC"
F_VOL, F_VAL = "ACC_TRDVOL", "ACC_TRDVAL"
F_MKTCAP, F_SHARES = "MKTCAP", "LIST_SHRS"
F_MARKET, F_CHG_PCT = "MKT_NM", "FLUC_RT"


def fail(msg: str) -> NoReturn:
    """NoReturn 이라고 밝혀야 `if x is None: fail(...)` 뒤에서 x 가 좁혀진다.

    None 으로 두면 타입 검사기가 fail 뒤에도 None 가능성을 남겨서, 실제로는
    도달할 수 없는 코드에 계속 경고가 붙는다.
    """
    print(f"오류: {msg}", file=sys.stderr)
    raise SystemExit(1)


def load_key() -> str:
    key = os.environ.get("KRX_API_KEY")
    if key:
        return key.strip()
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("KRX_API_KEY="):
                return line.split("=", 1)[1].strip()
    fail("KRX_API_KEY 가 없다. .env 에 넣거나 환경변수로 지정한다.")
    return ""


def request(path: str, params: dict, key: str) -> dict:
    url = f"{BASE}/{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"AUTH_KEY": key})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        body = e.read()
    except urllib.error.URLError as e:
        fail(f"연결 실패 — {e.reason}")
        return {}

    try:
        data = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError:
        fail(f"{path}: JSON 이 아닌 응답")
        return {}

    code = str(data.get("respCode") or "")
    if code == "401":
        msg = data.get("respMsg", "")
        if "Key" in msg:
            fail(f"인증키가 거부됐다 ({msg}). .env 의 KRX_API_KEY 를 확인한다")
        fail(f"'{path}' 서비스에 활용신청이 안 돼 있다 ({msg}).\n"
             f"  키는 유효하다. 아래에서 이 서비스를 신청한다:\n  {SUBSCRIBE_URL}")
    if code == "404":
        fail(f"{path}: 존재하지 않는 API 경로다")
    if code and code != "200":
        fail(f"KRX 응답 {code}: {data.get('respMsg')}")
    return data


def save_raw(name: str, payload) -> Path:
    day = dt.date.today().isoformat()
    out = RAW_DIR / day
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{name}_{dt.datetime.now().strftime('%H%M%S')}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def num(v) -> float | None:
    if v in (None, "", "-"):
        return None
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return None


def fetch_day(market: str, date: str, key: str) -> list[dict]:
    path = ENDPOINTS.get(market.upper())
    if not path:
        fail(f"{market}: {', '.join(ENDPOINTS)} 중 하나여야 한다")
    data = request(path, {"basDd": date.replace("-", "")}, key)
    return data.get("OutBlock_1") or data.get("outBlock_1") or []


def pick(rows: list[dict], code: str) -> dict | None:
    """종목코드로 한 건을 찾는다. **없으면 None 이고, 절대 폴백하지 않는다.**

    앞선 판은 필드명을 잘못 잡아 매칭이 하나도 안 됐는데, 호출부가 첫 행으로
    폴백해서 '삼성전자 대조'라며 AJ네트웍스를 보여줬다. 조용히 틀린 종목을
    내놓는 것이 못 찾는 것보다 훨씬 나쁘다.
    """
    if not rows:
        return None
    if F_CODE not in rows[0]:
        fail(f"응답에 {F_CODE} 필드가 없다. KRX 가 스키마를 바꿨다 "
             f"(실제 필드: {', '.join(sorted(rows[0]))})")
    return next((r for r in rows if str(r.get(F_CODE, "")).strip() == code), None)


def market_of(code: str) -> str:
    """어느 시장인지 모르면 KOSPI → KOSDAQ 순으로 찾는다."""
    path = PRICE_DIR / f"{code}_KQ.csv"
    return "KOSDAQ" if path.exists() else "KOSPI"


def cmd_verify(a, key):
    market = a.market or market_of(a.stock_code)
    rows = fetch_day(market, a.date, key)
    save_raw(f"bydd_{market}_{a.date}", rows)
    row = pick(rows, a.stock_code)
    if row is None:
        fail(f"{a.stock_code} 가 {a.date} {market} 응답에 없다 "
             f"(전체 {len(rows)}종목). 휴장일이거나 시장이 다르다")

    suffix = "KQ" if market == "KOSDAQ" else "KS"
    csv_path = PRICE_DIR / f"{a.stock_code}_{suffix}.csv"
    yahoo = None
    if csv_path.exists():
        with csv_path.open(encoding="utf-8") as f:
            lines = [ln for ln in f if not ln.lstrip().startswith("#")]
        yahoo = next((r for r in csv.DictReader(lines) if r["date"] == a.date), None)

    print("=" * 66)
    print(f"KRX 원문 대조 — {row.get(F_NAME, '').strip()} ({a.stock_code}) {a.date}")
    print("=" * 66)
    print(f"{'항목':<14}{'KRX 원문':>18}{'Yahoo CSV':>18}{'차이':>12}")
    for label, field, ykey in (("종가", F_CLOSE, "close"), ("고가", F_HIGH, "high"),
                               ("저가", F_LOW, "low"), ("거래량", F_VOL, "volume")):
        k = num(row.get(field))
        y = num(yahoo.get(ykey)) if yahoo else None
        diff = ("—" if k is None or y is None
                else ("일치" if abs(k - y) < max(1.0, abs(k) * 1e-6)
                      else f"{(y / k - 1) * 100:+.4f}%"))
        print(f"{label:<14}{(f'{k:,.0f}' if k is not None else '-'):>18}"
              f"{(f'{y:,.0f}' if y is not None else '-'):>18}{diff:>12}")

    print("-" * 66)
    print("[ Yahoo 가 주지 않는 것 — 밸류에이션에 직접 쓰인다 ]")
    cap, shares = num(row.get(F_MKTCAP)), num(row.get(F_SHARES))
    if cap is not None:
        print(f"  시가총액        {cap:>22,.0f} 원")
    if shares is not None:
        print(f"  상장주식수      {shares:>22,.0f} 주")
    if cap and shares:
        print(f"  역산 주당가격   {cap / shares:>22,.0f} 원")
    if not yahoo:
        print(f"\n※ {csv_path.name} 에 {a.date} 행이 없어 대조하지 못했다")
    print("\n※ KRX 는 거래소 원문이므로 여기서 온 값은 FACT 등급이다.")
    print("※ 상장주식수는 보통주만이다 — 우선주가 있는 회사의 희석주식수와 다르다.")


def cmd_index(a, key):
    """벤치마크 지수를 원문과 대조한다.

    `collect_events.py` 의 초과변동은 전부 ^KS11/^KQ11 을 기준으로 계산된다.
    "삼성전자 하락의 88% 가 시장" 같은 판단이 그 벤치마크 위에 서 있으므로,
    벤치마크가 2차 출처면 결론도 2차 출처다. 여기서 그걸 확인한다.
    """
    spec = BENCHMARKS[a.market]
    payload = request(spec["endpoint"], {"basDd": a.date.replace("-", "")}, key)
    rows = payload.get("OutBlock_1") or payload.get("outBlock_1") or []
    save_raw(f"idx_{a.market}_{a.date}", rows)

    row = next((r for r in rows if r.get(F_IDX_NM) == spec["index_name"]), None)
    if row is None:
        fail(f"{spec['index_name']} 이 응답에 없다 (지수 {len(rows)}개). "
             f"휴장일이거나 지수명이 바뀌었다")

    csv_path = PRICE_DIR / spec["yahoo"]
    yahoo = None
    if csv_path.exists():
        with csv_path.open(encoding="utf-8") as f:
            lines = [ln for ln in f if not ln.lstrip().startswith("#")]
        yahoo = next((r for r in csv.DictReader(lines) if r["date"] == a.date), None)

    print("=" * 66)
    print(f"벤치마크 대조 — {spec['index_name']} ({spec['symbol']}) {a.date}")
    print("=" * 66)
    print(f"{'항목':<8}{'KRX 원문':>16}{'Yahoo CSV':>18}{'차이':>14}")
    for label, field, ykey in (("종가", F_IDX_CLOSE, "close"),
                               ("고가", F_IDX_HIGH, "high"),
                               ("저가", F_IDX_LOW, "low")):
        k, y = num(row.get(field)), (num(yahoo.get(ykey)) if yahoo else None)
        diff = ("—" if k is None or y is None
                else ("일치" if abs(k - y) < 0.01 else f"{(y / k - 1) * 100:+.4f}%"))
        print(f"{label:<8}{(f'{k:,.2f}' if k is not None else '-'):>16}"
              f"{(f'{y:,.2f}' if y is not None else '-'):>18}{diff:>14}")

    print("-" * 66)
    cap, val = num(row.get(F_MKTCAP)), num(row.get(F_VAL))
    if cap:
        print(f"  시장 시가총액   {cap / 1e12:>12,.1f}조")
    if val:
        print(f"  거래대금        {val / 1e12:>12,.2f}조")
    print(f"  등락률          {row.get(F_CHG_PCT, '-'):>12}%")
    print("\n※ 초과변동 계산의 벤치마크다. 여기가 원문이면 "
          "'하락의 몇 %가 시장인가' 도 원문 위에 선다.")


def cmd_snapshot(a, key):
    rows = fetch_day(a.market, a.date, key)
    path = save_raw(f"bydd_{a.market}_{a.date}", rows)
    print(f"{a.market} {a.date} — {len(rows)}종목")
    print(f"원본: {path.relative_to(ROOT)}")
    for r in rows[: a.limit]:
        print(f"  {str(r.get(F_CODE, '')).strip():<8}{r.get(F_NAME, '').strip()[:16]:<18}"
              f"{num(r.get(F_CLOSE)) or 0:>12,.0f}{num(r.get(F_MKTCAP)) or 0:>20,.0f}")


def cmd_history(a, key):
    start = dt.date.fromisoformat(a.start)
    end = dt.date.fromisoformat(a.end)
    days = [start + dt.timedelta(days=i) for i in range((end - start).days + 1)]
    days = [d for d in days if d.weekday() < 5]  # 주말은 부르지 않는다
    if not a.yes:
        print(f"※ {len(days)}번 호출한다 ({a.start} ~ {a.end}, 주말 제외). "
              "KRX 는 날짜당 전 종목을 주므로 시계열은 날짜 수만큼 든다.")
        print("  진행하려면 --yes 를 붙인다.")
        return

    market = a.market or market_of(a.stock_code)
    out = []
    for d in days:
        rows = fetch_day(market, d.isoformat(), key)
        row = pick(rows, a.stock_code)
        if row:
            out.append({
                "date": d.isoformat(),
                "close": num(row.get(F_CLOSE)), "high": num(row.get(F_HIGH)),
                "low": num(row.get(F_LOW)), "volume": num(row.get(F_VOL)),
                "market_cap": num(row.get(F_MKTCAP)),
                "listed_shares": num(row.get(F_SHARES)),
            })
        time.sleep(0.2)

    if not out:
        fail("수집된 거래일이 없다")
    suffix = "KQ" if market == "KOSDAQ" else "KS"
    path = PRICE_DIR / f"{a.stock_code}_{suffix}_krx.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        f.write(f"# {a.stock_code} {market} 일별매매정보 / 통화 KRW\n")
        f.write(f"# 수집일 {dt.date.today().isoformat()} / 출처 KRX Open API "
                f"({ENDPOINTS[market]}) — 거래소 원문, FACT 등급\n")
        w = csv.DictWriter(f, fieldnames=list(out[0]))
        w.writeheader()
        w.writerows(out)
    print(f"{a.stock_code} {len(out)}거래일 {out[0]['date']} ~ {out[-1]['date']}")
    print(f"저장: {path.relative_to(ROOT)}")


def main():
    p = argparse.ArgumentParser(description="KRX Open API — 한국 시세 원문")
    sub = p.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("verify", help="특정일 KRX 원문과 기존 CSV 대조")
    v.add_argument("stock_code")
    v.add_argument("--date", required=True, help="YYYY-MM-DD")
    v.add_argument("--market", choices=list(ENDPOINTS))

    s = sub.add_parser("snapshot", help="특정일 전 종목")
    s.add_argument("--date", required=True)
    s.add_argument("--market", default="KOSPI", choices=list(ENDPOINTS))
    s.add_argument("--limit", type=int, default=10)

    h = sub.add_parser("history", help="날짜 구간 시계열 (호출 비쌈)")
    h.add_argument("stock_code")
    h.add_argument("--from", dest="start", required=True)
    h.add_argument("--to", dest="end", required=True)
    h.add_argument("--market", choices=list(ENDPOINTS))
    h.add_argument("--yes", action="store_true", help="호출 수를 확인했다")

    i = sub.add_parser("index", help="벤치마크 지수를 원문과 대조")
    i.add_argument("market", choices=list(BENCHMARKS))
    i.add_argument("--date", required=True, help="YYYY-MM-DD")

    a = p.parse_args()
    key = load_key()
    {"verify": cmd_verify, "snapshot": cmd_snapshot,
     "history": cmd_history, "index": cmd_index}[a.cmd](a, key)


if __name__ == "__main__":
    main()
