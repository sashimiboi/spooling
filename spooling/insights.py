"""Agent / tool / MCP / trace insights over the local span data.

These power ``spooling tools``, ``spooling agents``, ``spooling mcps`` and
``spooling traces``.

Naming matches the Spooling web app so numbers line up between the two:

* an agent is its ``agent_type`` (the subagent type) when set, else the span name
* a tool is its ``tool_name`` when set, else the span name
* an MCP tool is a tool named ``mcp__<server>__<tool>``; the MCP server is the
  ``<server>`` part

Matching:

* ``--agent`` / ``--tool``  exact name, case-insensitive; repeat to match any
* ``--mcp``                 MCP server name contains the text (case-insensitive)
* ``--search``              name contains the text (case-insensitive)

Filter semantics:

* ``tools --agent A``   tools called *by* agent A (inside its subtree)
* ``agents --tool T``   agents whose runs called tool T (inside their subtree)
* ``traces``            trace-level, like the Traces page: a trace matches an
                        agent / tool / MCP filter if it contains any such span;
                        different filter kinds must all be satisfied
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


def _is_mcp(alias: str = "sp") -> str:
    return f"{_tool(alias)} LIKE 'mcp\\_\\_%' ESCAPE '\\'"


def _mcp_server(alias: str = "sp") -> str:
    """SQL expression: the MCP server of a tool span (NULL for non-MCP tools)."""
    label = _tool(alias)
    rest = f"substr({label}, 6)"
    return (
        f"CASE WHEN {_is_mcp(alias)} THEN substr({rest}, 1, "
        f"CASE WHEN instr({rest}, '__') > 0 THEN instr({rest}, '__') - 1 ELSE length({rest}) END) END"
    )


def mcp_server_of(tool_name: str | None) -> str | None:
    """Python twin of ``_mcp_server``: 'mcp__spool__search' -> 'spool'."""
    if not tool_name or not tool_name.lower().startswith("mcp__"):
        return None
    return tool_name[5:].split("__", 1)[0]


def _like(text: str) -> str:
    """Contains-pattern for ``LIKE ? ESCAPE '\\'`` with %, _ and \\ in the text escaped."""
    esc = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{esc}%"


def _marks(n: int) -> str:
    return ",".join("?" * n)


def _lower_in(expr: str, names: list[str]) -> tuple[str, list[str]]:
    return f"LOWER({expr}) IN ({_marks(len(names))})", [n.lower() for n in names]


def _mcp_match(expr_server: str, servers: list[str]) -> tuple[str, list[str]]:
    sql = "(" + " OR ".join(f"{expr_server} LIKE ? ESCAPE '\\'" for _ in servers) + ")"
    return sql, [_like(s) for s in servers]


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
    mcps: list[str] | None = None,
    search: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Per-tool usage: calls, traces it appeared in, errors, average duration.

    ``mcps`` keeps only tools of MCP servers whose name contains the text.
    """
    where, params = _trace_filters(days=days, project=project, provider=provider)
    where.insert(0, "sp.kind = 'tool'")

    cte = ""
    cte_params: list = []
    if agents:
        # restrict to tool spans that live under one of the named agents
        in_sql, in_params = _lower_in(_agent(), agents)
        cte = f"""WITH RECURSIVE sub(id) AS (
                    SELECT sp.id FROM spans sp WHERE sp.kind = 'agent' AND {in_sql}
                    UNION
                    SELECT c.id FROM spans c JOIN sub ON c.parent_id = sub.id
                  )"""
        where.append("sp.id IN (SELECT id FROM sub)")
        cte_params = in_params
    if mcps:
        m_sql, m_params = _mcp_match(_mcp_server(), mcps)
        where.append(m_sql)
        params = params + m_params
    if search:
        where.append(f"LOWER({_tool()}) LIKE ? ESCAPE '\\'")
        params.append(_like(search.lower()))

    sql = f"""{cte}
        SELECT {_tool()} AS name,
               {_mcp_server()} AS mcp_server,
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
        rows = conn.execute(sql, cte_params + params + [limit]).fetchall()
    finally:
        conn.close()
    return [
        {
            "name": r["name"],
            "mcp_server": r["mcp_server"],
            "uses": int(r["uses"]),
            "traces": int(r["traces"]),
            "errors": int(r["errors"] or 0),
            "avg_ms": float(r["avg_ms"]) if r["avg_ms"] is not None else None,
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# MCP servers
# ---------------------------------------------------------------------------

def mcp_stats(
    *,
    days: int | None = None,
    project: str | None = None,
    provider: str | None = None,
    search: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Per-MCP-server usage: distinct tools, calls, traces, errors, average duration.

    ``search`` matches the server name or any of its tool names.
    """
    where, params = _trace_filters(days=days, project=project, provider=provider)
    where.insert(0, "sp.kind = 'tool'")
    where.insert(1, _is_mcp())
    if search:
        where.append(f"LOWER({_tool()}) LIKE ? ESCAPE '\\'")
        params.append(_like(search.lower()))

    sql = f"""
        SELECT {_mcp_server()} AS name,
               COUNT(DISTINCT {_tool()}) AS tools,
               COUNT(*) AS calls,
               COUNT(DISTINCT sp.trace_id) AS traces,
               COALESCE(SUM(sp.tool_is_error), 0) AS errors,
               AVG(NULLIF(sp.duration_ms, 0)) AS avg_ms
        FROM spans sp
        JOIN traces t ON t.id = sp.trace_id
        WHERE {' AND '.join(where)}
        GROUP BY 1
        ORDER BY calls DESC, name ASC
        LIMIT ?"""
    conn = get_connection()
    try:
        rows = conn.execute(sql, params + [limit]).fetchall()
    finally:
        conn.close()
    return [
        {
            "name": r["name"],
            "tools": int(r["tools"]),
            "calls": int(r["calls"]),
            "traces": int(r["traces"]),
            "errors": int(r["errors"] or 0),
            "avg_ms": float(r["avg_ms"]) if r["avg_ms"] is not None else None,
        }
        for r in rows
    ]


