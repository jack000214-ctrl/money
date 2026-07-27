# 12. 컨텍스트 및 데이터 계약

## 1. 목적

배치, 계산 엔진, SKILL, 앱, 실행 계층이 같은 의미의 데이터를 사용하도록 하는
논리 계약이다. 현재 최소 실행 계약은 `scripts/contracts.py`에 구현돼 있으며,
`scripts/validate_analysis.py`가 저장 전 검증 경계다.

## 2. 공통 규칙

### 필수 메타데이터

```yaml
metadata:
  id: uuid
  schema_version: string
  created_at: datetime
  as_of: datetime
  source_snapshot_ids: [uuid]
  producer:
    component: string
    version: string
```

### 불변 조건

- 통화가 필요한 숫자는 `currency`를 가진다.
- 단위가 필요한 숫자는 `unit`을 가진다.
- 기간 값은 시작일과 종료일을 가진다.
- 추정과 실제를 구분한다.
- 모든 공식 분석 결과는 하나 이상의 스냅샷을 참조한다.
- 모든 주장 출처 ID는 해당 분석 스냅샷의 출처 목록에 포함돼야 한다.
- 해시는 64자리 16진수 SHA-256이어야 한다.
- 상태를 삭제하거나 덮어쓰지 않고 새 버전을 만든다.
- 스키마 검증에 실패한 분석은 새 파일을 만들거나 기존 파일을 덮어쓰지 않는다.

## 3. 근거 참조

```yaml
EvidenceRef:
  evidence_id: uuid
  source_type: FILING | MARKET_DATA | ACCOUNT | MACRO | USER_INPUT | CALCULATION
  source_id: string
  source_version: string
  published_at: datetime | null
  locator:
    url: string | null
    document_section: string | null
    table: string | null
    row: string | null
  excerpt: string | null
  content_hash: string
```

`excerpt`는 편의를 위한 복사본이며 원문 식별자와 해시가 진실의 기준이다.

## 4. 측정값

```yaml
MetricValue:
  metric_id: string
  label: string
  value: number | null
  unit: string
  currency: string | null
  period:
    start: date | null
    end: date
  basis: ACTUAL | ESTIMATE | DERIVED
  calculation_id: string | null
  evidence_refs: [uuid]
  quality: VALID | SUSPECT | QUARANTINED
```

## 5. 포트폴리오 스냅샷

```yaml
PortfolioSnapshot:
  metadata: {}
  portfolio_id: uuid
  base_currency: KRW
  cash_flows_since_previous: []
  positions:
    - instrument_id: uuid
      quantity: number
      market_price: number
      market_value: number
      currency: string
      weights:
        total_assets:
          value: number
          denominator_value: number
        equity_sleeve:
          value: number
          denominator_value: number
        risk_assets:
          value: number
          denominator_value: number
      cost_basis: number | null
      unrealized_pnl: number | null
      thesis_id: uuid | null
      data_quality: VALID | STALE | SUSPECT
  exposures:
    asset_class: {}
    sector: {}
    country: {}
    currency: {}
    strategy: {}
  performance:
    time_weighted_return: number | null
    money_weighted_return: number | null
    benchmark_return: number | null
  risk:
    max_drawdown: number | null
    concentration: {}
    policy_breaches: []
```

## 6. 기업 투자논지

```yaml
CompanyThesis:
  metadata: {}
  thesis_id: uuid
  previous_version_id: uuid | null
  company_id: uuid
  instrument_id: uuid
  status: DRAFT | REVIEWED | APPROVED | INVALIDATED | SUPERSEDED
  summary: string
  business_model: string
  variant_view: string
  key_kpis:
    - metric_id: string
      expected_range: {}
      review_date: date
  strengths: []
  risks: []
  scenarios:
    bull: {}
    base: {}
    bear: {}
  invalidation_conditions:
    - condition_id: uuid
      description: string
      observable_metric: string | null
      threshold: string | null
      status: NOT_TESTED | SAFE | WARNING | TRIGGERED
  catalysts: []
  unanswered_questions: []
  evidence_refs: [uuid]
  owner: USER | AI_DRAFT
  approved_by_user_at: datetime | null
```

## 7. 공시 영향 분석

```yaml
FilingImpact:
  metadata: {}
  filing_id: uuid
  company_id: uuid
  thesis_id: uuid | null
  materiality: LOW | MEDIUM | HIGH | CRITICAL
  event_type: string
  facts:
    - statement: string
      evidence_refs: [uuid]
  derived_changes:
    - metric_id: string
      before: number | null
      after: number | null
      calculation_id: string
  inferences: []
  counterevidence: []
  thesis_effect: STRENGTHEN | NEUTRAL | WEAKEN | POSSIBLY_INVALIDATE | UNKNOWN
  next_actions: []
  confidence: number
  requires_user_review: boolean
```

