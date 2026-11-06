"""Agent / tool / trace insights over the local span data.

These power ``spooling tools``, ``spooling agents`` and ``spooling traces``.

Naming matches the Spooling web app so numbers line up between the two:

* an agent is its ``agent_type`` (the subagent type) when set, else the span name
* a tool is its ``tool_name`` when set, else the span name

Filter semantics:

* ``tools --agent A``   tools called *by* agent A (inside its subtree)
* ``agents --tool T``   agents whose runs called tool T (inside their subtree)
* ``traces``            trace-level, like the Traces page: a trace matches an
                        agent / tool filter if it contains any such span
"""

from __future__ import annotations

from spooling.db import get_connection
from spooling.stats import _days_ago

AGENT_LABEL = "COALESCE(NULLIF({a}.agent_type, ''), {a}.name)"
TOOL_LABEL = "COALESCE(NULLIF({a}.tool_name, ''), {a}.name)"


def _agent(alias: str = "sp") -> str:
    return AGENT_LABEL.format(a=alias)


def _tool(alias: str = "sp") -> str:
    return TOOL_LABEL.format(a=alias)


def _marks(n: int) -> str:
    return ",".join("?" * n)


def _trace_filters(
    *,
    days: int | None,
    project: str | None,
    provider: str | None,
    alias: str = "t",
) -> tuple[list[str], list]:
    """WHERE fragments restricting the ``traces`` table (alias ``t``)."""
    where: list[str] = []
    params: list = []
    if days:
        where.append(f"{alias}.started_at >= ?")
        params.append(_days_ago(days))
    if project:
        where.append(f"{alias}.project LIKE ?")
        params.append(f"%{project}%")
    if provider:
        where.append(f"{alias}.provider_id = ?")
        params.append(provider)
    return where, params


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------

