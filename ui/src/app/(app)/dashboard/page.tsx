'use client';

import { useState, useEffect, useCallback } from 'react';
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { AgCharts } from 'ag-charts-react';
import { ModuleRegistry, AllCommunityModule } from 'ag-charts-community';
import { useRouter } from 'next/navigation';
import { useTheme } from '@/components/ThemeProvider';
import { fetchApi, formatNumber, formatCost, formatDate, cleanProject } from '@/lib/api';
import { baseChartOptions, categoryAxis, valueAxis, getChartTokens } from '@/lib/agChartTheme';
import {
  MessageSquare, Wrench, Coins, FolderOpen, Hash, Activity,
} from 'lucide-react';

ModuleRegistry.registerModules([AllCommunityModule]);

interface Overview {
  summary: {
    total_sessions: number;
    total_messages: number;
    total_tool_calls: number;
    total_input_tokens: number;
    total_output_tokens: number;
    total_cost_usd: number;
  };
  projects: Array<{ project: string; sessions: number; messages: number; cost: number }>;
  top_tools: Array<{ tool_name: string; uses: number }>;
  recent_sessions: Array<{
    id: string; provider_id: string; project: string; title: string;
    started_at: string; message_count: number; estimated_cost_usd: number;
  }>;
}

interface DailyStats {
  day: string;
  sessions: number;
  messages: number;
  tool_calls: number;
  total_tokens: number;
  cost: number;
}

interface ProviderStats {
  provider_id: string;
  sessions: number;
  messages: number;
  tool_calls: number;
  input_tokens: number;
  output_tokens: number;
  cost: number;
  first_session: string;
  last_session: string;
}

const PROVIDER_LABELS: Record<string, string> = {
  'claude-code': 'Claude Code',
  'codex': 'OpenAI Codex CLI',
  'copilot': 'GitHub Copilot',
  'cursor': 'Cursor',
  'windsurf': 'Windsurf',
};

const PROVIDER_COLORS: Record<string, string> = {
  'claude-code': '#d97706',
  'codex': '#10b981',
  'copilot': '#6366f1',
  'cursor': '#06b6d4',
  'windsurf': '#ec4899',
};

