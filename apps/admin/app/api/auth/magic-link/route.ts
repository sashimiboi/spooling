import { NextRequest } from "next/server";
import crypto from "crypto";
import { getPool } from "@/lib/db";
import { sendMagicLink } from "@/lib/email";
import { checkRateLimit } from "@/lib/rate-limit";

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const TOKEN_TTL_MS = 24 * 60 * 60 * 1000;

export async function POST(req: NextRequest) {
  const ip = req.headers.get("x-forwarded-for")?.split(",")[0]?.trim() ?? "unknown";
  const limited = checkRateLimit(ip, "magic-link", { limit: 5, windowMs: 60 * 60 * 1000 });
  if (limited) return limited;

  const body = await req.json().catch(() => ({}));
  const email = typeof body.email === "string" ? body.email.trim().toLowerCase() : "";

  if (!EMAIL_RE.test(email)) {
    return Response.json({ ok: false, error: "invalid_email" }, { status: 400 });
  }

  const pool = getPool();
  const { rows } = await pool.query("SELECT 1 FROM users WHERE email = $1", [email]);
  const userExists = rows.length > 0;

  if (userExists) {
    const token = crypto.randomBytes(32).toString("hex");
    const expires = new Date(Date.now() + TOKEN_TTL_MS);
    await pool.query(
      "INSERT INTO magic_link_tokens (token, email, expires_at) VALUES ($1, $2, $3)",
      [token, email, expires],
    );
    const base = process.env.NEXTAUTH_URL ?? "https://app.spooling.ai";
    const url = `${base}/auth/callback/magic/${token}`;
    try {
      await sendMagicLink(email, url);
    } catch (e) {
      console.error("[magic-link] send failed", e);
    }
  }

  return Response.json({ ok: true });
}
