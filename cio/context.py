from __future__ import annotations

import csv
import datetime as dt
import glob
import json
import math
import os
import re
import shutil
import statistics
import tempfile
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from calculators.contracts import (
    ContractError,
    change_pct,
    content_hash,
    load_json,
    ratio_pct,
    stable_hash,
    validate_calculation,
    validate_fundamentals,
    write_json,
)
from cio.assets import get_asset
from cio.documents import documents_for_context, fetch_hint, stored_filing_keys
from cio.decision import require_valid_decision, validate_context_integrity
from cio.memory import (
    EVALUATION_ID_EXCLUDED_KEYS,
    READABLE_EVALUATION_SCHEMAS,
)
from cio.paths import KNOWLEDGE_DIR, ROOT
from cio.policy import POLICY_PATH, load_policy, policy_for_context
from cio.requests import open_requests_for_context
from cio.pipeline import pipeline_for_context
from cio.retire import retired_summary
from calculators.position import (
    PositionError,
    build as build_position_math,
    multiples as position_multiples,
    position_value_by_currency,
)
from cio.refresh import (
    DART_PRIMARY_REPORT_FORMS,
    SEC_PRIMARY_REPORT_FORMS,
    event_contract_errors,
    inspect_plan,
    safe_symbol,
)

KST = dt.timezone(dt.timedelta(hours=9))
CONTEXT_SCHEMA = "cio-context/1.0.0"
# 예산의 목적은 "원문을 통째로 넣지 않는다"이지 특정 바이트가 아니다.
# 24_000 → 32_000 → 40_000 으로 올라온 이유는 매번 같다. 직전 판단의 계획,
# 부문 배분, 공시 색인, 검색 가능한 원문 목록처럼 **한 블록이 조사 몇 턴을
# 대체하는** 것들이 들어왔기 때문이다. 절약의 기준은 packet 크기가 아니라
# 판단 하나에 드는 총 토큰이며, 사업보고서 100만 자를 넣지 않는다는 원칙은
# 그대로다. 넘칠 때는 _fit_context_budget 이 서술부터 버리고 결정에 직접
# 쓰이는 숫자를 남긴다.
# packet 은 원문이 아니라 선별된 값만 담는다 — 사업보고서 본문 같은 것은
# 애초에 들어오지 않고 `cio search` 로 필요한 자리만 꺼낸다. 그래서 한도의
# 목적은 절약이 아니라 **선별을 강제하는 것**이다. 한도가 없으면 "일단 넣고
# 보자"가 되고, 판단을 바꾸지 않는 값이 자리를 차지한다.
#
# 40,000 은 동종 비교·실적 캘린더·포지션 산술이 들어오기 전 값이었고 지금
# 35,000 근처까지 찼다. 살아 있는 판단이 여러 건 쌓이거나 부문별 매출이
# 붙으면 곧 넘는다. 여유를 두되 한도 자체는 남긴다 — 트리머가 계속 돌아야
# 무엇을 먼저 버릴지 정하는 규율이 유지된다.
MAX_CONTEXT_JSON_BYTES = 80_000

# 예산이 지켜지는 진짜 이유. 판단은 계속 쌓이지만 packet 에 실리는 것은 최근
# 몇 건뿐이라 `prior_assessments` 가 상한을 갖고, 그래서 다른 섹션이 다 차도
# packet 은 예산 안에 머문다. 실측으로 가장 큰 packet 에 이 상한만큼 판단을
# 채워도 54,240 바이트(예산의 68%)였다.
#
# 예전에는 `assessments[-3:]` 이라는 리터럴로 함수 안에 묻혀 있었다. 예산
# 안전성 전체가 이 숫자에 걸려 있는데 이름도 근거도 없으면, 늘리는 사람은
# 자기가 무엇을 건드리는지 모른다 — 늘린 순간 트리머가 처음으로 돌고, 그
# 경로는 무효화 신호를 버린다.
MAX_LIVE_ASSESSMENTS = 3


class ContextError(ValueError):
    pass


def _prompt_version(path: Path) -> str:
    """The prompt owns its version; the packet must not carry a second copy."""
    lines = path.read_text(encoding="utf-8").splitlines()
    header = lines[0] if lines else ""
    match = re.fullmatch(r"# CIO prompt v(\d+(?:\.\d+)+)", header.strip())
    if not match:
        raise ContextError("CIO prompt 첫 줄에 버전이 없다")
    return match.group(1)


def _csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig") as handle:
        return list(
            csv.DictReader(
                line for line in handle if not line.lstrip().startswith("#")
            )
        )


def _daily_ranges(symbol: str) -> list[tuple[str, float, float, float]]:
    """일봉의 (날짜, 고가, 저가, 분할계수). 새로 수집하는 것은 없다.

    고가·저가는 이미 price-series CSV 에 들어 있는데 `_price_summary` 가
    종가만 꺼내 쓰느라 버려지고 있었다. 문턱을 장중에 스쳤는지는 이 두 칸으로
    답이 나오며 분봉은 필요 없다.

    분할계수를 함께 돌려주는 이유는 `close` 와 같은 원가격이기 때문이다.
    가격계획의 문턱은 판단 시점의 원가격이므로, 그 사이 분할이 있었으면
    비교 자체가 성립하지 않는다.
    """
    path = KNOWLEDGE_DIR / "prices" / f"{safe_symbol(symbol)}.csv"
    if not path.exists():
        return []
    rows: list[tuple[str, float, float, float]] = []
    for row in _csv_rows(path):
        date = row.get("date")
        if not date:
            continue
        try:
            high = float(row["high"])
            low = float(row["low"])
            factor = float(row.get("split_adjustment_factor") or 1.0)
        except (KeyError, TypeError, ValueError):
            continue
        if (
            not math.isfinite(high)
            or not math.isfinite(low)
            or not math.isfinite(factor)
            or low <= 0
            or high < low
        ):
            continue
        rows.append((date, high, low, factor))
    rows.sort(key=lambda item: item[0])
    return rows


def _bars_since(
    ranges: list[tuple[str, float, float, float]] | None,
    decided_at: Any,
) -> list[tuple[str, float, float]]:
    """판단일(포함) 이후의 봉. 저장된 값을 그대로 준다.

    판단 당일을 포함하는 것은 의도다. 2026-08-07 11:16 판단의 재검토 하단
    340,000원은 **같은 날** 저가 333,000원에 스쳤고, 당일을 빼면 이 블록이
    존재하는 이유였던 그 사건이 잡히지 않는다.

    분할이 구간 안에 있어도 봉은 그대로 준다. 저장된 고가·저가는 이미 오늘
    기준으로 분할이 반영된 값이라 서로 비교할 수 있고(`collectors/price.py`),
    기준이 다른 쪽은 **판단 시점 호가로 적힌 문턱**이다. 그 변환은
    `_split_factor_since` 가 낸다.

    예전에는 계수가 달라지면 빈 목록을 줬다. 그러면 분할 한 번에 장중 관측이
    영구히 꺼진다 — 창은 언제나 판단일부터 시작하므로 분할일이 창에서 빠지는
    날이 오지 않기 때문이다.
    """
    if not ranges or not isinstance(decided_at, str) or len(decided_at) < 10:
        return []
    since = decided_at[:10]
    return [
        (date, high, low) for date, high, low, _ in ranges if date >= since
    ]


def _split_factor_since(
    ranges: list[tuple[str, float, float, float]] | None,
    decided_at: Any,
) -> float | None:
    """판단 시점 호가를 저장된 시계열 기준으로 옮기는 계수.

    `저장값 = 그날 호가 × 그 행의 분할계수` 이므로 판단일 행의 계수가 그대로
    변환 계수다. 분할이 없었으면 1.0 이고 그때는 아무것도 달라지지 않는다.

    이것이 없으면 직전 계획의 손절가는 판단하던 날의 호가로 남아 있는데 종가는
    오늘 기준이라, 2:1 분할 한 번에 **닿은 적 없는 손절가가 매 판단마다
    닿았다고** packet 에 실린다. 그리고 그 packet 을 읽는 것은 모델이다.
    """
    if not ranges or not isinstance(decided_at, str) or len(decided_at) < 10:
        return None
    since = decided_at[:10]
    for date, _, _, factor in ranges:
        if date >= since:
            return factor if factor > 0 else None
    return None


