from __future__ import annotations

import csv
import datetime as dt
import glob
import json
import math
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from calculators.contracts import (
    ContractError,
    load_json,
    load_raw_snapshot,
    validate_fundamentals,
)
from collectors.dart import (
    DART_FILING_TYPES,
    DART_PRIMARY_REPORT_FORMS,
    FILING_INDEX_LIMIT as DART_FILING_INDEX_LIMIT,
    filing_rows_from_list,
    merge_filing_rows,
)
from collectors.edgar import filing_rows_from_submissions
from cio.assets import get_asset
from cio.paths import DATA_DIR, ROOT

KST = dt.timezone(dt.timedelta(hours=9))
MARKET_PRICE_REQUIREMENTS = {
    "KR": (("^KS11", True), ("^KQ11", True), ("^VIX", False)),
    "US": (
        ("^GSPC", True),
        ("^IXIC", True),
        ("^VIX", True),
        ("KRW=X", True),
        ("^TNX", False),
    ),
}
MARKET_STATE_REQUIRED_INPUTS = {
    "KR": ("^KS11", "^KQ11"),
    "US": ("^GSPC", "^IXIC", "^VIX"),
}
SEC_FILING_FORMS = (
    "10-K",
    "10-K/A",
    "10-Q",
    "10-Q/A",
    "8-K",
    "8-K/A",
    "DEF 14A",
    "S-1",
    "S-1/A",
    "S-3",
    "S-3/A",
    "S-3ASR",
    "424B5",
)
SEC_PRIMARY_REPORT_FORMS = {
    "10-K",
    "10-K/A",
    "10-Q",
    "10-Q/A",
    "S-1",
    "S-1/A",
}
SEC_FILING_INDEX_LIMIT = 50
DART_FILING_LOOKBACK_DAYS = 400


def safe_symbol(symbol: str) -> str:
    return symbol.replace(".", "_").replace("^", "").replace("=", "")


def event_contract_errors(
    data: Any,
    asset: dict[str, Any],
) -> list[str]:
    errors = []
    if not isinstance(data, dict):
        return ["이벤트 packet은 JSON 객체여야 한다"]
    if data.get("schema_version") != "event-packet/2.1.0":
        errors.append("event-packet/2.1.0이 필요하다")
    if data.get("asset_id") != asset["asset_id"]:
        errors.append("이벤트 packet의 asset_id가 요청 자산과 다르다")
    if data.get("symbol") != asset["price_symbol"]:
        errors.append("이벤트 packet의 가격 심볼이 registry와 다르다")
    collected_at = data.get("collected_at")
    try:
        collected_time = dt.datetime.fromisoformat(collected_at)
    except (TypeError, ValueError):
        collected_time = None
        errors.append("이벤트 packet의 수집시각 형식이 잘못됐다")
    if collected_time is not None and collected_time.tzinfo is None:
        errors.append("이벤트 packet 수집시각에 시간대가 없다")
    benchmark = data.get("benchmark")
    if (
        not isinstance(benchmark, dict)
        or benchmark.get("symbol") != asset["benchmarks"][0]
    ):
        errors.append("이벤트 packet의 벤치마크가 registry와 다르다")
    elif not isinstance(benchmark.get("calendar_gaps"), list):
        # 구멍이 없으면 빈 배열이어야 한다. 필드 자체가 없다는 것은 캘린더를
        # 대조하지 않았다는 뜻이고, 그러면 상대변동을 믿을 수 없다.
        errors.append("이벤트 packet에 벤치마크 캘린더 대조 결과가 없다")
    last_bar = data.get("last_bar")
    if not isinstance(last_bar, dict) or last_bar.get("settled") is not True:
        errors.append("이벤트 packet이 확정 거래일 위에서 계산되지 않았다")
    comparison = data.get("benchmark_comparison")
    if (
        not isinstance(comparison, dict)
        or comparison.get("benchmark_symbol") != asset["benchmarks"][0]
    ):
        errors.append("이벤트 packet의 벤치마크 비교가 registry와 다르다")
    if not isinstance(data.get("judged_on"), str):
        errors.append("이벤트 packet에 판정 기준 표기가 없다")
    moves = data.get("moves")
    if not isinstance(moves, list):
        errors.append("이벤트 packet의 moves 배열이 없다")
    elif any(
        not isinstance(move, dict)
        or not isinstance(move.get("date"), str)
        or not isinstance(move.get("candidate_filings"), list)
        for move in moves
    ):
        errors.append("이벤트 packet의 급변 행 형식이 잘못됐다")
    return errors


@dataclass
class RefreshTask:
    name: str
    capability: str
    artifact: str
    max_age_hours: int
    command: list[str] | None
    required: bool = True
    dependencies: list[str] = field(default_factory=list)
    state: str = "UNKNOWN"
    age_hours: float | None = None
    data_as_of: str | None = None
    resolved_path: str | None = None
    reason: str | None = None


