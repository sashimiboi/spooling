'use client';

import { useEffect, useMemo, useState, useCallback, type JSX } from 'react';
import { useSearchParams } from 'next/navigation';
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs';
import { ArrowLeft, Activity, Bot, Wrench, Sparkles, AlertCircle, Play, CheckCircle2, XCircle, Package } from 'lucide-react';
import { cn } from '@/lib/utils';
import SpanTree, { Span, SpanBadges, VENDOR_COLORS } from '@/components/SpanTree';
import TraceConversation from '@/components/TraceConversation';
import { fetchApi, postApi, formatDate, cleanProject } from '@/lib/api';

interface TraceListRow {
  id: string;
  session_id: string;
  provider_id: string;
  project: string;
  title: string;
  started_at: string;
  duration_ms: number | null;
  span_count: number;
  agent_count: number;
  tool_count: number;
  llm_count: number;
  error_count: number;
  total_cost_usd: number;
  model: string | null;
}

interface CostBreakdown {
  input: number;
  output: number;
  cache_read: number;
  cache_write: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  model: string | null;
}

interface TraceDetail {
  trace: TraceListRow & {
    total_input_tokens: number;
    total_output_tokens: number;
    total_cache_read_tokens?: number;
    total_cache_write_tokens?: number;
    cwd: string | null;
    git_branch: string | null;
    cost_breakdown?: CostBreakdown;
  };
  spans: Span[];
  evals: Array<{ id: number; rubric_id: string; rubric_name: string; span_id: string | null; score: number | null; passed: boolean | null; label: string; rationale: string; run_at: string }>;
}

interface Rubric {
  id: string;
  name: string;
  description: string;
  kind: string;
  target_kind: string;
}

interface Summary {
  summary: {
    traces: number;
    spans: number;
    agents: number;
    tools: number;
    llm_calls: number;
    errors: number;
    cost: number;
  };
  top_agents: Array<{ agent_type: string; uses: number }>;
  top_tools: Array<{ tool_name: string; uses: number; errors: number }>;
  top_vendors: Array<{ vendor: string; category: string; uses: number; errors: number; traces: number }>;
}

function formatDuration(ms: number | null): string {
  if (ms === null || ms === undefined) return '—';
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  return `${(ms / 60_000).toFixed(1)}m`;
}

// Show up to 3 decimals but always at least 2, so 0.926 reads as "0.926"
// instead of collapsing to 0.93 (which made failing rubrics look like
// passing rubrics), while 0.93 stays "0.93" and 1 stays "1.00".
function formatScore(score: number): string {
  const fixed = score.toFixed(3);
  if (fixed.endsWith('0')) {
    const trimmed = fixed.slice(0, -1);
    return trimmed.endsWith('.') ? trimmed + '00' : trimmed;
  }
  return fixed;
}

type WindowKey = 'all' | '24h' | '7d' | '30d';
const WINDOW_DAYS: Record<WindowKey, number | null> = {
  all: null, '24h': 1, '7d': 7, '30d': 30,
};

