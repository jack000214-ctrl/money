"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { InvestorPolicyTab, UnreadBadge, WatchEventsTab, WatchSettingsTab, useWatchUnread } from "./WatchPanels";

const API_BASE = "http://127.0.0.1:3001";

// ISO(UTC) 시각을 한국 시간 "YYYY-MM-DD HH:mm:ss" 로 표시
function formatDateTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const parts = new Intl.DateTimeFormat("sv-SE", {
    timeZone: "Asia/Seoul",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(d);
  return `${parts} KST`;
}

interface AssetSummary {
  ticker: string;
  name: string;
  market: "KR" | "US";
  exchange: string;
  asset_type: "STOCK" | "ETF";
  currency: string;
  price_symbol: string;
  sector_code?: string;
  industry?: string;
  analysis_lens?: string;
  qty: number;
  price_krw: number;
  price_info: {
    exists: boolean;
    last_date: string | null;
    last_close: number | null;
    prev_close: number | null;
    session: string | null;
    retrieved_at?: string | null;
  };
  has_fundamentals: boolean;
  fundamentals_as_of: string | null;
  has_calculation: boolean;
  has_report: boolean;
  is_selected: boolean;
  trading_halted: boolean;
}

interface SearchResult {
  ticker: string;
  name: string;
  market: "KR" | "US";
  exchange: string;
  asset_type: "STOCK" | "ETF";
  currency: string;
  price_symbol: string;
  registered: boolean;
}

interface ForecastChartPoint {
  date: string;
  close: number;
  ma5?: number | null;
  ma20?: number | null;
  rsi?: number | null;
  bb_upper?: number | null;
  bb_mid?: number | null;
  bb_lower?: number | null;
}

interface AiInterpretation {
  success: boolean;
  headline?: string;
  morning?: string;
  afternoon?: string;
  rationale?: string;
  caution?: string;
  error?: string;
}

interface ForecastResult {
  ticker: string;
  name: string;
  market: string;
  currency: string;
  direction: "UP" | "DOWN" | "NEUTRAL";
  score: number;
  signals: string[];
  chart_data: ForecastChartPoint[];
  indicators: Record<string, number | null>;
  last_close: number | null;
  last_date: string | null;
  ai_interpretation?: AiInterpretation;
}

interface ForecastReport {
  id: string;
  algorithm: string;
  algorithm_label: string;
  created_at: string;
  asset_count: number;
  results: ForecastResult[];
}

interface ForecastSummary {
  id: string;
  algorithm: string;
  algorithm_label: string;
  created_at: string;
  asset_count: number;
}

// ─── ForecastCard: 종목 1개 예측 결과 카드 (SVG 스파크라인 포함) ───────────────

function SparklineSvg({ data, algorithm }: { data: ForecastChartPoint[]; algorithm: string }) {
  const W = 320, H = 72, PAD = 4;
  if (!data || data.length === 0) return null;

  const closes = data.map((d) => d.close);
  const minC = Math.min(...closes);
  const maxC = Math.max(...closes);
  const rangeC = maxC - minC || 1;

  const xScale = (i: number) => PAD + (i / (data.length - 1)) * (W - PAD * 2);
  const yScale = (v: number) => H - PAD - ((v - minC) / rangeC) * (H - PAD * 2);

  const closePath = data.map((d, i) => `${i === 0 ? "M" : "L"}${xScale(i).toFixed(1)},${yScale(d.close).toFixed(1)}`).join(" ");

  const ma5Path = (() => {
    const pts = data.map((d, i) => d.ma5 != null ? `${i === 0 ? "M" : "L"}${xScale(i).toFixed(1)},${yScale(d.ma5).toFixed(1)}` : null);
    const segments: string[] = [];
    let seg = "";
    pts.forEach((p, i) => {
      if (p) { seg += (seg ? " L" : "M") + `${xScale(i).toFixed(1)},${yScale(data[i].ma5!).toFixed(1)}`; }
      else if (seg) { segments.push(seg); seg = ""; }
    });
    if (seg) segments.push(seg);
    return segments.join(" ");
  })();

  const ma20Path = (() => {
    const pts = data.map((d, i) => d.ma20 != null ? true : null);
    const segments: string[] = [];
    let seg = "";
    data.forEach((d, i) => {
      if (d.ma20 != null) { seg += (seg ? " L" : "M") + `${xScale(i).toFixed(1)},${yScale(d.ma20).toFixed(1)}`; }
      else if (seg) { segments.push(seg); seg = ""; }
    });
    void pts;
    if (seg) segments.push(seg);
    return segments.join(" ");
  })();

  // 볼린저 밴드 영역
  const bbArea = (() => {
    const valid = data.filter((d) => d.bb_upper != null && d.bb_lower != null);
    if (valid.length < 2) return null;
    const upperPts = data.map((d, i) => d.bb_upper != null ? `${xScale(i).toFixed(1)},${yScale(d.bb_upper).toFixed(1)}` : null).filter(Boolean);
    const lowerPts = [...data].reverse().map((d, i) => {
      const idx = data.length - 1 - i;
      return d.bb_lower != null ? `${xScale(idx).toFixed(1)},${yScale(d.bb_lower).toFixed(1)}` : null;
    }).filter(Boolean);
    if (!upperPts.length || !lowerPts.length) return null;
    return `M${upperPts[0]} L${upperPts.slice(1).join(" L")} L${lowerPts[0]} L${lowerPts.slice(1).join(" L")} Z`;
  })();

  const lastX = xScale(data.length - 1);
  const lastY = yScale(closes[closes.length - 1]);

  return (
    <svg viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", height: "72px", display: "block" }}>
      {/* 볼린저 밴드 영역 */}
      {bbArea && <path d={bbArea} fill="rgba(99,102,241,0.12)" />}
      {/* 볼린저 밴드 선 */}
      {algorithm === "BOLLINGER" && data.map((d, i) => d.bb_upper != null && i > 0 ? null : null)}
      {/* MA20 */}
      {ma20Path && <path d={ma20Path} fill="none" stroke="#f59e0b" strokeWidth="1.2" strokeDasharray="3,2" opacity="0.8" />}
      {/* MA5 */}
      {ma5Path && <path d={ma5Path} fill="none" stroke="#38bdf8" strokeWidth="1.5" opacity="0.9" />}
      {/* 종가 선 */}
      <path d={closePath} fill="none" stroke="#e2e8f0" strokeWidth="1.8" />
      {/* 현재 종가 점 */}
      <circle cx={lastX} cy={lastY} r="3" fill="#f8fafc" />
    </svg>
  );
}

function ForecastCard({
  result,
  algorithm,
  createdAt,
  reportId,
  autoTrigger,
  onAutoComplete,
}: {
  result: ForecastResult;
  algorithm: string;
  createdAt: string;
  reportId: string;
  autoTrigger?: number;   // 값이 변경될 때마다 자동으로 Bob 분석 실행
  onAutoComplete?: () => void; // 자동 분석 완료 시 부모에 알림
}) {
  const [expanded, setExpanded] = useState(false);
  const [aiLoading, setAiLoading] = useState(false);
  const [aiResult, setAiResult] = useState<AiInterpretation | null>(
    result.ai_interpretation ?? null
  );
  const prevTriggerRef = useRef<number>(0);
  // 해석 완료 여부 (자동 순차 실행 시 이미 완료된 카드는 재실행하지 않기 위한 ref)
  const aiDoneRef = useRef<boolean>(!!result.ai_interpretation?.success);

  const dirColor = result.direction === "UP" ? "#4ade80" : result.direction === "DOWN" ? "#f87171" : "#94a3b8";
  const dirLabel = result.direction === "UP" ? "▲ 상승 우세" : result.direction === "DOWN" ? "▼ 하락 우세" : "↔ 중립";
  const dirBg = result.direction === "UP" ? "rgba(74,222,128,0.12)" : result.direction === "DOWN" ? "rgba(248,113,113,0.12)" : "rgba(148,163,184,0.08)";
  const scoreBar = Math.abs(result.score);
  const scoreColor = result.score > 0 ? "#4ade80" : result.score < 0 ? "#f87171" : "#64748b";

  const fmtClose = result.last_close != null
    ? result.currency === "KRW"
      ? `${result.last_close.toLocaleString()} 원`
      : `$${result.last_close.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
    : "—";

  const runInterpret = useCallback(async () => {
    setAiLoading(true);
    setAiResult(null);
    setExpanded(true);
    try {
      const res = await fetch(`${API_BASE}/api/forecast/interpret`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ report_id: reportId, ticker: result.ticker }),
      });
      const data: AiInterpretation = await res.json();
      aiDoneRef.current = !!data.success;
      setAiResult(data);
    } catch {
      setAiResult({ success: false, error: "네트워크 오류" });
    } finally {
      setAiLoading(false);
      onAutoComplete?.();
    }
  }, [reportId, result.ticker, onAutoComplete]);

  // autoTrigger 값이 새로 설정되면 자동 실행 (0 = 비활성: 초기 배열이 0으로 채워지므로 제외)
  // 이미 해석이 완료된 카드는 재실행하지 않고 다음 종목으로 바로 넘긴다.
  useEffect(() => {
    if (autoTrigger === undefined || autoTrigger <= 0 || autoTrigger === prevTriggerRef.current) return;
    prevTriggerRef.current = autoTrigger;
    if (aiDoneRef.current) {
      onAutoComplete?.();
      return;
    }
    runInterpret();
  }, [autoTrigger, runInterpret, onAutoComplete]);

  const handleAiInterpret = async (e: React.MouseEvent) => {
    e.stopPropagation();
    setAiLoading(true);
    setAiResult(null);
    if (!expanded) setExpanded(true);
    try {
      const res = await fetch(`${API_BASE}/api/forecast/interpret`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ report_id: reportId, ticker: result.ticker }),
      });
      const data: AiInterpretation = await res.json();
      aiDoneRef.current = !!data.success;
      setAiResult(data);
    } catch {
      setAiResult({ success: false, error: "네트워크 오류" });
    } finally {
      setAiLoading(false);
    }
  };

  return (
    <div style={{
      background: "#1e293b",
      border: `1px solid ${result.direction === "UP" ? "rgba(74,222,128,0.3)" : result.direction === "DOWN" ? "rgba(248,113,113,0.3)" : "#334155"}`,
      borderRadius: "10px",
      overflow: "hidden",
    }}>
      {/* 카드 헤더 */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: "16px",
          padding: "14px 18px",
          cursor: "pointer",
          background: dirBg,
          userSelect: "none",
        }}
        onClick={() => setExpanded((v) => !v)}
      >
        {/* 종목명 */}
        <div style={{ minWidth: "140px" }}>
          <div style={{ fontWeight: 700, fontSize: "15px", color: "#f8fafc" }}>{result.name}</div>
          <div style={{ fontSize: "12px", color: "#94a3b8" }}>{result.ticker} · {result.market}</div>
        </div>

        {/* 방향 배지 */}
        <div style={{
          background: dirColor + "22",
          color: dirColor,
          border: `1px solid ${dirColor}55`,
          borderRadius: "6px",
          padding: "4px 12px",
          fontWeight: 700,
          fontSize: "13px",
          whiteSpace: "nowrap",
        }}>
          {dirLabel}
        </div>

        {/* 점수 바 */}
        <div style={{ flex: 1, minWidth: "80px" }}>
          <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
            <div style={{ flex: 1, background: "#0f172a", borderRadius: "3px", height: "6px", overflow: "hidden" }}>
              <div style={{
                width: `${scoreBar}%`,
                height: "100%",
                background: scoreColor,
                borderRadius: "3px",
                transition: "width 0.4s ease",
              }} />
            </div>
            <span style={{ fontSize: "12px", color: scoreColor, fontWeight: 700, minWidth: "40px", textAlign: "right" }}>
              {result.score > 0 ? "+" : ""}{result.score}
            </span>
          </div>
        </div>

        {/* 최근 종가 */}
        <div style={{ textAlign: "right", minWidth: "100px" }}>
          <div style={{ fontWeight: 700, fontSize: "14px", color: "#f8fafc" }}>{fmtClose}</div>
          <div style={{ fontSize: "11px", color: "#64748b" }}>{result.last_date}</div>
        </div>

        {/* 스파크라인 */}
        <div style={{ width: "160px", flexShrink: 0 }}>
          <SparklineSvg data={result.chart_data.slice(-15)} algorithm={algorithm} />
        </div>

        {/* 조회 시각 */}
        <div style={{ fontSize: "11px", color: "#475569", whiteSpace: "nowrap" }}>
          {formatDateTime(createdAt)}
        </div>

        {/* Bob AI 해석 버튼 */}
        <button
          type="button"
          onClick={handleAiInterpret}
          disabled={aiLoading}
          style={{
            background: aiLoading ? "#78350f" : aiResult?.success ? "#065f46" : "#1e3a5f",
            color: aiLoading ? "#fcd34d" : "#fff",
            border: "none",
            borderRadius: "6px",
            padding: "5px 10px",
            fontSize: "11px",
            fontWeight: 700,
            cursor: aiLoading ? "not-allowed" : "pointer",
            whiteSpace: "nowrap",
            display: "flex",
            alignItems: "center",
            gap: "4px",
            flexShrink: 0,
          }}
        >
          {aiLoading ? (
            <><span style={{ animation: "spin 1s linear infinite", display: "inline-block" }}>⟳</span> 분석 중…</>
          ) : aiResult?.success ? (
            <>✓ AI 해석 완료</>
          ) : (
            <>🤖 Bob AI 해석</>
          )}
        </button>

        <span style={{ color: "#f8fafc", fontSize: "15px", marginLeft: "4px" }}>
          {expanded ? "▲" : "▼"}
        </span>
      </div>

      {/* 확장 영역: 신호 상세 + 큰 스파크라인 + AI 해석 */}
      {expanded && (
        <div style={{ padding: "16px 18px", borderTop: "1px solid #334155" }}>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "16px" }}>
            {/* 신호 목록 */}
            <div>
              <div style={{ fontSize: "12px", fontWeight: 700, color: "#94a3b8", marginBottom: "10px", textTransform: "uppercase", letterSpacing: "0.04em" }}>
                분석 신호
              </div>
              <ul style={{ margin: 0, padding: "0 0 0 16px", listStyle: "disc" }}>
                {result.signals.map((s, i) => (
                  <li key={i} style={{ fontSize: "12px", color: "#cbd5e1", lineHeight: 1.8, paddingLeft: "4px" }}>
                    {s}
                  </li>
                ))}
              </ul>
            </div>
            {/* 큰 스파크라인 */}
            <div>
              <div style={{ fontSize: "12px", fontWeight: 700, color: "#94a3b8", marginBottom: "10px", textTransform: "uppercase", letterSpacing: "0.04em" }}>
                최근 30일 차트
              </div>
              <div style={{ background: "#0f172a", borderRadius: "6px", padding: "8px" }}>
                <SparklineSvg data={result.chart_data} algorithm={algorithm} />
                <div style={{ display: "flex", gap: "16px", marginTop: "6px", fontSize: "10px", color: "#64748b" }}>
                  <span><span style={{ color: "#e2e8f0" }}>─</span> 종가</span>
                  {result.chart_data.some((d) => d.ma5 != null) && <span><span style={{ color: "#38bdf8" }}>─</span> 5MA</span>}
                  {result.chart_data.some((d) => d.ma20 != null) && <span><span style={{ color: "#f59e0b", fontStyle: "italic" }}>- -</span> 20MA</span>}
                  {result.chart_data.some((d) => d.bb_upper != null) && <span><span style={{ color: "#818cf8" }}>▒</span> 볼린저</span>}
                </div>
              </div>
            </div>
          </div>

          {/* Bob AI 해석 패널 */}
          {(aiLoading || aiResult) && (
            <div style={{
              marginTop: "16px",
              background: "#0f172a",
              border: "1px solid #1e3a5f",
              borderRadius: "8px",
              padding: "16px 18px",
            }}>
              <div style={{ fontSize: "12px", fontWeight: 700, color: "#38bdf8", marginBottom: "12px", display: "flex", alignItems: "center", gap: "8px" }}>
                🤖 Bob AI 흐름 해석
                {aiLoading && (
                  <span style={{ fontSize: "11px", color: "#f59e0b", fontWeight: 400 }}>
                    <span style={{ animation: "spin 1s linear infinite", display: "inline-block" }}>⟳</span>
                    {" "}분석 중... (30초~1분 소요)
                  </span>
                )}
              </div>

              {aiLoading && (
                <div style={{ fontSize: "12px", color: "#475569", lineHeight: 2 }}>
                  <div>• 기술지표 신호를 Bob에 전달 중...</div>
                  <div>• 오전/오후 흐름 분석 요청 중...</div>
                  <div>• 자연어 해석 생성 중...</div>
                </div>
              )}

              {aiResult && !aiResult.success && (
                <div style={{ fontSize: "12px", color: "#f87171" }}>
                  ✗ {aiResult.error || "해석 실패"}
                </div>
              )}

              {aiResult?.success && (
                <div style={{ display: "flex", flexDirection: "column", gap: "12px" }}>
                  {/* 헤드라인 */}
                  {aiResult.headline && (
                    <div style={{ fontSize: "16px", fontWeight: 700, color: "#f8fafc" }}>
                      {aiResult.headline}
                    </div>
                  )}
                  {/* 오전/오후 */}
                  <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "12px" }}>
                    {aiResult.morning && (
                      <div style={{ background: "#0f2b3d", borderRadius: "6px", padding: "10px 14px" }}>
                        <div style={{ fontSize: "10px", fontWeight: 700, color: "#38bdf8", marginBottom: "6px", textTransform: "uppercase", letterSpacing: "0.05em" }}>
                          ☀ 오전 흐름
                        </div>
                        <div style={{ fontSize: "12px", color: "#cbd5e1", lineHeight: 1.7 }}>{aiResult.morning}</div>
                      </div>
                    )}
                    {aiResult.afternoon && (
                      <div style={{ background: "#1a1033", borderRadius: "6px", padding: "10px 14px" }}>
                        <div style={{ fontSize: "10px", fontWeight: 700, color: "#a78bfa", marginBottom: "6px", textTransform: "uppercase", letterSpacing: "0.05em" }}>
                          🌙 오후 흐름
                        </div>
                        <div style={{ fontSize: "12px", color: "#cbd5e1", lineHeight: 1.7 }}>{aiResult.afternoon}</div>
                      </div>
                    )}
                  </div>
                  {/* 근거 */}
                  {aiResult.rationale && (
                    <div>
                      <div style={{ fontSize: "10px", fontWeight: 700, color: "#94a3b8", marginBottom: "6px", textTransform: "uppercase", letterSpacing: "0.05em" }}>
                        근거
                      </div>
                      <div style={{ fontSize: "12px", color: "#94a3b8", lineHeight: 1.8 }}>{aiResult.rationale}</div>
                    </div>
                  )}
                  {/* 주의 */}
                  {aiResult.caution && (
                    <div style={{ background: "#2d1b00", border: "1px solid #92400e", borderRadius: "6px", padding: "8px 14px", fontSize: "12px", color: "#fcd34d", lineHeight: 1.7 }}>
                      ⚠ {aiResult.caution}
                    </div>
                  )}
                  <div style={{ fontSize: "10px", color: "#475569", marginTop: "4px" }}>
                    ※ 기술적 분석 참고 정보이며 투자 권유가 아닙니다.
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// AI 추천서 마크다운을 세련되게 렌더링해주는 초경량 정적 헬퍼 함수
function parseInlineMarkdown(text: string) {
  const parts = text.split("**");
  return parts.map((part, idx) => {
    if (idx % 2 === 1) {
      return <strong key={idx} style={{ color: "#f8fafc", fontWeight: 700 }}>{part}</strong>;
    }
    return part;
  });
}

function renderMarkdown(text: string) {
  if (!text) return null;
  return text.split("\n").map((line, i) => {
    const trimmed = line.trim();
    if (trimmed.startsWith("### ")) {
      return <h4 key={i} style={{ color: "#818cf8", marginTop: "18px", marginBottom: "8px", fontSize: "15px", borderBottom: "1px solid #334155", paddingBottom: "4px", fontWeight: 700 }}>{trimmed.slice(4)}</h4>;
    }
    if (trimmed.startsWith("## ")) {
      return <h3 key={i} style={{ color: "#38bdf8", marginTop: "22px", marginBottom: "10px", fontSize: "17px", borderBottom: "1px solid #1e293b", paddingBottom: "6px", fontWeight: 700 }}>{trimmed.slice(3)}</h3>;
    }
    if (trimmed.startsWith("# ")) {
      return <h2 key={i} style={{ color: "#f8fafc", marginTop: "26px", marginBottom: "12px", fontSize: "19px", fontWeight: 800 }}>{trimmed.slice(2)}</h2>;
    }
    if (trimmed.startsWith("- ") || trimmed.startsWith("* ")) {
      return <li key={i} style={{ marginLeft: "20px", marginBottom: "6px", color: "#e2e8f0", listStyleType: "disc" }}>{parseInlineMarkdown(trimmed.slice(2))}</li>;
    }
    const numMatch = trimmed.match(/^(\d+)\.\s(.*)/);
    if (numMatch) {
      return <div key={i} style={{ marginLeft: "12px", marginBottom: "8px", color: "#e2e8f0", lineHeight: "1.6" }}><strong>{numMatch[1]}.</strong> {parseInlineMarkdown(numMatch[2])}</div>;
    }
    if (!trimmed) return <div key={i} style={{ height: "10px" }} />;
    return <p key={i} style={{ margin: "6px 0 10px 0", lineHeight: "1.65", color: "#cbd5e1" }}>{parseInlineMarkdown(line)}</p>;
  });
}

export function StockControlPanel({
  currentTicker,
  currentName,
  children,
}: {
  currentTicker: string;
  currentName: string;
  children: React.ReactNode;
}) {
  // 메인 뷰 모드: "stocks" (종목 관리/수집 메인 화면) vs "report" (리포트 상세 화면)
  const [viewMode, setViewMode] = useState<"stocks" | "report">("stocks");
  const [activeTab, setActiveTab] = useState<"list" | "collect" | "auto" | "forecast" | "longterm" | "portfolio" | "watch" | "settings">("auto");
  const [watchUnread, setWatchUnread] = useWatchUnread();

  // 타입 정의
  interface LongtermResult {
    ticker: string;
    name: string;
    asset_type: string;
    market: string;
    sector_code: string;
    industry: string;
    current_price: number;
    calc_id: string | null;
    calc_model: string | null;
    has_calc: boolean;
    bear_value: number | null;
    base_value: number | null;
    bull_value: number | null;
    safety_margin_pct: number | null;
    expected_return_pct: number | null;
  }

  interface PortfolioExposure {
    asset_id: string;
    name: string;
    asset_type: string;
    effective_pct: number;
    value_krw: number;
  }

  interface PortfolioWarning {
    code: string;
    name?: string;
    value_pct: number;
    limit_pct: number;
    sector?: string;
    currency?: string;
  }

  interface AllocationRebalance {
    status: "OK" | "OVERWEIGHT" | "UNDERWEIGHT";
    action: string;
    amount_krw: number;
  }

  interface AllocationStatus {
    total_assets_krw: number;
    stock_value_krw: number;
    cash_krw: number;
    safe_value_krw: number;
    risk_value_krw: number;
    safe_pct: number;
    risk_pct: number;
    safe_target_pct: number;
    risk_target_pct: number;
    safe_gap_pct: number;
    risk_gap_pct: number;
    tolerance_pct: number;
    rebalance: { safe: AllocationRebalance; risk: AllocationRebalance };
  }

  interface InvestorPolicy {
    total_assets_krw: number;
    cash_krw: number;
    allocation: { safe_asset_target_pct: number; risk_asset_target_pct: number };
    concentration: { max_single_position_pct_of_total: number; max_sector_pct_of_total: number };
    rebalance: { tolerance_pct: number };
  }

  interface PortfolioData {
    total_value_krw: number;
    asset_count: number;
    effective_position_count: number;
    warnings: PortfolioWarning[];
    is_policy_custom: boolean;
    exposures: PortfolioExposure[];
    sector_exposure: Record<string, number>;
    currency_exposure: Record<string, number>;
    allocation_status?: AllocationStatus;
    policy?: InvestorPolicy;
  }

  // 데이터 상태
  const [longtermResults, setLongtermResults] = useState<LongtermResult[]>([]);
  const [longtermLoading, setLongtermLoading] = useState(false);

  const [portfolioData, setPortfolioData] = useState<PortfolioData | null>(null);
  const [portfolioLoading, setPortfolioLoading] = useState(false);
  const [portfolioRecommend, setPortfolioRecommend] = useState<string>("");
  const [portfolioRecommendLoading, setPortfolioRecommendLoading] = useState(false);
  const [policyEdit, setPolicyEdit] = useState<InvestorPolicy | null>(null);
  const [policySaving, setPolicySaving] = useState(false);
  const [policyPanelOpen, setPolicyPanelOpen] = useState(false);
  const [assets, setAssets] = useState<AssetSummary[]>([]);
  const [apiConnected, setApiConnected] = useState(false);

  // 모든 종목 중 가장 최근에 시세를 조회한 시각 (ISO 문자열은 사전순 = 시간순)
  const latestRetrievedAt = assets.reduce<string | null>((latest, a) => {
    const t = a.price_info.retrieved_at;
    return t && (!latest || t > latest) ? t : latest;
  }, null);

  // 검색 상태
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<SearchResult[]>([]);
  const [searching, setSearching] = useState(false);

  // 편집/등록 폼 상태
  const [editingTicker, setEditingTicker] = useState<string | null>(null);
  const [formData, setFormData] = useState({
    ticker: "",
    name: "",
    market: "KR",
    exchange: "KOSPI",
    asset_type: "STOCK",
    currency: "KRW",
    price_symbol: "",
    sector_code: "TECH_SERVICES",
    industry: "일반",
    analysis_lens: "QUALITY_GROWTH",
    qty: 0,
  });
  const [showForm, setShowForm] = useState(false);
  const [loading, setLoading] = useState(false);

  // ── 예측(Forecast) 상태 ──
  const [forecastAlgorithm, setForecastAlgorithm] = useState<string>("MA_CROSS");
  const [forecastRunning, setForecastRunning] = useState(false);
  const [forecastReport, setForecastReport] = useState<ForecastReport | null>(null);
  const [forecastList, setForecastList] = useState<ForecastSummary[]>([]);
  const [forecastListLoading, setForecastListLoading] = useState(false);
  const [forecastSelectedId, setForecastSelectedId] = useState<string | null>(null);

  // ── 자동 Bob 분석 순차 실행 상태 ──
  // autoInterpretIdx: 현재 자동 실행 중인 종목 인덱스 (null = 비활성)
  const [autoInterpretIdx, setAutoInterpretIdx] = useState<number | null>(null);
  const [autoInterpretRunning, setAutoInterpretRunning] = useState(false);
  // 각 종목별 trigger 카운터 (autoInterpretIdx가 해당 인덱스를 가리킬 때 +1되어 ForecastCard에서 감지)
  const [autoTriggers, setAutoTriggers] = useState<number[]>([]);

  const ALGO_OPTIONS: { value: string; label: string }[] = [
    { value: "MA_CROSS", label: "이동평균 크로스 (5MA × 20MA)" },
    { value: "RSI", label: "RSI-14 과매수/과매도" },
    { value: "BOLLINGER", label: "볼린저 밴드 (20일, 2σ)" },
    { value: "COMBINED", label: "복합 신호 (MA + RSI + 볼린저)" },
  ];

  const fetchForecastList = useCallback(async () => {
    setForecastListLoading(true);
    try {
      const res = await fetch(`${API_BASE}/api/forecast/list?limit=30`);
      if (res.ok) {
        const data = await res.json();
        setForecastList(data.forecasts || []);
      }
    } catch {
      // ignore
    } finally {
      setForecastListLoading(false);
    }
  }, []);

  // 자동 Bob 분석 전체 순차 실행 시작
  const handleAutoInterpretAll = useCallback((report: ForecastReport) => {
    const count = report.results.length;
    if (count === 0) return;
    // 초기 배열: 인덱스 0만 1로 설정해 첫 번째 카드에 즉시 trigger 발사
    const initial = new Array(count).fill(0);
    initial[0] = 1;
    setAutoTriggers(initial);
    setAutoInterpretRunning(true);
    setAutoInterpretIdx(0);
  }, []);

  // 각 카드의 자동 분석 완료 콜백 — 다음 종목으로 넘어감
  const handleCardAutoComplete = useCallback((idx: number, totalCount: number) => {
    const next = idx + 1;
    if (next < totalCount) {
      setAutoInterpretIdx(next);
      setAutoTriggers((prev) => {
        const arr = [...prev];
        arr[next] = (arr[next] ?? 0) + 1;
        return arr;
      });
    } else {
      setAutoInterpretIdx(null);
      setAutoInterpretRunning(false);
    }
  }, []);

  const handleRunForecast = async () => {
    setForecastRunning(true);
    setForecastReport(null);
    setAutoInterpretIdx(null);
    setAutoInterpretRunning(false);
    setAutoTriggers([]);
    try {
      const res = await fetch(`${API_BASE}/api/forecast`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ algorithm: forecastAlgorithm }),
      });
      const data = await res.json();
      if (data.success && data.report) {
        setForecastReport(data.report);
        setForecastSelectedId(data.report.id);
        await fetchForecastList();
        // 분석 완료 직후 Bob 자동 분석 시작
        handleAutoInterpretAll(data.report);
      } else {
        alert(`예측 실패: ${data.error || "오류"}`);
      }
    } catch (err) {
      alert(`예측 오류: ${err}`);
    } finally {
      setForecastRunning(false);
    }
  };

  const handleLoadForecast = async (id: string) => {
    setForecastSelectedId(id);
    setForecastRunning(true);
    // 저장된 리포트 열람 시 일괄 자동 Bob AI 분석 비활성화
    setAutoInterpretIdx(null);
    setAutoInterpretRunning(false);
    setAutoTriggers([]);
    try {
      const res = await fetch(`${API_BASE}/api/forecast/${id}`);
      if (res.ok) {
        const data = await res.json();
        setForecastReport(data);
      }
    } catch {
      // ignore
    } finally {
      setForecastRunning(false);
    }
  };

  // 수집 제어 상태
  const [selectedCollectTicker, setSelectedCollectTicker] = useState<string>(currentTicker);
  const [collectingStep, setCollectingStep] = useState<string | null>(null);
  const [logs, setLogs] = useState<string[]>([]);          // 탭2: 수동 수집 로그
  const [autoLogs, setAutoLogs] = useState<string[]>([]);  // 탭3: 자동 업데이트 로그
  const logContainerRef = useRef<HTMLPreElement>(null);
  const autoLogContainerRef = useRef<HTMLPreElement>(null);
  const [showRunAllConfirm, setShowRunAllConfirm] = useState(false);

  // 리로드 후 자동 복원: select 직후 reload 시 sessionStorage 플래그로 report 뷰 복귀
  // SSR/클라이언트 불일치를 막기 위해 반드시 useEffect 안에서만 sessionStorage 접근.
  // 마운트 즉시 한 번만 실행되므로 setState-in-effect 경고는 의도된 패턴.
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (sessionStorage.getItem("open-report") === "1") {
      sessionStorage.removeItem("open-report");
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setViewMode("report");
    }
  }, []);

  useEffect(() => {
    if (logContainerRef.current) {
      logContainerRef.current.scrollTop = logContainerRef.current.scrollHeight;
    }
  }, [logs]);

  useEffect(() => {
    if (autoLogContainerRef.current) {
      autoLogContainerRef.current.scrollTop = autoLogContainerRef.current.scrollHeight;
    }
  }, [autoLogs]);

  // 자동 업데이트 상태
  const [autoStatus, setAutoStatus] = useState<{
    total: number;
    stale_candidates: string[];
  } | null>(null);
  const [autoUpdating, setAutoUpdating] = useState(false);

  // 초기 데이터 로드
  const fetchAssets = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/api/assets`);
      if (res.ok) {
        const data = await res.json();
        setAssets(data.assets || []);
        setApiConnected(true);
      } else {
        setApiConnected(false);
      }
    } catch {
      setApiConnected(false);
    }
  }, []);

  const checkAutoStatus = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/api/auto-update-status`);
      if (res.ok) {
        const data = await res.json();
        setAutoStatus(data);
      }
    } catch {
      // ignore
    }
  }, []);

  const fetchLongtermData = useCallback(async () => {
    setLongtermLoading(true);
    try {
      const res = await fetch(`${API_BASE}/api/longterm/status`);
      if (res.ok) {
        const data = await res.json();
        setLongtermResults(data.results || []);
      }
    } catch (e) {
      console.error("장기분석 데이터를 불러오지 못했습니다.", e);
    } finally {
      setLongtermLoading(false);
    }
  }, []);

  const fetchPortfolioData = useCallback(async () => {
    setPortfolioLoading(true);
    try {
      const res = await fetch(`${API_BASE}/api/portfolio/status`);
      if (res.ok) {
        const data = await res.json();
        setPortfolioData(data);
        // 정책을 서버에서 받아 편집 폼 초기화
        if (data.policy) {
          setPolicyEdit(data.policy);
        }
      }
    } catch (e) {
      console.error("포트폴리오 데이터를 불러오지 못했습니다.", e);
    } finally {
      setPortfolioLoading(false);
    }
  }, []);

  const savePolicy = useCallback(async () => {
    if (!policyEdit) return;
    setPolicySaving(true);
    try {
      const res = await fetch(`${API_BASE}/api/portfolio/policy`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(policyEdit),
      });
      if (res.ok) {
        await fetchPortfolioData();
      }
    } catch (e) {
      console.error("정책 저장 실패:", e);
    } finally {
      setPolicySaving(false);
    }
  }, [policyEdit, fetchPortfolioData]);

  const generatePortfolioRecommend = useCallback(async () => {
    setPortfolioRecommendLoading(true);
    setPortfolioRecommend("");
    try {
      const res = await fetch(`${API_BASE}/api/portfolio/recommend`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
      });
      if (res.ok) {
        const data = await res.json();
        setPortfolioRecommend(data.recommendation || "");
      } else {
        setPortfolioRecommend("### AI 자문서 생성 실패\n\n서버에서 에러를 반환했습니다.");
      }
    } catch (e) {
      console.error("포트폴리오 AI 추천서를 생성하지 못했습니다.", e);
      setPortfolioRecommend("### AI 자문서 생성 실패\n\n네트워크 또는 서버 연결이 원활하지 않습니다.");
    } finally {
      setPortfolioRecommendLoading(false);
    }
  }, []);

  // forecast 탭 진입 시 리포트 목록 자동 로드
  useEffect(() => {
    if (activeTab === "forecast") {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      fetchForecastList();
    }
  }, [activeTab, fetchForecastList]);

  // longterm 탭 진입 시 데이터 자동 로드
  useEffect(() => {
    if (activeTab === "longterm") {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      fetchLongtermData();
    }
  }, [activeTab, fetchLongtermData]);

  // portfolio 탭 진입 시 데이터 자동 로드
  useEffect(() => {
    if (activeTab === "portfolio") {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      fetchPortfolioData();
      setPortfolioRecommend(""); // 탭 진입 시 AI 추천 내용은 초기화
    }
  }, [activeTab, fetchPortfolioData]);

  useEffect(() => {
    let ignore = false;
    const init = async () => {
      // 1. 등록된 종목 목록 먼저 즉시 로드
      try {
        const res = await fetch(`${API_BASE}/api/assets`);
        if (res.ok && !ignore) {
          const data = await res.json();
          setAssets(data.assets || []);
          setApiConnected(true);
        }
      } catch {
        if (!ignore) setApiConnected(false);
      }

      // 2. 오늘 시세 갱신 필요 여부 확인 후 백그라운드 자동 업데이트
      try {
        const res2 = await fetch(`${API_BASE}/api/auto-update-status`);
        if (res2.ok && !ignore) {
          const data2 = await res2.json();
          setAutoStatus(data2);

          // 이미 실행 중이거나 stale 종목이 있으면 트리거
          const shouldStart = data2.stale_candidates && data2.stale_candidates.length > 0 && !data2.running;
          if (shouldStart || data2.running) {
            if (!ignore) setAutoUpdating(true);
            if (shouldStart) {
              try {
                await fetch(`${API_BASE}/api/auto-update`, { method: "POST" });
              } catch { /* ignore */ }
            }
            // 완료될 때까지 폴링
            const poll = async () => {
              if (ignore) return;
              try {
                const s = await fetch(`${API_BASE}/api/auto-update-status`);
                if (s.ok && !ignore) {
                  const sd = await s.json();
                  setAutoStatus(sd);
                  if (sd.running) {
                    setTimeout(poll, 3000);
                  } else {
                    await fetchAssets();
                    if (!ignore) setAutoUpdating(false);
                  }
                }
              } catch { if (!ignore) setAutoUpdating(false); }
            };
            setTimeout(poll, 3000);
          }
        }
      } catch {
        // ignore
      }
    };
    init();
    return () => {
      ignore = true;
    };
  }, [fetchAssets]);

  // 종목 검색 핸들러
  const handleSearch = async (q: string) => {
    setSearchQuery(q);
    if (!q.trim()) {
      setSearchResults([]);
      return;
    }
    try {
      setSearching(true);
      const res = await fetch(`${API_BASE}/api/search?q=${encodeURIComponent(q)}`);
      if (res.ok) {
        const data = await res.json();
        setSearchResults(data.results || []);
      }
    } catch {
      setSearchResults([]);
    } finally {
      setSearching(false);
    }
  };

  // 검색 결과에서 폼으로 채우기
  const handleSelectSearchResult = (item: SearchResult) => {
    setFormData({
      ticker: item.ticker,
      name: item.name,
      market: item.market,
      exchange: item.exchange,
      asset_type: item.asset_type,
      currency: item.currency,
      price_symbol: item.price_symbol,
      sector_code: item.market === "KR" ? "TECH_SERVICES" : "CONSUMER_AUTOMOTIVE",
      industry: item.market === "KR" ? "국내 주식" : "해외 주식",
      analysis_lens: "QUALITY_GROWTH",
      qty: 0,
    });
    setEditingTicker(item.registered ? item.ticker : null);
    setShowForm(true);
    setSearchResults([]);
    setSearchQuery("");
  };

  // 종목 등록/수정 저장
  const handleSaveAsset = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      const res = await fetch(`${API_BASE}/api/assets`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(formData),
      });
      if (res.ok) {
        alert(`종목 [${formData.name}] 이(가) 저장되었습니다.`);
        setShowForm(false);
        setEditingTicker(null);
        await fetchAssets();
      } else {
        const err = await res.json();
        alert(`저장 실패: ${err.error || "오류"}`);
      }
    } catch (err) {
      alert(`저장 실패: ${err}`);
    }
  };

  // 종목 삭제
  const handleDeleteAsset = async (ticker: string, name: string) => {
    if (!confirm(`정말로 종목 [${name} (${ticker})]을(를) 삭제하시겠습니까?`)) {
      return;
    }
    try {
      const res = await fetch(`${API_BASE}/api/assets/${ticker}`, {
        method: "DELETE",
      });
      if (res.ok) {
        alert("삭제되었습니다.");
        await fetchAssets();
      }
    } catch (err) {
      alert(`삭제 실패: ${err}`);
    }
  };

  // 종목 선택 및 리포트 화면으로 이동
  const handleOpenReport = async (ticker: string) => {
    if (ticker === currentTicker) {
      setViewMode("report");
      window.scrollTo({ top: 0, behavior: "smooth" });
      return;
    }
    try {
      setLoading(true);
      const res = await fetch(`${API_BASE}/api/select`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ticker }),
      });
      // API 는 빌드 실패도 HTTP 200 + success:false 로 돌려준다.
      const data = await res.json().catch(() => ({}));
      if (res.ok && data.success !== false) {
        sessionStorage.setItem("open-report", "1");
        window.location.reload();
      } else {
        alert(`종목 전환 실패: ${data.error || "오류"}`);
      }
    } catch (err) {
      alert(`전환 실패: ${err}`);
    } finally {
      setLoading(false);
    }
  };

  // 수집 실행 공통
  const appendLog = (msg: string) => {
    const time = new Date().toLocaleTimeString();
    setLogs((prev) => [...prev, `[${time}] ${msg}`]);
  };

  const appendAutoLog = (msg: string) => {
    const time = new Date().toLocaleTimeString();
    setAutoLogs((prev) => [...prev, `[${time}] ${msg}`]);
  };

  const runCollect = async (endpoint: string, payload: Record<string, unknown>, stepName: string) => {
    try {
      setCollectingStep(stepName);
      appendLog(`▶ ${stepName} 시작: ${payload.ticker}`);
      const res = await fetch(`${API_BASE}${endpoint}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json();
      if (data.output) {
        appendLog(data.output);
      }
      if (data.success || res.ok) {
        appendLog(`✓ ${stepName} 완료`);
      } else {
        appendLog(`✗ ${stepName} 실패: ${data.error || "오류"}`);
      }
      await fetchAssets();
    } catch (err) {
      appendLog(`✗ ${stepName} 네트워크 오류: ${err}`);
    } finally {
      setCollectingStep(null);
    }
  };

  // 오늘 시세 일괄 자동 업데이트 실행
  const handleRunAutoUpdate = async () => {
    try {
      setCollectingStep("오늘 시세 자동 업데이트");
      appendAutoLog("▶ 오늘 시세 자동 업데이트 검사 및 수집 시작...");
      const res = await fetch(`${API_BASE}/api/auto-update`, { method: "POST" });
      const data = await res.json();
      if (!data.started && data.reason === "already_running") {
        appendAutoLog("⏳ 이미 업데이트가 진행 중입니다. 완료를 기다립니다...");
      }
      // 폴링으로 완료 감지
      const poll = async (): Promise<void> => {
        try {
          const s = await fetch(`${API_BASE}/api/auto-update-status`);
          if (s.ok) {
            const sd = await s.json();
            setAutoStatus(sd);
            if (sd.running) {
              await new Promise((r) => setTimeout(r, 3000));
              return poll();
            }
            // 완료
            const result = sd.last_result;
            if (result?.logs) {
              result.logs.forEach((l: string) => appendAutoLog(l));
            }
            appendAutoLog(`✓ 자동 업데이트 완료 (대상: ${result?.stale_count ?? "?"}개, 성공: ${result?.updated_count ?? "?"}개)`);
            await fetchAssets();
            await checkAutoStatus();
          }
        } catch (err) {
          appendAutoLog(`✗ 자동 업데이트 오류: ${err}`);
        }
      };
      await poll();
    } catch (err) {
      appendAutoLog(`✗ 자동 업데이트 오류: ${err}`);
    } finally {
      setCollectingStep(null);
    }
  };

  return (
    <div className="cio-app-shell" style={{
      minHeight: "100vh",
      display: "flex",
      flexDirection: "column",
      background: "#0b1120",
    }}>
      {/* 1~4단계 전체 실행 확인 모달 */}
      {showRunAllConfirm && (
        <div style={{
          position: "fixed",
          inset: 0,
          background: "rgba(0,0,0,0.65)",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          zIndex: 9999,
        }}>
          <div style={{
            background: "#1e293b",
            border: "1px solid #334155",
            borderRadius: "12px",
            padding: "28px 32px",
            width: "360px",
            boxShadow: "0 8px 32px rgba(0,0,0,0.5)",
          }}>
            <div style={{ fontWeight: 700, fontSize: "15px", color: "#f1f5f9", marginBottom: "8px" }}>
              ⚡ 1~4단계 전체 실행
            </div>
            <div style={{ fontSize: "13px", color: "#94a3b8", marginBottom: "24px", lineHeight: 1.6 }}>
              실행 전 기존 로그를 어떻게 할까요?
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: "10px" }}>
              <button
                type="button"
                onClick={() => {
                  setShowRunAllConfirm(false);
                  setLogs([]);
                  runCollect("/api/collect/all", { ticker: selectedCollectTicker }, "전체 파이프라인 일괄 실행");
                }}
                style={{
                  background: "#7c3aed",
                  color: "#fff",
                  border: "none",
                  borderRadius: "7px",
                  padding: "11px 16px",
                  cursor: "pointer",
                  fontSize: "13px",
                  fontWeight: 600,
                  textAlign: "left",
                }}
              >
                🗑 로그 초기화 후 시작
                <div style={{ fontSize: "11px", fontWeight: 400, color: "#ddd6fe", marginTop: "3px" }}>
                  기존 로그를 모두 지우고 새로 시작합니다
                </div>
              </button>
              <button
                type="button"
                onClick={() => {
                  setShowRunAllConfirm(false);
                  runCollect("/api/collect/all", { ticker: selectedCollectTicker }, "전체 파이프라인 일괄 실행");
                }}
                style={{
                  background: "#0f172a",
                  color: "#e2e8f0",
                  border: "1px solid #334155",
                  borderRadius: "7px",
                  padding: "11px 16px",
                  cursor: "pointer",
                  fontSize: "13px",
                  fontWeight: 600,
                  textAlign: "left",
                }}
              >
                📋 로그 유지하며 시작
                <div style={{ fontSize: "11px", fontWeight: 400, color: "#64748b", marginTop: "3px" }}>
                  기존 로그 아래에 이어서 기록합니다
                </div>
              </button>
              <button
                type="button"
                onClick={() => setShowRunAllConfirm(false)}
                style={{
                  background: "transparent",
                  color: "#64748b",
                  border: "none",
                  padding: "8px",
                  cursor: "pointer",
                  fontSize: "13px",
                  textAlign: "center",
                }}
              >
                취소
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 종목 등록/수정 모달 팝업 */}
      {showForm && (
        <div
          onClick={(e) => {
            if (e.target === e.currentTarget) {
              setShowForm(false);
              setEditingTicker(null);
            }
          }}
          style={{
            position: "fixed",
            inset: 0,
            background: "rgba(0,0,0,0.7)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            zIndex: 9999,
            padding: "20px",
          }}
        >
          <div style={{
            background: "#1e293b",
            border: "1px solid #3b82f6",
            borderRadius: "12px",
            padding: "24px 28px",
            width: "560px",
            maxWidth: "100%",
            boxShadow: "0 10px 40px rgba(0,0,0,0.6)",
          }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "20px" }}>
              <h3 style={{ margin: 0, fontSize: "17px", fontWeight: 700, color: "#38bdf8" }}>
                {editingTicker ? `종목 정보 수정 (${formData.ticker})` : "신규 종목 등록"}
              </h3>
              <button
                type="button"
                onClick={() => {
                  setShowForm(false);
                  setEditingTicker(null);
                }}
                style={{
                  background: "transparent",
                  border: "none",
                  color: "#94a3b8",
                  fontSize: "18px",
                  cursor: "pointer",
                  padding: "4px 8px",
                }}
              >
                ✕
              </button>
            </div>

            {/* 신규 등록 시 종목 빠른 검색 */}
            {!editingTicker && (
              <div style={{ marginBottom: "18px", position: "relative" }}>
                <label style={{ display: "block", fontSize: "12px", color: "#38bdf8", fontWeight: 600, marginBottom: "6px" }}>
                  🔍 종목 검색으로 자동 완성
                </label>
                <div style={{ position: "relative" }}>
                  <input
                    type="text"
                    placeholder="종목명 또는 티커 검색 (예: 삼성전자, 005930, 카카오, 테슬라, TSLA, AAPL, SCHD)..."
                    value={searchQuery}
                    onChange={(e) => handleSearch(e.target.value)}
                    style={{
                      width: "100%",
                      background: "#0f172a",
                      border: "1px solid #0284c7",
                      borderRadius: "6px",
                      padding: "10px 36px 10px 12px",
                      color: "#fff",
                      fontSize: "13px",
                    }}
                  />
                  {searchQuery && (
                    <button
                      type="button"
                      onClick={() => {
                        setSearchQuery("");
                        setSearchResults([]);
                      }}
                      style={{
                        position: "absolute",
                        right: "10px",
                        top: "8px",
                        background: "#334155",
                        border: "none",
                        borderRadius: "50%",
                        width: "20px",
                        height: "20px",
                        color: "#94a3b8",
                        fontSize: "11px",
                        cursor: "pointer",
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "center",
                      }}
                    >
                      ✕
                    </button>
                  )}
                  {searching && (
                    <span style={{ position: "absolute", right: searchQuery ? "38px" : "12px", top: "10px", fontSize: "12px", color: "#94a3b8" }}>
                      검색 중...
                    </span>
                  )}
                </div>

                {/* 모달 내 검색 결과 목록 */}
                {searchResults.length > 0 && (
                  <div style={{
                    position: "absolute",
                    top: "100%",
                    left: 0,
                    right: 0,
                    background: "#0f172a",
                    border: "1px solid #38bdf8",
                    borderRadius: "6px",
                    marginTop: "4px",
                    maxHeight: "220px",
                    overflowY: "auto",
                    zIndex: 1000,
                    boxShadow: "0 10px 25px rgba(0,0,0,0.7)",
                  }}>
                    <div style={{
                      display: "flex",
                      justifyContent: "space-between",
                      alignItems: "center",
                      padding: "6px 12px",
                      background: "#1e293b",
                      borderBottom: "1px solid #334155",
                      fontSize: "11px",
                      color: "#94a3b8",
                    }}>
                      <span>검색 결과 ({searchResults.length}건) — 클릭 시 폼 자동 입력</span>
                      <button
                        type="button"
                        onClick={() => setSearchResults([])}
                        style={{ background: "transparent", border: "none", color: "#38bdf8", cursor: "pointer", fontSize: "11px" }}
                      >
                        닫기 ✕
                      </button>
                    </div>
                    {searchResults.map((item) => (
                      <div
                        key={item.ticker}
                        onClick={() => handleSelectSearchResult(item)}
                        style={{
                          display: "flex",
                          justifyContent: "space-between",
                          alignItems: "center",
                          padding: "8px 12px",
                          borderBottom: "1px solid #1e293b",
                          cursor: "pointer",
                          background: "#0f172a",
                          transition: "background 0.15s ease",
                        }}
                      >
                        <div>
                          <strong style={{ color: "#38bdf8", fontSize: "13px" }}>{item.name}</strong> ({item.ticker})
                          <span style={{ marginLeft: "8px", fontSize: "11px", color: "#94a3b8" }}>
                            {item.market} · {item.exchange} · {item.asset_type}
                          </span>
                        </div>
                        <span style={{
                          background: item.registered ? "#475569" : "#10b981",
                          color: "#fff",
                          padding: "3px 8px",
                          borderRadius: "4px",
                          fontSize: "11px",
                          fontWeight: 600,
                        }}>
                          {item.registered ? "수정" : "선택"}
                        </span>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}

            <form onSubmit={handleSaveAsset}>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(2, 1fr)", gap: "14px" }}>
                <div>
                  <label style={{ display: "block", fontSize: "12px", color: "#94a3b8", marginBottom: "4px" }}>
                    티커 (종목코드) *
                  </label>
                  <input
                    type="text"
                    required
                    value={formData.ticker}
                    onChange={(e) => setFormData({ ...formData, ticker: e.target.value })}
                    disabled={!!editingTicker}
                    placeholder="예: 005930, AAPL"
                    style={{ width: "100%", background: "#0f172a", border: "1px solid #334155", color: "#fff", padding: "8px 10px", borderRadius: "6px", fontSize: "13px" }}
                  />
                </div>
                <div>
                  <label style={{ display: "block", fontSize: "12px", color: "#94a3b8", marginBottom: "4px" }}>
                    종목명 *
                  </label>
                  <input
                    type="text"
                    required
                    value={formData.name}
                    onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                    placeholder="예: 삼성전자, Apple"
                    style={{ width: "100%", background: "#0f172a", border: "1px solid #334155", color: "#fff", padding: "8px 10px", borderRadius: "6px", fontSize: "13px" }}
                  />
                </div>
                <div>
                  <label style={{ display: "block", fontSize: "12px", color: "#94a3b8", marginBottom: "4px" }}>
                    시장
                  </label>
                  <select
                    value={formData.market}
                    onChange={(e) => {
                      const m = e.target.value;
                      setFormData({
                        ...formData,
                        market: m,
                        exchange: m === "KR" ? "KOSPI" : "NASDAQ",
                        currency: m === "KR" ? "KRW" : "USD",
                      });
                    }}
                    style={{ width: "100%", background: "#0f172a", border: "1px solid #334155", color: "#fff", padding: "8px 10px", borderRadius: "6px", fontSize: "13px" }}
                  >
                    <option value="KR">한국 (KR)</option>
                    <option value="US">미국 (US)</option>
                  </select>
                </div>
                <div>
                  <label style={{ display: "block", fontSize: "12px", color: "#94a3b8", marginBottom: "4px" }}>
                    자산 유형
                  </label>
                  <select
                    value={formData.asset_type}
                    onChange={(e) => setFormData({ ...formData, asset_type: e.target.value })}
                    style={{ width: "100%", background: "#0f172a", border: "1px solid #334155", color: "#fff", padding: "8px 10px", borderRadius: "6px", fontSize: "13px" }}
                  >
                    <option value="STOCK">주식 (STOCK)</option>
                    <option value="ETF">ETF</option>
                  </select>
                </div>
                <div>
                  <label style={{ display: "block", fontSize: "12px", color: "#94a3b8", marginBottom: "4px" }}>
                    시세 심볼
                  </label>
                  <input
                    type="text"
                    value={formData.price_symbol}
                    placeholder={formData.market === "KR" ? `${formData.ticker || "005930"}.KS` : formData.ticker || "AAPL"}
                    onChange={(e) => setFormData({ ...formData, price_symbol: e.target.value })}
                    style={{ width: "100%", background: "#0f172a", border: "1px solid #334155", color: "#fff", padding: "8px 10px", borderRadius: "6px", fontSize: "13px" }}
                  />
                </div>
                <div>
                  <label style={{ display: "block", fontSize: "12px", color: "#94a3b8", marginBottom: "4px" }}>
                    보유 수량
                  </label>
                  <input
                    type="number"
                    value={formData.qty}
                    onChange={(e) => setFormData({ ...formData, qty: Number(e.target.value) })}
                    style={{ width: "100%", background: "#0f172a", border: "1px solid #334155", color: "#fff", padding: "8px 10px", borderRadius: "6px", fontSize: "13px" }}
                  />
                </div>
              </div>

              <div style={{ display: "flex", justifyContent: "flex-end", gap: "10px", marginTop: "24px" }}>
                <button
                  type="button"
                  onClick={() => {
                    setShowForm(false);
                    setEditingTicker(null);
                  }}
                  style={{
                    background: "#334155",
                    color: "#cbd5e1",
                    border: "none",
                    padding: "9px 18px",
                    borderRadius: "6px",
                    cursor: "pointer",
                    fontSize: "13px",
                    fontWeight: 500,
                  }}
                >
                  취소
                </button>
                <button
                  type="submit"
                  style={{
                    background: "#2563eb",
                    color: "#fff",
                    border: "none",
                    padding: "9px 20px",
                    borderRadius: "6px",
                    cursor: "pointer",
                    fontSize: "13px",
                    fontWeight: 600,
                  }}
                >
                  저장하기
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* 글로벌 최상단 네비게이션 헤더 */}
      <header style={{
        background: "#0b1120",
        borderBottom: "1px solid #1e293b",
        padding: "12px 24px",
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        color: "#f8fafc",
      }}>
        <button
          type="button"
          onClick={() => { setViewMode("stocks"); setActiveTab("auto"); }}
          style={{ display: "flex", alignItems: "center", gap: "8px", background: "none", border: "none", cursor: "pointer", padding: 0 }}
        >
          <span style={{ fontSize: "20px" }}>📈</span>
          <span style={{ fontWeight: 800, fontSize: "16px", letterSpacing: "-0.02em", color: "#38bdf8" }}>
            Smart Investment Advisor
          </span>
        </button>

        {/* 우측 퀵 컨트롤 */}
        <div style={{ display: "flex", alignItems: "center", gap: "12px" }}>
          {autoUpdating ? (
            <span style={{
              background: "#1e3a8a",
              color: "#60a5fa",
              border: "1px solid #2563eb",
              borderRadius: "4px",
              padding: "4px 12px",
              fontSize: "12px",
              fontWeight: 600,
              display: "flex",
              alignItems: "center",
              gap: "6px",
            }}>
              <span style={{ display: "inline-block", animation: "spin 1s linear infinite" }}>🔄</span>
              오늘 시세 백그라운드 동기화 중...
            </span>
          ) : autoStatus && autoStatus.stale_candidates.length > 0 ? (
            <button
              type="button"
              onClick={handleRunAutoUpdate}
              style={{
                background: "#f59e0b",
                color: "#000",
                border: "none",
                borderRadius: "4px",
                padding: "4px 10px",
                fontSize: "12px",
                fontWeight: 700,
                cursor: "pointer",
              }}
            >
              ⚡ 오늘 시세 최신화 ({autoStatus.stale_candidates.length}개)
            </button>
          ) : (
            <span style={{
              color: "#10b981",
              fontSize: "12px",
              fontWeight: 600,
            }}>
              ✓ 모든 시세 최신
              {latestRetrievedAt && (
                <span style={{ color: "#94a3b8", fontWeight: 400, marginLeft: "8px" }}>
                  (최근 조회 {formatDateTime(latestRetrievedAt)})
                </span>
              )}
            </span>
          )}

          {!apiConnected && (
            <span style={{ color: "#ef4444", fontSize: "12px" }}>
              ● API 오프라인
            </span>
          )}

          {viewMode === "report" && (
            <button
              type="button"
              onClick={() => setViewMode("stocks")}
              style={{
                background: "#334155",
                color: "#fff",
                border: "none",
                borderRadius: "4px",
                padding: "5px 12px",
                fontSize: "13px",
                cursor: "pointer",
              }}
            >
              ← 종목 목록으로
            </button>
          )}
        </div>
      </header>

      {/* 1. 내 종목 관리 & 데이터 수집 센터 메인 화면 */}
      {viewMode === "stocks" && (
        <div style={{
          maxWidth: "1680px",
          width: "100%",
          margin: "0 auto",
          padding: "24px 32px",
          color: "#f8fafc",
          flex: 1,
          display: "flex",
          flexDirection: "column",
          boxSizing: "border-box",
        }}>
          {/* 서브 탭 */}
          <div style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            borderBottom: "1px solid #1e293b",
            paddingBottom: "12px",
            marginBottom: "20px",
          }}>
            <div style={{ display: "flex", gap: "8px" }}>
              <button
                type="button"
                onClick={() => setActiveTab("auto")}
                style={{
                  background: activeTab === "auto" ? "#1e293b" : "transparent",
                  color: activeTab === "auto" ? "#38bdf8" : "#94a3b8",
                  border: activeTab === "auto" ? "1px solid #334155" : "none",
                  padding: "8px 16px",
                  borderRadius: "6px",
                  fontWeight: 600,
                  fontSize: "14px",
                  cursor: "pointer",
                }}
              >
                1. 시세 조회
              </button>
              <button
                type="button"
                onClick={() => setActiveTab("list")}
                style={{
                  background: activeTab === "list" ? "#1e293b" : "transparent",
                  color: activeTab === "list" ? "#38bdf8" : "#94a3b8",
                  border: activeTab === "list" ? "1px solid #334155" : "none",
                  padding: "8px 16px",
                  borderRadius: "6px",
                  fontWeight: 600,
                  fontSize: "14px",
                  cursor: "pointer",
                }}
              >
                2. 내 종목 ({assets.length}개)
              </button>
              <button
                type="button"
                onClick={() => setActiveTab("collect")}
                style={{
                  background: activeTab === "collect" ? "#1e293b" : "transparent",
                  color: activeTab === "collect" ? "#38bdf8" : "#94a3b8",
                  border: activeTab === "collect" ? "1px solid #334155" : "none",
                  padding: "8px 16px",
                  borderRadius: "6px",
                  fontWeight: 600,
                  fontSize: "14px",
                  cursor: "pointer",
                }}
              >
                3. 종목 분석
              </button>
              <button
                type="button"
                onClick={() => setActiveTab("forecast")}
                style={{
                  background: activeTab === "forecast" ? "#1e293b" : "transparent",
                  color: activeTab === "forecast" ? "#f59e0b" : "#94a3b8",
                  border: activeTab === "forecast" ? "1px solid #f59e0b" : "none",
                  padding: "8px 16px",
                  borderRadius: "6px",
                  fontWeight: 600,
                  fontSize: "14px",
                  cursor: "pointer",
                }}
              >
                4. 단기 분석 ✨
              </button>
              <button
                type="button"
                onClick={() => setActiveTab("longterm")}
                style={{
                  background: activeTab === "longterm" ? "#1e293b" : "transparent",
                  color: activeTab === "longterm" ? "#10b981" : "#94a3b8",
                  border: activeTab === "longterm" ? "1px solid #10b981" : "none",
                  padding: "8px 16px",
                  borderRadius: "6px",
                  fontWeight: 600,
                  fontSize: "14px",
                  cursor: "pointer",
                }}
              >
                5. 장기 분석 📈
              </button>
              <button
                type="button"
                onClick={() => setActiveTab("portfolio")}
                style={{
                  background: activeTab === "portfolio" ? "#1e293b" : "transparent",
                  color: activeTab === "portfolio" ? "#818cf8" : "#94a3b8",
                  border: activeTab === "portfolio" ? "1px solid #818cf8" : "none",
                  padding: "8px 16px",
                  borderRadius: "6px",
                  fontWeight: 600,
                  fontSize: "14px",
                  cursor: "pointer",
                }}
              >
                6. 포트폴리오 추천 💼
              </button>
              <button
                type="button"
                onClick={() => setActiveTab("watch")}
                style={{
                  background: activeTab === "watch" ? "#1e293b" : "transparent",
                  color: activeTab === "watch" ? "#38bdf8" : "#94a3b8",
                  border: activeTab === "watch" ? "1px solid #334155" : "none",
                  padding: "8px 16px",
                  borderRadius: "6px",
                  fontWeight: 600,
                  fontSize: "14px",
                  cursor: "pointer",
                }}
              >
                7. 감시 알림
                <UnreadBadge count={watchUnread} />
              </button>
              <button
                type="button"
                onClick={() => setActiveTab("settings")}
                style={{
                  background: activeTab === "settings" ? "#1e293b" : "transparent",
                  color: activeTab === "settings" ? "#38bdf8" : "#94a3b8",
                  border: activeTab === "settings" ? "1px solid #334155" : "none",
                  padding: "8px 16px",
                  borderRadius: "6px",
                  fontWeight: 600,
                  fontSize: "14px",
                  cursor: "pointer",
                }}
              >
                8. 설정
              </button>
            </div>

            <button
              type="button"
              onClick={() => {
                setFormData({
                  ticker: "",
                  name: "",
                  market: "KR",
                  exchange: "KOSPI",
                  asset_type: "STOCK",
                  currency: "KRW",
                  price_symbol: "",
                  sector_code: "TECH_SERVICES",
                  industry: "일반",
                  analysis_lens: "QUALITY_GROWTH",
                  qty: 0,
                });
                setEditingTicker(null);
                setShowForm(true);
              }}
              style={{
                background: "#10b981",
                color: "#fff",
                border: "none",
                borderRadius: "6px",
                padding: "8px 16px",
                fontSize: "13px",
                fontWeight: 600,
                cursor: "pointer",
              }}
            >
              + 새 종목 추가
            </button>
          </div>

          {/* 탭 1: 종목 리스트 & 검색 & 등록 */}
          {activeTab === "list" && (
            <div>
              {/* 종목 검색창 */}
              <div style={{
                position: "relative",
                marginBottom: "20px",
              }}>
                <input
                  type="text"
                  placeholder="종목 검색 및 추가 (예: 삼성전자, 005930, LG전자, 테슬라, TSLA, 엔비디아, NVDA, SCHD)..."
                  value={searchQuery}
                  onChange={(e) => handleSearch(e.target.value)}
                  style={{
                    width: "100%",
                    background: "#1e293b",
                    border: "1px solid #334155",
                    borderRadius: "6px",
                    padding: "12px 40px 12px 16px",
                    color: "#fff",
                    fontSize: "14px",
                  }}
                />
                {searchQuery && (
                  <button
                    type="button"
                    onClick={() => {
                      setSearchQuery("");
                      setSearchResults([]);
                    }}
                    style={{
                      position: "absolute",
                      right: "12px",
                      top: "10px",
                      background: "#334155",
                      border: "none",
                      borderRadius: "50%",
                      width: "24px",
                      height: "24px",
                      color: "#94a3b8",
                      fontSize: "12px",
                      cursor: "pointer",
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                    }}
                  >
                    ✕
                  </button>
                )}
                {searching && (
                  <span style={{ position: "absolute", right: searchQuery ? "44px" : "16px", top: "14px", fontSize: "12px", color: "#94a3b8" }}>
                    검색 중...
                  </span>
                )}
                {/* 검색 결과 드롭다운 */}
                {searchResults.length > 0 && (
                  <div style={{
                    position: "absolute",
                    top: "100%",
                    left: 0,
                    right: 0,
                    background: "#0f172a",
                    border: "1px solid #334155",
                    borderRadius: "6px",
                    marginTop: "6px",
                    maxHeight: "280px",
                    overflowY: "auto",
                    zIndex: 100,
                    boxShadow: "0 20px 25px -5px rgba(0,0,0,0.5)",
                  }}>
                    <div style={{
                      display: "flex",
                      justifyContent: "space-between",
                      alignItems: "center",
                      padding: "8px 16px",
                      background: "#1e293b",
                      borderBottom: "1px solid #334155",
                      fontSize: "12px",
                      color: "#94a3b8",
                    }}>
                      <span>검색 결과 ({searchResults.length}건)</span>
                      <button
                        type="button"
                        onClick={() => setSearchResults([])}
                        style={{
                          background: "transparent",
                          border: "none",
                          color: "#38bdf8",
                          cursor: "pointer",
                          fontSize: "12px",
                          fontWeight: 600,
                        }}
                      >
                        닫기 ✕
                      </button>
                    </div>
                    {searchResults.map((item) => (
                      <div
                        key={item.ticker}
                        style={{
                          display: "flex",
                          justifyContent: "space-between",
                          alignItems: "center",
                          padding: "10px 16px",
                          borderBottom: "1px solid #1e293b",
                          cursor: "pointer",
                          transition: "background 0.15s ease",
                        }}
                        onClick={() => handleSelectSearchResult(item)}
                      >
                        <div>
                          <strong style={{ color: "#38bdf8", fontSize: "14px" }}>{item.name}</strong> ({item.ticker})
                          <span style={{ marginLeft: "10px", fontSize: "12px", color: "#94a3b8" }}>
                            {item.market} · {item.exchange} · {item.asset_type}
                          </span>
                        </div>
                        <button
                          type="button"
                          style={{
                            background: item.registered ? "#475569" : "#10b981",
                            color: "#fff",
                            border: "none",
                            borderRadius: "4px",
                            padding: "4px 10px",
                            fontSize: "12px",
                            fontWeight: 600,
                          }}
                        >
                          {item.registered ? "수정" : "+ 등록하기"}
                        </button>
                      </div>
                    ))}
                  </div>
                )}
              </div>


              {/* 종목 리스트 테이블 */}
              <div style={{
                background: "#1e293b",
                borderRadius: "8px",
                overflow: "hidden",
                border: "1px solid #334155",
              }}>
                <table style={{
                  width: "100%",
                  borderCollapse: "collapse",
                  fontSize: "14px",
                  textAlign: "left",
                }}>
                  <thead>
                    <tr style={{ background: "#0f172a", color: "#94a3b8", borderBottom: "2px solid #334155" }}>
                      <th style={{ padding: "14px 16px" }}>종목</th>
                      <th style={{ padding: "14px 16px" }}>시장/유형</th>
                      <th style={{ padding: "14px 16px" }}>보유수량</th>
                      <th style={{ padding: "14px 16px" }}>최근 시세일</th>
                      <th style={{ padding: "14px 16px" }}>최근 실적분기</th>
                      <th style={{ padding: "14px 16px" }}>리포트 상태</th>
                      <th style={{ padding: "14px 16px", textAlign: "right" }}>-</th>
                    </tr>
                  </thead>
                  <tbody>
                    {assets.map((asset) => (
                      <tr
                        key={asset.ticker}
                        style={{
                          borderBottom: "1px solid #334155",
                          background: asset.is_selected ? "rgba(37, 99, 235, 0.15)" : "transparent",
                        }}
                      >
                        <td style={{ padding: "14px 16px" }}>
                          <div style={{ fontWeight: 700, fontSize: "15px", color: asset.is_selected ? "#60a5fa" : "#f8fafc", display: "flex", alignItems: "center", gap: "6px" }}>
                            {asset.name}
                            {asset.trading_halted && (
                              <span style={{
                                background: "#7f1d1d",
                                color: "#fca5a5",
                                fontSize: "10px",
                                fontWeight: 600,
                                padding: "2px 6px",
                                borderRadius: "3px",
                                letterSpacing: "0.02em",
                              }}>
                                거래중지
                              </span>
                            )}
                          </div>
                          <div style={{ fontSize: "12px", color: "#94a3b8" }}>
                            {asset.ticker} · {asset.exchange}
                          </div>
                        </td>
                        <td style={{ padding: "14px 16px" }}>
                          <span style={{
                            background: asset.market === "KR" ? "#1e3a8a" : "#065f46",
                            color: "#fff",
                            padding: "3px 8px",
                            borderRadius: "4px",
                            fontSize: "12px",
                            fontWeight: 600,
                            marginRight: "6px",
                          }}>
                            {asset.market}
                          </span>
                          <span style={{ fontSize: "12px", color: "#94a3b8" }}>{asset.asset_type}</span>
                        </td>
                        <td style={{ padding: "14px 16px" }}>{asset.qty}주</td>
                        <td style={{ padding: "14px 16px" }}>
                          {asset.price_info.exists ? (
                            <span style={{ color: "#34d399", fontWeight: 500 }}>
                              {asset.price_info.last_date}
                            </span>
                          ) : (
                            <span style={{ color: "#f87171" }}>미수집</span>
                          )}
                          {asset.price_info.exists && asset.price_info.retrieved_at && (
                            <div style={{ fontSize: "11px", color: "#94a3b8", marginTop: "2px" }}>
                              조회 {formatDateTime(asset.price_info.retrieved_at)}
                            </div>
                          )}
                        </td>
                        <td style={{ padding: "14px 16px" }}>
                          {asset.has_fundamentals ? (
                            <span style={{ color: "#38bdf8", fontWeight: 500 }}>
                              {typeof asset.fundamentals_as_of === "string" ? asset.fundamentals_as_of : "반영됨"}
                            </span>
                          ) : (
                            <span style={{ color: "#f87171" }}>미수집</span>
                          )}
                        </td>
                        <td style={{ padding: "14px 16px" }}>
                          {asset.has_report ? (
                            <span style={{ color: "#4ade80", fontWeight: 600 }}>● 생성됨</span>
                          ) : (
                            <span style={{ color: "#fbbf24" }}>○ 미생성</span>
                          )}
                        </td>
                        <td style={{ padding: "14px 16px", textAlign: "right" }}>
                          <div style={{ display: "inline-flex", gap: "8px" }}>
                            <button
                              type="button"
                              disabled={loading}
                              onClick={() => handleOpenReport(asset.ticker)}
                              style={{
                                background: loading ? "#1e40af" : "#2563eb",
                                color: "#fff",
                                border: "none",
                                padding: "6px 14px",
                                borderRadius: "4px",
                                fontSize: "13px",
                                fontWeight: 700,
                                cursor: loading ? "wait" : "pointer",
                                opacity: loading ? 0.7 : 1,
                              }}
                            >
                              {loading ? "전환 중..." : "📄 리포트"}
                            </button>
                            <button
                              type="button"
                              onClick={() => {
                                setSelectedCollectTicker(asset.ticker);
                                setActiveTab("collect");
                              }}
                              style={{
                                background: "#475569",
                                color: "#fff",
                                border: "none",
                                padding: "6px 10px",
                                borderRadius: "4px",
                                fontSize: "12px",
                                cursor: "pointer",
                              }}
                            >
                              수집
                            </button>
                            <button
                              type="button"
                              onClick={() => {
                                setFormData({
                                  ticker: asset.ticker,
                                  name: asset.name,
                                  market: asset.market,
                                  exchange: asset.exchange,
                                  asset_type: asset.asset_type,
                                  currency: asset.currency,
                                  price_symbol: asset.price_symbol,
                                  sector_code: asset.sector_code || "TECH_SERVICES",
                                  industry: asset.industry || "일반",
                                  analysis_lens: asset.analysis_lens || "QUALITY_GROWTH",
                                  qty: asset.qty,
                                });
                                setEditingTicker(asset.ticker);
                                setShowForm(true);
                              }}
                              style={{
                                background: "#334155",
                                color: "#fff",
                                border: "none",
                                padding: "6px 10px",
                                borderRadius: "4px",
                                fontSize: "12px",
                                cursor: "pointer",
                              }}
                            >
                              수정
                            </button>
                            <button
                              type="button"
                              onClick={() => handleDeleteAsset(asset.ticker, asset.name)}
                              style={{
                                background: "#7f1d1d",
                                color: "#fca5a5",
                                border: "none",
                                padding: "6px 10px",
                                borderRadius: "4px",
                                fontSize: "12px",
                                cursor: "pointer",
                              }}
                            >
                              삭제
                            </button>
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {/* 탭 2: 수동 데이터 수집 & 파이프라인 제어실 */}
          {activeTab === "collect" && (
            <div style={{
              flex: 1,
              display: "flex",
              flexDirection: "column",
              minHeight: 0,
            }}>
              <div style={{
                background: "#1e293b",
                padding: "20px",
                borderRadius: "8px",
                marginBottom: "16px",
                border: "1px solid #334155",
                flexShrink: 0,
              }}>
                <div style={{ display: "flex", alignItems: "center", gap: "14px", marginBottom: "20px" }}>
                  <label style={{ fontSize: "15px", fontWeight: 700, color: "#38bdf8" }}>
                    수집 대상 종목:
                  </label>
                  <select
                    value={selectedCollectTicker}
                    onChange={(e) => setSelectedCollectTicker(e.target.value)}
                    style={{
                      background: "#0f172a",
                      color: "#fff",
                      border: "1px solid #334155",
                      borderRadius: "6px",
                      padding: "8px 16px",
                      fontSize: "14px",
                    }}
                  >
                    {assets.map((a) => (
                      <option key={a.ticker} value={a.ticker}>
                        {a.name} ({a.ticker}) - {a.market}{a.trading_halted ? " ⛔ 거래중지" : ""}
                      </option>
                    ))}
                  </select>
                </div>

                {/* 거래중지 종목 선택 시 경고 배너 */}
                {assets.find((a) => a.ticker === selectedCollectTicker)?.trading_halted && (
                  <div style={{
                    background: "#450a0a",
                    border: "1px solid #dc2626",
                    borderRadius: "6px",
                    padding: "10px 14px",
                    marginBottom: "16px",
                    color: "#fca5a5",
                    fontSize: "13px",
                    display: "flex",
                    alignItems: "center",
                    gap: "8px",
                  }}>
                    ⛔ <strong>{assets.find((a) => a.ticker === selectedCollectTicker)?.name}</strong>은(는) 거래중지 상태입니다. 시세 수집 및 리포트 생성을 진행할 수 없습니다.
                  </div>
                )}

                <div style={{ display: "grid", gridTemplateColumns: "repeat(5, 1fr)", gap: "12px" }}>
                  <button
                    type="button"
                    disabled={!!collectingStep || !!assets.find((a) => a.ticker === selectedCollectTicker)?.trading_halted}
                    onClick={() => runCollect("/api/collect/price", { ticker: selectedCollectTicker, range: "5y" }, "1단계: 시세 수집")}
                    style={{
                      background: "#1e40af",
                      color: "#fff",
                      border: "none",
                      borderRadius: "6px",
                      padding: "10px",
                      cursor: collectingStep ? "not-allowed" : "pointer",
                      textAlign: "left",
                    }}
                  >
                    <div style={{ fontWeight: 700, fontSize: "14px" }}>📈 1단계: 시세 수집</div>
                    <div style={{ fontSize: "11px", color: "#bfdbfe", marginTop: "4px" }}>
                      Yahoo/KRX 5년 일봉 가격
                    </div>
                  </button>

                  <button
                    type="button"
                    disabled={!!collectingStep || !!assets.find((a) => a.ticker === selectedCollectTicker)?.trading_halted}
                    onClick={() => runCollect("/api/collect/fundamentals", { ticker: selectedCollectTicker }, "2단계: 실적/공시 수집")}
                    style={{
                      background: "#0e7490",
                      color: "#fff",
                      border: "none",
                      borderRadius: "6px",
                      padding: "10px",
                      cursor: (collectingStep || assets.find((a) => a.ticker === selectedCollectTicker)?.trading_halted) ? "not-allowed" : "pointer",
                      textAlign: "left",
                    }}
                  >
                    <div style={{ fontWeight: 700, fontSize: "14px" }}>📑 2단계: 실적/공시 수집</div>
                    <div style={{ fontSize: "11px", color: "#a5f3fc", marginTop: "4px" }}>
                      DART / EDGAR / 컨센서스
                    </div>
                  </button>

                  <button
                    type="button"
                    disabled={!!collectingStep || !!assets.find((a) => a.ticker === selectedCollectTicker)?.trading_halted}
                    onClick={() => runCollect("/api/collect/build", { ticker: selectedCollectTicker, select: true }, "3단계: 리포트 생성")}
                    style={{
                      background: "#047857",
                      color: "#fff",
                      border: "none",
                      borderRadius: "6px",
                      padding: "10px",
                      cursor: (collectingStep || assets.find((a) => a.ticker === selectedCollectTicker)?.trading_halted) ? "not-allowed" : "pointer",
                      textAlign: "left",
                    }}
                  >
                    <div style={{ fontWeight: 700, fontSize: "14px" }}>📊 3단계: 리포트 생성</div>
                    <div style={{ fontSize: "11px", color: "#a7f3d0", marginTop: "4px" }}>
                      지표 계산 및 대시보드 반영
                    </div>
                  </button>

                  <button
                    type="button"
                    disabled={!!collectingStep || !!assets.find((a) => a.ticker === selectedCollectTicker)?.trading_halted}
                    onClick={() => runCollect("/api/collect/ai-analyze", { ticker: selectedCollectTicker }, "4단계: Bob AI 투자 판단 생성")}
                    style={{
                      background: "#d97706",
                      color: "#fff",
                      border: "none",
                      borderRadius: "6px",
                      padding: "10px",
                      cursor: (collectingStep || assets.find((a) => a.ticker === selectedCollectTicker)?.trading_halted) ? "not-allowed" : "pointer",
                      textAlign: "left",
                    }}
                  >
                    <div style={{ fontWeight: 700, fontSize: "14px" }}>🤖 4단계: Bob AI 투자 판단</div>
                    <div style={{ fontSize: "11px", color: "#fef3c7", marginTop: "4px" }}>
                      Bob Shell 정밀 분석 & 내러티브
                    </div>
                  </button>

                  <button
                    type="button"
                    disabled={!!collectingStep || !!assets.find((a) => a.ticker === selectedCollectTicker)?.trading_halted}
                    onClick={() => setShowRunAllConfirm(true)}
                    style={{
                      background: "#7c3aed",
                      color: "#fff",
                      border: "none",
                      borderRadius: "6px",
                      padding: "10px",
                      cursor: (collectingStep || assets.find((a) => a.ticker === selectedCollectTicker)?.trading_halted) ? "not-allowed" : "pointer",
                      textAlign: "left",
                    }}
                  >
                    <div style={{ fontWeight: 700, fontSize: "14px" }}>⚡ 1~4단계 전체 실행</div>
                    <div style={{ fontSize: "11px", color: "#ddd6fe", marginTop: "4px" }}>
                      원클릭 수집 및 분석
                    </div>
                  </button>
                </div>
              </div>

              {/* 실시간 로그 콘솔 */}
              <div style={{
                background: "#020617",
                border: "1px solid #1e293b",
                borderRadius: "8px",
                padding: "16px",
                flex: 1,
                display: "flex",
                flexDirection: "column",
                minHeight: "360px",
              }}>
                <div style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  marginBottom: "10px",
                  flexShrink: 0,
                }}>
                  <span style={{ fontSize: "13px", color: "#94a3b8", fontWeight: 700 }}>
                    실시간 실행 콘솔 로그 {collectingStep ? `[진행 중: ${collectingStep}]` : ""}
                  </span>
                  <button
                    type="button"
                    onClick={() => setLogs([])}
                    style={{ background: "transparent", color: "#64748b", border: "none", fontSize: "12px", cursor: "pointer" }}
                  >
                    로그 지우기
                  </button>
                </div>
                <pre
                  ref={logContainerRef}
                  style={{
                    margin: 0,
                    flex: 1,
                    minHeight: "260px",
                    overflowY: "auto",
                    fontFamily: "monospace",
                    fontSize: "13px",
                    lineHeight: "1.6",
                    color: "#38bdf8",
                    whiteSpace: "pre-wrap",
                    background: "#050b18",
                    padding: "14px",
                    borderRadius: "6px",
                    border: "1px solid #1e293b",
                  }}
                >
                  {logs.length === 0 ? "실행 대기 중... 위의 수집 버튼을 클릭하면 로그가 표시됩니다." : logs.join("\n")}
                </pre>
              </div>
            </div>
          )}

          {/* 탭 3: 자동 시세 수집 현황 */}
          {activeTab === "auto" && (
            <div style={{ flex: 1, display: "flex", flexDirection: "column", minHeight: 0 }}>
              <div style={{
                background: "#1e293b",
                padding: "20px",
                borderRadius: "8px",
                marginBottom: "16px",
                border: "1px solid #334155",
                flexShrink: 0,
              }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "16px" }}>
                  <div>
                    <h3 style={{ margin: "0 0 6px 0", fontSize: "16px", color: "#38bdf8" }}>
                      오늘 시세 자동 업데이트 기능
                    </h3>
                    <p style={{ margin: 0, fontSize: "13px", color: "#94a3b8" }}>
                      프로그램 실행 시 오늘 날짜의 시세가 최신이 아닌 모든 종목을 자동으로 검사하여 일괄 업데이트합니다.
                    </p>
                  </div>
                  <button
                    type="button"
                    onClick={handleRunAutoUpdate}
                    disabled={!!collectingStep}
                    style={{
                      background: "#f59e0b",
                      color: "#000",
                      border: "none",
                      borderRadius: "6px",
                      padding: "10px 20px",
                      fontWeight: 700,
                      fontSize: "14px",
                      cursor: collectingStep ? "not-allowed" : "pointer",
                    }}
                  >
                    🔄 즉시 업데이트
                  </button>
                </div>

                <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: "10px" }}>
                  {assets.map((a) => {
                    const lc = a.price_info.last_close;
                    const pc = a.price_info.prev_close;
                    const diff = lc != null && pc != null && pc !== 0 ? lc - pc : null;
                    const pct = diff != null && pc != null && pc !== 0 ? (diff / pc) * 100 : null;
                    const isUp = diff != null && diff > 0;
                    const isDown = diff != null && diff < 0;
                    const changeColor = isUp ? "#f87171" : isDown ? "#60a5fa" : "#94a3b8";
                    return (
                      <div key={a.ticker} style={{
                        background: "#0f172a",
                        padding: "12px 16px",
                        borderRadius: "6px",
                        display: "flex",
                        flexDirection: "column",
                        gap: "4px",
                        fontSize: "13px",
                      }}>
                        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
                          <span>
                            <strong>{a.name}</strong> ({a.ticker})
                          </span>
                          <span style={{ fontWeight: 700, color: "#f1f5f9", fontSize: "14px" }}>
                            {lc != null
                              ? a.currency === "KRW"
                                ? `${lc.toLocaleString()} 원`
                                : `$${lc.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
                              : "—"}
                          </span>
                        </div>
                        {pct != null && (
                          <div style={{
                            display: "flex",
                            justifyContent: "flex-end",
                            alignItems: "center",
                            gap: "4px",
                            color: changeColor,
                            fontSize: "12px",
                            fontWeight: 600,
                          }}>
                            <span>{isUp ? "▲" : isDown ? "▼" : "—"}</span>
                            <span>
                              {a.currency === "KRW"
                                ? `${Math.abs(diff!).toLocaleString()} 원`
                                : `$${Math.abs(diff!).toFixed(2)}`}
                              {" "}({isUp ? "+" : isDown ? "-" : ""}{Math.abs(pct).toFixed(2)}%)
                            </span>
                          </div>
                        )}
                        <span style={{
                          color: a.price_info.exists ? "#34d399" : "#f87171",
                          fontWeight: 500,
                          fontSize: "11px",
                          textAlign: "right",
                          display: "block",
                        }}>
                          {a.price_info.exists
                            ? `조회 ${a.price_info.retrieved_at ? formatDateTime(a.price_info.retrieved_at) : a.price_info.last_date}`
                            : "시세 없음"}
                        </span>
                      </div>
                    );
                  })}
                </div>
              </div>

              {/* 실시간 로그 콘솔 */}
              <div style={{
                background: "#020617",
                border: "1px solid #1e293b",
                borderRadius: "8px",
                padding: "16px",
                flex: 1,
                display: "flex",
                flexDirection: "column",
                minHeight: "260px",
              }}>
                <div style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  marginBottom: "10px",
                  flexShrink: 0,
                }}>
                  <span style={{ fontSize: "13px", color: "#94a3b8", fontWeight: 700 }}>
                    실시간 실행 콘솔 로그 {collectingStep ? `[진행 중: ${collectingStep}]` : ""}
                  </span>
                  <button
                    type="button"
                    onClick={() => setAutoLogs([])}
                    style={{ background: "transparent", color: "#64748b", border: "none", fontSize: "12px", cursor: "pointer" }}
                  >
                    로그 지우기
                  </button>
                </div>
                <pre
                  ref={autoLogContainerRef}
                  style={{
                    margin: 0,
                    flex: 1,
                    minHeight: "160px",
                    overflowY: "auto",
                    fontFamily: "monospace",
                    fontSize: "13px",
                    lineHeight: "1.6",
                    color: "#38bdf8",
                    whiteSpace: "pre-wrap",
                    background: "#050b18",
                    padding: "14px",
                    borderRadius: "6px",
                    border: "1px solid #1e293b",
                  }}
                >
                  {autoLogs.length === 0 ? "실행 대기 중... 위의 업데이트 버튼을 클릭하면 로그가 표시됩니다." : autoLogs.join("\n")}
                </pre>
              </div>
            </div>
          )}

          {/* 탭 4: 오늘의 추천 — 기술지표 기반 흐름 예측 */}
          {activeTab === "forecast" && (
            <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: "20px" }}>

              {/* 컨트롤 패널 */}
              <div style={{
                background: "#1e293b",
                border: "1px solid #334155",
                borderRadius: "10px",
                padding: "20px 24px",
              }}>
                <div style={{ display: "flex", alignItems: "center", gap: "16px", flexWrap: "wrap" }}>
                  <div style={{ fontSize: "15px", fontWeight: 700, color: "#f59e0b", whiteSpace: "nowrap" }}>
                    📊 알고리즘 선택
                  </div>
                  <select
                    value={forecastAlgorithm}
                    onChange={(e) => setForecastAlgorithm(e.target.value)}
                    disabled={forecastRunning}
                    style={{
                      background: "#0f172a",
                      color: "#f8fafc",
                      border: "1px solid #475569",
                      borderRadius: "6px",
                      padding: "9px 16px",
                      fontSize: "14px",
                      fontWeight: 600,
                      minWidth: "280px",
                      cursor: forecastRunning ? "not-allowed" : "pointer",
                    }}
                  >
                    {ALGO_OPTIONS.map((o) => (
                      <option key={o.value} value={o.value}>{o.label}</option>
                    ))}
                  </select>
                  <button
                    type="button"
                    onClick={handleRunForecast}
                    disabled={forecastRunning}
                    style={{
                      background: forecastRunning ? "#78350f" : "#f59e0b",
                      color: forecastRunning ? "#fcd34d" : "#000",
                      border: "none",
                      borderRadius: "7px",
                      padding: "10px 24px",
                      fontWeight: 700,
                      fontSize: "14px",
                      cursor: forecastRunning ? "not-allowed" : "pointer",
                      display: "flex",
                      alignItems: "center",
                      gap: "8px",
                      whiteSpace: "nowrap",
                    }}
                  >
                    {forecastRunning ? (
                      <>
                        <span style={{ display: "inline-block", animation: "spin 1s linear infinite" }}>⟳</span>
                        분석 중...
                      </>
                    ) : "🔍 분석 실행"}
                  </button>
                  <div style={{ marginLeft: "auto", fontSize: "12px", color: "#64748b" }}>
                    등록된 {assets.filter(a => !a.trading_halted).length}개 종목 대상 · 일봉 기반
                  </div>
                </div>
              </div>

              {/* 메인 영역: 리포트 목록(왼쪽) + 결과 카드(오른쪽) */}
              <div style={{ display: "flex", gap: "20px", flex: 1, minHeight: 0 }}>

                {/* 왼쪽: 저장된 리포트 목록 */}
                <div style={{
                  width: "260px",
                  flexShrink: 0,
                  background: "#1e293b",
                  border: "1px solid #334155",
                  borderRadius: "10px",
                  overflow: "hidden",
                  display: "flex",
                  flexDirection: "column",
                }}>
                  <div style={{
                    padding: "14px 16px",
                    background: "#0f172a",
                    borderBottom: "1px solid #334155",
                    display: "flex",
                    justifyContent: "space-between",
                    alignItems: "center",
                  }}>
                    <span style={{ fontSize: "13px", fontWeight: 700, color: "#94a3b8" }}>
                      저장된 리포트
                    </span>
                    <span style={{ fontSize: "11px", color: "#475569" }}>최근 7일 (일별 1건)</span>
                  </div>
                  <div style={{ overflowY: "auto", flex: 1 }}>
                    {forecastListLoading ? (
                      <div style={{ padding: "20px", fontSize: "13px", color: "#475569", textAlign: "center" }}>
                        불러오는 중...
                      </div>
                    ) : forecastList.length === 0 ? (
                      <div style={{ padding: "20px", fontSize: "12px", color: "#475569", textAlign: "center", lineHeight: 1.6 }}>
                        최근 7일간 저장된 리포트가<br />없습니다.<br />위에서 분석을 실행하세요.
                      </div>
                    ) : forecastList.map((f) => (
                      <button
                        key={f.id}
                        type="button"
                        onClick={() => handleLoadForecast(f.id)}
                        style={{
                          width: "100%",
                          background: forecastSelectedId === f.id ? "rgba(245,158,11,0.15)" : "transparent",
                          border: "none",
                          borderBottom: "1px solid #334155",
                          borderLeft: forecastSelectedId === f.id ? "3px solid #f59e0b" : "3px solid transparent",
                          padding: "12px 14px",
                          textAlign: "left",
                          cursor: "pointer",
                          transition: "background 0.15s",
                        }}
                      >
                        <div style={{ fontSize: "12px", fontWeight: 700, color: forecastSelectedId === f.id ? "#f59e0b" : "#e2e8f0", marginBottom: "3px" }}>
                          {f.algorithm_label.split(" (")[0]}
                        </div>
                        <div style={{ fontSize: "11px", color: "#64748b" }}>
                          {formatDateTime(f.created_at)}
                        </div>
                        <div style={{ fontSize: "10px", color: "#475569", marginTop: "2px" }}>
                          {f.asset_count}개 종목
                        </div>
                      </button>
                    ))}
                  </div>
                </div>

                {/* 오른쪽: 분석 결과 카드들 */}
                <div style={{ flex: 1, overflowY: "auto", display: "flex", flexDirection: "column", gap: "14px" }}>
                  {forecastRunning && !forecastReport && (
                    <div style={{
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                      flex: 1,
                      color: "#f59e0b",
                      fontSize: "15px",
                      fontWeight: 600,
                      gap: "12px",
                    }}>
                      <span style={{ fontSize: "24px", display: "inline-block", animation: "spin 1s linear infinite" }}>⟳</span>
                      기술지표 계산 중...
                    </div>
                  )}
                  {!forecastRunning && !forecastReport && (
                    <div style={{
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                      flex: 1,
                      color: "#475569",
                      fontSize: "14px",
                      textAlign: "center",
                      lineHeight: 2,
                    }}>
                      알고리즘을 선택하고<br />
                      <strong style={{ color: "#f59e0b" }}>분석 실행</strong> 버튼을 누르거나<br />
                      왼쪽에서 저장된 리포트를 선택하세요.
                    </div>
                  )}
                  {/* 자동 Bob 분석 진행 상태 배너 */}
                  {forecastReport && autoInterpretRunning && (
                    <div style={{
                      background: "#1e2d40",
                      border: "1px solid #1e3a5f",
                      borderRadius: "8px",
                      padding: "10px 16px",
                      display: "flex",
                      alignItems: "center",
                      gap: "10px",
                      fontSize: "13px",
                      color: "#7dd3fc",
                    }}>
                      <span style={{ animation: "spin 1s linear infinite", display: "inline-block", fontSize: "18px" }}>⟳</span>
                      <span>
                        🤖 Bob AI 자동 분석 진행 중 —{" "}
                        <strong style={{ color: "#f8fafc" }}>
                          {forecastReport.results[autoInterpretIdx ?? 0]?.name ?? ""}
                        </strong>{" "}
                        ({(autoInterpretIdx ?? 0) + 1} / {forecastReport.results.length})
                      </span>
                    </div>
                  )}
                  {forecastReport && forecastReport.results.map((r, idx) => (
                    <ForecastCard
                      key={`${forecastReport.id}-${r.ticker}`}
                      result={r}
                      algorithm={forecastReport.algorithm}
                      createdAt={forecastReport.created_at}
                      reportId={forecastReport.id}
                      autoTrigger={autoTriggers[idx]}
                      onAutoComplete={() => handleCardAutoComplete(idx, forecastReport.results.length)}
                    />
                  ))}
                </div>
              </div>
            </div>
          )}

          {/* 탭 5: 장기 분석 📈 */}
          {activeTab === "longterm" && (
            <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: "20px" }}>
              <div style={{
                background: "#1e293b",
                border: "1px solid #334155",
                borderRadius: "10px",
                padding: "20px 24px",
              }}>
                <h3 style={{ fontSize: "16px", fontWeight: 700, color: "#10b981", margin: "0 0 6px 0" }}>
                  장기 내재가치 및 시나리오 밴드 통합 대조
                </h3>
                <p style={{ fontSize: "13px", color: "#94a3b8", margin: 0, lineHeight: 1.6 }}>
                  각 종목의 역DCF(할인현금흐름) 계산 결과를 전사적으로 대조하여 가치 대비 가장 안전마진이 높은 종목을 한눈에 찾아냅니다.
                </p>
              </div>

              {longtermLoading ? (
                <div style={{ padding: "40px", textAlign: "center", color: "#94a3b8", background: "#1e293b", borderRadius: "10px" }}>
                  <span style={{ display: "inline-block", animation: "spin 1s linear infinite", fontSize: "24px", marginRight: "10px" }}>⟳</span>
                  내재가치 데이터 가공 중...
                </div>
              ) : longtermResults.length === 0 ? (
                <div style={{ padding: "40px", textAlign: "center", color: "#64748b", background: "#1e293b", borderRadius: "10px" }}>
                  등록된 종목이 없습니다.
                </div>
              ) : (
                <div style={{
                  background: "#1e293b",
                  border: "1px solid #334155",
                  borderRadius: "10px",
                  overflow: "hidden"
                }}>
                  <table style={{ width: "100%", borderCollapse: "collapse", textAlign: "left", fontSize: "13px" }}>
                    <thead>
                      <tr style={{ background: "#0f172a", borderBottom: "1px solid #334155", color: "#94a3b8" }}>
                        <th style={{ padding: "14px 16px" }}>종목명</th>
                        <th style={{ padding: "14px 16px" }}>자산유형</th>
                        <th style={{ padding: "14px 16px" }}>현재가</th>
                        <th style={{ padding: "14px 16px", color: "#ef4444" }}>Bear 가치</th>
                        <th style={{ padding: "14px 16px", color: "#38bdf8" }}>Base 가치</th>
                        <th style={{ padding: "14px 16px", color: "#10b981" }}>Bull 가치</th>
                        <th style={{ padding: "14px 16px", textAlign: "right" }}>안전마진 (Margin of Safety)</th>
                        <th style={{ padding: "14px 16px", textAlign: "right" }}>기대수익률 (Base)</th>
                        <th style={{ padding: "14px 16px", textAlign: "center" }}>계산서 상태</th>
                      </tr>
                    </thead>
                    <tbody>
                      {longtermResults.map((item) => {
                        const isEtf = item.asset_type === "ETF";
                        const hasCalc = item.has_calc;
                        const isNegativeFcf = item.calc_model === "negative_fcf";
                        const mos = item.safety_margin_pct;
                        const returnPct = item.expected_return_pct;

                        return (
                          <tr key={item.ticker} style={{ borderBottom: "1px solid #1e293b", color: "#f8fafc" }}>
                            <td style={{ padding: "14px 16px" }}>
                              <strong style={{ fontSize: "14px" }}>{item.name}</strong>{" "}
                              <span style={{ fontSize: "11px", color: "#64748b" }}>{item.ticker} · {item.market}</span>
                            </td>
                            <td style={{ padding: "14px 16px" }}>
                              <span style={{
                                padding: "2px 6px",
                                borderRadius: "4px",
                                fontSize: "11px",
                                background: isEtf ? "rgba(129,140,248,0.15)" : "rgba(16,185,129,0.15)",
                                color: isEtf ? "#818cf8" : "#10b981"
                              }}>
                                {item.asset_type}
                              </span>
                            </td>
                            <td style={{ padding: "14px 16px", fontWeight: 600 }}>
                              {item.current_price > 0 ? (
                                item.market === "KR" ? `${item.current_price.toLocaleString()} 원` : `$${item.current_price.toLocaleString()}`
                              ) : "시세 없음"}
                            </td>
                            <td style={{ padding: "14px 16px", color: "#f87171" }}>
                              {hasCalc && !isNegativeFcf && item.bear_value != null ? (
                                item.market === "KR" ? `${Math.round(item.bear_value).toLocaleString()} 원` : `$${item.bear_value.toLocaleString()}`
                              ) : "—"}
                            </td>
                            <td style={{ padding: "14px 16px", color: "#7dd3fc", fontWeight: 600 }}>
                              {hasCalc && !isNegativeFcf && item.base_value != null ? (
                                item.market === "KR" ? `${Math.round(item.base_value).toLocaleString()} 원` : `$${item.base_value.toLocaleString()}`
                              ) : "—"}
                            </td>
                            <td style={{ padding: "14px 16px", color: "#34d399" }}>
                              {hasCalc && !isNegativeFcf && item.bull_value != null ? (
                                item.market === "KR" ? `${Math.round(item.bull_value).toLocaleString()} 원` : `$${item.bull_value.toLocaleString()}`
                              ) : "—"}
                            </td>
                            <td style={{
                              padding: "14px 16px",
                              textAlign: "right",
                              fontWeight: 700,
                              color: mos != null && !isNegativeFcf ? (mos >= 0 ? "#34d399" : "#f87171") : "#64748b"
                            }}>
                              {mos != null && !isNegativeFcf ? `${mos > 0 ? "+" : ""}${mos.toFixed(1)}%` : "계산 필요"}
                            </td>
                            <td style={{
                              padding: "14px 16px",
                              textAlign: "right",
                              fontWeight: 700,
                              color: returnPct != null && !isNegativeFcf ? (returnPct >= 0 ? "#38bdf8" : "#f87171") : "#64748b"
                            }}>
                              {returnPct != null && !isNegativeFcf ? `${returnPct > 0 ? "+" : ""}${returnPct.toFixed(1)}%` : "—"}
                            </td>
                            <td style={{ padding: "14px 16px", textAlign: "center" }}>
                              {isEtf ? (
                                <span style={{ color: "#64748b", fontSize: "11px" }}>가치평가 대상제외</span>
                              ) : isNegativeFcf ? (
                                <span style={{ color: "#f59e0b", fontSize: "12px", background: "rgba(245,158,11,0.1)", padding: "2px 8px", borderRadius: "10px" }}>
                                  ⚠ 음수 FCF
                                </span>
                              ) : hasCalc ? (
                                <span style={{ color: "#34d399", fontSize: "12px", background: "rgba(52,211,153,0.1)", padding: "2px 8px", borderRadius: "10px" }}>
                                  ✓ 계산 완료
                                </span>
                              ) : (
                                <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: "4px" }}>
                                  <span style={{ color: "#f87171", fontSize: "12px", background: "rgba(248,113,113,0.1)", padding: "2px 8px", borderRadius: "10px" }}>
                                    ⚠ 계산 미진행
                                  </span>
                                  <button
                                    onClick={async () => {
                                      await runCollect("/api/collect/all", { ticker: item.ticker }, `${item.name} 전체 수집`);
                                      fetchLongtermData();
                                    }}
                                    style={{
                                      fontSize: "11px",
                                      padding: "2px 8px",
                                      background: "rgba(56,189,248,0.15)",
                                      color: "#38bdf8",
                                      border: "1px solid rgba(56,189,248,0.3)",
                                      borderRadius: "6px",
                                      cursor: "pointer",
                                    }}
                                  >
                                    ↺ 수집 실행
                                  </button>
                                </div>
                              )}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}

          {/* 탭 6: 포트폴리오 추천 💼 */}
          {activeTab === "portfolio" && (
            <div style={{ flex: 1, display: "flex", flexDirection: "column", gap: "20px" }}>

              {/* ── 투자자 정책(IPS) 설정 패널 ── */}
              <div style={{ background: "#1e293b", border: "1px solid #334155", borderRadius: "10px", overflow: "hidden" }}>
                <button
                  onClick={() => setPolicyPanelOpen(v => !v)}
                  style={{
                    width: "100%", display: "flex", justifyContent: "space-between", alignItems: "center",
                    padding: "16px 20px", background: "transparent", border: "none", cursor: "pointer",
                    color: "#f8fafc", fontSize: "14px", fontWeight: 700,
                  }}
                >
                  <span>⚙️ 투자자 정책(IPS) 설정</span>
                  <span style={{ fontSize: "11px", color: "#94a3b8" }}>{policyPanelOpen ? "▲ 접기" : "▼ 펼치기"}</span>
                </button>
                {policyPanelOpen && policyEdit && (
                  <div style={{ padding: "0 20px 20px", display: "flex", flexDirection: "column", gap: "20px" }}>
                    <div style={{ height: "1px", background: "#334155", marginBottom: "4px" }} />

                    {/* 총자산 / 예수금 */}
                    {(() => {
                      const stockValue = portfolioData?.allocation_status?.stock_value_krw ?? 0;
                      const cashAuto = policyEdit.total_assets_krw > 0;
                      const cashShown = cashAuto
                        ? Math.max(0, policyEdit.total_assets_krw - stockValue)
                        : policyEdit.cash_krw;
                      return (
                    <div>
                      <div style={{ fontSize: "12px", color: "#818cf8", fontWeight: 700, marginBottom: "12px", letterSpacing: "0.05em" }}>
                        총 자산 규모
                        <span style={{ marginLeft: "10px", fontSize: "11px", color: "#94a3b8", fontWeight: 400 }}>
                          매수 종목 평가액 {Math.round(stockValue).toLocaleString()}원 (내 종목 수량×현재가 자동 합산)
                        </span>
                      </div>
                      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "12px" }}>
                        <div>
                          <label style={{ fontSize: "11px", color: "#94a3b8", display: "block", marginBottom: "4px" }}>
                            총 자산 (원) <span style={{ color: "#475569" }}>— 주식 평가액 + 현금·예수금 전체. 입력하면 현금·예수금 자동계산, 0이면 예수금 직접 입력</span>
                          </label>
                          <input
                            type="number" min="0" step="1000000"
                            value={policyEdit.total_assets_krw}
                            onChange={e => setPolicyEdit(p => p ? { ...p, total_assets_krw: Number(e.target.value) } : p)}
                            style={{ width: "100%", padding: "8px 10px", background: "#0f172a", border: "1px solid #334155", borderRadius: "6px", color: "#f8fafc", fontSize: "13px" }}
                          />
                        </div>
                        <div>
                          <label style={{ fontSize: "11px", color: "#94a3b8", display: "block", marginBottom: "4px" }}>
                            현금·예수금 (원) {cashAuto && <span style={{ color: "#475569" }}>— 총 자산 − 매수 종목 평가액 (자동)</span>}
                          </label>
                          <input
                            type="number" min="0" step="100000"
                            readOnly={cashAuto}
                            value={Math.round(cashShown)}
                            onChange={e => setPolicyEdit(p => p ? { ...p, cash_krw: Number(e.target.value) } : p)}
                            style={cashAuto
                              ? { width: "100%", padding: "8px 10px", background: "#1e293b", border: "1px solid #1e293b", borderRadius: "6px", color: "#64748b", fontSize: "13px", cursor: "not-allowed" }
                              : { width: "100%", padding: "8px 10px", background: "#0f172a", border: "1px solid #334155", borderRadius: "6px", color: "#f8fafc", fontSize: "13px" }}
                          />
                        </div>
                      </div>
                    </div>
                      );
                    })()}

                    {/* 자산배분 목표 */}
                    <div>
                      <div style={{ fontSize: "12px", color: "#818cf8", fontWeight: 700, marginBottom: "12px", letterSpacing: "0.05em" }}>
                        자산배분 목표 비율
                      </div>
                      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "12px" }}>
                        <div>
                          <label style={{ fontSize: "11px", color: "#94a3b8", display: "block", marginBottom: "4px" }}>
                            안전자산 목표 (%) — 현금·채권
                          </label>
                          <input
                            type="number" min="0" max="100" step="5"
                            value={policyEdit.allocation.safe_asset_target_pct}
                            onChange={e => {
                              const safe = Math.min(100, Math.max(0, Number(e.target.value)));
                              setPolicyEdit(p => p ? { ...p, allocation: { safe_asset_target_pct: safe, risk_asset_target_pct: 100 - safe } } : p);
                            }}
                            style={{ width: "100%", padding: "8px 10px", background: "#0f172a", border: "1px solid #334155", borderRadius: "6px", color: "#f8fafc", fontSize: "13px" }}
                          />
                        </div>
                        <div>
                          <label style={{ fontSize: "11px", color: "#94a3b8", display: "block", marginBottom: "4px" }}>
                            위험자산 목표 (%) — 주식·ETF (자동계산)
                          </label>
                          <input
                            type="number" readOnly
                            value={policyEdit.allocation.risk_asset_target_pct}
                            style={{ width: "100%", padding: "8px 10px", background: "#1e293b", border: "1px solid #1e293b", borderRadius: "6px", color: "#64748b", fontSize: "13px", cursor: "not-allowed" }}
                          />
                        </div>
                      </div>
                    </div>

                    {/* 집중도 한도 */}
                    <div>
                      <div style={{ fontSize: "12px", color: "#818cf8", fontWeight: 700, marginBottom: "12px", letterSpacing: "0.05em" }}>
                        집중도 한도
                      </div>
                      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: "12px" }}>
                        <div>
                          <label style={{ fontSize: "11px", color: "#94a3b8", display: "block", marginBottom: "4px" }}>단일 종목 최대 (%)</label>
                          <input
                            type="number" min="1" max="100" step="1"
                            value={policyEdit.concentration.max_single_position_pct_of_total}
                            onChange={e => setPolicyEdit(p => p ? { ...p, concentration: { ...p.concentration, max_single_position_pct_of_total: Number(e.target.value) } } : p)}
                            style={{ width: "100%", padding: "8px 10px", background: "#0f172a", border: "1px solid #334155", borderRadius: "6px", color: "#f8fafc", fontSize: "13px" }}
                          />
                        </div>
                        <div>
                          <label style={{ fontSize: "11px", color: "#94a3b8", display: "block", marginBottom: "4px" }}>섹터 최대 (%)</label>
                          <input
                            type="number" min="1" max="100" step="1"
                            value={policyEdit.concentration.max_sector_pct_of_total}
                            onChange={e => setPolicyEdit(p => p ? { ...p, concentration: { ...p.concentration, max_sector_pct_of_total: Number(e.target.value) } } : p)}
                            style={{ width: "100%", padding: "8px 10px", background: "#0f172a", border: "1px solid #334155", borderRadius: "6px", color: "#f8fafc", fontSize: "13px" }}
                          />
                        </div>
                        <div>
                          <label style={{ fontSize: "11px", color: "#94a3b8", display: "block", marginBottom: "4px" }}>리밸런싱 허용 오차 (%p)</label>
                          <input
                            type="number" min="0" max="20" step="1"
                            value={policyEdit.rebalance.tolerance_pct}
                            onChange={e => setPolicyEdit(p => p ? { ...p, rebalance: { tolerance_pct: Number(e.target.value) } } : p)}
                            style={{ width: "100%", padding: "8px 10px", background: "#0f172a", border: "1px solid #334155", borderRadius: "6px", color: "#f8fafc", fontSize: "13px" }}
                          />
                        </div>
                      </div>
                    </div>

                    <button
                      onClick={savePolicy}
                      disabled={policySaving}
                      style={{
                        alignSelf: "flex-end", padding: "10px 28px", background: policySaving ? "#4338ca" : "#818cf8",
                        color: "#fff", border: "none", borderRadius: "6px", fontWeight: 700, fontSize: "13px",
                        cursor: policySaving ? "not-allowed" : "pointer",
                      }}
                    >
                      {policySaving ? "저장 중..." : "✓ 정책 저장 및 재계산"}
                    </button>
                  </div>
                )}
              </div>

              {portfolioLoading ? (
                <div style={{ padding: "40px", textAlign: "center", color: "#94a3b8", background: "#1e293b", borderRadius: "10px" }}>
                  <span style={{ display: "inline-block", animation: "spin 1s linear infinite", fontSize: "24px", marginRight: "10px" }}>⟳</span>
                  실보유 자산 원장 및 관통(Look-through) 분석 연산 중...
                </div>
              ) : !portfolioData || portfolioData.asset_count === 0 ? (
                <div style={{ padding: "40px", textAlign: "center", color: "#64748b", background: "#1e293b", borderRadius: "10px", lineHeight: 1.8 }}>
                  현재 보유 주식이 등록되어 있지 않습니다.<br />
                  <strong style={{ color: "#38bdf8" }}>[2. 내 종목]</strong> 탭에서 보유 수량(qty)과 시세 데이터를 입력해 주세요.<br />
                  <span style={{ fontSize: "12px", color: "#475569" }}>(수량이 존재하는 자산들만 자동으로 취합되어 실시간 포트폴리오가 구성됩니다)</span>
                </div>
              ) : (
                <div style={{ display: "flex", flexDirection: "column", gap: "20px" }}>

                  {/* ── 자산배분 현황 & 리밸런싱 가이드 ── */}
                  {portfolioData.allocation_status && (() => {
                    const as = portfolioData.allocation_status!;
                    const fmtKrw = (v: number) => v >= 100000000
                      ? `${(v / 100000000).toFixed(1)}억`
                      : v >= 10000 ? `${(v / 10000).toFixed(0)}만` : `${v.toLocaleString()}`;
                    const gapColor = (gap: number) => Math.abs(gap) <= as.tolerance_pct ? "#34d399" : gap > 0 ? "#f59e0b" : "#f87171";
                    const barSafe = Math.min(100, as.safe_pct);
                    const barRisk = Math.min(100, as.risk_pct);
                    return (
                      <div style={{ background: "#1e293b", border: "1px solid #334155", borderRadius: "10px", padding: "20px 24px" }}>
                        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: "16px" }}>
                          <h4 style={{ fontSize: "14px", fontWeight: 700, color: "#f8fafc", margin: 0 }}>📊 자산배분 현황</h4>
                          <span style={{ fontSize: "11px", color: "#64748b" }}>
                            총 자산 {fmtKrw(as.total_assets_krw)}원 (주식 {fmtKrw(as.stock_value_krw)}원 + 예수금 {fmtKrw(as.cash_krw)}원)
                          </span>
                        </div>

                        {/* 배분 바 */}
                        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "16px", marginBottom: "20px" }}>
                          {/* 안전자산 */}
                          <div>
                            <div style={{ display: "flex", justifyContent: "space-between", fontSize: "12px", marginBottom: "6px" }}>
                              <span style={{ color: "#94a3b8", fontWeight: 600 }}>안전자산 (현금·채권)</span>
                              <span style={{ color: gapColor(as.safe_gap_pct), fontWeight: 700 }}>
                                {as.safe_pct.toFixed(1)}%
                                <span style={{ fontSize: "10px", marginLeft: "4px", color: "#64748b" }}>
                                  목표 {as.safe_target_pct}% ({as.safe_gap_pct > 0 ? "+" : ""}{as.safe_gap_pct.toFixed(1)}%p)
                                </span>
                              </span>
                            </div>
                            <div style={{ height: "10px", background: "#0f172a", borderRadius: "5px", overflow: "hidden", position: "relative" }}>
                              <div style={{ width: `${barSafe}%`, height: "100%", background: "#38bdf8", borderRadius: "5px", transition: "width 0.4s" }} />
                              {/* 목표선 */}
                              <div style={{
                                position: "absolute", top: 0, left: `${as.safe_target_pct}%`, width: "2px",
                                height: "100%", background: "#f59e0b", opacity: 0.8
                              }} />
                            </div>
                            <div style={{ fontSize: "11px", color: "#64748b", marginTop: "4px" }}>{fmtKrw(as.safe_value_krw)}원</div>
                          </div>
                          {/* 위험자산 */}
                          <div>
                            <div style={{ display: "flex", justifyContent: "space-between", fontSize: "12px", marginBottom: "6px" }}>
                              <span style={{ color: "#94a3b8", fontWeight: 600 }}>위험자산 (주식·ETF)</span>
                              <span style={{ color: gapColor(as.risk_gap_pct), fontWeight: 700 }}>
                                {as.risk_pct.toFixed(1)}%
                                <span style={{ fontSize: "10px", marginLeft: "4px", color: "#64748b" }}>
                                  목표 {as.risk_target_pct}% ({as.risk_gap_pct > 0 ? "+" : ""}{as.risk_gap_pct.toFixed(1)}%p)
                                </span>
                              </span>
                            </div>
                            <div style={{ height: "10px", background: "#0f172a", borderRadius: "5px", overflow: "hidden", position: "relative" }}>
                              <div style={{ width: `${barRisk}%`, height: "100%", background: "#818cf8", borderRadius: "5px", transition: "width 0.4s" }} />
                              <div style={{
                                position: "absolute", top: 0, left: `${as.risk_target_pct}%`, width: "2px",
                                height: "100%", background: "#f59e0b", opacity: 0.8
                              }} />
                            </div>
                            <div style={{ fontSize: "11px", color: "#64748b", marginTop: "4px" }}>{fmtKrw(as.risk_value_krw)}원</div>
                          </div>
                        </div>

                        {/* 리밸런싱 액션 */}
                        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "12px" }}>
                          {(["safe", "risk"] as const).map(key => {
                            const rb = as.rebalance[key];
                            const isOk = rb.status === "OK";
                            const bg = isOk ? "rgba(52,211,153,0.08)" : "rgba(248,113,113,0.08)";
                            const border = isOk ? "rgba(52,211,153,0.3)" : "rgba(248,113,113,0.3)";
                            const color = isOk ? "#34d399" : rb.status === "OVERWEIGHT" ? "#f59e0b" : "#f87171";
                            return (
                              <div key={key} style={{ background: bg, border: `1px solid ${border}`, borderRadius: "8px", padding: "12px 14px" }}>
                                <div style={{ fontSize: "11px", color: "#64748b", marginBottom: "4px" }}>{key === "safe" ? "안전자산 리밸런싱" : "위험자산 리밸런싱"}</div>
                                <div style={{ fontSize: "12px", color, fontWeight: 600 }}>{rb.action}</div>
                                {rb.amount_krw > 0 && (
                                  <div style={{ fontSize: "13px", fontWeight: 800, color: "#f8fafc", marginTop: "6px" }}>
                                    {fmtKrw(rb.amount_krw)}원 {rb.status === "OVERWEIGHT" ? "매도/축소" : "매수/투입"}
                                  </div>
                                )}
                              </div>
                            );
                          })}
                        </div>

                        <div style={{ fontSize: "10px", color: "#475569", marginTop: "12px" }}>
                          ※ 노란 선(|) = 목표 비율. 허용 오차 ±{as.tolerance_pct}%p 이내는 정상 범위.
                        </div>
                      </div>
                    );
                  })()}

                  {/* 정량 자산 현황 요약 보드 */}
                  <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))", gap: "16px" }}>

                    {/* 평가금액 카드 */}
                    <div style={{ background: "#1e293b", border: "1px solid #334155", borderRadius: "10px", padding: "16px 20px" }}>
                      <div style={{ fontSize: "12px", color: "#94a3b8", fontWeight: 600, marginBottom: "8px" }}>포트폴리오 총 평가금액</div>
                      <div style={{ fontSize: "24px", fontWeight: 800, color: "#38bdf8" }}>
                        {portfolioData.total_value_krw.toLocaleString()} <span style={{ fontSize: "14px", fontWeight: 500 }}>KRW</span>
                      </div>
                      <div style={{ fontSize: "11px", color: "#475569", marginTop: "6px" }}>현금성 및 주식/ETF 실효 자산 합산</div>
                    </div>

                    {/* 보유 종목 카드 */}
                    <div style={{ background: "#1e293b", border: "1px solid #334155", borderRadius: "10px", padding: "16px 20px" }}>
                      <div style={{ fontSize: "12px", color: "#94a3b8", fontWeight: 600, marginBottom: "8px" }}>활성 보유 종목 수</div>
                      <div style={{ fontSize: "24px", fontWeight: 800, color: "#f8fafc" }}>
                        {portfolioData.asset_count} <span style={{ fontSize: "14px", fontWeight: 500, color: "#94a3b8" }}>개 자산</span>
                      </div>
                      <div style={{ fontSize: "11px", color: "#475569", marginTop: "6px" }}>실효 분산 노출 기준: {portfolioData.effective_position_count}개 종목</div>
                    </div>

                    {/* 위험 위반 여부 카드 */}
                    <div style={{ background: "#1e293b", border: "1px solid #334155", borderRadius: "10px", padding: "16px 20px" }}>
                      <div style={{ fontSize: "12px", color: "#94a3b8", fontWeight: 600, marginBottom: "8px" }}>투자자 정책(IPS) 규율 위반 여부</div>
                      <div style={{
                        fontSize: "24px",
                        fontWeight: 800,
                        color: portfolioData.warnings.length > 0 ? "#f87171" : "#34d399"
                      }}>
                        {portfolioData.warnings.length > 0 ? `⚠️ 위반 ${portfolioData.warnings.length}건` : "✓ 정상 (PASS)"}
                      </div>
                      <div style={{ fontSize: "11px", color: "#475569", marginTop: "6px" }}>
                        {portfolioData.is_policy_custom ? "사용자 맞춤 정책 기준" : "기본 포트폴리오 안전선 기준"}
                      </div>
                    </div>

                  </div>

                  {/* 세부 비중 테이블 및 경고 사항 */}
                  <div style={{ display: "grid", gridTemplateColumns: "1.2fr 0.8fr", gap: "20px" }}>
                    
                    {/* 실효 비중 목록 */}
                    <div style={{ background: "#1e293b", border: "1px solid #334155", borderRadius: "10px", padding: "20px" }}>
                      <h4 style={{ fontSize: "14px", fontWeight: 700, color: "#f8fafc", margin: "0 0 14px 0" }}>📊 실효 노출 비중 (Look-through)</h4>
                      <div style={{ display: "flex", flexDirection: "column", gap: "12px" }}>
                        {portfolioData.exposures.map((item: PortfolioExposure) => {
                          const isUnclassified = item.asset_id.startsWith("UNCLASSIFIED::");
                          return (
                            <div key={item.asset_id} style={{ display: "flex", flexDirection: "column", gap: "4px" }}>
                              <div style={{ display: "flex", justifyContent: "space-between", fontSize: "12px", color: "#cbd5e1" }}>
                                <span>
                                  <strong>{isUnclassified ? "※ " : ""}{item.name}</strong>{" "}
                                  <span style={{ fontSize: "10px", color: "#64748b" }}>{item.asset_id}</span>
                                </span>
                                <span style={{ fontWeight: 600 }}>{item.effective_pct.toFixed(2)}% ({item.value_krw.toLocaleString()}원)</span>
                              </div>
                              <div style={{ height: "6px", background: "#0f172a", borderRadius: "3px", overflow: "hidden" }}>
                                <div style={{
                                  width: `${item.effective_pct}%`,
                                  height: "100%",
                                  background: isUnclassified ? "#475569" : "#38bdf8",
                                  borderRadius: "3px"
                                }} />
                              </div>
                            </div>
                          );
                        })}
                      </div>
                    </div>

                    {/* 섹터/통화 편중도 및 경고 리스트 */}
                    <div style={{ display: "flex", flexDirection: "column", gap: "20px" }}>
                      
                      {/* 섹터 비중 노출 */}
                      <div style={{ background: "#1e293b", border: "1px solid #334155", borderRadius: "10px", padding: "16px 20px" }}>
                        <h4 style={{ fontSize: "13px", fontWeight: 700, color: "#f8fafc", margin: "0 0 10px 0" }}>🏢 섹터 포지셔닝 노출</h4>
                        <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
                          {Object.entries(portfolioData.sector_exposure).map(([sec, pct]: [string, number]) => (
                            <div key={sec} style={{ display: "flex", justifyContent: "space-between", fontSize: "12px", color: "#cbd5e1" }}>
                              <span style={{ color: "#94a3b8" }}>{sec}</span>
                              <span style={{ fontWeight: 600 }}>{pct.toFixed(1)}%</span>
                            </div>
                          ))}
                        </div>
                      </div>

                      {/* 통화 비중 노출 */}
                      <div style={{ background: "#1e293b", border: "1px solid #334155", borderRadius: "10px", padding: "16px 20px" }}>
                        <h4 style={{ fontSize: "13px", fontWeight: 700, color: "#f8fafc", margin: "0 0 10px 0" }}>💱 외환(통화) 노출 위험</h4>
                        <div style={{ display: "flex", flexDirection: "column", gap: "8px" }}>
                          {Object.entries(portfolioData.currency_exposure).map(([cur, pct]: [string, number]) => (
                            <div key={cur} style={{ display: "flex", justifyContent: "space-between", fontSize: "12px", color: "#cbd5e1" }}>
                              <span style={{ color: "#94a3b8" }}>{cur} 노출</span>
                              <span style={{ fontWeight: 600 }}>{pct.toFixed(1)}%</span>
                            </div>
                          ))}
                        </div>
                      </div>

                      {/* 규율 경보 목록 */}
                      {portfolioData.warnings.length > 0 && (
                        <div style={{ background: "#2d1b1b", border: "1px solid #7f1d1d", borderRadius: "10px", padding: "16px 20px" }}>
                          <h4 style={{ fontSize: "13px", fontWeight: 700, color: "#fca5a5", margin: "0 0 8px 0" }}>⚠️ 투자 규율 한도 준칙 경보</h4>
                          <div style={{ display: "flex", flexDirection: "column", gap: "6px", fontSize: "12px", color: "#fca5a5" }}>
                            {portfolioData.warnings.map((warn: PortfolioWarning, idx: number) => {
                              if (warn.code === "SINGLE_NAME_CONCENTRATION") {
                                return <div key={idx}>• <strong>{warn.name}</strong> 실효비중 {warn.value_pct.toFixed(1)}% 가 최대 한도 {warn.limit_pct}% 를 초과했습니다.</div>;
                              }
                              if (warn.code === "SECTOR_CONCENTRATION") {
                                return <div key={idx}>• <strong>{warn.sector}</strong> 섹터 비중 {warn.value_pct.toFixed(1)}% 가 최대 한도 {warn.limit_pct}% 를 초과했습니다.</div>;
                              }
                              if (warn.code === "CURRENCY_CONCENTRATION") {
                                return <div key={idx}>• <strong>{warn.currency}</strong> 통화 자산 {warn.value_pct.toFixed(1)}% 가 한도 {warn.limit_pct}% 를 초과했습니다.</div>;
                              }
                              return <div key={idx}>• {warn.code} 경보가 활성화되었습니다. (실효수치: {warn.value_pct}%)</div>;
                            })}
                          </div>
                        </div>
                      )}

                    </div>

                  </div>

                  {/* AI 자문 리포트 생성기 세션 */}
                  <div style={{
                    background: "#0f172a",
                    border: "1px solid #1e293b",
                    borderRadius: "10px",
                    padding: "24px",
                    display: "flex",
                    flexDirection: "column",
                    gap: "16px"
                  }}>
                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                      <div>
                        <h4 style={{ fontSize: "15px", fontWeight: 700, color: "#818cf8", margin: "0 0 4px 0" }}>🤖 Bob AI 포트폴리오 리밸런싱 자문서 생성</h4>
                        <p style={{ fontSize: "12px", color: "#64748b", margin: 0 }}>현재 비중과 보유 종목들의 최신 가치 시나리오를 종합하여 최상의 액션 지침을 받아봅니다.</p>
                      </div>
                      <button
                        type="button"
                        onClick={generatePortfolioRecommend}
                        disabled={portfolioRecommendLoading}
                        style={{
                          background: portfolioRecommendLoading ? "#4338ca" : "#818cf8",
                          color: "#fff",
                          border: "none",
                          borderRadius: "6px",
                          padding: "10px 20px",
                          fontWeight: 700,
                          fontSize: "13px",
                          cursor: portfolioRecommendLoading ? "not-allowed" : "pointer",
                          whiteSpace: "nowrap"
                        }}
                      >
                        {portfolioRecommendLoading ? "🤖 리포트 작성 중..." : "✍️ 자문 리포트 생성"}
                      </button>
                    </div>

                    {portfolioRecommendLoading && (
                      <div style={{
                        padding: "40px",
                        textAlign: "center",
                        background: "#1e293b",
                        border: "1px solid #334155",
                        borderRadius: "8px",
                        color: "#94a3b8"
                      }}>
                        <span style={{ display: "inline-block", animation: "spin 1s linear infinite", fontSize: "24px", marginBottom: "12px" }}>⟳</span>
                        <div style={{ fontSize: "13px", fontWeight: 600 }}>포트폴리오의 외환 노출, 섹터 밸런스 및 보유 종목의 FCF 안전마진을 심층 심사하는 중...</div>
                      </div>
                    )}

                    {portfolioRecommend && (
                      <div style={{
                        background: "#1e293b",
                        border: "1px solid #334155",
                        borderRadius: "8px",
                        padding: "20px 24px",
                        overflowX: "auto",
                        color: "#e2e8f0"
                      }}>
                        <div style={{
                          borderBottom: "1px solid #334155",
                          paddingBottom: "12px",
                          marginBottom: "16px",
                          fontSize: "12px",
                          color: "#94a3b8",
                          display: "flex",
                          justifyContent: "space-between"
                        }}>
                          <span>ISSUED BY: Bob CIO Model</span>
                          <span>{new Date().toLocaleDateString()} 발행</span>
                        </div>
                        <div style={{ fontSize: "14px", lineHeight: "1.7" }}>
                          {/* 마크다운 렌더링 헬퍼 적용 */}
                          {renderMarkdown(portfolioRecommend)}
                        </div>
                      </div>
                    )}

                  </div>

                </div>
              )}
            </div>
          )}

          {/* 탭 7: 감시 알림 */}
          {activeTab === "watch" && <WatchEventsTab onUnreadChange={setWatchUnread} />}

          {/* 탭 8: 설정 (이메일 발송 · 배치 시간 · 투자자 정책) */}
          {activeTab === "settings" && (
            <div>
              <WatchSettingsTab />
              <div style={{ borderTop: "1px solid #334155", margin: "8px 0 20px 0" }} />
              <div style={{ fontSize: "15px", fontWeight: 700, color: "#f8fafc", marginBottom: "12px" }}>투자자 정책</div>
              <InvestorPolicyTab />
            </div>
          )}
        </div>
      )}

      {/* 2. 투자 의사결정 리포트 뷰 (viewMode === "report") */}
      <div style={{ display: viewMode === "report" ? "block" : "none" }}>
        {children}
      </div>
    </div>
  );
}