def _price_task(symbol: str, *, required: bool = True) -> RefreshTask:
    filename = safe_symbol(symbol)
    return RefreshTask(
        name=f"price:{symbol}",
        capability="PRICE",
        artifact=f"knowledge/prices/{filename}.csv",
        max_age_hours=12,
        command=[
            sys.executable,
            "-m",
            "collectors.price",
            "history",
            symbol,
            "--range",
            "5y",
        ],
        required=required,
    )


KR_INDEX_MARKETS = {"^KS11": "KOSPI", "^KQ11": "KOSDAQ"}


def _benchmark_backfill_task(symbol: str) -> RefreshTask:
    """벤치마크 지수의 누락 거래일을 KRX 원문으로 메우는 작업.

    Yahoo 는 거래일을 통째로 빠뜨리고도 정상 응답을 준다. price 수집이 파일을
    덮어쓸 때마다 구멍도 함께 돌아오므로, 표식 파일이 가격 파일보다 오래되면
    자동으로 다시 돈다.
    """
    market = KR_INDEX_MARKETS[symbol]
    filename = safe_symbol(symbol)
    return RefreshTask(
        name=f"benchmark_backfill:{symbol}",
        capability="BENCHMARK_BACKFILL",
        artifact=f"knowledge/prices/_backfill/{filename}.json",
        max_age_hours=12,
        command=[
            sys.executable,
            "-m",
            "collectors.krx",
            "backfill",
            market,
        ],
        dependencies=[f"knowledge/prices/{filename}.csv"],
    )


