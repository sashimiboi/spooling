"""Snowflake Cortex Code session parser.

Cortex Code CLI stores conversations on disk at:

    ~/.snowflake/cortex/conversations/<uuid>.history.jsonl    # messages
    ~/.snowflake/cortex/conversations/<uuid>.json             # sidecar meta

Per-line message shape (JSONL):

    {
      "role": "user" | "assistant",
      "id": "msg_...",
      "content": [
        {"type": "text", "text": "...", "is_user_prompt": true, ...},
        {"type": "tool_use", "name": "sql_execute", "input": {...}, "id": "tu_..."},
        {"type": "tool_result", "tool_use_id": "tu_...", "content": "..."}
      ],
      "user_sent_time": "2026-05-15T23:59:18.306Z"   # user rows only
    }

This mirrors the Anthropic content-block convention closely enough that we
extract text, tool calls, and tool results the same way we do for Claude
Code, but the wrapping is flat (`content` at top level, no nested `message`
object) so we can't reuse `parse_session_file` from spool.parser directly.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from spool.config import CORTEX_CONVERSATIONS_DIR
from spool.parser import (
    ParsedMessage,
    ParsedSession,
    ToolCallDetail,
    _summarize_tool_input,
)
from spool.providers.base import Provider


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        # Cortex writes RFC 3339 with trailing Z; fromisoformat handles
        # offset-aware strings but treats "Z" as UTC in Python 3.11+.
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _extract_text(content: list) -> str:
    """Concatenate visible text blocks. Tool calls render as a marker so
    downstream search/embedding still has a hook for them; the structured
    detail lives on the ParsedMessage.tool_details list."""
    out: list[str] = []
    for block in content or []:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            # Skip internalOnly system reminders — they bloat the index
            # with framework boilerplate that has nothing to do with the
            # actual conversation.
            if block.get("internalOnly"):
                continue
            text = block.get("text", "")
            if text:
                out.append(text)
        elif btype == "tool_use":
            name = block.get("name", "tool")
            out.append(f"[tool: {name}]")
        # tool_result blocks are skipped — they're noisy and the preview
        # already lives on the matching tool_use via tool_details.
    return "\n".join(out)


def _extract_tool_calls(content: list) -> list[ToolCallDetail]:
    calls: list[ToolCallDetail] = []
    for block in content or []:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        name = block.get("name", "")
        inp = block.get("input", {}) or {}
        calls.append(ToolCallDetail(
            tool_use_id=block.get("id", ""),
            name=name,
            input_summary=_summarize_tool_input(name, inp) if isinstance(inp, dict) else "",
        ))
    return calls


def _extract_tool_results(content: list) -> dict[str, str]:
    """Map tool_use_id -> truncated preview text from any tool_result blocks
    in this message. Cortex puts tool results in the *user* message that
    follows the assistant's tool_use, matching Anthropic's convention."""
    out: dict[str, str] = {}
    for block in content or []:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        tool_id = block.get("tool_use_id") or block.get("id") or ""
        raw = block.get("content", "")
        if isinstance(raw, list):
            raw = "\n".join(
                b.get("text", "")
                for b in raw
                if isinstance(b, dict) and b.get("type") == "text"
            )
        if tool_id and isinstance(raw, str):
            out[tool_id] = raw[:500]
    return out


class CortexCodeProvider(Provider):
    """Filesystem provider for Snowflake Cortex Code CLI sessions."""

    type_id = "cortex-code"
    name = "Cortex Code"

    def default_data_path(self) -> Path:
        return CORTEX_CONVERSATIONS_DIR

    def discover_session_files(self, data_path: Path | None = None) -> list[Path]:
        base = data_path or self.resolved_data_path()
        if not base.exists():
            return []
        files = [f for f in base.glob("*.history.jsonl") if f.is_file()]
        return sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)

    def parse_session_file(self, file_path: Path) -> list[ParsedSession]:
        # The session id is the leading uuid in <uuid>.history.jsonl
        stem = file_path.stem  # e.g. "<uuid>.history"
        session_id = stem[:-len(".history")] if stem.endswith(".history") else stem

        # Pair with the sidecar metadata file (same uuid, .json)
        sidecar = file_path.parent / f"{session_id}.json"
        meta: dict = {}
        if sidecar.exists():
            try:
                meta = json.loads(sidecar.read_text())
            except (OSError, json.JSONDecodeError):
                meta = {}

        title = meta.get("title") or None
        cwd = meta.get("working_directory")
        git_branch = meta.get("git_branch")
        project = cwd or "(unknown)"
        started_at = _parse_ts(meta.get("created_at"))
        ended_at = _parse_ts(meta.get("last_updated"))

        messages: list[ParsedMessage] = []
        # We need a two-pass-ish view: when an assistant message contains a
        # tool_use, the next user message's tool_result fills in the preview.
        # We collect per-message tool detail lists, then patch previews in
        # after we've seen the following user message.
        pending_calls: list[ToolCallDetail] = []

        try:
            with file_path.open("r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    role = row.get("role")
                    if role not in ("user", "assistant"):
                        continue

                    content = row.get("content", [])
                    text = _extract_text(content if isinstance(content, list) else [])
                    calls = _extract_tool_calls(content if isinstance(content, list) else [])
                    results = _extract_tool_results(content if isinstance(content, list) else [])

                    # Patch previews onto the previous assistant turn's calls.
                    if role == "user" and results and pending_calls:
                        for tc in pending_calls:
                            if tc.tool_use_id in results:
                                tc.result_preview = results[tc.tool_use_id]

                    ts = _parse_ts(row.get("user_sent_time") or row.get("timestamp"))

                    msg = ParsedMessage(
                        uuid=row.get("id", ""),
                        session_id=session_id,
                        role=role,
                        content=text,
                        timestamp=ts,
                        cwd=cwd,
                        git_branch=git_branch,
                        tools_used=[c.name for c in calls],
                        tool_details=calls,
                        estimated_tokens=max(0, len(text) // 4),  # rough heuristic
                    )
                    messages.append(msg)

                    # Carry assistant tool calls forward so the next user
                    # row's tool_result can attach a preview.
                    pending_calls = calls if role == "assistant" else []
        except OSError:
            return []

        if not messages:
            return []

        # Fall back to first/last message timestamps when sidecar was missing.
        if started_at is None:
            for m in messages:
                if m.timestamp:
                    started_at = m.timestamp
                    break
        if ended_at is None:
            for m in reversed(messages):
                if m.timestamp:
                    ended_at = m.timestamp
                    break

        return [ParsedSession(
            session_id=session_id,
            project=project,
            messages=messages,
            started_at=started_at,
            ended_at=ended_at,
            cwd=cwd,
            git_branch=git_branch,
            title=title,
            provider_id="cortex-code",
        )]
