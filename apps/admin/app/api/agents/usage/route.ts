import { NextRequest } from "next/server";
import { getServerSession } from "next-auth";
import { authOptions } from "@/lib/auth";
import { getPool } from "@/lib/db";
import { getActiveWorkspace } from "@/lib/workspace";

export const runtime = "nodejs";

/**
 * Activity rollup for the /agents page. Returns the agent subtypes and
 * tool names *observed inside pushed traces* — i.e. what the customer's
 * developer AI assistants actually did when running locally. This is
 * NOT the same as invocations of hosted Spooling agents (those aren't
 * individually tracked yet; chat_sessions has no agent_id).
 *
 * Both rollups derive from cloud_spans (kind='agent' / kind='tool'),
 * joined back to cloud_traces for workspace scoping and date filtering.
 */
export async function GET(req: NextRequest) {
  const session = await getServerSession(authOptions);
  const userId = (session?.user as { id?: string } | undefined)?.id;
  if (!userId) return Response.json({ error: "unauthorized" }, { status: 401 });

  const ws = await getActiveWorkspace(userId);
  if (!ws) return Response.json({ top_agents: [], top_tools: [], total_traces: 0 });

  const url = new URL(req.url);
  const sinceDays = parseInt(url.searchParams.get("since_days") ?? "30", 10) || 30;
  const limit = Math.min(parseInt(url.searchParams.get("limit") ?? "10", 10) || 10, 50);

  const pool = getPool();
  const params: Array<string | number> = [ws.id, sinceDays, limit];

  // Total trace count in window for the panel header.
  const tracesSql = `
    SELECT COUNT(*)::int AS total
    FROM cloud_traces
    WHERE workspace_id = $1
      AND started_at >= now() - ($2::int || ' days')::interval
  `;

  const agentsSql = `
    SELECT s.agent_type, COUNT(*)::int AS uses
    FROM cloud_spans s
    JOIN cloud_traces t ON t.id = s.trace_id AND t.workspace_id = s.workspace_id
    WHERE s.workspace_id = $1
      AND t.started_at >= now() - ($2::int || ' days')::interval
      AND s.kind = 'agent'
      AND s.agent_type IS NOT NULL
      AND s.agent_type <> ''
    GROUP BY s.agent_type
    ORDER BY uses DESC
    LIMIT $3
  `;

  const toolsSql = `
    SELECT s.tool_name, COUNT(*)::int AS uses,
           SUM(CASE WHEN s.tool_is_error THEN 1 ELSE 0 END)::int AS errors
    FROM cloud_spans s
    JOIN cloud_traces t ON t.id = s.trace_id AND t.workspace_id = s.workspace_id
    WHERE s.workspace_id = $1
      AND t.started_at >= now() - ($2::int || ' days')::interval
      AND s.kind = 'tool'
      AND s.tool_name IS NOT NULL
      AND s.tool_name <> ''
    GROUP BY s.tool_name
    ORDER BY uses DESC
    LIMIT $3
  `;

  const [traces, agents, tools] = await Promise.all([
    pool.query(tracesSql, params),
    pool.query(agentsSql, params),
    pool.query(toolsSql, params),
  ]);

  return Response.json({
    total_traces: Number(traces.rows[0]?.total ?? 0),
    since_days: sinceDays,
    top_agents: agents.rows.map((r) => ({
      agent_type: String(r.agent_type),
      uses: Number(r.uses),
    })),
    top_tools: tools.rows.map((r) => ({
      tool_name: String(r.tool_name),
      uses: Number(r.uses),
      errors: Number(r.errors ?? 0),
    })),
  });
}
