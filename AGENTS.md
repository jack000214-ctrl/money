# Codex bootstrap for money (Personal CIO)

이 프로젝트의 공통 지침과 누적 판단은 Claude와 Codex가 같은 정본을 사용한다.
이 `AGENTS.md`는 내용을 복제하는 파일이 아니라 정본의 위치를 알려 주는
부트스트랩이다. 따라서 정본이 바뀌어도 별도 사본을 동기화할 필요가 없다.

이 저장소에는 `CLAUDE.md`가 없다. 프로젝트 지침의 정본은 `README.md`와 `docs/`이며,
판단 규칙의 정본은 `prompts/cio-system.md`, 작업 절차의 정본은
`skills/decide-investment/SKILL.md`다 (`.claude/skills/decide-investment`는
같은 디렉터리를 가리키는 심볼릭 링크다).

## 먼저 세션 모드를 확인한다

사용자는 첫 메시지 맨 앞에 `[분석]` 또는 `[개발]`을 붙인다. **이 접두사가 아래
어떤 파일을 읽을지를 정하므로 다른 무엇보다 먼저 본다.** 접두사가 없으면 질문의
성격으로 판단하되, 실제 매매 질문은 `[분석]`으로 취급한다.

### `[분석]` — 실제 투자 질문

**메모리를 읽지 않는다.** 프로젝트가 정의한 절차 그대로 수행한다.

1. `README.md`의 빠른 시작
2. `skills/decide-investment/SKILL.md` (워크플로)
3. `prompts/cio-system.md` (판단 규칙) + `cio prepare`가 만든 packet

판단에 필요한 것은 전부 packet과 사용자 원문에 있다. 세션 메모리를 섞으면 판단의
입력이 재현 불가능해지고, 같은 종목·같은 가격에 두 번 물었을 때 답이 달라진다.
그런 시스템은 `cio score`로 평가할 수 없다.

### `[개발]` — 시스템 자체를 고치는 세션

역할 경계와 과거 결정이 중요하므로, 저장소를 탐색하거나 계획을 세우기 전에 다음
파일을 순서대로 끝까지 읽는다.

1. `README.md`
2. `/Users/jaehoseo/.claude/projects/-Users-jaehoseo-Desktop-vswrk-money/memory/MEMORY.md`
3. 현재 요청과 관련된 `docs/` 문서 (`architecture.md`, `data-policy.md`,
   `decision-contract.md`, `evaluation.md`)

`MEMORY.md` 안의 상대 링크는 그 파일이 있는 `memory/` 디렉터리를 기준으로 해석하고,
현재 요청과 관련된 링크만 추가로 읽는다. 이미 기록된 결정·기각된 제안·검증 완료
사실을 처음부터 다시 추적하거나 재검증하지 않는다. 사용자가 명시적으로 요청했거나
현재 저장소 상태와 기록이 충돌할 때만 다시 확인한다.

## 두 세션 모두에 적용되는 것

역할 경계가 이 프로젝트의 핵심이다 — 사용자는 종목·가격·금액을 고르고 주문을
결정하며, 로컬 PC는 수집·계산·검사만 하고, 모델이 모든 투자 판단을 한다. PC의
`validation = PASS | BLOCKED`은 데이터 준비 상태이고, 모델의 `verdict`가 투자
판단이다. 둘을 합치거나 PC 검사 결과를 판단처럼 표현하지 않는다. 파이썬 규칙으로
판정을 대신 만들지 않는다.

**커밋은 사용자가 직접 한다.** 코드만 고치고 `git add`/`git commit`을 실행하지
않으며, 커밋해줄지 묻지도 않는다. 작업을 마치면 `git diff` 요약만 보고하고 멈춘다.

발견한 데이터 함정이나 시스템 결함은 대화나 메모리에만 남기지 말고 계약·패킷·
검사 코드로 옮긴다. 다음 세션의 모델이 읽는 것은 그쪽이다.

기준 런타임은 Python 3.13(`python3.13 -m cio ...`)이고 외부 패키지는 필요 없다.
`config/investor-policy.json`과 보유 내역은 `.gitignore` 대상이므로 그 내용을
저장소나 외부로 옮기지 않는다.

상위 우선순위 지침이나 사용자의 현재 요청과 충돌하면 상위 지침과 현재 요청을
따른다.