export default function TracesPage() {
  const [loading, setLoading] = useState(true);
  const [traces, setTraces] = useState<TraceListRow[]>([]);
  const [totalTraces, setTotalTraces] = useState(0);
  const [selected, setSelected] = useState<TraceDetail | null>(null);
  const [selectedSpan, setSelectedSpan] = useState<Span | null>(null);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [rubrics, setRubrics] = useState<Rubric[]>([]);
  const [runningRubric, setRunningRubric] = useState<string | null>(null);
  const [messages, setMessages] = useState<Array<{ role: string; content: string; timestamp: string | null; tools_used: string | string[] | null; estimated_tokens: number }>>([]);

  // Filters
  const [search, setSearch] = useState('');
  const [providerFilter, setProviderFilter] = useState<string | null>(null);
  const [vendorFilter, setVendorFilter] = useState<string | null>(null);
  const [windowFilter, setWindowFilter] = useState<WindowKey>('all');

  const loadList = useCallback(async () => {
    try {
      const params = new URLSearchParams({ limit: '1000' });
      if (providerFilter) params.set('provider', providerFilter);
      if (vendorFilter) params.set('vendor', vendorFilter);
      if (WINDOW_DAYS[windowFilter]) params.set('since_days', String(WINDOW_DAYS[windowFilter]));
      const [list, sum, rbs] = await Promise.all([
        fetchApi(`/api/traces?${params.toString()}`),
        fetchApi('/api/observability/summary'),
        fetchApi('/api/evals/rubrics'),
      ]);
      // New shape: { rows, total, limit, offset }
      const rows = Array.isArray(list) ? list : (list?.rows || []);
      const total = Array.isArray(list) ? list.length : (list?.total ?? rows.length);
      setTraces(rows);
      setTotalTraces(total);
      setSummary(sum);
      setRubrics(rbs);
    } catch (e) { console.error(e); }
    finally { setLoading(false); }
  }, [providerFilter, vendorFilter, windowFilter]);

  useEffect(() => { loadList(); }, [loadList]);

  const availableProviders = useMemo(() => {
    const set = new Set<string>();
    traces.forEach((t) => t.provider_id && set.add(t.provider_id));
    return Array.from(set).sort();
  }, [traces]);

  const filteredTraces = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return traces;
    return traces.filter((t) => {
      const hay = `${t.title || ''} ${t.project || ''} ${t.session_id || ''} ${t.id} ${t.provider_id || ''} ${t.model || ''}`.toLowerCase();
      return hay.includes(q);
    });
  }, [traces, search]);

  const openTrace = useCallback(async (id: string) => {
    try {
      const detail: TraceDetail = await fetchApi(`/api/traces/${id}`);
      setSelected(detail);
      setSelectedSpan(detail.spans[0] || null);
      setMessages([]);
      // Fetch the legacy session messages so we can render the conversation.
      // Experiments-captured traces don't have messages yet; that's fine,
      // TraceConversation handles an empty list.
      try {
        const sess = await fetchApi(`/api/session/${detail.trace.session_id}`);
        if (sess && Array.isArray(sess.messages)) {
          setMessages(sess.messages);
        }
      } catch { /* messages not available */ }
    } catch (e) { console.error(e); }
  }, []);

  // Deep link: ?trace=<id> opens a specific trace, ?session=<id> looks up
  // the trace for that session and opens it. Used by the Sessions page's
  // "View trace" button so you can jump from a session's conversation to
  // its span tree and evals without losing context.
  const searchParams = useSearchParams();
  const traceParam = searchParams.get('trace');
  const sessionParam = searchParams.get('session');
  useEffect(() => {
    let cancelled = false;
    const deepLink = async () => {
      if (traceParam) {
        await openTrace(traceParam);
        return;
      }
      if (sessionParam) {
        try {
          const detail = await fetchApi(`/api/session/${sessionParam}/trace`);
          if (cancelled) return;
          if (detail && !('error' in detail) && detail.trace?.id) {
            await openTrace(detail.trace.id);
          } else {
            console.warn('[traces] no trace for session', sessionParam);
          }
        } catch (e) {
          console.error('[traces] session lookup failed:', e);
        }
      }
    };
    deepLink();
    return () => { cancelled = true; };
  }, [traceParam, sessionParam, openTrace]);

  const runRubric = async (rubricId: string) => {
    if (!selected) return;
    setRunningRubric(rubricId);
    try {
      await postApi('/api/evals/run', { rubric_id: rubricId, trace_id: selected.trace.id });
      const detail: TraceDetail = await fetchApi(`/api/traces/${selected.trace.id}`);
      setSelected(detail);
    } catch (e) { console.error(e); }
    finally { setRunningRubric(null); }
  };

  if (loading) {
    return <div className="flex items-center justify-center h-64">
      <div className="animate-spin rounded-full h-6 w-6 border-2 border-primary border-t-transparent" />
    </div>;
  }

  if (selected) {
    const t = selected.trace;
    return (
      <div className="space-y-4">
        <Button variant="ghost" size="sm" onClick={() => { setSelected(null); setSelectedSpan(null); }}>
          <ArrowLeft className="h-3.5 w-3.5 mr-1" /> Back to traces
        </Button>

        <div>
          {t.title ? (
            <>
              <h1 className="text-base font-semibold truncate">{t.title}</h1>
              <p className="text-[11px] text-muted-foreground font-mono">{t.id}</p>
            </>
          ) : (
            <h1 className="text-base font-semibold font-mono truncate">{t.id}</h1>
          )}
        </div>

        <div className="flex flex-wrap gap-5 text-[13px] py-3 px-4 rounded-lg bg-card border">
          {[
            ['Provider', t.provider_id],
            ['Project', cleanProject(t.project || '')],
            ['Duration', formatDuration(t.duration_ms)],
            ['Spans', t.span_count],
            ['Agents', t.agent_count],
            ['Tools', t.tool_count],
            ['LLM calls', t.llm_count],
            ['Errors', t.error_count],
          ].map(([label, val]) => (
            <div key={label as string}>
              <div className="text-[11px] text-muted-foreground mb-0.5">{label}</div>
              <div className="font-medium">{val}</div>
            </div>
          ))}
        </div>

        <Tabs defaultValue="conversation">
          <TabsList>
            <TabsTrigger value="conversation">Conversation ({messages.length})</TabsTrigger>
            <TabsTrigger value="tree">Span tree</TabsTrigger>
            <TabsTrigger value="evals">Evals ({selected.evals.length})</TabsTrigger>
          </TabsList>

          <TabsContent value="conversation" className="mt-3">
            <Card>
              <CardContent className="p-3">
                <TraceConversation messages={messages} spans={selected.spans} />
              </CardContent>
            </Card>
          </TabsContent>

          <TabsContent value="tree" className="mt-3">
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
              <Card className="overflow-hidden">
                <CardHeader>
                  <CardTitle className="text-sm">Spans</CardTitle>
                </CardHeader>
                <CardContent className="p-0 max-h-[70vh] overflow-auto scrollbar-thin">
                  <SpanTree
                    spans={selected.spans}
                    onSelect={setSelectedSpan}
                    selectedId={selectedSpan?.id || null}
                  />
                </CardContent>
              </Card>

              <Card>
                <CardHeader>
                  <CardTitle className="text-sm">
                    {selectedSpan ? `${selectedSpan.kind}: ${selectedSpan.name}` : 'Select a span'}
                  </CardTitle>
                </CardHeader>
                <CardContent>
                  {selectedSpan ? (
                    <div className="space-y-3 text-[12px]">
                      <SpanBadges span={selectedSpan} />
                      <div className="grid grid-cols-2 gap-2">
                        <div>
                          <div className="text-muted-foreground text-[11px]">Duration</div>
                          <div className="tabular-nums">{formatDuration(selectedSpan.duration_ms)}</div>
                        </div>
                        <div>
                          <div className="text-muted-foreground text-[11px]">Status</div>
                          <div>{selectedSpan.status}</div>
                        </div>
                        {selectedSpan.kind === 'llm_call' && (
                          <>
                            <div>
                              <div className="text-muted-foreground text-[11px]">Tokens in/out</div>
                              <div className="tabular-nums">{selectedSpan.input_tokens} / {selectedSpan.output_tokens}</div>
                            </div>
                            <div>
                              <div className="text-muted-foreground text-[11px]">Cache read/write</div>
                              <div className="tabular-nums">{selectedSpan.cache_read_tokens} / {selectedSpan.cache_write_tokens}</div>
                            </div>
                            <div>
                              <div className="text-muted-foreground text-[11px]">Model</div>
                              <div>{selectedSpan.model || '—'}</div>
                            </div>
                          </>
                        )}
                        {selectedSpan.kind === 'agent' && selectedSpan.agent_type && (
                          <div className="col-span-2">
                            <div className="text-muted-foreground text-[11px]">Agent type</div>
                            <div>{selectedSpan.agent_type}</div>
                          </div>
                        )}
                        {selectedSpan.kind === 'tool' && selectedSpan.tool_name && (
                          <div className="col-span-2">
                            <div className="text-muted-foreground text-[11px]">Tool</div>
                            <div>{selectedSpan.tool_name}</div>
                          </div>
                        )}
                      </div>
                      {selectedSpan.agent_prompt && (
                        <div>
                          <div className="text-muted-foreground text-[11px] mb-1">Agent prompt</div>
                          <pre className="whitespace-pre-wrap bg-secondary/50 p-2 rounded text-[11px] max-h-40 overflow-auto scrollbar-thin">{selectedSpan.agent_prompt}</pre>
                        </div>
                      )}
                      {selectedSpan.tool_input && (
                        <div>
                          <div className="text-muted-foreground text-[11px] mb-1">Tool input</div>
                          <pre className="whitespace-pre-wrap bg-secondary/50 p-2 rounded text-[11px] max-h-40 overflow-auto scrollbar-thin">{JSON.stringify(selectedSpan.tool_input, null, 2)}</pre>
                        </div>
                      )}
                      {selectedSpan.tool_output && (
                        <div>
                          <div className="text-muted-foreground text-[11px] mb-1">Tool output</div>
                          <pre className="whitespace-pre-wrap bg-secondary/50 p-2 rounded text-[11px] max-h-60 overflow-auto scrollbar-thin">{selectedSpan.tool_output}</pre>
                        </div>
                      )}
                    </div>
                  ) : (
                    <p className="text-[12px] text-muted-foreground">No span selected.</p>
                  )}
                </CardContent>
              </Card>
            </div>
          </TabsContent>

          <TabsContent value="evals" className="mt-3 space-y-4">
            <Card>
              <CardHeader>
                <CardTitle className="text-sm">Run a rubric</CardTitle>
              </CardHeader>
              <CardContent className="space-y-2">
                {rubrics.map((r) => (
                  <div key={r.id} className="flex items-center justify-between gap-3 p-2 rounded-md border">
                    <div className="min-w-0">
                      <div className="text-[13px] font-medium truncate">{r.name}</div>
                      <div className="text-[11px] text-muted-foreground truncate">{r.description}</div>
                    </div>
                    <div className="flex items-center gap-2 shrink-0">
                      <Badge variant="outline">{r.kind}</Badge>
                      <Badge variant="outline">{r.target_kind}</Badge>
                      <Button
                        size="sm"
                        variant="secondary"
                        onClick={() => runRubric(r.id)}
                        disabled={runningRubric === r.id}
                      >
                        <Play className="h-3 w-3 mr-1" />
                        {runningRubric === r.id ? 'Running...' : 'Run'}
                      </Button>
                    </div>
                  </div>
                ))}
              </CardContent>
            </Card>

            <Card>
              <CardHeader>
                <CardTitle className="text-sm">Results</CardTitle>
              </CardHeader>
              <CardContent>
                {selected.evals.length === 0 ? (
                  <p className="text-[12px] text-muted-foreground">No evals yet. Run a rubric above.</p>
                ) : (
                  <div className="space-y-2">
                    {selected.evals.map((e) => (
                      <div key={e.id} className="p-3 rounded-md border bg-card">
                        <div className="flex items-center justify-between mb-1">
                          <div className="flex items-center gap-2">
                            {e.passed === true
                              ? <CheckCircle2 className="h-3.5 w-3.5 text-emerald-500" />
                              : e.passed === false
                                ? <XCircle className="h-3.5 w-3.5 text-destructive" />
                                : <AlertCircle className="h-3.5 w-3.5 text-amber-500" />}
                            <span className="text-[13px] font-medium">{e.rubric_name}</span>
                          </div>
                          <div className="flex items-center gap-2 text-[11px] text-muted-foreground">
                            {e.score !== null && (
                              <span className={cn(
                                'tabular-nums',
                                e.passed === false && 'text-destructive',
                                e.passed === true && 'text-emerald-500',
                              )}>
                                {formatScore(Number(e.score))}
                              </span>
                            )}
                            {e.label && <Badge variant="outline">{e.label}</Badge>}
                          </div>
                        </div>
                        {e.rationale && <p className="text-[12px] text-muted-foreground">{e.rationale}</p>}
                      </div>
                    ))}
                  </div>
                )}
              </CardContent>
            </Card>
          </TabsContent>
        </Tabs>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex items-baseline justify-between">
        <h1 className="text-lg font-semibold tracking-tight flex items-center gap-2">
          <Activity className="h-5 w-5" /> Traces
        </h1>
        <span className="text-[11px] text-muted-foreground tabular-nums">
          {filteredTraces.length} of {totalTraces.toLocaleString()}
        </span>
      </div>

      {/* Filter toolbar */}
      <div className="flex flex-wrap items-center gap-2">
        <input
          type="text"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search title, project, session, branch..."
          className="flex-1 min-w-[220px] max-w-md h-9 px-3 rounded-md border bg-card text-[13px]"
        />
        {availableProviders.length > 1 && (
          <div className="flex items-center rounded-md border bg-card p-0.5 text-[12px]">
            <button
              onClick={() => setProviderFilter(null)}
              className={cn(
                'px-2.5 py-1 rounded transition-colors',
                providerFilter === null
                  ? 'bg-accent text-foreground font-medium'
                  : 'text-muted-foreground hover:text-foreground'
              )}
            >
              All
            </button>
            {availableProviders.map((p) => (
              <button
                key={p}
                onClick={() => setProviderFilter(providerFilter === p ? null : p)}
                className={cn(
                  'px-2.5 py-1 rounded transition-colors',
                  providerFilter === p
                    ? 'bg-accent text-foreground font-medium'
                    : 'text-muted-foreground hover:text-foreground'
                )}
              >
                {p}
              </button>
            ))}
          </div>
        )}
        <div className="flex items-center rounded-md border bg-card p-0.5 text-[12px]">
          {(['all', '24h', '7d', '30d'] as WindowKey[]).map((w) => (
            <button
              key={w}
              onClick={() => setWindowFilter(w)}
              className={cn(
                'px-2.5 py-1 rounded transition-colors',
                windowFilter === w
                  ? 'bg-accent text-foreground font-medium'
                  : 'text-muted-foreground hover:text-foreground'
              )}
            >
              {w === 'all' ? 'All time' : w}
            </button>
          ))}
        </div>
        {(search || providerFilter || vendorFilter || windowFilter !== 'all') && (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => { setSearch(''); setProviderFilter(null); setVendorFilter(null); setWindowFilter('all'); }}
            className="h-8 text-[12px] text-muted-foreground"
          >
            Clear
          </Button>
        )}
      </div>

      {summary && (
        <div className="grid grid-cols-2 md:grid-cols-6 gap-2">
          {[
            { label: 'Traces',    value: summary.summary.traces },
            { label: 'Spans',     value: summary.summary.spans },
            { label: 'Agents',    value: summary.summary.agents, icon: Bot },
            { label: 'Tools',     value: summary.summary.tools, icon: Wrench },
            { label: 'LLM calls', value: summary.summary.llm_calls, icon: Sparkles },
            { label: 'Errors',    value: summary.summary.errors, icon: AlertCircle },
          ].map((s) => {
            const Icon = s.icon;
            return (
              <Card key={s.label}>
                <CardContent className="p-3">
                  <div className="text-[11px] text-muted-foreground flex items-center gap-1">
                    {Icon && <Icon className="h-3 w-3" />}
                    {s.label}
                  </div>
                  <div className="text-lg font-semibold tabular-nums">{Number(s.value || 0).toLocaleString()}</div>
                </CardContent>
              </Card>
            );
          })}
        </div>
      )}

      {summary && summary.top_vendors && summary.top_vendors.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="text-sm flex items-center justify-between gap-2">
              <span className="flex items-center gap-2">
                <Package className="h-4 w-4" /> Top providers
              </span>
              {vendorFilter && (
                <button
                  onClick={() => setVendorFilter(null)}
                  className="text-[11px] font-normal text-muted-foreground hover:text-foreground underline underline-offset-2"
                >
                  Clear filter
                </button>
              )}
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="flex flex-wrap gap-2">
              {summary.top_vendors.map((v) => {
                const active = vendorFilter === v.vendor;
                return (
                  <button
                    key={`${v.vendor}-${v.category}`}
                    onClick={() => setVendorFilter(active ? null : v.vendor)}
                    className={cn(
                      'px-2.5 py-1 rounded border text-[12px] flex items-center gap-2 transition-all',
                      VENDOR_COLORS[v.vendor] || VENDOR_COLORS.unknown,
                      active
                        ? 'ring-2 ring-primary ring-offset-1 ring-offset-background'
                        : 'hover:brightness-125 opacity-90 hover:opacity-100',
                    )}
                    title={active ? `Showing traces that used ${v.vendor}. Click to clear.` : `Filter traces by ${v.vendor}`}
                  >
                    <span className="font-medium">{v.vendor}</span>
                    <span className="opacity-70 text-[11px]">{v.category}</span>
                    <span className="tabular-nums">· {v.uses}</span>
                    {v.errors > 0 && <span className="text-destructive tabular-nums">· {v.errors} err</span>}
                  </button>
                );
              })}
            </div>
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle className="text-sm flex items-center justify-between gap-2">
            <span>All traces</span>
            <span className="text-[11px] text-muted-foreground font-normal tabular-nums">
              showing {filteredTraces.length.toLocaleString()}
            </span>
          </CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          <div className="divide-y max-h-[70vh] overflow-auto scrollbar-thin">
            {filteredTraces.map((t) => (
              <div
                key={t.id}
                onClick={() => openTrace(t.id)}
                className="flex items-center gap-4 px-4 py-2.5 hover:bg-accent/50 cursor-pointer"
              >
                <div className="flex-1 min-w-0">
                  <div className={cn(
                    'text-[13px] truncate',
                    t.title ? 'font-medium' : 'font-mono text-muted-foreground',
                  )}>
                    {t.title || t.id}
                  </div>
                  <div className="text-[11px] text-muted-foreground truncate">
                    {t.provider_id} · {cleanProject(t.project || '')} · {formatDate(t.started_at)}
                  </div>
                </div>
                <div className="flex items-center gap-3 text-[11px] text-muted-foreground tabular-nums shrink-0">
                  <span>{t.span_count} spans</span>
                  <span>{t.agent_count > 0 && `${t.agent_count}a`}</span>
                  <span>{t.tool_count}t</span>
                  <span>{t.llm_count}l</span>
                  {t.error_count > 0 && <Badge variant="destructive">{t.error_count} err</Badge>}
                  <span>{formatDuration(t.duration_ms)}</span>
                </div>
              </div>
            ))}
            {filteredTraces.length === 0 && (
              <p className="p-6 text-[12px] text-muted-foreground text-center">
                {traces.length === 0
                  ? <>No traces yet. Run <code>spool sync</code> to ingest.</>
                  : 'No traces match your filters.'}
              </p>
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return n.toLocaleString();
}

