#!/usr/bin/env python3
"""실행 계획 — 살 거라면 몇 회로, 얼마씩, 어떤 조건에서.

이 스크립트는 **매수 신호를 내지 않는다.** 사용자가 사기로 정했을 때 그것을
어떻게 집행할지만 계산한다. "살까?"에 답하는 건 모델과 사용자의 몫이고,
여기는 "산다면 이렇게"를 만든다.

왜 필요한가: 지금까지 시스템은 판정(PROCEED/BLOCKED)까지만 갔고 **얼마씩 몇 번**은
사람 머릿속에 있었다. 사용자 평가에서 "분할진입·증액 계획: 없음" 이었던 칸이다.

세 가지 원칙.

1. **가격을 예측하지 않는다.** 트리거는 시간과 관측 가능한 사건이다.
   "20만원 오면 산다"는 가격 예측이고, 이 프로젝트가 금지하는 가격대 근거다.
   대신 **상한**은 둔다 — 안전마진이 더 나빠지는 가격에 더 넣지 않는다는 뜻이라
   예측이 아니라 정책이다.
2. **분할 횟수는 시장 국면이 정한다.** market_regime 의 가이드를 숫자로 바꾼다.
   STRESS 에서 한 번에 넣는 것이 최대 실수라는 게 그 스크립트의 결론이다.
3. **하방 예산이 총액을 정한다.** 약세 시나리오가 실현될 때 이 포지션 하나가
   총자산에 입히는 손실이 IPS 한도를 넘지 않는 크기가 최대 총액이다.

사용:
    python3 scripts/action_plan.py 005930 --amount 3000000
    python3 scripts/action_plan.py 005930 --amount 3000000 --price 231000
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from contracts import load_json  # noqa: E402

SCHEMA = "action-plan/1.0.0"

# 시장 국면 → 분할 횟수·간격. market_regime.py 의 산문 가이드를 숫자로 옮긴 것이다.
# 근거: 위기장은 장기 투자자에게 매수 기회였던 경우가 많고 문제는 한 번에 넣는 것.
REGIME_TRANCHES = {
    "NORMAL":   {"count": 2, "weeks": 4,
                 "why": "평소 기준이 통한다. 최소 분할만 한다"},
    "ELEVATED": {"count": 3, "weeks": 3,
                 "why": "평소보다 변동이 커 체결가가 계획과 어긋날 수 있다"},
    "STRESS":   {"count": 4, "weeks": 3,
                 "why": "시장 전체가 가격을 지배하는 구간이다. 좋은 회사도 같이 "
                        "빠지므로 한 번에 넣으면 진입가가 국면에 종속된다"},
    "CRISIS":   {"count": 6, "weeks": 4,
                 "why": "체제 전환 국면이다. 과거 밸류에이션 밴드가 기준이 되지 "
                        "않으므로 기간을 길게 벌린다"},
}


def fail(msg: str) -> None:
    print(f"오류: {msg}", file=sys.stderr)
    raise SystemExit(1)


def next_events(asset_id: str) -> list[dict]:
    """timeline.md 의 「다음 확인 이벤트」 표에서 날짜가 확정된 것만 뽑는다.

    시간만으로 분할하면 회차 사이에 **정보가 들어오지 않는다.** 알려진 확인
    이벤트가 계획 구간 안에 있으면 그 뒤로 회차를 밀어야, 같은 돈으로 더 많은
    정보를 사게 된다. 날짜가 '2026-10 중' 처럼 미확정이면 트리거로 쓸 수 없다.
    """
    import re
    base = ROOT / "knowledge" / "companies"
    if not base.exists():
        return []
    for path in base.iterdir():
        if not (path.is_dir() and (path.name == asset_id
                                   or path.name.startswith(f"{asset_id}-"))):
            continue
        tl = path / "timeline.md"
        if not tl.exists():
            return []
        text = tl.read_text(encoding="utf-8")
        block = text.split("다음 확인 이벤트", 1)
        if len(block) < 2:
            return []
        out = []
        for line in block[1].splitlines():
            cells = [c.strip() for c in line.split("|") if c.strip()]
            if len(cells) < 2:
                continue
            m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})", cells[0])
            if m:
                out.append({"date": cells[0],
                            "what": re.sub(r"[*`]", "", cells[1])})
        return sorted(out, key=lambda x: x["date"])
    return []


def build_packet(asset_id: str) -> dict:
    out = ROOT / "data" / "packets" / f"{asset_id}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(["python3", "scripts/build_packet.py", asset_id,
                        "--out", str(out)], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr, file=sys.stderr)
        raise SystemExit(1)
    return json.loads(out.read_text(encoding="utf-8"))


def main() -> int:
    p = argparse.ArgumentParser(
        description="실행 계획 (매수 신호가 아니라 집행 방법)")
    p.add_argument("asset_id")
    p.add_argument("--amount", type=float, required=True,
                   help="넣으려는 총액 (원)")
    p.add_argument("--price", type=float, help="현재가. 생략하면 패킷의 최근 종가")
    p.add_argument("--start", help="첫 회 날짜 YYYY-MM-DD (기본 오늘)")
    p.add_argument("--event-reach-weeks", type=int, default=8,
                   help="계획 창을 넘어선 확인 이벤트를 트리거로 끌어올 최대 주수. "
                        "결정적 정보를 며칠 차이로 놓치지 않기 위한 여유")
    p.add_argument("--out")
    a = p.parse_args()

    packet = build_packet(a.asset_id)
    pk = packet["packets"]
    ips = load_json(ROOT / "knowledge/portfolio/ips.json")

    # --- 가격 ---
    price = a.price or ((pk["event"].get("latest") or {}).get("close"))
    if not price:
        fail("현재가를 알 수 없다. --price 로 지정한다")

    # --- 시장 국면 → 분할 ---
    regime_block = (pk.get("market_regime") or {}).get("this_market") or {}
    regime = regime_block.get("regime")
    if regime not in REGIME_TRANCHES:
        fail(f"시장 국면을 알 수 없다 ({regime}). market_regime.py 를 먼저 돌린다")
    shape = REGIME_TRANCHES[regime]

    # --- 하방 예산이 총액 상한을 정한다 ---
    portfolio = pk["portfolio"]
    total_assets = portfolio["total_assets_krw"]
    cash = portfolio["cash_krw"]
    budget_pct = ips["decision_policy"]["max_bear_loss_contribution_pct"]

    calc = pk["calculation"].get("this_asset") or {}
    # 약세 시나리오 깊이 — 계산 산출물의 약세 주당가치로 잰다
    bear_depth = None
    calc_id = calc.get("calculation_id")
    if calc_id:
        c = load_json(ROOT / "knowledge/calculations" / f"{calc_id}.json")
        bear_value = c["outputs"]["scenarios"]["bear"]["value_per_share"]
        bear_depth = max(0.0, 1 - bear_value / price)

    limits = []
    if bear_depth and bear_depth > 0:
        cap = budget_pct / 100 * total_assets / bear_depth
        limits.append({"name": f"하방 예산 {budget_pct}%",
                       "max_krw": round(cap),
                       "why": f"약세 시나리오 −{bear_depth*100:.0f}% 가 실현될 때 "
                              f"총자산의 {budget_pct}% 를 넘지 않는 크기"})
    risk_class = "STOCK"
    std = ips["position_limits"]["by_risk_class"][risk_class]["standard_pct"]
    limits.append({"name": f"{risk_class} 표준 한도 {std}%",
                   "max_krw": round(std / 100 * total_assets),
                   "why": "이 선을 넘으면 집중 사유를 남겨야 한다"})
    limits.append({"name": "현금 여력", "max_krw": round(cash),
                   "why": "예수금 전액"})

    binding = min(limits, key=lambda x: x["max_krw"])
    planned = min(a.amount, binding["max_krw"])

    # --- 분할 ---
    count, weeks = shape["count"], shape["weeks"]
    per = planned / count
    shares_per = int(per // price)
    if shares_per < 1:
        count = max(1, int(planned // price))
        per = planned / count if count else 0
        shares_per = int(per // price)
    start = dt.date.fromisoformat(a.start) if a.start else dt.date.today()

    # 다음 확인 이벤트를 트리거에 붙인다 — 시간만으로 나누면 정보가 안 들어온다
    events = next_events(a.asset_id)
    last_slot = start + dt.timedelta(weeks=weeks * (count - 1))
    # 확인 이벤트가 계획 창을 조금 넘겨도 트리거로 쓴다. 결정적 정보를 며칠
    # 차이로 안 기다리는 건 말이 안 된다 — 실제로 삼성전자 자사주 재공시
    # (2026-10-22)가 창(10-20)을 **이틀** 넘겨서 제외됐다. 다만 무한정 밀면
    # 계획이 사라지므로 상한을 둔다.
    horizon = last_slot + dt.timedelta(weeks=a.event_reach_weeks)
    usable = [e for e in events
              if start.isoformat() < e["date"] <= horizon.isoformat()]

    tranches = []
    for i in range(count):
        when = start + dt.timedelta(weeks=weeks * i)
        gate = None
        # 마지막 회차부터 거꾸로 이벤트를 붙인다 — 정보가 들어온 뒤에 넣는 게
        # 남은 회차의 목적이다
        idx_from_end = count - 1 - i
        if idx_from_end < len(usable):
            gate = usable[len(usable) - 1 - idx_from_end]
            if gate["date"] > when.isoformat():
                when = dt.date.fromisoformat(gate["date"])
        if tranches and when.isoformat() < tranches[-1]["on_or_after"]:
            when = dt.date.fromisoformat(tranches[-1]["on_or_after"])
        tranches.append({
            "n": i + 1,
            "on_or_after": when.isoformat(),
            "gated_on": (f"{gate['date']} {gate['what']}" if gate else None),
            "shares": shares_per,
            "approx_krw": round(shares_per * price),
            # 가격 예측이 아니라 정책 상한이다
            "do_not_pay_above": round(price * 1.15),
            "skip_if": ("무효화 조건이 하나라도 관측되면 이 회차를 건너뛰고 "
                        "논지를 다시 검토한다"),
        })

    committed = sum(t["approx_krw"] for t in tranches)
    residual = round(planned - committed)

    plan = {
        "schema_version": SCHEMA,
        "asset_id": a.asset_id,
        "built_at": dt.datetime.now(
            dt.timezone(dt.timedelta(hours=9))).isoformat(timespec="seconds"),
        "_not_a_signal": "이 계획은 매수 권유가 아니다. 사용자가 사기로 정했을 때 "
                         "집행 방법만 계산한 것이다. '살까'는 이 파일 밖의 판단이다",
        "price_used": price,
        "requested_krw": a.amount,
        "planned_krw": round(planned),
        "trimmed": planned < a.amount,
        "binding_limit": binding,
        "all_limits": limits,
        "market_regime": {
            "regime": regime,
            "tranche_count": count,
            "interval_weeks": weeks,
            "why": shape["why"],
            "guidance": regime_block.get("guidance"),
            "divergence": (pk.get("market_regime") or {}).get("divergence"),
        },
        "safety_margin": {
            "price_pct_of_value": calc.get("price_pct_of_value"),
            "rank": calc.get("rank"),
            "rank_of": pk["calculation"].get("rank_of"),
            "reliability": pk["calculation"].get("rank_reliability"),
        },
        "tranches": tranches,
        "committed_krw": committed,
        "residual_krw": residual,
        "residual_note": ("주 단위로 내림해서 남은 금액이다. 억지로 한 주 더 "
                          "넣지 않는다 — 계획을 맞추려고 한도를 넘는 것이 "
                          "가장 흔한 실수다" if residual else None),
        "known_events_in_window": usable,
        "stop_conditions": pk["company"].get("invalidation_conditions") or [],
        "open_questions": pk["unknowns"].get("open"),
        "_gaps": [],
    }

    # 계획을 무효로 만드는 조건들을 명시한다
    thesis = pk["company"].get("thesis_status")
    if thesis != "APPROVED":
        plan["_gaps"].append(
            f"논지가 {thesis} 다 — 사용자 승인 전에는 이 계획을 집행하면 안 된다")
    if calc.get("price_pct_of_value", 0) > 100:
        plan["_gaps"].append(
            f"안전마진이 없다 — 확률가중 내재가치의 "
            f"{calc['price_pct_of_value']:.0f}% 에 산다. 분할은 진입가 위험을 "
            "줄이지만 비싼 값을 싸게 만들지는 않는다")
    if not calc:
        plan["_gaps"].append("계산 산출물이 없어 하방 예산 상한을 못 냈다")

    out = Path(a.out) if a.out else (ROOT / "data" / "plans"
                                     / f"{a.asset_id}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")

    print(f"실행 계획 — {a.asset_id} @ {price:,.0f}원   "
          f"※ 매수 신호가 아니라 집행 방법")
    print(f"\n[ 총액 ]  요청 {a.amount:,.0f} → 계획 {planned:,.0f}"
          + ("  (한도로 축소)" if plan["trimmed"] else ""))
    for lim in sorted(limits, key=lambda x: x["max_krw"]):
        mark = "←구속" if lim is binding else "     "
        print(f"  {mark} {lim['name']:<22}{lim['max_krw']:>14,.0f}원  {lim['why']}")
    print(f"\n[ 분할 ]  {regime} → {count}회 / {weeks}주 간격")
    print(f"  {shape['why']}")
    print(f"\n  {'회차':>4s}{'시점 이후':>13s}{'수량':>6s}{'금액':>11s}"
          f"{'상한':>11s}  대기 이벤트")
    for t in tranches:
        gate = t["gated_on"] or "—"
        print(f"  {t['n']:>4d}{t['on_or_after']:>13s}{t['shares']:>6d}"
              f"{t['approx_krw']:>11,.0f}{t['do_not_pay_above']:>10,.0f}원  {gate[:46]}")
    print(f"  합계 {committed:,.0f}원" +
          (f" · 잔액 {residual:,.0f}원 (주 단위 내림)" if residual else ""))
    sm = plan["safety_margin"]
    if sm.get("price_pct_of_value"):
        print(f"\n[ 안전마진 ]  내재가치의 {sm['price_pct_of_value']:.0f}% · "
              f"관측 {sm['rank_of']}개 중 {sm['rank']}위")
    if plan["_gaps"]:
        print("\n[ 집행 전 해결 ]")
        for g in plan["_gaps"]:
            print(f"  ! {g}")
    print(f"\n작성: {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
