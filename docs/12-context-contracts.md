# 12. 컨텍스트 및 데이터 계약

## 1. 목적

배치, 계산 엔진, SKILL, 앱, 실행 계층이 같은 의미의 데이터를 사용하도록 하는 논리 계약이다. 실제 구현 시 언어별 타입과 API 스키마로 변환한다.

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
- 상태를 삭제하거나 덮어쓰지 않고 새 버전을 만든다.

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
      weight: number
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
  current_weight: number
  proposed_weight_range:
    min: number
    max: number
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

## 10. 사용자 결정

```yaml
UserDecision:
  decision_id: uuid
  recommendation_id: uuid
  decided_at: datetime
  decision: APPROVE | REJECT | MODIFY | DEFER
  modified_weight: number | null
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

## 14. 검토가 필요한 결정

- 통화 금액 표현 방식
- 확률 값의 허용 범위와 보정 버전 관리
- 투자논지 승인 단위
- 추천과 주문 의도의 만료시간
- 세금 추정치의 표현
- API 계약에 사용할 구체 스키마 언어
