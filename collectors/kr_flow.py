#!/usr/bin/env python3
"""한국 시장·종목의 투자자별 순매매 관측치를 수집한다.

현재 출처는 네이버 금융이 재배포하는 KRX 집계이므로 산출물 등급은
`EXPLORATORY`다. 수급 수치만 저장하며 가격 변동의 원인을 판정하지 않는다.

사용:
    python3.13 -m collectors.kr_flow market KOSPI --days 90
    python3.13 -m collectors.kr_flow market KOSDAQ --days 90
    python3.13 -m collectors.kr_flow stock 005930 --days 90
    python3.13 -m collectors.kr_flow show KOSPI --days 20
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FLOW_DIR = ROOT / "knowledge" / "flows"
RAW_DIR = ROOT / "data" / "raw" / "naver"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

MARKET_CODE = {"KOSPI": "01", "KOSDAQ": "02"}
INDEX_URL = ("https://finance.naver.com/sise/investorDealTrendDay.naver"
             "?bizdate={bizdate}&sosok={sosok}")
STOCK_URL = "https://finance.naver.com/item/frgn.naver?code={code}&page={page}"
STOCK_API_URL = ("https://m.stock.naver.com/api/stock/{code}/trend"
                 "?pageSize=60&page={page}")

# 네이버 지수 수급 표의 열 순서. 헤더가 2행으로 쪼개져 있어 위치로 읽는다.
MARKET_FIELDS = ("date", "individual", "foreign", "institution_total",
                 "fin_invest", "insurance", "trust", "bank", "other_fin",
                 "pension", "other_corp")
STOCK_FIELDS = ("date", "close", "change_pct", "volume",
                "institution_net", "foreign_net", "foreign_shares", "foreign_pct",
                "individual_net")

SOURCE_NOTE = ("네이버 금융(KRX 집계 재배포) · 2차 출처이므로 EXPLORATORY 등급이다. "
               "KRX 원문 재확인 전에는 매매 근거로 승격하지 않는다. "
               "종가·등락률은 거래소 원문과 다를 수 있으므로 수급 자료의 가격을 "
               "평가가격으로 쓰지 않는다. 기타법인 수급은 제공하지 않는다")


def fail(msg: str) -> None:
    print(f"오류: {msg}", file=sys.stderr)
    raise SystemExit(1)


def get(url: str) -> str:
    """구 금융 화면은 EUC-KR, 새 화면은 응답에 명시한 UTF-8로 읽는다."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            encoding = r.headers.get_content_charset() or "euc-kr"
    except urllib.error.HTTPError as e:
        fail(f"HTTP {e.code} — {url}")
    except urllib.error.URLError as e:
        fail(f"연결 실패 — {e.reason}")
    return raw.decode(encoding, errors="replace")


def keep_raw(html: str, name: str) -> Path:
    """원문을 수집 시각과 함께 남긴다. 나중에 파서를 고쳐도 재파싱할 수 있다."""
    day = dt.date.today().isoformat()
    out = RAW_DIR / day
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{name}_{dt.datetime.now().strftime('%H%M%S')}.html"
    path.write_text(html, encoding="utf-8")
    return path


def cells(row_html: str) -> list[str]:
    return [re.sub(r"<[^>]+>", "", c).replace("\xa0", " ").strip()
            for c in re.findall(r"<td[^>]*>(.*?)</td>", row_html, re.S)]


def rows_of(html: str) -> list[list[str]]:
    return [c for r in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S)
            if (c := cells(r))]


def num(text: str) -> float | None:
    """네이버는 부호를 +/− 로 적고 천단위 콤마를 쓴다. 빈 칸은 결측이다."""
    t = text.replace(",", "").replace("+", "").replace("−", "-").strip()
    if not t or t in {"-", "--"}:
        return None
    try:
        return float(t)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 지수 수급 — 주체별 순매수 (억원)
# ---------------------------------------------------------------------------

