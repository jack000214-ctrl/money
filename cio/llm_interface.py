"""
Personal-CIO LLM 인터페이스 — Bob Shell CLI 연동 모듈.

Bob Shell CLI (`bob run`)를 서브프로세스로 호출하여:
1. CIO 시스템 프롬프트(prompts/cio-system.md) 기반의 투자 판단(BUY_NOW/WAIT/AVOID) 생성
2. 대시보드 리포트용 고품질 내러티브(dashboard/data/narrative/{ticker}.json) 자동 생성
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# 프로젝트 루트 및 경로 설정
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# .env 로드
def _load_env_file() -> None:
    env_file = ROOT_DIR / ".env"
    if env_file.exists():
        try:
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip("'\"")
                if k and k not in os.environ:
                    os.environ[k] = v
        except Exception:
            pass

_load_env_file()

# Bob Shell 실행 설정
_BOB_BIN = os.getenv("BOB_BIN", "bob")
_BOB_TIMEOUT = int(os.getenv("BOB_TIMEOUT", "600"))
_BOB_MODE = os.getenv("BOB_MODE", "agent")


def _bob_available() -> bool:
    """BOB_API_KEY 또는 Bob CLI 사용 가능 여부 확인."""
    return bool(os.getenv("BOB_API_KEY")) or bool(os.getenv("BOB_TOKEN"))


def _strip_preamble(text: str) -> str:
    """Bob 응답 앞뒤의 메타 안내 문구를 정리합니다."""
    text = text.strip()
    # 마크다운 코드 블록(```json ... ```) 추출
    json_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if json_match:
        candidate = json_match.group(1).strip()
        if candidate.startswith("{") or candidate.startswith("["):
            return candidate

    return text


def _extract_final_response_json(raw: str) -> str:
    """bob run --format json 출력에서 어시스턴트 최종 응답 추출."""
    last_assistant = None
    result_message = None

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue

        if obj.get("type") == "result":
            msg = obj.get("last_message", "")
            if msg:
                result_message = msg
        elif obj.get("type") == "message" and obj.get("role") == "assistant":
            content = obj.get("content", "")
            if content:
                last_assistant = content

    if result_message:
        return _strip_preamble(result_message)
    if last_assistant:
        return _strip_preamble(last_assistant)

    return _strip_preamble(raw)


def run_bob_shell(
    prompt: str,
    workspace: Optional[str] = None,
    mode: Optional[str] = None,
    allow_file_tools: bool = False,
) -> str:
    """bob run CLI를 서브프로세스로 실행하고 결과 반환."""
    run_mode = mode or _BOB_MODE
    work_dir = workspace or str(ROOT_DIR)

    if allow_file_tools:
        disable_groups = "mcp,browser"
    else:
        disable_groups = "read,edit,command,mcp,browser"

    args = [
        "cmd.exe", "/c", _BOB_BIN, "run",
        "--mode", run_mode,
        "--workspace", work_dir,
        "--format", "json",
        "--disable-tool-groups", disable_groups,
        "--trust", "--accept-license",
    ]

    env = {**os.environ}
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    try:
        proc = subprocess.run(
            args,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_BOB_TIMEOUT,
            env=env,
            cwd=work_dir,
        )
        output = proc.stdout or ""
        if proc.returncode != 0:
            stderr_msg = (proc.stderr or "").strip()
            stdout_msg = output[:300].strip()
            return f"[ERROR] Bob 실행 실패 (코드 {proc.returncode}): {stderr_msg or stdout_msg}"
        return _extract_final_response_json(output)
    except subprocess.TimeoutExpired as exc:
        try:
            exc.process.kill()
        except Exception:
            pass
        return f"[ERROR] Bob 실행 타임아웃 ({_BOB_TIMEOUT}초 초과)"
    except FileNotFoundError:
        return f"[ERROR] '{_BOB_BIN}' 실행 파일을 찾을 수 없습니다."
    except Exception as exc:
        return f"[ERROR] Bob 서브프로세스 오류: {exc}"


class LLMInterface:
    """Personal-CIO 전용 LLM(Bob) 인터페이스."""

    def __init__(self):
        _load_env_file()
        self.available = _bob_available()

    def generate(self, prompt: str, system_message: Optional[str] = None) -> str:
        """기본 프롬프트 질의."""
        full_prompt = ""
        if system_message:
            full_prompt += f"[시스템 지침]\n{system_message}\n\n[사용자 요청]\n"
        full_prompt += prompt
        return run_bob_shell(full_prompt, mode="ask")

    def generate_narrative(
        self,
        ticker: str,
        name: str,
        market: str = "KR",
        price_summary: Optional[Dict[str, Any]] = None,
        fundamentals_summary: Optional[Dict[str, Any]] = None,
        consensus_summary: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """대시보드 의사결정 리포트용 서술(narrative.json)을 Bob을 통해 자동 생성."""
        system_prompt = (
            "당신은 헤지펀드 최고투자책임자(CIO)입니다.\n"
            "주어진 기업 실적, 현금흐름, 컨센서스, 시세 데이터를 엄격하게 분석하여 대시보드 리포트용 JSON을 한국어로 작성하세요.\n"
            "반드시 순수한 JSON 형식만 출력해야 합니다 (마크다운 설명 제외)."
        )

        user_prompt = f"""
다음은 {name}({ticker}, 시장: {market})의 최신 정량 데이터입니다:

1. 시세 및 가격 위치:
{json.dumps(price_summary or {}, ensure_ascii=False, indent=2)}

2. 재무/현금흐름/손익 스냅샷 (DART/EDGAR 원문):
{json.dumps(fundamentals_summary or {}, ensure_ascii=False, indent=2)}

3. 시장 컨센서스 및 증권사 추정치:
{json.dumps(consensus_summary or {}, ensure_ascii=False, indent=2)}

