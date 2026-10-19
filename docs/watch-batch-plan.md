# 보유 종목 감시 배치 + 알림 + 일일 브리핑 계획

## 상위 개요

### 목표
보유 종목(`tickers.json`의 `qty > 0`)의 현재가를 주기적으로 조회한다. 모델이 이미 저장한 판단의 가격 문턱과 사용자의 정책 한도를 **부등호로 대조**하고, 문턱에 닿으면 알림과 재질문 패킷 준비까지 한다. 하루 1회 일일 브리핑도 낸다.

### 범위
- 한국 시장(KR) 종목만 다룬다. 미국 주식은 제외한다.
- 감시 시간은 한국 시각 08:00~20:00이다. 주말과 휴장일은 건너뛴다.
- 주기는 `config/watch.json` 한 곳에서 정한다. 테스트 값은 **60분**, 이후 **10분**으로 바꾼다.
- 알림 채널 우선순위
  - 1차: 콘솔, 파일(이벤트 로그), 대시보드 배지
  - 2차: 이메일(SMTP)
  - 텔레그램은 이번 범위에서 제외한다.

### 비범위 (계약상 하지 않는 것)
- PC가 매수·매도를 판정하지 않는다. 알림은 "문턱 X에 현재가 Y가 닿았다. 판단 CIO-…이 약속한 행동은 Z다" 형태의 **사실 진술**이다.
- 주문 실행과 증권사 자격증명 접근은 하지 않는다.
- `invalidation_signals[].signal`은 자유 문장이라 자동 대조하지 않는다. 알림에 문장을 그대로 실어 사용자와 모델이 확인하게 한다.
- 일봉 CSV(`knowledge/prices/`)와 `session_state` 계약은 바꾸지 않는다.

### 접근 방식
1. 장중 현재가는 일봉 저장소와 분리된 `knowledge/watch/quotes.json`에 둔다. 일봉 CSV에 섞으면 `UNSETTLED` 봉이 하루치로 오인된다. 근거는 `collectors/price.py`의 `session_state()` 주석이다.
2. 트리거 이벤트는 `knowledge/watch/events.jsonl`에 추가만 한다. 중복 제거 키는 `(assessment_id, 문턱 이름)`이고, 문턱을 벗어났다가 다시 닿았을 때만 재발생한다.
3. 트리거가 켜지면 기존 `cio prepare`로 재질문 패킷만 준비한다. 새 verdict는 모델이 낸다.

### 알려진 위험
- **08:00~09:00, 15:30~20:00 가격 출처 미확인.** 정규장은 09:00~15:30이다. 앞뒤 시간대에 현재가를 주는 출처가 있는지 아직 확인하지 못했다. Yahoo `regularMarketPrice`가 정규장 가격만 줄 수 있다. 서브태스크 2에서 실측하고, 못 받으면 해당 시간대는 "마지막 정규장 가격"임을 `quotes.json`에 명시한다.
- 패킷은 만든 뒤 12시간까지만 유효하다(README). 자동 준비한 패킷이 만료되면 폐기하고 다시 만드는 규칙이 필요하다.
- `knowledge/`, `config/investor-policy.json`, `.env`는 `.gitignore` 대상이다. 새 알림 설정의 비밀값(SMTP 비밀번호)은 `.env`에만 둔다.
- 작업 완료 후 `git add`/`git commit`은 하지 않고 `git diff` 요약만 보고한다(AGENTS.md).

### 공통 제약
- Python은 stdlib만 쓴다. `python` 명령으로 실행한다.
- 경로는 `cio/paths.py` 상수를 통해 접근한다.
- `.env`는 `scripts/stock_service.py`의 `load_env_file()` 방식으로 읽는다. 값에 따옴표를 쓰지 않는다.
- 새 모듈마다 `tests/test_*.py`를 추가하고 `python tests/run.py`로 검증한다.
- 서브태스크는 순서대로 하나씩 진행하고, 각 단계 후 사용자 확인을 받는다.

---

## 서브태스크

### 1. 감시 설정과 장 시간 게이트
**Intent**: 언제 얼마나 자주 감시할지를 코드 밖 설정으로 분리하고, 감시해도 되는 시각인지 판정한다.

