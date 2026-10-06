/**
 * Mirrors connector data from the admin DB to the Spooling DB (port 5434)
 * so the chat agent (agent.py) can see MCP connectors added via the admin UI.
 *
 * The Spooling DB's `mcp_connectors` table has a simpler schema (no user_id,
 * no UUID pk), so we map fields directly and use the slug as the row id.
 */

import { Pool } from "pg";

let spoolingPool: Pool | null = null;

function getSpoolingPool(): Pool | null {
  if (!process.env.SPOOLING_DATABASE_URL) return null;
  if (!spoolingPool) {
    spoolingPool = new Pool({ connectionString: process.env.SPOOLING_DATABASE_URL, max: 2, idleTimeoutMillis: 10_000 });
  }
  return spoolingPool;
}

export interface ConnectorData {
  id: string;
  slug: string;
  name: string;
  url: string;
  auth_header: string | null;
  transport: string;
  status: string;
  last_error?: string;
  tools_json: Array<{ name: string; description: string }>;
  tool_count: number;
}

export async function syncConnectorToSpoolingDb(data: ConnectorData): Promise<void> {
  try {
    const pool = getSpoolingPool();
    if (!pool) return; // SPOOLING_DATABASE_URL not configured — skip sync
    await pool.query(
      `INSERT INTO mcp_connectors (id, slug, name, url, auth_header, transport, status, last_error, tools_json, tool_count)
       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10)
       ON CONFLICT (id) DO UPDATE SET
         slug = EXCLUDED.slug,
         name = EXCLUDED.name,
         url = EXCLUDED.url,
         auth_header = EXCLUDED.auth_header,
         transport = EXCLUDED.transport,
         status = EXCLUDED.status,
         last_error = EXCLUDED.last_error,
         tools_json = EXCLUDED.tools_json,
         tool_count = EXCLUDED.tool_count,
         last_checked_at = now()`,
      [
        data.id,
        data.slug,
        data.name,
        data.url,
        data.auth_header,
        data.transport,
        data.status,
        data.last_error ?? null,
        JSON.stringify(data.tools_json),
        data.tool_count,
      ],
    );
  } catch (e) {
    // Don't fail the main request if the sync fails.
    console.error("[spooling-db-sync] failed:", (e as Error).message);
  }
}
