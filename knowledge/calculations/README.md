# 계산 산출물

`scripts/reverse_dcf.py` 같은 승인된 계산기가 발급한 산출물을 `CALC-*.json` 으로
보관한다. 분석의 `expected_return.calculation_id` 가 여기를 가리키고,
`scripts/trade_check.py` 와 `scripts/validate_analysis.py` 가 시나리오 수익률을
이 파일들과 대조한다.

**손으로 만들거나 고치지 않는다.** `calculation_id` 는 모델·입력·산출값 전체의
SHA-256 지문이라, 파일에서 무엇을 고치든 ID 가 어긋나고 계약 검증에서 걸린다.
가정을 바꾸려면 계산기를 다시 돌려 새 산출물을 만든다.

```bash
python3 scripts/reverse_dcf.py scenarios \
  --instrument {종목} --price {주당가격} --shares {주식수} \
  --fcf {FCF} --net-debt={순부채} \
  --discount-rate {할인율} --terminal-growth {영구성장률} --years 10 \
  --growth bear=5 base=15 bull=25 \
  --horizon-months 24 --source-id SRC-... --save
```

옛 산출물은 지우지 않는다. 과거 결정이 어떤 가정 위에서 내려졌는지가
`decision_snapshot_id` 로 추적되기 때문이다.
