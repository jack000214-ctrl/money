"""오늘의 흐름 예측 엔진 (Python 3.13 stdlib 전용).

지원 알고리즘:
  MA_CROSS   — 단기(5일) vs 장기(20일) 이동평균 크로스
  RSI        — RSI-14 과매수/과매도
  BOLLINGER  — 볼린저 밴드 (20일, 2σ) 위치

각 알고리즘은 동일한 출력 구조를 반환한다:
  direction  : "UP" | "DOWN" | "NEUTRAL"
  score      : -100 ~ +100  (+: 상승, -: 하락, 0: 중립)
  signals    : 신호 이유 문자열 목록
  chart_data : 최근 30일 종가 + 지표값 (스파크라인용)

리포트는 data/forecasts/<id>.json 에 저장된다.
"""

from __future__ import annotations

from collections import defaultdict
import json
import math
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from cio.paths import ROOT

PRICES_DIR = ROOT / "knowledge" / "prices"
FORECASTS_DIR = ROOT / "data" / "forecasts"
FORECASTS_DIR.mkdir(parents=True, exist_ok=True)

ALGORITHMS = {
    "MA_CROSS": "이동평균 크로스 (5MA × 20MA)",
    "RSI": "RSI-14 과매수/과매도",
    "BOLLINGER": "볼린저 밴드 (20일, 2σ)",
    "COMBINED": "복합 신호 (MA + RSI + 볼린저)",
}

KST = timezone(timedelta(hours=9))
RETENTION_DAYS = 7