def tasks_for(asset_id: str, *, include_optional: bool = False) -> list[RefreshTask]:
    asset = get_asset(asset_id)
    tasks: list[RefreshTask] = [_price_task(asset["price_symbol"])]

    market_symbols = list(asset["benchmarks"])
    for symbol in market_symbols:
        tasks.append(_price_task(symbol))
    for symbol, required in MARKET_PRICE_REQUIREMENTS[asset["market"]]:
        tasks.append(_price_task(symbol, required=required))
    # 가격 수집 뒤에 놓는다 — 덮어쓴 파일 위에서 다시 메워야 한다.
    for symbol in dict.fromkeys(
        [*market_symbols, *(s for s, _ in MARKET_PRICE_REQUIREMENTS[asset["market"]])]
    ):
        if symbol in KR_INDEX_MARKETS:
            tasks.append(_benchmark_backfill_task(symbol))

    if asset["asset_type"] == "STOCK":
        filing_source = asset["filing_source"]
        if filing_source == "DART":
            tasks.append(
                RefreshTask(
                    name=f"fundamentals:{asset['asset_id']}",
                    capability="FUNDAMENTALS",
                    artifact=(
                        f"knowledge/fundamentals/{asset['asset_id']}-*.json"
                    ),
                    max_age_hours=720,
                    command=[
                        sys.executable,
                        "-m",
                        "collectors.dart",
                        "fundamentals",
                        asset["asset_id"],
                        "--year",
                        str(dt.date.today().year),
                    ],
                )
            )
            # 재무 수치만으로는 "그 사이 무슨 일이 있었나"에 답할 수 없다.
            # 잠정실적·주요사항보고서는 분기보고서보다 먼저 나오고, 모델이
            # 원문을 읽을 진입점이 되는 것이 이 색인의 목적이다.
            tasks.append(
                RefreshTask(
                    name=f"filings:{asset['asset_id']}",
                    capability="FILINGS",
                    artifact=f"knowledge/filings/{asset['asset_id']}.json",
                    max_age_hours=24,
                    command=[
                        sys.executable,
                        "-m",
                        "collectors.dart",
                        "filings",
                        asset["asset_id"],
                        "--since",
                        (
                            dt.date.today()
                            - dt.timedelta(days=DART_FILING_LOOKBACK_DAYS)
                        ).strftime("%Y%m%d"),
                    ],
                )
            )
        elif filing_source == "SEC_EDGAR":
            tasks.append(
                RefreshTask(
                    name=f"fundamentals:{asset['asset_id']}",
                    capability="FUNDAMENTALS",
                    artifact=(
                        f"knowledge/fundamentals/{asset['asset_id']}-*.json"
                    ),
                    max_age_hours=720,
                    command=[
                        sys.executable,
                        "-m",
                        "collectors.edgar",
                        "fundamentals",
                        asset["asset_id"],
                    ],
                )
            )
            tasks.append(
                RefreshTask(
                    name=f"filings:{asset['asset_id']}",
                    capability="FILINGS",
                    artifact=f"knowledge/filings/{asset['asset_id']}.json",
                    max_age_hours=24,
                    command=[
                        sys.executable,
                        "-m",
                        "collectors.edgar",
                        "filings",
                        asset["asset_id"],
                        "--forms",
                        *SEC_FILING_FORMS,
                        "--limit",
                        str(SEC_FILING_INDEX_LIMIT),
                    ],
                )
            )
        else:
            raise ValueError(
                f"{asset['asset_id']}: 지원하지 않는 filing_source {filing_source}"
            )

        if asset["market"] == "KR":
            tasks.append(
                RefreshTask(
                    name=f"consensus:{asset['asset_id']}",
                    capability="CONSENSUS",
                    artifact=f"knowledge/consensus/{asset['asset_id']}.json",
                    max_age_hours=24,
                    command=[
                        sys.executable,
                        "-m",
                        "collectors.consensus",
                        asset["asset_id"],
                    ],
                    required=False,
                )
            )
            # 동종은 같은 수집기로 같은 방식으로 모은다. 기준일·EPS 기준·PER
            # 계산이 전부 같아야 배수 비교가 성립한다 — 언론이 인용한 남의
            # 배수는 기준연도도 주가시점도 알 수 없어 비교에 쓸 수 없다.
            for peer in asset.get("peers") or []:
                tasks.append(
                    RefreshTask(
                        name=f"consensus_peer:{peer['asset_id']}",
                        capability="CONSENSUS",
                        artifact=(
                            f"knowledge/consensus/{peer['asset_id']}.json"
                        ),
                        max_age_hours=168,
                        command=[
                            sys.executable,
                            "-m",
                            "collectors.consensus",
                            peer["asset_id"],
                        ],
                        required=False,
                    )
                )
            primary_benchmark = asset["benchmarks"][0]
            tasks.append(
                RefreshTask(
                    name=f"events:{asset['asset_id']}",
                    capability="EVENTS",
                    artifact=f"knowledge/events/{asset['asset_id']}.json",
                    max_age_hours=24,
                    command=[
                        sys.executable,
                        "-m",
                        "collectors.kr_events",
                        asset["asset_id"],
                        "--symbol",
                        asset["price_symbol"],
                        "--benchmark",
                        primary_benchmark,
                    ],
                    dependencies=[
                        f"knowledge/prices/{safe_symbol(asset['price_symbol'])}.csv",
                        f"knowledge/prices/{safe_symbol(primary_benchmark)}.csv",
                        # 백필은 원래 retrieved_at 을 보존하므로 가격 파일의
                        # 회수시각만 보면 구멍이 메워진 것을 알 수 없다. 표식이
                        # 갱신되면 상대변동을 다시 계산해야 한다.
                        *([
                            "knowledge/prices/_backfill/"
                            f"{safe_symbol(primary_benchmark)}.json"
                        ] if primary_benchmark in KR_INDEX_MARKETS else []),
                    ],
                )
            )
    else:
        holdings_source = asset["etf"]["holdings_source"]
        tasks.append(
            RefreshTask(
                name=f"holdings:{asset['asset_id']}",
                capability="ETF_HOLDINGS",
                artifact=f"knowledge/etfs/{asset['asset_id']}/holdings-*.csv",
                max_age_hours=168,
                command=None,
                reason=(
                    f"{holdings_source} 등급의 최신 구성종목 스냅샷을 "
                    "확인해 저장해야 한다"
                ),
            )
        )
        underlying = (asset.get("etf") or {}).get("underlying")
        if underlying and underlying not in market_symbols:
            tasks.append(_price_task(underlying))

    tasks.append(
        RefreshTask(
            name="portfolio:holdings",
            capability="PORTFOLIO",
            artifact="knowledge/portfolio/holdings.csv",
            max_age_hours=168,
            command=None,
            required=False,
            reason="보유수량·평가액·투자가능 현금을 사용자가 갱신해야 한다",
        )
    )

    regime_dependencies = [
        _price_task(symbol).artifact
        for symbol in MARKET_STATE_REQUIRED_INPUTS[asset["market"]]
    ]
    tasks.append(
        RefreshTask(
            name=f"market:{asset['market']}",
            capability="MARKET_STATE",
            artifact=f"knowledge/market/regime-{asset['market']}.json",
            max_age_hours=24,
            command=[
                sys.executable,
                "-m",
                "calculators.market_regime",
                "--market",
                asset["market"],
                "--out",
                f"knowledge/market/regime-{asset['market']}.json",
                "--local-only",
            ],
            dependencies=regime_dependencies,
        )
    )

    if include_optional:
        _append_optional(tasks, asset)
    return _deduplicate(tasks)


def _append_optional(tasks: list[RefreshTask], asset: dict[str, Any]) -> None:
    enabled = set(asset.get("optional_collectors") or [])
    asset_id = asset["asset_id"]
    if "KR_FLOW" in enabled:
        tasks.append(
            RefreshTask(
                name=f"flow:{asset_id}",
                capability="KR_FLOW",
                artifact=f"knowledge/flows/{asset_id}.csv",
                max_age_hours=24,
                command=[
                    sys.executable,
                    "-m",
                    "collectors.kr_flow",
                    "stock",
                    asset_id,
                    "--days",
                    "90",
                ],
                required=False,
            )
        )
    if "KR_INVENTORY" in enabled:
        year = dt.date.today().year
        tasks.append(
            RefreshTask(
                name=f"inventory:{asset_id}",
                capability="KR_INVENTORY",
                artifact=f"knowledge/inventory/{asset_id}.csv",
                max_age_hours=720,
                command=[
                    sys.executable,
                    "-m",
                    "collectors.kr_inventory",
                    asset_id,
                    "--years",
                    str(year - 3),
                    str(year - 2),
                    str(year - 1),
                    str(year),
                ],
                required=False,
            )
        )
    if "NORMALIZED_EARNINGS" in enabled:
        year = dt.date.today().year
        tasks.append(
            RefreshTask(
                name=f"normalized:{asset_id}",
                capability="NORMALIZED_EARNINGS",
                artifact=f"knowledge/normalized/{asset_id}.json",
                max_age_hours=720,
                command=[
                    sys.executable,
                    "-m",
                    "calculators.normalized_earnings",
                    asset_id,
                    "--years",
                    *[str(value) for value in range(year - 7, year + 1)],
                ],
                required=False,
            )
        )
