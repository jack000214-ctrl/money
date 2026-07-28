#!/usr/bin/env python3
"""모델의 조사 요구를 받아 채우고 패킷을 다시 만든다 — 조사 요구 루프.

지금까지는 컨텍스트가 부족하면 `RESEARCH_REQUIRED` 로 끝났다. 그건 종료가
아니라 **요청**이어야 한다 — "DRAM 가격·수급·CXMT 증설 계획을 더 가져와라"라고
모델이 말하면 프로그램이 채워서 다시 불러야 한다.

이 스크립트가 그 왕복을 담당한다.

    build_packet  →  _research_requests  →  research_loop fill  →  build_packet
                          ↑                                            ↓
                          └──────── 모델이 남은 구멍을 보고 재요청 ────────┘

**채울 수 있는 것과 없는 것을 구분한다.** 프로그램이 못 채우는 요청(사용자
승인, 없는 데이터 소스)은 `blocked` 로 돌려주고 왜 못 채우는지 적는다. 조용히
빈칸으로 두면 모델이 그 항목을 없는 것으로 취급한다.

사용:
    python3 scripts/research_loop.py plan 005930
    python3 scripts/research_loop.py fill 005930
    python3 scripts/research_loop.py fill 005930 --request "재고자산 추이"
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 조사 요구를 어떤 명령으로 채우는가. 키워드는 _gaps·미확인 문장에서 찾는다.
#
# 여기 없는 요청은 blocked 로 돌려준다 — 사람이 붙여야 하는 것과 데이터 소스가
# 아예 없는 것을 구분해서 알려주는 게 조용히 넘어가는 것보다 낫다.
HANDLERS = [
    {
        "id": "events",
        "match": ("급변", "공시로 설명되지 않는", "사건 수집", "당일", "하락 원인"),
        "command": lambda a: ["python3", "scripts/collect_events.py", a["asset_id"],
                              "--symbol", a["symbol"], "--days", "90"],
        "fills": "사건 패킷 — 급변일과 후보 공시 대조",
    },
    {
        "id": "prices",
        "match": ("시세", "가격 데이터", "price"),
        "command": lambda a: ["python3", "scripts/price_fetch.py", "history",
                              a["symbol"], "--range", "10y"],
        "fills": "시세 이력",
    },
    {
        "id": "fundamentals",
        # "재무" 처럼 넓게 잡으면 이 수집기가 채우지 못하는 요청까지 삼킨다.
        # 재고자산은 이 스냅샷에 없다 — 넣으면 fundamentals_id 지문이 바뀌어
        # 이미 저장된 계산이 전부 어긋나므로 별도 수집기가 필요하다.
        "match": ("FCF", "현금흐름", "밸류에이션 입력", "밸류에이션 스냅샷",
                  "순부채", "희석주식수"),
        "command": lambda a: ["python3", "scripts/dart_fetch.py", "fundamentals",
                              a["asset_id"], "--year", a["year"]],
        "fills": "밸류에이션 입력 스냅샷 (재무상태·현금흐름 포함)",
        "market": "KR",
    },
    {
        "id": "quarters",
        "match": ("실적", "분기", "영업이익률", "매출"),
        "command": lambda a: ["python3", "scripts/dart_fetch.py", "quarters",
                              a["asset_id"], "--years", a["year"],
                              str(int(a["year"]) - 1), str(int(a["year"]) - 2)],
        "fills": "분기별 실적 시리즈",
        "market": "KR",
    },
]

# 로컬 수집기가 없는 요청. **"없다"가 아니라 "여긴 없으니 네가 가져와라"다.**
#
# 처음엔 이걸 전부 blocked 로 돌려줬는데 잘못된 설계였다. blocked 는 모델에게
# "이 항목은 존재하지 않는다"로 읽히고, 그러면 모델이 포기한다. 모델이 메인이면
# 정상 반응은 "이 프로젝트에 없으니 내가 웹으로 찾아온다"여야 한다.
#
# 그래서 로컬에 없는 요청은 **조사 방법과 근거 등급을 함께** 돌려준다.
# 등급을 미리 알려주는 이유: 웹에서 온 값은 FACT 가 될 수 없고 EXPLORATORY 다.
# 모델이 그걸 모르면 뉴스 숫자를 공시 숫자와 같은 무게로 쓴다.
NEEDS_MODEL_RESEARCH = [
    ("현물가", "DRAM·NAND 현물가는 DART 에 없다. TrendForce·DRAMeXchange 인용 "
             "기사나 증권사 리포트를 웹에서 찾는다. 수치는 EXPLORATORY 이며 "
             "무효화 조건의 관측에는 쓰되 밸류에이션 입력으로는 못 쓴다"),
    ("컨센서스", "증권사 컨센서스 수집기가 없다. 웹에서 목표주가·추정치의 "
              "**방향과 편차**를 찾는다 — 편차가 크면 논쟁 중이라는 뜻이다. "
              "컨센서스는 항상 EXPLORATORY 이며 자주 틀린다"),
    ("뉴스", "뉴스 수집기가 없다. 급변일 날짜로 웹 검색해 그날의 시장 귀인을 "
           "찾는다. 여러 매체가 같은 원인을 지목하는지 확인하고, 한 매체만 "
           "말하는 원인은 그렇게 표시한다. 전부 EXPLORATORY"),
    ("CXMT", "경쟁사(비상장·해외)는 DART 범위 밖이다. 상장 거래소 공시·현지 "
             "보도를 웹에서 찾는다. 생산능력·노드·수율은 회사가 공개하지 않는 "
             "경우가 많으니 못 찾으면 UNKNOWN 으로 남긴다"),
    ("HBM4 공급계약", "계약 조건은 공시 의무가 아니다. 웹에서 찾되 확인되더라도 "
                   "EXPLORATORY 다. 물량·기간·가격 고정 여부가 핵심이므로 "
                   "'MOU' 와 '본계약' 을 구분해서 적는다"),
    ("공시로 설명되지 않는", "공시에 없는 급변은 산업·지수·동종사 요인일 가능성이 "
                     "높다. 해당 날짜로 웹 검색해 시장 귀인을 찾고, 같은 날 "
                     "동종사(SK하이닉스·마이크론)도 함께 움직였는지 확인한다"),
    ("재고자산", "재무상태표에 있으나 밸류에이션 스냅샷에는 없다 — 넣으면 "
              "fundamentals_id 지문이 바뀌어 저장된 계산이 어긋난다. "
              "dart_fetch.py financials 로 직접 조회하면 FACT 로 쓸 수 있다"),
    ("순위 모집단", "후보 종목의 계산 산출물이 더 있어야 한다. 종목 발굴이 "
                "미구현이므로, 지금은 모델이 비교 대상을 직접 고르고 "
                "reverse_dcf 를 돌려 모집단을 늘린다"),
    ("ETF 룩스루", "knowledge/etfs/ 에 구성종목 CSV 는 있으나 섹터 매핑이 없다. "
                "모델이 상위 보유종목의 섹터를 직접 판정해 룩스루 하한을 "
                "좁힐 수 있다"),
]

# 진짜 막힌 것 — 사용자만 결정할 수 있는 것. 이것만 dead end 다.
NEEDS_USER = [
    ("논지가", "논지 승인은 사용자 권한이다. 모델도 프로그램도 대신할 수 없다 — "
               "profile.md 의 thesis.status 를 사용자가 APPROVED 로 바꿔야 한다"),
    ("horizon_months", "IPS 값은 사용자가 정한다. 재현 테스트 결과를 근거로 "
                       "제안할 수는 있으나 승인은 사용자 몫이다"),
    ("할인율", "IPS 의 valuation_assumptions 는 사용자 승인 사항이다"),
]


def load_packet(asset_id: str) -> dict:
    out = ROOT / "data" / "packets" / f"{asset_id}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["python3", "scripts/build_packet.py", asset_id, "--out", str(out)],
        cwd=ROOT, capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        raise SystemExit(1)
    return json.loads(out.read_text(encoding="utf-8"))


def requests_from(packet: dict, extra: list[str]) -> list[str]:
    out = [r["gap"] for r in packet.get("_research_requests", [])]
    out += packet["packets"].get("unknowns", {}).get("open", [])
    out += extra
    return out


def classify(text: str, ctx: dict) -> dict:
    """요청을 세 갈래로 나눈다.

    fillable        — 로컬 수집기가 있다. 프로그램이 돌린다
    model_research  — 로컬에 없다. **모델이 웹 등으로 직접 가져온다** (방법·등급 제시)
    needs_user      — 사용자만 결정할 수 있다. 유일한 dead end
    """
    plain = re.sub(r"[*`~]", "", text)
    for needle, why in NEEDS_USER:
        if needle in plain:
            return {"request": plain, "status": "needs_user", "reason": why}
    # 로컬 수집기를 먼저 본다 — 있으면 웹보다 원문이 낫다
    for handler in HANDLERS:
        if handler.get("market") and handler["market"] != ctx["market"]:
            continue
        if any(word in plain for word in handler["match"]):
            return {"request": plain, "status": "fillable",
                    "handler": handler["id"], "fills": handler["fills"],
                    "command": handler["command"](ctx)}
    for needle, how in NEEDS_MODEL_RESEARCH:
        if needle in plain:
            return {"request": plain, "status": "model_research", "how": how}
    return {"request": plain, "status": "model_research",
            "how": "로컬 수집기가 없다. 모델이 웹 검색·원문 열람으로 직접 조사한다. "
                   "웹에서 온 값은 EXPLORATORY 이며 밸류에이션 입력으로 쓸 수 없다"}


def context(asset_id: str, packet: dict) -> dict:
    ident = packet["packets"].get("company", {}).get("identity") or {}
    exchange = (ident.get("exchange") or "").upper()
    symbol = (f"{asset_id}.KS" if exchange == "KOSPI"
              else f"{asset_id}.KQ" if exchange == "KOSDAQ" else asset_id)
    latest = (packet["packets"].get("event", {}).get("latest") or {}).get("date")
    return {
        "asset_id": asset_id,
        "symbol": symbol,
        "market": "KR" if exchange in ("KOSPI", "KOSDAQ") else "US",
        "year": (latest or "2026")[:4],
    }


def cmd_plan(a) -> int:
    packet = load_packet(a.asset_id)
    ctx = context(a.asset_id, packet)
    plans = [classify(r, ctx) for r in requests_from(packet, a.request or [])]

    # 같은 핸들러가 여러 요청에 걸리면 한 번만 돌린다
    seen, unique = set(), []
    for p in plans:
        key = p.get("handler")
        if p["status"] == "fillable" and key in seen:
            p = {**p, "status": "duplicate", "reason": f"{key} 로 이미 처리됨"}
        elif p["status"] == "fillable":
            seen.add(key)
        unique.append(p)

    counts: dict[str, int] = {}
    for p in unique:
        counts[p["status"]] = counts.get(p["status"], 0) + 1
    print(f"{a.asset_id} 조사 요구 {len(unique)}건 — "
          + " / ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    for p in unique:
        mark = {"fillable": "프로그램", "model_research": "모델이 조사",
                "needs_user": "사용자 결정", "duplicate": "중복"}[p["status"]]
        print(f"\n  [{mark}] {p['request'][:86]}")
        if p["status"] == "fillable":
            print(f"        → {' '.join(p['command'])}")
        else:
            print(f"        → {p.get('how') or p.get('reason')}")
    return 0


def cmd_fill(a) -> int:
    packet = load_packet(a.asset_id)
    ctx = context(a.asset_id, packet)
    plans = [classify(r, ctx) for r in requests_from(packet, a.request or [])]

    ran, seen = [], set()
    for p in plans:
        if p["status"] != "fillable" or p["handler"] in seen:
            continue
        seen.add(p["handler"])
        print(f"채움: {p['fills']}")
        result = subprocess.run(p["command"], cwd=ROOT,
                                capture_output=True, text=True)
        ok = result.returncode == 0
        ran.append({"handler": p["handler"], "ok": ok,
                    "tail": (result.stdout or result.stderr).strip().splitlines()[-1:]})
        print(f"  {'완료' if ok else '실패'}"
              + (f" — {ran[-1]['tail'][0][:80]}" if ran[-1]["tail"] else ""))

    if not ran:
        print("채울 수 있는 요청이 없다")

    after = load_packet(a.asset_id)
    before_n = len(packet.get("_research_requests", []))
    after_n = len(after.get("_research_requests", []))
    print(f"\n패킷 재조립 — 구멍 {before_n} → {after_n}")
    remaining = [classify(r, ctx) for r in requests_from(after, [])]
    todo = [r for r in remaining if r["status"] == "model_research"]
    user = [r for r in remaining if r["status"] == "needs_user"]
    if todo:
        print(f"\n모델이 직접 조사할 것 {len(todo)}건 "
              "— 로컬에 없다는 뜻이고, 없는 정보라는 뜻이 아니다:")
        for r in todo:
            print(f"  · {r['request'][:72]}\n      {r['how']}")
    if user:
        print(f"\n사용자 결정이 필요한 것 {len(user)}건:")
        for r in user:
            print(f"  · {r['request'][:72]}\n      {r['reason']}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="조사 요구 루프")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, help_text in (("plan", "무엇을 채울 수 있는지만 본다"),
                            ("fill", "채울 수 있는 것을 실제로 채우고 재조립")):
        s = sub.add_parser(name, help=help_text)
        s.add_argument("asset_id")
        s.add_argument("--request", action="append",
                       help="모델이 직접 요청한 항목. 여러 번 줄 수 있다")
    a = p.parse_args()
    return {"plan": cmd_plan, "fill": cmd_fill}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