def _usable_factor(value: Any) -> float:
    """대조에 쓸 분할계수. 없으면 1.0.

    **이 1.0 은 지어낸 값이 아니다.** `_split_factor_since` 가 None 을 내는
    경우는 (a) 가격 파일이 없거나 (b) 판단일 이후의 봉이 하나도 없을 때뿐이다.
    (a) 면 대조할 종가 자체가 없어 호출부가 먼저 None 을 내고, (b) 면 마지막
    봉이 판단보다 **앞서** 있다는 뜻이라 그 사이에 분할이 끼어들 구간이 없다.
    두 경우 다 "분할 없음"이 관측이지 가정이 아니다.

    형이 틀렸거나 0 이하인 값도 1.0 으로 모으는 것은 다른 문제다 — 그건 계수를
    못 읽은 것이므로, 여기가 조용해지면 `collectors/price.py` 가 이상한 계수를
    쓴 사실이 묻힌다. 그 검사는 수집 시점에 있어야 하고 여기 있으면 늦다.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 1.0
    number = float(value)
    return number if math.isfinite(number) and number > 0 else 1.0


def _price_summary(symbol: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    path = KNOWLEDGE_DIR / "prices" / f"{safe_symbol(symbol)}.csv"
    source = {
        "source_id": f"PRICE:{symbol}",
        "kind": "MARKET_DATA",
        "path": str(path.relative_to(ROOT)),
        "data_as_of": None,
    }
    if not path.exists():
        return None, source
    try:
        with path.open(encoding="utf-8-sig") as handle:
            schema_line = handle.readline().strip()
            metadata_line = handle.readline().strip()
            handle.readline()
            session_line = handle.readline().strip()
    except OSError:
        return None, source
    metadata = re.fullmatch(
        r"# symbol (.+) / currency ([A-Z]{3})",
        metadata_line,
    )
    if (
        schema_line != "# schema_version price-series/2.1.0"
        or not metadata
        or metadata.group(1) != symbol
    ):
        return None, source
    session = re.fullmatch(
        r"# last_bar (\d{4}-\d{2}-\d{2}) / session (\w+)",
        session_line,
    )
    # 헤더 줄이 없으면 그것도 '모른다'다. 수집기가 UNKNOWN 을 쓰는 경우와
    # 같은 자리이므로 같은 값으로 모은다.
    session_state = session.group(2) if session else "UNKNOWN"
    rows = _csv_rows(path)
    points = []
    adjusted_complete = True
    for row in rows:
        if not row.get("date") or not row.get("close"):
            continue
        try:
            close = float(row["close"])
            adjusted = float(row.get("adjusted_close") or close)
        except (TypeError, ValueError):
            continue
        if (
            not math.isfinite(close)
            or not math.isfinite(adjusted)
            or close <= 0
            or adjusted <= 0
        ):
            continue
        adjusted_complete = adjusted_complete and bool(
            row.get("adjusted_close")
        )
        points.append((row["date"], close, adjusted))
    if not points:
        return None, source
    source["data_as_of"] = points[-1][0]
    closes = [point[1] for point in points]
    return_closes = [point[2] for point in points]
    # Short-window claims ("the last two days") need exchange-session dates.
    # Never let an unfinished/latest-unknown bar silently become a daily close.
    settled_points = points if session_state == "SETTLED" else points[:-1]
    session_returns = {}
    for count in (1, 2, 3):
        if len(settled_points) > count:
            start, end = settled_points[-1 - count], settled_points[-1]
            session_returns[str(count)] = {
                "from_date": start[0], "through_date": end[0],
                "return_pct": round((end[2] / start[2] - 1) * 100, 2),
            }

    def change(days: int) -> float | None:
        if len(return_closes) <= days or return_closes[-1 - days] <= 0:
            return None
        return round(
            (return_closes[-1] / return_closes[-1 - days] - 1) * 100,
            2,
        )

    recent = closes[-252:]
    high = max(recent)
    low = min(recent)
    returns = [
        return_closes[index] / return_closes[index - 1] - 1
        for index in range(
            max(1, len(return_closes) - 63), len(return_closes)
        )
        if return_closes[index - 1] > 0
    ]
    return (
        {
            "symbol": symbol,
            "currency": metadata.group(2),
            "as_of": points[-1][0],
            "close": closes[-1],
            "returns_pct": {
                "1m": change(21),
                "3m": change(63),
                "12m": change(252),
            },
            "returns_basis": (
                "ADJUSTED_CLOSE"
                if adjusted_complete
                else "RAW_CLOSE_FALLBACK"
            ),
            "settled_session_returns": {
                "windows": session_returns,
                "excluded_latest_bar": points[-1][0] if session_state != "SETTLED" else None,
                "_note": "최근 1·2·3개 확정 거래 세션의 누적 수익률이다. 달력 일수가 아니다. 마지막 봉이 미확정·불명확하면 제외하며 위 returns_basis와 같은 가격 기준을 쓴다",
            },
            "drawdown_from_12m_high_pct": round(
                (closes[-1] / high - 1) * 100, 2
            ),
            # 고가와 저가가 같으면(거래 정지·상장 직후) 위치라는 것이 없다.
            # 예전에는 50 을 넣었다. 0·100 을 피하려던 것인데 50 도 똑같이
            # 지어낸 값이고, 이 자리는 화면이 아니라 **packet** 이라 모델이
            # "구간 중간"을 관측으로 읽는다. 없는 것은 없다고 낸다.
            "position_in_12m_range_pct": round(
                (closes[-1] - low) / (high - low) * 100, 2
            )
            if high > low
            else None,
            "realized_volatility_3m_pct": round(
                statistics.stdev(returns) * math.sqrt(252) * 100, 2
            )
            if len(returns) >= 2
            else None,
            # 마지막 행이 확정 종가인지. 장중 수집분이면 close·거래량·수익률이
            # 전부 진행 중인 값이라 확정 일봉처럼 인용하면 안 된다.
            #
            # **세 갈래를 두 갈래로 접지 않는다.** 수집기는 SETTLED·UNSETTLED·
            # UNKNOWN 을 구분해 헤더에 남기는데(`collectors/price.py` 의
            # `session_state`), 여기서 `bool(session) and ...` 로 접으면 모른다가
            # **확정 종가가 아니다**로 바뀐다. 게다가 그때는 아래 경고도 안 붙어서
            # 모델은 이유 없는 음성 하나만 보게 된다.
            "last_bar": {
                "date": session.group(1) if session else points[-1][0],
                "session": session_state,
                "is_settled_close": (
                    None
                    if session_state == "UNKNOWN"
                    else session_state == "SETTLED"
                ),
                "_warning": (
                    "마지막 봉이 확정 종가가 아니다 — 장중 수집분이다. close 와 "
                    "그것을 쓰는 수익률·낙폭·변동성은 진행 중인 값이며 종가로 "
                    "인용하면 안 된다"
                    if session_state == "UNSETTLED"
                    else "마지막 봉이 확정 종가인지 판별하지 못했다 — 거래소가 "
                    "선언한 세션 경계를 못 읽었다. 확정으로도 미확정으로도 "
                    "인용하면 안 된다"
                    if session_state == "UNKNOWN"
                    else None
                ),
            },
        },
        source,
    )


def _latest_fundamentals(
    asset: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    asset_id = asset["asset_id"]
    paths = [
        Path(path)
        for path in glob.glob(
            str(KNOWLEDGE_DIR / "fundamentals" / f"{asset_id}-*.json")
        )
    ]
    source = {
        "source_id": f"FUNDAMENTALS:{asset_id}",
        "kind": "FILING_DERIVED",
        "path": None,
        "data_as_of": None,
    }
    if not paths:
        return None, source
    candidates: list[tuple[str, Path, dict[str, Any]]] = []
    for path in paths:
        try:
            data = load_json(path)
        except (ContractError, OSError):
            continue
        candidates.append((str(data.get("period_end") or ""), path, data))
    if not candidates:
        return None, source
    _, path, data = max(candidates, key=lambda item: item[0])
    source["path"] = str(path.relative_to(ROOT))
    source["data_as_of"] = data.get("period_end")
    binding_errors = []
    if data.get("instrument_id") != asset_id:
        binding_errors.append("재무 스냅샷 instrument_id가 요청 자산과 다르다")
    period_end = data.get("period_end")
    if path.name != f"{asset_id}-{period_end}.json":
        binding_errors.append("재무 스냅샷 파일명과 기준일이 다르다")
    if binding_errors:
        return {"contract_errors": binding_errors}, source
    observed_filing_source = (
        "DART"
        if data.get("corp_code")
        else "SEC_EDGAR"
        if data.get("cik")
        else "UNKNOWN"
    )
    expected_filing_source = asset["filing_source"]
    if observed_filing_source != expected_filing_source:
        return (
            {
                "contract_errors": [
                    "자산 registry와 재무 스냅샷의 공시원이 다르다"
                ],
                "expected_filing_source": expected_filing_source,
                "observed_filing_source": observed_filing_source,
            },
            source,
        )
    errors = validate_fundamentals(data)
    if errors:
        return {"contract_errors": errors[:5]}, source

    income = data.get("income_statement") or {}
    income_summary = {}
    for key in ("revenue", "gross_profit", "operating_income", "net_income"):
        entry = income.get(key)
        if isinstance(entry, dict):
            income_summary[key] = {
                "ttm": entry.get("ttm"),
                "tag": entry.get("tag"),
            }
    values = data.get("values") or {}
    summary = {
        "fundamentals_id": data["fundamentals_id"],
        "period_end": data["period_end"],
        "currency": data.get("currency"),
        "values": values,
        "income_statement_ttm": income_summary,
        "source": data.get("source") or observed_filing_source,
    }
    # This describes the historical EPS denominator, not today's capitalization.
    for field in ("eps_basis", "share_count_cross_check", "share_count_basis"):
        if field in data:
            summary[field] = data[field]
    # TTM 네 숫자만 실으면 분기 흐름이 packet 에서 사라진다. 수집기가 이미
    # 만들어 둔 시계열을 지우지 말고 한 줄씩 접어 함께 싣는다.
    bridge = _earnings_bridge(data)
    if bridge:
        summary["earnings_bridge"] = bridge
    # 총차입만 실으면 리스·전환사채를 포함할지가 수집기에서 이미 정해져 버린다.
    # 그건 방법론 판단이므로 분류를 함께 실어 모델이 다시 셀 수 있게 한다.
    # 계정 한 줄씩(debt_detail)은 싣지 않는다 — 다시 세는 데 분류 합이면 족하다.
    # 0 이 관측인지 결측인지 말해 주지 않으면 순부채가 과대계상된 사실이 사라진다.
    # 정상일 때는 싣지 않는다 — 매 판단마다 보내는 자리라 한 줄이 비용이다.
    basis = data.get("short_term_investments_basis")
    if basis and basis != "OBSERVED":
        summary["short_term_investments_basis"] = {
            "basis": basis,
            "_warning": (
                data.get("short_term_investments_note")
                or "단기투자자산을 쓸 수 없어 0 으로 두었다"
            ) + ". values.short_term_investments 의 0 은 관측이 아니라 결측이고, "
                "그만큼 net_debt 가 과대계상돼 있다",
        }

    by_category = data.get("debt_by_category")
    if isinstance(by_category, dict) and by_category:
        summary["debt_by_category"] = {
            **by_category,
            "_role": (
                "총차입(values.long_term_debt)의 구성이다. "
                + (
                    "LEASE는 US GAAP ASC842 운용·금융리스 부채다. 운용리스 "
                    "지급액은 영업현금흐름에 이미 반영되므로 해당 FCF로 가치평가하며 "
                    "리스부채까지 차감하면 이중 반영될 수 있다. 리스 처리에 맞춰 "
                    "현금흐름과 순부채를 함께 조정한다"
                    if asset["market"] == "US"
                    else "LEASE는 IFRS16 리스부채다. 현금흐름의 리스원금·이자 "
                    "분류와 맞춰 포함 여부를 정한다"
                )
                + ". 리스를 뺀 순부채를 쓰려면 LEASE를 덜어내고 다시 센다"
            ),
        }
    fcf = _as_float(values.get("fcf_ttm"))
    cash = _as_float(values.get("cash_and_equivalents"))
    short_investments = _as_float(values.get("short_term_investments"))
    if (
        fcf is not None
        and fcf < 0
        and cash is not None
        and short_investments is not None
        and cash + short_investments >= 0
    ):
        summary["historical_cash_runway"] = {
            "months": round((cash + short_investments) / -fcf * 12, 1),
            "basis": "cash_and_short_investments / trailing_free_cash_flow_burn",
            "_warning": (
                "과거 burn을 직선 연장한 결정론적 참고치다. 증자·마일스톤·비용 "
                "변화는 CIO 모델이 직접 조사한다"
            ),
        }
    return summary, source


BRIDGE_INCOME_METRICS = ("revenue", "gross_profit", "operating_income", "net_income")
BRIDGE_CASH_METRICS = ("ocf", "capex")
BRIDGE_MAX_QUARTERS = 8


def _month_end(year: int, month: int, day: int) -> dt.date:
    """말일 기준으로 자른 날짜. 3월 31일의 3개월 뒤는 6월 30일이다."""
    following = dt.date(year + month // 12, month % 12 + 1, 1)
    last = (following - dt.timedelta(days=1)).day
    return dt.date(year, month, min(day, last))


def _next_quarter_end(day: dt.date) -> dt.date:
    month = day.month + 3
    return _month_end(day.year + (month - 1) // 12, (month - 1) % 12 + 1, day.day)


def _pct_change(current: float | None, previous: float | None) -> float | None:
    """변화율. 기준이 0 이하면 비우고 원값을 읽게 한다.

    적자에서 흑자로 가는 변화를 백분율로 적으면 숫자는 나오지만 뜻이 없다
    (-100 → +50 을 "+150%"로 적는 것). 원값이 같은 줄에 있으므로 지어내지 않는다.
    """
    result = change_pct(current, previous)
    return None if result is None else round(result, 1)


def _margin_pct(value: float | None, revenue: float | None) -> float | None:
    """분기 마진. 산술은 `ratio_pct` 에 맡기고 여기서는 자릿수만 정한다.

    같은 규칙(분모가 0 이하면 값을 내지 않는다)을 두 번 구현하고 있었다. 지금은
    두 구현이 일치하지만, 일치한다는 것은 확인해 봐야 아는 사실이지 유지되는
    성질이 아니다 — 한쪽만 고치면 화면과 packet 이 다른 답을 내고 아무도
    모른다.
    """
    result = ratio_pct(value, revenue)
    return None if result is None else round(result, 1)


def _quarter_index(entries: Any) -> dict[tuple[int, int], dict[str, Any]]:
    index: dict[tuple[int, int], dict[str, Any]] = {}
    if not isinstance(entries, list):
        return index
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        fy, quarter = entry.get("fy"), entry.get("quarter")
        value = _as_float(entry.get("val"))
        if not isinstance(fy, int) or quarter not in (1, 2, 3, 4) or value is None:
            continue
        index[(fy, quarter)] = entry
    return index


def _earnings_bridge(
    data: dict[str, Any], *, today: dt.date | None = None
) -> dict[str, Any] | None:
    """최근 8분기를 한 표로 접는다.

    수집기는 8분기를 만들어 저장하는데 packet 은 TTM 네 숫자만 실어 보냈다.
    그래서 분기 흐름을 보려면 모델이 DART 원문을 다시 뒤져야 했고, 근거의
    절반이 이미 로컬에 있는데도 그랬다. 예산 때문에 압축은 필요하지만
    **압축은 삭제가 아니라 변환이다** — raw 시계열 대신 분기당 한 줄로 접는다.

    여기서 나오는 값은 전부 합·차·비율이다. 추세 판정은 모델의 몫이라
    "가속"·"둔화" 같은 값은 만들지 않는다.
    """
    today = today or dt.date.today()
    income = data.get("income_statement")
    income = income if isinstance(income, dict) else {}
    cash = data.get("quarters")
    cash = cash if isinstance(cash, dict) else {}

    series: dict[str, dict[tuple[int, int], dict[str, Any]]] = {}
    absent: list[str] = []
    for metric in BRIDGE_INCOME_METRICS:
        entry = income.get(metric)
        index = _quarter_index((entry or {}).get("quarters") if isinstance(entry, dict) else None)
        series[metric] = index
        if not index:
            absent.append(metric)
    for metric in BRIDGE_CASH_METRICS:
        index = _quarter_index(cash.get(metric))
        series[metric] = index
        if not index:
            absent.append(metric)

    periods = sorted({key for index in series.values() for key in index})
    if not periods:
        return None
    window = periods[-BRIDGE_MAX_QUARTERS:]

    def value_of(metric: str, key: tuple[int, int]) -> float | None:
        entry = series[metric].get(key)
        return _as_float(entry.get("val")) if entry else None

    rows: list[dict[str, Any]] = []
    for key in window:
        fy, quarter = key
        previous = (fy, quarter - 1) if quarter > 1 else (fy - 1, 4)
        year_ago = (fy - 1, quarter)
        row: dict[str, Any] = {"period": f"{fy}Q{quarter}"}
        end = next(
            (
                series[metric][key].get("end")
                for metric in series
                if key in series[metric] and series[metric][key].get("end")
            ),
            None,
        )
        if end:
            row["end"] = end
        for metric in BRIDGE_INCOME_METRICS + BRIDGE_CASH_METRICS:
            value = value_of(metric, key)
            if value is not None:
                row[metric] = round(value, 2)
        ocf, capex = value_of("ocf", key), value_of("capex", key)
        if ocf is not None and capex is not None:
            row["fcf"] = round(ocf - capex, 2)
        revenue = value_of("revenue", key)
        for metric, name in (
            ("gross_profit", "gross_margin_pct"),
            ("operating_income", "operating_margin_pct"),
            ("net_income", "net_margin_pct"),
        ):
            margin = _margin_pct(value_of(metric, key), revenue)
            if margin is not None:
                row[name] = margin
        for metric in ("revenue", "operating_income", "net_income"):
            yoy = _pct_change(value_of(metric, key), value_of(metric, year_ago))
            if yoy is not None:
                row[f"{metric}_yoy_pct"] = yoy
            qoq = _pct_change(value_of(metric, key), value_of(metric, previous))
            if qoq is not None:
                row[f"{metric}_qoq_pct"] = qoq
        derived = [
            metric
            for metric in BRIDGE_INCOME_METRICS + BRIDGE_CASH_METRICS
            if series[metric].get(key, {}).get("derived")
        ]
        if derived:
            row["derived"] = derived
        rows.append(row)

    covered = data.get("period_end")
    if not isinstance(covered, str):
        covered = rows[-1].get("end")
    elapsed: list[str] = []
    try:
        cursor = dt.date.fromisoformat(covered)
    except (TypeError, ValueError):
        cursor = None
    while cursor:
        cursor = _next_quarter_end(cursor)
        if cursor > today:
            break
        elapsed.append(cursor.isoformat())

    bridge: dict[str, Any] = {
        "currency": data.get("currency"),
        "covered_through": covered,
        "quarter_ends_after_covered": elapsed,
        "rows": rows,
        "_role": (
            "로컬 재무제표의 분기 시계열을 한 줄씩 접은 표다. 값은 자산 통화의 "
            "원단위이고 백분율은 전부 산술이다 — 전년동기(yoy)·전분기(qoq) 대비 "
            "변화율과 매출 대비 마진. 기준이 0 이하면 변화율은 비운다. "
            "derived 로 표시된 항목은 실측이 아니라 복원값이다(4분기 = 연간 − "
            "3분기 누계, 현금흐름 = 누계 차감). quarter_ends_after_covered 는 "
            "covered_through 이후에 이미 끝난 결산 분기이며 그 실적은 이 표에 "
            "없다. 추세 판정은 이 표에 없다 — 모델이 한다"
        ),
    }
    if absent:
        bridge["metrics_absent"] = absent
        bridge["_metrics_absent_role"] = (
            "로컬 스냅샷에 분기 시계열이 아예 없는 항목이다. 0 이 아니라 "
            "결측이며, 이 항목으로 논지를 세우려면 공시 원문을 직접 읽어야 한다"
        )
    return bridge


def _mark_bridge_disclosures(
    bridge: dict[str, Any] | None, reporting_lag: dict[str, Any] | None
) -> None:
    """기준일 이후의 실적 공시를 표 옆에 붙인다.

    로컬 TTM 영업이익 94.1조 vs 이미 공시된 179.0조 — 1.9배 차이가
    `validation.optional_gaps` 한 줄에만 있었고 모델이 주의 깊게 읽어서 겨우
    잡았다. 주의력에 의존하는 안전장치는 안전장치가 아니므로, 그 사실을
    숫자를 읽는 자리로 옮긴다.
    """
    if not isinstance(bridge, dict) or not isinstance(reporting_lag, dict):
        return
    disclosures = reporting_lag.get("disclosures_after_local_period") or []
    bridge["unreflected_result_disclosures"] = [
        {
            key: item.get(key)
            for key in (
                "form", "filing_date", "description",
                "document_url", "accession_number",
            )
            if item.get(key)
        }
        for item in disclosures
        if isinstance(item, dict)
    ]
    if not bridge["unreflected_result_disclosures"]:
        return
    elapsed = bridge.get("quarter_ends_after_covered") or []
    bridge["_warning"] = (
        f"이 표와 income_statement_ttm 은 {bridge.get('covered_through')} 까지의 "
        f"재무제표다. 그 뒤로 결산 분기 {len(elapsed)}개가 끝났고 실적 관련 공시가 "
        f"{len(bridge['unreflected_result_disclosures'])}건 나왔는데 그 숫자는 여기 "
        "없다. TTM 을 최신 실적으로 읽으면 이미 공시된 분기가 통째로 빠진 값을 "
        "최신으로 읽는 것이다 — document_url 의 원문을 열어 직접 반영한다. "
        "잠정치는 감사받은 재무제표와 항목 정의가 다를 수 있으니 구분해 쓴다"
    )


def _filing_summary(
    asset: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """주식의 최신 공시를 작은 탐색 색인으로 만든다.

    미국은 SEC EDGAR, 한국은 DART다. 색인일 뿐이며 문서의 뜻은 모델이 원문을
    읽고 판단한다.
    """
    if asset["asset_type"] != "STOCK":
        return None, None
    asset_id = asset["asset_id"]
    path = KNOWLEDGE_DIR / "filings" / f"{asset_id}.json"
    source = {
        "source_id": f"FILINGS:{asset_id}",
        "kind": "REGULATOR_FILING_INDEX",
        "path": str(path.relative_to(ROOT)),
        "data_as_of": None,
    }
    if not path.exists():
        return None, source
    try:
        data = load_json(path)
    except (ContractError, OSError):
        return {"contract_errors": ["공시 색인 JSON을 읽을 수 없다"]}, source
    source["data_as_of"] = data.get("collected_at")
    errors = []
    if data.get("schema_version") != "filing-index/1.0.0":
        errors.append("filing-index/1.0.0이 아니다")
    if data.get("asset_id") != asset_id:
        errors.append("공시 색인의 asset_id가 registry와 다르다")
    filings = data.get("filings")
    if not isinstance(filings, list) or not filings:
        errors.append("공시 색인에 제출 문서가 없다")
    if errors:
        return {"contract_errors": errors}, source
    is_dart = data.get("source") == "DART_LIST"
    primary_forms = (
        DART_PRIMARY_REPORT_FORMS if is_dart else SEC_PRIMARY_REPORT_FORMS
    )
    primary_reports = []
    material_events = []
    for filing in filings:
        if not isinstance(filing, dict):
            continue
        # 값이 없는 키는 싣지 않는다. DART 는 report_date 를 주지 않아 KR 공시
        # 8건이 매번 `"report_date": null` 을 들고 나갔다(152 B). SEC 는 실제
        # 값을 주므로 그쪽에서는 그대로 남는다 — 키를 지우는 게 아니라 빈 값을
        # 안 보내는 것이다.
        compact = {
            key: filing[key]
            for key in (
                "filing_date",
                "report_date",
                "form",
                "accession_number",
                "description",
                "document_url",
            )
            if filing.get(key) is not None
        }
        if is_dart and filing.get("remark"):
            compact["remark"] = filing["remark"]
        # SEC 는 primaryDocDescription 이 거의 항상 form 코드와 같아서 제목만
        # 보면 8-K 31건이 전부 "8-K"다. Item 라벨이 있어야 임상 결과 발표와
        # 임원 교체를 구분할 수 있다.
        if not is_dart and filing.get("item_labels"):
            compact["item_labels"] = filing["item_labels"]
        target = (
            primary_reports
            if filing.get("form") in primary_forms
            else material_events
        )
        target.append(compact)
    if not primary_reports:
        return {"contract_errors": ["공시 색인의 제출 문서가 유효하지 않다"]}, source
    summary = {
        "collected_at": data.get("collected_at"),
        "latest_primary_reports": primary_reports[:4],
        "latest_material_events": material_events[:8],
        "_note": (
            "제출 문서 탐색용 색인이다. 문서의 의미와 투자 영향은 모델이 "
            f"{'DART' if is_dart else 'SEC'} 원문을 읽고 판단한다. 여기 없는 "
            "공시가 있을 수 있으니 결론이 특정 공시에 걸리면 원문을 직접 "
            "확인한다"
        ),
    }
    if is_dart:
        summary["corp_code"] = data.get("corp_code")
        summary["since"] = data.get("since")
        summary["_form_note"] = (
            "form은 DART 보고서 이름으로 나눈 갈래이지 법정 서식 코드가 "
            "아니다. 정확한 성격은 description 원문을 읽는다"
        )
    else:
        summary["cik"] = data.get("cik")
    return (
        summary,
        source,
    )


def _earnings_calendar_section(
    asset: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """다음 실적 발표일과 정기보고서 예상일을 싣는다.

    공시 색인은 "결산실적공시예고"라는 제목만 갖는다. 공시예정일은 원문
    본문에만 있어서, 이벤트까지 며칠 남았는지가 패킷에 없었다. 이벤트 전에
    살 것인가는 매수 판단의 1차 분기점이므로 색인과 따로 실어 눈에 띄게 한다.

    잠정실적과 정기보고서를 나눠 싣는 이유는 담는 내용이 다르기 때문이다.
    재고자산·부문별 매출은 잠정실적에 없다. 이 구분이 없으면 잠정실적만 보고
    "확인했다"고 결론 낼 수 있다.
    """
    if asset["asset_type"] != "STOCK":
        return None, None
    asset_id = asset["asset_id"]
    path = KNOWLEDGE_DIR / "calendar" / f"{asset_id}.json"
    source = {
        "source_id": f"EARNINGS_CALENDAR:{asset_id}",
        "kind": "REGULATOR_FILING_DOCUMENT",
        "path": str(path.relative_to(ROOT)),
        "data_as_of": None,
    }
    if not path.exists():
        return None, source
    try:
        data = load_json(path)
    except (ContractError, OSError):
        return {"contract_errors": ["실적 캘린더 JSON을 읽을 수 없다"]}, source
    source["data_as_of"] = data.get("collected_at")
    if data.get("schema_version") != "earnings-calendar/1.0.0":
        return (
            {"contract_errors": ["earnings-calendar/1.0.0이 아니다"]},
            source,
        )
    if data.get("asset_id") != asset_id:
        return (
            {"contract_errors": ["실적 캘린더의 asset_id가 registry와 다르다"]},
            source,
        )
    upcoming = data.get("next_provisional_result")
    report = data.get("expected_periodic_report")
    if not isinstance(upcoming, dict) and not isinstance(report, dict):
        return (
            {
                "as_of_date": data.get("as_of_date"),
                "next_provisional_result": None,
                "expected_periodic_report": None,
                "_note": (
                    "결산실적 공시예고가 색인에 없어 다음 발표일을 모른다. "
                    "모른다는 것이지 발표가 없다는 뜻이 아니다"
                ),
            },
            source,
        )
    # 남은 영업일은 **수집 시점이 아니라 지금** 기준으로 다시 센다.
    #
    # 이 값은 수집기가 계산해 파일에 굳힌 파생값이다. 캘린더 자체는 하루쯤
    # 지나도 FRESH 라 refresh 가 건너뛰는데, 그러면 어제 센 "4영업일 남음"이
    # 오늘 packet 에 그대로 실린다. 실제로 그랬다 — 2026-08-09 수집분이
    # 2026-08-10 packet 에 4를 실었고 참값은 3이었다. 며칠 남았는지는 판단이
    # 직접 쓰는 입력(이벤트 전에 주문을 낼 것인가)이라 하루 차이가 결론을
    # 바꾼다. 날짜(`date`)는 사실이고 남은 일수는 그 사실에서 파생된 값이므로,
    # 파생값은 읽는 시점에 다시 만든다.
    today = dt.datetime.now(KST).date()

    def _days_left(target: Any) -> int | None:
        if not isinstance(target, str):
            return None
        try:
            end = dt.date.fromisoformat(target)
        except ValueError:
            return None
        if end < today:
            return None
        days = 0
        cursor = today
        while cursor < end:
            cursor += dt.timedelta(days=1)
            if cursor.weekday() < 5:
                days += 1
        return days

    summary: dict[str, Any] = {
        "as_of_date": data.get("as_of_date"),
        "business_day_basis": data.get("business_day_basis"),
        "_note": data.get("_note"),
        "_not_a_judgment": data.get("_not_a_judgment"),
    }
    if data.get("next_provisional_result_missing_reason"):
        summary["next_provisional_result"] = None
        summary["next_provisional_result_missing_reason"] = data[
            "next_provisional_result_missing_reason"
        ]
    if isinstance(upcoming, dict):
        summary["next_provisional_result"] = {
            key: upcoming.get(key)
            for key in (
                "disclosure_date",
                "business_days_remaining",
                "period_start",
                "period_end",
                "is_confirmed",
                "notice_filing_date",
                # document_url 은 싣지 않는다 — 같은 공시가 filings 에도 있고
                # 거기 url 이 있다. 여기서는 evidence_ref(DARTDOC:접수번호)가
                # 정본 핸들이며, 그것으로 filings·local_documents 양쪽을 짚는다.
                "evidence_ref",
                "source_caveat",
                "_content",
            )
        }
        summary["next_provisional_result"]["business_days_remaining"] = (
            _days_left(upcoming.get("disclosure_date"))
        )
        summary["next_provisional_result"]["_days_basis"] = (
            f"packet 조립 시점({today.isoformat()}) 기준으로 다시 셌다. "
            "수집 파일의 값은 수집일 기준이라 그대로 실으면 낡는다"
        )
    if isinstance(report, dict):
        summary["expected_periodic_report"] = {
            key: report.get(key)
            for key in (
                "date",
                "business_days_remaining",
                "estimated",
                "report_class",
                "basis",
                "sample_size",
                "_content",
            )
        }
        summary["expected_periodic_report"]["business_days_remaining"] = (
            _days_left(report.get("date"))
        )
        summary["expected_periodic_report"]["_days_basis"] = (
            f"packet 조립 시점({today.isoformat()}) 기준으로 다시 셌다. "
            "None 이면 추정일이 이미 지났다는 뜻이며, 그때는 제출이 "
            "늦어지는 중인지 색인을 확인한다"
        )
    elif data.get("expected_periodic_report_missing_reason"):
        summary["expected_periodic_report"] = None
        summary["expected_periodic_report_missing_reason"] = data[
            "expected_periodic_report_missing_reason"
        ]
    return summary, source


RESULT_DISCLOSURE_FORMS = {"PROVISIONAL_RESULT", "FAIR_DISCLOSURE", "8-K"}


def _reporting_lag(
    fundamentals: dict[str, Any] | None,
    filings: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """로컬 재무가 결산 캘린더에서 몇 걸음 뒤에 있는지 계산한다.

    신선도를 "언제 받았나"로만 재면 3월 말 재무를 7월에 받아도 FRESH가 된다.
    회사가 이미 다음 분기를 공시했는지는 시계가 아니라 공시 목록이 답한다.
    """
    if not fundamentals or not isinstance(filings, dict):
        return None
    period_end = fundamentals.get("period_end")
    if not isinstance(period_end, str):
        return None
    primary = filings.get("latest_primary_reports") or []
    events = filings.get("latest_material_events") or []
    if not isinstance(primary, list) or not isinstance(events, list):
        return None

    newer_reports = [
        {
            "form": item.get("form"),
            "report_date": item.get("report_date"),
            "filing_date": item.get("filing_date"),
            "description": item.get("description"),
            "document_url": item.get("document_url"),
        }
        for item in primary
        if isinstance(item, dict)
        and isinstance(item.get("report_date"), str)
        and item["report_date"] > period_end
    ]
    # 공시는 **이미 끝난** 기간만 보고할 수 있다. 그래서 로컬 기준일 다음
    # 분기말 전에 나온 실적 공시는 로컬 스냅샷이 이미 담은 분기(또는 그 이전)를
    # 말하는 것이라 새 숫자가 아니다 — 3월 31일 기준 재무에 5월 8일 1분기
    # 잠정실적을 "미반영 공시"로 세면 매 판단마다 없는 공백이 켜진다.
    # 다음 분기말을 넘겨 나온 것만 로컬에 없는 기간을 담는다.
    cutoff = period_end
    try:
        cutoff = _next_quarter_end(dt.date.fromisoformat(period_end)).isoformat()
    except ValueError:
        pass
    newer_disclosures = [
        {
            "form": item.get("form"),
            "filing_date": item.get("filing_date"),
            "description": item.get("description"),
            "document_url": item.get("document_url"),
            "accession_number": item.get("accession_number"),
        }
        for item in events
        if isinstance(item, dict)
        and item.get("form") in RESULT_DISCLOSURE_FORMS
        and isinstance(item.get("filing_date"), str)
        and item["filing_date"] > cutoff
    ]
    days_since = None
    try:
        days_since = (
            dt.date.today() - dt.date.fromisoformat(period_end)
        ).days
    except ValueError:
        pass
    return {
        "local_period_end": period_end,
        "days_since_period_end": days_since,
        "newer_periodic_report_filed": bool(newer_reports),
        "newer_periodic_reports": newer_reports[:2],
        "disclosures_after_local_period": newer_disclosures[:4],
        "_warning": (
            "로컬 재무는 local_period_end 기준이다. "
            "disclosures_after_local_period 가 비어 있지 않으면 그 기간 이후의 "
            "실적 정보가 이미 공시로 나와 있다는 뜻이며, 로컬 숫자만으로 "
            "최신 상태를 논하면 안 된다. 그 공시는 요약·잠정치라 감사받은 "
            "재무제표와 항목 정의가 다를 수 있으니 원문을 읽고 구분해 쓴다"
        ),
    }


def _latest_calculation(
    asset: dict[str, Any],
    *,
    candidate_price: float,
    fundamentals_id: str | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if "DCF" not in asset["valuation_family"]:
        return None, None
    candidates: list[tuple[str, Path, dict[str, Any]]] = []
    for path in (KNOWLEDGE_DIR / "calculations").glob("CALC-*.json"):
        try:
            data = load_json(path)
        except (ContractError, OSError):
            continue
        if data.get("instrument_id") != asset["asset_id"]:
            continue
        if validate_calculation(data):
            continue
        calculation_price = _as_float((data.get("inputs") or {}).get("price"))
        if calculation_price is None or not math.isclose(
            calculation_price,
            candidate_price,
            rel_tol=1e-9,
            abs_tol=1e-6,
        ):
            continue
        provenance_id = (
            ((data.get("inputs") or {}).get("input_provenance") or {}).get(
                "fundamentals_id"
            )
        )
        if fundamentals_id and provenance_id and provenance_id != fundamentals_id:
            continue
        candidates.append((str(data.get("created_at") or ""), path, data))
    if not candidates:
        return None, None
    _, path, data = max(candidates, key=lambda item: item[0])
    summary = {
        "calculation_id": data["calculation_id"],
        "model": data["model"],
        "model_version": data["model_version"],
        "created_at": data["created_at"],
        "inputs": data["inputs"],
        "outputs": data["outputs"],
        "_role": (
            "현재 요청 가격·재무 스냅샷과 일치하는 선택형 계산이다. "
            "모델은 방법론 적합성을 먼저 판단한다"
        ),
    }
    source = {
        "source_id": data["calculation_id"],
        "kind": "CALCULATION",
        "path": str(path.relative_to(ROOT)),
        "data_as_of": data.get("created_at"),
    }
    return summary, source


def _market_summary(market: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    path = KNOWLEDGE_DIR / "market" / f"regime-{market}.json"
    source = {
        "source_id": f"MARKET_STATE:{market}",
        "kind": "DERIVED",
        "path": str(path.relative_to(ROOT)),
        "data_as_of": None,
    }
    if not path.exists():
        return None, source
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, source
    if data.get("schema_version") != "market-regime/2.0.0":
        return None, source
    block = (data.get("by_market") or {}).get(market)
    if not isinstance(block, dict):
        return None, source
    input_sources = [
        {
            "symbol": item.get("symbol"),
            "mode": item.get("mode"),
            "data_as_of": item.get("data_as_of"),
        }
        for item in (data.get("input_sources") or [])
        if isinstance(item, dict)
    ]
    input_dates = [
        item["data_as_of"] for item in input_sources if item.get("data_as_of")
    ]
    source["data_as_of"] = min(input_dates) if input_dates else data.get(
        "collected_at"
    )
    macro_root = data.get("macro") or {}
    macro_by_market = macro_root.get("by_market") or {}
    macro_rows = (
        macro_by_market.get(market)
        if isinstance(macro_by_market, dict)
        else None
    )
    if not isinstance(macro_rows, list):
        macro_rows = macro_root.get("series") or []
    return (
        {
            "schema_version": data.get("schema_version"),
            "as_of": data.get("collected_at"),
            "market": market,
            "regime": block.get("regime"),
            "score": block.get("score"),
            "notes": block.get("notes") or [],
            "volatility": block.get("volatility"),
            "indices": block.get("indices") or [],
            "macro": macro_rows,
            "input_sources": input_sources,
            "_role": "현재 상태 측정치이며 매수·매도 지시가 아니다",
        },
        source,
    )


def _event_summary(asset: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    # 시장을 가리지 않는다. 판정 로직이 한 곳이라 두 시장의 packet 이 같은 뜻을
    # 갖고, 여기서 한쪽만 실으면 그 사실이 다시 사라진다.
    if asset["asset_type"] != "STOCK":
        return None, None
    path = KNOWLEDGE_DIR / "events" / f"{asset['asset_id']}.json"
    source = {
        "source_id": f"EVENTS:{asset['asset_id']}",
        "kind": "FILING_EVENT_INDEX",
        "path": str(path.relative_to(ROOT)),
        "data_as_of": None,
    }
    if not path.exists():
        return None, source
    try:
        data = load_json(path)
    except (ContractError, OSError):
        return {"contract_errors": ["이벤트 packet JSON을 읽을 수 없다"]}, source
    source["data_as_of"] = data.get("collected_at")
    errors = event_contract_errors(data, asset)
    if errors:
        return {"contract_errors": errors[:5]}, source
    moves = []
    for move in (data.get("moves") or [])[-5:]:
        moves.append(
            {
                "date": move.get("date"),
                "change_pct": move.get("change_pct"),
                "benchmark_pct": move.get("benchmark_pct"),
                "excess_pct_beta_1": move.get("excess_pct"),
                # 공시 식별자는 시장마다 다르다 (DART 접수번호 / SEC 접수번호).
                # 없는 키는 싣지 않는다 — 매 판단마다 나가는 자리다.
                "candidate_filings": [
                    {
                        key: value
                        for key, value in (
                            ("filed", filing.get("filed")),
                            ("name", filing.get("name")),
                            ("rcept_no", filing.get("rcept_no")),
                            ("accession_number", filing.get("accession_number")),
                        )
                        if value is not None
                    }
                    for filing in (move.get("candidate_filings") or [])[:3]
                ],
            }
        )
    return (
        {
            "as_of": data.get("collected_at"),
            "move_count": len(data.get("moves") or []),
            "recent_moves": moves,
            # 판정이 어느 거래일 위에서 이뤄졌는지, 그리고 벤치마크 캘린더가
            # 어긋나 상대변동을 포기한 날이 있는지. 둘 다 숫자의 신뢰도를
            # 바꾸므로 요약에서 빠지면 안 된다.
            "last_bar": data.get("last_bar"),
            "benchmark_calendar_gaps": (
                (data.get("benchmark") or {}).get("calendar_gaps")
            ),
            "_warning": (
                "excess_pct_beta_1은 벤치마크를 beta=1로 뺀 값이며 인과 비중이 아니다. "
                "candidate_filings도 시간상 후보일 뿐 원인이 아니다"
            ),
        },
        source,
    )


def _etf_summary(asset: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if asset["asset_type"] != "ETF":
        return None, None
    paths = sorted(
        (KNOWLEDGE_DIR / "etfs" / asset["asset_id"]).glob("holdings-*.csv")
    )
    holdings_source = (asset.get("etf") or {}).get(
        "holdings_source", "MANUAL"
    )
    source = {
        "source_id": f"ETF_HOLDINGS:{asset['asset_id']}",
        "kind": f"{holdings_source}_HOLDINGS",
        "path": None,
        "data_as_of": None,
    }
    if not paths:
        return None, source
    path = paths[-1]
    rows = _csv_rows(path)
    weighted = []
    errors = []
    holding_ids: list[str] = []
    as_of_values: set[str] = set()
    for index, row in enumerate(rows):
        if row.get("etf_id") != asset["asset_id"]:
            errors.append(f"{index + 2}행 etf_id가 요청 자산과 다르다")
        holding_id = (row.get("holding_id") or "").strip()
        if not holding_id:
            errors.append(f"{index + 2}행 holding_id가 비었다")
        else:
            holding_ids.append(holding_id)
        row_as_of = (row.get("as_of") or "").strip()
        if not row_as_of:
            errors.append(f"{index + 2}행 as_of가 비었다")
        else:
            as_of_values.add(row_as_of)
        weight = _as_float(row.get("weight_pct"))
        if weight is None or weight < 0:
            errors.append(f"{index + 2}행 weight_pct가 유효하지 않다")
            continue
        weighted.append(
            {
                "holding_id": holding_id,
                "holding_name": row.get("holding_name"),
                "weight_pct": weight,
                "sector": row.get("sector") or None,
            }
        )
    if len(holding_ids) != len(set(holding_ids)):
        errors.append("holding_id가 중복됐다")
    if len(as_of_values) != 1:
        errors.append("구성종목 기준일이 행마다 다르다")
    weighted.sort(key=lambda row: row["weight_pct"], reverse=True)
    as_of = next(iter(as_of_values), path.stem.removeprefix("holdings-"))
    source["path"] = str(path.relative_to(ROOT))
    source["data_as_of"] = as_of
    coverage = round(sum(row["weight_pct"] for row in weighted), 2)
    warnings = []
    if holdings_source != "ISSUER":
        warnings.append("운용사 원문이 아닌 구성종목 스냅샷이다")
    if coverage <= 0:
        errors.append("구성종목 비중 합계가 0보다 커야 한다")
    elif coverage > 101:
        warnings.append(
            "비중 합계가 100%를 넘는다. 파생상품의 총노출 또는 중복을 확인한다"
        )
    return (
        {
            "as_of": as_of,
            "known_coverage_pct": coverage,
            "top_holdings": weighted[:8],
            "structure": asset.get("etf"),
            "warnings": warnings,
            "contract_errors": errors[:8],
        },
        source,
    )


def _as_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _estimate_rows(data: dict[str, Any]) -> dict[str, Any] | None:
    """연도별 추정을 판단에 쓰는 열만 남겨 싣는다.

    PBR·EV/EBITDA·순부채비율까지 넣으면 자산 하나에 표가 다섯 줄씩 붙는다.
    멀티플 판단에 실제로 쓰는 것은 이익 추정과 그 종목 자신의 PER 밴드다.
    """
    estimates = data.get("estimates") or {}
    rows = estimates.get("annual") or []
    if not rows:
        return None
    return {
        "unit": "억원",
        "annual": [
            {
                "period": row["period"],
                "actual": row["actual"],
                "revenue": row["revenue_100m"],
                "revenue_yoy_pct": row["revenue_yoy_pct"],
                "operating_income": row["operating_income_100m"],
                "eps": row["eps"],
                "per_at_source_price": row["per"],
                "roe_pct": row["roe_pct"],
            }
            for row in rows
        ],
        "per_band_actual": {
            key: (estimates.get("per_band_actual") or {}).get(key)
            for key in ("years", "min", "max", "median")
        },
        "_note": (
            "확정 연도 PER 밴드는 이 종목이 실제로 거래돼 온 멀티플이다. "
            "남의 목표주가 없이 만들 수 있는 자체 앵커이며, 여기에 사업 변화와 "
            "위험을 반영해 멀티플을 정하는 것은 모델의 판단이다"
        ),
    }


def _pct(value: float | None) -> float | None:
    """packet 에 싣는 비율의 자릿수. 없으면 없는 채로 둔다.

    `-8.132067439241252` 는 없는 정밀도를 주장하면서 자리만 먹는다. 원 데이터가
    20 거래일 잔고 두 개인데 소수 15 자리가 나올 이유가 없다.
    """
    return None if value is None else round(value, 2)


# 어떤 공시가 추정을 낡게 만드는가. 실적을 새로 말하는 것만 센다 — IR 개최나
# 주주총회까지 세면 이 표시가 항상 켜져 있게 되고, 항상 켜진 경고는 아무것도
# 말하지 않는다.
ESTIMATE_INVALIDATING_FORMS = {
    "PROVISIONAL_RESULT",
    "ANNUAL_REPORT",
    "HALF_REPORT",
    "QUARTER_REPORT",
    "10-K",
    "10-K/A",
    "10-Q",
    "10-Q/A",
}


def _estimates_event_lag(asset_id: str, as_of: Any) -> dict[str, Any] | None:
    """추정치 기준일 이후에 실적 공시가 있었는가.

    신선도를 시간으로만 재면 이것을 못 잡는다. `consensus:214450` 는 as_of
    2026-07-28 로 refresh 가 FRESH 라고 답했는데 그 사이 2026-08-07 에 2Q26
    잠정실적이 나왔고, 그 EPS 위에서 계산된 "2026E PER 19.87배" 가 판단에
    그대로 들어갔다. 문제는 12일 낡은 것이 아니라 **사건 하나만큼** 낡은 것이다.

    다시 수집해도 제공처가 아직 개정하지 않았으면 as_of 는 그대로다 — 실제로
    그랬다. 그래서 이것은 수집 지시(STALE)가 아니라 **판단 입력**이며,
    refresh 의 상태를 건드리지 않고 packet 의 관측으로만 남긴다.

    기준일과 공시일이 같은 날이면 켜지 않는다. 그날 그 공시를 반영했는지
    로컬에서 가릴 수 없고, 모르는 것을 경고로 바꾸면 경고가 신호를 잃는다.
    """
    if not isinstance(as_of, str) or len(as_of) < 10:
        return None
    boundary = as_of[:10]
    try:
        data = load_json(KNOWLEDGE_DIR / "filings" / f"{asset_id}.json")
    except (ContractError, OSError):
        return None
    events = []
    for filing in (data.get("filings") or []) if isinstance(data, dict) else []:
        if not isinstance(filing, dict):
            continue
        filed = filing.get("filing_date")
        if not isinstance(filed, str) or filed[:10] <= boundary:
            continue
        if filing.get("form") not in ESTIMATE_INVALIDATING_FORMS:
            continue
        events.append(
            {
                "filing_date": filed[:10],
                "form": filing.get("form"),
                "accession_number": filing.get("accession_number"),
                "description": filing.get("description"),
            }
        )
    if not events:
        return None
    events.sort(key=lambda row: row["filing_date"], reverse=True)
    return {
        "estimates_as_of": boundary,
        "latest_event_date": events[0]["filing_date"],
        "events_since": len(events),
        "events": events[:4],
        "_role": (
            "이 추정치는 기준일 이후에 나온 실적 공시를 **반영하지 않았다.** "
            "시간으로는 신선해도 사건으로는 낡았다는 뜻이며, 이 EPS 로 낸 "
            "배수는 그만큼 실제와 다르다. 다시 수집해도 제공처가 개정하지 "
            "않았으면 같은 값이 오므로 재수집을 요청하는 자리가 아니다 — "
            "공시 원문의 실적으로 직접 조정하거나, 조정하지 않았다면 그 "
            "한계를 unknowns 에 적는다"
        ),
    }


def _with_event_lag(
    block: dict[str, Any] | None,
    asset_id: str,
    *,
    as_of_key: str,
) -> dict[str, Any] | None:
    """추정 기준일이 실린 블록에 사건 낙후를 붙인다.

    배수가 계산되는 자리와 EPS 가 실린 자리 **둘 다**에 붙인다. 한 곳만 보고
    결론을 쓰는 경로가 실제로 있었다 — 2026-08-09 판단은 `multiples_at_review_price`
    의 19.87배를 그대로 인용했고 `structured_optional.consensus` 는 개정 방향을
    보려고 따로 읽었다. 둘 중 하나만 표시하면 다른 쪽을 읽은 날 놓친다.
    """
    if not isinstance(block, dict):
        return block
    lag = _estimates_event_lag(asset_id, block.get(as_of_key))
    if lag:
        block["stale_across_event"] = lag
    return block


def _forward_multiple_row(asset_id: str, name: str | None) -> dict[str, Any]:
    """컨센서스 파일 하나에서 선행 배수 한 줄. 없으면 available=False.

    자신과 동종이 **같은 함수**를 지나야 한다. 기준연도·EPS 기준·PER 계산이
    한 곳에서 나와야 "우리가 중앙값보다 싸다"가 성립한다.
    """
    path = KNOWLEDGE_DIR / "consensus" / f"{asset_id}.json"
    try:
        data = load_json(path)
    except (ContractError, OSError):
        return {"asset_id": asset_id, "name": name, "available": False,
                "forward_per": None}
    annual = ((data.get("estimates") or {}).get("annual")) or []
    forward = next((row for row in annual if not row["actual"]), None)
    band = (data.get("estimates") or {}).get("per_band_actual") or {}
    return {
        "asset_id": asset_id,
        "name": name,
        "available": True,
        "as_of": data.get("as_of"),
        "forward_period": forward["period"] if forward else None,
        "forward_per": forward["per"] if forward else None,
        "forward_revenue_yoy_pct": (
            forward["revenue_yoy_pct"] if forward else None
        ),
        "forward_roe_pct": forward["roe_pct"] if forward else None,
        "per_band_actual_min": band.get("min"),
        "per_band_actual_max": band.get("max"),
    }


def _peer_multiples(asset: dict[str, Any]) -> dict[str, Any] | None:
    """동종의 배수를 같은 소스·같은 방식으로 모아 비교한다.

    언론이 인용하는 '에이피알 30배' 같은 값은 기준연도도 주가시점도 알 수 없어
    비교에 쓸 수 없다. 여기서는 동종도 같은 수집기를 거치므로 EPS 기준·PER
    계산·기준일이 전부 같다.
    """
    peers = asset.get("peers") or []
    if not peers:
        return None
    rows = [_forward_multiple_row(peer["asset_id"], peer["name"])
            for peer in peers]
    usable = [row["forward_per"] for row in rows if row.get("forward_per")]
    median = round(statistics.median(usable), 2) if usable else None
    # 자기 자신도 **같은 경로로** 읽는다. 동종만 이 함수를 지나고 자신은 다른
    # 블록에서 오면 기준연도·주가시점이 달라져 비교가 성립하지 않는다.
    subject = _forward_multiple_row(asset["asset_id"], asset.get("name"))
    return {
        "basis": "동일 수집기·동일 기준일의 컨센서스 추정",
        "subject": subject,
        "peers": rows,
        "forward_per_median": median,
        # 배수와 중앙값을 둘 다 실으면서 괴리를 빼면 모델이 직접 나눈다.
        # 화면(`peer_kr`·`peer_us`)은 이미 이 값과 "중앙값보다 낮음/높음/근접"
        # 라벨을 내고 있었다 — packet 에만 없었다.
        "forward_per_vs_median_pct": _pct(
            change_pct(subject["forward_per"], median)
        ),
        "forward_per_min": min(usable) if usable else None,
        "forward_per_max": max(usable) if usable else None,
        "_warning": (
            "동종 선정은 registry에 명시된 판단이며 자동으로 옳지 않다. "
            "성장률·수익성이 다르면 같은 배수를 줄 이유가 없으므로 "
            "forward_revenue_yoy_pct와 forward_roe_pct를 함께 본다"
        ),
    }


# 이 자산에서 무엇을 받을 수 있고 무엇을 못 받는가. 각 행은 넷 중 하나다 —
# 값이 있다 / 등록했는데 아직 없다 / 이 자산에 등록하지 않았다 / 이 시장에
# 수집기가 없다. 마지막 둘이 이 블록이 존재하는 이유다. 한국 종목은 수급·
# 부문·정상화이익을 받는데 미국은 하나도 못 받았고, **그 사실이 packet 어디에도
# 없어서** 모델은 그 입력이 이 회사에 원래 없는 것인지 이번에만 빠진 것인지
# 구분할 수 없었다. 빈칸은 "해당 없음"으로 읽힌다.
COVERAGE_NO_COLLECTOR = {
    "epc_contract_notes": "현재 도급공사 주석 추출기는 한국 DART 연결 보고서 경로다. 이 시장의 원문은 모델이 직접 확인한다",
    "forward_estimates": (
        "컨센서스 수집기는 네이버 금융 기반이라 KR 전용이다. forward EPS·"
        "성장률·목표주가가 필요하면 직접 조사해 sources 에 등록한다"
    ),
    "peer_multiples": (
        "동일기준 peer 배수는 같은 컨센서스 수집기 위에서만 만들어진다. "
        "미국 종목의 비교배수는 기준연도·주가시점이 같은지 직접 확인해야 한다"
    ),
    "segment_revenue": (
        "부문별 매출 수집기는 DART 사업보고서 표 전용이다. 미국은 10-K/10-Q "
        "원문에 있으므로 local_documents 를 `cio search` 로 읽는다"
    ),
    "flow": (
        "종목별 투자자 수급·대차잔고는 한국거래소 공개 자료다. 미국에는 "
        "같은 단위의 공개 경로가 없다"
    ),
    "inventory": (
        "재고자산 시계열 수집기는 DART 재무제표 주석 전용이다. 미국은 "
        "10-Q 원문에서 읽는다"
    ),
}


def _data_coverage(
    asset: dict[str, Any],
    *,
    structured_optional: dict[str, Any] | None = None,
    fundamentals: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """무엇이 있고 무엇이 왜 없는지. 판정하지 않고 가용성만 센다."""
    optional = structured_optional or {}
    fundamentals = fundamentals or {}
    enabled = set(asset.get("optional_collectors") or [])
    market = asset.get("market")
    is_kr = market == "KR"

    def row(
        key: str,
        *,
        registered: bool,
        available_when: Any,
        market_supported: bool,
        note: str | None = None,
    ) -> dict[str, Any]:
        if not market_supported:
            return {
                "state": "NO_COLLECTOR_FOR_MARKET",
                "note": COVERAGE_NO_COLLECTOR[key],
            }
        if not registered:
            return {
                "state": "NOT_REGISTERED",
                "note": (
                    "이 자산의 optional_collectors 에 없다. 필요하면 "
                    "config/assets.json 에 등록한 뒤 `cio refresh <asset_id> "
                    "--run --optional` 로 받는다"
                ),
            }
        # 수집기가 돌았고 "이 자산에서는 낼 수 없다"는 결론이 나온 경우다.
        # 안 받은 것과 구분하지 않으면, 다시 돌려도 같은 결과인데 packet 은
        # 계속 "받으면 된다"고 말하고 모델은 그 요청을 반복한다.
        if isinstance(available_when, dict) and available_when.get("state") == "INVALID":
            return {"state": "REGISTERED_NOT_COLLECTED", "note": "산출물이 원문 재검증에 실패했다. refresh로 복구한다"}
        if isinstance(available_when, dict) and available_when.get("state") in {"NETWORK_ERROR", "PARSE_ERROR", "NO_DATA", "PARTIAL", "STALE_AFTER_FAILURE"}:
            return {"state": available_when["state"], "last_attempt": available_when.get("last_attempt"),
                    "components": available_when.get("components"),
                    "note": "연결 실패는 재시도, 해석 실패는 수집기 점검, NO_DATA는 제공처의 부재 관측이다. PARTIAL은 정상 부분만 사용한다. 실패 후 남은 과거 값의 기준일을 확인한다"}
        if isinstance(available_when, dict) and available_when.get(
            "available"
        ) is False:
            return {
                "state": "COLLECTOR_FOUND_NOTHING",
                "note": (
                    "수집기를 돌렸으나 이 자산에서 낼 수 없었다. 다시 돌려도 "
                    "같으므로 재수집을 요청하지 않는다. 사유는 "
                    "structured_optional 의 해당 항목에 있다"
                ),
            }
        if not available_when:
            return {
                "state": "REGISTERED_NOT_COLLECTED",
                "note": (
                    "등록은 됐는데 산출물이 없거나 낡았다. "
                    "`cio refresh <asset_id> --run --optional` 로 받는다"
                ),
            }
        entry: dict[str, Any] = {"state": "AVAILABLE"}
        if isinstance(available_when, dict) and available_when.get("as_of"):
            entry["as_of"] = available_when["as_of"]
        if note:
            entry["note"] = note
        return entry

    coverage: dict[str, Any] = {
        "forward_estimates": row(
            "forward_estimates",
            registered=is_kr,
            available_when=optional.get("consensus"),
            market_supported=is_kr,
        ),
        "peer_multiples": row(
            "peer_multiples",
            registered=bool(asset.get("peers")),
            available_when=optional.get("peer_multiples"),
            market_supported=is_kr,
        ),
        "segment_revenue": row(
            "segment_revenue",
            registered="KR_SEGMENTS" in enabled,
            available_when=optional.get("segments"),
            market_supported=is_kr,
        ),
        "flow_and_lending": row(
            "flow",
            registered=bool({"KR_FLOW", "KR_LENDING"} & enabled),
            available_when=optional.get("kr_flow") or optional.get("kr_lending"),
            market_supported=is_kr,
        ),
        "inventory": row(
            "inventory",
            registered="KR_INVENTORY" in enabled,
            available_when=optional.get("kr_inventory"),
            market_supported=is_kr,
        ),
        "normalized_earnings": row(
            "forward_estimates",  # 시장 제한이 없어 이 자리는 쓰이지 않는다
            registered="NORMALIZED_EARNINGS" in enabled,
            available_when=optional.get("normalized_earnings"),
            market_supported=True,
        ),
        "_role": (
            "이 자산에서 받을 수 있는 입력의 가용성이다. NOT_REGISTERED 는 "
            "등록하면 받을 수 있다는 뜻이고 NO_COLLECTOR_FOR_MARKET 은 "
            "만들기 전에는 못 받는다는 뜻이다. REGISTERED_NOT_COLLECTED 는 "
            "아직 안 받았다는 뜻이라 refresh 로 채워지지만, "
            "COLLECTOR_FOUND_NOTHING 은 **이미 돌렸고 낼 수 없었다**는 뜻이라 "
            "다시 돌려도 같다 — 이 둘을 섞으면 채워지지 않을 것을 계속 "
            "요청하게 된다. **빈칸을 '해당 없음'으로 읽지 말라** — 없는 입력이 "
            "결론을 바꿀 수 있으면 unknowns 에 적고, 계속 필요하면 "
            "research_requests 로 남긴다"
        ),
    }
    values = fundamentals.get("values") or {}
    shares = _as_float(values.get("shares_diluted"))
    coverage["dilution"] = (
        {
            "state": "AVAILABLE",
            "shares_diluted": shares,
            "basis": fundamentals.get("eps_basis"),
            **({"share_count_basis": fundamentals["share_count_basis"]}
               if fundamentals.get("share_count_basis") else {}),
            "note": (
                "재무 보고기간의 EPS 분모이며 현재 발행주식수·미래 완전희석주식수가 아니다. "
                "**주식수 추이는 없다** — "
                "SBC·전환사채·유상증자로 몇 %씩 늘고 있는지는 이 packet 이 "
                "답하지 못한다. 희석이 결론에 걸리면 원문에서 직접 읽는다"
                + (
                    ". eps_basis 가 DILUTED 가 아니면 희석 공시가 없어 "
                    "기본주당이익으로 계산한 값이라 희석증권이 생기면 틀린다"
                    if fundamentals.get("eps_basis") not in (None, "DILUTED")
                    else ""
                )
            ),
        }
        if shares
        else {
            "state": "REGISTERED_NOT_COLLECTED",
            "note": "재무 스냅샷에 희석주식수가 없다",
        }
    )
    if not is_kr and asset.get("asset_type") == "STOCK":
        def us_coverage(key: str, note: str) -> dict[str, Any]:
            section = optional.get(key) or {}
            state = section.get("state", section.get("status", "NOT_COLLECTED"))
            return {"state": "REGISTERED_NOT_COLLECTED" if state == "NOT_COLLECTED" else state,
                    "freshness": section.get("freshness"), "source_id": f"{key.upper()}:{asset['asset_id']}",
                    "note": note}
        coverage["segment_revenue"] = us_coverage("us_segments", "SEC 원문의 부문·제품·지역 축별 실적이다. 기간·축이 다른 값을 합산하지 않는다. 단일부문과 추출 실패를 구분한다")
        coverage["eps_denominator"] = coverage["dilution"]
        coverage["dilution"] = us_coverage("us_capital", "표지 발행량·기말 총량·종류별 주식수·주식보상·발행 현금흐름을 자동 추출한다. 날짜/범위가 다른 값을 합치거나 ATM 한도를 발행 완료로 읽지 않는다. 미래 완전희석 분모는 모델이 고른다")
        coverage["earnings_announcement"] = us_coverage("us_earnings", "회사 IR에서 수집한 실제 발표 안내이며 잠정/확정 여부를 보존한다. 다음 SEC 제출일 추정과 다르다")
    if asset.get("analysis_lens") == "EPC_BACKLOG" or "KR_EPC" in enabled:
        coverage["epc_contract_notes"] = row(
            "epc_contract_notes", registered="KR_EPC" in enabled,
            available_when=optional.get("epc_contract_notes"), market_supported=is_kr,
        )
        if (optional.get("epc_contract_notes") or {}).get("unresolved"):
            coverage["epc_contract_notes"]["unresolved"] = optional["epc_contract_notes"]["unresolved"]
    return coverage


def _optional_collection_summaries(
    asset: dict[str, Any],
    *, fundamentals: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    enabled = set(asset.get("optional_collectors") or [])
    asset_id = asset["asset_id"]
    summaries: dict[str, Any] = {}
    sources: list[dict[str, Any]] = []

    if asset["market"] == "KR":
        path = KNOWLEDGE_DIR / "consensus" / f"{asset_id}.json"
        data = None
        if path.exists():
            try:
                data = load_json(path)
            except (ContractError, OSError):
                data = None
            if data:
                near = (data.get("dispersion") or {}).get("recent") or {}
                wide = (data.get("dispersion") or {}).get(
                    "consensus_window"
                ) or {}
                revisions = data.get("revisions") or {}
                history = data.get("history") or []
                # 제공처 16곳을 그대로 실으면 패킷만 무거워지고 읽히지 않는다.
                # 남기는 것은 이익 추정(멀티플은 모델이 정한다), 얼마나 갈리는가,
                # 어느 쪽으로 개정되는가 셋뿐이다.
                summaries["consensus"] = {
                    "as_of": data.get("as_of"),
                    "estimates_as_of": data.get("estimates_as_of"),
                    "as_of_basis": data.get("as_of_basis"),
                    "consensus_eps": (data.get("summary") or {}).get("eps"),
                    "consensus_per": (data.get("summary") or {}).get("per"),
                    "rating": (data.get("summary") or {}).get("rating"),
                    "institution_count": (data.get("summary") or {}).get(
                        "institution_count"
                    ),
                    "target_price_mean": (data.get("summary") or {}).get(
                        "target_price"
                    ),
                    "target_recent_30d": {
                        key: near.get(key)
                        for key in ("count", "min", "max", "median", "spread_pct")
                    },
                    "target_window_90d": {
                        key: wide.get(key)
                        for key in ("count", "min", "max", "spread_pct")
                    },
                    "stale_targets_lift_the_mean": bool(
                        wide.get("max")
                        and near.get("max")
                        and wide["max"] > near["max"]
                    ),
                    # 건수만 실으면 8 건 하향이 8×-1% 인지 1×-40%+7×-2% 인지
                    # 구분되지 않는다. 실제로 000660 은 건수로는 "약간 하향
                    # 우위"(상향 6·하향 8)인데 실제 개정은 -35.7% 와 +171.4%
                    # 였다 — 컨센서스의 완만한 이동이 아니라 **애널리스트들이
                    # 갈라진 상태**이고, 그 사실이 통째로 사라졌다.
                    #
                    # 예전에는 "목표가 수준보다 방향이 정보를 담는다"를 근거로
                    # 크기를 뺐다. 그러나 그것은 **어느 값이 판단에 중요한가**에
                    # 대한 주장이고, 그 판단은 모델의 몫이다. 개정폭은 계산하면
                    # 나오는 사실이므로 PC 는 실어 보내고, 무게를 정하는 일은
                    # 모델에게 남긴다. 둘 합쳐 276 바이트다.
                    "revisions": {
                        key: revisions.get(key)
                        for key in (
                            "window_days",
                            "observed",
                            "raised",
                            "lowered",
                            "unchanged",
                            "largest_raise",
                            "largest_cut",
                        )
                    },
                    "estimates": _estimate_rows(data),
                    "history_points": len(history),
                    "_warning": (
                        "목표주가는 애널리스트의 결론이지 입력이 아니다. 매도 "
                        "리포트가 거의 나오지 않아 목표가는 대체로 현재가 위에 "
                        "있고 주가를 따라 개정되므로 '목표가가 현재가보다 높다'는 "
                        "정보가 아니다. 밸류에이션에는 consensus_eps 를 쓰고 "
                        "멀티플은 직접 정한다. 2차 출처(EXPLORATORY)다"
                    ),
                }
                if data.get("components"):
                    summaries["consensus"].update(state=data.get("state"), available=data.get("available"), components=data["components"])
                    summaries["consensus"]["_warning"] += " 전망 자체의 갱신일은 estimates_as_of이며 null이면 모른다. 수집일이나 최신 목표가 보고일로 대체하지 않는다."
                _with_event_lag(
                    summaries["consensus"], asset_id, as_of_key="as_of"
                )
                sources.append(
                    {
                        "source_id": f"CONSENSUS:{asset_id}",
                        "kind": "SECONDARY_ESTIMATE",
                        "path": str(path.relative_to(ROOT)),
                        "data_as_of": data.get("as_of"),
                    }
                )
        attempt_path = KNOWLEDGE_DIR / "collection_status" / f"consensus-{asset_id}.json"
        if attempt_path.exists():
            try:
                attempt = load_json(attempt_path)
            except (ContractError, OSError):
                attempt = None
            if attempt and attempt.get("asset_id") == asset_id:
                summary = summaries.setdefault("consensus", {"available": False})
                summary["last_attempt"] = attempt
                newer_attempt = not data or str(attempt.get("attempted_at") or "") >= str(data.get("collected_at") or "")
                if newer_attempt and attempt.get("state") in {"NETWORK_ERROR", "PARSE_ERROR"}:
                    summary["state"] = "STALE_AFTER_FAILURE" if summary.get("estimates") else attempt["state"]
                    summary["components"] = attempt.get("components")
                sources.append({"source_id": f"COLLECTION_STATUS:{asset_id}", "kind": "COLLECTION_STATUS",
                                "path": str(attempt_path.relative_to(ROOT)), "data_as_of": attempt.get("attempted_at")})
        peer_block = _peer_multiples(asset)
        if peer_block:
            summaries["peer_multiples"] = peer_block
            for peer in asset.get("peers") or []:
                peer_path = (
                    KNOWLEDGE_DIR / "consensus" / f"{peer['asset_id']}.json"
                )
                if peer_path.exists():
                    sources.append({
                        "source_id": f"CONSENSUS_PEER:{peer['asset_id']}",
                        "kind": "SECONDARY_ESTIMATE",
                        "path": str(peer_path.relative_to(ROOT)),
                        "data_as_of": None,
                    })

    if "KR_FLOW" in enabled:
        path = KNOWLEDGE_DIR / "flows" / f"{asset_id}.csv"
        if path.exists():
            rows = _csv_rows(path)
            recent = rows[-20:]
            if recent:
                institution = sum(
                    _as_float(row.get("institution_net")) or 0 for row in recent
                )
                foreign = sum(
                    _as_float(row.get("foreign_net")) or 0 for row in recent
                )
                summaries["kr_flow"] = {
                    "as_of": recent[-1].get("date"),
                    "lookback_sessions": len(recent),
                    "institution_net_shares": round(institution),
                    "foreign_net_shares": round(foreign),
                    "foreign_ownership_pct": _as_float(
                        recent[-1].get("foreign_pct")
                    ),
                    "_warning": "2차 출처 수급이며 단독 매매 근거가 아니다",
                }
                sources.append(
                    {
                        "source_id": f"KR_FLOW:{asset_id}",
                        "kind": "SECONDARY_MARKET_DATA",
                        "path": str(path.relative_to(ROOT)),
                        "data_as_of": recent[-1].get("date"),
                    }
                )

    if "KR_LENDING" in enabled:
        path = KNOWLEDGE_DIR / "lending" / f"{asset_id}.csv"
        if path.exists():
            rows = _csv_rows(path)
            recent = rows[-20:]
            if recent:
                first = _as_float(recent[0].get("balance_shares"))
                last = _as_float(recent[-1].get("balance_shares"))
                # balance_amount 는 싣지 않는다 — balance_shares × 종가이고
                # 종가는 이미 packet 에 있다. 같은 사실을 두 번 보내지 않는다.
                summaries["kr_lending"] = {
                    "as_of": recent[-1].get("date"),
                    "lookback_sessions": len(recent),
                    "balance_shares": round(last) if last is not None else None,
                    "balance_change_shares": (
                        round(last - first)
                        if first is not None and last is not None
                        else None
                    ),
                    # 주식수만 주고 변화율을 빼면 모델이 시작잔고를 역산해서
                    # 직접 나눈다. **모델이 하는 산술은 검증되지 않은 산술**이고,
                    # 화면은 이미 `change_pct` 로 같은 값을 내고 있다 — packet 에만
                    # 없어서 판단이 화면을 읽어야 했다. 세 역할의 경계가 여기서
                    # 샌다: 계산은 PC 가 하고 모델은 판단만 한다.
                    "balance_change_pct": _pct(change_pct(last, first)),
                    "_role": (
                        "빌려가 아직 갚지 않은 주식 수. 공매도 잔고가 아니다 — "
                        "결제·차익거래·의결권 목적도 같은 숫자에 섞인다"
                    ),
                    "_unobservable": (
                        "공매도·신용융자 잔고는 종목 단위 공개 경로가 없다. 찾지 않는다"
                    ),
                }
                sources.append(
                    {
                        "source_id": f"KR_LENDING:{asset_id}",
                        "kind": "MARKET_DATA",
                        "path": str(path.relative_to(ROOT)),
                        "data_as_of": recent[-1].get("date"),
                    }
                )

    if "KR_INVENTORY" in enabled:
        path = KNOWLEDGE_DIR / "inventory" / f"{asset_id}.csv"
        if path.exists():
            rows = _csv_rows(path)
            trend = [
                {
                    "period_end": row.get("period_end"),
                    "inventories": _as_float(row.get("inventories")),
                    "dio_days": _as_float(row.get("dio_days")),
                    "qoq_pct": _as_float(row.get("qoq_pct")),
                    "yoy_pct": _as_float(row.get("yoy_pct")),
                    "cogs_derived": row.get("cogs_derived") == "True",
                }
                for row in rows[-4:]
            ]
            if trend:
                summaries["kr_inventory"] = {
                    "latest_period": trend[-1]["period_end"],
                    "recent_quarters": trend,
                }
                sources.append(
                    {
                        "source_id": f"KR_INVENTORY:{asset_id}",
                        "kind": "FILING_DERIVED",
                        "path": str(path.relative_to(ROOT)),
                        "data_as_of": trend[-1]["period_end"],
                    }
                )

    if "KR_SEGMENTS" in enabled:
        path = KNOWLEDGE_DIR / "segments" / f"{asset_id}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict) and data.get("available") is False:
            # 표를 못 찾았다는 관측도 결과다. 이것을 싣지 않으면 packet 은
            # "아직 안 받았다"로 읽고, 몇 번을 다시 돌려도 같은데 계속 받을 수
            # 있는 것처럼 보인다.
            summaries["segments"] = {
                "available": False,
                "absence": data.get("absence"),
                "report": data.get("report_name"),
                "single_segment_statement": data.get(
                    "single_segment_statement"
                ),
                "_reason": data.get("_reason"),
                "_retry_note": data.get("_retry_note"),
                "_role": (
                    "부문이 없다는 것도 관측이다. SINGLE_SEGMENT 는 회사가 그렇게 "
                    "공시했다는 뜻이며 그 자체가 정보다 — 이 회사의 실적은 부문으로 "
                    "쪼개 볼 수 없고, 제품군 비중은 부문 공시가 아니라 다른 근거로 "
                    "찾아야 한다. NO_TABLE_FOUND 는 다르다: 우리가 못 읽은 것일 수 "
                    "있으므로 결론이 부문에 걸리면 원문을 직접 본다"
                ),
            }
        elif isinstance(data, dict) and data.get("latest_mix"):
            summaries["segments"] = {
                "report": data.get("report_name"),
                "period": data.get("latest_period"),
                "mix": data["latest_mix"],
                "_role": (
                    "이 회사가 어느 부문으로 돈을 버는지다. drivers 의 비중을 "
                    "감으로 주지 말고 여기서 시작한다 — 영업이익 비중이 한 자리인 "
                    "부문의 뉴스는 그만큼만 무게를 갖는다. 매출 비중과 영업이익 "
                    "비중이 크게 다른 것이 이 표의 요점이다"
                ),
                "_warning": (
                    "정기보고서 기준이라 그 이후 분기는 반영되지 않는다. "
                    "회사가 적은 비중이며 내부거래 제거 방식에 따라 합이 100이 "
                    "아닐 수 있다"
                ),
            }
            sources.append(
                {
                    "source_id": f"KR_SEGMENTS:{asset_id}",
                    "kind": "FILING_DERIVED",
                    "path": str(path.relative_to(ROOT)),
                    "data_as_of": data.get("filing_date"),
                }
            )

    if "KR_EPC" in enabled:
        from collectors.kr_epc import contract_errors
        from calculators.epc import summary as epc_summary
        path = KNOWLEDGE_DIR / "epc" / f"{asset_id}.json"
        if path.exists():
            try:
                data = load_json(path)
                errors = contract_errors(data, asset_id)
            except (OSError, ValueError) as exc:
                errors = [str(exc)]
            if errors:
                summaries["epc_contract_notes"] = {"available": False, "state": "INVALID", "problems": errors}
            else:
                summaries["epc_contract_notes"] = epc_summary(data, fundamentals)
                sources.append({"source_id": f"KR_EPC:{asset_id}", "kind": "FILING_DERIVED", "path": str(path.relative_to(ROOT)), "data_as_of": data["reports"][0]["report_date"]})

    if "NORMALIZED_EARNINGS" in enabled:
        path = KNOWLEDGE_DIR / "normalized" / f"{asset_id}.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict):
            series = data.get("ttm_series") or []
            latest = series[-1] if series else None
            summaries["normalized_earnings"] = {
                "as_of": (latest or {}).get("period_end"),
                "latest_ttm": latest,
                "normalization": data.get("normalization"),
                "_warning": (
                    "가공된 사이클 참고치다. 모델이 방법론을 선택한 경우에만 "
                    "가치평가 입력으로 승격한다"
                ),
            }
            sources.append(
                {
                    "source_id": f"NORMALIZED_EARNINGS:{asset_id}",
                    "kind": "CALCULATION",
                    "path": str(path.relative_to(ROOT)),
                    "data_as_of": (latest or {}).get("period_end"),
                }
            )

    return summaries, sources


def _fx_rate() -> tuple[float | None, str | None]:
    summary, _ = _price_summary("KRW=X")
    if not summary:
        return None, None
    return float(summary["close"]), summary["as_of"]


def _us_automated_summaries(
    asset: dict[str, Any], *, refresh_tasks: list[Any] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Official US originals are collected once; only checked summaries enter the packet."""
    if (asset["market"], asset["asset_type"]) != ("US", "STOCK"):
        return {}, []
    import importlib
    if refresh_tasks is None:
        refresh_tasks = inspect_plan(asset["asset_id"], include_optional=True)
    task_states = {capability: task for task in refresh_tasks
                   if (capability := getattr(task, "capability", None))}
    summaries, sources = {}, []
    for key, source_prefix in (("us_capital", "US_CAPITAL"), ("us_segments", "US_SEGMENTS"), ("us_earnings", "US_EARNINGS")):
        if key == "us_earnings" and not asset.get("ir_sources"):
            summaries[key] = {"state": "NOT_REGISTERED", "available": False,
                              "_note": "config/assets.json에 공식 IR URL(ir_sources)을 등록해야 실적 발표일을 수집할 수 있다"}
            continue
        path = KNOWLEDGE_DIR / key / f"{asset['asset_id']}.json"
        if not path.exists():
            summaries[key] = {"state": "NOT_COLLECTED", "available": False,
                              "_note": "cio refresh --run이 공식 원문을 수집하고 추출한다"}
            continue
        module = importlib.import_module(f"collectors.{key}")
        try:
            data = load_json(path)
            errors = module.validate_artifact(data, asset["asset_id"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors = [str(exc)]
        if errors:
            summaries[key] = {"state": "INVALID", "available": False, "problems": errors[:4]}
            continue
        summaries[key] = module.summary(data)
        summaries[key].setdefault("state", data.get("status", data.get("state", "UNKNOWN")))
        task = task_states.get(source_prefix)
        if task and task.state != "FRESH":
            summaries[key]["freshness"] = task.state
            summaries[key]["_freshness_note"] = task.reason or "수집 산출물이 오래됐거나 입력 공시보다 먼저 생성됐다. 갱신한다"
            if summaries[key]["state"] in {"AVAILABLE", "PARTIAL", "NO_ANNOUNCEMENT"}:
                summaries[key]["source_state"] = summaries[key]["state"]
                summaries[key]["state"] = task.state
                summaries[key]["available"] = False
        sources.append({"source_id": f"{source_prefix}:{asset['asset_id']}",
                        "kind": "ISSUER_EVENT_CALENDAR" if key == "us_earnings" else "FILING_DERIVED",
                        "path": str(path.relative_to(ROOT)), "data_as_of": data.get("collected_at")})
        for document in data.get("reports", data.get("sources", [])):
            if isinstance(document, dict) and document.get("source_id") and document.get("path"):
                sources.append({"source_id": document["source_id"], "path": document["path"],
                                "kind": "REGULATOR_FILING_DOCUMENT" if key != "us_earnings" else "ISSUER_DOCUMENT",
                                "data_as_of": document.get("filing_date") or document.get("fetched_at")})
    return summaries, sources


def _portfolio_as_of(path: Path) -> str | None:
    try:
        with path.open(encoding="utf-8-sig") as handle:
            for line in handle:
                if not line.lstrip().startswith("#"):
                    break
                match = re.search(r"기준일\s+(\d{4}-\d{2}-\d{2})", line)
                if match:
                    return match.group(1)
    except OSError:
        return None
    return None


def _attach_position_math(
    portfolio: dict[str, Any] | None,
    asset: dict[str, Any],
    price: float,
    *,
    include_add_ladder: bool = True,
) -> dict[str, Any] | None:
    """거래 원장 기반 산술을 보유 현황에 붙인다.

    지금까지 패킷의 평단은 증권사 스냅샷의 (평가액 − 평가손익) ÷ 수량 역산
    하나뿐이었다. 스냅샷이 낡으면 조용히 틀리는데 아무도 모른다 — 거래 원장이
    로컬에 있는데도 읽는 코드가 없었다. 두 값을 각각 내고 차이를 함께 싣는다.

    배수 환산·세 분모 비중·수량 사다리도 여기서 붙인다. 전부 결정론적 산술이며
    모델이 추론 시점에 나눗셈을 반복할 이유가 없다. 판정은 붙이지 않는다.
    """
    if not portfolio or not portfolio.get("available"):
        return None
    existing = portfolio.get("existing_position") or {}
    if not existing.get("held"):
        return None
    asset_id = asset["asset_id"]
    trades = KNOWLEDGE_DIR / "portfolio" / "trades" / f"{asset_id}.csv"
    if not trades.exists():
        existing["ledger"] = {
            "available": False,
            "_reason": (
                f"knowledge/portfolio/trades/{asset_id}.csv 가 없다. 평단은 "
                "증권사 스냅샷 역산값 하나뿐이며 스냅샷이 낡으면 틀린다"
            ),
        }
        return None
    try:
        math_result = build_position_math(
            asset_id, price, currency=asset["currency"]
        )
    except (PositionError, OSError, ValueError) as exc:
        existing["ledger"] = {"available": False, "_reason": str(exc)}
        return None

    ledger = math_result["ledger"]
    existing["ledger"] = {
        "available": True,
        "source": ledger["source"],
        "method": ledger["method"],
        "currency": math_result["currency"],
        "quantity": ledger["quantity"],
        "average_cost": ledger["average_cost"],
        "cost_basis": ledger["cost_basis"],
        "realized_pnl": ledger["realized_pnl"],
        "trade_count": ledger["trade_count"],
        "first_settle_date": ledger["first_settle_date"],
        "free_receipts": ledger.get("free_receipts"),
        "_role": (
            "거래 원장에서 이동평균법으로 접은 교차검증값이다. **평단의 "
            "권위는 위의 average_cost(증권사 스냅샷)** 이며 결론은 그쪽을 "
            "쓴다. 이 값이 벌어졌다면 원장이 재현하지 못하는 항목이 있다는 "
            "신호이고, 실현손익은 스냅샷에 없으므로 여기서만 나온다"
        ),
    }
    reconciliation = math_result["reconciliation"]
    if reconciliation.get("available"):
        existing["ledger_vs_snapshot"] = {
            key: reconciliation.get(key)
            for key in (
                "snapshot_as_of", "quantity_diff", "average_cost_diff",
                "average_cost_diff_pct", "comparable", "within_tolerance",
                "tolerance_pct", "authority",
            )
        } | {
            "_role": (
                "평단의 권위는 authority 가 가리키는 쪽이다. 벌어졌다는 것은 "
                "다른 쪽이 재현하지 못하는 항목이 있다는 뜻이며, 결론이 평단에 "
                "걸리면 그 사실을 unknowns 에 남긴다"
            )
        }
    if math_result.get("recurring_buy"):
        portfolio["recurring_buy"] = math_result["recurring_buy"]
    if math_result.get("weights"):
        weight = math_result["weights"]
        portfolio["weights_by_denominator"] = {
            key: weight.get(key)
            for key in (
                "total_assets_pct", "equity_sleeve_pct", "risk_assets_pct",
                "denominators_krw", "_definitions", "_staleness",
            )
        } | {"_role": weight["_role"]}
    # 추가 수량별 사다리는 매수 질문의 눈금이다. 보유분을 어떻게 할지 묻는
    # packet 에 실으면 묻지 않은 매수 계획이 답 옆에 놓인다.
    if math_result.get("ladder") and include_add_ladder:
        portfolio["add_ladder"] = {
            "basis": math_result.get("ladder_basis"),
            "rungs": [
                {
                    "add_quantity": rung["add_quantity"],
                    "add_cost": rung["add_cost"],
                    "new_average_cost": rung["new_average_cost"],
                    "average_cost_change": rung["average_cost_change"],
                    "total_assets_pct": (
                        (rung["weights_after"] or {}).get("total_assets_pct")
                    ),
                    "equity_sleeve_pct": (
                        (rung["weights_after"] or {}).get("equity_sleeve_pct")
                    ),
                }
                for rung in math_result["ladder"]
            ],
            "_role": (
                "추가 수량별로 평단과 비중이 어디로 가는지의 산술이다. 얼마를 "
                "사라는 제안이 아니며 눈금은 현재 보유 수량 기준이다"
            ),
        }
    return {
        "source_id": f"POSITION_LEDGER:{asset_id}",
        "kind": "USER_DATA",
        "path": ledger["source"],
        "data_as_of": ledger.get("last_settle_date"),
    }


def _sell_block(
    *,
    held_quantity: float | None,
    review_quantity: float | None,
    target_quantity: float | None,
    price: float,
    fx: float | None,
    cash: float | None,
    total_at_review: float | None,
    market: str,
) -> dict[str, Any]:
    """보유분을 어떻게 할지 묻는 질문의 산술.

    매수 사다리를 만들지 않는다. **묻지 않은 질문에 답을 만들어 주면 모델은
    그 답을 근거로 받아들인다** — 분할매도를 묻는 packet 이 803주 매수 계획을
    싣고 나간 적이 있다.

    수량을 사용자가 정하지 않았으면 여기서 정하지 않는다. 얼마를 팔지는
    모델의 판단이고 PC 는 사용자가 말한 수량의 산술만 낸다.
    """
    errors: list[str] = []
    quantity = review_quantity
    basis = "USER_INPUT"
    if quantity is None and target_quantity is not None:
        basis = "DERIVED_FROM_TARGET"
        quantity = (
            held_quantity - target_quantity if held_quantity is not None else None
        )
        if held_quantity is not None and target_quantity >= held_quantity:
            errors.append(
                f"목표 잔여수량 {target_quantity}이 보유수량 {held_quantity} "
                "이상이라 매도 수량이 나오지 않는다"
            )
            quantity = None
    if quantity is None and target_quantity is None:
        basis = "NOT_SPECIFIED"
    if (
        quantity is not None
        and held_quantity is not None
        and quantity > held_quantity + 1e-9
    ):
        errors.append(
            f"검토 수량 {quantity}이 분석 기준 보유수량 {held_quantity}을 넘는다"
        )
        quantity = None
    block: dict[str, Any] = {
        "side": "SELL",
        "held_quantity": held_quantity,
        "review_quantity": quantity,
        "review_quantity_basis": basis,
        "_role": (
            "사용자가 검토 중인 매도 수량의 산술이다. 얼마를 팔지는 모델이 "
            "정하며 여기 있는 수량은 제안이 아니라 사용자가 말한 값이다. "
            "review_quantity 가 null 이면 사용자가 수량을 정하지 않은 것이고, "
            "PC 는 대신 정하지 않는다. 이 packet 에는 매수 예산이 없다"
        ),
        "_warning": "수수료·세금·슬리피지는 대금과 잔여 비중에서 제외했다",
    }
    if errors:
        block["contract_errors"] = errors
    if quantity is None or held_quantity is None:
        return block
    remaining = held_quantity - quantity
    proceeds_asset = quantity * price
    proceeds_krw = (
        proceeds_asset * fx if market == "US" else proceeds_asset
    ) if (market != "US" or fx is not None) else None
    remaining_value_asset = remaining * price
    remaining_value_krw = (
        remaining_value_asset * fx if market == "US" else remaining_value_asset
    ) if (market != "US" or fx is not None) else None
    block["remaining_quantity"] = round(remaining, 6)
    block["estimated_proceeds_krw"] = (
        round(proceeds_krw, 2) if proceeds_krw is not None else None
    )
    block["exposure_after_krw"] = (
        round(remaining_value_krw, 2) if remaining_value_krw is not None else None
    )
    block["post_trade_weight_total_assets_pct"] = (
        round(remaining_value_krw / total_at_review * 100, 2)
        if remaining_value_krw is not None and total_at_review is not None and total_at_review > 0
        else None
    )
    block["cash_after_krw"] = (
        round(cash + proceeds_krw, 2)
        if cash is not None and proceeds_krw is not None else None
    )
    return block


def _user_position_summary(
    asset: dict[str, Any], *, quantity: float, average_cost: float | None,
    price: float, review_quantity: float | None, target_quantity: float | None,
    local_portfolio: dict[str, Any],
    average_cost_currency: str | None = None,
) -> dict[str, Any]:
    """이번 질문의 보유 입력만 계산한다. 거래·계좌 잔액을 추정하지 않는다."""
    cost_currency = average_cost_currency or asset["currency"]
    fx, fx_as_of = _fx_rate() if "USD" in (asset["currency"], cost_currency) else (None, None)
    values = position_value_by_currency(
        quantity=quantity, price=price, asset_currency=asset["currency"],
        average_cost=average_cost, cost_currency=cost_currency,
        fx_krw_per_usd=fx, fx_as_of=fx_as_of,
    )
    fx = values["fx_krw_per_usd"]
    local = local_portfolio.get("existing_position") or {}
    local_cost_currency = local.get("average_cost_currency") or asset["currency"]
    costs_comparable = (local_cost_currency == cost_currency
                        and local.get("average_cost") is not None and average_cost is not None)
    return {
        "available": True,
        "scope": "USER_SUPPLIED_POSITION",
        "total_assets_krw": None,
        "cash_krw": None,
        "existing_position": {
            "held": quantity > 0,
            "quantity": quantity,
            **values,
            "authority": "USER_INPUT",
            "verified": False,
            "weight_at_review_price_pct": None,
        },
        "local_position_comparison": {
            "available": local_portfolio.get("available", False),
            "quantity": local.get("quantity"),
            "average_cost": local.get("average_cost"),
            "average_cost_currency": local_cost_currency,
            "as_of": local.get("value_as_of"),
            "quantity_matches": local.get("quantity") == quantity
            if local.get("quantity") is not None else None,
            "average_cost_matches": math.isclose(local["average_cost"], average_cost, abs_tol=0.01)
            if costs_comparable else None,
            "average_cost_comparable": costs_comparable,
        },
        "proposed_trade": _sell_block(
            held_quantity=quantity, review_quantity=review_quantity,
            target_quantity=target_quantity, price=price, fx=fx, cash=None,
            total_at_review=None, market=asset["market"],
        ),
        "_role": "사용자가 이번 질문에 명시한 보유 조건의 산술이며 실제 잔고 확인이 아니다. "
        "이 조건 아래에서만 보유분 행동을 판단한다. 로컬 원장은 변경하지 않는다. "
        "달라진 수량의 거래·자금 출처를 모르므로 총자산·현금·계좌 비중은 계산하지 않는다. "
        "손익은 세금·수수료·슬리피지 전이며 사용자 주장 검증의 독립 근거가 아니다",
    }


def _portfolio_summary(
    asset: dict[str, Any],
    *,
    amount: float | None,
    amount_currency: str,
    price: float,
    intent: str | None = None,
    review_quantity: float | None = None,
    target_quantity: float | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    path = KNOWLEDGE_DIR / "portfolio" / "holdings.csv"
    if not path.exists():
        return {"available": False}, None
    holdings_as_of = _portfolio_as_of(path)
    source = {
        "source_id": "PORTFOLIO:HOLDINGS",
        "kind": "USER_DATA",
        "path": str(path.relative_to(ROOT)),
        "data_as_of": holdings_as_of,
    }
    try:
        rows = _csv_rows(path)
    except OSError:
        return {
            "available": False,
            "contract_errors": ["포트폴리오 파일을 읽을 수 없다"],
        }, source
    parsed_values = [_as_float(row.get("value_krw")) for row in rows]
    if (
        not rows
        or any(value is None or value < 0 for value in parsed_values)
    ):
        return {
            "available": False,
            "contract_errors": ["포트폴리오 value_krw가 유효하지 않다"],
        }, source
    values = [value for value in parsed_values if value is not None]
    total = sum(values)
    cash = sum(
        _as_float(row.get("value_krw")) or 0
        for row in rows
        if row.get("value_krw") and row.get("risk_class") == "CASH"
    )
    held = next(
        (row for row in rows if row.get("asset_id") == asset["asset_id"]), None
    )
    fx, fx_as_of = _fx_rate() if asset["market"] == "US" else (1.0, None)
    family = _intent_family(
        intent,
        amount=amount,
        review_quantity=review_quantity,
        target_quantity=target_quantity,
    )
    budget_krw: float | None
    budget_asset_currency: float | None
    if amount is None:
        budget_krw = None
        budget_asset_currency = None
    elif amount_currency == "KRW":
        budget_krw = amount
        budget_asset_currency = (
            amount / fx if asset["market"] == "US" and fx else amount
        )
    elif amount_currency == asset["currency"]:
        budget_asset_currency = amount
        budget_krw = amount * fx if fx else None
    else:
        budget_krw = None
        budget_asset_currency = None
    quantity = None
    deployed_asset_currency = None
    if budget_asset_currency is not None and price > 0:
        raw_quantity = budget_asset_currency / price
        quantity = (
            math.floor(raw_quantity)
            if asset["market"] == "KR"
            else raw_quantity
        )
        deployed_asset_currency = quantity * price
    deployed_krw = (
        deployed_asset_currency * fx
        if deployed_asset_currency is not None and fx is not None
        else None
    )
    existing = (
        _as_float(held.get("value_krw")) or 0.0 if held else 0.0
    )
    # 보유 현황은 검토 가격 기준으로 다시 계산한다. CSV 의 value_krw 는 스냅샷
    # 시점 값이라 오늘 비중을 그대로 쓰면 틀린다. 평단은 원가(평가액 - 평가손익)를
    # 수량으로 나눠 복원한다 — 매도 판단에는 평단과 현재 비중이 반드시 필요하다.
    held_quantity = _as_float(held.get("quantity")) if held else None
    held_pnl_krw = _as_float(held.get("pnl_krw")) if held else None
    cost_basis_krw = (
        existing - held_pnl_krw if held_pnl_krw is not None else None
    )
    average_cost = (
        cost_basis_krw / held_quantity
        if cost_basis_krw is not None
        and held_quantity
        and held_quantity > 0
        else None
    )
    # The snapshot only supplies a KRW acquisition basis. Dividing it by today's
    # FX rate would invent a historical USD average cost.
    value_at_review_krw = None
    if held_quantity is not None and price > 0:
        in_asset_currency = held_quantity * price
        value_at_review_krw = (
            in_asset_currency * fx
            if asset["market"] == "US" and fx is not None
            else in_asset_currency
            if asset["market"] != "US"
            else None
        )
    # 다른 보유 종목은 스냅샷 그대로 두고 이 종목만 검토 가격으로 갈아끼운다.
    total_at_review = (
        total - existing + value_at_review_krw
        if value_at_review_krw is not None
        else total
    )
    unrealized_krw = (
        value_at_review_krw - cost_basis_krw
        if value_at_review_krw is not None and cost_basis_krw is not None
        else None
    )
    sufficient_cash = budget_krw is not None and budget_krw <= cash
    post_total = total
    post_weight = (
        (existing + (deployed_krw or 0)) / post_total * 100
        if post_total > 0 and deployed_krw is not None
        else None
    )
    exposure_after_krw = (
        (value_at_review_krw or 0.0) + deployed_krw
        if deployed_krw is not None
        and (value_at_review_krw is not None or not held)
        else None
    )
    buy_block = {
        "side": "BUY",
        "amount_krw": round(budget_krw, 2) if budget_krw is not None else None,
        "estimated_quantity": round(quantity, 6) if quantity is not None else None,
        "quantity_basis": (
            "INTEGER_SHARES"
            if asset["market"] == "KR"
            else "FRACTIONAL_SHARES_ALLOWED"
        ),
        "estimated_deployed_krw": round(deployed_krw, 2)
        if deployed_krw is not None
        else None,
        "estimated_unspent_budget_krw": round(budget_krw - deployed_krw, 2)
        if budget_krw is not None and deployed_krw is not None
        else None,
        "funding_assumption": "기존 포트폴리오 현금에서 집행",
        "sufficient_cash": sufficient_cash,
        "cash_after_krw": round(cash - deployed_krw, 2)
        if deployed_krw is not None
        else None,
        "exposure_after_krw": round(exposure_after_krw, 2)
        if exposure_after_krw is not None
        else None,
        "post_trade_weight_total_assets_pct": round(post_weight, 2)
        if post_weight is not None
        else None,
        "fx_krw_per_usd": round(fx, 4)
        if asset["market"] == "US" and fx is not None
        else None,
        "fx_as_of": fx_as_of,
        "_warning": "수수료·세금·슬리피지는 수량과 집행액에서 제외했다",
    }
    trade_block = (
        buy_block
        if family == "NEW_CAPITAL"
        else _sell_block(
            held_quantity=held_quantity,
            review_quantity=review_quantity,
            target_quantity=target_quantity,
            price=price,
            fx=fx,
            cash=cash,
            total_at_review=total_at_review,
            market=asset["market"],
        )
    )
    return (
        {
            "available": True,
            "total_assets_krw": round(total, 2),
            "cash_krw": round(cash, 2),
            "existing_position": {
                "held": held is not None,
                "value_krw": round(existing, 2),
                "value_as_of": holdings_as_of,
                "quantity": held_quantity,
                "average_cost": round(average_cost, 4)
                if average_cost is not None
                else None,
                "average_cost_currency": "KRW",
                "average_cost_asset_currency": round(average_cost, 4)
                if average_cost is not None and asset["currency"] == "KRW" else None,
                "cost_basis_krw": round(cost_basis_krw, 2)
                if cost_basis_krw is not None
                else None,
                "value_at_review_price_krw": round(value_at_review_krw, 2)
                if value_at_review_krw is not None
                else None,
                "unrealized_pnl_krw": round(unrealized_krw, 2)
                if unrealized_krw is not None
                else None,
                "unrealized_pnl_pct": round(
                    (value_at_review_krw / cost_basis_krw - 1) * 100, 2
                )
                if value_at_review_krw is not None
                and cost_basis_krw
                and cost_basis_krw > 0
                else None,
                "unrealized_pnl_return_currency": "KRW",
                "fx_krw_per_usd": fx if asset["market"] == "US" else None,
                "fx_as_of": fx_as_of,
                "_currency_note": "스냅샷 평단과 취득원가는 원화 기준이다. 과거 환율이 "
                "없으므로 미국 자산의 달러 취득평단을 현재 환율로 역산하지 않는다",
                "weight_at_review_price_pct": round(
                    value_at_review_krw / total_at_review * 100, 2
                )
                if value_at_review_krw is not None and total_at_review > 0
                else None,
                "_role": (
                    "검토 가격 기준으로 다시 계산한 현재 보유 상태다. 비중은 "
                    "노출 측정치이며 그 자체로 매도 사유가 아니다 — 매도는 "
                    "논지·밸류에이션이 주도하고, 비중은 명시된 한도가 있을 때만 "
                    "제약으로 작동한다"
                ),
            },
            "proposed_trade": trade_block,
            "_role": "제약과 노출 계산일 뿐 투자 매력도 판정이 아니다",
        },
        source,
    )


def _clip(text: Any, limit: int) -> str | None:
    """긴 서술을 자르되 잘렸다는 사실과 원문 위치를 남긴다."""
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if len(stripped) <= limit:
        return stripped
    return (
        stripped[:limit].rstrip()
        + f"…〔{len(stripped)}자 중 {limit}자. 전문은 assessment 파일에 있다〕"
    )


def _pace_since_decision(
    *,
    decided_at: Any,
    asked_price: Any,
    horizon_months: Any,
    win_threshold_pct: Any,
    latest_close: float | None,
    close_date: str | None,
    close_is_settled: bool | None,
    target_annual_return_pct: Any,
    return_currency: str | None = None,
    objective_currency: str | None = None,
    split_factor: float | None = None,
) -> dict[str, Any] | None:
    """수익이 **얼마나 빨리** 왔는지 잰다.

    가격 계획은 문턱만 있고 시간이 없었다. 그래서 판단 이틀 뒤 +10%가 나도
    packet 에는 그 사실을 담을 자리가 없었다 — 문턱은 아무것도 켜지지 않고,
    모델은 아무 일도 없었다고 읽는다. 24개월치 승리의 40%를 이틀에 받은 것과
    24개월에 걸쳐 받은 것은 완전히 다른 사건인데 구분되지 않았다.

    연율 환산은 쓰지 않는다. 2거래일 +10%를 연율화하면 천문학적 숫자가 나와
    아무 의미가 없다. 대신 두 개의 비율을 낸다 — **승리 문턱까지의 진도**를
    **평가 기간의 경과**로 나눈 값, 그리고 수익 통화가 같을 때만 실현 수익을
    사용자가 정한 연 목표수익률이 같은 기간에 냈을 값으로 나눈 값이다.

    문턱이 아니다. 몇 배부터 팔아야 하는지 여기서 정하지 않으며 그 판단은
    전부 모델이 한다. 이 블록은 지금까지 packet 에 존재하지 않던 사실 하나를
    측정해 놓을 뿐이다.
    """
    if latest_close is None or not isinstance(latest_close, (int, float)):
        return None
    if not math.isfinite(latest_close) or latest_close <= 0:
        return None
    if not isinstance(asked_price, (int, float)) or asked_price <= 0:
        return None
    decided = _date_of(decided_at)
    observed = _date_of(close_date)
    if decided is None or observed is None:
        return None
    elapsed_days = (observed - decided).days
    if elapsed_days < 0:
        return None
    horizon_days = (
        round(float(horizon_months) * 365 / 12)
        if isinstance(horizon_months, (int, float)) and horizon_months > 0
        else None
    )
    # 질문가는 그날의 호가이고 종가는 오늘 기준이다. 그 사이 분할이 있었으면
    # 계수를 곱해야 같은 것을 잰다 — 안 그러면 2:1 분할 하나로 실현 수익이
    # -50% 로 찍히고, 그 값이 "논지보다 가격이 뒤처졌다"는 근거가 된다.
    compared_price = float(asked_price) * _usable_factor(split_factor)
    realized_pct = (latest_close / compared_price - 1) * 100

    elapsed_pct = (
        round(elapsed_days / horizon_days * 100, 4) if horizon_days else None
    )
    progress_pct = (
        round(realized_pct / float(win_threshold_pct) * 100, 4)
        if isinstance(win_threshold_pct, (int, float)) and win_threshold_pct > 0
        else None
    )
    # 경과가 0일이면 나눌 수 없다. 판단 당일에 묻는 경우이며, 그때는 속도라는
    # 개념 자체가 성립하지 않는다.
    pace_x = (
        round(progress_pct / elapsed_pct, 2)
        if progress_pct is not None and elapsed_pct not in (None, 0)
        else None
    )

    objective_pct = None
    objective_x = None
    objective_comparison_state = "NO_POSITIVE_OBJECTIVE"
    if (
        isinstance(target_annual_return_pct, (int, float))
        and target_annual_return_pct > -100
        and elapsed_days > 0
    ):
        objective_pct = round(
            ((1 + target_annual_return_pct / 100) ** (elapsed_days / 365) - 1)
            * 100,
            4,
        )
        if objective_pct > 0:
            if return_currency not in {"KRW", "USD"} or objective_currency not in {"KRW", "USD"}:
                objective_comparison_state = "CURRENCY_UNKNOWN"
            elif return_currency != objective_currency:
                objective_comparison_state = "CURRENCY_MISMATCH"
            else:
                objective_x = round(realized_pct / objective_pct, 2)
                objective_comparison_state = "COMPARABLE"

    result: dict[str, Any] = {
        "decided_on": decided.isoformat(),
        "observed_on": observed.isoformat(),
        "close_is_settled": close_is_settled,
        "asked_price": asked_price,
        "latest_close": latest_close,
        "elapsed_days": elapsed_days,
        "horizon_days": horizon_days,
        "elapsed_pct_of_horizon": elapsed_pct,
        "return_since_decision_pct": round(realized_pct, 4),
        "win_return_threshold_pct": win_threshold_pct,
        "progress_to_win_threshold_pct": progress_pct,
        "pace_progress_over_elapsed_x": pace_x,
        "objective_annual_return_pct": target_annual_return_pct,
        "objective_return_to_date_pct": objective_pct,
        "realized_over_objective_x": objective_x,
        "return_currency": return_currency,
        "objective_currency": objective_currency,
        "objective_comparison_state": objective_comparison_state,
        "_objective_comparison_note": (
            "자산 통화의 가격 수익률과 정책 기본통화의 목표수익률이 달라 "
            "기준일별 환율 없이 비교하지 않는다"
            if objective_comparison_state == "CURRENCY_MISMATCH" else
            "수익률 또는 목표수익률의 통화를 확인할 수 없어 비교하지 않는다"
            if objective_comparison_state == "CURRENCY_UNKNOWN" else None
        ),
        "_note": (
            "판단 시점의 검토 가격 대비 실현 수익과, 그것이 평가 기간의 어느 "
            "지점에서 나왔는지다. pace_progress_over_elapsed_x 가 1보다 크면 "
            "예정보다 빠르게 왔다는 뜻이고, realized_over_objective_x 는 같은 "
            "기간에 사용자의 목표 연수익률이 냈을 값의 몇 배인지다. 배당·세금·"
            "수수료는 들어 있지 않다"
        ),
        "_not_a_judgment": (
            "빠르게 왔다는 것은 팔라는 뜻도 더 사라는 뜻도 아니다. 값이 크면 "
            "가격이 논지보다 앞서 갔을 수 있고, 그것이 사실인지는 이번 판단이 "
            "정한다. 몇 배부터 무엇을 하라는 문턱을 PC 는 갖고 있지 않다"
        ),
    }
    if compared_price != float(asked_price):
        result["split_adjustment"] = {
            "factor": round(_usable_factor(split_factor), 10),
            "compared_price": round(compared_price, 6),
            "_warning": (
                "판단 이후 분할이 있었다. asked_price 는 그날의 호가이고 "
                "수익률은 compared_price 로 계산했다"
            ),
        }
    return result


def _date_of(value: Any) -> dt.date | None:
    """ISO 날짜 또는 일시에서 날짜만 꺼낸다."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        return dt.date.fromisoformat(text[:10])
    except ValueError:
        return None


def _prior_plan_check(
    plan: Any,
    *,
    latest_close: float | None,
    close_date: str | None,
    close_is_settled: bool | None,
    bars: list[tuple[str, float, float]] | None = None,
    split_factor: float | None = None,
) -> dict[str, Any] | None:
    """직전 가격계획의 각 문턱을 현재 종가와 기계적으로 대조한다.

    판단이 아니라 산술이다. 모델이 자기가 세운 손절·재검토 가격이 이미
    닿았는지를 매번 다시 계산하지 않게 하는 것이 목적이다.

    `split_factor` 는 판단 이후의 분할계수다(`_split_factor_since`). 계획의
    문턱은 판단하던 날의 호가인데 종가는 오늘 기준이라, 그 사이 분할이
    있었으면 두 수는 같은 것을 재지 않는다. 문턱 쪽에 계수를 곱해 맞추고
    **맞췄다는 사실을 같이 싣는다** — 화면에 남는 값은 모델이 그때 쓴 원래
    문턱이어야 하므로, 대조에 쓴 값은 따로 적는다.

    `bars` 는 판단일 이후의 (날짜, 고가, 저가) 다. 종가만 보면 장중에 스친
    문턱이 통째로 사라진다 — 2026-08-07 파마리서치가 그랬다. 직전 계획의
    재검토 하단이 340,000원이었고 그날 저가가 333,000원이었는데, 종가
    378,500원만 대조하니 닿지 않은 것으로 남았다.

    **스친 것과 종가로 뚫린 것은 다른 사실이라 칸을 나눈다.** `triggered` 는
    종가 기준 그대로 두고 장중 관측은 `intraday` 아래에만 쌓는다. 둘을 합치면
    "종가가 손절 아래에서 끝났다"와 "장중에 한 번 스쳤다"가 구분되지 않고,
    그 구분이 사라지면 다음 판단이 스친 것만으로 매도를 정산하게 된다.
    """
    if not isinstance(plan, dict) or latest_close is None:
        return None
    if not math.isfinite(latest_close) or latest_close <= 0:
        return None

    def level(name: str) -> float | None:
        value = plan.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value) if math.isfinite(value) else None

    factor = _usable_factor(split_factor)

    def comparable(value: float | None) -> float | None:
        """문턱을 저장된 종가와 같은 기준으로 옮긴다."""
        return None if value is None else value * factor

    stop = level("stop_price")
    reask_below = level("reask_below")
    reask_above = level("reask_above")
    scale_out = level("scale_out_start")
    entry_low = level("entry_range_low")
    entry_high = level("entry_range_high")
    preferred = level("preferred_entry")
    stop_c = comparable(stop)
    reask_below_c = comparable(reask_below)
    reask_above_c = comparable(reask_above)
    scale_out_c = comparable(scale_out)
    entry_low_c = comparable(entry_low)
    entry_high_c = comparable(entry_high)
    preferred_c = comparable(preferred)
    triggered: list[str] = []
    if stop_c is not None and latest_close <= stop_c:
        triggered.append("stop_price")
    if reask_below_c is not None and latest_close <= reask_below_c:
        triggered.append("reask_below")
    if reask_above_c is not None and latest_close >= reask_above_c:
        triggered.append("reask_above")
    if scale_out_c is not None and latest_close >= scale_out_c:
        triggered.append("scale_out_start")
    result: dict[str, Any] = {
        "intraday": _intraday_touch(
            bars,
            stop=stop_c,
            reask_below=reask_below_c,
            reask_above=reask_above_c,
            scale_out=scale_out_c,
            entry_low=entry_low_c,
            entry_high=entry_high_c,
        ),
        "close": latest_close,
        "close_date": close_date,
        "close_is_settled": close_is_settled,
        "stop_price": stop,
        "stop_breached": None if stop_c is None else latest_close <= stop_c,
        "reask_below": reask_below,
        "reask_below_reached": (
            None if reask_below_c is None else latest_close <= reask_below_c
        ),
        "reask_above": reask_above,
        "reask_above_reached": (
            None if reask_above_c is None else latest_close >= reask_above_c
        ),
        "scale_out_start": scale_out,
        "scale_out_reached": (
            None if scale_out_c is None else latest_close >= scale_out_c
        ),
        "in_entry_range": (
            None
            if entry_low_c is None or entry_high_c is None
            else entry_low_c <= latest_close <= entry_high_c
        ),
        "close_vs_preferred_entry_pct": (
            None
            if preferred_c is None or preferred_c <= 0
            else round((latest_close / preferred_c - 1) * 100, 2)
        ),
        "triggered": triggered,
        "_note": (
            "종가와 문턱의 산술 비교다. 장중에 스쳤는지는 intraday 에 따로 "
            "있으며 둘은 같은 사실이 아니다. 문턱에 닿았다는 사실이 그때의 "
            "논지가 여전히 옳다는 뜻도 아니다 — 왜 닿았는지는 모델이 판단한다"
        ),
    }
    # 분할이 없으면 아무것도 덧붙이지 않는다. 매 판단마다 나가는 자리라
    # 정상일 때의 한 줄이 비용이다.
    if factor != 1.0:
        result["split_adjustment"] = {
            "factor": round(factor, 10),
            "compared_levels": {
                name: round(value, 6)
                for name, value in (
                    ("stop_price", stop_c),
                    ("reask_below", reask_below_c),
                    ("reask_above", reask_above_c),
                    ("scale_out_start", scale_out_c),
                    ("entry_range_low", entry_low_c),
                    ("entry_range_high", entry_high_c),
                    ("preferred_entry", preferred_c),
                )
                if value is not None
            },
            "_warning": (
                "판단 이후 분할이 있었다. 위에 적힌 문턱은 **판단하던 날의 "
                "호가**이고 종가는 오늘 기준이라 그대로 비교되지 않는다. "
                "대조에는 compared_levels 의 값을 썼다 — 인용할 때 어느 쪽 "
                "기준인지 밝히지 않으면 다음 판단이 두 기준을 섞는다"
            ),
        }
    return result


def _intraday_touch(
    bars: list[tuple[str, float, float]] | None,
    *,
    stop: float | None,
    reask_below: float | None,
    reask_above: float | None,
    scale_out: float | None,
    entry_low: float | None,
    entry_high: float | None,
) -> dict[str, Any] | None:
    """판단일 이후의 고가·저가가 각 문턱을 스쳤는지.

    **닿은 날은 가장 이른 날을 적는다.** 마지막 날을 적으면 "언제부터 알 수
    있었나"가 사라지고, 그것이 이 블록의 존재 이유다.

    봉이 없으면 빈 값이 아니라 `None` 이다. false 로 채우면 관측하지 않은 것이
    관측된 음성으로 둔갑한다 — packet 에서 그 둘은 절대 같은 값일 수 없다.
    """
    usable = [
        (str(date), float(high), float(low))
        for date, high, low in (bars or [])
        if isinstance(high, (int, float))
        and isinstance(low, (int, float))
        and not isinstance(high, bool)
        and not isinstance(low, bool)
        and math.isfinite(float(high))
        and math.isfinite(float(low))
        and float(low) > 0
    ]
    if not usable:
        return None
    usable.sort(key=lambda row: row[0])

    def first_day(hit: Any) -> str | None:
        for date, high, low in usable:
            if hit(high, low):
                return date
        return None

    checks: list[tuple[str, str, str | None]] = [
        (
            "stop_price",
            "stop_touched_on",
            None if stop is None else first_day(lambda h, l: l <= stop),
        ),
        (
            "reask_below",
            "reask_below_touched_on",
            None
            if reask_below is None
            else first_day(lambda h, l: l <= reask_below),
        ),
        (
            "reask_above",
            "reask_above_touched_on",
            None
            if reask_above is None
            else first_day(lambda h, l: h >= reask_above),
        ),
        (
            "scale_out_start",
            "scale_out_touched_on",
            None
            if scale_out is None
            else first_day(lambda h, l: h >= scale_out),
        ),
        (
            # 봉의 [저가, 고가] 구간이 진입 범위와 겹치면 그 가격에 살 수 있었다.
            "entry_range",
            "entry_range_touched_on",
            None
            if entry_low is None or entry_high is None
            else first_day(lambda h, l: l <= entry_high and h >= entry_low),
        ),
    ]
    result: dict[str, Any] = {
        "from": usable[0][0],
        "to": usable[-1][0],
        "sessions": len(usable),
        "high": max(high for _, high, _ in usable),
        "low": min(low for _, _, low in usable),
        "touched": [name for name, _, day in checks if day is not None],
    }
    for _, key, day in checks:
        result[key] = day
    result["_note"] = (
        "판단일 이후 일봉의 고가·저가가 각 문턱을 스쳤는지다. **스친 것은 "
        "종가로 뚫린 것과 다르다** — 종가 기준 판정은 위의 triggered 에 있다. "
        "장중에 스쳤다는 것은 그날 그 가격에 거래가 가능했다는 사실일 뿐, "
        "그 가격에 체결됐다는 뜻도 그때 행동했어야 했다는 뜻도 아니다. "
        "봉 안에서의 순서는 일봉이 담지 않으므로 같은 날 두 문턱을 스쳤다면 "
        "어느 쪽이 먼저인지는 여기서 알 수 없다"
    )
    return result


def _compact_size(value: Any) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )


# context_id는 예산을 잰 뒤에 채워지므로 그 자리만큼 미리 비워둔다.
CONTEXT_ID_RESERVE_BYTES = 64


def _fit_context_budget(packet: dict[str, Any], *, seal_plans: bool = False) -> None:
    """예산을 넘으면 이어받은 서술부터 단계적으로 버린다.

    직전 논지가 길다는 이유로 packet 자체를 만들지 못하면 판단이 멈춘다.
    가격계획·무효화 신호·문턱 대조는 결정에 직접 쓰이므로 마지막까지 남기고,
    서사부터 버린다. 무엇을 버렸는지는 packet 안에 남긴다.

    기록 자체도 packet을 키우므로 기록을 먼저 넣고 그 위에서 잰다.
    """
    limit = MAX_CONTEXT_JSON_BYTES - CONTEXT_ID_RESERVE_BYTES

    def fits() -> bool:
        return _compact_size(packet) <= limit

    if fits():
        return
    trimmed: list[str] = []
    packet["validation"]["optional_gaps"].append(
        {
            "task": "context_packet",
            "state": "TRIMMED_FOR_BUDGET",
            "trimmed": trimmed,
            "reason": (
                "packet 예산을 넘어 아래 항목을 잘라냈다. 원문은 "
                "knowledge/assessments/ 에 그대로 있고 미해결 요청 전체는 "
                "`python3.13 -m cio requests <asset_id>`로 볼 수 있다"
            ),
        }
    )
    # 사다리부터 버린다. 추가 수량별 평단·비중은 CLI 로 언제든 다시 낼 수 있는
    # 편의값이고, 무효화 신호와 가격 문턱은 이번 판단에서 바로 대조해야 한다.
    portfolio = packet["context"].get("portfolio")
    if isinstance(portfolio, dict) and portfolio.get("add_ladder") is not None:
        portfolio["add_ladder"] = None
        trimmed.append("portfolio.add_ladder")
        if fits():
            return
    digest = packet["context"].get("prior_assessments") or {}
    rows = (digest.get("live") or []) if isinstance(digest, dict) else digest
    carried = next(
        (
            row["carried_forward"]
            for row in rows
            if isinstance(row, dict) and row.get("carried_forward")
        ),
        {},
    )
    # 서사부터 버린다 — 가격 문턱과 무효화 신호는 이번 판단에서 바로 대조해야
    # 하지만, 지난번 논지의 문장은 필요하면 원문을 열어보면 된다.
    for field in ("thesis", "strongest_counter_thesis", "assumption_review"):
        if carried.get(field) is None:
            continue
        carried[field] = None
        trimmed.append(f"prior_assessments.carried_forward.{field}")
        if fits():
            return
    if carried.get("entry_plan") and not fits():
        # Keep the full plan by a content-addressed reference if it cannot fit.
        identifier = next((r.get("assessment_id") for r in rows if r.get("carried_forward") is carried), None)
        plan_path = KNOWLEDGE_DIR / "assessments" / packet["request"]["asset_id"] / f"{identifier}.json"
        if identifier and plan_path.is_file():
            if seal_plans:
                plan_path = _snapshot_evidence(plan_path, content_hash(plan_path))
            carried["entry_plan_ref"] = {"assessment_id": identifier, "path": str(plan_path.relative_to(ROOT)), "content_sha256": content_hash(plan_path)}
            carried.pop("entry_plan")
            trimmed.append("prior_assessments.carried_forward.entry_plan (use entry_plan_ref)")
            if fits():
                return
    if carried.get("exit_plan") and not fits():
        identifier = next((r.get("assessment_id") for r in rows if r.get("carried_forward") is carried), None)
        plan_path = KNOWLEDGE_DIR / "assessments" / packet["request"]["asset_id"] / f"{identifier}.json"
        if identifier and plan_path.is_file():
            if seal_plans:
                plan_path = _snapshot_evidence(plan_path, content_hash(plan_path))
            carried["exit_plan_ref"] = {"assessment_id": identifier, "path": str(plan_path.relative_to(ROOT)), "content_sha256": content_hash(plan_path)}
            carried.pop("exit_plan")
            trimmed.append("prior_assessments.carried_forward.exit_plan (use exit_plan_ref)")
            if fits():
                return
    open_requests = packet["context"].get("open_research_requests") or []
    while len(open_requests) > 2 and not fits():
        open_requests.pop(-2)
        if "open_research_requests" not in trimmed:
            trimmed.append("open_research_requests")
    if fits():
        return
    if carried.get("invalidation_signals") is not None:
        carried["invalidation_signals"] = None
        trimmed.append("prior_assessments.carried_forward.invalidation_signals")
        # 여기까지 왔다는 것은 **직전 판단이 무엇을 보면 틀린 것인지**를 버렸다는
        # 뜻이다. 사용자의 stop_style 이 THESIS 면 무효화 신호가 유일한 매도
        # 규율이므로, 그것을 버린 packet 은 '조금 덜 실린 packet' 이 아니라
        # 매도 판단의 축이 빠진 packet 이다.
        #
        # 경고를 optional_gaps 에만 남기면 다른 잘라낸 항목들 사이에 묻힌다.
        # 구멍이 난 자리에 직접 표시한다 — 모델은 carried_forward 를 읽을 때
        # 그 자리에서 없어진 이유를 본다.
        #
        # validation.problems 로 올려 BLOCKED 를 만들지는 않는다. BLOCKED 는
        # 모든 판단을 INSUFFICIENT 로 막는데, 새 자금에 대한 verdict 는 직전
        # 무효화 신호 없이도 낼 수 있다. 무엇을 못 하게 되는지는 아래 문장이
        # 지정하고, 그 판단은 모델이 한다.
        carried["invalidation_signals_dropped"] = (
            "packet 예산 때문에 직전 판단의 무효화 신호를 버렸다. 그것 없이는 "
            "'무엇이 깨졌는지'를 대조할 수 없으므로, 보유분에 대해 TRIM·EXIT·"
            "HOLD 를 그대로 내지 말고 (1) 이번에 무효화 신호를 새로 세우거나 "
            "(2) action.direction=UNDETERMINED / undetermined_reason="
            "NO_POSITION_DATA 로 답한다. 원문은 knowledge/assessments/ 에 있다"
        )


def _assessment_market_date(created_at: str, market: str) -> str:
    zone = ZoneInfo("Asia/Seoul" if market == "KR" else "America/New_York")
    return dt.datetime.fromisoformat(created_at).astimezone(zone).date().isoformat()


def _memory_digest(
    asset_id: str,
    *,
    latest_close: float | None = None,
    close_date: str | None = None,
    close_is_settled: bool | None = None,
    target_annual_return_pct: Any = None,
    return_currency: str | None = None,
    objective_currency: str | None = None,
    daily_ranges: list[tuple[str, float, float, float]] | None = None,
) -> dict[str, Any]:
    directory = KNOWLEDGE_DIR / "assessments" / asset_id
    assessments = []
    # 계약이 바뀌면 과거 판단이 재검증에 실패한다. 그때 조용히 버리면 모델은
    # "이 종목은 처음 본다"고 착각한다. 실을 수 없다는 사실 자체를 싣는다.
    #
    # verdict 도 가격도 싣지 않는다. 실을 수 없다고 써 놓고 인용할 수 있는 값을
    # 함께 보내면 그 경고는 지켜지지 않는다 — 실제로 그렇게 해서 죽은 판단의
    # 손절가가 근거로 쓰였다. 존재와 사유만 남기고 내용은 뺀다.
    unusable: list[dict[str, Any]] = []

    def _unusable(path: Path, data: Any, reason: str) -> None:
        record = data if isinstance(data, dict) else {}
        unusable.append(
            {
                "assessment_id": record.get("assessment_id") or path.stem,
                "created_at": record.get("created_at"),
                "reason": reason,
            }
        )

    for path in (
        sorted(directory.glob("CIO-*.json")) if directory.exists() else []
    ):
        data: Any = None
        try:
            data = load_json(path)
            context_id = str(data.get("context_id") or "")
            context_path = (
                KNOWLEDGE_DIR / "contexts" / asset_id / f"{context_id}.json"
            )
            context = load_json(context_path)
        except (ContractError, OSError) as exc:
            _unusable(path, data, f"판단 또는 그 context를 읽을 수 없다: {exc}")
            continue
        integrity_errors = validate_context_integrity(
            context, expected_id=context_id
        )
        if integrity_errors:
            _unusable(
                path,
                data,
                "당시 context의 무결성 검사가 실패한다: "
                + "; ".join(integrity_errors[:2]),
            )
            continue
        try:
            normalized = require_valid_decision(data, context=context)
        except ContractError as exc:
            _unusable(
                path,
                data,
                "현재 판단 계약으로 재검증되지 않는다: "
                + "; ".join(exc.errors[:2]),
            )
            continue
        if normalized != data or data.get("asset_id") != asset_id:
            _unusable(
                path,
                data,
                "저장된 판단이 정규화 결과나 자산 ID와 일치하지 않는다",
            )
            continue
        assessments.append((data, context))
    assessments.sort(
        key=lambda item: str(item[0].get("created_at") or "")
    )

    valid_by_id = {
        data["assessment_id"]: (data, context)
        for data, context in assessments
    }
    evaluations: dict[str, dict[str, Any]] = {}
    evaluation_dir = KNOWLEDGE_DIR / "evaluations" / asset_id
    for path in evaluation_dir.glob("EVAL-*.json") if evaluation_dir.exists() else []:
        try:
            data = load_json(path)
        except (ContractError, OSError):
            continue
        assessment_pair = valid_by_id.get(data.get("assessment_id"))
        if not assessment_pair:
            continue
        assessment, _ = assessment_pair
        evaluation_id = data.get("evaluation_id")
        try:
            expected_id = stable_hash(
                {
                    key: value
                    for key, value in data.items()
                    if key not in EVALUATION_ID_EXCLUDED_KEYS
                },
                "EVAL-",
            )
        except (TypeError, ValueError):
            continue
        if (
            evaluation_id != expected_id
            or path.stem != evaluation_id
            or data.get("schema_version") not in READABLE_EVALUATION_SCHEMAS
            or data.get("asset_id") != asset_id
            or data.get("context_id") != assessment.get("context_id")
            or data.get("model") != assessment.get("model")
            or data.get("verdict") != assessment.get("verdict")
            or data.get("outcome_metric") != assessment.get("outcome_metric")
        ):
            continue
        previous = evaluations.get(data["assessment_id"])
        if not previous or str(data.get("evaluated_at") or "") > str(
            previous.get("evaluated_at") or ""
        ):
            evaluations[data["assessment_id"]] = data

    selected = assessments[-MAX_LIVE_ASSESSMENTS:]
    result = []
    for index, (data, context) in enumerate(selected):
        request = context.get("request") or {}
        evaluation = evaluations.get(data.get("assessment_id"))
        row = {
            "assessment_id": data.get("assessment_id"),
            "created_at": data.get("created_at"),
            "model": data.get("model"),
            "verdict": data.get("verdict"),
            "action": (data.get("action") or {}).get("direction"),
            "win_probability_pct": data.get("win_probability_pct"),
            "horizon_months": data.get("horizon_months"),
            "asked_price": request.get("price"),
            "latest_evaluation": {
                "complete": (evaluation.get("window") or {}).get("complete"),
                "latest_available_date": (
                    evaluation.get("window") or {}
                ).get("latest_available_date"),
                "return_to_end_pct": (evaluation.get("price") or {}).get(
                    "return_to_end_pct"
                ),
                "win_observed": (evaluation.get("thresholds") or {}).get(
                    "win_observed"
                ),
                "brier_score": (evaluation.get("calibration") or {}).get(
                    "brier_score"
                ),
            }
            if evaluation
            else None,
        }
        # 가장 최근 판단만 논지까지 싣는다. 세 건을 모두 실으면 packet 예산을
        # 넘고, 모델이 확인해야 하는 것은 "지금 살아 있는 계획"이다.
        if index == len(selected) - 1:
            if evaluation and evaluation.get("entry_plan") and row.get("latest_evaluation"):
                row["latest_evaluation"]["entry_plan"] = evaluation["entry_plan"]
            if evaluation and evaluation.get("exit_plan") and row.get("latest_evaluation"):
                row["latest_evaluation"]["exit_plan"] = evaluation["exit_plan"]
            plan = data.get("price_plan")
            counter = data.get("strongest_counter_thesis") or {}
            signals = data.get("invalidation_signals")
            drivers = data.get("drivers")
            row["carried_forward"] = {
                "action": {
                    **{key: value for key, value in (data.get("action") or {}).items()
                       if key != "rationale"},
                    "rationale": _clip((data.get("action") or {}).get("rationale"), 1600),
                },
                "action_execution": {
                    "status": "UNKNOWN",
                    "staged_sell_plan": "STRUCTURED" if data.get("exit_plan") else "NARRATIVE_ONLY",
                    "_note": (
                        "과거 매도 제안과 미래 단계의 가격 접촉은 실제 체결 내역이 아니다. "
                        "각 단계의 조건·취소 사유·선행 체결 여부를 다시 확인한다"
                        if data.get("exit_plan") else
                        "과거 매도 제안은 체결 내역이 아니다. action의 수량·지정가·"
                        "이유를 함께 검토한다. 이유 안의 미래 분할매도는 단계별 계약으로 "
                        "검증되지 않았다. 잘린 문장이 있으면 이 assessment 원문을 읽는다"
                    ),
                },
                "drivers": (
                    [
                        {
                            "name": driver.get("name"),
                            "weight_pct": driver.get("weight_pct"),
                            "researched": driver.get("researched"),
                            "skipped_reason": _clip(
                                driver.get("skipped_reason"), 160
                            ),
                        }
                        for driver in drivers
                        if isinstance(driver, dict)
                    ]
                    if isinstance(drivers, list)
                    else None
                ),
                "thesis": _clip(data.get("thesis"), 550),
                "strongest_counter_thesis": _clip(counter.get("text"), 400),
                "price_plan": plan if isinstance(plan, dict) else None,
                # action 은 절대 잘라내지 않는다. 서술은 필요하면 원문을 열면
                # 되지만 action 은 **직전 판단이 미리 한 약속**이고, 그것이
                # 빠지면 이번 판단은 약속이 있었다는 사실조차 모른 채 새 가격에서
                # 다시 계산하게 된다. 그 순간 신호는 규율이 아니라 회고가 된다.
                # index 는 이번 판단이 prior_signal_checks 로 되짚을 때 쓰는
                # 결속자다. 배열 순서로 추론하게 두면 모델이 어느 신호를 닫는
                # 중인지 PC 가 확인할 방법이 없고, 그러면 정산이 위조된다.
                "invalidation_signals": (
                    [
                        {
                            "index": position,
                            "signal": _clip(row.get("signal"), 200),
                            "action": row.get("action"),
                            "rationale": _clip(row.get("rationale"), 120),
                        }
                        if isinstance(row, dict)
                        else {
                            "index": position,
                            "signal": _clip(row, 200),
                            "action": None,
                            "rationale": None,
                        }
                        for position, row in enumerate(signals[:6])
                    ]
                    if isinstance(signals, list)
                    else None
                ),
                "_role": (
                    "과거 모델의 판단 기록이며 회고와 개선을 위한 비교 대상이다. "
                    "현재 근거로 판단한 뒤 대조하며 결론·가정·가격선은 언제든 "
                    "버릴 수 있다. 새로운 사실 없이 과거 해석의 오류를 발견해도 "
                    "수정한다. 유지할 때도 현재 근거를 밝힌다. drivers 는 그때의 이익 동인 "
                    "배분이며 researched:false 였던 것은 아직 한 번도 보지 않은 "
                    "동인이다 — 계속 미루고 있는지 확인한다"
                ),
                "_commitment": (
                    "각 신호의 action 은 그 조건이 관측되면 보유분에 무엇을 "
                    "할지 직전 판단이 미리 정해 둔 약속이다. prior_signal_checks "
                    "로 하나씩 닫아야 하며, FIRED 인데 약속과 다르게 행동하려면 "
                    "override_reason 이 필요하다. 과거 기록을 그대로 정산하는 "
                    "것과 현재 그 행동을 따르는 것은 다르다. 변경은 모델이 "
                    "결정하며 이유만 남긴다. 가격 변화도 가치·기대수익 계산에 "
                    "연결되면 판단을 바꾸는 근거가 될 수 있다"
                ),
            }
            if data.get("assumption_review"):
                from cio.assumptions import carried_review
                row["carried_forward"]["assumption_review"] = carried_review(data)
            if data.get("entry_plan"):
                row["carried_forward"]["entry_plan"] = data["entry_plan"]
            if data.get("exit_plan"):
                row["carried_forward"]["exit_plan"] = data["exit_plan"]
            # 판단 시점 호가(문턱·질문가)를 저장된 시계열 기준으로 옮기는
            # 계수. 분할이 없었으면 1.0 이라 아무것도 달라지지 않는다.
            split_factor = _split_factor_since(
                daily_ranges, data.get("created_at")
            )
            row["price_vs_prior_plan"] = _prior_plan_check(
                plan,
                latest_close=latest_close,
                close_date=close_date,
                close_is_settled=close_is_settled,
                bars=_bars_since(daily_ranges, data.get("created_at")),
                split_factor=split_factor,
            )
            row["pace_since_decision"] = _pace_since_decision(
                decided_at=data.get("created_at"),
                asked_price=request.get("price"),
                horizon_months=data.get("horizon_months"),
                win_threshold_pct=data.get("win_return_threshold_pct"),
                latest_close=latest_close,
                close_date=close_date,
                close_is_settled=close_is_settled,
                target_annual_return_pct=target_annual_return_pct,
                return_currency=return_currency,
                objective_currency=objective_currency,
                split_factor=split_factor,
            )
        result.append(row)
    unusable.sort(key=lambda item: str(item.get("created_at") or ""))
    retired = retired_summary(asset_id)
    digest: dict[str, Any] = {
        "live_count": len(result),
        "state": "CARRIED_FORWARD" if result else "NO_LIVE_JUDGMENT",
        "live": result,
    }
    latest_with_plan = next((a for a, _ in reversed(assessments) if a.get("entry_plan")), None)
    if latest_with_plan and (not result or latest_with_plan["assessment_id"] != result[-1]["assessment_id"]):
        plan_path = directory / f"{latest_with_plan['assessment_id']}.json"
        digest["latest_entry_plan"] = {"assessment_id": latest_with_plan["assessment_id"],
            "entry_plan_ref": {"path": str(plan_path.relative_to(ROOT)), "content_sha256": content_hash(plan_path)},
            "_role": "가장 최근의 유효한 분할 제안 기록이다. 이후 판단에 계획이 생략됐다고 잃지 않는다. 현재 실행 지시가 아니며 조건·취소 기록을 재검토한다."}
    latest_exit_plan = next((a for a, _ in reversed(assessments) if a.get("exit_plan")), None)
    if latest_exit_plan and (not result or latest_exit_plan["assessment_id"] != result[-1]["assessment_id"]):
        plan_path = directory / f"{latest_exit_plan['assessment_id']}.json"
        market = get_asset(asset_id)["market"]
        stage_ids = {stage["stage_id"] for stage in latest_exit_plan["exit_plan"]["stages"]}
        stage_reviews: dict[str, dict[str, Any]] = {}
        plan_created = dt.datetime.fromisoformat(latest_exit_plan["created_at"]).astimezone(dt.timezone.utc)
        for review, _ in sorted(assessments, key=lambda pair: dt.datetime.fromisoformat(pair[0]["created_at"]).astimezone(dt.timezone.utc)):
            if dt.datetime.fromisoformat(review["created_at"]).astimezone(dt.timezone.utc) <= plan_created:
                continue
            for check in review.get("exit_plan_checks") or []:
                if check["assessment_id"] != latest_exit_plan["assessment_id"] or check["stage_id"] not in stage_ids:
                    continue
                # Cancellation is terminal for this plan. A later review cannot
                # silently revive a cancelled stage; the model must make a new plan.
                if (stage_reviews.get(check["stage_id"]) or {}).get("state") == "CANCELLED":
                    continue
                stage_reviews[check["stage_id"]] = {
                    "stage_id": check["stage_id"], "state": check["state"],
                    "review_assessment_id": review["assessment_id"],
                    "reviewed_at": review["created_at"],
                    "review_date": _assessment_market_date(review["created_at"], market),
                    "note": _clip(check["note"], 160),
                }
        digest["latest_exit_plan"] = {"assessment_id": latest_exit_plan["assessment_id"],
            "exit_plan_ref": {"path": str(plan_path.relative_to(ROOT)), "content_sha256": content_hash(plan_path)},
            "execution": "UNKNOWN", "status": "HISTORICAL_REFERENCE",
            "stage_reviews": [stage_reviews[stage["stage_id"]] for stage in latest_exit_plan["exit_plan"]["stages"] if stage["stage_id"] in stage_reviews],
            "_role": "가장 최근의 미래 분할매도 제안과 단계별 최종 검토 기록이다. CANCELLED는 이 계획에서 되살릴 수 없고 새 계획이 필요하다. 주문이 살아 있거나 체결됐다는 뜻은 아니다."}
    if unusable:
        digest["not_carried"] = {
            "count": len(unusable),
            "class": "CONTRACT_FAILURE",
            "items": unusable[-3:],
            "_warning": (
                "이 판단들은 파일로 남아 있지만 현재 계약으로 재검증되지 않아 "
                "싣지 못했다. 의도적 폐기가 아니라 계약이 바뀌면서 끊긴 것이다. "
                "verdict·가격계획·논지를 일부러 뺐으니 원본을 열어 인용하지 "
                "않는다. 정리하려면 `cio retire <asset_id>`"
            ),
        }
    if retired.get("count"):
        digest["retired"] = {
            **retired,
            "_note": (
                "사용자가 명시적으로 폐기한 판단이다. 있었다는 사실만 남기고 "
                "내용은 싣지 않는다"
            ),
        }
    digest["_directive"] = (
        "현재 근거로 판단한 뒤 과거의 예측·가정·계획과 대조해 개선점을 찾는다. "
        "과거 결론은 언제든 버릴 수 있다. 유지·변경 모두 현재 근거를 설명한다"
        if result
        else "이 자산에 유효한 판단이 0건이다. 이어받을 논지가 없으므로 "
        "처음부터 판단한다. 아래 목록은 그런 판단이 있었다는 사실일 뿐이며 "
        "그 값을 추측해 채우지 않는다"
    )
    return digest


def _research_requirements(
    asset: dict[str, Any], filings: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    common = [
        {
            "owner": "MODEL",
            "topic": "current_catalysts_and_counterevidence",
            "instruction": "최신 공시 이후 변화와 가장 강한 반대 근거를 직접 조사한다",
        }
    ]
    if asset.get("asset_type") == "STOCK" and any(
        any(word in str(row.get("description", ""))
            for word in ("증자", "권리락", "주식분할", "주식병합", "신주인수권"))
        for row in (filings or {}).get("latest_material_events", [])
    ):
        common.append({
            "owner": "MODEL",
            "topic": "corporate_action_price_and_share_basis",
            "known_local_gap": "재무의 shares_diluted는 과거 EPS 분모이며 현재 발행주식수나 "
                "예정 증자 후 주식수가 아니다. 가격계획·분할매수의 권리 전후 기준과 "
                "청약 자격·추가 납입액은 자동으로 검증되지 않는다",
            "instruction": "최신 정정공시 원문으로 발행·유통·잠재 주식수, 배정·권리락·납입·상장 "
                "일정을 구분한다. 가치평가에는 선택한 미래 주식수를 명시하고, 가격과 수량을 "
                "같은 권리 기준으로 환산한다. 무상증자를 수익으로 세거나 과거 EPS 분모를 "
                "현재 시가총액 분모로 사용하지 않는다",
        })
    lens = asset["analysis_lens"]
    additions = {
        "EPC_BACKLOG": [
            {
                "owner": "MODEL",
                "topic": "backlog_scope_and_conversion",
                "instruction": "발행사 IR의 전체 수주잔고, 사업보고서 주요 프로젝트 잔고, "
                "재무 주석의 잔여 수행의무는 범위가 다르다. 기준일·범위·신규수주와 "
                "매출 전환을 확인하고 입찰 파이프라인을 확정 수주에 합산하지 않는다",
            },
            {
                "owner": "MODEL",
                "topic": "epc_earnings_quality",
                "known_local_gap": "epc_contract_notes가 연결 도급공사 주석의 잔액·누적 "
                "추정 변경 손익·미래 손익을 추출한다. unresolved 항목, 실제 전입액과 "
                "잔액의 구분, 이익 반복성·선수금 사용 제약은 원문 추가 확인이 필요하다",
                "instruction": "최신 연결 도급공사계약 주석에서 총계약수익·원가 추정 변경의 "
                "당기/미래 손익 영향, 미청구·초과청구공사, 선수금과 공사손실충당부채를 "
                "확인한다. 추정 변경 이익 전부를 일회성으로 빼거나 반복 이익으로 "
                "연장하지 말고 지속성을 판단한다. EPC 선수금을 주주의 자유 현금과 "
                "동일시하지 말고 DCF·순현금 가치에 반영한 가정을 공개한다",
            },
        ],
        "SEMICONDUCTOR_CYCLE": [
            {
                "owner": "MODEL",
                "topic": "cycle_inputs",
                "instruction": "메모리 가격·공급증설·재고·capex의 지속성을 확인한다",
            },
            {
                "owner": "MODEL",
                "topic": "memory_contract_prices",
                "known_local_gap": (
                    "DRAM·NAND 고정거래가격 시계열이 로컬에 없다. 자동 수집을 "
                    "만들지 않기로 한 결정이며 게으름이 아니다 — 고정거래가격은 "
                    "TrendForce 유료 구간이고, 무료로 구할 수 있는 것(현물가·"
                    "수출물가지수·수출단가)을 '판가'라고 부르면 2차 출처를 1차인 "
                    "척하는 것이 된다"
                ),
                "instruction": (
                    "로컬에 있는 것(가격·재무·재고·수급)은 전부 사이클의 결과이고, "
                    "사이클을 움직이는 원인인 판가는 이 저장소에 없다. 재고자산과 "
                    "DIO로 역추정할 수는 있으나 분기당 한 번, 45일 늦게 들어온다. "
                    "판가는 매번 직접 조사한다. 조사에서 시간을 아끼려면 무엇이 "
                    "열려 있는지부터 알아둔다 — TrendForce 현물가(trendforce.com/"
                    "price/dram/dram_spot)는 로그인 없이 품목별 일간 시세가 "
                    "보이고, 고정거래가격은 회원 전용이라 언론 인용으로만 접근된다. "
                    "현물가는 고정거래가격의 대체재가 아니라 선행지표다 — 계약가는 "
                    "늦게 반영되므로 사이클 전환은 현물에서 먼저 보인다. 둘을 "
                    "같은 것으로 섞어 쓰지 않는다. 결론을 좌우하는 근거는 인용 "
                    "전에 `python3.13 -m cio snapshot`으로 보존하고, 끝내 2차 "
                    "출처뿐이라면 그 사실을 unknowns에 남긴다"
                ),
            },
        ],
        "CLINICAL_BIOTECH": [
            {
                "owner": "MODEL",
                "topic": "pipeline_rnpv",
                "known_local_gap": "임상단계와 현금은 적자 바이오의 기업가치를 확정하지 않는다. "
                "허가 대상 환자·순약가·시장 침투·추가 개발비·특허 및 규제독점·로열티·"
                "상업화 시점은 구조화되지 않아 원문 조사와 명시적 모델 가정이 필요하다",
                "instruction": "파이프라인 단계·효능 표본·대조군·내약성·등록경로·경쟁·"
                "희석·현금 런웨이를 조사한다. 10-K의 특허 만료와 인라이선스 대가도 "
                "확인하고 orphan 지정을 승인이나 경쟁사 배제로 읽지 않는다. 환자 전체와 "
                "실제 허가 대상, 회사 TAM과 달성 매출을 구분한다. 단계별 승인 빈도를 "
                "해당 약의 성공확률이나 주식 승률로 그대로 쓰지 않는다. DCF를 강제하지 않는다",
            }
        ],
        "QUALITY_GROWTH": [
            {
                "owner": "MODEL",
                "topic": "growth_quality",
                "instruction": "성장 지속성·가격결정력·마진·경쟁우위와 현재 멀티플을 검증한다",
            }
        ],
        "CAPITAL_INTENSIVE_GROWTH": [
            {
                "owner": "MODEL",
                "topic": "capital_returns",
                "instruction": "수요 가정·설비투자 회수율·세그먼트별 현금창출과 자금조달 위험을 검증한다",
            }
        ],
        "MATURE_TECHNOLOGY": [
            {
                "owner": "MODEL",
                "topic": "legacy_transition",
                "instruction": "레거시 매출 감소·반복매출 전환·현금흐름 질·레버리지를 검증한다",
            }
        ],
        "BROAD_EQUITY_ETF": [
            {
                "owner": "MODEL",
                "topic": "etf_structure",
                "instruction": "보수·추적오차·구성·분배의 지속성과 대안을 비교한다",
            }
        ],
        "SINGLE_STOCK_OPTION_INCOME": [
            {
                "owner": "MODEL",
                "topic": "derivative_payoff",
                "instruction": "기초자산·상승 캡·옵션 프리미엄·NAV 침식·분배 원천을 분석한다. 회사 DCF를 ETF에 적용하지 않는다",
            }
        ],
    }
    return common + _market_requirements(asset) + additions.get(lens, [])


# 렌즈만으로는 안 되는 이유 — 결측은 종목의 성격이 아니라 **시장**을 따라 갈린다.
# 미국 자산에는 컨센서스·수급·대차가 통째로 없는데, 그 사실이 어디에도 안 적히면
# packet 은 `multiples_at_review_price: null` 만 보여 준다. 그것은 "볼 것이 없다"
# 와 구분되지 않고, 모델은 없는 줄도 모른 채 판단을 닫는다. 결함을 아는 모델만
# 올바로 판단하는 상태가 바로 이 저장소가 없애려는 것이다.
MARKET_GAPS = {
    "US": [
        {
            "owner": "MODEL",
            "topic": "forward_multiples",
            "known_local_gap": (
                "미국 자산에는 컨센서스 추정 EPS 가 로컬에 없다. 그래서 "
                "`multiples_at_review_price` 가 null 이고, 검토 가격의 forward "
                "PER 을 이 저장소가 내주지 못한다. 한국 자산은 FnGuide 가 "
                "제공하는 1차 표를 받지만 미국은 같은 등급의 무료 출처가 없다 — "
                "만들지 않기로 한 결정이며 게으름이 아니다. 2차 출처를 1차인 "
                "척 실으면 배수의 근거가 조용히 약해진다"
            ),
            "instruction": (
                "이 종목의 forward 배수를 쓰려면 추정 EPS 를 직접 조사하고 "
                "출처와 기준일을 밝힌다. 로컬에 있는 것은 확정 실적(TTM)뿐이며, "
                "그것으로 낸 배수를 forward 라고 부르지 않는다"
            ),
        },
        {
            "owner": "MODEL",
            "topic": "ownership_and_short_interest",
            "known_local_gap": (
                "수급(외국인·기관 순매수)과 대차잔고가 로컬에 없다. 한국은 "
                "KRX·금융투자협회가 1차로 주는데 미국은 13F 가 분기 45일 지연, "
                "공매도 잔고는 격주 지연이라 같은 성격의 시계열이 아니다"
            ),
            "instruction": (
                "수급을 논지에 쓰려면 직접 조사하고 그 지연을 함께 밝힌다. "
                "`structured_optional` 이 비어 있는 것은 수급이 평온하다는 뜻이 "
                "아니라 보지 않았다는 뜻이다"
            ),
        },
    ],
}


def _market_requirements(asset: dict[str, Any]) -> list[dict[str, str]]:
    if asset.get("asset_type") == "ETF":
        return []
    return MARKET_GAPS.get(asset.get("market"), [])


# 사용자가 무엇을 묻고 있는가. 신규매수와 분할매도 검토는 같은 질문이 아니고,
# 하나의 "이 가격에 살까"로 정규화하면 그 차이가 사라진다.
INTENTS = {
    "NEW_BUY",
    "ADD",
    "HOLD_REVIEW",
    "TRIM_REVIEW",
    "EXIT_REVIEW",
    "REBALANCE",
}
# 무엇을 입력으로 받고 무엇을 계산할 것인가. 두 갈래는 받는 것부터 다르다 —
# 앞은 가격과 최대 투입예산, 뒤는 최신 보유수량과 검토 수량이다. 섞으면
# 매도를 묻는 packet 이 매수 사다리를 만든다(실제로 그랬다).
NEW_CAPITAL_INTENTS = {"NEW_BUY", "ADD"}
EXISTING_POSITION_INTENTS = {"HOLD_REVIEW", "TRIM_REVIEW", "EXIT_REVIEW"}


def _intent_family(
    intent: str | None,
    *,
    amount: Any = None,
    review_quantity: Any = None,
    target_quantity: Any = None,
) -> str:
    """어느 계약으로 이 질문을 받을 것인가.

    `REBALANCE` 만 사용자의 입력에서 갈래를 읽는다 — 비중 조절은 양방향이라
    intent 만으로는 어느 쪽인지 알 수 없다. 다만 **양쪽을 동시에 싣지는
    않는다.** 한 packet 이 매수 사다리와 매도 계산을 모두 갖고 있으면 어느
    질문이었는지가 사라지고, 그 자리가 곧 우회로가 된다.
    """
    if intent in EXISTING_POSITION_INTENTS:
        return "EXISTING_POSITION"
    if intent == "REBALANCE":
        if review_quantity is not None or target_quantity is not None:
            return "EXISTING_POSITION"
        return "NEW_CAPITAL"
    return "NEW_CAPITAL"


def _user_assertions(claims: list[str] | None) -> list[dict[str, Any]]:
    """사용자가 사실이라고 말한 것을 **미확인 주장**으로 분해한다.

    원문을 그냥 packet 에 넣으면 위험하다. "실적 1,860% 나왔더만" 같은 문장은
    사용자가 이미 매수하고 싶은 상태에서 나오고, 강한 모델일수록 그 프레이밍을
    정교하게 보강한다. 그래서 원문은 싣되 **주장으로 라벨을 붙여** 싣는다.

    `drivers` 가 `researched: true/false` 로 조사 배분을 강제하듯, 각 주장은
    확인·반박·확인불가 중 하나로 닫혀야 한다. 닫지 않고 결론을 내면 계약이
    거부한다.
    """
    rows = []
    for claim in claims or []:
        text = (claim or "").strip()
        if not text:
            continue
        rows.append(
            {
                "claim_id": f"UA-{stable_hash({'claim': text}, '')[:10]}",
                "claim": text,
                "status": "USER_ASSERTED",
                "verified": False,
                "_note": "사용자 주장이며 로컬에서 확인하지 않았다",
            }
        )
    return rows


def build_context(
    asset_id: str,
    *,
    price: float,
    amount: float | None = None,
    amount_currency: str | None = None,
    intent: str | None = None,
    review_quantity: float | None = None,
    target_quantity: float | None = None,
    held_quantity: float | None = None,
    average_cost: float | None = None,
    average_cost_currency: str | None = None,
    verbatim: str | None = None,
    claims: list[str] | None = None,
    save: bool = True,
) -> tuple[dict[str, Any], Path | None]:
    asset = get_asset(asset_id)
    amount_currency = (amount_currency or "KRW").upper()
    explicit_cost_currency = average_cost_currency is not None
    average_cost_currency = (average_cost_currency or asset["currency"]).upper()
    problems = []
    position_problems: list[str] = []
    if intent is not None and intent not in INTENTS:
        problems.append(
            f"intent는 {' | '.join(sorted(INTENTS))} 중 하나여야 한다"
        )
    intent = intent or "NEW_BUY"
    family = _intent_family(
        intent,
        amount=amount,
        review_quantity=review_quantity,
        target_quantity=target_quantity,
    )
    verbatim = (verbatim or "").strip() or None
    assertions = _user_assertions(claims)
    if average_cost is not None and held_quantity is None:
        problems.append("--average-cost는 --held-quantity와 함께 준다")
    if explicit_cost_currency and average_cost is None:
        problems.append("--average-cost-currency는 --average-cost와 함께 준다")
    if average_cost_currency not in {"KRW", "USD"}:
        problems.append("average_cost_currency는 KRW 또는 USD만 지원한다")
    if held_quantity is not None and family != "EXISTING_POSITION":
        problems.append("--held-quantity는 보유분 검토에만 준다")
    for name, value in (("held_quantity", held_quantity), ("average_cost", average_cost)):
        if value is not None and (not math.isfinite(value) or value <= 0):
            problems.append(f"{name}는 0보다 큰 유한한 숫자여야 한다")
    if not math.isfinite(price) or price <= 0:
        problems.append("price는 0보다 큰 유한한 숫자여야 한다")
    # 받는 입력이 갈래마다 다르다. 한쪽 입력을 다른 갈래에 허용하면 그 자리가
    # 우회로가 된다 — 매도 질문에 예산을 실으면 매수 계산이 다시 살아난다.
    if family == "NEW_CAPITAL":
        if amount is None:
            problems.append(
                f"{intent}은 매수 예산이 필요하다 — --amount 로 최대 투입금액을 "
                "준다"
            )
        elif not math.isfinite(amount) or amount <= 0:
            problems.append("amount는 0보다 큰 유한한 숫자여야 한다")
        if review_quantity is not None or target_quantity is not None:
            problems.append(
                f"{intent}은 매수 질문이라 검토 수량·목표 잔여수량을 받지 않는다"
            )
    else:
        if amount is not None:
            problems.append(
                f"{intent}은 보유분을 어떻게 할지 묻는 질문이라 매수 예산을 "
                "받지 않는다. 새 자금 판단이 필요하면 NEW_BUY·ADD 로 따로 묻는다"
            )
        if review_quantity is not None and target_quantity is not None:
            problems.append(
                "검토 수량과 목표 잔여수량 중 하나만 준다 — 둘이 다르면 어느 "
                "쪽이 질문인지 알 수 없다"
            )
        for name, value in (
            ("검토 수량", review_quantity),
            ("목표 잔여수량", target_quantity),
        ):
            if value is not None and (not math.isfinite(value) or value <= 0):
                problems.append(f"{name}은 0보다 큰 유한한 숫자여야 한다")
    if amount_currency not in {"KRW", "USD"}:
        problems.append("amount_currency는 KRW 또는 USD만 지원한다")
    if asset["market"] == "KR" and amount_currency != "KRW":
        problems.append("한국 자산의 입력 금액은 KRW여야 한다")
    # 유한하지 않은 값은 **packet 에 담길 수 없다.** JSON 에 nan·inf 가 없으므로
    # 그대로 두면 수집을 다 마친 뒤 직렬화에서 죽고, 사용자가 보는 것은
    # "Out of range float values are not JSON compliant" 뿐이다 — 위에서 만든
    # "price는 0보다 큰 유한한 숫자여야 한다"는 그 packet 과 함께 사라진다.
    #
    # 0 이나 음수는 담기기는 하므로 BLOCKED packet 으로 돌려보낸다. 모델이
    # 이유를 읽고 INSUFFICIENT 로 답할 수 있기 때문이다. 유한하지 않은 값은
    # 돌려보낼 packet 자체가 없으므로 여기서 멈춘다.
    unrepresentable = [
        name
        for name, value in (
            ("price", price), ("amount", amount),
            ("review_quantity", review_quantity),
            ("target_quantity", target_quantity),
            ("held_quantity", held_quantity), ("average_cost", average_cost),
        )
        if isinstance(value, float) and not math.isfinite(value)
    ]
    if unrepresentable:
        raise ContextError(
            f"{', '.join(unrepresentable)}에 유한하지 않은 값이 들어왔다 — "
            "packet 으로 만들 수 없다. " + (problems[0] if problems else "")
        )
    sources: list[dict[str, Any]] = []
    optional_gaps: list[dict[str, Any]] = []
    price_summary, source = _price_summary(asset["price_symbol"])
    if price_summary and asset["asset_type"] == "ETF":
        price_summary["_note"] = (
            "수익률·변동성은 조정종가 우선, 현재가·가격범위는 실제 종가 기준이다"
            if price_summary["returns_basis"] == "ADJUSTED_CLOSE"
            else "조정종가가 없어 수익률·변동성도 실제 종가로 계산한 임시값이다"
        )
    sources.append(source)
    benchmarks = {}
    for symbol in asset["benchmarks"]:
        summary, benchmark_source = _price_summary(symbol)
        benchmarks[symbol] = summary
        sources.append(benchmark_source)
    fundamentals = None
    filings = None
    earnings_calendar = None
    if asset["asset_type"] == "STOCK":
        fundamentals, source = _latest_fundamentals(asset)
        sources.append(source)
        filings, source = _filing_summary(asset)
        if source:
            sources.append(source)
        earnings_calendar, source = _earnings_calendar_section(asset)
        if source:
            sources.append(source)
        reporting_lag = _reporting_lag(fundamentals, filings)
        if reporting_lag and fundamentals:
            fundamentals["reporting_lag"] = reporting_lag
            _mark_bridge_disclosures(
                fundamentals.get("earnings_bridge"), reporting_lag
            )
            if reporting_lag["newer_periodic_report_filed"]:
                problems.append(
                    "재무 스냅샷보다 새 정기보고서가 이미 공시됐다 — "
                    "fundamentals를 다시 수집해야 한다"
                )
            elif reporting_lag["disclosures_after_local_period"]:
                pending = reporting_lag["disclosures_after_local_period"]
                optional_gaps.append(
                    {
                        "task": f"fundamentals:{asset['asset_id']}",
                        "state": "SUPERSEDED_BY_DISCLOSURE",
                        "reason": (
                            "로컬 재무 기준일 이후에 실적 관련 공시가 "
                            f"{len(pending)}건 나왔다. 다음 정기보고서 전까지 "
                            "이 숫자가 최신이 아닐 수 있으니 공시 원문을 확인한다"
                        ),
                    }
                )
                # 경고만으로는 fail-open 이다. 로컬 재무가 낡았다는 것을
                # 알면서 그 공시의 **원문마저 없으면** 모델에게는 최신 실적을
                # 확인할 경로가 packet 안에 하나도 없다. 실제로 삼성전자
                # TTM 영업이익이 로컬 94.1조 vs 공시 179.0조로 1.9배 벌어진
                # 채로 통과했다. 색인에 제목만 있는 것은 근거가 아니다.
                stored = stored_filing_keys(asset["asset_id"])
                unfetched = (
                    [
                        item
                        for item in pending
                        if not (
                            (item.get("accession_number") or "") in stored
                            or (item.get("document_url") or "") in stored
                        )
                    ]
                    if stored is not None
                    else []
                )
                if unfetched:
                    problems.append(
                        "로컬 재무보다 새 실적 공시가 있는데 그 원문이 없다 "
                        + ", ".join(
                            f"{item.get('filing_date')} "
                            f"{item.get('description') or item.get('form')}"
                            f"({item.get('accession_number') or ''})"
                            for item in unfetched[:3]
                        )
                        + " — 받은 뒤 다시 만든다: "
                        + fetch_hint(asset, unfetched[0])
                    )
    calculation, calculation_source = _latest_calculation(
        asset,
        candidate_price=price,
        fundamentals_id=(fundamentals or {}).get("fundamentals_id"),
    )
    if calculation_source:
        sources.append(calculation_source)
    market, source = _market_summary(asset["market"])
    sources.append(source)
    for market_input in (market or {}).get("input_sources") or []:
        if market_input.get("mode") != "LOCAL_SNAPSHOT":
            continue
        symbol = market_input.get("symbol")
        if not symbol:
            continue
        _, market_input_source = _price_summary(symbol)
        sources.append(market_input_source)
    events, source = _event_summary(asset)
    if source:
        sources.append(source)
    etf, source = _etf_summary(asset)
    if source:
        sources.append(source)
    plan = inspect_plan(asset["asset_id"], include_optional=True)
    structured_optional, optional_sources = _optional_collection_summaries(asset, fundamentals=fundamentals)
    sources.extend(optional_sources)
    us_summaries, us_sources = _us_automated_summaries(asset, refresh_tasks=plan)
    structured_optional.update(us_summaries)
    sources.extend(us_sources)
    if us_summaries:
        earnings_calendar = earnings_calendar or {}
        ir_calendar = us_summaries["us_earnings"]
        earnings_calendar["announced_earnings"] = (ir_calendar.get("next_earnings")
                                                    if ir_calendar.get("state") in {"AVAILABLE", "PARTIAL"} else None)
        earnings_calendar["announcement_state"] = ir_calendar.get("state")
        earnings_calendar["announcement_note"] = ir_calendar.get("reason") or ir_calendar.get("_note")
        earnings_calendar["_announcement_role"] = "회사 IR의 실제 발표 안내다. ANNOUNCED_PRELIMINARY는 회사의 잠정 일정이며 확정이 아니다. expected_periodic_report는 별도의 SEC 제출일 추정이다"
    portfolio, source = _portfolio_summary(
        asset,
        amount=amount,
        amount_currency=amount_currency,
        price=price,
        intent=intent,
        review_quantity=review_quantity,
        target_quantity=target_quantity,
    )
    if source:
        sources.append(source)
    user_position = held_quantity is not None and held_quantity > 0 and family == "EXISTING_POSITION"
    if user_position:
        portfolio = _user_position_summary(
            asset, quantity=held_quantity, average_cost=average_cost, price=price,
            review_quantity=review_quantity, target_quantity=target_quantity,
            local_portfolio=portfolio,
            average_cost_currency=average_cost_currency,
        )
        ledger_source = None
    else:
        ledger_source = _attach_position_math(
            portfolio, asset, price, include_add_ladder=(family == "NEW_CAPITAL")
        )
    if ledger_source:
        sources.append(ledger_source)
    if asset["market"] == "US" or (user_position and average_cost_currency == "USD"):
        _, fx_source = _price_summary("KRW=X")
        sources.append(fx_source)

    # 투자정책은 사용자가 정한 자본 제약이다. 없어도 BLOCKED 로 만들지 않는다 —
    # 정책이 없다는 것은 데이터 결손이 아니라 '아직 정하지 않았다'는 상태이고,
    # 그 상태에서 무엇을 할 수 있는지는 모델이 정한다. 다만 계약 자체가 깨진
    # 파일은 조용히 무시하지 않고 optional_gaps 로 드러낸다.
    policy_data, policy_errors = load_policy()
    investor_policy = policy_for_context(
        policy_data, portfolio, errors=policy_errors
    )
    if policy_data is not None and not policy_errors:
        sources.append(
            {
                "source_id": "POLICY:INVESTOR",
                "kind": "USER_DATA",
                "path": str(POLICY_PATH.relative_to(ROOT)),
                "data_as_of": policy_data.get("policy_version"),
            }
        )
    if policy_errors:
        optional_gaps.append(
            {
                "task": "policy:investor",
                "state": "CONTRACT_FAILED",
                "reason": "; ".join(policy_errors),
            }
        )
    elif policy_data is None:
        optional_gaps.append(
            {
                "task": "policy:investor",
                "state": "MISSING",
                "reason": (
                    "config/investor-policy.json이 없다. 수량·분할매도 판단의 "
                    "한도가 어디에도 없다"
                ),
            }
        )
    elif investor_policy.get("unset"):
        optional_gaps.append(
            {
                "task": "policy:investor",
                "state": "PARTIAL",
                "reason": (
                    "정하지 않은 한도: "
                    + ", ".join(investor_policy["unset"])
                ),
            }
        )

    for task in plan:
        if task.required and task.state != "FRESH":
            problems.append(f"{task.name}: {task.state}")
        elif not task.required and task.state != "FRESH":
            optional_gaps.append(
                {
                    "task": task.name,
                    "state": task.state,
                    "reason": task.reason,
                }
            )
    if not price_summary:
        problems.append("가격 시계열이 없다")
    elif price_summary.get("currency") != asset["currency"]:
        problems.append("가격 시계열 통화가 자산 registry와 다르다")
    if asset["asset_type"] == "STOCK" and not fundamentals:
        problems.append("재무 스냅샷이 없다")
    if fundamentals and fundamentals.get("contract_errors"):
        problems.append("재무 스냅샷 계약 또는 공시원 검증에 실패했다")
    if asset["market"] == "US" and asset["asset_type"] == "STOCK" and not filings:
        problems.append("SEC 제출 문서 색인이 없다")
    if filings and filings.get("contract_errors"):
        problems.append("SEC 제출 문서 색인 계약 검증에 실패했다")
    if events and events.get("contract_errors"):
        problems.append("한국 이벤트 packet 계약 검증에 실패했다")
    if asset["asset_type"] == "ETF" and not etf:
        problems.append("ETF 구성·구조 스냅샷이 없다")
    if etf and etf.get("contract_errors"):
        problems.append("ETF 구성 스냅샷 계약 검증에 실패했다")
    proposed_trade = portfolio.get("proposed_trade") or {}
    buying = proposed_trade.get("side") != "SELL"
    if (
        asset["market"] == "US"
        and buying
        and proposed_trade
        and proposed_trade.get("amount_krw") is None
    ):
        problems.append("미국 자산 매수금액 환산에 필요한 KRW/USD 환율이 없다")
    if proposed_trade and proposed_trade.get("sufficient_cash") is False:
        optional_gaps.append(
            {
                "task": "portfolio:cash",
                "state": "USER_CONFIRMATION",
                "reason": (
                    "요청 금액이 저장된 현금을 넘는다. 사용자가 이번 입력 금액을 "
                    "최신 한도로 확인하면 그 입력을 우선한다"
                ),
            }
        )
    if (
        buying
        and proposed_trade
        and proposed_trade.get("estimated_quantity") is not None
        and proposed_trade["estimated_quantity"] <= 0
    ):
        problems.append("요청 금액으로 최소 1주를 매수할 수 없다")

    # 수량 지시를 낼 수 있는가는 종목 관점을 세울 수 있는가와 다른 질문이다.
    # 원장이 낡았으면 "이 종목은 비싸다"는 여전히 말할 수 있지만 "30주 중
    # 10주를 팔아라"는 말할 수 없다 — 30주가 아직 30주인지 모르기 때문이다.
    existing_position = portfolio.get("existing_position") or {}
    if not portfolio.get("available"):
        position_problems.append(
            "보유 원장(knowledge/portfolio/holdings.csv)이 없거나 읽을 수 없다"
        )
    else:
        for gap in optional_gaps:
            if gap.get("task") == "portfolio:holdings" and not user_position:
                position_problems.append(
                    f"보유 원장이 최신이 아니다({gap.get('state')}) — {gap.get('reason')}"
                )
        if existing_position.get("held") and not existing_position.get("quantity"):
            position_problems.append(
                "원장에 보유는 있는데 수량이 없다. 수량 대조를 할 수 없다"
            )
        if family == "EXISTING_POSITION" and not existing_position.get("held"):
            position_problems.append(
                f"{intent}인데 원장에 이 자산의 보유가 없다. 질문과 원장이 다르다"
            )
    position_problems.extend(proposed_trade.get("contract_errors") or [])

    created_at = dt.datetime.now(KST).isoformat(timespec="seconds")
    held_quantity_for_question = existing_position.get("quantity")
    request = {
        "asset_id": asset["asset_id"],
        "price": price,
        "price_currency": asset["currency"],
        "amount": amount,
        "amount_currency": amount_currency,
        "as_of": created_at,
        "intent": intent,
        "intent_family": family,
        "question": (
            "이 가격과 금액으로 매수하면 정해진 기간 안에 승리할 가능성이 있는가. "
            "기대수익, 선호 진입가, 손절가, 분할매도 시작가, 다시 물어볼 가격을 결정하라"
            if family == "NEW_CAPITAL"
            else (
                f"보유 중인 {held_quantity_for_question}주를 이 가격에서 계속 들고 갈 "
                "것인가, 줄일 것인가, 전량 정리할 것인가. 수량과 사유, 남는 보유분의 "
                "손절가·분할매도 시작가·다시 물어볼 가격을 결정하라. "
                "**이 질문에는 매수 예산이 실려 있지 않다** — 더 살 이유가 있으면 "
                "그 근거를 thesis 에 남기고 새 자금 판단은 NEW_BUY·ADD 로 다시 묻는다"
            )
        ),
    }
    if family == "EXISTING_POSITION":
        request["review_quantity"] = review_quantity
        request["target_remaining_quantity"] = target_quantity
    if held_quantity is not None or average_cost is not None:
        request["position_input"] = {
            "quantity": held_quantity, "average_cost": average_cost,
            "currency": average_cost_currency, "source": "USER_INPUT",
            "average_cost_currency": average_cost_currency,
            "as_of": created_at, "verified": False,
        }
    if verbatim:
        request["verbatim"] = verbatim
    if assertions:
        request["user_assertions"] = assertions
    if verbatim or assertions:
        request["_role"] = (
            "사용자가 실제로 한 말이다. **분석의 전제가 아니라 검증 대상이다.** "
            "사용자가 사실이라고 말한 것은 사실이 아니라 주장이며, 각 주장은 "
            "decision.user_assertion_checks에서 확인·반박·확인불가 중 하나로 "
            "닫혀야 한다. 사용자의 낙관적 프레이밍을 그대로 논지에 옮기면 "
            "이 시스템은 독립 CIO가 아니라 확신을 정교하게 보강해 주는 도구가 "
            "된다"
        )
    if price_summary and price_summary.get("close"):
        request["latest_local_close"] = price_summary["close"]
        request["asked_vs_latest_close_pct"] = round(
            (price / price_summary["close"] - 1) * 100, 2
        )
    from cio.capital import section as capital_section
    capital, capital_sources = capital_section(asset)
    for source in capital_sources:
        if source["source_id"] not in {s.get("source_id") for s in sources if s}:
            sources.append(source)
    evidence_index = []
    evidence_by_id: dict[str, dict[str, Any]] = {}
    for item in sources:
        if not item or not item.get("path"):
            continue
        origin = (ROOT / item["path"]).resolve()
        try:
            origin.relative_to(ROOT.resolve())
        except ValueError as exc:
            raise ContextError(f"evidence 경로가 저장소 밖을 가리킨다: {item['path']}") from exc
        if not origin.exists() or not origin.is_file():
            continue
        source_id = item.get("source_id")
        if not source_id:
            continue
        digest = content_hash(origin)
        entry = {**item, "content_sha256": digest}
        if save:
            snapshot = _snapshot_evidence(origin, digest)
            entry["origin_path"] = item["path"]
            entry["path"] = str(snapshot.relative_to(ROOT))
        previous = evidence_by_id.get(source_id)
        if previous:
            if previous["content_sha256"] != digest:
                raise ContextError(f"source_id가 서로 다른 파일을 가리킨다: {source_id}")
            continue
        evidence_by_id[source_id] = entry
        evidence_index.append(entry)
    prompt_path = ROOT / "prompts" / "cio-system.md"
    schema_path = ROOT / "schemas" / "cio-decision.schema.json"
    prompt_digest = content_hash(prompt_path)
    schema_digest = content_hash(schema_path)
    model_contract = {
        "decision_schema": "schemas/cio-decision.schema.json",
        "prompt": "prompts/cio-system.md",
        "prompt_version": _prompt_version(prompt_path),
        "assumption_review_required": True,
        "assumption_spec": "schemas/cio-assumptions.schema.json",
        "prompt_sha256": prompt_digest,
        "decision_schema_sha256": schema_digest,
        "provider_neutral": True,
    }
    if save:
        model_contract["prompt_snapshot"] = str(
            _snapshot_evidence(prompt_path, prompt_digest).relative_to(ROOT)
        )
        model_contract["decision_schema_snapshot"] = str(
            _snapshot_evidence(schema_path, schema_digest).relative_to(ROOT)
        )
    packet: dict[str, Any] = {
        "schema_version": CONTEXT_SCHEMA,
        "context_id": None,
        "created_at": created_at,
        "request": request,
        "asset": asset,
        "validation": {
            "status": "BLOCKED" if problems else "PASS",
            "problems": sorted(set(problems)),
            "optional_gaps": optional_gaps,
            # 하나의 PASS 는 두 질문을 한 칸에 접는다. 종목 관점을 세울 수
            # 있는가와 수량 지시를 낼 수 있는가는 요구하는 입력이 다르고,
            # 하나로 접으면 원장이 낡아도 "PASS" 가 되어 매도 수량이 그대로
            # 나간다. status 는 앞의 것이고 뒤의 것은 따로 센다.
            "readiness": {
                "security_view": {
                    "status": "BLOCKED" if problems else "PASS",
                    "problems": sorted(set(problems)),
                },
                "position_action": {
                    "status": "BLOCKED" if position_problems else "PASS",
                    "problems": sorted(set(position_problems)),
                    "basis": "USER_INPUT_CONDITIONAL" if user_position else "LOCAL_HOLDINGS",
                },
                "_role": (
                    "security_view 는 이 종목에 대한 관점을 세울 수 있는가이고 "
                    "validation.status 와 같다. position_action 은 실제 수량 "
                    "지시를 낼 수 있는가이며, BLOCKED 면 보유분에 대해 "
                    "ADD·HOLD·TRIM·EXIT·NOT_APPLICABLE 을 낼 수 없다 — "
                    "action.direction=UNDETERMINED 와 undetermined_reason="
                    "NO_POSITION_DATA 로 답한다. 종목 관점(verdict·thesis·"
                    "가격계획)은 그대로 낸다"
                ),
            },
            "meaning": "데이터·산술 준비 상태이며 투자 판정이 아니다",
        },
        "context": {
            "price": price_summary,
            "benchmarks": benchmarks,
            "market": market,
            "fundamentals": fundamentals,
            "filings": filings,
            "earnings_calendar": earnings_calendar,
            "clinical_pipeline": (
                pipeline_for_context(asset["asset_id"])
                if asset["analysis_lens"] == "CLINICAL_BIOTECH" else None
            ),
            "multiples_at_review_price": _with_event_lag(
                position_multiples(
                    asset["asset_id"], price, estimates_only=True
                )
                if asset["asset_type"] == "STOCK" else None,
                asset["asset_id"],
                as_of_key="consensus_as_of",
            ),
            "applicable_calculation": calculation,
            "events": events,
            "etf": etf,
            "structured_optional": structured_optional,
            "data_coverage": _data_coverage(
                asset,
                structured_optional=structured_optional,
                fundamentals=fundamentals,
            ),
            "portfolio": portfolio,
            "investor_policy": investor_policy,
            "prior_assessments": _memory_digest(
                asset["asset_id"],
                latest_close=(price_summary or {}).get("close"),
                close_date=(price_summary or {}).get("as_of"),
                close_is_settled=(
                    ((price_summary or {}).get("last_bar") or {}).get(
                        "is_settled_close"
                    )
                ),
                target_annual_return_pct=(
                    ((investor_policy or {}).get("objective") or {}).get(
                        "target_annual_return_pct"
                    )
                ),
                return_currency=asset["currency"],
                objective_currency=(investor_policy or {}).get("base_currency"),
                daily_ranges=_daily_ranges(asset["price_symbol"]),
            ),
            "research_requirements": _research_requirements(asset, filings),
            "open_research_requests": open_requests_for_context(
                asset["asset_id"]
            ),
            # 보존한 웹 원문은 local_documents 에 url 과 함께 들어간다.
            # 따로 web_snapshots 를 싣던 시절에는 같은 문서가 두 목록에 나왔다.
            "local_documents": documents_for_context(asset["asset_id"]),
        },
        "evidence_index": evidence_index,
        "model_contract": model_contract,
    }
    if capital:
        packet["context"]["capital_changes"] = capital
        packet["model_contract"]["earnings_basis_required"] = capital.get("state") == "ANNOUNCED"
    if save:
        prior = packet["context"].get("prior_assessments") or {}
        if isinstance(prior, dict):
            for key, field in (("latest_entry_plan", "entry_plan_ref"), ("latest_exit_plan", "exit_plan_ref")):
                ref = (prior.get(key) or {}).get(field)
                if ref:
                    original = ROOT / ref["path"]
                    ref["path"] = str(_snapshot_evidence(original, ref["content_sha256"]).relative_to(ROOT))
    _fit_context_budget(packet, seal_plans=save)
    packet["context_id"] = stable_hash(
        {key: value for key, value in packet.items() if key != "context_id"},
        "CTX-",
    )
    compact_size = _compact_size(packet)
    if compact_size > MAX_CONTEXT_JSON_BYTES:
        # 여기까지 왔다는 것은 트리머가 이미 다 돌고도 못 맞췄다는 뜻이다.
        # 그 사실을 말하지 않으면 "packet 이 크다"로만 읽히고, 무엇을 줄여야
        # 하는지 알 수 없다 — 트리머가 손댈 수 있는 것은 이미 다 버린 뒤다.
        # packet 은 저장하지 않으므로 버린 내용이 남지는 않는다.
        dropped = next(
            (gap.get("trimmed") or []
             for gap in packet["validation"]["optional_gaps"]
             if gap.get("state") == "TRIMMED_FOR_BUDGET"),
            [],
        )
        raise ContextError(
            f"context packet이 {compact_size} bytes로 제한 "
            f"{MAX_CONTEXT_JSON_BYTES} bytes를 넘었다"
            + (f" — 트리머가 {', '.join(dropped)} 를 버리고도 모자랐다. "
               "packet 은 저장하지 않았다"
               if dropped else
               " — 트리머가 버릴 수 있는 항목이 없었다")
        )
    path = None
    if save:
        path = (
            KNOWLEDGE_DIR
            / "contexts"
            / asset["asset_id"]
            / f"{packet['context_id']}.json"
        )
        write_json(path, packet)
    return packet, path


def _snapshot_evidence(origin: Path, digest: str) -> Path:
    directory = KNOWLEDGE_DIR / "evidence" / "objects"
    directory.mkdir(parents=True, exist_ok=True)
    suffix = origin.suffix.lower() if origin.suffix else ".bin"
    path = directory / f"{digest}{suffix}"
    if path.exists():
        if content_hash(path) != digest:
            raise ContextError(f"evidence snapshot 지문이 충돌했다: {path}")
        return path
    temporary_name: str | None = None
    try:
        with origin.open("rb") as source, tempfile.NamedTemporaryFile(
            "wb", dir=directory, delete=False
        ) as temporary:
            shutil.copyfileobj(source, temporary)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_name = temporary.name
        os.replace(temporary_name, path)
    finally:
        if temporary_name and Path(temporary_name).exists():
            Path(temporary_name).unlink()
    return path