**Expected Outcomes**
- `config/watch.json`에서 시작·종료 시각(08:00/20:00), 시간대(Asia/Seoul), 주기(분), 활성 시장(KR), 활성 채널을 읽는다.
- 주기를 60에서 10으로 바꾸는 데 설정 파일 수정 한 줄이면 충분하다.
- 주말과 감시 시간 밖에는 "건너뜀" 사유가 반환된다.

**Todo List**
- [ ] `config/templates/watch.json` 템플릿을 만든다(`investor-policy.json` 템플릿 방식 참고). 실제 설정은 `.gitignore` 여부를 결정한다.
- [ ] `cio/paths.py`에 감시 관련 경로 상수를 추가한다(`knowledge/watch/`).
- [ ] 설정 로더와 검증을 만든다(`cio/watch_config.py` 신규). 시각 형식, 주기 양수, 시작 < 종료를 검사한다.
- [ ] 장 시간 게이트 함수를 만든다. 입력은 현재 시각이고 출력은 `RUN` 또는 `SKIP(사유)`이다. 주말을 처리한다.
- [ ] 휴장일 처리 방식을 정한다. 우선 주말만 처리하고, 평일 휴장은 시세 날짜 미갱신으로 감지할지 결정한다.
- [ ] `tests/test_watch_config.py`를 만든다. 경계 시각(07:59, 08:00, 19:59, 20:00)과 주말을 검사한다.

**Relevant Context**: `config/` 구조, `calculators/portfolio_math.py`의 `_load_policy()`(정책 파일 읽기 패턴), `cio/paths.py`, `tests/helpers.py`

**Status**: `[ ] pending`

---

### 2. 장중 현재가 조회
**Intent**: 일봉 저장소를 오염시키지 않고 보유 종목의 최신 현재가를 얻는다.

**Expected Outcomes**
- 보유 종목마다 현재가, 조회 시각, 출처, 장 상태가 `knowledge/watch/quotes.json`에 저장된다.
- 08:00~09:00과 15:30~20:00에 받을 수 있는 가격 종류가 실측으로 확인되고 문서에 기록된다.
- 조회에 실패한 종목은 이전 값을 덮어쓰지 않고 실패로 표시된다.

**Todo List**
- [ ] Yahoo chart 응답의 `meta`(`regularMarketPrice`, `regularMarketTime`, `currentTradingPeriod`, 프리/포스트 필드 유무)를 한국 종목 한두 개로 시간대별 실측한다.
- [ ] 실측 결과로 가격 출처 방침을 정한다. 시간외 가격이 없으면 "마지막 정규장 가격"을 명시한다. 결정은 사용자에게 보고한다.
- [ ] 장중 조회 함수를 만든다(`collectors/quote.py` 신규). `collectors/price.py`의 `fetch`/`USER_AGENT` 사용 방식을 따르고 `same_instrument` 검사를 재사용한다.
- [ ] 보유 종목 목록 로더를 만든다. `tickers.json`의 `qty>0`에 `config/assets.json`의 `price_symbol`을 매핑한다.
- [ ] `quotes.json`을 원자적으로 쓴다. 임시 파일 후 교체한다.
- [ ] `tests/test_watch_quotes.py`를 만든다. 네트워크 없이 고정된 응답으로 파싱과 실패 시 이전값 보존을 검사한다.

**Relevant Context**: `collectors/price.py`(`fetch`, `session_state`, `same_instrument`), `scripts/stock_service.py`(`get_price_info`, `collect_price`, `ASSETS_FILE`, `TICKERS_FILE`), `calculators/portfolio_math.py`(`get_portfolio_status`의 보유 목록 구성)

**Status**: `[ ] pending`

---

### 3. 트리거 워처 (A)
**Intent**: 저장된 판단의 가격 문턱과 현재가를 대조해 문턱 접촉 이벤트를 만든다. 대조는 부등호뿐이다.

**Expected Outcomes**
- 보유 종목별 살아 있는 최신 판단의 문턱이 현재가와 대조된다.
  - `price_plan`: `stop_price`, `scale_out_start`, `reask_below`, `reask_above`, `entry_range_low/high`
  - `exit_plan.stages[].limit_price`(`valid_from`~`valid_until` 범위 안에서만)
- 이벤트는 `knowledge/watch/events.jsonl`에 추가된다. 같은 문턱의 중복 알림은 발생하지 않는다.
- 이벤트에는 판단 ID, 문턱 이름과 값, 현재가, 시각, 그 문턱에 연결된 약속 행동(있는 경우)만 담긴다. 점수나 추천 필드는 없다.

