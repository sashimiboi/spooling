import { getServerSession } from "next-auth";
import { authOptions } from "@/lib/auth";
import { getPool } from "@/lib/db";

export async function GET() {
  const session = await getServerSession(authOptions);
  const userId = (session?.user as { id?: string } | undefined)?.id;
  if (!userId) return Response.json({ error: "unauthorized" }, { status: 401 });

  const pool = getPool();
  const { rows } = await pool.query(
    `SELECT u.name, u.email, u.email_verified_at, u.created_at, u.last_login_at, u.avatar_url,
            COALESCE((
              SELECT SUM(pg_total_relation_size(relid))
              FROM pg_stat_user_tables
              WHERE relname LIKE 'cloud\\_%'
            ), 0)::bigint AS storage_bytes
     FROM users u WHERE u.id = $1`,
    [userId],
  );

  if (!rows.length) return Response.json({ error: "not_found" }, { status: 404 });

  return Response.json({
    name: rows[0].name,
    email: rows[0].email,
    avatar_url: rows[0].avatar_url ?? null,
    email_verified_at: rows[0].email_verified_at?.toISOString() ?? null,
    created_at: rows[0].created_at?.toISOString() ?? null,
    last_login_at: rows[0].last_login_at?.toISOString() ?? null,
    storage_bytes: Number(rows[0].storage_bytes ?? 0),
  });
}

export async function PATCH(req: Request) {
  const session = await getServerSession(authOptions);
  const userId = (session?.user as { id?: string } | undefined)?.id;
  if (!userId) return Response.json({ error: "unauthorized" }, { status: 401 });

  const { name } = await req.json();
  if (typeof name !== "string" || name.length > 100) {
    return Response.json({ error: "invalid_name" }, { status: 400 });
  }

  const pool = getPool();
  await pool.query(`UPDATE users SET name = $1 WHERE id = $2`, [name.trim(), userId]);

  return Response.json({ ok: true });
}

export async function DELETE() {
  const session = await getServerSession(authOptions);
  const userId = (session?.user as { id?: string } | undefined)?.id;
  if (!userId) return Response.json({ error: "unauthorized" }, { status: 401 });

  const pool = getPool();
  const client = await pool.connect();
  try {
    await client.query("BEGIN");
    await client.query(`DELETE FROM cloud_span_events WHERE span_id IN (SELECT id FROM cloud_spans WHERE user_id = $1)`, [userId]);
    await client.query(`DELETE FROM cloud_spans WHERE user_id = $1`, [userId]);
    await client.query(`DELETE FROM cloud_traces WHERE user_id = $1`, [userId]);
    await client.query(`DELETE FROM cloud_messages WHERE user_id = $1`, [userId]);
    await client.query(`DELETE FROM cloud_sessions WHERE user_id = $1`, [userId]);
    await client.query(`DELETE FROM chat_messages WHERE chat_session_id IN (SELECT id FROM chat_sessions WHERE user_id = $1)`, [userId]);
    await client.query(`DELETE FROM chat_sessions WHERE user_id = $1`, [userId]);
    await client.query(`DELETE FROM user_model_keys WHERE user_id = $1`, [userId]);
    await client.query(`DELETE FROM mcp_connectors WHERE user_id = $1`, [userId]);
    await client.query(`DELETE FROM agents WHERE created_by = $1`, [userId]);
    await client.query(`DELETE FROM api_keys WHERE user_id = $1`, [userId]);
    await client.query(`DELETE FROM magic_link_tokens WHERE email = (SELECT email FROM users WHERE id = $1)`, [userId]);
    await client.query(`DELETE FROM password_reset_tokens WHERE email = (SELECT email FROM users WHERE id = $1)`, [userId]);
    await client.query(`DELETE FROM workspace_members WHERE user_id = $1`, [userId]);
    await client.query(`DELETE FROM workspace_invites WHERE invited_by = $1`, [userId]);
    await client.query(`DELETE FROM user_settings WHERE user_id = $1`, [userId]);
    await client.query(`DELETE FROM users WHERE id = $1`, [userId]);
    await client.query("COMMIT");
  } catch (e) {
    try { await client.query("ROLLBACK"); } catch {}
    console.error("[profile] delete failed", e);
    return Response.json({ error: "delete_failed" }, { status: 500 });
  } finally {
    client.release();
  }

  return Response.json({ ok: true });
}
