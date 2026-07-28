# Deterministic calculations

`CALC-*.json`은 모델의 투자판단이 아니라 재현 가능한 산술 결과다.
`calculation_id`는 모델 버전, 입력, 출력의 내용 지문이며 입력 출처 ID를 함께
보존한다.

역산 DCF가 자산에 맞을 때만 다음처럼 만든다.

```bash
python3 -m calculators.reverse_dcf scenarios \
  --instrument TSLA --price 300 \
  --fundamentals knowledge/fundamentals/TSLA-2026-06-30.json \
  --discount-rate 10 --terminal-growth 3 --years 10 \
  --growth bear=5 base=15 bull=25 --horizon-months 24 \
  --source-id FUND-example --save
```

모델은 이 결과를 채택·수정·기각할 수 있지만, 수치를 인용할 때는
`calculation_id`를 결정 기록의 근거로 남긴다.
