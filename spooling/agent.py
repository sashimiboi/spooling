"""Chat agent that can talk to your Spooling data.

Supports Ollama (free/local) and Anthropic API (bring your own key).
Uses RAG - retrieves relevant session context from pgvector before answering.
"""

import json
import os
from datetime import datetime, timezone

import httpx

from spooling.search import search as semantic_search
from spooling.stats import get_overview, get_daily_stats, get_session_detail
from spooling.db import get_connection

SYSTEM_PROMPT = """You are Spooling Assistant, an AI that helps users understand their coding session history.
You have access to the user's session data from AI coding tools (Codex, Cursor, etc.).

When answering questions:
- Be concise and specific. Reference actual session data, projects, and timestamps.
- Every session has a UUID session ID (e.g. 8cb6f9d2-0214-4a4c-b731-d6d9c7914836). Always include the full session ID when referencing sessions.
- If you don't have enough context, say so rather than guessing.
- Format costs as dollars, tokens with commas, and dates in a readable format.
- When listing sessions or results, keep it scannable - use short descriptions.

You're given relevant context from the user's session history below. Use it to answer their question."""


def _build_context(query: str) -> str:
    """Build RAG context from session data relevant to the query."""
    parts = []

    # 1. Semantic search for relevant session chunks
    results = semantic_search(query, limit=6)
    if results:
        parts.append("## Relevant Session Context")
        for r in results:
            ts = r["timestamp"][:16] if r["timestamp"] else "unknown"
            parts.append(
                f"- [session_id: {r['session_id']}] [{r['role']}] ({r['project']} | {ts} | {r['similarity']:.0%} match): "
                f"{r['content'][:300]}"
            )

    # 2. Overview stats
    overview = get_overview()
    s = overview["summary"]
    total_tokens = (s.get("total_input_tokens", 0) or 0) + (s.get("total_output_tokens", 0) or 0)
    parts.append("\n## Usage Summary")
    parts.append(
        f"- Total sessions: {s.get('total_sessions', 0)}\n"
        f"- Total messages: {s.get('total_messages', 0)}\n"
        f"- Total tool calls: {s.get('total_tool_calls', 0)}\n"
        f"- Est. tokens: {total_tokens:,}\n"
        f"- Est. cost: ${float(s.get('total_cost_usd', 0)):.2f}"
    )

    # 3. Projects
    if overview["projects"]:
        parts.append("\n## Projects")
        for p in overview["projects"][:8]:
            parts.append(f"- {p['project']}: {p['sessions']} sessions, {p['messages']} msgs, ${float(p['cost'] or 0):.2f}")

    # 4. Recent sessions (with session IDs)
    if overview["recent_sessions"]:
        parts.append("\n## Recent Sessions")
        for r in overview["recent_sessions"][:6]:
            ts = r["started_at"].strftime("%m/%d %H:%M") if r["started_at"] else ""
            parts.append(f"- [session_id: {r['id']}] [{ts}] {r['project']}: {r['title'][:60]} ({r['message_count']} msgs)")

    # 5. Daily stats (last 7 days)
    daily = get_daily_stats(days=7)
    if daily:
        parts.append("\n## Last 7 Days")
        for d in daily:
            parts.append(f"- {d['day']}: {d['sessions']} sessions, {d['messages']} msgs, ${float(d['cost']):.2f}")

    return "\n".join(parts)


def _get_config() -> dict:
    """Load agent config from the database."""
    try:
        conn = get_connection()
        row = conn.execute(
            "SELECT config FROM providers WHERE id = 'spooling-agent'"
        ).fetchone()
        conn.close()
        if row and row["config"]:
            return row["config"] if isinstance(row["config"], dict) else json.loads(row["config"])
    except Exception:
        pass
    return {}


def _get_provider() -> str:
    """Determine which LLM provider to use."""
    config = _get_config()
    provider = config.get("provider", "")
    if provider:
        return provider
    # Check env vars
    if os.getenv("ANTHROPIC_API_KEY"):
        return "anthropic"
    return "ollama"


async def chat(messages: list[dict], provider: str | None = None) -> dict:
    """Send messages to the configured LLM.

    Returns a dict with the assistant `response` and the retrieved `sources`
    (the RAG chunks fed into the prompt), so the UI can show what the agent
    actually had in context when answering.
    """
    config = _get_config()
    prov = provider or _get_provider()

    user_msg = ""
    for m in reversed(messages):
        if m["role"] == "user":
            user_msg = m["content"]
            break

    sources = semantic_search(user_msg, limit=6) if user_msg else []
    context = _build_context(user_msg) if user_msg else ""

    system = SYSTEM_PROMPT
    if context:
        system += f"\n\n---\n\n{context}"

    if prov == "anthropic":
        response = await _chat_anthropic(system, messages, config)
    else:
        response = await _chat_ollama(system, messages, config)

    return {
        "response": response,
        "sources": [
            {
                "session_id": s["session_id"],
                "project": s.get("project"),
                "role": s.get("role"),
                "timestamp": s.get("timestamp"),
                "similarity": s.get("similarity"),
                "title": s.get("title"),
                "excerpt": (s.get("content") or "")[:200],
            }
            for s in sources
        ],
    }


async def _chat_anthropic(system: str, messages: list[dict], config: dict) -> str:
    """Chat using Anthropic API."""
    import anthropic

    api_key = config.get("anthropic_api_key") or os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return "No Anthropic API key configured. Add one in Settings or set ANTHROPIC_API_KEY env var."

    model = config.get("model", "claude-sonnet-4-20250514")

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=model,
        max_tokens=1024,
        system=system,
        messages=[{"role": m["role"], "content": m["content"]} for m in messages],
    )

    return response.content[0].text


async def _chat_ollama(system: str, messages: list[dict], config: dict) -> str:
    """Chat using local Ollama."""
    base_url = config.get("ollama_url", "http://localhost:11434")
    model = config.get("model", "gemma3:4b")

    ollama_messages = [{"role": "system", "content": system}]
    for m in messages:
        ollama_messages.append({"role": m["role"], "content": m["content"]})

    async with httpx.AsyncClient(timeout=180) as client:
        try:
            resp = await client.post(
                f"{base_url}/api/chat",
                json={
                    "model": model,
                    "messages": ollama_messages,
                    "stream": False,
                    "options": {
                        "num_ctx": 8192,
                        "num_predict": 1024,
                        "temperature": 0.3,
                    },
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("message", {}).get("content", "No response from Ollama.")
        except httpx.ConnectError:
            return (
                f"Cannot connect to Ollama at {base_url}. "
                "Make sure Ollama is running (`ollama serve`) and you've pulled a model (`ollama pull gemma3:4b`).\n\n"
                "Or switch to Anthropic in Settings with your API key."
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return (
                    f"Model '{model}' not found in Ollama. "
                    f"Pull it with: `ollama pull {model}`"
                )
            return f"Ollama error: {e.response.text}"
