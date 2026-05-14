'use client';

import { useEffect, useState, useCallback, useMemo } from 'react';
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { FilterSelect, type FilterOption } from '@/components/ui/filter-select';
import { CheckCircle2, XCircle, AlertCircle, Play, ClipboardList, Calendar } from 'lucide-react';
import { cn } from '@/lib/utils';
import { fetchApi, postApi, formatDate, cleanProject } from '@/lib/api';
import { useSearchParams } from 'next/navigation';

interface Rubric {
  id: string;
  name: string;
  description: string;
  kind: string;          // function | llm_judge
  target_kind: string;   // trace | span
  config: Record<string, unknown>;
}

interface EvalRow {
  id: number;
  rubric_id: string;
  rubric_name: string;
  trace_id: string | null;
  span_id: string | null;
  score: number | string | null;
  passed: boolean | null;
  label: string | null;
  rationale: string | null;
  run_at: string;
  judge_model: string | null;
  session_id: string | null;
  provider_id: string | null;
  project: string | null;
  trace_title: string | null;
}

type Window = 'all' | '24h' | '7d' | '30d';
const WINDOWS: Array<{ key: Window; label: string; days: number | null }> = [
  { key: 'all', label: 'All time', days: null },
  { key: '24h', label: '24h', days: 1 },
  { key: '7d',  label: '7 days', days: 7 },
  { key: '30d', label: '30 days', days: 30 },
];

function scoreColor(score: number | null | undefined): string {
  if (score === null || score === undefined) return 'text-muted-foreground';
  if (score >= 0.9) return 'text-emerald-500';
  if (score >= 0.7) return 'text-amber-500';
  return 'text-destructive';
}

function StatusIcon({ passed }: { passed: boolean | null }) {
  if (passed === true) return <CheckCircle2 className="h-3.5 w-3.5 text-emerald-500 shrink-0" />;
  if (passed === false) return <XCircle className="h-3.5 w-3.5 text-destructive shrink-0" />;
  return <AlertCircle className="h-3.5 w-3.5 text-amber-500 shrink-0" />;
}

