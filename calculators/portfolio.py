#!/usr/bin/env python3
"""ETF를 관통한 실효 종목 노출을 계산한다.

반복 산술은 로컬에서 결정론적으로 수행하고 모델에는 계산 결과만 제공한다.

입력:
  holdings.csv      asset_id,name,market,asset_type,value_krw[,fx_exposure][,note]
  constituents.csv  etf_id,holding_id,holding_name,weight_pct[,market][,sector][,as_of]

표준 라이브러리만 사용한다.
"""

import argparse
import csv
import hashlib
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, NoReturn

from cio.display import display_width

SCHEMA_VERSION = "1.0.0"
COMPONENT = "portfolio-xray/lookthrough"

MARKET_TO_CURRENCY = {"KR": "KRW", "US": "USD"}
PASSTHROUGH_TYPES = {"STOCK", "CASH", "BOND", "REIT"}


def fail(msg) -> NoReturn:
    print(f"오류: {msg}", file=sys.stderr)
    sys.exit(1)


def read_csv(path, required):
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            # '#' 로 시작하는 줄은 주석이다. knowledge/ 의 CSV는 출처와 기준일을 머리말로 갖는다.
            lines = [ln for ln in f if not ln.lstrip().startswith("#")]
        rows = [r for r in csv.DictReader(lines) if any(v.strip() for v in r.values() if v)]
    except FileNotFoundError:
        fail(f"파일을 찾을 수 없다: {path}")
    if not rows:
        fail(f"{path} 에 데이터 행이 없다")
    missing = required - set(rows[0].keys())
    if missing:
        fail(f"{path} 에 필수 열이 없다: {', '.join(sorted(missing))}")
    return rows


def to_number(raw, path, field, line):
    try:
        number = float(str(raw).replace(",", "").strip())
    except (ValueError, AttributeError):
        fail(f"{path} {line}행 {field}: 숫자로 읽을 수 없다 ({raw!r})")
    if not math.isfinite(number):
        fail(f"{path} {line}행 {field}: 유한한 숫자가 아니다 ({raw!r})")
    return number