def _deduplicate(tasks: list[RefreshTask]) -> list[RefreshTask]:
    seen: dict[tuple[str, str], RefreshTask] = {}
    result: list[RefreshTask] = []
    for task in tasks:
        key = (task.capability, task.artifact)
        existing = seen.get(key)
        if existing is None:
            seen[key] = task
            result.append(task)
            continue
        existing.required = existing.required or task.required
        existing.dependencies = list(
            dict.fromkeys([*existing.dependencies, *task.dependencies])
        )
    return result


def _dart_filing_index_error(data: dict[str, Any], asset_id: str) -> str | None:
    """DART 공시 색인이 저장된 원문에서 그대로 다시 나오는지 확인한다.

    SEC 색인과 같은 기준이다 — 색인은 원문의 파생물이어야 하고, 손으로
    고칠 수 있는 별개의 사실이 되면 안 된다.
    """
    corp_code = data.get("corp_code")
    if not isinstance(corp_code, str) or not re.fullmatch(r"\d{8}", corp_code):
        return "공시 색인의 DART 고유번호 형식이 잘못됐다"
    if not re.fullmatch(r"\d{8}", str(data.get("since") or "")):
        return "공시 색인의 조회 시작일이 없다"
    expected_types = [code for code, _ in DART_FILING_TYPES]
    if data.get("pblntf_types") != expected_types:
        return "공시 색인의 공시유형 필터가 수집 계약과 다르다"
    raw_sources = data.get("raw_sources")
    if not isinstance(raw_sources, list) or len(raw_sources) != len(
        expected_types
    ):
        return "공시 색인의 원문 목록이 공시유형 수와 다르다"
    if [item.get("pblntf_ty") for item in raw_sources] != expected_types:
        return "공시 색인의 원문 목록이 공시유형 순서와 다르다"
    row_groups = []
    for item in raw_sources:
        if item.get("empty") is True:
            continue
        digest = item.get("raw_payload_sha256")
        if not isinstance(digest, str) or not re.fullmatch(
            r"[0-9a-f]{64}", digest
        ):
            return "공시 색인의 원문 지문 형식이 잘못됐다"
        locator = f"data/raw/dart/objects/{digest}.json"
        if (
            item.get("raw_locator") != locator
            or not (ROOT / locator).is_file()
        ):
            return "공시 색인의 원문 위치가 유효하지 않다"
        try:
            raw_document = load_raw_snapshot(
                ROOT / locator,
                expected_digest=digest,
                expected_source="opendart.fss.or.kr",
            )
            row_groups.append(
                filing_rows_from_list(
                    raw_document["payload"],
                    stock_code=asset_id,
                    limit=DART_FILING_INDEX_LIMIT,
                )
            )
        except (OSError, ValueError):
            return "공시 색인의 DART 원문에서 목록을 재현할 수 없다"
    filings = data.get("filings")
    if (
        not isinstance(filings, list)
        or not filings
        or len(filings) > DART_FILING_INDEX_LIMIT
    ):
        return "공시 색인의 제출 문서 수가 유효하지 않다"
    if filings != merge_filing_rows(row_groups, limit=DART_FILING_INDEX_LIMIT):
        return "공시 색인이 DART 원문의 공시 목록과 다르다"
    if not any(
        filing.get("form") in DART_PRIMARY_REPORT_FORMS for filing in filings
    ):
        return "공시 색인에 정기보고서가 없다"
    return None


