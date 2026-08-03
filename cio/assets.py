from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

from calculators.contracts import write_json
from cio.paths import CONFIG_DIR

REGISTRY_PATH = CONFIG_DIR / "assets.json"
MARKETS = {"KR", "US"}
ASSET_TYPES = {"STOCK", "ETF"}
FILING_SOURCE_BY_MARKET = {"KR": "DART", "US": "SEC_EDGAR"}
FILING_PROFILE_BY_MARKET = {
    "KR": "DART_KIFRS",
    "US": "SEC_DOMESTIC_US_GAAP",
}
OPTIONAL_COLLECTORS_BY_ROUTE = {
    ("KR", "STOCK"): {
        "KR_FLOW",
        "KR_LENDING",
        "KR_INVENTORY",
        "NORMALIZED_EARNINGS",
        "KR_SEGMENTS",
    },
    ("US", "STOCK"): set(),
    ("KR", "ETF"): set(),
    ("US", "ETF"): set(),
}
ETF_HOLDINGS_SOURCES = {"ISSUER", "SECONDARY", "MANUAL"}
ASSET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
MARKET_SYMBOL_RE = re.compile(r"^[A-Za-z0-9.^=_-]{1,64}$")


class AssetRegistryError(ValueError):
    pass


def _read(path: Path = REGISTRY_PATH) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise AssetRegistryError(f"자산 registry가 없다: {path}") from exc
    except json.JSONDecodeError as exc:
        raise AssetRegistryError(f"자산 registry JSON 오류: {exc}") from exc
    validate_registry(data)
    return data


def validate_registry(data: dict[str, Any]) -> None:
    if data.get("schema_version") != "asset-registry/1.0.0":
        raise AssetRegistryError("지원하지 않는 asset registry 버전")
    assets = data.get("assets")
    if not isinstance(assets, dict):
        raise AssetRegistryError("assets 객체가 필요하다")
    if set(data.get("supported_markets") or []) != MARKETS:
        raise AssetRegistryError("supported_markets는 KR, US여야 한다")
    if set(data.get("supported_asset_types") or []) != ASSET_TYPES:
        raise AssetRegistryError("supported_asset_types는 STOCK, ETF여야 한다")
    for asset_id, asset in assets.items():
        validate_asset(asset_id, asset)


