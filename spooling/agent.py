"""Chat agent with streaming SSE, tool-use loop, and MCP connector support.

Supports Ollama (free/local) and Anthropic API (bring your own key).
When using Anthropic, the agent runs a tool-use loop with built-in session
tools AND any connected MCP connectors (GitHub, Linear, etc.).
"""

import json
import os
import uuid
from datetime import datetime, timezone, timedelta

import httpx

from spooling.db import get_connection
from spooling.search import search as semantic_search



SYSTEM_PROMPT = """You are Spooling Assistant, an AI that helps users understand their coding session history.
You have access to the user's session data from AI coding tools (Codex, Cursor, etc.).

When answering questions:
- Be concise and specific. Reference actual session data, projects, and timestamps.
- Every session has a UUID session ID (e.g. 8cb6f9d2-0214-4a4c-b731-d6d9c7914836). Always include the full session ID when referencing sessions.
- If you don't have enough context, say so rather than guessing.
- Format costs as dollars, tokens with commas, and dates in a readable format.
- When listing sessions or results, keep it scannable - use short descriptions.

You're given relevant context from the user's session history below. Use it to answer their question."""

TOOL_LOOP_MAX = 4

# ── Built-in session tools ──────────────────────────────────────────

SESSION_TOOLS = [
    {
        "name": "spool_search",
        "description": "Ranked message excerpts for a query.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural-language query."},
                "limit": {"type": "number", "description": "Max hits to return (1-30, default 10)."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "spool_recent_sessions",
        "description": "Newest sessions, optionally filtered by provider / days.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "number", "description": "Max sessions (1-50, default 10)."},
                "provider": {"type": "string", "description": "Filter to one provider id (claude-code, codex, cursor, copilot, windsurf)."},
                "days": {"type": "number", "description": "Only sessions started in the last N days."},
            },
        },
    },
    {
        "name": "spool_get_session",
        "description": "Full session metadata + ordered messages.",
        "input_schema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "Session UUID."},
                "message_limit": {"type": "number", "description": "Cap on messages returned (default 40)."},
            },
            "required": ["session_id"],
        },
    },
    {
        "name": "spool_workspace_stats",
        "description": "Counts, cost, per-provider rollup.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "spool_top_projects",
        "description": "Projects by spend + volume.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "number", "description": "Max projects (1-25, default 8)."},
            },
        },
    },
]

MCP_TOOL_PREFIX = "mcp__"


def make_qualified_tool_name(slug: str, tool_name: str) -> str:
    import re

    safe_slug = re.sub(r'[^a-zA-Z0-9_-]', '_', slug) if slug else "connector"
    prefix = f"{MCP_TOOL_PREFIX}{safe_slug}__"
    budget = 64 - len(prefix)
    safe_tool = re.sub(r'[^a-zA-Z0-9_-]', '_', tool_name)[:max(1, budget)]
    return prefix + safe_tool


def parse_qualified_tool_name(qualified: str) -> tuple[str, str] | None:
    if not qualified.startswith(MCP_TOOL_PREFIX):
        return None
    rest = qualified[len(MCP_TOOL_PREFIX):]
    sep = rest.find("__")
    if sep < 0:
        return None
    return rest[:sep], rest[sep + 2:]


# ── Context building ────────────────────────────────────────────────