def _parse_created_at(iso_str: str) -> datetime | None:
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def cleanup_old_forecasts(retention_days: int = RETENTION_DAYS) -> int:
    """리포트 보관 정책에 따라 오래되거나 중복된 리포트 파일을 정리한다.

    보관 정책:
      1. 일주일(retention_days=7일) 초과된 리포트 파일 자동 삭제
      2. 동일 알고리즘 + 동일 일자(KST 기준 YYYY-MM-DD)는 가장 최신 1건만 보관 (이전 리포트 자동 삭제)

    Returns:
        삭제된 파일 수
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=retention_days)

    files = list(FORECASTS_DIR.glob("FC-*.json"))
    records: list[tuple[Path, str, datetime, str]] = []
    deleted_count = 0

    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            algo = data.get("algorithm", "")
            created_str = data.get("created_at", "")
            dt = _parse_created_at(created_str)
            if dt is None:
                dt = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)

            # 1. 일주일 초과 파일 삭제
            if dt < cutoff:
                f.unlink(missing_ok=True)
                deleted_count += 1
                continue

            dt_kst = dt.astimezone(KST)
            date_kst_str = dt_kst.strftime("%Y-%m-%d")
            records.append((f, algo, dt, date_kst_str))
        except Exception:
            continue

    # 2. 알고리즘별 + KST 일자별 그룹화하여 최신 1건 외 삭제
    groups: dict[tuple[str, str], list[tuple[Path, datetime]]] = defaultdict(list)
    for path, algo, dt, date_str in records:
        groups[(algo, date_str)].append((path, dt))

    for (algo, date_str), items in groups.items():
        if len(items) > 1:
            # 최신순 (dt 내림차순) 정렬
            items.sort(key=lambda x: x[1], reverse=True)
            for path, _ in items[1:]:
                path.unlink(missing_ok=True)
                deleted_count += 1

    return deleted_count


# ──────────────────────────────────────────────
# CSV 가격 로더
# ──────────────────────────────────────────────

def _safe_name(symbol: str) -> str:
    return symbol.replace("/", "_").replace("^", "_").replace(".", "_")


def _load_prices(price_symbol: str) -> list[tuple[str, float]]:
    """종가 CSV 에서 (date_str, close) 목록 반환. 날짜 오름차순."""
    safe = _safe_name(price_symbol)
    path = PRICES_DIR / f"{safe}.csv"
    if not path.exists():
        return []
    rows: list[tuple[str, float]] = []
    try:
        with path.open(encoding="utf-8-sig") as fh:
            for line in fh:
                if line.startswith("#") or not line.strip():
                    continue
                parts = line.strip().split(",")
                if len(parts) < 2:
                    continue
                try:
                    rows.append((parts[0], float(parts[1])))
                except ValueError:
                    continue
    except Exception:
        pass
    return rows


# ──────────────────────────────────────────────
# 기술지표 계산 헬퍼
# ──────────────────────────────────────────────

def _sma(closes: list[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(closes)
    for i in range(period - 1, len(closes)):
        result[i] = sum(closes[i - period + 1 : i + 1]) / period
    return result


def _ema(closes: list[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(closes)
    k = 2.0 / (period + 1)
    for i, c in enumerate(closes):
        if i < period - 1:
            continue
        if i == period - 1:
            result[i] = sum(closes[:period]) / period
        else:
            prev = result[i - 1]
            result[i] = c * k + (prev or c) * (1 - k)  # type: ignore[operator]
    return result


def _rsi(closes: list[float], period: int = 14) -> list[float | None]:
    result: list[float | None] = [None] * len(closes)
    if len(closes) < period + 1:
        return result
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(closes)):
        if avg_loss == 0:
            result[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            result[i] = 100 - 100 / (1 + rs)
        if i < len(gains):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    return result


def _bollinger(closes: list[float], period: int = 20, mult: float = 2.0) -> list[dict[str, float | None]]:
    result: list[dict[str, float | None]] = [{"mid": None, "upper": None, "lower": None}] * len(closes)
    for i in range(period - 1, len(closes)):
        window = closes[i - period + 1 : i + 1]
        mid = sum(window) / period
        std = math.sqrt(sum((x - mid) ** 2 for x in window) / period)
        result[i] = {"mid": mid, "upper": mid + mult * std, "lower": mid - mult * std}
    return result


# ──────────────────────────────────────────────
# 알고리즘별 분석
# ──────────────────────────────────────────────

def _analyze_ma_cross(closes: list[float], dates: list[str]) -> dict[str, Any]:
    ma5 = _sma(closes, 5)
    ma20 = _sma(closes, 20)
    signals: list[str] = []
    score = 0

    last = len(closes) - 1
    if ma5[last] is not None and ma20[last] is not None:
        diff_pct = (ma5[last] - ma20[last]) / ma20[last] * 100  # type: ignore[operator]
        if ma5[last] > ma20[last]:  # type: ignore[operator]
            score += 40
            signals.append(f"5MA({ma5[last]:,.0f}) > 20MA({ma20[last]:,.0f}): 단기 상승 추세")
        else:
            score -= 40
            signals.append(f"5MA({ma5[last]:,.0f}) < 20MA({ma20[last]:,.0f}): 단기 하락 추세")

        # 골든/데드 크로스 감지 (최근 5일 내)
        for i in range(max(1, last - 4), last + 1):
            if ma5[i] is None or ma20[i] is None or ma5[i - 1] is None or ma20[i - 1] is None:
                continue
            if ma5[i - 1] <= ma20[i - 1] and ma5[i] > ma20[i]:  # type: ignore[operator]
                score += 30
                signals.append(f"골든크로스 발생 ({dates[i]}): 매수 신호")
                break
            if ma5[i - 1] >= ma20[i - 1] and ma5[i] < ma20[i]:  # type: ignore[operator]
                score -= 30
                signals.append(f"데드크로스 발생 ({dates[i]}): 매도 신호")
                break

        # 기울기
        if last >= 3 and ma5[last] is not None and ma5[last - 3] is not None:
            slope = (ma5[last] - ma5[last - 3]) / 3  # type: ignore[operator]
            if slope > 0:
                score += 15
                signals.append("5MA 기울기 ↑ (상승 모멘텀)")
            else:
                score -= 15
                signals.append("5MA 기울기 ↓ (하락 모멘텀)")

        signals.append(f"5MA/20MA 괴리율: {diff_pct:+.2f}%")

    # 차트 데이터 (최근 30개)
    n = min(30, len(closes))
    chart: list[dict[str, Any]] = []
    for i in range(len(closes) - n, len(closes)):
        chart.append({
            "date": dates[i],
            "close": closes[i],
            "ma5": ma5[i],
            "ma20": ma20[i],
        })

    return {"score": max(-100, min(100, score)), "signals": signals, "chart_data": chart, "indicators": {"ma5": ma5[last], "ma20": ma20[last]}}


def _analyze_rsi(closes: list[float], dates: list[str]) -> dict[str, Any]:
    rsi = _rsi(closes)
    signals: list[str] = []
    score = 0
    last = len(closes) - 1

    rsi_val = rsi[last]
    if rsi_val is not None:
        if rsi_val >= 70:
            score -= 50
            signals.append(f"RSI {rsi_val:.1f} — 과매수 구간 (70 이상): 매도 압력")
        elif rsi_val >= 60:
            score += 20
            signals.append(f"RSI {rsi_val:.1f} — 강세 구간 (60~70)")
        elif rsi_val <= 30:
            score += 50
            signals.append(f"RSI {rsi_val:.1f} — 과매도 구간 (30 이하): 반등 가능")
        elif rsi_val <= 40:
            score -= 20
            signals.append(f"RSI {rsi_val:.1f} — 약세 구간 (30~40)")
        else:
            signals.append(f"RSI {rsi_val:.1f} — 중립 구간 (40~60)")

        # RSI 방향
        if last >= 3 and rsi[last - 3] is not None:
            delta = rsi_val - rsi[last - 3]  # type: ignore[operator]
            if delta > 5:
                score += 15
                signals.append(f"RSI 3일 상승 +{delta:.1f}p: 모멘텀 강화")
            elif delta < -5:
                score -= 15
                signals.append(f"RSI 3일 하락 {delta:.1f}p: 모멘텀 약화")

    n = min(30, len(closes))
    chart = [{"date": dates[i], "close": closes[i], "rsi": rsi[i]} for i in range(len(closes) - n, len(closes))]
    return {"score": max(-100, min(100, score)), "signals": signals, "chart_data": chart, "indicators": {"rsi": rsi_val}}


def _analyze_bollinger(closes: list[float], dates: list[str]) -> dict[str, Any]:
    bands = _bollinger(closes)
    signals: list[str] = []
    score = 0
    last = len(closes) - 1
    band = bands[last]
    close = closes[last]

    if band["upper"] is not None:
        mid, upper, lower = band["mid"], band["upper"], band["lower"]
        width = (upper - lower) / mid * 100  # type: ignore[operator]
        pos = (close - lower) / (upper - lower) * 100  # type: ignore[operator]  # 0~100%

        signals.append(f"볼린저 위치: {pos:.0f}% (상단={upper:,.0f} / 중앙={mid:,.0f} / 하단={lower:,.0f})")
        signals.append(f"밴드 폭: {width:.1f}% ({'좁음(저변동)' if width < 5 else '넓음(고변동)'})")

        if close >= upper:  # type: ignore[operator]
            score -= 40
            signals.append("상단 밴드 돌파: 단기 과열, 되돌림 가능")
        elif close <= lower:  # type: ignore[operator]
            score += 40
            signals.append("하단 밴드 이탈: 과매도, 반등 시도 가능")
        elif pos >= 70:
            score += 20
            signals.append("상단 밴드 근접(70%+): 상승 탄력")
        elif pos <= 30:
            score -= 20
            signals.append("하단 밴드 근접(~30%): 하락 압력")
        else:
            signals.append("밴드 중간대 횡보")

    n = min(30, len(closes))
    chart = [
        {"date": dates[i], "close": closes[i], "bb_upper": bands[i]["upper"], "bb_mid": bands[i]["mid"], "bb_lower": bands[i]["lower"]}
        for i in range(len(closes) - n, len(closes))
    ]
    return {"score": max(-100, min(100, score)), "signals": signals, "chart_data": chart, "indicators": {**band}}


def _analyze_combined(closes: list[float], dates: list[str]) -> dict[str, Any]:
    ma = _analyze_ma_cross(closes, dates)
    rsi = _analyze_rsi(closes, dates)
    bb = _analyze_bollinger(closes, dates)
    total_score = int(ma["score"] * 0.45 + rsi["score"] * 0.3 + bb["score"] * 0.25)
    signals = (
        ["[MA크로스]"] + ma["signals"][:2]
        + ["[RSI]"] + rsi["signals"][:2]
        + ["[볼린저]"] + bb["signals"][:2]
    )
    return {
        "score": max(-100, min(100, total_score)),
        "signals": signals,
        "chart_data": ma["chart_data"],  # MA 차트 기본 사용
        "indicators": {**ma.get("indicators", {}), **rsi.get("indicators", {}), **bb.get("indicators", {})},
    }


_ALGO_FUNCS = {
    "MA_CROSS": _analyze_ma_cross,
    "RSI": _analyze_rsi,
    "BOLLINGER": _analyze_bollinger,
    "COMBINED": _analyze_combined,
}


def _direction(score: int) -> str:
    if score >= 20:
        return "UP"
    if score <= -20:
        return "DOWN"
    return "NEUTRAL"


# ──────────────────────────────────────────────
# 공개 API
# ──────────────────────────────────────────────

def run_forecast(assets: list[dict[str, Any]], algorithm: str) -> dict[str, Any]:
    """등록 종목 전체에 대해 지정 알고리즘으로 예측을 수행하고 저장한다.

    Args:
        assets: stock_service.list_assets() 반환값
        algorithm: ALGORITHMS 키 중 하나

    Returns:
        저장된 리포트 dict
    """
    if algorithm not in ALGORITHMS:
        raise ValueError(f"지원하지 않는 알고리즘: {algorithm}")

    fn = _ALGO_FUNCS[algorithm]
    results: list[dict[str, Any]] = []

    for asset in assets:
        ticker = asset["ticker"]
        price_symbol = asset.get("price_symbol", ticker)
        rows = _load_prices(price_symbol)
        if len(rows) < 21:
            results.append({
                "ticker": ticker,
                "name": asset.get("name", ticker),
                "market": asset.get("market", "KR"),
                "currency": asset.get("currency", "KRW"),
                "direction": "NEUTRAL",
                "score": 0,
                "signals": ["시세 데이터 부족 (최소 21일 필요)"],
                "chart_data": [],
                "indicators": {},
                "last_close": None,
                "last_date": None,
            })
            continue

        dates = [r[0] for r in rows]
        closes = [r[1] for r in rows]
        last_close = closes[-1]
        last_date = dates[-1]

        analysis = fn(closes, dates)
        results.append({
            "ticker": ticker,
            "name": asset.get("name", ticker),
            "market": asset.get("market", "KR"),
            "currency": asset.get("currency", "KRW"),
            "direction": _direction(analysis["score"]),
            "score": analysis["score"],
            "signals": analysis["signals"],
            "chart_data": analysis["chart_data"],
            "indicators": analysis.get("indicators", {}),
            "last_close": last_close,
            "last_date": last_date,
        })

    now_iso = datetime.now(timezone.utc).isoformat()
    report_id = f"FC-{uuid.uuid4().hex[:12].upper()}"
    report: dict[str, Any] = {
        "id": report_id,
        "algorithm": algorithm,
        "algorithm_label": ALGORITHMS[algorithm],
        "created_at": now_iso,
        "asset_count": len(results),
        "results": results,
    }

    path = FORECASTS_DIR / f"{report_id}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    cleanup_old_forecasts()
    return report


def list_forecasts(limit: int = 30) -> list[dict[str, Any]]:
    """저장된 예측 리포트 목록 (최신 순, limit 개).

    호출 시 보관 정책(7일 이내, 알고리즘별 1일 1건)에 맞춰 자동 정리 후 반환.
    """
    cleanup_old_forecasts()
    files = sorted(FORECASTS_DIR.glob("FC-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    summaries: list[dict[str, Any]] = []
    for f in files[:limit]:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            summaries.append({
                "id": data.get("id", f.stem),
                "algorithm": data.get("algorithm", ""),
                "algorithm_label": data.get("algorithm_label", ""),
                "created_at": data.get("created_at", ""),
                "asset_count": data.get("asset_count", 0),
            })
        except Exception:
            continue
    return summaries


def get_forecast(report_id: str) -> dict[str, Any] | None:
    """ID로 저장된 예측 리포트를 반환한다."""
    path = FORECASTS_DIR / f"{report_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def interpret_with_bob(result: dict[str, Any], algorithm_label: str) -> dict[str, Any]:
    """단일 종목 기술지표 분석 결과를 Bob AI에 넘겨 자연어 해석을 받는다.

    Args:
        result: run_forecast() 결과 내 개별 종목 dict
        algorithm_label: 알고리즘 한글 레이블

    Returns:
        {
          "success": bool,
          "headline": str,       # 한 줄 요약
          "morning": str,        # 오전 흐름 예측
          "afternoon": str,      # 오후 흐름 예측
          "rationale": str,      # 근거 (2~3문장)
          "caution": str,        # 주의 사항
          "raw": str,            # Bob 원문 응답
        }
    """
    try:
        from cio.llm_interface import LLMInterface, run_bob_shell, _bob_available
    except ImportError as exc:
        return {"success": False, "error": f"LLMInterface import 실패: {exc}"}

    if not _bob_available():
        return {"success": False, "error": "BOB_API_KEY 또는 BOB_TOKEN 미설정"}

    ticker = result.get("ticker", "")
    name = result.get("name", ticker)
    direction = result.get("direction", "NEUTRAL")
    score = result.get("score", 0)
    signals = result.get("signals", [])
    last_close = result.get("last_close")
    last_date = result.get("last_date", "")
    currency = result.get("currency", "KRW")
    indicators = result.get("indicators", {})

    close_str = (
        f"{last_close:,.0f}원" if currency == "KRW" and last_close
        else f"${last_close:,.2f}" if last_close
        else "미확인"
    )

    dir_kor = {"UP": "상승 우세", "DOWN": "하락 우세", "NEUTRAL": "중립·횡보"}[direction]

    signals_text = "\n".join(f"  - {s}" for s in signals) if signals else "  - 신호 없음"

    indicators_text = ""
    if indicators:
        parts = []
        if indicators.get("ma5") and indicators.get("ma20"):
            parts.append(f"5MA={indicators['ma5']:,.0f} / 20MA={indicators['ma20']:,.0f}")
        if indicators.get("rsi") is not None:
            parts.append(f"RSI={indicators['rsi']:.1f}")
        if indicators.get("upper") is not None:
            parts.append(f"볼린저상단={indicators['upper']:,.0f} / 중앙={indicators['mid']:,.0f} / 하단={indicators['lower']:,.0f}")
        indicators_text = " / ".join(parts)

    prompt = f"""당신은 한국 주식 시장 전문 기술적 분석가입니다.
