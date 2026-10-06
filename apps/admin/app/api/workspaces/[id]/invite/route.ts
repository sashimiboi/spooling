import { NextRequest } from "next/server";
import crypto from "crypto";
import { getServerSession } from "next-auth";
import { authOptions } from "@/lib/auth";
import { getPool } from "@/lib/db";
import { Resend } from "resend";

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export async function POST(req: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const session = await getServerSession(authOptions);
  const userId = (session?.user as { id?: string } | undefined)?.id;
  const inviterEmail = session?.user?.email ?? "";
  if (!userId) return Response.json({ error: "unauthorized" }, { status: 401 });

  const { id: workspaceId } = await params;
  const body = await req.json().catch(() => ({}));
  const email = typeof body.email === "string" ? body.email.trim().toLowerCase() : "";
  if (!EMAIL_RE.test(email)) return Response.json({ error: "invalid_email" }, { status: 400 });

  const pool = getPool();
  const { rows: membership } = await pool.query(
    `SELECT role FROM workspace_members WHERE workspace_id = $1 AND user_id = $2`,
    [workspaceId, userId],
  );
  if (!membership.length || !["owner", "admin"].includes(membership[0].role)) {
    return Response.json({ error: "forbidden" }, { status: 403 });
  }

  const { rows: wsRows } = await pool.query(
    `SELECT name, kind FROM workspaces WHERE id = $1`,
    [workspaceId],
  );
  if (!wsRows[0] || wsRows[0].kind !== "team") {
    return Response.json({ error: "cannot_invite_to_personal" }, { status: 400 });
  }
  const workspaceName = wsRows[0].name;

  const token = crypto.randomBytes(24).toString("base64url");
  const expires = new Date(Date.now() + 7 * 24 * 60 * 60 * 1000);
  await pool.query(
    `INSERT INTO workspace_invites (workspace_id, email, token, invited_by, role, expires_at)
     VALUES ($1, $2, $3, $4, 'member', $5)`,
    [workspaceId, email, token, userId, expires],
  );

  const base = process.env.NEXTAUTH_URL ?? "https://app.spooling.ai";
  const url = `${base}/invite/${token}`;

  if (process.env.RESEND_API_KEY) {
    try {
      const resend = new Resend(process.env.RESEND_API_KEY);
      await resend.emails.send({
        from: "Spooling <auth@spooling.ai>",
        to: email,
        subject: `Join "${workspaceName}" on Spooling`,
        html: `
<div style="font-family:system-ui,sans-serif;max-width:480px;margin:0 auto;padding:24px;color:#18181b">
  <h1 style="font-size:20px;margin:0 0 12px">You've been invited to ${workspaceName}</h1>
  <p style="color:#52525b;line-height:1.6;margin:0 0 24px">${inviterEmail} added you to a Spooling team workspace. Click the link below to accept. Expires in 7 days.</p>
  <a href="${url}" style="display:inline-block;background:#0b0b0f;color:#ffffff;text-decoration:none;font-weight:500;padding:12px 20px;border-radius:8px">Accept invite</a>
</div>`,
        text: `${inviterEmail} invited you to ${workspaceName} on Spooling.\n\nAccept: ${url}`,
      });
    } catch (e) {
      console.error("[invite] email send failed", e);
    }
  }

  return Response.json({ ok: true, url, token });
}

export async function GET(_req: NextRequest, { params }: { params: Promise<{ id: string }> }) {
  const session = await getServerSession(authOptions);
  const userId = (session?.user as { id?: string } | undefined)?.id;
  if (!userId) return Response.json({ error: "unauthorized" }, { status: 401 });
  const { id: workspaceId } = await params;
  const pool = getPool();

  // Verify the caller is a member of this workspace
  const { rows: membership } = await pool.query(
    `SELECT role FROM workspace_members WHERE workspace_id = $1 AND user_id = $2`,
    [workspaceId, userId],
  );
  if (!membership.length) return Response.json({ error: "forbidden" }, { status: 403 });

  const { rows } = await pool.query(
    `SELECT id, email, role, expires_at, accepted_at, revoked_at, created_at
     FROM workspace_invites
     WHERE workspace_id = $1
     ORDER BY created_at DESC`,
    [workspaceId],
  );
  return Response.json({ invites: rows });
}
