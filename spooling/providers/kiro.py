"""Kiro (AWS) session parser.

Kiro stores agent chat sessions as standalone JSON files at:

    ~/Library/Application Support/Kiro/User/globalStorage/kiro.kiroagent/
        workspace-sessions/<base64-workspace-path>/<uuid>.json

Each workspace dir contains a ``sessions.json`` index with metadata
(``sessionId``, ``title``, ``dateCreated``, ``workspaceDirectory``) for
the sessions in that workspace, alongside the per-session JSON files.

Earlier versions of this parser walked Kiro's ``state.vscdb`` files
hoping the chat lived under VS Code-style keys; it does not. Kiro's
`chat.ChatSessionStore.index` value is a stub (``{"version":1,
"entries":{}}``). The real conversation data is in the JSON files
above. We parse those and ignore ``state.vscdb`` entirely.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from spooling.config import CHARS_PER_TOKEN
from spooling.parser import ParsedSession, ParsedMessage
from spooling.providers.base import Provider
from spooling.tracing import build_flat_trace_from_messages

_CANDIDATE_BASES = [
    Path.home() / "Library" / "Application Support" / "Kiro" / "User"
        / "globalStorage" / "kiro.kiroagent" / "workspace-sessions",
    Path.home() / ".config" / "Kiro" / "User"
        / "globalStorage" / "kiro.kiroagent" / "workspace-sessions",
]


class KiroProvider(Provider):
    type_id = "kiro"
    name = "Kiro"

    def default_data_path(self) -> Path:
        for base in _CANDIDATE_BASES:
            if base.exists():
                return base
        return _CANDIDATE_BASES[0]

    def is_available(self) -> bool:
        return any(base.exists() for base in _CANDIDATE_BASES)

    def discover_session_files(self, data_path: Optional[Path] = None) -> list[Path]:
        if data_path is not None and data_path.exists():
            bases = [data_path]
        else:
            bases = [b for b in _CANDIDATE_BASES if b.exists()]

        files: list[Path] = []
        for base in bases:
            for ws_dir in base.iterdir():
                if not ws_dir.is_dir():
                    continue
                for f in ws_dir.glob("*.json"):
                    if f.name == "sessions.json":
                        continue
                    if not _looks_like_uuid(f.stem):
                        continue
                    files.append(f)
        return sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)

    def parse_session_file(self, file_path: Path) -> list[ParsedSession]:
        try:
            with open(file_path) as f:
                data = json.load(f)
        except Exception:
            return []

        history = data.get("history") or []
        if not history:
            return []

        session_id = data.get("sessionId") or file_path.stem
        title = data.get("title") or "Kiro session"
        cwd = (
            data.get("workspaceDirectory")
            or data.get("workspacePath")
            or _decode_workspace_dirname(file_path.parent.name)
        )
        project = Path(cwd).name if cwd else "kiro"

        selected = data.get("selectedModel")
        if isinstance(selected, dict):
            model = (
                selected.get("title")
                or selected.get("model")
                or data.get("defaultModelTitle")
            )
        elif isinstance(selected, str) and selected:
            model = selected
        else:
            model = data.get("defaultModelTitle")

        # Per-message timestamps don't exist in Kiro's payload. Use
        # dateCreated from the sibling sessions.json index for the
        # session's started_at, and leave individual messages without
        # timestamps. Ingest tolerates None.
        started_at = _read_session_index_date(file_path, session_id)

        messages: list[ParsedMessage] = []
        for i, item in enumerate(history):
            msg = item.get("message") or {}
            role = msg.get("role") or "user"
            if role not in ("user", "assistant", "system", "tool"):
                role = "user"
            content = _extract_text(msg.get("content"))
            if not content.strip():
                continue
            messages.append(
                ParsedMessage(
                    uuid=msg.get("id") or f"{session_id}:{i}",
                    session_id=session_id,
                    role=role,
                    content=content,
                    timestamp=started_at if i == 0 else None,
                    cwd=cwd,
                    estimated_tokens=max(1, len(content) // CHARS_PER_TOKEN),
                )
            )

        if not messages:
            return []

        # If the index didn't give us a date, fall back to the file's mtime so
        # the session at least sorts correctly in the UI.
        if started_at is None:
            try:
                started_at = datetime.fromtimestamp(
                    file_path.stat().st_mtime, tz=timezone.utc,
                )
                if messages and messages[0].timestamp is None:
                    messages[0].timestamp = started_at
            except OSError:
                pass

        ps = ParsedSession(
            session_id=session_id,
            project=project,
            messages=messages,
            started_at=started_at,
            ended_at=None,
            cwd=cwd,
            model=model,
            title=_short_title(title, messages),
            provider_id="kiro",
        )
        ps.trace = build_flat_trace_from_messages(
            provider_id="kiro",
            session_id=session_id,
            project=project,
            title=ps.title,
            messages=messages,
            cwd=cwd,
            model=model,
        )
        return [ps]


def _looks_like_uuid(s: str) -> bool:
    return len(s) == 36 and s.count("-") == 4


def _extract_text(content) -> str:
    """Kiro stores user content as a list of typed parts; assistant as a string."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for c in content:
            if isinstance(c, str):
                parts.append(c)
            elif isinstance(c, dict) and c.get("type") == "text":
                parts.append(c.get("text", ""))
        return "\n".join(parts)
    return str(content)


def _decode_workspace_dirname(name: str) -> Optional[str]:
    """Decode the urlsafe-base64 workspace dir name back to its filesystem path.

    Kiro replaces ``=`` padding with ``_``. Restore it before decoding.
    Failures return None; callers fall back to the raw name.
    """
    try:
        s = name.replace("__", "==").replace("_=", "=")
        s = s + "=" * (-len(s) % 4)
        return base64.urlsafe_b64decode(s).decode("utf-8")
    except Exception:
        return None


def _read_session_index_date(session_file: Path, session_id: str) -> Optional[datetime]:
    """Look up dateCreated for this session in the workspace's sessions.json."""
    idx = session_file.parent / "sessions.json"
    if not idx.exists():
        return None
    try:
        with open(idx) as f:
            entries = json.load(f)
    except Exception:
        return None
    if not isinstance(entries, list):
        return None
    for e in entries:
        if not isinstance(e, dict):
            continue
        if e.get("sessionId") != session_id:
            continue
        raw = e.get("dateCreated")
        if isinstance(raw, str) and raw.isdigit():
            raw = int(raw)
        if isinstance(raw, (int, float)):
            return datetime.fromtimestamp(raw / 1000, tz=timezone.utc)
    return None


def _short_title(title: str, messages: list[ParsedMessage]) -> str:
    """If Kiro left the default 'New Session', synthesize from the first user msg."""
    t = (title or "").strip()
    if t and t.lower() != "new session":
        return t
    first_user = next((m for m in messages if m.role == "user"), None)
    if not first_user:
        return t or "Kiro session"
    snippet = first_user.content.replace("\n", " ").strip()
    if len(snippet) > 80:
        snippet = snippet[:80] + "..."
    return snippet or t or "Kiro session"
