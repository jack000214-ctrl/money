# AGENTS.md

This file provides guidance to agents when working with code in this repository.

## 세션 모드 (최우선 확인)

첫 메시지 앞의 `[분석]` / `[개발]` 접두사로 읽을 파일이 달라진다. 없으면 투자 질문은 `[분석]`으로 취급.

- **`[분석]`**: 세션 메모리 일절 사용 금지. `README.md` → `skills/decide-investment/SKILL.md` → `prompts/cio-system.md` + `cio prepare` 패킷 순서로만 진행. 같은 종목·같은 가격에 두 번 물었을 때 답이 달라지면 안 된다.
- **`[개발]`**: `README.md` → `MEMORY.md`(경로: `/Users/jaehoseo/.claude/projects/-Users-jaehoseo-Desktop-vswrk-money/memory/MEMORY.md`) → 관련 `docs/` 순으로 읽은 뒤 작업. 이미 기록된 결정·기각된 제안을 재검증하지 않는다.

## 역할 경계 (핵심 불변 규칙)

- PC의 `validation = PASS | BLOCKED` = 데이터 준비 상태 (판단 아님)
- 모델의 `verdict` = 투자 판단
- 파이썬 규칙으로 판정을 대신 만들지 않는다. 숫자는 `build_decision_report.py`에서 생성하고 산문은 `narrative.json`에 쓴다.
- **커밋 금지**: `git add`/`git commit` 실행 불가. 작업 후 `git diff` 요약만 보고.

## 명령 모음

```bash
# Python (외부 패키지 없음, stdlib만 사용)
python3.13 -m cio assets                        # 등록 자산 목록
python3.13 -m cio prepare <ticker> --price ...  # 컨텍스트 패킷 생성
python3.13 tests/run.py                         # 전체 Python 테스트
python3.13 -m unittest tests.test_<name>        # 단일 Python 테스트

# Dashboard (dashboard/ 디렉터리에서 실행)
npm run test    # vinext build 후 node --test tests/rendered-html.test.mjs 실행 (build 필요)
npm run lint    # eslint (dist, .next 제외)
npm run dev     # 개발 서버 (vinext dev, port 3000)

# 전체 시스템 실행 (Windows)
./start.ps1     # API서버(3001) + 대시보드(3000) 동시 기동
```

## 비표준 패턴

- **대시보드 빌드**: Next.js가 아니라 `vinext`(Vite 기반 RSC) 사용. `next build` 아님.
- **Python 런타임**: `python3.13` 고정. `python` / `python3` 은 버전이 다를 수 있음.
- **API 서버**: `scripts/api_server.py` (Python stdlib `http.server`, port 3001). 프레임워크 없음.
- **`.env` 로딩**: `scripts/stock_service.py`의 `load_env_file()`이 직접 파싱. `python-dotenv` 없음. 값에 따옴표 넣으면 따옴표까지 전송됨.
- **대시보드 테스트**: `dist/server/index.js`를 직접 import해 Worker로 실행. 테스트 전 반드시 `npm run build` 선행.
- **리포트 JSON 경로**: `dashboard/data/decision-report.json` — 빌드 타임에 정적 import. 선택된 종목이 바뀌면 `api/select` → `window.location.reload()`.
- **tickers.json**: `dashboard/scripts/tickers.json` — `stock_service.py`가 수집 완료 후 자동 동기화.

## 민감 파일 (`.gitignore` 대상 — 읽기만, 저장소 외부로 이동 금지)

- `config/investor-policy.json` — 투자 한도·계좌 유형
- `knowledge/` 하위 실제 데이터 파일
- `.env`

## 대시보드 CSS 규칙

- CSS 변수: `--halt`(차단), `--hold`(보류), `--approved`(승인) — 판정 색. 시세 색(`--rise`/`--fall`)과 절대 혼용 금지.
- `StockControlPanel.tsx`는 Tailwind 없이 순수 inline style 사용 (다크 배경 `#0b1120` 기반).
- 리포트 뷰(`page.tsx`)는 Tailwind + CSS 변수 혼용.
