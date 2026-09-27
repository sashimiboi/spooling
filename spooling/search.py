"""Semantic search over session history.

Embeddings are stored as packed float32 BLOBs in the chunks table.
Search uses numpy brute-force cosine similarity — fast enough for
local datasets (typically <50k chunks) with no extra dependencies
beyond what sentence-transformers already pulls in.
"""

import struct

import numpy as np

from spooling.db import get_connection
from spooling.embeddings import embed_text

MIN_SIMILARITY = 0.25
OVERFETCH = 4


def _deserialize(blob: bytes) -> np.ndarray:
    n = len(blob) // 4
    return np.frombuffer(blob, dtype=np.float32)


def search(query: str, limit: int = 10, project: str | None = None) -> list[dict]:
    """Search session chunks by semantic similarity."""
    vec = embed_text(query)
    query_arr = np.array(vec, dtype=np.float32)

    conn = get_connection()
    if project:
        rows = conn.execute(
            """SELECT c.id, c.content, c.role, c.project, c.timestamp,
                      c.session_id, c.embedding, s.title, s.cwd
               FROM chunks c
               JOIN sessions s ON s.id = c.session_id
               WHERE c.project = ? AND c.embedding IS NOT NULL""",
            (project,),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT c.id, c.content, c.role, c.project, c.timestamp,
                      c.session_id, c.embedding, s.title, s.cwd
               FROM chunks c
               JOIN sessions s ON s.id = c.session_id
               WHERE c.embedding IS NOT NULL"""
        ).fetchall()
    conn.close()

    if not rows:
        return []

    # Build embedding matrix — all-MiniLM-L6-v2 produces unit vectors so
    # dot-product == cosine similarity directly.
    embeddings = np.array([_deserialize(r["embedding"]) for r in rows], dtype=np.float32)
    similarities = embeddings @ query_arr  # (n_chunks,)

    # Sort descending; keep one chunk per session (best score), drop noise.
    order = np.argsort(similarities)[::-1]
    fetch_n = max(limit * OVERFETCH, 40)

    seen: set[str] = set()
    results: list[dict] = []
    for idx in order[:fetch_n]:
        sim = float(similarities[idx])
        if sim < MIN_SIMILARITY:
            break
        r = rows[idx]
        sid = r["session_id"]
        if sid in seen:
            continue
        seen.add(sid)
        results.append({
            "content": (r["content"] or "")[:200],
            "role": r["role"],
            "project": r["project"],
            "timestamp": r["timestamp"],
            "session_id": sid,
            "similarity": round(sim, 4),
            "title": r["title"],
            "cwd": r["cwd"],
        })
        if len(results) >= limit:
            break

    return results
