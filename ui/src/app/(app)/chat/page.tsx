'use client';

import { useState, useRef, useEffect, useCallback } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Send, Bot, User, Loader2, Settings, Plus, Trash2, MessageSquare, ChevronDown, FileText, PanelRight, PanelRightClose, Plug, AlertCircle } from 'lucide-react';
import Link from 'next/link';
import { fetchApi, postApi, deleteApi } from '@/lib/api';
import { useRouter } from 'next/navigation';

interface Source {
  session_id: string;
  project?: string | null;
  role?: string | null;
  timestamp?: string | null;
  similarity?: number | null;
  title?: string | null;
  excerpt?: string | null;
}

interface Message {
  role: 'user' | 'assistant';
  content: string;
  sources?: Source[];
}

interface ChatSession {
  id: string;
  title: string;
  model: string;
  provider: string;
  message_count: number;
  created_at: string;
  updated_at: string;
}

interface ConnectedProvider {
  id: string;
  name: string;
  type: string;
  status: string;
  icon: string;
  session_count: number;
  last_synced_at: string | null;
}

const SUGGESTIONS = [
  'What did I work on this week?',
  'How much have I spent on AI coding?',
  'Which project has the most sessions?',
  'What tools do I use most?',
  'Summarize my recent coding activity',
  'What was my last session about?',
];