def _build_context(query: str) -> dict:
    hits = semantic_search(query, limit=6) if query else []
    conn = get_connection()
    stats = conn.execute(
        """SELECT COUNT(*)::int AS sessions,
                  COALESCE(SUM(message_count), 0)::int AS messages,
                  COALESCE(SUM(estimated_cost_usd), 0)::float AS cost
           FROM sessions"""
    ).fetchone()
    recent = conn.execute(
        """SELECT id, provider_id, project, title, started_at, message_count, estimated_cost_usd
           FROM sessions ORDER BY started_at DESC NULLS LAST LIMIT 6"""
    ).fetchall()
    conn.close()

    context_parts = []
    context_parts.append(
        f"## Workspace\nSessions: {stats['sessions']}. Messages: {stats['messages']}. Total cost: ${float(stats['cost'] or 0):.2f}."
    )
    if hits:
        context_parts.append("## Relevant excerpts")
        for h in hits:
            context_parts.append(f"- [{h['session_id']}] {h['role']}: {(h.get('content') or '')[:200]}")
    if recent:
        context_parts.append("## Recent sessions")
        for r in recent:
            title = (r["title"] or "")[:60]
            started = r["started_at"].isoformat()[:10] if r["started_at"] else ""
            context_parts.append(
                f"- [{r['id']}] {r['provider_id']} {r['project'] or ''} {started}: {title}"
            )

    return {
        "system_context": "\n\n".join(context_parts),
        "sources": [
            {"session_id": h["session_id"], "project": h.get("project"), "role": h.get("role"),
             "timestamp": h.get("timestamp"), "similarity": h.get("similarity"),
             "title": h.get("title"), "excerpt": (h.get("content") or "")[:200]}
            for h in hits
        ],
        "stats": dict(stats) if stats else {},
    }


# ── MCP connector loading ───────────────────────────────────────────

