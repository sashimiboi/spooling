"use client";

import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Bot, Send, Square, ChevronDown, Check, Loader2, Plus, Trash2, MessageSquare, PanelLeft, PanelLeftClose, PanelRight, PanelRightClose, Search, Database, Sparkles, CheckCircle2, Wrench, Plug, AlertCircle, ChevronRight, Code } from "lucide-react";

type StepKind = "search" | "workspace" | "model" | "done" | "tool_call";
interface Step { kind: StepKind; label: string; detail?: string; tool_input?: string; tool_result?: string; done: boolean }
type Msg = {
  role: "user" | "assistant";
  content: string;
  sources?: Array<{ session_id: string; role: string; project: string | null }>;
  using?: string;
  steps?: Step[];
};

interface ModelOption { id: string; label: string; provider: string; available: boolean; requiresKey?: string; }
interface ChatStatus { current: string; models: ModelOption[]; ollama_ok: boolean; byok: string[]; has_anthropic: boolean; }
interface ChatSessionMeta { id: string; title: string; created_at: string; updated_at: string; message_count: number; }
interface ConnectorInfo { id: string; slug: string; name: string; url: string; transport: string; status: string; last_error: string | null; has_auth: boolean; tool_count: number; tools_json: Array<{ name: string; description: string }>; }

const BUILT_IN_TOOLS: Array<{ name: string; desc: string }> = [
  { name: "spooling_search", desc: "Ranked message excerpts for a query." },
  { name: "spooling_recent_sessions", desc: "Newest sessions, optionally filtered by provider / days." },
  { name: "spooling_get_session", desc: "Full session metadata + ordered messages." },
  { name: "spooling_workspace_stats", desc: "Counts, cost, per-provider rollup." },
  { name: "spooling_top_projects", desc: "Projects by spend + volume." },
  { name: "spooling_semantic_search", desc: "Semantic / vector search over session messages using pgvector." },
];

