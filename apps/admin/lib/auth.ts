import type { AuthOptions } from "next-auth";
import CredentialsProvider from "next-auth/providers/credentials";
import bcrypt from "bcryptjs";
import { getPool } from "./db";
import { rateLimit } from "./rate-limit";

export const authOptions: AuthOptions = {
  session: { strategy: "jwt", maxAge: 60 * 60 * 8 },
  pages: {
    signIn: "/sign-in",
    error: "/sign-in",
  },
  providers: [
    CredentialsProvider({
      id: "credentials",
      name: "credentials",
      credentials: {
        email: { label: "Email", type: "email" },
        password: { label: "Password", type: "password" },
      },
      async authorize(credentials) {
        if (!credentials?.email || !credentials?.password) return null;
        const email = credentials.email.trim().toLowerCase();

        // Rate limit: 10 attempts per email per 15 minutes
        const { allowed } = rateLimit(`sign-in:${email}`, { limit: 10, windowMs: 15 * 60 * 1000 });
        if (!allowed) return null;

        const pool = getPool();
        const { rows } = await pool.query(
          "SELECT id, email, name, password_hash FROM users WHERE email = $1 LIMIT 1",
          [email],
        );
        const user = rows[0];
        if (!user || !user.password_hash) return null;
        const ok = await bcrypt.compare(credentials.password, user.password_hash);
        if (!ok) return null;
        await pool.query("UPDATE users SET last_login_at = now() WHERE id = $1", [user.id]);
        return { id: String(user.id), email: user.email, name: user.name };
      },
    }),
    CredentialsProvider({
      id: "magic-link",
      name: "magic-link",
      credentials: { token: { label: "Token", type: "text" } },
      async authorize(credentials) {
        if (!credentials?.token) return null;
        const pool = getPool();
        const client = await pool.connect();
        try {
          await client.query("BEGIN");
          const { rows } = await client.query(
            "SELECT email, expires_at, used_at FROM magic_link_tokens WHERE token = $1 FOR UPDATE",
            [credentials.token],
          );
          const t = rows[0];
          if (!t || t.used_at || new Date(t.expires_at) < new Date()) {
            await client.query("ROLLBACK");
            return null;
          }
          const userRes = await client.query(
            "SELECT id, email, name FROM users WHERE email = $1 LIMIT 1",
            [t.email],
          );
          const user = userRes.rows[0];
          if (!user) {
            await client.query("ROLLBACK");
            return null;
          }
          await client.query("UPDATE magic_link_tokens SET used_at = now() WHERE token = $1", [credentials.token]);
          await client.query("UPDATE users SET last_login_at = now() WHERE id = $1", [user.id]);
          await client.query("COMMIT");
          return { id: String(user.id), email: user.email, name: user.name };
        } catch (e) {
          try { await client.query("ROLLBACK"); } catch {}
          console.error("[magic-link authorize] failed", e);
          return null;
        } finally {
          client.release();
        }
      },
    }),
  ],
  callbacks: {
    async jwt({ token, user }) {
      if (user) token.uid = user.id;
      return token;
    },
    async session({ session, token }) {
      if (session.user && token.uid) (session.user as { id?: string }).id = String(token.uid);
      return session;
    },
  },
};