def load_mcp_connectors() -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT id, slug, name, url, transport, auth_header, tools_json
               FROM mcp_connectors WHERE status = 'connected' AND tool_count > 0"""
        ).fetchall()
        return [
            {
                "id": r["id"],
                "slug": r.get("slug") or r["id"],
                "name": r["name"],
                "url": r["url"],
                "transport": r["transport"],
                "auth_header": r.get("auth_header"),
                "tools": list(r.get("tools_json") or []),
            }
            for r in rows
        ]
    finally:
        conn.close()


def build_mcp_anthropic_tools(connectors: list[dict]) -> list[dict]:
    out = []
    for c in connectors:
        for t in c["tools"]:
            qname = make_qualified_tool_name(c["slug"], t["name"])
            schema = t.get("inputSchema") or t.get("input_schema") or {}
            out.append({
                "name": qname,
                "description": f"[{c['name']}] {t.get('description') or t['name']}"[:1000],
                "input_schema": {
                    "type": "object",
                    "properties": schema.get("properties", {}),
                    **({"required": schema["required"]} if schema.get("required") else {}),
                },
            })
    return out


def build_mcp_openai_tools(connectors: list[dict]) -> list[dict]:
    out = []
    for c in connectors:
        for t in c["tools"]:
            qname = make_qualified_tool_name(c["slug"], t["name"])
            schema = t.get("inputSchema") or t.get("input_schema") or {}
            out.append({
                "name": qname,
                "description": f"[{c['name']}] {t.get('description') or t['name']}"[:1000],
                "parameters": {
                    "type": "object",
                    "properties": schema.get("properties", {}),
                    **({"required": schema["required"]} if schema.get("required") else {}),
                },
            })
    return out


async def call_mcp_tool_http(connector: dict, tool_name: str, args: dict) -> str:
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if connector.get("auth_header"):
        headers["Authorization"] = connector["auth_header"]

    payload = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": args},
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(connector["url"], headers=headers, json=payload)
            if resp.status_code >= 400:
                return json.dumps({"ok": False, "error": f"HTTP {resp.status_code}"})
            data = resp.json()
            result = data.get("result", {})
            content = result.get("content", [])
            text_parts = [
                c["text"] for c in content
                if isinstance(c, dict) and c.get("type") == "text"
            ]
            is_error = result.get("isError", False)
            return json.dumps({
                "ok": not is_error,
                "content": "\n".join(text_parts) or json.dumps(result),
            })
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)[:500]})


# ── Built-in tool execution ─────────────────────────────────────────

def _clamp(v, default, lo, hi):
    try:
        n = int(v) if v is not None else default
    except (ValueError, TypeError):
        n = default
    if not n:
        n = default
    return max(lo, min(n, hi))


async def execute_builtin_tool(name: str, args: dict) -> str:
    conn = get_connection()
    try:
        match name:
            case "spool_search":
                q = str(args.get("query", ""))[:500]
                if not q:
                    return json.dumps({"error": "query required"})
                limit = _clamp(args.get("limit"), 10, 1, 30)
                results = semantic_search(q, limit=limit)
                return json.dumps([
                    {
                        "session_id": r["session_id"],
                        "role": r.get("role"),
                        "project": r.get("project"),
                        "title": r.get("title"),
                        "timestamp": r.get("timestamp"),
                        "score": r.get("similarity"),
                        "content": (r.get("content") or "")[:500],
                    }
                    for r in results
                ])

            case "spool_recent_sessions":
                limit = _clamp(args.get("limit"), 10, 1, 50)
                provider = args.get("provider")
                days = args.get("days")
                where = "TRUE"
                params = []
                if provider:
                    where += " AND provider_id = %s"
                    params.append(provider)
                if days:
                    cutoff = datetime.now(timezone.utc) - timedelta(days=int(days))
                    where += " AND started_at >= %s"
                    params.append(cutoff)
                rows = conn.execute(
                    f"""SELECT id, provider_id, project, title, started_at, message_count, estimated_cost_usd
                        FROM sessions WHERE {where} ORDER BY started_at DESC NULLS LAST LIMIT %s""",
                    tuple(params + [limit]),
                ).fetchall()
                return json.dumps([dict(r) for r in rows])

            case "spool_get_session":
                sid = str(args.get("session_id", ""))
                if not sid:
                    return json.dumps({"error": "session_id required"})
                cap = _clamp(args.get("message_limit"), 40, 1, 200)
                session = conn.execute("SELECT * FROM sessions WHERE id = %s", (sid,)).fetchone()
                if not session:
                    return json.dumps({"error": "not_found", "session_id": sid})
                msgs = conn.execute(
                    """SELECT role, content, timestamp FROM messages
                       WHERE session_id = %s ORDER BY timestamp LIMIT %s""",
                    (sid, cap),
                ).fetchall()
                return json.dumps({
                    "session": dict(session),
                    "messages": [
                        {"role": m["role"], "content": (m.get("content") or "")[:2000],
                         "timestamp": m["timestamp"]}
                        for m in msgs
                    ],
                    "truncated": len(msgs) >= cap,
                })

            case "spool_workspace_stats":
                summary = conn.execute(
                    """SELECT COUNT(*)::int AS sessions,
                              COALESCE(SUM(message_count), 0)::int AS messages,
                              COALESCE(SUM(tool_call_count), 0)::int AS tool_calls,
                              COALESCE(SUM(estimated_input_tokens + estimated_output_tokens), 0)::bigint AS tokens,
                              COALESCE(SUM(estimated_cost_usd), 0)::float AS cost_usd
                       FROM sessions"""
                ).fetchone()
                providers = conn.execute(
                    """SELECT provider_id, COUNT(*)::int AS sessions,
                              COALESCE(SUM(message_count), 0)::int AS messages,
                              COALESCE(SUM(estimated_cost_usd), 0)::float AS cost
                       FROM sessions GROUP BY provider_id ORDER BY sessions DESC"""
                ).fetchall()
                return json.dumps({
                    "sessions": summary["sessions"],
                    "messages": summary["messages"],
                    "tool_calls": summary["tool_calls"],
                    "tokens": int(summary["tokens"] or 0),
                    "cost_usd": float(summary["cost_usd"] or 0),
                    "per_provider": [dict(r) for r in providers],
                })

            case "spool_top_projects":
                limit = _clamp(args.get("limit"), 8, 1, 25)
                rows = conn.execute(
                    """SELECT project, COUNT(*)::int AS sessions,
                              SUM(message_count)::int AS messages,
                              COALESCE(SUM(estimated_cost_usd), 0)::float AS cost
                       FROM sessions WHERE project IS NOT NULL AND project != ''
                       GROUP BY project ORDER BY cost DESC LIMIT %s""",
                    (limit,),
                ).fetchall()
                return json.dumps([dict(r) for r in rows])

            case _:
                return json.dumps({"error": "unknown_tool", "name": name})
    except Exception as e:
        return json.dumps({"error": "tool_failed", "detail": str(e)})
    finally:
        conn.close()


# ── Config helpers ──────────────────────────────────────────────────

def _get_config() -> dict:
    try:
        conn = get_connection()
        row = conn.execute(
            "SELECT config FROM providers WHERE id = 'spooling-agent'"
        ).fetchone()
        conn.close()
        if row and row["config"]:
            cfg = row["config"]
            if isinstance(cfg, str):
                cfg = json.loads(cfg)
            return cfg if isinstance(cfg, dict) else {}
    except Exception:
        pass
    return {}


def _get_provider(config: dict) -> str:
    p = config.get("provider", "")
    if p:
        return p
    return "anthropic" if os.getenv("ANTHROPIC_API_KEY") else "ollama"


# ── Streaming chat (SSE events) ─────────────────────────────────────

async def chat_stream(
    messages: list[dict],
    chat_session_id: str | None = None,
    agent_ids: list[str] | None = None,
    enabled_tools: list[str] | None = None,
):
    """Async generator yielding SSE event dicts.

    Events:
      {"type": "meta", "sources": [...], "req_id": "..."}
      {"type": "step", "step": {"kind": "workspace"|"search"|"model", "label": "...", "detail": "...", "done": bool}}
      {"type": "delta", "text": "..."}
      {"type": "error", "error": "...", "detail": "..."}
      {"type": "done", "chat_session_id": "...", "using": "anthropic"|"gemma"}
    """
    config = _get_config()
    prov = _get_provider(config)
    api_key = config.get("anthropic_api_key") or os.getenv("ANTHROPIC_API_KEY")
    model = config.get("model", "gemma3:4b")
    ollama_url = config.get("ollama_url", "http://localhost:11434")
    openai_base_url = config.get("openai_base_url", "").rstrip("/")
    openai_api_key = config.get("openai_api_key", "")

    # Determine which backend to use
    use_anthropic = prov == "anthropic" and api_key and not model.startswith("gemma")
    use_openai = prov == "openai_compatible" and openai_base_url
    hit_limit = 8 if (use_anthropic or use_openai) else 3
    recent_limit = 6 if (use_anthropic or use_openai) else 3
    hit_content_cap = 240 if (use_anthropic or use_openai) else 120

    last_user_msg = ""
    for m in reversed(messages):
        if m["role"] == "user":
            last_user_msg = m["content"]
            break

    # Build context in parallel
    hits = semantic_search(last_user_msg, limit=hit_limit) if last_user_msg else []
    conn = get_connection()
    stats = conn.execute(
        """SELECT COUNT(*)::int AS sessions,
                  COALESCE(SUM(message_count), 0)::int AS messages,
                  COALESCE(SUM(estimated_cost_usd), 0)::float AS cost
           FROM sessions"""
    ).fetchone()
    recent = conn.execute(
        """SELECT id, provider_id, project, title, started_at, message_count, estimated_cost_usd
           FROM sessions ORDER BY started_at DESC NULLS LAST LIMIT %s""",
        (recent_limit,),
    ).fetchall()
    conn.close()

    req_id = os.urandom(3).hex()

    # Build system context
    context_parts = []
    context_parts.append(
        f"## Workspace\nSessions: {stats['sessions']}. Messages: {stats['messages']}. Total cost: ${float(stats['cost'] or 0):.2f}."
    )
    if hits:
        context_parts.append("## Relevant excerpts")
        for h in hits:
            content = (h.get("content") or "")[:hit_content_cap]
            context_parts.append(f"- [{h['session_id']}] {h['role']}: {content}")
    if recent:
        context_parts.append("## Recent sessions")
        for r in recent:
            title = (r["title"] or "")[:60]
            started = r["started_at"].isoformat()[:10] if r["started_at"] else ""
            context_parts.append(
                f"- [{r['id']}] {r['provider_id']} {r['project'] or ''} {started}: {title}"
            )
    system_context = "\n\n".join(context_parts)
    system = f"{SYSTEM_PROMPT}\n\n---\n\n{system_context}"

    # Sources
    sources = [
        {"session_id": h["session_id"], "project": h.get("project"), "role": h.get("role"),
         "timestamp": h.get("timestamp"), "similarity": h.get("similarity"),
         "title": h.get("title"), "excerpt": (h.get("content") or "")[:200]}
        for h in hits
    ]

    # Step: meta
    yield {"type": "meta", "sources": sources, "req_id": req_id}

    # Step: workspace
    yield {
        "type": "step",
        "step": {
            "kind": "workspace",
            "label": "Read workspace",
            "detail": f"{stats['sessions']} sessions, {stats['messages']} messages, ${float(stats['cost'] or 0):.2f}",
            "done": True,
        },
    }

    # Step: search
    yield {
        "type": "step",
        "step": {
            "kind": "search",
            "label": "Searched messages",
            "detail": f"{len(hits)} relevant excerpt{'s' if len(hits) != 1 else ''}" if hits else "no excerpts matched",
            "done": True,
        },
    }

    assembled = ""
    persist_id = chat_session_id
    using = None

    try:
        if use_openai:
            # ── OpenAI-compatible API path with tool loop ──
            yield {
                "type": "step",
                "step": {
                    "kind": "model",
                    "label": f"Spooling Assistant via {model}",
                    "detail": "OpenAI-compatible + tools",
                    "done": False,
                },
            }
            using = "openai"

            # Load MCP connectors + build tools
            mcp_connectors = load_mcp_connectors()
            mcp_openai_tools = build_mcp_openai_tools(mcp_connectors)
            connector_by_slug = {c["slug"]: c for c in mcp_connectors}

            enabled_set = set(enabled_tools or [])
            builtin_active = [t for t in SESSION_TOOLS if not enabled_set or t["name"] in enabled_set]
            openai_tools_defs = builtin_active + mcp_openai_tools

            # Convert messages to OpenAI format
            convo = [{"role": "system", "content": system}]
            for m in messages:
                convo.append({"role": m["role"], "content": m["content"]})

            final_text = ""
            tool_err = None

            headers = {
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {openai_api_key}"} if openai_api_key else {}),
            }

            async with httpx.AsyncClient(timeout=120) as http:
                for turn in range(TOOL_LOOP_MAX):
                    body = {
                        "model": model,
                        "max_tokens": 2048,
                        "temperature": 0.3,
                        "messages": convo,
                    }
                    if openai_tools_defs:
                        body["tools"] = [{"type": "function", "function": t} for t in openai_tools_defs]
                        body["tool_choice"] = "auto"

                    try:
                        resp = await http.post(
                            f"{openai_base_url}/chat/completions",
                            headers=headers,
                            json=body,
                        )
                        if not resp.is_success:
                            tool_err = f"HTTP {resp.status_code}: {resp.text[:300]}"
                            yield {"type": "error", "error": "openai_upstream", "detail": tool_err}
                            break
                        payload = resp.json()
                    except Exception as e:
                        tool_err = str(e)[:400]
                        yield {"type": "error", "error": "openai_upstream", "detail": tool_err}
                        break

                    choice = payload.get("choices", [{}])[0]
                    msg = choice.get("message", {})
                    tool_calls = msg.get("tool_calls")
                    turn_text = msg.get("content", "") or ""

                    convo.append({"role": "assistant", "content": turn_text, **({"tool_calls": tool_calls} if tool_calls else {})})

                    if not tool_calls or choice.get("finish_reason") != "tool_calls":
                        final_text = turn_text
                        break

                    # Execute tools
                    for tc in tool_calls:
                        tname = tc["function"]["name"]
                        try:
                            tinput = json.loads(tc["function"]["arguments"] or "{}")
                        except json.JSONDecodeError:
                            tinput = {}
                        mcp_ref = parse_qualified_tool_name(tname)
                        label_name = f"{mcp_ref[0]}.{mcp_ref[1]}" if mcp_ref else tname

                        yield {
                            "type": "step",
                            "step": {
                                "kind": "tool_call",
                                "label": f"Tool: {label_name}",
                                "detail": json.dumps(tinput)[:120] if tinput else "",
                                "tool_input": json.dumps(tinput) if tinput else "",
                                "done": False,
                            },
                        }

                        if mcp_ref:
                            slug, tool_name = mcp_ref
                            c = connector_by_slug.get(slug)
                            if c:
                                result = await call_mcp_tool_http(c, tool_name, tinput)
                            else:
                                result = json.dumps({"ok": False, "error": "connector_not_found", "slug": slug})
                        else:
                            result = await execute_builtin_tool(tname, tinput)

                        yield {
                            "type": "step",
                            "step": {
                                "kind": "tool_call",
                                "label": f"Tool: {label_name}",
                                "detail": result[:200] if result else "done",
                                "tool_result": result[:2000] if result else "",
                                "done": True,
                            },
                        }

                        convo.append({
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": result,
                        })

            if not tool_err:
                if final_text:
                    assembled = final_text
                    yield {"type": "delta", "text": final_text}
                else:
                    yield {
                        "type": "error",
                        "error": "tool_loop_exhausted",
                        "detail": f"No final answer after {TOOL_LOOP_MAX} tool turns.",
                    }

        elif use_anthropic:
            # ── Anthropic path with tool loop ──
            yield {
                "type": "step",
                "step": {
                    "kind": "model",
                    "label": f"Spooling Assistant via {model}",
                    "detail": "anthropic + session tools",
                    "done": False,
                },
            }
            using = "anthropic"

            # Load MCP connectors + build tools
            mcp_connectors = load_mcp_connectors()
            mcp_tools = build_mcp_anthropic_tools(mcp_connectors)
            connector_by_slug = {c["slug"]: c for c in mcp_connectors}

            enabled_set = set(enabled_tools or [])
            builtin_active = [t for t in SESSION_TOOLS if not enabled_set or t["name"] in enabled_set]
            active_tools = builtin_active + mcp_tools

            # Convert messages to Anthropic API format
            convo = []
            for m in messages:
                role = "assistant" if m["role"] == "assistant" else "user"
                convo.append({"role": role, "content": m["content"]})

            # Tool-use loop (matching cloud's implementation, using raw HTTP)
            final_text = ""
            tool_err = None

            headers = {
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            }

            async with httpx.AsyncClient(timeout=120) as http:
                for turn in range(TOOL_LOOP_MAX):
                    body = {
                        "model": model,
                        "max_tokens": 2048,
                        "system": system,
                        "messages": convo,
                    }
                    if active_tools:
                        body["tools"] = active_tools

                    try:
                        resp = await http.post(
                            "https://api.anthropic.com/v1/messages",
                            headers=headers,
                            json=body,
                        )
                        if not resp.is_success:
                            tool_err = f"HTTP {resp.status_code}: {resp.text[:300]}"
                            yield {"type": "error", "error": "anthropic_upstream", "detail": tool_err}
                            break
                        payload = resp.json()
                    except Exception as e:
                        tool_err = str(e)[:400]
                        yield {"type": "error", "error": "anthropic_upstream", "detail": tool_err}
                        break

                    blocks = payload.get("content", [])
                    tool_uses = [b for b in blocks if b.get("type") == "tool_use"]
                    turn_text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")

                    # Persist assistant response in conversation
                    convo.append({"role": "assistant", "content": blocks})

                    if not tool_uses or payload.get("stop_reason") != "tool_use":
                        final_text = turn_text
                        break

                    # Execute tools
                    tool_results_content = []
                    for tu in tool_uses:
                        tname = tu.get("name", "unknown")
                        tinput = tu.get("input", {})
                        mcp_ref = parse_qualified_tool_name(tname)
                        label_name = f"{mcp_ref[0]}.{mcp_ref[1]}" if mcp_ref else tname

                        yield {
                            "type": "step",
                            "step": {
                                "kind": "tool_call",
                                "label": f"Tool: {label_name}",
                                "detail": json.dumps(tinput)[:120] if tinput else "",
                                "tool_input": json.dumps(tinput) if tinput else "",
                                "done": False,
                            },
                        }

                        if mcp_ref:
                            slug, tool_name = mcp_ref
                            c = connector_by_slug.get(slug)
                            if c:
                                result = await call_mcp_tool_http(c, tool_name, tinput)
                            else:
                                result = json.dumps({"ok": False, "error": "connector_not_found", "slug": slug})
                        else:
                            result = await execute_builtin_tool(tname, tinput)

                        yield {
                            "type": "step",
                            "step": {
                                "kind": "tool_call",
                                "label": f"Tool: {label_name}",
                                "detail": result[:200] if result else "done",
                                "tool_result": result[:2000] if result else "",
                                "done": True,
                            },
                        }

                        tool_results_content.append({
                            "type": "tool_result",
                            "tool_use_id": tu.get("id"),
                            "content": result,
                        })

                    # Feed tool results back
                    convo.append({"role": "user", "content": tool_results_content})

            if not tool_err:
                if final_text:
                    assembled = final_text
                    yield {"type": "delta", "text": final_text}
                else:
                    yield {
                        "type": "error",
                        "error": "tool_loop_exhausted",
                        "detail": f"No final answer after {TOOL_LOOP_MAX} tool turns.",
                    }

        else:
            # ── Ollama path with tool loop ──
            yield {
                "type": "step",
                "step": {
                    "kind": "model",
                    "label": f"Spooling Assistant via {model}",
                    "detail": "Ollama + tools",
                    "done": False,
                },
            }
            using = "gemma"

            # Load MCP connectors + build tools
            mcp_connectors = load_mcp_connectors()
            mcp_ollama_tools = build_mcp_openai_tools(mcp_connectors)
            connector_by_slug = {c["slug"]: c for c in mcp_connectors}

            enabled_set = set(enabled_tools or [])
            builtin_active = [t for t in SESSION_TOOLS if not enabled_set or t["name"] in enabled_set]
            ollama_tools_defs = builtin_active + mcp_ollama_tools

            convo = [{"role": "system", "content": system}]
            for m in messages:
                convo.append({"role": m["role"], "content": m["content"]})

            final_text = ""
            tool_err = None

            async with httpx.AsyncClient(timeout=180) as http:
                for turn in range(TOOL_LOOP_MAX):
                    body = {
                        "model": model,
                        "messages": convo,
                        "stream": False,
                        "options": {"num_ctx": 8192, "num_predict": 2048, "temperature": 0.3},
                    }
                    if ollama_tools_defs:
                        body["tools"] = ollama_tools_defs

                    try:
                        resp = await http.post(f"{ollama_url}/api/chat", json=body)
                        if not resp.is_success:
                            tool_err = f"HTTP {resp.status_code}: {resp.text[:300]}"
                            yield {"type": "error", "error": "ollama_upstream", "detail": tool_err}
                            break
                        data = resp.json()
                    except httpx.ConnectError:
                        tool_err = f"Cannot connect to Ollama at {ollama_url}."
                        yield {"type": "error", "error": "ollama_connect", "detail": tool_err}
                        break
                    except Exception as e:
                        tool_err = str(e)[:400]
                        yield {"type": "error", "error": "ollama_upstream", "detail": tool_err}
                        break

                    msg = data.get("message", {})
                    tool_calls = msg.get("tool_calls")
                    turn_text = msg.get("content", "") or ""

                    convo.append({"role": "assistant", "content": turn_text, **({"tool_calls": tool_calls} if tool_calls else {})})

                    if not tool_calls:
                        final_text = turn_text
                        break

                    # Execute tools
                    for tc in tool_calls:
                        tname = tc["function"]["name"]
                        raw_args = tc["function"].get("arguments", {})
                        tinput = raw_args if isinstance(raw_args, dict) else (json.loads(raw_args) if raw_args else {})
                        mcp_ref = parse_qualified_tool_name(tname)
                        label_name = f"{mcp_ref[0]}.{mcp_ref[1]}" if mcp_ref else tname

                        yield {
                            "type": "step",
                            "step": {
                                "kind": "tool_call",
                                "label": f"Tool: {label_name}",
                                "detail": json.dumps(tinput)[:120] if tinput else "",
                                "tool_input": json.dumps(tinput) if tinput else "",
                                "done": False,
                            },
                        }

                        if mcp_ref:
                            slug, tool_name = mcp_ref
                            c = connector_by_slug.get(slug)
                            if c:
                                result = await call_mcp_tool_http(c, tool_name, tinput)
                            else:
                                result = json.dumps({"ok": False, "error": "connector_not_found", "slug": slug})
                        else:
                            result = await execute_builtin_tool(tname, tinput)

                        yield {
                            "type": "step",
                            "step": {
                                "kind": "tool_call",
                                "label": f"Tool: {label_name}",
                                "detail": result[:200] if result else "done",
                                "tool_result": result[:2000] if result else "",
                                "done": True,
                            },
                        }

                        convo.append({
                            "role": "tool",
                            "content": result,
                        })

            if not tool_err:
                if final_text:
                    assembled = final_text
                    yield {"type": "delta", "text": final_text}
                else:
                    yield {
                        "type": "error",
                        "error": "tool_loop_exhausted",
                        "detail": f"No final answer after {TOOL_LOOP_MAX} tool turns.",
                    }

    except Exception as e:
        yield {"type": "error", "error": "unexpected", "detail": str(e)[:500]}

    # Persist chat session
    if assembled:
        conn = get_connection()
        try:
            if not persist_id:
                title = (last_user_msg or "New chat")[:80].replace("\n", " ").strip()
                new_id = str(uuid.uuid4())
                conn.execute(
                    """INSERT INTO chat_sessions (id, title, model, provider, message_count)
                       VALUES (%s, %s, %s, %s, 2)
                       ON CONFLICT (id) DO UPDATE SET updated_at = now(), message_count = chat_sessions.message_count + 2""",
                    (new_id, title, model, prov),
                )
                persist_id = new_id
            else:
                conn.execute(
                    "UPDATE chat_sessions SET updated_at = now(), message_count = message_count + 2 WHERE id = %s",
                    (persist_id,),
                )

            if last_user_msg:
                conn.execute(
                    "INSERT INTO chat_messages (chat_session_id, role, content) VALUES (%s, %s, %s)",
                    (persist_id, "user", last_user_msg),
                )
            conn.execute(
                "INSERT INTO chat_messages (chat_session_id, role, content) VALUES (%s, %s, %s)",
                (persist_id, "assistant", assembled),
            )
            conn.commit()
        except Exception as e:
            print(f"[chat {req_id}] persist failed: {e}")
        finally:
            conn.close()

    # Step: model done
    if assembled:
        yield {
            "type": "step",
            "step": {"kind": "model", "label": f"Finished ({using or 'unknown'})", "done": True},
        }

    yield {"type": "done", "chat_session_id": persist_id, "using": using}
