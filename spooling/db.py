"""SQLite database connection with PostgreSQL compatibility shim.

Replaces the old psycopg/PostgreSQL backend so `pip install spooling`
works without Docker or any system dependencies.

The shim translates the subset of PostgreSQL SQL idioms used in this
codebase to their SQLite equivalents at query time:

  %s           → ?            (parameter placeholder)
  now()        → datetime('now')
  ::typename   → (stripped)   (%s::vector, %s::jsonb, etc.)
  ILIKE        → LIKE         (SQLite LIKE is case-insensitive for ASCII)
"""

import re
import sqlite3
from pathlib import Path

from spooling.config import DB_PATH
from spooling.schema import SCHEMA_SQL


# ---------------------------------------------------------------------------
# SQL compatibility translation
# ---------------------------------------------------------------------------

_PLACEHOLDER_RE = re.compile(r"%s")
_CAST_RE = re.compile(r"::\w+")
_ILIKE_RE = re.compile(r"\bILIKE\b", re.IGNORECASE)


def _adapt_sql(sql: str) -> str:
    """Translate PostgreSQL SQL idioms → SQLite equivalents."""
    sql = _PLACEHOLDER_RE.sub("?", sql)
    sql = sql.replace("now()", "datetime('now')")
    sql = _CAST_RE.sub("", sql)
    sql = _ILIKE_RE.sub("LIKE", sql)
    return sql


# ---------------------------------------------------------------------------
# Row and cursor wrappers
# ---------------------------------------------------------------------------

class _Row(dict):
    """dict subclass that also supports attribute access and .get()."""

    def __getattr__(self, key: str):
        try:
            return self[key]
        except KeyError:
            raise AttributeError(key) from None


def _row_factory(cursor: sqlite3.Cursor, row: tuple) -> _Row:
    return _Row(zip((col[0] for col in cursor.description), row))


class _Cursor:
    """Thin wrapper around sqlite3.Cursor with psycopg-style execute()."""

    __slots__ = ("_cur",)

    def __init__(self, cur: sqlite3.Cursor) -> None:
        self._cur = cur

    def execute(self, sql: str, params=()) -> "_Cursor":
        self._cur.execute(_adapt_sql(sql), params)
        return self

    def executemany(self, sql: str, seq) -> "_Cursor":
        self._cur.executemany(_adapt_sql(sql), seq)
        return self

    def fetchone(self) -> _Row | None:
        return self._cur.fetchone()

    def fetchall(self) -> list[_Row]:
        return self._cur.fetchall()

    @property
    def lastrowid(self) -> int | None:
        return self._cur.lastrowid

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount


class SpoolingConnection:
    """Wraps sqlite3.Connection with a psycopg-compatible interface."""

    __slots__ = ("_conn",)

    def __init__(self, conn: sqlite3.Connection) -> None:
        conn.row_factory = _row_factory
        self._conn = conn

    # ---- core methods ----

    def execute(self, sql: str, params=()) -> _Cursor:
        return _Cursor(self._conn.execute(_adapt_sql(sql), params))

    def executemany(self, sql: str, seq) -> _Cursor:
        return _Cursor(self._conn.executemany(_adapt_sql(sql), seq))

    def cursor(self) -> _Cursor:
        return _Cursor(self._conn.cursor())

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()

    # ---- context manager ----

    def __enter__(self) -> "SpoolingConnection":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if exc_type is None:
            self._conn.commit()
        else:
            self._conn.rollback()
        self._conn.close()


# ---------------------------------------------------------------------------
# Schema bootstrap
# ---------------------------------------------------------------------------

def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create all tables/indexes if they don't exist yet."""
    conn.executescript(SCHEMA_SQL)
    conn.commit()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_connection() -> SpoolingConnection:
    """Open (and auto-create if needed) the local SQLite database."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    raw.execute("PRAGMA journal_mode=WAL")
    raw.execute("PRAGMA foreign_keys=ON")
    _ensure_schema(raw)
    return SpoolingConnection(raw)


def check_db() -> bool:
    """Return True if the database is reachable (always True for SQLite)."""
    try:
        conn = get_connection()
        conn.execute("SELECT 1")
        conn.close()
        return True
    except Exception:
        return False
