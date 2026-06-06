'use client';

import { useState, useEffect, useCallback, useMemo } from 'react';
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/card';
import { FilterSelect, type FilterOption } from '@/components/ui/filter-select';
import { AgCharts } from 'ag-charts-react';
import { ModuleRegistry as ChartsModuleRegistry, AllCommunityModule as ChartsAllCommunityModule } from 'ag-charts-community';
import { AgGridReact } from 'ag-grid-react';
import { ModuleRegistry as GridModuleRegistry, AllCommunityModule as GridAllCommunityModule, type ColDef } from 'ag-grid-community';
import { useTheme } from '@/components/ThemeProvider';
import { getGridTheme } from '@/lib/agGridTheme';
import { fetchApi, formatNumber, formatCost, cleanProject } from '@/lib/api';
import ActivityChart from '@/components/ActivityChart';
import {
  baseChartOptions,
  categoryAxis,
  valueAxis,
  donutSeries,
  getChartTokens,
  CATEGORICAL_PALETTE,
} from '@/lib/agChartTheme';

ChartsModuleRegistry.registerModules([ChartsAllCommunityModule]);
GridModuleRegistry.registerModules([GridAllCommunityModule]);

interface DailyStats {
  day: string; sessions: number; messages: number;
  tool_calls: number; total_tokens: number; cost: number;
}
interface Overview {
  summary: Record<string, number>;
  projects: Array<{ project: string; sessions: number; messages: number; cost: number }>;
  top_tools: Array<{ tool_name: string; uses: number }>;
}
interface ToolInfo {
  tool_name: string; uses: number; sessions: number;
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
  'codex': 'Codex CLI',
  'copilot': 'Copilot',
  'cursor': 'Cursor',
  'windsurf': 'Windsurf',
  'cortex-code': 'Cortex Code',
  'opencode': 'opencode',
};

const PROVIDER_COLORS: Record<string, string> = {
  'codex': '#10b981',
  'copilot': '#6366f1',
  'cursor': '#06b6d4',
  'windsurf': '#ec4899',
  'cortex-code': '#29b5e8',
  'opencode': '#14b8a6',
};

