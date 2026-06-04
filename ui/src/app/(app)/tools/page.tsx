'use client';

import { useEffect, useMemo, useState, useCallback } from 'react';
import { useRouter } from 'next/navigation';
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Wrench, Bot, Sparkles, Plug, CheckCircle2, XCircle, AlertCircle,
  Server, ExternalLink, Plus, ArrowRight, Activity,
} from 'lucide-react';
import { cn } from '@/lib/utils';
import { fetchApi, formatNumber } from '@/lib/api';

// --- Types ---

interface ChatAgent {
  name: string; role: string; provider: string; model: string;
  connected: boolean; ollama_url?: string | null; has_key?: boolean | null;
  purpose: string; endpoint: string;
}
interface JudgeAgent extends ChatAgent { note?: string | null }
interface McpServerAgent {
  name: string; role: string; transport: string; tools: string[];
  url: string; host: string; port: number; purpose: string; connected: boolean;
}
interface AgentsResponse {
  chat: ChatAgent;
  judge: JudgeAgent;
  mcp: McpServerAgent;
  ollama: { status: string; url: string; models: string[] };
}

interface Connector {
  id: string;
  name: string;
  url: string;
  transport: string;
  status: 'connected' | 'disconnected' | 'error';
  last_error: string | null;
  last_checked_at: string | null;
  has_auth: boolean;
}

interface VendorUsage {
  vendor: string;
  category: string;
  uses: number;
  errors: number;
  traces: number;
}
interface ToolUsage {
  tool_name: string;
  uses: number;
  errors: number;
}
interface AgentUsage {
  agent_type: string;
  uses: number;
}
interface ObservabilitySummary {
  summary: Record<string, number>;
  top_agents: AgentUsage[];
  top_tools: ToolUsage[];
  top_vendors: VendorUsage[];
}

// --- Page ---