export default function EvalsPage() {
  const [loading, setLoading] = useState(true);
  const [rubrics, setRubrics] = useState<Rubric[]>([]);
  const [evals, setEvals] = useState<EvalRow[]>([]);
  const [totalEvals, setTotalEvals] = useState(0);
  const [rubricFilter, setRubricFilter] = useState<string | null>(null);
  const [runningBulk, setRunningBulk] = useState<string | null>(null);
  const [bulkWindow, setBulkWindow] = useState<Window>('7d');
  const [lastRun, setLastRun] = useState<string | null>(null);

  // List filters (separate from the bulk-run window)
  const [search, setSearch] = useState('');
  const [passFilter, setPassFilter] = useState<'all' | 'passed' | 'failed' | 'null'>('all');
  const [providerFilter, setProviderFilter] = useState<string | null>(null);
  const [projectFilter, setProjectFilter] = useState<string | null>(null);
  const [listWindow, setListWindow] = useState<Window>('all');

  const searchParams = useSearchParams();
  const projectParam = searchParams.get('project');
  useEffect(() => { setProjectFilter(projectParam || null); }, [projectParam]);

  const loadAll = useCallback(async () => {
    try {
      const params = new URLSearchParams({ limit: '2000' });
      if (listWindow !== 'all') {
        const days = WINDOWS.find(w => w.key === listWindow)?.days;
        if (days) params.set('since_days', String(days));
      }
      if (providerFilter) params.set('provider', providerFilter);
      if (passFilter === 'passed') params.set('passed', 'true');
      else if (passFilter === 'failed') params.set('passed', 'false');
      else if (passFilter === 'null') params.set('passed', 'null');
      const [rbs, evs] = await Promise.all([
        fetchApi('/api/evals/rubrics'),
        fetchApi(`/api/evals?${params.toString()}`),
      ]);
      setRubrics(rbs);
      const rows = Array.isArray(evs) ? evs : (evs?.rows || []);
      const total = Array.isArray(evs) ? evs.length : (evs?.total ?? rows.length);
      setEvals(rows);
      setTotalEvals(total);
    } catch (e) { console.error(e); }
    finally { setLoading(false); }
  }, [listWindow, providerFilter, passFilter]);

  useEffect(() => { loadAll(); }, [loadAll]);

  const runBulk = async (rubricId: string) => {
    setRunningBulk(rubricId);
    setLastRun(null);
    try {
      const w = WINDOWS.find(x => x.key === bulkWindow);
      const body: Record<string, unknown> = { rubric_id: rubricId };
      if (w?.days) body.days = w.days;
      const result = await postApi('/api/evals/run', body);
      setLastRun(`${rubricId}: scored ${result.scored}/${result.traces} traces`);
      await loadAll();
    } catch (e) {
      console.error(e);
      setLastRun(`${rubricId}: error`);
    } finally {
      setRunningBulk(null);
    }
  };

  // Aggregate: per-rubric stats (runs, avg score, pass rate)
  const rubricStats = rubrics.map(r => {
    const runs = evals.filter(e => e.rubric_id === r.id);
    const scored = runs.filter(e => e.score !== null).map(e => Number(e.score));
    const avg = scored.length ? scored.reduce((a, b) => a + b, 0) / scored.length : null;
    const passed = runs.filter(e => e.passed === true).length;
    const failed = runs.filter(e => e.passed === false).length;
    const passRate = passed + failed > 0 ? passed / (passed + failed) : null;
    const latest = runs[0] || null;
    return { rubric: r, runs: runs.length, avg, passRate, passed, failed, latest };
  });

  const availableProviders = useMemo(() => {
    const set = new Set<string>();
    evals.forEach((e) => e.provider_id && set.add(e.provider_id));
    return Array.from(set).sort();
  }, [evals]);

  const availableProjects = useMemo(() => {
    const counts = new Map<string, number>();
    evals.forEach((e) => {
      const p = e.project || '';
      if (!p) return;
      counts.set(p, (counts.get(p) || 0) + 1);
    });
    return Array.from(counts.entries())
      .sort((a, b) => b[1] - a[1])
      .map(([project, n]) => ({ project, count: n }));
  }, [evals]);

  const filteredEvals = useMemo(() => {
    const q = search.trim().toLowerCase();
    return evals.filter((e) => {
      if (rubricFilter && e.rubric_id !== rubricFilter) return false;
      if (projectFilter && e.project !== projectFilter) return false;
      if (q) {
        const hay = `${e.trace_id || ''} ${e.session_id || ''} ${e.project || ''} ${e.label || ''} ${e.rationale || ''} ${e.rubric_name || e.rubric_id}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });
  }, [evals, rubricFilter, projectFilter, search]);

  if (loading) {
    return <div className="flex items-center justify-center h-64">
      <div className="animate-spin rounded-full h-6 w-6 border-2 border-primary border-t-transparent" />
    </div>;
  }

  return (
    <div className="space-y-4">
      <div className="flex items-baseline justify-between">
        <h1 className="text-lg font-semibold tracking-tight flex items-center gap-2">
          <ClipboardList className="h-5 w-5" /> Evals
        </h1>
        <span className="text-[11px] text-muted-foreground tabular-nums">
          {rubrics.length} rubrics · {evals.length} runs
        </span>
      </div>

      {/* Overview grid */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
        {rubricStats.map(({ rubric, runs, avg, passRate, passed, failed, latest }) => (
          <Card key={rubric.id} className="overflow-hidden">
            <CardHeader className="pb-2">
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <CardTitle className="text-sm truncate">{rubric.name}</CardTitle>
                  <p className="text-[11px] text-muted-foreground truncate">{rubric.description}</p>
                </div>
                <div className="flex flex-col items-end gap-1 shrink-0">
                  <Badge variant="outline">{rubric.kind}</Badge>
                  <Badge variant="outline">{rubric.target_kind}</Badge>
                </div>
              </div>
            </CardHeader>
            <CardContent className="space-y-2.5">
              <div className="grid grid-cols-3 gap-2 text-[12px]">
                <div>
                  <div className="text-[11px] text-muted-foreground">Runs</div>
                  <div className="tabular-nums font-medium">{runs}</div>
                </div>
                <div>
                  <div className="text-[11px] text-muted-foreground">Avg score</div>
                  <div className={cn('tabular-nums font-medium', scoreColor(avg))}>
                    {avg !== null ? avg.toFixed(2) : '—'}
                  </div>
                </div>
                <div>
                  <div className="text-[11px] text-muted-foreground">Pass rate</div>
                  <div className={cn('tabular-nums font-medium',
                    passRate === null ? 'text-muted-foreground'
                    : passRate >= 0.9 ? 'text-emerald-500'
                    : passRate >= 0.7 ? 'text-amber-500'
                    : 'text-destructive')}>
                    {passRate !== null ? `${Math.round(passRate * 100)}%` : '—'}
                  </div>
                </div>
              </div>

              {(passed > 0 || failed > 0) && (
                <div className="flex gap-1 text-[11px]">
                  {passed > 0 && <Badge variant="secondary">{passed} passed</Badge>}
                  {failed > 0 && <Badge variant="destructive">{failed} failed</Badge>}
                </div>
              )}

              {latest && (
                <div className="text-[11px] text-muted-foreground flex items-center gap-1">
                  <Calendar className="h-3 w-3" /> Last run {formatDate(latest.run_at)}
                </div>
              )}

              <div className="flex items-center gap-2 pt-1">
                <Button
                  size="sm"
                  variant="secondary"
                  disabled={runningBulk === rubric.id}
                  onClick={() => runBulk(rubric.id)}
                  className="flex-1"
                >
                  <Play className="h-3 w-3 mr-1" />
                  {runningBulk === rubric.id ? 'Running...' : `Run on ${bulkWindow === 'all' ? 'all' : bulkWindow}`}
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => setRubricFilter(rubricFilter === rubric.id ? null : rubric.id)}
                >
                  {rubricFilter === rubric.id ? 'Clear' : 'Filter'}
                </Button>
              </div>
            </CardContent>
          </Card>
        ))}
      </div>

      {/* Bulk-run window selector */}
      <div className="flex items-center gap-3">
        <FilterSelect
          label="Run window"
          value={bulkWindow}
          onChange={(v) => setBulkWindow(v as Window)}
          options={WINDOWS.map(w => ({ value: w.key, label: w.label }))}
        />
        {lastRun && (
          <span className="text-[12px] text-muted-foreground">{lastRun}</span>
        )}
      </div>

      {/* List filter toolbar */}
      <div className="flex flex-wrap items-center gap-2 pt-1">
        <input
          type="text"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search session, trace, project, label, rationale..."
          className="flex-1 min-w-[240px] max-w-md h-9 px-3 rounded-md border bg-card text-[13px]"
        />
        <FilterSelect
          label="Status"
          value={passFilter}
          onChange={(v) => setPassFilter(v as typeof passFilter)}
          options={[
            { value: 'all', label: 'All' },
            { value: 'passed', label: 'Passed' },
            { value: 'failed', label: 'Failed' },
            { value: 'null', label: 'Skipped' },
          ]}
        />
        {availableProviders.length > 1 && (
          <FilterSelect
            label="Provider"
            value={providerFilter ?? 'all'}
            onChange={(v) => setProviderFilter(v === 'all' ? null : v)}
            options={[
              { value: 'all', label: 'All providers' } as FilterOption,
              ...availableProviders.map((p) => ({ value: p, label: p })),
            ]}
          />
        )}
        {availableProjects.length > 1 && (
          <FilterSelect
            label="Project"
            value={projectFilter ?? 'all'}
            onChange={(v) => setProjectFilter(v === 'all' ? null : v)}
            options={[
              { value: 'all', label: 'All projects' } as FilterOption,
              ...availableProjects.map((p) => ({
                value: p.project,
                label: cleanProject(p.project),
                hint: String(p.count),
              })),
            ]}
          />
        )}
        <FilterSelect
          label="Window"
          value={listWindow}
          onChange={(v) => setListWindow(v as Window)}
          options={WINDOWS.map(w => ({ value: w.key, label: w.label }))}
        />
        {(search || rubricFilter || providerFilter || projectFilter || passFilter !== 'all' || listWindow !== 'all') && (
          <Button
            variant="ghost"
            size="sm"
            onClick={() => { setSearch(''); setRubricFilter(null); setProviderFilter(null); setProjectFilter(null); setPassFilter('all'); setListWindow('all'); }}
            className="h-8 text-[12px] text-muted-foreground"
          >
            Clear
          </Button>
        )}
      </div>

      {/* Recent runs table */}
      <Card>
        <CardHeader>
          <CardTitle className="text-sm flex items-center justify-between gap-2">
            <span>All eval runs {rubricFilter && `· ${rubricFilter}`}</span>
            <span className="text-[11px] text-muted-foreground font-normal tabular-nums">
              {filteredEvals.length.toLocaleString()} of {totalEvals.toLocaleString()}
            </span>
          </CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          <div className="divide-y max-h-[70vh] overflow-auto scrollbar-thin">
            {filteredEvals.length === 0 ? (
              <p className="p-6 text-[12px] text-muted-foreground text-center">
                {evals.length === 0
                  ? <>No eval runs yet. Pick a rubric above and click <strong>Run</strong>.</>
                  : 'No eval runs match your filters.'}
              </p>
            ) : (
              filteredEvals.map(e => (
                <a
                  key={e.id}
                  href={e.trace_id ? `/traces?trace=${e.trace_id}` : '#'}
                  className="flex items-center gap-3 px-4 py-2.5 hover:bg-accent/50 cursor-pointer"
                >
                  <StatusIcon passed={e.passed} />
                  <div className="min-w-0 flex-1">
                    <div className="text-[12px] font-medium truncate flex items-center gap-1.5">
                      {e.rubric_name || e.rubric_id}
                      {e.provider_id && <Badge variant="outline" className="text-[10px]">{e.provider_id}</Badge>}
                    </div>
                    <div className="text-[11px] text-muted-foreground truncate font-mono">
                      {e.session_id ? `session ${e.session_id.slice(0, 20)}` : (e.trace_id || e.span_id || '')}
                      {e.project && <span className="ml-2 opacity-70">· {e.project}</span>}
                    </div>
                  </div>
                  <div className="flex items-center gap-3 text-[11px] tabular-nums shrink-0">
                    {e.score !== null && (
                      <span className={cn('font-medium', scoreColor(Number(e.score)))}>
                        {Number(e.score).toFixed(2)}
                      </span>
                    )}
                    {e.label && <Badge variant="outline">{e.label}</Badge>}
                    <span className="text-muted-foreground">{formatDate(e.run_at)}</span>
                  </div>
                </a>
              ))
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
