#!/usr/bin/env python3
"""KRX Open API에서 한국 시세·시가총액·상장주식수 원문을 수집한다.

API는 날짜별 전 종목 응답이므로 긴 시계열은 호출 비용이 크다. `verify`는 기존
가격을 원문과 대조하고, `snapshot`은 하루 전체를, `history`는 확인 후 기간을
수집한다. 투자자별 순매매는 제공하지 않아 `collectors.kr_flow`가 별도로 다룬다.

사용:
    python3.13 -m collectors.krx verify 005930 --date 2026-07-27
    python3.13 -m collectors.krx snapshot --date 2026-07-27 --market KOSPI
    python3.13 -m collectors.krx history 005930 --from 2026-07-01 --to 2026-07-27
    python3.13 -m collectors.krx backfill KOSDAQ
    python3.13 -m collectors.krx backfill-stock 214450
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

from collectors import price as price_series

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw" / "krx"
PRICE_DIR = ROOT / "knowledge" / "prices"

BASE = "https://data-dbg.krx.co.kr/svc/apis"
SUBSCRIBE_URL = "https://openapi.krx.co.kr/contents/OPP/MYPG/mypage/OPPMYPG001.cmd"

# KRX Open API 서비스 경로.
ENDPOINTS = {
    "KOSPI": "sto/stk_bydd_trd",
    "KOSDAQ": "sto/ksq_bydd_trd",
    "KONEX": "sto/knx_bydd_trd",
    "ETF": "etp/etf_bydd_trd",
}
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

# KRX 응답 필드명.
# 추측했던 ISU_SRT_CD/ISU_ABBRV 는 없는 필드였고, 그 탓에 종목 검색이 조용히
# 첫 행으로 폴백했다 (삼성전자를 찾는데 AJ네트웍스가 나왔다). 없는 키로 찾으면
# 빈 결과가 아니라 **틀린 결과**가 나오므로 실제 응답으로 확정한다.
F_CODE, F_NAME = "ISU_CD", "ISU_NM"
F_CLOSE, F_OPEN, F_HIGH, F_LOW = "TDD_CLSPRC", "TDD_OPNPRC", "TDD_HGPRC", "TDD_LWPRC"
F_VOL, F_VAL = "ACC_TRDVOL", "ACC_TRDVAL"
F_MKTCAP, F_SHARES = "MKTCAP", "LIST_SHRS"
F_MARKET, F_CHG_PCT = "MKT_NM", "FLUC_RT"


def fail(msg: str) -> NoReturn:
    """오류를 출력하고 종료한다."""
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
    """종목코드가 정확히 일치하는 행만 반환하며 임의 행으로 폴백하지 않는다."""
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
                else "일치" if abs(k - y) < max(1.0, abs(k) * 1e-6)
                else f"{y - k:+,.2f}" if not k
                else f"{(y / k - 1) * 100:+.4f}%")
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
    """상대수익률 계산에 쓰는 벤치마크 지수를 KRX 원문과 대조한다."""
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
        # 기준값이 0 이면 차이를 비율로 말할 수 없다. 0 으로 나누지 않고
        # 절대차를 그대로 보여준다 — 두 출처가 다르다는 사실이 요점이다.
        diff = ("—" if k is None or y is None
                else "일치" if abs(k - y) < 0.01
                else f"{y - k:+,.2f}" if not k
                else f"{(y / k - 1) * 100:+.4f}%")
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


def price_rows(path: Path) -> tuple[list[str], list[dict]]:
    """가격 CSV를 주석 헤더와 데이터 행으로 나눠 읽는다."""
    header, lines = [], []
    with path.open(encoding="utf-8-sig") as f:
        for line in f:
            (header if line.lstrip().startswith("#") else lines).append(line)
    return header, list(csv.DictReader(lines))


def calendar_files() -> list[Path]:
    """한국 거래소 달력의 근거가 되는 가격 파일들.

    유가증권시장과 코스닥시장은 휴장일이 같으므로 달력은 하나다. 예전에는 같은
    시장의 종목 파일만 봤는데, 2026-10-13 에는 코스닥 종목과 코스닥 지수가 같은
    하루(10-12)를 **함께** 잃었고 그 날짜를 가진 것은 코스피 지수뿐이었다 —
    같은 시장끼리만 대조해서는 보이지 않는 구멍이다.

    자기가 메운 `_krx.csv` 는 뺀다. 그것을 달력의 근거로 쓰면 순환 논증이다.
    """
    paths = [
        path
        for suffix in ("_KS.csv", "_KQ.csv")
        for path in PRICE_DIR.glob(f"*{suffix}")
        if not path.name.endswith("_krx.csv")
    ]
    paths += [
        PRICE_DIR / spec["yahoo"]
        for spec in BENCHMARKS.values()
        if (PRICE_DIR / spec["yahoo"]).exists()
    ]
    return sorted(set(paths))


def trading_calendar() -> set[str]:
    """한국 시계열들이 실제로 거래된 날짜의 합집합.

    한 파일은 자기 구멍을 볼 수 없다. 다른 파일에 그 날짜가 있어야 보인다.
    봉이 저장됐다가 사라진 날(`# missing_sessions`)도 넣는다 — 값은 버렸어도
    그날 거래가 있었다는 사실은 남아 있고, 모든 파일이 같은 날을 잃었을 때
    남는 흔적은 그것뿐이다.
    """
    days: set[str] = set()
    for path in calendar_files():
        _, rows = price_rows(path)
        days.update(r["date"] for r in rows if r.get("date"))
        days.update(price_series.missing_sessions(path))
    return days


def _neighbor_basis(rows: list[dict], date: str) -> tuple[float, float] | None:
    """빈 날짜에 붙일 (분할계수, 조정종가÷종가). 앞뒤 봉이 같은 값을 말할 때만.

    거래소 원문은 그날의 실제 호가만 준다. 저장된 시계열은 오늘 기준으로 분할이
    반영된 값이고 조정종가에는 배당이 들어 있어, 그 두 기준을 원문에 입혀야 같은
    파일에 놓을 수 있다. 기준은 분할일·배당락일에만 바뀌므로 앞뒤 봉이 같으면
    그 사이의 날도 같다. 다르면 그 경계가 빈 날의 앞인지 뒤인지 알 수 없다 —
    그때는 메우지 않는다. 한쪽을 골라 메우면 조용히 틀린 수익률이 된다.
    """
    before = [r for r in rows if r.get("date") and r["date"] < date]
    after = [r for r in rows if r.get("date") and r["date"] > date]
    if not before or not after:
        return None

    def basis(row: dict) -> tuple[float, float] | None:
        close = num(row.get("close"))
        adjusted = num(row.get("adjusted_close"))
        factor = num(row.get("split_adjustment_factor"))
        if not close or adjusted is None or not factor:
            return None
        return factor, adjusted / close

    # 목록의 순서가 아니라 날짜로 고른다. 같은 실행에서 먼저 메운 봉이 목록
    # 끝에 붙어 있어서, 순서로 고르면 1년 전 봉이 '직전 봉'이 된다.
    left = basis(max(before, key=lambda r: r["date"]))
    right = basis(min(after, key=lambda r: r["date"]))
    if left is None or right is None:
        return None
    if abs(left[0] - right[0]) > 1e-9 * max(1.0, abs(left[0])):
        return None
    # 조정종가는 6자리에서 끊어 저장하므로 비율이 1e-6 쯤 흔들린다. 배당락은
    # 그보다 두 자릿수 이상 크다.
    if abs(left[1] - right[1]) > 1e-4:
        return None
    return left


def backfill_series(csv_path: Path, *, symbol: str, endpoint: str, fetch_bar,
                    limit: int) -> None:
    """한 시계열의 누락 거래일을 KRX 원문으로 메운다.

    Yahoo 는 거래일을 통째로 빠뜨리고도 정상 응답을 준다. 그러면 일별 수익률이
    이틀치를 하루로 합쳐 계산되고, 벤치마크에서 일어나면 초과수익 부호까지
    뒤집힌다 (2026-07-28 코스피 -10.84% 폭락일이 실제로 그렇게 사라졌다).
    종목에서 일어나면 "최근 1개 세션 수익률"이 엉뚱한 날을 가리킨다
    (2026-10-12 파마리서치). 여기서 원문으로 되메운다.

    `fetch_bar(date)` 는 그날의 실제 호가 `{close, high, low, volume}` 를 주거나,
    거래소에도 없으면 None 을 준다.
    """
    if not csv_path.exists():
        fail(f"{csv_path.name} 이 없다. 먼저 collectors.price history 로 수집한다")

    header, rows = price_rows(csv_path)
    if not rows:
        fail(f"{csv_path.name} 에 데이터 행이 없다")
    have = {r["date"] for r in rows}
    first, last = min(have), max(have)
    calendar = trading_calendar()
    sources = len(calendar_files())

    missing = sorted(d for d in calendar if first <= d <= last and d not in have)
    marker = PRICE_DIR / "_backfill" / f"{csv_path.stem}.json"
    marker.parent.mkdir(parents=True, exist_ok=True)

    def write_marker(filled: list[str], unfilled: list[str],
                     reasons: dict[str, str]) -> None:
        marker.write_text(json.dumps({
            "schema_version": "price-backfill/1.0.0",
            "symbol": symbol, "target": csv_path.name,
            "collected_at": dt.datetime.now(dt.timezone.utc).isoformat(
                timespec="seconds"),
            "calendar_source": f"한국 시계열 {sources}개의 합집합 {len(calendar)}거래일",
            "checked_range": {"from": first, "to": last},
            "filled": filled, "unfilled": unfilled,
            **({"unfilled_reasons": reasons} if reasons else {}),
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    if not missing:
        write_marker([], [], {})
        print(f"{symbol} {first} ~ {last} — 누락 거래일 없다")
        return

    if len(missing) > limit:
        fail(f"누락 {len(missing)}일이 --limit {limit} 을 넘는다 "
             f"(날짜당 1회 호출). 확인 후 --limit 를 올린다:\n  "
             f"{', '.join(missing[:10])}{' …' if len(missing) > 10 else ''}")

    filled, unfilled, reasons = [], [], {}
    for date in missing:
        bar = fetch_bar(date)
        time.sleep(0.2)
        if bar is None or bar.get("close") is None:
            # 휴장일이면 KRX 도 주지 않는다 — 그건 구멍이 아니다.
            unfilled.append(date)
            continue
        basis = _neighbor_basis(rows, date)
        if basis is None:
            unfilled.append(date)
            reasons[date] = ("앞뒤 봉의 분할계수·배당 조정 기준이 달라 원문을 "
                             "어느 기준으로 옮길지 정할 수 없다")
            continue
        factor, adjusted_ratio = basis
        close = bar["close"] * factor
        rows.append({
            "date": date,
            "close": close,
            "adjusted_close": price_series._stable(close * adjusted_ratio),
            "split_adjustment_factor": factor,
            "high": bar["high"] * factor if bar.get("high") is not None else None,
            "low": bar["low"] * factor if bar.get("low") is not None else None,
            "volume": (bar["volume"] / factor
                       if bar.get("volume") is not None else None),
        })
        filled.append(date)

    if filled:
        rows.sort(key=lambda r: r["date"])
        present = {r["date"] for r in rows}
        earlier, _ = price_series._header_notes(
            csv_path, price_series.BACKFILLED_PREFIX)
        backfilled = sorted({*(d for d in earlier if d in present), *filled})
        still_missing = [d for d in price_series.missing_sessions(csv_path)
                         if d not in present]
        kept = [ln for ln in header if not ln.startswith(
            (price_series.BACKFILLED_PREFIX, price_series.MISSING_SESSIONS_PREFIX))]
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            f.writelines(kept)
            f.write(f"{price_series.BACKFILLED_PREFIX}{','.join(backfilled)} / "
                    f"source KRX Open API ({endpoint}) — 거래소 원문\n")
            if still_missing:
                f.write(f"{price_series.MISSING_SESSIONS_PREFIX}"
                        f"{','.join(still_missing)} / 봉이 저장됐다가 응답에서 "
                        "사라졌고 확정값으로 재확인되지 않았다\n")
            w = csv.DictWriter(f, fieldnames=price_series.FIELDS)
            w.writeheader()
            w.writerows(rows)

    write_marker(filled, unfilled, reasons)

    print(f"{symbol} {first} ~ {last}")
    print(f"  메움 {len(filled)}일: {', '.join(filled) or '—'}")
    holidays = [d for d in unfilled if d not in reasons]
    if holidays:
        print(f"  KRX 에도 없음 {len(holidays)}일 (휴장 추정): {', '.join(holidays)}")
    for date, reason in reasons.items():
        print(f"  메우지 않음 {date}: {reason}")
    print(f"기록: {marker.relative_to(ROOT)}")


def cmd_backfill(a, key):
    """벤치마크 지수 시계열의 누락 거래일을 메운다."""
    spec = BENCHMARKS[a.market]

    def fetch_bar(date: str) -> dict | None:
        payload = request(spec["endpoint"], {"basDd": date.replace("-", "")}, key())
        found = payload.get("OutBlock_1") or payload.get("outBlock_1") or []
        row = next((r for r in found if r.get(F_IDX_NM) == spec["index_name"]), None)
        if row is None:
            return None
        return {
            "close": num(row.get(F_IDX_CLOSE)),
            "high": num(row.get(F_IDX_HIGH)),
            "low": num(row.get(F_IDX_LOW)),
            # KRX 의 지수 거래량(시장 전체 주식 수)은 Yahoo 의 지수 거래량과
            # 단위가 다르다 (2026-07-27 Yahoo 275,700 vs KRX 330,556,522).
            # 한 열에 두 단위를 섞느니 비운다 — 지수 거래량은 쓰는 곳이 없다.
            "volume": None,
        }

    backfill_series(PRICE_DIR / spec["yahoo"], symbol=spec["symbol"],
                    endpoint=spec["endpoint"], fetch_bar=fetch_bar, limit=a.limit)


def cmd_backfill_stock(a, key):
    """종목·ETF 시계열의 누락 거래일을 메운다."""
    kosdaq = PRICE_DIR / f"{a.stock_code}_KQ.csv"
    csv_path = kosdaq if kosdaq.exists() else PRICE_DIR / f"{a.stock_code}_KS.csv"
    suffix = csv_path.stem.rsplit("_", 1)[-1]
    market = a.market or ("KOSDAQ" if suffix == "KQ" else "KOSPI")

    def fetch_bar(date: str) -> dict | None:
        row = pick(fetch_day(market, date, key()), a.stock_code)
        if row is None:
            return None
        return {
            "close": num(row.get(F_CLOSE)),
            "high": num(row.get(F_HIGH)),
            "low": num(row.get(F_LOW)),
            # 종목 거래량은 Yahoo 와 같은 단위(주)다.
            "volume": num(row.get(F_VOL)),
        }

    backfill_series(csv_path, symbol=f"{a.stock_code}.{suffix}",
                    endpoint=ENDPOINTS[market], fetch_bar=fetch_bar, limit=a.limit)


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

    b = sub.add_parser("backfill", help="벤치마크 지수의 누락 거래일을 원문으로 메움")
    b.add_argument("market", choices=list(BENCHMARKS))
    b.add_argument("--limit", type=int, default=20,
                   help="한 번에 메울 최대 일수 (날짜당 1회 호출)")

    bs = sub.add_parser("backfill-stock",
                        help="종목·ETF 시계열의 누락 거래일을 원문으로 메움")
    bs.add_argument("stock_code")
    bs.add_argument("--market", choices=list(ENDPOINTS),
                    help="생략하면 가격 파일의 접미사로 정한다. ETF 는 명시한다")
    bs.add_argument("--limit", type=int, default=20,
                    help="한 번에 메울 최대 일수 (날짜당 1회 호출)")

    a = p.parse_args()
    if a.cmd in ("backfill", "backfill-stock"):
        # 구멍이 있는지는 로컬 파일만으로 안다. 메울 것이 없으면 거래소를 부르지
        # 않으므로 키도 필요 없다 — 키를 먼저 요구하면 키 없는 설치에서는
        # "구멍 없음"조차 기록하지 못한다. 실제로 부를 때 처음 읽는다.
        cached: list[str] = []

        def key() -> str:
            if not cached:
                cached.append(load_key())
            return cached[0]
    else:
        key = load_key()
    {"verify": cmd_verify, "snapshot": cmd_snapshot, "history": cmd_history,
     "index": cmd_index, "backfill": cmd_backfill,
     "backfill-stock": cmd_backfill_stock}[a.cmd](a, key)


if __name__ == "__main__":
    main()
