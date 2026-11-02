"""Tests for the storage optimizations: tool_call dedupe, incremental opencode
ingest, embedding backfill, cached semantic search, and `optimize`."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

from spooling import db as db_mod
from spooling import ingest, search as search_mod
from spooling.optimize import dedupe_tool_calls, optimize, truncate_previews
from spooling.parser import ParsedMessage, ParsedSession, ToolCallDetail
from spooling.providers.opencode import OpencodeProvider

NOW = datetime(2026, 11, 2, tzinfo=timezone.utc)
DIM = 4


@pytest.fixture
def conn(tmp_path, monkeypatch):
    path = tmp_path / "spooling.db"
    monkeypatch.setattr(db_mod, "DB_PATH", path)
    c = db_mod.get_connection()
    yield c
    c.close()


def make_session(sid="s1", n_tools=2, text="hello world") -> ParsedSession:
    tools = [
        ToolCallDetail(tool_use_id=f"t{i}", name="Bash", input_summary=f"ls {i}", result_preview="ok")
        for i in range(n_tools)
    ]
    msgs = [
        ParsedMessage(uuid=f"{sid}-m1", session_id=sid, role="user", content=text, timestamp=NOW),
        ParsedMessage(
            uuid=f"{sid}-m2", session_id=sid, role="assistant",
            content="\n".join(f"[tool: Bash]" for _ in tools), timestamp=NOW,
            tools_used=[t.name for t in tools], tool_details=tools,
        ),
    ]
    return ParsedSession(session_id=sid, project="proj", messages=msgs, started_at=NOW, ended_at=NOW)


def count(conn, table, where="1=1"):
    return conn.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE {where}").fetchone()["n"]


# ---------------------------------------------------------------------------
# tool_calls duplication
# ---------------------------------------------------------------------------

def test_store_session_twice_does_not_duplicate_tool_calls(conn):
    for _ in range(3):
        ingest._store_session(conn, make_session(n_tools=2))
    conn.commit()
    assert count(conn, "tool_calls") == 2
    assert count(conn, "messages") == 2


def test_store_session_updates_tool_calls_when_session_grows(conn):
    ingest._store_session(conn, make_session(n_tools=2))
    ingest._store_session(conn, make_session(n_tools=3))
    conn.commit()
    assert count(conn, "tool_calls") == 3


def test_result_preview_is_capped(conn):
    s = make_session(n_tools=1)
    s.messages[1].tool_details[0].result_preview = "x" * 10_000
    ingest._store_session(conn, s)
    conn.commit()
    row = conn.execute("SELECT length(tool_result_preview) AS n FROM tool_calls").fetchone()
    assert row["n"] == ingest.MAX_RESULT_PREVIEW


def test_dedupe_keeps_latest_block_only(conn):
    ingest._store_session(conn, make_session(n_tools=2))
    # Simulate old buggy behaviour: two extra appended blocks.
    for _ in range(2):
        for i in range(2):
            conn.execute(
                "INSERT INTO tool_calls (session_id, message_id, tool_name, tool_input, timestamp) "
                "VALUES ('s1', 's1-m2', 'Bash', ?, ?)", (f"old {i}", NOW.isoformat()),
            )
    conn.commit()
    assert count(conn, "tool_calls") == 6
    raw = sqlite3.connect(db_mod.DB_PATH)
    raw.row_factory = sqlite3.Row
    assert dedupe_tool_calls(raw) == 4
    raw.commit()
    rows = raw.execute("SELECT tool_input FROM tool_calls ORDER BY id").fetchall()
    # newest block (the two "old N" rows appended last) survives
    assert [r["tool_input"] for r in rows] == ["old 0", "old 1"]
    raw.close()


def test_truncate_previews(conn):
    ingest._store_session(conn, make_session(n_tools=1))
    conn.execute("UPDATE tool_calls SET tool_result_preview = ?", ("y" * 5000,))
    conn.commit()
    raw = sqlite3.connect(db_mod.DB_PATH)
    assert truncate_previews(raw, 100) == 1
    assert raw.execute("SELECT length(tool_result_preview) FROM tool_calls").fetchone()[0] == 100
    raw.close()


# ---------------------------------------------------------------------------
# incremental opencode
# ---------------------------------------------------------------------------

def make_opencode_db(path: Path):
    c = sqlite3.connect(path)
    c.executescript(
        """
        CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT, title TEXT, version TEXT,
            model TEXT, agent TEXT, time_created INT, time_updated INT);
        CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, time_created INT,
            time_updated INT, data TEXT);
        CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
            time_created INT, time_updated INT, data TEXT);
        """
    )
    for sid, t in (("a", 1000), ("b", 2000)):
        c.execute("INSERT INTO session VALUES (?, '/tmp/proj', ?, '1', NULL, NULL, ?, ?)", (sid, sid, t, t))
        c.execute("INSERT INTO message VALUES (?, ?, ?, ?, ?)", (f"{sid}-m", sid, t, t, json.dumps({"role": "user"})))
        c.execute(
            "INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
            (f"{sid}-p", f"{sid}-m", sid, t, t, json.dumps({"type": "text", "text": f"hi {sid}"})),
        )
    c.commit()
    return c


def test_opencode_skips_unchanged_sessions(tmp_path):
    dbp = tmp_path / "opencode.db"
    src = make_opencode_db(dbp)
    prov = OpencodeProvider()

    first = prov.parse_session_file(dbp, synced={})
    assert {s.session_id for s in first} == {"a", "b"}
    assert all(s.source_key and s.source_version for s in first)

    synced = {s.source_key: s.source_version for s in first}
    assert prov.parse_session_file(dbp, synced=synced) == []

    # New activity on one session only (a part update, not a session row change).
    src.execute("UPDATE part SET time_updated = 9999 WHERE session_id = 'b'")
    src.commit()
    changed = prov.parse_session_file(dbp, synced=synced)
    assert [s.session_id for s in changed] == ["b"]
    assert changed[0].source_version == 9999


def test_sync_state_roundtrip_makes_second_ingest_a_noop(conn, tmp_path):
    dbp = tmp_path / "opencode.db"
    make_opencode_db(dbp)
    prov = OpencodeProvider()

    for s in ingest.parse_file(prov, dbp, ingest._get_synced_files(conn)):
        ingest._store_session(conn, s)
        ingest._mark_session_synced(conn, s)
    conn.commit()
    assert ingest.parse_file(prov, dbp, ingest._get_synced_files(conn)) == []


# ---------------------------------------------------------------------------
# embedding backfill
# ---------------------------------------------------------------------------

def fake_embed(texts):
    return [[float(len(t) % 7), 1.0, 0.0, 0.0] for t in texts]


def test_backfill_embeds_missing_messages_once(conn, monkeypatch):
    monkeypatch.setattr(ingest, "embed_texts", fake_embed)
    ingest._store_session(conn, make_session())
    conn.commit()

    # m1 is real text; m2 is only "[tool: Bash]" markers -> no chunks.
    assert ingest.backfill_embeddings(conn) == 1
    assert count(conn, "chunks") == 1
    assert conn.execute("SELECT message_id FROM chunks").fetchone()["message_id"] == "s1-m1"
    assert ingest.backfill_embeddings(conn) == 0


def test_tool_only_messages_make_no_chunks():
    assert ingest._chunks_for_content("[tool: Bash]\n[tool: Read]") == []
    assert ingest._chunks_for_content("fixed it [tool: Edit]") != []
    assert ingest._chunks_for_content("") == []
    assert ingest._chunks_for_content(None) == []


# ---------------------------------------------------------------------------
# search index
# ---------------------------------------------------------------------------

def add_chunk(conn, sid, project, vec, text):
    conn.execute(
        "INSERT OR IGNORE INTO sessions (id, provider_id, project, title) VALUES (?, 'p', ?, ?)",
        (sid, project, f"title {sid}"),
    )
    conn.execute(
        "INSERT INTO chunks (session_id, message_id, content, role, project, embedding) "
        "VALUES (?, ?, ?, 'user', ?, ?)",
        (sid, f"{sid}-m", text, project, np.array(vec, dtype=np.float32).tobytes()),
    )
    conn.commit()


def unit(*v):
    a = np.array(v, dtype=np.float32)
    return (a / np.linalg.norm(a)).tolist()


def test_search_ranks_filters_and_refreshes_cache(conn, monkeypatch):
    search_mod._INDEX = None
    monkeypatch.setattr(search_mod, "embed_text", lambda q: unit(1, 0, 0, 0))
    add_chunk(conn, "s1", "alpha", unit(1, 0, 0, 0), "exact match")
    add_chunk(conn, "s2", "beta", unit(1, 1, 0, 0), "partial match")
    add_chunk(conn, "s3", "alpha", unit(0, 1, 0, 0), "orthogonal")

    res = search_mod.search("q", limit=5)
    assert [r["session_id"] for r in res] == ["s1", "s2"]  # s3 below MIN_SIMILARITY
    assert res[0]["similarity"] == pytest.approx(1.0)

    assert [r["session_id"] for r in search_mod.search("q", project="beta")] == ["s2"]

    first_index = search_mod._INDEX
    search_mod.search("q")
    assert search_mod._INDEX is first_index  # cached, not rebuilt

    add_chunk(conn, "s4", "alpha", unit(1, 0.1, 0, 0), "new chunk")
    assert "s4" in [r["session_id"] for r in search_mod.search("q")]
    assert search_mod._INDEX is not first_index


def test_search_one_hit_per_session(conn, monkeypatch):
    search_mod._INDEX = None
    monkeypatch.setattr(search_mod, "embed_text", lambda q: unit(1, 0, 0, 0))
    add_chunk(conn, "s1", "alpha", unit(1, 0, 0, 0), "a")
    add_chunk(conn, "s1", "alpha", unit(1, 0.1, 0, 0), "b")
    assert len(search_mod.search("q")) == 1


def test_search_empty_db(conn, monkeypatch):
    search_mod._INDEX = None
    monkeypatch.setattr(search_mod, "embed_text", lambda q: unit(1, 0, 0, 0))
    assert search_mod.search("q") == []


# ---------------------------------------------------------------------------
# optimize end to end
# ---------------------------------------------------------------------------

def test_optimize_end_to_end(conn, tmp_path):
    ingest._store_session(conn, make_session(n_tools=2))
    for _ in range(3):
        for i in range(2):
            conn.execute(
                "INSERT INTO tool_calls (session_id, message_id, tool_name, timestamp) "
                "VALUES ('s1', 's1-m2', 'Bash', ?)", (NOW.isoformat(),),
            )
    conn.commit()
    conn.close()

    summary = optimize(db_mod.DB_PATH, embed=False, log=lambda m: None)

    assert summary["tool_calls_deleted"] == 6
    assert Path(summary["backup"]).exists()

    raw = sqlite3.connect(db_mod.DB_PATH)
    assert raw.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0] == 2
    assert raw.execute("PRAGMA auto_vacuum").fetchone()[0] == 2  # INCREMENTAL
    raw.close()

    # Idempotent
    again = optimize(db_mod.DB_PATH, backup=False, embed=False, log=lambda m: None)
    assert again["tool_calls_deleted"] == 0