## 8. 전략 신호

```yaml
StrategySignal:
  metadata: {}
  strategy_id: string
  strategy_version: string
  instrument_id: uuid
  signal: ELIGIBLE | WATCH | POSITIVE | NEUTRAL | NEGATIVE | EXCLUDED
  feature_values: [MetricValue]
  rationale_codes: []
  expected_horizon: string
  valid_until: datetime
```

전략 신호는 추천 또는 주문이 아니다.

## 9. 행동 후보

```yaml
Recommendation:
  metadata: {}
  recommendation_id: uuid
  portfolio_id: uuid
  instrument_id: uuid
  thesis_id: uuid
  strategy_signal_ids: [uuid]
  state: DRAFT | PROPOSED | BLOCKED | APPROVED | REJECTED | EXPIRED
  action: NO_ACTION | RESEARCH_REQUIRED | WATCH | ENTER_PROPOSAL |
          ADD_PROPOSAL | HOLD | TRIM_PROPOSAL | EXIT_PROPOSAL
  current_weights:
    total_assets: {}
    equity_sleeve: {}
    risk_assets: {}
  proposed_weight_ranges:
    total_assets:
      min: number
      max: number
      denominator_value: number
    equity_sleeve:
      min: number
      max: number
      denominator_value: number
    risk_assets:
      min: number
      max: number
      denominator_value: number
  reasons: []
  counterarguments: []
  scenarios:
    bull: {}
    base: {}
    bear: {}
  invalidation_conditions: []
  portfolio_impact:
    concentration_delta: {}
    liquidity_assessment: string
    estimated_cost: {}
    estimated_tax: {}
  policy_checks:
    - policy_id: string
      status: PASS | WARN | FAIL
      detail: string
  data_freshness: {}
  confidence:
    value: number
    calibration_version: string
  valid_until: datetime
  evidence_refs: [uuid]
```

정책 검사에서 하나라도 `FAIL`이면 상태는 `BLOCKED`다.

현재 구현은 다음을 추가로 강제한다.

- 분석에는 논지 변화, 약세·기준·강세 기대수익, 계산 ID, 하나 이상의 대안이 필요하다.
- 출처·주장 ID 중복과 끊어진 참조를 거부한다.
- 분석·출처 회수·스냅샷 생성 시각의 역전을 거부한다.
- `NaN`과 무한대를 포함한 비유한 숫자를 거부한다.
- 저장은 검증 성공 뒤 같은 디렉터리의 임시 파일을 원자적으로 교체한다.

## 10. 사용자 결정

```yaml
UserDecision:
  decision_id: uuid
  recommendation_id: uuid
  decided_at: datetime
  decision: APPROVE | REJECT | MODIFY | DEFER
  modified_weights:
    total_assets: number | null
    equity_sleeve: number | null
    risk_assets: number | null
  reason_code: string
  note: string | null
  policy_override_requested: boolean
  confirmation_method: string
```

정책 변경과 개별 예외 승인은 구분한다.

## 11. 주문 의도

```yaml
OrderIntent:
  metadata: {}
  order_intent_id: uuid
  recommendation_id: uuid
  user_decision_id: uuid
  account_id: uuid
  instrument_id: uuid
  side: BUY | SELL
  order_type: LIMIT | MARKET
  quantity: number
  limit_price: number | null
  currency: string
  idempotency_key: string
  expires_at: datetime
  risk_check_status: PENDING | PASSED | BLOCKED
  user_confirmation_status: PENDING | CONFIRMED | REJECTED
```

주문 의도는 실행 게이트웨이가 재검증한 후에만 외부 주문으로 변환된다.

## 12. SKILL 컨텍스트 패키지

```yaml
SkillContext:
  request:
    intent: string
    question: string
    requested_as_of: datetime
  user_policy_excerpt: {}
  company: {}
  portfolio_exposure: {}
  financial_metrics: [MetricValue]
  market_metrics: [MetricValue]
  active_thesis: CompanyThesis | null
  relevant_filings: [EvidenceRef]
  prior_analyses: []
  data_freshness: {}
  allowed_tools: []
  prohibited_actions:
    - CHANGE_POLICY
    - ACCESS_SECRETS
    - PLACE_ORDER
```

## 13. SKILL 분석 결과

```yaml
SkillAnalysis:
  metadata: {}
  classification: NO_ACTION | REVIEW | RESEARCH_REQUIRED
  summary: string
  facts: []
  derived_findings: []
  inferences: []
  hypotheses: []
  unknowns: []
  counterarguments: []
  thesis_impact: {}
  next_checks: []
  evidence_refs: [uuid]
  confidence: number
  validator_results: []
```

