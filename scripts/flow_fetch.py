#!/usr/bin/env python3
"""투자자별 수급 수집기 — "한국만 왜 빠지나"의 관측 장치.

2026-07-28 에 삼성전자 하락 원인을 세 번 고쳐 잡았다. 종목 고유 → 시장 전체 →
한국 국지. 마지막 단계에서 막힌 이유는 **"한국 시장에서 누가 팔았나"를 볼 데이터가
없었기** 때문이다. 지수 하락률은 결과이지 주체가 아니다. 외국인이 판 것과 기관이
판 것과 개인이 던진 것은 지속성이 전혀 다르다.

**이 수집기는 원인을 말하지 않는다.** 누가 얼마를 순매도했는지만 적는다.
"외국인이 팔아서 빠졌다"는 해석은 모델이 다른 패킷과 함께 보고 할 일이고,
프로그램이 단정하면 그게 결론 유도다.

## 출처 등급 — 여기가 중요하다

원문은 KRX 다. 그런데 `data.krx.co.kr` 의 JSON 엔드포인트
(`getJsonData.cmd`)는 세션 쿠키·리퍼러·브라우저 헤더를 모두 갖춰도 `LOGOUT`
만 돌려준다 (2026-07-28 확인, WAF 추정). 그래서 이 수집기는 **네이버 금융이
재배포하는 같은 KRX 집계**를 읽는다.

따라서 여기서 나온 숫자는 `FACT` 가 아니라 `EXPLORATORY` 다 — 2차 출처이기
때문이다. 이 프로젝트의 원문 우선 원칙(00-product-charter 6.1)상, 수급을 매매
근거로 승격하려면 KRX 원문으로 재확인해야 한다. CSV 헤더에 그 사실을 적어 둔다.

KRX 직접 경로가 열리면 `--source krx` 를 추가하고 이 파일의 파서만 바꾸면 된다.
저장 형식은 출처와 무관하게 같도록 잡아 뒀다.

사용:
    python3 scripts/flow_fetch.py market KOSPI --days 90
    python3 scripts/flow_fetch.py market KOSDAQ --days 90
    python3 scripts/flow_fetch.py stock 005930 --days 90
    python3 scripts/flow_fetch.py show KOSPI --days 20
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
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

# 네이버 지수 수급 표의 열 순서. 헤더가 2행으로 쪼개져 있어 위치로 읽는다.
MARKET_FIELDS = ("date", "individual", "foreign", "institution_total",
                 "fin_invest", "insurance", "trust", "bank", "other_fin",
                 "pension", "other_corp")
STOCK_FIELDS = ("date", "close", "change_pct", "volume",
                "institution_net", "foreign_net", "foreign_shares", "foreign_pct")

SOURCE_NOTE = ("네이버 금융(KRX 집계 재배포) · 2차 출처이므로 EXPLORATORY 등급이다. "
               "KRX 원문 재확인 전에는 매매 근거로 승격하지 않는다")


def fail(msg: str) -> None:
    print(f"오류: {msg}", file=sys.stderr)
    raise SystemExit(1)


def get(url: str) -> str:
    """네이버 금융은 EUC-KR 이다. UTF-8 로 읽으면 조용히 깨진 문자열이 나온다."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        fail(f"HTTP {e.code} — {url}")
    except urllib.error.URLError as e:
        fail(f"연결 실패 — {e.reason}")
    return raw.decode("euc-kr", errors="replace")


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
            break
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
