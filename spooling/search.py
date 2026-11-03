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
    return np.frombuffer(blob, dtype=np.float32)


class _Index:
    """In-memory embedding matrix, rebuilt only when the chunks table changes.

    Only ids/projects and the float32 matrix are cached; chunk text is fetched
    for the handful of top hits, so building the index never touches the large
    ``content`` column.
    """

    __slots__ = ("stamp", "ids", "projects", "matrix")

    def __init__(self, stamp, ids, projects, matrix):
        self.stamp = stamp
        self.ids = ids
        self.projects = projects
        self.matrix = matrix


_INDEX: _Index | None = None

def _stamp(conn, dim: int) -> tuple[int, int, int]:
    row = conn.execute("SELECT COUNT(*) AS n, COALESCE(MAX(id), 0) AS m FROM chunks").fetchone()
    return (row["n"], row["m"], dim)


def _load_index(conn, dim: int) -> _Index:
    global _INDEX
    stamp = _stamp(conn, dim)
    if _INDEX is not None and _INDEX.stamp == stamp:
        return _INDEX

    ids: list[int] = []
    projects: list[str | None] = []
    blobs: list[bytes] = []
    for r in conn.execute(
        "SELECT id, project, embedding FROM chunks WHERE embedding IS NOT NULL"
    ).fetchall():
        emb = r["embedding"]
        if len(emb) != dim * 4:  # skip rows from a different embedding model
            continue
        ids.append(r["id"])
        projects.append(r["project"])
        blobs.append(emb)

    if blobs:
        matrix = np.frombuffer(b"".join(blobs), dtype=np.float32).reshape(len(blobs), dim)
    else:
        matrix = np.zeros((0, dim), dtype=np.float32)
    _INDEX = _Index(stamp, np.array(ids, dtype=np.int64), np.array(projects, dtype=object), matrix)
    return _INDEX


def search(query: str, limit: int = 10, project: str | None = None) -> list[dict]:
    """Search session chunks by semantic similarity.

    Session ids (full or partial) found in the query are matched directly and
    ranked first, so ids are searchable alongside titles and content.
    """
    id_hits = _id_matches(query, project)
    results = _semantic_search(query, limit, project)
    seen = {r["session_id"] for r in id_hits}
    merged = id_hits + [r for r in results if r["session_id"] not in seen]
    return merged[:limit]


def _id_matches(query: str, project: str | None) -> list[dict]:
    from spooling.sessions import find_sessions_by_id_in_query

    out = []
    for s in find_sessions_by_id_in_query(query):
        if project and s["project"] != project:
            continue
        out.append({
            "content": s["title"] or "(untitled)",
            "role": "session",
            "project": s["project"],
            "timestamp": s["started_at"],
            "session_id": s["id"],
            "similarity": 1.0,
            "title": s["title"],
            "cwd": s["cwd"],
            "match": "session_id",
        })
    return out


def _semantic_search(query: str, limit: int = 10, project: str | None = None) -> list[dict]:
    query_arr = np.array(embed_text(query), dtype=np.float32)

    conn = get_connection()
    try:
        index = _load_index(conn, query_arr.shape[0])
        if index.matrix.shape[0] == 0:
            return []

        # all-MiniLM-L6-v2 produces unit vectors, so dot-product == cosine.
        sims = index.matrix @ query_arr
        if project:
            sims = np.where(index.projects == project, sims, -1.0)

        fetch_n = min(max(limit * OVERFETCH, 40), sims.shape[0])
        top = np.argpartition(-sims, fetch_n - 1)[:fetch_n]
        top = top[np.argsort(-sims[top])]
        top = [i for i in top if sims[i] >= MIN_SIMILARITY]
        if not top:
            return []

        top_ids = [int(index.ids[i]) for i in top]
        marks = ",".join("?" * len(top_ids))
        rows = {
            r["id"]: r
            for r in conn.execute(
                f"""SELECT c.id, c.content, c.role, c.project, c.timestamp,
                           c.session_id, s.title, s.cwd
                    FROM chunks c JOIN sessions s ON s.id = c.session_id
                    WHERE c.id IN ({marks})""",
                top_ids,
            ).fetchall()
        }
    finally:
        conn.close()

    # One chunk per session (best score first).
    seen: set[str] = set()
    results: list[dict] = []
    for i, cid in zip(top, top_ids):
        r = rows.get(cid)
        if r is None:
            continue
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
            "similarity": round(float(sims[i]), 4),
            "title": r["title"],
            "cwd": r["cwd"],
        })
        if len(results) >= limit:
            break

    return results
