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
from zoneinfo import ZoneInfo

from calculators.contracts import (
    content_hash,
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
    # 달러/원은 필수가 아니다. 없다고 판단을 세우지는 않지만, 원화로 보고하는
    # 수출 회사의 packet 에 분기 평균·기말 환율을 싣는 재료다
    # (`structured_optional.fx_usdkrw`).
    "KR": (("^KS11", True), ("^KQ11", True), ("^VIX", False), ("KRW=X", False)),
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
US_EARNINGS_FAILURE_RETRY_HOURS = 1


def safe_symbol(symbol: str) -> str:
    return symbol.replace(".", "_").replace("^", "").replace("=", "")


def dated_artifacts(directory: Path, prefix: str, suffix: str) -> list[Path]:
    """`<prefix><YYYY-MM-DD><suffix>` 꼴의 파일만, 날짜순으로.

    날짜가 붙는 산출물(재무 스냅샷, ETF 구성)은 여러 곳이 "가장 최근 것"을 고른다.
    고르는 규칙이 곳마다 달랐다 — refresh 는 파일 속 기준일로, packet 과 수집기는
    파일 이름 순으로. 이름과 기준일이 어긋난 파일이 하나 끼면 refresh 가 신선하다고
    본 파일과 packet 이 실은 파일이 서로 달라진다. 그리고 `IBM-*.json` 같은 느슨한
    무늬는 `IBM-B-2026-06-30.json`(다른 자산)까지 집는다. 한 함수로 고른다.
    """
    pattern = re.compile(
        re.escape(prefix) + r"\d{4}-\d{2}-\d{2}" + re.escape(suffix)
    )
    if not directory.is_dir():
        return []
    return sorted(
        path for path in directory.iterdir()
        if path.is_file() and pattern.fullmatch(path.name)
    )


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
        collected_time = dt.datetime.fromisoformat(collected_at or "")
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
    # 낡은 것과 틀린 것은 다르다. 둘 다 state 는 STALE 이지만, 기한이 지난 값은
    # 과거 관측으로 실을 수 있고 계약을 어긴 값(다른 회사·다른 스키마·원문과
    # 다른 숫자)은 실으면 안 된다. packet 조립이 이 칸으로 둘을 가른다.
    contract_error: str | None = None


# 가격을 "지금의 시세"로 부를 수 있는 기한. packet 이 판단에 쓰일 수 있는 기한
# (`cio/decision.py` 의 context 나이 검사)도 같은 값을 쓴다 — packet 은 만든
# 시점의 가격 위에 서 있으므로 가격보다 오래 유효할 수 없다.
PRICE_MAX_AGE_HOURS = 12


def _price_task(symbol: str, *, required: bool = True) -> RefreshTask:
    filename = safe_symbol(symbol)
    return RefreshTask(
        name=f"price:{symbol}",
        capability="PRICE",
        artifact=f"knowledge/prices/{filename}.csv",
        max_age_hours=PRICE_MAX_AGE_HOURS,
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


def _benchmark_backfill_task(
    symbol: str, companions: tuple[str, ...] = (),
) -> RefreshTask:
    """벤치마크 지수의 누락 거래일을 KRX 원문으로 메우는 작업.

    Yahoo 는 거래일을 통째로 빠뜨리고도 정상 응답을 준다. 메운 봉은 다음 price
    수집에서도 보존되지만(`collectors/price.py:merge_with_existing`) 새 구멍은
    수집할 때마다 생길 수 있으므로, 표식 파일이 가격 파일보다 오래되면 다시 돈다.

    `companions` 는 같은 계획에서 함께 수집되는 한국 시계열이다. 거래일 달력이
    그 파일들의 합집합이라, 다른 파일에 새 날짜가 들어오면 이 지수의 구멍이
    그때 처음 보인다 — 자기 파일만 의존성으로 걸면 "구멍 없음" 기록이 낡는다.

    필수가 아니다. 이것은 가격이 아니라 가격의 **보완**이다. 필수로 두면 KRX
    키가 없는 설치에서는 Yahoo 가격과 재무가 멀쩡해도 표식 파일 하나가 없다는
    이유로 모든 한국 자산의 판단이 막힌다. 메우지 못한 구멍은 packet 의
    `session_gaps` 가 말하고, 그 날을 지나는 값은 packet 이 싣지 않는다.
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
        required=False,
        dependencies=[
            f"knowledge/prices/{filename}.csv",
            *(f"knowledge/prices/{safe_symbol(other)}.csv"
              for other in companions if other != symbol),
        ],
    )


# `collectors.krx` 의 종목 서비스 경로와 같은 이름이다. ETF 는 거래소가 KOSPI 로
# 적혀 있어도 서비스가 따로다.
KRX_STOCK_MARKETS = {"KOSPI", "KOSDAQ", "KONEX"}


def _asset_backfill_task(asset: dict[str, Any]) -> RefreshTask | None:
    """종목·ETF 자기 시계열의 누락 거래일을 KRX 원문으로 메우는 작업.

    지수만 메우던 때에는 종목 쪽 구멍을 볼 방법이 없었다. 2026-10-13 수집에서
    파마리서치의 10-12 봉이 사라졌고, 그날의 packet 은 세션 수익률이 한 세션을
    건너뛴 채 `validation: PASS` 로 나갔다.

    필수가 아니다. 거래소 API 가 막혀도 판단을 세우지는 않는다 — 메우지 못한
    구멍은 packet 의 `price.session_gaps` 가 따로 말한다. ETF 서비스는 활용신청이
    별도라 신청하지 않았으면 이 작업만 실패한다.
    """
    if asset["market"] != "KR":
        return None
    market = "ETF" if asset["asset_type"] == "ETF" else asset.get("exchange")
    if market != "ETF" and market not in KRX_STOCK_MARKETS:
        return None
    symbol = asset["price_symbol"]
    filename = safe_symbol(symbol)
    return RefreshTask(
        name=f"price_backfill:{symbol}",
        capability="PRICE_BACKFILL",
        artifact=f"knowledge/prices/_backfill/{filename}.json",
        max_age_hours=12,
        command=[
            sys.executable,
            "-m",
            "collectors.krx",
            "backfill-stock",
            symbol.split(".", 1)[0],
            "--market",
            market,
        ],
        required=False,
        # 달력은 한국 시계열 전체의 합집합이다. 지수 파일에 새 날짜가 들어오면
        # 종목 쪽 구멍이 그때 처음 보인다.
        dependencies=[
            f"knowledge/prices/{filename}.csv",
            *(f"knowledge/prices/{safe_symbol(index)}.csv"
              for index in KR_INDEX_MARKETS),
        ],
    )


def tasks_for(asset_id: str, *, include_optional: bool = False) -> list[RefreshTask]:
    return tasks_for_asset(get_asset(asset_id), include_optional=include_optional)


def tasks_for_asset(
    asset: dict[str, Any], *, include_optional: bool = False
) -> list[RefreshTask]:
    tasks: list[RefreshTask] = [_price_task(asset["price_symbol"])]

    market_symbols = list(asset["benchmarks"])
    for symbol in market_symbols:
        tasks.append(_price_task(symbol))
    for symbol, required in MARKET_PRICE_REQUIREMENTS[asset["market"]]:
        tasks.append(_price_task(symbol, required=required))
    # 가격 수집 뒤에 놓는다 — 덮어쓴 파일 위에서 다시 메워야 한다.
    planned = list(dict.fromkeys(
        [*market_symbols, *(s for s, _ in MARKET_PRICE_REQUIREMENTS[asset["market"]])]
    ))
    korean_series = tuple(
        symbol for symbol in planned if symbol in KR_INDEX_MARKETS
    ) + ((asset["price_symbol"],) if asset["market"] == "KR" else ())
    for symbol in planned:
        if symbol in KR_INDEX_MARKETS:
            tasks.append(_benchmark_backfill_task(symbol, korean_series))
    asset_backfill = _asset_backfill_task(asset)
    if asset_backfill:
        tasks.append(asset_backfill)

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
            # 색인에 있는데 안 받아서 못 본 것은 데이터가 없는 것이 아니다.
            # 2026-08-07 판단은 접수번호까지 알면서 잠정실적 원문 수집을
            # 사용자 대기열로 넘겼고, 그 안에 있던 품목군별 매출이 이틀 뒤에야
            # 논지를 바꿨다. 색인이 갱신되면 함께 돈다 (dependencies).
            #
            # 잠정실적만 받는다 — 정기보고서는 100만 자라 이 배선이 감당할
            # 문서가 아니고, 필요한 자리만 `cio search` 로 읽는다.
            #
            # required=False 인 것은 의도다. 원문 하나를 못 받았다고 packet 을
            # BLOCKED 로 만들면, 있으면 좋은 입력 때문에 판단 자체가 멈춘다.
            tasks.append(
                RefreshTask(
                    name=f"filing_documents:{asset['asset_id']}",
                    capability="FILING_DOCUMENTS",
                    artifact=(
                        f"knowledge/filings/_documents/{asset['asset_id']}.json"
                    ),
                    max_age_hours=720,
                    command=[
                        sys.executable,
                        "-m",
                        "collectors.dart",
                        "documents",
                        asset["asset_id"],
                    ],
                    required=False,
                    dependencies=[
                        f"knowledge/filings/{asset['asset_id']}.json"
                    ],
                )
            )
            # 색인은 "결산실적공시예고"라는 제목만 갖는다. 판단을 바꾸는 값인
            # 공시예정일은 원문 본문에만 있어서, 이벤트까지 며칠 남았는지 알려면
            # 매번 원문을 열어야 했다. 이벤트 전에 살 것인가는 매수 판단의 1차
            # 분기점이므로 렌즈가 아니라 기본 입력으로 둔다.
            tasks.append(
                RefreshTask(
                    name=f"calendar:{asset['asset_id']}",
                    capability="EARNINGS_CALENDAR",
                    artifact=f"knowledge/calendar/{asset['asset_id']}.json",
                    # 예고 공시가 새로 뜨거나 발표일이 바뀌면 바로 반영해야 한다.
                    max_age_hours=24,
                    dependencies=[
                        f"knowledge/filings/{asset['asset_id']}.json"
                    ],
                    command=[
                        sys.executable,
                        "-m",
                        "collectors.kr_calendar",
                        asset["asset_id"],
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
            # 제출일 추정과 회사 IR의 실제 발표 안내는 별도 자료다.
            tasks.append(
                RefreshTask(
                    name=f"calendar:{asset['asset_id']}",
                    capability="EARNINGS_CALENDAR",
                    artifact=f"knowledge/calendar/{asset['asset_id']}.json",
                    max_age_hours=24,
                    dependencies=[
                        f"knowledge/filings/{asset['asset_id']}.json"
                    ],
                    command=[
                        sys.executable,
                        "-m",
                        "collectors.us_calendar",
                        asset["asset_id"],
                    ],
                )
            )
            for name, capability, module, directory, dependencies in (
                ("us_capital", "US_CAPITAL", "collectors.us_capital", "us_capital", [f"knowledge/filings/{asset['asset_id']}.json"]),
                ("us_segments", "US_SEGMENTS", "collectors.us_segments", "us_segments", [f"knowledge/filings/{asset['asset_id']}.json"]),
                ("earnings_announced", "US_EARNINGS", "collectors.us_earnings", "us_earnings", ["config/assets.json"]),
            ):
                command = None if capability == "US_EARNINGS" and not asset.get("ir_sources") else [
                    sys.executable, "-m", module, asset["asset_id"]
                ]
                tasks.append(RefreshTask(
                    name=f"{name}:{asset['asset_id']}", capability=capability,
                    artifact=f"knowledge/{directory}/{asset['asset_id']}.json",
                    max_age_hours=24, required=False, dependencies=dependencies,
                    command=command,
                ))
        else:
            raise ValueError(
                f"{asset['asset_id']}: 지원하지 않는 filing_source {filing_source}"
            )

        if asset["market"] == "KR":
            tasks.append(RefreshTask(
                name=f"capital:{asset['asset_id']}", capability="KR_CAPITAL",
                artifact=f"knowledge/capital/{asset['asset_id']}.json", max_age_hours=24,
                command=[sys.executable, "-m", "collectors.kr_capital", asset["asset_id"]],
                required=False, dependencies=[f"knowledge/filings/{asset['asset_id']}.json"],
            ))
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
        # 급변일 ↔ 공시 대조는 두 시장 모두에 있다. 판정 로직은 한 곳
        # (kr_events.build_event_packet)이고 공시를 어디서 읽는지만 갈린다 —
        # 한국은 DART 를 그때 부르고, 미국은 이미 모아 둔 EDGAR 색인을 읽는다.
        primary_benchmark = asset["benchmarks"][0]
        events_module = (
            "collectors.kr_events" if asset["market"] == "KR"
            else "collectors.us_events"
        )
        tasks.append(
            RefreshTask(
                name=f"events:{asset['asset_id']}",
                capability="EVENTS",
                artifact=f"knowledge/events/{asset['asset_id']}.json",
                max_age_hours=24,
                command=[
                    sys.executable,
                    "-m",
                    events_module,
                    asset["asset_id"],
                    "--symbol",
                    asset["price_symbol"],
                    "--benchmark",
                    primary_benchmark,
                ],
                dependencies=[
                    f"knowledge/prices/{safe_symbol(asset['price_symbol'])}.csv",
                    f"knowledge/prices/{safe_symbol(primary_benchmark)}.csv",
                    # 미국판은 SEC 를 다시 부르지 않고 공시 색인을 읽는다.
                    # 색인이 갱신되면 대조를 다시 해야 한다.
                    *([f"knowledge/filings/{asset['asset_id']}.json"]
                      if asset["market"] != "KR" else []),
                    # 백필은 원래 retrieved_at 을 보존하므로 가격 파일의
                    # 회수시각만 보면 구멍이 메워진 것을 알 수 없다. 표식이
                    # 갱신되면 상대변동을 다시 계산해야 한다.
                    *([
                        "knowledge/prices/_backfill/"
                        f"{safe_symbol(primary_benchmark)}.json"
                    ] if primary_benchmark in KR_INDEX_MARKETS and (
                        ROOT / "knowledge" / "prices" / "_backfill"
                        / f"{safe_symbol(primary_benchmark)}.json").exists()
                      else []),
                    # 백필은 필수가 아니라 표식이 없을 수 있다. 없는 입력을
                    # 의존성으로 걸면 급변 대조가 통째로 막히므로, 지수·종목
                    # 모두 한 번이라도 돈 뒤에만 건다.
                    *([asset_backfill.artifact] if asset_backfill and (
                        ROOT / asset_backfill.artifact).exists() else []),
                ],
            )
        )
    else:
        holdings_source = asset["etf"]["holdings_source"]
        # 운용사 수집 경로가 확인된 ETF 는 구성종목을 자동으로 받는다. 경로가
        # 없으면 예전처럼 사람이 원문을 확인해 저장해야 한다 — 그 사실을
        # 숨기지 않고 MANUAL_REQUIRED 로 남긴다.
        from collectors.kr_etf_holdings import ISSUERS as ETF_ISSUERS

        issuer = asset["etf"].get("issuer")
        automated = issuer in ETF_ISSUERS and bool(asset["etf"].get("isin"))
        tasks.append(
            RefreshTask(
                name=f"holdings:{asset['asset_id']}",
                capability="ETF_HOLDINGS",
                artifact=f"knowledge/etfs/{asset['asset_id']}/holdings-*.csv",
                max_age_hours=168,
                command=(
                    [
                        sys.executable,
                        "-m",
                        "collectors.kr_etf_holdings",
                        asset["asset_id"],
                    ]
                    if automated
                    else None
                ),
                reason=(
                    None
                    if automated
                    else f"{holdings_source} 등급의 최신 구성종목 스냅샷을 "
                    "확인해 저장해야 한다. 자동 수집은 registry 의 etf.issuer·"
                    "etf.isin 이 있고 그 운용사 경로가 확인된 ETF 만 된다"
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
    if "KR_LENDING" in enabled:
        tasks.append(
            RefreshTask(
                name=f"lending:{asset_id}",
                capability="KR_LENDING",
                artifact=f"knowledge/lending/{asset_id}.csv",
                max_age_hours=24,
                command=[
                    sys.executable,
                    "-m",
                    "collectors.kr_lending",
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
    if "KR_SEGMENTS" in enabled:
        tasks.append(
            RefreshTask(
                name=f"segments:{asset['asset_id']}",
                capability="KR_SEGMENTS",
                artifact=f"knowledge/segments/{asset['asset_id']}.json",
                # 정기보고서에서 나오므로 분기에 한 번만 바뀐다. 공시 색인에
                # 시간 의존을 걸지 않는다 — 걸면 색인을 다시 받을 때마다(매일)
                # 같은 보고서에서 같은 표를 다시 뽑는다. 무효화는 내용으로 한다:
                # 색인의 최신 정기보고서가 이 자료의 보고서와 다를 때만 낡았다
                # (`collectors.kr_segments.contract_errors`).
                max_age_hours=2160,
                required=False,
                command=[
                    sys.executable,
                    "-m",
                    "collectors.kr_segments",
                    asset["asset_id"],
                ],
            )
        )
    if "KR_PRODUCT_SALES" in enabled:
        # 정기보고서에 부문표가 없고 잠정실적 본문에만 품목군별 매출을 적는 회사용.
        # 네트워크를 타지 않는다 — filing_documents 가 받아 둔 원문을 읽으므로
        # 그 작업이 새 원문을 받으면(표식 파일이 갱신되면) 다시 돈다.
        tasks.append(
            RefreshTask(
                name=f"product_sales:{asset['asset_id']}",
                capability="KR_PRODUCT_SALES",
                artifact=f"knowledge/product_sales/{asset['asset_id']}.json",
                max_age_hours=720,
                required=False,
                dependencies=[
                    f"knowledge/filings/_documents/{asset['asset_id']}.json"
                ],
                command=[
                    sys.executable,
                    "-m",
                    "collectors.kr_product_sales",
                    asset["asset_id"],
                ],
            )
        )

    if "KR_EPC" in enabled:
        tasks.append(RefreshTask(
            name=f"epc:{asset_id}", capability="KR_EPC",
            artifact=f"knowledge/epc/{asset_id}.json", max_age_hours=2160,
            required=False, dependencies=[f"knowledge/filings/{asset_id}.json"],
            command=[sys.executable, "-m", "collectors.kr_epc", asset_id],
        ))

    if "ETF_CONSTITUENTS" in enabled:
        # 구성종목별 가격·배수·환율. look-through 배수와 수익률 분해의 재료다.
        # 구성이 바뀌면(holdings 가 새로 수집되면) 다시 받는다. 가격 파일에는
        # 의존을 걸지 않는다 — 걸면 12시간마다 종목 수십 개를 다시 요청한다.
        tasks.append(
            RefreshTask(
                name=f"constituents:{asset_id}",
                capability="ETF_CONSTITUENTS",
                artifact=f"knowledge/etfs/{asset_id}/constituents-*.json",
                max_age_hours=168,
                required=False,
                dependencies=[f"knowledge/etfs/{asset_id}/holdings-*.csv"],
                command=[
                    sys.executable,
                    "-m",
                    "collectors.etf_constituents",
                    asset_id,
                ],
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


# 일봉 행 간격의 중앙값 상한. 주말·연휴가 끼어도 중앙값은 1~2일에 머물고,
# 주봉(7)·월봉(30)은 이 위에 있다. 표본이 적으면 판정하지 않는다 — 갓 상장한
# 종목이나 백필 중인 파일을 간격 부족만으로 계약 위반으로 몰지 않는다.
MAX_DAILY_GAP_DAYS = 4
MIN_BARS_TO_JUDGE_GRANULARITY = 5


def _non_daily_reason(dates: list[str]) -> str | None:
    """가격 파일이 일봉이 아니면 그 사실을, 판정할 수 없으면 None 을 돌려준다."""
    if len(dates) < MIN_BARS_TO_JUDGE_GRANULARITY:
        return None
    try:
        days = [dt.date.fromisoformat(value) for value in dates]
    except ValueError:
        return "가격 파일의 날짜 열을 읽을 수 없다"
    gaps = sorted((days[i] - days[i - 1]).days for i in range(1, len(days)))
    median_gap = gaps[len(gaps) // 2]
    if median_gap > MAX_DAILY_GAP_DAYS:
        return (
            f"가격 파일이 일봉이 아니다 — 행 간격 중앙값 {median_gap}일. "
            "변동성·이동평균·급변 판정이 한 행 = 하루를 가정하므로 이대로는 "
            "쓸 수 없다. collectors.price history 로 --range 10y 이하로 다시 받는다"
        )
    return None


def _latest_ttm_period(data: dict[str, Any]) -> str | None:
    """정상화 packet 이 실제로 덮는 마지막 분기.

    순서를 믿지 않고 최대값을 고른다 — 정렬은 생성기의 약속이고, 여기서
    확인하려는 것이 바로 그 생성기의 산출물이다.
    """
    periods: list[str] = [
        row.get("period_end")  # type: ignore[misc]
        for row in (data.get("ttm_series") or [])
        if isinstance(row, dict) and isinstance(row.get("period_end"), str)
    ]
    return max(periods) if periods else None


def _latest_fundamentals_period(asset_id: str) -> str | None:
    """이 자산에 대해 지금 손에 있는 가장 최근 재무 스냅샷의 기준일.

    파일명에서 읽는다. 파일명과 period_end 가 같다는 것은 FUNDAMENTALS 계약이
    이미 강제하므로(위 분기 참조), 스냅샷을 전부 열지 않아도 된다.
    """
    pattern = re.compile(re.escape(asset_id) + r"-(\d{4}-\d{2}-\d{2})\.json$")
    periods = [
        match.group(1)
        for candidate in (ROOT / "knowledge" / "fundamentals").glob(
            f"{asset_id}-*.json"
        )
        for match in [pattern.search(candidate.name)]
        if match
    ]
    return max(periods) if periods else None


# 2차 출처 CSV 의 계약: (이름, 있어야 하는 열, 최근 행에 값이 있어야 하는 열).
#
# 이 셋에는 계약 분기가 아예 없었다. 그래서 순매수 열이 통째로 없는 수급 파일이
# FRESH 로 통과했고 packet 은 결측을 순매수 0주로 실었다.
SECONDARY_CSV_CONTRACTS = {
    "KR_FLOW": ("수급", ("date", "institution_net", "foreign_net"),
                ("institution_net", "foreign_net")),
    "KR_LENDING": ("대차잔고", ("date", "balance_shares"), ("balance_shares",)),
    "KR_INVENTORY": ("재고자산", ("period_end", "inventories"), ("inventories",)),
}
SECONDARY_CSV_RECENT_ROWS = 20


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _secondary_csv_error(
    path: Path,
    asset_id: str,
    *,
    label: str,
    required: tuple[str, ...],
    value_columns: tuple[str, ...],
) -> str | None:
    try:
        with path.open(encoding="utf-8-sig") as handle:
            lines = list(handle)
    except OSError:
        return f"{label} 파일을 읽을 수 없다"
    # 파일 이름이 아니라 파일이 스스로 말하는 종목을 본다. 수집기는 첫 줄에
    # 종목코드를 적는다.
    first = lines[0] if lines else ""
    if not re.match(rf"#\s*{re.escape(asset_id)}(\s|$)", first):
        return f"{label} 파일 머리말의 종목이 요청 자산과 다르거나 없다"
    rows = list(
        csv.DictReader(line for line in lines if not line.lstrip().startswith("#"))
    )
    if not rows or not set(required).issubset(rows[0]):
        return f"{label} 파일에 필수 열({', '.join(required)}) 또는 행이 없다"
    recent = rows[-SECONDARY_CSV_RECENT_ROWS:]
    if not any(_finite(row.get(column)) for row in recent for column in value_columns):
        return (
            f"{label} 파일의 최근 {len(recent)}행에 {'·'.join(value_columns)} 값이 "
            "하나도 없다 — 결측을 0 으로 읽지 않는다"
        )
    return None


def _artifact_contract_error(task: RefreshTask, path: Path) -> str | None:
    if task.capability in {"US_CAPITAL", "US_SEGMENTS", "US_EARNINGS"}:
        import importlib
        modules = {"US_CAPITAL": "collectors.us_capital", "US_SEGMENTS": "collectors.us_segments", "US_EARNINGS": "collectors.us_earnings"}
        try:
            data = load_json(path)
            errors = importlib.import_module(modules[task.capability]).validate_artifact(data, task.name.split(":", 1)[1])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return f"미국 원문 산출물 검증 실패: {exc}"
        if errors:
            return "; ".join(errors[:3])
        if data.get("status", data.get("state")) in {"NETWORK_ERROR", "PARSE_ERROR", "INVALID"}:
            return data.get("reason") or "미국 원문 수집/추출이 실패했다"
    if task.capability == "PRICE":
        try:
            with path.open(encoding="utf-8-sig") as handle:
                first_line = handle.readline().strip()
                metadata_line = handle.readline().strip()
                retrieval_line = handle.readline().strip()
                session_line = handle.readline().strip()
                body = csv.reader(
                    line
                    for line in handle
                    if not line.lstrip().startswith("#")
                )
                header = next(body, [])
                data_rows = [row for row in body if row]
                dates = [row[0] for row in data_rows]
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
        session_metadata = re.fullmatch(
            r"# last_bar (\d{4}-\d{2}-\d{2}) / session "
            r"(SETTLED|UNSETTLED|UNKNOWN)",
            session_line,
        )
        if not session_metadata:
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
        if not dates:
            return "가격 파일에 마지막 봉을 대조할 데이터 행이 없다"
        try:
            dt.date.fromisoformat(session_metadata.group(1))
            dt.date.fromisoformat(dates[-1])
        except ValueError:
            return "가격 파일의 마지막 봉 날짜가 유효하지 않다"
        if session_metadata.group(1) != dates[-1]:
            return "가격 파일의 last_bar 날짜가 실제 마지막 행과 다르다 — 세션 상태를 적용할 수 없다"
        # 마지막 행의 종가가 숫자가 아니면 packet 은 그 행을 버리고 하루 전 값을
        # 현재가로 쓰면서, 세션 상태는 버린 날짜의 것을 그대로 붙인다.
        try:
            last_close = float(data_rows[-1][header.index("close")])
        except (IndexError, ValueError):
            last_close = math.nan
        if not math.isfinite(last_close):
            return "가격 파일 마지막 행의 종가가 숫자가 아니다"
        # 신선도는 회수시각으로 잰다. 그러나 **지금 받았다는 것과 지금의 시세라는
        # 것은 다르다** — 과거에 끝난 시계열을 오늘 받아도 회수시각은 오늘이다.
        # 마지막 봉이 회수일과 너무 멀면 현재 시세가 아니고, 회수일보다 뒤면
        # 관측이 아니다.
        lag_days = (retrieved_at.date() - dt.date.fromisoformat(dates[-1])).days
        if lag_days < -1:
            return (
                f"가격 파일의 마지막 봉 {dates[-1]} 이 회수시각보다 뒤다 — "
                "미래 날짜의 시세는 관측이 아니다"
            )
        if lag_days > MAX_PRICE_BAR_LAG_DAYS:
            return (
                f"가격 파일의 마지막 봉이 {dates[-1]} 로 회수일보다 {lag_days}일 "
                "앞선다 — 거래정지·상장폐지이거나 응답이 과거 구간만 실었다. "
                "현재 시세로 쓸 수 없다"
            )
        # 한 행 = 하루라는 것이 이 파일을 읽는 모든 계산의 전제다. 수집기가
        # 월봉 저장을 막지만 그것은 앞으로 저장될 파일에만 걸린다 — 이미
        # 디스크에 있는 파일은 여기서 걸러야 모델에 실려 나가지 않는다.
        error = _non_daily_reason(dates)
        if error:
            return error
    if task.capability in SECONDARY_CSV_CONTRACTS:
        label, required, value_columns = SECONDARY_CSV_CONTRACTS[task.capability]
        error = _secondary_csv_error(
            path, task.name.split(":", 1)[-1],
            label=label, required=required, value_columns=value_columns,
        )
        if error:
            return error
    if task.capability == "KR_SEGMENTS":
        from collectors.kr_segments import contract_errors as segment_errors

        asset_id = task.name.split(":", 1)[-1]
        index_path = ROOT / "knowledge" / "filings" / f"{asset_id}.json"
        try:
            errors = segment_errors(
                load_json(path), asset_id,
                index=load_json(index_path) if index_path.exists() else None,
                root=ROOT,
            )
        except (OSError, ValueError) as exc:
            return f"부문 자료를 읽을 수 없다: {exc}"
        if errors:
            return errors[0]
    if task.capability == "KR_PRODUCT_SALES":
        try:
            data = load_json(path)
        except (OSError, ValueError):
            return "품목군별 매출 자료를 읽을 수 없다"
        if data.get("schema_version") != "kr-product-sales/1.0.0":
            return "kr-product-sales/1.0.0이 필요하다"
        if data.get("asset_id") != task.name.split(":", 1)[-1]:
            return "품목군별 매출 자료의 asset_id가 요청 자산과 다르다"
    if task.capability == "KR_EPC":
        from collectors.kr_epc import contract_errors
        try:
            errors = contract_errors(load_json(path), task.name.split(":", 1)[-1])
        except (OSError, ValueError) as exc:
            return f"EPC 자료를 읽을 수 없다: {exc}"
        if errors:
            return errors[0]
    if task.capability == "KR_CAPITAL":
        from collectors.kr_capital import contract_errors
        try:
            asset_id = task.name.split(":", 1)[1]
            errors = contract_errors(load_json(path), asset_id, index=load_json(ROOT / "knowledge" / "filings" / f"{asset_id}.json"))
        except (OSError, ValueError, KeyError) as exc:
            return str(exc)
        if errors:
            return errors[0]
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
        components = data.get("components") or {}
        if data.get("state") == "NO_DATA" and (components.get("estimates") or {}).get("state") == "NO_DATA":
            return None  # Fresh observation of absence, not a failed download.
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
    if task.capability in {"BENCHMARK_BACKFILL", "PRICE_BACKFILL"}:
        try:
            data = load_json(path)
        except (OSError, ValueError):
            return "가격 백필 기록을 읽을 수 없다"
        if data.get("schema_version") != "price-backfill/1.0.0":
            return "price-backfill/1.0.0이 필요하다"
        filled = data.get("filled")
        target = data.get("target")
        if not isinstance(filled, list) or not isinstance(target, str):
            return "가격 백필 기록에 메운 거래일 목록이 없다"
        # 기록이 스스로를 증명해야 한다. 대상 파일이 어떤 경로로든 다시 쓰여
        # 메웠던 거래일이 사라지면, 기록만 남아 '메웠다'고 거짓말한다.
        target_path = ROOT / "knowledge" / "prices" / target
        if filled:
            if not target_path.exists():
                return "가격 백필 대상 가격 파일이 없다"
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
                return "가격 백필 대상 가격 파일을 읽을 수 없다"
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
            or expected_locator is None
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
        # 지문은 파일이 보존됐다는 증거이지 그 안의 숫자가 원문과 같다는 증거가
        # 아니다. 수집기의 도출 함수를 봉인된 원문에 다시 돌려 값을 대조한다.
        # 추출 규칙이 바뀌어 옛 규칙의 스냅샷이 틀려진 경우도 여기서 걸린다 —
        # SK하이닉스의 총차입은 옛 분류로 2.5조, 지금 분류로 21.8조다.
        if raw_kind == "dart":
            from collectors.dart import replay_fundamentals
        else:
            from collectors.edgar import replay_fundamentals
        differences = replay_fundamentals(data, raw_document["payload"])
        if differences:
            return (
                "재무 스냅샷이 봉인된 원문에서 그대로 다시 나오지 않는다 — "
                f"{differences[0]}"
                + (f" 외 {len(differences) - 1}건" if len(differences) > 1 else "")
                + ". 다시 수집한다"
            )
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
            or expected_locator is None
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
    if task.capability == "NORMALIZED_EARNINGS":
        try:
            data = load_json(path)
        except (OSError, ValueError):
            return "정상화 packet JSON을 읽을 수 없다"
        asset_id = task.name.split(":", 1)[-1]
        if data.get("instrument_id") != asset_id:
            return "정상화 packet의 instrument_id가 요청 자산과 다르다"
        covered = _latest_ttm_period(data)
        if covered is None:
            return "정상화 packet에 TTM 시계열이 없다"
        available = _latest_fundamentals_period(asset_id)
        # 수집 시각으로는 이 상태를 볼 수 없다. 실제로 이 저장소의 정상화
        # packet 은 재무 스냅샷보다 **2분 늦게** 만들어졌으면서 데이터는 한
        # 분기 적었다 — 시각만 보는 의존성 비교에는 최신으로 보인다.
        #
        # 낡은 정상화값은 틀린 값이 아니라 **오래된 값**이라 경고 없이 그대로
        # 읽힌다. 실측으로 5.02배와 6.19배의 차이였고, 이 배수는 "지금 이익이
        # 정상 대비 얼마나 부풀었나"라서 판단을 바꾼다.
        if available is not None and covered < available:
            return (
                f"정상화 packet 이 재료보다 낡았다 — TTM 은 {covered} 까지인데 "
                f"재무 스냅샷은 {available} 까지 있다. 다시 계산하지 않으면 "
                "사이클 배수가 한 분기 전 값으로 읽힌다"
            )
    if task.capability == "EARNINGS_CALENDAR":
        try:
            data = load_json(path)
        except (OSError, ValueError):
            return "실적 캘린더 JSON을 읽을 수 없다"
        if data.get("schema_version") != "earnings-calendar/1.0.0":
            return "earnings-calendar/1.0.0이 필요하다"
        if data.get("asset_id") != task.name.split(":", 1)[-1]:
            return "실적 캘린더의 asset_id가 요청 자산과 다르다"
        upcoming = data.get("next_provisional_result")
        if upcoming is not None:
            if not isinstance(upcoming, dict):
                return "실적 캘린더의 발표 예정 항목이 객체가 아니다"
            # 날짜가 원문에서 온 것임을 근거로 되짚을 수 없으면 쓰지 않는다.
            if not str(upcoming.get("evidence_ref") or "").startswith(
                "DARTDOC:"
            ):
                return "발표 예정일에 공시 원문 근거가 없다"
            try:
                dt.date.fromisoformat(str(upcoming.get("disclosure_date")))
            except (TypeError, ValueError):
                return "발표 예정일이 날짜 형식이 아니다"
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
        # 이름의 날짜로 최신 구성을 고르므로 이름과 내용의 기준일이 같아야 한다.
        if path.name != f"holdings-{next(iter(as_of_values))}.csv":
            return (
                f"ETF 구성 파일명({path.name})과 행의 기준일"
                f"({next(iter(as_of_values))})이 다르다"
            )
        try:
            weights = [float(row["weight_pct"]) for row in rows]
        except (TypeError, ValueError):
            return "ETF weight_pct가 숫자가 아니다"
        if (
            any(not math.isfinite(weight) or weight < 0 for weight in weights)
            or sum(weights) <= 0
        ):
            return "ETF weight_pct가 유효하지 않다"
        # 자동 수집본은 기준일이 아니라 조회시각으로 신선도를 잰다(아래
        # inspect_task). 그러면 운용사가 갱신을 멈춘 구성도 조회만 하면 계속
        # 신선해 보이므로, 기준일이 조회일보다 한참 뒤처지면 여기서 막는다.
        retrieved = _holdings_retrieved_at(path)
        as_of = _parse_artifact_time(next(iter(as_of_values)))
        if (
            retrieved is not None
            and as_of is not None
            and (retrieved - as_of).days > MAX_HOLDINGS_LAG_DAYS
        ):
            return (
                f"운용사 구성 기준일이 조회일보다 {(retrieved - as_of).days}일 "
                "뒤처졌다 — 운용사가 갱신을 멈췄거나 다른 펀드를 받았다"
            )
    if task.capability == "ETF_CONSTITUENTS":
        from collectors.etf_constituents import validate_artifact

        asset_id = task.name.split(":", 1)[-1]
        try:
            data = load_json(path)
        except (OSError, ValueError):
            return "구성종목 재료 JSON을 읽을 수 없다"
        errors = validate_artifact(data, asset_id)
        if errors:
            return errors[0]
        # 재료가 어느 구성 위에서 만들어졌는지. 구성 기준일이 바뀌었는데 옛
        # 재료를 실으면 빠진 종목의 배수가 지금 ETF 의 것처럼 읽힌다.
        holdings = dated_artifacts(
            ROOT / "knowledge" / "etfs" / asset_id, "holdings-", ".csv"
        )
        if holdings:
            current = holdings[-1].stem.removeprefix("holdings-")
            if data.get("holdings_as_of") != current:
                return (
                    f"구성종목 재료는 {data.get('holdings_as_of')} 구성 위에서 "
                    f"만들어졌는데 지금 구성은 {current} 이다"
                )
    return None


# 한국 영업일 기준으로 가장 긴 연휴(추석+주말)도 열흘을 넘지 않는다.
MAX_HOLDINGS_LAG_DAYS = 14
MAX_PRICE_BAR_LAG_DAYS = 14


def _holdings_retrieved_at(path: Path) -> dt.datetime | None:
    """수집기가 쓴 구성 파일의 조회시각. 손으로 적은 파일에는 없다."""
    try:
        with path.open(encoding="utf-8-sig") as handle:
            for line in handle:
                if not line.lstrip().startswith("#"):
                    break
                match = re.search(r"retrieved_at\s+([^/]+?)(?:\s+/|$)", line)
                if match:
                    return _parse_artifact_time(match.group(1).strip())
    except OSError:
        return None
    return None


def inspect_task(task: RefreshTask, *, now: dt.datetime | None = None) -> RefreshTask:
    now = now or dt.datetime.now(dt.timezone.utc)
    if task.capability == "US_EARNINGS" and task.command is None:
        task.state = "NOT_REGISTERED"
        task.reason = "config/assets.json에 공식 IR URL(ir_sources)을 등록해야 실적 발표일을 수집할 수 있다"
        return task
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
    # 날짜가 붙는 산출물은 이름이 계약의 일부다. 무늬에 걸렸다는 이유로 다른
    # 자산의 파일이나 날짜 없는 이름을 후보로 삼지 않는다.
    dated_prefix = {
        "FUNDAMENTALS": f"{task.name.split(':', 1)[-1]}-",
        "ETF_HOLDINGS": "holdings-",
    }.get(task.capability)
    if dated_prefix and matches:
        suffix = Path(task.artifact).suffix
        named = dated_artifacts(matches[0].parent, dated_prefix, suffix)
        if not named and task.capability == "ETF_HOLDINGS":
            task.state = "STALE"
            task.reason = (
                "ETF 구성 파일이 있지만 이름이 holdings-<기준일>.csv 가 아니다 "
                f"({', '.join(sorted(path.name for path in matches)[:3])}). "
                "packet 과 수집기는 그 이름으로 최신 구성을 고른다"
            )
            task.contract_error = task.reason
            return task
        matches = [path for path in matches if path in named]
    if not matches:
        task.state = "MISSING"
        return task
    path = max(
        matches,
        key=lambda item: (_data_as_of(item) or "", item.stat().st_mtime),
    )
    task.resolved_path = path.relative_to(ROOT).as_posix()
    task.data_as_of = _data_as_of(path)
    freshness_time = None
    if task.capability == "ETF_HOLDINGS":
        # 수집기가 받은 파일은 조회시각이 신선도다. 기준일로 재면 연휴 뒤에는
        # 방금 받은 최신 구성도 낡은 것이 되어, 필수 입력이라 packet 이 멈춘다.
        # 손으로 적은 파일은 조회시각을 믿을 근거가 없어 예전처럼 기준일로 잰다.
        freshness_time = _holdings_retrieved_at(path)
    if (
        freshness_time is None
        and task.capability in {"ETF_HOLDINGS", "PORTFOLIO"}
        and task.data_as_of
    ):
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
        task.contract_error = contract_error

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
    # 시각 비교만으로는 **회수시각을 바꾸지 않는 내용 변화**를 못 본다. 거래소
    # 백필은 가격 파일에 빠진 봉을 채우면서 원래 `retrieved_at` 을 보존한다 — 그
    # 위에서 계산된 시장 상태는 입력이 바뀌었는데도 신선하다고 남았다(지수 봉
    # 하나가 채워져 NORMAL·0 이 STRESS·3 이 되는데 packet 은 계속 NORMAL 이었다).
    # 산출물이 자기가 읽은 입력의 지문을 적어 뒀으면 지금 파일과 내용으로 대조한다.
    changed = _changed_inputs(
        path, [dep for paths in dependency_matches.values() for dep in paths]
    )
    if changed:
        task.state = "STALE"
        task.reason = (
            "계산에 쓴 입력의 내용이 그 뒤에 바뀌었다: " + ", ".join(changed[:3])
            + (f" 외 {len(changed) - 3}건" if len(changed) > 3 else "")
        )
    elif changed is None and task.capability == "MARKET_STATE":
        task.state = "STALE"
        task.reason = (
            "시장 상태 산출물에 입력 지문이 없다 — 입력이 바뀌었는지 대조할 수 "
            "없으므로 다시 계산한다"
        )
    return task


def _changed_inputs(artifact: Path, dependencies: list[Path]) -> list[str] | None:
    """산출물이 적어 둔 입력 지문과 지금 파일을 대조한다.

    → 내용이 달라졌거나 사라진 입력, 그리고 선언된 의존 입력인데 계산에 쓰이지
    않은 것의 경로 목록. 지문을 적지 않는 산출물이면 None(대조할 것이 없다).
    """
    if artifact.suffix != ".json":
        return None
    try:
        recorded = json.loads(artifact.read_text(encoding="utf-8")).get(
            "input_fingerprints"
        )
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(recorded, dict):
        return None
    changed = []
    for locator, digest in sorted(recorded.items()):
        source = (ROOT / str(locator)).resolve()
        try:
            source.relative_to(ROOT.resolve())
            current = content_hash(source)
        except (OSError, ValueError):
            current = None
        if current != digest:
            changed.append(str(locator))
    known = {(ROOT / str(locator)).resolve() for locator in recorded}
    for dependency in dependencies:
        if dependency.resolve() not in known:
            try:
                changed.append(dependency.resolve().relative_to(ROOT.resolve()).as_posix())
            except ValueError:
                changed.append(str(dependency))
    return changed


def inspect_plan(
    asset_id: str, *, include_optional: bool = False
) -> list[RefreshTask]:
    return [
        inspect_task(task)
        for task in tasks_for(asset_id, include_optional=include_optional)
    ]


def inspect_asset_plan(
    asset: dict[str, Any], *, include_optional: bool = False
) -> list[RefreshTask]:
    """registry 를 다시 읽지 않고, 넘겨받은 자산 정의 그대로 계획을 점검한다."""
    return [
        inspect_task(task)
        for task in tasks_for_asset(asset, include_optional=include_optional)
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


def _recent_us_earnings_failure(
    task: RefreshTask, *, now: dt.datetime,
) -> dt.datetime | None:
    """Delay repeated failed IR requests while keeping their failure visible."""
    if (task.capability != "US_EARNINGS" or task.state != "STALE"
            or task.command is None or not task.resolved_path):
        return None
    try:
        path = (ROOT / task.resolved_path).resolve()
        if not path.is_relative_to(ROOT.resolve()):
            return None
        data = load_json(path)
        asset_id = task.name.split(":", 1)[1]
        if (data.get("status") not in {"NETWORK_ERROR", "PARSE_ERROR"}
                or data.get("asset_id") != asset_id
                or data.get("configured_sources") != (get_asset(asset_id).get("ir_sources") or [])):
            return None
        from collectors.us_earnings import validate_artifact
        if validate_artifact(data, asset_id):
            return None
        collected_at = dt.datetime.fromisoformat(data["collected_at"])
        as_of_date = dt.date.fromisoformat(data["as_of_date"])
        if collected_at.tzinfo is None:
            return None
        market_zone = ZoneInfo("America/New_York")
        if (as_of_date != collected_at.astimezone(market_zone).date()
                or as_of_date != now.astimezone(market_zone).date()):
            return None
        age = (now - collected_at).total_seconds()
        retry_after = collected_at + dt.timedelta(hours=US_EARNINGS_FAILURE_RETRY_HOURS)
        return retry_after if 0 <= age < US_EARNINGS_FAILURE_RETRY_HOURS * 3600 else None
    except (AttributeError, KeyError, OSError, TypeError, ValueError):
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
        retry_after = None if force else _recent_us_earnings_failure(
            task, now=dt.datetime.now(dt.timezone.utc),
        )
        if retry_after is not None:
            results.append({"task": task.name, "status": "SKIPPED_RECENT_FAILURE",
                            "reason": task.reason, "retry_after": retry_after.isoformat()})
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
            status = ("NOT_REGISTERED" if task.state == "NOT_REGISTERED" else
                      "MANUAL_REQUIRED" if task.required else "OPTIONAL_MISSING")
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
                "log": log_path.relative_to(ROOT).as_posix(),
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