export default function AnalyticsPage() {
  const { resolved } = useTheme();
  const [loading, setLoading] = useState(true);
  const [overview, setOverview] = useState<Overview | null>(null);
  const [daily, setDaily] = useState<DailyStats[]>([]);
  const [tools, setTools] = useState<ToolInfo[]>([]);
  const [providers, setProviders] = useState<ProviderStats[]>([]);
  const [days, setDays] = useState(30);
  const [providerFilter, setProviderFilter] = useState<string>('all');

  const load = useCallback(async () => {
    try {
      const provParam = providerFilter !== 'all' ? `&provider=${providerFilter}` : '';
      const provQ = providerFilter !== 'all' ? `?provider=${providerFilter}` : '';
      const [ov, dl, tl] = await Promise.all([
        fetchApi(`/api/overview${provQ}`),
        fetchApi(`/api/daily?days=${days}${provParam}`),
        fetchApi(`/api/tools?limit=20${provParam}`),
      ]);
      setOverview(ov); setDaily(dl); setTools(tl);
      try { setProviders(await fetchApi('/api/stats/providers')); } catch {}
    } catch (e) { console.error(e); }
    finally { setLoading(false); }
  }, [days, providerFilter]);

  useEffect(() => { load(); }, [load]);

  const gridTheme = useMemo(() => getGridTheme(resolved), [resolved]);
  const tokens = getChartTokens(resolved);
  const base = baseChartOptions(resolved);

  // --- ag-grid: Daily Breakdown ---
  const dailyColDefs = useMemo<ColDef[]>(() => [
    {
      field: 'day', headerName: 'Date', sortable: true, filter: true,
      valueFormatter: (p: any) => p.value ? new Date(p.value).toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' }) : '',
      flex: 1.5, minWidth: 140,
    },
    { field: 'sessions', headerName: 'Sessions', sortable: true, filter: 'agNumberColumnFilter', type: 'rightAligned', flex: 1, minWidth: 90 },
    { field: 'messages', headerName: 'Messages', sortable: true, filter: 'agNumberColumnFilter', type: 'rightAligned', flex: 1, minWidth: 90 },
    { field: 'tool_calls', headerName: 'Tools', sortable: true, filter: 'agNumberColumnFilter', type: 'rightAligned', flex: 1, minWidth: 80 },
    {
      field: 'total_tokens', headerName: 'Tokens', sortable: true, filter: 'agNumberColumnFilter', type: 'rightAligned', flex: 1, minWidth: 100,
      valueFormatter: (p: any) => formatNumber(p.value || 0),
    },
  ], []);

  // --- ag-grid: Provider Breakdown ---
  const providerColDefs = useMemo<ColDef[]>(() => {
    const totalSessions = providers.reduce((sum, p) => sum + p.sessions, 0);
    return [
      {
        field: 'provider_id', headerName: 'Provider', sortable: true, filter: true, flex: 1.5, minWidth: 140,
        valueFormatter: (p: any) => PROVIDER_LABELS[p.value] || p.value,
        cellRenderer: (p: any) => {
          const color = PROVIDER_COLORS[p.value] || '#8b8b9e';
          const label = PROVIDER_LABELS[p.value] || p.value;
          return (
            <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              <div style={{ width: 8, height: 8, borderRadius: '50%', background: color, flexShrink: 0 }} />
              <span>{label}</span>
            </div>
          );
        },
      },
      { field: 'sessions', headerName: 'Sessions', sortable: true, filter: 'agNumberColumnFilter', type: 'rightAligned', flex: 1, minWidth: 90 },
      {
        field: 'messages', headerName: 'Messages', sortable: true, filter: 'agNumberColumnFilter', type: 'rightAligned', flex: 1, minWidth: 100,
        valueFormatter: (p: any) => formatNumber(p.value || 0),
      },
      {
        headerName: 'Tokens', sortable: true, filter: 'agNumberColumnFilter', type: 'rightAligned', flex: 1, minWidth: 100,
        valueGetter: (p: any) => (p.data?.input_tokens || 0) + (p.data?.output_tokens || 0),
        valueFormatter: (p: any) => formatNumber(p.value || 0),
      },
      {
        headerName: 'Share', sortable: true, type: 'rightAligned', flex: 1, minWidth: 80,
        valueGetter: (p: any) => totalSessions > 0 ? (p.data?.sessions / totalSessions) * 100 : 0,
        valueFormatter: (p: any) => `${(p.value || 0).toFixed(1)}%`,
      },
    ];
  }, [providers]);

  const providerRowData = useMemo(() => providers, [providers]);

  if (loading || !overview) {
    return <div className="flex items-center justify-center h-64"><div className="animate-spin rounded-full h-6 w-6 border-2 border-primary border-t-transparent" /></div>;
  }

  // Summary cards follow the selected date window so flipping 7d/30d/90d
  // actually updates the numbers. `overview.summary` is all-time, which is
  // what we used to read from here — we keep it as a fallback only if the
  // window is "all" (no days filter).
  const rollup = daily.reduce(
    (acc, d) => {
      acc.sessions += Number(d.sessions) || 0;
      acc.messages += Number(d.messages) || 0;
      acc.tool_calls += Number(d.tool_calls) || 0;
      acc.tokens += Number(d.total_tokens) || 0;
      acc.cost += Number(d.cost) || 0;
      return acc;
    },
    { sessions: 0, messages: 0, tool_calls: 0, tokens: 0, cost: 0 },
  );
  const s = {
    total_sessions: rollup.sessions,
    total_messages: rollup.messages,
    total_tool_calls: rollup.tool_calls,
    total_input_tokens: rollup.tokens,  // combined in/out
    total_output_tokens: 0,
    total_cost_usd: rollup.cost,
  };

  const projectData = [...overview.projects]
    .sort((a, b) => b.messages - a.messages)
    .slice(0, 8)
    .map(p => ({ project: cleanProject(p.project), messages: p.messages }));

  const toolChartData = [...tools]
    .sort((a, b) => b.uses - a.uses)
    .slice(0, 10)
    .map(t => ({ tool: t.tool_name, uses: t.uses }));

  const tokenChart: any = {
    ...base,
    data: daily.map(d => ({
      day: new Date(d.day).toLocaleDateString('en-US', { month: 'short', day: 'numeric' }),
      tokens: d.total_tokens,
    })),
    series: [
      {
        type: 'area',
        xKey: 'day',
        yKey: 'tokens',
        yName: 'Tokens',
        fill: tokens.hero,
        fillOpacity: 0.12,
        stroke: tokens.hero,
        strokeWidth: 2,
        interpolation: { type: 'smooth' },
        marker: { enabled: false },
      },
    ],
    axes: [categoryAxis(resolved, { rotation: -45 }), valueAxis(resolved, { formatter: (p: any) => formatNumber(p.value) })],
    legend: { enabled: false },
  };

  const totalProjectMessages = projectData.reduce((sum, p) => sum + p.messages, 0);
  const projectChart: any = {
    ...base,
    data: projectData,
    series: [
      {
        type: 'bar',
        xKey: 'project',
        yKey: 'messages',
        yName: 'Messages',
        // Hero = biggest project; rest muted so the eye lands on the leader.
        fill: tokens.hero,
        itemStyler: ({ datum }: any) => ({
          fill: datum.project === projectData[0]?.project ? tokens.hero : tokens.muted,
        }),
        cornerRadius: 3,
        label: {
          enabled: true,
          color: tokens.text,
          fontSize: 10,
          formatter: ({ value }: any) => formatNumber(value),
          placement: 'outside-end',
        },
      },
    ],
    axes: [categoryAxis(resolved, { rotation: -30 }), valueAxis(resolved, { formatter: (p: any) => formatNumber(p.value) })],
    legend: { enabled: false },
  };

  const totalToolUses = toolChartData.reduce((sum, t) => sum + t.uses, 0);
  const toolPieChart: any = {
    ...base,
    data: toolChartData,
    series: [
      donutSeries({
        angleKey: 'uses',
        labelKey: 'tool',
        fills: CATEGORICAL_PALETTE,
        centerTitle: 'tool calls',
        centerValue: formatNumber(totalToolUses),
        resolved,
      }),
    ],
    legend: { enabled: false },
  };

  const toolBarChart: any = {
    ...base,
    data: toolChartData,
    series: [
      {
        type: 'bar',
        direction: 'horizontal',
        xKey: 'tool',
        yKey: 'uses',
        yName: 'Uses',
        fill: tokens.hero,
        itemStyler: ({ datum }: any) => ({
          fill: datum.tool === toolChartData[0]?.tool ? tokens.hero : tokens.muted,
        }),
        cornerRadius: 3,
        label: {
          enabled: true,
          color: tokens.text,
          fontSize: 10,
          formatter: ({ value }: any) => formatNumber(value),
          placement: 'outside-end',
        },
      },
    ],
    axes: [categoryAxis(resolved, { position: 'left' }), valueAxis(resolved, { position: 'bottom', formatter: (p: any) => formatNumber(p.value) })],
    legend: { enabled: false },
  };

  const totalProviderSessions = providers.reduce((sum, p) => sum + p.sessions, 0);
  const providerPieChart: any = providers.length > 1 ? {
    ...base,
    data: providers.map(p => ({
      provider: PROVIDER_LABELS[p.provider_id] || p.provider_id,
      sessions: p.sessions,
    })),
    series: [
      donutSeries({
        angleKey: 'sessions',
        labelKey: 'provider',
        fills: providers.map(p => PROVIDER_COLORS[p.provider_id] || '#8b8b9e'),
        centerTitle: 'sessions',
        centerValue: formatNumber(totalProviderSessions),
        resolved,
      }),
    ],
    legend: { enabled: false },
  } : null;

  const activeLabel = providerFilter === 'all' ? 'All Providers' : (PROVIDER_LABELS[providerFilter] || providerFilter);
  const dailyGridHeight = Math.min(daily.length, 15) * 34 + 46;

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <h1 className="text-lg font-semibold tracking-tight">Analytics</h1>
        {providers.length > 1 && (
          <FilterSelect
            label="Provider"
            value={providerFilter}
            onChange={(v) => { setProviderFilter(v); setLoading(true); }}
            options={[
              { value: 'all', label: 'All providers' } as FilterOption,
              ...providers.map((p) => ({
                value: p.provider_id,
                label: PROVIDER_LABELS[p.provider_id] || p.provider_id,
                color: PROVIDER_COLORS[p.provider_id],
                hint: `${p.sessions}`,
              })),
            ]}
            align="end"
          />
        )}
      </div>

      {/* Summary */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        {[
          { label: 'Token cost', value: formatCost(s.total_cost_usd || 0) },
          { label: 'Sessions', value: formatNumber(s.total_sessions || 0) },
          { label: 'Messages', value: formatNumber(s.total_messages || 0) },
          { label: 'Tokens',   value: formatNumber((s.total_input_tokens || 0) + (s.total_output_tokens || 0)) },
        ].map(c => (
          <Card key={c.label}>
            <CardContent className="pt-3 pb-3 px-3">
              <div className="text-[11px] font-medium text-muted-foreground uppercase tracking-wider">{c.label}</div>
              <div className="text-xl font-semibold mt-0.5">{c.value}</div>
            </CardContent>
          </Card>
        ))}
      </div>

      {/* Provider Breakdown */}
      {providers.length > 1 && providerPieChart && (
        <div className="grid md:grid-cols-3 gap-4">
          <Card className="md:col-span-1">
            <CardHeader>
              <CardTitle className="text-sm font-medium text-foreground normal-case tracking-normal">Sessions by Provider</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="h-[240px] w-full overflow-hidden">
                <AgCharts options={{ ...providerPieChart, width: undefined, height: 240 }} />
              </div>
            </CardContent>
          </Card>
          <Card className="md:col-span-2">
            <CardHeader>
              <CardTitle className="text-sm font-medium text-foreground normal-case tracking-normal">Provider Breakdown</CardTitle>
            </CardHeader>
            <CardContent>
              <div style={{ height: providers.length * 34 + 46, width: '100%' }}>
                <AgGridReact
                  theme={gridTheme}
                  columnDefs={providerColDefs}
                  rowData={providerRowData}
                  domLayout="normal"
                  headerHeight={36}
                  rowHeight={34}
                  suppressMovableColumns
                  defaultColDef={{ flex: 1, minWidth: 80, resizable: true }}
                  enableCellTextSelection
                  ensureDomOrder
                />
              </div>
            </CardContent>
          </Card>
        </div>
      )}

      {/* Primary activity chart — CGM-inspired pill filters + line chart */}
      <ActivityChart
        data={daily}
        days={days}
        onDaysChange={(d) => { setDays(d); setLoading(true); }}
      />

      <div className="grid md:grid-cols-2 gap-4">
        <Card>
          <CardHeader><CardTitle className="text-sm font-medium text-foreground normal-case tracking-normal">Token Usage</CardTitle></CardHeader>
          <CardContent>
            <div className="h-[260px] w-full overflow-hidden">
              <AgCharts options={{ ...tokenChart, width: undefined, height: 260 }} />
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardHeader><CardTitle className="text-sm font-medium text-foreground normal-case tracking-normal">Messages by Project</CardTitle></CardHeader>
          <CardContent>
            <div className="h-[260px] w-full overflow-hidden">
              <AgCharts options={{ ...projectChart, width: undefined, height: 260 }} />
            </div>
          </CardContent>
        </Card>
      </div>

      <div className="grid md:grid-cols-2 gap-4">
        <Card>
          <CardHeader><CardTitle className="text-sm font-medium text-foreground normal-case tracking-normal">Tool Distribution</CardTitle></CardHeader>
          <CardContent>
            <div className="h-[280px] w-full overflow-hidden">
              <AgCharts options={{ ...toolPieChart, width: undefined, height: 280 }} />
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardHeader><CardTitle className="text-sm font-medium text-foreground normal-case tracking-normal">Tool Usage (Top 10)</CardTitle></CardHeader>
          <CardContent>
            <div className="h-[280px] w-full overflow-hidden">
              <AgCharts options={{ ...toolBarChart, width: undefined, height: 280 }} />
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Daily Breakdown - ag-grid */}
      <Card>
        <CardHeader><CardTitle className="text-sm font-medium text-foreground normal-case tracking-normal">Daily Breakdown</CardTitle></CardHeader>
        <CardContent>
          <div style={{ height: dailyGridHeight, width: '100%' }}>
            <AgGridReact
              theme={gridTheme}
              columnDefs={dailyColDefs}
              rowData={daily}
              domLayout="normal"
              headerHeight={36}
              rowHeight={34}
              pagination={daily.length > 15}
              paginationPageSize={15}
              paginationPageSizeSelector={[15, 30, 50]}
              suppressMovableColumns
              defaultColDef={{ flex: 1, minWidth: 80, resizable: true, sortable: true }}
              enableCellTextSelection
              ensureDomOrder
            />
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
