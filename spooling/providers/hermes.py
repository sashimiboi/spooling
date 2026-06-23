"""Hermes Agent (Nous Research) session parser.

Hermes stores everything in ``~/.hermes/state.db`` (SQLite, WAL mode).
The relevant tables are:

* ``sessions`` — one row per conversation. Carries source (cli, telegram,
  discord, …), model, title, directory/cwd, token counts, cost.
* ``messages`` — one row per user/assistant/tool/system turn. ``tool_calls``
  is JSON, ``content`` is the message text, ``reasoning`` carries CoT text.
* ``messages_fts`` / ``messages_fts_trigram`` — FTS5 full-text search.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from spooling.parser import ParsedMessage, ParsedSession, ToolCallDetail
from spooling.providers.base import Provider
from spooling.tracing import build_flat_trace_from_messages


_HERMES_HOME = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
_DB_PATH = _HERMES_HOME / "state.db"


def _ts_to_dt(ts: float | None) -> datetime | None:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


class HermesProvider(Provider):
    """Filesystem provider for Hermes Agent (~/.hermes/state.db)."""

    type_id = "hermes"
    name = "Hermes Agent"

    def default_data_path(self) -> Path:
        return _DB_PATH

    def is_available(self) -> bool:
        return _DB_PATH.exists()

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
                "SELECT id, source, model, title, cwd, started_at, ended_at, "
                "message_count, tool_call_count, input_tokens, output_tokens, "
                "cache_read_tokens, cache_write_tokens, reasoning_tokens, "
                "estimated_cost_usd, actual_cost_usd, cost_status, "
                "system_prompt "
                "FROM sessions "
                "WHERE archived = 0 "
                "ORDER BY started_at"
            ).fetchall()

            message_rows_raw = conn.execute(
                "SELECT id, session_id, role, content, tool_calls, tool_name, "
                "timestamp, reasoning, token_count, finish_reason, active "
                "FROM messages WHERE active = 1 ORDER BY session_id, timestamp, id"
            ).fetchall()

            msgs_by_session: dict[str, list[sqlite3.Row]] = {}
            for mr in message_rows_raw:
                msgs_by_session.setdefault(mr["session_id"], []).append(mr)

            for s in session_rows:
                sid = s["id"]
                msg_rows = msgs_by_session.get(sid, [])
                if not msg_rows:
                    continue

                cwd = s["cwd"] or None
                project = Path(cwd).name if cwd else s["source"] or "hermes"
                source = s["source"] or "cli"
                model = s["model"] or None

                messages: list[ParsedMessage] = []
                for mr in msg_rows:
                    role = mr["role"] or "user"
                    content = mr["content"] or ""

                    reasoning = mr["reasoning"] or ""
                    full_content = content
                    if reasoning:
                        full_content = f"[reasoning] {reasoning}\n{content}"

                    tool_calls_raw = mr["tool_calls"]
                    tool_calls_data: list[dict] = []
                    if tool_calls_raw:
                        try:
                            tc = json.loads(tool_calls_raw)
                            if isinstance(tc, list):
                                tool_calls_data = tc
                            elif isinstance(tc, dict):
                                tool_calls_data = [tc]
                        except (json.JSONDecodeError, TypeError):
                            pass

                    tool_details: list[ToolCallDetail] = []
                    tools_used: list[str] = []
                    for tc in tool_calls_data:
                        name = tc.get("function", {}).get("name") or tc.get("name") or "tool"
                        arguments = tc.get("function", {}).get("arguments") or tc.get("arguments") or {}
                        if isinstance(arguments, str):
                            try:
                                arguments = json.loads(arguments)
                            except (json.JSONDecodeError, TypeError):
                                arguments = {"raw": arguments}
                        call_id = tc.get("id") or tc.get("tool_call_id") or ""
                        tool_details.append(ToolCallDetail(
                            tool_use_id=call_id,
                            name=name,
                            input_summary=str(arguments)[:200],
                            tool_input_raw=arguments if isinstance(arguments, dict) else None,
                        ))
                        tools_used.append(name)

                    token_count = mr["token_count"] or 0
                    if token_count == 0 and full_content:
                        token_count = max(0, len(full_content) // 4)

                    messages.append(ParsedMessage(
                        uuid=str(mr["id"]),
                        session_id=sid,
                        role=role,
                        content=full_content,
                        timestamp=_ts_to_dt(mr["timestamp"]),
                        cwd=cwd,
                        tools_used=tools_used,
                        tool_details=tool_details,
                        estimated_tokens=token_count,
                    ))

                if not messages:
                    continue

                session = ParsedSession(
                    session_id=sid,
                    project=project,
                    messages=messages,
                    started_at=_ts_to_dt(s["started_at"]),
                    ended_at=_ts_to_dt(s["ended_at"]),
                    cwd=cwd,
                    title=s["title"] or None,
                    model=model,
                    provider_id="hermes",
                )
                session.trace = build_flat_trace_from_messages(
                    provider_id="hermes",
                    session_id=sid,
                    project=project,
                    title=session.title,
                    messages=messages,
                    cwd=cwd,
                    model=model,
                    agent_name=source,
                )
                sessions.append(session)
        except sqlite3.Error:
            return sessions
        finally:
            conn.close()

        return sessions
