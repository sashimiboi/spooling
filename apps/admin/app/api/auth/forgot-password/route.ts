import { NextRequest } from "next/server";
import crypto from "crypto";
import { getPool } from "@/lib/db";
import { sendPasswordReset } from "@/lib/email";
import { checkRateLimit } from "@/lib/rate-limit";

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const TOKEN_TTL_MS = 60 * 60 * 1000;

export async function POST(req: NextRequest) {
  const ip = req.headers.get("x-forwarded-for")?.split(",")[0]?.trim() ?? "unknown";
  const limited = checkRateLimit(ip, "forgot-password", { limit: 5, windowMs: 60 * 60 * 1000 });
  if (limited) return limited;

  const body = await req.json().catch(() => ({}));
  const email = typeof body.email === "string" ? body.email.trim().toLowerCase() : "";

  if (!EMAIL_RE.test(email)) {
    return Response.json({ ok: false, error: "invalid_email" }, { status: 400 });
  }

  const pool = getPool();
  const { rows } = await pool.query("SELECT id FROM users WHERE email = $1", [email]);
  const user = rows[0];

  if (user) {
    const token = crypto.randomBytes(32).toString("hex");
    const expires = new Date(Date.now() + TOKEN_TTL_MS);
    await pool.query(
      "INSERT INTO password_reset_tokens (token, user_id, expires_at) VALUES ($1, $2, $3)",
      [token, user.id, expires],
    );
    const base = process.env.NEXTAUTH_URL ?? "https://app.spooling.ai";
    const url = `${base}/reset-password/${token}`;
    try {
      await sendPasswordReset(email, url);
    } catch (e) {
      console.error("[forgot-password] send failed", e);
    }
  }

  return Response.json({ ok: true });
}
