# 감시 배치 구현 진행 기록 (새 채팅 인수인계용)

> 새 채팅은 이 파일 → `watch-batch-plan.md` 해당 서브태스크 → 아래 "코드 위치" 순으로만 읽고 시작한다.
> 이미 끝난 모듈은 **다시 읽지 말고** 아래 "공개 인터페이스" 요약만 신뢰한다.
> 규칙: AGENTS.md 준수(커밋 금지, stdlib만, `cio/paths.py` 상수, 테스트 먼저). 작업이 끝나면 이 파일의 해당 단계를 갱신한다.

## 사용자 추가 요구 (계획서 밖)
- 대시보드 **메뉴 맨 끝에 "설정" 탭**을 둔다: ① 이메일 발송 설정 ② 배치 시간 설정.
- 참고 구현: `C:\Project\project-assistant\src\web\templates\settings_notifications.html`, `src\core\notification_sender.py`(`_send_email`, 286행 부근). 이미 읽어서 `cio/watch_notify.py`에 반영함 — 다시 읽을 필요 없음.
- SMTP 비밀번호는 `.env`의 `SMTP_PASSWORD`에만 있다(이미 존재). 설정 파일·API 응답에 싣지 않는다. 화면에는 "설정됨/없음"만 표시.
- 호스트·포트·계정·발신자·수신자 목록·활성 채널·주기·감시 시간·브리핑 시각은 `config/watch.json`을 화면에서 편집.

## 단계 분할 (채팅 단위)

| 채팅 | 범위 | 상태 |
|---|---|---|
| A | 서브태스크 1~5 코어(설정·게이트·시세·트리거·리뷰·알림) | **완료** |
| B | 서브태스크 6(재질문 큐) + 7(일일 브리핑) + 8의 러너/CLI | **완료** |
| C | API 엔드포인트 + 설정 화면(백엔드: 설정 GET/PUT, 테스트 메일) | **완료** |
| D | 대시보드: 알림 탭/배지 + 설정 탭 + `start.ps1` + 문서/README + 전체 검증 | **완료** |

각 채팅은 끝날 때 `python tests/run.py`가 통과해야 하고, 이 파일을 갱신한다.

## 완료된 것 (채팅 A)

테스트 90개 통과: `python -m unittest tests.test_watch_config tests.test_watch_quotes tests.test_watch_triggers tests.test_watch_review_events tests.test_watch_notify`

### 코드 위치와 공개 인터페이스

**`cio/paths.py`** — `WATCH_DIR = KNOWLEDGE_DIR / "watch"` 추가. 저장소 모듈은 `from cio import paths` 후 `paths.WATCH_DIR`을 호출 시점에 읽는다(테스트가 갈아끼움).

**`config/templates/watch.json`** — 템플릿. 실제 `config/watch.json`은 아직 없음(없으면 템플릿 값). `.gitignore` 추가는 D 단계에서 결정(이메일 주소 포함 → 추가 권장).

**`cio/watch_config.py`**
- `load_config(path=None)`, `save_config(changes, path=None)`(부분 병합, 수신자 배열은 통째 교체, 검증 실패 시 미저장, 모르는 키 무시), `validate_config`, `default_config`
- `gate(now, config) -> {decision: RUN|SKIP, reason: WEEKEND|BEFORE_WINDOW|AFTER_WINDOW|None, message, at}` 시작 포함·종료 미포함
- `unset_review_fields(config)` — 미정(null) 감시 항목 보고용
- 설정 구조: `timezone, window{start,end}, interval_minutes, markets["KR"], channels{console,file,dashboard,email}, email{smtp_host,smtp_port,smtp_user,smtp_from,recipients[{name,address,enabled}]}, review{stale_after_days,price_drift_pct,earnings_within_business_days}, briefing{enabled,time}`
- 상수 `CHANNELS`, `REVIEW_FIELDS`, `WatchConfigError(.errors)`

