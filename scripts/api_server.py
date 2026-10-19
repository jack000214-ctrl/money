"""로컬 REST API 서버 (Python 3.13 stdlib 전용).

종목 검색, CRUD, 단계별 수집 및 리포트 빌드, 자동 업데이트 API 제공.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from scripts.stock_service import (
    build_report,
    check_and_update_prices,
    collect_fundamentals,
    collect_price,
    delete_asset,
    ensure_initial_report,
    generate_ai_analysis,
    get_asset,
    list_assets,
    save_asset,
    search_stocks,
    select_report,
    get_longterm_analysis,
)
from calculators.portfolio_math import get_policy, save_policy
from scripts.forecast_engine import (
    ALGORITHMS,
    get_forecast,
    interpret_with_bob,
    list_forecasts,
    run_forecast,
    save_interpretation,
)
from calculators.portfolio_math import get_portfolio_status
from cio import watch_api
from cio.llm_interface import LLMInterface


class APIServerHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:
        # 터미널에 간단한 로그만 출력
        sys.stdout.write(f"[{self.log_date_time_string()}] {format % args}\n")
        sys.stdout.flush()

    def _send_cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def _send_json(self, data: Any, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        query = parse_qs(parsed.query)

        watch_response = watch_api.route("GET", path, query, {})
        if watch_response is not None:
            self._send_json(watch_response[1], status=watch_response[0])
            return

        # 포트폴리오 상태 조회
        if path == "/api/portfolio/status":
            try:
                status = get_portfolio_status()
                self._send_json(status)
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        # 투자자 정책 조회
        if path == "/api/portfolio/policy":
            try:
                self._send_json(get_policy())
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        # 장기분석 요약 조회
        if path == "/api/longterm/status":
            try:
                analysis = get_longterm_analysis()
                self._send_json({"results": analysis})
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        # 1. 종목 검색
        if path == "/api/search":
            q = query.get("q", [""])[0]
            results = search_stocks(q)
            self._send_json({"results": results})
            return

        # 2. 등록 종목 목록
        if path == "/api/assets":
            assets = list_assets()
            self._send_json({"assets": assets})
            return

        # 3. 단일 종목 조회 (/api/assets/<ticker>)
        if path.startswith("/api/assets/"):
            ticker = path.split("/api/assets/")[1]
            asset = get_asset(ticker)
            if asset:
                self._send_json(asset)
            else:
                self._send_json({"error": "종목을 찾을 수 없습니다."}, status=HTTPStatus.NOT_FOUND)
            return

        # 4. 지원 알고리즘 목록
        if path == "/api/forecast/algorithms":
            self._send_json({"algorithms": ALGORITHMS})
            return

        # 5. 예측 리포트 목록
        if path == "/api/forecast/list":
            limit = int(query.get("limit", ["30"])[0])
            summaries = list_forecasts(limit=limit)
            self._send_json({"forecasts": summaries})
            return

        # 6. 예측 리포트 단건 조회
        if path.startswith("/api/forecast/"):
            report_id = path.split("/api/forecast/")[1]
            report = get_forecast(report_id)
            if report:
                self._send_json(report)
            else:
                self._send_json({"error": "리포트를 찾을 수 없습니다."}, status=HTTPStatus.NOT_FOUND)
            return

        # 7. 자동 업데이트 상태 확인
        if path == "/api/auto-update-status":
            from datetime import date as _date, timedelta as _td
            _today = _date.today()
            if _today.weekday() == 5:
                _target = _today - _td(days=1)
            elif _today.weekday() == 6:
                _target = _today - _td(days=2)
            else:
                _target = _today
            _target_str = _target.isoformat()
            assets = list_assets()
            stale = [
                a for a in assets
                if not a.get("price_info", {}).get("exists")
                or a.get("price_info", {}).get("last_date") != _target_str
                or a.get("price_info", {}).get("session") == "UNSETTLED"
            ]
            self._send_json({
                "total": len(assets),
                "stale_candidates": [a["ticker"] for a in stale],
            })
            return

        # 헬스체크
        if path == "/api/health" or path == "":
            self._send_json({"status": "ok", "service": "Money API"})
            return

        self._send_json({"error": "Not Found"}, status=HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        content_length = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(content_length) if content_length > 0 else b"{}"
        try:
            body = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
        except Exception:
            body = {}

        watch_response = watch_api.route("POST", path, parse_qs(parsed.query), body)
        if watch_response is not None:
            self._send_json(watch_response[1], status=watch_response[0])
            return

        # 투자자 정책 저장
        if path == "/api/portfolio/policy":
            try:
                saved = save_policy(body)
                self._send_json({"success": True, "policy": saved})
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        # 포트폴리오 리밸런싱 및 AI 추천서 생성
        if path == "/api/portfolio/recommend":
            try:
                # 1. 정량 데이터 가져오기
                portfolio_data = get_portfolio_status()
                longterm_data = get_longterm_analysis()

                # 보유 중인 종목만 요약 정보 생성
                held_assets_summary = []
                for item in longterm_data:
                    ticker = item["ticker"]
                    matching_exposure = next((exp for exp in portfolio_data.get("exposures", []) if exp["asset_id"] == ticker), None)
                    if matching_exposure and matching_exposure.get("value_krw", 0) > 0:
                        held_assets_summary.append({
                            "ticker": ticker,
                            "name": item["name"],
                            "current_price": item["current_price"],
                            "value_krw": matching_exposure["value_krw"],
                            "effective_pct": matching_exposure["effective_pct"],
                            "has_calc": item["has_calc"],
                            "bear_value": item["bear_value"],
                            "base_value": item["base_value"],
                            "bull_value": item["bull_value"],
                            "safety_margin_pct": item["safety_margin_pct"],
                            "expected_return_pct": item["expected_return_pct"],
                        })

                # 2. AI 추천서 작성을 위한 가독성 높은 프롬프트 작성
                user_prompt = f"""
