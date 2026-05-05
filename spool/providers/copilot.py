"""GitHub Copilot Chat session parser - reads JSON/JSONL from VS Code workspaceStorage."""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from spool.config import CHARS_PER_TOKEN
from spool.parser import ParsedSession, ParsedMessage
from spool.providers.base import Provider
from spool.tracing import build_flat_trace_from_messages

VSCODE_BASE = Path.home() / "Library" / "Application Support" / "Code" / "User"
VSCODE_WORKSPACE_STORAGE = VSCODE_BASE / "workspaceStorage"


class CopilotProvider(Provider):
    type_id = "copilot"
    name = "GitHub Copilot"

    def default_data_path(self) -> Path:
        return VSCODE_WORKSPACE_STORAGE

    def discover_session_files(self, data_path: Path | None = None) -> list[Path]:
        base = data_path or self.resolved_data_path()
        if not base.exists():
            return []

        files = []
        for ws_dir in base.iterdir():
            if not ws_dir.is_dir():
                continue
            chat_dir = ws_dir / "chatSessions"
            if chat_dir.exists():
                for f in chat_dir.iterdir():
                    if f.suffix in (".json", ".jsonl") and f.stat().st_size > 200:
                        files.append(f)

        return sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)

    def parse_session_file(self, file_path: Path) -> list[ParsedSession]:
        if file_path.suffix == ".jsonl":
            session = _parse_copilot_jsonl(file_path)
        else:
            session = _parse_copilot_json(file_path)
        if not session:
            return []
        session.trace = build_flat_trace_from_messages(
            provider_id="copilot",
            session_id=session.session_id,
            project=session.project,
            title=session.title,
            messages=session.messages,
            cwd=session.cwd,
            git_branch=session.git_branch,
            model=session.model,
        )
        return [session]


def _get_workspace_project(file_path: Path) -> str:
    """Try to get project name from workspace.json."""
    ws_dir = file_path.parent.parent
    ws_json = ws_dir / "workspace.json"
    if ws_json.exists():
        try:
            data = json.loads(ws_json.read_text())
            folder = data.get("folder", "")
            if folder.startswith("file://"):
                return Path(folder[7:]).name
            return Path(folder).name if folder else ws_dir.name
        except Exception:
            pass
    return ws_dir.name


