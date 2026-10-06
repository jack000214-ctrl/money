#!/usr/bin/env python3
"""증권사 컨센서스와 목표주가 개정 이력을 시계열로 수집한다.

목표주가 자체는 근거가 약하다. 매도 리포트가 사실상 나오지 않아 목표가는 거의
항상 현재가 위에 있고, 주가를 따라 개정된다. 손으로 몇 개를 골라 평균 내면
시점이 다른 값이 섞여 더 나빠진다.

그래서 여기서 모으는 것은 세 가지다.

1. **컨센서스 EPS** — 애널리스트의 결론(목표가)이 아니라 입력(이익 추정)이다.
   멀티플은 모델이 직접 정한다.
2. **분산** — 추정기관수와 목표가 최소·최대. 15곳이 420,000~540,000으로 갈리면
   평균 494,000은 합의가 아니라 평균일 뿐이다.
3. **개정 이력** — 상향·하향 전환은 관측 가능한 사건이다. 평균값 하나로는
   교보가 650,000에서 530,000으로 내린 것을 볼 수 없다.

출처는 FnGuide 가 네이버에 제공하는 기업분석 페이지이므로 등급은 `EXPLORATORY`
다. 컨센서스는 출처 정의상 '최근 3개월간 증권사 발표 추정치의 평균'이다.

사용:
    python3.13 -m collectors.consensus 214450
    python3.13 -m collectors.consensus 005930 --show
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import html as html_module
import math
import re
import statistics
import sys
import urllib.error
import urllib.request
from pathlib import Path

from cio.display import ABSENT, cell, pad

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "knowledge" / "consensus"
RAW_DIR = ROOT / "data" / "raw" / "naver"

SCHEMA = "consensus-packet/1.0.0"
URL = "https://navercomp.wisereport.co.kr/v2/company/c1010001.aspx?cmp_cd={code}"
# 연도별 추정실적은 별도 프레임이다. 목표주가보다 이쪽이 본체다 — 매출·영업이익·
# EPS 추정과 함께 실적 연도의 PER 이 나오므로, 그 종목 자신의 멀티플 밴드를
# 남의 목표가 없이 만들 수 있다.
ESTIMATE_URL = (
    "https://navercomp.wisereport.co.kr/v2/company/cF1002.aspx"
    "?cmp_cd={code}&finGubun=IFRSL&frq=0&sec_cd=&arrSaveGubun="
)
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
SOURCE = "FnGuide via navercomp.wisereport.co.kr"
SOURCE_NOTE = (
    "FnGuide 가 네이버에 제공하는 기업분석 페이지 · 2차 출처이므로 EXPLORATORY "
    "등급이다. 컨센서스는 최근 3개월간 증권사 발표 추정치의 평균이다"
)
# 개정 이력을 셀 기간. 출처가 말하는 3개월 컨센서스와 맞춘다.
REVISION_WINDOW_DAYS = 90
# '지금의 견해'를 보는 좁은 창. 실적 시즌 한 번이 지나갈 정도의 길이다.
RECENT_WINDOW_DAYS = 30


def fail(message: str) -> None:
    print(f"오류: {message}", file=sys.stderr)
    raise SystemExit(1)


def fetch(code: str, url: str = URL) -> str:
    request = urllib.request.Request(
        url.format(code=code),
        headers={
            "User-Agent": UA,
            "Referer": URL.format(code=code),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        fail(f"HTTP {exc.code} — {code}")
    except urllib.error.URLError as exc:
        fail(f"연결 실패 — {exc.reason}")
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return body.decode("euc-kr", "replace")


def cells(row: str) -> list[str]:
    return [
        text
        for text in (
            re.sub(r"\s+", " ", html_module.unescape(re.sub(r"<[^>]+>", "", cell))).strip()
            for cell in re.findall(r"<t[hd].*?</t[hd]>", row, re.S)
        )
    ]


def table_rows(html: str, table_id: str) -> list[list[str]]:
    start = html.find(f'id="{table_id}"')
    if start < 0:
        return []
    segment = html[start : html.find("</table>", start)]
    return [
        parsed
        for row in re.findall(r"<tr.*?</tr>", segment, re.S)
        if (parsed := cells(row))
    ]


def number(text: str | None) -> float | None:
    if not text:
        return None
    cleaned = text.replace(",", "").replace("%", "").strip()
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def parse_date(text: str) -> str | None:
    """'26/07/28' 을 ISO 날짜로. 2000년대만 다룬다."""
    match = re.fullmatch(r"(\d{2})/(\d{2})/(\d{2})", text.strip())
    if not match:
        return None
    year, month, day = match.groups()
    return f"20{year}-{month}-{day}"


def parse_summary(html: str) -> dict:
    """투자의견·목표주가·EPS·PER·추정기관수. 헤더 행 다음이 값 행이다."""
    rows = table_rows(html, "cTB15")
    header = next(
        (row for row in rows if "추정기관수" in row),
        None,
    )
    if header is None:
        fail("투자의견 컨센서스 표(cTB15)를 찾지 못했다. 페이지 구조가 바뀌었다")
    values = rows[rows.index(header) + 1] if rows.index(header) + 1 < len(rows) else []
    # 값 행은 [투자의견, 목표주가, EPS, PER, 추정기관수] 순서다. 앞에 등급
    # 그림용 숫자가 한 번 더 붙는 경우가 있어 뒤에서 다섯 개를 취한다.
    tail = values[-5:]
    if len(tail) < 5:
        fail("컨센서스 요약 값을 읽지 못했다")
    rating, target, eps, per, count = (number(value) for value in tail)
    return {
        "rating": rating,
        "target_price": target,
        "eps": eps,
        "per": per,
        "institution_count": int(count) if count is not None else None,
    }


def parse_brokers(html: str) -> list[dict]:
    """제공처별 목표가와 직전 목표가. 직전값이 없는 행도 있다."""
    out = []
    for row in table_rows(html, "cTB24"):
        if row[0] == "제공처" or len(row) < 3:
            continue
        date = parse_date(row[1])
        if date is None:
            continue
        target = number(row[2])
        prior = number(row[3]) if len(row) >= 5 else None
        change = number(row[4]) if len(row) >= 5 else None
        out.append({
            "broker": row[0],
            "date": date,
            "target_price": target,
            "prior_target_price": prior,
            "change_pct": change,
            "rating": row[-2] if len(row) >= 7 else row[-1],
        })
    return out


def _dispersion(brokers: list[dict], as_of: str, days: int) -> dict:
    cutoff = (dt.date.fromisoformat(as_of) - dt.timedelta(days=days)).isoformat()
    rows = [
        b for b in brokers
        if b["date"] >= cutoff and b["target_price"]
    ]
    targets = [b["target_price"] for b in rows]
    return {
        "window_days": days,
        "count": len(targets),
        "min": min(targets) if targets else None,
        "max": max(targets) if targets else None,
        "median": statistics.median(targets) if targets else None,
        "mean": round(statistics.fmean(targets), 0) if targets else None,
        "spread_pct": round((max(targets) / min(targets) - 1) * 100, 2)
        if len(targets) >= 2 and min(targets) > 0
        else None,
        "oldest": min((b["date"] for b in rows), default=None),
        "newest": max((b["date"] for b in rows), default=None),
    }


ESTIMATE_FIELDS = (
    ("revenue_100m", 0),
    ("revenue_yoy_pct", 1),
    ("operating_income_100m", 2),
    ("net_income_100m", 3),
    ("eps", 4),
    ("per", 5),
    ("pbr", 6),
    ("roe_pct", 7),
    ("ev_ebitda", 8),
    ("net_debt_ratio_pct", 9),
)


def parse_estimates(html: str) -> dict:
    """연도별 실적·추정 표(cTB25). (A)는 확정, (E)는 컨센서스다."""
    years = []
    for row in table_rows(html, "cTB25"):
        match = re.fullmatch(r"(\d{4})\.(\d{2})\((A|E)\)", row[0])
        if not match:
            continue
        values = row[1:]
        year = {
            "period": f"{match.group(1)}-{match.group(2)}",
            "fiscal_year": int(match.group(1)),
            "actual": match.group(3) == "A",
        }
        for name, index in ESTIMATE_FIELDS:
            year[name] = number(values[index]) if index < len(values) else None
        years.append(year)
    # 확정 연도의 PER 범위. 이 종목이 실제로 어떤 멀티플에 거래돼 왔는가 —
    # 남의 목표주가 없이 만들 수 있는 유일한 밸류에이션 앵커다.
    actual_pers = [
        y["per"] for y in years if y["actual"] and y["per"] and y["per"] > 0
    ]
    return {
        "annual": years,
        "per_band_actual": {
            "years": len(actual_pers),
            "min": min(actual_pers) if actual_pers else None,
            "max": max(actual_pers) if actual_pers else None,
            "median": statistics.median(actual_pers) if actual_pers else None,
            "_note": (
                "확정 실적 연도의 PER 범위다. 추정 연도 PER 은 주가가 이미 "
                "반영된 값이라 밴드에 넣지 않는다"
            ),
        },
        "_note": (
            "금액 단위는 억원이다. (A)는 확정, (E)는 컨센서스 추정이다. "
            "PER 은 출처가 전 영업일 수정주가로 계산한 값이므로 오늘 주가와 다르다"
        ),
    }


def summarize(brokers: list[dict], as_of: str) -> tuple[dict, dict]:
    """분산과 개정 방향. 평균 하나로는 볼 수 없는 것들이다.

    분산은 두 창으로 낸다. 넓은 창은 출처가 말하는 컨센서스와 같은 범위이고,
    좁은 창은 지금의 견해다. 둘이 벌어져 있으면 평균값에 오래된 목표가가 섞여
    있다는 뜻이다 — 주가가 저점이던 시절에 잡은 목표가와 어제 잡은 목표가는
    같은 값이 아니다.
    """
    dispersion = {
        "consensus_window": _dispersion(brokers, as_of, REVISION_WINDOW_DAYS),
        "recent": _dispersion(brokers, as_of, RECENT_WINDOW_DAYS),
        "_note": (
            "목표가가 넓게 갈릴수록 평균은 합의가 아니라 평균일 뿐이다. 두 창의 "
            "최대·최소가 다르면 오래된 목표가가 평균을 끌고 있다는 신호이며, "
            "그 목표가는 지금과 다른 주가 수준에서 잡힌 값이다"
        ),
    }
    cutoff = (
        dt.date.fromisoformat(as_of) - dt.timedelta(days=REVISION_WINDOW_DAYS)
    ).isoformat()
    recent = [
        b for b in brokers
        if b["date"] >= cutoff and b["change_pct"] is not None
    ]
    raised = [b for b in recent if b["change_pct"] > 0]
    lowered = [b for b in recent if b["change_pct"] < 0]
    revisions = {
        "window_days": REVISION_WINDOW_DAYS,
        "observed": len(recent),
        "raised": len(raised),
        "lowered": len(lowered),
        "unchanged": len(recent) - len(raised) - len(lowered),
        "largest_raise": max(
            (b for b in raised), key=lambda b: b["change_pct"], default=None
        ),
        "largest_cut": min(
            (b for b in lowered), key=lambda b: b["change_pct"], default=None
        ),
        "_note": (
            "상향·하향 전환은 관측 가능한 사건이다. 목표가 수준보다 방향이 "
            "정보를 담는다"
        ),
    }
    return dispersion, revisions


def build(code: str, html: str, estimate_html: str = "", *, collected_at=None) -> dict:
    collected_at = collected_at or dt.datetime.now(dt.timezone(dt.timedelta(hours=9))).isoformat(timespec="seconds")
    components = {}
    summary = dict.fromkeys(("rating", "target_price", "eps", "per", "institution_count"))
    # Independent components: absent recommendations must not discard earnings.
    if 'id="cTB15"' not in html:
        components["summary"] = {"state": "PARSE_ERROR", "reason": "요약표를 찾지 못했다"}
    else:
        try:
            summary = parse_summary(html)
            components["summary"] = {"state": "AVAILABLE" if all(v is not None for v in summary.values()) else "PARTIAL",
                                     "reason": "비어 있는 칸은 null로 보존"}
        except SystemExit:
            components["summary"] = {"state": "PARSE_ERROR", "reason": "요약표 열을 해석하지 못했다"}
    brokers = parse_brokers(html) if html else []
    components["brokers"] = {"state": "AVAILABLE" if brokers else "NO_DATA" if "제시된 의견이 없습니다" in html else "PARSE_ERROR",
                             "reason": "제공처별 의견 상태; 실적 추정 유무와 별개"}
    estimates = parse_estimates(estimate_html) if estimate_html else {"annual": []}
    estimate_state = "AVAILABLE" if estimates["annual"] else "NO_DATA" if any(t in estimate_html for t in ("데이터가 없습니다", "자료가 없습니다", "추정실적이 없습니다")) else "PARSE_ERROR"
    components["estimates"] = {"state": estimate_state, "reason": "연도별 실적·추정 표의 관측 상태"}
    as_of = max((b["date"] for b in brokers), default=None)
    # A retrieval date is not the publication date of an earnings estimate.
    dispersion, revisions = summarize(brokers, as_of or collected_at[:10])
    if components["brokers"]["state"] == "PARSE_ERROR":
        for window in dispersion.values():
            if isinstance(window, dict):
                window["count"] = None
        for field in ("observed", "raised", "lowered", "unchanged"):
            revisions[field] = None
    return {
        "schema_version": SCHEMA,
        "asset_id": code,
        "collected_at": collected_at,
        "as_of": as_of,
        "as_of_basis": "LATEST_BROKER_REPORT" if as_of else "UNKNOWN",
        "estimates_as_of": None,
        "components": components,
        "available": estimate_state == "AVAILABLE",
        "state": "AVAILABLE" if all(c["state"] == "AVAILABLE" for c in components.values()) else "PARTIAL" if estimate_state == "AVAILABLE" else estimate_state,
        "source": SOURCE,
        "source_grade": "EXPLORATORY",
        "summary": summary,
        "estimates": estimates,
        "dispersion": dispersion,
        "revisions": revisions,
        "brokers": brokers,
        "_warning": (
            "목표주가는 애널리스트의 결론이지 입력이 아니다. 매도 리포트가 "
            "사실상 나오지 않아 거의 항상 현재가 위에 있고 주가를 따라 "
            "개정되므로, '목표가가 현재가보다 높다'는 사실 자체는 정보가 아니다. "
            "밸류에이션 근거로는 eps 를 쓰고 멀티플은 직접 정한다"
        ),
        "_source_note": SOURCE_NOTE,
    }


def merge_history(path: Path, packet: dict) -> dict:
    """기준일이 다른 스냅샷만 시계열로 쌓는다. 같은 날 여러 번 돌려도 안 늘어난다."""
    history = []
    if path.exists():
        try:
            history = json.loads(path.read_text(encoding="utf-8")).get("history") or []
        except (OSError, json.JSONDecodeError):
            history = []
    if packet.get("as_of") is None:
        packet["history"] = history
        return packet
    point = {
        "as_of": packet["as_of"],
        "collected_at": packet["collected_at"],
        **{
            key: packet["summary"][key]
            for key in ("rating", "target_price", "eps", "per", "institution_count")
        },
    }
    history = [row for row in history if row.get("as_of") != packet["as_of"]]
    history.append(point)
    history.sort(key=lambda row: row["as_of"])
    packet["history"] = history
    return packet


# 연도별 표의 열 — 이름, 칸 너비, 서식, 단위, packet 의 키.
# 폭은 조 단위 회사 기준이다. SK하이닉스 2027년 추정 매출이 5,017,411(억원)
# 이라 9자리를 쓴다. 좁게 잡으면 값이 넘쳐 옆 칸과 붙어 읽을 수 없다.
ESTIMATE_COLUMNS = (
    ("매출", 11, ",.0f", "", "revenue_100m"),
    ("YoY", 8, ".1f", "%", "revenue_yoy_pct"),
    ("영업익", 11, ",.0f", "", "operating_income_100m"),
    ("EPS", 10, ",.0f", "", "eps"),
    ("PER", 7, ".1f", "", "per"),
    ("ROE", 7, ".1f", "%", "roe_pct"),
)
PERIOD_COLUMN_WIDTH = 11


def show(packet: dict) -> None:
    summary = packet["summary"]
    dispersion = packet["dispersion"]
    revisions = packet["revisions"]
    print(f"{packet['asset_id']} 컨센서스 — 보고일 기준 {packet['as_of'] or '미확인'}")
    if packet.get("estimates_as_of") is None:
        print("  실적 전망의 개정일은 확인되지 않았다. 수집일을 전망일로 대체하지 않는다")
    print(f"  투자의견 {summary['rating'] if summary['rating'] is not None else ABSENT}"
          f" · 추정기관 "
          f"{summary['institution_count'] if summary['institution_count'] is not None else ABSENT}곳")
    print(f"  목표주가 컨센서스 {cell(summary['target_price'], 0, ',.0f')}원 "
          f"(출처 평균, 최근 3개월)")
    for label, key in (("전체 3개월", "consensus_window"), ("최근 30일", "recent")):
        window = dispersion[key]
        if window["count"] is None:
            print(f"    {label:<9}  확인 불가 (자료 부재로 판정하지 않음)")
            continue
        if not window["count"]:
            # 줄을 지우면 "이 화면은 원래 한 줄"로 읽힌다. 최근 30일에 추정이
            # 0곳이라는 사실 자체가 신호다 — 애널리스트가 갱신을 멈췄다는 뜻이라
            # 남아 있는 평균이 오래된 목표가라는 경고가 된다.
            print(f"    {label:<9}  0곳 (해당 기간 추정 없음)")
            continue
        print(f"    {label:<9} {window['count']:>2}곳 "
              f"{cell(window['min'], 9, ',.0f')} ~ {cell(window['max'], 9, ',.0f')} "
              f"(스프레드 {window['spread_pct']}%) "
              f"{window['oldest']}~{window['newest']}")
    wide, near = dispersion["consensus_window"], dispersion["recent"]
    if wide["max"] and near["max"] and wide["max"] > near["max"]:
        print(f"    ※ 전체 창의 최고 {wide['max']:,.0f}원은 최근 30일 밖 값이다 — "
              "평균이 옛 목표가에 끌리고 있다")
    estimates = packet.get("estimates") or {}
    if estimates.get("annual"):
        print("\n  연도별 실적·추정 (억원)")
        print("    " + pad("연도", PERIOD_COLUMN_WIDTH)
              + "".join(pad(name, width) for name, width, *_ in ESTIMATE_COLUMNS))
        for year in estimates["annual"]:
            mark = "A" if year["actual"] else "E"
            print("    "
                  + f"{year['period']}({mark})".ljust(PERIOD_COLUMN_WIDTH)
                  + "".join(
                      cell(year.get(key), width, spec, unit)
                      for _name, width, spec, unit, key in ESTIMATE_COLUMNS
                  ))
        band = estimates["per_band_actual"]
        if band["min"]:
            print(f"    확정 연도 PER 밴드 {band['min']:.1f} ~ {band['max']:.1f}배 "
                  f"(중앙 {band['median']:.1f}) — 멀티플 판단의 자체 앵커다")
    print(f"  컨센서스 EPS {cell(summary['eps'], 0, ',.0f')}원 · "
          f"PER {summary['per'] if summary['per'] is not None else ABSENT}배")
    print(f"\n  최근 {revisions['window_days']}일 개정 {cell(revisions['observed'], 0)}건 — "
          f"상향 {cell(revisions['raised'], 0)} / 하향 {cell(revisions['lowered'], 0)} / "
          f"유지 {cell(revisions['unchanged'], 0)}")
    for label, row in (("최대 상향", revisions["largest_raise"]),
                       ("최대 하향", revisions["largest_cut"])):
        if row:
            print(f"    {label}: {row['broker']} "
                  f"{cell(row['prior_target_price'], 0, ',.0f')} → "
                  f"{cell(row['target_price'], 0, ',.0f')} "
                  f"({cell(row['change_pct'], 0, '+.2f')}%)")
    if len(packet.get("history", [])) > 1:
        print("\n  기준일별 컨센서스 추이")
        for row in packet["history"][-6:]:
            print(f"    {row['as_of']}  목표 {cell(row['target_price'], 10, ',.0f')}  "
                  f"EPS {cell(row['eps'], 8, ',.0f')}  "
                  f"기관 {cell(row['institution_count'], 3)}")
    print(f"\n※ {SOURCE_NOTE}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="증권사 컨센서스·목표주가 개정 이력 (EXPLORATORY 등급)"
    )
    parser.add_argument("asset_id")
    parser.add_argument("--show", action="store_true", help="저장본만 출력")
    args = parser.parse_args()
    from cio.assets import get_asset_or_declared_peer
    # Resolve the ID before constructing paths. Peers have no registry entry of
    # their own, only a declaration under the asset they are compared with.
    get_asset_or_declared_peer(args.asset_id)

    path = OUT_DIR / f"{args.asset_id}.json"
    if args.show:
        if not path.exists():
            fail(f"{path.relative_to(ROOT)} 가 없다. 먼저 수집한다")
        show(json.loads(path.read_text(encoding="utf-8")))
        return 0

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    fetched, failures, originals = {}, {}, {}
    for component, url in (("summary", URL), ("estimates", ESTIMATE_URL)):
        try:
            fetched[component] = fetch(args.asset_id, url)
            prefix = "consensus" if component == "summary" else "estimates"
            raw_path = RAW_DIR / f"{prefix}_{args.asset_id}_{stamp}.html"
            raw_path.write_text(fetched[component], encoding="utf-8")
            originals[component] = str(raw_path.relative_to(ROOT))
        except SystemExit:
            failures[component] = "NETWORK_ERROR"
    packet = build(args.asset_id, fetched.get("summary", ""), fetched.get("estimates", ""))
    for component, state in failures.items():
        packet["components"][component] = {"state": state, "reason": "HTTP 또는 연결 실패. 로그 확인"}
    if "estimates" in failures:
        packet["state"] = "NETWORK_ERROR"
    packet["raw_sources"] = originals
    attempt = {"asset_id": args.asset_id, "attempted_at": packet["collected_at"],
               "state": packet["state"], "components": packet["components"], "raw_sources": originals}
    attempt_path = ROOT / "knowledge" / "collection_status" / f"consensus-{args.asset_id}.json"
    attempt_path.parent.mkdir(parents=True, exist_ok=True)
    attempt_path.write_text(json.dumps(attempt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if not packet["available"] and packet["state"] != "NO_DATA":
        print("오류: 실적 추정을 확보하지 못했다. 기존 성공 자료는 유지하며 실패 상태를 별도 보존했다", file=sys.stderr)
        return 1
    packet = merge_history(path, packet)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(packet, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    show(packet)
    print(f"저장: {path.relative_to(ROOT)}")
    print(f"구성요소 상태: {packet['components']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
