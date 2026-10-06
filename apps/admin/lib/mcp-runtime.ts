// Loads connected MCP connectors and their cached tool catalogs, and dispatches
// individual tool_use calls back to the upstream MCP server. Used by the chat
// tool-use loop so the assistant can call Linear/GitHub/etc alongside the
// built-in spool_* tools.
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { getPool } from "./db";
import { decryptIfEncrypted } from "./crypto";

export interface ConnectorWithTools {
  id: string;
  slug: string;
  name: string;
  url: string;
  transport: string;
  auth_header: string | null;
  tools: Array<{ name: string; description: string; inputSchema?: Record<string, unknown> }>;
}

export interface AnthropicToolDef {
  name: string;
  description: string;
  input_schema: { type: "object"; properties: Record<string, unknown>; required?: string[] };
}

const MCP_PREFIX = "mcp__";

function buildAuthHeaders(authHeader: string | null): Record<string, string> {
  if (!authHeader) return {};
  const colonIdx = authHeader.indexOf(": ");
  if (colonIdx > 0 && colonIdx < 60 && !authHeader.startsWith("Bearer ")) {
    return { [authHeader.slice(0, colonIdx)]: authHeader.slice(colonIdx + 2) };
  }
  return { Authorization: authHeader };
}

// Anthropic tool names must match ^[a-zA-Z0-9_-]{1,64}$. We namespace MCP tools
// as `mcp__<slug>__<toolname>` so the chat router can route tool_use back to
// the right connector. If a tool name overflows 64 chars after prefixing, we
// truncate the tool portion (uniqueness within a connector still holds).
function makeQualifiedToolName(slug: string, toolName: string): string {
  const safeSlug = slug.replace(/[^a-zA-Z0-9_-]/g, "_");
  const prefix = `${MCP_PREFIX}${safeSlug}__`;
  const budget = 64 - prefix.length;
  const safeTool = toolName.replace(/[^a-zA-Z0-9_-]/g, "_").slice(0, Math.max(1, budget));
  return prefix + safeTool;
}

export function parseQualifiedToolName(qualified: string): { slug: string; toolName: string } | null {
  if (!qualified.startsWith(MCP_PREFIX)) return null;
  const rest = qualified.slice(MCP_PREFIX.length);
  const sep = rest.indexOf("__");
  if (sep < 0) return null;
  return { slug: rest.slice(0, sep), toolName: rest.slice(sep + 2) };
}

/**
 * Load the connected MCP connectors for this user, along with their cached
 * tool catalogs. If `restrictToIds` is non-empty, only those connector ids
 * are returned (used to honor an agent's connector_ids filter).
 */
export async function loadConnectorsWithTools(userId: string, restrictToIds?: string[]): Promise<ConnectorWithTools[]> {
  const pool = getPool();
  const params: unknown[] = [userId];
  let where = `user_id = $1 AND status = 'connected' AND tool_count > 0`;
  if (restrictToIds && restrictToIds.length > 0) {
    params.push(restrictToIds);
    where += ` AND id = ANY($${params.length}::uuid[])`;
  }
  const { rows } = await pool.query(
    `SELECT id, slug, name, url, transport, auth_header, tools_json
     FROM mcp_connectors WHERE ${where}`,
    params,
  );
  return rows.map((r) => ({
    id: r.id,
    slug: r.slug,
    name: r.name,
    url: r.url,
    transport: r.transport,
    auth_header: decryptIfEncrypted(r.auth_header),
    tools: Array.isArray(r.tools_json) ? r.tools_json : [],
  }));
}

/**
 * Convert a list of connectors-with-cached-tools into Anthropic tool defs
 * suitable for the `tools` field of a /v1/messages request. Tool names are
 * namespaced with `mcp__<slug>__<toolname>` so we can route tool_use back.
 */
export function buildMcpAnthropicTools(connectors: ConnectorWithTools[]): AnthropicToolDef[] {
  const out: AnthropicToolDef[] = [];
  for (const c of connectors) {
    for (const t of c.tools) {
      const qname = makeQualifiedToolName(c.slug, t.name);
      const schema = (t.inputSchema as { type?: string; properties?: Record<string, unknown>; required?: string[] } | undefined) ?? {};
      out.push({
        name: qname,
        description: `[${c.name}] ${t.description || t.name}`.slice(0, 1000),
        input_schema: {
          type: "object",
          properties: schema.properties ?? {},
          ...(schema.required ? { required: schema.required } : {}),
        },
      });
    }
  }
  return out;
}

/**
 * Convert a list of connectors-with-cached-tools into OpenAI function-calling
 * tool defs suitable for /v1/chat/completions `tools` field. Tool names are
 * namespaced with `mcp__<slug>__<toolname>` so we can route tool_calls back.
 */
export function buildMcpOpenAiTools(connectors: ConnectorWithTools[]) {
  const out: Array<{
    type: "function";
    function: { name: string; description: string; parameters: Record<string, unknown> };
  }> = [];
  for (const c of connectors) {
    for (const t of c.tools) {
      const qname = makeQualifiedToolName(c.slug, t.name);
      const schema = (t.inputSchema as Record<string, unknown> | undefined) ?? {};
      out.push({
        type: "function",
        function: {
          name: qname,
          description: `[${c.name}] ${t.description || t.name}`.slice(0, 1000),
          parameters: schema,
        },
      });
    }
  }
  return out;
}

/**
 * Open a fresh MCP client to the connector, call the tool, return the
 * stringified result. We open/close per call for now; caching connections
 * across tool calls is a follow-up.
 */
export async function callMcpTool(connector: ConnectorWithTools, toolName: string, args: Record<string, unknown>): Promise<string> {
  if (connector.transport !== "streamable-http") {
    return JSON.stringify({ error: "unsupported_transport", transport: connector.transport });
  }
  const headers = buildAuthHeaders(connector.auth_header);
  const client = new Client({ name: "spooling-admin", version: "1.0.0" }, {});
  const transport = new StreamableHTTPClientTransport(new URL(connector.url), { requestInit: { headers } });
  try {
    const timer = new AbortController();
    const t = setTimeout(() => timer.abort(), 30_000);
    try {
      await client.connect(transport);
      const result = await client.callTool({ name: toolName, arguments: args });
      clearTimeout(t);
      const content = (result as { content?: Array<{ type: string; text?: string }> }).content ?? [];
      const text = content
        .filter((c) => c.type === "text" && typeof c.text === "string")
        .map((c) => c.text as string)
        .join("\n");
      const isError = (result as { isError?: boolean }).isError === true;
      return JSON.stringify({
        ok: !isError,
        content: text || JSON.stringify(result),
      });
    } finally {
      clearTimeout(t);
    }
  } catch (e) {
    return JSON.stringify({ ok: false, error: (e as Error).message?.slice(0, 500) || "mcp_call_failed" });
  } finally {
    try { await client.close(); } catch { /* ignore */ }
  }
}
