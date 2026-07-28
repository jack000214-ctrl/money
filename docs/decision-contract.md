# 판단 계약

## 목적

모델이 바뀌어도 같은 질문을 받고 같은 종류의 답을 남기기 위한 계약이다. 설명용
최종 권위는 이 문서가 아니라 `cio/decision.py`와
`schemas/cio-decision.schema.json`이다.

## 입력: `cio-context/1.0.0`

`python3.13 -m cio prepare`가 다음 입력을 받는다.

```bash
python3.13 -m cio prepare <asset_id> \
  --price <검토가격> \
  --amount <최대사용금액> \
  --amount-currency <KRW|USD>
```

컨텍스트에는 다음만 포함한다.

- `request`: 자산, 가격·통화, 금액·통화, 기준시각과 질문
- `asset`: 시장, 상품 유형, 산업, 분석 렌즈, 방법론 후보, 벤치마크
- `validation`: PC가 확인한 준비 상태와 문제 목록
- `context`: 가격 요약, 벤치마크, 시장, 재무, 미국 제출 문서 색인, 적용 가능 계산,
  이벤트, ETF 구조, 포트폴리오 영향, 최근 판단 요약
- `research_requirements`: 로컬 데이터 밖에서 모델이 확인할 질문
- `evidence_index`: 로컬 산출물의 ID, 경로와 기준시점
- `model_contract`: 공통 prompt와 decision schema 위치

전체 공시·뉴스·과거 대화를 넣지 않는다. 모델은 evidence index와 요약으로 시작하고,
결론을 바꿀 수 있는 빈칸만 추가 조사한다.

## 출력: `cio-decision/1.0.0`

모델은 Markdown 설명을 섞지 않고 JSON 객체 하나를 반환한다. 필수 결론은 다음과
같다.

| 영역 | 필수 내용 |
|---|---|
| 판단 | `verdict`, 측정 metric, 기간, 승리·실패 정의와 각각의 수익률 문턱 |
| 확률 | 승리 확률, 분석 신뢰도, 약세·기준·강세 확률 |
| 기대수익 | 약세·기준·강세 및 확률가중 수익률 |
| 가격 계획 | 선호 진입가·범위, 손절가, 분할매도 시작가, 재질문 하단·상단·사건 |
| 금액 | 사용자가 제시한 한도 안의 제안 금액과 이유 |
| 논지 | 핵심 논지, 가장 강한 반대 논지, 관측 가능한 무효화 신호 |
| 근거 | 방법론·가정, 모르는 것, 추가 조사 요청, 출처와 evidence ID |
| 재현성 | context ID와 모델·prompt 버전 |

### Verdict

- `BUY_NOW`: 제시 가격에서 매수를 선호한다. 기존 보유가 있어도 추가 매수는 이
  verdict로 표현한다.
- `WAIT`: 자산은 검토 가치가 있지만 더 나은 가격 또는 사건을 기다린다.
- `AVOID`: 현재 논지·가격·위험 조합을 피한다.
- `INSUFFICIENT`: 조사 후에도 책임 있는 숫자 판단을 만들 정보가 부족하다.

`INSUFFICIENT`는 쉬운 탈출구가 아니다. 로컬 구조화 데이터 갱신과 모델의 직접
조사 후에도 결론을 지탱할 수 없을 때만 사용한다.

## 승리와 실패

확률에는 측정 가능한 사건이 필요하다. 모델은 매 판단마다 다음을 명시한다.

- `horizon_months`: 평가 기간
- `win_definition`과 `win_return_threshold_pct`
- `failure_definition`과 `failure_return_threshold_pct`
- `win_probability_pct`: 위 승리 정의가 관측될 확률

`outcome_metric`은 v1에서
`TERMINAL_TOTAL_RETURN_ASSET_CURRENCY`로 고정한다. 즉 승리·실패는 입력 가격
기준 평가기간 말의 자산 통화 총수익률이며, 조정종가로 분배를 반영하되 환율·세금·
수수료·슬리피지는 제외한다. 자유문장 정의가 이 측정 기준을 다른 사건으로 바꿀 수
없다. 승리 문턱은 실패 문턱보다 커야 한다.

## 시나리오와 산술