**`cio/watch_store.py`** — 파일 I/O
- `load_quotes/save_quotes`, `load_state/save_state`(키: `active_triggers`, `last_cycle`, `briefing_sent_dates`, `events_read_through`), `load_queue/save_queue`(`{"items":[]}`)
- `append_event(event)->row(seq 부여)`, `read_events(since_seq=0, limit=None)`, `update_event(seq, {context_id|queue_status|queue_note})`(허용 필드 외 거부), `unread_count()`, `mark_read_through(seq=None)`
- `briefings_dir()`, `now_iso()`, `watch_dir()`

**`collectors/quote.py`** — 장중 현재가
- 실측 결과(문서화됨): Yahoo는 한국 종목에 `preMarketPrice/postMarketPrice`를 주지 않음 → 08~09시·15:30~20시는 `price_kind=LAST_REGULAR`
- `load_holdings(markets=("KR",))`, `collect(holdings, fetcher=fetch_meta, now=None, save=True) -> {updated_at, quotes, ok, failed}`
- 시세 행: `price, currency, quote_time, session(REGULAR|BEFORE_REGULAR|AFTER_REGULAR|UNKNOWN), price_kind, fresh_today, comparable, previous_close, status(OK|FAILED), fetched_at, last_error`
- `comparable=False`(시세 시각이 오늘이 아님 = 개장 전/평일 휴장)이면 트리거 대조 제외. 실패 시 이전 가격 보존하되 `comparable=False`.

**`cio/watch_triggers.py`** — 부등호 대조
- `evaluate_asset(ticker, name, assessment, quote, active, today, now_iso) -> {events, errors, active, skipped}`
- `latest_live_assessment(ticker)` — `cio.retire.survey()` 재사용
- `intent_for(name)` — `scale_out_start`/`exit_stage:*`→`TRIM_REVIEW`, 나머지→`HOLD_REVIEW`
- 이벤트 type `PRICE_TRIGGER`, 필드: `seq(저장시), ticker, name, assessment_id, threshold{name,label,direction,value[,value_high]}, price, currency, quote_time, price_kind, detected_at, intent, promised_action, invalidation_signals`
- 중복 제거 키 `"{ticker}|{assessment_id}|{문턱}"` → `state["active_triggers"]`

**`cio/watch_review.py`** — 한도·노후화·실적
- `policy_conditions(holdings, quotes, policy, sectors, average_costs, now_iso)`, `staleness_conditions(holdings, quotes, assessments, asked_prices, review, today, now_iso)`, `earnings_conditions(holdings, calendars, review, today, now_iso, business_days_until)`, `apply_dedup(active, conditions, evaluated_prefixes, now_iso) -> (events, new_active)`, `load_calendar(ticker, knowledge_dir)`
- 반환의 `not_monitored[{item,reason}]`는 상태 화면·브리핑에 보고해야 함. 키 접두사 `REV|`.
- 이벤트 type: `POLICY_BREACH`, `STALE_ASSESSMENT`(reason `NO_LIVE_ASSESSMENT|AGE|PRICE_DRIFT`), `EARNINGS_NEAR`

**`cio/watch_notify.py`**
- `format_event(event)->{title,body}`, `format_digest(events)`, `FORBIDDEN_PHRASES`
- `dispatch(events, config, email_sender=None) -> {채널: {ok, reason}}`, `dispatch_message(subject, body, config)`(브리핑용)
- `send_email(subject, body, config, env_file=None, smtp_factory=None) -> (ok, reason)`, `resolve_email_settings`, `smtp_password_present()`, `read_env(name)`
- 이벤트에 `context_id`/`queue_status(BLOCKED|PENDING_EXISTS)`/`queue_note`가 있으면 문구에 반영됨
- 콘솔 cp949 인코딩 오류는 채널 실패로 보지 않고 `replace`로 출력(수정 완료)

### 테스트 파일
`tests/test_watch_config.py`, `test_watch_quotes.py`, `test_watch_triggers.py`, `test_watch_review_events.py`, `test_watch_notify.py`

## 완료된 것 (채팅 B)

