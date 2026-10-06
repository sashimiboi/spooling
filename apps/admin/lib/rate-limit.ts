/**
 * Simple in-memory rate limiter.
 * Works for single-process deployments. For multi-process/multi-node,
 * replace the Map with a Redis-backed store.
 */

interface Entry {
  count: number;
  resetAt: number;
}

const store = new Map<string, Entry>();

// Prune stale entries every 5 minutes to prevent unbounded growth
setInterval(() => {
  const now = Date.now();
  for (const [key, entry] of store) {
    if (entry.resetAt < now) store.delete(key);
  }
}, 5 * 60 * 1000);

export interface RateLimitOptions {
  /** Max requests allowed in the window */
  limit: number;
  /** Window duration in milliseconds */
  windowMs: number;
}

export interface RateLimitResult {
  allowed: boolean;
  remaining: number;
  resetAt: number;
}

export function rateLimit(key: string, opts: RateLimitOptions): RateLimitResult {
  const now = Date.now();
  let entry = store.get(key);

  if (!entry || entry.resetAt < now) {
    entry = { count: 1, resetAt: now + opts.windowMs };
    store.set(key, entry);
  } else {
    entry.count += 1;
  }

  const allowed = entry.count <= opts.limit;
  const remaining = Math.max(0, opts.limit - entry.count);

  return { allowed, remaining, resetAt: entry.resetAt };
}

/** Returns a 429 Response with Retry-After header, or null if not rate-limited. */
export function checkRateLimit(
  ip: string,
  action: string,
  opts: RateLimitOptions,
): Response | null {
  const { allowed, resetAt } = rateLimit(`${action}:${ip}`, opts);
  if (allowed) return null;
  const retryAfter = Math.ceil((resetAt - Date.now()) / 1000);
  return Response.json(
    { error: "too_many_requests" },
    {
      status: 429,
      headers: {
        "Retry-After": String(retryAfter),
        "X-RateLimit-Reset": String(Math.ceil(resetAt / 1000)),
      },
    },
  );
}
