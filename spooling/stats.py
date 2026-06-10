"""Usage statistics and metrics."""

from datetime import datetime, timedelta, timezone

from spooling.db import get_connection


def get_overview(provider: str | None = None) -> dict:
    """Get high-level usage stats, optionally filtered by provider."""
    conn = get_connection()
    where = "WHERE provider_id = %s" if provider else ""
    params: tuple = (provider,) if provider else ()

    summary = conn.execute(
        f"""SELECT
           COUNT(*) AS total_sessions,
           COALESCE(SUM(message_count), 0) AS total_messages,
           COALESCE(SUM(tool_call_count), 0) AS total_tool_calls,
           COALESCE(SUM(estimated_input_tokens), 0) AS total_input_tokens,
           COALESCE(SUM(estimated_output_tokens), 0) AS total_output_tokens,
           COALESCE(SUM(estimated_cost_usd), 0) AS total_cost_usd,
           MIN(started_at) AS earliest_session,
           MAX(ended_at) AS latest_session
        FROM sessions {where}""",
        params,
    ).fetchone()

    # Sessions per project
    projects = conn.execute(
        f"""SELECT project, COUNT(*) AS sessions, SUM(message_count) AS messages,
                  SUM(estimated_cost_usd) AS cost
           FROM sessions {where} GROUP BY project ORDER BY sessions DESC LIMIT 20""",
        params,
    ).fetchall()

    # Top tools
    tool_where = (
        "WHERE tc.session_id IN (SELECT id FROM sessions WHERE provider_id = %s)"
        if provider else ""
    )
    top_tools = conn.execute(
        f"""SELECT tc.tool_name, COUNT(*) AS uses
           FROM tool_calls tc {tool_where}
           GROUP BY tc.tool_name ORDER BY uses DESC LIMIT 15""",
        params,
    ).fetchall()

    # Recent sessions
    recent = conn.execute(
        f"""SELECT id, provider_id, project, title, started_at, message_count,
                  estimated_cost_usd, agent_version
           FROM sessions {where} ORDER BY started_at DESC LIMIT 10""",
        params,
    ).fetchall()

    conn.close()

    return {
        "summary": dict(summary) if summary else {},
        "projects": [dict(r) for r in projects],
        "top_tools": [dict(r) for r in top_tools],
        "recent_sessions": [dict(r) for r in recent],
    }


def get_provider_breakdown() -> list[dict]:
    """Get usage stats grouped by provider."""
    conn = get_connection()
    rows = conn.execute(
        """SELECT provider_id,
                  COUNT(*) AS sessions,
                  COALESCE(SUM(message_count), 0) AS messages,
                  COALESCE(SUM(tool_call_count), 0) AS tool_calls,
                  COALESCE(SUM(estimated_input_tokens), 0) AS input_tokens,
                  COALESCE(SUM(estimated_output_tokens), 0) AS output_tokens,
                  COALESCE(SUM(estimated_cost_usd), 0) AS cost,
                  MIN(started_at) AS first_session,
                  MAX(started_at) AS last_session
           FROM sessions
           GROUP BY provider_id
           ORDER BY sessions DESC"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_daily_stats(days: int = 7, provider: str | None = None) -> list[dict]:
    """Get daily usage breakdown, optionally filtered by provider."""
    conn = get_connection()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    if provider:
        rows = conn.execute(
            """SELECT DATE(started_at) AS day,
                      COUNT(*) AS sessions,
                      COALESCE(SUM(message_count), 0) AS messages,
                      COALESCE(SUM(tool_call_count), 0) AS tool_calls,
                      COALESCE(SUM(estimated_input_tokens + estimated_output_tokens), 0) AS total_tokens,
                      COALESCE(SUM(estimated_cost_usd), 0) AS cost
               FROM sessions
               WHERE started_at >= %s AND provider_id = %s
               GROUP BY DATE(started_at)
               ORDER BY day""",
            (cutoff, provider),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT DATE(started_at) AS day,
                      COUNT(*) AS sessions,
                      COALESCE(SUM(message_count), 0) AS messages,
                      COALESCE(SUM(tool_call_count), 0) AS tool_calls,
                      COALESCE(SUM(estimated_input_tokens + estimated_output_tokens), 0) AS total_tokens,
                      COALESCE(SUM(estimated_cost_usd), 0) AS cost
               FROM sessions
               WHERE started_at >= %s
               GROUP BY DATE(started_at)
               ORDER BY day""",
            (cutoff,),
        ).fetchall()

    conn.close()
    return [dict(r) for r in rows]


