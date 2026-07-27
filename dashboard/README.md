# Twelve Decision Dashboard

검증된 투자 분석 결과를 읽기 쉬운 의사결정 보고서로 표현하는 렌더러다.

## 원칙

- 화면은 어떤 투자 계산도 하지 않는다.
- 가격, 수익률, 연환산 값, 시나리오 확률, 비중, 임계값, 차트 길이는 모두
  상위 계층이 미리 계산하고 검증한 표시값이다.
- 화면은 `data/decision-report.json`과 미리 계산된 가격 차트 좌표를 순서대로
  렌더링할 뿐이다.
- 현재 보고서는 로컬 TSLA 일별 시세, 2026 Q2 재무, 동종사 SEC 비교,
  `CALC-44eb75970b617b730887` 역산 DCF 산출물을 사용한다.

## 재사용 방법

1. `lib/decision-report.ts`의 계약에 맞는 JSON을 생성한다.
2. `data/decision-report.json`을 검증된 산출물로 교체한다.
3. `scripts/build_price_chart.py`로 일별 시세를 화면용 좌표로 변환한다.
4. 빌드하면 같은 구조로 다른 종목과 다른 시점의 보고서가 나온다.

운영 연결 시에는 `contracts.py` 검증을 통과한 analysis, calculation,
trade_check 결과를 백엔드에서 `DecisionReport`로 변환한다. 변환과 숫자 계산은
백엔드 책임이며 이 렌더러에 추가하지 않는다.
