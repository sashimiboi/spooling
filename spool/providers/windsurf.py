"""Windsurf (Codeium) session parser - reads SQLite vscdb files."""

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from spool.config import CHARS_PER_TOKEN
from spool.parser import ParsedSession, ParsedMessage
from spool.providers.base import Provider
from spool.tracing import build_flat_trace_from_messages

WINDSURF_BASE = Path.home() / "Library" / "Application Support" / "Windsurf" / "User"
WINDSURF_WORKSPACE_STORAGE = WINDSURF_BASE / "workspaceStorage"
WINDSURF_GLOBAL_STORAGE = WINDSURF_BASE / "globalStorage"


class WindsurfProvider(Provider):
    type_id = "windsurf"
    name = "Windsurf"

    def default_data_path(self) -> Path:
        return WINDSURF_WORKSPACE_STORAGE

    def is_available(self) -> bool:
        return WINDSURF_WORKSPACE_STORAGE.exists() or WINDSURF_GLOBAL_STORAGE.exists()

    def discover_session_files(self, data_path: Path | None = None) -> list[Path]:
        files = []
        # Workspace-level state.vscdb files (chat data)
        ws_dir = data_path or self.resolved_data_path()
        if ws_dir.exists():
            for vscdb in ws_dir.rglob("state.vscdb"):
                if _has_windsurf_data(vscdb):
                    files.append(vscdb)
        # Global state.vscdb (cascade/agent data)
        global_db = WINDSURF_GLOBAL_STORAGE / "state.vscdb"
        if global_db.exists() and _has_windsurf_data(global_db):
            files.append(global_db)
        return sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)

    def parse_session_file(self, file_path: Path) -> list[ParsedSession]:
        sessions = []
        sessions.extend(_parse_chat_data(file_path))
        sessions.extend(_parse_agent_data(file_path))
        for s in sessions:
            s.trace = build_flat_trace_from_messages(
                provider_id="windsurf",
                session_id=s.session_id,
                project=s.project,
                title=s.title,
                messages=s.messages,
                cwd=s.cwd,
                git_branch=s.git_branch,
                model=s.model,
            )
        return sessions