## 14. 현재 실행 파일

| 계약 | 파일 |
|---|---|
| IPS | `knowledge/portfolio/ips.json` |
| 분석 예시 | `knowledge/research/analysis-template.json` |
| 계산 산출물 | `knowledge/calculations/CALC-*.json` |
| 검증기 | `scripts/contracts.py`, `scripts/validate_analysis.py` |
| 밸류에이션 계산기 | `scripts/reverse_dcf.py` |
| 매매 점검 결과 | `scripts/trade_check.py` |

IPS는 2026-07-27에 `APPROVED`가 됐다. 승인 IPS는 위 문서의 앞 절들이 개념적으로
기술한 값 중 아래를 **반드시** 채워야 하며, 비어 있으면 검사가 조용히 꺼지는
대신 계약 검증이 실패한다.

### 14.1 스키마 1.1.0 — 기대수익 계약

`expected_return`은 숫자 세 개의 평평한 묶음이 아니라 **시나리오 객체**다.

```yaml
expected_return:
  horizon_months: int          # 가격이 가치로 수렴한다고 보는 기간
  required_return_pct: number  # 승인 IPS의 요구수익률과 일치해야 한다
  methodology: string
  calculation_id: "CALC-<20자리 16진수>"   # 계산 산출물을 가리킨다
  probability_weighted_pct: number         # Σ(확률 × 수익률)과 일치해야 한다
  scenarios:
    bear: {return_pct, probability, claim_ids}
    base: {return_pct, probability, claim_ids}
    bull: {return_pct, probability, claim_ids}
```

강제되는 것:

- 세 시나리오가 모두 있어야 하고, 그 외 이름은 거부된다
- `bear ≤ base ≤ bull`
- 확률 합이 1이고, 확률 0인 시나리오는 없다 (일어나지 않는 시나리오는
  시나리오가 아니다)
- **각 시나리오에 근거 claim이 하나 이상** 있어야 한다. 특히 `bear`에 근거가
  없으면 하방은 지어낸 숫자다
- `probability_weighted_pct`는 적힌 값이 아니라 계산된 값과 대조된다
- `return_pct`는 `calculation_id`가 가리키는 산출물과 일치해야 한다

`return_pct`는 **연율**이다. IPS 요구수익률이 연율이므로 같은 자로 재야 한다.

### 14.2 반대 논지

```yaml
counter_thesis:
  text: string                  # 이 논지에 대한 가장 강한 반박
  invalidation_signal: string   # 관측 가능한 지표와 임계값
  claim_ids: [claim_id]
```

필수 항목이다. 07번 문서 §8이 "가장 강한 반대 근거"를 추천 필수 항목으로 정해
놓았으나 계약에는 없었고, 그래서 실제로는 아무도 쓰지 않았다.

### 14.3 계산 산출물

```yaml
Calculation:
  schema_version: "1.1.0"
  calculation_id: "CALC-<20자리 16진수>"
  model: string                # reverse_dcf
  model_version: string
  instrument_id: string
  created_at: datetime
  source_ids: [source_id]      # 입력 숫자의 출처
  inputs: {}
  outputs:
    implied_growth_pct: number # 현재가가 요구하는 성장률
    scenarios: {bear, base, bull → return_pct}
```

`calculation_id`는 **모델·입력·산출값 전체의 SHA-256 지문**이다. 파일에서
무엇을 고치든 ID가 어긋나고 검증에서 걸린다. 입력만 지문화하면 가정을 그대로
두고 결과 숫자만 고칠 수 있으므로 산출값도 포함한다.

### 14.4 IPS 추가 필드

```yaml
decision_policy:
  return_measure: BASE | PROBABILITY_WEIGHTED
  max_bear_loss_contribution_pct: number
```

`return_measure`는 기대수익을 무엇으로 잴지 정한다. `alternatives[].measure`가
이와 다르면 비교하지 않고 `ALTERNATIVE_MEASURE_MISMATCH`로 남긴다.

`max_bear_loss_contribution_pct`는 하방 예산이다.
`거래 후 포지션 × |약세 수익률| ÷ 총자산`이 이 값을 넘으면 차단한다.
구속하지 않으려면 `null`이 아니라 큰 값을 적는다 — 꺼져 있는 검사와 느슨한
검사는 다르다.

## 15. 검토가 필요한 결정

- 통화 금액 표현 방식
- 확률 값의 허용 범위와 보정 버전 관리
- 투자논지 승인 단위
- 추천과 주문 의도의 만료시간
- 세금 추정치의 표현
- 수동 검증기를 JSON Schema 또는 타입 생성 계약과 병행할지
