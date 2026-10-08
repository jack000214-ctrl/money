#!/usr/bin/env python3
"""ETF 구성종목 들여다보기(look-through)의 결정론적 산술.

`collectors/etf_constituents.py` 가 모은 재료에서 세 가지를 낸다.

1. **가중 배수** — 흑자 종목 묶음의 forward·trailing PER(비중 가중 조화평균),
   적자 종목의 비중, 전체 이익수익률.
2. **수익률 분해** — ETF 수익률을 구성종목의 현지통화 수익률과 환율로 나눈다.
3. **비교** — 이전 재료와 견준 forward 이익의 변화(같은 주가 기준 PER).

조화평균을 쓰는 이유: 묶음의 PER 은 `시가총액 합 / 이익 합` 이고, 비중이
시가총액 몫이므로 이익 몫은 `비중 / PER` 이다. 산술평균은 PER 200배짜리 한
종목이 묶음 전체를 비싸 보이게 만든다.

**이 계산기는 판정을 내지 않는다.** 몇 배가 싼지, 환율 몫이 크면 무엇을 해야
하는지는 여기 없다. 문턱은 모델이 세우고(`--at-forward-pe`) 계산기는 그 문턱을
가격으로 옮길 뿐이다.

사용:
    python3.13 -m calculators.etf_lookthrough 0053L0
    python3.13 -m calculators.etf_lookthrough 0053L0 --at-forward-pe 22 20 --price 8245
    python3.13 -m calculators.etf_lookthrough 0053L0 --compare knowledge/etfs/0053L0/constituents-2026-10-09.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from cio.paths import KNOWLEDGE_DIR, ROOT

ETF_DIR = KNOWLEDGE_DIR / "etfs"
PACKET_HOLDINGS = 10

NOT_A_JUDGMENT = (
    "전부 결정론적 산술이다. 몇 배가 적정한지, 환율 몫이 크면 사야 하는지 "
    "같은 값은 여기 없다. 문턱은 모델이 세우고 이 계산기는 대조만 한다"
)


def _positive(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and value > 0 else None


def _round(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)


def _harmonic(pairs: list[tuple[float, float]]) -> tuple[float | None, float]:
    """(비중, 배수) 쌍의 비중 가중 조화평균과 그 비중 합."""
    weight = sum(w for w, _ in pairs)
    inverse = sum(w / multiple for w, multiple in pairs)
    return (weight / inverse if inverse > 0 else None), weight


def earnings_yield(item: dict[str, Any]) -> tuple[float | None, str]:
    """한 종목의 이익수익률(이익/시가총액)과 그 근거.

    forward PER 이 있으면 그 역수다. 없으면 `순이익률 / PS` 로 낸다 — 둘 다
    비율이라 통화가 섞이지 않는다(홍콩 상장 본토 기업은 실적이 위안, 시가총액이
    홍콩달러다). 적자 종목은 여기서 음수가 된다.
    """
    valuation = item.get("valuation") or {}
    forward = _positive(valuation.get("forward_pe"))
    if forward:
        return 1 / forward, "FORWARD_PE"
    margin = valuation.get("profit_margin_pct")
    sales_multiple = _positive(valuation.get("ps"))
    if isinstance(margin, (int, float)) and sales_multiple:
        return margin / 100 / sales_multiple, "TRAILING_MARGIN_OVER_PS"
    return None, "NO_DATA"


def valuation(holdings: list[dict[str, Any]]) -> dict[str, Any]:
    total = sum(item["weight_pct"] for item in holdings)
    forward_pairs, trailing_pairs, sales_pairs = [], [], []
    loss_weight = no_forward_weight = no_data_weight = 0.0
    yield_sum = yield_weight = 0.0
    for item in holdings:
        weight = item["weight_pct"]
        figures = item.get("valuation") or {}
        forward = _positive(figures.get("forward_pe"))
        trailing = _positive(figures.get("trailing_pe"))
        sales = _positive(figures.get("ps"))
        if forward:
            forward_pairs.append((weight, forward))
        if trailing:
            trailing_pairs.append((weight, trailing))
        if sales:
            sales_pairs.append((weight, sales))
        value, basis = earnings_yield(item)
        if value is None:
            no_data_weight += weight
            continue
        yield_sum += weight * value
        yield_weight += weight
        if basis != "FORWARD_PE":
            if value < 0:
                loss_weight += weight
            else:
                no_forward_weight += weight
    forward_pe, forward_weight = _harmonic(forward_pairs)
    trailing_pe, trailing_weight = _harmonic(trailing_pairs)
    sales_multiple, sales_weight = _harmonic(sales_pairs)
    overall = yield_sum / yield_weight if yield_weight else None
    return {
        "weight_total_pct": _round(total),
        "forward_pe": _round(forward_pe),
        "forward_pe_weight_pct": _round(forward_weight),
        "trailing_pe": _round(trailing_pe),
        "trailing_pe_weight_pct": _round(trailing_weight),
        "ps": _round(sales_multiple),
        "ps_weight_pct": _round(sales_weight),
        "loss_making_weight_pct": _round(loss_weight),
        "profitable_without_forward_weight_pct": _round(no_forward_weight),
        "no_data_weight_pct": _round(no_data_weight),
        "earnings_yield_all_pct": _round(overall * 100 if overall is not None else None),
        "earnings_yield_weight_pct": _round(yield_weight),
    }


def exposure(holdings: list[dict[str, Any]]) -> dict[str, Any]:
    by_market: dict[str, float] = {}
    by_currency: dict[str, float] = {}
    for item in holdings:
        market = item.get("market") or "UNKNOWN"
        by_market[market] = by_market.get(market, 0.0) + item["weight_pct"]
        currency = (item.get("price") or {}).get("currency") or "UNKNOWN"
        by_currency[currency] = by_currency.get(currency, 0.0) + item["weight_pct"]
    order = lambda mapping: {  # noqa: E731
        key: round(value, 2)
        for key, value in sorted(mapping.items(), key=lambda pair: -pair[1])
    }
    return {"by_market_pct": order(by_market), "by_currency_pct": order(by_currency)}


def attribution(data: dict[str, Any]) -> dict[str, Any]:
    """창마다 ETF 수익률을 종목(현지통화)·환율로 나눈다.

    `holdings_in_quote_pct` 는 종목별로 `(1+현지수익률)(1+환율수익률)-1` 을 낸
    뒤 **현재 비중**으로 가중한 값이다. 과거의 비중이 아니므로 근사이며, 비중
    변화·정기변경·보수·장 마감 시차가 `unexplained_pp` 에 남는다. 그 잔차를
    0으로 맞추지 않는다 — 맞추려면 어느 한쪽 숫자를 지어내야 한다.
    """
    fx = data.get("fx") or {}
    etf_returns = (data.get("etf") or {}).get("return_pct") or {}
    windows: dict[str, Any] = {}
    for name in (data.get("windows_days") or {}):
        weight = local = currency_part = combined = 0.0
        for item in data.get("holdings") or []:
            price = item.get("price") or {}
            item_return = (price.get("return_pct") or {}).get(name)
            fx_return = ((fx.get(price.get("currency")) or {}).get("return_pct") or {}).get(name)
            if item_return is None or fx_return is None:
                continue
            w = item["weight_pct"]
            weight += w
            local += w * item_return
            currency_part += w * fx_return
            combined += w * ((1 + item_return / 100) * (1 + fx_return / 100) - 1) * 100
        if not weight:
            windows[name] = None
            continue
        in_quote = combined / weight
        etf_return = etf_returns.get(name)
        windows[name] = {
            "etf_pct": etf_return,
            "holdings_local_pct": _round(local / weight),
            "fx_pct": _round(currency_part / weight),
            "holdings_in_quote_pct": _round(in_quote),
            "unexplained_pp": _round(etf_return - in_quote) if etf_return is not None else None,
            "covered_weight_pct": _round(weight),
        }
    return windows


def compare(current: dict[str, Any], reference: dict[str, Any]) -> dict[str, Any]:
    """기준 재료 이후 forward 이익이 얼마나 바뀌었나.

    종목별 내재 forward EPS 는 `종가 / forward PER` 이다. 기준 시점의 비중과
    주가를 고정하고 이익만 새 값으로 바꾼 묶음 PER 을 낸다 — "그때 주가로
    환산한 지금의 forward PER" 이다. 두 재료에 모두 있고 양쪽 다 forward PER 이
    양수인 종목만 센다.
    """
    def table(data: dict[str, Any]) -> dict[str, tuple[float, float]]:
        rows = {}
        for item in data.get("holdings") or []:
            forward = _positive((item.get("valuation") or {}).get("forward_pe"))
            close = _positive((item.get("price") or {}).get("close"))
            if forward and close:
                rows[item["holding_id"]] = (close / forward, forward)
        return rows

    now, then = table(current), table(reference)
    reference_weights = {
        item["holding_id"]: item["weight_pct"] for item in reference.get("holdings") or []
    }
    matched = sorted(set(now) & set(then))
    weight = sum(reference_weights[key] for key in matched)
    earnings_then = sum(reference_weights[key] / then[key][1] for key in matched)
    earnings_now = sum(
        reference_weights[key] * (now[key][0] / then[key][0]) / then[key][1]
        for key in matched
    )
    return {
        "reference_collected_at": reference.get("collected_at"),
        "reference_holdings_as_of": reference.get("holdings_as_of"),
        "matched_weight_pct": _round(weight),
        "reference_forward_pe": _round(weight / earnings_then if earnings_then else None),
        "forward_pe_at_reference_prices": _round(weight / earnings_now if earnings_now else None),
        "forward_earnings_change_pct": _round(
            (earnings_now / earnings_then - 1) * 100 if earnings_then else None
        ),
        "dropped_since_reference": sorted(set(reference_weights) - {
            item["holding_id"] for item in current.get("holdings") or []
        }),
        "added_since_reference": sorted({
            item["holding_id"] for item in current.get("holdings") or []
        } - set(reference_weights)),
        "_role": (
            "기준 재료의 비중·주가를 고정하고 종목별 내재 forward EPS(종가÷forward "
            "PER)만 새 값으로 바꾼 산술이다. 추정 기준연도가 넘어가면 이익이 "
            "늘어난 것처럼 보인다 — 변화의 원인은 원문에서 확인한다"
        ),
    }


def price_at_forward_pe(
    data: dict[str, Any], etf_price: float, targets: list[float]
) -> list[dict[str, Any]]:
    """모델이 준 forward PER 문턱을 ETF 가격으로 옮긴다.

    가정은 하나다 — 추정이익, forward PER 이 없는 종목의 가격, 환율이 그대로다.
    그 가정 아래에서 흑자 묶음만 배수가 `target/현재` 만큼 움직인다.
    """
    figures = valuation(data.get("holdings") or [])
    forward, weight, total = (
        figures["forward_pe"], figures["forward_pe_weight_pct"], figures["weight_total_pct"],
    )
    rows = []
    for target in targets:
        if not forward or not total:
            rows.append({"target_forward_pe": target, "etf_price": None})
            continue
        change = (weight / total) * (target / forward - 1)
        rows.append({
            "target_forward_pe": target,
            "etf_price": round(etf_price * (1 + change), 2),
            "vs_input_price_pct": round(change * 100, 2),
        })
    return rows


def summarize(data: dict[str, Any], *, top: int = PACKET_HOLDINGS) -> dict[str, Any]:
    """packet 에 싣는 압축본."""
    holdings = data.get("holdings") or []
    rows = []
    for item in holdings[:top]:
        price = item.get("price") or {}
        figures = item.get("valuation") or {}
        returns = price.get("return_pct") or {}
        rows.append({
            "id": item["holding_id"],
            "w": item["weight_pct"],
            "ccy": price.get("currency"),
            "px_as_of": price.get("as_of"),
            "ret_3m": returns.get("3m"),
            "ret_12m": returns.get("12m"),
            "dd_12m": price.get("drawdown_12m_pct"),
            "fwd_pe": figures.get("forward_pe"),
            "ps": figures.get("ps"),
            "margin": figures.get("profit_margin_pct"),
        })
    price_dates = sorted({
        (item.get("price") or {}).get("as_of")
        for item in holdings if (item.get("price") or {}).get("as_of")
    })
    return {
        "collected_at": data.get("collected_at"),
        "holdings_as_of": data.get("holdings_as_of"),
        "etf_price_as_of": (data.get("etf") or {}).get("as_of"),
        "constituent_price_as_of": (
            {"earliest": price_dates[0], "latest": price_dates[-1]} if price_dates else None
        ),
        "exposure": exposure(holdings),
        "valuation": {
            **valuation(holdings),
            "source": (data.get("valuation_source") or {}).get("provider"),
            "source_grade": (data.get("valuation_source") or {}).get("grade"),
        },
        "return_attribution": attribution(data),
        "fx": {
            currency: {"basis": row.get("basis"), "as_of": row.get("as_of"), "rate": row.get("rate")}
            for currency, row in (data.get("fx") or {}).items()
        },
        "top_holdings": rows,
        "unresolved_count": len(data.get("unresolved") or []),
        "_role": (
            "구성종목을 들여다본 산술이며 판정이 아니다. valuation 의 forward_pe 는 "
            "forward PER 이 양수인 종목만의 비중 가중 조화평균이고 그 비중이 "
            "forward_pe_weight_pct 다 — 적자·무자료 종목은 이 배수에 없으므로 "
            "loss_making_weight_pct 와 같이 읽는다. earnings_yield_all_pct 는 적자를 "
            "음수로 넣은 전체 이익수익률이다. 배수는 2차 출처이고 forward 추정의 "
            "기준연도·표본을 알 수 없다. return_attribution 은 ETF 수익률을 종목의 "
            "현지통화 수익률과 환율로 나눈 것으로, **현재 비중을 과거에 적용한 "
            "근사**라 unexplained_pp 가 남는다. ETF 와 구성종목의 가격 기준일이 "
            "다르면(휴장·시차) 그 차이도 잔차에 들어간다. 창은 달력 일수"
            "(1m=30·3m=91·6m=182·12m=365일)라 price.returns_pct 의 거래일 창과 "
            "값이 다를 수 있다"
        ),
    }


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def latest_path(asset_id: str) -> Path | None:
    paths = sorted((ETF_DIR / asset_id).glob("constituents-*.json"))
    return paths[-1] if paths else None


def render(result: dict[str, Any]) -> None:
    figures = result["valuation"]
    print(f"{result['asset_id']} look-through · 구성 기준일 {result['holdings_as_of']} · 수집 {result['collected_at']}")
    print("-" * 76)
    print(
        f"forward PER {figures['forward_pe']}배 (비중 {figures['forward_pe_weight_pct']}%) · "
        f"trailing PER {figures['trailing_pe']}배 (비중 {figures['trailing_pe_weight_pct']}%)"
    )
    print(
        f"적자 비중 {figures['loss_making_weight_pct']}% · 무자료 {figures['no_data_weight_pct']}% · "
        f"전체 이익수익률 {figures['earnings_yield_all_pct']}% · PS {figures['ps']}배"
    )
    print("시장 " + json.dumps(result["exposure"]["by_market_pct"], ensure_ascii=False)
          + " · 통화 " + json.dumps(result["exposure"]["by_currency_pct"], ensure_ascii=False))
    print("-" * 76)
    print(f"{'창':<4} {'ETF':>8} {'종목(현지)':>10} {'환율':>8} {'종목(환산)':>10} {'잔차pp':>8} {'비중':>8}")
    for name, row in result["return_attribution"].items():
        if not row:
            print(f"{name:<4} 계산 불가")
            continue
        cells = [row["etf_pct"], row["holdings_local_pct"], row["fx_pct"],
                 row["holdings_in_quote_pct"], row["unexplained_pp"], row["covered_weight_pct"]]
        print(f"{name:<4} " + " ".join(
            f"{'-' if value is None else format(value, '.2f'):>9}" for value in cells
        ))
    for row in result.get("at_forward_pe") or []:
        print(
            f"forward PER {row['target_forward_pe']}배 → ETF {row['etf_price']} "
            f"({row.get('vs_input_price_pct')}%) — 추정이익·적자 종목·환율 불변 가정"
        )
    if result.get("comparison"):
        change = result["comparison"]
        print(
            f"기준({change['reference_holdings_as_of']}) 대비 forward 이익 "
            f"{change['forward_earnings_change_pct']}% · 그때 주가 기준 PER "
            f"{change['reference_forward_pe']} → {change['forward_pe_at_reference_prices']}배 "
            f"(대조 비중 {change['matched_weight_pct']}%)"
        )
    print("-" * 76)
    print(f"※ {result['_not_a_judgment']}")


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python3.13 -m calculators.etf_lookthrough",
        description="ETF 구성종목 look-through 산술 (판정은 하지 않는다)",
    )
    parser.add_argument("asset_id")
    parser.add_argument("--file", help="재료 파일. 없으면 가장 최근 constituents-*.json")
    parser.add_argument("--at-forward-pe", type=float, nargs="+",
                        help="모델이 정한 forward PER 문턱을 ETF 가격으로 옮긴다")
    parser.add_argument("--price", type=float,
                        help="--at-forward-pe 의 기준 ETF 가격")
    parser.add_argument("--compare", help="비교할 이전 재료 파일")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    path = Path(args.file) if args.file else latest_path(args.asset_id.upper())
    if path is None or not path.exists():
        print(
            f"오류: knowledge/etfs/{args.asset_id}/constituents-*.json 이 없다. "
            f"먼저 `python3.13 -m collectors.etf_constituents {args.asset_id}` 로 받는다",
            file=sys.stderr,
        )
        return 1
    data = load(path)
    result = {"asset_id": data.get("asset_id"), "source_file": str(path.resolve().relative_to(ROOT))
              if path.resolve().is_relative_to(ROOT) else str(path),
              **summarize(data, top=len(data.get("holdings") or [])),
              "_not_a_judgment": NOT_A_JUDGMENT}
    if args.at_forward_pe:
        if args.price is None:
            print("오류: --at-forward-pe 에는 --price 가 필요하다", file=sys.stderr)
            return 1
        result["at_forward_pe"] = price_at_forward_pe(data, args.price, args.at_forward_pe)
    if args.compare:
        result["comparison"] = compare(data, load(Path(args.compare)))
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        render(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
