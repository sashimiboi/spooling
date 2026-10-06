import { NextRequest } from "next/server";
import { getServerSession } from "next-auth";
import { authOptions } from "@/lib/auth";
import { getPool } from "@/lib/db";
import { encryptIfConfigured } from "@/lib/crypto";

const PROVIDERS = ["anthropic", "openai", "google", "groq", "mistral", "together", "openrouter"] as const;
type Provider = typeof PROVIDERS[number];

async function uid() {
  const s = await getServerSession(authOptions);
  return (s?.user as { id?: string } | undefined)?.id ?? null;
}

export async function GET() {
  const userId = await uid();
  if (!userId) return Response.json({ error: "unauthorized" }, { status: 401 });
  const pool = getPool();
  const { rows } = await pool.query(
    `SELECT provider, CASE WHEN api_key IS NOT NULL AND api_key <> '' THEN true ELSE false END AS has_key,
            updated_at
     FROM user_model_keys WHERE user_id = $1`,
    [userId],
  );
  const map: Record<string, { has_key: boolean; updated_at: string | null }> = {};
  for (const p of PROVIDERS) map[p] = { has_key: false, updated_at: null };
  for (const r of rows) map[r.provider] = { has_key: r.has_key, updated_at: r.updated_at };
  return Response.json({ providers: PROVIDERS, keys: map });
}

export async function POST(req: NextRequest) {
  const userId = await uid();
  if (!userId) return Response.json({ error: "unauthorized" }, { status: 401 });
  const body = await req.json().catch(() => ({}));
  const provider = typeof body.provider === "string" ? body.provider : "";
  const apiKey = typeof body.api_key === "string" ? body.api_key.trim() : "";
  if (!(PROVIDERS as readonly string[]).includes(provider)) {
    return Response.json({ error: "invalid_provider" }, { status: 400 });
  }
  const pool = getPool();
  if (!apiKey) {
    await pool.query(`DELETE FROM user_model_keys WHERE user_id = $1 AND provider = $2`, [userId, provider]);
    return Response.json({ ok: true, cleared: true });
  }
  await pool.query(
    `INSERT INTO user_model_keys (user_id, provider, api_key) VALUES ($1, $2, $3)
     ON CONFLICT (user_id, provider) DO UPDATE SET api_key = EXCLUDED.api_key, updated_at = now()`,
    [userId, provider as Provider, encryptIfConfigured(apiKey)],
  );
  return Response.json({ ok: true });
}
