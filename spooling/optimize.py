"""One-shot maintenance for the local SQLite store: `spooling optimize`.

Older versions appended a fresh copy of every session's tool calls on each
re-ingest (the opencode provider re-ingests *every* session on *every* sync),
so ``tool_calls`` could be ~20x larger than the real data. This module:

1. backs the database up,
2. collapses duplicated ``tool_calls`` blocks,
3. truncates oversized tool-result previews,
4. embeds messages that are missing chunks (``sync --no-embed`` / push),
5. enables incremental auto-vacuum and VACUUMs to give the space back.
"""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from spooling.config import DB_PATH

MAX_RESULT_PREVIEW = 2000


def db_size(path: Path = DB_PATH) -> int:
    total = 0
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(path) + suffix)
        if p.exists():
            total += p.stat().st_size
    return total


def duplicate_tool_calls(conn: sqlite3.Connection) -> tuple[int, int]:
    """Return (stored_rows, expected_rows) for tool_calls.

    ``expected`` is the sum of per-session tool_call_count, which is what a
    clean store holds. Cheap enough to run after every sync as a hint.
    """
    stored = conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0]
    expected = conn.execute("SELECT COALESCE(SUM(tool_call_count), 0) FROM sessions").fetchone()[0]
    return stored, expected


def dedupe_tool_calls(conn: sqlite3.Connection) -> int:
    """Keep only each session's most recent block of tool calls.

    Every ingest of a session inserted one full block of ``tool_call_count``
    rows, so the newest ``tool_call_count`` rows (by id) are exactly the
    current copy. Returns rows deleted.
    """
    cur = conn.execute(
        """DELETE FROM tool_calls WHERE id IN (
               SELECT id FROM (
                   SELECT t.id AS id,
                          ROW_NUMBER() OVER (PARTITION BY t.session_id ORDER BY t.id DESC) AS rn,
                          s.tool_call_count AS n
                   FROM tool_calls t
                   JOIN sessions s ON s.id = t.session_id
               ) WHERE n > 0 AND rn > n
           )"""
    )
    return cur.rowcount


def truncate_previews(conn: sqlite3.Connection, limit: int = MAX_RESULT_PREVIEW) -> int:
    cur = conn.execute(
        "UPDATE tool_calls SET tool_result_preview = substr(tool_result_preview, 1, ?) "
        "WHERE length(tool_result_preview) > ?",
        (limit, limit),
    )
    return cur.rowcount


def backup_database(path: Path = DB_PATH) -> Path:
    """Consistent copy via the SQLite backup API (safe with WAL/open readers)."""
    backups = path.parent / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    dest = backups / f"{path.stem}-{datetime.now():%Y%m%d-%H%M%S}.db"
    free = shutil.disk_usage(path.parent).free
    need = db_size(path) * 2  # backup + headroom for VACUUM's temp copy
    if free < need:
        raise RuntimeError(
            f"Not enough free disk space for backup + VACUUM "
            f"(need ~{need // 2**20} MB, have {free // 2**20} MB). "
            f"Free some space or re-run with --no-backup --no-vacuum."
        )
    src = sqlite3.connect(str(path))
    dst = sqlite3.connect(str(dest))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return dest


def optimize(
    path: Path = DB_PATH,
    *,
    backup: bool = True,
    vacuum: bool = True,
    embed: bool = True,
    log: Callable[[str], None] = print,
) -> dict:
    """Run all maintenance steps. Returns a summary dict."""
    summary: dict = {"before_bytes": db_size(path)}

    if backup:
        dest = backup_database(path)
        summary["backup"] = str(dest)
        log(f"Backup written to {dest}")

    # isolation_level=None -> autocommit, required for VACUUM.
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=60)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        stored, expected = duplicate_tool_calls(conn)
        log(f"tool_calls: {stored:,} stored vs ~{expected:,} expected")

        conn.execute("BEGIN")
        summary["tool_calls_deleted"] = dedupe_tool_calls(conn)
        summary["previews_truncated"] = truncate_previews(conn)
        conn.execute("COMMIT")
        log(
            f"Removed {summary['tool_calls_deleted']:,} duplicate tool_calls, "
            f"truncated {summary['previews_truncated']:,} previews"
        )
    finally:
        conn.close()

    if embed:
        from spooling.db import get_connection
        from spooling.ingest import backfill_embeddings

        c = get_connection()
        try:
            summary["chunks_backfilled"] = backfill_embeddings(c, log=log)
        finally:
            c.close()
        log(f"Backfilled {summary['chunks_backfilled']:,} chunks")

    conn = sqlite3.connect(str(path), isolation_level=None, timeout=60)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        if vacuum:
            # auto_vacuum only takes effect on a full VACUUM.
            conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
            log("Running VACUUM (this can take a minute)…")
            conn.execute("VACUUM")
        conn.execute("ANALYZE")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()

    summary["after_bytes"] = db_size(path)
    return summary
