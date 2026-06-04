"""Semantic search over session history via pgvector."""

from spooling.db import get_connection
from spooling.embeddings import embed_text

MIN_SIMILARITY = 0.25
OVERFETCH = 4


def search(query: str, limit: int = 10, project: str | None = None) -> list[dict]:
    """Search session chunks by semantic similarity."""
    vec = embed_text(query)
    fetch_n = max(limit * OVERFETCH, 40)

    conn = get_connection()

    conn.execute("SET LOCAL ivfflat.probes = 40")

    if project:
        rows = conn.execute(
            """SELECT c.content, c.role, c.project, c.timestamp, c.session_id,
                      1 - (c.embedding <=> %s::vector) AS similarity,
                      s.title, s.cwd
               FROM chunks c
               JOIN sessions s ON s.id = c.session_id
               WHERE c.project = %s
               ORDER BY c.embedding <=> %s::vector
               LIMIT %s""",
            (str(vec), project, str(vec), fetch_n),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT c.content, c.role, c.project, c.timestamp, c.session_id,
                      1 - (c.embedding <=> %s::vector) AS similarity,
                      s.title, s.cwd
               FROM chunks c
               JOIN sessions s ON s.id = c.session_id
               ORDER BY c.embedding <=> %s::vector
               LIMIT %s""",
            (str(vec), str(vec), fetch_n),
        ).fetchall()

    conn.close()

    # Drop low-similarity noise, then keep one chunk per session (best-scoring).
    seen: set[str] = set()
    results: list[dict] = []
    for r in rows:
        sim = float(r["similarity"])
        if sim < MIN_SIMILARITY:
            continue
        sid = r["session_id"]
        if sid in seen:
            continue
        seen.add(sid)
        results.append({
            "content": r["content"][:200],
            "role": r["role"],
            "project": r["project"],
            "timestamp": r["timestamp"].isoformat() if r["timestamp"] else None,
            "session_id": sid,
            "similarity": round(sim, 4),
            "title": r["title"],
            "cwd": r["cwd"],
        })
        if len(results) >= limit:
            break
    return results
