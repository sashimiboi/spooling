import { NextRequest } from "next/server";
import { cookies } from "next/headers";
import { getServerSession } from "next-auth";
import { authOptions } from "@/lib/auth";
import { listUserWorkspaces, ACTIVE_WS_COOKIE } from "@/lib/workspace";

export async function POST(req: NextRequest) {
  const session = await getServerSession(authOptions);
  const userId = (session?.user as { id?: string } | undefined)?.id;
  if (!userId) return Response.json({ error: "unauthorized" }, { status: 401 });
  const body = await req.json().catch(() => ({}));
  const workspaceId = typeof body.workspaceId === "string" ? body.workspaceId : "";
  if (!workspaceId) return Response.json({ error: "missing_workspaceId" }, { status: 400 });
  const workspaces = await listUserWorkspaces(userId);
  if (!workspaces.some((w) => w.id === workspaceId)) {
    return Response.json({ error: "not_a_member" }, { status: 403 });
  }
  const jar = await cookies();
  jar.set(ACTIVE_WS_COOKIE, workspaceId, {
    path: "/",
    httpOnly: true,
    sameSite: "lax",
    secure: process.env.NODE_ENV === "production",
    maxAge: 60 * 60 * 24 * 365,
  });
  // Return the active workspace ID in the response body so client-side
  // code (Redux store) can update state without reading document.cookie.
  return Response.json({ ok: true, workspaceId });
}