export default function ToolsPage() {
  const router = useRouter();
  const [loading, setLoading] = useState(true);
  const [agents, setAgents] = useState<AgentsResponse | null>(null);
  const [connectors, setConnectors] = useState<Connector[]>([]);
  const [summary, setSummary] = useState<ObservabilitySummary | null>(null);

  const load = useCallback(async () => {
    try {
      const [a, c, s] = await Promise.all([
        fetchApi('/api/settings/agents'),
        fetchApi('/api/connectors').catch(() => []),
        fetchApi('/api/observability/summary'),
      ]);
      setAgents(a);
      setConnectors(Array.isArray(c) ? c : []);
      setSummary(s);
    } catch (e) { console.error(e); }
    finally { setLoading(false); }
  }, []);

  useEffect(() => { load(); }, [load]);

  // Cross-reference connectors with observability vendor usage. Match is by
  // id/name (loose, case-insensitive) so "linear" matches both the connector
  // row and the `top_vendors.vendor` string emitted by tool spans.
  const connectorRows = useMemo(() => {
    if (!summary) return connectors.map((c) => ({ connector: c, usage: null as VendorUsage | null }));
    return connectors.map((c) => {
      const key = c.id.toLowerCase();
      const match = summary.top_vendors.find((v) => v.vendor.toLowerCase().includes(key) || key.includes(v.vendor.toLowerCase()));
      return { connector: c, usage: match || null };
    });
  }, [connectors, summary]);

  // Vendors seen in spans but not explicitly connected — likely MCPs the
  // agent is already pulling from (e.g. via MCP config) or
  // first-party tool vendors. Show as "Discovered" so the user can decide
  // whether to formally connect them for the Spool chat agent.
  const discoveredVendors = useMemo(() => {
    if (!summary) return [];
    const connectedIds = new Set(connectors.map((c) => c.id.toLowerCase()));
    return summary.top_vendors.filter((v) => {
      const vendor = v.vendor.toLowerCase();
      return !Array.from(connectedIds).some((id) => vendor.includes(id) || id.includes(vendor));
    });
  }, [summary, connectors]);

  if (loading || !agents || !summary) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-6 w-6 border-2 border-primary border-t-transparent" />
      </div>
    );
  }

  const topTools = summary.top_tools.slice(0, 10);
  const maxToolUses = topTools[0]?.uses || 1;

  return (
    <div className="space-y-6">
      <div className="flex items-baseline justify-between">
        <h1 className="text-lg font-semibold tracking-tight flex items-center gap-2">
          <Wrench className="h-5 w-5" /> Agents &amp; Tools
        </h1>
        <span className="text-[11px] text-muted-foreground">
          Local agents, connected MCP servers, and the tools they&rsquo;re calling.
        </span>
      </div>

      {/* --- Local agents (Spool's own) --- */}
      <section className="space-y-2">
        <div className="flex items-baseline justify-between">
          <h2 className="text-[13px] font-semibold uppercase tracking-wider text-muted-foreground">
            Local agents
          </h2>
          <span className="text-[11px] text-muted-foreground">Run on this machine</span>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
          <AgentCard
            icon={<Sparkles className="h-4 w-4" />}
            name={agents.chat.name}
            role="Chat agent"
            badge={`${agents.chat.provider} · ${agents.chat.model}`}
            status={agents.chat.connected ? 'connected' : 'disconnected'}
            description={agents.chat.purpose}
            href={agents.chat.endpoint}
          />
          <AgentCard
            icon={<Bot className="h-4 w-4" />}
            name={agents.judge.name}
            role="Eval judge"
            badge={`${agents.judge.provider} · ${agents.judge.model}`}
            status={agents.judge.connected ? 'connected' : 'disconnected'}
            description={agents.judge.purpose}
            href={agents.judge.endpoint}
            note={agents.judge.note}
          />
          <AgentCard
            icon={<Server className="h-4 w-4" />}
            name={agents.mcp.name}
            role="MCP server"
            badge={`${agents.mcp.tools.length} tools · ${agents.mcp.transport}`}
            status={agents.mcp.connected ? 'connected' : 'disconnected'}
            description={agents.mcp.purpose}
            mono={agents.mcp.url}
            tools={agents.mcp.tools}
          />
        </div>
      </section>

      {/* --- Connected MCP servers (external) --- */}
      <section className="space-y-2">
        <div className="flex items-baseline justify-between">
          <h2 className="text-[13px] font-semibold uppercase tracking-wider text-muted-foreground">
            Connected MCP servers
          </h2>
          <span className="text-[11px] text-muted-foreground">
            External MCP endpoints your agents pull tools from
          </span>
        </div>

        <Card>
          <CardContent className="p-0">
            {connectorRows.length === 0 ? (
              <div className="px-4 py-6 text-center">
                <p className="text-[12px] text-muted-foreground">
                  No external MCP servers connected yet.
                </p>
                <Button
                  size="sm"
                  variant="secondary"
                  className="mt-3 h-7 text-[11px]"
                  onClick={() => router.push('/chat')}
                >
                  <Plus className="h-3 w-3 mr-1" /> Add one from Chat
                </Button>
              </div>
            ) : (
              <div className="divide-y">
                {connectorRows.map(({ connector, usage }) => (
                  <ConnectorRow key={connector.id} c={connector} usage={usage} onOpenTraces={(vendor) => router.push(`/traces?vendor=${encodeURIComponent(vendor)}`)} />
                ))}
              </div>
            )}
          </CardContent>
        </Card>
      </section>

      {/* --- Discovered tool sources --- */}
      {discoveredVendors.length > 0 && (
        <section className="space-y-2">
          <div className="flex items-baseline justify-between">
            <h2 className="text-[13px] font-semibold uppercase tracking-wider text-muted-foreground">
              Discovered tool sources
            </h2>
            <span className="text-[11px] text-muted-foreground">
              Vendors your agents already call, even without a Spool connector
            </span>
          </div>

          <Card>
            <CardContent className="p-3">
              <div className="flex flex-wrap gap-2">
                {discoveredVendors.map((v) => (
                  <button
                    key={`${v.vendor}-${v.category}`}
                    onClick={() => router.push(`/traces?vendor=${encodeURIComponent(v.vendor)}`)}
                    className="group inline-flex items-center gap-2 rounded-md border bg-card px-2.5 py-1.5 text-[12px] hover:bg-accent transition-colors"
                    title={`Show traces that called ${v.vendor}`}
                  >
                    <span className="font-medium">{v.vendor}</span>
                    <span className="text-[10px] text-muted-foreground uppercase tracking-wider">{v.category}</span>
                    <span className="tabular-nums text-muted-foreground">· {formatNumber(v.uses)}</span>
                    {v.errors > 0 && (
                      <span className="text-destructive tabular-nums text-[11px]">· {v.errors} err</span>
                    )}
                    <ArrowRight className="h-3 w-3 opacity-0 group-hover:opacity-100 transition-opacity" />
                  </button>
                ))}
              </div>
            </CardContent>
          </Card>
        </section>
      )}

      {/* --- Most-used tools --- */}
      <section className="space-y-2">
        <div className="flex items-baseline justify-between">
          <h2 className="text-[13px] font-semibold uppercase tracking-wider text-muted-foreground">
            Most-used tools
          </h2>
          <span className="text-[11px] text-muted-foreground">All tool calls aggregated across traces</span>
        </div>

        <Card>
          <CardContent className="p-3 space-y-2">
            {topTools.length === 0 ? (
              <p className="text-[12px] text-muted-foreground text-center py-4">
                No tool calls captured yet.
              </p>
            ) : topTools.map((t) => (
              <div key={t.tool_name} className="flex items-center gap-3">
                <span className="text-[12px] font-mono w-32 shrink-0 truncate" title={t.tool_name}>{t.tool_name}</span>
                <div className="flex-1 h-1.5 bg-secondary rounded-full overflow-hidden">
                  <div
                    className="h-full bg-primary/70 rounded-full transition-all"
                    style={{ width: `${(t.uses / maxToolUses) * 100}%` }}
                  />
                </div>
                <span className="text-[11px] text-muted-foreground w-14 text-right tabular-nums">
                  {formatNumber(t.uses)}
                </span>
                {t.errors > 0 && (
                  <span className="text-[11px] text-destructive w-12 text-right tabular-nums">
                    {t.errors} err
                  </span>
                )}
              </div>
            ))}
          </CardContent>
        </Card>
      </section>

      {/* --- Top agent types --- */}
      {summary.top_agents.length > 0 && (
        <section className="space-y-2">
          <div className="flex items-baseline justify-between">
            <h2 className="text-[13px] font-semibold uppercase tracking-wider text-muted-foreground">
              Agent activity
            </h2>
            <span className="text-[11px] text-muted-foreground">Agent types observed in traces</span>
          </div>

          <Card>
            <CardContent className="p-3">
              <div className="flex flex-wrap gap-2">
                {summary.top_agents.map((a) => (
                  <span
                    key={a.agent_type}
                    className="inline-flex items-center gap-2 rounded-md border bg-card px-2.5 py-1 text-[12px]"
                  >
                    <Bot className="h-3 w-3 text-muted-foreground" />
                    <span className="font-medium">{a.agent_type}</span>
                    <span className="text-muted-foreground tabular-nums">· {formatNumber(a.uses)}</span>
                  </span>
                ))}
              </div>
            </CardContent>
          </Card>
        </section>
      )}
    </div>
  );
}

