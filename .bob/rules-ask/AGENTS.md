# AGENTS.md — Ask 모드

This file provides guidance to agents when working with code in this repository.

## 코드베이스 탐색 시 주의사항

- **진실의 원천 위계**: 계약·스키마·검사 코드 > `docs/` 문서 > 대화·메모리. 문서보다 `cio/decision.py`와 `schemas/cio-decision.schema.json`이 우선.
- **`validation` ≠ `verdict`**: `PASS|BLOCKED`는 데이터 준비, `verdict`는 투자 판단. 이 둘을 동의어로 설명하면 안 됨.
- **`.claude/skills/decide-investment`**는 `skills/decide-investment/`를 가리키는 심볼릭 링크. 파일이 두 곳에 있는 게 아님.
- **`dashboard/` 는 Next.js처럼 보이지만** 실제 빌드는 `vinext` (Vite 기반 RSC). `next.config.js` 없음.
- **Python 테스트 100개+**: `tests/run.py` 로만 전체 실행. 개별 테스트는 `python3.13 -m unittest tests.test_<name>`.
- **대시보드 테스트**는 반드시 `npm run build` 후 실행 (`npm run test` 가 build 포함).
- `config/investor-policy.json`, `knowledge/`, `.env` — gitignore 대상. 질문에 그 내용을 포함하거나 외부에 공개하지 않음.