def tool_stats(
    *,
    days: int | None = None,
    project: str | None = None,
    provider: str | None = None,
    agents: list[str] | None = None,
    limit: int = 20,
) -> list[dict]:
    """Per-tool usage: calls, traces it appeared in, errors, average duration."""
    where, params = _trace_filters(days=days, project=project, provider=provider)
    where.insert(0, "sp.kind = 'tool'")

    cte = ""
    if agents:
        # restrict to tool spans that live under one of the named agents
        cte = f"""WITH RECURSIVE sub(id) AS (
                    SELECT sp.id FROM spans sp WHERE sp.kind = 'agent' AND {_agent()} IN ({_marks(len(agents))})
                    UNION
                    SELECT c.id FROM spans c JOIN sub ON c.parent_id = sub.id
                  )"""
        where.append("sp.id IN (SELECT id FROM sub)")
        params = list(agents) + params

    sql = f"""{cte}
        SELECT {_tool()} AS name,
               COUNT(*) AS uses,
               COUNT(DISTINCT sp.trace_id) AS traces,
               COALESCE(SUM(sp.tool_is_error), 0) AS errors,
               AVG(NULLIF(sp.duration_ms, 0)) AS avg_ms
        FROM spans sp
        JOIN traces t ON t.id = sp.trace_id
        WHERE {' AND '.join(where)}
        GROUP BY 1
        ORDER BY uses DESC, name ASC
        LIMIT ?"""
    conn = get_connection()
    try:
        rows = conn.execute(sql, params + [limit]).fetchall()
    finally:
        conn.close()
    return [
        {
            "name": r["name"],
            "uses": int(r["uses"]),
            "traces": int(r["traces"]),
            "errors": int(r["errors"] or 0),
            "avg_ms": float(r["avg_ms"]) if r["avg_ms"] is not None else None,
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# agents
# ---------------------------------------------------------------------------

def agent_stats(
    *,
    days: int | None = None,
    project: str | None = None,
    provider: str | None = None,
    tools: list[str] | None = None,
    limit: int = 20,
) -> list[dict]:
    """Per-agent usage: runs, traces it appeared in, tokens, cost."""
    where, params = _trace_filters(days=days, project=project, provider=provider)
    where.insert(0, "sp.kind = 'agent'")

    cte = ""
    if tools:
        # restrict to agent spans that have one of the named tools somewhere beneath them
        cte = f"""WITH RECURSIVE up(id) AS (
                    SELECT sp.parent_id FROM spans sp
                      WHERE sp.kind = 'tool' AND {_tool()} IN ({_marks(len(tools))}) AND sp.parent_id IS NOT NULL
                    UNION
                    SELECT p.parent_id FROM spans p JOIN up ON p.id = up.id WHERE p.parent_id IS NOT NULL
                  )"""
        where.append("sp.id IN (SELECT id FROM up)")
        params = list(tools) + params

    sql = f"""{cte}
        SELECT {_agent()} AS name,
               COUNT(*) AS runs,
               COUNT(DISTINCT sp.trace_id) AS traces,
               COALESCE(SUM(sp.input_tokens), 0) + COALESCE(SUM(sp.output_tokens), 0) AS tokens,
               COALESCE(SUM(sp.cost_usd), 0) AS cost,
               AVG(NULLIF(sp.duration_ms, 0)) AS avg_ms
        FROM spans sp
        JOIN traces t ON t.id = sp.trace_id
        WHERE {' AND '.join(where)}
        GROUP BY 1
        ORDER BY runs DESC, name ASC
        LIMIT ?"""
    conn = get_connection()
    try:
        rows = conn.execute(sql, params + [limit]).fetchall()
    finally:
        conn.close()
    return [
        {
            "name": r["name"],
            "runs": int(r["runs"]),
            "traces": int(r["traces"]),
            "tokens": int(r["tokens"] or 0),
            "cost": float(r["cost"] or 0),
            "avg_ms": float(r["avg_ms"]) if r["avg_ms"] is not None else None,
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# traces
# ---------------------------------------------------------------------------

def list_traces(
    *,
    query: str | None = None,
    agents: list[str] | None = None,
    tools: list[str] | None = None,
    session: str | None = None,
    days: int | None = None,
    project: str | None = None,
    provider: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Traces, newest first, each with its distinct agents.

    ``query`` matches title, project, session id, trace id or an agent name
    (the same fields the Traces page searches).
    """
    where, params = _trace_filters(days=days, project=project, provider=provider)

    if session:
        where.append("t.session_id LIKE ?")
        params.append(f"{session}%")
    if query and query.strip():
        like = f"%{query.strip()}%"
        where.append(
            f"""(COALESCE(t.title, s.title) LIKE ? OR COALESCE(t.project, s.project) LIKE ?
                 OR t.session_id LIKE ? OR t.id LIKE ?
                 OR EXISTS (SELECT 1 FROM spans sp WHERE sp.trace_id = t.id AND sp.kind = 'agent'
                            AND {_agent()} LIKE ?))"""
        )
        params.extend([like] * 5)
    if agents:
        where.append(
            f"""EXISTS (SELECT 1 FROM spans sp WHERE sp.trace_id = t.id AND sp.kind = 'agent'
                        AND {_agent()} IN ({_marks(len(agents))}))"""
        )
        params.extend(agents)
    if tools:
        where.append(
            f"""EXISTS (SELECT 1 FROM spans sp WHERE sp.trace_id = t.id AND sp.kind = 'tool'
                        AND {_tool()} IN ({_marks(len(tools))}))"""
        )
        params.extend(tools)

    sql = f"""
        SELECT t.id, t.session_id, t.provider_id,
               COALESCE(t.project, s.project) AS project,
               COALESCE(t.title, s.title) AS title,
               t.started_at, t.duration_ms, t.span_count, t.agent_count, t.tool_count,
               t.error_count, t.total_input_tokens, t.total_output_tokens,
               t.total_cost_usd, t.model
        FROM traces t
        LEFT JOIN sessions s ON s.id = t.session_id
        {('WHERE ' + ' AND '.join(where)) if where else ''}
        ORDER BY t.started_at DESC
        LIMIT ?"""
    conn = get_connection()
    try:
        rows = [dict(r) for r in conn.execute(sql, params + [limit]).fetchall()]
        if rows:
            ids = [r["id"] for r in rows]
            by_trace: dict[str, list[str]] = {i: [] for i in ids}
            for a in conn.execute(
                f"""SELECT DISTINCT sp.trace_id AS trace_id, {_agent()} AS name
                    FROM spans sp
                    WHERE sp.kind = 'agent' AND sp.trace_id IN ({_marks(len(ids))})
                    ORDER BY name""",
                ids,
            ).fetchall():
                by_trace[a["trace_id"]].append(a["name"])
            for r in rows:
                r["agents"] = by_trace[r["id"]]
    finally:
        conn.close()
    return rows