def _parse_copilot_json(file_path: Path) -> ParsedSession | None:
    """Parse a legacy Copilot Chat JSON session file."""
    try:
        data = json.loads(file_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None

    return _build_session_from_requests(
        data.get("requests", []),
        data.get("sessionId", file_path.stem),
        _get_workspace_project(file_path),
        data.get("customTitle"),
    )


def _parse_copilot_jsonl(file_path: Path) -> ParsedSession | None:
    """Parse a modern Copilot Chat JSONL mutation log.

    Format:
    - kind 0: initial state (usually empty requests), key is "v"
    - kind 1: SET mutation — k=[path...], v=value
    - kind 2: SPLICE mutation — k=[path...], v=[values to insert], i=splice index
    """
    project = _get_workspace_project(file_path)
    state = {}

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

                kind = record.get("kind")
                k = record.get("k", [])
                v = record.get("v")

                if kind == 0:
                    # Initial state
                    state = v if isinstance(v, dict) else {}

                elif kind == 1 and isinstance(k, list) and k:
                    # SET mutation: navigate path and set value
                    _set_at_path(state, k, v)

                elif kind == 2 and isinstance(k, list) and k:
                    # SPLICE mutation: insert values into array at path
                    values = v if isinstance(v, list) else []
                    idx = record.get("i")
                    _splice_at_path(state, k, values, idx)

    except Exception:
        return None

    requests = state.get("requests", [])
    if not requests:
        return None

    session_id = state.get("sessionId", file_path.stem)
    title = state.get("customTitle")

    return _build_session_from_requests(requests, session_id, project, title)


def _set_at_path(obj, path: list, value):
    """Navigate a nested dict/list by path and set the value at the leaf."""
    for key in path[:-1]:
        if isinstance(obj, dict):
            obj = obj.get(key, {})
        elif isinstance(obj, list) and isinstance(key, int) and key < len(obj):
            obj = obj[key]
        else:
            return
    final_key = path[-1]
    if isinstance(obj, dict):
        obj[final_key] = value
    elif isinstance(obj, list) and isinstance(final_key, int) and final_key < len(obj):
        obj[final_key] = value


def _splice_at_path(obj, path: list, values: list, idx: int | None):
    """Navigate to an array at path and splice values into it."""
    target = obj
    for key in path:
        if isinstance(target, dict):
            if key not in target:
                target[key] = []
            target = target[key]
        elif isinstance(target, list) and isinstance(key, int) and key < len(target):
            target = target[key]
        else:
            return

    if isinstance(target, list):
        if idx is not None and isinstance(idx, int):
            for i, v in enumerate(values):
                target.insert(idx + i, v)
        else:
            target.extend(values)


def _build_session_from_requests(
    requests: list, session_id: str, project: str, title: str | None
) -> ParsedSession | None:
    """Build a ParsedSession from Copilot's requests array."""
    if not requests:
        return None

    messages = []
    for i, req in enumerate(requests):
        # User message
        user_msg = req.get("message", {})
        user_text = user_msg.get("text", "") if isinstance(user_msg, dict) else str(user_msg)
        timestamp = _parse_ts(req.get("timestamp"))

        if user_text.strip():
            est_tokens = max(1, len(user_text) // CHARS_PER_TOKEN)
            messages.append(ParsedMessage(
                uuid=f"{session_id}-user-{i}",
                session_id=session_id,
                role="user",
                content=user_text,
                timestamp=timestamp,
                estimated_tokens=est_tokens,
            ))

        # Assistant response — reconstruct from response parts
        response_parts = req.get("response", [])
        resp_text = _extract_response_text(response_parts)

        if resp_text.strip():
            est_tokens = max(1, len(resp_text) // CHARS_PER_TOKEN)
            messages.append(ParsedMessage(
                uuid=f"{session_id}-asst-{i}",
                session_id=session_id,
                role="assistant",
                content=resp_text,
                timestamp=timestamp,
                estimated_tokens=est_tokens,
            ))

    if not messages:
        return None

    if not title:
        first_user = next((m for m in messages if m.role == "user"), None)
        if first_user:
            title = first_user.content[:80].replace("\n", " ").strip()
            if len(first_user.content) > 80:
                title += "..."

    timestamps = [m.timestamp for m in messages if m.timestamp]

    return ParsedSession(
        session_id=f"copilot-{session_id}",
        project=project,
        messages=messages,
        started_at=min(timestamps) if timestamps else None,
        ended_at=max(timestamps) if timestamps else None,
        title=title,
        provider_id="copilot",
    )


def _extract_response_text(response_parts: list) -> str:
    """Extract readable text from Copilot's response array.

    Response parts can be:
    - {"value": "markdown text", ...} — actual response content
    - {"kind": "progressMessage", "content": {"value": "..."}} — status updates (skip)
    - {"invocationMessage": "..."} — tool invocations
    """
    text_parts = []
    for part in response_parts:
        if not isinstance(part, dict):
            continue

        kind = part.get("kind")

        # Skip progress messages and confirmations
        if kind in ("progressMessage", "progressTask"):
            continue

        # Direct value field — this is the actual response text
        value = part.get("value", "")
        if isinstance(value, str) and value.strip():
            text_parts.append(value)
        elif isinstance(value, dict):
            # Nested content like {"value": {"value": "text"}}
            inner = value.get("value", "")
            if isinstance(inner, str) and inner.strip():
                text_parts.append(inner)

        # Content field (used in some formats)
        content = part.get("content", "")
        if isinstance(content, str) and content.strip() and kind != "progressMessage":
            text_parts.append(content)

    return "".join(text_parts)


def _parse_ts(raw) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        if raw > 1e12:
            return datetime.fromtimestamp(raw / 1000, tz=timezone.utc)
        return datetime.fromtimestamp(raw, tz=timezone.utc)
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None
