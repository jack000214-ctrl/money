#!/usr/bin/env python3
"""과거 시점으로 돌아가 지금의 판정 논리를 다시 돌린다 — 편향 계측기.

이 스크립트가 답하려는 질문은 하나다.
**"바닥에서도 이 시스템은 사지 말라고 했을까?"**

삼성전자는 2026-01-02 에 128,500 원이었고 6개월 뒤 362,500 원이 됐다(+182%).
그때 이 시스템이 BLOCKED 를 냈다면, 문제는 그 종목이 비쌌던 게 아니라
**판정 논리가 구조적으로 거부 쪽으로 기울어 있다**는 뜻이다.

세 가지를 지킨다.

1. **미래 정보를 쓰지 않는다.** 각 시점에 DART 에 실제로 제출돼 있던 정기보고서만
   쓴다. 제출일을 조회해서 확인한다 — 분기 종료일이 아니라 제출일이 기준이다
   (2025.09 분기보고서는 2025-11-14 에야 열람 가능했다).
2. **시나리오 성장률에 내 재량을 넣지 않는다.** 스냅샷의 분기 FCF 범위에서
   기계적으로 뽑는다. 약세는 최저 분기, 강세는 최고 분기의 연율화 값으로
   10년에 걸쳐 수렴하는 CAGR 이고 기준은 그 중간이다. 사람이 고르면 재현
   테스트가 사람의 사후 판단을 측정하게 된다.
3. **판정 후 실제 수익률을 함께 적는다.** 그게 채점표다.

사용:
    python3 scripts/replay_decision.py 005930 --symbol 005930.KS \\
        --dates 2025-07-28 2026-01-02 2026-04-01 2026-06-01 2026-07-28
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dart_fetch import load_key, request, resolve  # noqa: E402
from decision_log import SCHEMA as LOG_SCHEMA, append as log_append  # noqa: E402

PERIODIC = ("사업보고서", "분기보고서", "반기보고서")
# 보고서 대상기간 → TTM 창의 끝
PERIOD_END = {"03": "03-31", "06": "06-30", "09": "09-30", "12": "12-31"}


def filings(stock_code: str, from_year: int = 2023) -> list[dict]:
    key = load_key()
    corp = resolve(stock_code, key)["corp_code"]
    out = []
    # 가장 이른 재현 날짜보다 1년 앞부터 받는다. 범위를 고정하면 그 이전
    # 시점은 "제출된 보고서가 없다"로 죽는다.
    for year in range(from_year, dt.date.today().year + 1):
        data = request("list.json", {
            "corp_code": corp, "bgn_de": f"{year}0101", "end_de": f"{year}1231",
            "pblntf_ty": "A", "page_count": "100"}, key, allow_empty=True)
        if data:
            out += [r for r in data.get("list", [])
                    if r["report_nm"].startswith(PERIODIC)]
    return sorted(out, key=lambda r: r["rcept_dt"])


def latest_available(rows: list[dict], as_of: str) -> tuple[str, str]:
    """as_of 날짜에 **실제로 제출돼 있던** 가장 최근 정기보고서의 기준 분기말."""
    stamp = as_of.replace("-", "")
    seen = [r for r in rows if r["rcept_dt"] <= stamp]
    if not seen:
        raise SystemExit(f"{as_of} 시점에 제출된 정기보고서가 없다")
    row = seen[-1]
    name = row["report_nm"]
    year, month = name.split("(")[1].rstrip(")").split(".")
    return f"{year}-{PERIOD_END[month]}", f"{row['rcept_dt']} {name}"


def price_at(csv_path: Path, as_of: str) -> tuple[str, float]:
    with csv_path.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(l for l in f if not l.startswith("#")))
    usable = [r for r in rows if r["date"] <= as_of]
    if not usable:
        raise SystemExit(f"{as_of} 이전 시세가 없다")
    return usable[-1]["date"], float(usable[-1]["close"])


def read_closes(csv_path: Path) -> list[tuple[str, float]]:
    with csv_path.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(l for l in f if not l.startswith("#")))
    return [(r["date"], float(r["close"])) for r in rows]


def latest_price(csv_path: Path) -> tuple[str, float]:
    with csv_path.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(l for l in f if not l.startswith("#")))
    return rows[-1]["date"], float(rows[-1]["close"])


def mechanical_growth(snapshot: dict) -> dict[str, float]:
    """분기 FCF 범위에서 시나리오 성장률을 기계적으로 만든다.

    약세는 최저 분기 FCF 를 연율화한 수준으로, 강세는 최고 분기 수준으로
    10년에 걸쳐 수렴하는 CAGR 이다. 사후 판단이 끼어들 자리가 없다.
    """
    ocf = {(q["fy"], q["quarter"]): q["val"] for q in snapshot["quarters"]["ocf"]}
    capex = {(q["fy"], q["quarter"]): q["val"] for q in snapshot["quarters"]["capex"]}
    quarterly = [ocf[k] - capex[k] for k in sorted(ocf) if k in capex]
    ttm = snapshot["values"]["fcf_ttm"]
    if ttm <= 0 or min(quarterly) <= 0:
        # 중단하지 않는다. **평가 불가라는 사실 자체가 결과**다 — 이 하네스는
        # FCF 가 양수인 국면만 채점할 수 있고, 그건 "아니라고 말하는 게 맞았던"
        # 하락 국면이 평가셋에서 조용히 빠진다는 뜻이다.
        return None

    def cagr(target: float) -> float:
        return ((target / ttm) ** 0.1 - 1) * 100

    low, high = min(quarterly) * 4, max(quarterly) * 4
    return {"bear": round(cagr(low), 1),
            "base": round(cagr((low + high) / 2), 1),
            "bull": round(cagr(high), 1)}


def spearman(pairs: list[tuple[float, float]]) -> float:
    """순위상관. 문턱이 틀려도 **순서**가 맞으면 오프셋만 교정하면 된다.

    절대값이 전부 음수로 눌려 있어도 순위가 맞으면 신호는 살아 있다는 뜻이다.
    """
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
    return num / den if den else 0.0


def run(cmd: list[str], tolerate: bool = False) -> str | None:
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
    if result.returncode != 0:
        if tolerate:
            # 한 시점이 실패해도 나머지는 채점해야 한다. 어떤 시점이 왜
            # 평가 불가인지가 커버리지 정보 그 자체다.
            return None
        raise SystemExit(f"실패: {' '.join(cmd)}\n{result.stderr}")
    return result.stdout


def main() -> int:
    p = argparse.ArgumentParser(description="과거 시점 판정 재현 — 편향 계측")
    p.add_argument("stock_code")
    p.add_argument("--symbol", required=True, help="시세 CSV 심볼 (005930.KS)")
    p.add_argument("--dates", nargs="+", required=True)
    p.add_argument("--discount-rate", type=float, default=12.0)
    p.add_argument("--terminal-growth", type=float, default=2.5)
    p.add_argument("--horizon-months", type=int, default=24)
    p.add_argument("--required-return", type=float, default=12.0)
    p.add_argument("--log", action="store_true",
                   help="재현 판정을 knowledge/decisions/ 에 origin=replay 로 기록한다. "
                        "기억 패킷이 실제 판정과 재현을 같은 자로 비교하려면 필요하다")
    p.add_argument("--score-months", nargs="+", type=int, default=[12, 24, 36],
                   help="채점 지평. 판정 지평(--horizon-months)과 분리한다 — "
                        "24개월만 보면 최근 시점이 전부 버려진다")
    p.add_argument("--probabilities", default="0.4,0.4,0.2",
                   help="bear,base,bull")
    a = p.parse_args()

    prob = dict(zip(("bear", "base", "bull"),
                    (float(x) for x in a.probabilities.split(","))))
    csv_path = ROOT / "knowledge/prices" / f"{a.symbol.replace('.', '_')}.csv"
    rows = filings(a.stock_code, from_year=int(min(a.dates)[:4]) - 2)
    now_date, now_price = latest_price(csv_path)

    print(f"재현 대상 {a.stock_code} · 판정 논리 고정 "
          f"(할인율 {a.discount_rate}% / 영구성장 {a.terminal_growth}% / "
          f"수렴 {a.horizon_months}개월 / 요구 {a.required_return}%)")
    print(f"실제 수익률 기준 시점 {now_date} {now_price:,.0f}\n")

    results, unevaluable = [], []
    with tempfile.TemporaryDirectory() as tmp:
        for as_of in a.dates:
            pdate, price = price_at(csv_path, as_of)
            period_end, provenance = latest_available(rows, as_of)
            year = int(period_end[:4])

            built = run(["python3", "scripts/dart_fetch.py", "fundamentals",
                         a.stock_code, "--year", str(year),
                         "--period-end", period_end, "--out-dir", tmp],
                        tolerate=True)
            if built is None:
                unevaluable.append({
                    "as_of": pdate, "price": price, "period_end": period_end,
                    "fcf_ttm": None, "provenance": provenance,
                    "why": "스냅샷 생성 실패 (원문 API 커버리지)"})
                continue
            snap = json.loads(
                (Path(tmp) / f"{a.stock_code}-{period_end}.json").read_text("utf-8"))
            growth = mechanical_growth(snap)
            if growth is None:
                unevaluable.append({
                    "as_of": pdate, "price": price, "period_end": period_end,
                    "fcf_ttm": snap["values"]["fcf_ttm"], "provenance": provenance,
                    "why": "분기 FCF 음수 — CAGR 앵커 불가"})
                continue

            out = run(["python3", "scripts/reverse_dcf.py", "scenarios",
                       "--instrument", a.stock_code, "--price", str(price),
                       "--fundamentals",
                       str(Path(tmp) / f"{a.stock_code}-{period_end}.json"),
                       "--discount-rate", str(a.discount_rate),
                       "--terminal-growth", str(a.terminal_growth),
                       "--years", "10",
                       "--growth", *[f"{k}={v}" for k, v in growth.items()],
                       "--horizon-months", str(a.horizon_months),
                       "--source-id", "REPLAY", "--json"])
            calc = json.loads(out)
            scen = calc["outputs"]["scenarios"]
            weighted = sum(scen[k]["return_pct"] * prob[k] for k in prob)
            # 판정 지평(horizon) 만큼 뒤의 가격으로 잰다. "오늘까지"로 재면
            # 최근 시점은 창이 며칠뿐인데 연율화되어 −97.9% 같은 비교 불가능한
            # 값이 나온다. 창이 짧으면 연율화하지 않고 원시 수익률만 적는다.
            start = dt.date.fromisoformat(pdate)
            closes = read_closes(csv_path)
            scores = {}
            for months in a.score_months:
                target = start + dt.timedelta(days=int(months * 30.44))
                window = [r for r in closes if r[0] <= target.isoformat()]
                end_date, end_price = window[-1]
                days = max((dt.date.fromisoformat(end_date) - start).days, 1)
                complete = end_date >= target.isoformat()
                scores[months] = {
                    "complete": complete,
                    "end_date": end_date,
                    "raw": (end_price / price - 1) * 100,
                    "annualized": ((end_price / price) ** (365 / days) - 1) * 100,
                }

            results.append({
                "as_of": pdate, "price": price, "period_end": period_end,
                "scores": scores,
                "provenance": provenance, "growth": growth,
                "fcf_ttm": snap["values"]["fcf_ttm"],
                "implied": calc["outputs"]["implied_growth_pct"],
                "weighted": weighted,
                "verdict": "BLOCKED" if weighted < a.required_return else "통과",
            })

    head = (f"{'시점':11s}{'주가':>10s}{'내재성장':>9s}{'기대수익':>10s}{'판정':>9s}"
            + "".join(f"{str(m) + '개월 연율':>13s}" for m in a.score_months))
    print(head)
    print("-" * len(head))
    for r in results:
        line = (f"{r['as_of']:11s}{r['price']:>10,.0f}{r['implied']:>8.1f}%"
                f"{r['weighted']:>9.1f}%{r['verdict']:>9s}")
        for m in a.score_months:
            sc = r["scores"][m]
            line += (f"{sc['annualized']:>12.1f}%" if sc["complete"]
                     else f"{'미도래':>13s}")
        print(line)
    print("-" * len(head))
    print("'미도래' = 그 지평이 아직 안 지났다 (채점 제외)")

    if a.log:
        for r in results:
            log_append(a.stock_code, {
                "schema_version": LOG_SCHEMA,
                "origin": "replay",
                # 재현은 같은 입력이면 같은 ID 여야 중복이 안 쌓인다.
                "decision_snapshot_id":
                    f"REPLAY-{a.stock_code}-{r['as_of']}-{a.horizon_months}m",
                "decided_at": f"{r['as_of']}T00:00:00+09:00",
                "asset_id": a.stock_code,
                "name": None,
                "side": "BUY",
                "quantity": None,
                "price": r["price"],
                "verdict": "BLOCKED" if r["verdict"] == "BLOCKED" else "PROCEED",
                "issue_codes": (["EXPECTED_RETURN_BELOW_HURDLE"]
                                if r["verdict"] == "BLOCKED" else []),
                "expected_return_pct": round(r["weighted"], 2),
                "return_measure": "PROBABILITY_WEIGHTED",
                "required_return_pct": a.required_return,
                "implied_growth_pct": r["implied"],
                "period_end": r["period_end"],
                "provenance": r["provenance"],
                "scenario_growth_pct": r["growth"],
                "outcome": {
                    str(m): {
                        "complete": r["scores"][m]["complete"],
                        "end_date": r["scores"][m]["end_date"],
                        "raw_pct": round(r["scores"][m]["raw"], 2),
                        "annualized_pct": round(r["scores"][m]["annualized"], 2),
                    } for m in a.score_months
                },
            })
        print(f"\n판정 기록: knowledge/decisions/{a.stock_code}.jsonl "
              f"({len(results)}건, origin=replay)")

    blocked = [r for r in results if r["verdict"] == "BLOCKED"]
    print(f"\nBLOCKED {len(blocked)}/{len(results)}건")
    for m in a.score_months:
        scorable = [r for r in results if r["scores"][m]["complete"]]
        if not scorable:
            print(f"   {m}개월: 채점 가능 0건")
            continue
        miss = [r for r in scorable if r["verdict"] == "BLOCKED"
                and r["scores"][m]["annualized"] > a.required_return]
        good = [r for r in scorable if r["verdict"] == "BLOCKED"
                and r["scores"][m]["annualized"] <= a.required_return]
        print(f"   {m}개월: 채점 {len(scorable)}건 · 맞은 거부 {len(good)} / "
              f"놓친 기회 {len(miss)}")
        if not good:
            print(f"      ⚠ 맞은 거부 0건 — 판별력 없음")
    # 순위 상관: 기대수익이 높은 순서가 실제 수익 순서와 맞는지
    for m in a.score_months:
        pairs = [(r["weighted"], r["scores"][m]["annualized"])
                 for r in results if r["scores"][m]["complete"]]
        if len(pairs) >= 3:
            print(f"   {m}개월 순위상관(Spearman) {spearman(pairs):+.2f}  "
                  "— 문턱과 무관하게 '순서'는 맞는지")
    missed = []
    if missed:
        print(f"⚠ 그중 {len(missed)}건은 이후 실제 연율이 요구수익률 "
              f"{a.required_return}% 를 넘겼다 — 놓친 기회다:")
        for r in missed:
            print(f"   {r['as_of']} {r['price']:,.0f} → 기대 {r['weighted']:.1f}% "
                  f"인데 실제 연율 {r['annualized']:+.1f}% "
                  f"({r['end_date']} {r['end_price']:,.0f})")
    if unevaluable:
        print(f"\n평가 불가 {len(unevaluable)}건:")
        for r in unevaluable:
            fcf = (f"TTM FCF {r['fcf_ttm']/1e12:,.1f}조"
                   if r["fcf_ttm"] is not None else "—")
            print(f"   {r['as_of']} {r['price']:>10,.0f}  {fcf:>16s}  "
                  f"{r['why']}  ← {r['provenance']}")
        print("   ⚠ 이 국면이 평가셋에서 빠진다는 것 자체가 편향이다 — "
              "하락 국면이 채점되지 않는다")

    print("\n시나리오 성장률 앵커 (기계적, 분기 FCF 범위에서):")
    for r in results:
        print(f"   {r['as_of']}  {r['growth']}  ← {r['provenance']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
