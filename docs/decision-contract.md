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
- `context`: 가격 요약, 벤치마크, 시장, 재무, 공시 색인(DART·SEC), 적용 가능 계산,
  이벤트, ETF 구조, 포트폴리오 영향, 최근 판단, 미해결 수집 요청, 보존한 웹 원문
- `research_requirements`: 로컬 데이터 밖에서 모델이 확인할 질문
- `evidence_index`: 로컬 산출물의 ID, 경로와 기준시점
- `model_contract`: 공통 prompt와 decision schema 위치

전체 공시·뉴스·과거 대화를 넣지 않는다. 모델은 evidence index와 요약으로 시작하고,
결론을 바꿀 수 있는 빈칸만 추가 조사한다.

### 판단을 이어 붙이는 블록

한 종목을 여러 번 묻는 것이 이 시스템의 정상 사용법이므로, 컨텍스트는 직전
판단을 이어받을 수 있을 만큼만 함께 싣는다.

| 블록 | 내용 | 왜 |
|---|---|---|
| `prior_assessments[].carried_forward` | 직전 판단의 논지·가격계획·무효화 신호 | 모델이 자기 계획을 확인하고 시작하게 한다 |
| `prior_assessments[].price_vs_prior_plan` | 그 문턱과 현재 종가의 산술 대조 | 문턱 도달 여부는 PC가 계산한다 |
| `prior_assessments[].usable: false` | 현재 계약으로 재검증되지 않는 판단 | 계약이 바뀌었을 때 기억이 조용히 사라지지 않게 한다 |
| `open_research_requests` | 아직 처리되지 않은 모델의 수집 요청 | 요청이 편지함에서 썩지 않게 한다 |
| `web_snapshots` | 모델이 이전에 보존한 웹 원문 목록 | 같은 조사를 반복하지 않게 한다 |
| `local_documents` | 검색 가능한 원문 목록과 미수집 공시 수 | 100만 자 문서를 넣지 않고 존재만 알린다 |
| `fundamentals.reporting_lag` | 로컬 재무 기준일 이후의 공시 | 신선도를 시계가 아니라 결산 캘린더로 잰다 |

`carried_forward`는 그대로 따르라는 명령이 아니다. 같은 자리를 처음 보는 것처럼
다시 계산하지 말고, 판단이 바뀌었으면 무엇이 바뀌었는지 말하라는 요구다.

packet이 예산(`MAX_CONTEXT_JSON_BYTES`)을 넘으면 서술부터 버리고 가격 문턱과
무효화 신호를 남긴다. 버린 항목은 `validation.optional_gaps`에
`TRIMMED_FOR_BUDGET`으로 기록되며, 원문은 `knowledge/assessments/`에 그대로 있다.

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
| 보유분 행동 | `action`의 방향·수량·매도 사유와 이유 |
| 논지 | 핵심 논지, 가장 강한 반대 논지, 관측 가능한 무효화 신호 |
| 조사 배분 | `drivers` — 이익 동인과 비중, 각 동인을 조사했는지 건너뛰었는지 |
| 근거 | 방법론·가정, 모르는 것, 추가 조사 요청, 출처와 evidence ID |
| 재현성 | context ID와 모델·prompt 버전 |

### `drivers` — 조사를 배분한다

결론 전에 이익 동인을 나열하고 **비중의 합이 100이 되도록 배분**한다. 각 동인은
조사했거나(`researched: true` + `finding`) 건너뛰었거나(`researched: false` +
`skipped_reason`) 둘 중 하나여야 하며, 둘 다이거나 둘 다 아닐 수 없다.

합을 100으로 강제하는 것이 이 계약의 요점이다. **"다 중요하다"는 배분이 아니다.**
동인이 여럿인 회사에서 순서를 정하지 않으면 이익에 거의 영향이 없는 이야기에
판단의 무게가 실린다 — 어떤 사업부가 영업이익의 5%인지 50%인지 모른 채 그
사업부의 최신 뉴스부터 읽는 것이 그 실패다.

`basis`는 그 비중의 근거다. 부문별 매출과 비중은 사업보고서에 있으므로
`cio search`로 확인해 쓴다.

건너뛰는 것은 정당하다. 모든 동인을 매번 조사할 수는 없다. 다만 건너뛴 사실과
이유가 다음 컨텍스트의 `carried_forward.drivers`로 넘어가므로, 같은 동인을 계속
미루고 있으면 그것이 드러난다.

### `research_requests` — 편지함이 아니라 대기열

모델이 "로컬에 이것이 필요하다"고 적는 필수 필드다. 이 요청은 assessment 안에서
끝나지 않는다.

```bash
python3.13 -m cio requests <asset_id>                    # 미해결·해결 목록
python3.13 -m cio requests <asset_id> \
  --resolve REQ-3e547a42822d --state DONE --note "무엇을 했는지"
```

요청의 신원은 정규화한 본문의 sha256이다. 같은 요청을 여러 판단에서 반복하면
`asked_count`가 올라가고, 그 횟수가 우선순위 신호가 된다. 미해결 요청은 다음
컨텍스트의 `open_research_requests`로 되돌아온다.