def _artifact_contract_error(task: RefreshTask, path: Path) -> str | None:
    if task.capability == "PRICE":
        try:
            with path.open(encoding="utf-8-sig") as handle:
                first_line = handle.readline().strip()
                metadata_line = handle.readline().strip()
                retrieval_line = handle.readline().strip()
                session_line = handle.readline().strip()
                header = next(
                    csv.reader(
                        line
                        for line in handle
                        if not line.lstrip().startswith("#")
                    ),
                    [],
                )
        except OSError:
            return "가격 파일을 읽을 수 없다"
        if first_line != "# schema_version price-series/2.1.0":
            return "조정종가·세션상태가 있는 price-series/2.1.0이 필요하다"
        metadata = re.fullmatch(
            r"# symbol (.+) / currency ([A-Z]{3})",
            metadata_line,
        )
        expected_symbol = task.name.split(":", 1)[-1]
        if not metadata or metadata.group(1) != expected_symbol:
            return "가격 파일의 symbol 메타데이터가 요청 심볼과 다르다"
        retrieval = re.fullmatch(
            r"# retrieved_at (.+) / source Yahoo Finance chart API",
            retrieval_line,
        )
        try:
            retrieved_at = (
                dt.datetime.fromisoformat(retrieval.group(1))
                if retrieval
                else None
            )
        except ValueError:
            retrieved_at = None
        if retrieved_at is None or retrieved_at.tzinfo is None:
            return "가격 파일의 회수시각 메타데이터가 유효하지 않다"
        # 마지막 봉이 확정 종가인지가 파일에 남아 있어야 한다. 장중 수집분은
        # 진행 중인 봉을 확정 일봉과 같은 형태로 담는다.
        if not re.fullmatch(
            r"# last_bar \d{4}-\d{2}-\d{2} / session "
            r"(SETTLED|UNSETTLED|UNKNOWN)",
            session_line,
        ):
            return "가격 파일에 마지막 봉의 세션 상태가 없다"
        required_fields = {
            "date",
            "close",
            "adjusted_close",
            "split_adjustment_factor",
            "high",
            "low",
        }
        if not required_fields.issubset(set(header)):
            return "가격 v2 필수 열이 없다"
    if task.capability == "CONSENSUS":
        try:
            data = load_json(path)
        except (OSError, ValueError):
            return "컨센서스 packet을 읽을 수 없다"
        if data.get("schema_version") != "consensus-packet/1.0.0":
            return "consensus-packet/1.0.0이 필요하다"
        if data.get("asset_id") != task.name.split(":", 1)[-1]:
            return "컨센서스 packet의 asset_id가 요청 자산과 다르다"
        estimates = data.get("estimates")
        if not isinstance(estimates, dict) or not estimates.get("annual"):
            # 연도별 추정이 밸류에이션의 본체다. 목표주가만 있는 packet 은
            # 애널리스트의 결론만 있고 입력이 없는 상태다.
            return "컨센서스 packet에 연도별 실적·추정이 없다"
        # 분산 없이 평균만 남으면 '몇 곳이 얼마나 갈리는가'를 잃는다.
        dispersion = data.get("dispersion")
        if not isinstance(dispersion, dict) or not isinstance(
            dispersion.get("recent"), dict
        ):
            return "컨센서스 packet에 최근 목표가 분산이 없다"
    if task.capability == "BENCHMARK_BACKFILL":
        try:
            data = load_json(path)
        except (OSError, ValueError):
            return "벤치마크 백필 기록을 읽을 수 없다"
        if data.get("schema_version") != "price-backfill/1.0.0":
            return "price-backfill/1.0.0이 필요하다"
        filled = data.get("filled")
        target = data.get("target")
        if not isinstance(filled, list) or not isinstance(target, str):
            return "벤치마크 백필 기록에 메운 거래일 목록이 없다"
        # 기록이 스스로를 증명해야 한다. 대상 파일이 어떤 경로로든 다시 쓰여
        # 메웠던 거래일이 사라지면, 기록만 남아 '메웠다'고 거짓말한다.
        target_path = ROOT / "knowledge" / "prices" / target
        if filled:
            if not target_path.exists():
                return "벤치마크 백필 대상 가격 파일이 없다"
            try:
                with target_path.open(encoding="utf-8-sig") as handle:
                    present = {
                        row.get("date")
                        for row in csv.DictReader(
                            line
                            for line in handle
                            if not line.lstrip().startswith("#")
                        )
                    }
            except OSError:
                return "벤치마크 백필 대상 가격 파일을 읽을 수 없다"
            if not set(filled).issubset(present):
                return "백필로 메웠던 거래일이 가격 파일에서 다시 사라졌다"
    if task.capability == "MARKET_STATE":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return "시장 상태 JSON을 읽을 수 없다"
        if data.get("schema_version") != "market-regime/2.0.0":
            return "market-regime/2.0.0이 필요하다"
    if task.capability == "FUNDAMENTALS":
        try:
            data = load_json(path)
        except (OSError, ValueError):
            return "재무 스냅샷 JSON을 읽을 수 없다"
        errors = validate_fundamentals(data)
        if errors:
            return f"재무 스냅샷 계약 오류: {errors[0]}"
        expected_id = task.name.split(":", 1)[-1]
        period_end = data.get("period_end")
        if data.get("instrument_id") != expected_id:
            return "재무 스냅샷의 instrument_id가 요청 자산과 다르다"
        if (
            not isinstance(period_end, str)
            or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", period_end)
            or path.name != f"{expected_id}-{period_end}.json"
        ):
            return "재무 스냅샷 파일명과 기준일이 다르다"
        digest = data.get("raw_payload_sha256")
        if data.get("corp_code"):
            raw_kind = "dart"
            raw_source = "opendart.fss.or.kr"
            raw_identity = {
                "instrument_id": expected_id,
                "corp_code": data["corp_code"],
            }
        elif data.get("cik"):
            raw_kind = "edgar"
            raw_source = "sec.gov/edgar"
            raw_identity = {
                "instrument_id": expected_id,
                "cik": data["cik"],
            }
        else:
            return "재무 스냅샷에 공시원 식별자가 없다"
        expected_locator = (
            f"data/raw/{raw_kind}/objects/{digest}.json"
            if isinstance(digest, str)
            else None
        )
        if (
            not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or data.get("raw_locator") != expected_locator
            or not (ROOT / expected_locator).is_file()
        ):
            return "재무 스냅샷의 내용 주소형 원문이 없다"
        try:
            raw_document = load_raw_snapshot(
                ROOT / expected_locator,
                expected_digest=digest,
                expected_source=raw_source,
            )
        except (OSError, ValueError):
            return "재무 스냅샷 원문의 내용 지문이 유효하지 않다"
        if (raw_document.get("payload") or {}).get("_instrument") != raw_identity:
            return "재무 스냅샷과 원문의 자산 식별자가 다르다"
    if task.capability == "FILINGS":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return "공시 색인 JSON을 읽을 수 없다"
        if data.get("schema_version") != "filing-index/1.0.0":
            return "filing-index/1.0.0이 필요하다"
        expected_id = task.name.split(":", 1)[-1]
        if data.get("asset_id") != expected_id:
            return "공시 색인의 asset_id가 요청 자산과 다르다"
        collected_at = data.get("collected_at")
        if not collected_at:
            return "공시 색인의 수집시각이 없다"
        try:
            collected_time = dt.datetime.fromisoformat(collected_at)
        except (TypeError, ValueError):
            return "공시 색인의 수집시각 형식이 잘못됐다"
        if collected_time.tzinfo is None:
            return "공시 색인의 수집시각에 시간대가 없다"
        if data.get("source") == "DART_LIST":
            return _dart_filing_index_error(data, expected_id)
        if not data.get("cik"):
            return "공시 색인의 CIK가 없다"
        if data.get("source") != "SEC_EDGAR_SUBMISSIONS":
            return "공시 색인의 출처가 SEC submissions가 아니다"
        if data.get("forms_filter") != sorted(SEC_FILING_FORMS):
            return "공시 색인의 중요 form 필터가 수집 계약과 다르다"
        digest = data.get("raw_payload_sha256")
        expected_locator = (
            f"data/raw/edgar/objects/{digest}.json"
            if isinstance(digest, str)
            else None
        )
        if (
            not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or data.get("raw_locator") != expected_locator
            or not (ROOT / expected_locator).is_file()
        ):
            return "공시 색인의 원문 지문 또는 위치가 유효하지 않다"
        try:
            raw_document = load_raw_snapshot(
                ROOT / expected_locator,
                expected_digest=digest,
                expected_source="sec.gov/edgar",
            )
        except (OSError, ValueError):
            return "공시 색인의 SEC 원문 내용 지문이 유효하지 않다"
        raw_cik = str((raw_document.get("payload") or {}).get("cik") or "")
        if not raw_cik.isdigit() or raw_cik.zfill(10) != data.get("cik"):
            return "공시 색인과 SEC 원문의 CIK가 다르다"
        raw_tickers = (raw_document.get("payload") or {}).get("tickers")
        if (
            not isinstance(raw_tickers, list)
            or expected_id not in {
                str(ticker).upper() for ticker in raw_tickers
            }
        ):
            return "공시 색인의 자산 ID가 SEC 원문 ticker와 다르다"
        filings = data.get("filings")
        if (
            not isinstance(filings, list)
            or not filings
            or len(filings) > SEC_FILING_INDEX_LIMIT
        ):
            return "공시 색인의 제출 문서 수가 유효하지 않다"
        required_fields = {
            "filing_date",
            "report_date",
            "form",
            "accession_number",
            "primary_document",
            "document_url",
        }
        required_values = required_fields - {"report_date"}
        if any(
            not isinstance(filing, dict)
            or not required_fields.issubset(filing)
            or any(filing.get(field) in (None, "") for field in required_values)
            for filing in filings
        ):
            return "공시 색인의 제출 문서 필드가 불완전하다"
        cik_value = data.get("cik")
        if not isinstance(cik_value, str) or not re.fullmatch(
            r"\d{10}", cik_value
        ):
            return "공시 색인의 CIK 형식이 잘못됐다"
        if any(
            not isinstance(filing["accession_number"], str)
            or not re.fullmatch(
                r"\d{10}-\d{2}-\d{6}", filing["accession_number"]
            )
            or not isinstance(filing["primary_document"], str)
            or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._-]*",
                filing["primary_document"],
            )
            or filing["form"] not in SEC_FILING_FORMS
            for filing in filings
        ):
            return "공시 색인의 form·accession·문서명 형식이 잘못됐다"
        if not any(
            filing["form"] in SEC_PRIMARY_REPORT_FORMS for filing in filings
        ):
            return "공시 색인에 정기보고서 또는 상장신고서가 없다"
        accessions = [filing["accession_number"] for filing in filings]
        if len(accessions) != len(set(accessions)):
            return "공시 색인의 accession_number가 중복됐다"
        cik = str(int(cik_value))
        if any(
            filing["document_url"]
            != (
                "https://www.sec.gov/Archives/edgar/data/"
                f"{cik}/{filing['accession_number'].replace('-', '')}/"
                f"{filing['primary_document']}"
            )
            for filing in filings
        ):
            return "공시 색인의 문서 URL이 SEC 원문을 가리키지 않는다"
        try:
            expected_filings = filing_rows_from_submissions(
                raw_document["payload"],
                cik=cik_value,
                forms=SEC_FILING_FORMS,
                limit=SEC_FILING_INDEX_LIMIT,
            )
        except ValueError:
            return "SEC 원문에서 공시 색인을 재현할 수 없다"
        if filings != expected_filings:
            return "공시 색인이 SEC 원문의 form·순서·필드와 다르다"
    if task.capability == "EVENTS":
        try:
            data = load_json(path)
            asset = get_asset(task.name.split(":", 1)[-1])
        except (OSError, ValueError):
            return "이벤트 packet JSON을 읽을 수 없다"
        errors = event_contract_errors(data, asset)
        if errors:
            return errors[0]
    if task.capability == "PORTFOLIO":
        try:
            with path.open(encoding="utf-8-sig") as handle:
                rows = list(
                    csv.DictReader(
                        line
                        for line in handle
                        if not line.lstrip().startswith("#")
                    )
                )
        except OSError:
            return "포트폴리오 파일을 읽을 수 없다"
        required_fields = {"asset_id", "risk_class", "value_krw", "quantity"}
        fieldnames = set(rows[0]) if rows else set()
        if not rows or not required_fields.issubset(fieldnames):
            return "포트폴리오 필수 열 또는 행이 없다"
        asset_ids = [row.get("asset_id") for row in rows]
        if len(asset_ids) != len(set(asset_ids)):
            return "포트폴리오 asset_id가 중복됐다"
        try:
            values = [float(row["value_krw"]) for row in rows]
        except (TypeError, ValueError):
            return "포트폴리오 value_krw가 숫자가 아니다"
        if any(not math.isfinite(value) or value < 0 for value in values):
            return "포트폴리오 value_krw가 유효하지 않다"
    if task.capability == "ETF_HOLDINGS":
        try:
            with path.open(encoding="utf-8-sig") as handle:
                rows = list(
                    csv.DictReader(
                        line
                        for line in handle
                        if not line.lstrip().startswith("#")
                    )
                )
        except OSError:
            return "ETF 구성 파일을 읽을 수 없다"
        required_fields = {
            "etf_id",
            "holding_id",
            "holding_name",
            "weight_pct",
            "as_of",
        }
        if not rows or not required_fields.issubset(set(rows[0])):
            return "ETF 구성 필수 열 또는 행이 없다"
        expected_id = task.name.split(":", 1)[-1]
        if any(row.get("etf_id") != expected_id for row in rows):
            return "ETF 구성의 etf_id가 요청 자산과 다르다"
        holding_ids = [row.get("holding_id") for row in rows]
        if (
            any(not holding_id for holding_id in holding_ids)
            or len(holding_ids) != len(set(holding_ids))
        ):
            return "ETF holding_id가 비었거나 중복됐다"
        as_of_values = {row.get("as_of") for row in rows}
        if None in as_of_values or "" in as_of_values or len(as_of_values) != 1:
            return "ETF 구성 기준일이 비었거나 행마다 다르다"
        try:
            weights = [float(row["weight_pct"]) for row in rows]
        except (TypeError, ValueError):
            return "ETF weight_pct가 숫자가 아니다"
        if (
            any(not math.isfinite(weight) or weight < 0 for weight in weights)
            or sum(weights) <= 0
        ):
            return "ETF weight_pct가 유효하지 않다"
    return None