def get_cost_summary(
    provider: str | None = None,
    days: int | None = None,
) -> dict:
    """Aggregate cost broken down by input/output/cache components.

    Uses the traces table (which carries per-input/output/cache token
    counts) when available, falling back to the sessions table for
    providers that don't emit traces.
    """
    conn = get_connection()
    clauses: list[str] = []
    params: list = []
    if provider:
        clauses.append("provider_id = %s")
        params.append(provider)
    if days:
        clauses.append("started_at >= now() - make_interval(days => %s)")
        params.append(days)

    where = "WHERE " + " AND ".join(clauses) if clauses else ""

    trace_row = conn.execute(
        f"""SELECT
               COUNT(*) AS traces,
               COALESCE(SUM(total_input_tokens), 0) AS input_tokens,
               COALESCE(SUM(total_output_tokens), 0) AS output_tokens,
               COALESCE(SUM(total_cache_read_tokens), 0) AS cache_read_tokens,
               COALESCE(SUM(total_cache_write_tokens), 0) AS cache_write_tokens,
               COALESCE(SUM(total_cost_usd), 0) AS cost
           FROM traces {where}""",
        tuple(params),
    ).fetchone()

    session_row = conn.execute(
        f"""SELECT
               COUNT(*) AS sessions,
               COALESCE(SUM(estimated_input_tokens), 0) AS input_tokens,
               COALESCE(SUM(estimated_output_tokens), 0) AS output_tokens,
               COALESCE(SUM(estimated_cost_usd), 0) AS cost
           FROM sessions {where}""",
        tuple(params),
    ).fetchone()

    conn.close()

    t = dict(trace_row) if trace_row else {}
    s = dict(session_row) if session_row else {}

    return {
        "trace_count": t.get("traces", 0),
        "session_count": s.get("sessions", 0),
        "input_tokens": int(t.get("input_tokens", 0) or s.get("input_tokens", 0)),
        "output_tokens": int(t.get("output_tokens", 0) or s.get("output_tokens", 0)),
        "cache_read_tokens": int(t.get("cache_read_tokens", 0)),
        "cache_write_tokens": int(t.get("cache_write_tokens", 0)),
        "cost_usd": round(float(t.get("cost", 0) or s.get("cost", 0)), 4),
    }


