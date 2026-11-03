"""Session id helpers: resolve id prefixes and find ids mentioned in a search query."""

import re

from spooling.db import get_connection

# Tokens that look like (part of) a session id:
#   - opencode style ids: ``ses_...``
#   - UUIDs or hex prefixes of at least 8 chars (dashes allowed)
# Plain words and short numbers are never treated as ids.
_ID_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-]{5,}")
_HEX_ID = re.compile(r"^(?=.*\d)[0-9a-fA-F][0-9a-fA-F\-]{7,}$")


def looks_like_session_id(token: str) -> bool:
    if token.isdigit():
        return False
    return token.lower().startswith("ses_") or bool(_HEX_ID.match(token))


def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def resolve_session_ids(ref: str, limit: int = 10) -> list[str]:
    """Resolve an exact id or id prefix to matching session ids."""
    conn = get_connection()
    try:
        exact = conn.execute("SELECT id FROM sessions WHERE id = ?", (ref,)).fetchone()
        if exact:
            return [exact["id"]]
        rows = conn.execute(
            "SELECT id FROM sessions WHERE id LIKE ? ESCAPE '\\' ORDER BY started_at DESC LIMIT ?",
            (_like_escape(ref) + "%", limit),
        ).fetchall()
        return [r["id"] for r in rows]
    finally:
        conn.close()


def find_sessions_by_id_in_query(query: str, limit: int = 10) -> list[dict]:
    """Find sessions whose id (or id prefix/substring) appears in a free-text query.

    Lets semantic search accept a session id (full or partial) alongside or
    instead of natural language.
    """
    tokens = {t for t in _ID_TOKEN.findall(query) if looks_like_session_id(t)}
    if not tokens:
        return []
    clauses, params = [], []
    for t in tokens:
        clauses.append("id LIKE ? ESCAPE '\\'")
        params.append(f"%{_like_escape(t)}%")
    conn = get_connection()
    try:
        rows = conn.execute(
            f"SELECT id, title, project, cwd, started_at FROM sessions WHERE {' OR '.join(clauses)} "
            "ORDER BY started_at DESC LIMIT ?",
            tuple(params) + (limit,),
        ).fetchall()
        return [dict(r) if not isinstance(r, dict) else r for r in rows]
    finally:
        conn.close()
