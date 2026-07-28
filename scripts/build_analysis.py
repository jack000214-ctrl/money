#!/usr/bin/env python3
"""분석 계약을 스펙 파일에서 만든다. 종목에 종속되지 않는다.

`build_analysis_tsla.py` 는 claim·논지·반대논지 산문이 코드에 박혀 있어서
종목이 늘면 스크립트가 늘어났다. 이 스크립트는 그 구조를 뒤집는다.

  산문·확률·출처 목록  →  `knowledge/research/specs/{종목}.json`  (사람이 쓴다)
  해시·시나리오 수익률 →  이 스크립트가 계산한다                   (손으로 못 쓴다)

즉 **판단은 스펙에, 계산은 코드에** 둔다. 스펙에 시나리오 수익률을 적어도
무시된다 — 그 값은 항상 `calculation_id` 가 가리키는 산출물에서 온다.

사용:
    python3 scripts/build_analysis.py --spec knowledge/research/specs/005930.json
    python3 scripts/build_analysis.py --spec ... --calculation CALC-xxxx
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from contracts import (ContractError, content_hash,  # noqa: E402
                       load_calculation, load_json, validate_analysis)

SCENARIOS = ("bear", "base", "bull")


def fail(msg: str) -> None:
    print(f"오류: {msg}", file=sys.stderr)
    raise SystemExit(1)


def resolve_calculation(instrument: str, explicit: str | None) -> dict:
    """계산 산출물을 찾는다. 스펙에 ID 를 박으면 재계산할 때마다 낡는다."""
    calc_dir = ROOT / "knowledge/calculations"
    if explicit:
        return load_calculation(explicit, calc_dir)
    found = [p for p in calc_dir.glob("CALC-*.json")
             if load_json(p).get("instrument_id") == instrument]
    if len(found) != 1:
        fail(f"{instrument} 계산 산출물이 {len(found)}개다. "
             f"--calculation 으로 지정한다: {[p.stem for p in found]}")
    return load_calculation(found[0].stem, calc_dir)


def build(spec: dict, calculation: dict) -> dict:
    instrument = spec["instrument_id"]
    if calculation["instrument_id"] != instrument:
        fail(f"계산 산출물은 {calculation['instrument_id']} 것인데 "
             f"스펙은 {instrument} 다")

    outputs = calculation["outputs"]["scenarios"]
    probabilities = {name: spec["expected_return"]["probabilities"][name]
                     for name in SCENARIOS}
    total = sum(probabilities.values())
    if abs(total - 1.0) > 1e-9:
        fail(f"확률 합이 {total} 이다. 1 이어야 한다")
    if any(p <= 0 for p in probabilities.values()):
        fail("확률 0 인 시나리오는 허용되지 않는다. 일어날 수 없다고 보면 "
             "시나리오가 아니라 다른 시나리오의 일부다")

    weighted = sum(outputs[n]["return_pct"] * probabilities[n] for n in SCENARIOS)

    # 출처 해시는 실제 파일에서 계산한다. 스펙에 적을 수 없다.
    sources = []
    for item in spec["sources"]:
        path = ROOT / item["path"]
        if not path.exists():
            fail(f"출처 {item['source_id']}: 파일이 없다 — {item['path']}")
        sources.append({
            "source_id": item["source_id"],
            "source_type": item["source_type"],
            "locator": item["locator"],
            "published_at": item["published_at"],
            "retrieved_at": item["retrieved_at"],
            "content_hash": content_hash(path),
        })

    input_hashes = {}
    for name, rel in spec["snapshot"]["input_hashes"].items():
        path = ROOT / rel
        if not path.exists():
            fail(f"입력 해시 {name}: 파일이 없다 — {rel}")
        input_hashes[name] = content_hash(path)

    expected = spec["expected_return"]
    return {
        "schema_version": spec.get("schema_version", "1.1.0"),
        "analysis_id": spec["analysis_id"],
        "instrument_id": instrument,
        "company_id": spec.get("company_id", instrument),
        "as_of": spec["as_of"],
        "snapshot": {
            "snapshot_id": spec["snapshot"]["snapshot_id"],
            "created_at": spec["snapshot"]["created_at"],
            "source_ids": [s["source_id"] for s in sources],
            "input_hashes": input_hashes,
        },
        "sources": sources,
        "claims": spec["claims"],
        "thesis": spec["thesis"],
        "counter_thesis": spec["counter_thesis"],
        "expected_return": {
            "horizon_months": expected["horizon_months"],
            "required_return_pct": expected["required_return_pct"],
            "methodology": expected["methodology"],
            "calculation_id": calculation["calculation_id"],
            "probability_weighted_pct": round(weighted, 4),
            "scenarios": {
                name: {
                    "return_pct": outputs[name]["return_pct"],
                    "probability": probabilities[name],
                    "claim_ids": expected["scenario_claim_ids"][name],
                }
                for name in SCENARIOS
            },
        },
        "alternatives": [
            {**alt, "snapshot_id": spec["snapshot"]["snapshot_id"]}
            for alt in spec["alternatives"]
        ],
    }


def main() -> int:
    p = argparse.ArgumentParser(description="스펙 → 검증된 분석 계약")
    p.add_argument("--spec", required=True)
    p.add_argument("--calculation", help="CALC-xxxx. 생략하면 종목으로 찾는다")
    p.add_argument("--out", help="기본: knowledge/research/analysis-{종목}-{날짜}.json")
    a = p.parse_args()

    spec = load_json(a.spec)
    try:
        calculation = resolve_calculation(spec["instrument_id"], a.calculation)
    except ContractError as exc:
        for message in exc.errors:
            print(f"INVALID: {message}", file=sys.stderr)
        return 1

    analysis = build(spec, calculation)

    errors = validate_analysis(analysis)
    if errors:
        for message in errors:
            print(f"INVALID: {message}", file=sys.stderr)
        return 1

    out = Path(a.out) if a.out else (
        ROOT / "knowledge/research"
        / f"analysis-{spec['instrument_id'].lower()}-{spec['as_of'][:10].replace('-', '')}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(analysis, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")

    weighted = analysis["expected_return"]["probability_weighted_pct"]
    required = analysis["expected_return"]["required_return_pct"]
    print(f"확률가중 기대수익 {weighted:.2f}%  (요구 {required:.1f}%)  "
          f"→ {'통과' if weighted >= required else '미달'}")
    for name in SCENARIOS:
        s = analysis["expected_return"]["scenarios"][name]
        print(f"  {name:<5}{s['return_pct']:>8.1f}%  확률 {s['probability']:.0%}")
    print(f"계산 산출물 {analysis['expected_return']['calculation_id']}")
    print(f"작성: {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