def _has_windsurf_data(db_path: Path) -> bool:
    """Check if a vscdb file contains Windsurf chat or agent data."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cursor = conn.cursor()
        # Check ItemTable for chat data
        chat_keys = [
            "%aichat%", "%aiChat%", "%chat.data%", "%cascade%",
        ]
        for pattern in chat_keys:
            cursor.execute("SELECT 1 FROM ItemTable WHERE key LIKE ? LIMIT 1", (pattern,))
            if cursor.fetchone():
                conn.close()
                return True
        # Check for cursorDiskKV table (agent/cascade data)
        cursor.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cursorDiskKV'"
        )
        if cursor.fetchone():
            cursor.execute(
                "SELECT 1 FROM cursorDiskKV WHERE key LIKE 'composerData:%' "
                "OR key LIKE 'agentData:%' OR key LIKE 'flowData:%' LIMIT 1"
            )
            if cursor.fetchone():
                conn.close()
                return True
        conn.close()
    except Exception:
        pass
    return False


def _get_workspace_project(db_path: Path) -> str:
    """Try to get the project name from workspace.json alongside the vscdb."""
    ws_json = db_path.parent / "workspace.json"
    if ws_json.exists():
        try:
            data = json.loads(ws_json.read_text())
            folder = data.get("folder", "")
            if folder.startswith("file://"):
                return Path(folder[7:]).name
            return Path(folder).name if folder else db_path.parent.name
        except Exception:
            pass
    return db_path.parent.name


def _parse_chat_data(db_path: Path) -> list[ParsedSession]:
    """Parse Windsurf chat sessions from ItemTable."""
    sessions = []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cursor = conn.cursor()

        chat_keys = [
            "workbench.panel.aichat.view.aichat.chatdata",
            "aiChat.chatdata",
            "chat.data",
            "cascade.chatdata",
        ]

        for key in chat_keys:
            cursor.execute("SELECT value FROM ItemTable WHERE key = ?", (key,))
            row = cursor.fetchone()
            if not row:
                continue

            try:
                data = json.loads(row[0])
            except (json.JSONDecodeError, TypeError):
                continue

            project = _get_workspace_project(db_path)

            # Windsurf can store data as a single chat or as tabs
            if isinstance(data, dict):
                if "messages" in data:
                    session = _parse_message_list(data, project)
                    if session:
                        sessions.append(session)
                elif "tabs" in data:
                    for tab in data["tabs"]:
                        session = _parse_tab(tab, project)
                        if session:
                            sessions.append(session)

        conn.close()
    except Exception:
        pass
    return sessions


def _parse_message_list(data: dict, project: str) -> ParsedSession | None:
    """Parse a Windsurf chat with a messages array."""
    raw_messages = data.get("messages", [])
    if not raw_messages:
        return None

    tab_id = data.get("tab_id", data.get("workspace_id", str(uuid.uuid4())))
    title = data.get("chat_title", "")
    messages = []

    for i, msg in enumerate(raw_messages):
        role = msg.get("role", "assistant")
        if role not in ("user", "assistant"):
            role = "assistant"
        content = msg.get("content", "")
        if not content or not content.strip():
            continue

        est_tokens = max(1, len(content) // CHARS_PER_TOKEN)
        messages.append(ParsedMessage(
            uuid=f"{tab_id}-{i}",
            session_id=tab_id,
            role=role,
            content=content,
            timestamp=None,
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

    return ParsedSession(
        session_id=f"windsurf-chat-{tab_id}",
        project=project,
        messages=messages,
        title=title,
        provider_id="windsurf",
    )


def _parse_tab(tab: dict, project: str) -> ParsedSession | None:
    """Parse a Windsurf chat tab (similar to Cursor tab format)."""
    tab_id = tab.get("tabId", tab.get("tab_id", str(uuid.uuid4())))
    title = tab.get("chatTitle", tab.get("chat_title", ""))
    bubbles = tab.get("bubbles", tab.get("messages", []))
    if not bubbles:
        return None

    messages = []
    for i, bubble in enumerate(bubbles):
        role_raw = bubble.get("type", bubble.get("role", "assistant"))
        if isinstance(role_raw, int):
            role = "user" if role_raw == 1 else "assistant"
        else:
            role = "user" if role_raw == "user" else "assistant"

        content = bubble.get("rawText", bubble.get("text", bubble.get("content", "")))
        if not content or not content.strip():
            continue

        est_tokens = max(1, len(content) // CHARS_PER_TOKEN)
        messages.append(ParsedMessage(
            uuid=f"{tab_id}-{i}",
            session_id=tab_id,
            role=role,
            content=content,
            timestamp=None,
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

    return ParsedSession(
        session_id=f"windsurf-chat-{tab_id}",
        project=project,
        messages=messages,
        title=title,
        provider_id="windsurf",
    )


def _parse_agent_data(db_path: Path) -> list[ParsedSession]:
    """Parse Windsurf Cascade/agent sessions from cursorDiskKV table."""
    sessions = []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cursor = conn.cursor()

        cursor.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cursorDiskKV'"
        )
        if not cursor.fetchone():
            conn.close()
            return []

        # Look for agent/composer/flow data
        cursor.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%' "
            "OR key LIKE 'agentData:%' OR key LIKE 'flowData:%'"
        )
        rows = cursor.fetchall()
        conn.close()

        project = _get_workspace_project(db_path)

        for key, value in rows:
            try:
                data = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                continue
            session = _parse_agent_session(data, key, project)
            if session:
                sessions.append(session)

    except Exception:
        pass
    return sessions


def _parse_agent_session(data: dict, key: str, project: str) -> ParsedSession | None:
    """Parse a single Windsurf agent/cascade session."""
    # Extract ID from key (e.g., "composerData:uuid" -> "uuid")
    agent_id = key.split(":", 1)[-1] if ":" in key else str(uuid.uuid4())
    name = data.get("name", "")
    raw_messages = data.get("messages", data.get("conversation", []))
    if not raw_messages:
        return None

    created_at = _parse_ws_ts(data.get("created_at", data.get("createdAt")))
    updated_at = _parse_ws_ts(data.get("updated_at", data.get("lastUpdatedAt")))

    messages = []
    for i, msg in enumerate(raw_messages):
        role_raw = msg.get("role", msg.get("type", "assistant"))
        if isinstance(role_raw, int):
            role = "user" if role_raw == 1 else "assistant"
        else:
            role = "user" if role_raw == "user" else "assistant"

        content = msg.get("content", msg.get("text", ""))
        if not content or not content.strip():
            continue

        est_tokens = max(1, len(content) // CHARS_PER_TOKEN)
        messages.append(ParsedMessage(
            uuid=f"{agent_id}-{i}",
            session_id=agent_id,
            role=role,
            content=content,
            timestamp=created_at,
            estimated_tokens=est_tokens,
        ))

    if not messages:
        return None

    title = name
    if not title:
        first_user = next((m for m in messages if m.role == "user"), None)
        if first_user:
            title = first_user.content[:80].replace("\n", " ").strip()
            if len(first_user.content) > 80:
                title += "..."

    return ParsedSession(
        session_id=f"windsurf-agent-{agent_id}",
        project=project,
        messages=messages,
        started_at=created_at,
        ended_at=updated_at,
        title=title,
        provider_id="windsurf",
    )


def _parse_ws_ts(raw) -> datetime | None:
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