def calculation_id(holdings_rows, constituent_rows):
    """동일 입력이 동일 ID를 만들도록 정규화 후 해시한다."""
    payload = json.dumps(
        {"holdings": holdings_rows, "constituents": constituent_rows},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def load_holdings(path):
    rows = read_csv(path, {"asset_id", "name", "market", "asset_type", "value_krw"})
    holdings = []
    seen = set()
    for i, r in enumerate(rows, start=2):
        asset_id = r["asset_id"].strip()
        if not asset_id:
            fail(f"{path} {i}행: asset_id가 비어 있다")
        if asset_id in seen:
            fail(f"{path} {i}행: 중복 asset_id {asset_id}")
        seen.add(asset_id)
        value = to_number(r["value_krw"], path, "value_krw", i)
        if value < 0:
            fail(f"{path} {i}행: 평가금액이 음수다. 공매도·차입은 이 계산이 지원하지 않는다")
        holdings.append(
            {
                "asset_id": asset_id,
                "name": r["name"].strip(),
                "market": r["market"].strip().upper(),
                "asset_type": r["asset_type"].strip().upper(),
                "value_krw": value,
                "fx_exposure": (r.get("fx_exposure") or "").strip().upper() or None,
                "sector": (r.get("sector") or "").strip() or None,
                "note": (r.get("note") or "").strip(),
            }
        )
    return holdings


def load_constituents(paths):
    """ETF별 구성종목 파일을 여러 개 받는다. knowledge/etfs/ 아래에 ETF마다 따로 있다."""
    by_etf = defaultdict(list)
    for path in paths or []:
        _load_constituent_file(path, by_etf)
    return dict(by_etf)


def _load_constituent_file(path, by_etf):
    rows = read_csv(path, {"etf_id", "holding_id", "holding_name", "weight_pct"})
    for i, r in enumerate(rows, start=2):
        weight = to_number(r["weight_pct"], path, "weight_pct", i)
        if not 0 <= weight <= 100:
            fail(f"{path} {i}행: weight_pct 는 0~100 이어야 한다 ({weight})")
        by_etf[r["etf_id"].strip()].append(
            {
                "holding_id": r["holding_id"].strip(),
                "holding_name": r["holding_name"].strip(),
                "weight_pct": weight,
                "market": (r.get("market") or "").strip().upper() or None,
                "sector": (r.get("sector") or "").strip() or None,
                "asset_type": (r.get("asset_type") or "").strip().upper() or "EQUITY",
                "as_of": (r.get("as_of") or "").strip() or None,
            }
        )


def resolve_currency(market, explicit):
    if explicit:
        return explicit, "EXPLICIT"
    if market in MARKET_TO_CURRENCY:
        return MARKET_TO_CURRENCY[market], "INFERRED_FROM_MARKET"
    return "UNKNOWN", "UNRESOLVED"


def analyze(holdings, constituents, limits):
    total = sum(h["value_krw"] for h in holdings)
    if total <= 0:
        fail("포트폴리오 평가금액 합계가 0 이하다")

    exposure: defaultdict[str, dict[str, Any]] = defaultdict(
        lambda: {"name": "", "asset_type": None, "value_krw": 0.0, "paths": []}
    )
    currency = defaultdict(float)
    sector = defaultdict(float)
    fx_inferred = []
    etf_reports = []
    unresolved_etfs = []

    for h in holdings:
        weight = h["value_krw"] / total
        h["portfolio_weight"] = weight

        if h["asset_type"] != "ETF":
            if h["asset_type"] not in PASSTHROUGH_TYPES:
                unresolved_etfs.append(
                    {"asset_id": h["asset_id"], "name": h["name"],
                     "reason": f"알 수 없는 asset_type: {h['asset_type']}"}
                )
            e = exposure[h["asset_id"]]
            e["name"] = h["name"]
            e["asset_type"] = h["asset_type"]
            e["value_krw"] += h["value_krw"]
            e["paths"].append({"via": "DIRECT", "value_krw": h["value_krw"]})

            cur, how = resolve_currency(h["market"], h["fx_exposure"])
            currency[cur] += h["value_krw"]
            if how == "INFERRED_FROM_MARKET":
                fx_inferred.append(h["asset_id"])
            if h["sector"]:
                sector[h["sector"]] += h["value_krw"]
            continue

        members = constituents.get(h["asset_id"])
        if not members:
            unresolved_etfs.append(
                {"asset_id": h["asset_id"], "name": h["name"],
                 "reason": "구성종목 데이터 없음"}
            )
            exposure[f"UNCLASSIFIED::{h['asset_id']}"].update(
                name=f"{h['name']} (구성 미확보)", asset_type="ETF",
                value_krw=h["value_krw"]
            )
            cur, how = resolve_currency(h["market"], h["fx_exposure"])
            currency[cur] += h["value_krw"]
            if how == "INFERRED_FROM_MARKET":
                fx_inferred.append(h["asset_id"])
            continue

        coverage = sum(m["weight_pct"] for m in members)
        if coverage > 100.5:
            fail(f"{h['asset_id']} 구성종목 비중 합계가 {coverage:.1f}% 로 100%를 넘는다")

        for m in members:
            share = m["weight_pct"] / 100.0
            value = h["value_krw"] * share
            e = exposure[m["holding_id"]]
            e["name"] = e["name"] or m["holding_name"]
            e["asset_type"] = e["asset_type"] or m["asset_type"]
            e["value_krw"] += value
            e["paths"].append(
                {"via": h["asset_id"], "via_name": h["name"],
                 "weight_in_etf_pct": m["weight_pct"], "value_krw": value}
            )

            member_market = m["market"] or h["market"]
            cur, how = resolve_currency(member_market, h["fx_exposure"])
            currency[cur] += value
            if how == "INFERRED_FROM_MARKET" and not m["market"]:
                fx_inferred.append(f"{h['asset_id']}>{m['holding_id']}")
            if m["sector"]:
                sector[m["sector"]] += value

        uncovered = max(0.0, 100.0 - coverage) / 100.0 * h["value_krw"]
        if uncovered > 0:
            exposure[f"UNCLASSIFIED::{h['asset_id']}"].update(
                name=f"{h['name']} 잔여분", asset_type="ETF",
                value_krw=uncovered
            )
            cur, _ = resolve_currency(h["market"], h["fx_exposure"])
            currency[cur] += uncovered

        etf_reports.append(
            {
                "etf_id": h["asset_id"], "name": h["name"],
                "portfolio_weight_pct": round(weight * 100, 2),
                "coverage_pct": round(coverage, 2),
                "constituents_used": len(members),
                "as_of": next((m["as_of"] for m in members if m["as_of"]), None),
            }
        )

    return build_result(
        holdings, exposure, currency, sector, total,
        etf_reports, unresolved_etfs, sorted(set(fx_inferred)), limits,
    )


def build_result(holdings, exposure, currency, sector, total,
                 etf_reports, unresolved_etfs, fx_inferred, limits):
    nominal = {h["asset_id"]: h["value_krw"] for h in holdings if h["asset_type"] != "ETF"}
    equity_total = sum(
        h["value_krw"] for h in holdings if h["asset_type"] in {"STOCK", "ETF", "REIT"}
    )
    risk_total = sum(
        h["value_krw"] for h in holdings
        if h["asset_type"] not in {"CASH", "CASH_EQUIVALENT", "BOND"}
    )

    rows = []
    for asset_id, e in exposure.items():
        eff_pct = e["value_krw"] / total * 100
        nom_pct = nominal.get(asset_id, 0.0) / total * 100
        is_equity = e["asset_type"] in {"STOCK", "ETF", "REIT", "EQUITY"}
        is_risk = e["asset_type"] not in {
            "CASH", "CASH_EQUIVALENT", "BOND", "UNCLASSIFIED"
        }
        rows.append(
            {
                "asset_id": asset_id,
                "name": e["name"],
                "asset_type": e["asset_type"] or "UNKNOWN",
                "effective_pct": round(eff_pct, 3),
                "total_assets_pct": round(eff_pct, 3),
                "equity_sleeve_pct": (
                    round(e["value_krw"] / equity_total * 100, 3)
                    if is_equity and equity_total > 0 else None
                ),
                "risk_assets_pct": (
                    round(e["value_krw"] / risk_total * 100, 3)
                    if is_risk and risk_total > 0 else None
                ),
                "nominal_pct": round(nom_pct, 3),
                "hidden_pct": round(eff_pct - nom_pct, 3),
                "value_krw": round(e["value_krw"]),
                "path_count": len(e["paths"]) or 1,
                "paths": e["paths"],
            }
        )
    rows.sort(key=lambda r: -r["effective_pct"])

    unclassified_pct = sum(
        r["effective_pct"] for r in rows if r["asset_id"].startswith("UNCLASSIFIED::")
    )
    real = [r for r in rows if not r["asset_id"].startswith("UNCLASSIFIED::")]
    concentration_positions = [
        r for r in real if r["asset_type"] not in {"CASH", "CASH_EQUIVALENT"}
    ]
    top10_pct = sum(r["effective_pct"] for r in concentration_positions[:10])

    warnings = []
    for r in concentration_positions:
        if limits.get("single") is not None and r["effective_pct"] > limits["single"]:
            warnings.append(
                {"code": "SINGLE_NAME_CONCENTRATION", "asset_id": r["asset_id"],
                 "name": r["name"], "value_pct": r["effective_pct"],
                 "limit_pct": limits["single"], "path_count": r["path_count"]}
            )
    if limits.get("top10") is not None and top10_pct > limits["top10"]:
        warnings.append({"code": "TOP10_CONCENTRATION",
                         "value_pct": round(top10_pct, 2), "limit_pct": limits["top10"]})
    for cur, val in currency.items():
        pct = val / total * 100
        if (limits.get("currency") is not None and cur != "UNKNOWN"
                and pct > limits["currency"]):
            warnings.append({"code": "CURRENCY_CONCENTRATION", "currency": cur,
                             "value_pct": round(pct, 2), "limit_pct": limits["currency"]})
    for sec, val in sector.items():
        pct = val / total * 100
        if limits.get("sector") is not None and pct > limits["sector"]:
            warnings.append({"code": "SECTOR_CONCENTRATION", "sector": sec,
                             "value_pct": round(pct, 2), "limit_pct": limits["sector"]})
    if (limits.get("unclassified") is not None
            and unclassified_pct > limits["unclassified"]):
        warnings.append({"code": "LOW_COVERAGE",
                         "value_pct": round(unclassified_pct, 2),
                         "limit_pct": limits["unclassified"],
                         "detail": "미분류 비중이 커서 상위 순위를 단정할 수 없다"})

    hidden = sorted(
        [r for r in real if r["hidden_pct"] > 0.5 and r["path_count"] > 1],
        key=lambda r: -r["hidden_pct"],
    )

    return {
        "total_value_krw": round(total),
        "base_currency": "KRW",
        "denominators_krw": {
            "total_assets": round(total),
            "equity_sleeve": round(equity_total),
            "risk_assets": round(risk_total),
        },
        "denominator_definitions": {
            "total_assets": "현금을 포함한 전체 평가자산",
            "equity_sleeve": "STOCK·ETF·REIT 평가자산",
            "risk_assets": "CASH·CASH_EQUIVALENT·BOND를 제외한 평가자산",
        },
        "asset_count": len(holdings),
        "effective_position_count": len(real),
        "exposures": rows,
        "hidden_exposures": hidden,
        "top10_effective_pct": round(top10_pct, 2),
        "unclassified_pct": round(unclassified_pct, 3),
        "currency_exposure": {
            k: round(v / total * 100, 2) for k, v in sorted(currency.items(), key=lambda x: -x[1])
        },
        "sector_exposure": {
            k: round(v / total * 100, 2) for k, v in sorted(sector.items(), key=lambda x: -x[1])
        },
        "sector_classified_pct": round(sum(sector.values()) / total * 100, 2),
        "etf_coverage": etf_reports,
        "unresolved": unresolved_etfs,
        "fx_inferred_from_market": fx_inferred,
        "warnings": warnings,
        "limits_applied": limits,
    }


def render_text(result, meta):
    out = []
    w = out.append
    w("=" * 68)
    w("포트폴리오 X-ray — 실효 노출")
    w("=" * 68)
    w(f"기준시각      : {meta['as_of']}")
    w(f"계산 ID       : {meta['calculation_id']}  (동일 입력 = 동일 ID)")
    w(f"평가금액 합계 : {result['total_value_krw']:,} KRW")
    w(f"주식 슬리브   : {result['denominators_krw']['equity_sleeve']:,} KRW")
    w(f"위험자산      : {result['denominators_krw']['risk_assets']:,} KRW")
    w(f"보유 자산 수  : {result['asset_count']}개")
    w(f"실효 종목 수  : {result['effective_position_count']}개")
    w("")

    w("[ 실효 노출 상위 15 ]   (※ = 관통 못 한 ETF 잔여분, 개별 종목 아님)")
    w(f"{'종목':<29}{'총자산%':>9}{'주식%':>9}{'위험%':>9}{'경로':>6}")
    w("-" * 72)
    for r in result["exposures"][:15]:
        unclassified = r["asset_id"].startswith("UNCLASSIFIED::")
        label = ("※ " if unclassified else "") + (r["name"] or r["asset_id"])
        if len(label) > 32:
            label = label[:31] + "…"
        pad = 29 - display_width(label)
        eq = "-" if r["equity_sleeve_pct"] is None else f"{r['equity_sleeve_pct']:.2f}"
        risk = "-" if r["risk_assets_pct"] is None else f"{r['risk_assets_pct']:.2f}"
        w(f"{label}{' ' * max(1, pad)}{r['total_assets_pct']:>9.2f}"
          f"{eq:>9}{risk:>9}"
          f"{'-' if unclassified else r['path_count']:>6}")
    w("")

    if result["hidden_exposures"]:
        w("[ 여러 경로로 겹친 노출 — 몰랐을 가능성이 높은 곳 ]")
        for r in result["hidden_exposures"][:6]:
            w(f"  {r['name'] or r['asset_id']}: 명목 {r['nominal_pct']:.2f}% "
              f"→ 실효 {r['effective_pct']:.2f}%")
            for p in r["paths"]:
                if p["via"] == "DIRECT":
                    w(f"      직접 보유            {p['value_krw']:>14,.0f} KRW")
                else:
                    w(f"      {p['via_name'][:18]:<18} 내 {p['weight_in_etf_pct']:>5.2f}%"
                      f"  {p['value_krw']:>14,.0f} KRW")
        w("")

    w("[ 통화 노출 ]")
    for cur, pct in result["currency_exposure"].items():
        w(f"  {cur:<8}{pct:>7.2f}%")
    if result["fx_inferred_from_market"]:
        w(f"  ※ 시장 코드로 추정한 항목 {len(result['fx_inferred_from_market'])}건 — "
          f"환헤지 여부 확인 필요")
    w("")

    if result["sector_exposure"]:
        w("[ 섹터 노출 ]")
        for sec, pct in result["sector_exposure"].items():
            w(f"  {sec:<20}{pct:>7.2f}%")
        w(f"  ※ 섹터가 기재된 비중 {result['sector_classified_pct']:.2f}% 기준 — "
          f"나머지는 미분류이므로 위 값은 최소치다")
        w("")

    if result["etf_coverage"]:
        w("[ ETF 관통 커버리지 ]")
        for e in result["etf_coverage"]:
            stamp = f"  기준일 {e['as_of']}" if e["as_of"] else "  기준일 미기재"
            w(f"  {e['name'][:24]:<26} 커버리지 {e['coverage_pct']:>6.2f}% "
              f"({e['constituents_used']}종목){stamp}")
        w("")

    if result["unresolved"]:
        w("[ 관통하지 못한 자산 ]")
        for u in result["unresolved"]:
            w(f"  {u['name']} ({u['asset_id']}): {u['reason']}")
        w("")

    w("[ 한도 검토 ]")
    if not any(v is not None for v in result["limits_applied"].values()):
        w("  명시적 한도가 없어 판정하지 않음")
    elif not result["warnings"]:
        w("  검토 기준선을 넘는 항목 없음")
    for warn in result["warnings"]:
        code = warn["code"]
        if code == "SINGLE_NAME_CONCENTRATION":
            w(f"  ! {warn['name']} 실효 {warn['value_pct']:.2f}% "
              f"(기준 {warn['limit_pct']}%, 경로 {warn['path_count']}개)")
        elif code == "TOP10_CONCENTRATION":
            w(f"  ! 상위 10종목 합계 {warn['value_pct']:.2f}% (기준 {warn['limit_pct']}%)")
        elif code == "CURRENCY_CONCENTRATION":
            w(f"  ! {warn['currency']} 노출 {warn['value_pct']:.2f}% (기준 {warn['limit_pct']}%)")
        elif code == "SECTOR_CONCENTRATION":
            w(f"  ! {warn['sector']} 섹터 {warn['value_pct']:.2f}% (기준 {warn['limit_pct']}%)")
        elif code == "LOW_COVERAGE":
            w(f"  ! 미분류 {warn['value_pct']:.2f}% — {warn['detail']}")
    w("")
    w("한도 초과가 곧 잘못은 아니다. 의도한 집중인지 모르고 생긴 집중인지 구분한다.")
    w("이 출력은 계산 결과이며 매수·매도 권유가 아니다.")
    return "\n".join(out)


def main():
    p = argparse.ArgumentParser(description="ETF 관통 실효 노출 계산")
    p.add_argument("--holdings", required=True, help="보유 내역 CSV")
    p.add_argument("--constituents", nargs="*", default=[],
                   help="ETF 구성종목 CSV (여러 개 가능: knowledge/etfs/*/holdings-*.csv)")
    p.add_argument("--json", action="store_true", help="JSON으로 출력")
    p.add_argument("--as-of", help="기준시각 (기본: 현재 UTC)")
    p.add_argument("--single-limit", type=float)
    p.add_argument("--top10-limit", type=float)
    p.add_argument("--sector-limit", type=float)
    p.add_argument("--currency-limit", type=float)
    p.add_argument("--unclassified-limit", type=float)
    args = p.parse_args()

    holdings = load_holdings(args.holdings)
    constituents = load_constituents(args.constituents)

    limits = {
        "single": args.single_limit,
        "top10": args.top10_limit,
        "sector": args.sector_limit,
        "currency": args.currency_limit,
        "unclassified": args.unclassified_limit,
    }
    result = analyze(holdings, constituents, limits)
    meta = {
        "schema_version": SCHEMA_VERSION,
        "component": COMPONENT,
        "as_of": args.as_of or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "calculation_id": calculation_id(holdings, constituents),
        "inputs": {"holdings": args.holdings, "constituents": args.constituents},
        "limits": limits,
    }

    if args.json:
        print(json.dumps({"metadata": meta, "result": result}, ensure_ascii=False, indent=2))
    else:
        print(render_text(result, meta))


if __name__ == "__main__":
    main()