export default function DashboardPage() {
  const router = useRouter();
  const { resolved } = useTheme();
  const [loading, setLoading] = useState(true);
  const [overview, setOverview] = useState<Overview | null>(null);
  const [daily, setDaily] = useState<DailyStats[]>([]);
  const [providers, setProviders] = useState<ProviderStats[]>([]);

  const fetchData = useCallback(async () => {
    try {
      const [ov, dl] = await Promise.all([
        fetchApi('/api/overview'),
        fetchApi('/api/daily?days=14'),
      ]);
      setOverview(ov);
      setDaily(dl);
      // Provider breakdown is optional — don't break dashboard if endpoint not available
      try { setProviders(await fetchApi('/api/stats/providers')); } catch {}
    } catch (e) {
      console.error('Failed to load dashboard:', e);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { fetchData(); }, [fetchData]);

  if (loading || !overview) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-6 w-6 border-2 border-primary border-t-transparent" />
      </div>
    );
  }

  const tokens = getChartTokens(resolved);
  const base = baseChartOptions(resolved);
  const s = overview.summary;
  const totalTokens = (s.total_input_tokens || 0) + (s.total_output_tokens || 0);

  const statCards = [
    { label: 'Sessions', value: s.total_sessions, icon: Activity },
    { label: 'Messages', value: s.total_messages, icon: MessageSquare },
    { label: 'Tool Calls', value: s.total_tool_calls, icon: Wrench },
    { label: 'Est. Tokens', value: totalTokens, icon: Hash },
    { label: 'Est. Cost', value: formatCost(s.total_cost_usd || 0), icon: Coins, raw: true },
    { label: 'Projects', value: overview.projects.length, icon: FolderOpen },
  ];

  // Stacked: messages (hero) on top of tool calls (muted). Direct-labeled via
  // tooltip on hover — the shared tokens pick up light/dark automatically.
  const chartOptions: any = {
    ...base,
    data: daily.map(d => ({
      day: new Date(d.day).toLocaleDateString('en-US', { month: 'short', day: 'numeric' }),
      messages: d.messages,
      toolCalls: d.tool_calls,
    })),
    series: [
      { type: 'bar', xKey: 'day', yKey: 'toolCalls', yName: 'Tool Calls', stacked: true, fill: tokens.muted, cornerRadius: 2 },
      { type: 'bar', xKey: 'day', yKey: 'messages', yName: 'Messages', stacked: true, fill: tokens.hero, cornerRadius: 3 },
    ],
    axes: [categoryAxis(resolved), valueAxis(resolved, { formatter: (p: any) => formatNumber(p.value) })],
    legend: {
      position: 'bottom',
      spacing: 16,
      item: {
        marker: { size: 8, shape: 'square' },
        label: { fontSize: 11, color: tokens.text },
        paddingX: 12,
      },
    },
  };

  return (
    <div className="space-y-5">
      <h1 className="text-lg font-semibold tracking-tight">Dashboard</h1>

      {/* Stat cards */}
      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
        {statCards.map((card) => {
          const Icon = card.icon;
          return (
            <Card key={card.label}>
              <CardContent className="pt-3 pb-3 px-3">
                <div className="flex items-center justify-between mb-1">
                  <span className="text-[11px] font-medium text-muted-foreground uppercase tracking-wider">{card.label}</span>
                  <Icon className="h-3.5 w-3.5 text-muted-foreground" />
                </div>
                <div className="text-xl font-semibold">
                  {card.raw ? card.value : formatNumber(card.value as number)}
                </div>
              </CardContent>
            </Card>
          );
        })}
      </div>
      <p className="text-[11px] text-muted-foreground -mt-2 mb-2">
        Cost is an estimate based on per-token API rates for each session&apos;s model. Actual spend depends on your subscription plan (Claude Max, Cursor Pro, Copilot Pro, etc.) and may differ.
      </p>

      {/* Daily activity chart */}
      {daily.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="text-sm font-medium text-foreground normal-case tracking-normal">Daily Activity</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="h-[240px] w-full overflow-hidden">
              <AgCharts options={{ ...chartOptions, width: undefined, height: 240 }} />
            </div>
          </CardContent>
        </Card>
      )}

      {/* Provider Breakdown */}
      {providers.length > 1 && (
        <Card>
          <CardHeader>
            <CardTitle className="text-sm font-medium text-foreground normal-case tracking-normal">Provider Breakdown</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
              {providers.map((p) => {
                const totalSessions = providers.reduce((sum, pv) => sum + pv.sessions, 0);
                const pct = totalSessions > 0 ? ((p.sessions / totalSessions) * 100).toFixed(0) : '0';
                const color = PROVIDER_COLORS[p.provider_id] || '#8b8b9e';
                return (
                  <div key={p.provider_id} className="border border-border rounded-lg p-3 space-y-2">
                    <div className="flex items-center justify-between">
                      <span className="text-[13px] font-medium">{PROVIDER_LABELS[p.provider_id] || p.provider_id}</span>
                      <Badge variant="secondary" className="text-[10px]">{pct}%</Badge>
                    </div>
                    <div className="h-1.5 bg-secondary rounded-full overflow-hidden">
                      <div className="h-full rounded-full" style={{ width: `${pct}%`, backgroundColor: color }} />
                    </div>
                    <div className="grid grid-cols-3 gap-2 text-center">
                      <div>
                        <div className="text-xs text-muted-foreground">Sessions</div>
                        <div className="text-sm font-semibold tabular-nums">{formatNumber(p.sessions)}</div>
                      </div>
                      <div>
                        <div className="text-xs text-muted-foreground">Messages</div>
                        <div className="text-sm font-semibold tabular-nums">{formatNumber(p.messages)}</div>
                      </div>
                      <div>
                        <div className="text-xs text-muted-foreground">Cost (est)</div>
                        <div className="text-sm font-semibold tabular-nums">{formatCost(p.cost || 0)}</div>
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          </CardContent>
        </Card>
      )}

      {/* Two column: Projects + Tools */}
      <div className="grid md:grid-cols-2 gap-4">
        <Card>
          <CardHeader>
            <CardTitle className="text-sm font-medium text-foreground normal-case tracking-normal">Projects</CardTitle>
          </CardHeader>
          <CardContent className="space-y-1">
            {overview.projects.slice(0, 8).map((p) => (
              <div key={p.project} className="flex items-center justify-between py-1.5 border-b border-border last:border-0">
                <div>
                  <div className="text-[13px] font-medium">{cleanProject(p.project)}</div>
                  <div className="text-[11px] text-muted-foreground">{p.sessions} sessions</div>
                </div>
                <span className="text-xs text-muted-foreground font-mono">{formatCost(p.cost || 0)}</span>
              </div>
            ))}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="text-sm font-medium text-foreground normal-case tracking-normal">Top Tools</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            {overview.top_tools.slice(0, 8).map((t) => {
              const maxUses = overview.top_tools[0]?.uses || 1;
              return (
                <div key={t.tool_name} className="flex items-center gap-3">
                  <span className="text-xs font-mono text-muted-foreground w-20 shrink-0 truncate">{t.tool_name}</span>
                  <div className="flex-1 h-1 bg-secondary rounded-full overflow-hidden">
                    <div className="h-full bg-primary/60 rounded-full" style={{ width: `${(t.uses / maxUses) * 100}%` }} />
                  </div>
                  <span className="text-[11px] text-muted-foreground w-10 text-right tabular-nums">{formatNumber(t.uses)}</span>
                </div>
              );
            })}
          </CardContent>
        </Card>
      </div>

      {/* Recent Sessions */}
      <Card>
        <CardHeader>
          <div className="flex items-center justify-between">
            <CardTitle className="text-sm font-medium text-foreground normal-case tracking-normal">Recent Sessions</CardTitle>
            <button onClick={() => router.push('/sessions')} className="text-[11px] text-primary font-medium hover:underline">
              View all
            </button>
          </div>
        </CardHeader>
        <CardContent>
          <div className="space-y-px">
            {overview.recent_sessions.slice(0, 8).map((r) => (
              <div
                key={r.id}
                onClick={() => router.push(`/sessions?id=${r.id}`)}
                className="flex items-center justify-between py-2 px-2 -mx-2 rounded-md hover:bg-accent cursor-pointer transition-colors"
              >
                <div className="min-w-0">
                  <div className="text-[13px] font-medium truncate">{(r.title || 'Untitled').slice(0, 60)}</div>
                  <div className="text-[11px] text-muted-foreground flex gap-2">
                    <span>{cleanProject(r.project || '')}</span>
                    <span>{formatDate(r.started_at)}</span>
                  </div>
                </div>
                <div className="flex gap-1.5 shrink-0 ml-4 items-center">
                  <Badge variant="outline" className="text-[10px]">{PROVIDER_LABELS[r.provider_id] || r.provider_id}</Badge>
                  <Badge variant="secondary">{r.message_count} msgs</Badge>
                  <span className="text-[11px] text-muted-foreground font-mono">{formatCost(r.estimated_cost_usd || 0)}</span>
                </div>
              </div>
            ))}
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
