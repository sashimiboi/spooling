"""FastAPI server for Spooling API."""

import json

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from spooling.stats import get_overview, get_daily_stats, get_session_detail, get_provider_breakdown
from spooling.search import search as do_search
from spooling.db import get_connection
from spooling.tracing import Trace, Span, SpanKind, SpanStatus, SpanEvent
from spooling.ingest import _store_trace, _store_session
from spooling.parser import ParsedSession
from datetime import datetime

app = FastAPI(title="Spooling", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3003", "http://127.0.0.1:3003"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Overview / Stats ---

@app.get("/api/overview")
async def api_overview(provider: str | None = Query(default=None)):
    return get_overview(provider=provider)


@app.get("/api/daily")
async def api_daily(days: int = Query(default=14), provider: str | None = Query(default=None)):
    return get_daily_stats(days=days, provider=provider)


@app.get("/api/stats/providers")
async def api_provider_breakdown():
    return get_provider_breakdown()


@app.get("/api/stats/models")
async def api_model_breakdown(days: int | None = Query(default=None)):
    from spooling.stats import get_cost_by_model
    return get_cost_by_model(days=days)


@app.get("/api/sessions")
async def api_sessions(limit: int = Query(default=50), provider: str | None = Query(default=None)):
    conn = get_connection()
    if provider:
        rows = conn.execute(
            """SELECT id, provider_id, project, cwd, git_branch, started_at, ended_at,
                      message_count, tool_call_count, estimated_input_tokens,
                      estimated_output_tokens, estimated_cost_usd, agent_version, model, title
               FROM sessions WHERE provider_id = %s ORDER BY started_at DESC LIMIT %s""",
            (provider, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT id, provider_id, project, cwd, git_branch, started_at, ended_at,
                      message_count, tool_call_count, estimated_input_tokens,
                      estimated_output_tokens, estimated_cost_usd, agent_version, model, title
               FROM sessions ORDER BY started_at DESC LIMIT %s""",
            (limit,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/session/{session_id}")
async def api_session(session_id: str):
    detail = get_session_detail(session_id)
    if not detail:
        return {"error": "Session not found"}
    # Enrich with per-model cost from the trace if available.
    conn = get_connection()
    try:
        trace_row = conn.execute(
            "SELECT id FROM traces WHERE session_id = %s", (session_id,)
        ).fetchone()
        if trace_row:
            detail["per_model_cost"] = _model_cost_breakdown(trace_row["id"])
    finally:
        conn.close()
    return detail


# --- Search ---

@app.get("/api/search")
async def api_search(
    q: str = Query(...),
    limit: int = Query(default=10),
    project: str | None = Query(default=None),
):
    return do_search(q, limit=limit, project=project)


# --- Providers / Connections ---

PROVIDER_TEMPLATES = {
    "jsonl-session": {
        "name": "Session Files",
        "icon": "file",
        "default_path": "~/.sessions/projects",
        "description": "JSONL session files from the sessions directory.",
        "status_hint": "Auto-detected from ~/.sessions/",
    },
    "codex": {
        "name": "OpenAI Codex CLI",
        "icon": "openai",
        "default_path": "~/.codex/sessions",
        "description": "OpenAI's coding agent CLI. JSONL session logs organized by date.",
        "status_hint": "Auto-detected from ~/.codex/sessions/",
    },
    "copilot": {
        "name": "GitHub Copilot",
        "icon": "github",
        "default_path": "~/Library/Application Support/Code/User/workspaceStorage",
        "description": "GitHub Copilot Chat sessions from VS Code. Reads chatSessions per workspace.",
        "status_hint": "Auto-detected from VS Code workspaceStorage.",
    },
    "cursor": {
        "name": "Cursor",
        "icon": "cursor",
        "default_path": "~/Library/Application Support/Cursor/User/workspaceStorage",
        "description": "Cursor AI editor. Tracks chat and composer/agent interactions from SQLite.",
        "status_hint": "Auto-detected from Cursor Application Support.",
    },
    "windsurf": {
        "name": "Windsurf",
        "icon": "windsurf",
        "default_path": "~/Library/Application Support/Windsurf/User/workspaceStorage",
        "description": "Codeium's Windsurf editor. Tracks chat and Cascade agent sessions from SQLite.",
        "status_hint": "Auto-detected from Windsurf Application Support.",
    },
    "kiro": {
        "name": "Kiro",
        "icon": "kiro",
        "default_path": "~/Library/Application Support/Kiro/User/workspaceStorage",
        "description": "AWS Kiro — agent-first VS Code fork. Tracks chat and agent sessions from SQLite.",
        "status_hint": "Auto-detected from Kiro Application Support.",
    },
    "antigravity": {
        "name": "Google Antigravity",
        "icon": "antigravity",
        "default_path": "~/Library/Application Support/Antigravity/User/workspaceStorage",
        "description": "Google's Antigravity agent IDE. Tracks chat and agent sessions from SQLite (VS Code fork layout).",
        "status_hint": "Auto-detected from Antigravity Application Support.",
    },
    "gemini": {
        "name": "Gemini Code Assist",
        "icon": "gemini",
        "default_path": "~/Library/Application Support/Code/User/workspaceStorage",
        "description": "Google's Gemini Code Assist VS Code extension and Gemini CLI. Reads chat from extension storage and ~/.gemini/.",
        "status_hint": "Auto-detected from VS Code extension storage and ~/.gemini/.",
    },
    "opencode": {
        "name": "opencode",
        "icon": "opencode",
        "default_path": "~/.local/share/opencode/opencode.db",
        "description": "sst/opencode — open-source AI coding agent. Single SQLite DB carrying sessions, messages, and Vercel AI SDK part payloads with per-session token/cost roll-ups.",
        "status_hint": "Auto-detected from ~/.local/share/opencode/.",
    },
    "gitlab": {
        "name": "GitLab",
        "icon": "gitlab",
        "default_path": "https://gitlab.com",
        "description": "GitLab merge request threads. One MR = one session, with the description + every note (review comment, system event) as a message. Requires a personal access token with `read_api` scope.",
        "status_hint": "Connect with a GitLab URL + personal access token.",
        "remote": True,
        "credentials": [
            {"key": "gitlab_url", "label": "GitLab URL", "default": "https://gitlab.com", "placeholder": "https://gitlab.com"},
            {"key": "token", "label": "Personal access token", "secret": True, "placeholder": "glpat-..."},
            {"key": "scope", "label": "MR scope", "default": "assigned_to_me", "options": ["assigned_to_me", "created_by_me", "all"]},
        ],
    },
    "github": {
        "name": "GitHub",
        "icon": "github",
        "default_path": "https://api.github.com",
        "description": "GitHub pull request and issue threads. One PR/issue = one session, with the body + every comment + every PR review (and review comments) as messages. Requires a personal access token with `repo` scope (or `public_repo` for public-only).",
        "status_hint": "Connect with an API URL + personal access token.",
        "remote": True,
        "credentials": [
            {"key": "api_url", "label": "API URL", "default": "https://api.github.com", "placeholder": "https://api.github.com"},
            {"key": "token", "label": "Personal access token", "secret": True, "placeholder": "ghp_... or github_pat_..."},
            {"key": "scope", "label": "PR/issue scope", "default": "involves", "options": ["involves", "author", "assignee"]},
        ],
    },
}


@app.get("/api/providers")
async def api_providers():
    """Get all configured providers with status=connected."""
    conn = get_connection()
    rows = conn.execute(
        """SELECT id, name, type, status, data_path, icon, config,
                  session_count, last_synced_at, created_at
           FROM providers WHERE status = 'connected' ORDER BY created_at"""
    ).fetchall()
    conn.close()

    providers = [dict(r) for r in rows]

    for p in providers:
        tmpl = PROVIDER_TEMPLATES.get(p["type"], {})
        p["description"] = tmpl.get("description", "")
        p["status_hint"] = tmpl.get("status_hint", "")

    return providers


@app.get("/api/providers/available")
async def api_available_providers():
    """Get all available provider types that can be connected."""
    from spooling.providers import get_all_providers

    conn = get_connection()
    existing = conn.execute("SELECT type FROM providers WHERE status = 'connected'").fetchall()
    conn.close()
    existing_types = {r["type"] for r in existing}

    all_providers = get_all_providers()
    available = []
    for type_id, tmpl in PROVIDER_TEMPLATES.items():
        provider = all_providers.get(type_id)
        detected = provider.is_available() if provider else False
        file_count = len(provider.discover_session_files()) if (provider and detected) else 0
        available.append({
            "type": type_id,
            "name": tmpl["name"],
            "icon": tmpl["icon"],
            "default_path": tmpl["default_path"],
            "description": tmpl["description"],
            "connected": type_id in existing_types,
            "detected": detected,
            "file_count": file_count,
            "remote": bool(tmpl.get("remote")),
            "credentials": tmpl.get("credentials", []),
        })
    return available


class ProviderCreate(BaseModel):
    type: str
    data_path: str | None = None
    config: dict | None = None


@app.post("/api/providers")
async def api_create_provider(body: ProviderCreate):
    """Connect a new provider."""
    tmpl = PROVIDER_TEMPLATES.get(body.type)
    if not tmpl:
        return {"error": f"Unknown provider type: {body.type}"}

    provider_id = body.type
    data_path = body.data_path or tmpl["default_path"]
    config_json = json.dumps(body.config or {})

    conn = get_connection()
    conn.execute(
        """INSERT INTO providers (id, name, type, data_path, icon, config, status)
           VALUES (%s, %s, %s, %s, %s, %s, 'connected')
           ON CONFLICT (id) DO UPDATE SET data_path = %s, config = %s, status = 'connected'""",
        (provider_id, tmpl["name"], body.type, data_path, tmpl["icon"], config_json, data_path, config_json),
    )
    conn.commit()
    conn.close()

    return {"id": provider_id, "name": tmpl["name"], "status": "connected"}


class SyncRequest(BaseModel):
    provider: str | None = None
    embed: bool = False


@app.post("/api/sync")
async def api_sync(body: SyncRequest):
    """Trigger a sync for all or a specific provider."""
    from spooling.ingest import sync as do_sync
    import threading

    # Run sync in a background thread so the API doesn't block
    result = {"status": "syncing", "provider": body.provider or "all"}

    def run_sync():
        try:
            do_sync(embed=body.embed, provider_filter=body.provider)
        except Exception as e:
            print(f"Sync error: {e}")

    thread = threading.Thread(target=run_sync, daemon=True)
    thread.start()
    # Wait briefly so fast syncs complete before response
    thread.join(timeout=30)

    if thread.is_alive():
        return {"status": "syncing", "message": "Sync is running in the background"}

    return {"status": "complete", "message": "Sync finished"}


@app.post("/api/providers/{provider_id}/sync")
async def api_sync_provider(provider_id: str):
    """Trigger a sync for a specific provider."""
    from spooling.ingest import sync as do_sync
    import threading

    def run_sync():
        try:
            do_sync(embed=False, provider_filter=provider_id)
        except Exception as e:
            print(f"Sync error for {provider_id}: {e}")

    thread = threading.Thread(target=run_sync, daemon=True)
    thread.start()
    thread.join(timeout=30)

    if thread.is_alive():
        return {"status": "syncing", "message": f"Syncing {provider_id} in the background"}

    return {"status": "complete", "message": f"Sync for {provider_id} finished"}


@app.delete("/api/providers/{provider_id}")
async def api_delete_provider(provider_id: str):
    """Disconnect a provider."""
    conn = get_connection()
    conn.execute("UPDATE providers SET status = 'disconnected' WHERE id = %s", (provider_id,))
    conn.commit()
    conn.close()
    return {"status": "disconnected"}


# --- Traces / Spans / Evals ---

@app.get("/api/traces")
async def api_traces(
    limit: int = Query(default=500, le=10000),
    offset: int = Query(default=0, ge=0),
    provider: str | None = Query(default=None),
    project: str | None = Query(default=None),
    session_id: str | None = Query(default=None),
    since_days: int | None = Query(default=None),
    vendor: str | None = Query(default=None),
):
    """List traces with optional filters. Returns up to `limit` rows plus
    a total count so the GUI can show "X of Y".

    The `vendor` filter is span-scoped: it matches any trace that has at
    least one span tagged with the given vendor (linear, github, slack,
    etc.), so the Top Providers pills on the GUI can drill into their
    backing traces by click.
    """
    conn = get_connection()
    clauses = ["TRUE"]
    params: list = []
    if provider:
        clauses.append("t.provider_id = %s")
        params.append(provider)
    if project:
        clauses.append("t.project = %s")
        params.append(project)
    if session_id:
        clauses.append("t.session_id = %s")
        params.append(session_id)
    if since_days:
        clauses.append("t.started_at >= now() - make_interval(days => %s)")
        params.append(since_days)
    if vendor:
        clauses.append(
            "EXISTS (SELECT 1 FROM spans s WHERE s.trace_id = t.id AND s.vendor = %s)"
        )
        params.append(vendor)
    where = "WHERE " + " AND ".join(clauses)

    total_row = conn.execute(
        f"SELECT COUNT(*) AS n FROM traces t {where}",
        tuple(params),
    ).fetchone()
    total = int((total_row or {}).get("n") or 0)

    rows = conn.execute(
        f"""SELECT t.id, t.session_id, t.provider_id, t.project, t.title, t.started_at, t.ended_at,
                   t.duration_ms, t.span_count, t.agent_count, t.tool_count, t.llm_count, t.error_count,
                   t.total_input_tokens, t.total_output_tokens, t.total_cost_usd, t.model
           FROM traces t {where} ORDER BY t.started_at DESC NULLS LAST LIMIT %s OFFSET %s""",
        tuple(params + [limit, offset]),
    ).fetchall()
    conn.close()
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "rows": [dict(r) for r in rows],
    }


@app.get("/api/traces/{trace_id}")
async def api_trace_detail(trace_id: str):
    conn = get_connection()
    trace = conn.execute("SELECT * FROM traces WHERE id = %s", (trace_id,)).fetchone()
    if not trace:
        conn.close()
        return {"error": "trace not found"}

    spans = conn.execute(
        """SELECT id, trace_id, parent_id, kind, name, status,
                  started_at, ended_at, duration_ms, depth, sequence,
                  input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
                  cost_usd, model, tool_name, tool_input, tool_output, tool_is_error,
                  agent_type, agent_prompt, attrs
           FROM spans WHERE trace_id = %s ORDER BY sequence""",
        (trace_id,),
    ).fetchall()

    evals = conn.execute(
        """SELECT e.id, e.rubric_id, r.name AS rubric_name, e.span_id, e.score,
                  e.passed, e.label, e.rationale, e.run_at
           FROM evals e LEFT JOIN eval_rubrics r ON r.id = e.rubric_id
           WHERE e.trace_id = %s ORDER BY e.run_at DESC""",
        (trace_id,),
    ).fetchall()
    conn.close()

    trace_dict = dict(trace)
    breakdown = _breakdown_cost_for_trace(trace_dict)
    trace_dict["cost_breakdown"] = breakdown

    model_breakdown = _model_cost_breakdown(trace["id"])
    trace_dict["per_model_cost"] = model_breakdown
    # Recompute total_cost_usd from the live breakdown so the header number
    # stays in sync with the tooltip components even when rows were ingested
    # under an older pricing formula.
    trace_dict["total_cost_usd"] = round(
        breakdown["input"] + breakdown["output"]
        + breakdown["cache_read"] + breakdown["cache_write"],
        6,
    )
    return {
        "trace": trace_dict,
        "spans": [dict(s) for s in spans],
        "evals": [dict(e) for e in evals],
    }


def _breakdown_cost_for_trace(trace: dict) -> dict:
    """Split a trace's total cost into input / output / cache_read / cache_write.

    Looks up per-token rates via spooling.pricing (LiteLLM-backed) so the
    GUI can render a breakdown tooltip without having to know model
    pricing, and so old rows with stale stored totals get re-priced at
    read time against the current rate card.
    """
    from spooling.pricing import get_rates

    model = trace.get("model") or ""
    rates = get_rates(model)
    in_tok = int(trace.get("total_input_tokens") or 0)
    out_tok = int(trace.get("total_output_tokens") or 0)
    cr = int(trace.get("total_cache_read_tokens") or 0)
    cw = int(trace.get("total_cache_write_tokens") or 0)

    return {
        "input": round(in_tok * rates.input, 6),
        "output": round(out_tok * rates.output, 6),
        "cache_write": round(cw * rates.cache_write, 6),
        "cache_read": round(cr * rates.cache_read, 6),
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "cache_read_tokens": cr,
        "cache_write_tokens": cw,
        "model": model or None,
    }


def _model_cost_breakdown(trace_id: str) -> dict:
    """Aggregate llm_call span cost by model for a given trace."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT model,
                      COUNT(*) AS calls,
                      COALESCE(SUM(input_tokens), 0) AS input_tokens,
                      COALESCE(SUM(output_tokens), 0) AS output_tokens,
                      COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                      COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens,
                      COALESCE(SUM(cost_usd), 0) AS cost
               FROM spans
               WHERE trace_id = %s AND kind = 'llm_call' AND model IS NOT NULL
               GROUP BY model
               ORDER BY cost DESC""",
            (trace_id,),
        ).fetchall()
        return {
            "models": [
                {
                    "model": r["model"],
                    "calls": r["calls"],
                    "input_tokens": r["input_tokens"],
                    "output_tokens": r["output_tokens"],
                    "cache_read_tokens": r["cache_read_tokens"],
                    "cache_write_tokens": r["cache_write_tokens"],
                    "cost": round(float(r["cost"]), 6),
                }
                for r in rows
            ],
        }
    finally:
        conn.close()


@app.get("/api/session/{session_id}/trace")
async def api_session_trace(session_id: str):
    """Look up a trace by its session_id (convenience for the session detail UI)."""
    conn = get_connection()
    row = conn.execute("SELECT id FROM traces WHERE session_id = %s", (session_id,)).fetchone()
    conn.close()
    if not row:
        return {"error": "no trace for session"}
    return await api_trace_detail(row["id"])


@app.get("/api/spans/{span_id}")
async def api_span_detail(span_id: str):
    conn = get_connection()
    span = conn.execute("SELECT * FROM spans WHERE id = %s", (span_id,)).fetchone()
    if not span:
        conn.close()
        return {"error": "span not found"}
    events = conn.execute(
        "SELECT name, timestamp, attrs FROM span_events WHERE span_id = %s ORDER BY timestamp",
        (span_id,),
    ).fetchall()
    evals = conn.execute(
        """SELECT e.id, e.rubric_id, r.name AS rubric_name, e.score, e.passed,
                  e.label, e.rationale, e.run_at
           FROM evals e LEFT JOIN eval_rubrics r ON r.id = e.rubric_id
           WHERE e.span_id = %s ORDER BY e.run_at DESC""",
        (span_id,),
    ).fetchall()
    conn.close()
    return {
        "span": dict(span),
        "events": [dict(e) for e in events],
        "evals": [dict(e) for e in evals],
    }


@app.get("/api/evals/rubrics")
async def api_eval_rubrics():
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, name, description, kind, target_kind, config FROM eval_rubrics ORDER BY id"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/evals")
async def api_evals(
    limit: int = Query(default=500, le=10000),
    offset: int = Query(default=0, ge=0),
    rubric: str | None = Query(default=None),
    trace_id: str | None = Query(default=None),
    session_id: str | None = Query(default=None),
    provider: str | None = Query(default=None),
    passed: str | None = Query(default=None),  # "true" | "false" | "null"
    search: str | None = Query(default=None),
    since_days: int | None = Query(default=None),
):
    """List eval runs with filters. Joins traces so each row carries the
    session_id, provider_id and project it graded, and returns a total
    count so the GUI can paginate/display 'X of Y'."""
    conn = get_connection()
    clauses: list[str] = []
    params: list = []
    if rubric:
        clauses.append("e.rubric_id = %s")
        params.append(rubric)
    if trace_id:
        clauses.append("e.trace_id = %s")
        params.append(trace_id)
    if session_id:
        clauses.append("t.session_id = %s")
        params.append(session_id)
    if provider:
        clauses.append("t.provider_id = %s")
        params.append(provider)
    if passed == "true":
        clauses.append("e.passed IS TRUE")
    elif passed == "false":
        clauses.append("e.passed IS FALSE")
    elif passed == "null":
        clauses.append("e.passed IS NULL")
    if since_days:
        clauses.append("e.run_at >= now() - make_interval(days => %s)")
        params.append(since_days)
    if search:
        like = f"%{search}%"
        clauses.append(
            "(e.trace_id ILIKE %s OR t.session_id ILIKE %s OR t.project ILIKE %s OR e.label ILIKE %s OR e.rationale ILIKE %s)"
        )
        params.extend([like, like, like, like, like])
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    total_row = conn.execute(
        f"""SELECT COUNT(*) AS n
            FROM evals e
            LEFT JOIN traces t ON t.id = e.trace_id
            {where}""",
        tuple(params),
    ).fetchone()
    total = int((total_row or {}).get("n") or 0)

    rows = conn.execute(
        f"""SELECT e.id, e.rubric_id, r.name AS rubric_name, e.trace_id, e.span_id,
                   e.score, e.passed, e.label, e.rationale, e.run_at,
                   e.judge_model,
                   t.session_id, t.provider_id, t.project, t.title AS trace_title
            FROM evals e
            LEFT JOIN eval_rubrics r ON r.id = e.rubric_id
            LEFT JOIN traces t ON t.id = e.trace_id
            {where}
            ORDER BY e.run_at DESC LIMIT %s OFFSET %s""",
        tuple(params + [limit, offset]),
    ).fetchall()
    conn.close()
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "rows": [dict(r) for r in rows],
    }


class SpanIn(BaseModel):
    id: str | None = None
    parent_id: str | None = None
    kind: str  # session|agent|tool|llm_call|eval|step
    name: str
    status: str = "ok"
    started_at: datetime | None = None
    ended_at: datetime | None = None
    sequence: int | None = None
    depth: int | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    model: str | None = None
    tool_name: str | None = None
    tool_input: dict | None = None
    tool_output: str | None = None
    tool_is_error: bool | None = None
    agent_type: str | None = None
    agent_prompt: str | None = None
    vendor: str | None = None
    category: str | None = None
    attrs: dict = {}


class TraceIngest(BaseModel):
    id: str | None = None
    session_id: str
    provider_id: str
    project: str | None = None
    title: str | None = None
    cwd: str | None = None
    git_branch: str | None = None
    model: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    attrs: dict = {}
    spans: list[SpanIn]


def _rehydrate_trace(payload: TraceIngest) -> Trace:
    """Turn a validated TraceIngest into a Trace with Span objects."""
    from spooling.classifiers import classify

    trace_id = payload.id or f"trace-{payload.session_id}"
    trace = Trace(
        id=trace_id,
        session_id=payload.session_id,
        provider_id=payload.provider_id,
        project=payload.project,
        title=payload.title,
        cwd=payload.cwd,
        git_branch=payload.git_branch,
        model=payload.model,
        started_at=payload.started_at,
        ended_at=payload.ended_at,
        attrs=dict(payload.attrs or {}),
    )

    # First pass: materialize spans without children
    span_objs: list[Span] = []
    for i, s in enumerate(payload.spans):
        try:
            kind = SpanKind(s.kind)
        except ValueError:
            kind = SpanKind.STEP
        try:
            status = SpanStatus(s.status)
        except ValueError:
            status = SpanStatus.OK

        vendor = s.vendor
        category = s.category
        if kind == SpanKind.TOOL and s.tool_name and not (vendor and category):
            cls = classify(s.tool_name)
            vendor = vendor or cls.vendor
            category = category or cls.category
        if kind == SpanKind.AGENT and not vendor:
            vendor = "agent"
            category = category or "agent"

        span = Span(
            id=s.id or f"span-{trace_id}-{i}",
            trace_id=trace_id,
            parent_id=s.parent_id,
            kind=kind,
            name=s.name,
            status=status,
            started_at=s.started_at,
            ended_at=s.ended_at,
            depth=s.depth or 0,
            sequence=s.sequence if s.sequence is not None else i,
            input_tokens=s.input_tokens,
            output_tokens=s.output_tokens,
            cache_read_tokens=s.cache_read_tokens,
            cache_write_tokens=s.cache_write_tokens,
            cost_usd=s.cost_usd,
            model=s.model,
            tool_name=s.tool_name,
            tool_input=s.tool_input,
            tool_output=s.tool_output,
            tool_is_error=s.tool_is_error,
            agent_type=s.agent_type,
            agent_prompt=s.agent_prompt,
            vendor=vendor,
            category=category,
            attrs=dict(s.attrs or {}),
        )
        span_objs.append(span)

    # Compute depth if clients didn't set it.
    by_id = {sp.id: sp for sp in span_objs}
    def _depth(sp: Span, seen: set) -> int:
        if sp.parent_id and sp.parent_id in by_id and sp.parent_id not in seen:
            seen.add(sp.parent_id)
            return _depth(by_id[sp.parent_id], seen) + 1
        return 0
    for sp in span_objs:
        if sp.depth == 0 and sp.parent_id:
            sp.depth = _depth(sp, {sp.id})

    # Pick root: first SESSION span, else first without parent_id.
    root = next((sp for sp in span_objs if sp.kind == SpanKind.SESSION), None)
    if root is None:
        root = next((sp for sp in span_objs if not sp.parent_id), None)
    trace.root = root

    # Infer trace start/end if missing.
    if not trace.started_at:
        starts = [sp.started_at for sp in span_objs if sp.started_at]
        if starts:
            trace.started_at = min(starts)
    if not trace.ended_at:
        ends = [sp.ended_at for sp in span_objs if sp.ended_at]
        if ends:
            trace.ended_at = max(ends)

    trace.spans = span_objs
    return trace


@app.post("/api/traces/ingest")
async def api_traces_ingest(body: TraceIngest):
    """Accept a Trace JSON payload from a third-party agent/SDK.

    Writes to the same traces/spans tables used by provider-based sync.
    Idempotent on trace id; re-posting overwrites spans for that trace.
    Also creates a lightweight row in `sessions` so the legacy session
    detail UI can find it.
    """
    if not body.spans:
        return {"status": "error", "message": "no spans in payload"}

    trace = _rehydrate_trace(body)

    # Token + cost roll-ups for the trace row.
    from spooling.tracing import compute_trace_metrics
    m = compute_trace_metrics(trace)

    conn = get_connection()
    try:
        # Upsert a minimal legacy session row so /api/sessions picks it up.
        conn.execute(
            """INSERT INTO sessions (
                id, provider_id, project, cwd, git_branch, started_at, ended_at,
                message_count, tool_call_count, estimated_input_tokens,
                estimated_output_tokens, estimated_cost_usd, model, title
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                provider_id = EXCLUDED.provider_id,
                started_at = EXCLUDED.started_at,
                ended_at = EXCLUDED.ended_at,
                tool_call_count = EXCLUDED.tool_call_count,
                estimated_input_tokens = EXCLUDED.estimated_input_tokens,
                estimated_output_tokens = EXCLUDED.estimated_output_tokens,
                estimated_cost_usd = EXCLUDED.estimated_cost_usd,
                model = EXCLUDED.model,
                title = EXCLUDED.title""",
            (
                trace.session_id, trace.provider_id, trace.project,
                trace.cwd, trace.git_branch, trace.started_at, trace.ended_at,
                0, m["tool_count"], m["input_tokens"], m["output_tokens"],
                m["cost_usd"], trace.model, trace.title,
            ),
        )
        _store_trace(conn, trace)
        conn.commit()
    finally:
        conn.close()

    return {
        "status": "ok",
        "trace_id": trace.id,
        "spans": len(trace.spans),
        "vendors": m["vendor_count"],
        "top_vendors": m["top_vendors"],
    }


class EvalRunRequest(BaseModel):
    rubric_id: str
    trace_id: str | None = None
    span_id: str | None = None
    days: int | None = None


@app.post("/api/evals/run")
async def api_eval_run(body: EvalRunRequest):
    from spooling.evals import run_rubric, run_rubric_bulk
    from datetime import datetime, timezone, timedelta

    if body.trace_id:
        eval_id = run_rubric(body.rubric_id, body.trace_id, body.span_id)
        return {"status": "ok" if eval_id else "skipped", "eval_id": eval_id}

    since = None
    if body.days:
        since = datetime.now(timezone.utc) - timedelta(days=body.days)
    return run_rubric_bulk(body.rubric_id, since=since)


@app.get("/api/observability/summary")
async def api_observability_summary(provider: str | None = Query(default=None)):
    """Top-line observability stats for the dashboard."""
    conn = get_connection()
    where = "WHERE provider_id = %s" if provider else ""
    params: tuple = (provider,) if provider else ()
    row = conn.execute(
        f"""SELECT
               COUNT(*) AS traces,
               COALESCE(SUM(span_count), 0) AS spans,
               COALESCE(SUM(agent_count), 0) AS agents,
               COALESCE(SUM(tool_count), 0) AS tools,
               COALESCE(SUM(llm_count), 0) AS llm_calls,
               COALESCE(SUM(error_count), 0) AS errors,
               COALESCE(SUM(total_cost_usd), 0) AS cost
           FROM traces {where}""",
        params,
    ).fetchone()

    top_agents = conn.execute(
        """SELECT agent_type, COUNT(*) AS uses
           FROM spans WHERE kind = 'agent' AND agent_type IS NOT NULL
           GROUP BY agent_type ORDER BY uses DESC LIMIT 10"""
    ).fetchall()

    top_tools = conn.execute(
        """SELECT tool_name,
                  COUNT(*) AS uses,
                  SUM(CASE WHEN tool_is_error THEN 1 ELSE 0 END) AS errors
           FROM spans WHERE kind = 'tool' AND tool_name IS NOT NULL
           GROUP BY tool_name ORDER BY uses DESC LIMIT 10"""
    ).fetchall()

    top_vendors = conn.execute(
        """SELECT vendor, category,
                  COUNT(*) AS uses,
                  SUM(CASE WHEN tool_is_error THEN 1 ELSE 0 END) AS errors,
                  COUNT(DISTINCT trace_id) AS traces
           FROM spans WHERE kind = 'tool' AND vendor IS NOT NULL
             AND vendor NOT IN ('filesystem', 'shell', 'search', 'unknown')
           GROUP BY vendor, category ORDER BY uses DESC LIMIT 15"""
    ).fetchall()
    conn.close()

    return {
        "summary": dict(row) if row else {},
        "top_agents": [dict(r) for r in top_agents],
        "top_tools": [dict(r) for r in top_tools],
        "top_vendors": [dict(r) for r in top_vendors],
    }


# --- Tool usage breakdown ---

@app.get("/api/tools")
async def api_tools(limit: int = Query(default=20), provider: str | None = Query(default=None)):
    conn = get_connection()
    if provider:
        rows = conn.execute(
            """SELECT tc.tool_name, COUNT(*) AS uses,
                      COUNT(DISTINCT tc.session_id) AS sessions
               FROM tool_calls tc
               JOIN sessions s ON s.id = tc.session_id
               WHERE s.provider_id = %s
               GROUP BY tc.tool_name ORDER BY uses DESC LIMIT %s""",
            (provider, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT tool_name, COUNT(*) AS uses,
                      COUNT(DISTINCT session_id) AS sessions
               FROM tool_calls GROUP BY tool_name ORDER BY uses DESC LIMIT %s""",
            (limit,),
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# --- Chat Agent ---

class ChatRequest(BaseModel):
    messages: list[dict]
    provider: str | None = None
    chat_session_id: str | None = None
    agent_ids: list[str] | None = None
    enabled_tools: list[str] | None = None


@app.post("/api/chat")
async def api_chat(body: ChatRequest):
    from fastapi.responses import StreamingResponse
    from spooling.agent import chat_stream

    async def event_stream():
        async for event in chat_stream(
            messages=body.messages,
            chat_session_id=body.chat_session_id,
            agent_ids=body.agent_ids,
            enabled_tools=body.enabled_tools,
        ):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# --- Chat Session History ---

@app.get("/api/chat/sessions")
async def api_chat_sessions(limit: int = Query(default=30)):
    conn = get_connection()
    rows = conn.execute(
        """SELECT id, title, model, provider, message_count, created_at, updated_at
           FROM chat_sessions ORDER BY updated_at DESC LIMIT %s""",
        (limit,),
    ).fetchall()
    conn.close()
    return {"sessions": [dict(r) for r in rows]}


@app.get("/api/chat/sessions/{session_id}")
async def api_chat_session(session_id: str):
    conn = get_connection()
    session = conn.execute(
        "SELECT id, title, model, provider, message_count, created_at FROM chat_sessions WHERE id = %s",
        (session_id,),
    ).fetchone()
    if not session:
        conn.close()
        return {"error": "Chat session not found"}

    messages = conn.execute(
        "SELECT role, content, created_at FROM chat_messages WHERE chat_session_id = %s ORDER BY created_at",
        (session_id,),
    ).fetchall()
    conn.close()

    return {
        "session": dict(session),
        "messages": [dict(m) for m in messages],
    }


@app.delete("/api/chat/sessions/{session_id}")
async def api_delete_chat_session(session_id: str):
    conn = get_connection()
    conn.execute("DELETE FROM chat_sessions WHERE id = %s", (session_id,))
    conn.commit()
    conn.close()
    return {"status": "deleted"}


# --- Settings ---

class SettingsUpdate(BaseModel):
    provider: str | None = None
    model: str | None = None
    anthropic_api_key: str | None = None
    ollama_url: str | None = None
    openai_base_url: str | None = None
    openai_api_key: str | None = None


@app.get("/api/settings")
async def api_settings():
    """Get current agent settings."""
    conn = get_connection()
    row = conn.execute(
        "SELECT config FROM providers WHERE id = 'spooling-agent'"
    ).fetchone()
    conn.close()

    if row and row["config"]:
        config = row["config"] if isinstance(row["config"], dict) else {}
        if config.get("anthropic_api_key"):
            key = config["anthropic_api_key"]
            config["anthropic_api_key_masked"] = f"{key[:8]}...{key[-4:]}" if len(key) > 12 else "***"
            del config["anthropic_api_key"]
        if config.get("openai_api_key"):
            key = config["openai_api_key"]
            config["openai_api_key_masked"] = f"{key[:8]}...{key[-4:]}" if len(key) > 12 else "***"
            del config["openai_api_key"]
        return config

    return {"provider": "ollama", "model": "gemma3:4b", "ollama_url": "http://localhost:11434"}


@app.post("/api/settings")
async def api_update_settings(body: SettingsUpdate):
    """Update agent settings."""
    conn = get_connection()

    row = conn.execute(
        "SELECT config FROM providers WHERE id = 'spooling-agent'"
    ).fetchone()

    if row:
        config = row["config"] if isinstance(row["config"], dict) else {}
    else:
        config = {}
        conn.execute(
            "INSERT INTO providers (id, name, type, status, icon) VALUES ('spooling-agent', 'Spooling Agent', 'agent', 'connected', 'spooling')"
        )

    if body.provider is not None:
        config["provider"] = body.provider
    if body.model is not None:
        config["model"] = body.model
    if body.anthropic_api_key is not None:
        config["anthropic_api_key"] = body.anthropic_api_key
    if body.ollama_url is not None:
        config["ollama_url"] = body.ollama_url
    if body.openai_base_url is not None:
        config["openai_base_url"] = body.openai_base_url
    if body.openai_api_key is not None:
        config["openai_api_key"] = body.openai_api_key

    import json as _json
    conn.execute(
        "UPDATE providers SET config = %s WHERE id = 'spooling-agent'",
        (_json.dumps(config),),
    )
    conn.commit()
    conn.close()
    return {"status": "updated"}


@app.get("/api/settings/agents")
async def api_settings_agents():
    """Aggregate status of all three spool agents (chat, judge, mcp)."""
    import httpx as _httpx
    import os as _os

    conn = get_connection()
    row = conn.execute("SELECT config FROM providers WHERE id = 'spooling-agent'").fetchone()
    conn.close()
    cfg = (row["config"] if row and isinstance(row.get("config"), dict) else {}) if row else {}

    chat_provider = cfg.get("provider") or ("anthropic" if _os.getenv("ANTHROPIC_API_KEY") else "ollama")
    chat_model = cfg.get("model") or "gemma3:4b"
    ollama_url = cfg.get("ollama_url") or "http://localhost:11434"
    openai_base_url = cfg.get("openai_base_url", "").rstrip("/")
    openai_api_key = cfg.get("openai_api_key", "")

    # Probe Ollama once for both chat (if ollama) and judge.
    ollama_status = "disconnected"
    ollama_models: list[str] = []
    try:
        async with _httpx.AsyncClient(timeout=3) as client:
            resp = await client.get(f"{ollama_url}/api/tags")
            resp.raise_for_status()
            ollama_models = [m["name"] for m in resp.json().get("models", [])]
            ollama_status = "connected"
    except Exception:
        pass

    anthropic_key = cfg.get("anthropic_api_key") or _os.getenv("ANTHROPIC_API_KEY")

    # Chat agent
    if chat_provider == "ollama":
        chat_connected = ollama_status == "connected" and chat_model in ollama_models
    elif chat_provider == "anthropic":
        chat_connected = bool(anthropic_key)
    elif chat_provider == "openai_compatible":
        chat_connected = bool(openai_base_url)
    else:
        chat_connected = False

    chat_agent = {
        "name": "Spooling Assistant",
        "role": "chat",
        "provider": chat_provider,
        "model": chat_model,
        "connected": chat_connected,
        "ollama_url": ollama_url if chat_provider == "ollama" else None,
        "has_key": bool(anthropic_key) if chat_provider == "anthropic" else None,
        "openai_base_url": openai_base_url if chat_provider == "openai_compatible" else None,
        "purpose": "RAG chat over your session history. Backs the /chat page.",
        "endpoint": "/chat",
    }

    # Judge agent — reads from spooling.evals defaults
    judge_model = cfg.get("judge_model") or _os.getenv("SPOOLING_JUDGE_MODEL", "qwen2.5:7b")
    judge_connected = ollama_status == "connected" and judge_model in ollama_models
    judge_agent = {
        "name": "Strands Eval Judge",
        "role": "judge",
        "provider": "ollama",
        "model": judge_model,
        "connected": judge_connected,
        "ollama_url": ollama_url,
        "purpose": "Backs every LLM-as-judge rubric in Strands (Helpfulness, Coherence, Trajectory, Tool Selection, etc.). Needs a tool-capable model.",
        "endpoint": "/evals",
        "note": None if judge_connected else (
            f"Pull the judge model: ollama pull {judge_model}"
            if ollama_status == "connected"
            else "Ollama not running. Start it with 'ollama serve'."
        ),
    }

    # MCP server — streamable-HTTP at http://127.0.0.1:3004/mcp
    from spooling.mcp_server import MCP_HOST, MCP_PORT, MCP_URL
    import socket as _socket

    mcp_tools = [
        "list_traces", "get_trace", "search_sessions", "get_stats",
        "get_top_vendors", "list_evals", "list_rubrics", "run_eval",
    ]

    mcp_connected = False
    try:
        with _socket.create_connection((MCP_HOST, MCP_PORT), timeout=0.5):
            mcp_connected = True
    except OSError:
        pass

    mcp_agent = {
        "name": "Spooling MCP Server",
        "role": "mcp",
        "transport": "streamable-http",
        "tools": mcp_tools,
        "url": MCP_URL,
        "host": MCP_HOST,
        "port": MCP_PORT,
        "purpose": "Exposes Spooling as an MCP context source over streamable-HTTP. Any agent (Codex, Cursor, web agents) can connect by URL.",
        "connected": mcp_connected,
    }

    return {
        "chat": chat_agent,
        "judge": judge_agent,
        "mcp": mcp_agent,
        "ollama": {
            "status": ollama_status,
            "url": ollama_url,
            "models": ollama_models,
        },
    }


@app.get("/api/connectors")
async def api_connectors_list():
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT id, name, url, transport, status, last_error, last_checked_at, created_at,
                      slug, tool_count, tools_json,
                      CASE WHEN auth_header IS NOT NULL AND auth_header <> '' THEN true ELSE false END AS has_auth
               FROM mcp_connectors ORDER BY created_at ASC"""
        ).fetchall()
        return {"connectors": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.post("/api/connectors")
async def api_connectors_upsert(body: dict):
    cid = str(body.get("id") or "").strip()
    name = str(body.get("name") or "").strip()
    url = str(body.get("url") or "").strip()
    auth_header = str(body.get("auth_header") or "").strip() or None
    transport = str(body.get("transport") or "streamable-http").strip()
    if not cid or not name or not url:
        return JSONResponse({"error": "id, name, and url are required"}, status_code=400)

    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO mcp_connectors (id, name, url, auth_header, transport, status)
               VALUES (%s, %s, %s, %s, %s, 'disconnected')
               ON CONFLICT (id) DO UPDATE SET
                 name = EXCLUDED.name,
                 url = EXCLUDED.url,
                 auth_header = COALESCE(EXCLUDED.auth_header, mcp_connectors.auth_header),
                 transport = EXCLUDED.transport""",
            (cid, name, url, auth_header, transport),
        )
        conn.commit()
        return {"ok": True, "id": cid}
    finally:
        conn.close()


@app.delete("/api/connectors/{connector_id}")
async def api_connectors_delete(connector_id: str):
    conn = get_connection()
    try:
        conn.execute("DELETE FROM mcp_connectors WHERE id = %s", (connector_id,))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.post("/api/connectors/{connector_id}/test")
async def api_connectors_test(connector_id: str):
    import httpx as _httpx
    import json as _json
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT url, auth_header, slug FROM mcp_connectors WHERE id = %s", (connector_id,)
        ).fetchone()
        if not row:
            return JSONResponse({"error": "not_found"}, status_code=404)

        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if row.get("auth_header"):
            headers["Authorization"] = row["auth_header"]

        url = row["url"]

        async def _rpc(method: str, params: dict | None = None) -> dict | None:
            payload = {
                "jsonrpc": "2.0",
                "id": 1 if method == "initialize" else 2,
                "method": method,
                "params": params or {},
            }
            try:
                async with _httpx.AsyncClient(timeout=10) as client:
                    resp = await client.post(url, headers=headers, json=payload)
                if not resp.is_success:
                    return None
                data = resp.json()
                return data.get("result")
            except Exception:
                return None

        # Step 1: Initialize handshake
        init_result = await _rpc("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "spooling", "version": "0.1.0"},
        })
        if not init_result:
            conn.execute(
                "UPDATE mcp_connectors SET status = 'error', last_error = 'Initialize failed', last_checked_at = now() WHERE id = %s",
                (connector_id,),
            )
            conn.commit()
            return {"ok": False, "error": "initialize_failed"}

        # Step 2: Discover tools
        tools_result = await _rpc("tools/list")
        tools = []
        if tools_result and "tools" in tools_result:
            tools = tools_result["tools"]

        # Update connector with tool cache
        slug = row.get("slug") or connector_id
        conn.execute(
            """UPDATE mcp_connectors
               SET status = 'connected', last_error = NULL, last_checked_at = now(),
                   tools_json = %s::jsonb, tool_count = %s, slug = COALESCE(slug, %s)
               WHERE id = %s""",
            (_json.dumps(tools), len(tools), slug, connector_id),
        )
        conn.commit()
        return {"ok": True, "tools": len(tools), "tool_names": [t.get("name") for t in tools]}
    finally:
        conn.close()


@app.get("/api/chat/status")
async def api_chat_status():
    """Model picker status matching cloud's /api/chat/status."""
    import os as _os
    import httpx as _httpx
    conn = get_connection()
    row = conn.execute("SELECT config FROM providers WHERE id = 'spooling-agent'").fetchone()
    conn.close()
    cfg = (row["config"] if row and isinstance(row.get("config"), dict) else {}) if row else {}
    current = cfg.get("model", "gemma3:4b")
    api_key = cfg.get("anthropic_api_key") or _os.getenv("ANTHROPIC_API_KEY")
    ollama_url = cfg.get("ollama_url", "http://localhost:11434")
    openai_base_url = cfg.get("openai_base_url", "").rstrip("/")
    openai_api_key = cfg.get("openai_api_key", "")

    ollama_ok = False
    ollama_models = []
    try:
        async with _httpx.AsyncClient(timeout=3) as client:
            resp = await client.get(f"{ollama_url}/api/tags")
            if resp.is_success:
                ollama_models = [m["name"] for m in resp.json().get("models", [])]
                ollama_ok = True
    except Exception:
        pass

    models = []
    for m_name in ["gemma3:4b", "gemma3:12b", "llama3.2:3b", "qwen2.5:7b", "qwen2.5:14b"]:
        models.append({
            "id": m_name,
            "label": m_name,
            "provider": "ollama",
            "available": m_name in ollama_models,
        })
    for m_name in ["claude-sonnet-4-20250514", "claude-haiku-3-5-20241022", "claude-opus-4-20250514"]:
        models.append({
            "id": m_name,
            "label": m_name,
            "provider": "anthropic",
            "available": bool(api_key),
            "requiresKey": "Anthropic",
        })

    byok = []
    if api_key:
        byok.append("anthropic")
    if openai_api_key or openai_base_url:
        byok.append("openai_compatible")

    return {
        "current": current,
        "models": models,
        "ollama_ok": ollama_ok,
        "byok": byok,
        "has_anthropic": bool(api_key),
        "openai_compatible": bool(openai_base_url),
    }


@app.post("/api/settings/agent")
async def api_settings_agent(body: dict):
    """Update chat model (used by cloud model picker)."""
    conn = get_connection()
    chat_model = body.get("chat_model")
    if chat_model:
        row = conn.execute("SELECT config FROM providers WHERE id = 'spooling-agent'").fetchone()
        cfg = (row["config"] if row and isinstance(row.get("config"), dict) else {}) if row else {}
        cfg["model"] = chat_model
        conn.execute("UPDATE providers SET config = %s WHERE id = 'spooling-agent'", (json.dumps(cfg),))
        conn.commit()
    conn.close()
    return {"status": "ok"}


@app.get("/api/settings/check-ollama")
async def api_check_ollama():
    """Check if Ollama is running and what models are available."""
    import httpx as _httpx
    conn = get_connection()
    row = conn.execute("SELECT config FROM providers WHERE id = 'spooling-agent'").fetchone()
    conn.close()
    config = (row["config"] if row and isinstance(row.get("config"), dict) else {}) if row else {}
    base_url = config.get("ollama_url", "http://localhost:11434")

    try:
        async with _httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{base_url}/api/tags")
            resp.raise_for_status()
            models = [m["name"] for m in resp.json().get("models", [])]
            return {"status": "connected", "models": models, "url": base_url}
    except Exception:
        return {"status": "disconnected", "models": [], "url": base_url}
