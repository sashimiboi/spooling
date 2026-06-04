"""Google Antigravity session parser.

Antigravity is a VS Code fork from Google (the "agent-first IDE"), so it
uses the same `state.vscdb` SQLite layout Cursor/Windsurf/Kiro use. We
probe a few candidate base dirs: the top-level Application Support entry,
the Google/ namespace, and a dotfile fallback.
"""

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from spooling.config import CHARS_PER_TOKEN
from spooling.parser import ParsedSession, ParsedMessage
from spooling.providers.base import Provider
from spooling.tracing import build_flat_trace_from_messages

_CANDIDATE_BASES = [
    Path.home() / "Library" / "Application Support" / "Antigravity" / "User",
    Path.home() / "Library" / "Application Support" / "Google" / "Antigravity" / "User",
    Path.home() / ".config" / "Antigravity" / "User",
    Path.home() / ".antigravity",
]

_CHAT_KEYS = [
    "workbench.panel.aichat.view.aichat.chatdata",
    "aiChat.chatdata",
    "antigravity.chat.data",
    "chat.data",
]

_AGENT_KEY_PREFIXES = ("composerData:", "agentData:", "antigravityAgent:", "flowData:")


class AntigravityProvider(Provider):
    type_id = "antigravity"
    name = "Google Antigravity"

    def default_data_path(self) -> Path:
        for base in _CANDIDATE_BASES:
            if base.exists():
                ws = base / "workspaceStorage"
                return ws if ws.exists() else base
        return _CANDIDATE_BASES[0] / "workspaceStorage"

    def is_available(self) -> bool:
        return any(base.exists() for base in _CANDIDATE_BASES)

    def discover_session_files(self, data_path: Path | None = None) -> list[Path]:
        files: list[Path] = []
        if data_path is not None and data_path.exists():
            bases = [data_path]
        else:
            bases = [b for b in _CANDIDATE_BASES if b.exists()]
        for base in bases:
            ws = base / "workspaceStorage" if (base / "workspaceStorage").exists() else base
            for vscdb in ws.rglob("state.vscdb"):
                if _has_ag_data(vscdb):
                    files.append(vscdb)
            gdb = base / "globalStorage" / "state.vscdb"
            if gdb.exists() and _has_ag_data(gdb):
                files.append(gdb)
        return sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)

    def parse_session_file(self, file_path: Path) -> list[ParsedSession]:
        sessions: list[ParsedSession] = []
        sessions.extend(_parse_chat_data(file_path))
        sessions.extend(_parse_agent_data(file_path))
        for s in sessions:
            s.trace = build_flat_trace_from_messages(
                provider_id="antigravity",
                session_id=s.session_id,
                project=s.project,
                title=s.title,
                messages=s.messages,
                cwd=s.cwd,
                git_branch=s.git_branch,
                model=s.model,
            )
        return sessions


def _has_ag_data(db_path: Path) -> bool:
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cur = conn.cursor()
        for pattern in ("%aichat%", "%aiChat%", "%antigravity%", "%chat.data%"):
            cur.execute("SELECT 1 FROM ItemTable WHERE key LIKE ? LIMIT 1", (pattern,))
            if cur.fetchone():
                conn.close()
                return True
        cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cursorDiskKV'")
        if cur.fetchone():
            for prefix in _AGENT_KEY_PREFIXES:
                cur.execute("SELECT 1 FROM cursorDiskKV WHERE key LIKE ? LIMIT 1", (f"{prefix}%",))
                if cur.fetchone():
                    conn.close()
                    return True
        conn.close()
    except Exception:
        pass
    return False


def _get_workspace_project(db_path: Path) -> str:
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
    sessions: list[ParsedSession] = []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cur = conn.cursor()
        for key in _CHAT_KEYS:
            cur.execute("SELECT value FROM ItemTable WHERE key = ?", (key,))
            row = cur.fetchone()
            if not row:
                continue
            try:
                data = json.loads(row[0])
            except (json.JSONDecodeError, TypeError):
                continue
            project = _get_workspace_project(db_path)
            if isinstance(data, dict):
                if "messages" in data:
                    s = _parse_message_list(data, project)
                    if s:
                        sessions.append(s)
                elif "tabs" in data:
                    for tab in data["tabs"]:
                        s = _parse_tab(tab, project)
                        if s:
                            sessions.append(s)
        conn.close()
    except Exception:
        pass
    return sessions