export default function ChatPage() {
  const router = useRouter();
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false);
  const [modelInfo, setModelInfo] = useState<{ provider: string; model: string; connected?: boolean; purpose?: string } | null>(null);
  const [chatSessionId, setChatSessionId] = useState<string | null>(null);
  const [history, setHistory] = useState<ChatSession[]>([]);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [providers, setProviders] = useState<ConnectedProvider[]>([]);
  const [rightPaneCollapsed, setRightPaneCollapsed] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    try { const v = localStorage.getItem('spooling-chat-rightpane'); if (v === '1') setRightPaneCollapsed(true); } catch {}
  }, []);
  useEffect(() => {
    try { localStorage.setItem('spooling-chat-rightpane', rightPaneCollapsed ? '1' : '0'); } catch {}
  }, [rightPaneCollapsed]);

  const loadHistory = useCallback(async () => {
    try {
      const data = await fetchApi('/api/chat/sessions?limit=30');
      setHistory(Array.isArray(data) ? data : []);
      setHistoryError(null);
    } catch (e) {
      console.error('[chat] loadHistory failed:', e);
      setHistoryError(e instanceof Error ? e.message : 'Failed to load chat history');
    }
  }, []);

  useEffect(() => {
    fetchApi('/api/settings/agents').then((a) => {
      if (a?.chat) {
        setModelInfo({
          provider: a.chat.provider || 'ollama',
          model: a.chat.model || 'gemma3:4b',
          connected: a.chat.connected,
          purpose: a.chat.purpose,
        });
      }
    }).catch(() => {
      fetchApi('/api/settings').then((s) => {
        setModelInfo({ provider: s.provider || 'ollama', model: s.model || 'gemma3:4b' });
      }).catch(() => {});
    });
    loadHistory();
    fetchApi('/api/providers').then((p) => setProviders(Array.isArray(p) ? p : [])).catch(() => {});
  }, [loadHistory]);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  const send = async (text?: string) => {
    const msg = text || input.trim();
    if (!msg || loading) return;

    const userMsg: Message = { role: 'user', content: msg };
    const newMessages = [...messages, userMsg];
    setMessages(newMessages);
    setInput('');
    setLoading(true);

    try {
      const data = await postApi('/api/chat', {
        messages: newMessages,
        chat_session_id: chatSessionId,
      });
      setMessages([...newMessages, { role: 'assistant', content: data.response, sources: data.sources }]);
      if (data.chat_session_id) {
        setChatSessionId(data.chat_session_id);
      }
      loadHistory();
    } catch (e) {
      setMessages([...newMessages, { role: 'assistant', content: 'Failed to get response. Check that the API server is running.' }]);
    } finally {
      setLoading(false);
    }
  };

  const loadSession = async (id: string) => {
    try {
      const data = await fetchApi(`/api/chat/sessions/${id}`);
      if (data.messages) {
        setMessages(data.messages.map((m: any) => ({ role: m.role, content: m.content })));
        setChatSessionId(id);
      }
    } catch (e) { console.error(e); }
  };

  const deleteSession = async (id: string, e: React.MouseEvent) => {
    e.stopPropagation();
    try {
      await deleteApi(`/api/chat/sessions/${id}`);
      if (chatSessionId === id) {
        newChat();
      }
      loadHistory();
    } catch (e) { console.error(e); }
  };

  const newChat = () => {
    setMessages([]);
    setChatSessionId(null);
  };

  return (
    <div className="flex h-[calc(100vh-40px)] gap-3">
      {/* Chat history sidebar */}
      <div className="w-52 shrink-0 flex flex-col border-r border-border pr-3">
        <Button variant="outline" size="sm" className="w-full mb-2" onClick={newChat}>
          <Plus className="h-3 w-3 mr-1.5" /> New Chat
        </Button>

        <div className="flex-1 overflow-auto scrollbar-thin space-y-px">
          {history.map((s) => (
            <div
              key={s.id}
              onClick={() => loadSession(s.id)}
              className={`group flex items-center gap-2 px-2 py-1.5 rounded-md cursor-pointer transition-colors text-left ${
                chatSessionId === s.id
                  ? 'bg-accent text-foreground'
                  : 'hover:bg-accent/50 text-muted-foreground'
              }`}
            >
              <MessageSquare className="h-3 w-3 shrink-0" />
              <div className="flex-1 min-w-0">
                <div className="text-[11px] font-medium truncate">{s.title || 'Untitled'}</div>
                <div className="text-[10px] text-muted-foreground">
                  {new Date(s.updated_at).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })}
                  {' \u00B7 '}
                  {s.message_count} msgs
                </div>
              </div>
              <button
                onClick={(e) => deleteSession(s.id, e)}
                className="opacity-0 group-hover:opacity-100 p-0.5 rounded hover:bg-destructive/10 hover:text-destructive transition-all"
                title="Delete chat"
              >
                <Trash2 className="h-3 w-3" />
              </button>
            </div>
          ))}
          {history.length === 0 && !historyError && (
            <p className="text-[11px] text-muted-foreground text-center py-4">No chat history yet</p>
          )}
          {historyError && (
            <div className="text-[11px] text-center py-4 space-y-1.5">
              <p className="text-amber-500">Could not load history</p>
              <p className="text-muted-foreground break-words px-1">{historyError}</p>
              <button onClick={loadHistory} className="underline underline-offset-2 text-muted-foreground hover:text-foreground">
                Retry
              </button>
            </div>
          )}
        </div>
      </div>

      {/* Main chat area */}
      <div className="flex-1 flex flex-col min-w-0">
        <div className="flex items-start justify-between gap-3 mb-3">
          <div className="min-w-0">
            <h1 className="text-lg font-semibold tracking-tight">Chat</h1>
            <p className="text-[13px] text-muted-foreground">Ask questions about your coding sessions</p>
          </div>
          <div className="flex items-center gap-2 shrink-0">
          {modelInfo && (
            <button
              onClick={() => router.push('/settings')}
              className="flex items-center gap-2 px-3 py-1.5 rounded-md border border-border bg-card hover:bg-accent transition-colors text-[11px] group shrink-0"
              title="Change model in Settings"
            >
              <div className="flex items-center gap-1">
                <span className={`relative inline-block h-1.5 w-1.5 rounded-full ${modelInfo.connected === false ? 'bg-amber-500' : 'bg-emerald-500'}`}>
                  {modelInfo.connected !== false && (
                    <span className="absolute inset-0 rounded-full bg-emerald-500/60 animate-ping" />
                  )}
                </span>
                <Bot className="h-3 w-3 text-muted-foreground" />
              </div>
              <div className="flex flex-col items-start leading-tight">
                <span className="font-medium text-foreground tabular-nums">
                  {modelInfo.model || ''}
                </span>
                <span className="text-[9px] text-muted-foreground uppercase tracking-wider">
                  {modelInfo.provider === 'anthropic' ? 'Anthropic · RAG' : 'Ollama · RAG'}
                </span>
              </div>
              <Settings className="h-3 w-3 text-muted-foreground group-hover:text-foreground transition-colors" />
            </button>
          )}
          {rightPaneCollapsed && (
            <button
              onClick={() => setRightPaneCollapsed(false)}
              className="p-1.5 rounded-md border border-border bg-card hover:bg-accent text-muted-foreground hover:text-foreground transition-colors"
              title="Show sources & tools"
            >
              <PanelRight className="h-3.5 w-3.5" />
            </button>
          )}
          </div>
        </div>

        {/* Messages */}
        <div className="flex-1 overflow-auto scrollbar-thin space-y-3 pb-3">
          {messages.length === 0 && (
            <div className="flex flex-col items-center justify-center h-full gap-5">
              <div className="text-center">
                <div className="w-10 h-10 rounded-lg bg-primary/10 flex items-center justify-center mx-auto mb-2">
                  <Bot className="h-5 w-5 text-primary" />
                </div>
                <h2 className="text-sm font-semibold">Spool Assistant</h2>
                <p className="text-[13px] text-muted-foreground mt-1 max-w-md">
                  Explore your session history, usage stats, and costs.
                </p>
              </div>
              <div className="flex flex-wrap justify-center gap-1.5 max-w-lg">
                {SUGGESTIONS.map((s) => (
                  <button
                    key={s}
                    onClick={() => send(s)}
                    className="px-2.5 py-1 text-[11px] rounded-md border border-border bg-transparent hover:bg-accent transition-colors text-muted-foreground hover:text-foreground"
                  >
                    {s}
                  </button>
                ))}
              </div>
            </div>
          )}

          {messages.map((m, i) => (
            <div key={i} className={`flex gap-2.5 min-w-0 ${m.role === 'user' ? 'justify-end' : ''}`}>
              {m.role === 'assistant' && (
                <div className="w-7 h-7 rounded-md bg-primary/10 flex items-center justify-center shrink-0 mt-0.5">
                  <Bot className="h-3.5 w-3.5 text-primary" />
                </div>
              )}
              <div className={`min-w-0 max-w-[75%] ${
                m.role === 'user'
                  ? 'bg-primary text-primary-foreground rounded-xl rounded-br-sm px-3 py-2'
                  : 'bg-secondary rounded-xl rounded-bl-sm px-3 py-2'
              }`}>
                {m.role === 'user' ? (
                  <div className="text-[13px] leading-relaxed whitespace-pre-wrap" style={{ overflowWrap: 'anywhere' }}>{m.content}</div>
                ) : (
                  <>
                    <div className="text-[13px] leading-relaxed prose prose-sm max-w-none dark:prose-invert break-words prose-p:my-1.5 prose-pre:my-2 prose-pre:bg-background prose-pre:text-[11px] prose-pre:overflow-x-auto prose-pre:whitespace-pre prose-code:text-[11px] prose-code:bg-background prose-code:px-1 prose-code:py-0.5 prose-code:rounded prose-code:before:content-none prose-code:after:content-none prose-code:break-words prose-ul:my-1.5 prose-ol:my-1.5 prose-li:my-0.5 prose-headings:mt-2 prose-headings:mb-1 prose-headings:text-[13px] prose-headings:font-semibold prose-a:text-primary prose-table:text-[12px] prose-th:font-semibold prose-td:py-1 prose-th:py-1">
                      <ReactMarkdown remarkPlugins={[remarkGfm]}>
                        {m.content || ''}
                      </ReactMarkdown>
                    </div>
                    {m.sources && m.sources.length > 0 && <SourcesDisclosure sources={m.sources} />}
                  </>
                )}
              </div>
              {m.role === 'user' && (
                <div className="w-7 h-7 rounded-md bg-secondary flex items-center justify-center shrink-0 mt-0.5">
                  <User className="h-3.5 w-3.5 text-muted-foreground" />
                </div>
              )}
            </div>
          ))}

          {loading && (
            <div className="flex gap-2.5">
              <div className="w-7 h-7 rounded-md bg-primary/10 flex items-center justify-center shrink-0">
                <Bot className="h-3.5 w-3.5 text-primary" />
              </div>
              <div className="bg-secondary rounded-xl rounded-bl-sm px-3 py-2">
                <Loader2 className="h-3.5 w-3.5 animate-spin text-muted-foreground" />
              </div>
            </div>
          )}

          <div ref={messagesEndRef} />
        </div>

        {/* Input */}
        <div className="border-t pt-3">
          <div className="flex gap-2">
            <Input
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } }}
              placeholder="Ask about your sessions..."
              disabled={loading}
              className="flex-1"
            />
            <Button onClick={() => send()} disabled={loading || !input.trim()} size="icon">
              <Send className="h-3.5 w-3.5" />
            </Button>
          </div>
        </div>
      </div>

      {/* Right pane: sources & tools the chat agent can see */}
      {!rightPaneCollapsed && (
        <div className="w-64 shrink-0 flex flex-col border-l border-border pl-3">
          <div className="flex items-center justify-between mb-2">
            <h3 className="text-[11px] font-semibold uppercase tracking-wider text-muted-foreground">Workspace</h3>
            <button
              onClick={() => setRightPaneCollapsed(true)}
              className="p-1 rounded hover:bg-accent text-muted-foreground hover:text-foreground transition-colors"
              title="Hide pane"
            >
              <PanelRightClose className="h-3.5 w-3.5" />
            </button>
          </div>
          <div className="flex-1 overflow-auto scrollbar-thin space-y-4">
            <section>
              <div className="flex items-center justify-between mb-1.5">
                <div className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
                  <Plug className="h-3 w-3" />
                  Connected sources <span className="font-normal opacity-70">({providers.length})</span>
                </div>
                <Link href="/settings" className="text-[10px] text-primary hover:underline">Manage</Link>
              </div>
              {providers.length === 0 ? (
                <p className="text-[11px] text-muted-foreground py-2">
                  No sources connected. <Link href="/settings" className="text-primary hover:underline">Add one</Link>.
                </p>
              ) : (
                <div className="space-y-1">
                  {providers.map((p) => {
                    const ok = p.status === 'connected';
                    return (
                      <Link key={p.id} href="/settings" className="block">
                        <div className="rounded-md border border-border bg-card hover:bg-accent/50 transition-colors px-2 py-1.5">
                          <div className="flex items-center gap-1.5 min-w-0">
                            <span className={`h-1.5 w-1.5 rounded-full shrink-0 ${ok ? 'bg-emerald-500' : 'bg-amber-500'}`} />
                            <span className="text-[12px] font-medium truncate flex-1 text-foreground">{p.name}</span>
                            {!ok && <AlertCircle className="h-3 w-3 text-amber-500 shrink-0" />}
                          </div>
                          <div className="text-[10px] text-muted-foreground mt-0.5 truncate">
                            {p.session_count} session{p.session_count === 1 ? '' : 's'}
                            {p.last_synced_at && ` · synced ${new Date(p.last_synced_at).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })}`}
                          </div>
                        </div>
                      </Link>
                    );
                  })}
                </div>
              )}
            </section>

          </div>
        </div>
      )}
    </div>
  );
}

