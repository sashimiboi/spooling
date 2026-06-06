"""opencode (sst/opencode) session parser.

opencode stores everything in a single SQLite database at
``~/.local/share/opencode/opencode.db`` (Drizzle-managed). The relevant
tables are:

* ``session``  — one row per conversation. Carries title, directory,
  agent, model (JSON ``{"id": "...", "providerID": "..."}``), and
  roll-up totals: ``cost``, ``tokens_input``, ``tokens_output``,
  ``tokens_reasoning``, ``tokens_cache_read``, ``tokens_cache_write``.
* ``message``  — one row per user/assistant turn. ``data`` is JSON
  with role, model, and (for assistants) ``tokens`` + ``time``.
* ``part``     — content blocks for a message, ordered by
  ``time_created``. ``data`` follows the Vercel AI SDK UIMessage shape:
  ``{"type": "text", "text": "..."}``,
  ``{"type": "reasoning", "text": "..."}``,
  ``{"type": "step-start" | "step-finish", ...}``, and tool parts
  named ``tool-<toolName>`` (or generic ``tool-call`` / ``tool-result``).

Times are unix-epoch milliseconds. Project worktree is on the session
row (``session.directory``); ``project.worktree`` defaults to ``/`` for
the "global" project and isn't useful as a cwd.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from spooling.config import OPENCODE_DB
from spooling.parser import ParsedMessage, ParsedSession, ToolCallDetail, _summarize_tool_input
from spooling.providers.base import Provider
from spooling.tracing import build_flat_trace_from_messages


def _ms_to_dt(ms: int | float | None) -> datetime | None:
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _extract_model(session_model_raw: str | None, first_assistant_data: dict | None) -> str | None:
    """opencode stores model as JSON ``{"id": "...", "providerID": "..."}``
    on the session row, and as flat ``modelID`` / ``providerID`` keys on
    assistant message ``data``. Prefer the session row, fall back to the
    first assistant turn."""
    if session_model_raw:
        try:
            m = json.loads(session_model_raw)
            mid = m.get("id") or m.get("modelID")
            if mid:
                return mid
        except (json.JSONDecodeError, TypeError):
            pass
    if first_assistant_data:
        return first_assistant_data.get("modelID")
    return None


def _part_is_tool(part_type: str) -> bool:
    """Vercel AI SDK uses ``tool-<toolName>`` for tool-bound parts and
    ``tool-call`` / ``tool-invocation`` / ``tool-result`` for the older
    generic shape. Either gets bucketed as a tool call here."""
    if not part_type:
        return False
    return (
        part_type.startswith("tool-")
        or part_type in ("tool", "tool-invocation")
    )


def _tool_name_from_part(part_type: str, data: dict) -> str:
    # ``tool-foo`` → ``foo``; otherwise fall back to whichever name-ish
    # field opencode wrote (toolName / name / tool).
    if part_type and part_type.startswith("tool-") and part_type not in ("tool-call", "tool-invocation", "tool-result"):
        return part_type.removeprefix("tool-")
    return data.get("toolName") or data.get("name") or data.get("tool") or "tool"


def _build_message(
    message_id: str,
    session_id: str,
    role: str,
    timestamp_ms: int | None,
    parts: list[dict],
    cwd: str | None,
) -> ParsedMessage:
    """Collapse a message's parts into a ParsedMessage.

    Visible text (text + reasoning) is concatenated into ``content`` for
    search/embedding; tool calls become structured ``tool_details``.
    Reasoning gets a ``[reasoning]`` marker so a user grepping "reasoning"
    can find it without bloating the prose with chain-of-thought.
    """
    text_chunks: list[str] = []
    calls: list[ToolCallDetail] = []
    results: dict[str, str] = {}

    for p in parts:
        if not isinstance(p, dict):
            continue
        ptype = p.get("type", "")

        if ptype == "text":
            txt = p.get("text", "")
            if txt:
                text_chunks.append(txt)
            continue

        if ptype == "reasoning":
            txt = p.get("text", "")
            if txt:
                text_chunks.append(f"[reasoning] {txt}")
            continue

        if ptype in ("step-start", "step-finish"):
            continue

        if _part_is_tool(ptype):
            name = _tool_name_from_part(ptype, p)
            state = p.get("state") or {}
            inp = state.get("input") or p.get("input") or p.get("args") or {}
            call_id = p.get("toolCallId") or p.get("id") or p.get("tool_use_id") or p.get("callID") or ""
            output = state.get("output") or p.get("output") or p.get("result")

            input_summary = ""
            if isinstance(inp, dict) and inp:
                input_summary = _summarize_tool_input(name, inp)

            result_preview = ""
            if output is not None:
                if isinstance(output, (dict, list)):
                    try:
                        result_preview = json.dumps(output)[:500]
                    except (TypeError, ValueError):
                        result_preview = str(output)[:500]
                else:
                    result_preview = str(output)[:500]

            calls.append(ToolCallDetail(
                tool_use_id=call_id,
                name=name,
                input_summary=input_summary,
                result_preview=result_preview,
            ))
            text_chunks.append(f"[tool: {name}]")
            if call_id and result_preview:
                results[call_id] = result_preview
            continue

        # Unknown part types: ignore silently, opencode may add new ones.

    content = "\n".join(text_chunks)
    return ParsedMessage(
        uuid=message_id,
        session_id=session_id,
        role=role,
        content=content,
        timestamp=_ms_to_dt(timestamp_ms),
        cwd=cwd,
        tools_used=[c.name for c in calls],
        tool_details=calls,
        estimated_tokens=max(0, len(content) // 4),
    )


class OpencodeProvider(Provider):
    """Filesystem provider for sst/opencode (single SQLite DB)."""

    type_id = "opencode"
    name = "opencode"

    def default_data_path(self) -> Path:
        return OPENCODE_DB

    def is_available(self) -> bool:
        return OPENCODE_DB.exists()

    def discover_session_files(self, data_path: Path | None = None) -> list[Path]:
        db = data_path or self.resolved_data_path()
        return [db] if db.exists() else []

    def parse_session_file(self, file_path: Path) -> list[ParsedSession]:
        try:
            conn = sqlite3.connect(f"file:{file_path}?mode=ro", uri=True)
        except sqlite3.Error:
            return []
        conn.row_factory = sqlite3.Row

        sessions: list[ParsedSession] = []
        try:
            session_rows = conn.execute(
                "SELECT id, directory, title, version, model, agent, "
                "time_created, time_updated FROM session ORDER BY time_created"
            ).fetchall()

            for s in session_rows:
                sid = s["id"]
                message_rows = conn.execute(
                    "SELECT id, time_created, data FROM message "
                    "WHERE session_id = ? ORDER BY time_created, id",
                    (sid,),
                ).fetchall()

                if not message_rows:
                    continue

                # Pull every part for this session in one query, group in Python.
                part_rows = conn.execute(
                    "SELECT message_id, time_created, data FROM part "
                    "WHERE session_id = ? ORDER BY message_id, time_created, id",
                    (sid,),
                ).fetchall()
                parts_by_msg: dict[str, list[dict]] = {}
                for pr in part_rows:
                    try:
                        parts_by_msg.setdefault(pr["message_id"], []).append(json.loads(pr["data"]))
                    except (json.JSONDecodeError, TypeError):
                        continue

                cwd = s["directory"] or None
                project = Path(cwd).name if cwd else "(unknown)"

                messages: list[ParsedMessage] = []
                first_assistant_data: dict | None = None
                for m in message_rows:
                    try:
                        mdata = json.loads(m["data"])
                    except (json.JSONDecodeError, TypeError):
                        continue
                    role = mdata.get("role")
                    if role not in ("user", "assistant"):
                        continue
                    if role == "assistant" and first_assistant_data is None:
                        first_assistant_data = mdata

                    messages.append(_build_message(
                        message_id=m["id"],
                        session_id=sid,
                        role=role,
                        timestamp_ms=m["time_created"],
                        parts=parts_by_msg.get(m["id"], []),
                        cwd=cwd,
                    ))

                if not messages:
                    continue

                model = _extract_model(s["model"], first_assistant_data)

                session = ParsedSession(
                    session_id=sid,
                    project=project,
                    messages=messages,
                    started_at=_ms_to_dt(s["time_created"]),
                    ended_at=_ms_to_dt(s["time_updated"]),
                    cwd=cwd,
                    title=s["title"] or None,
                    agent_version=s["version"] or None,
                    model=model,
                    provider_id="opencode",
                )
                session.trace = build_flat_trace_from_messages(
                    provider_id="opencode",
                    session_id=sid,
                    project=project,
                    title=session.title,
                    messages=messages,
                    cwd=cwd,
                    model=model,
                )
                sessions.append(session)
        except sqlite3.Error:
            return sessions
        finally:
            conn.close()

        return sessions
