#!/bin/zsh
# 종목별 리포트를 생성하고, 화면이 읽을 파일로 선택한다.
#
# 화면(app/page.tsx)은 data/decision-report.json 과 data/price-chart.json 을
# 정적 import 한다. 종목이 늘어도 **화면은 하나**여야 하므로, 리포트는
# data/reports/{종목}/ 아래에 쌓고 그중 하나를 위 두 파일로 복사한다.
#
# 라우팅으로 여러 종목을 동시에 보여줄지, 앱으로 갈지, 배치로 돌릴지는
# 아직 정하지 않았다. 그 결정과 무관하게 이 구조는 유지된다.
#
#   ./scripts/build.sh 005930    # 삼성전자
#   ./scripts/build.sh 005930 --no-select   # 생성만, 화면은 그대로

set -e
cd "$(dirname "$0")/.."
if [[ -z "$1" ]]; then
  echo "사용: ./scripts/build.sh <005930|000660|...> [--no-select]" >&2
  exit 1
fi
ASSET="$1"
ROOT=..

case "$ASSET" in
005930)
  TICKER_LABEL="005930 · KOSPI"; NAME="삼성전자"; QTY=13
  MARKET=KR; RISK=STOCK; CURRENCY=KRW
  PRICE_KRW=231000; PRICE_AS_OF="2026-07-28T09:30:00+09:00"
  PRICE_NOTE="2026-07-28 장중 231,000원"; AS_OF="2026-07-28"
  ANALYSIS=$ROOT/knowledge/research/analysis-005930-20260728.json
  FUNDAMENTALS=$ROOT/knowledge/fundamentals/005930-2026-03-31.json
  PRICES=$ROOT/knowledge/prices/005930_KS.csv
  CHART_CURRENT=231000; CHART_ENTRY=126107
  CHART_ENTRY_LABEL="강세 시나리오 단독 기준 (매수 트리거 아님)"
  ;;
*)
  echo "모르는 종목: $ASSET" >&2; exit 1 ;;
esac

OUT=data/reports/$ASSET
mkdir -p "$OUT"

python3 scripts/build_price_chart.py \
  --input "$PRICES" --output "$OUT/chart.json" \
  --current "$CHART_CURRENT" --entry-ceiling "$CHART_ENTRY" \
  --entry-label "$CHART_ENTRY_LABEL" --currency "$CURRENCY" \
  --lookback-days 365

python3 scripts/build_decision_report.py \
  --ticker "$ASSET" --ticker-label "$TICKER_LABEL" --name "$NAME" \
  --quantity "$QTY" --market "$MARKET" --risk-class "$RISK" \
  --price-krw "$PRICE_KRW" --price-as-of "$PRICE_AS_OF" \
  --price-note "$PRICE_NOTE" --as-of "$AS_OF" \
  --analysis "$ANALYSIS" --fundamentals "$FUNDAMENTALS" \
  --prices "$PRICES" --narrative "data/narrative/$ASSET.json" \
  --output "$OUT/report.json"

if [[ "$2" != "--no-select" ]]; then
  cp "$OUT/report.json" data/decision-report.json
  cp "$OUT/chart.json" data/price-chart.json
  echo "화면 선택: $ASSET"
fi
