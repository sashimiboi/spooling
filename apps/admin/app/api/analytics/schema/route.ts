import { getServerSession } from "next-auth";
import { authOptions } from "@/lib/auth";
import { getPool } from "@/lib/db";
import { NextRequest } from "next/server";

// Keep in sync with ALLOWED_TABLES in ../query/route.ts
const ALLOWED_TABLES = new Set([
  "cloud_sessions",
  "cloud_messages",
  "cloud_traces",
  "cloud_spans",
  "cloud_span_events",
]);

export async function POST(req: NextRequest) {
  const session = await getServerSession(authOptions);
  const userId = (session?.user as { id?: string } | undefined)?.id;
  if (!userId) return Response.json({ error: "unauthorized" }, { status: 401 });

  const pool = getPool();

  try {
    const allowedList = Array.from(ALLOWED_TABLES);
    const placeholders = allowedList.map((_, i) => `$${i + 1}`).join(", ");

    const { rows: tables } = await pool.query(
      `SELECT table_name FROM information_schema.tables
       WHERE table_schema = 'public'
         AND table_type = 'BASE TABLE'
         AND table_name IN (${placeholders})
       ORDER BY table_name`,
      allowedList,
    );

    const schemas = [{ name: "public", tables: [] as any[] }];

    for (const t of tables) {
      const { rows: cols } = await pool.query(
        `SELECT column_name, data_type, is_nullable, character_maximum_length
         FROM information_schema.columns
         WHERE table_schema = 'public' AND table_name = $1
         ORDER BY ordinal_position`,
        [t.table_name],
      );

      schemas[0].tables.push({
        name: t.table_name,
        columns: cols.map((c: any) => ({
          name: c.column_name,
          dataType: c.data_type,
          nullable: c.is_nullable === "YES",
          maxLength: c.character_maximum_length,
        })),
      });
    }

    return Response.json({ schemas });
  } catch (e: any) {
    return Response.json({ error: e.message ?? String(e), schemas: [] }, { status: 500 });
  }
}
