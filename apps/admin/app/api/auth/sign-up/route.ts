import { NextRequest } from "next/server";
import bcrypt from "bcryptjs";
import { getPool } from "@/lib/db";
import { checkRateLimit } from "@/lib/rate-limit";

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export async function POST(req: NextRequest) {
  const ip = req.headers.get("x-forwarded-for")?.split(",")[0]?.trim() ?? "unknown";
  const limited = checkRateLimit(ip, "sign-up", { limit: 5, windowMs: 60 * 60 * 1000 });
  if (limited) return limited;

  const body = await req.json().catch(() => ({}));
  const email = typeof body.email === "string" ? body.email.trim().toLowerCase() : "";
  const password = typeof body.password === "string" ? body.password : "";
  const name = typeof body.name === "string" ? body.name.trim().slice(0, 200) : null;

  if (!EMAIL_RE.test(email)) {
    return Response.json({ ok: false, error: "invalid_email" }, { status: 400 });
  }
  if (password.length < 8) {
    return Response.json({ ok: false, error: "password_too_short" }, { status: 400 });
  }

  const pool = getPool();
  const { rows: existing } = await pool.query("SELECT 1 FROM users WHERE email = $1", [email]);
  if (existing.length) {
    return Response.json({ ok: false, error: "email_in_use" }, { status: 409 });
  }

  const hash = await bcrypt.hash(password, 12);
  await pool.query(
    "INSERT INTO users (email, name, password_hash) VALUES ($1, $2, $3)",
    [email, name, hash],
  );
  return Response.json({ ok: true });
}