**Todo List**
- [ ] 문턱 목록 추출 함수를 만든다(`cio/watch_triggers.py` 신규). 입력은 판단 JSON, 출력은 `{name, value, direction}` 목록이다.
- [ ] 방향 규칙을 정한다. `stop_price`, `reask_below`는 이하, `scale_out_start`, `reask_above`는 이상, 진입 범위는 범위 안 진입이다. `cio/entry_plan.py`와 `cio/exit_plan.py`의 `observations()`와 판정 방식을 맞춘다.
- [ ] 통화 불일치(판단 통화 vs 시세 통화)는 대조하지 않고 이벤트 대신 오류로 기록한다.
- [ ] 중복 제거 상태 파일(`knowledge/watch/state.json`)을 만든다. 문턱 이탈 시 해제하고 재접촉 시 재발생시킨다.
- [ ] 살아 있는 판단 조회에 `cio/retire.py`의 `survey()`를 재사용한다. 계약 미달·폐기 판단은 대조하지 않는다.
- [ ] `tests/test_watch_triggers.py`를 만든다. 방향별 접촉, 경계값 동일가, 중복 억제, 재진입 재발생, 만료된 exit stage 제외를 검사한다.

**Relevant Context**: `cio/retire.py:74 survey()`, `cio/report.py:133 load_assessment()`, `cio/entry_plan.py`, `cio/exit_plan.py`, `schemas/cio-decision.schema.json`(`price_plan` 49행~, `exit_plan` 275행~)

**Status**: `[ ] pending`

---

### 4. 한도 위반·판단 노후화 감시 (C, D)
**Intent**: 가격 문턱 외에, 시세 변동으로 생기는 한도 위반과 판단의 낡음을 사실로 알린다.

**Expected Outcomes**
- 현재가 기준 평가액으로 `investor-policy` 한도(비중, 손실 한도) 위반 여부가 대조되어 이벤트로 남는다.
- 보유 종목의 최신 판단이 없거나, 오래됐거나, 판단 시점 가격에서 설정 비율 이상 벗어났거나, 실적발표가 임박했으면 "재검토 필요" 이벤트가 남는다. 임계값(며칠, 몇 %)은 `watch.json`에서 사용자가 정한다.
- 비어 있는 설정값에는 기본값을 채우지 않는다(README 정책: 비어 있음은 "정하지 않음"이지 "제한 없음"이 아니다). 정하지 않은 항목은 감시하지 않고 그 사실을 보고한다.

**Todo List**
- [ ] 정책 한도 대조 방식을 정한다. `get_portfolio_status()`의 `allocation_status`·`warnings`와 `cio/policy.py`의 `breach_now` 산술을 재사용할 수 있는지 확인한다. 현재가 갱신을 반영하려면 입력을 `quotes.json` 가격으로 대체해야 하는지 결정한다.
- [ ] 판단 노후화 규칙을 만든다. 판단 없음, 판단 후 경과, 가격 괴리율을 설정값으로 대조한다.
- [ ] 실적발표 임박 판정을 `collectors/kr_calendar.py` 산출물로 대조한다.
- [ ] 이벤트 유형 필드를 추가한다(`PRICE_TRIGGER`, `POLICY_BREACH`, `STALE_ASSESSMENT`, `EARNINGS_NEAR`). 이벤트 문구 어디에도 매수·매도 지시가 없어야 한다.
- [ ] `tests/test_watch_review_events.py`를 만든다. 미설정 항목은 감시하지 않는 경우를 포함한다.

**Relevant Context**: `calculators/portfolio_math.py:137`, `cio/policy.py`, `cio/capital.py`, `collectors/kr_calendar.py`, `config/investor-policy.json`(읽기만 한다)

**Status**: `[ ] pending`

---

### 5. 알림 어댑터 (1차: 콘솔·파일, 2차: 이메일)
**Intent**: 이벤트를 사용자에게 전달하는 채널을 어댑터로 분리한다. 문구가 판정으로 오해되지 않도록 테스트로 고정한다.