테스트 51개 추가 통과(watch 전체 141개): `python -m unittest tests.test_watch_queue tests.test_watch_briefing tests.test_watch_runner tests.test_watch_cli`

**`cio/watch_queue.py`**
- `prepare_for_events(events, holdings, build=None, now=None) -> list` — 저장된(`seq` 있는) `PRICE_TRIGGER`마다 `build_context(ticker, price, intent, held_quantity, save=True)` 호출(amount 없음). 이벤트 로그에 `queue_status(PREPARED|BLOCKED|PENDING_EXISTS)`/`context_id`/`queue_note`를 덧붙이고 같은 필드를 얹은 사본을 반환. 빌더 예외는 `패킷 준비 실패: <예외 타입명>`만 기록(문자열 노출 없음).
- `expire(now)`, `pending_for(ticker, now)`, `list_items(now)`(만료 처리 후 목록), `resolve(context_id, "USED"|"EXPIRED")`, `format_queue(items)`. 대기열 항목: `{context_id, ticker, name, event_seq, intent, created_at, expires_at(+12h), status: PENDING|EXPIRED|USED}`.

**`cio/watch_briefing.py`**
- `build_briefing(holdings, quotes, events, queue_items, not_monitored, today, open_requests=, evaluation_summary=) -> {title, body}`, `briefing_due(config, state, now)`(켜짐·시각 경과·오늘 미발송·평일), `send_briefing(config, holdings, quotes, not_monitored, now, dispatch=, **builders)`. 파일 `knowledge/watch/briefings/YYYY-MM-DD.md`. 전달 시도 자체를 하루 1회로 센다.

**`cio/watch_runner.py`**
- `Deps` 데이터클래스(주입점: holdings, collect, assessment, asked_price, average_cost, sectors, policy, calendar, business_days_until, prepare, dispatch, dispatch_message, briefing_builders).
- `run_cycle(now=None, deps=None, config=None) -> last_cycle` — 게이트 SKIP이면 시세 호출 없이 기록. `knowledge/watch/runner.lock`(O_EXCL, 2시간 지나면 죽은 잠금으로 간주)으로 겹침 방지(`SKIPPED/BUSY`). `last_cycle = {at, result: OK|SKIPPED|ERROR, reason, counts{holdings,quotes_ok,quotes_failed,events}, errors[], not_monitored[], channels}`.
  - 이벤트는 알림 **전에** append하고 `active_triggers`를 저장한다(알림이 실패해도 같은 사실이 다시 나가지 않는다).
  - `NO_LIVE_ASSESSMENT`는 사이클마다 `STALE_ASSESSMENT` 이벤트 1회(중복 제거 키로 억제).
  - 정하지 않은 `review.*`는 `not_monitored`에 `review.<이름>`으로 보고된다.
- `maybe_send_briefing(now, deps, config)`, `run_loop(deps, sleep=, max_ticks=, log=)`(30초 틱, 설정 파일을 틱마다 다시 읽음), `status() -> {config_errors, gate, last_cycle, unread, unset_review_fields, smtp_password_set}`(비밀번호 값 없음).

**`cio/__main__.py`** — `watch run [--once]`, `watch status`, `watch queue`. `--once`는 사이클 후 브리핑 점검. 결과 `ERROR`면 종료코드 2. 출력 스트림을 UTF-8(`errors="replace"`)로 재설정.

### 알려진 전체 테스트 상태 (채팅 B 종료 시점)
`python tests/run.py`: 2059개 중 실패 191건. **전부 `config/assets.json`의 `0167A0`(ETF) 등록 오류(`AssetRegistryError: ETF 구조 정보가 필요하다`)에서 비롯**(watch 관련 실패 0건). 이 파일은 사용자가 외부에서 수정 중이라 건드리지 않았다. 사용자가 `0167A0`에 etf 구조를 채우면 해소된다.
→ 실제 `python -m cio watch run --once`도 `cio.retire.survey()`가 레지스트리 전체 검증을 거치므로 해당 종목의 판단 조회가 `errors`에 기록된다(사이클은 죽지 않음).