// --- Pieces ---

function AgentCard({
  icon, name, role, badge, status, description, href, mono, tools, note,
}: {
  icon: React.ReactNode;
  name: string;
  role: string;
  badge: string;
  status: 'connected' | 'disconnected';
  description: string;
  href?: string;
  mono?: string;
  tools?: string[];
  note?: string | null;
}) {
  return (
    <Card className="overflow-hidden">
      <CardHeader className="pb-2">
        <div className="flex items-start justify-between gap-2">
          <div className="flex items-center gap-2 min-w-0">
            <span className="text-muted-foreground">{icon}</span>
            <CardTitle className="text-[13px] font-semibold truncate">{name}</CardTitle>
          </div>
          <StatusDot connected={status === 'connected'} />
        </div>
        <div className="text-[11px] text-muted-foreground mt-0.5 flex items-center gap-1.5 flex-wrap">
          <span className="uppercase tracking-wider">{role}</span>
          <span>·</span>
          <span className="truncate">{badge}</span>
        </div>
      </CardHeader>
      <CardContent className="pt-1 pb-3 space-y-2">
        <p className="text-[12px] text-muted-foreground leading-relaxed">{description}</p>
        {mono && (
          <code className="block bg-secondary/50 px-2 py-1 rounded text-[11px] font-mono truncate" title={mono}>
            {mono}
          </code>
        )}
        {tools && tools.length > 0 && (
          <div className="flex flex-wrap gap-1">
            {tools.map((t) => (
              <Badge key={t} variant="outline" className="text-[10px] font-mono">{t}</Badge>
            ))}
          </div>
        )}
        {note && (
          <div className="text-[11px] text-amber-600 dark:text-amber-400 flex items-start gap-1.5">
            <AlertCircle className="h-3 w-3 shrink-0 mt-0.5" />
            <span>{note}</span>
          </div>
        )}
        {href && (
          <a
            href={href}
            className="inline-flex items-center gap-1 text-[11px] text-primary hover:underline"
          >
            Open {role.toLowerCase()} <ExternalLink className="h-3 w-3" />
          </a>
        )}
      </CardContent>
    </Card>
  );
}

