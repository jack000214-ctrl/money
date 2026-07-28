#!/usr/bin/env python3
"""투자 의사결정 패킷을 조립한다 — 모델에게 저장소를 던지지 않는다.

이 프로젝트의 목표는 모델을 대체하는 게 아니라 **좋은 모델을 전담 CIO 로
만드는 것**이다. 그러려면 질문마다 작은 패킷을 만들어 주고, 원문 전체는
필요할 때 도구로 열게 해야 한다.

패킷은 **결론을 담지 않는다.** 특히 계산 패킷은 "기대수익 −42.9%" 같은 절대값을
앞세우지 않는다. 그 숫자는 24개월 수렴 가정에 곱해져 있어서 계측 결과 판별력이
0이었다(`replay_decision.py`: 14/14 BLOCKED, 맞은 거부 0건). 대신 **순위와 순위
신뢰도**를 준다 — "관측된 N개 중 k위, 과거 순위상관 +1.00" 이 모델에게 훨씬
쓸모 있는 정보다. 절대값을 주면 믿거나 무시하는 이분법이 되고, 순위를 주면
모델이 자기 판단에 얼마나 반영할지 정할 수 있다.

각 패킷은 `_gaps` 를 갖는다. 그게 **조사 요구 루프의 입력**이다 — 모델이
"이 구멍을 채워라"라고 요청하면 프로그램이 채워서 다시 부른다.

사용:
    python3 scripts/build_packet.py 005930
    python3 scripts/build_packet.py 005930 --price 231000 --quantity 13
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from contracts import load_json  # noqa: E402
from decision_log import read as read_decisions  # noqa: E402

SCHEMA = "decision-packet/1.0.0"


def fail(msg: str) -> None:
    print(f"오류: {msg}", file=sys.stderr)
    raise SystemExit(1)


# ---------------------------------------------------------------------------
# 프로파일 읽기 — frontmatter 와 본문 섹션
# ---------------------------------------------------------------------------

def company_dir(asset_id: str) -> Path | None:
    base = ROOT / "knowledge" / "companies"
    if not base.exists():
        return None
    for path in base.iterdir():
        if path.is_dir() and (path.name == asset_id
                              or path.name.startswith(f"{asset_id}-")):
            return path
    return None


def parse_frontmatter(text: str) -> dict:
    """profile.md 의 YAML frontmatter 를 읽는다.

    PyYAML 없이 처리한다 — 이 프로젝트는 의존성을 늘리지 않는다.
    2단 들여쓰기와 스칼라만 지원하며, 그 이상은 스키마가 허용하지 않는다.
    """
    if not text.startswith("---"):
        return {}
    body = text.split("---", 2)[1]
    out: dict = {}
    current = None
    for line in body.splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        key, _, raw = line.strip().partition(":")
        value = raw.strip().strip('"').strip("'")
        if indent == 0:
            if value == "":
                current = key
                out[key] = {}
            else:
                current = None
                out[key] = _scalar(value)
        elif current:
            out[current][key] = _scalar(value)
    return out


def _scalar(value: str):
    if value in ("null", "~", ""):
        return None
    if value in ("true", "false"):
        return value == "true"
    try:
        return int(value.replace(",", ""))
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def sections(text: str) -> dict[str, str]:
    """`## 제목` 단위로 본문을 자른다."""
    parts = re.split(r"^## +", text, flags=re.M)[1:]
    out = {}
    for part in parts:
        title, _, rest = part.partition("\n")
        out[title.strip()] = rest.strip()
    return out


def bullets(block: str) -> list[str]:
    """번호·불릿 목록만 뽑는다. 표와 산문은 버린다."""
    out = []
    for line in block.splitlines():
        m = re.match(r"^\s*(?:[-*]|\d+\.)\s+(.*)$", line)
        if m and m.group(1).strip():
            out.append(re.sub(r"\s+", " ", m.group(1)).strip())
    return out


# ---------------------------------------------------------------------------
# 패킷들
# ---------------------------------------------------------------------------

def company_packet(asset_id: str) -> dict:
    path = company_dir(asset_id)
    if path is None or not (path / "profile.md").exists():
        return {"_gaps": [f"{asset_id} 프로파일이 없다. company-research 로 만든다"],
                "available": False}
    text = (path / "profile.md").read_text(encoding="utf-8")
    fm = parse_frontmatter(text)
    sec = sections(text)

    gaps = []
    thesis = fm.get("thesis") or {}
    if thesis.get("status") != "APPROVED":
        gaps.append(f"논지가 {thesis.get('status')} 다 — 사용자 승인 전이라 "
                    "매수 판정에 쓸 수 없다")
    valuation = fm.get("valuation") or {}
    if not valuation.get("verified"):
        gaps.append("밸류에이션이 검증된 계산 산출물에서 오지 않았다")
    freshness = fm.get("data_freshness") or {}
    today = dt.date.today()
    for label, key in (("뉴스", "news_scanned_at"), ("공시", "filings_verified_at"),
                       ("재무", "financials_verified_at")):
        when = freshness.get(key)
        if not when:
            gaps.append(f"{label} 확인 시점이 없다")
        else:
            age = (today - dt.date.fromisoformat(str(when))).days
            if age > 30:
                gaps.append(f"{label} 확인이 {age}일 전이다")

    # 무효화 조건은 관측 가능해야 한다 — 임계값 없는 문장은 조건이 아니다
    invalidation = next((bullets(v) for k, v in sec.items() if "무효화" in k), [])
    vague = [c for c in invalidation
             if not re.search(r"\d", c)]

    return {
        "available": True,
        "identity": {k: fm.get(k) for k in
                     ("company_id", "name", "ticker", "exchange", "sector", "industry")},
        "thesis_status": thesis.get("status"),
        "thesis_owner": thesis.get("owner"),
        "valuation": valuation,
        "evidence_level": fm.get("evidence_level"),
        "data_freshness": freshness,
        # 사업구조·무효화 조건은 모델이 매번 원문을 다시 파지 않도록 여기 둔다
        "business_structure": next((v for k, v in sec.items() if "사업구조" in k), None),
        "invalidation_conditions": invalidation,
        "invalidation_without_threshold": vague,
        "section_index": list(sec),
        "profile_path": str((path / "profile.md").relative_to(ROOT)),
        "_gaps": gaps,
    }


def unknowns_packet(asset_id: str) -> dict:
    """미확인 패킷 — 조사 요구 루프의 입력."""
    path = company_dir(asset_id)
    items = []
    if path and (path / "profile.md").exists():
        sec = sections((path / "profile.md").read_text(encoding="utf-8"))
        for title, block in sec.items():
            if "미해결" in title or "UNKNOWN" in title.upper():
                for line in bullets(block):
                    resolved = line.startswith("~~") or "해소" in line[:30]
                    items.append({"question": line, "resolved": resolved})
    open_items = [i for i in items if not i["resolved"]]
    return {
        "open": [i["question"] for i in open_items],
        "resolved": [i["question"] for i in items if i["resolved"]],
        "_gaps": ([] if open_items else
                  ["미확인 항목이 비어 있다 — 정말 없는지, 아직 안 적었는지 구분 필요"]),
        "_note": "이 목록이 조사 요구 루프의 입력이다. 모델이 여기서 골라 "
                 "'채워라'를 요청하면 프로그램이 수집해 패킷을 다시 만든다",
    }


def event_packet(asset_id: str, symbol: str | None, days: int) -> dict:
    """수집된 사건 파일이 있으면 그것을 쓴다. 없으면 시세만으로 급변일을 만든다.

    collect_events.py 가 공시까지 대조한 결과가 훨씬 쓸모 있으므로 그걸 우선한다.
    """
    collected = ROOT / "knowledge" / "events" / f"{asset_id}.json"
    if collected.exists():
        data = load_json(collected)
        age_h = (dt.datetime.now(dt.timezone.utc).timestamp()
                 - collected.stat().st_mtime) / 3600
        gaps = list(data.get("_gaps", []))
        if age_h > 24:
            gaps.append(f"사건 수집이 {age_h:.0f}시간 전이다 — collect_events.py 재실행")
        return {
            "source": "collect_events.py",
            "collected_at": data.get("collected_at"),
            "window": data.get("window"),
            "threshold": data.get("threshold"),
            # 상시 필드 — 종목 고유 원인을 찾기 전에 시장 요인을 먼저 본다
            "market_decomposition": data.get("market_decomposition"),
            "benchmark": data.get("benchmark"),
            "latest": data.get("latest"),
            # 급변일마다 후보 공시가 붙어 있다. 인과는 모델이 고른다.
            "moves": data.get("moves"),
            "unexplained_moves": [m["date"] for m in data.get("moves", [])
                                  if not m.get("candidate_filings")],
            "moved_with_market": [m["date"] for m in data.get("moves", [])
                                  if m.get("moved_with_market")],
            "_note": data.get("_note"),
            "_gaps": gaps,
        }
    return _event_from_prices(asset_id, symbol, days)


def _event_from_prices(asset_id: str, symbol: str | None, days: int) -> dict:
    """사건 패킷 — 최근 가격 급변과 그 후보 원인.

    **원인을 단정하지 않는다.** 가격이 크게 움직인 날과 그 무렵 공시를 나란히
    놓기만 한다. 인과는 모델이 판단할 일이고, 프로그램이 붙이면 그게 곧
    결론 유도다.
    """
    moves, gaps = [], []
    if not symbol:
        return {"_gaps": ["시세 심볼을 모른다"], "moves": []}
    path = ROOT / "knowledge" / "prices" / f"{symbol.replace('.', '_')}.csv"
    if not path.exists():
        return {"_gaps": [f"시세 파일이 없다: {path.name}"], "moves": []}

    with path.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(l for l in f if not l.startswith("#")))
    closes = [(r["date"], float(r["close"])) for r in rows]
    cutoff = (dt.date.fromisoformat(closes[-1][0]) - dt.timedelta(days=days)).isoformat()
    window = [c for c in closes if c[0] >= cutoff]
    for i in range(1, len(window)):
        change = (window[i][1] / window[i - 1][1] - 1) * 100
        if abs(change) >= 5.0:
            moves.append({"date": window[i][0],
                          "close": window[i][1],
                          "change_pct": round(change, 2)})

    gaps.append("공시·뉴스 자동 대조가 미구현이다 — 급변일에 어떤 공시가 있었는지 "
                "dart_fetch filings 로 사람이 붙이고 있다")
    return {
        "window_days": days,
        "period": f"{window[0][0]} ~ {window[-1][0]}" if window else None,
        "latest": {"date": closes[-1][0], "close": closes[-1][1]},
        "moves_over_5pct": moves,
        "_note": "원인은 붙이지 않았다. 급변일과 공시를 나란히 놓는 것까지가 "
                 "프로그램의 몫이고 인과는 모델이 판단한다",
        "_gaps": gaps,
    }


def rank_universe(calculations_dir: Path) -> list[dict]:
    """계산 산출물이 있는 종목을 안전마진 순으로 늘어놓는다."""
    rows = []
    for path in sorted(calculations_dir.glob("CALC-*.json")):
        calc = load_json(path)
        outputs = calc.get("outputs", {}).get("scenarios", {})
        price = calc.get("inputs", {}).get("price")
        if not price or not {"bear", "base", "bull"} <= set(outputs):
            continue
        # 확률은 분석에 있고 계산엔 없다. 순위 비교는 동일 가중으로 한다 —
        # 종목마다 다른 확률을 쓰면 순위가 확률 선택을 반영해버린다.
        values = [outputs[n]["value_per_share"] for n in ("bear", "base", "bull")]
        equal_weighted = sum(values) / 3
        rows.append({
            "instrument_id": calc["instrument_id"],
            "calculation_id": calc["calculation_id"],
            "price": price,
            "equal_weighted_value": round(equal_weighted, 4),
            "price_pct_of_value": round(price / equal_weighted * 100, 1),
        })
    rows.sort(key=lambda r: r["price_pct_of_value"])
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    return rows


def spearman(pairs: list[tuple[float, float]]) -> float | None:
    if len(pairs) < 3:
        return None

    def ranks(values):
        order = sorted(range(len(values)), key=lambda i: values[i])
        out = [0.0] * len(values)
        for rank, i in enumerate(order):
            out[i] = rank
        return out

    xs, ys = ranks([p[0] for p in pairs]), ranks([p[1] for p in pairs])
    n = len(pairs)
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = (sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys)) ** 0.5
    return round(num / den, 3) if den else None


def calculation_packet(asset_id: str, score_months: int) -> dict:
    """계산 패킷 — 절대 기대수익이 아니라 **순위와 순위 신뢰도**."""
    calc_dir = ROOT / "knowledge" / "calculations"
    universe = rank_universe(calc_dir)
    mine = next((r for r in universe if r["instrument_id"] == asset_id), None)

    # 순위 신뢰도 — 과거에 이 자로 낸 순서가 실제 수익 순서와 맞았는지
    reliability = {}
    for path in sorted((ROOT / "knowledge" / "decisions").glob("*.jsonl")):
        entries = read_decisions(path.stem)
        pairs = []
        for e in entries:
            outcome = (e.get("outcome") or {}).get(str(score_months))
            if outcome and outcome.get("complete") \
                    and isinstance(e.get("expected_return_pct"), (int, float)):
                pairs.append((e["expected_return_pct"], outcome["annualized_pct"]))
        rho = spearman(pairs)
        if rho is not None:
            reliability[path.stem] = {"spearman": rho, "n": len(pairs),
                                      "horizon_months": score_months}

    gaps = []
    if mine is None:
        gaps.append(f"{asset_id} 계산 산출물이 없다 — reverse_dcf scenarios --save")
    if len(universe) < 5:
        gaps.append(f"순위 모집단이 {len(universe)}개뿐이다. 순위의 의미가 약하다 — "
                    "후보 종목을 늘려야 한다")
    if not reliability:
        gaps.append(f"{score_months}개월 채점이 끝난 판정이 없어 순위 신뢰도를 "
                    "낼 수 없다")

    return {
        "measure": "안전마진 — 동일가중 내재가치 대비 가격. 수렴 기간 가정을 "
                   "쓰지 않으므로 계측되지 않은 다이얼이 없다",
        "this_asset": mine,
        "rank_of": len(universe),
        "universe": universe,
        "rank_reliability": reliability,
        "_why_not_expected_return": (
            "확률가중 기대수익(연율)은 24개월 수렴 가정에 곱해져 있어 계측 결과 "
            "판별력이 0이었다 — 과거 재현 14건 전부 BLOCKED, 맞은 거부 0건. "
            "주가가 내재가치와 같은 수준(내재성장 4.9%)일 때조차 -14%가 나온다. "
            "그래서 절대값을 앞세우지 않고 순위를 준다"),
        "_gaps": gaps,
    }


def memory_packet(asset_id: str, score_months: list[int]) -> dict:
    """기억 패킷 — 지난 판단과 그 뒤 실제 결과."""
    entries = read_decisions(asset_id)
    scored, pending = [], []
    for e in entries:
        row = {
            "decided_at": e["decided_at"][:10],
            "origin": e.get("origin"),
            "price": e.get("price"),
            "verdict": e.get("verdict"),
            "expected_return_pct": e.get("expected_return_pct"),
            "outcome": {},
        }
        any_complete = False
        for m in score_months:
            o = (e.get("outcome") or {}).get(str(m))
            if o and o.get("complete"):
                row["outcome"][f"{m}m"] = o["annualized_pct"]
                any_complete = True
        (scored if any_complete else pending).append(row)

    verdicts = [e.get("verdict") for e in entries]
    blocked = sum(1 for v in verdicts if v == "BLOCKED")
    lessons = []
    for m in score_months:
        pairs = [(e["expected_return_pct"], (e.get("outcome") or {}).get(str(m), {}).get("annualized_pct"))
                 for e in entries
                 if isinstance(e.get("expected_return_pct"), (int, float))
                 and (e.get("outcome") or {}).get(str(m), {}).get("complete")]
        pairs = [(a, b) for a, b in pairs if b is not None]
        if not pairs:
            continue
        missed = [(a, b) for a, b in pairs if b > 12.0]
        lessons.append({
            "horizon_months": m,
            "scored": len(pairs),
            "missed_opportunities": len(missed),
            "spearman": spearman(pairs),
            "note": (f"{len(pairs)}건 중 {len(missed)}건은 거부했는데 이후 연율이 "
                     "요구수익률을 넘겼다"),
        })

    return {
        "total_decisions": len(entries),
        "blocked": blocked,
        "scored": scored,
        "pending": pending,
        "lessons": lessons,
        "_gaps": ([] if entries else
                  [f"{asset_id} 판정 기록이 없다 — decision_log.py record"]),
        "_note": "origin=replay 는 과거 시점을 재현한 판정이다. 실제로 낸 판정과 "
                 "같은 자로 비교하려고 같은 파일에 둔다",
    }


def portfolio_packet(asset_id: str) -> dict:
    """포트폴리오 패킷 — 보유 비중과 신규 매수 여력."""
    path = ROOT / "knowledge" / "portfolio" / "holdings.csv"
    if not path.exists():
        return {"_gaps": ["holdings.csv 가 없다"]}
    rows, meta = [], {}
    with path.open(encoding="utf-8") as f:
        for line in f:
            if line.startswith("#"):
                m = re.search(r"기준일\s*(\d{4}-\d{2}-\d{2})", line)
                if m:
                    meta["as_of"] = m.group(1)
                continue
            rows.append(line)
    holdings = list(csv.DictReader(rows))
    total = sum(float(h["value_krw"]) for h in holdings if h.get("value_krw"))
    cash = sum(float(h["value_krw"]) for h in holdings
               if h.get("risk_class") == "CASH")
    by_sector: dict[str, float] = {}
    for h in holdings:
        if h.get("risk_class") == "CASH":
            continue
        by_sector[h.get("sector") or "미분류"] = (
            by_sector.get(h.get("sector") or "미분류", 0.0) + float(h["value_krw"]))

    held = next((h for h in holdings if h["asset_id"] == asset_id), None)
    return {
        "as_of": meta.get("as_of"),
        "total_assets_krw": total,
        "cash_krw": cash,
        "cash_pct": round(cash / total * 100, 2) if total else None,
        "positions": [
            {"asset_id": h["asset_id"], "name": h["name"],
             "risk_class": h["risk_class"], "sector": h.get("sector") or None,
             "value_krw": float(h["value_krw"]),
             "pct_total_assets": round(float(h["value_krw"]) / total * 100, 2)}
            for h in holdings if h.get("risk_class") != "CASH"
        ],
        "sector_pct": {k: round(v / total * 100, 2) for k, v in sorted(
            by_sector.items(), key=lambda kv: -kv[1])},
        "this_asset_held": bool(held),
        "_gaps": ["ETF 룩스루 미반영 — 섹터 비중은 하한이다"],
    }


def main() -> int:
    p = argparse.ArgumentParser(description="투자 의사결정 패킷 조립")
    p.add_argument("asset_id")
    p.add_argument("--symbol", help="시세 심볼 (005930.KS). 생략하면 추정")
    p.add_argument("--event-days", type=int, default=60)
    p.add_argument("--score-months", nargs="+", type=int, default=[3, 12, 24])
    p.add_argument("--rank-horizon", type=int, default=12,
                   help="순위 신뢰도를 계산할 채점 지평")
    p.add_argument("--out")
    a = p.parse_args()

    symbol = a.symbol
    if not symbol:
        fm = {}
        path = company_dir(a.asset_id)
        if path and (path / "profile.md").exists():
            fm = parse_frontmatter((path / "profile.md").read_text(encoding="utf-8"))
        exchange = (fm.get("exchange") or "").upper()
        if exchange == "KOSPI":
            symbol = f"{a.asset_id}.KS"
        elif exchange == "KOSDAQ":
            symbol = f"{a.asset_id}.KQ"
        else:
            symbol = a.asset_id

    packet = {
        "schema_version": SCHEMA,
        "asset_id": a.asset_id,
        "built_at": dt.datetime.now(
            dt.timezone(dt.timedelta(hours=9))).isoformat(timespec="seconds"),
        "_contract": {
            "모델에게 강제하는 산출물": [
                "가장 강한 매수 논지",
                "가장 강한 반대 논지",
                "시장이 현재 가격에 반영한 기대",
                "어떤 정보가 결론을 바꾸는지",
                "독립적인 최종 판단",
                "적정 비중과 실행 순서",
            ],
            "금지": [
                "이 패킷은 결론을 담지 않는다. 역할을 '반박'으로 지정하지 않는다 — "
                "그러면 더 좋은 모델이 더 정교한 반대 논리만 생산한다",
                "원문 전체를 여기 넣지 않는다. 필요하면 도구로 연다",
            ],
        },
        "packets": {
            "company": company_packet(a.asset_id),
            "event": event_packet(a.asset_id, symbol, a.event_days),
            "unknowns": unknowns_packet(a.asset_id),
            "memory": memory_packet(a.asset_id, a.score_months),
            "calculation": calculation_packet(a.asset_id, a.rank_horizon),
            "portfolio": portfolio_packet(a.asset_id),
        },
    }
    packet["_missing_packets"] = {
        "user": "목표·기간·위험성향·세금·월 투자가능액 — 사용자에게 물어야 한다",
        "market_expectation": "컨센서스·추정치 변화·동종사 밸류에이션 — "
                              "새 데이터 소스가 필요하다",
    }
    all_gaps = []
    for name, body in packet["packets"].items():
        for gap in body.get("_gaps", []):
            all_gaps.append({"packet": name, "gap": gap})
    packet["_research_requests"] = all_gaps

    text = json.dumps(packet, ensure_ascii=False, indent=2) + "\n"
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
        print(f"작성: {a.out}")
    else:
        print(text)
    print(f"패킷 6개 · 구멍 {len(all_gaps)}건", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