def get_cost_by_provider(
    days: int | None = None,
) -> list[dict]:
    """Cost broken down by provider, with per-component detail."""
    conn = get_connection()
    where = ""
    params: list = []
    if days:
        where = "WHERE t.started_at >= now() - make_interval(days => %s)"
        params.append(days)

    rows = conn.execute(
        f"""SELECT
               t.provider_id,
               COUNT(*) AS traces,
               COALESCE(SUM(t.total_input_tokens), 0) AS input_tokens,
               COALESCE(SUM(t.total_output_tokens), 0) AS output_tokens,
               COALESCE(SUM(t.total_cache_read_tokens), 0) AS cache_read_tokens,
               COALESCE(SUM(t.total_cache_write_tokens), 0) AS cache_write_tokens,
               COALESCE(SUM(t.total_cost_usd), 0) AS cost
           FROM traces t {where}
           GROUP BY t.provider_id
           UNION ALL
           SELECT
               s.provider_id,
               0 AS traces,
               COALESCE(SUM(s.estimated_input_tokens), 0),
               COALESCE(SUM(s.estimated_output_tokens), 0),
               0, 0,
               COALESCE(SUM(s.estimated_cost_usd), 0)
           FROM sessions s
           WHERE NOT EXISTS (SELECT 1 FROM traces t2 WHERE t2.session_id = s.id)
           GROUP BY s.provider_id
           ORDER BY cost DESC""",
        tuple(params),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_cost_by_model(days: int | None = None) -> list[dict]:
    """Cost broken down by model from llm_call spans."""
    conn = get_connection()
    where = ""
    params: list = []
    if days:
        where = "AND s.started_at >= now() - make_interval(days => %s)"
        params.append(days)

    rows = conn.execute(
        f"""SELECT
               sp.model,
               COUNT(*) AS calls,
               COALESCE(SUM(sp.input_tokens), 0) AS input_tokens,
               COALESCE(SUM(sp.output_tokens), 0) AS output_tokens,
               COALESCE(SUM(sp.cache_read_tokens), 0) AS cache_read_tokens,
               COALESCE(SUM(sp.cache_write_tokens), 0) AS cache_write_tokens,
               COALESCE(SUM(sp.cost_usd), 0) AS cost
           FROM spans sp
           JOIN spans s ON s.trace_id = sp.trace_id AND s.kind = 'session'
           WHERE sp.kind = 'llm_call' AND sp.model IS NOT NULL {where}
           GROUP BY sp.model
           ORDER BY cost DESC""",
        tuple(params),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_cost_by_project(days: int | None = None) -> list[dict]:
    """Cost broken down by project."""
    conn = get_connection()
    where = ""
    params: list = []
    if days:
        where = "WHERE started_at >= now() - make_interval(days => %s)"
        params.append(days)

    rows = conn.execute(
        f"""SELECT
               project,
               COUNT(*) AS sessions,
               COALESCE(SUM(estimated_input_tokens), 0) AS input_tokens,
               COALESCE(SUM(estimated_output_tokens), 0) AS output_tokens,
               COALESCE(SUM(estimated_cost_usd), 0) AS cost
           FROM sessions {where}
           GROUP BY project
           ORDER BY cost DESC
           LIMIT 25""",
        tuple(params),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_monthly_cost(months: int = 12) -> list[dict]:
    """Monthly cost aggregation for the last N months."""
    conn = get_connection()
    rows = conn.execute(
        """SELECT
               DATE_TRUNC('month', started_at) AS month,
               COUNT(*) AS sessions,
               COALESCE(SUM(estimated_input_tokens + estimated_output_tokens), 0) AS total_tokens,
               COALESCE(SUM(estimated_cost_usd), 0) AS cost
           FROM sessions
           WHERE started_at >= DATE_TRUNC('month', now()) - make_interval(months => %s)
           GROUP BY DATE_TRUNC('month', started_at)
           ORDER BY month""",
        (months,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_session_cost_detail(session_id: str) -> dict | None:
    """Detailed cost breakdown for a single session.

    Returns session-level tokens/cost plus per-message token estimates
    and a per-model rate breakdown from the trace spans.
    """
    conn = get_connection()

    session = conn.execute(
        "SELECT * FROM sessions WHERE id = %s", (session_id,)
    ).fetchone()
    if not session:
        conn.close()
        return None

    trace = conn.execute(
        """SELECT t.id, t.total_input_tokens, t.total_output_tokens,
                  t.total_cache_read_tokens, t.total_cache_write_tokens,
                  t.total_cost_usd, t.model
           FROM traces t WHERE t.session_id = %s""",
        (session_id,),
    ).fetchone()

    messages = conn.execute(
        """SELECT role, estimated_tokens, timestamp
           FROM messages WHERE session_id = %s ORDER BY timestamp""",
        (session_id,),
    ).fetchall()

    llm_spans = []
    if trace:
        llm_spans = conn.execute(
            """SELECT model, input_tokens, output_tokens,
                      cache_read_tokens, cache_write_tokens, cost_usd
               FROM spans
               WHERE trace_id = %s AND kind = 'llm_call'
               ORDER BY sequence""",
            (trace["id"],),
        ).fetchall()

    conn.close()

    s = dict(session)
    user_tokens = sum(m["estimated_tokens"] for m in messages if m["role"] == "user")
    assistant_tokens = sum(m["estimated_tokens"] for m in messages if m["role"] == "assistant")

    per_model_costs: dict[str, float] = {}
    for sp in llm_spans:
        model = sp["model"] or "unknown"
        per_model_costs[model] = per_model_costs.get(model, 0) + float(sp["cost_usd"] or 0)

    return {
        "session_id": s["id"],
        "provider_id": s["provider_id"],
        "project": s["project"],
        "model": s["model"],
        "title": s["title"],
        "started_at": s["started_at"],
        "message_count": s["message_count"],
        "tool_call_count": s["tool_call_count"],
        "tokens": {
            "input": int(trace["total_input_tokens"]) if trace else user_tokens,
            "output": int(trace["total_output_tokens"]) if trace else assistant_tokens,
            "cache_read": int(trace["total_cache_read_tokens"]) if trace else 0,
            "cache_write": int(trace["total_cache_write_tokens"]) if trace else 0,
        },
        "cost_usd": round(float(trace["total_cost_usd"] if trace else s["estimated_cost_usd"] or 0), 6),
        "per_model": per_model_costs if per_model_costs else None,
        "llm_calls": [dict(sp) for sp in llm_spans],
    }


def recalc_cost(
    session_id: str | None = None,
    provider: str | None = None,
    dry_run: bool = True,
) -> dict:
    """Re-price sessions/traces against current LiteLLM rate table.

    Returns summary of what *would* change. Pass ``dry_run=False`` to
    actually update rows.
    """
    from spooling.pricing import get_rates

    conn = get_connection()
    clauses = ["TRUE"]
    params: list = []
    if session_id:
        clauses.append("s.id = %s")
        params.append(session_id)
    if provider:
        clauses.append("s.provider_id = %s")
        params.append(provider)

    where = "WHERE " + " AND ".join(clauses)

    sessions = conn.execute(
        f"""SELECT id, model, provider_id,
                   estimated_input_tokens, estimated_output_tokens,
                   estimated_cost_usd
            FROM sessions {where}""",
        tuple(params),
    ).fetchall()

    updated = 0
    total_old = 0.0
    total_new = 0.0
    changes: list[dict] = []

    for s in sessions:
        model = s["model"] or None
        try:
            rates = get_rates(model, provider_id=s["provider_id"])
        except Exception:
            continue
        new_cost = rates.cost(
            input_tokens=int(s["estimated_input_tokens"] or 0),
            output_tokens=int(s["estimated_output_tokens"] or 0),
        )
        old_cost = float(s["estimated_cost_usd"] or 0)
        diff = round(new_cost - old_cost, 6)
        if abs(diff) < 0.000001:
            continue

        changes.append({
            "session_id": s["id"],
            "old_cost": round(old_cost, 6),
            "new_cost": round(new_cost, 6),
            "diff": diff,
        })
        total_old += old_cost
        total_new += new_cost
        updated += 1

        if not dry_run:
            conn.execute(
                "UPDATE sessions SET estimated_cost_usd = %s WHERE id = %s",
                (round(new_cost, 6), s["id"]),
            )

    # Also recalc trace costs
    trace_where = "TRUE"
    trace_params: list = []
    if session_id:
        trace_where = "t.session_id = %s"
        trace_params.append(session_id)

    traces = conn.execute(
        f"""SELECT t.id, t.session_id, t.model, t.provider_id,
                   t.total_input_tokens, t.total_output_tokens,
                   t.total_cache_read_tokens, t.total_cache_write_tokens,
                   t.total_cost_usd
            FROM traces t WHERE {trace_where}""",
        tuple(trace_params),
    ).fetchall()

    trace_updated = 0
    for tr in traces:
        model = tr["model"] or None
        try:
            rates = get_rates(model, provider_id=tr["provider_id"])
        except Exception:
            continue
        new_cost = rates.cost(
            input_tokens=int(tr["total_input_tokens"] or 0),
            output_tokens=int(tr["total_output_tokens"] or 0),
            cache_write_tokens=int(tr["total_cache_write_tokens"] or 0),
            cache_read_tokens=int(tr["total_cache_read_tokens"] or 0),
        )
        old_cost = float(tr["total_cost_usd"] or 0)
        diff = round(new_cost - old_cost, 6)
        if abs(diff) < 0.000001:
            continue

        changes.append({
            "session_id": tr["session_id"],
            "trace_id": tr["id"],
            "old_cost": round(old_cost, 6),
            "new_cost": round(new_cost, 6),
            "diff": diff,
        })
        total_old += old_cost
        total_new += new_cost
        trace_updated += 1

        if not dry_run:
            conn.execute(
                "UPDATE traces SET total_cost_usd = %s WHERE id = %s",
                (round(new_cost, 6), tr["id"]),
            )

    if not dry_run:
        conn.commit()

    conn.close()

    return {
        "dry_run": dry_run,
        "sessions_reviewed": len(sessions),
        "traces_reviewed": len(traces),
        "sessions_changed": updated,
        "traces_changed": trace_updated,
        "changes": changes[:50],
        "total_old_cost": round(total_old, 4),
        "total_new_cost": round(total_new, 4),
        "total_diff": round(total_new - total_old, 4),
    }


def get_session_detail(session_id: str) -> dict | None:
    """Get detailed info for a specific session."""
    conn = get_connection()

    session = conn.execute(
        "SELECT * FROM sessions WHERE id = %s", (session_id,)
    ).fetchone()

    if not session:
        conn.close()
        return None

    messages = conn.execute(
        """SELECT id, role, content, timestamp, tools_used, estimated_tokens
           FROM messages WHERE session_id = %s ORDER BY timestamp""",
        (session_id,),
    ).fetchall()

    tool_calls_rows = conn.execute(
        """SELECT message_id, tool_name, tool_input, tool_result_preview
           FROM tool_calls WHERE session_id = %s ORDER BY id""",
        (session_id,),
    ).fetchall()

    tool_summary = conn.execute(
        """SELECT tool_name, COUNT(*) AS uses
           FROM tool_calls WHERE session_id = %s
           GROUP BY tool_name ORDER BY uses DESC""",
        (session_id,),
    ).fetchall()

    conn.close()

    tool_calls_by_msg: dict[str, list[dict]] = {}
    for tc in tool_calls_rows:
        mid = tc["message_id"]
        if mid not in tool_calls_by_msg:
            tool_calls_by_msg[mid] = []
        tool_calls_by_msg[mid].append({
            "name": tc["tool_name"],
            "input": tc["tool_input"],
            "result_preview": tc["tool_result_preview"],
        })

    msg_list = []
    for m in messages:
        d = dict(m)
        d["tool_calls"] = tool_calls_by_msg.get(m["id"], [])
        del d["id"]
        msg_list.append(d)

    return {
        "session": dict(session),
        "messages": msg_list,
        "tool_summary": [dict(t) for t in tool_summary],
    }
