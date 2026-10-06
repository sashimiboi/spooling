import { NextRequest } from "next/server";
import { getServerSession } from "next-auth";
import { authOptions } from "@/lib/auth";
import { getPool } from "@/lib/db";
import { encryptIfConfigured } from "@/lib/crypto";

export async function GET() {
  const session = await getServerSession(authOptions);
  const userId = (session?.user as { id?: string } | undefined)?.id;
  if (!userId) return Response.json({ error: "unauthorized" }, { status: 401 });
  const pool = getPool();
  const { rows } = await pool.query(
    `SELECT chat_provider, chat_model, eval_model,
            CASE WHEN anthropic_api_key IS NULL OR anthropic_api_key = '' THEN false ELSE true END AS has_key
     FROM user_settings WHERE user_id = $1`,
    [userId],
  );
  const r = rows[0] ?? { chat_provider: "anthropic", chat_model: "claude-haiku-4-5-20251001", eval_model: null, has_key: false };
  return Response.json(r);
}

export async function POST(req: NextRequest) {
  const session = await getServerSession(authOptions);
  const userId = (session?.user as { id?: string } | undefined)?.id;
  if (!userId) return Response.json({ error: "unauthorized" }, { status: 401 });
  const body = await req.json().catch(() => ({}));
  const chat_model = typeof body.chat_model === "string" ? body.chat_model.trim() : undefined;
  const eval_model = typeof body.eval_model === "string" ? body.eval_model.trim() : undefined;
  const anthropic_api_key = typeof body.anthropic_api_key === "string" ? body.anthropic_api_key.trim() : undefined;

  const pool = getPool();
  if (chat_model !== undefined) {
    await pool.query(
      `INSERT INTO user_settings (user_id, chat_model) VALUES ($1, $2)
       ON CONFLICT (user_id) DO UPDATE SET chat_model = EXCLUDED.chat_model, updated_at = now()`,
      [userId, chat_model],
    );
  }
  if (eval_model !== undefined) {
    await pool.query(
      `INSERT INTO user_settings (user_id, eval_model) VALUES ($1, $2)
       ON CONFLICT (user_id) DO UPDATE SET eval_model = EXCLUDED.eval_model, updated_at = now()`,
      [userId, eval_model || null],
    );
  }
  if (anthropic_api_key !== undefined) {
    const stored = anthropic_api_key ? encryptIfConfigured(anthropic_api_key) : null;
    await pool.query(
      `INSERT INTO user_settings (user_id, anthropic_api_key) VALUES ($1, $2)
       ON CONFLICT (user_id) DO UPDATE SET anthropic_api_key = EXCLUDED.anthropic_api_key, updated_at = now()`,
      [userId, stored],
    );
  }
  return Response.json({ ok: true });
}