아래 기술지표 분석 결과를 바탕으로 오늘의 주가 흐름을 예측하고 투자자에게 안내해 주세요.

## 종목 정보
- 종목: {name} ({ticker})
- 기준일: {last_date} / 최근 종가: {close_str}
- 사용 알고리즘: {algorithm_label}

## 기술지표 분석 결과
- 종합 방향: {dir_kor} (점수: {score:+d} / -100~+100)
- 주요 지표: {indicators_text or '없음'}
- 감지된 신호:
{signals_text}

## 요청 사항
위 분석을 바탕으로 **반드시 아래 JSON 형식만** 출력하세요 (마크다운 없이 순수 JSON):

{{
  "headline": "한 줄 핵심 요약 (20자 이내)",
  "morning": "오전 흐름 예측 (1~2문장, 구체적 수치 포함)",
  "afternoon": "오후 흐름 예측 (1~2문장)",
  "rationale": "기술지표 근거 설명 (2~3문장, 신호 이름 인용)",
  "caution": "주의할 점 또는 반대 시나리오 (1문장)"
}}

주의: 투자 권유가 아닌 기술적 분석 관점의 참고 정보임을 전제로 작성하세요.
"""

    raw = run_bob_shell(prompt, mode="ask")

    if raw.startswith("[ERROR]"):
        return {"success": False, "error": raw, "raw": raw}

    # JSON 파싱 시도
    import re as _re
    try:
        clean = raw.strip()
        m = _re.search(r"\{[\s\S]*\}", clean)
        if m:
            parsed = json.loads(m.group(0))
        else:
            parsed = json.loads(clean)
        return {
            "success": True,
            "headline": parsed.get("headline", ""),
            "morning": parsed.get("morning", ""),
            "afternoon": parsed.get("afternoon", ""),
            "rationale": parsed.get("rationale", ""),
            "caution": parsed.get("caution", ""),
            "raw": raw,
        }
    except Exception:
        # 파싱 실패 시 raw 텍스트를 rationale로
        return {
            "success": True,
            "headline": f"{name} {dir_kor}",
            "morning": "",
            "afternoon": "",
            "rationale": raw[:400],
            "caution": "",
            "raw": raw,
        }