def _parse_message_list(data: dict, project: str) -> ParsedSession | None:
    raw = data.get("messages", [])
    if not raw:
        return None
    tab_id = data.get("tab_id", data.get("workspace_id", str(uuid.uuid4())))
    title = data.get("chat_title", data.get("chatTitle", ""))
    messages: list[ParsedMessage] = []
    for i, msg in enumerate(raw):
        role = msg.get("role", "assistant")
        if role not in ("user", "assistant"):
            role = "assistant"
        content = msg.get("content", msg.get("text", ""))
        if not content or not (isinstance(content, str) and content.strip()):
            continue
        messages.append(ParsedMessage(
            uuid=f"{tab_id}-{i}",
            session_id=tab_id,
            role=role,
            content=content,
            timestamp=None,
            estimated_tokens=max(1, len(content) // CHARS_PER_TOKEN),
        ))
    if not messages:
        return None
    if not title:
        first = next((m for m in messages if m.role == "user"), None)
        if first:
            title = first.content[:80].replace("\n", " ").strip()
            if len(first.content) > 80:
                title += "..."
    return ParsedSession(
        session_id=f"antigravity-chat-{tab_id}",
        project=project,
        messages=messages,
        title=title,
        provider_id="antigravity",
    )


def _parse_tab(tab: dict, project: str) -> ParsedSession | None:
    tab_id = tab.get("tabId", tab.get("tab_id", str(uuid.uuid4())))
    title = tab.get("chatTitle", tab.get("chat_title", ""))
    bubbles = tab.get("bubbles", tab.get("messages", []))
    if not bubbles:
        return None
    messages: list[ParsedMessage] = []
    for i, b in enumerate(bubbles):
        role_raw = b.get("type", b.get("role", "assistant"))
        role = "user" if (isinstance(role_raw, int) and role_raw == 1) or role_raw == "user" else "assistant"
        content = b.get("rawText", b.get("text", b.get("content", "")))
        if not content or not (isinstance(content, str) and content.strip()):
            continue
        messages.append(ParsedMessage(
            uuid=f"{tab_id}-{i}",
            session_id=tab_id,
            role=role,
            content=content,
            timestamp=None,
            estimated_tokens=max(1, len(content) // CHARS_PER_TOKEN),
        ))
    if not messages:
        return None
    if not title:
        first = next((m for m in messages if m.role == "user"), None)
        if first:
            title = first.content[:80].replace("\n", " ").strip()
            if len(first.content) > 80:
                title += "..."
    return ParsedSession(
        session_id=f"antigravity-chat-{tab_id}",
        project=project,
        messages=messages,
        title=title,
        provider_id="antigravity",
    )


def _parse_agent_data(db_path: Path) -> list[ParsedSession]:
    sessions: list[ParsedSession] = []
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cursorDiskKV'")
        if not cur.fetchone():
            conn.close()
            return []
        like_clause = " OR ".join("key LIKE ?" for _ in _AGENT_KEY_PREFIXES)
        params = tuple(f"{p}%" for p in _AGENT_KEY_PREFIXES)
        cur.execute(f"SELECT key, value FROM cursorDiskKV WHERE {like_clause}", params)
        rows = cur.fetchall()
        conn.close()
        project = _get_workspace_project(db_path)
        for key, value in rows:
            try:
                data = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                continue
            s = _parse_agent_session(data, key, project)
            if s:
                sessions.append(s)
    except Exception:
        pass
    return sessions


def _parse_agent_session(data: dict, key: str, project: str) -> ParsedSession | None:
    agent_id = key.split(":", 1)[-1] if ":" in key else str(uuid.uuid4())
    name = data.get("name", "")
    raw = data.get("messages", data.get("conversation", []))
    if not raw:
        return None
    created_at = _parse_ts(data.get("created_at", data.get("createdAt")))
    updated_at = _parse_ts(data.get("updated_at", data.get("lastUpdatedAt")))
    messages: list[ParsedMessage] = []
    for i, msg in enumerate(raw):
        role_raw = msg.get("role", msg.get("type", "assistant"))
        role = "user" if (isinstance(role_raw, int) and role_raw == 1) or role_raw == "user" else "assistant"
        content = msg.get("content", msg.get("text", ""))
        if not content or not (isinstance(content, str) and content.strip()):
            continue
        messages.append(ParsedMessage(
            uuid=f"{agent_id}-{i}",
            session_id=agent_id,
            role=role,
            content=content,
            timestamp=created_at,
            estimated_tokens=max(1, len(content) // CHARS_PER_TOKEN),
        ))
    if not messages:
        return None
    title = name
    if not title:
        first = next((m for m in messages if m.role == "user"), None)
        if first:
            title = first.content[:80].replace("\n", " ").strip()
            if len(first.content) > 80:
                title += "..."
    return ParsedSession(
        session_id=f"antigravity-agent-{agent_id}",
        project=project,
        messages=messages,
        started_at=created_at,
        ended_at=updated_at,
        title=title,
        provider_id="antigravity",
    )


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
