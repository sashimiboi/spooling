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