위 데이터를 바탕으로 아래 스키마에 맞는 JSON 객체를 정확히 완성하세요:
{{
  "_note": "산문만 여기 둔다. 숫자는 build_decision_report.py 가 엔진·원문에서 만든다.",
  "decision": {{
    "headline": "핵심 한 줄 요약 (예: 떨어진 건 시장이고, 싼 건 아닙니다)",
    "reason": "구체적인 투자 판단 근거 (시세 등락 원인 분해, 내재가치 대비 현재가, FCF/Capex 구조 등)"
  }},
  "sectionTitles": {{
    "incomeStatement": {{
      "title": "손익 분석 제목",
      "conclusion": "매출총이익 및 영업이익률 추이 요약"
    }},
    "cashFlow": {{
      "title": "현금흐름 분석 제목",
      "conclusion": "영업활동현금흐름 및 유형자산 취득(Capex), FCF 성질 분석"
    }},
    "valuation": {{
      "title": "밸류에이션 분석 제목",
      "conclusion": "시장 멀티플 vs TTM 현금흐름 기준 배수 해석",
      "entryExplanation": "요구수익률과 확률가중치에 기반한 목표 진입가 해설"
    }},
    "peers": {{
      "title": "동종사 비교",
      "conclusion": "동종 섹터 대비 경쟁력 요약"
    }}
  }},
  "interpretations": {{
    "incomeStatement": {{
      "매출": "매출 핵심 해석",
      "매출총이익률": "마진 구조 해석",
      "판매비와관리비": "판관비 통제 해석",
      "영업이익": "영업이익 핵심 기여 부문"
    }}
  }},
  "marketPositioning": {{
    "title": "시장 포지셔닝",
    "conclusion": "수급 및 포지셔닝 분석",
    "items": [
      {{
        "id": "gap",
        "label": "시장 수급 및 포지셔닝",
        "status": "UNKNOWN",
        "value": "공시 데이터 기반",
        "comparison": "-",
        "asOf": "-",
        "interpretation": "주요 기관 수급 및 지분 공시 요약",
        "limitation": "공시 원문 스냅샷 기준",
        "signal": "plain"
      }}
    ],
    "decisionUse": "검증된 재무 스냅샷과 IPS 한도 기준으로 판정됩니다."
  }},
  "peers": {{
    "rows": []
  }},
  "detailedAnalysis": {{
    "title": "무엇을 보고 이렇게 판단했는가",
    "paragraphs": [
      "문단 1: 시장 요인과 종목 고유 요인 분해 분석",
      "문단 2: 실적 및 Capex/FCF의 본질 분석",
      "문단 3: 반대 논지(Counter-thesis) 및 향후 모니터링할 핵심 신호"
    ]
  }},
  "sources": [
    "DART 공시 시스템 / SEC EDGAR",
    "한국거래소(KRX) / Yahoo Finance / FnGuide"
  ]
}}
"""
        response_text = self.generate(user_prompt, system_message=system_prompt)
        
        # JSON 파싱 시도
        try:
            # 코드 블록 제거 후 파싱
            clean_json = _strip_preamble(response_text)
            parsed = json.loads(clean_json)
            if isinstance(parsed, dict) and "decision" in parsed:
                return parsed
        except Exception:
            pass

        # 파싱 실패 시 기본 템플릿 반환
        return {
            "_note": "기본 분석 템플릿 (AI 파싱 대기)",
            "decision": {
                "headline": f"{name} 투자 판단 분석",
                "reason": response_text[:300] if not response_text.startswith("[ERROR]") else f"{name}({ticker})의 재무 스냅샷 기반 분석입니다.",
            },
            "sectionTitles": {
                "incomeStatement": {"title": "손익 분석", "conclusion": "매출 및 영업이익 추이 분석입니다."},
                "cashFlow": {"title": "현금흐름 분석", "conclusion": "FCF 및 Capex 추이 분석입니다."},
                "valuation": {"title": "밸류에이션 분석", "conclusion": "내재가치 및 적정주가 분석입니다.", "entryExplanation": "요구수익률 기준 목표 진입가입니다."},
                "peers": {"title": "동종사 비교", "conclusion": "동종 섹터 지표입니다."},
            },
            "interpretations": {
                "incomeStatement": {
                    "매출": "매출 성장 추이",
                    "매출총이익률": "마진 지표",
                    "판매비와관리비": "비용 지표",
                    "영업이익": "영업 성과",
                }
            },
            "marketPositioning": {
                "title": "시장 포지셔닝",
                "conclusion": "수급 분석 대기",
                "items": [{"id": "gap", "label": "수급 데이터", "status": "UNKNOWN", "value": "-", "comparison": "-", "asOf": "-", "interpretation": "-", "limitation": "-", "signal": "plain"}],
                "decisionUse": "검증된 재무 스냅샷 기준입니다.",
            },
            "peers": {"rows": []},
            "detailedAnalysis": {
                "title": "종목 분석 상세",
                "paragraphs": [response_text if not response_text.startswith("[ERROR]") else f"{name} 종목의 기초 분석입니다."],
            },
            "sources": ["DART / SEC EDGAR", "KRX / Yahoo Finance"],
        }


# 단독 실행 시 테스트
if __name__ == "__main__":
    ticker_arg = sys.argv[1] if len(sys.argv) > 1 else "005930"
    llm = LLMInterface()
    print(f"=== Bob Shell LLM Interface Test ({ticker_arg}) ===")
    print(f"Bob Available: {llm.available}")
    test_result = llm.generate(f"{ticker_arg} 종목의 최근 반도체 산업 내 입지와 핵심 관전 포인트를 3줄 요약해주세요.")
    print("\n[응답 결과]:")
    print(test_result)