def parse_market_page(html: str) -> list[dict]:
    out = []
    for c in rows_of(html):
        # 데이터 행은 'YY.MM.DD' 로 시작하고 11칸이다. 페이지 하단의
        # 페이지네이션 행은 칸 수가 달라 여기서 걸러진다.
        if len(c) < len(MARKET_FIELDS) or not re.fullmatch(r"\d{2}\.\d{2}\.\d{2}", c[0]):
            continue
        yy, mm, dd = c[0].split(".")
        row = {"date": f"20{yy}-{mm}-{dd}"}
        for field, text in zip(MARKET_FIELDS[1:], c[1:]):
            row[field] = num(text)
        out.append(row)
    return out


def fetch_market(market: str, days: int) -> list[dict]:
    sosok = MARKET_CODE.get(market.upper())
    if sosok is None:
        fail(f"{market}: KOSPI 또는 KOSDAQ 이어야 한다")
    seen: dict[str, dict] = {}
    bizdate = dt.date.today()
    # 한 페이지가 약 10거래일이다. 넉넉히 돌되 진전이 없으면 멈춘다.
    for _ in range(days // 10 + 4):
        html = get(INDEX_URL.format(bizdate=bizdate.strftime("%Y%m%d"), sosok=sosok))
        keep_raw(html, f"flow_{market.upper()}_{bizdate.strftime('%Y%m%d')}")
        page = parse_market_page(html)
        if not page:
            break
        before = len(seen)
        for row in page:
            seen.setdefault(row["date"], row)
        oldest = min(r["date"] for r in page)
        if len(seen) == before:
            break  # 같은 페이지를 반복해서 받고 있다
        if len(seen) >= days:
            break
        bizdate = dt.date.fromisoformat(oldest) - dt.timedelta(days=1)
        time.sleep(0.4)
    return sorted(seen.values(), key=lambda r: r["date"])[-days:]


# ---------------------------------------------------------------------------
# 종목 수급 — 기관·외국인 순매매 (주)
# ---------------------------------------------------------------------------

def parse_stock_page(html: str) -> list[dict]:
    out = []
    for c in rows_of(html):
        if not c or not re.fullmatch(r"\d{4}\.\d{2}\.\d{2}", c[0]):
            continue
        if len(c) < 9:
            continue
        # 열: 날짜 종가 전일비 등락률 거래량 기관순매매 외국인순매매 보유주수 보유율
        out.append({
            "date": c[0].replace(".", "-"),
            "close": num(c[1]),
            "change_pct": num(c[3].replace("%", "")),
            "volume": num(c[4]),
            "institution_net": num(c[5]),
            "foreign_net": num(c[6]),
            "foreign_shares": num(c[7]),
            "foreign_pct": num(c[8].replace("%", "")),
        })
    return out


def fetch_stock(code: str, days: int) -> list[dict]:
    seen: dict[str, dict] = {}
    for page in range(1, days // 20 + 3):
        html = get(STOCK_URL.format(code=code, page=page))
        keep_raw(html, f"flow_{code}_p{page}")
        rows = parse_stock_page(html)
        if not rows:
            # 구 HTML 주소가 새 UTF-8 화면으로 이동하면 표가 없다. 숫자를
            # 추측하거나 낡은 자료를 최신으로 재저장하지 않고 공개 API로 받는다.
            if not seen:
                return fetch_stock_api(code, days)
            break
        before = len(seen)
        for row in rows:
            seen.setdefault(row["date"], row)
        if len(seen) == before or len(seen) >= days:
            break
        time.sleep(0.4)
    return sorted(seen.values(), key=lambda r: r["date"])[-days:]


def parse_stock_api(payload: list[dict], code: str) -> list[dict]:
    """순매매 주수만 읽는다. 미제공 보유주수·기타법인은 역산하지 않는다."""
    out = []
    for item in payload:
        date = str(item.get("bizdate", ""))
        if str(item.get("itemCode")) != code or not re.fullmatch(r"\d{8}", date):
            continue
        try:
            date = dt.datetime.strptime(date, "%Y%m%d").date().isoformat()
        except ValueError:
            continue
        close = num(str(item.get("closePrice", "")))
        change = num(str(item.get("compareToPreviousClosePrice", "")))
        previous = close - change if close is not None and change is not None else None
        out.append({
            "date": date, "close": close,
            "change_pct": change / previous * 100 if previous and change is not None else None,
            "volume": num(str(item.get("accumulatedTradingVolume", ""))),
            "institution_net": num(str(item.get("organPureBuyQuant", ""))),
            "foreign_net": num(str(item.get("foreignerPureBuyQuant", ""))),
            "individual_net": num(str(item.get("individualPureBuyQuant", ""))),
            "foreign_shares": None,
            "foreign_pct": num(str(item.get("foreignerHoldRatio", "")).replace("%", "")),
        })
    return out


def fetch_stock_api(code: str, days: int) -> list[dict]:
    seen: dict[str, dict] = {}
    for page in range(1, days // 60 + 3):
        url = STOCK_API_URL.format(code=code, page=page)
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}),
                                        timeout=30) as response:
                body = response.read().decode("utf-8")
        except (urllib.error.URLError, UnicodeDecodeError) as error:
            fail(f"{code}: 수급 API 수집 실패 — {error}")
        keep_raw(body, f"flow_{code}_api_p{page}")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            fail(f"{code}: 수급 API 응답이 JSON이 아니다")
        if not isinstance(payload, list):
            fail(f"{code}: 수급 API 응답이 목록이 아니다")
        rows = parse_stock_api(payload, code)
        before = len(seen)
        for row in rows:
            seen.setdefault(row["date"], row)
        if len(seen) == before or len(seen) >= days:
            break
        time.sleep(0.4)
    return sorted(seen.values(), key=lambda r: r["date"])[-days:]