**같은 뜻의 요청은 문장이 달라도 한 묶음이 된다.** 모델은 매 판단에서 같은
요청을 조금씩 다르게 쓴다 — 마침표 하나, `재수집해`와 `재수집하고`. 본문 해시만
쓰면 목록이 사실상 같은 말로 채워지고 `asked_count`가 언제나 1이 되어 반복
신호가 죽는다. 그래서 구두점과 공백을 버린 표기의 유사도가 0.62 이상이면 묶는다.
실제 데이터에서 같은 뜻의 요청은 0.69 이상, 서로 다른 요청은 0.45 미만에 몰려
그 사이가 비어 있다.

30자 미만은 유사도를 쓰지 않고 정확히 같을 때만 묶는다. 짧은 문자열은 유사도가
부풀려져 `요청 0`과 `요청 1`이 0.8로 나오기 때문이다.

묶음의 대표는 **가장 먼저 요청된 문장**이라 나중에 비슷한 요청이 들어와도 ID가
바뀌지 않는다. 해결은 대표에 걸리며 묶음 안 어느 문장으로도 닫을 수 있고, 한 번
닫으면 나중의 재표현도 미해결로 돌아오지 않는다. 묶인 문장은 목록에 `↔`로 함께
보여, 잘못 묶였으면 눈에 띄게 한다.

**해결 판정은 사용자만 한다.** 모델이 자기 요청을 닫을 수 있으면 "요청했는데
아직 없다"는 신호가 의미를 잃기 때문이다. `state`는 `DONE`(만들었다),
`DECLINED`(만들지 않기로 했다), `SUPERSEDED`(다른 것으로 대체됐다)다.

### 근거 보존과 검색

모델이 조사한 원문은 인용 전에 로컬에 남긴다.

```bash
python3.13 -m cio snapshot <URL> --asset <asset_id> --label <이름> --note "왜"
python3.13 -m collectors.dart document <14자리 접수번호>
```

보존한 원문은 검색해서 필요한 자리만 쓴다.

```bash
python3.13 -m cio docs <asset_id>
python3.13 -m cio search <asset_id> <검색어> [--context N] [--limit N] [--fixed]
```

부문별 매출·주요 매출처·수주잔고·생산능력처럼 **회사 구조에 관한 사실은 웹보다
로컬 원문이 먼저다.** 부문 비중을 모른 채 특정 사업부의 최신 뉴스부터 읽으면
이익에 거의 영향이 없는 이야기에 판단의 무게를 싣게 된다.

`snapshot`은 받은 바이트를 그대로 내용 지문으로 저장한다. 광고나 마크업을
걷어내지 않는다 — 페이지 구조는 언제든 바뀌지만 저장한 바이트는 바뀌지 않고,
무엇이 본문인지는 그 파일을 읽는 모델이 판단한다. 응답이 프레임 껍데기면
`shell_page_warning`이 붙으며 그 스냅샷은 내용 근거가 아니다.

### Verdict

- `BUY_NOW`: 제시 가격에서 매수를 선호한다. 기존 보유가 있어도 추가 매수는 이
  verdict로 표현한다.
- `WAIT`: 자산은 검토 가치가 있지만 더 나은 가격 또는 사건을 기다린다.
- `AVOID`: 현재 논지·가격·위험 조합을 피한다.
- `INSUFFICIENT`: 조사 후에도 책임 있는 숫자 판단을 만들 정보가 부족하다.

`INSUFFICIENT`는 쉬운 탈출구가 아니다. 로컬 구조화 데이터 갱신과 모델의 직접
조사 후에도 결론을 지탱할 수 없을 때만 사용한다.

### Action — 가진 것을 어떻게 할 것인가

`verdict`는 **새 자금**에 대한 판단이고 `action`은 **이미 보유한 수량**에 대한
판단이다. 두 질문은 다르며 동시에 참일 수 있다. 더 사지 않지만 팔 이유도 없으면
`WAIT` + `HOLD`다.

`direction`은 `ADD | HOLD | TRIM | EXIT`이며 매도(`TRIM`·`EXIT`)에는 수량과
`sell_reason`이 필요하다. 사유는 두 갈래로 나뉘고, 이 구분이 사후 평가의 집계
단위가 된다.

| 갈래 | 사유 | 규칙 |
|---|---|---|
| 판단 기반 | `THESIS_BROKEN`, `VALUATION`, `BETTER_OPPORTUNITY` | 자산 자체에 대한 판정이라 `BUY_NOW`와 함께 쓸 수 없다 |
| 제약 기반 | `CONCENTRATION`, `RISK_BUDGET`, `LIQUIDITY_NEED` | 자산이 좋아도 낼 수 있으나 제약임을 밝혀야 한다 |

**밸류에이션과 논지가 매도를 주도한다.** 비중·변동성·시장 국면은 노출 측정치이지
그 자체로 매도 사유가 아니다. 사용자가 명시한 한도가 없는 한 비중을 이유로 매도를
권하지 않으며, 노출은 숫자로 보여주고 판단은 사용자에게 맡긴다. 제약 때문에 파는
것이 옳더라도 그것을 밸류에이션 판정처럼 포장하지 않는다.

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
