"""Gemini Code Assist session parser.

Two surfaces are supported:

1. **Gemini Code Assist VS Code extension** (``google.geminicodeassist``).
   The extension stores chat threads in VS Code's global ``state.vscdb``
   under the ``google.geminicodeassist`` ItemTable key. The value is JSON
   shaped like::

       {
         "geminicodeassist.hasRunOnce": true,
         "geminiCodeAssist.chatThreads": {
           "<user-email>": {
             "<thread-uuid>": {
               "id": "...",
               "title": "...",
               "create_time": "ISO8601",
               "update_time": "ISO8601",
               "history": [
                 {"entity": "USER",   "markdownText": "...", ...},
                 {"entity": "SYSTEM", "markdownText": "...", "modelID": "..."},
                 ...
               ],
               "version": "2.0"
             }
           }
         }
       }

   This is what the IDE chat panel actually persists. Confirmed live on
   extension version 2.79.0 (2026-04-30).

2. **Gemini CLI** (``@google/gemini-cli``). Stores saved-chat JSON under
   ``~/.gemini/tmp/<project-slug>/chats/`` only when the user runs
   ``/chat save <name>``. The schema is conservative-best-effort because
   it shifts between CLI versions.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from spool.config import CHARS_PER_TOKEN
from spool.parser import ParsedSession, ParsedMessage
from spool.providers.base import Provider
from spool.tracing import build_flat_trace_from_messages

_VSCODE_USER_DIRS = [
    Path.home() / "Library" / "Application Support" / "Code" / "User",
    Path.home() / "Library" / "Application Support" / "Code - Insiders" / "User",
    Path.home() / ".config" / "Code" / "User",
    Path.home() / ".config" / "Code - Insiders" / "User",
]

_GEMINI_CLI_HOME = Path.home() / ".gemini"

# The ItemTable key that holds all of Gemini Code Assist's persisted state,
# including chat threads keyed by user email.
_VSCDB_STATE_KEY = "google.geminicodeassist"


class GeminiProvider(Provider):
    type_id = "gemini"
    name = "Gemini Code Assist"

    def default_data_path(self) -> Path:
        for base in _VSCODE_USER_DIRS:
            db = base / "globalStorage" / "state.vscdb"
            if db.exists():
                return db
        return _GEMINI_CLI_HOME

    def is_available(self) -> bool:
        for base in _VSCODE_USER_DIRS:
            db = base / "globalStorage" / "state.vscdb"
            if db.exists() and _vscdb_has_chat_threads(db):
                return True
        if _GEMINI_CLI_HOME.exists():
            return True
        return False

    def discover_session_files(self, data_path: Path | None = None) -> list[Path]:
        files: list[Path] = []
        seen: set[Path] = set()

        def add(p: Path) -> None:
            rp = p.resolve()
            if rp not in seen and p.exists():
                seen.add(rp)
                files.append(p)

        if data_path is not None and data_path.exists():
            if data_path.is_file():
                add(data_path)
            else:
                for f in data_path.rglob("*.json*"):
                    if f.is_file() and f.stat().st_size > 64:
                        add(f)
                db = data_path / "globalStorage" / "state.vscdb"
                if db.exists() and _vscdb_has_chat_threads(db):
                    add(db)
        else:
            for base in _VSCODE_USER_DIRS:
                db = base / "globalStorage" / "state.vscdb"
                if db.exists() and _vscdb_has_chat_threads(db):
                    add(db)
            if _GEMINI_CLI_HOME.exists():
                for sub in ("sessions", "history"):
                    sub_dir = _GEMINI_CLI_HOME / sub
                    if sub_dir.exists():
                        for f in sub_dir.rglob("*.json*"):
                            if f.is_file() and f.stat().st_size > 64:
                                add(f)
                tmp = _GEMINI_CLI_HOME / "tmp"
                if tmp.exists():
                    for f in tmp.rglob("chats/*.json"):
                        if f.is_file() and f.stat().st_size > 64:
                            add(f)
                    for f in tmp.rglob("checkpoints/*.json"):
                        if f.is_file() and f.stat().st_size > 64:
                            add(f)

        return sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)

    def parse_session_file(self, file_path: Path) -> list[ParsedSession]:
        if file_path.name == "state.vscdb":
            sessions = _parse_code_assist_threads(file_path)
        elif file_path.suffix == ".jsonl":
            s = _parse_cli_jsonl(file_path)
            sessions = [s] if s else []
        else:
            sessions = _parse_cli_json(file_path)

        for s in sessions:
            s.trace = build_flat_trace_from_messages(
                provider_id="gemini",
                session_id=s.session_id,
                project=s.project,
                title=s.title,
                messages=s.messages,
                cwd=s.cwd,
                git_branch=s.git_branch,
                model=s.model,
            )
        return sessions


def _vscdb_has_chat_threads(db_path: Path) -> bool:
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cur = conn.cursor()
        cur.execute("SELECT value FROM ItemTable WHERE key = ?", (_VSCDB_STATE_KEY,))
        row = cur.fetchone()
        conn.close()
        if not row:
            return False
        data = json.loads(row[0])
        threads = data.get("geminiCodeAssist.chatThreads") or {}
        for _email, by_id in threads.items():
            if isinstance(by_id, dict) and by_id:
                return True
    except Exception:
        pass
    return False


def _parse_code_assist_threads(db_path: Path) -> list[ParsedSession]:
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cur = conn.cursor()
        cur.execute("SELECT value FROM ItemTable WHERE key = ?", (_VSCDB_STATE_KEY,))
        row = cur.fetchone()
        conn.close()
        if not row:
            return []
        data = json.loads(row[0])
    except Exception:
        return []

    sessions: list[ParsedSession] = []
    threads_by_email = data.get("geminiCodeAssist.chatThreads") or {}
    for _email, by_id in threads_by_email.items():
        if not isinstance(by_id, dict):
            continue
        for thread_id, thread in by_id.items():
            if not isinstance(thread, dict):
                continue
            s = _build_session_from_thread(thread_id, thread)
            if s:
                sessions.append(s)
    return sessions


def _build_session_from_thread(thread_id: str, thread: dict) -> ParsedSession | None:
    history = thread.get("history") or []
    if not history:
        return None

    sid = thread.get("id") or thread_id
    title = thread.get("title")
    started = _parse_ts(thread.get("create_time"))
    ended = _parse_ts(thread.get("update_time"))

    cwd = _infer_workspace_root(history)

    messages: list[ParsedMessage] = []
    model: str | None = None

    for i, item in enumerate(history):
        if not isinstance(item, dict):
            continue
        entity = item.get("entity", "USER")
        role = "user" if entity == "USER" else "assistant"
        content = item.get("markdownText") or ""
        if not isinstance(content, str) or not content.strip():
            continue
        if model is None:
            model = item.get("modelDisplayName") or item.get("modelID")
        messages.append(ParsedMessage(
            uuid=item.get("chatSectionId") or f"{sid}-{i}",
            session_id=sid,
            role=role,
            content=content,
            timestamp=started if i == 0 else None,
            cwd=cwd,
            estimated_tokens=max(1, len(content) // CHARS_PER_TOKEN),
        ))

    if not messages:
        return None

    project = Path(cwd).name if cwd else "gemini"
    if not title:
        first_user = next((m for m in messages if m.role == "user"), None)
        if first_user:
            snippet = first_user.content[:80].replace("\n", " ").strip()
            title = snippet + ("..." if len(first_user.content) > 80 else "")

    return ParsedSession(
        session_id=f"gemini-{sid}",
        project=project,
        messages=messages,
        started_at=started,
        ended_at=ended,
        cwd=cwd,
        model=model,
        title=title,
        provider_id="gemini",
    )


def _infer_workspace_root(history: list) -> str | None:
    """Pick the workspace root from ideContext file paths in the history.

    The IDE's currentFile is often empty, and otherFiles can include
    node_modules / build artifacts that are misleading. Strategy:

    1. Collect every file path seen across all messages.
    2. Take the longest common parent directory.
    3. Walk up until we leave well-known noise dirs (node_modules, .git,
       dist, build, .next, .vercel) so we don't anchor inside them.
    """
    paths: list[str] = []
    for item in history:
        if not isinstance(item, dict):
            continue
        ctx = item.get("ideContext") or {}
        cur = (ctx.get("currentFile") or {}).get("path")
        if cur:
            paths.append(cur)
        for f in ctx.get("otherFiles") or []:
            if isinstance(f, dict) and f.get("path"):
                paths.append(f["path"])
    if not paths:
        return None

    parts_lists = [Path(p).parent.parts for p in paths if p]
    if not parts_lists:
        return None
    common: list[str] = []
    for tup in zip(*parts_lists):
        if len(set(tup)) == 1:
            common.append(tup[0])
        else:
            break
    if not common:
        return None
    root = Path(*common)
    noise = {"node_modules", ".git", "dist", "build", ".next", ".vercel", ".turbo"}
    while root.name in noise and root.parent != root:
        root = root.parent
    return str(root)


def _parse_cli_json(file_path: Path) -> list[ParsedSession]:
    try:
        data = json.loads(file_path.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    return _build_sessions_from_cli_json(data, file_path)


def _parse_cli_jsonl(file_path: Path) -> ParsedSession | None:
    messages: list[ParsedMessage] = []
    sid = file_path.stem
    title: str | None = None
    model: str | None = None
    try:
        with open(file_path) as f:
            for i, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                title = title or rec.get("title")
                model = model or rec.get("model")
                msg = _coerce_cli_message(rec, sid, i)
                if msg:
                    messages.append(msg)
    except OSError:
        return None
    if not messages:
        return None
    if not title:
        first_user = next((m for m in messages if m.role == "user"), None)
        if first_user:
            snippet = first_user.content[:80].replace("\n", " ").strip()
            title = snippet + ("..." if len(first_user.content) > 80 else "")
    timestamps = [m.timestamp for m in messages if m.timestamp]
    return ParsedSession(
        session_id=f"gemini-{sid}",
        project=file_path.parent.parent.name if file_path.parent.name == "chats" else file_path.parent.name,
        messages=messages,
        started_at=min(timestamps) if timestamps else None,
        ended_at=max(timestamps) if timestamps else None,
        model=model,
        title=title,
        provider_id="gemini",
    )


def _build_sessions_from_cli_json(data, file_path: Path) -> list[ParsedSession]:
    project = file_path.parent.parent.name if file_path.parent.name == "chats" else file_path.parent.name
    sid = file_path.stem
    messages: list[ParsedMessage] = []
    title: str | None = None
    model: str | None = None

    raw = []
    if isinstance(data, dict):
        title = data.get("title") or data.get("name")
        model = data.get("model")
        raw = (
            data.get("messages")
            or data.get("history")
            or data.get("turns")
            or data.get("conversation")
            or []
        )
    elif isinstance(data, list):
        raw = data

    for i, item in enumerate(raw):
        if isinstance(item, dict):
            msg = _coerce_cli_message(item, sid, i)
            if msg:
                messages.append(msg)

    if not messages:
        return []

    if not title:
        first_user = next((m for m in messages if m.role == "user"), None)
        if first_user:
            snippet = first_user.content[:80].replace("\n", " ").strip()
            title = snippet + ("..." if len(first_user.content) > 80 else "")

    timestamps = [m.timestamp for m in messages if m.timestamp]
    return [ParsedSession(
        session_id=f"gemini-{sid}",
        project=project,
        messages=messages,
        started_at=min(timestamps) if timestamps else None,
        ended_at=max(timestamps) if timestamps else None,
        model=model,
        title=title,
        provider_id="gemini",
    )]


def _coerce_cli_message(item: dict, session_id: str, index: int) -> ParsedMessage | None:
    nested = item.get("message") if isinstance(item.get("message"), dict) else None
    src = nested or item
    role_raw = src.get("role") or src.get("type") or src.get("author") or src.get("entity") or "assistant"
    if isinstance(role_raw, dict):
        role_raw = role_raw.get("role") or "assistant"
    role_raw = str(role_raw).lower()
    role = "user" if role_raw in ("user", "human", "1", "USER".lower()) else "assistant"

    content = _extract_text(
        src.get("content")
        or src.get("text")
        or src.get("markdownText")
        or src.get("parts")
        or item.get("content")
        or item.get("text")
    )
    if not content.strip():
        return None

    ts = _parse_ts(
        item.get("timestamp")
        or item.get("createdAt")
        or item.get("created_at")
        or src.get("timestamp")
    )

    return ParsedMessage(
        uuid=str(src.get("id") or item.get("id") or f"{session_id}-{index}"),
        session_id=session_id,
        role=role,
        content=content,
        timestamp=ts,
        estimated_tokens=max(1, len(content) // CHARS_PER_TOKEN),
    )


def _extract_text(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out: list[str] = []
        for part in content:
            if isinstance(part, str):
                out.append(part)
            elif isinstance(part, dict):
                t = part.get("text") or part.get("content") or part.get("value") or part.get("markdownText")
                if isinstance(t, str):
                    out.append(t)
        return "\n".join(out)
    if isinstance(content, dict):
        t = content.get("text") or content.get("value") or content.get("markdownText")
        if isinstance(t, str):
            return t
    return ""


def _parse_ts(raw) -> datetime | None:
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        if raw > 1e12:
            return datetime.fromtimestamp(raw / 1000, tz=timezone.utc)
        return datetime.fromtimestamp(raw, tz=timezone.utc)
    if isinstance(raw, str):
        if raw.isdigit():
            return _parse_ts(int(raw))
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None
