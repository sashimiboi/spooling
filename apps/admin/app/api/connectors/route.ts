import { NextRequest } from "next/server";
import { getServerSession } from "next-auth";
import { authOptions } from "@/lib/auth";
import { getPool } from "@/lib/db";
import { syncConnectorToSpoolingDb } from "@/lib/spooling-db-sync";
import { encryptIfConfigured } from "@/lib/crypto";

async function uid() {
  const s = await getServerSession(authOptions);
  return (s?.user as { id?: string } | undefined)?.id ?? null;
}

export async function GET() {
  const userId = await uid();
  if (!userId) return Response.json({ error: "unauthorized" }, { status: 401 });
  const pool = getPool();
  const { rows } = await pool.query(
    `SELECT id, slug, name, url, transport, status, last_error, last_checked_at, created_at,
            tool_count, tools_json,
            CASE WHEN auth_header IS NOT NULL AND auth_header <> '' THEN true ELSE false END AS has_auth
     FROM mcp_connectors WHERE user_id = $1 ORDER BY created_at ASC`,
    [userId],
  );
  return Response.json({ connectors: rows });
}

export async function POST(req: NextRequest) {
  const userId = await uid();
  if (!userId) return Response.json({ error: "unauthorized" }, { status: 401 });
  const body = await req.json().catch(() => ({}));
  const slug = String(body.slug ?? "").trim();
  const name = String(body.name ?? "").trim();
  const url = String(body.url ?? "").trim();
  const auth_header = body.auth_header ? String(body.auth_header).trim() : null;
  const transport = String(body.transport ?? "streamable-http");
  if (!slug || !name || !url) return Response.json({ error: "missing_fields" }, { status: 400 });

  const pool = getPool();
  const encrypted_auth = auth_header ? encryptIfConfigured(auth_header) : null;
  await pool.query(
    `INSERT INTO mcp_connectors (user_id, slug, name, url, auth_header, transport, status)
     VALUES ($1, $2, $3, $4, $5, $6, 'disconnected')
     ON CONFLICT (user_id, slug) DO UPDATE SET
       name = EXCLUDED.name,
       url = EXCLUDED.url,
       auth_header = COALESCE(EXCLUDED.auth_header, mcp_connectors.auth_header),
       transport = EXCLUDED.transport`,
    [userId, slug, name, url, encrypted_auth, transport],
  );
  // Mirror to the Spooling DB so agent.py sees the connector.
  await syncConnectorToSpoolingDb({
    id: slug,
    name, slug, url, auth_header, transport,
    status: "disconnected",
    tools_json: [],
    tool_count: 0,
  });
  return Response.json({ ok: true });
}
