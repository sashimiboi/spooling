import { getServerSession } from "next-auth";
import { authOptions } from "@/lib/auth";
import { getActiveWorkspace } from "@/lib/workspace";
import { withWorkspace } from "@/lib/db";
import { NextRequest } from "next/server";

// Tables users are allowed to query via the analytics endpoint.
// Sensitive tables (users, api_keys, mcp_connectors, schema_migrations, etc.)
// are excluded to prevent data leaks across workspaces.
//
// RLS on cloud_* tables scopes results by workspace_id when
// app.workspace_id is set in the session via withWorkspace().
const ALLOWED_TABLES = new Set([
  "cloud_sessions",
  "cloud_messages",
  "cloud_traces",
  "cloud_spans",
  "cloud_span_events",
]);

// Boundary keywords that end a FROM table list
const FROM_BOUNDARY = /\b(WHERE|GROUP|HAVING|ORDER|LIMIT|UNION|EXCEPT|INTERSECT|INNER|LEFT|RIGHT|FULL|CROSS|NATURAL|JOIN|ON|USING|SET)\b/i;

function extractTableRefs(sql: string): string[] {
  // Strip string literals to avoid false matches inside quoted values
  const stripped = sql.replace(/'(?:[^'\\]|\\.)*'/g, "''");
  const tables: string[] = [];

  // 1. Keyword-prefixed references: FROM t, JOIN t, etc.
  const keywordRe = /\b(?:FROM|JOIN|INTO|UPDATE|TABLE)\s+(?:[a-zA-Z_]\w*\.)?([a-zA-Z_]\w*)/gi;
  let m: RegExpExecArray | null;
  while ((m = keywordRe.exec(stripped)) !== null) {
    tables.push(m[1]);
  }

  // 2. Comma-separated tables in FROM lists: FROM a, b, c
  //    Walk from each FROM keyword to the first boundary keyword and split by comma.
  const fromRe = /\bFROM\s+/gi;
  while ((m = fromRe.exec(stripped)) !== null) {
    const rest = stripped.slice(m.index + m[0].length);
    const boundary = FROM_BOUNDARY.exec(rest);
    const clause = boundary ? rest.slice(0, boundary.index) : rest;
    for (const part of clause.split(",")) {
      const firstWord = part.trim().split(/\s+/)[0];
      if (firstWord && /^[a-zA-Z_]\w*$/.test(firstWord)) {
        tables.push(firstWord);
      }
    }
  }

  return tables;
}

function validateQuery(sql: string): string | null {
  // Only allow SELECT / WITH queries
  const trimmed = sql.trim().toLowerCase();
  if (!trimmed.startsWith("select") && !trimmed.startsWith("with")) {
    return "Only SELECT and WITH queries are allowed";
  }

  // Check every extracted table reference against the allowlist
  for (const table of extractTableRefs(sql)) {
    if (!ALLOWED_TABLES.has(table.toLowerCase())) {
      return `Table "${table}" is not accessible via analytics queries`;
    }
  }

  return null;
}

export async function POST(req: NextRequest) {
  const session = await getServerSession(authOptions);
  const userId = (session?.user as { id?: string } | undefined)?.id;
  if (!userId) return Response.json({ error: "unauthorized" }, { status: 401 });

  const ws = await getActiveWorkspace(userId);
  if (!ws) {
    return Response.json({ error: "No active workspace" }, { status: 403 });
  }

  const { sql } = await req.json();
  if (!sql || typeof sql !== "string") {
    return Response.json({ error: "sql is required" }, { status: 400 });
  }

  const validationError = validateQuery(sql);
  if (validationError) {
    return Response.json({ error: validationError }, { status: 403 });
  }

  const start = performance.now();

  try {
    const MAX_ROWS = 5000;
    const result = await withWorkspace(ws.id, async (client) => {
      await client.query("SET statement_timeout = '30s'");
      // Wrap the user query to enforce a hard row cap
      const capped = `SELECT * FROM (${sql}) __q LIMIT ${MAX_ROWS + 1}`;
      const { rows: rawRows, fields, rowCount } = await client.query(capped);
      const truncated = rawRows.length > MAX_ROWS;
      const rows = truncated ? rawRows.slice(0, MAX_ROWS) : rawRows;
      return { rows, fields, rowCount, truncated };
    });

    const elapsed = performance.now() - start;

    return Response.json({
      fields: result.fields.map((f: any) => ({ name: f.name, dataTypeID: f.dataTypeID })),
      rows: result.rows,
      rowCount: result.rows.length,
      truncated: result.truncated,
      executionTime: elapsed,
    });
  } catch (e: any) {
    return Response.json({
      error: e.message ?? String(e),
      fields: [],
      rows: [],
      rowCount: 0,
      executionTime: performance.now() - start,
    });
  }
}
