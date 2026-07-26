# 다음 세션 인수인계 (2026-07-26 작성)

이 파일은 임시 작업 메모다. IPS 승인이 끝나면 지워도 된다.

## 지금 상태 한 줄

매매 엔진의 안전 검사는 다 들어갔고 테스트 129개가 통과한다.
**IPS가 `DRAFT`라 아직 모든 매매는 `RESEARCH_REQUIRED`로 떨어진다.**
승인에 필요한 값 8개만 채우면 엔진이 실제로 판정을 시작한다.


---

## 다음에 할 일 1 — IPS 승인 (30분)

`knowledge/portfolio/ips.json`의 `status`를 `APPROVED`로 바꾸려면 아래 8개가
필요하다. 스키마가 막고 있어서 값 없이는 승인이 안 된다.

### 토스증권 앱에서 확인할 6개

**앱 → 전체 → 주식 거래내역 → 개별 체결 건**을 열면 수수료·세금이 원 단위로 나온다.
`수수료 ÷ 체결금액 × 10000`이 bps다. 공시 요율보다 실제 체결 건 역산이 정확하다
(프로모션·우대가 반영되므로, ADR-026).

| 필드 | 확인 방법 |
|---|---|
| `costs.by_market.KR.buy_commission_bps` | 국내 매수 체결 1건 |
| `costs.by_market.KR.sell_commission_bps` | 국내 매도 체결 1건 |
| `costs.by_market.KR.sell_tax_bps` | 국내 매도 건의 "세금" 항목 (파마리서치는 코스닥) |
| `costs.by_market.US.buy_commission_bps` | 미국 매수 체결 1건 |
| `costs.by_market.US.sell_commission_bps` | 미국 매도 체결 1건 |
| `costs.by_market.US.fx_cost_bps` | 환전 스프레드. 통합증거금이면 체결가에 녹아 있어 확인 필요 |

`costs.by_market.US.sell_tax_bps`는 **0으로 둔다.** 미국주식 양도소득세는
거래마다 떼는 게 아니라 연 250만원 공제 후 연간 정산이라, 거래 단위 비용
필드에 넣으면 매 거래 비용이 부풀려진다.

### 제안값 2개 — 승인만 하면 되는 것

| 필드 | 제안 | 근거 |
|---|---|---|
| `data_freshness.price_max_age_minutes` | `60` | 앱에서 호가 보고 1시간 안에 판정하면 통과 |
| `data_quality.price_max_deviation_pct` | `30` | USD/KRW 혼동(1,400배)은 잡고, 보유내역이 최대 3일 낡을 수 있는 정상 등락은 통과 |

### 승인 절차

```bash
# 값을 채운 뒤
python3 -c "
import sys; sys.path.insert(0,'scripts')
from contracts import load_json, validate_ips
d = load_json('knowledge/portfolio/ips.json')
d.update(status='APPROVED', approved_by='jaehoseo', approved_at='2026-07-2?T??:00:00+09:00')
print(validate_ips(d) or '승인 가능')"
```

빈 목록이 나오면 `status`, `approved_by`, `approved_at`을 파일에 직접 반영한다.

---

## 다음에 할 일 2 — 승인 직후 검증

```bash
# 실제 파마리서치 1주 매수가 어떻게 판정되는지
python3 scripts/trade_check.py 214450 buy 1 --price {현재가} \
  --price-as-of "$(date -u +%Y-%m-%dT%H:%M:%S+00:00)"
```

`--analysis`가 없으면 `ANALYSIS_MISSING`으로 `RESEARCH_REQUIRED`가 뜬다.
그게 정상이다. 실제 `PROCEED`를 보려면 검증된 분석 JSON이 하나 필요하다
(`knowledge/research/analysis-template.json` 참고,
`python3 scripts/validate_analysis.py`로 검증).

---

## 결정된 정책 (이미 반영됨)

| 항목 | 값 |
|---|---|
| 개별주 단일 종목 | 표준 25% / 절대상한 50% |
| 광역 ETF (SCHD) | 표준 40% / 절대상한 50% |
| 파생 ETF (MSTY) | 표준 5% / 절대상한 5% |
| 요구수익률 | 연 12%, 미달 매수는 차단 |
| 대안 우위 경고 | 5%p 이상 |
| 슬리피지 | 10bp (국내·해외 공통) |
| 보유내역 / 분석 / 기준가 최대 경과 | 3일 / 30일 / 3일 |
| 현금 하한 | 없음 (생활비 별도 분리) |

표준~절대상한 구간은 `--concentration-override-reason "{사유}"`로 사유를
남기면 `CAUTION`으로 통과한다. 절대상한은 사유로 해제되지 않는다.

---

## 미결 사항

### 아직 정하지 않은 것 (null이어도 승인은 됨)

- `position_limits.equity_sleeve_pct`, `risk_assets_pct`
- `portfolio_limits`의 상위10 / 섹터 / 통화 / 미분류

**섹터 한도는 한 번 생각해볼 값이다.** 현재 헬스케어 직접 비중이 **30.64%**이고,
이건 ETF 룩스루를 제외한 하한이다. SCHD 안의 헬스케어 18.8%를 더하면 실제로는
36% 안팎이다. 파마리서치(23.56%)와 RXRX(7.08%)가 같은 섹터라는 뜻이다.

### 알려진 한계

1. **`--price-as-of`는 자기신고다.** 가격 자체는 기준가와 대조하지만, "언제
   관측했나"는 검증할 방법이 없다.
2. **섹터·통화·상위10 집중도는 ETF 룩스루 미반영 직접 비중이다.** 섹터 비중은
   항상 하한이며, 사각지대 크기는 `unclassified_total_assets_pct`로 제한한다.
   실제 룩스루는 portfolio-xray SKILL이 계산한다.
3. **거래 기록에 수수료 열이 없다.** `knowledge/portfolio/trades/*.csv`에
   `fee_krw`, `tax_krw` 열을 추가하면 다음부터는 실제 비용을 역산할 수 있다.
4. **RXRX 거래 내역이 불완전하다.** 2025년 적립 매수분의 개별 일자·단가가
   빠져 있어 평단과 실현손익 검증이 불가능하다. 증권사 거래내역 다운로드 필요.

---

## 정정 이력 (중요)

2026-07-26에 현금이 **2억 → 4천만**으로 정정됐다. 적금·주택청약·퇴직연금
약 6천만은 이 프로그램의 분모에서 제외한다 (ADR-024). 이 때문에:

| | 정정 전 | 정정 후 |
|---|---|---|
| 총자산 | 3억 400만 | **1억 4,405만** |
| 파마리서치 | 11.16% | **23.56%** |
| SCHD | 13.84% | **29.22%** |
| 주식 비중 | 34.2% | **72.2%** |
| 통화 | KRW 76.9% | **KRW 51.3 / USD 48.7** |
| 목표(월200+12%) | 52.9억 | **35.5억** |

**이전 세션 문서에 남아 있는 11.16%는 전부 잘못된 값이다.** 적금 만기 등으로
투자 가능 자금이 늘면 `holdings.csv`의 `CASH_KRW`에 반영한다.
