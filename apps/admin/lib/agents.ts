import { getPool } from "./db";

export interface AgentRow {
  id: string;
  workspace_id: string;
  slug: string;
  name: string;
  description: string;
  system_prompt: string;
  model: string;
  model_provider: string;
  connector_ids: string[];
  status: "active" | "paused" | string;
  created_by: string;
  created_at: string;
  updated_at: string;
}

function toAgent(r: Record<string, unknown>): AgentRow {
  return {
    id: r.id as string,
    workspace_id: r.workspace_id as string,
    slug: r.slug as string,
    name: r.name as string,
    description: (r.description as string) ?? "",
    system_prompt: (r.system_prompt as string) ?? "",
    model: r.model as string,
    model_provider: r.model_provider as string,
    connector_ids: (r.connector_ids as string[]) ?? [],
    status: r.status as string,
    created_by: r.created_by as string,
    created_at: new Date(r.created_at as string).toISOString(),
    updated_at: new Date(r.updated_at as string).toISOString(),
  };
}

export async function listAgents(workspaceId: string): Promise<AgentRow[]> {
  const pool = getPool();
  const { rows } = await pool.query(
    `SELECT * FROM agents WHERE workspace_id = $1 ORDER BY created_at DESC`,
    [workspaceId],
  );
  return rows.map(toAgent);
}

export async function getAgent(workspaceId: string, id: string): Promise<AgentRow | null> {
  const pool = getPool();
  const { rows } = await pool.query(
    `SELECT * FROM agents WHERE workspace_id = $1 AND id = $2`,
    [workspaceId, id],
  );
  return rows[0] ? toAgent(rows[0]) : null;
}

export async function getAgentBySlug(workspaceId: string, slug: string): Promise<AgentRow | null> {
  const pool = getPool();
  const { rows } = await pool.query(
    `SELECT * FROM agents WHERE workspace_id = $1 AND slug = $2`,
    [workspaceId, slug],
  );
  return rows[0] ? toAgent(rows[0]) : null;
}

function slugify(s: string): string {
  return s.toLowerCase().trim()
    .replace(/[^a-z0-9\s-]/g, "")
    .replace(/\s+/g, "-")
    .replace(/-+/g, "-")
    .slice(0, 40) || "agent";
}

async function uniqueSlug(workspaceId: string, base: string): Promise<string> {
  const pool = getPool();
  const proposed = slugify(base);
  const { rows } = await pool.query(
    `SELECT slug FROM agents WHERE workspace_id = $1 AND slug LIKE $2`,
    [workspaceId, `${proposed}%`],
  );
  const taken = new Set(rows.map((r) => r.slug as string));
  if (!taken.has(proposed)) return proposed;
  for (let i = 2; i < 999; i++) {
    const candidate = `${proposed}-${i}`;
    if (!taken.has(candidate)) return candidate;
  }
  return `${proposed}-${Date.now()}`;
}

export async function createAgent(workspaceId: string, createdBy: string, input: {
  name: string;
  description?: string;
  system_prompt?: string;
  model?: string;
  model_provider?: string;
  connector_ids?: string[];
  slug?: string;
}): Promise<AgentRow> {
  const pool = getPool();
  const slug = await uniqueSlug(workspaceId, input.slug ?? input.name);
  const { rows } = await pool.query(
    `INSERT INTO agents (workspace_id, slug, name, description, system_prompt, model, model_provider, connector_ids, created_by)
     VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
     RETURNING *`,
    [
      workspaceId, slug, input.name,
      input.description ?? "",
      input.system_prompt ?? "",
      input.model ?? "claude-haiku-4-5-20251001",
      input.model_provider ?? "anthropic",
      input.connector_ids ?? [],
      createdBy,
    ],
  );
  return toAgent(rows[0]);
}

const ALLOWED_AGENT_COLS = new Set<string>([
  "name", "description", "system_prompt", "model", "model_provider", "connector_ids", "status",
]);

export async function updateAgent(workspaceId: string, id: string, patch: Partial<Pick<AgentRow, "name" | "description" | "system_prompt" | "model" | "model_provider" | "connector_ids" | "status">>): Promise<AgentRow | null> {
  const sets: string[] = [];
  const params: unknown[] = [workspaceId, id];
  for (const [col, val] of Object.entries(patch)) {
    if (val === undefined) continue;
    if (!ALLOWED_AGENT_COLS.has(col)) continue; // reject unknown column names
    params.push(val);
    sets.push(`"${col}" = $${params.length}`);
  }
  if (sets.length === 0) return getAgent(workspaceId, id);
  sets.push("updated_at = now()");
  const pool = getPool();
  const { rows } = await pool.query(
    `UPDATE agents SET ${sets.join(", ")} WHERE workspace_id = $1 AND id = $2 RETURNING *`,
    params,
  );
  return rows[0] ? toAgent(rows[0]) : null;
}

export async function deleteAgent(workspaceId: string, id: string): Promise<boolean> {
  const pool = getPool();
  const r = await pool.query(`DELETE FROM agents WHERE workspace_id = $1 AND id = $2`, [workspaceId, id]);
  return (r.rowCount ?? 0) > 0;
}

// Default workspace assistant — mirrors the system prompt used by /api/chat so
// the built-in chat is also addressable as a named agent (and shows up in the
// chat sidebar). Seeded on workspace create and backfilled by migration 0013.
export const DEFAULT_ASSISTANT_SLUG = "spooling-assistant";
export const DEFAULT_ASSISTANT_PROMPT = `You are Spooling Assistant. You help users understand their cloud-synced AI coding sessions.

When answering:
- Reference actual session ids, projects, and dates from the context.
- Be concise. Use session ids in full when citing.
- Say when you do not have enough context.`;

export async function seedDefaultAgent(workspaceId: string, createdBy: string): Promise<void> {
  const pool = getPool();
  await pool.query(
    `INSERT INTO agents (workspace_id, slug, name, description, system_prompt, model, model_provider, created_by)
     VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
     ON CONFLICT (workspace_id, slug) DO NOTHING`,
    [
      workspaceId,
      DEFAULT_ASSISTANT_SLUG,
      "Spooling Assistant",
      "Built-in assistant that answers questions about your synced coding sessions.",
      DEFAULT_ASSISTANT_PROMPT,
      "claude-haiku-4-5-20251001",
      "anthropic",
      createdBy,
    ],
  );
}