def validate_asset(asset_id: str, asset: Any) -> None:
    if not isinstance(asset_id, str) or not ASSET_ID_RE.fullmatch(asset_id):
        raise AssetRegistryError(
            "asset_id는 영문·숫자로 시작하는 64자 이하의 영문·숫자·_·-만 가능하다"
        )
    if asset_id != asset_id.upper():
        raise AssetRegistryError(f"{asset_id}: asset_id 영문은 대문자여야 한다")
    if not isinstance(asset, dict):
        raise AssetRegistryError(f"{asset_id}: 자산 프로파일은 객체여야 한다")
    for field in (
        "name",
        "market",
        "exchange",
        "asset_type",
        "currency",
        "price_symbol",
        "sector_code",
        "analysis_lens",
        "valuation_family",
        "benchmarks",
    ):
        if field not in asset:
            raise AssetRegistryError(f"{asset_id}: {field}가 없다")
    for field in (
        "name",
        "exchange",
        "currency",
        "price_symbol",
        "sector_code",
        "analysis_lens",
    ):
        if not isinstance(asset[field], str) or not asset[field].strip():
            raise AssetRegistryError(f"{asset_id}: {field}는 비어 있지 않은 문자열이어야 한다")
    if asset["market"] not in MARKETS:
        raise AssetRegistryError(f"{asset_id}: market은 KR 또는 US만 가능하다")
    if asset["asset_type"] not in ASSET_TYPES:
        raise AssetRegistryError(f"{asset_id}: asset_type은 STOCK 또는 ETF만 가능하다")
    if not MARKET_SYMBOL_RE.fullmatch(asset["price_symbol"]):
        raise AssetRegistryError(f"{asset_id}: price_symbol 형식이 안전하지 않다")
    expected_currency = "KRW" if asset["market"] == "KR" else "USD"
    if asset["currency"] != expected_currency:
        raise AssetRegistryError(
            f"{asset_id}: {asset['market']} 기본 통화는 {expected_currency}다"
        )
    if (
        not isinstance(asset["valuation_family"], list)
        or not asset["valuation_family"]
        or any(
            not isinstance(item, str) or not item.strip()
            for item in asset["valuation_family"]
        )
    ):
        raise AssetRegistryError(f"{asset_id}: valuation_family가 비어 있다")
    if (
        not isinstance(asset["benchmarks"], list)
        or not asset["benchmarks"]
        or any(
            not isinstance(item, str) or not item.strip()
            for item in asset["benchmarks"]
        )
        or len(asset["benchmarks"]) != len(set(asset["benchmarks"]))
    ):
        raise AssetRegistryError(f"{asset_id}: benchmark가 비어 있다")
    if any(not MARKET_SYMBOL_RE.fullmatch(item) for item in asset["benchmarks"]):
        raise AssetRegistryError(f"{asset_id}: benchmark symbol 형식이 안전하지 않다")
    # 동종 비교군. 누구를 동종으로 볼지는 판단이므로 registry 에 명시해 검토
    # 가능하게 둔다. 모델이 매번 즉흥으로 고르면 비교 자체가 재현되지 않는다.
    peers = asset.get("peers", [])
    if not isinstance(peers, list):
        raise AssetRegistryError(f"{asset_id}: peers는 배열이어야 한다")
    peer_ids = []
    for peer in peers:
        if (
            not isinstance(peer, dict)
            or not isinstance(peer.get("asset_id"), str)
            or not ASSET_ID_RE.fullmatch(peer["asset_id"])
            or not isinstance(peer.get("name"), str)
            or not peer["name"].strip()
        ):
            raise AssetRegistryError(
                f"{asset_id}: peers 항목은 asset_id와 name을 가져야 한다"
            )
        if peer["asset_id"] == asset_id:
            raise AssetRegistryError(f"{asset_id}: 자기 자신을 동종에 둘 수 없다")
        peer_ids.append(peer["asset_id"])
    if len(peer_ids) != len(set(peer_ids)):
        raise AssetRegistryError(f"{asset_id}: peers에 중복이 있다")

    optional_collectors = asset.get("optional_collectors", [])
    if (
        not isinstance(optional_collectors, list)
        or any(not isinstance(item, str) for item in optional_collectors)
        or len(optional_collectors) != len(set(optional_collectors))
    ):
        raise AssetRegistryError(f"{asset_id}: optional_collectors 형식이 잘못됐다")
    allowed_collectors = OPTIONAL_COLLECTORS_BY_ROUTE[
        (asset["market"], asset["asset_type"])
    ]
    unsupported = set(optional_collectors) - allowed_collectors
    if unsupported:
        raise AssetRegistryError(
            f"{asset_id}: 이 시장·자산유형에서 지원하지 않는 선택 수집기: "
            f"{', '.join(sorted(unsupported))}"
        )

    if asset["asset_type"] == "STOCK":
        expected_source = FILING_SOURCE_BY_MARKET[asset["market"]]
        expected_profile = FILING_PROFILE_BY_MARKET[asset["market"]]
        if asset.get("filing_source") != expected_source:
            raise AssetRegistryError(
                f"{asset_id}: {asset['market']} 주식 filing_source는 "
                f"{expected_source}여야 한다"
            )
        if asset.get("filing_profile") != expected_profile:
            raise AssetRegistryError(
                f"{asset_id}: 현재 {asset['market']} 주식 자동 수집은 "
                f"filing_profile={expected_profile}만 지원한다"
            )
        if "etf" in asset:
            raise AssetRegistryError(f"{asset_id}: 주식에는 etf 구조를 둘 수 없다")
        return

    if "filing_source" in asset:
        raise AssetRegistryError(f"{asset_id}: ETF에는 filing_source를 둘 수 없다")
    etf = asset.get("etf")
    if not isinstance(etf, dict):
        raise AssetRegistryError(f"{asset_id}: ETF 구조 정보가 필요하다")
    for field in ("strategy", "holdings_source", "distribution_policy"):
        if not isinstance(etf.get(field), str) or not etf[field].strip():
            raise AssetRegistryError(f"{asset_id}: etf.{field}가 필요하다")
    if etf["holdings_source"] not in ETF_HOLDINGS_SOURCES:
        raise AssetRegistryError(
            f"{asset_id}: etf.holdings_source는 "
            f"{', '.join(sorted(ETF_HOLDINGS_SOURCES))} 중 하나여야 한다"
        )
    if not isinstance(etf.get("derivative_use"), bool):
        raise AssetRegistryError(f"{asset_id}: etf.derivative_use는 boolean이어야 한다")
    leverage = etf.get("leverage")
    if (
        isinstance(leverage, bool)
        or not isinstance(leverage, (int, float))
        or not math.isfinite(leverage)
        or leverage <= 0
    ):
        raise AssetRegistryError(f"{asset_id}: etf.leverage는 0보다 큰 유한한 수여야 한다")
    single_stock_derivative = (
        "SINGLE_STOCK" in etf["strategy"]
        or asset["sector_code"] == "SINGLE_STOCK_DERIVATIVE"
        or asset["analysis_lens"] == "SINGLE_STOCK_OPTION_INCOME"
    )
    if single_stock_derivative and not etf["derivative_use"]:
        raise AssetRegistryError(
            f"{asset_id}: 단일종목 파생 ETF는 etf.derivative_use=true여야 한다"
        )
    if single_stock_derivative and (
        not isinstance(etf.get("underlying"), str)
        or not etf["underlying"].strip()
    ):
        raise AssetRegistryError(
            f"{asset_id}: 단일종목 파생 ETF에는 etf.underlying이 필요하다"
        )
    if single_stock_derivative and etf["underlying"] not in asset["benchmarks"]:
        raise AssetRegistryError(
            f"{asset_id}: 기초자산은 benchmarks에도 포함해야 한다"
        )
    stock_only_valuations = {"DCF", "NORMALIZED_CYCLE", "PIPELINE_RNPV"}
    if stock_only_valuations.intersection(asset["valuation_family"]):
        raise AssetRegistryError(
            f"{asset_id}: ETF에 기업가치 평가법을 직접 적용할 수 없다"
        )


