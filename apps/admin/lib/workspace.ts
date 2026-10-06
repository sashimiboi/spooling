import { cookies } from "next/headers";
import { getPool } from "./db";
import { seedDefaultAgent } from "./agents";

export const ACTIVE_WS_COOKIE = "spooling-ws";

export interface Workspace {
  id: string;
  name: string;
  slug: string;
  kind: "personal" | "team";
  owner_id: string;
  role: "owner" | "admin" | "member";
  plan?: "free" | "team" | "business";
  plan_renews_at?: string | null;
  stripe_status?: string | null;
}

export async function listUserWorkspaces(userId: string): Promise<Workspace[]> {
  const pool = getPool();
  const { rows } = await pool.query(
    `SELECT w.id, w.name, w.slug, w.kind, w.owner_id, m.role,
            COALESCE(w.plan, 'free') AS plan, w.plan_renews_at, w.stripe_status
     FROM workspaces w
     JOIN workspace_members m ON m.workspace_id = w.id
     WHERE m.user_id = $1
     ORDER BY (w.kind = 'personal') DESC, w.created_at ASC`,
    [userId],
  );
  return rows as Workspace[];
}

export async function getActiveWorkspace(userId: string): Promise<Workspace | null> {
  const workspaces = await listUserWorkspaces(userId);
  if (workspaces.length === 0) return null;
  const jar = await cookies();
  const wanted = jar.get(ACTIVE_WS_COOKIE)?.value;
  if (wanted) {
    const match = workspaces.find((w) => w.id === wanted || w.slug === wanted);
    if (match) return match;
  }
  return workspaces.find((w) => w.kind === "personal") ?? workspaces[0];
}

export async function ensurePersonalWorkspace(userId: string, email: string, name: string | null): Promise<Workspace> {
  const pool = getPool();
  const existing = await pool.query(
    `SELECT id, name, slug, kind, owner_id, 'owner' AS role
     FROM workspaces WHERE owner_id = $1 AND kind = 'personal' LIMIT 1`,
    [userId],
  );
  if (existing.rows[0]) return existing.rows[0];

  const label = name || email.split("@")[0] || "Personal";
  const slug = `p-${userId.replace(/-/g, "")}`;
  const client = await pool.connect();
  try {
    await client.query("BEGIN");
    const { rows } = await client.query(
      `INSERT INTO workspaces (name, slug, kind, owner_id) VALUES ($1, $2, 'personal', $3)
       ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name
       RETURNING id, name, slug, kind, owner_id`,
      [label, slug, userId],
    );
    const ws = rows[0];
    await client.query(
      `INSERT INTO workspace_members (workspace_id, user_id, role) VALUES ($1, $2, 'owner')
       ON CONFLICT DO NOTHING`,
      [ws.id, userId],
    );
    await client.query("COMMIT");
    await seedDefaultAgent(ws.id, userId).catch(() => {});
    return { ...ws, role: "owner" };
  } catch (e) {
    try { await client.query("ROLLBACK"); } catch {}
    throw e;
  } finally {
    client.release();
  }
}

export function slugify(name: string): string {
  const base = name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40) || "team";
  return `${base}-${Math.random().toString(36).slice(2, 8)}`;
}
