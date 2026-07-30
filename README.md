# Personal CIO

한·미 주식과 ETF에 대해 다음 질문에 답하기 위한 개인용 시스템이다.

> 이 종목을 이 가격에 이 금액만큼 사면 승리할 가능성이 있는가? 기대수익은
> 얼마이며, 나라면 얼마에 들어가고 어디서 손절·분할매도·재검토할 것인가?

좋은 모델일수록 더 나은 판단을 하도록 데이터와 과거 평가를 로컬 자산으로 남긴다.
GPT와 Claude를 포함한 모델은 교체할 수 있고, 같은 컨텍스트와 출력 계약을 사용한다.

## 역할

| 주체 | 책임 |
|---|---|
| 사용자 | 종목·검토 가격·사용 가능 금액을 고르고 실제 주문을 최종 결정한다 |
| 로컬 PC | 공식 데이터 수집, 저장, 결정론적 계산, 신선도·스키마·산술 검증을 맡는다 |
| 모델 | 부족한 정보를 조사하고 투자 방법론·확률·가격 계획·최종 투자 판단을 만든다 |

PC의 `validation = PASS | BLOCKED`는 **데이터 준비 상태**다. 모델의
`verdict = BUY_NOW | WAIT | AVOID | INSUFFICIENT`가
**투자 판단**이다. 둘을 합치거나 PC 검사 결과를 모델 판단처럼 표현하지 않는다.
실제 주문 기능은 없으며 모델도 증권사 자격증명에 접근하지 않는다.

## 빠른 시작

기준 런타임은 Python 3.13.12다. 프로젝트 실행에는 외부 Python 패키지가 필요하지
않는다. 저장소 루트에서 등록 자산과 필요한 수집 작업을 확인한다.

```bash
python3.13 -m cio assets
python3.13 -m cio refresh 005930
```

필요한 기본 수집을 실행한다. `--optional`은 해당 자산에 등록된 보조 수집기까지
포함한다.

```bash
python3.13 -m cio refresh 005930 --run --optional
```

검토 가격과 최대 사용 금액으로 모델용 최소 컨텍스트를 만든다.

```bash
python3.13 -m cio prepare 005930 \
  --price 185000 \
  --amount 5000000 \
  --amount-currency KRW
```

생성된 `knowledge/contexts/<asset_id>/<context_id>.json`을
`prompts/cio-system.md`와 함께 원하는 모델에 제공한다. 모델은
`schemas/cio-decision.schema.json`에 맞는 JSON 하나를 반환해야 한다.

모델 출력을 `decision.json`으로 저장했다면 읽기 전용 검증을 할 수 있다. 공식
시도는 `record`가 다시 검증한 뒤 유효 판단 또는 실패 출력으로 남긴다.

```bash
python3.13 -m cio validate decision.json  # 선택: 쓰기 없는 사전검사
python3.13 -m cio record decision.json
```

가격 이력이 쌓인 뒤 과거 판단을 사후 평가한다.

```bash
python3.13 -m cio score 005930
```

`record`는 유효한 판단을 `knowledge/assessments/`에 저장한다. 유효하지 않은 모델
출력은 깨진 JSON까지 버리지 않고 `knowledge/quarantine/`에 격리한다. `score` 결과는
`knowledge/evaluations/`에 쌓인다.

모델이 판단에 남긴 수집 요청은 사용자가 읽고 닫는 대기열이다. 처리되기 전까지
다음 컨텍스트에 다시 실려 나간다.

```bash
python3.13 -m cio requests 005930
python3.13 -m cio requests 005930 --resolve REQ-3e547a42822d --state DONE \
  --note "collectors/dart.py filings 배선"
```

모델이 조사한 원문은 인용하기 전에 로컬에 보존한다. 받은 그대로 저장하며
정제하지 않는다.

```bash
python3.13 -m cio snapshot <URL> --asset 005930 --label dram-price-2026q3
python3.13 -m collectors.dart document 20260730800077   # 한국 공시 원문
```

컨텍스트가 참조한 근거와 prompt/schema는 내용 지문으로
`knowledge/evidence/`에 중복 없이 보존된다. 이후 원본 파일이 갱신돼도 당시 모델이
사용한 입력은 바뀌지 않는다.

## 자산별 분기

`config/assets.json`이 시장(`KR`/`US`), 상품(`STOCK`/`ETF`), 공시원,
벤치마크, 분석 렌즈와 적합한 가치평가 후보를 지정한다. 이 값은 모델의 출발점이지
결론을 강제하는 규칙이 아니다. ETF에 회사 DCF를 적용하거나 적자 임상 바이오에
정률성장 DCF를 적용하지 않는다.

새 자산은 `python3.13 -m cio register`로 등록한다. 상세 필드와 수집 정책은
[데이터 정책](docs/data-policy.md)을 참고한다.

## 문서

- [아키텍처](docs/architecture.md)
- [판단 계약](docs/decision-contract.md)
- [데이터 정책](docs/data-policy.md)
- [평가와 기억](docs/evaluation.md)

기계 판독 계약의 최종 권위는 문서 설명이 아니라 코드와
`schemas/cio-decision.schema.json`이다.