def inspect_task(task: RefreshTask, *, now: dt.datetime | None = None) -> RefreshTask:
    now = now or dt.datetime.now(dt.timezone.utc)
    dependency_matches: dict[str, list[Path]] = {
        pattern: [
            Path(path) for path in glob.glob(str(ROOT / pattern))
        ]
        for pattern in task.dependencies
    }
    missing_dependencies = [
        pattern for pattern, paths in dependency_matches.items() if not paths
    ]
    if missing_dependencies:
        task.state = "BLOCKED"
        task.reason = (
            "입력 산출물이 없다: " + ", ".join(missing_dependencies)
        )
        return task

    matches = [Path(path) for path in glob.glob(str(ROOT / task.artifact))]
    if not matches:
        task.state = "MISSING"
        return task
    path = max(
        matches,
        key=lambda item: (_data_as_of(item) or "", item.stat().st_mtime),
    )
    task.resolved_path = str(path.relative_to(ROOT))
    task.data_as_of = _data_as_of(path)
    freshness_time = None
    if task.capability in {"ETF_HOLDINGS", "PORTFOLIO"} and task.data_as_of:
        freshness_time = _parse_artifact_time(task.data_as_of)
    if freshness_time is None:
        freshness_time = _collection_time(path)
    if freshness_time is None:
        task.state = "STALE"
        task.reason = "산출물에 신뢰할 수 있는 수집·생성시각이 없다"
        return task
    task.age_hours = (now - freshness_time).total_seconds() / 3600
    task.state = "FRESH" if task.age_hours <= task.max_age_hours else "STALE"
    if task.age_hours < -1:
        task.state = "STALE"
        task.reason = "산출물 기준시점이 현재보다 미래다"
    contract_error = _artifact_contract_error(task, path)
    if contract_error:
        task.state = "STALE"
        task.reason = contract_error

    dependency_times = [
        observed
        for paths in dependency_matches.values()
        for path in paths
        for observed in [_collection_time(path)]
        if observed is not None
    ]
    if dependency_times and max(dependency_times) > freshness_time:
        task.state = "STALE"
        task.reason = "입력 산출물보다 오래됐다"
    return task