안녕하세요, CIO로서 저의 보유 포트폴리오를 진단하고 리밸런싱 및 투자 추천 자문서(Korean)를 작성해 주세요.

[포트폴리오 총평 정보]
- 총 평가금액: {portfolio_data.get('total_value_krw', 0):,} KRW
- 보유 자산 수: {portfolio_data.get('asset_count', 0)}개
- 통화 비중 노출: {json.dumps(portfolio_data.get('currency_exposure', {}), ensure_ascii=False)}
- 섹터 비중 노출: {json.dumps(portfolio_data.get('sector_exposure', {}), ensure_ascii=False)}
- 경고 및 규율 검토 결과: {json.dumps(portfolio_data.get('warnings', []), ensure_ascii=False)}
- 적용 한도 기준: {json.dumps(portfolio_data.get('limits_applied', {}), ensure_ascii=False)}

[개별 보유 종목의 밸류에이션 및 기대수익 시나리오]
{json.dumps(held_assets_summary, ensure_ascii=False, indent=2)}

위 데이터를 기반으로 다음 세 가지 섹션을 포함한 가독성이 높은 진단 마크다운 리포트를 한국어로 작성해 주세요:
1. **포트폴리오 자산 배분 총평**: 현재 비중 및 편중 리스크, 한도 위반 여부에 대한 명확한 진단
2. **보유 자산 내재가치 및 기대수익 대조**: 각 종목의 안전마진(내재가치 대비 저평가율)과 기대수익률 관점에서의 포지션 평가
3. **구체적인 포트폴리오 리밸런싱 & 액션 제안**: 리스크 완화 및 수익률 극대화를 위해 비중을 확대할 종목, 축소/유지할 종목과 그 이유 및 향후 모니터야 할 핵심 무효화 시그널
"""

                # 3. LLM 인터페이스를 통해 Bob AI 구동
                llm = LLMInterface()
                if not llm.available:
                    self._send_json({"recommendation": "### AI 자문서 로딩 불가\n\n현재 `BOB_API_KEY` 혹은 `BOB_TOKEN` 설정이 없거나 LLM 인터페이스를 사용할 수 없는 환경입니다. `.env` 파일을 점검해 주세요."})
                    return

                system_message = (
                    "당신은 금융 시장과 자산배분론에 정통한 최고투자책임자(CIO)이자 냉철하고 이성적인 포트폴리오 매니저입니다.\n"
                    "어떤 상황에서도 사용자의 눈치를 보지 않고 오직 정량적 밸류에이션 사실과 포트폴리오 규칙에 입각해 가장 안전하고 효율적인 자문 리포트를 작성합니다.\n"
                    "격식 있고 신뢰감을 주는 서체와 마크다운 형식으로 한글로만 대답하십시오."
                )
                recommendation_text = llm.generate(user_prompt, system_message=system_message)
                self._send_json({"recommendation": recommendation_text})
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        # 1. 종목 등록
        if path == "/api/assets":
            try:
                result = save_asset(body)
                self._send_json(result, status=HTTPStatus.CREATED)
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return

        # 2. 시세 수집
        if path == "/api/collect/price":
            ticker = body.get("ticker", "")
            range_str = body.get("range", "5y")
            result = collect_price(ticker, range_str)
            self._send_json(result)
            return

        # 3. 재무/공시 수집
        if path == "/api/collect/fundamentals":
            ticker = body.get("ticker", "")
            result = collect_fundamentals(ticker)
            self._send_json(result)
            return

        # 4. 리포트 빌드
        if path == "/api/collect/build":
            ticker = body.get("ticker", "")
            select_val = body.get("select", True)
            result = build_report(ticker, select_for_dashboard=select_val)
            self._send_json(result)
            return

        # 5. Bob AI 투자 판단 및 내러티브 생성
        if path == "/api/collect/ai-analyze":
            ticker = body.get("ticker", "")
            result = generate_ai_analysis(ticker)
            self._send_json(result)
            return

        # 6. 전체 파이프라인 일괄 실행 (시세 -> 실적 -> 리포트 빌드 -> AI 분석)
        if path == "/api/collect/all":
            ticker = body.get("ticker", "")
            logs = []

            p_res = collect_price(ticker)
            logs.append(f"1. 시세 수집: {'성공' if p_res.get('success') else '실패'}\n{p_res.get('output', '')}")

            f_res = collect_fundamentals(ticker)
            logs.append(f"2. 재무/공시 수집:\n{f_res.get('output', '')}")

            b_res = build_report(ticker, select_for_dashboard=True)
            logs.append(f"3. 리포트 빌드: {'성공' if b_res.get('success') else '실패'}\n{b_res.get('output', '') or b_res.get('error', '')}")

            a_res = generate_ai_analysis(ticker)
            logs.append(f"4. Bob AI 투자 판단: {'성공' if a_res.get('success') else '실패'}\n{a_res.get('output', '') or a_res.get('error', '')}")

            self._send_json({
                "success": a_res.get("success", False),
                "output": "\n\n".join(logs),
            })
            return

        # 단일 종목 AI 해석 (Bob Shell 호출 — 30초~1분 소요)
        if path == "/api/forecast/interpret":
            report_id = body.get("report_id", "")
            ticker = body.get("ticker", "")
            if not report_id or not ticker:
                self._send_json({"success": False, "error": "report_id, ticker 필수"}, status=HTTPStatus.BAD_REQUEST)
                return
            report = get_forecast(report_id)
            if not report:
                self._send_json({"success": False, "error": "리포트를 찾을 수 없습니다."}, status=HTTPStatus.NOT_FOUND)
                return
            target = next((r for r in report.get("results", []) if r["ticker"] == ticker), None)
            if not target:
                self._send_json({"success": False, "error": f"{ticker} 결과가 리포트에 없습니다."}, status=HTTPStatus.NOT_FOUND)
                return
            interp = interpret_with_bob(target, report.get("algorithm_label", ""))
            # 해석 결과를 리포트에 캐시 저장 (재요청 비용 절감)
            if interp.get("success"):
                interp["saved"] = save_interpretation(report_id, ticker, interp)
                if not interp["saved"]:
                    sys.stdout.write(f"[interpret] 저장 실패: {report_id} {ticker} (리포트 삭제됨?)\n")
            else:
                sys.stdout.write(f"[interpret] 해석 실패: {report_id} {ticker}: {interp.get('error', '')[:200]}\n")
            sys.stdout.flush()
            self._send_json(interp)
            return

        # 예측 실행
        if path == "/api/forecast":
            algorithm = body.get("algorithm", "MA_CROSS")
            if algorithm not in ALGORITHMS:
                self._send_json({"error": f"지원하지 않는 알고리즘: {algorithm}"}, status=HTTPStatus.BAD_REQUEST)
                return
            try:
                assets = list_assets()
                report = run_forecast(assets, algorithm)
                self._send_json({"success": True, "report": report})
            except Exception as exc:
                self._send_json({"success": False, "error": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        # 대시보드 표시 종목 선택
        if path == "/api/select":
            ticker = body.get("ticker", "")
            result = select_report(ticker)
            self._send_json(result)
            return

        # 7. 오늘 시세 자동 업데이트 실행
        if path == "/api/auto-update":
            result = check_and_update_prices()
            self._send_json(result)
            return

        self._send_json({"error": "Not Found"}, status=HTTPStatus.NOT_FOUND)

    def do_PUT(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        content_length = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(content_length) if content_length > 0 else b"{}"
        try:
            body = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
        except Exception:
            body = {}

        watch_response = watch_api.route("PUT", path, parse_qs(parsed.query), body)
        if watch_response is not None:
            self._send_json(watch_response[1], status=watch_response[0])
            return

        if path.startswith("/api/assets/"):
            ticker = path.split("/api/assets/")[1]
            body["ticker"] = ticker
            try:
                result = save_asset(body)
                self._send_json(result)
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return

        self._send_json({"error": "Not Found"}, status=HTTPStatus.NOT_FOUND)

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path.startswith("/api/assets/"):
            ticker = path.split("/api/assets/")[1]
            result = delete_asset(ticker)
            self._send_json(result)
            return

        self._send_json({"error": "Not Found"}, status=HTTPStatus.NOT_FOUND)


def run_server(port: int = 3001, host: str = "127.0.0.1") -> None:
    ensure_initial_report()
    server = ThreadingHTTPServer((host, port), APIServerHandler)
    print(f"Money Backend API Server running at http://{host}:{port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server...")
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Money API Server")
    parser.add_argument("--port", type=int, default=3001, help="Server port (default: 3001)")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    args = parser.parse_args()
    run_server(args.port, args.host)
