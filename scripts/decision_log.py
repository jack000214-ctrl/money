#!/usr/bin/env python3
"""판정을 디스크에 남기고, 나중에 실제 결과로 채점한다 — 기억 패킷의 저장소.

`trade_check` 는 `decision_snapshot_id` 를 발급하고 그대로 휘발시켰다. 그래서
"지난번에 뭐라고 했고 그게 맞았나"를 물을 수 없었다. 모델에게 줄 **기억 패킷**은
이 파일이 쌓는 기록에서 나온다.

두 가지를 담는다.

1. **판정 기록** — 언제, 무엇을, 어떤 가격에, 어떤 근거로 어떻게 판정했는지.
   `trade_check` 결과에서 채점에 필요한 최소 필드만 뽑는다 (전체 결과는 크고
   대부분 재현 가능하다).
2. **사후 채점** — 그 뒤 실제 가격이 어떻게 됐는지. `score` 로 나중에 채운다.

`replay_decision.py` 가 만든 과거 재현 결과도 같은 형식으로 넣는다 (`origin`
필드로 구분). 실제 판정 3건과 재현 14건을 같은 자로 비교할 수 있어야 모델이
자기 문턱을 의심할 근거가 생긴다.

사용:
    python3 scripts/decision_log.py record --result result.json
    python3 scripts/decision_log.py score --symbol 005930.KS
    python3 scripts/decision_log.py show --asset 005930
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "knowledge" / "decisions"
SCHEMA = "decision-log/1.0.0"


def fail(msg: str) -> None:
    print(f"오류: {msg}", file=sys.stderr)
    raise SystemExit(1)


def log_path(asset_id: str) -> Path:
    return LOG_DIR / f"{asset_id}.jsonl"


def append(asset_id: str, entry: dict) -> Path:
    """한 줄 = 한 판정. JSONL 이라 동시 추가가 서로를 깨지 않는다."""
    path = log_path(asset_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = read(asset_id)
    # 같은 스냅샷 ID 를 두 번 넣지 않는다. 같은 입력이면 같은 판정이다.
    if any(e.get("decision_snapshot_id") == entry["decision_snapshot_id"]
           for e in existing):
        return path
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return path


def read(asset_id: str) -> list[dict]:
    path = log_path(asset_id)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def entry_from_result(result: dict, origin: str = "live") -> dict:
    """trade_check 결과에서 채점에 필요한 것만 뽑는다."""
    meta, trade = result["metadata"], result["trade"]
    basis = result.get("decision_basis") or {}
    margin = basis.get("safety_margin") or {}
    return {
        "schema_version": SCHEMA,
        "origin": origin,
        "decision_snapshot_id": meta["decision_snapshot_id"],
        "decided_at": meta["evaluated_at"],
        "asset_id": trade["asset_id"],
        "name": trade.get("name"),
        "side": trade["side"],
        "quantity": trade.get("quantity"),
        "price": trade.get("price_krw"),
        "verdict": result["decision"]["verdict"],
        "issue_codes": [i["code"] for i in result.get("issues", [])],
        "expected_return_pct": basis.get("position_return_pct"),
        "return_measure": basis.get("return_measure"),
        "required_return_pct": (
            result.get("decision_basis", {}).get("required_return_pct")),
        "bear_loss_contribution_pct": basis.get("bear_loss_contribution_pct"),
        "safety_margin_pct": margin.get("margin_pct"),
        "price_pct_of_value": margin.get("price_pct_of_value"),
        "analysis_id": meta.get("analysis_id"),
        "calculation_id": (basis.get("expected_return") or {}).get("calculation_id"),
        "ips_id": meta.get("ips_id"),
        "engine_version": meta.get("engine_version"),
        "outcome": None,
    }


def cmd_record(a) -> int:
    result = json.loads(Path(a.result).read_text(encoding="utf-8"))
    entry = entry_from_result(result, origin=a.origin)
    path = append(entry["asset_id"], entry)
    print(f"{entry['asset_id']} {entry['decided_at'][:10]} "
          f"{entry['verdict']} @ {entry['price']:,.0f} → {path.relative_to(ROOT)}")
    return 0


def closes(symbol: str) -> list[tuple[str, float]]:
    path = ROOT / "knowledge" / "prices" / f"{symbol.replace('.', '_')}.csv"
    if not path.exists():
        fail(f"시세 파일이 없다: {path}")
    with path.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(l for l in f if not l.startswith("#")))
    return [(r["date"], float(r["close"])) for r in rows]


def cmd_score(a) -> int:
    """판정 뒤 실제 가격으로 채점한다. 아직 안 지난 지평은 비워둔다."""
    series = closes(a.symbol)
    entries = read(a.asset)
    if not entries:
        fail(f"{a.asset} 판정 기록이 없다")

    scored = []
    for e in entries:
        start = e["decided_at"][:10]
        base = [c for c in series if c[0] <= start]
        if not base or not e.get("price"):
            scored.append(e)
            continue
        outcome = {}
        for months in a.score_months:
            target = (dt.date.fromisoformat(start)
                      + dt.timedelta(days=int(months * 30.44))).isoformat()
            window = [c for c in series if c[0] <= target]
            end_date, end_price = window[-1]
            days = max((dt.date.fromisoformat(end_date)
                        - dt.date.fromisoformat(start)).days, 1)
            outcome[str(months)] = {
                "complete": end_date >= target,
                "end_date": end_date,
                "end_price": end_price,
                "raw_pct": round((end_price / e["price"] - 1) * 100, 2),
                "annualized_pct": round(
                    ((end_price / e["price"]) ** (365 / days) - 1) * 100, 2),
            }
        e["outcome"] = outcome
        e["scored_at"] = dt.datetime.now(
            dt.timezone(dt.timedelta(hours=9))).isoformat(timespec="seconds")
        scored.append(e)

    path = log_path(a.asset)
    path.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in scored),
                    encoding="utf-8")

    print(f"{a.asset} — 판정 {len(scored)}건 채점")
    print(f"{'판정일':12s}{'가격':>11s}{'판정':>20s}{'기대':>9s}"
          + "".join(f"{str(m) + 'M':>10s}" for m in a.score_months))
    print("-" * (52 + 10 * len(a.score_months)))
    for e in scored:
        exp = (f"{e['expected_return_pct']:>8.1f}%"
               if isinstance(e.get("expected_return_pct"), (int, float))
               else f"{'—':>9s}")
        line = (f"{e['decided_at'][:10]:12s}{e['price']:>11,.0f}"
                f"{e['verdict']:>20s}{exp}")
        for m in a.score_months:
            o = (e.get("outcome") or {}).get(str(m))
            if not o:
                line += f"{'—':>10s}"
            elif o["complete"]:
                line += f"{o['annualized_pct']:>9.1f}%"
            else:
                line += f"{'진행중':>10s}"
        print(line)
    return 0


def cmd_show(a) -> int:
    entries = read(a.asset)
    if not entries:
        print(f"{a.asset} 판정 기록 없음")
        return 0
    print(json.dumps(entries, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="판정 기록·채점 — 기억 패킷 저장소")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("record", help="trade_check --json 결과를 기록")
    r.add_argument("--result", required=True)
    r.add_argument("--origin", default="live", choices=["live", "replay"])

    s = sub.add_parser("score", help="판정 뒤 실제 가격으로 채점")
    s.add_argument("--asset", required=True)
    s.add_argument("--symbol", required=True)
    s.add_argument("--score-months", nargs="+", type=int, default=[3, 12, 24])

    w = sub.add_parser("show", help="기록 출력")
    w.add_argument("--asset", required=True)

    a = p.parse_args()
    return {"record": cmd_record, "score": cmd_score, "show": cmd_show}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
