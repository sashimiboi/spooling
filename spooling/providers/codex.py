"""OpenAI Codex CLI session parser - reads JSONL from ~/.codex/sessions/."""

import json
import os
from dataclasses import field
from datetime import datetime, timezone
from pathlib import Path

from spooling.config import CHARS_PER_TOKEN
from spooling.parser import ParsedSession, ParsedMessage
from spooling.providers.base import Provider
from spooling.tracing import build_flat_trace_from_messages


CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
CODEX_SESSIONS_DIR = CODEX_HOME / "sessions"


class CodexProvider(Provider):
    type_id = "codex"
    name = "OpenAI Codex CLI"

    def default_data_path(self) -> Path:
        return CODEX_SESSIONS_DIR

    def discover_session_files(self, data_path: Path | None = None) -> list[Path]:
        base = data_path or self.resolved_data_path()
        if not base.exists():
            return []
        # Sessions are in YYYY/MM/DD/ subdirectories as rollout-*.jsonl
        files = list(base.rglob("rollout-*.jsonl"))
        return sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)

    def parse_session_file(self, file_path: Path) -> list[ParsedSession]:
        session = _parse_codex_session(file_path)
        if not session:
            return []
        session.trace = build_flat_trace_from_messages(
            provider_id="codex",
            session_id=session.session_id,
            project=session.project,
            title=session.title,
            messages=session.messages,
            cwd=session.cwd,
            git_branch=session.git_branch,
            model=session.model,
        )
        return [session]


def _extract_session_id(filename: str) -> str:
    """Extract UUID from rollout-YYYY-MM-DDThh-mm-ss-<uuid>.jsonl."""
    # Remove 'rollout-' prefix and '.jsonl' suffix
    stem = filename.replace("rollout-", "").replace(".jsonl", "")
    # The datetime part is YYYY-MM-DDThh-mm-ss (19 chars + T = 20), then '-' + UUID
    # UUID is 36 chars (8-4-4-4-12), so grab the last 36 chars
    if len(stem) >= 36:
        potential_uuid = stem[-36:]
        if potential_uuid.count("-") == 4:
            return potential_uuid
    # Fallback: use the full stem as ID
    return stem


def _parse_codex_session(file_path: Path) -> ParsedSession | None:
    """Parse a Codex CLI rollout JSONL file."""
    session_id = _extract_session_id(file_path.name)
    messages = []
    cwd = None
    model = None
    cli_version = None
    project = file_path.parent.name  # DD directory, but we'll try to get better context

    try:
        with open(file_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                rec_type = record.get("type", "")
                timestamp = _parse_ts(record.get("timestamp"))
                payload = record.get("payload", {})

                if rec_type == "session_meta":
                    session_id = payload.get("id", session_id)
                    cwd = payload.get("cwd")
                    cli_version = payload.get("cli_version")
                    # Derive project from cwd
                    if cwd:
                        project = Path(cwd).name

                elif rec_type == "response_item":
                    # ResponseItem contains message content
                    content, role, tools = _extract_response_item(payload)
                    if content.strip():
                        msg_id = payload.get("id", "")
                        est_tokens = max(1, len(content) // CHARS_PER_TOKEN)
                        messages.append(ParsedMessage(
                            uuid=msg_id,
                            session_id=session_id,
                            role=role,
                            content=content,
                            timestamp=timestamp,
                            cwd=cwd,
                            tools_used=tools,
                            estimated_tokens=est_tokens,
                        ))

                elif rec_type == "event_msg":
                    content, role, tools = _extract_event_msg(payload)
                    if content.strip():
                        msg_id = payload.get("id", f"evt-{len(messages)}")
                        est_tokens = max(1, len(content) // CHARS_PER_TOKEN)
                        messages.append(ParsedMessage(
                            uuid=msg_id,
                            session_id=session_id,
                            role=role,
                            content=content,
                            timestamp=timestamp,
                            cwd=cwd,
                            tools_used=tools,
                            estimated_tokens=est_tokens,
                        ))

    except Exception:
        return None

    if not messages:
        return None

    first_user = next((m for m in messages if m.role == "user"), None)
    title = None
    if first_user:
        title = first_user.content[:80].replace("\n", " ").strip()
        if len(first_user.content) > 80:
            title += "..."

    timestamps = [m.timestamp for m in messages if m.timestamp]

    return ParsedSession(
        session_id=session_id,
        project=project,
        messages=messages,
        started_at=min(timestamps) if timestamps else None,
        ended_at=max(timestamps) if timestamps else None,
        cwd=cwd,
        agent_version=cli_version,
        model=model,
        title=title,
        provider_id="codex",
    )


def _extract_response_item(payload: dict) -> tuple[str, str, list[str]]:
    """Extract content from a response_item payload. Returns (content, role, tools)."""
    role = payload.get("role", "assistant")
    tools = []
    parts = []

    content = payload.get("content", "")
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                block_type = block.get("type", "")
                if block_type in ("text", "output_text"):
                    parts.append(block.get("text", ""))
                elif block_type == "input_text":
                    parts.append(block.get("text", ""))
                    role = "user"
                elif block_type in ("function_call", "tool_use"):
                    name = block.get("name", block.get("function", "unknown"))
                    tools.append(name)
                    parts.append(f"[tool: {name}]")

    # Also check for top-level text field
    if not parts and payload.get("text"):
        parts.append(payload["text"])

    return "\n".join(parts), role, tools


def _extract_event_msg(payload: dict) -> tuple[str, str, list[str]]:
    """Extract content from an event_msg payload. Returns (content, role, tools)."""
    event_type = payload.get("event_type", payload.get("type", ""))
    tools = []

    if event_type == "user_message":
        text = payload.get("text", payload.get("message", ""))
        return text, "user", []

    if event_type in ("agent_message", "agent_message_delta"):
        text = payload.get("text", payload.get("delta", ""))
        return text, "assistant", []

    if event_type in ("exec_command_begin", "exec_command"):
        cmd = payload.get("command", payload.get("cmd", ""))
        tools.append("exec")
        return f"[exec: {cmd}]", "assistant", tools

    if event_type in ("task_completed", "task_started"):
        text = payload.get("message", payload.get("text", ""))
        return text, "assistant", []

    # For other event types, try to extract any text
    text = payload.get("message", payload.get("text", payload.get("content", "")))
    if isinstance(text, dict):
        text = text.get("text", "")
    return str(text) if text else "", "assistant", tools


def _parse_ts(raw) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(raw / 1000, tz=timezone.utc)
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None
