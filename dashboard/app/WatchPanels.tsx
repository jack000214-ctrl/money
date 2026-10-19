"use client";

import { useCallback, useEffect, useState } from "react";
import type { CSSProperties } from "react";

const API_BASE = "http://127.0.0.1:3001";
const UNREAD_POLL_MS = 30000;

// ── 타입 ─────────────────────────────────────────────────────────────────

interface WatchEvent {
  seq: number;
  type: string;
  ticker: string | null;
  name: string | null;
  detected_at: string;
  read: boolean;
  text: { title: string; body: string };
}

interface QueueItem {
  context_id: string;
  ticker: string;
  name: string;
  intent: string;
  status: string;
  created_at: string;
  expires_at: string;
}

interface NotMonitored {
  item: string;
  reason: string;
}

interface LastCycle {
  at: string;
  result: string;
  reason: string | null;
  counts?: { holdings: number; quotes_ok: number; quotes_failed: number; events: number };
  errors?: string[];
}

interface WatchStatus {
  config_errors: string[] | null;
  gate: { decision: string; message: string } | null;
  last_cycle: LastCycle | null;
  unread: number;
  unset_review_fields: string[];
  smtp_password_set: boolean;
  not_monitored: NotMonitored[];
}

interface Recipient {
  name: string;
  address: string;
  enabled: boolean;
}

interface WatchConfig {
  window: { start: string; end: string };
  interval_minutes: number;
  channels: Record<string, boolean>;
  email: {
    smtp_host: string;
    smtp_port: number;
    smtp_user: string;
    smtp_from: string;
    recipients: Recipient[];
  };
  review: Record<string, number | null>;
  briefing: { enabled: boolean; time: string };
}

// ── 표시 상수 ─────────────────────────────────────────────────────────────

const TYPE_LABELS: Record<string, string> = {
  PRICE_TRIGGER: "가격 문턱",
  POLICY_BREACH: "정책 한도",
  STALE_ASSESSMENT: "판단 재검토",
  EARNINGS_NEAR: "실적 임박",
};

const CHANNEL_LABELS: Record<string, string> = {
  console: "콘솔",
  file: "알림 파일",
  dashboard: "대시보드",
  email: "이메일",
};

const REVIEW_LABELS: Record<string, { label: string; unit: string }> = {
  stale_after_days: { label: "판단 노후화 기준", unit: "일" },
  price_drift_pct: { label: "판단 시점 가격 괴리", unit: "%" },
  earnings_within_business_days: { label: "실적발표 임박 기준", unit: "영업일" },
};

const CYCLE_LABELS: Record<string, string> = {
  OK: "정상",
  SKIPPED: "건너뜀",
  ERROR: "오류",
};

const card: CSSProperties = {
  background: "#1e293b",
  border: "1px solid #334155",
  borderRadius: "8px",
  padding: "16px 20px",
  marginBottom: "16px",
  color: "#e2e8f0",
};

const sectionTitle: CSSProperties = {
  fontSize: "15px",
  fontWeight: 700,
  color: "#f8fafc",
  margin: "0 0 12px 0",
};

const inputStyle: CSSProperties = {
  background: "#0b1120",
  color: "#f8fafc",
  border: "1px solid #334155",
  borderRadius: "6px",
  padding: "7px 10px",
  fontSize: "13px",
  boxSizing: "border-box",
};

const labelStyle: CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: "4px",
  fontSize: "12px",
  color: "#94a3b8",
};

const primaryButton: CSSProperties = {
  background: "#0284c7",
  color: "#fff",
  border: "none",
  borderRadius: "6px",
  padding: "8px 16px",
  fontSize: "13px",
  fontWeight: 600,
  cursor: "pointer",
};

const subtleButton: CSSProperties = {
  background: "#334155",
  color: "#e2e8f0",
  border: "none",
  borderRadius: "6px",
  padding: "7px 14px",
  fontSize: "13px",
  cursor: "pointer",
};

