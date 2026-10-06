import { getServerSession } from "next-auth";
import { authOptions } from "@/lib/auth";
import { getPool } from "@/lib/db";
import { writeFile, mkdir } from "fs/promises";
import path from "path";

/** Returns the canonical extension if the buffer starts with a known image magic sequence, otherwise null. */
function detectImageType(buf: Buffer): "jpg" | "png" | "gif" | "webp" | null {
  if (buf.length < 12) return null;
  if (buf[0] === 0xff && buf[1] === 0xd8 && buf[2] === 0xff) return "jpg";
  if (buf[0] === 0x89 && buf[1] === 0x50 && buf[2] === 0x4e && buf[3] === 0x47) return "png";
  if (buf[0] === 0x47 && buf[1] === 0x49 && buf[2] === 0x46 && buf[3] === 0x38) return "gif";
  if (
    buf[0] === 0x52 && buf[1] === 0x49 && buf[2] === 0x46 && buf[3] === 0x46 &&
    buf[8] === 0x57 && buf[9] === 0x45 && buf[10] === 0x42 && buf[11] === 0x50
  ) return "webp";
  return null;
}

export async function POST(req: Request) {
  const session = await getServerSession(authOptions);
  const userId = (session?.user as { id?: string } | undefined)?.id;
  if (!userId) return Response.json({ error: "unauthorized" }, { status: 401 });

  const formData = await req.formData();
  const file = formData.get("avatar") as File | null;
  if (!file) return Response.json({ error: "no_file" }, { status: 400 });

  const ext = file.name.split(".").pop()?.toLowerCase();
  if (!ext || !["jpg", "jpeg", "png", "gif", "webp"].includes(ext)) {
    return Response.json({ error: "invalid_format" }, { status: 400 });
  }

  const buffer = Buffer.from(await file.arrayBuffer());
  if (buffer.length > 5 * 1024 * 1024) {
    return Response.json({ error: "file_too_large" }, { status: 400 });
  }

  // Validate actual file content via magic bytes — extension alone is not sufficient.
  const detectedExt = detectImageType(buffer);
  if (!detectedExt) {
    return Response.json({ error: "invalid_image_content" }, { status: 400 });
  }
  // Use the detected extension, not the user-supplied one
  const safeExt = detectedExt;

  const filename = `${userId}.${safeExt}`;
  const publicDir = path.join(process.cwd(), "public", "uploads", "avatars");
  await mkdir(publicDir, { recursive: true });
  await writeFile(path.join(publicDir, filename), buffer);

  const avatarUrl = `/uploads/avatars/${filename}`;

  const pool = getPool();
  await pool.query(`UPDATE users SET avatar_url = $1 WHERE id = $2`, [avatarUrl, userId]);

  return Response.json({ avatarUrl });
}

export async function DELETE() {
  const session = await getServerSession(authOptions);
  const userId = (session?.user as { id?: string } | undefined)?.id;
  if (!userId) return Response.json({ error: "unauthorized" }, { status: 401 });

  const pool = getPool();
  await pool.query(`UPDATE users SET avatar_url = NULL WHERE id = $1`, [userId]);

  return Response.json({ ok: true });
}