## 완료된 것 (채팅 C)

테스트 26개 추가 통과(watch 전체 167개): `python -m unittest tests.test_watch_api`

**`cio/watch_api.py`** — HTTP와 분리된 핸들러. `route(method, path, query, body) -> (status, payload) | None`(`/api/watch/` 밖이면 `None`). `scripts/api_server.py`의 `do_GET/do_POST/do_PUT` 맨 앞에서 호출한다(기존 라우트 무변경).

| 메서드·경로 | 응답 |
|---|---|
| GET `status` | `watch_runner.status()` + `not_monitored`(마지막 사이클에서). `smtp_password_set`은 불리언만 |
| GET `events?limit=` | `{events:[{...이벤트, read:bool, text:{title,body}}], unread}` — **최신순**, 기본 50·최대 200, 잘못된 값은 기본값 |
| GET `queue` | `{items}` (`watch_queue.list_items`) |
| POST `events/read` | 본문 `{through_seq?}`(없으면 전부). 정수 아니면 400. `{read_through, unread}` |
| GET `config` | `{config, errors, smtp_password_set}` — 설정 파일이 잘못이면 `config:null`+`errors`(200) |
| PUT·POST `config` | 부분 저장. 설정에 없는 키(비밀번호 등)는 `_prune`으로 버림. 검증 실패 400 `{error, errors}`. `{config, errors:[], smtp_password_set}` |
| POST `test-email` | `{ok, reason}`. 30초 쿨다운(429). 내부 예외는 일반 메시지(500) |
| POST `run-once` | `{cycle}` — 동기 실행(시세 조회 때문에 수십 초 걸릴 수 있음) |

- 모르는 `/api/watch/*`·메서드 불일치는 404. 내부 예외는 응답·로그에 문자열을 싣지 않고 타입 이름만 로그.
- 화면(D) 주의: `events`는 이미 최신순이다. 이벤트 `text`는 서버가 만든 문구이므로 그대로 표시한다(재구성 금지). 설정 PUT 본문은 바꾼 부분만 보내면 되고, 수신자는 배열 전체를 보내야 한다.
- 서버 스모크: 3091 포트로 기동해 `status` 200, `config` 200, 없는 경로 404 확인.

### 전체 테스트 상태 (채팅 C 종료 시점)
`python tests/run.py`: 2085개 중 실패/오류 194건. watch 관련 0건. 대부분 `0167A0` ETF 등록 오류(`AssetRegistryError`) + 기존 cp949 디코드 오류. 채팅 B 때(191건)보다 3건 늘었으나 외부에서 수정 중인 `config/assets.json` 등 비-watch 영역이다.

## 완료된 것 (채팅 D)

- **`dashboard/app/WatchPanels.tsx`(신규)** — `useWatchUnread()`(30초 폴링, API 오프라인이면 예외 없이 마지막 값 유지), `UnreadBadge`, `WatchEventsTab`(상태·재질문 대기열·이벤트 목록, 모두 읽음/새로고침/지금 1회 실행), `WatchSettingsTab`(이메일 발송 설정 + 배치 시간 설정). 이벤트는 서버 순서(최신순)·서버 `text`를 그대로 표시. 비밀번호 입력란 없음(“설정됨/없음”만). 재검토 기준을 비우면 `null`로 저장되고 “감시 안 함” 표시. 저장하지 않은 변경이 있으면 테스트 메일 버튼 비활성(저장된 설정으로 발송하므로). 순수 inline style.
- **`dashboard/app/StockControlPanel.tsx`** — 탭 유니온에 `"watch"`, `"settings"` 추가. `6. 포트폴리오 추천` 뒤에 `7. 감시 알림`(배지), 맨 끝 `8. 설정`. 파일이 3천 줄이라 화면 코드는 `WatchPanels.tsx`로 분리하고 이 파일에는 연결만 추가.
- **`start.ps1`** — 감시 루프(`python -m cio watch run`) 기동·종료 추가(실패해도 대시보드는 계속). 단계 표기 `[n/4]`로 갱신.
- **`.gitignore`** — `config/watch.json` 추가(도구가 이 파일 편집을 막아 셸로 4줄 삽입). **`.env.example`** — `SMTP_PASSWORD` 안내.
- **문서** — `README.md`(키 표·`.env` 예시·“감시 배치” 절을 구현됨으로 전환, CLI·화면·설정 규칙), `docs/architecture.md`(컴포넌트 표 + “PC는 문턱 접촉만 알리며 판단하지 않는다”), `docs/watch-design.md`(상태 “구현됨”, 미결 7건 → 결정 기록).