function formatKst(iso: string | null | undefined): string {
  if (!iso) return "-";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString("ko-KR", { timeZone: "Asia/Seoul", hour12: false });
}

// ── 미읽음 배지 ───────────────────────────────────────────────────────────

/** API가 꺼져 있어도 예외 없이 0으로 둔다(기존 화면은 영향받지 않는다). */
export function useWatchUnread(): [number, (value: number) => void] {
  const [unread, setUnread] = useState(0);

  useEffect(() => {
    let ignore = false;
    const poll = async () => {
      try {
        const res = await fetch(`${API_BASE}/api/watch/status`);
        if (!res.ok) return;
        const data = await res.json();
        if (!ignore && typeof data.unread === "number") setUnread(data.unread);
      } catch {
        // API 오프라인: 배지는 마지막 값을 유지한다.
      }
    };
    poll();
    const id = setInterval(poll, UNREAD_POLL_MS);
    return () => {
      ignore = true;
      clearInterval(id);
    };
  }, []);

  return [unread, setUnread];
}

export function UnreadBadge({ count }: { count: number }) {
  if (count <= 0) return null;
  return (
    <span
      style={{
        background: "#ef4444",
        color: "#fff",
        borderRadius: "999px",
        padding: "1px 7px",
        fontSize: "11px",
        fontWeight: 700,
        marginLeft: "6px",
      }}
    >
      {count > 99 ? "99+" : count}
    </span>
  );
}

// ── 감시 알림 탭 ──────────────────────────────────────────────────────────