def inspect_plan(
    asset_id: str, *, include_optional: bool = False
) -> list[RefreshTask]:
    return [
        inspect_task(task)
        for task in tasks_for(asset_id, include_optional=include_optional)
    ]


def _data_as_of(path: Path) -> str | None:
    try:
        if path.suffix == ".csv":
            with path.open(encoding="utf-8-sig") as handle:
                lines = list(handle)
            for line in lines:
                if not line.lstrip().startswith("#"):
                    break
                match = re.search(r"기준일\s+(\d{4}-\d{2}-\d{2})", line)
                if match:
                    return match.group(1)
            rows = list(
                csv.DictReader(
                    line for line in lines if not line.lstrip().startswith("#")
                )
            )
            if not rows:
                return None
            return (
                rows[-1].get("date")
                or rows[-1].get("as_of")
                or rows[-1].get("period_end")
            )
        if path.suffix == ".json":
            data = json.loads(path.read_text(encoding="utf-8"))
            return (
                data.get("period_end")
                or data.get("as_of")
                or data.get("collected_at")
                or data.get("built_at")
            )
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return None


def _parse_artifact_time(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KST)
    return parsed.astimezone(dt.timezone.utc)


def _collection_time(path: Path) -> dt.datetime | None:
    """파일 mtime이 아니라 산출물 내부의 회수·생성시각을 읽는다."""
    try:
        if path.suffix == ".csv":
            with path.open(encoding="utf-8-sig") as handle:
                for line in handle:
                    if not line.lstrip().startswith("#"):
                        break
                    retrieved = re.search(
                        r"retrieved_at\s+([^/]+?)(?:\s+/|$)",
                        line,
                    )
                    if retrieved:
                        parsed = _parse_artifact_time(retrieved.group(1).strip())
                        if parsed is not None:
                            return parsed
                    dated = re.search(
                        r"(?:수집일|조회일)\s+(\d{4}-\d{2}-\d{2})",
                        line,
                    )
                    if dated:
                        return _parse_artifact_time(dated.group(1))
            return None
        if path.suffix == ".json":
            data = load_json(path)
            for key in ("collected_at", "created_at", "built_at"):
                parsed = _parse_artifact_time(data.get(key))
                if parsed is not None:
                    return parsed
    except (OSError, ValueError):
        return None
    return None