**Expected Outcomes**
- 이벤트 하나를 입력받아 문구를 만드는 단일 포매터가 있고, 모든 채널이 같은 문구를 쓴다.
- 1차: 콘솔 출력과 파일(`events.jsonl`) 기록이 동작한다.
- 2차: SMTP 이메일 발송이 동작한다. 자격증명은 `.env`에서 읽는다.
- 채널 한 곳의 실패가 다른 채널과 이벤트 기록을 막지 않는다.
- 금지 표현(예: "매수하세요", "매도하세요", "사세요", "파세요", "추천")이 포매터 출력에 나오지 않음이 테스트로 고정된다.

**Todo List**
- [ ] 포매터를 만든다(`cio/watch_notify.py` 신규). 사실 진술 템플릿만 쓴다.
- [ ] 채널 인터페이스(`send(event_text) -> 성공/실패`)를 정의하고 콘솔·파일 어댑터를 구현한다.
- [ ] 이메일 어댑터를 구현한다(`smtplib` 사용). `.env.example`에 `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `NOTIFY_EMAIL_TO` 같은 키 안내를 추가한다. 실제 값은 `.env`에만 둔다.
- [ ] `config/watch.json`의 활성 채널 목록에 따라 어댑터를 선택한다. 이메일은 기본 비활성이다.
- [ ] README의 키 설정 표에 항목을 추가하고, 값에 따옴표를 쓰지 않는 규칙을 안내한다.
- [ ] `tests/test_watch_notify.py`를 만든다. 금지 표현 검사, 채널 실패 격리, 이메일은 SMTP를 가짜로 대체해 검사한다.

**Relevant Context**: `.env.example`, `scripts/stock_service.py:41 load_env_file()`, `docs/architecture.md`("의도적으로 없는 것"), `tests/test_position_math.py`(경계를 고정하는 테스트 방식)

**Status**: `[ ] pending`

---

### 6. 재질문 큐 (B)
**Intent**: 트리거가 켜진 종목에 대해 모델에게 줄 재질문 패킷을 자동으로 준비해, 사용자가 곧바로 모델에 넘길 수 있게 한다.

**Expected Outcomes**
- `PRICE_TRIGGER` 이벤트가 발생하면 보유 종목의 `HOLD_REVIEW`(또는 분할매도 문턱이면 `TRIM_REVIEW`) 패킷이 생성되고 이벤트에 `context_id`가 연결된다.
- 같은 종목에 미처리 패킷이 있으면 새로 만들지 않는다.
- 12시간이 지난 미사용 패킷은 대기열에서 만료로 표시된다.
- 대기열을 CLI로 조회할 수 있다. 예: `python -m cio watch queue`.

**Todo List**
- [ ] 이벤트 유형·문턱 이름별 intent 매핑표를 정한다. 매핑은 "어떤 질문을 준비하느냐"만 정하고 어떤 답이 맞는지는 정하지 않는다.
- [ ] 보유수량·평단은 원장에서 `build_context()`가 읽는 값을 쓰고, 현재가는 `quotes.json` 값을 `--price`로 넘긴다.
- [ ] 패킷 생성 호출은 `cio/context.py`의 `build_context()`를 직접 호출한다. `validation = BLOCKED`면 이벤트에 사유를 남기고 알림에는 "데이터 준비 필요"로 표시한다.
- [ ] 대기열 상태 파일과 만료 규칙을 만든다.
- [ ] `cio/__main__.py`에 `watch` 하위 명령(`queue`)을 추가한다. `tests/test_cli_wiring.py`, `tests/test_cli_contract.py` 방식에 맞춘다.
- [ ] `tests/test_watch_queue.py`를 만든다. 중복 생성 억제, 만료, BLOCKED 처리를 검사한다.

**Relevant Context**: `cio/__main__.py:149 cmd_prepare`, `cio/context.py`(`build_context`), README의 `HOLD_REVIEW`/`TRIM_REVIEW` 설명, `cio/requests.py`(대기열 패턴 참고)

**Status**: `[ ] pending`

---

### 7. 일일 브리핑 (E)
**Intent**: 하루 1회 보유 종목 현황과 하루 동안의 감시 이력을 한 문서로 요약한다.

**Expected Outcomes**
- 하루 1회(시각은 `watch.json`) 브리핑이 생성되어 파일로 저장되고 활성 채널로 발송된다.
- 브리핑에는 종목별 현재가·전일 대비, 그날의 트리거 이벤트, 미해결 모델 요청(`cio requests`), 대기 중인 재질문 패킷, 최근 채점 요약(`cio evals`)이 사실로 나열된다.
- 같은 날 두 번 발송되지 않는다.

**Todo List**
- [ ] 브리핑 데이터 수집 함수를 만든다(`cio/watch_briefing.py` 신규). `cio/requests.py`, `cio/report.py`의 `evaluations_for()`, `events.jsonl`을 재사용한다.
- [ ] 마크다운 템플릿으로 렌더링하고 `knowledge/watch/briefings/YYYY-MM-DD.md`에 저장한다.
- [ ] 판정 표현 금지 테스트를 브리핑에도 적용한다.
- [ ] 하루 1회 보장 상태를 기록한다.
- [ ] `tests/test_watch_briefing.py`를 만든다.

**Relevant Context**: `cio/requests.py`, `cio/report.py:593 evaluations_for()`, `cio/display.py`, `docs/evaluation.md`

**Status**: `[ ] pending`

---

### 8. 스케줄러 기동과 API·대시보드 연동 (F)
**Intent**: 위 구성요소를 주기적으로 실행하고, 사용자가 대시보드에서 알림을 확인한다.

**Expected Outcomes**
- 감시 루프가 설정된 주기로 [게이트 → 시세 → 트리거 → 알림 → 재질문 준비] 순서로 한 사이클을 실행한다. 단독 실행(`python -m cio watch run`)과 1회 실행(`--once`)을 지원한다.
- 사이클이 겹치지 않는다. 앞 사이클이 길어지면 다음 사이클은 건너뛴다.
- API 서버에 `/api/watch/status`, `/api/watch/events`, `/api/watch/queue`(GET)가 생긴다.
- 대시보드에 읽지 않은 알림 개수 배지와 이벤트 목록이 보인다. API 서버가 오프라인이어도 기존 리포트 뷰는 동작한다.
- `start.ps1`로 감시 루프를 함께 기동·종료할 수 있다.

**Todo List**
- [ ] 한 사이클 함수와 반복 루프를 만든다(`cio/watch_runner.py` 신규). 오류가 나도 루프가 죽지 않고, 사이클 결과(건너뜀/성공/오류)를 상태 파일에 남긴다.
- [ ] `cio/__main__.py`의 `watch` 하위 명령에 `run`, `--once`, `status`를 추가한다.
- [ ] `scripts/api_server.py`에 GET 엔드포인트를 추가한다. 응답은 읽기 전용이고 CORS 헤더는 기존 방식을 따른다.
- [ ] 대시보드 변경 파일을 확정한다. `dashboard/` 아래 `StockControlPanel.tsx`와 리포트 뷰 쪽 중 어디에 배지를 넣을지 파악한다. `StockControlPanel`은 Tailwind 없이 inline style이므로 같은 방식을 따른다. 판정색(`--halt`/`--hold`/`--approved`)과 시세색(`--rise`/`--fall`)은 섞지 않는다.
- [ ] `start.ps1`에 감시 프로세스 기동·종료를 추가한다(`Start-Process`/`Stop-Process` 패턴이 이미 API 서버에 있음).
- [ ] `docs/architecture.md`의 컴포넌트 표에 감시 구성요소를 추가하고, "PC는 문턱 접촉만 알리며 판단하지 않는다"는 경계를 명시한다.
- [ ] 주기를 60분에서 10분으로 바꿨을 때 외부 호출량과 사이클 소요 시간을 확인한다.
- [ ] `tests/test_watch_runner.py`를 만든다(사이클 순서, 게이트 건너뜀, 오류 격리, 겹침 방지).
- [ ] `npm run lint`, `npm run test`(dashboard), `python tests/run.py`를 모두 통과시킨다.

**Relevant Context**: `scripts/api_server.py`(`do_GET` 76행~, `ThreadingHTTPServer` 428행), `start.ps1`, `dashboard/` (`StockControlPanel.tsx`, `rendered-html.test.mjs`), `AGENTS.md`의 대시보드 CSS 규칙

**Status**: `[ ] pending`

---

## 구현 순서와 의존성
1 → 2 → 3 → (4, 5 병행 가능) → 6 → 7 → 8

- 서브태스크 3은 1과 2가 끝나야 시작할 수 있다.
- 서브태스크 5는 이벤트 형식이 정해지는 3 이후에 시작한다.
- 서브태스크 6은 3(이벤트)과 5(알림 문구)에 의존한다.
- 서브태스크 8은 모든 구성요소가 준비된 뒤 통합한다.
