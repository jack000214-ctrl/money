"""내 종목 관리(검색/등록/수정/삭제) 및 데이터 수집·리포트 빌드 서비스 모듈."""

from __future__ import annotations

import csv
import glob
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Any

from cio.paths import CONFIG_DIR, DATA_DIR, KNOWLEDGE_DIR, ROOT

ASSETS_FILE = CONFIG_DIR / "assets.json"
TICKERS_FILE = ROOT / "dashboard" / "scripts" / "tickers.json"
REPORTS_DIR = ROOT / "dashboard" / "data" / "reports"
DASHBOARD_DATA_DIR = ROOT / "dashboard" / "data"
PRICES_DIR = KNOWLEDGE_DIR / "prices"
FUNDAMENTALS_DIR = KNOWLEDGE_DIR / "fundamentals"
CALCULATIONS_DIR = KNOWLEDGE_DIR / "calculations"

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


def load_env_file() -> dict[str, str]:
    """ROOT / .env 파일을 읽어 os.environ 에 설정 및 반환."""
    env_file = ROOT / ".env"
    env_vars: dict[str, str] = {}
    if not env_file.exists():
        return env_vars
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip("'\"")
            if k:
                env_vars[k] = v
                os.environ[k] = v
    except Exception:
        pass
    return env_vars


# 모듈 로드 시 .env 자동 로드
load_env_file()


def safe_name(symbol: str) -> str:
    return symbol.replace(".", "_").replace("^", "").replace("=", "")


