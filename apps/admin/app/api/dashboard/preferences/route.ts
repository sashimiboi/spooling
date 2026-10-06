import { getServerSession } from "next-auth";
import { authOptions } from "@/lib/auth";
import { getPool } from "@/lib/db";
import { NextRequest } from "next/server";

export async function GET() {
  const session = await getServerSession(authOptions);
  const userId = (session?.user as { id?: string } | undefined)?.id;
  if (!userId) return Response.json({ error: "unauthorized" }, { status: 401 });

  const pool = getPool();
  const { rows } = await pool.query(
    `SELECT dashboard_prefs FROM user_settings WHERE user_id = $1`,
    [userId],
  );
  const prefs = rows[0]?.dashboard_prefs ? rows[0].dashboard_prefs : null;
  return Response.json(prefs ?? { visible: [], layout: [] });
}

export async function PUT(req: NextRequest) {
  const session = await getServerSession(authOptions);
  const userId = (session?.user as { id?: string } | undefined)?.id;
  if (!userId) return Response.json({ error: "unauthorized" }, { status: 401 });

  const body = await req.json();
  const pool = getPool();
  await pool.query(
    `INSERT INTO user_settings (user_id, dashboard_prefs)
     VALUES ($1, $2::jsonb)
     ON CONFLICT (user_id) DO UPDATE SET dashboard_prefs = $2::jsonb`,
    [userId, JSON.stringify(body)],
  );
  return Response.json({ ok: true });
}