function SourcesDisclosure({ sources }: { sources: Source[] }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="mt-2 border-t border-border/50 pt-2">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-1.5 text-[11px] text-muted-foreground hover:text-foreground transition-colors"
      >
        <ChevronDown className={`h-3 w-3 transition-transform ${open ? '' : '-rotate-90'}`} />
        <FileText className="h-3 w-3" />
        <span>
          {sources.length} source{sources.length === 1 ? '' : 's'} from your sessions
        </span>
      </button>
      {open && (
        <div className="mt-2 space-y-1.5">
          {sources.map((s, idx) => (
            <div key={`${s.session_id}-${idx}`} className="rounded-md border border-border/60 bg-background/60 p-2">
              <div className="flex items-center justify-between gap-2 text-[10px] text-muted-foreground">
                <div className="flex items-center gap-1.5 min-w-0">
                  {s.role && <span className="uppercase tracking-wider">{s.role}</span>}
                  {s.project && <span className="truncate">{s.project}</span>}
                </div>
                <div className="flex items-center gap-1.5 shrink-0 tabular-nums">
                  {typeof s.similarity === 'number' && (
                    <span>{(s.similarity * 100).toFixed(0)}%</span>
                  )}
                  {s.timestamp && <span>{s.timestamp.slice(0, 10)}</span>}
                </div>
              </div>
              {s.excerpt && (
                <div className="mt-1 text-[11px] text-foreground/80 leading-relaxed line-clamp-3 break-words">
                  {s.excerpt}
                </div>
              )}
              <div className="mt-1 text-[10px] text-muted-foreground font-mono truncate">
                {s.session_id}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
