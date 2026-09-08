"""Same-period ratios and adjacent-quarter differences for EPC note facts."""

import calendar
import datetime as dt
import math


def numeric(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def value(report, metric):
    row = (report.get("values") or {}).get(metric) or {}
    return numeric(row.get("amount_krw")) if row.get("state") == "AVAILABLE" else None


def previous_quarter(date):
    end = dt.date.fromisoformat(date)
    if end.month not in {3, 6, 9, 12} or end.day != calendar.monthrange(end.year, end.month)[1]:
        return None
    if end.month == 3:
        return f"{end.year - 1}-12-31"
    month = end.month - 3
    return f"{end.year}-{month:02d}-{calendar.monthrange(end.year, month)[1]}"


def summary(data, fundamentals=None):
    reports = data["reports"]
    latest = reports[0]
    end = latest["report_date"]
    lookup = {r["report_date"]: r for r in reports}
    prior_date = previous_quarter(end)
    prior = lookup.get(prior_date)
    current_effect = value(latest, "estimate_change_profit_ytd")
    prior_effect = value(prior, "estimate_change_profit_ytd") if prior else None
    calendar_ytd = (latest.get("report_period_start") == end[:4] + "-01-01")
    quarter_effect = None
    if calendar_ytd and prior_date and end.endswith("03-31"):
        quarter_effect = current_effect
    elif calendar_ytd and current_effect is not None and prior_effect is not None and prior_date[:4] == end[:4] and prior.get("report_period_start") == latest["report_period_start"]:
        quarter_effect = current_effect - prior_effect
    latest_provision = value(latest, "loss_provision_balance")
    prior_provision = value(prior, "loss_provision_balance") if prior else None
    math_block = {
        "estimate_change_profit_quarter_krw": quarter_effect,
        "quarter_basis": "FISCAL_PERIOD_UNRESOLVED" if not calendar_ytd else "Q1_EQUALS_YTD" if end.endswith("03-31") else "ADJACENT_YTD_DIFFERENCE" if quarter_effect is not None else "ADJACENT_REPORT_UNAVAILABLE",
        "quarter_source_ids": [latest["source_id"]] + ([prior["source_id"]] if prior and quarter_effect is not None and not end.endswith("03-31") else []),
        "loss_provision_balance_change_krw": latest_provision - prior_provision if latest_provision is not None and prior_provision is not None else None,
        "_note": "충당부채 잔액 차이는 신규 전입액이 아니다. 추정 변경 손익의 분기 차감은 같은 연도 인접 누계가 있을 때만 하며 두 보고서 사이 정정·범위 변경 여부는 모델이 원문에서 확인한다. 미래 손익 영향은 분기 차감하지 않는다.",
    }
    fundamentals = fundamentals or {}
    f_end = fundamentals.get("period_end")
    revenue = numeric((((fundamentals.get("income_statement_ttm") or {}).get("revenue") or {}).get("ttm")))
    unbilled = value(latest, "unbilled_balance")
    same_period = f_end == end
    math_block["unbilled_pct_of_ttm_revenue"] = round(unbilled / revenue * 100, 4) if same_period and unbilled is not None and revenue and revenue > 0 else None
    bridge = (fundamentals.get("earnings_bridge") or {}).get("rows") or []
    ytd_rows = [row for row in bridge if str(row.get("end", "")).startswith(end[:4]) and row["end"] <= end]
    expected_ends = {f"{end[:4]}-{month:02d}-{calendar.monthrange(int(end[:4]), month)[1]}" for month in range(3, int(end[5:7]) + 1, 3)}
    complete = {row.get("end") for row in ytd_rows} == expected_ends and len(ytd_rows) == len(expected_ends) and bool(expected_ends)
    amounts = [numeric(row.get("operating_income")) for row in ytd_rows]
    op_ytd = sum(amounts) if complete and all(v is not None for v in amounts) else None
    math_block["operating_income_ytd_krw"] = op_ytd if same_period and calendar_ytd else None
    math_block["estimate_change_effect_pct_of_ytd_operating_income"] = round(current_effect / op_ytd * 100, 4) if same_period and calendar_ytd and current_effect is not None and op_ytd and op_ytd > 0 else None
    math_block["fundamentals_period_match"] = same_period
    math_block["ratios_note"] = "같은 기준일의 재무와만 나눈다. 추정 변경 영향/영업이익 비율은 규모 대조이며 비경상 이익 비중이라는 판정이 아니다. 표의 반올림 정밀도를 넘는 원단위 정확성을 가정하지 않는다."
    return {"available": latest["available"], "as_of": end,
            "source_ids": [r["source_id"] for r in reports],
            "latest": latest["values"],
            "history": [{"period_end": r["report_date"], "source_id": r["source_id"],
                         "values": {k: v.get("amount_krw") for k, v in r["values"].items()}} for r in reports],
            "arithmetic": math_block,
            "_role": "연결 도급공사계약 주석의 합계만 추출했다. 잔액·당기 누적 손익·미래 잔여기간 손익을 분리했다. 수주잔고는 잔여 수행의무 기준이며 IR 전체 잔고·주요 프로젝트 잔고와 범위가 다르다. None은 0이 아니라 미추출 또는 미확정이다. 원문 행·단위·표 위치는 KR_EPC 근거 파일에 있다.",
            "unresolved": [k for k, v in latest["values"].items() if v["state"] != "AVAILABLE"]}
