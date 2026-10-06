import { getServerSession } from "next-auth";
import { authOptions } from "@/lib/auth";
import { getPool } from "@/lib/db";
import { syncConnectorToSpoolingDb } from "@/lib/spooling-db-sync";

async function uid() {
  const s = await getServerSession(authOptions);
  return (s?.user as { id?: string } | undefined)?.id ?? null;
}

export async function DELETE(_req: Request, { params }: { params: Promise<{ id: string }> }) {
  const userId = await uid();
  if (!userId) return Response.json({ error: "unauthorized" }, { status: 401 });
  const { id } = await params;
  const pool = getPool();
  // Grab slug before deleting so we can remove from the Spooling DB too.
  const { rows } = await pool.query(`SELECT slug FROM mcp_connectors WHERE id = $1 AND user_id = $2`, [id, userId]);
  await pool.query(`DELETE FROM mcp_connectors WHERE id = $1 AND user_id = $2`, [id, userId]);
  if (rows[0]?.slug && process.env.SPOOLING_DATABASE_URL) {
    // Mirror delete to Spooling DB.
    try {
      const { Pool } = await import("pg");
      const sp = new Pool({ connectionString: process.env.SPOOLING_DATABASE_URL, max: 1, idleTimeoutMillis: 5_000 });
      await sp.query(`DELETE FROM mcp_connectors WHERE id = $1`, [rows[0].slug]);
      await sp.end();
    } catch { /* non-fatal */ }
  }
  return Response.json({ ok: true });
}