function ConnectorRow({
  c, usage, onOpenTraces,
}: {
  c: Connector;
  usage: VendorUsage | null;
  onOpenTraces: (vendor: string) => void;
}) {
  return (
    <div className="flex items-center gap-3 px-4 py-3">
      <Plug className="h-4 w-4 text-muted-foreground shrink-0" />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="text-[13px] font-medium">{c.name}</span>
          <ConnectorStatus status={c.status} hasAuth={c.has_auth} />
        </div>
        <div className="text-[11px] text-muted-foreground font-mono truncate">{c.url}</div>
        {c.last_error && c.status === 'error' && (
          <div className="text-[11px] text-destructive mt-0.5 truncate">{c.last_error}</div>
        )}
      </div>
      <div className="flex items-center gap-3 shrink-0 text-[12px] text-muted-foreground tabular-nums">
        {usage ? (
          <>
            <span title="Tool calls observed in traces">
              <Activity className="h-3 w-3 inline mr-1 opacity-70" />
              {formatNumber(usage.uses)}
            </span>
            {usage.errors > 0 && <span className="text-destructive">{usage.errors} err</span>}
            <button
              onClick={() => onOpenTraces(usage.vendor)}
              className="inline-flex items-center gap-1 text-primary hover:underline"
            >
              View traces <ArrowRight className="h-3 w-3" />
            </button>
          </>
        ) : (
          <span className="text-muted-foreground/70 text-[11px]">no calls yet</span>
        )}
      </div>
    </div>
  );
}

function StatusDot({ connected }: { connected: boolean }) {
  return (
    <span className={cn(
      'h-2 w-2 rounded-full shrink-0',
      connected ? 'bg-emerald-500' : 'bg-zinc-500',
    )} />
  );
}

function ConnectorStatus({ status, hasAuth }: { status: string; hasAuth: boolean }) {
  if (!hasAuth) return <Badge variant="outline" className="text-[10px]">Not connected</Badge>;
  if (status === 'connected') return (
    <Badge variant="outline" className="text-[10px] border-emerald-500/30 text-emerald-500">
      <CheckCircle2 className="h-2.5 w-2.5 mr-1" /> Connected
    </Badge>
  );
  if (status === 'error') return (
    <Badge variant="outline" className="text-[10px] border-destructive/40 text-destructive">
      <XCircle className="h-2.5 w-2.5 mr-1" /> Error
    </Badge>
  );
  return <Badge variant="outline" className="text-[10px]">Pending</Badge>;
}