def all_assets(path: Path = REGISTRY_PATH) -> dict[str, dict[str, Any]]:
    return _read(path)["assets"]


def get_asset(asset_id: str, path: Path = REGISTRY_PATH) -> dict[str, Any]:
    key = asset_id.upper() if not asset_id.isdigit() else asset_id
    assets = all_assets(path)
    if key not in assets:
        raise AssetRegistryError(
            f"{asset_id}: 등록되지 않은 자산이다. 먼저 "
            "`python3.13 -m cio register`를 사용한다"
        )
    return {"asset_id": key, **assets[key]}


def register_asset(
    asset_id: str,
    *,
    name: str,
    market: str,
    exchange: str,
    asset_type: str,
    price_symbol: str,
    sector_code: str,
    analysis_lens: str,
    valuation_family: list[str],
    benchmarks: list[str],
    filing_source: str | None = None,
    etf: dict[str, Any] | None = None,
    path: Path = REGISTRY_PATH,
) -> dict[str, Any]:
    market = market.upper()
    asset_type = asset_type.upper()
    key = asset_id.upper() if not asset_id.isdigit() else asset_id
    profile: dict[str, Any] = {
        "name": name,
        "market": market,
        "exchange": exchange.upper(),
        "asset_type": asset_type,
        "currency": "KRW" if market == "KR" else "USD",
        "price_symbol": price_symbol,
        "sector_code": sector_code,
        "industry": "",
        "analysis_lens": analysis_lens,
        "valuation_family": valuation_family,
        "benchmarks": benchmarks,
        "optional_collectors": [],
    }
    if asset_type == "STOCK":
        profile["filing_source"] = filing_source or FILING_SOURCE_BY_MARKET.get(
            market
        )
        profile["filing_profile"] = FILING_PROFILE_BY_MARKET.get(market)
    if asset_type == "ETF":
        profile["etf"] = etf or {
            "strategy": "UNKNOWN",
            "derivative_use": False,
            "leverage": 1.0,
            "holdings_source": "ISSUER",
            "distribution_policy": "UNKNOWN",
        }
    validate_asset(key, profile)

    registry = _read(path)
    registry["assets"][key] = profile
    write_json(path, registry)
    return {"asset_id": key, **profile}