- 약세·기준·강세 확률은 모두 0보다 크고 합이 100이어야 한다.
- 수익률은 `bear <= base <= bull`이어야 한다.
- `probability_weighted`는 세 수익률과 확률의 가중합이어야 한다.
- 일반 verdict에서는 확률과 수익률을 비워둘 수 없다.
- `INSUFFICIENT`에서는 확률·기대수익·가격 계획·제안 금액을 모두 `null`로 둔다.

PC는 이 관계를 검증할 뿐 시나리오와 확률을 대신 선택하지 않는다.

## 가격 계획

신규 매수 관점의 기계적 관계는 다음과 같다.

```text
stop_price < entry_range_low
entry_range_low <= preferred_entry <= entry_range_high
entry_range_high < scale_out_start
reask_below < 사용자가 제시한 가격 < reask_above
```

손절가는 단순 변동성 숫자가 아니라 `invalidation_signals`와 연결해 설명한다.
분할매도 시작가는 전량 매도 목표가가 아니다. 재질문 사건에는 실적, 공시, 임상,
ETF 구성·분배 정책 등 가격 외 판단 변경 조건도 포함한다.

제안 금액은 사용자가 제시한 금액을 넘을 수 없고 입력 금액 통화와 같아야 한다.
`BUY_NOW`는 0보다 큰 현재 집행액, `WAIT`와 `AVOID`는 0, `INSUFFICIENT`는 null이다.

## 방법론 분기

`asset.analysis_lens`와 `valuation_family`는 후보이며 모델이 적합성을 다시 판단한다.

- 일반 주식: 현금창출, 경쟁우위, 재무안전, 상대·절대가치
- 반도체 등 사이클주: 정상화 이익, 재고·가격·공급·capex와 사이클 위치
- 임상·적자 바이오: 현금 런웨이, 희석, 파이프라인 단계와 확률가중 가치
- 광역 ETF: 지수 방법론, 구성, 보수, 추적오차, 밸류에이션과 대안
- 파생 ETF: 기초자산, 옵션 구조, 상승 제한, NAV 침식과 분배 원천

하나의 DCF를 모든 자산에 강제하지 않는다. 선택한 방법과 적합한 이유, 중요한
가정을 `method`에 남긴다.

## 근거와 출처

`evidence_refs`와 가장 강한 반대 논지의 근거는 다음 중 하나를 가리켜야 한다.

- context의 `evidence_index`에 있는 로컬 source ID
- 모델이 직접 조사해 `sources`에 등록한 source ID

모델 조사 source는 식별자, 유형, 위치, 공개시각과 회수시각을 갖는다. 출처 없는
핵심 숫자와 반대 논지는 공식 판단으로 저장하지 않는다. 모델 source ID는 로컬
evidence ID를 재정의할 수 없고 `published_at <= retrieved_at <= decision.created_at`
순서를 지켜야 한다.

모델이 직접 조사한 `sources`는 URL과 시각을 보존하는 **citation metadata**다.
프로그램은 URL 형식과 시각 순서를 검사하지만 페이지의 실재·당시 내용까지
보존했다고 주장하지 않는다. 정확한 본문 재현이 필요한 결정 핵심 자료는 전용
수집기로 로컬 evidence를 만든 뒤 새 context에 포함해야 한다.

## 검증과 저장

```bash
python3.13 -m cio validate decision.json  # 선택: 쓰기 없는 사전검사
python3.13 -m cio record decision.json
```

검증은 context·asset 일치, 시각 순서, 정확한 필드 집합, 유한 숫자, 확률·수익률
산술, 가격 관계, 제안 금액, evidence 참조와 내용 지문을 확인한다. `BLOCKED`
context에서는 숫자 verdict를 기록할 수 없으며 null 수치의 `INSUFFICIENT`만
허용한다. 공식 모델 시도는 `record`를 직접 실행해야 깨진 JSON을 포함한 실패도
quarantine 자산으로 남는다. `assessment_id`는 내용의 지문으로 프로그램이
계산하므로 모델은 생략해도 된다.

`record`가 검증을 통과한 판단은 `knowledge/assessments/<asset_id>/`에 저장된다.
실패한 출력은 공식 기억에 섞지 않고 `knowledge/quarantine/<asset_id>/`에 오류와
함께 보존해 모델 평가 자료로 사용한다.
