"""Cursor AI editor session parser - reads SQLite vscdb files."""

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from spooling.config import CHARS_PER_TOKEN
from spooling.parser import ParsedSession, ParsedMessage
from spooling.providers.base import Provider
from spooling.tracing import build_flat_trace_from_messages

CURSOR_BASE = Path.home() / "Library" / "Application Support" / "Cursor" / "User"
CURSOR_WORKSPACE_STORAGE = CURSOR_BASE / "workspaceStorage"
CURSOR_GLOBAL_STORAGE = CURSOR_BASE / "globalStorage"


class CursorProvider(Provider):
    type_id = "cursor"
    name = "Cursor"

    def default_data_path(self) -> Path:
        return CURSOR_WORKSPACE_STORAGE

    def is_available(self) -> bool:
        return CURSOR_WORKSPACE_STORAGE.exists() or CURSOR_GLOBAL_STORAGE.exists()

    def discover_session_files(self, data_path: Path | None = None) -> list[Path]:
        files = []
        # Workspace-level state.vscdb files (chat data)
        ws_dir = data_path or self.resolved_data_path()
        if ws_dir.exists():
            for vscdb in ws_dir.rglob("state.vscdb"):
                if _has_cursor_data(vscdb):
                    files.append(vscdb)
        # Global state.vscdb (composer/agent data)
        global_db = CURSOR_GLOBAL_STORAGE / "state.vscdb"
        if global_db.exists() and _has_cursor_data(global_db):
            files.append(global_db)
        return sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)

    def parse_session_file(self, file_path: Path) -> list[ParsedSession]:
        sessions = []
        # Try chat data from ItemTable
        sessions.extend(_parse_chat_data(file_path))
        # Try composer data from cursorDiskKV
        sessions.extend(_parse_composer_data(file_path))
        for s in sessions:
            s.trace = build_flat_trace_from_messages(
                provider_id="cursor",
                session_id=s.session_id,
                project=s.project,
                title=s.title,
                messages=s.messages,
                cwd=s.cwd,
                git_branch=s.git_branch,
                model=s.model,
            )
        return sessions


def _has_cursor_data(db_path: Path) -> bool:
    """Check if a vscdb file contains Cursor chat or composer data."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cursor = conn.cursor()
        # Check ItemTable for chat data
        cursor.execute(
            "SELECT 1 FROM ItemTable WHERE key LIKE '%aichat%' OR key LIKE '%aiService%' LIMIT 1"
        )
        if cursor.fetchone():
            conn.close()
            return True
        # Check for cursorDiskKV table (composer data)
        cursor.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cursorDiskKV'"
        )
        if cursor.fetchone():
            cursor.execute("SELECT 1 FROM cursorDiskKV WHERE key LIKE 'composerData:%' LIMIT 1")
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
            # folder is usually a file:// URI
            if folder.startswith("file://"):
                return Path(folder[7:]).name
            return Path(folder).name if folder else db_path.parent.name
        except Exception:
            pass
    return db_path.parent.name


def _parse_chat_data(db_path: Path) -> list[ParsedSession]:
    """Parse Cursor chat tabs from ItemTable."""
    sessions = []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cursor = conn.cursor()

        # Try known keys for chat data
        chat_keys = [
            "workbench.panel.aichat.view.aichat.chatdata",
            "aiService.prompts",
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

            tabs = data.get("tabs", [])
            if not tabs and isinstance(data, list):
                tabs = data

            project = _get_workspace_project(db_path)

            for tab in tabs:
                session = _parse_chat_tab(tab, project)
                if session:
                    sessions.append(session)

        conn.close()
    except Exception:
        pass
    return sessions


def _parse_chat_tab(tab: dict, project: str) -> ParsedSession | None:
    """Parse a single chat tab into a ParsedSession."""
    tab_id = tab.get("tabId", str(uuid.uuid4()))
    title = tab.get("chatTitle", "")
    bubbles = tab.get("bubbles", [])
    if not bubbles:
        return None

    messages = []
    for i, bubble in enumerate(bubbles):
        # Role can be string or int (1=user, 2=assistant)
        role_raw = bubble.get("type", bubble.get("role", ""))
        if isinstance(role_raw, int):
            role = "user" if role_raw == 1 else "assistant"
        else:
            role = "user" if role_raw == "user" else "assistant"

        content = bubble.get("rawText", bubble.get("text", bubble.get("content", "")))
        if not content or not content.strip():
            continue

        model = bubble.get("modelId", bubble.get("model", ""))
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
        session_id=f"cursor-chat-{tab_id}",
        project=project,
        messages=messages,
        title=title,
        model=messages[0].uuid if messages else None,  # Cursor doesn't always expose model
        provider_id="cursor",
    )


def _parse_composer_data(db_path: Path) -> list[ParsedSession]:
    """Parse Cursor composer/agent sessions from cursorDiskKV table."""
    sessions = []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cursor = conn.cursor()

        # Check if table exists
        cursor.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cursorDiskKV'"
        )
        if not cursor.fetchone():
            conn.close()
            return []

        cursor.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'"
        )
        rows = cursor.fetchall()
        conn.close()

        project = _get_workspace_project(db_path)

        for key, value in rows:
            try:
                data = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                continue
            session = _parse_composer_session(data, project)
            if session:
                sessions.append(session)

    except Exception:
        pass
    return sessions


def _parse_composer_session(data: dict, project: str) -> ParsedSession | None:
    """Parse a single composer session."""
    composer_id = data.get("composerId", str(uuid.uuid4()))
    name = data.get("name", "")
    conversation = data.get("conversation", [])
    if not conversation:
        return None

    model_config = data.get("modelConfig", {})
    model = model_config.get("modelName", "")
    created_at = _parse_cursor_ts(data.get("createdAt"))
    updated_at = _parse_cursor_ts(data.get("lastUpdatedAt"))

    messages = []
    for i, msg in enumerate(conversation):
        role_raw = msg.get("type", msg.get("role", 2))
        if isinstance(role_raw, int):
            role = "user" if role_raw == 1 else "assistant"
        else:
            role = "user" if role_raw == "user" else "assistant"

        content = msg.get("text", msg.get("content", ""))
        if not content or not content.strip():
            continue

        est_tokens = max(1, len(content) // CHARS_PER_TOKEN)
        messages.append(ParsedMessage(
            uuid=f"{composer_id}-{i}",
            session_id=composer_id,
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
        session_id=f"cursor-composer-{composer_id}",
        project=project,
        messages=messages,
        started_at=created_at,
        ended_at=updated_at,
        model=model,
        title=title,
        provider_id="cursor",
    )


def _parse_cursor_ts(raw) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        # Could be ms or seconds
        if raw > 1e12:
            return datetime.fromtimestamp(raw / 1000, tz=timezone.utc)
        return datetime.fromtimestamp(raw, tz=timezone.utc)
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None
