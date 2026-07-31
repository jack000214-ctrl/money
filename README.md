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

판단 계약을 조이면 그 이전 판단이 재검증에 실패해 다음 컨텍스트로 이어지지
않는다. 파일은 남아 있으므로 **직접 열어 본 사람과 패킷을 읽는 모델이 서로 다른
것을 보게 된다.** 어떤 판단이 살아 있고 무엇이 끊겼는지 먼저 확인한다.

```bash
python3.13 -m cio retire 214450          # 살아 있음 / 계약 미달 / 폐기됨
```

더 못 믿을 판단은 명시적으로 폐기한다. 자동으로 치우지 않는 이유는 계약 미달이
결론이 아니라 사고이기 때문이다. 폐기한 판단은 지우지 않고
`knowledge/retired/`로 옮겨 사유와 함께 남기며, 이후 컨텍스트에는 **있었다는
사실만** 실린다 — verdict·가격계획·논지는 인용할 수 없게 빠진다.

```bash
python3.13 -m cio retire 214450 --reason "구버전 산물"            # 대상만 확인
python3.13 -m cio retire 214450 --reason "구버전 산물" --confirm  # 실제 이관
python3.13 -m cio retire 214450 --reason "..." --all --confirm    # 유효한 것까지
```

모델이 판단에 남긴 수집 요청은 사용자가 읽고 닫는 대기열이다. 처리되기 전까지
다음 컨텍스트에 다시 실려 나간다.

```bash
python3.13 -m cio requests 005930
python3.13 -m cio requests 005930 --resolve REQ-3e547a42822d --state DONE \
  --note "collectors/dart.py filings 배선"
```

보유 종목의 결정론적 산술은 계산기가 낸다. 평단·실현손익은 증권사 스냅샷이 아니라
거래 원장에서 이동평균법으로 접고, 검토 가격 기준 배수 환산·세 분모 비중·추가
수량별 눈금을 함께 낸다.

```bash
python3.13 -m calculators.position 214450 --price 331000
python3.13 -m calculators.position 214450 --price 331000 --add 5 10 20
```

평단은 **두 경로로 나오고 둘을 합치지 않는다** — 거래 원장의 이동평균과 스냅샷의
(평가액 − 평가손익) ÷ 수량이다. 벌어졌다면 둘 중 하나가 낡은 것이며, 어느 쪽인지는
계산기가 정하지 않고 차이만 보고한다.

**이 계산기는 판정을 내지 않는다.** 싸다·비싸다, 사도 된다·줄여라 같은 값은 출력에
없고 임계값도 여기서 정하지 않는다. 모델이 세운 문턱을 받아 대조할 뿐이다. 계산기가
판정을 내기 시작하면 모델이 그것을 근거로 받아들이고, 그 순간 판단의 주체가 조용히
바뀐다. `tests/test_position_math.py`가 그 경계를 고정한다.

한국 종목은 다음 실적 발표일을 결산실적 공시예고 원문에서 읽어 캘린더로 만든다.
공시 색인에는 제목만 있고 발표 예정일은 본문에만 있어서, 이벤트까지 며칠
남았는지가 판단 입력에서 빠져 있었다.

```bash
python3.13 -m collectors.kr_calendar 214450
```

잠정실적과 정기보고서를 나눠 낸다. **잠정실적 공정공시는 매출·영업이익 요약만
담고 재고자산·부문별 매출은 정기보고서에만 나온다.** 정기보고서 제출일은
공시되지 않으므로 과거 (잠정 → 정기) 간격의 중앙값으로 추정하며, 분기·반기와
사업보고서는 법정 기한이 달라 따로 센다.

모델이 조사한 원문은 인용하기 전에 로컬에 보존한다. 받은 그대로 저장하며
정제하지 않는다.

```bash
python3.13 -m cio snapshot <URL> --asset 005930 --label dram-price-2026q3
python3.13 -m collectors.dart document 20260730800077   # 한국 공시 원문
```

보존한 원문은 통째로 읽는 것이 아니라 필요한 자리만 찾아 쓴다. 사업보고서
하나가 100만 자라 컨텍스트에 넣을 수 없기 때문이다.

```bash
python3.13 -m cio docs 005930                  # 무엇이 있고 무엇을 안 받았나
python3.13 -m cio search 005930 "부 문 \|"     # 부문별 매출 표
python3.13 -m cio search 005930 "주요 매출처"
```

부문별 매출·비중, 주요 매출처, 수주잔고, 생산능력, 원재료 공급처, 시장점유율은
사업보고서에 있다. 회사 구조에 관한 사실을 언론 기사로 인용하는 것은 1차 출처를
두고 2차 출처를 쓰는 것이다.

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
