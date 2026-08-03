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

from calculators.contracts import (
    ContractError,
    content_hash,
    load_json,
    stable_hash,
    validate_calculation,
    validate_fundamentals,
    write_json,
)
from cio.assets import get_asset
from cio.documents import documents_for_context
from cio.decision import require_valid_decision, validate_context_integrity
from cio.paths import KNOWLEDGE_DIR, ROOT
from cio.policy import POLICY_PATH, load_policy, policy_for_context
from cio.requests import open_requests_for_context
from cio.pipeline import pipeline_for_context
from cio.retire import retired_summary
from calculators.position import (
    PositionError,
    build as build_position_math,
    multiples as position_multiples,
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


class ContextError(ValueError):
    pass


def _csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig") as handle:
        return list(
            csv.DictReader(
                line for line in handle if not line.lstrip().startswith("#")
            )
        )


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
            "drawdown_from_12m_high_pct": round(
                (closes[-1] / high - 1) * 100, 2
            ),
            "position_in_12m_range_pct": round(
                (closes[-1] - low) / (high - low) * 100, 2
            )
            if high > low
            else 50.0,
            "realized_volatility_3m_pct": round(
                statistics.stdev(returns) * math.sqrt(252) * 100, 2
            )
            if len(returns) >= 2
            else None,
            # 마지막 행이 확정 종가인지. 장중 수집분이면 close·거래량·수익률이
            # 전부 진행 중인 값이라 확정 일봉처럼 인용하면 안 된다.
            "last_bar": {
                "date": session.group(1) if session else points[-1][0],
                "session": session.group(2) if session else "UNKNOWN",
                "is_settled_close": bool(session)
                and session.group(2) == "SETTLED",
                "_warning": (
                    "마지막 봉이 확정 종가가 아니다 — 장중 수집분이다. close 와 "
                    "그것을 쓰는 수익률·낙폭·변동성은 진행 중인 값이며 종가로 "
                    "인용하면 안 된다"
                    if session and session.group(2) == "UNSETTLED"
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
                "총차입(values.long_term_debt)의 구성이다. LEASE 는 IFRS16 "
                "리스부채이고 영업현금흐름도 같은 기준이라 기본은 포함이다. "
                "리스를 뺀 순부채를 쓰려면 LEASE 를 덜어내고 다시 센다"
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
    newer_disclosures = [
        {
            "form": item.get("form"),
            "filing_date": item.get("filing_date"),
            "description": item.get("description"),
            "document_url": item.get("document_url"),
        }
        for item in events
        if isinstance(item, dict)
        and item.get("form") in RESULT_DISCLOSURE_FORMS
        and isinstance(item.get("filing_date"), str)
        and item["filing_date"] > period_end
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


def _peer_multiples(asset: dict[str, Any]) -> dict[str, Any] | None:
    """동종의 배수를 같은 소스·같은 방식으로 모아 비교한다.

    언론이 인용하는 '에이피알 30배' 같은 값은 기준연도도 주가시점도 알 수 없어
    비교에 쓸 수 없다. 여기서는 동종도 같은 수집기를 거치므로 EPS 기준·PER
    계산·기준일이 전부 같다.
    """
    peers = asset.get("peers") or []
    if not peers:
        return None
    rows = []
    for peer in peers:
        path = KNOWLEDGE_DIR / "consensus" / f"{peer['asset_id']}.json"
        if not path.exists():
            rows.append({**peer, "available": False})
            continue
        try:
            data = load_json(path)
        except (ContractError, OSError):
            rows.append({**peer, "available": False})
            continue
        annual = ((data.get("estimates") or {}).get("annual")) or []
        forward = next((row for row in annual if not row["actual"]), None)
        band = (data.get("estimates") or {}).get("per_band_actual") or {}
        rows.append({
            "asset_id": peer["asset_id"],
            "name": peer["name"],
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
        })
    usable = [row["forward_per"] for row in rows if row.get("forward_per")]
    return {
        "basis": "동일 수집기·동일 기준일의 컨센서스 추정",
        "peers": rows,
        "forward_per_median": round(statistics.median(usable), 2)
        if usable
        else None,
        "forward_per_min": min(usable) if usable else None,
        "forward_per_max": max(usable) if usable else None,
        "_warning": (
            "동종 선정은 registry에 명시된 판단이며 자동으로 옳지 않다. "
            "성장률·수익성이 다르면 같은 배수를 줄 이유가 없으므로 "
            "forward_revenue_yoy_pct와 forward_roe_pct를 함께 본다"
        ),
    }


def _optional_collection_summaries(
    asset: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    enabled = set(asset.get("optional_collectors") or [])
    asset_id = asset["asset_id"]
    summaries: dict[str, Any] = {}
    sources: list[dict[str, Any]] = []

    if asset["market"] == "KR":
        path = KNOWLEDGE_DIR / "consensus" / f"{asset_id}.json"
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
                    "revisions": {
                        key: revisions.get(key)
                        for key in (
                            "window_days",
                            "observed",
                            "raised",
                            "lowered",
                            "unchanged",
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
                sources.append(
                    {
                        "source_id": f"CONSENSUS:{asset_id}",
                        "kind": "SECONDARY_ESTIMATE",
                        "path": str(path.relative_to(ROOT)),
                        "data_as_of": data.get("as_of"),
                    }
                )
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
        if isinstance(data, dict) and data.get("latest_mix"):
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
    if math_result.get("ladder"):
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


def _portfolio_summary(
    asset: dict[str, Any],
    *,
    amount: float,
    amount_currency: str,
    price: float,
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
    budget_krw: float | None
    budget_asset_currency: float | None
    if amount_currency == "KRW":
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
    if asset["market"] == "US":
        average_cost = average_cost / fx if average_cost and fx else None
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
                "average_cost_currency": asset["currency"],
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
            "proposed_trade": {
                "amount_krw": round(budget_krw, 2)
                if budget_krw is not None
                else None,
                "estimated_quantity": round(quantity, 6)
                if quantity is not None
                else None,
                "quantity_basis": (
                    "INTEGER_SHARES"
                    if asset["market"] == "KR"
                    else "FRACTIONAL_SHARES_ALLOWED"
                ),
                "estimated_deployed_krw": round(deployed_krw, 2)
                if deployed_krw is not None
                else None,
                "estimated_unspent_budget_krw": round(
                    budget_krw - deployed_krw, 2
                )
                if budget_krw is not None and deployed_krw is not None
                else None,
                "funding_assumption": "기존 포트폴리오 현금에서 집행",
                "sufficient_cash": sufficient_cash,
                "cash_after_krw": round(cash - deployed_krw, 2)
                if deployed_krw is not None
                else None,
                "post_trade_weight_total_assets_pct": round(post_weight, 2)
                if post_weight is not None
                else None,
                "fx_krw_per_usd": round(fx, 4)
                if asset["market"] == "US" and fx is not None
                else None,
                "fx_as_of": fx_as_of,
                "_warning": "수수료·세금·슬리피지는 수량과 집행액에서 제외했다",
            },
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


def _prior_plan_check(
    plan: Any,
    *,
    latest_close: float | None,
    close_date: str | None,
    close_is_settled: bool | None,
) -> dict[str, Any] | None:
    """직전 가격계획의 각 문턱을 현재 종가와 기계적으로 대조한다.

    판단이 아니라 산술이다. 모델이 자기가 세운 손절·재검토 가격이 이미
    닿았는지를 매번 다시 계산하지 않게 하는 것이 목적이다.
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

    stop = level("stop_price")
    reask_below = level("reask_below")
    reask_above = level("reask_above")
    scale_out = level("scale_out_start")
    entry_low = level("entry_range_low")
    entry_high = level("entry_range_high")
    preferred = level("preferred_entry")
    triggered: list[str] = []
    if stop is not None and latest_close <= stop:
        triggered.append("stop_price")
    if reask_below is not None and latest_close <= reask_below:
        triggered.append("reask_below")
    if reask_above is not None and latest_close >= reask_above:
        triggered.append("reask_above")
    if scale_out is not None and latest_close >= scale_out:
        triggered.append("scale_out_start")
    return {
        "close": latest_close,
        "close_date": close_date,
        "close_is_settled": close_is_settled,
        "stop_price": stop,
        "stop_breached": None if stop is None else latest_close <= stop,
        "reask_below": reask_below,
        "reask_below_reached": (
            None if reask_below is None else latest_close <= reask_below
        ),
        "reask_above": reask_above,
        "reask_above_reached": (
            None if reask_above is None else latest_close >= reask_above
        ),
        "scale_out_start": scale_out,
        "scale_out_reached": (
            None if scale_out is None else latest_close >= scale_out
        ),
        "in_entry_range": (
            None
            if entry_low is None or entry_high is None
            else entry_low <= latest_close <= entry_high
        ),
        "close_vs_preferred_entry_pct": (
            None
            if preferred is None or preferred <= 0
            else round((latest_close / preferred - 1) * 100, 2)
        ),
        "triggered": triggered,
        "_note": (
            "종가와 문턱의 산술 비교일 뿐이다. 장중 저가·고가로 스쳤는지는 "
            "보지 않으며, 문턱에 닿았다는 사실이 그때의 논지가 여전히 옳다는 "
            "뜻도 아니다. 왜 닿았는지는 모델이 판단한다"
        ),
    }


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


def _fit_context_budget(packet: dict[str, Any]) -> None:
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
    for field in ("thesis", "strongest_counter_thesis"):
        if carried.get(field) is None:
            continue
        carried[field] = None
        trimmed.append(f"prior_assessments.carried_forward.{field}")
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


def _memory_digest(
    asset_id: str,
    *,
    latest_close: float | None = None,
    close_date: str | None = None,
    close_is_settled: bool | None = None,
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
                    if key not in {"evaluation_id", "evaluated_at"}
                },
                "EVAL-",
            )
        except (TypeError, ValueError):
            continue
        if (
            evaluation_id != expected_id
            or path.stem != evaluation_id
            or data.get("schema_version") != "cio-evaluation/1.0.0"
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

    selected = assessments[-3:]
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
            plan = data.get("price_plan")
            counter = data.get("strongest_counter_thesis") or {}
            signals = data.get("invalidation_signals")
            drivers = data.get("drivers")
            row["carried_forward"] = {
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
                "invalidation_signals": (
                    [_clip(signal, 220) for signal in signals[:6]]
                    if isinstance(signals, list)
                    else None
                ),
                "_role": (
                    "직전 판단의 살아 있는 계획이다. 이번 판단은 이 무효화 "
                    "신호와 재검토 사건이 실제로 발생했는지 먼저 확인한 뒤 "
                    "시작한다. 과거 결론을 따르라는 뜻이 아니라, 바뀐 것이 "
                    "무엇인지 명시하라는 뜻이다. drivers 는 그때의 이익 동인 "
                    "배분이며 researched:false 였던 것은 아직 한 번도 보지 않은 "
                    "동인이다 — 계속 미루고 있는지 확인한다"
                ),
            }
            row["price_vs_prior_plan"] = _prior_plan_check(
                plan,
                latest_close=latest_close,
                close_date=close_date,
                close_is_settled=close_is_settled,
            )
        result.append(row)
    unusable.sort(key=lambda item: str(item.get("created_at") or ""))
    retired = retired_summary(asset_id)
    digest: dict[str, Any] = {
        "live_count": len(result),
        "state": "CARRIED_FORWARD" if result else "NO_LIVE_JUDGMENT",
        "live": result,
    }
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
        "직전 판단이 살아 있다. 무효화 신호와 재검토 문턱이 실제로 닿았는지 "
        "먼저 확인하고, 바뀐 것을 명시한 뒤 시작한다"
        if result
        else "이 자산에 유효한 판단이 0건이다. 이어받을 논지가 없으므로 "
        "처음부터 판단한다. 아래 목록은 그런 판단이 있었다는 사실일 뿐이며 "
        "그 값을 추측해 채우지 않는다"
    )
    return digest


def _research_requirements(asset: dict[str, Any]) -> list[dict[str, str]]:
    common = [
        {
            "owner": "MODEL",
            "topic": "current_catalysts_and_counterevidence",
            "instruction": "최신 공시 이후 변화와 가장 강한 반대 근거를 직접 조사한다",
        }
    ]
    lens = asset["analysis_lens"]
    additions = {
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
                "instruction": "파이프라인 단계·성공확률·희석·현금 런웨이를 조사한다. DCF를 강제하지 않는다",
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
    amount: float,
    amount_currency: str | None = None,
    intent: str | None = None,
    verbatim: str | None = None,
    claims: list[str] | None = None,
    save: bool = True,
) -> tuple[dict[str, Any], Path | None]:
    asset = get_asset(asset_id)
    amount_currency = (amount_currency or "KRW").upper()
    problems = []
    if intent is not None and intent not in INTENTS:
        problems.append(
            f"intent는 {' | '.join(sorted(INTENTS))} 중 하나여야 한다"
        )
    verbatim = (verbatim or "").strip() or None
    assertions = _user_assertions(claims)
    if not math.isfinite(price) or price <= 0:
        problems.append("price는 0보다 큰 유한한 숫자여야 한다")
    if not math.isfinite(amount) or amount <= 0:
        problems.append("amount는 0보다 큰 유한한 숫자여야 한다")
    if amount_currency not in {"KRW", "USD"}:
        problems.append("amount_currency는 KRW 또는 USD만 지원한다")
    if asset["market"] == "KR" and amount_currency != "KRW":
        problems.append("한국 자산의 입력 금액은 KRW여야 한다")
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
            if reporting_lag["newer_periodic_report_filed"]:
                problems.append(
                    "재무 스냅샷보다 새 정기보고서가 이미 공시됐다 — "
                    "fundamentals를 다시 수집해야 한다"
                )
            elif reporting_lag["disclosures_after_local_period"]:
                optional_gaps.append(
                    {
                        "task": f"fundamentals:{asset['asset_id']}",
                        "state": "SUPERSEDED_BY_DISCLOSURE",
                        "reason": (
                            "로컬 재무 기준일 이후에 실적 관련 공시가 "
                            f"{len(reporting_lag['disclosures_after_local_period'])}건 "
                            "나왔다. 다음 정기보고서 전까지 이 숫자가 최신이 "
                            "아닐 수 있으니 공시 원문을 확인한다"
                        ),
                    }
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
    structured_optional, optional_sources = _optional_collection_summaries(asset)
    sources.extend(optional_sources)
    portfolio, source = _portfolio_summary(
        asset, amount=amount, amount_currency=amount_currency, price=price
    )
    if source:
        sources.append(source)
    ledger_source = _attach_position_math(portfolio, asset, price)
    if ledger_source:
        sources.append(ledger_source)
    if asset["market"] == "US":
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

    plan = inspect_plan(asset["asset_id"], include_optional=True)
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
    if (
        asset["market"] == "US"
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
        proposed_trade
        and proposed_trade.get("estimated_quantity") is not None
        and proposed_trade["estimated_quantity"] <= 0
    ):
        problems.append("요청 금액으로 최소 1주를 매수할 수 없다")

    created_at = dt.datetime.now(KST).isoformat(timespec="seconds")
    request = {
        "asset_id": asset["asset_id"],
        "price": price,
        "price_currency": asset["currency"],
        "amount": amount,
        "amount_currency": amount_currency,
        "as_of": created_at,
        "intent": intent or "NEW_BUY",
        "question": (
            "이 가격과 금액으로 매수하면 정해진 기간 안에 승리할 가능성이 있는가. "
            "기대수익, 선호 진입가, 손절가, 분할매도 시작가, 다시 물어볼 가격을 결정하라"
        ),
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
        # 2.1 positioning 강제 조건에 kr_lending 추가 / 2.2 필드 의미를 packet
        # `_role` 로 옮기고 prompt 에는 규율만 남김. 프롬프트 본문이 바뀌면
        # 반드시 올린다 — 같은 버전이 다른 규칙을 가리키면 모델 비교가 깨진다.
        "prompt_version": "2.2",
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
            "multiples_at_review_price": (
                position_multiples(
                    asset["asset_id"], price, estimates_only=True
                )
                if asset["asset_type"] == "STOCK" else None
            ),
            "applicable_calculation": calculation,
            "events": events,
            "etf": etf,
            "structured_optional": structured_optional,
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
            ),
            "research_requirements": _research_requirements(asset),
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
    _fit_context_budget(packet)
    packet["context_id"] = stable_hash(
        {key: value for key, value in packet.items() if key != "context_id"},
        "CTX-",
    )
    compact_size = _compact_size(packet)
    if compact_size > MAX_CONTEXT_JSON_BYTES:
        raise ContextError(
            f"context packet이 {compact_size} bytes로 제한 "
            f"{MAX_CONTEXT_JSON_BYTES} bytes를 넘었다"
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