def group_mcp_tools(rows: list[dict]) -> list[dict]:
    """Group per-tool rows (``name``/``tool_name`` + ``uses`` + ``errors``) by MCP server.

    Used for cloud data, where the server only exposes per-tool totals.
    Traces can't be summed across tools, so they are not reported here.
    """
    servers: dict[str, dict] = {}
    for r in rows:
        name = r.get("name") or r.get("tool_name")
        server = mcp_server_of(name)
        if server is None:
            continue
        g = servers.setdefault(server, {"name": server, "tools": 0, "calls": 0, "errors": 0})
        g["tools"] += 1
        g["calls"] += int(r.get("uses") or 0)
        g["errors"] += int(r.get("errors") or 0)
    return sorted(servers.values(), key=lambda g: (-g["calls"], g["name"]))


# ---------------------------------------------------------------------------
# agents
# ---------------------------------------------------------------------------

def agent_stats(
    *,
    days: int | None = None,
    project: str | None = None,
    provider: str | None = None,
    tools: list[str] | None = None,
    mcps: list[str] | None = None,
    search: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Per-agent usage: runs, traces it appeared in, tokens, cost.

    ``tools`` / ``mcps`` keep agents whose own runs called one of those tools /
    a tool of one of those MCP servers. With both, each must be satisfied.
    """
    where, params = _trace_filters(days=days, project=project, provider=provider)
    where.insert(0, "sp.kind = 'agent'")

    ctes: list[str] = []
    cte_params: list = []
    preds: list[tuple[str, list]] = []
    if tools:
        in_sql, in_params = _lower_in(_tool(), tools)
        preds.append((in_sql, in_params))
    if mcps:
        preds.append(_mcp_match(_mcp_server(), mcps))
    for k, (pred_sql, pred_params) in enumerate(preds):
        # climb from matching tool spans to every ancestor; agents among them qualify
        ctes.append(
            f"""up{k}(id) AS (
                  SELECT sp.parent_id FROM spans sp
                    WHERE sp.kind = 'tool' AND {pred_sql} AND sp.parent_id IS NOT NULL
                  UNION
                  SELECT p.parent_id FROM spans p JOIN up{k} ON p.id = up{k}.id WHERE p.parent_id IS NOT NULL
                )"""
        )
        cte_params += pred_params
        where.append(f"sp.id IN (SELECT id FROM up{k})")
    cte = ("WITH RECURSIVE " + ",\n".join(ctes)) if ctes else ""

    if search:
        where.append(f"LOWER({_agent()}) LIKE ? ESCAPE '\\'")
        params.append(_like(search.lower()))

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
        rows = conn.execute(sql, cte_params + params + [limit]).fetchall()
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
    mcps: list[str] | None = None,
    session: str | None = None,
    days: int | None = None,
    project: str | None = None,
    provider: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Traces, newest first, each with its distinct agents and MCP servers.

    ``query`` matches title, project, session id, trace id, an agent name or an
    MCP server name (the same fields the Traces page searches).
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
                            AND {_agent()} LIKE ?)
                 OR EXISTS (SELECT 1 FROM spans sp WHERE sp.trace_id = t.id AND sp.kind = 'tool'
                            AND {_mcp_server()} LIKE ? ESCAPE '\\'))"""
        )
        params.extend([like] * 5 + [_like(query.strip())])
    if agents:
        in_sql, in_params = _lower_in(_agent(), agents)
        where.append(
            f"EXISTS (SELECT 1 FROM spans sp WHERE sp.trace_id = t.id AND sp.kind = 'agent' AND {in_sql})"
        )
        params.extend(in_params)
    if tools:
        in_sql, in_params = _lower_in(_tool(), tools)
        where.append(
            f"EXISTS (SELECT 1 FROM spans sp WHERE sp.trace_id = t.id AND sp.kind = 'tool' AND {in_sql})"
        )
        params.extend(in_params)
    if mcps:
        m_sql, m_params = _mcp_match(_mcp_server(), mcps)
        where.append(
            f"EXISTS (SELECT 1 FROM spans sp WHERE sp.trace_id = t.id AND sp.kind = 'tool' AND {m_sql})"
        )
        params.extend(m_params)

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
            agents_by: dict[str, list[str]] = {i: [] for i in ids}
            mcps_by: dict[str, list[str]] = {i: [] for i in ids}
            for a in conn.execute(
                f"""SELECT DISTINCT sp.trace_id AS trace_id, {_agent()} AS name
                    FROM spans sp
                    WHERE sp.kind = 'agent' AND sp.trace_id IN ({_marks(len(ids))})
                    ORDER BY name""",
                ids,
            ).fetchall():
                agents_by[a["trace_id"]].append(a["name"])
            for m in conn.execute(
                f"""SELECT DISTINCT sp.trace_id AS trace_id, {_mcp_server()} AS name
                    FROM spans sp
                    WHERE sp.kind = 'tool' AND {_is_mcp()} AND sp.trace_id IN ({_marks(len(ids))})
                    ORDER BY name""",
                ids,
            ).fetchall():
                mcps_by[m["trace_id"]].append(m["name"])
            for r in rows:
                r["agents"] = agents_by[r["id"]]
                r["mcps"] = mcps_by[r["id"]]
    finally:
        conn.close()
    return rows