export function WatchEventsTab({ onUnreadChange }: { onUnreadChange: (value: number) => void }) {
  const [events, setEvents] = useState<WatchEvent[]>([]);
  const [queue, setQueue] = useState<QueueItem[]>([]);
  const [status, setStatus] = useState<WatchStatus | null>(null);
  const [loadError, setLoadError] = useState("");
  const [reloadKey, setReloadKey] = useState(0);
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState("");

  useEffect(() => {
    let ignore = false;
    const load = async () => {
      try {
        const [evRes, qRes, stRes] = await Promise.all([
          fetch(`${API_BASE}/api/watch/events?limit=100`),
          fetch(`${API_BASE}/api/watch/queue`),
          fetch(`${API_BASE}/api/watch/status`),
        ]);
        if (!evRes.ok || !qRes.ok || !stRes.ok) throw new Error("bad status");
        const [ev, q, st] = await Promise.all([evRes.json(), qRes.json(), stRes.json()]);
        if (ignore) return;
        // events 는 서버가 이미 최신순으로 준다. 다시 정렬하지 않는다.
        setEvents(ev.events ?? []);
        setQueue(q.items ?? []);
        setStatus(st);
        setLoadError("");
        onUnreadChange(typeof ev.unread === "number" ? ev.unread : 0);
      } catch {
        if (!ignore) setLoadError("API 서버에 연결하지 못했습니다.");
      }
    };
    load();
    return () => {
      ignore = true;
    };
  }, [reloadKey, onUnreadChange]);

  const reload = useCallback(() => setReloadKey((k) => k + 1), []);

  const markAllRead = async () => {
    setBusy("read");
    try {
      const res = await fetch(`${API_BASE}/api/watch/events/read`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
      if (!res.ok) throw new Error("bad status");
      setNotice("");
      reload();
    } catch {
      setNotice("읽음 처리에 실패했습니다.");
    } finally {
      setBusy("");
    }
  };

  const runOnce = async () => {
    setBusy("run");
    setNotice("1회 실행 중입니다. 시세 조회 때문에 수십 초 걸릴 수 있습니다.");
    try {
      const res = await fetch(`${API_BASE}/api/watch/run-once`, { method: "POST" });
      if (!res.ok) throw new Error("bad status");
      const data = await res.json();
      const cycle = data.cycle as LastCycle | undefined;
      setNotice(
        cycle
          ? `실행 결과: ${CYCLE_LABELS[cycle.result] ?? cycle.result}${cycle.reason ? ` (${cycle.reason})` : ""}`
          : "실행했습니다.",
      );
      reload();
    } catch {
      setNotice("1회 실행에 실패했습니다.");
    } finally {
      setBusy("");
    }
  };

  const cycle = status?.last_cycle ?? null;

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "16px" }}>
        <div style={{ fontSize: "13px", color: "#94a3b8", lineHeight: 1.6 }}>
          PC는 문턱에 닿았다는 사실만 알립니다. 매수·매도 판단은 모델이 새로 내립니다.
        </div>
        <div style={{ display: "flex", gap: "8px" }}>
          <button type="button" style={subtleButton} onClick={reload} disabled={busy !== ""}>
            새로고침
          </button>
          <button type="button" style={subtleButton} onClick={markAllRead} disabled={busy !== ""}>
            모두 읽음
          </button>
          <button type="button" style={primaryButton} onClick={runOnce} disabled={busy !== ""}>
            지금 1회 실행
          </button>
        </div>
      </div>

      {loadError && (
        <div style={{ ...card, borderColor: "#ef4444", color: "#fca5a5" }}>{loadError}</div>
      )}
      {notice && <div style={{ ...card, color: "#cbd5e1" }}>{notice}</div>}

      {/* 상태 */}
      <div style={card}>
        <h3 style={sectionTitle}>감시 상태</h3>
        {status ? (
          <div style={{ fontSize: "13px", lineHeight: 1.8, color: "#cbd5e1" }}>
            {status.config_errors && (
              <div style={{ color: "#fca5a5" }}>설정 오류: {status.config_errors.join(" / ")}</div>
            )}
            <div>현재 게이트: {status.gate ? status.gate.message : "-"}</div>
            <div>
              마지막 사이클: {cycle ? formatKst(cycle.at) : "기록 없음"}
              {cycle && ` · ${CYCLE_LABELS[cycle.result] ?? cycle.result}`}
              {cycle?.reason ? ` (${cycle.reason})` : ""}
            </div>
            {cycle?.counts && (
              <div>
                보유 {cycle.counts.holdings}종목 · 시세 성공 {cycle.counts.quotes_ok} / 실패 {cycle.counts.quotes_failed} ·
                새 이벤트 {cycle.counts.events}건
              </div>
            )}
            {cycle?.errors && cycle.errors.length > 0 && (
              <div style={{ color: "#fcd34d" }}>사이클 오류: {cycle.errors.join(" / ")}</div>
            )}
            {status.not_monitored.length > 0 && (
              <div>
                감시하지 않는 항목:
                <ul style={{ margin: "4px 0 0 18px", padding: 0 }}>
                  {status.not_monitored.map((n) => (
                    <li key={`${n.item}|${n.reason}`}>
                      {n.item} — {n.reason}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        ) : (
          <div style={{ fontSize: "13px", color: "#94a3b8" }}>불러오는 중...</div>
        )}
      </div>

      {/* 재질문 대기열 */}
      <div style={card}>
        <h3 style={sectionTitle}>재질문 대기열 ({queue.length})</h3>
        {queue.length === 0 ? (
          <div style={{ fontSize: "13px", color: "#94a3b8" }}>준비된 패킷이 없습니다.</div>
        ) : (
          <table style={{ width: "100%", fontSize: "13px", borderCollapse: "collapse" }}>
            <thead>
              <tr style={{ color: "#94a3b8", textAlign: "left" }}>
                <th style={{ padding: "4px 8px" }}>종목</th>
                <th style={{ padding: "4px 8px" }}>의도</th>
                <th style={{ padding: "4px 8px" }}>상태</th>
                <th style={{ padding: "4px 8px" }}>만료</th>
                <th style={{ padding: "4px 8px" }}>패킷 ID</th>
              </tr>
            </thead>
            <tbody>
              {queue.map((item) => (
                <tr key={item.context_id} style={{ borderTop: "1px solid #334155" }}>
                  <td style={{ padding: "6px 8px" }}>
                    {item.name} ({item.ticker})
                  </td>
                  <td style={{ padding: "6px 8px" }}>{item.intent}</td>
                  <td style={{ padding: "6px 8px" }}>{item.status}</td>
                  <td style={{ padding: "6px 8px" }}>{formatKst(item.expires_at)}</td>
                  <td style={{ padding: "6px 8px", fontFamily: "monospace" }}>{item.context_id}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* 이벤트 목록 (서버 정렬: 최신순) */}
      <div style={card}>
        <h3 style={sectionTitle}>알림 이벤트 ({events.length})</h3>
        {events.length === 0 ? (
          <div style={{ fontSize: "13px", color: "#94a3b8" }}>기록된 이벤트가 없습니다.</div>
        ) : (
          events.map((event) => (
            <div
              key={event.seq}
              style={{
                borderTop: "1px solid #334155",
                padding: "10px 0",
                opacity: event.read ? 0.65 : 1,
              }}
            >
              <div style={{ display: "flex", gap: "8px", alignItems: "center", marginBottom: "4px" }}>
                {!event.read && (
                  <span style={{ background: "#38bdf8", borderRadius: "50%", width: "8px", height: "8px" }} />
                )}
                <span
                  style={{
                    background: "#0b1120",
                    border: "1px solid #334155",
                    borderRadius: "4px",
                    padding: "1px 8px",
                    fontSize: "11px",
                    color: "#94a3b8",
                  }}
                >
                  {TYPE_LABELS[event.type] ?? event.type}
                </span>
                <span style={{ fontSize: "12px", color: "#94a3b8" }}>{formatKst(event.detected_at)}</span>
              </div>
              {/* 서버가 만든 문구를 그대로 표시한다(재구성 금지) */}
              <div style={{ fontSize: "14px", fontWeight: 600, color: "#f8fafc" }}>{event.text.title}</div>
              {event.text.body && (
                <div style={{ fontSize: "13px", color: "#cbd5e1", whiteSpace: "pre-wrap", lineHeight: 1.6 }}>
                  {event.text.body}
                </div>
              )}
            </div>
          ))
        )}
      </div>
    </div>
  );
}

// ── 설정 탭 ───────────────────────────────────────────────────────────────

interface FormState {
  start: string;
  end: string;
  interval: string;
  channels: Record<string, boolean>;
  smtp_host: string;
  smtp_port: string;
  smtp_user: string;
  smtp_from: string;
  recipients: Recipient[];
  review: Record<string, string>;
  briefing_enabled: boolean;
  briefing_time: string;
}

function toForm(config: WatchConfig): FormState {
  const review: Record<string, string> = {};
  for (const key of Object.keys(REVIEW_LABELS)) {
    const value = config.review[key];
    review[key] = value === null || value === undefined ? "" : String(value);
  }
  return {
    start: config.window.start,
    end: config.window.end,
    interval: String(config.interval_minutes),
    channels: { ...config.channels },
    smtp_host: config.email.smtp_host,
    smtp_port: String(config.email.smtp_port),
    smtp_user: config.email.smtp_user,
    smtp_from: config.email.smtp_from,
    recipients: config.email.recipients.map((r) => ({ ...r })),
    review,
    briefing_enabled: config.briefing.enabled,
    briefing_time: config.briefing.time,
  };
}

// 숫자로 읽을 수 없는 문자열은 NaN → 서버 검증이 거부한다(JSON 직렬화 시 null이 되므로 문자열 그대로 보낸다).
function parseNumber(text: string): number | string {
  const trimmed = text.trim();
  if (trimmed === "") return "";
  const value = Number(trimmed);
  return Number.isFinite(value) ? value : trimmed;
}

function toPayload(form: FormState) {
  const review: Record<string, number | string | null> = {};
  for (const [key, text] of Object.entries(form.review)) {
    const parsed = parseNumber(text);
    review[key] = parsed === "" ? null : parsed;
  }
  return {
    window: { start: form.start, end: form.end },
    interval_minutes: parseNumber(form.interval),
    channels: form.channels,
    email: {
      smtp_host: form.smtp_host.trim(),
      smtp_port: parseNumber(form.smtp_port),
      smtp_user: form.smtp_user.trim(),
      smtp_from: form.smtp_from.trim(),
      recipients: form.recipients.map((r) => ({
        name: r.name.trim(),
        address: r.address.trim(),
        enabled: r.enabled,
      })),
    },
    review,
    briefing: { enabled: form.briefing_enabled, time: form.briefing_time },
  };
}

export function WatchSettingsTab() {
  const [form, setForm] = useState<FormState | null>(null);
  const [savedPayload, setSavedPayload] = useState("");
  const [smtpPasswordSet, setSmtpPasswordSet] = useState(false);
  const [configErrors, setConfigErrors] = useState<string[]>([]);
  const [loadError, setLoadError] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveMessage, setSaveMessage] = useState("");
  const [saveErrors, setSaveErrors] = useState<string[]>([]);
  const [testing, setTesting] = useState(false);
  const [testMessage, setTestMessage] = useState("");

  useEffect(() => {
    let ignore = false;
    const load = async () => {
      try {
        const res = await fetch(`${API_BASE}/api/watch/config`);
        if (!res.ok) throw new Error("bad status");
        const data = await res.json();
        if (ignore) return;
        setSmtpPasswordSet(Boolean(data.smtp_password_set));
        setConfigErrors(data.errors ?? []);
        if (data.config) {
          const next = toForm(data.config as WatchConfig);
          setForm(next);
          setSavedPayload(JSON.stringify(toPayload(next)));
        }
        setLoadError("");
      } catch {
        if (!ignore) setLoadError("API 서버에 연결하지 못했습니다.");
      }
    };
    load();
    return () => {
      ignore = true;
    };
  }, []);

  const update = (patch: Partial<FormState>) => setForm((prev) => (prev ? { ...prev, ...patch } : prev));

  const updateRecipient = (index: number, patch: Partial<Recipient>) =>
    setForm((prev) =>
      prev
        ? { ...prev, recipients: prev.recipients.map((r, i) => (i === index ? { ...r, ...patch } : r)) }
        : prev,
    );

  const dirty = form ? JSON.stringify(toPayload(form)) !== savedPayload : false;

  const save = async () => {
    if (!form) return;
    setSaving(true);
    setSaveMessage("");
    setSaveErrors([]);
    try {
      const res = await fetch(`${API_BASE}/api/watch/config`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(toPayload(form)),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        setSaveErrors(data.errors ?? [data.error ?? "저장하지 못했습니다."]);
        return;
      }
      const next = toForm(data.config as WatchConfig);
      setForm(next);
      setSavedPayload(JSON.stringify(toPayload(next)));
      setSmtpPasswordSet(Boolean(data.smtp_password_set));
      setSaveMessage("저장했습니다. 감시 루프는 다음 틱(30초 이내)에 새 설정을 읽습니다.");
    } catch {
      setSaveErrors(["API 서버에 연결하지 못했습니다."]);
    } finally {
      setSaving(false);
    }
  };

  const sendTest = async () => {
    setTesting(true);
    setTestMessage("");
    try {
      const res = await fetch(`${API_BASE}/api/watch/test-email`, { method: "POST" });
      const data = await res.json().catch(() => ({}));
      if (res.status === 429) {
        setTestMessage(data.error ?? "잠시 후 다시 시도하세요.");
      } else if (!res.ok) {
        setTestMessage(data.error ?? "시험 메일을 보내지 못했습니다.");
      } else {
        setTestMessage(`${data.ok ? "성공" : "실패"}: ${data.reason}`);
      }
    } catch {
      setTestMessage("API 서버에 연결하지 못했습니다.");
    } finally {
      setTesting(false);
    }
  };

  if (loadError) {
    return <div style={{ ...card, borderColor: "#ef4444", color: "#fca5a5" }}>{loadError}</div>;
  }
  if (!form) {
    return (
      <div style={card}>
        {configErrors.length > 0 ? (
          <div style={{ color: "#fca5a5", fontSize: "13px" }}>
            설정 파일이 올바르지 않습니다: {configErrors.join(" / ")}
          </div>
        ) : (
          <div style={{ color: "#94a3b8", fontSize: "13px" }}>불러오는 중...</div>
        )}
      </div>
    );
  }

  return (
    <div>
      {/* 이메일 발송 설정 */}
      <div style={card}>
        <h3 style={sectionTitle}>이메일 발송 설정</h3>
        <label style={{ display: "flex", gap: "8px", alignItems: "center", fontSize: "13px", marginBottom: "12px" }}>
          <input
            type="checkbox"
            checked={form.channels.email ?? false}
            onChange={(e) => update({ channels: { ...form.channels, email: e.target.checked } })}
          />
          이메일 알림 사용
        </label>
        <div style={{ display: "grid", gridTemplateColumns: "2fr 1fr 2fr 2fr", gap: "12px", marginBottom: "12px" }}>
          <label style={labelStyle}>
            SMTP 호스트
            <input style={inputStyle} value={form.smtp_host} onChange={(e) => update({ smtp_host: e.target.value })} />
          </label>
          <label style={labelStyle}>
            포트
            <input style={inputStyle} value={form.smtp_port} onChange={(e) => update({ smtp_port: e.target.value })} />
          </label>
          <label style={labelStyle}>
            계정
            <input style={inputStyle} value={form.smtp_user} onChange={(e) => update({ smtp_user: e.target.value })} />
          </label>
          <label style={labelStyle}>
            발신자
            <input style={inputStyle} value={form.smtp_from} onChange={(e) => update({ smtp_from: e.target.value })} />
          </label>
        </div>
        <div style={{ fontSize: "13px", marginBottom: "16px", color: smtpPasswordSet ? "#86efac" : "#fcd34d" }}>
          SMTP 비밀번호(.env의 SMTP_PASSWORD): {smtpPasswordSet ? "설정됨" : "없음"}
          <span style={{ color: "#94a3b8" }}> — 비밀번호는 화면에서 입력·저장하지 않습니다.</span>
        </div>

        <div style={{ fontSize: "13px", fontWeight: 600, marginBottom: "8px" }}>수신자</div>
        {form.recipients.length === 0 && (
          <div style={{ fontSize: "13px", color: "#94a3b8", marginBottom: "8px" }}>등록된 수신자가 없습니다.</div>
        )}
        {form.recipients.map((r, index) => (
          <div key={index} style={{ display: "flex", gap: "8px", alignItems: "center", marginBottom: "8px" }}>
            <input
              style={{ ...inputStyle, width: "160px" }}
              placeholder="이름"
              value={r.name}
              onChange={(e) => updateRecipient(index, { name: e.target.value })}
            />
            <input
              style={{ ...inputStyle, flex: 1 }}
              placeholder="이메일 주소"
              value={r.address}
              onChange={(e) => updateRecipient(index, { address: e.target.value })}
            />
            <label style={{ display: "flex", gap: "4px", alignItems: "center", fontSize: "12px" }}>
              <input
                type="checkbox"
                checked={r.enabled}
                onChange={(e) => updateRecipient(index, { enabled: e.target.checked })}
              />
              사용
            </label>
            <button
              type="button"
              style={subtleButton}
              onClick={() => update({ recipients: form.recipients.filter((_, i) => i !== index) })}
            >
              삭제
            </button>
          </div>
        ))}
        <button
          type="button"
          style={subtleButton}
          onClick={() => update({ recipients: [...form.recipients, { name: "", address: "", enabled: true }] })}
        >
          + 수신자 추가
        </button>
      </div>

      {/* 배치 시간 설정 */}
      <div style={card}>
        <h3 style={sectionTitle}>배치 시간 설정</h3>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: "12px", marginBottom: "16px" }}>
          <label style={labelStyle}>
            주기 (분)
            <input style={inputStyle} value={form.interval} onChange={(e) => update({ interval: e.target.value })} />
          </label>
          <label style={labelStyle}>
            감시 시작 (HH:MM)
            <input style={inputStyle} value={form.start} onChange={(e) => update({ start: e.target.value })} />
          </label>
          <label style={labelStyle}>
            감시 종료 (HH:MM)
            <input style={inputStyle} value={form.end} onChange={(e) => update({ end: e.target.value })} />
          </label>
        </div>

        <div style={{ fontSize: "13px", fontWeight: 600, marginBottom: "8px" }}>알림 채널</div>
        <div style={{ display: "flex", gap: "16px", marginBottom: "16px" }}>
          {Object.keys(CHANNEL_LABELS).map((name) => (
            <label key={name} style={{ display: "flex", gap: "4px", alignItems: "center", fontSize: "13px" }}>
              <input
                type="checkbox"
                checked={form.channels[name] ?? false}
                onChange={(e) => update({ channels: { ...form.channels, [name]: e.target.checked } })}
              />
              {CHANNEL_LABELS[name]}
            </label>
          ))}
        </div>

        <div style={{ fontSize: "13px", fontWeight: 600, marginBottom: "8px" }}>일일 브리핑</div>
        <div style={{ display: "flex", gap: "16px", alignItems: "center", marginBottom: "16px" }}>
          <label style={{ display: "flex", gap: "4px", alignItems: "center", fontSize: "13px" }}>
            <input
              type="checkbox"
              checked={form.briefing_enabled}
              onChange={(e) => update({ briefing_enabled: e.target.checked })}
            />
            사용
          </label>
          <label style={{ ...labelStyle, flexDirection: "row", alignItems: "center", gap: "8px" }}>
            발송 시각 (HH:MM)
            <input
              style={{ ...inputStyle, width: "90px" }}
              value={form.briefing_time}
              onChange={(e) => update({ briefing_time: e.target.value })}
            />
          </label>
        </div>

        <div style={{ fontSize: "13px", fontWeight: 600, marginBottom: "4px" }}>재검토 기준</div>
        <div style={{ fontSize: "12px", color: "#94a3b8", marginBottom: "8px" }}>
          비워 두면 &quot;감시 안 함&quot;입니다. 기본값을 대신 채우지 않습니다.
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: "12px" }}>
          {Object.entries(REVIEW_LABELS).map(([key, meta]) => (
            <label key={key} style={labelStyle}>
              {meta.label} ({meta.unit})
              <input
                style={inputStyle}
                placeholder="감시 안 함"
                value={form.review[key] ?? ""}
                onChange={(e) => update({ review: { ...form.review, [key]: e.target.value } })}
              />
              {(form.review[key] ?? "").trim() === "" && (
                <span style={{ color: "#fcd34d" }}>감시 안 함</span>
              )}
            </label>
          ))}
        </div>
      </div>

      {saveErrors.length > 0 && (
        <div style={{ ...card, borderColor: "#ef4444", color: "#fca5a5", fontSize: "13px" }}>
          {saveErrors.map((message) => (
            <div key={message}>{message}</div>
          ))}
        </div>
      )}
      {saveMessage && <div style={{ ...card, color: "#86efac", fontSize: "13px" }}>{saveMessage}</div>}

      <div style={{ display: "flex", gap: "12px", alignItems: "center" }}>
        <button type="button" style={primaryButton} onClick={save} disabled={saving || !dirty}>
          {saving ? "저장 중..." : "설정 저장"}
        </button>
        <button
          type="button"
          style={subtleButton}
          onClick={sendTest}
          disabled={testing || dirty}
          title={dirty ? "저장한 설정으로 발송하므로 먼저 저장하세요." : undefined}
        >
          {testing ? "발송 중..." : "테스트 메일 발송"}
        </button>
        {dirty && <span style={{ fontSize: "12px", color: "#fcd34d" }}>저장하지 않은 변경이 있습니다.</span>}
        {testMessage && <span style={{ fontSize: "13px", color: "#cbd5e1" }}>{testMessage}</span>}
      </div>
    </div>
  );
}