### 최종 검증 결과 (채팅 D 종료 시점)
- watch 테스트 167개 `OK` (`python -m unittest tests.test_watch_*` 10개 모듈).
- `python tests/run.py`: 2085개 중 실패 11 + 오류 183 = 194건. **watch 관련 0건**, 채팅 C 종료 시점과 동일(`0167A0` ETF 등록 오류 등 비-watch 기존 문제).
- `npm run lint`: 오류 0, 경고 4건(전부 기존 코드: `FinancialTabs.tsx`, `StockControlPanel.tsx` 147·552·813행). `WatchPanels.tsx` 경고 없음.
- `npm run test`(dashboard): 12개 중 4개 실패. **내 변경을 되돌린 상태에서도 동일하게 4개 실패**(`rendered-html.test.mjs` 118·221·235·256행: 리포트 verdict `HOLDINGS_INVALID`, 시나리오 CSS 등) → 감시 기능과 무관한 기존 문제.
- 스모크: API 서버를 3092 포트로 띄워 `/api/watch/status`·`/config` 200, 응답에 비밀번호 값 없음(`smtp_password_set`은 불리언), `start.ps1` 구문 오류 0.
- **미검증**: 브라우저에서의 실제 화면 확인(탭 렌더링·저장·테스트 메일)은 하지 않았다. 실제 SMTP 발송도 하지 않았다.

## 남은 일
- 없음(계획 범위 완료). 사용자가 직접: 브라우저에서 7·8번 탭 동작 확인, 설정 탭에서 SMTP 호스트·수신자 입력 후 테스트 메일 확인, `0167A0` ETF 구조 보완.

## 결정 기록 (계획서 "정해야 할 것"에 대한 답)
1. 시간외 가격: 못 받음 → 정규장 마지막 가격 명시(`LAST_REGULAR`).
2. 평일 휴장일: 시세 시각이 오늘이 아니면 `comparable=False`로 대조 제외(캘린더 없음).
3. 정책 한도 입력: `quotes.json` 가격으로 보유 종목 단위 평가액 재계산(ETF 관통 미반영, 이벤트에 `basis` 표기).
4. 실행 형태: 단독 프로세스 `python -m cio watch run`(결정). API 서버는 읽기·설정만.
5. 재질문 자동 생성: `PRICE_TRIGGER` 이벤트에만 생성(정책/노후화/실적 이벤트는 패킷 만들지 않음).
6. 설정 저장: `config/watch.json`. 비밀번호는 `.env`만.

## 알려진 주의
- 이 환경의 PowerShell 콘솔은 cp949라 한글 출력이 깨져 보인다(테스트 결과는 정상). 테스트 확인은 마지막 줄 `OK`/`Ran N tests`만 본다.
- `cio.retire.survey()`는 `config/assets.json` 전체 검증을 거친다. 현재 `0167A0`(ETF)에 `etf` 구조가 없어 `AssetRegistryError`로 실패한다 → 러너는 종목별 `try/except`로 격리하고 `errors`에 기록해야 한다(채팅 B). `config/assets.json`은 외부에서 수정 중일 수 있으니 임의로 고치지 않는다.
- 외부 변경 파일: `dashboard/scripts/tickers.json`, `config/assets.json`(사용자가 수정함) — 건드리지 않는다.
- `git add/commit` 금지. 끝나면 `git diff --stat`로 보고만.
