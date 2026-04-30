"""Spool MCP server.

Exposes Spool's trace, span, eval, and stats data over the Model Context
Protocol so any MCP-compatible agent (Claude Code, Codex, Cursor, etc.) can
query it as a source of context. Defaults to streamable-HTTP transport on
http://127.0.0.1:3004/mcp so web-based and remote agents can connect; stdio
is still available for stdio-only clients via `serve_stdio()`.

Tools exposed:
  - list_traces(limit, provider, project)
  - get_trace(trace_id)
  - search_sessions(query, limit, project)
  - get_stats()
  - get_top_vendors()
  - list_evals(rubric_id, limit)
  - run_eval(rubric_id, trace_id)

The server is read-mostly: `run_eval` is the only mutation, and it writes
to the same evals table the GUI reads from.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from spool.db import get_connection


MCP_HOST = "127.0.0.1"
MCP_PORT = 3004
MCP_PATH = "/mcp"
MCP_URL = f"http://{MCP_HOST}:{MCP_PORT}{MCP_PATH}"


mcp = FastMCP(
    name="spool",
    instructions=(
        "Spool tracks your AI coding sessions across Claude Code, Codex, "
        "Cursor, Copilot, Windsurf, Kiro, and Antigravity. Use these tools "
        "to recall past sessions, search history semantically, inspect "
        "span trees, and score sessions with Strands evaluators. Traces "
        "and their spans carry token usage, cost, vendor tags, and eval "
        "scores, so you can answer questions like 'how much did I spend "
        "on Linear tool calls last week?' or 'show me the longest-running "
        "agent span from Cursor'."
    ),
    host=MCP_HOST,
    port=MCP_PORT,
    stateless_http=True,
)


def _row(r) -> dict | None:
    return dict(r) if r else None


def _rows(rs) -> list[dict]:
    return [dict(r) for r in rs]


# --- tools -----------------------------------------------------------------

@mcp.tool()
def list_traces(
    limit: int = 25,
    provider: Optional[str] = None,
    project: Optional[str] = None,
) -> list[dict]:
    """Recent Spool traces. Use this to find recent sessions before drilling in.

    Args:
        limit: Max rows to return (default 25, capped at 200).
        provider: Filter to one provider id (claude-code, codex, cursor, copilot, windsurf, kiro, antigravity, gemini).
        project: Filter to sessions whose project name matches exactly.
    """
    limit = max(1, min(limit, 200))
    clauses = []
    params: list[Any] = []
    if provider:
        clauses.append("provider_id = %s")
        params.append(provider)
    if project:
        clauses.append("project = %s")
        params.append(project)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)

    conn = get_connection()
    try:
        rows = conn.execute(
            f"""SELECT id, session_id, provider_id, project, title, started_at,
                       duration_ms, span_count, agent_count, tool_count, llm_count,
                       error_count, total_cost_usd, model
                FROM traces {where}
                ORDER BY started_at DESC LIMIT %s""",
            tuple(params),
        ).fetchall()
    finally:
        conn.close()
    return _rows(rows)


@mcp.tool()
def get_trace(trace_id: str) -> dict:
    """Full detail for one trace: header row, span tree (flattened), and eval scores.

    Args:
        trace_id: The id from list_traces (looks like `trace-<session-uuid>`).
    """
    conn = get_connection()
    try:
        trace = conn.execute("SELECT * FROM traces WHERE id = %s", (trace_id,)).fetchone()
        if not trace:
            return {"error": f"trace not found: {trace_id}"}

        spans = conn.execute(
            """SELECT id, parent_id, kind, name, status, started_at, ended_at,
                      duration_ms, depth, sequence, input_tokens, output_tokens,
                      cost_usd, model, tool_name, tool_is_error, vendor, category,
                      agent_type, agent_prompt
               FROM spans WHERE trace_id = %s ORDER BY sequence""",
            (trace_id,),
        ).fetchall()

        evals = conn.execute(
            """SELECT e.rubric_id, r.name AS rubric_name, e.score, e.passed,
                      e.label, e.rationale, e.run_at
               FROM evals e LEFT JOIN eval_rubrics r ON r.id = e.rubric_id
               WHERE e.trace_id = %s ORDER BY e.run_at DESC""",
            (trace_id,),
        ).fetchall()
    finally:
        conn.close()

    return {
        "trace": _row(trace),
        "spans": _rows(spans),
        "evals": _rows(evals),
    }


@mcp.tool()
def search_sessions(
    query: str,
    limit: int = 10,
    project: Optional[str] = None,
) -> list[dict]:
    """Semantic search over Spool's embedded session chunks. Returns ranked matches.

    Args:
        query: Natural-language description of what to find.
        limit: Max results (default 10, capped at 50).
        project: Optional project name filter.
    """
    from spool.search import search as do_search
    limit = max(1, min(limit, 50))
    return do_search(query, limit=limit, project=project)


@mcp.tool()
def get_stats() -> dict:
    """Top-line Spool stats: total traces, spans, tools, llm calls, cost, errors."""
    conn = get_connection()
    try:
        row = conn.execute(
            """SELECT
                   COUNT(*) AS traces,
                   COALESCE(SUM(span_count), 0) AS spans,
                   COALESCE(SUM(agent_count), 0) AS agents,
                   COALESCE(SUM(tool_count), 0) AS tools,
                   COALESCE(SUM(llm_count), 0) AS llm_calls,
                   COALESCE(SUM(error_count), 0) AS errors,
                   COALESCE(SUM(total_input_tokens), 0) AS input_tokens,
                   COALESCE(SUM(total_output_tokens), 0) AS output_tokens,
                   COALESCE(SUM(total_cost_usd), 0) AS cost_usd
               FROM traces"""
        ).fetchone()

        per_provider = conn.execute(
            """SELECT provider_id, COUNT(*) AS traces,
                      SUM(total_cost_usd) AS cost_usd
               FROM traces GROUP BY provider_id ORDER BY traces DESC"""
        ).fetchall()
    finally:
        conn.close()

    return {
        "summary": _row(row) or {},
        "by_provider": _rows(per_provider),
    }


@mcp.tool()
def get_top_vendors(limit: int = 20) -> list[dict]:
    """Top external vendors (Linear, GitHub, Slack, Snowflake, ...) by tool-call count.

    Args:
        limit: Max rows (default 20, capped at 100).
    """
    limit = max(1, min(limit, 100))
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT vendor, category,
                      COUNT(*) AS uses,
                      SUM(CASE WHEN tool_is_error THEN 1 ELSE 0 END) AS errors,
                      COUNT(DISTINCT trace_id) AS traces
               FROM spans
               WHERE kind = 'tool' AND vendor IS NOT NULL
                 AND vendor NOT IN ('filesystem', 'shell', 'search', 'unknown')
               GROUP BY vendor, category
               ORDER BY uses DESC LIMIT %s""",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return _rows(rows)


