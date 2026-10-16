"use client";

import { useCallback, useEffect, useRef, useState } from "react";

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
}: {
  result: ForecastResult;
  algorithm: string;
  createdAt: string;
  reportId: string;
}) {
  const [expanded, setExpanded] = useState(false);
  const [aiLoading, setAiLoading] = useState(false);
  const [aiResult, setAiResult] = useState<AiInterpretation | null>(
    result.ai_interpretation ?? null
  );

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

        <span style={{ color: "#475569", fontSize: "12px", marginLeft: "4px" }}>
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
  const [activeTab, setActiveTab] = useState<"list" | "collect" | "auto" | "forecast">("list");

  // 데이터 상태
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

  const handleRunForecast = async () => {
    setForecastRunning(true);
    setForecastReport(null);
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

  // forecast 탭 진입 시 리포트 목록 자동 로드
  useEffect(() => {
    if (activeTab === "forecast") {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      fetchForecastList();
    }
  }, [activeTab, fetchForecastList]);

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

          // 업데이트할 종목이 있으면 화면 블로킹 없이 백그라운드 자동 갱신
          if (data2.stale_candidates && data2.stale_candidates.length > 0) {
            setAutoUpdating(true);
            try {
              const updateRes = await fetch(`${API_BASE}/api/auto-update`, { method: "POST" });
              if (updateRes.ok && !ignore) {
                // 업데이트 완료 후 종목 목록 및 상태 자동 새로고침
                await fetchAssets();
                const refreshedStatus = await fetch(`${API_BASE}/api/auto-update-status`);
                if (refreshedStatus.ok && !ignore) {
                  setAutoStatus(await refreshedStatus.json());
                }
              }
            } catch {
              // ignore
            } finally {
              if (!ignore) setAutoUpdating(false);
            }
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
    setEditingTicker(item.ticker);
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
      const res = await fetch(`${API_BASE}/api/auto-update`, {
        method: "POST",
      });
      const data = await res.json();
      if (data.logs) {
        data.logs.forEach((l: string) => appendAutoLog(l));
      }
      appendAutoLog(`✓ 자동 업데이트 완료 (대상: ${data.stale_count}개, 성공: ${data.updated_count}개)`);
      await fetchAssets();
      await checkAutoStatus();
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
          onClick={() => setViewMode("stocks")}
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
                1. 내 종목 ({assets.length}개)
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
                2. 종목 분석
              </button>
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
                3. 시세 조회
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
                setShowForm(!showForm);
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

              {/* 종목 등록/수정 폼 */}
              {showForm && (
                <form onSubmit={handleSaveAsset} style={{
                  background: "#1e293b",
                  padding: "20px",
                  borderRadius: "8px",
                  marginBottom: "20px",
                  border: "1px solid #3b82f6",
                }}>
                  <h3 style={{ margin: "0 0 16px 0", fontSize: "16px", color: "#38bdf8" }}>
                    {editingTicker ? `종목 정보 수정 (${formData.ticker})` : "신규 종목 등록"}
                  </h3>
                  <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: "12px" }}>
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
                        style={{ width: "100%", background: "#0f172a", border: "1px solid #334155", color: "#fff", padding: "8px", borderRadius: "4px" }}
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
                        style={{ width: "100%", background: "#0f172a", border: "1px solid #334155", color: "#fff", padding: "8px", borderRadius: "4px" }}
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
                        style={{ width: "100%", background: "#0f172a", border: "1px solid #334155", color: "#fff", padding: "8px", borderRadius: "4px" }}
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
                        style={{ width: "100%", background: "#0f172a", border: "1px solid #334155", color: "#fff", padding: "8px", borderRadius: "4px" }}
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
                        placeholder={formData.market === "KR" ? `${formData.ticker}.KS` : formData.ticker}
                        onChange={(e) => setFormData({ ...formData, price_symbol: e.target.value })}
                        style={{ width: "100%", background: "#0f172a", border: "1px solid #334155", color: "#fff", padding: "8px", borderRadius: "4px" }}
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
                        style={{ width: "100%", background: "#0f172a", border: "1px solid #334155", color: "#fff", padding: "8px", borderRadius: "4px" }}
                      />
                    </div>
                  </div>
                  <div style={{ display: "flex", justifyContent: "flex-end", gap: "8px", marginTop: "16px" }}>
                    <button
                      type="button"
                      onClick={() => setShowForm(false)}
                      style={{ background: "#475569", color: "#fff", border: "none", padding: "8px 16px", borderRadius: "4px", cursor: "pointer" }}
                    >
                      취소
                    </button>
                    <button
                      type="submit"
                      style={{ background: "#2563eb", color: "#fff", border: "none", padding: "8px 16px", borderRadius: "4px", cursor: "pointer", fontWeight: 600 }}
                    >
                      저장하기
                    </button>
                  </div>
                </form>
              )}

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
                      <th style={{ padding: "14px 16px", textAlign: "right" }}>작업</th>
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
                      padding: "14px",
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
                      padding: "14px",
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
                      padding: "14px",
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
                      padding: "14px",
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
                      padding: "14px",
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
                  {forecastReport && forecastReport.results.map((r) => (
                    <ForecastCard key={r.ticker} result={r} algorithm={forecastReport.algorithm} createdAt={forecastReport.created_at} reportId={forecastReport.id} />
                  ))}
                </div>
              </div>
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
