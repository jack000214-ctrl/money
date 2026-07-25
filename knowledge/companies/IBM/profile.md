---
schema_version: "1.0.0"
company_id: US-IBM
name: International Business Machines
ticker: IBM
exchange: NYSE
cik: "0000051143"
sector: IT
industry: 엔터프라이즈 소프트웨어·인프라·컨설팅
updated_at: 2026-07-26
updated_by: company-research
position:
  held: true
  quantity: 25
  weight_pct_stocks: 7.55
  weight_pct_total_assets: 2.58
  value_krw: 7852025
  avg_cost_krw: 336388
  price_krw: 314081
  unrealized_pnl_krw: -557676
thesis:
  status: DRAFT
  owner: AI_DRAFT
  approved_by_user_at: null
evidence_level: VERIFIED_FINANCIALS
data_freshness:
  news_scanned_at: 2026-07-26
  filings_verified_at: 2026-07-26
---

# IBM

> ✅ 재무는 SEC EDGAR 원문(XBRL)에서 검증됨. `scripts/edgar_fetch.py`

## 사건 — 2026-07-14 사상 최악의 하루

**주가 -25%.** 1987-10-19(-23.7%)를 넘어선 IBM 역사상 최대 낙폭.

2026-07-14에 2분기 잠정실적 경고를 냈고, 2026-07-22에 정식 발표하며 연간 가이던스를 낮췄다.

| 항목 | 실적 | 비고 |
|---|---|---|
| 2Q26 매출 | $17.16B | 컨센 $17.86B 대비 **-$660M** |
| 2Q26 매출 YoY | **+1.1%** | 1Q26 +9.5%에서 급감 |
| 조정 EPS | $2.93 | 컨센 하회 |
| 소프트웨어 | $7.76B | **+5%** |
| 인프라 | $3.84B | **-7%** |
| Z 메인프레임 | — | **-42%** |
| FY26 가이던스 | 고정환율 매출성장 4~5% | 기존 ">5%" 에서 하향 |

## 분기 매출 (EDGAR 원문, 백만 USD)

| 기간 | 매출 | YoY |
|---|---|---|
| 2026 Q2 | 17,162 | **+1.1%** |
| 2026 Q1 | 15,917 | +9.5% |
| 2025 Q3 | 16,331 | +9.1% |
| 2025 Q2 | 16,977 | +7.7% |
| 2025 Q1 | 14,541 | +0.5% |
| 2024 Q3 | 14,968 | +1.5% |

※ 4분기(10~12월)는 10-K가 12개월 구간으로 보고해 3개월 시리즈에 잡히지 않는다.
DART의 4분기 문제와 동일하며, 필요하면 연간에서 1~3분기를 빼서 역산한다.

## 문제의 성격 — 경쟁 열위가 아니라 사이클 반대편

보도된 원인은 "IBM이 AI 경쟁에서 졌다"가 아니다.

**고객이 자본지출을 IBM 소프트웨어·인프라에서 GPU·서버·스토리지·메모리로 옮겼다.**

즉 AI 자본지출이 IBM 예산을 **빼앗고 있다.** 이것이 중요한 이유는 회복 조건이 다르기 때문이다.

- 경쟁 열위라면 → 제품 개선으로 회복 가능
- 예산 잠식이라면 → **AI 자본지출 사이클이 진정되기 전까지 구조적으로 안 풀린다**

## 재무 상태 (EDGAR, 2026-06-30)

| 항목 | 금액 |
|---|---|
| 현금성자산 | **$7,172M** |
| 장기차입금 | **$56,212M** |
| 영업현금흐름 (최근 4분기) | $17,481M |

### ⚠️ "현금이 많은 회사"는 사실이 아니다

순차입금이 약 **$49B**다. IBM은 현금 부자가 아니라 **차입이 많은 회사**다.

정확한 표현은 "현금이 많다"가 아니라 **"현금창출력이 좋다"**이다 (연 $17.5B).
배당과 이자를 감당하기에 충분하지만, 위기에 쓸 실탄이 두둑한 상태와는 다르다.

## 양자컴퓨터 — 규모와 시점을 확인해야 한다

| 주장 | 사실 |
|---|---|
| "미국정부 1000억달러 양자 투자" | **총 $2B, IBM 몫 $1B** (100배 차이) |
| 출처 | 상무부, CHIPS법 재원. 정부가 소수지분을 취득 |
| **발표 시점** | **2026-05-21** |

**핵심: 양자 호재는 폭락(7/14) 이전에 이미 공개됐다.** 새 촉매가 아니다.

그리고 $1B은 IBM 연매출 약 $63B의 1.6%다. 여러 해에 걸쳐 들어오면 연 0.3% 수준으로,
주가를 움직일 규모가 아니다. IBM 자체 투자 계획($10B+/5년)은 **비용**이지 매출이 아니다.

양자는 2029년 내결함성 컴퓨터 목표를 두고 있다. **투자 기간이 최소 3~5년인 논지다.**

## 무효화 조건 (초안)

| 조건 | 관측 지표 | 임계값 | 상태 |
|---|---|---|---|
| 소프트웨어 성장 둔화 | 소프트웨어 매출 YoY | 2개 분기 연속 +3% 미만 | NOT_TESTED (현재 +5%) |
| 영업현금흐름 훼손 | 최근 4분기 OCF | $15B 미만 | SAFE ($17.5B) |
| 가이던스 추가 하향 | 분기 가이던스 | 재하향 시 | **WARNING** (1회 발생) |
| 인프라 하락 지속 | 인프라 매출 YoY | 2개 분기 연속 -5% 초과 | NOT_TESTED |
| AI 자본지출 잠식 지속 | 전체 매출 YoY | 2개 분기 연속 +3% 미만 | NOT_TESTED (2Q26 +1.1%) |

## 미해결 질문

- Z 메인프레임 -42%가 사이클(z17 출시 주기)인가 구조적 이탈인가 — **판단에 결정적**
- 대형 계약 지연(deals slip)이 취소인가 이연인가
- AI 자본지출 사이클이 언제 진정되는가 (아무도 모른다 → 논지에 넣으면 안 된다)
- 배당 지속 가능성 (순차입 $49B 상태에서)

## 출처

- [CNBC — 사상 최악의 하루, 2분기 경고](https://www.cnbc.com/2026/07/14/ibm-warns-second-quarter-earnings-fell-short-of-expectations.html)
- [CNBC — 2분기 실적 보고](https://www.cnbc.com/2026/07/22/ibm-q2-earnings-report-2026.html)
- [Quartz — 연간 매출 가이던스 하향](https://qz.com/ibm-cuts-full-year-revenue-outlook-second-quarter-earnings-miss-072326)
- [CNN — 미국 정부 $2B 양자 지원, IBM $1B](https://www.cnn.com/2026/05/21/business/ibm-quantum-computing-firms-grants)
- [IBM 뉴스룸 — 양자에 $10B 투자](https://newsroom.ibm.com/2026-06-02-ibm-commits-more-than-10-billion-to-quantum-computing,-funding-its-roadmap-from-todays-leading-systems-to-the-worlds-first-fault-tolerant-quantum-computers)
- [Motley Fool — 잠정실적은 나빴지만 끔찍하진 않았다](https://www.fool.com/investing/2026/07/19/ibm-stock-q2-preliminary-warning-ai-capex/)
