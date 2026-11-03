"""Tests for session-id lookup: id detection, prefix resolution, id-aware search,
the MCP get_session tool, and the `session` CLI commands."""

from datetime import datetime, timezone

import numpy as np
import pytest
from click.testing import CliRunner

from spooling import db as db_mod
from spooling import ingest, search as search_mod
from spooling.parser import ParsedMessage, ParsedSession
from spooling.sessions import (
    find_sessions_by_id_in_query,
    looks_like_session_id,
    resolve_session_ids,
)

NOW = datetime(2026, 11, 2, tzinfo=timezone.utc)

OC1 = "ses_e6310bd1fffezQhC7EHnDnrp3R"
OC2 = "ses_e6310cafefffeAAAAAAAAAAAAA"
UU1 = "0b104f33-1ffe-4a2b-9c3d-123456789abc"


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "spooling.db")
    c = db_mod.get_connection()
    for sid, title in [(OC1, "login issue"), (OC2, "other work"), (UU1, "uuid session")]:
        ingest._store_session(c, ParsedSession(
            session_id=sid, project="proj", started_at=NOW, ended_at=NOW,
            messages=[
                ParsedMessage(uuid=f"{sid}-1", session_id=sid, role="user", content=f"hello from {title}", timestamp=NOW),
                ParsedMessage(uuid=f"{sid}-2", session_id=sid, role="assistant", content="reply " + "x" * 50, timestamp=NOW),
            ],
        ))
        c.execute("UPDATE sessions SET title = ? WHERE id = ?", (title, sid))
    c.commit()
    yield c
    c.close()


@pytest.mark.parametrize("token,expected", [
    ("ses_e6310bd1", True),
    ("SES_E6310BD1", True),
    ("e6310bd1fffe", True),
    ("0b104f33-1ffe-4a2b", True),
    ("password", False),
    ("authentication", False),
    ("deadbeef", False),     # hex-looking word, no digit
    ("12345678", False),     # plain number
    ("login-issue", False),
])
def test_looks_like_session_id(token, expected):
    assert looks_like_session_id(token) is expected


def test_resolve_exact_prefix_ambiguous_and_missing(conn):
    assert resolve_session_ids(OC1) == [OC1]
    assert resolve_session_ids("ses_e6310bd1") == [OC1]
    assert sorted(resolve_session_ids("ses_e6310")) == sorted([OC1, OC2])
    assert resolve_session_ids("nope") == []


def test_resolve_escapes_like_wildcards(conn):
    assert resolve_session_ids("%") == []
    assert resolve_session_ids("ses_%") == []  # '%' is literal, not a wildcard


def test_find_by_id_in_query(conn):
    hits = find_sessions_by_id_in_query("what happened in ses_e6310bd1 yesterday")
    assert [h["id"] for h in hits] == [OC1]
    assert find_sessions_by_id_in_query("how do I fix authentication")  == []
    assert [h["id"] for h in find_sessions_by_id_in_query("0b104f33-1ffe")] == [UU1]


def test_search_ranks_id_match_first(conn, monkeypatch):
    # Stub the semantic side so the test needs no embedding model.
    monkeypatch.setattr(search_mod, "_semantic_search", lambda q, limit, project: [
        {"content": "other", "role": "user", "project": "proj", "timestamp": "t",
         "session_id": OC2, "similarity": 0.9, "title": "other work", "cwd": None},
        {"content": "dup", "role": "user", "project": "proj", "timestamp": "t",
         "session_id": OC1, "similarity": 0.5, "title": "login issue", "cwd": None},
    ])
    res = search_mod.search("ses_e6310bd1fffe", limit=5)
    assert res[0]["session_id"] == OC1
    assert res[0]["match"] == "session_id"
    assert res[0]["similarity"] == 1.0
    # no duplicate of the id-matched session from the semantic results
    assert [r["session_id"] for r in res].count(OC1) == 1
    assert len(res) == 2


def test_search_id_match_respects_project_and_limit(conn, monkeypatch):
    monkeypatch.setattr(search_mod, "_semantic_search", lambda q, limit, project: [])
    assert search_mod.search("ses_e6310bd1", project="other") == []
    assert len(search_mod.search("ses_e6310", limit=1)) == 1


def test_mcp_get_session(conn):
    from spooling import mcp_server

    out = mcp_server.get_session("ses_e6310bd1")
    assert out["session"]["id"] == OC1
    assert out["message_count"] == 2
    assert [m["role"] for m in out["messages"]] == ["user", "assistant"]

    short = mcp_server.get_session(OC1, max_chars=5)
    assert short["messages"][1]["content"].endswith("...")

    capped = mcp_server.get_session(OC1, max_messages=1)
    assert len(capped["messages"]) == 1 and capped["truncated"] is True

    assert "messages" not in mcp_server.get_session(OC1, include_messages=False)
    assert "ambiguous" in mcp_server.get_session("ses_e6310")["error"]
    assert "not found" in mcp_server.get_session("zzz")["error"]


def test_cli_session_show_and_list(conn):
    from spooling.cli import cli

    r = CliRunner()
    res = r.invoke(cli, ["session", "show", "ses_e6310bd1", "-m"])
    assert res.exit_code == 0, res.output
    assert "hello from login issue" in res.output

    res = r.invoke(cli, ["session", "show", "ses_e6310"])
    assert res.exit_code == 1
    assert OC1 in res.output and OC2 in res.output

    res = r.invoke(cli, ["session", "list", "--id", "0b104f33"], env={"COLUMNS": "200"})
    assert res.exit_code == 0
    assert UU1 in res.output
    assert OC1 not in res.output