@mcp.tool()
def list_evals(
    rubric_id: Optional[str] = None,
    limit: int = 50,
) -> list[dict]:
    """Recent eval runs. Optionally filter by rubric id.

    Args:
        rubric_id: e.g. "helpfulness", "tool-error-rate".
        limit: Max rows (default 50, capped at 200).
    """
    limit = max(1, min(limit, 200))
    conn = get_connection()
    try:
        if rubric_id:
            rows = conn.execute(
                """SELECT e.id, e.rubric_id, r.name AS rubric_name, e.trace_id,
                          e.score, e.passed, e.label, e.rationale, e.run_at
                   FROM evals e LEFT JOIN eval_rubrics r ON r.id = e.rubric_id
                   WHERE e.rubric_id = %s ORDER BY e.run_at DESC LIMIT %s""",
                (rubric_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT e.id, e.rubric_id, r.name AS rubric_name, e.trace_id,
                          e.score, e.passed, e.label, e.rationale, e.run_at
                   FROM evals e LEFT JOIN eval_rubrics r ON r.id = e.rubric_id
                   ORDER BY e.run_at DESC LIMIT %s""",
                (limit,),
            ).fetchall()
    finally:
        conn.close()
    return _rows(rows)


@mcp.tool()
def list_rubrics() -> list[dict]:
    """All configured eval rubrics (Strands evaluators + function rubrics)."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT id, name, description, kind, target_kind,
                      evaluator_type, rubric_text, is_default
               FROM eval_rubrics ORDER BY id"""
        ).fetchall()
    finally:
        conn.close()
    return _rows(rows)


@mcp.tool()
def run_eval(rubric_id: str, trace_id: str) -> dict:
    """Run a rubric against a single trace and persist the result.

    Args:
        rubric_id: From list_rubrics().
        trace_id: From list_traces() or get_trace().
    """
    from spool.evals import run_rubric

    eid = run_rubric(rubric_id, trace_id)
    if eid is None:
        return {"status": "skipped", "rubric_id": rubric_id, "trace_id": trace_id}

    conn = get_connection()
    try:
        row = conn.execute(
            """SELECT id, score, passed, label, rationale, judge_model
               FROM evals WHERE id = %s""",
            (eid,),
        ).fetchone()
    finally:
        conn.close()
    return {"status": "ok", "rubric_id": rubric_id, "trace_id": trace_id, "result": _row(row)}


# --- entrypoint ------------------------------------------------------------

def serve_stdio() -> None:
    """Run the MCP server over stdio (for stdio-only MCP clients)."""
    mcp.run(transport="stdio")


def serve_http() -> None:
    """Run the MCP server over streamable-HTTP at MCP_URL."""
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    serve_http()
