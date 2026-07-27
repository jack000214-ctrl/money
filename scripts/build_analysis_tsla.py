#!/usr/bin/env python3
"""TSLA 추가매수 분석 계약을 생성한다. 출처 해시는 실제 수집 파일에서 계산한다.

⚠️ 아직 TSLA 전용이다. claim·논지·반대논지 산문이 코드에 박혀 있다.
   다른 종목으로 쓰려면 산문을 별도 파일로 빼고 종목을 인자로 받아야 한다.

이 파일이 필요한 이유: `knowledge/research/analysis-tsla-20260727.json` 의
`content_hash` 는 실제 수집 파일에서 계산한 값이라 손으로 갱신할 수 없다.
출처를 다시 수집하면 이 스크립트를 다시 돌려야 분석이 유효해진다.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from contracts import content_hash  # noqa: E402

RAW = ROOT / "data/raw/edgar/2026-07-27"
CAPEX = RAW / "concept_TSLA_PaymentsToAcquirePropertyPlantAndEquipment_043005.json"
RND = RAW / "concept_TSLA_ResearchAndDevelopmentExpense_043006.json"
BAL = RAW / "balance_TSLA_042912.json"
PRICE = ROOT / "knowledge/prices/TSLA.csv"
HOLD = ROOT / "knowledge/portfolio/holdings.csv"

# 계산 ID 를 박아두면 reverse_dcf 를 다시 돌릴 때마다 여기가 낡는다.
# 인자로 받고, 생략하면 이 종목의 계산 산출물을 찾아 쓴다.
if len(sys.argv) > 1:
    CALC_PATH = ROOT / "knowledge/calculations" / f"{sys.argv[1]}.json"
else:
    found = [p for p in (ROOT / "knowledge/calculations").glob("CALC-*.json")
             if json.loads(p.read_text(encoding="utf-8"))["instrument_id"] == "TSLA"]
    if len(found) != 1:
        raise SystemExit(
            f"TSLA 계산 산출물이 {len(found)}개다. 인자로 지정한다: "
            f"python3 scripts/build_analysis_tsla.py CALC-xxxx")
    CALC_PATH = found[0]
CALC = json.loads(CALC_PATH.read_text(encoding="utf-8"))
S = CALC["outputs"]["scenarios"]

PROB = {"bear": 0.40, "base": 0.45, "bull": 0.15}
weighted = sum(S[k]["return_pct"] * PROB[k] for k in PROB)
print(f"확률가중 기대수익 {weighted:.4f}%")

SRC = [
    ("SRC-TSLA-10Q-2026Q2", CAPEX,
     "SEC EDGAR 10-Q 0001628280-26-049270 (2026-07-23 제출) XBRL "
     "PaymentsToAcquirePropertyPlantAndEquipment 전 기간",
     "2026-07-23T00:00:00+09:00"),
    ("SRC-TSLA-OPEX-2026Q2", RND,
     "SEC EDGAR XBRL ResearchAndDevelopmentExpense 전 기간 (2026Q2 10-Q 반영)",
     "2026-07-23T00:00:00+09:00"),
    ("SRC-TSLA-BALANCE-2026Q2", BAL,
     "SEC EDGAR 재무상태 요약 — 현금·단기투자·영업현금흐름 (2026-06-30 기준)",
     "2026-07-23T00:00:00+09:00"),
    ("SRC-TSLA-PRICE-20260724", PRICE,
     "Yahoo Finance chart API 일별 종가 2025-07-25~2026-07-24",
     "2026-07-24T00:00:00+09:00"),
    ("SRC-PORTFOLIO-HOLDINGS", HOLD,
     "보유내역 knowledge/portfolio/holdings.csv 기준일 2026-07-27",
     "2026-07-27T00:00:00+09:00"),
]

analysis = {
    "schema_version": "1.1.0",
    "analysis_id": "ANALYSIS-20260727-TSLA",
    "instrument_id": "TSLA",
    "company_id": "TSLA",
    "as_of": "2026-07-27T09:00:00+09:00",
    "snapshot": {
        "snapshot_id": "SNAPSHOT-20260727-TSLA",
        "created_at": "2026-07-27T14:00:00+09:00",
        "source_ids": [s[0] for s in SRC],
        "input_hashes": {
            "capex_concept": content_hash(CAPEX),
            "rnd_concept": content_hash(RND),
            "balance": content_hash(BAL),
            "price_series": content_hash(PRICE),
        },
    },
    "sources": [
        {
            "source_id": sid,
            "source_type": "FILING" if "edgar" in str(path) else "MARKET_DATA",
            "locator": loc,
            "published_at": pub,
            "retrieved_at": "2026-07-27T13:30:00+09:00",
            "content_hash": content_hash(path),
        }
        for sid, path, loc, pub in SRC
    ],
    "claims": [
        {"claim_id": "CLAIM-REV-001", "type": "FACT",
         "text": "2026 Q2 매출 28,236M USD, 전년 동기 22,496M 대비 +25.5%. "
                 "매출 성장은 둔화되지 않았다.",
         "source_ids": ["SRC-TSLA-OPEX-2026Q2"]},
        {"claim_id": "CLAIM-OPEX-001", "type": "FACT",
         "text": "2026 Q2 R&D 2,371M USD (전년 1,589M, +49.2%), "
                 "SG&A 1,982M USD (전년 1,366M, +45.1%). 매출 성장률(+25.5%)의 "
                 "두 배 속도로 영업비용이 늘었다. 영업이익은 923M→398M (-56.9%), "
                 "영업이익률 4.10%→1.41%.",
         "source_ids": ["SRC-TSLA-OPEX-2026Q2"]},
        {"claim_id": "CLAIM-GM-001", "type": "DERIVED",
         "text": "2026 Q2 매출총이익 4,751M / 매출 28,236M = 16.83%. 전년 동기 "
                 "3,878M / 22,496M = 17.24%. 매출총이익률은 사실상 유지됐다. "
                 "영업이익 감소분은 전액 영업비용 증가에서 나온다.",
         "source_ids": ["SRC-TSLA-OPEX-2026Q2"]},
        {"claim_id": "CLAIM-BEAR-CAPEX", "type": "FACT",
         "text": "2026 Q2 capex 5,789M USD, 전년 동기 2,394M 대비 +141.8%. "
                 "같은 분기 영업현금흐름 4,697M을 초과해 분기 FCF가 -1,092M로 "
                 "음수 전환했다. 회사는 벌어들인 현금보다 많이 쓰고 있다.",
         "source_ids": ["SRC-TSLA-10Q-2026Q2", "SRC-TSLA-BALANCE-2026Q2"]},
        {"claim_id": "CLAIM-BEAR-FCF", "type": "DERIVED",
         "text": "최근 4분기 TTM FCF 5,762M USD (영업현금흐름 18,685M − capex "
                 "12,923M). 직전 4분기 TTM FCF 5,586M 대비 +3.2%. 현재가가 "
                 "요구하는 연 38.2% 성장과 12배 차이가 난다.",
         "source_ids": ["SRC-TSLA-10Q-2026Q2", "SRC-TSLA-BALANCE-2026Q2"]},
        {"claim_id": "CLAIM-BEAR-PRICE", "type": "FACT",
         "text": "2026-07-22 종가 374.01 → 07-24 종가 313.03 (-16.3%). "
                 "07-23 하루 -14.5%, 거래량 115.6M주로 직전 평균의 약 3.7배. "
                 "급락일은 2026 Q2 10-Q 제출일(2026-07-23)과 일치한다.",
         "source_ids": ["SRC-TSLA-PRICE-20260724"]},
        {"claim_id": "CLAIM-BULL-001", "type": "HYPOTHESIS",
         "text": "R&D·capex 급증이 로보택시·Optimus·에너지 등 자본집약 신사업의 "
                 "선행투자라면, 투자 사이클 종료 후 FCF가 자동차 사업 성장률을 "
                 "크게 웃돌 수 있다. 강세 시나리오는 10년 연 40% FCF 성장을 "
                 "가정하며, 이는 현재가가 요구하는 38.2%를 넘는 수준이다.",
         "source_ids": ["SRC-TSLA-OPEX-2026Q2", "SRC-TSLA-10Q-2026Q2"]},
        {"claim_id": "CLAIM-BASE-001", "type": "INFERENCE",
         "text": "기준 시나리오는 10년 연 15% FCF 성장. 현재 관측되는 +3.2%보다 "
                 "5배 빠르지만 현재가가 요구하는 38.2%에는 크게 못 미친다. "
                 "AI 투자가 부분적으로 회수되는 경우다.",
         "source_ids": ["SRC-TSLA-10Q-2026Q2"]},
        {"claim_id": "CLAIM-ALT-CASH", "type": "INFERENCE",
         "text": "대안은 원화 예수금 유지. 기회비용을 세전 연 3.0%로 가정한다. "
                 "⚠️ 이 3.0%는 수집된 출처가 아니라 가정값이다. holdings.csv가 "
                 "현금 4,000만원을 '대기자금 - 전액 투자 가능'으로 정의한 것이 "
                 "대안 성립의 근거다.",
         "source_ids": ["SRC-PORTFOLIO-HOLDINGS"]},
    ],
    "thesis": {
        "status": "DRAFT",
        "change": "WEAKEN",
        "invalidation_triggered": False,
        "approved_by": None,
        "approved_at": None,
        "claim_ids": ["CLAIM-REV-001", "CLAIM-GM-001", "CLAIM-OPEX-001",
                      "CLAIM-BULL-001"],
    },
    "counter_thesis": {
        "text": "'실적이 나쁜 게 아니라 AI에 투자해서 떨어진다'는 해석은 손익계산서만 "
                "보면 맞다 — 매출총이익률은 유지됐고 영업이익 감소는 전액 R&D·SG&A "
                "증가분이다. 그러나 현금흐름표를 보면 이야기가 달라진다. Q2 capex가 "
                "전년 대비 +142% 늘어 영업현금흐름을 초과했고, 분기 FCF가 음수로 "
                "돌아섰다. 즉 '수익으로 AI에 투자'하는 단계를 이미 넘어 '버는 것보다 "
                "많이 쓰는' 단계다. 더 결정적인 것은 가격이다. 20% 급락한 315.70달러도 "
                "10년간 연 38.2% FCF 성장을 요구한다. 실제 TTM FCF 성장률은 3.2%다. "
                "우주 데이터센터·피지컬 AI가 전부 성공하는 강세 시나리오(연 40% 성장, "
                "요구치를 웃도는 수준)에서도 24개월 연율 수익률은 +5.9%로 IPS "
                "요구수익률 12%에 미달한다. 꿈이 실현돼도 이 가격에서는 수익이 부족하다.",
        "invalidation_signal":
            "다음 중 하나가 관측되면 이 반대 논지를 접는다 — (1) 2026 Q3 10-Q에서 "
            "분기 FCF가 양수로 복귀하고 TTM FCF가 8,000M USD를 넘는다. "
            "(2) 분기 capex가 4,000M USD 아래로 내려가면서 영업이익률이 5%를 회복한다. "
            "반대로 다음이 관측되면 반대 논지가 강화된다 — 2026 Q3에도 분기 FCF가 "
            "음수여서 2개 분기 연속 음수가 되거나, TTM FCF가 4,000M USD 아래로 하락.",
        "claim_ids": ["CLAIM-BEAR-CAPEX", "CLAIM-BEAR-FCF", "CLAIM-BEAR-PRICE"],
    },
    "expected_return": {
        "horizon_months": 24,
        "required_return_pct": 12.0,
        "methodology": "2단계 FCF 역산 DCF (scripts/reverse_dcf.py). 할인율 10%, "
                       "영구성장 3%, 1단계 10년. FCF는 2026 Q2 기준 TTM "
                       "5,762M USD, 순부채 -35,803M USD(순현금), "
                       "희석주식수 3,540M주.",
        "calculation_id": CALC["calculation_id"],
        "probability_weighted_pct": round(weighted, 4),
        "scenarios": {
            "bear": {"return_pct": S["bear"]["return_pct"], "probability": PROB["bear"],
                     "claim_ids": ["CLAIM-BEAR-CAPEX", "CLAIM-BEAR-FCF"]},
            "base": {"return_pct": S["base"]["return_pct"], "probability": PROB["base"],
                     "claim_ids": ["CLAIM-BASE-001", "CLAIM-REV-001"]},
            "bull": {"return_pct": S["bull"]["return_pct"], "probability": PROB["bull"],
                     "claim_ids": ["CLAIM-BULL-001"]},
        },
    },
    "alternatives": [
        {"instrument_id": "CASH_KRW", "expected_return_pct": 3.0,
         "measure": "PROBABILITY_WEIGHTED",
         "snapshot_id": "SNAPSHOT-20260727-TSLA",
         "claim_ids": ["CLAIM-ALT-CASH"]},
    ],
}

out = ROOT / "knowledge/research/analysis-tsla-20260727.json"
out.write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"작성: {out}")