def run_refresh(
    asset_id: str,
    *,
    include_optional: bool = False,
    force: bool = False,
) -> tuple[list[dict[str, Any]], int]:
    tasks = tasks_for(asset_id, include_optional=include_optional)
    log_dir = DATA_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    failures = 0
    for planned_task in tasks:
        task = inspect_task(planned_task)
        if not force and task.state == "FRESH":
            results.append({"task": task.name, "status": "SKIPPED_FRESH"})
            continue
        if task.state == "BLOCKED":
            results.append(
                {
                    "task": task.name,
                    "status": "DEPENDENCY_BLOCKED",
                    "reason": task.reason,
                }
            )
            if task.required:
                failures += 1
            continue
        if task.command is None:
            status = "MANUAL_REQUIRED" if task.required else "OPTIONAL_MISSING"
            results.append(
                {"task": task.name, "status": status, "reason": task.reason}
            )
            if task.required:
                failures += 1
            continue
        completed = subprocess.run(
            task.command,
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        log_path = log_dir / f"{task.name.replace(':', '-')}.log"
        log_path.write_text(
            (completed.stdout or "") + (completed.stderr or ""),
            encoding="utf-8",
        )
        verified = inspect_task(task)
        artifact_ready = verified.state == "FRESH"
        status = (
            "UPDATED"
            if completed.returncode == 0 and artifact_ready
            else "FAILED_ARTIFACT"
            if completed.returncode == 0
            else "FAILED"
        )
        results.append(
            {
                "task": task.name,
                "status": status,
                "returncode": completed.returncode,
                "log": str(log_path.relative_to(ROOT)),
                "artifact_state": verified.state,
            }
        )
        if (completed.returncode != 0 or not artifact_ready) and task.required:
            failures += 1
    return results, 1 if failures else 0


def plan_json(asset_id: str, *, include_optional: bool = False) -> dict[str, Any]:
    return {
        "schema_version": "refresh-plan/1.0.0",
        "asset": get_asset(asset_id),
        "created_at": dt.datetime.now(KST).isoformat(timespec="seconds"),
        "tasks": [
            asdict(task)
            for task in inspect_plan(asset_id, include_optional=include_optional)
        ],
    }
