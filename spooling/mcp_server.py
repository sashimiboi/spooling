"""Spooling MCP server.

Exposes Spooling's trace, span, eval, and stats data over the Model Context
Protocol so any MCP-compatible agent (Codex, Cursor, etc.) can
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

When the local DB returns no results, the server automatically falls back
to the Spooling Cloud API (if configured via ``spooling cloud login``), so
you can discover sessions from teammates or other machines in the same
workspace.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import httpx
from mcp.server.fastmcp import FastMCP

from spooling.db import get_connection


MCP_HOST = "127.0.0.1"
MCP_PORT = 3004
MCP_PATH = "/mcp"
MCP_URL = f"http://{MCP_HOST}:{MCP_PORT}{MCP_PATH}"

# --- Mode: hybrid / local / cloud -------------------------------------------

_MCP_MODE: str = "hybrid"


def set_mode(mode: str) -> None:
    """Set the MCP data-source mode.

    ``"hybrid"`` (default) — local DB, fall back to cloud when empty.
    ``"local"`` — local DB only, never call cloud API.
    ``"cloud"`` — cloud only, skip local DB entirely.
    """
    global _MCP_MODE
    _MCP_MODE = mode


def _use_cloud() -> bool:
    """Whether the current mode allows cloud API calls."""
    return _MCP_MODE in ("hybrid", "cloud")


def _use_local() -> bool:
    """Whether the current mode queries the local DB first."""
    return _MCP_MODE in ("hybrid", "local")


# --- Cloud proxy helpers ----------------------------------------------------

_CLOUD_CONFIG_PATH = Path.home() / ".config" / "spooling" / "cloud.json"
_CLOUD_DEFAULT_API = "https://api.spooling.ai"


def _cloud_config() -> dict:
    try:
        return json.loads(_CLOUD_CONFIG_PATH.read_text())
    except Exception:
        return {}


def _cloud_headers() -> dict | None:
    cfg = _cloud_config()
    key = cfg.get("api_key")
    if not key:
        return None
    return {"Authorization": f"Bearer {key}"}


def _cloud_base() -> str:
    cfg = _cloud_config()
    return cfg.get("api_url") or _CLOUD_DEFAULT_API


def _cloud_available() -> bool:
    return _cloud_headers() is not None


def _cloud_get(path: str, params: dict | None = None) -> dict | None:
    """Call a cloud API endpoint. Returns parsed JSON or None."""
    headers = _cloud_headers()
    if not headers:
        return None
    try:
        r = httpx.get(
            f"{_cloud_base()}{path}",
            headers=headers,
            params=params or {},
            timeout=15,
        )
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


mcp = FastMCP(
    name="spooling",
    instructions=(
        "Spooling tracks your AI coding sessions across Codex, Cursor, "
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
    """Recent Spooling traces. Use this to find recent sessions before drilling in.

    Falls back to Spooling Cloud when the local DB is empty and a cloud
    API key is configured.

    Args:
        limit: Max rows to return (default 25, capped at 200).
        provider: Filter to one provider id (jsonl-session, codex, cursor, copilot, windsurf, kiro, antigravity, gemini, opencode).
        project: Filter to sessions whose project name matches exactly.
    """
    limit = max(1, min(limit, 200))

    if _use_cloud() and not _use_local():
        # Cloud-only mode
        cloud_params: dict = {"limit": limit}
        if provider:
            cloud_params["pushed_by"] = provider
        if project:
            cloud_params["project"] = project
        data = _cloud_get("/v1/sessions", cloud_params)
        if not data:
            return []
        sessions = data.get("sessions", [])
        return [
            {
                "id": f"cloud-{s['id']}",
                "session_id": s["id"],
                "provider_id": s.get("provider_id", "cloud"),
                "project": s.get("project"),
                "title": s.get("title"),
                "started_at": s.get("started_at"),
                "duration_ms": None,
                "span_count": 0,
                "agent_count": 0,
                "tool_count": 0,
                "llm_count": 0,
                "error_count": 0,
                "total_cost_usd": s.get("estimated_cost_usd", 0),
                "model": None,
                "_source": "cloud",
            }
            for s in sessions
        ]

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

    if rows:
        return _rows(rows)

    # Hybrid: fallback to cloud sessions if local is empty
    if not _use_cloud() or not _cloud_available():
        return []

    cloud_params: dict = {"limit": limit}
    if provider:
        cloud_params["pushed_by"] = provider
    if project:
        cloud_params["project"] = project
    data = _cloud_get("/v1/sessions", cloud_params)
    if not data:
        return []
    sessions = data.get("sessions", [])
    return [
        {
            "id": f"cloud-{s['id']}",
            "session_id": s["id"],
            "provider_id": s.get("provider_id", "cloud"),
            "project": s.get("project"),
            "title": s.get("title"),
            "started_at": s.get("started_at"),
            "duration_ms": None,
            "span_count": 0,
            "agent_count": 0,
            "tool_count": 0,
            "llm_count": 0,
            "error_count": 0,
            "total_cost_usd": s.get("estimated_cost_usd", 0),
            "model": None,
            "_source": "cloud",
        }
        for s in sessions
    ]


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
    """Semantic search over Spooling's embedded session chunks. Returns ranked matches.

    Falls back to Spooling Cloud ILIKE search when the local DB has no
    embedded results and a cloud API key is configured. Cloud results
    include a ``_source: "cloud"`` field.

    Args:
        query: Natural-language description of what to find.
        limit: Max results (default 10, capped at 50).
        project: Optional project name filter.
    """
    limit = max(1, min(limit, 50))

    if _use_cloud() and not _use_local():
        # Cloud-only mode
        params: dict = {"q": query, "limit": limit}
        if project:
            params["project"] = project
        data = _cloud_get("/v1/search", params)
        if not data:
            return []
        sessions = data.get("sessions", [])
        return [
            {
                "session_id": s["id"],
                "provider_id": s.get("provider_id", "cloud"),
                "project": s.get("project"),
                "title": s.get("title"),
                "content": f"{s.get('title') or '(untitled)'} — {s.get('project') or '?'}",
                "similarity": 0.0,
                "role": "system",
                "timestamp": s.get("started_at"),
                "_source": "cloud",
            }
            for s in sessions
        ]

    from spooling.search import search as do_search

    local = do_search(query, limit=limit, project=project)
    if local:
        return local

    # Hybrid: fallback to cloud ILIKE search
    if not _use_cloud() or not _cloud_available():
        return []

    params: dict = {"q": query, "limit": limit}
    if project:
        params["project"] = project
    data = _cloud_get("/v1/search", params)
    if not data:
        return []
    sessions = data.get("sessions", [])
    return [
        {
            "session_id": s["id"],
            "provider_id": s.get("provider_id", "cloud"),
            "project": s.get("project"),
            "title": s.get("title"),
            "content": f"{s.get('title') or '(untitled)'} — {s.get('project') or '?'}",
            "similarity": 0.0,
            "role": "system",
            "timestamp": s.get("started_at"),
            "_source": "cloud",
        }
        for s in sessions
    ]


@mcp.tool()
def get_stats() -> dict:
    """Top-line Spooling stats: total traces, spans, tools, llm calls, cost, errors.

    When the local DB is empty, falls back to Spooling Cloud stats if a
    cloud API key is configured.
    """
    if _use_cloud() and not _use_local():
        # Cloud-only mode
        data = _cloud_get("/v1/stats")
        if not data:
            return {"summary": {}, "by_provider": []}
        return {
            "summary": {
                "traces": data.get("sessions", 0),
                "spans": 0,
                "agents": 0,
                "tools": 0,
                "llm_calls": 0,
                "errors": 0,
                "input_tokens": int(data.get("tokens", 0)),
                "output_tokens": 0,
                "cost_usd": float(data.get("cost", 0)),
            },
            "by_provider": [{"provider_id": "cloud", "traces": data.get("sessions", 0), "cost_usd": float(data.get("cost", 0))}],
            "_source": "cloud",
        }

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

    local_summary = _row(row) or {}
    if local_summary.get("traces", 0) > 0:
        return {
            "summary": local_summary,
            "by_provider": _rows(per_provider),
        }

    # Hybrid: fallback to cloud stats
    if not _use_cloud() or not _cloud_available():
        return {"summary": local_summary, "by_provider": []}

    data = _cloud_get("/v1/stats")
    if not data:
        return {"summary": local_summary, "by_provider": []}

    return {
        "summary": {
            "traces": data.get("sessions", 0),
            "spans": 0,
            "agents": 0,
            "tools": 0,
            "llm_calls": 0,
            "errors": 0,
            "input_tokens": int(data.get("tokens", 0)),
            "output_tokens": 0,
            "cost_usd": float(data.get("cost", 0)),
        },
        "by_provider": [{"provider_id": "cloud", "traces": data.get("sessions", 0), "cost_usd": float(data.get("cost", 0))}],
        "_source": "cloud",
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
    from spooling.evals import run_rubric

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
