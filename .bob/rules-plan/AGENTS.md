# AGENTS.md — Plan 모드

This file provides guidance to agents when working with code in this repository.

## 설계 시 숨겨진 제약

### 데이터 흐름 (변경 불가 원칙)
- `build_decision_report.py` → `dashboard/data/decision-report.json` → 빌드 타임 정적 import. 런타임 API fetch 구조가 아님. 종목 전환 시 반드시 파일 교체 + 재빌드 또는 `window.location.reload()`.
- `dashboard/scripts/tickers.json` — `stock_service.py`가 수집 후 자동 동기화. 이 파일을 수동 편집하면 다음 수집 시 덮어씌워짐.

### 역할 경계 (아키텍처 불변)
- PC는 수집·계산·검증만. 투자 판단 로직을 Python 규칙으로 구현하면 계약 위반.
- 수집 파이프라인 4단계: ①시세 ②fundamentals ③리포트 빌드 ④AI 분석. 각 단계는 독립 API 엔드포인트. 전체 실행은 `/api/collect/all`.

### 대시보드 상태 관리
- `StockControlPanel`의 `viewMode("stocks"|"report")` 가 최상위 뷰 전환. 두 뷰는 조건부 렌더(report는 `display:none`으로 숨김, unmount 아님).
- API 서버(port 3001)가 오프라인이면 `StockControlPanel`은 빈 종목 목록을 표시. 리포트 뷰는 정적 JSON이므로 API 없이도 표시됨.

### 신규 기능 추가 시 검토 필요 파일
- 새 verdict 값: `decision-report.ts` + `VerdictMark.tsx` + `rendered-html.test.mjs` 동시 수정.
- 새 수집 단계: `stock_service.py` 함수 → `api_server.py` 엔드포인트 → `StockControlPanel.tsx` 버튼 → grid `gridTemplateColumns` 열 수 조정.
- 새 자산 필드: `config/assets.json` 스키마 → `cio/paths.py` 경로 상수 없이 직접 접근 금지.
