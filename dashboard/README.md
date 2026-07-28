# Twelve Decision Dashboard

검증된 투자 분석 결과를 읽기 쉬운 의사결정 보고서로 표현하는 렌더러다.
**종목이 늘어도 화면은 하나다.**

## 원칙

- 화면은 어떤 투자 계산도 하지 않는다.
- 가격, 수익률, 연환산 값, 시나리오 확률, 비중, 임계값, 차트 길이는 모두
  상위 계층이 미리 계산하고 검증한 표시값이다.
- 화면은 `data/decision-report.json`과 미리 계산된 가격 차트 좌표를 순서대로
  렌더링할 뿐이다.
- **종목명·통화·회계기준을 화면에 박지 않는다.** 표시 문자열은 전부
  리포트 JSON에서 온다.

## 쓰는 법

```bash
./scripts/build.sh 005930    # 삼성전자 리포트 생성 후 화면에 올림
./scripts/build.sh TSLA      # 테슬라
./scripts/build.sh 005930 --no-select   # 생성만, 화면 선택은 유지
npm run dev
```

리포트는 `data/reports/{종목}/` 아래에 **쌓인다.** 화면이 정적 import 하는
`data/decision-report.json`·`data/price-chart.json`은 그중 하나를 복사한 것이다.
라우팅으로 여러 종목을 동시에 보여줄지, 앱·SKILL·배치 중 무엇으로 갈지는
아직 정하지 않았고, 그 결정과 무관하게 이 구조는 유지된다.

## 데이터 배치

```
data/
├── decision-report.json      # 화면이 읽는 것 (선택된 종목의 복사본)
├── price-chart.json          # 화면이 읽는 것 (선택된 종목의 복사본)
├── narrative/{종목}.json      # 사람이 쓰는 산문. 숫자는 넣지 않는다
└── reports/{종목}/
    ├── report.json           # 생성기 산출물
    └── chart.json            # 생성기 산출물
```

## 파이프라인

```
원문 수집    edgar_fetch fundamentals {티커}      (미국)
            dart_fetch  fundamentals {종목코드}   (한국)
   ↓
밸류에이션   reverse_dcf scenarios --fundamentals ... --save
   ↓
분석 계약    build_analysis.py --spec knowledge/research/specs/{종목}.json
   ↓
화면 리포트  dashboard/scripts/build.sh {종목}
```

각 단계는 앞 단계의 지문(`fundamentals_id` → `calculation_id` →
`snapshot_id`)을 검사한다. 중간 값을 손으로 고치면 지문이 어긋나 멈춘다.

## 시장별로 다른 것

두 시장은 회계 표기가 달라서, 그 차이를 **스냅샷이 선언하고 생성기가 읽는다.**

| | 미국 (EDGAR) | 한국 (DART) |
|---|---|---|
| 손익 비용 라인 | R&D · SG&A 분리 | 판매비와관리비 한 줄 (R&D 포함) |
| 현금흐름 기간 | 분기값 | **연초 누계** — 차감 복원 필요 |
| 손익 기간 | 분기값 | 분기값 (4분기만 역산) |
| 표시 단위 | 백만 USD | 조 KRW |
| 차입금 | 장기차입 중심 | 단기차입금이 더 큰 경우가 있다 |

생성기는 스냅샷의 `currency`로 표시 형식을 고르고, `expense_lines`로 손익
항등식을 만든다. **항등식(매출총이익 − 비용 = 영업이익)이 어긋나면 리포트를
만들지 않는다** — 비용 라인이 중복되거나 빠졌다는 뜻이다.

## 테스트

```bash
npm test
```

12개 테스트가 **선택된 종목이 무엇이든** 통과해야 한다. 특정 종목의 수치를
단정하지 않고 구조 불변식을 검사한다 — 배수가 리포트 자신의 주가에서 나왔는지,
전년 동기가 실제로 4분기 전인지, 세그먼트 수가 점 개수 − 1인지, 엔진 판정이
자연어로 완화되지 않았는지.

운영 연결 시에는 `contracts.py` 검증을 통과한 analysis, calculation,
trade_check 결과를 백엔드에서 `DecisionReport`로 변환한다. 변환과 숫자 계산은
백엔드 책임이며 이 렌더러에 추가하지 않는다.