# ---------------------------------------------------------------------------
# 저장 · 표시
# ---------------------------------------------------------------------------

def save(name: str, fields: tuple[str, ...], rows: list[dict], unit: str) -> Path:
    FLOW_DIR.mkdir(parents=True, exist_ok=True)
    path = FLOW_DIR / f"{name}.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        f.write(f"# {name} 투자자별 순매수 / 단위 {unit}\n")
        f.write(f"# 수집일 {dt.date.today().isoformat()} / 출처 {SOURCE_NOTE}\n")
        w = csv.DictWriter(f, fieldnames=list(fields))
        w.writeheader()
        w.writerows(rows)
    return path


def load(name: str) -> list[dict]:
    path = FLOW_DIR / f"{name}.csv"
    if not path.exists():
        fail(f"{path.relative_to(ROOT)} 가 없다. 먼저 market/stock 으로 수집한다")
    with path.open(newline="", encoding="utf-8") as f:
        lines = [ln for ln in f if not ln.lstrip().startswith("#")]
    return list(csv.DictReader(lines))


def cmd_market(a):
    rows = fetch_market(a.market, a.days)
    if not rows:
        fail(f"{a.market}: 수집된 행이 없다")
    path = save(f"market-{a.market.upper()}", MARKET_FIELDS, rows, "억원")
    print(f"{a.market.upper()}  {len(rows)}거래일  {rows[0]['date']} ~ {rows[-1]['date']}")
    print(f"저장: {path.relative_to(ROOT)}")


