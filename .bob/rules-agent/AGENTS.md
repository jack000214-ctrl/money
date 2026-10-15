# AGENTS.md — Agent (코딩) 모드

This file provides guidance to agents when working with code in this repository.

## 코드 수정 시 필수 확인

### Python
- 모든 모듈은 `cio/paths.py`의 `ROOT`, `CONFIG_DIR`, `KNOWLEDGE_DIR`, `DATA_DIR` 경로 상수를 사용한다. `Path(__file__).parent` 로 직접 경로를 계산하지 않는다.
- `sys.path.insert(0, str(ROOT_DIR))` 패턴은 `scripts/` 하위 파일에서만 사용. `cio/` 패키지 모듈은 패키지 import를 사용.
- `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` — Windows 터미널 인코딩 문제로 `scripts/` 하위 엔트리포인트마다 선행 추가.
- 테스트: 새 버그는 **수정 전에** `tests/` 에 테스트 먼저 추가. `tests/run.py` 로 전체 실행.

### 대시보드 (TypeScript/React)
- `StockControlPanel.tsx`는 `"use client"` 클라이언트 컴포넌트. `page.tsx`(서버 컴포넌트)가 이를 감싸고 리포트 JSX를 `children`으로 넘기는 패턴이 현재 구조.
- 리포트 데이터(`dashboard/data/decision-report.json`)는 빌드 타임 정적 import. 종목 전환은 `scripts/api_server.py`의 `/api/select` → `window.location.reload()` 이 유일한 경로.
- `build_decision_report.py`에서 숫자 생성, `narrative.json`에서 산문 작성. 이 분리를 깨면 두 번째 진실의 원천 문제가 재발.
- verdict 타입에 새 값 추가 시: `dashboard/lib/decision-report.ts` 유니온, `VerdictMark.tsx` MARKS, 테스트의 ETF 분기 — 세 곳을 동시 수정.

### ETF vs STOCK 분기
- ETF 리포트: `schemaVersion = "etf-dashboard/1.0.0"`, `verdict = "INSUFFICIENT"`. DART/DCF 없이 가격 차트만.
- `AnalysisDialog.tsx`의 배열 필드에 `?? []` 방어 코드가 있는 이유: ETF 리포트는 해당 필드가 없음.