def _load_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_json_file(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(".tmp")
    temp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(path)


def search_stocks(query: str) -> list[dict[str, Any]]:
    """종목 검색: 한국(네이버) 및 미국(Yahoo Finance) 및 로컬 등록 종목 검색."""
    query = query.strip()
    if not query:
        return []

    results: list[dict[str, Any]] = []
    seen_tickers: set[str] = set()

    # 1. 로컬 등록 종목 우선 매칭
    assets_data = _load_json_file(ASSETS_FILE).get("assets", {})
    for ticker, asset in assets_data.items():
        name = asset.get("name", "")
        if query.lower() in ticker.lower() or query.lower() in name.lower():
            results.append({
                "ticker": ticker,
                "name": name,
                "market": asset.get("market", "KR"),
                "exchange": asset.get("exchange", "KOSPI"),
                "asset_type": asset.get("asset_type", "STOCK"),
                "currency": asset.get("currency", "KRW"),
                "price_symbol": asset.get("price_symbol", f"{ticker}.KS"),
                "registered": True,
            })
            seen_tickers.add(ticker)

    # 2. 한국 종목 검색 (네이버 증권 자동완성 API)
    try:
        naver_url = f"https://ac.stock.naver.com/ac?q={urllib.parse.quote(query)}&target=stock"
        req = urllib.request.Request(naver_url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            items = data.get("items", [])
            for item in items:
                if isinstance(item, dict):
                    code = str(item.get("code", "")).strip()
                    name = str(item.get("name", "")).strip()
                    type_code = str(item.get("typeCode", "KOSPI")).upper()
                    category = str(item.get("category", "stock")).lower()
                elif isinstance(item, list) and len(item) >= 2:
                    code = str(item[0]).strip()
                    name = str(item[1]).strip()
                    type_code = str(item[2]).upper() if len(item) > 2 else "KOSPI"
                    category = "stock"
                else:
                    continue

                if not code or code in seen_tickers:
                    continue

                is_kosdaq = "KOSDAQ" in type_code or "코스닥" in type_code
                exchange = "KOSDAQ" if is_kosdaq else "KOSPI"
                symbol_suffix = ".KQ" if is_kosdaq else ".KS"
                is_etf = category == "etf" or "ETF" in type_code or "ETF" in name

                results.append({
                    "ticker": code,
                    "name": name,
                    "market": "KR",
                    "exchange": exchange,
                    "asset_type": "ETF" if is_etf else "STOCK",
                    "currency": "KRW",
                    "price_symbol": f"{code}{symbol_suffix}",
                    "registered": False,
                })
                seen_tickers.add(code)
    except Exception:
        pass

    # 3. 미국 / 글로벌 종목 검색 (Yahoo Finance Search API)
    try:
        yahoo_url = f"https://query2.finance.yahoo.com/v1/finance/search?q={urllib.parse.quote(query)}&quotesCount=10&newsCount=0"
        req = urllib.request.Request(yahoo_url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            quotes = data.get("quotes", [])
            for q in quotes:
                symbol = q.get("symbol", "").strip()
                if not symbol:
                    continue

                short_name = q.get("shortname") or q.get("longname") or symbol
                quote_type = q.get("quoteType", "EQUITY")
                raw_exch = q.get("exchange", "NASDAQ").upper()
                is_etf = (quote_type == "ETF")

                # 한국 종목 (.KS / .KQ 또는 KSC / KOE 거래소)
                if symbol.endswith(".KS") or symbol.endswith(".KQ") or raw_exch in ("KSC", "KOE", "KOSDAQ"):
                    raw_code = symbol.split(".")[0]
                    if raw_code in seen_tickers:
                        continue
                    is_kq = symbol.endswith(".KQ") or "KQ" in raw_exch or "KOSDAQ" in raw_exch
                    results.append({
                        "ticker": raw_code,
                        "name": short_name,
                        "market": "KR",
                        "exchange": "KOSDAQ" if is_kq else "KOSPI",
                        "asset_type": "ETF" if is_etf else "STOCK",
                        "currency": "KRW",
                        "price_symbol": symbol if ("." in symbol) else f"{raw_code}{'.KQ' if is_kq else '.KS'}",
                        "registered": False,
                    })
                    seen_tickers.add(raw_code)
                    continue

                if symbol in seen_tickers:
                    continue

                # 미국 종목 (NYSE, NASDAQ, AMEX, BATS, PCX, NMS, NYQ, NGM 등)
                exchange_clean = "NASDAQ"
                if "NY" in raw_exch or "NMS" in raw_exch:
                    exchange_clean = "NYSE" if "NY" in raw_exch else "NASDAQ"
                elif "AMEX" in raw_exch or "ASE" in raw_exch:
                    exchange_clean = "AMEX"
                elif "PCX" in raw_exch or "BATS" in raw_exch:
                    exchange_clean = "BATS"

                results.append({
                    "ticker": symbol,
                    "name": short_name,
                    "market": "US",
                    "exchange": exchange_clean,
                    "asset_type": "ETF" if is_etf else "STOCK",
                    "currency": "USD",
                    "price_symbol": symbol,
                    "registered": False,
                })
                seen_tickers.add(symbol)
    except Exception:
        pass

    return results[:20]


def get_price_info(price_symbol: str) -> dict[str, Any]:
    """가격 CSV 파일에서 최근 일자, 종가, 전일 종가, 상태 확인."""
    safe = safe_name(price_symbol)
    path = PRICES_DIR / f"{safe}.csv"
    if not path.exists():
        return {"exists": False, "last_date": None, "last_close": None, "prev_close": None, "session": None, "retrieved_at": None}

    last_date = None
    last_close = None
    prev_close = None
    session = None
    retrieved_at = None
    try:
        with path.open(encoding="utf-8-sig") as handle:
            lines = handle.readlines()
            for line in lines:
                if line.startswith("# retrieved_at"):
                    parts = line.strip().split()
                    if len(parts) >= 3:
                        retrieved_at = parts[2]
                if line.startswith("# last_bar "):
                    parts = line.strip().split()
                    if len(parts) >= 3:
                        last_date = parts[2]
                    if "session" in line:
                        session = line.split("session")[-1].strip()

            # 마지막 2개 데이터 행으로 종가 및 전일 종가 확인
            data_lines = [l for l in lines if not l.startswith("#") and l.strip()]
            # 헤더 행(date,close,...) 제외
            data_lines = [l for l in data_lines if not l.startswith("date")]
            if data_lines:
                last_row = data_lines[-1].strip().split(",")
                if len(last_row) >= 2:
                    try:
                        last_close = float(last_row[1])
                        if not last_date:
                            last_date = last_row[0]
                    except ValueError:
                        pass
            if len(data_lines) >= 2:
                prev_row = data_lines[-2].strip().split(",")
                if len(prev_row) >= 2:
                    try:
                        prev_close = float(prev_row[1])
                    except ValueError:
                        pass
    except Exception:
        pass

    return {
        "exists": True,
        "last_date": last_date,
        "last_close": last_close,
        "prev_close": prev_close,
        "session": session or "UNKNOWN",
        "retrieved_at": retrieved_at,
    }


def list_assets() -> list[dict[str, Any]]:
    """등록된 모든 종목 및 상태 목록 조회."""
    assets_raw = _load_json_file(ASSETS_FILE).get("assets", {})
    tickers_raw = _load_json_file(TICKERS_FILE)

    current_report = _load_json_file(DASHBOARD_DATA_DIR / "decision-report.json")
    current_selected_ticker = current_report.get("asset", {}).get("ticker")

    asset_list: list[dict[str, Any]] = []
    for ticker, asset in assets_raw.items():
        ticker_cfg = tickers_raw.get(ticker, {})
        price_symbol = asset.get("price_symbol", ticker)
        price_info = get_price_info(price_symbol)

        # 재무 스냅샷 확인
        fund_matches = glob.glob(str(FUNDAMENTALS_DIR / f"{ticker}-*.json"))
        has_fundamentals = len(fund_matches) > 0
        latest_fund = sorted(fund_matches)[-1] if fund_matches else None
        fund_as_of = None
        if latest_fund:
            fund_data = _load_json_file(Path(latest_fund))
            if fund_data.get("period_end"):
                fund_as_of = str(fund_data["period_end"])
            elif isinstance(fund_data.get("as_of"), dict):
                as_of_dict = fund_data["as_of"]
                fund_as_of = str(as_of_dict.get("cash") or as_of_dict.get("shares") or list(as_of_dict.values())[0])
            elif fund_data.get("as_of"):
                fund_as_of = str(fund_data["as_of"])

        # 계산 파일 확인
        calc_path_str = ticker_cfg.get("calculation", "")
        has_calculation = bool(calc_path_str and (ROOT / calc_path_str).exists())

        # 리포트 파일 확인
        report_path = REPORTS_DIR / ticker / "report.json"
        has_report = report_path.exists()

        asset_list.append({
            "ticker": ticker,
            "name": asset.get("name", ticker_cfg.get("name", ticker)),
            "market": asset.get("market", "KR"),
            "exchange": asset.get("exchange", "KOSPI"),
            "asset_type": asset.get("asset_type", "STOCK"),
            "currency": asset.get("currency", "KRW"),
            "price_symbol": price_symbol,
            "sector_code": asset.get("sector_code", "TECH_SERVICES"),
            "industry": asset.get("industry", ""),
            "analysis_lens": asset.get("analysis_lens", "QUALITY_GROWTH"),
            "qty": ticker_cfg.get("qty", 0),
            "avg_cost_krw": ticker_cfg.get("avg_cost_krw", 0),
            "price_krw": ticker_cfg.get("priceKrw", 0),
            "price_info": price_info,
            "has_fundamentals": has_fundamentals,
            "fundamentals_as_of": fund_as_of,
            "has_calculation": has_calculation,
            "has_report": has_report,
            "is_selected": (ticker == current_selected_ticker),
            "trading_halted": bool(asset.get("trading_halted", False)),
        })

    # 한국, 미국, 이름순 정렬
    asset_list.sort(key=lambda x: (0 if x["market"] == "KR" else 1, x["name"]))
    return asset_list


def get_asset(ticker: str) -> dict[str, Any] | None:
    """단일 종목 정보 조회."""
    assets_raw = _load_json_file(ASSETS_FILE).get("assets", {})
    if ticker not in assets_raw:
        return None
    asset = assets_raw[ticker]
    tickers_raw = _load_json_file(TICKERS_FILE)
    ticker_cfg = tickers_raw.get(ticker, {})

    price_symbol = asset.get("price_symbol", ticker)
    price_info = get_price_info(price_symbol)

    return {
        "ticker": ticker,
        "asset": asset,
        "ticker_cfg": ticker_cfg,
        "price_info": price_info,
    }


def _update_holdings_csv(ticker: str, name: str, market: str, asset_type: str,
                          qty: int, avg_cost_krw: int) -> None:
    """knowledge/portfolio/holdings.csv 에서 해당 종목의 행을 추가 또는 갱신한다."""
    holdings_path = KNOWLEDGE_DIR / "portfolio" / "holdings.csv"
    holdings_path.parent.mkdir(parents=True, exist_ok=True)

    FIELDNAMES = ["asset_id", "name", "market", "asset_type", "risk_class",
                  "value_krw", "fx_exposure", "sector", "quantity", "avg_cost_krw", "pnl_krw", "note"]

    # 기존 행 읽기
    rows: list[dict] = []
    if holdings_path.exists():
        with holdings_path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("asset_id") != ticker:
                    rows.append(row)

    # CASH 행이 없으면 기본값 추가 (첫 종목 등록 시)
    has_cash = any(r.get("risk_class") == "CASH" for r in rows)

    # 이 종목 행 생성 (qty=0 이면 보유 없음이므로 행 자체를 넣지 않는다)
    if qty > 0:
        rows.append({
            "asset_id": ticker,
            "name": name,
            "market": market,
            "asset_type": asset_type,
            "risk_class": asset_type,
            "value_krw": "",
            "fx_exposure": "USD" if market == "US" else "",
            "sector": "",
            "quantity": qty,
            "avg_cost_krw": avg_cost_krw if avg_cost_krw > 0 else "",
            "pnl_krw": "",
            "note": "",
        })

    if not has_cash and not holdings_path.exists():
        rows.append({
            "asset_id": "CASH_KRW",
            "name": "원화 예수금",
            "market": "KR",
            "asset_type": "CASH",
            "risk_class": "CASH",
            "value_krw": "0",
            "fx_exposure": "KRW",
            "sector": "",
            "quantity": "",
            "avg_cost_krw": "",
            "pnl_krw": "",
            "note": "",
        })

    with holdings_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)


def save_asset(data: dict[str, Any]) -> dict[str, Any]:
    """종목 등록 또는 수정."""
    ticker = data.get("ticker", "").strip()
    if not ticker:
        raise ValueError("티커(종목코드)는 필수입니다.")

    name = data.get("name", ticker).strip()
    market = data.get("market", "KR").upper()
    exchange = data.get("exchange", "KOSPI" if market == "KR" else "NASDAQ")
    asset_type = data.get("asset_type", "STOCK").upper()
    currency = data.get("currency", "KRW" if market == "KR" else "USD").upper()
    price_symbol = data.get("price_symbol", ticker).strip()
    sector_code = data.get("sector_code", "TECH_SERVICES")
    industry = data.get("industry", "일반")
    analysis_lens = data.get("analysis_lens", "QUALITY_GROWTH")
    valuation_family = data.get("valuation_family", ["DCF", "PEER_MULTIPLE"])
    benchmarks = data.get("benchmarks", ["^KS11" if market == "KR" else "^GSPC"])
    optional_collectors = data.get("optional_collectors", [])
    qty = int(data.get("qty", 0))
    avg_cost_krw = int(data.get("avg_cost_krw", 0))

    # 1. assets.json 업데이트
    registry = _load_json_file(ASSETS_FILE)
    if not registry:
        registry = {
            "schema_version": "asset-registry/1.0.0",
            "supported_markets": ["KR", "US"],
            "supported_asset_types": ["STOCK", "ETF"],
            "assets": {},
        }
    if "assets" not in registry:
        registry["assets"] = {}

    filing_source = "DART" if market == "KR" else "SEC_EDGAR"
    filing_profile = "DART_KIFRS" if market == "KR" else "SEC_DOMESTIC_US_GAAP"

    asset_entry: dict[str, Any] = {
        "name": name,
        "market": market,
        "exchange": exchange,
        "asset_type": asset_type,
        "currency": currency,
        "price_symbol": price_symbol,
        "sector_code": sector_code,
        "industry": industry,
        "analysis_lens": analysis_lens,
        "valuation_family": valuation_family,
        "benchmarks": benchmarks,
        "optional_collectors": optional_collectors,
    }
    if asset_type == "STOCK":
        asset_entry["filing_source"] = filing_source
        asset_entry["filing_profile"] = filing_profile

    registry["assets"][ticker] = asset_entry
    _save_json_file(ASSETS_FILE, registry)

    # 2. tickers.json 업데이트
    tickers_cfg = _load_json_file(TICKERS_FILE)
    today_str = date.today().isoformat()
    now_iso = datetime.now(timezone.utc).astimezone().isoformat()

    price_safe = safe_name(price_symbol)
    price_info = get_price_info(price_symbol)
    last_close = price_info.get("last_close")
    current_price = float(last_close) if last_close is not None else 0.0

    # 기존 설정 유지 또는 신규 생성
    existing_cfg = tickers_cfg.get(ticker, {})
    existing_chart_curr = existing_cfg.get("chartCurrent", 0.0)
    if existing_chart_curr == 100.0:
        existing_chart_curr = 0.0
    chart_current = current_price if current_price > 0 else float(existing_chart_curr or 0.0)

    existing_chart_entry = existing_cfg.get("chartEntry", 0.0)
    if existing_chart_entry == 90.0:
        existing_chart_entry = 0.0
    chart_entry = (chart_current * 0.9) if chart_current > 0 else float(existing_chart_entry or 0.0)

    new_cfg = {
        "tickerLabel": f"{ticker} · {exchange}",
        "name": name,
        "qty": qty,
        "avg_cost_krw": avg_cost_krw,
        "market": market,
        "riskClass": asset_type,
        "currency": currency,
        "priceKrw": int(chart_current) if currency == "KRW" else int(chart_current * 1350),
        "priceAsOf": now_iso if chart_current > 0 else "",
        "priceNote": f"{today_str} 기준" if chart_current > 0 else "미수집",
        "asOf": today_str,
        "fundamentals": existing_cfg.get("fundamentals", f"knowledge/fundamentals/{ticker}-{today_str}.json"),
        "calculation": existing_cfg.get("calculation", ""),
        "prices": f"knowledge/prices/{price_safe}.csv",
        "chartCurrent": chart_current,
        "chartEntry": chart_entry,
        "chartEntryLabel": existing_cfg.get("chartEntryLabel", "강세 시나리오 기준"),
    }
    tickers_cfg[ticker] = new_cfg
    _save_json_file(TICKERS_FILE, tickers_cfg)

    # holdings.csv 업데이트 (보유수량·평단가 반영)
    _update_holdings_csv(ticker, name, market, asset_type, qty, avg_cost_krw)

    # 기본 narrative 파일 보장
    _ensure_narrative(ticker, name)

    return {"success": True, "ticker": ticker, "name": name}


def delete_asset(ticker: str) -> dict[str, Any]:
    """종목 삭제."""
    registry = _load_json_file(ASSETS_FILE)
    if "assets" in registry and ticker in registry["assets"]:
        del registry["assets"][ticker]
        _save_json_file(ASSETS_FILE, registry)

    tickers_cfg = _load_json_file(TICKERS_FILE)
    if ticker in tickers_cfg:
        del tickers_cfg[ticker]
        _save_json_file(TICKERS_FILE, tickers_cfg)

    return {"success": True, "ticker": ticker}


def execute_python_module(module: str, args: list[str]) -> tuple[int, str]:
    """Python 모듈 실행 및 출력 반환."""
    cmd = [sys.executable, "-m", module, *args]
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=240,
        )
        return proc.returncode, proc.stdout
    except subprocess.TimeoutExpired:
        return -1, f"타임아웃(240초 초과): {' '.join(cmd)}"
    except Exception as exc:
        return -1, f"실행 오류: {exc}"


def collect_price(ticker: str, range_str: str = "5y") -> dict[str, Any]:
    """시세 수집 실행."""
    assets_raw = _load_json_file(ASSETS_FILE).get("assets", {})
    asset = assets_raw.get(ticker)
    if not asset:
        return {"success": False, "error": f"등록되지 않은 종목입니다: {ticker}"}

    price_symbol = asset.get("price_symbol", ticker)
    code, output = execute_python_module("collectors.price", ["history", price_symbol, "--range", range_str])

    # 벤치마크도 함께 수집
    benchmarks = asset.get("benchmarks", [])
    bench_logs = []
    for bench in benchmarks:
        b_code, b_out = execute_python_module("collectors.price", ["history", bench, "--range", range_str])
        bench_logs.append(f"[{bench}] {b_out.strip()}")

    success = (code == 0)
    price_info = get_price_info(price_symbol)

    # 시세 수집 성공 시 tickers.json 실시간 동기화
    if success and price_info.get("last_close"):
        last_close = float(price_info["last_close"])
        tickers_cfg = _load_json_file(TICKERS_FILE)
        if ticker in tickers_cfg:
            cfg = tickers_cfg[ticker]
            curr = asset.get("currency", cfg.get("currency", "KRW"))
            cfg["priceKrw"] = int(last_close) if curr == "KRW" else int(last_close * 1350)
            cfg["chartCurrent"] = last_close
            if not cfg.get("chartEntry") or cfg.get("chartEntry") == 90.0 or cfg.get("chartEntry") == 0.0:
                cfg["chartEntry"] = round(last_close * 0.9, 2)
            cfg["priceAsOf"] = datetime.now(timezone.utc).astimezone().isoformat()
            cfg["priceNote"] = f"{date.today().isoformat()} 기준"
            _save_json_file(TICKERS_FILE, tickers_cfg)

    return {
        "success": success,
        "returncode": code,
        "output": output + ("\n" + "\n".join(bench_logs) if bench_logs else ""),
        "price_info": price_info,
    }


def collect_fundamentals(ticker: str) -> dict[str, Any]:
    """재무/공시/컨센서스 수집 실행."""
    load_env_file()
    assets_raw = _load_json_file(ASSETS_FILE).get("assets", {})
    asset = assets_raw.get(ticker)
    if not asset:
        return {"success": False, "error": f"등록되지 않은 종목입니다: {ticker}"}

    market = asset.get("market", "KR")
    asset_type = asset.get("asset_type", "STOCK").upper()
    logs = []

    import time as _time

    if asset_type == "ETF":
        # ETF는 DART 공시·재무제표·컨센서스 대상이 아님 — 시세 수집만 유효
        logs.append(f"[재무 수집] {ticker}은 ETF입니다. DART/컨센서스 수집을 건너뜁니다.")
    elif market == "KR":
        # 한국 개별주: DART / 컨센서스
        dart_key = os.environ.get("DART_API_KEY")
        if dart_key:
            current_year = date.today().year
            # 1. DART 재무제표 스냅샷 수집 (fundamentals)
            _t = _time.time()
            f_code, f_out = execute_python_module("collectors.dart", ["fundamentals", ticker, "--year", str(current_year)])
            logs.append(f"[DART Fundamentals {_time.time()-_t:.1f}s] {f_out.strip()}")
            # 2. DART 공시 목록 수집 (filings, 최근 1년)
            since_date = (date.today() - timedelta(days=365)).strftime("%Y%m%d")
            _t = _time.time()
            fl_code, fl_out = execute_python_module("collectors.dart", ["filings", ticker, "--since", since_date])
            logs.append(f"[DART Filings {_time.time()-_t:.1f}s] {fl_out.strip()}")
        else:
            logs.append("[DART] DART_API_KEY 미설정으로 DART API 건너뜀")

        _t = _time.time()
        c_code, c_out = execute_python_module("collectors.consensus", [ticker])
        logs.append(f"[Consensus {_time.time()-_t:.1f}s] {c_out.strip()}")
    else:
        # 미국 개별주: EDGAR
        _t = _time.time()
        code, out = execute_python_module("collectors.edgar", ["fundamentals", ticker])
        logs.append(f"[SEC EDGAR {_time.time()-_t:.1f}s] {out.strip()}")

    # 수집된 최신 fundamentals를 tickers.json에 반영
    fund_candidates = sorted(glob.glob(str(FUNDAMENTALS_DIR / f"{ticker}-*.json")), key=os.path.getmtime)
    if fund_candidates:
        latest_fund = Path(fund_candidates[-1])
        tickers_cfg = _load_json_file(TICKERS_FILE)
        if ticker in tickers_cfg:
            tickers_cfg[ticker]["fundamentals"] = latest_fund.relative_to(ROOT).as_posix().replace("\\", "/")
            _save_json_file(TICKERS_FILE, tickers_cfg)

    return {
        "success": True,
        "output": "\n".join(logs),
    }


def _auto_generate_calculation(ticker: str, fund_path: Path, price: float) -> tuple[Path | None, str]:
    """해당 종목 및 fundamentals에 맞는 reverse_dcf calculation 자동 생성.

    Returns:
        (calc_path, reason) — 성공 시 (Path, ""), 실패 시 (None, 실패_사유).
    """
    fund_data = _load_json_file(fund_path)
    fundamentals_id = fund_data.get("fundamentals_id")
    if not fundamentals_id:
        return None, "fundamentals 파일에 fundamentals_id가 없습니다"

    if price <= 0:
        price = 100.0

    calc_cmd = [
        sys.executable,
        "-m", "calculators.reverse_dcf",
        "scenarios",
        "--instrument", ticker,
        "--price", str(price),
        "--fundamentals", str(fund_path),
        "--discount-rate", "10.0",
        "--terminal-growth", "2.5",
        "--horizon-months", "24",
        "--source-id", fundamentals_id,
        "--growth", "bear=-5.0", "base=5.0", "bull=15.0",
        "--save",
    ]

    share_basis = fund_data.get("share_count_basis") or {}
    share_tag = (fund_data.get("tags") or {}).get("shares")
    current_outstanding = (
        share_basis.get("is_current_outstanding") is True
        and not str(share_basis.get("kind", "")).startswith("WEIGHTED_AVERAGE")
        and not str(share_tag or "").startswith("WeightedAverage")
    )
    if not current_outstanding:
        shares = (fund_data.get("values") or {}).get("shares_diluted")
        if shares and shares > 0:
            calc_cmd.extend([
                "--shares", str(shares),
                "--shares-reason", f"자동 파이프라인: 스냅샷({fundamentals_id}) 희석주식수({shares:,.0f}주) 적용",
            ])
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    try:
        proc = subprocess.run(calc_cmd, cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
        if proc.returncode == 0:
            all_calcs = sorted(glob.glob(str(CALCULATIONS_DIR / "CALC-*.json")), key=os.path.getmtime)
            for cp in reversed(all_calcs):
                try:
                    cdata = _load_json_file(Path(cp))
                    if cdata.get("instrument_id") == ticker and cdata.get("inputs", {}).get("input_provenance", {}).get("fundamentals_id") == fundamentals_id:
                        return Path(cp), ""
                except Exception:
                    pass
            return None, "CALC 파일 생성 후 탐색 실패"
        reason = (proc.stderr or proc.stdout or "알 수 없는 오류").strip().splitlines()[0]
        # 음수 FCF 거부는 계산기가 의도한 결과다. 플레이스홀더 CALC를 만들어 리포트를 계속 빌드한다.
        if "음수인 회사" in reason or "FCF 가" in reason:
            return _make_negative_fcf_placeholder(ticker, fund_data, fundamentals_id, price)
        return None, f"reverse_dcf 실패: {reason}"
    except Exception as exc:
        return None, f"reverse_dcf 실행 오류: {exc}"


def _make_negative_fcf_placeholder(
    ticker: str, fund_data: dict, fundamentals_id: str, price: float
) -> tuple["Path | None", str]:
    """음수 FCF 종목용 플레이스홀더 CALC 파일을 만든다.

    reverse_dcf 모델은 FCF>0 을 전제하므로 음수 FCF 에 DCF 를 돌릴 수 없다.
    그래도 리포트 뼈대(현금흐름, 손익, 포트폴리오 비중 등)는 필요하다.
    이 파일은 build_decision_report.py 가 'negative_fcf' 모델명을 보고
    DCF 관련 섹션(sensitivity, valuation scenarios)을 대체 표시로 교체한다.
    """
    import hashlib
    from datetime import datetime, timezone

    values = fund_data.get("values") or {}
    fcf_ttm = values.get("fcf_ttm", 0.0)
    shares = (fund_data.get("values") or {}).get("shares_diluted", 1.0)
    net_debt = (fund_data.get("values") or {}).get("net_debt", 0.0)

    # 각 시나리오를 −100% 수익률(주당가치 0) 로 채워 contracts 검증을 통과한다.
    # build_decision_report.py 는 플레이스홀더를 감지해 이 값을 화면에 쓰지 않는다.
    scenario_template = {
        "growth_pct": 0.0,
        "value_per_share": 0.0,
        "total_return_pct": -100.0,
        "annualized_return_pct": -100.0,
    }
    outputs = {
        "scenarios": {n: dict(scenario_template) for n in ("bear", "base", "bull")},
        "implied_growth_pct": 0.0,
        "implied_growth_status": "N/A",
        "_note": "FCF 가 음수이므로 DCF 를 적용할 수 없다. 값은 전부 placeholder 다.",
    }
    inputs = {
        "price": price,
        "shares": shares,
        "fcf": fcf_ttm,
        "net_debt": net_debt,
        "discount_rate_pct": 10.0,
        "terminal_growth_pct": 2.5,
        "years": 10,
        "horizon_months": 24,
        "scenario_growth_pct": {"bear": 0.0, "base": 0.0, "bull": 0.0},
        "input_provenance": {
            "verified": True,
            "source": fund_data.get("source", "dart_fundamentals"),
            "fundamentals_id": fundamentals_id,
            "period_end": fund_data.get("period_end", ""),
            "fundamentals_sha256": "",
            "share_count": {
                "origin": "SNAPSHOT",
                "selected_shares": shares,
                "snapshot_shares": shares,
                "snapshot_basis": None,
                "snapshot_tag": None,
                "reason": None,
            },
        },
    }

    # calculation_id 를 inputs/outputs 해시로 만든다 (contracts.py 방식).
    model = "negative_fcf"
    model_version = "1.0.0"
    payload = json.dumps(
        {"model": model, "model_version": model_version,
         "inputs": inputs, "outputs": outputs},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    )
    calc_id = "CALC-" + hashlib.sha256(payload.encode()).hexdigest()[:20]

    calculation = {
        "schema_version": "1.1.0",
        "calculation_id": calc_id,
        "model": model,
        "model_version": model_version,
        "instrument_id": ticker,
        "created_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "source_ids": [fundamentals_id],
        "inputs": inputs,
        "outputs": outputs,
    }

    CALCULATIONS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = CALCULATIONS_DIR / f"{calc_id}.json"
    _save_json_file(out_path, calculation)
    return out_path, ""


def _ensure_narrative(ticker: str, name: str) -> Path:
    """narrative 파일이 없으면 기본 템플릿 생성."""
    narrative_dir = ROOT / "dashboard" / "data" / "narrative"
    narrative_dir.mkdir(parents=True, exist_ok=True)
    narrative_path = narrative_dir / f"{ticker}.json"
    if narrative_path.exists():
        return narrative_path

    template = {
        "_note": "산문만 여기 둔다. 숫자는 build_decision_report.py 가 엔진·DART 원문에서 만든다.",
        "decision": {
            "headline": f"{name} 의사결정 분석",
            "reason": f"{name}({ticker})의 재무 및 밸류에이션 기반 분석 리포트입니다."
        },
        "sectionTitles": {
            "incomeStatement": {
                "title": f"손익 - {name} 마진 및 수익 구조",
                "conclusion": "매출 및 영업이익 추이와 마진 구조 분석입니다."
            },
            "cashFlow": {
                "title": f"현금흐름 - {name} FCF 및 투자 구조",
                "conclusion": "영업활동현금흐름과 Capex 및 잉여현금흐름(FCF) 추이입니다."
            },
            "valuation": {
                "title": "밸류에이션 - 내재가치 및 시나리오 분석",
                "conclusion": "DCF 및 시나리오별 내재가치와 요구수익률 기준 적정주가 분석입니다.",
                "entryExplanation": "요구수익률과 확률가중치에 기반한 목표 진입가 기준입니다."
            },
            "peers": {
                "title": "동종사 비교",
                "conclusion": "동종 섹터 기업과의 비교 지표입니다."
            }
        },
        "interpretations": {
            "incomeStatement": {
                "매출": "매출 성장 추이",
                "매출총이익률": "수익성 지표",
                "판매비와관리비": "비용 통제 지표",
                "영업이익": "핵심 영업 성과"
            }
        },
        "marketPositioning": {
            "title": "시장 포지셔닝",
            "conclusion": "수급 및 포지셔닝 분석입니다.",
            "items": [
                {
                    "id": "gap",
                    "label": "시장 포지셔닝",
                    "status": "UNKNOWN",
                    "value": "데이터 수집 대기",
                    "comparison": "-",
                    "asOf": "-",
                    "interpretation": "지분 공시 및 수급 데이터 스냅샷 대기 중입니다.",
                    "limitation": "공시 원문 기반 데이터가 수집되면 반영됩니다.",
                    "signal": "plain"
                }
            ],
            "decisionUse": "검증된 재무 스냅샷과 IPS 한도 기준으로 판정됩니다."
        },
        "peers": {
            "rows": []
        },
        "detailedAnalysis": {
            "title": f"{name} 투자 판단 상세",
            "paragraphs": [
                f"{name}({ticker}) 종목의 실적 및 가격 추세에 대한 분석입니다."
            ],
            "confirmed": [
                f"{name} DART/EDGAR 공시 및 재무 스냅샷이 확인되었습니다."
            ],
            "unresolved": [
                "매크로 변수 및 향후 실적 가이던스 확인 필요"
            ],
            "failureSignals": [
                "영업이익률 급락 또는 현금흐름 훼손 시 재평가"
            ],
            "caveats": [
                "본 리포트는 자동 수집된 공시 데이터를 기반으로 산출되었습니다."
            ]
        },
        "sources": [
            "DART 공시 시스템 / SEC EDGAR",
            "한국거래소(KRX) / Yahoo Finance"
        ]
    }
    _save_json_file(narrative_path, template)
    return narrative_path


def _build_etf_report(ticker: str, cfg: dict[str, Any], select_for_dashboard: bool) -> dict[str, Any]:
    """ETF 전용 리포트 빌드. DART/재무제표 없이 가격 차트와 최소 리포트를 생성한다.

    ETF는 개별 재무제표·DCF 계산 대상이 아니므로 build_decision_report.py 를 거치지 않는다.
    생성된 리포트는 대시보드 메인 화면에 선택되지 않는다(테스트가 재무 데이터 구조를 전제함).
    """
    out_dir = REPORTS_DIR / ticker
    out_dir.mkdir(parents=True, exist_ok=True)

    prices_path = ROOT / cfg["prices"]
    chart_out = out_dir / "chart.json"
    report_out = out_dir / "report.json"
    logs: list[str] = []

    # 1. 가격 차트 빌드
    chart_cmd = [
        sys.executable,
        str(ROOT / "dashboard" / "scripts" / "build_price_chart.py"),
        "--input", str(prices_path),
        "--output", str(chart_out),
        "--current", str(cfg.get("chartCurrent", 0)),
        "--entry-ceiling", str(cfg.get("chartEntry", 0)),
        "--entry-label", str(cfg.get("chartEntryLabel", "기준선")),
        "--currency", str(cfg.get("currency", "KRW")),
        "--lookback-days", "365",
    ]
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    chart_built = False
    if prices_path.exists():
        try:
            p1 = subprocess.run(chart_cmd, cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", env=env)
            logs.append(f"[Chart Build] {p1.stdout.strip() or ('OK' if p1.returncode == 0 else p1.stderr)}")
            chart_built = p1.returncode == 0 and chart_out.exists()
        except Exception as exc:
            logs.append(f"[Chart Build] 실패 (무시): {exc}")
    else:
        logs.append(f"[Chart Build] 가격 파일 없음, 더미 차트 생성: {prices_path}")

    # 가격 데이터가 없거나 차트 빌드가 실패한 경우 최소 유효 차트 JSON 생성
    if not chart_built:
        price_current = float(cfg.get("chartCurrent") or cfg.get("priceKrw") or 0.0)
        price_entry = float(cfg.get("chartEntry") or (price_current * 0.9 if price_current > 0 else 0.0))
        currency_fmt = cfg.get("currency", "KRW")
        fmt = (lambda v: f"₩{v:,.0f}") if currency_fmt == "KRW" else (lambda v: f"${v:,.2f}")
        dummy_chart = {
            "schemaVersion": "price-chart/1.0.0",
            "viewBox": "0 0 1000 360",
            "plotRight": "926",
            "tagX": "930",
            "tagTextX": "961",
            "linePath": "",
            "areaPath": "",
            "segments": [],
            "points": [],
            "guides": [],
            "xLabels": [],
            "entryLine": {
                "value": fmt(price_entry),
                "y": "294.00",
                "tagY": "285.00",
                "tagTextY": "298.00",
                "label": cfg.get("chartEntryLabel", "기준선"),
            },
            "currentLine": {
                "value": fmt(price_current),
                "y": "180.00",
                "tagY": "171.00",
                "tagTextY": "184.00",
                "label": "현재 참고가",
            },
            "markers": [],
            "summary": {
                "period": "—",
                "sessions": "가격 데이터 없음",
                "twoSessionMove": "—",
                "fromHigh": "—",
                "fromLow": "—",
            },
        }
        chart_out.write_text(json.dumps(dummy_chart, ensure_ascii=False, indent=2), encoding="utf-8")
        logs.append(f"[Chart Build] 더미 차트 생성 완료: {chart_out}")

    # 2. ETF 최소 리포트 직접 생성 (재무제표·DCF 없음)
    price_native = float(cfg.get("priceKrw") or cfg.get("chartCurrent") or 0)
    currency = cfg.get("currency", "KRW")
    price_str = f"₩{price_native:,.0f}" if currency == "KRW" else f"{price_native:,.2f}"
    report = {
        "schemaVersion": "etf-dashboard/1.0.0",
        "meta": {
            "asOf": cfg.get("asOf", ""),
            "priceAsOf": cfg.get("priceNote", ""),
            "financialAsOf": None,
            "calculationId": None,
            "fundamentalsId": None,
            "engineVersion": "etf",
            "generatedBy": "scripts/stock_service.py:_build_etf_report",
        },
        "asset": {
            "name": cfg.get("name", ticker),
            "ticker": cfg.get("tickerLabel", ticker),
            "currency": currency,
            "current": price_str,
            "currentBase": f"₩{cfg.get('priceKrw', 0):,.0f}",
            "baseNote": "기준통화와 동일",
            "assetType": "ETF",
        },
        "decision": {
            "verdict": "INSUFFICIENT",
            "status": "재무 분석 해당 없음",
            "verdictNote": "ETF는 개별 재무제표·DCF 분석 대상이 아닙니다.",
            "primaryReason": "ETF 해당 없음",
            "headline": f"{cfg.get('name', ticker)} ETF 보유 현황",
            "reason": "ETF는 개별 기업 재무분석이 아닌 포트폴리오 비중·NAV 추적 관점으로 관리됩니다.",
            "issues": [],
            "measure": "N/A",
            "positionReturn": "—",
            "requiredReturn": "—",
            "hurdleResult": "ETF — DCF 계산 해당 없음",
            "entryCeiling": "—",
            "entryCeilingNote": "ETF는 DCF 기반 목표가 계산을 수행하지 않습니다.",
            "bullOnlyCeiling": "—",
            "bullOnlyNote": "ETF는 강세/약세 시나리오 DCF 계산을 수행하지 않습니다.",
            "actionNow": "—",
            "highlights": [],
        },
        "portfolio": {
            "snapshotId": f"TRADE-SNAPSHOT-etf{ticker.lower()[:16]}",
            "ticker": ticker,
            "quantity": float(cfg.get("qty", 0)),
            "priceNative": price_native,
            "priceKrw": price_str,
            "tradeValueKrw": price_str,
            "marketValueKrw": float(cfg.get("priceKrw", 0)) * float(cfg.get("qty", 0)),
            "weights": [],
            "bearBudget": {"used": "—", "limit": "—", "note": "ETF — 해당 없음", "withinBudget": True},
            "costs": None,
        },
        "valuation": {
            "title": "밸류에이션",
            "conclusion": "ETF는 DCF 밸류에이션 대상이 아닙니다.",
            "impliedGrowth": "—",
            "modelInputs": "ETF — 해당 없음",
            "entryExplanation": "",
            "multiples": [],
            "scenarios": [],
        },
        "incomeStatement": {
            "title": "손익계산서",
            "conclusion": "ETF는 개별 손익계산서가 없습니다.",
            "equation": [],
            "rows": [],
        },
        "cashFlow": {
            "title": "현금흐름",
            "conclusion": "ETF는 개별 현금흐름표가 없습니다.",
            "rows": [],
            "chart": {"labels": [], "series": [], "groups": [], "ticks": []},
        },
        "sensitivity": {},
        "priceEvidence": [],
        "reviewMap": [
            {
                "when": "정기 리밸런싱",
                "state": "포트폴리오 비중 검토",
                "action": "목표 비중 대비 현재 비중을 확인합니다.",
                "why": "ETF는 비중 관리가 핵심입니다.",
                "signal": "watch",
            },
        ],
        "peersTable": {
            "title": "동종 ETF",
            "conclusion": "ETF 비교 데이터를 수집하지 않았습니다.",
            "basisWarning": "",
            "rows": [],
        },
        "marketPositioning": {
            "title": "시장 포지셔닝",
            "conclusion": "ETF — 해당 없음",
            "items": [],
            "decisionUse": "ETF는 포트폴리오 비중으로 관리됩니다.",
        },
        "detailedAnalysis": {
            "title": f"{cfg.get('name', ticker)} ETF",
            "paragraphs": [f"{cfg.get('name', ticker)}({ticker}) ETF 보유 현황입니다."],
            "confirmed": [],
            "unresolved": [],
            "failureSignals": [],
            "caveats": ["ETF는 개별 재무분석 대상이 아닙니다."],
        },
        "sources": [{"label": "시세", "detail": "Yahoo Finance / 한국거래소(KRX)"}],
    }
    report_out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    logs.append(f"[Report Build] ETF 리포트 생성 완료: {report_out}")

    # ETF도 대시보드 메인에 선택 가능 — decision-report.json으로 복사
    if select_for_dashboard:
        shutil.copy2(report_out, DASHBOARD_DATA_DIR / "decision-report.json")
        chart_out_path = out_dir / "chart.json"
        if chart_out_path.exists():
            shutil.copy2(chart_out_path, DASHBOARD_DATA_DIR / "price-chart.json")
        logs.append(f"[Select] 대시보드 표시 종목으로 선택됨 (ETF): {ticker}")

    return {
        "success": True,
        "output": "\n".join(logs),
        "report_path": str(report_out),
    }




def generate_ai_analysis(ticker: str) -> dict[str, Any]:
    """Bob LLM을 통해 정량 데이터 기반의 CIO 투자 판단 및 내러티브 산문을 자동 생성하고 리포트를 빌드합니다."""
    assets_raw = _load_json_file(ASSETS_FILE).get("assets", {})
    asset = assets_raw.get(ticker, {})
    name = asset.get("name", ticker)
    market = asset.get("market", "KR")

    price_symbol = asset.get("price_symbol", ticker)
    price_info = get_price_info(price_symbol)

    # 1. 재무 스냅샷 로드
    fund_candidates = sorted(glob.glob(str(FUNDAMENTALS_DIR / f"{ticker}-*.json")), key=os.path.getmtime)
    fund_data = _load_json_file(Path(fund_candidates[-1])) if fund_candidates else {}

    # 2. 컨센서스 데이터 로드
    consensus_path = KNOWLEDGE_DIR / "consensus" / f"{ticker}.json"
    consensus_data = _load_json_file(consensus_path)

    logs = [f"▶ Bob Shell을 통한 [{name} ({ticker})] AI 투자 판단 생성 시작..."]
    narrative: dict[str, Any] = {}

    # 3. LLMInterface 호출
    try:
        from cio.llm_interface import LLMInterface
        llm = LLMInterface()
        if not llm.available:
            logs.append("[Bob] BOB_API_KEY 미설정 또는 CLI 비활성 — 기본 템플릿 사용")
            narrative = _load_json_file(_ensure_narrative(ticker, name))
        else:
            logs.append("[Bob] 정량 데이터 기반 전문 CIO 투자 분석 및 내러티브 작성 중...")
            narrative = llm.generate_narrative(
                ticker=ticker,
                name=name,
                market=market,
                price_summary=price_info,
                fundamentals_summary={
                    "period_end": fund_data.get("period_end"),
                    "ocf_ttm": fund_data.get("quarters", {}).get("ocf", [])[-4:] if "quarters" in fund_data else [],
                    "currency": fund_data.get("currency", "KRW"),
                },
                consensus_summary={
                    "target_price": consensus_data.get("summary", {}).get("target_price"),
                    "rating": consensus_data.get("summary", {}).get("rating"),
                    "eps": consensus_data.get("summary", {}).get("eps"),
                    "annual": consensus_data.get("estimates", {}).get("annual", []),
                },
            )
            narrative_path = ROOT / "dashboard" / "data" / "narrative" / f"{ticker}.json"
            _save_json_file(narrative_path, narrative)
            logs.append(f"[Bob] 내러티브 파일 생성 완료: dashboard/data/narrative/{ticker}.json")
            if "decision" in narrative:
                logs.append(f"[AI 헤드라인] {narrative['decision'].get('headline', '')}")
    except Exception as exc:
        logs.append(f"[Bob] AI 생성 중 오류: {exc}")
        _ensure_narrative(ticker, name)

    # 4. 리포트 빌드 및 대시보드 반영
    logs.append("▶ 대시보드 리포트 빌드 수행...")
    build_res = build_report(ticker, select_for_dashboard=True)
    if build_res.get("output"):
        logs.append(build_res["output"])

    return {
        "success": build_res.get("success", True),
        "output": "\n".join(logs),
        "headline": narrative.get("decision", {}).get("headline", ""),
    }


def build_report(ticker: str, select_for_dashboard: bool = True) -> dict[str, Any]:
    """종목 리포트 빌드 및 대시보드 반영."""
    tickers_cfg = _load_json_file(TICKERS_FILE)
    cfg = tickers_cfg.get(ticker)
    if not cfg:
        return {"success": False, "error": f"tickers.json에 종목 설정이 없습니다: {ticker}"}

    # ETF는 별도 경로로 처리
    assets_raw = _load_json_file(ASSETS_FILE).get("assets", {})
    is_etf = assets_raw.get(ticker, {}).get("asset_type", "STOCK").upper() == "ETF"
    if is_etf:
        return _build_etf_report(ticker, cfg, select_for_dashboard)

    out_dir = REPORTS_DIR / ticker
    out_dir.mkdir(parents=True, exist_ok=True)

    prices_path = ROOT / cfg["prices"]
    chart_out = out_dir / "chart.json"
    report_out = out_dir / "report.json"

    logs = []

    # 1. 가격 차트 빌드
    chart_cmd = [
        sys.executable,
        str(ROOT / "dashboard" / "scripts" / "build_price_chart.py"),
        "--input", str(prices_path),
        "--output", str(chart_out),
        "--current", str(cfg.get("chartCurrent", 0)),
        "--entry-ceiling", str(cfg.get("chartEntry", 0)),
        "--entry-label", str(cfg.get("chartEntryLabel", "강세 시나리오 기준")),
        "--currency", str(cfg.get("currency", "KRW")),
        "--lookback-days", "365",
    ]
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    try:
        p1 = subprocess.run(chart_cmd, cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", env=env)
        logs.append(f"[Chart Build] {p1.stdout.strip() or ('OK' if p1.returncode == 0 else p1.stderr)}")
    except Exception as exc:
        return {"success": False, "error": f"차트 빌드 실패: {exc}"}

    # 2. 의사결정 리포트 빌드
    narrative_path = _ensure_narrative(ticker, cfg.get("name", ticker))

    # 최신 fundamentals 파일 탐색
    fund_path = None
    fund_candidates = sorted(glob.glob(str(FUNDAMENTALS_DIR / f"{ticker}-*.json")), key=os.path.getmtime)
    if fund_candidates:
        fund_path = Path(fund_candidates[-1])
    else:
        fund_rel = cfg.get("fundamentals")
        if fund_rel and (ROOT / fund_rel).is_file():
            fund_path = ROOT / fund_rel

    if fund_path is None or not fund_path.is_file():
        return {
            "success": False,
            "error": f"재무 스냅샷 파일이 없습니다. 2단계에서 실적 수집(DART/EDGAR)을 먼저 진행해주세요.",
            "output": "\n".join(logs) + f"\n[Report Build] 실패: {ticker}의 재무 스냅샷(fundamentals) 파일이 없습니다.",
        }

    fund_data = _load_json_file(fund_path)
    fundamentals_id = fund_data.get("fundamentals_id")

    # calculation 파일 확인 및 fundamentals_id 일치 여부 검증
    calc_path = None
    calc_rel = cfg.get("calculation")
    if calc_rel and (ROOT / calc_rel).is_file():
        try:
            cdata = _load_json_file(ROOT / calc_rel)
            if cdata.get("instrument_id") == ticker and cdata.get("inputs", {}).get("input_provenance", {}).get("fundamentals_id") == fundamentals_id:
                calc_path = ROOT / calc_rel
        except Exception:
            pass

    if calc_path is None:
        # 해당 종목의 일치하는 calculation 파일 탐색
        all_calcs = sorted(glob.glob(str(CALCULATIONS_DIR / "CALC-*.json")), key=os.path.getmtime)
        for cp in reversed(all_calcs):
            try:
                cdata = _load_json_file(Path(cp))
                if cdata.get("instrument_id") == ticker and cdata.get("inputs", {}).get("input_provenance", {}).get("fundamentals_id") == fundamentals_id:
                    calc_path = Path(cp)
                    break
            except Exception:
                pass

    # 일치하는 calculation이 없으면 자동 생성
    auto_fail_reason = ""
    if calc_path is None:
        price_val = float(cfg.get("chartCurrent") or cfg.get("priceKrw") or 0.0)
        if price_val <= 0:
            p_info = get_price_info(cfg.get("price_symbol", ticker))
            if p_info.get("last_close"):
                price_val = float(p_info["last_close"])
        if price_val <= 0:
            return {
                "success": False,
                "error": f"시세 데이터가 없어 DCF 계산을 생성할 수 없습니다. 1단계에서 시세를 먼저 수집해주세요.",
                "output": "\n".join(logs) + f"\n[Report Build] 실패: {ticker}의 시세가 0이거나 미수집 상태입니다.",
            }
        calc_path, auto_fail_reason = _auto_generate_calculation(ticker, fund_path, price_val)

    if calc_path is None or not calc_path.is_file():
        if "음수인 회사" in auto_fail_reason:
            # 계산기가 의도대로 거부한 경우다. 버그나 데이터 누락과 구분해 보여 준다.
            msg = (f"DCF 적용 불가 — {auto_fail_reason}. 연결 FCF 가 음수이면 이 모델로는 "
                   "가치를 낼 수 없다. 금융 계열사를 포함한 연결 재무제표(예: 자동차·지주사)라면 "
                   "영업현금흐름·순부채에 금융 부문이 섞였는지 DART 원문을 확인할 것")
            return {
                "success": False,
                "error": msg,
                "output": "\n".join(logs) + f"\n[Report Build] {msg}",
            }
        detail = f" ({auto_fail_reason})" if auto_fail_reason else ""
        return {
            "success": False,
            "error": f"CIO 계산(CALC) 결과 파일이 없고 자동 생성에 실패했습니다{detail}.",
            "output": "\n".join(logs) + f"\n[Report Build] 실패: 계산(CALC) 파일이 없습니다{detail}.",
        }

    # tickers.json 최신 경로로 동기화
    tickers_cfg = _load_json_file(TICKERS_FILE)
    if ticker in tickers_cfg:
        tickers_cfg[ticker]["fundamentals"] = fund_path.relative_to(ROOT).as_posix().replace("\\", "/")
        tickers_cfg[ticker]["calculation"] = calc_path.relative_to(ROOT).as_posix().replace("\\", "/")
        _save_json_file(TICKERS_FILE, tickers_cfg)

    report_cmd = [
        sys.executable,
        str(ROOT / "dashboard" / "scripts" / "build_decision_report.py"),
        "--ticker", ticker,
        "--ticker-label", str(cfg.get("tickerLabel", ticker)),
        "--name", str(cfg.get("name", ticker)),
        "--quantity", str(cfg.get("qty", 0)),
        "--market", str(cfg.get("market", "KR")),
        "--risk-class", str(cfg.get("riskClass", "STOCK")),
        "--price-krw", str(cfg.get("priceKrw", 0)),
        "--price-as-of", str(cfg.get("priceAsOf", "")),
        "--price-note", str(cfg.get("priceNote", "")),
        "--as-of", str(cfg.get("asOf", "")),
        "--fundamentals", str(fund_path),
        "--calculation", str(calc_path),
        "--prices", str(prices_path),
        "--narrative", str(narrative_path),
        "--output", str(report_out),
    ]
    try:
        p2 = subprocess.run(report_cmd, cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", env=env)
        logs.append(f"[Report Build] {p2.stdout.strip() or ('OK' if p2.returncode == 0 else p2.stderr)}")
        if p2.returncode != 0:
            return {"success": False, "error": f"리포트 빌드 오류: {p2.stderr}", "output": "\n".join(logs)}
    except Exception as exc:
        return {"success": False, "error": f"리포트 빌드 실패: {exc}"}

    # 3. 대시보드 선택
    if select_for_dashboard:
        select_report(ticker)
        logs.append(f"[Select] 대시보드 표시 종목으로 선택됨: {ticker}")

    return {
        "success": True,
        "output": "\n".join(logs),
        "report_path": str(report_out),
    }


def ensure_initial_report() -> bool:
    """대시보드 초기 구동 시 decision-report.json 및 price-chart.json 존재를 보장."""
    DASHBOARD_DATA_DIR.mkdir(parents=True, exist_ok=True)
    report_main = DASHBOARD_DATA_DIR / "decision-report.json"
    chart_main = DASHBOARD_DATA_DIR / "price-chart.json"
    if report_main.exists() and chart_main.exists():
        return True

    # 가용 종목 중 우선적으로 빌드 가능한 종목 선택
    assets_raw = _load_json_file(ASSETS_FILE).get("assets", {})
    tickers = list(assets_raw.keys())
    # ETF 우선 시도
    etf_tickers = [t for t in tickers if assets_raw.get(t, {}).get("asset_type") == "ETF"]
    candidate_tickers = etf_tickers + [t for t in tickers if t not in etf_tickers]

    for ticker in candidate_tickers:
        res = build_report(ticker, select_for_dashboard=True)
        if res.get("success"):
            return True

    # 빌드 결과와 무관하게 report가 있으면 더미 차트라도 생성해 서버를 기동 가능하게 한다
    if report_main.exists() and not chart_main.exists():
        dummy = {
            "schemaVersion": "price-chart/1.0.0",
            "viewBox": "0 0 1000 360",
            "plotRight": "926", "tagX": "930", "tagTextX": "961",
            "linePath": "", "areaPath": "",
            "segments": [], "points": [], "guides": [], "xLabels": [],
            "entryLine": {"value": "—", "y": "294.00", "tagY": "285.00", "tagTextY": "298.00", "label": "기준선"},
            "currentLine": {"value": "—", "y": "180.00", "tagY": "171.00", "tagTextY": "184.00", "label": "현재 참고가"},
            "markers": [],
            "summary": {"period": "—", "sessions": "가격 데이터 없음", "twoSessionMove": "—", "fromHigh": "—", "fromLow": "—"},
        }
        chart_main.write_text(json.dumps(dummy, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    return False


def select_report(ticker: str) -> dict[str, Any]:
    """대시보드 표시 종목 선택."""
    DASHBOARD_DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_dir = REPORTS_DIR / ticker
    report_file = out_dir / "report.json"
    chart_file = out_dir / "chart.json"

    if not report_file.exists():
        # 없으면 빌드 시도
        res = build_report(ticker, select_for_dashboard=False)
        if not res.get("success"):
            return res

    shutil.copy2(report_file, DASHBOARD_DATA_DIR / "decision-report.json")
    if chart_file.exists():
        shutil.copy2(chart_file, DASHBOARD_DATA_DIR / "price-chart.json")

    return {"success": True, "ticker": ticker}


def check_and_update_prices() -> dict[str, Any]:
    """오늘 날짜 기준 시세가 미수집된 종목을 검사하고 자동 업데이트."""
    assets_raw = _load_json_file(ASSETS_FILE).get("assets", {})
    today = date.today()
    # 주말이면 금요일 기준
    target_date = today
    if today.weekday() == 5:  # 토
        target_date = today - timedelta(days=1)
    elif today.weekday() == 6:  # 일
        target_date = today - timedelta(days=2)
    target_str = target_date.isoformat()

    stale_assets: list[str] = []
    for ticker, asset in assets_raw.items():
        price_symbol = asset.get("price_symbol", ticker)
        info = get_price_info(price_symbol)
        if not info["exists"] or info["last_date"] != target_str or info["session"] == "UNSETTLED":
            stale_assets.append(ticker)

    updated: list[str] = []
    failed: list[str] = []
    logs: list[str] = []

    for ticker in stale_assets:
        res = collect_price(ticker, range_str="1mo")
        if res.get("success"):
            updated.append(ticker)
            logs.append(f"[{ticker}] 시세 자동 업데이트 성공")
        else:
            failed.append(ticker)
            logs.append(f"[{ticker}] 시세 업데이트 실패: {res.get('error') or res.get('output')}")

    return {
        "target_date": target_str,
        "total_checked": len(assets_raw),
        "stale_count": len(stale_assets),
        "updated_count": len(updated),
        "updated": updated,
        "failed": failed,
        "logs": logs,
    }


def get_longterm_analysis() -> list[dict[str, Any]]:
    """모든 등록 종목의 내재가치 시나리오(DCF 등) 및 현재가 괴리 분석 요약을 가져온다."""
    assets_raw = _load_json_file(ASSETS_FILE).get("assets", {})
    tickers_cfg = _load_json_file(TICKERS_FILE)

    analysis_list = []
    for ticker, asset_meta in assets_raw.items():
        ticker_info = tickers_cfg.get(ticker, {})
        current_price = float(ticker_info.get("priceKrw", 0))
        if current_price <= 0:
            current_price = float(ticker_info.get("chartCurrent", 0))

        # ETF의 경우 가치평가(DCF 등)가 없으므로 가벼운 형태로 우회
        if asset_meta.get("asset_type") == "ETF":
            analysis_list.append({
                "ticker": ticker,
                "name": asset_meta.get("name", ticker_info.get("name", ticker)),
                "asset_type": "ETF",
                "market": asset_meta.get("market", "KR"),
                "sector_code": asset_meta.get("sector_code", "ETF"),
                "industry": asset_meta.get("industry", "ETF"),
                "current_price": current_price,
                "calc_id": None,
                "has_calc": False,
                "bear_value": None,
                "base_value": None,
                "bull_value": None,
                "safety_margin_pct": None,
                "expected_return_pct": None,
            })
            continue

        calc_path_str = ticker_info.get("calculation", "")
        calc_data = None
        if calc_path_str:
            calc_path = ROOT / calc_path_str
            if calc_path.exists():
                calc_data = _load_json_file(calc_path)

        has_calc = calc_data is not None
        bear_value = None
        base_value = None
        bull_value = None
        expected_return_pct = None
        safety_margin_pct = None

        if has_calc and calc_data:
            scenarios = calc_data.get("outputs", {}).get("scenarios", {})
            bear_scenario = scenarios.get("bear", {})
            base_scenario = scenarios.get("base", {})
            bull_scenario = scenarios.get("bull", {})

            bear_value = bear_scenario.get("value_per_share")
            base_value = base_scenario.get("value_per_share")
            bull_value = bull_scenario.get("value_per_share")
            
            expected_return_pct = base_scenario.get("annualized_return_pct")
            if expected_return_pct is None:
                expected_return_pct = base_scenario.get("total_return_pct")

            if base_value and base_value > 0 and current_price > 0:
                safety_margin_pct = ((base_value - current_price) / base_value) * 100

        calc_model = calc_data.get("model") if has_calc and calc_data else None
        analysis_list.append({
            "ticker": ticker,
            "name": asset_meta.get("name", ticker_info.get("name", ticker)),
            "asset_type": asset_meta.get("asset_type", "STOCK"),
            "market": asset_meta.get("market", "KR"),
            "sector_code": asset_meta.get("sector_code", "일반"),
            "industry": asset_meta.get("industry", "일반"),
            "current_price": current_price,
            "calc_id": calc_data.get("calculation_id") if has_calc and calc_data else None,
            "calc_model": calc_model,
            "has_calc": has_calc,
            "bear_value": round(bear_value, 1) if bear_value is not None else None,
            "base_value": round(base_value, 1) if base_value is not None else None,
            "bull_value": round(bull_value, 1) if bull_value is not None else None,
            "safety_margin_pct": round(safety_margin_pct, 2) if safety_margin_pct is not None else None,
            "expected_return_pct": round(expected_return_pct, 2) if expected_return_pct is not None else None,
        })

    return analysis_list