def cmd_stock(a):
    rows = fetch_stock(a.code, a.days)
    if not rows:
        fail(f"{a.code}: 수집된 행이 없다")
    # 날짜는 왔는데 순매수가 한 칸도 없으면 응답 형식이 바뀐 것이다. 빈 칸만
    # 담은 파일을 저장하면 하류는 그것을 순매수 0 으로 더한다.
    if not any(
        row.get(field) is not None
        for row in rows
        for field in ("institution_net", "foreign_net")
    ):
        fail(
            f"{a.code}: {len(rows)}거래일을 받았지만 기관·외국인 순매수 값이 "
            "하나도 없다. 응답 형식이 바뀌었을 수 있다 — 저장하지 않는다"
        )
    path = save(a.code, STOCK_FIELDS, rows, "주")
    print(f"{a.code}  {len(rows)}거래일  {rows[0]['date']} ~ {rows[-1]['date']}")
    print(f"저장: {path.relative_to(ROOT)}")


def cmd_show(a):
    name = a.name if a.name.replace("-", "").isdigit() else f"market-{a.name.upper()}"
    if (FLOW_DIR / f"{a.name}.csv").exists():
        name = a.name
    rows = load(name)[-a.days:]
    is_market = "individual" in rows[0]

    print("=" * 74)
    print(f"투자자별 순매수 — {name}   {rows[0]['date']} ~ {rows[-1]['date']} "
          f"({len(rows)}거래일)")
    print("=" * 74)

    if is_market:
        keys = [("individual", "개인"), ("foreign", "외국인"),
                ("institution_total", "기관계"), ("pension", "연기금"),
                ("trust", "투신"), ("fin_invest", "금융투자")]
        print(f"{'날짜':<12}" + "".join(f"{label:>11}" for _, label in keys))
        for r in rows[-15:]:
            line = f"{r['date']:<12}"
            for key, _ in keys:
                v = r.get(key) or ""
                line += f"{float(v):>+11,.0f}" if v else f"{'-':>11}"
            print(line)
        print("-" * 74)
        total = {key: sum(float(r[key]) for r in rows if r.get(key))
                 for key, _ in keys}
        print(f"{'기간 누계':<12}" + "".join(f"{total[k]:>+11,.0f}" for k, _ in keys))
        print("\n단위 억원. 순매수(+) / 순매도(−)")
    else:
        print(f"{'날짜':<12}{'종가':>10}{'등락률':>9}{'기관':>14}{'외국인':>14}{'외인비율':>9}")
        for r in rows[-15:]:
            print(f"{r['date']:<12}{float(r['close']):>10,.0f}"
                  f"{float(r['change_pct']):>+8.2f}%"
                  f"{float(r['institution_net']):>+14,.0f}"
                  f"{float(r['foreign_net']):>+14,.0f}"
                  f"{float(r['foreign_pct']):>8.2f}%")
        print("-" * 74)
        inst = sum(float(r["institution_net"]) for r in rows if r.get("institution_net"))
        frgn = sum(float(r["foreign_net"]) for r in rows if r.get("foreign_net"))
        print(f"{'기간 누계':<12}{'':>10}{'':>9}{inst:>+14,.0f}{frgn:>+14,.0f}")
        print("\n단위 주. 순매수(+) / 순매도(−)")

    print(f"\n※ 출처 등급 EXPLORATORY — {SOURCE_NOTE}")
    print("※ 이 표는 누가 팔았는지만 말한다. 왜 팔았는지는 말하지 않는다.")


def main():
    p = argparse.ArgumentParser(description="투자자별 수급 수집 (네이버 금융 경유)")
    sub = p.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("market", help="지수 투자자별 순매수")
    m.add_argument("market", help="KOSPI / KOSDAQ")
    m.add_argument("--days", type=int, default=90)

    s = sub.add_parser("stock", help="종목 기관·외국인 순매매")
    s.add_argument("code", help="005930 / 214450")
    s.add_argument("--days", type=int, default=90)

    v = sub.add_parser("show", help="수집된 수급 표시")
    v.add_argument("name", help="KOSPI / KOSDAQ / 005930")
    v.add_argument("--days", type=int, default=20)

    a = p.parse_args()
    {"market": cmd_market, "stock": cmd_stock, "show": cmd_show}[a.cmd](a)


if __name__ == "__main__":
    main()
