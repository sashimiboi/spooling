import { NextRequest } from "next/server";
import { getServerSession } from "next-auth";
import { authOptions } from "@/lib/auth";
import { getPool } from "@/lib/db";
import { listUserWorkspaces, ensurePersonalWorkspace, slugify } from "@/lib/workspace";
import { seedDefaultAgent } from "@/lib/agents";

async function userContext() {
  const session = await getServerSession(authOptions);
  const user = session?.user as { id?: string; email?: string; name?: string } | undefined;
  return user?.id ? { userId: user.id, email: user.email ?? "", name: user.name ?? null } : null;
}

export async function GET() {
  const ctx = await userContext();
  if (!ctx) return Response.json({ error: "unauthorized" }, { status: 401 });
  await ensurePersonalWorkspace(ctx.userId, ctx.email, ctx.name);
  const workspaces = await listUserWorkspaces(ctx.userId);
  return Response.json({ workspaces });
}

export async function POST(req: NextRequest) {
  const ctx = await userContext();
  if (!ctx) return Response.json({ error: "unauthorized" }, { status: 401 });
  const body = await req.json().catch(() => ({}));
  const name = typeof body.name === "string" ? body.name.trim().slice(0, 80) : "";
  if (!name) return Response.json({ error: "name_required" }, { status: 400 });

  const pool = getPool();
  const slug = slugify(name);
  const client = await pool.connect();
  try {
    await client.query("BEGIN");
    const { rows } = await client.query(
      `INSERT INTO workspaces (name, slug, kind, owner_id) VALUES ($1, $2, 'team', $3)
       RETURNING id, name, slug, kind, owner_id`,
      [name, slug, ctx.userId],
    );
    const ws = rows[0];
    await client.query(
      `INSERT INTO workspace_members (workspace_id, user_id, role) VALUES ($1, $2, 'owner')`,
      [ws.id, ctx.userId],
    );
    await client.query("COMMIT");
    await seedDefaultAgent(ws.id, ctx.userId).catch(() => {});
    return Response.json({ workspace: { ...ws, role: "owner" } });
  } catch (e) {
    try { await client.query("ROLLBACK"); } catch {}
    console.error("[workspaces] create failed", e);
    return Response.json({ error: "create_failed" }, { status: 500 });
  } finally {
    client.release();
  }
}