export default function ChatPage() {
  const [sessions, setSessions] = useState<ChatSessionMeta[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Msg[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<ChatStatus | null>(null);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState<boolean>(false);
  const [rightPaneCollapsed, setRightPaneCollapsed] = useState<boolean>(false);
  const [connectors, setConnectors] = useState<ConnectorInfo[]>([]);
  const [disabledTools, setDisabledTools] = useState<Set<string>>(new Set());
  const [expandedConnectors, setExpandedConnectors] = useState<Set<string>>(new Set());

  useEffect(() => {
    try { const v = localStorage.getItem("spooling-chat-sidebar"); if (v === "1") setSidebarCollapsed(true); } catch {}
    try { const v = localStorage.getItem("spooling-chat-rightpane"); if (v === "1") setRightPaneCollapsed(true); } catch {}
    try {
      const v = localStorage.getItem("spooling-chat-disabled-tools");
      if (v) setDisabledTools(new Set(JSON.parse(v) as string[]));
    } catch {}
  }, []);

  useEffect(() => {
    try { localStorage.setItem("spooling-chat-disabled-tools", JSON.stringify(Array.from(disabledTools))); } catch {}
  }, [disabledTools]);
  useEffect(() => {
    try { localStorage.setItem("spooling-chat-sidebar", sidebarCollapsed ? "1" : "0"); } catch {}
  }, [sidebarCollapsed]);
  useEffect(() => {
    try { localStorage.setItem("spooling-chat-rightpane", rightPaneCollapsed ? "1" : "0"); } catch {}
  }, [rightPaneCollapsed]);

  const pickerRef = useRef<HTMLDivElement>(null);
  const endRef = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController | null>(null);

  const loadSessions = useCallback(async () => {
    const r = await fetch("/api/chat/sessions");
    if (r.ok) { const d = await r.json(); setSessions(d.sessions ?? []); }
  }, []);

  const loadStatus = useCallback(async () => {
    const r = await fetch("/api/chat/status");
    if (r.ok) setStatus(await r.json());
  }, []);

  const loadConnectors = useCallback(async () => {
    const r = await fetch("/api/connectors");
    if (r.ok) { const d = await r.json(); setConnectors(d.connectors ?? []); }
  }, []);

  useEffect(() => { loadSessions(); loadStatus(); loadConnectors(); }, [loadSessions, loadStatus, loadConnectors]);
  useEffect(() => { endRef.current?.scrollIntoView({ behavior: "smooth" }); }, [messages, busy]);

  useEffect(() => {
    if (!pickerOpen) return;
    const onDoc = (e: MouseEvent) => { if (!pickerRef.current?.contains(e.target as Node)) setPickerOpen(false); };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [pickerOpen]);

  async function openSession(id: string) {
    setActiveId(id);
    const r = await fetch(`/api/chat/sessions/${id}`);
    if (r.ok) {
      const d = await r.json();
      setMessages((d.messages ?? []).map((m: { role: string; content: string }) => ({ role: m.role as "user" | "assistant", content: m.content })));
    }
  }

  function newChat() {
    setActiveId(null);
    setMessages([]);
    setError(null);
  }

  async function deleteSession(id: string) {
    await fetch(`/api/chat/sessions/${id}`, { method: "DELETE" });
    if (activeId === id) newChat();
    await loadSessions();
  }

  const currentModel = status?.models.find((m) => m.id === status.current);

  async function pickModel(id: string) {
    setPickerOpen(false);
    await fetch("/api/settings/agent", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ chat_model: id }) });
    await loadStatus();
  }

  function cancel() {
    if (abortRef.current) {
      abortRef.current.abort();
      abortRef.current = null;
    }
  }

  async function send(e?: FormEvent) {
    e?.preventDefault();
    const text = input.trim();
    if (!text || busy) return;
    const next: Msg[] = [...messages, { role: "user", content: text }, { role: "assistant", content: "" }];
    setMessages(next);
    setInput("");
    setBusy(true);
    setError(null);
    const assistantIdx = next.length - 1;

    const controller = new AbortController();
    abortRef.current = controller;

    try {
      const enabledTools = BUILT_IN_TOOLS.filter((t) => !disabledTools.has(t.name)).map((t) => t.name);
      const res = await fetch("/api/chat", {
        method: "POST",
        signal: controller.signal,
        headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
        body: JSON.stringify({
          messages: next.slice(0, -1).map((m) => ({ role: m.role, content: m.content })),
          chat_session_id: activeId,
          enabled_tools: enabledTools,
        }),
      });

      if (!res.ok || !res.body) {
        const detail = await res.text().catch(() => "");
        setError(detail || `Chat failed (${res.status})`);
        setMessages((ms) => ms.slice(0, -1));
        return;
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      let assembled = "";

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let sep = buf.indexOf("\n\n");
        while (sep !== -1) {
          const block = buf.slice(0, sep);
          buf = buf.slice(sep + 2);
          const dataLine = block.split("\n").find((l) => l.startsWith("data: "));
          if (!dataLine) { sep = buf.indexOf("\n\n"); continue; }
          try {
            const evt = JSON.parse(dataLine.slice(6)) as {
              type: string;
              text?: string;
              sources?: Msg["sources"];
              chat_session_id?: string | null;
              using?: string;
              error?: string;
              detail?: string;
              step?: { kind: StepKind; label: string; detail?: string; tool_input?: string; tool_result?: string; done?: boolean };
            };
            if (evt.type === "delta" && evt.text) {
              assembled += evt.text;
              setMessages((ms) => {
                const copy = ms.slice();
                copy[assistantIdx] = { ...copy[assistantIdx], content: assembled };
                return copy;
              });
            } else if (evt.type === "step" && evt.step) {
              const step = evt.step;
              setMessages((ms) => {
                const copy = ms.slice();
                const current = copy[assistantIdx];
                const steps = current.steps ? [...current.steps] : [];
                const existing = steps.findIndex((s) => s.kind === step.kind);
                const next: Step = { kind: step.kind, label: step.label, detail: step.detail, tool_input: step.tool_input, tool_result: step.tool_result, done: step.done ?? false };
                if (existing >= 0) steps[existing] = next;
                else steps.push(next);
                copy[assistantIdx] = { ...current, steps };
                return copy;
              });
            } else if (evt.type === "meta") {
              setMessages((ms) => {
                const copy = ms.slice();
                copy[assistantIdx] = { ...copy[assistantIdx], sources: evt.sources };
                return copy;
              });
            } else if (evt.type === "done") {
              if (evt.chat_session_id) setActiveId(evt.chat_session_id);
              setMessages((ms) => {
                const copy = ms.slice();
                copy[assistantIdx] = { ...copy[assistantIdx], using: evt.using };
                return copy;
              });
            } else if (evt.type === "error") {
              setError(evt.detail || evt.error || "Chat failed.");
            }
          } catch { /* ignore malformed */ }
          sep = buf.indexOf("\n\n");
        }
      }
    } catch (err) {
      if ((err as Error)?.name === "AbortError") {
        setMessages((ms) => ms.slice(0, -1));
      } else {
        setError((err as Error).message || "Chat failed.");
        setMessages((ms) => ms.slice(0, -1));
      }
    } finally {
      setBusy(false);
      abortRef.current = null;
      loadSessions();
    }
  }

  return (
    <div style={{ display: "grid", gridTemplateColumns: `${sidebarCollapsed ? "0" : "240px"} minmax(0, 1fr) ${rightPaneCollapsed ? "0" : "280px"}`, columnGap: 0, height: "calc(100vh - 88px)", transition: "grid-template-columns 180ms ease" }}>
      {/* Sidebar */}
      <aside style={{ display: "flex", flexDirection: "column", gap: 10, minHeight: 0, overflow: "hidden", opacity: sidebarCollapsed ? 0 : 1, transition: "opacity 120ms", borderRight: sidebarCollapsed ? "none" : "1px solid var(--border)", paddingRight: sidebarCollapsed ? 0 : 12 }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 4 }}>
          <h1 style={{ fontSize: 16, fontWeight: 600, margin: 0 }}>Chat</h1>
          <div style={{ display: "flex", gap: 4 }}>
            <button onClick={newChat} style={newBtn} title="New chat"><Plus size={14} /></button>
            <button onClick={() => setSidebarCollapsed(true)} style={newBtn} title="Hide sidebar"><PanelLeftClose size={14} /></button>
          </div>
        </div>
        <div style={{ flex: 1, overflow: "auto", display: "flex", flexDirection: "column", gap: 2 }}>
          {sessions.length === 0 ? (
            <div style={{ fontSize: 11, color: "var(--muted-2)", padding: "16px 4px" }}>No chat history yet.</div>
          ) : sessions.map((s) => (
            <div
              key={s.id}
              onClick={() => openSession(s.id)}
              style={{
                padding: "8px 10px", borderRadius: 6, cursor: "pointer",
                background: activeId === s.id ? "var(--surface-hover)" : "transparent",
                border: "1px solid " + (activeId === s.id ? "var(--border-strong)" : "transparent"),
                display: "flex", alignItems: "center", gap: 8,
              }}
            >
              <MessageSquare size={12} style={{ color: "var(--muted-2)", flexShrink: 0 }} />
              <div style={{ minWidth: 0, flex: 1 }}>
                <div style={{ fontSize: 12, fontWeight: 500, color: "var(--fg)", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{s.title}</div>
                <div style={{ fontSize: 10, color: "var(--muted-2)" }}>{new Date(s.updated_at).toLocaleDateString()} · {s.message_count} msg</div>
              </div>
              <button onClick={(e) => { e.stopPropagation(); deleteSession(s.id); }} style={{ background: "transparent", border: "none", color: "var(--muted-2)", cursor: "pointer", padding: 2 }}><Trash2 size={11} /></button>
            </div>
          ))}
        </div>
      </aside>

      {/* Main chat area */}
      <div style={{ display: "flex", flexDirection: "column", minHeight: 0, marginLeft: sidebarCollapsed ? 0 : 20, marginRight: rightPaneCollapsed ? 0 : 20, transition: "margin 180ms ease" }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 10, gap: 10 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 8, minWidth: 0 }}>
            {sidebarCollapsed && (
              <button onClick={() => setSidebarCollapsed(false)} style={newBtn} title="Show chat history">
                <PanelLeft size={14} />
              </button>
            )}
            <p style={{ fontSize: 13, color: "var(--muted)", margin: 0 }}>Ask about synced sessions. Agent searches the workspace + feeds excerpts to the model.</p>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 6, flexShrink: 0 }}>
            {status && (
              <div ref={pickerRef} style={{ position: "relative" }}>
                <button onClick={() => setPickerOpen((v) => !v)} style={modelBtn}>
                  <span style={{ position: "relative", height: 8, width: 8, display: "inline-flex", flexShrink: 0 }}>
                    <span style={{ position: "absolute", inset: 0, borderRadius: 999, background: status.ollama_ok ? "#10b981" : "#f59e0b" }} />
                    {status.ollama_ok && <span style={{ position: "absolute", inset: 0, borderRadius: 999, background: "rgba(16,185,129,0.5)", animation: "ping 1.6s infinite" }} />}
                  </span>
                  <Bot size={12} style={{ flexShrink: 0 }} />
                  <span style={{ fontWeight: 500, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis", maxWidth: 220 }}>{currentModel?.label ?? status.current}</span>
                  <ChevronDown size={12} style={{ color: "var(--muted-2)", flexShrink: 0, transform: pickerOpen ? "rotate(180deg)" : "none" }} />
                </button>
                {pickerOpen && (
                  <div style={{ position: "absolute", top: "calc(100% + 6px)", right: 0, zIndex: 30, width: 320, background: "#141420", border: "1px solid var(--border-strong)", borderRadius: 10, padding: 4, boxShadow: "0 12px 28px rgba(0,0,0,0.5)" }}>
                    <div style={{ padding: "6px 8px 2px", fontSize: 10, color: "var(--muted-2)", textTransform: "uppercase", letterSpacing: "0.06em" }}>Default (no key)</div>
                    {status.models.filter((m) => m.provider === "ollama").map((m) => (
                      <ModelRow key={m.id} m={m} active={status.current === m.id} onPick={pickModel} connectedLabel={status.ollama_ok ? "Connected" : "Warming"} />
                    ))}
                    <div style={{ padding: "8px 8px 2px", fontSize: 10, color: "var(--muted-2)", textTransform: "uppercase", letterSpacing: "0.06em" }}>Bring your own key</div>
                    {status.models.filter((m) => m.provider !== "ollama").map((m) => (
                      <ModelRow key={m.id} m={m} active={status.current === m.id} onPick={pickModel} />
                    ))}
                    <div style={{ borderTop: "1px solid var(--border)", marginTop: 4, padding: "8px 10px", fontSize: 11 }}>
                      <Link href="/settings" style={{ color: "#c4b5fd", textDecoration: "none" }}>+ Add API keys in Settings</Link>
                    </div>
                  </div>
                )}
              </div>
            )}
            {rightPaneCollapsed && (
              <button onClick={() => setRightPaneCollapsed(false)} style={newBtn} title="Show tools & connectors">
                <PanelRight size={14} />
              </button>
            )}
          </div>
        </div>

        <div style={{ flex: 1, overflow: "auto", paddingBottom: 12 }}>
          {messages.length === 0 ? (
            <div style={{ color: "var(--muted-2)", fontSize: 13, padding: 20, textAlign: "center", lineHeight: 1.6 }}>
              Ask about your synced sessions. Try &ldquo;what did I work on this week?&rdquo; or &ldquo;which provider cost the most?&rdquo;
            </div>
          ) : messages.map((m, i) => {
            const isStreamingAssistant = busy && m.role === "assistant" && i === messages.length - 1;
            const isUser = m.role === "user";
            return (
            <div key={i} style={{ display: "flex", justifyContent: isUser ? "flex-end" : "flex-start", marginBottom: 10 }}>
              <div style={{
                maxWidth: isUser ? "75%" : "92%",
                padding: "10px 14px", borderRadius: 12, fontSize: 13, lineHeight: 1.55, minWidth: 0,
                background: isUser ? "rgba(167,139,250,0.18)" : "var(--surface)",
                border: isUser ? "1px solid rgba(167,139,250,0.3)" : "1px solid var(--border)",
                overflowWrap: "anywhere",
                whiteSpace: isUser ? "pre-wrap" : "normal",
              }}>
                {!isUser && m.steps && m.steps.length > 0 && <StepsStrip steps={m.steps} /> }
                {m.content
                  ? (isUser
                      ? <span>{m.content}</span>
                      : <div className="markdown"><ReactMarkdown remarkPlugins={[remarkGfm]}>{m.content}</ReactMarkdown></div>)
                  : (isStreamingAssistant && (
                    <span style={{ display: "inline-flex", alignItems: "center", gap: 6, color: "var(--muted)" }}>
                      <Loader2 size={12} className="spin" /> Thinking&hellip;
                    </span>
                  ))}
                {isStreamingAssistant && m.content && (
                  <span className="blink" style={{ display: "inline-block", width: 6, height: 12, background: "var(--muted)", marginLeft: 2, verticalAlign: "middle" }} />
                )}
                {m.sources && m.sources.length > 0 && (
                  <div style={{ marginTop: 10, paddingTop: 8, borderTop: "1px solid var(--border)", fontSize: 11, color: "var(--muted-2)" }}>
                    {m.sources.length} source{m.sources.length === 1 ? "" : "s"}:{" "}
                    {m.sources.slice(0, 5).map((s, j) => (
                      <Link key={j} href={`/sessions/${encodeURIComponent(s.session_id)}`} style={{ color: "var(--muted)", marginRight: 8 }}>
                        {s.session_id.slice(0, 8)}
                      </Link>
                    ))}
                  </div>
                )}
                {m.using && m.role === "assistant" && !isStreamingAssistant && (
                  <div style={{ marginTop: 6, fontSize: 10, color: "var(--muted-2)" }}>via {m.using}</div>
                )}
              </div>
            </div>
          );})}
          {error && (
            <div style={{ fontSize: 12, color: "#fca5a5", background: "rgba(248,113,113,0.08)", border: "1px solid rgba(248,113,113,0.25)", borderRadius: 6, padding: "8px 10px", marginTop: 8 }}>
              {error}
            </div>
          )}
          <div ref={endRef} />
        </div>

        <form onSubmit={send} style={{ display: "flex", gap: 8 }}>
          <input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="Ask about your sessions&hellip;"
            disabled={busy}
            style={{ flex: 1, background: "var(--input-bg)", border: "1px solid var(--border)", borderRadius: 8, padding: "10px 14px", fontSize: 14, color: "var(--fg)", outline: "none" }}
          />
          {busy ? (
            <button type="button" onClick={cancel} style={{ background: "#ef4444", color: "#fff", border: "none", borderRadius: 8, padding: "10px 18px", fontSize: 14, fontWeight: 500, cursor: "pointer", display: "inline-flex", alignItems: "center", gap: 6 }}>
              <Square size={14} /> Stop
            </button>
          ) : (
            <button type="submit" disabled={!input.trim()} style={{ background: "#ffffff", color: "#000", border: "none", borderRadius: 8, padding: "10px 18px", fontSize: 14, fontWeight: 500, cursor: "pointer", opacity: input.trim() ? 1 : 0.5, display: "inline-flex", alignItems: "center", gap: 6 }}>
              <Send size={14} /> Send
            </button>
          )}
        </form>
      </div>

      {/* Right pane */}
      <aside style={{ display: "flex", flexDirection: "column", gap: 14, minHeight: 0, overflow: "hidden", opacity: rightPaneCollapsed ? 0 : 1, transition: "opacity 120ms", borderLeft: rightPaneCollapsed ? "none" : "1px solid var(--border)", paddingLeft: rightPaneCollapsed ? 0 : 12 }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
          <h2 style={{ fontSize: 13, fontWeight: 600, margin: 0, color: "var(--fg)" }}>Workspace</h2>
          <button onClick={() => setRightPaneCollapsed(true)} style={newBtn} title="Hide pane"><PanelRightClose size={14} /></button>
        </div>

        <div style={{ flex: 1, overflow: "auto", display: "flex", flexDirection: "column", gap: 18 }}>
          <section>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 8 }}>
              <div style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 10, fontWeight: 600, color: "var(--muted-2)", textTransform: "uppercase", letterSpacing: "0.08em" }}>
                <Wrench size={11} /> Tools <span style={{ color: "var(--muted-2)", fontWeight: 500 }}>({BUILT_IN_TOOLS.length + connectors.length})</span>
              </div>
              <Link href="/settings" style={{ fontSize: 10, color: "#a78bfa", textDecoration: "none" }}>Manage</Link>
            </div>

            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 6 }}>
              <span style={{ fontSize: 9, fontWeight: 600, color: "var(--muted-2)", textTransform: "uppercase", letterSpacing: "0.08em" }}>Built-in</span>
              <span style={{ fontSize: 10, color: "var(--muted-2)" }}>
                {BUILT_IN_TOOLS.filter((t) => !disabledTools.has(t.name)).length}/{BUILT_IN_TOOLS.length} on
              </span>
            </div>
            <div style={{ marginBottom: 12 }}>
              {BUILT_IN_TOOLS.map((t) => {
                const isOn = !disabledTools.has(t.name);
                return (
                  <div
                    key={t.name}
                    title={t.desc}
                    style={{
                      ...paneCard,
                      cursor: "pointer",
                      opacity: isOn ? 1 : 0.7,
                      display: "flex", alignItems: "center", gap: 8,
                    }}
                    onClick={() => {
                      setDisabledTools((prev) => {
                        const next = new Set(prev);
                        if (next.has(t.name)) next.delete(t.name);
                        else next.add(t.name);
                        return next;
                      });
                    }}
                  >
                    <Wrench size={11} style={{ color: isOn ? "#a78bfa" : "var(--muted-2)", flexShrink: 0 }} />
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ fontSize: 12, fontWeight: 500, color: "var(--fg)", fontFamily: "ui-monospace, Menlo, monospace" }}>{t.name}</div>
                      <div style={{ fontSize: 10, color: "var(--muted-2)", marginTop: 2, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{t.desc}</div>
                    </div>
                    <ToggleSwitch checked={isOn} />
                  </div>
                );
              })}
            </div>

            <div style={{ fontSize: 9, fontWeight: 600, color: "var(--muted-2)", textTransform: "uppercase", letterSpacing: "0.08em", marginBottom: 6 }}>MCP connectors</div>
            {connectors.length === 0 ? (
              <div style={{ fontSize: 11, color: "var(--muted-2)", padding: "4px 4px 8px", lineHeight: 1.5 }}>
                None connected. <Link href="/settings" style={{ color: "#a78bfa" }}>Connect MCP servers</Link> like Linear, Notion, GitHub.
              </div>
            ) : connectors.map((c) => {
              const ok = c.status === "connected";
              const err = c.status === "error" || !!c.last_error;
              const untested = c.has_auth && c.status !== "connected" && c.status !== "error";
              const label = ok
                ? `${c.tool_count} tool${c.tool_count === 1 ? "" : "s"}`
                : err ? "error"
                : untested ? "untested"
                : "disconnected";
              const dot = ok ? "#10b981" : err ? "#ef4444" : untested ? "#fbbf24" : "#6b7280";
              const tools = Array.isArray(c.tools_json) ? c.tools_json : [];
              const expanded = expandedConnectors.has(c.id);
              const expandable = ok && tools.length > 0;
              return (
                <div key={c.id} style={{ ...paneCard, padding: 0 }} title={c.last_error ?? c.url}>
                  <button
                    onClick={() => {
                      if (!expandable) return;
                      setExpandedConnectors((prev) => {
                        const next = new Set(prev);
                        if (next.has(c.id)) next.delete(c.id);
                        else next.add(c.id);
                        return next;
                      });
                    }}
                    style={{
                      width: "100%", background: "transparent", border: "none", color: "inherit",
                      padding: "8px 10px", textAlign: "left", cursor: expandable ? "pointer" : "default",
                      display: "block",
                    }}
                  >
                    <div style={{ display: "flex", alignItems: "center", gap: 6, minWidth: 0 }}>
                      <span style={{ height: 6, width: 6, borderRadius: 999, background: dot, flexShrink: 0 }} />
                      <Plug size={11} style={{ color: "var(--muted-2)", flexShrink: 0 }} />
                      <span style={{ fontSize: 12, fontWeight: 500, color: "var(--fg)", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis", flex: 1, minWidth: 0 }}>{c.name}</span>
                      {err && <AlertCircle size={11} style={{ color: "#ef4444", flexShrink: 0 }} />}
                      {expandable && (
                        <ChevronDown size={11} style={{ color: "var(--muted-2)", flexShrink: 0, transform: expanded ? "rotate(180deg)" : "none", transition: "transform 120ms" }} />
                      )}
                    </div>
                    <div style={{ fontSize: 10, color: "var(--muted-2)", marginTop: 4, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
                      {c.transport} · {label}
                    </div>
                  </button>
                  {expanded && expandable && (
                    <div style={{ borderTop: "1px solid var(--border)", padding: "6px 4px", maxHeight: 220, overflow: "auto" }}>
                      {tools.map((t) => (
                        <div key={t.name} title={t.description} style={{ padding: "4px 8px", fontSize: 11, color: "var(--fg)", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
                          <span style={{ color: "#a78bfa", fontFamily: "ui-monospace, Menlo, monospace", fontSize: 10 }}>{t.name}</span>
                          {t.description && (
                            <div style={{ fontSize: 9, color: "var(--muted-2)", marginTop: 1, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{t.description}</div>
                          )}
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              );
            })}
          </section>
        </div>
      </aside>

      <style>{`
        @keyframes ping { 0% { transform: scale(1); opacity: 0.9 } 100% { transform: scale(2.2); opacity: 0 } }
        .spin { animation: spin 1s linear infinite }
        @keyframes spin { to { transform: rotate(360deg) } }
        .blink { animation: blink 1.1s step-end infinite }
        @keyframes blink { 50% { opacity: 0 } }

        .markdown p { margin: 0 0 8px; }
        .markdown p:last-child { margin-bottom: 0; }
        .markdown ul, .markdown ol { margin: 4px 0 8px; padding-left: 20px; }
        .markdown li { margin: 2px 0; }
        .markdown code { background: var(--border); padding: 1px 5px; border-radius: 4px; font-family: ui-monospace, Menlo, monospace; font-size: 12px; }
        .markdown pre { background: var(--input-bg); border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; overflow: auto; margin: 6px 0; }
        .markdown pre code { background: transparent; padding: 0; font-size: 12px; }
        .markdown h1, .markdown h2, .markdown h3 { margin: 10px 0 6px; font-weight: 600; line-height: 1.3; }
        .markdown h1 { font-size: 16px; }
        .markdown h2 { font-size: 14px; }
        .markdown h3 { font-size: 13px; }
        .markdown a { color: #a78bfa; }
        .markdown table { border-collapse: collapse; margin: 6px 0; font-size: 12px; }
        .markdown th, .markdown td { border: 1px solid var(--border); padding: 4px 8px; }
        .markdown blockquote { border-left: 2px solid var(--border-strong); margin: 6px 0; padding: 2px 10px; color: var(--muted); }
      `}</style>
    </div>
  );
}

function StepsStrip({ steps }: { steps: Step[] }) {
  const [expandedTool, setExpandedTool] = useState<string | null>(null);
  const ICONS: Record<StepKind, React.ComponentType<{ size?: number; style?: React.CSSProperties }>> = {
    search: Search,
    workspace: Database,
    model: Sparkles,
    done: CheckCircle2,
    tool_call: Code,
  };
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4, marginBottom: 10, padding: 8, borderRadius: 8, background: "var(--surface)", border: "1px solid var(--border)" }}>
      {steps.map((s, i) => {
        const Icon = ICONS[s.kind] ?? Sparkles;
        const iconColor = s.done ? "#34d399" : s.kind === "model" ? "#a78bfa" : "#fbbf24";
        const isToolCall = s.kind === "tool_call";
        const isExpanded = expandedTool === `${i}`;
        return (
          <div key={i}>
            <div
              style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 11, color: "var(--muted)", cursor: isToolCall && s.done ? "pointer" : "default" }}
              onClick={() => isToolCall && s.done && setExpandedTool(isExpanded ? null : `${i}`)}
            >
              {s.done
                ? (isToolCall
                    ? <Code size={11} style={{ color: "#34d399", flexShrink: 0 }} />
                    : <CheckCircle2 size={11} style={{ color: "#34d399", flexShrink: 0 }} />)
                : <Icon size={11} style={{ color: iconColor, flexShrink: 0 }} />}
              <span style={{ fontWeight: 500, color: s.done ? "var(--muted)" : "var(--fg)" }}>{s.label}</span>
              {s.detail && <span style={{ color: "var(--muted-2)", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis", maxWidth: 240 }}>{s.detail}</span>}
              {!s.done && s.kind !== "done" && <Loader2 size={10} className="spin" style={{ color: "var(--muted-2)", flexShrink: 0 }} />}
              {isToolCall && s.done && (
                <ChevronRight size={11} style={{ color: "var(--muted-2)", flexShrink: 0, marginLeft: "auto", transform: isExpanded ? "rotate(90deg)" : "none", transition: "transform 120ms" }} />
              )}
            </div>
            {isExpanded && isToolCall && s.done && (
              <div style={{ marginTop: 4, marginLeft: 19, padding: "6px 8px", borderRadius: 6, background: "rgba(0,0,0,0.15)", border: "1px solid var(--border)", fontSize: 11, lineHeight: 1.5 }}>
                {s.tool_input && (
                  <div style={{ marginBottom: 6 }}>
                    <div style={{ fontSize: 9, fontWeight: 600, color: "var(--muted-2)", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 2 }}>Input</div>
                    <pre style={{ margin: 0, fontSize: 10, color: "var(--muted)", whiteSpace: "pre-wrap", wordBreak: "break-all", maxHeight: 120, overflow: "auto" }}>{s.tool_input}</pre>
                  </div>
                )}
                {s.tool_result && (
                  <div>
                    <div style={{ fontSize: 9, fontWeight: 600, color: "var(--muted-2)", textTransform: "uppercase", letterSpacing: "0.06em", marginBottom: 2 }}>Result</div>
                    <pre style={{ margin: 0, fontSize: 10, color: "var(--muted)", whiteSpace: "pre-wrap", wordBreak: "break-all", maxHeight: 200, overflow: "auto" }}>{s.tool_result}</pre>
                  </div>
                )}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

function ToggleSwitch({ checked }: { checked: boolean }) {
  return (
    <span
      aria-checked={checked}
      role="switch"
      style={{
        position: "relative", width: 26, height: 14, borderRadius: 999, flexShrink: 0,
        background: checked ? "rgba(167,139,250,0.65)" : "var(--border-strong)",
        transition: "background 120ms ease",
      }}
    >
      <span style={{
        position: "absolute", top: 2, left: checked ? 14 : 2, height: 10, width: 10,
        borderRadius: 999, background: "#ffffff", transition: "left 120ms ease",
      }} />
    </span>
  );
}

function ModelRow({ m, active, onPick, connectedLabel }: { m: ModelOption; active: boolean; onPick: (id: string) => void; connectedLabel?: string }) {
  return (
    <button
      onClick={() => m.available && onPick(m.id)}
      disabled={!m.available}
      style={{
        width: "100%", display: "flex", alignItems: "center", gap: 10, padding: "8px 10px",
        background: active ? "var(--surface-hover)" : "transparent",
        border: "none", textAlign: "left", cursor: m.available ? "pointer" : "not-allowed",
        color: m.available ? "var(--fg)" : "var(--muted-2)", borderRadius: 6,
      }}
      title={m.available ? "" : `Add an ${m.requiresKey} key in Settings`}
    >
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: 12, fontWeight: 500, display: "flex", alignItems: "center", gap: 6, flexWrap: "nowrap" }}>
          <span style={{ whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis", minWidth: 0 }}>{m.label}</span>
          {m.provider === "ollama" && m.available && (
            <span style={{ fontSize: 9, color: "#10b981", background: "rgba(16,185,129,0.1)", border: "1px solid rgba(16,185,129,0.25)", padding: "1px 5px", borderRadius: 999, textTransform: "uppercase", letterSpacing: "0.06em", whiteSpace: "nowrap", flexShrink: 0 }}>
              {connectedLabel ?? "Connected"}
            </span>
          )}
          {!m.available && m.requiresKey && (
            <span style={{ fontSize: 9, color: "var(--muted-2)", whiteSpace: "nowrap", flexShrink: 0 }}>requires {m.requiresKey} key</span>
          )}
        </div>
        <div style={{ fontSize: 10, color: "var(--muted-2)", fontFamily: "ui-monospace, Menlo, monospace", whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{m.id}</div>
      </div>
      {active && <Check size={12} style={{ color: "#a78bfa", flexShrink: 0 }} />}
    </button>
  );
}

const newBtn: React.CSSProperties = { background: "transparent", border: "1px solid var(--border)", color: "var(--fg)", padding: 4, borderRadius: 6, cursor: "pointer", display: "inline-flex" };
const modelBtn: React.CSSProperties = { display: "inline-flex", alignItems: "center", gap: 8, background: "var(--surface)", border: "1px solid var(--border)", borderRadius: 8, padding: "6px 10px", fontSize: 12, color: "var(--fg)", cursor: "pointer", maxWidth: 320, minWidth: 0 };
const paneCard: React.CSSProperties = { display: "block", padding: "8px 10px", borderRadius: 8, border: "1px solid var(--border)", background: "var(--surface)", marginBottom: 6 };
