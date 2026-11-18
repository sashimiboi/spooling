"""Tests for session summaries: schema migration, ingest, the Claude Code
`summary` record, `spooling.summarize`, and the `spooling summarize` command."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from click.testing import CliRunner

from spooling import db as db_mod
from spooling import ingest
from spooling import summarize as sm
from spooling.cli import cli
from spooling.parser import ParsedMessage, ParsedSession, parse_session_file

NOW = datetime.now(timezone.utc)
SID = "33333333-aaaa-4bbb-8ccc-000000000003"


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "spooling.db")
    c = db_mod.get_connection()
    yield c
    c.close()


def add_session(conn, sid, *, n_msgs=6, summary=None, title="a title", days_ago=0, project="proj"):
    ingest._store_session(conn, ParsedSession(
        session_id=sid, project=project, title=title, summary=summary,
        started_at=NOW - timedelta(days=days_ago), ended_at=NOW,
        messages=[
            ParsedMessage(uuid=f"{sid}-{i}", session_id=sid,
                          role="user" if i % 2 == 0 else "assistant",
                          content=f"message number {i}", timestamp=NOW + timedelta(seconds=i))
            for i in range(n_msgs)
        ],
    ))
    conn.commit()


def row(conn, sid):
    return conn.execute("SELECT * FROM sessions WHERE id = ?", (sid,)).fetchone()


# --- schema / migration ----------------------------------------------------

def test_migrates_existing_db_without_summary_columns(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    # Simulate a DB created before the summary columns existed.
    monkeypatch.setattr(db_mod, "DB_PATH", path)
    first = db_mod.get_connection()
    first.execute("INSERT INTO sessions (id, provider_id, title) VALUES ('s1', 'p', 't')")
    first.commit()
    first.close()
    raw = sqlite3.connect(path)
    raw.execute("ALTER TABLE sessions DROP COLUMN summary")
    raw.execute("ALTER TABLE sessions DROP COLUMN summary_message_count")
    raw.commit()
    cols = {r[1] for r in raw.execute("PRAGMA table_info(sessions)")}
    assert "summary" not in cols
    raw.close()

    monkeypatch.setattr(db_mod, "DB_PATH", path)
    c = db_mod.get_connection()
    cols = {r["name"] for r in c.execute("PRAGMA table_info(sessions)").fetchall()}
    assert {"summary", "summary_message_count"} <= cols
    assert row(c, "s1")["title"] == "t"          # existing data intact
    c.close()
    db_mod.get_connection().close()               # second open is a no-op, not an error


# --- ingest ----------------------------------------------------------------

def test_ingest_stores_provider_summary(conn):
    add_session(conn, "s1", summary="Fixed the login bug.")
    r = row(conn, "s1")
    assert r["summary"] == "Fixed the login bug."
    assert r["summary_message_count"] is None


def test_resync_without_summary_keeps_generated_summary(conn):
    add_session(conn, "s1")
    sm.store_summary(conn, "s1", "Generated.", 6)
    add_session(conn, "s1", n_msgs=8)             # provider re-syncs, no summary
    r = row(conn, "s1")
    assert r["summary"] == "Generated."
    assert r["summary_message_count"] == 6
    assert r["message_count"] == 8


def test_provider_summary_overrides_generated(conn):
    add_session(conn, "s1")
    sm.store_summary(conn, "s1", "Generated.", 6)
    add_session(conn, "s1", summary="From provider.")
    r = row(conn, "s1")
    assert r["summary"] == "From provider."
    assert r["summary_message_count"] is None


def test_ingest_strips_nul_from_summary(conn):
    # Same handling as title: NUL bytes are stripped (local DB is otherwise unredacted).
    add_session(conn, "s1", summary="Fixed\x00 it")
    assert row(conn, "s1")["summary"] == "Fixed it"


# --- Claude Code parser ----------------------------------------------------

def test_parser_reads_claude_code_summary_record(tmp_path):
    recs = [
        {"type": "summary", "summary": "Older summary", "leafUuid": "x"},
        {"type": "user", "uuid": "u1", "parentUuid": None, "timestamp": "2026-11-01T10:00:00Z",
         "sessionId": SID, "cwd": "/x", "message": {"role": "user", "content": "Fix the bug"}},
        {"type": "assistant", "uuid": "a1", "parentUuid": "u1", "timestamp": "2026-11-01T10:00:05Z",
         "sessionId": SID, "cwd": "/x",
         "message": {"role": "assistant", "model": "claude-sonnet-4-5",
                     "content": [{"type": "text", "text": "Done."}],
                     "usage": {"input_tokens": 1, "output_tokens": 1}}},
        {"type": "summary", "summary": "  Latest summary  ", "leafUuid": "a1"},
    ]
    f = tmp_path / f"{SID}.jsonl"
    f.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    parsed = parse_session_file(f)
    assert parsed.summary == "Latest summary"


def test_parser_without_summary_record(tmp_path):
    recs = [
        {"type": "user", "uuid": "u1", "parentUuid": None, "timestamp": "2026-11-01T10:00:00Z",
         "sessionId": SID, "cwd": "/x", "message": {"role": "user", "content": "Hi"}},
    ]
    f = tmp_path / f"{SID}.jsonl"
    f.write_text(json.dumps(recs[0]) + "\n")
    assert parse_session_file(f).summary is None


# --- selection -------------------------------------------------------------

def test_select_sessions_rules(conn):
    add_session(conn, "none", n_msgs=6)
    add_session(conn, "short", n_msgs=2)
    add_session(conn, "provider", n_msgs=6, summary="Provider wrote this.")
    add_session(conn, "fresh", n_msgs=6)
    sm.store_summary(conn, "fresh", "ok", 6)
    add_session(conn, "stale", n_msgs=6)
    sm.store_summary(conn, "stale", "old", 4)     # session now has 6 > 4

    ids = {s["id"] for s in sm.select_sessions(conn)}
    assert ids == {"none", "stale"}

    forced = {s["id"] for s in sm.select_sessions(conn, force=True)}
    assert forced == {"none", "provider", "fresh", "stale"}   # "short" still below min


def test_select_sessions_filters(conn):
    add_session(conn, "a", project="alpha", days_ago=0)
    add_session(conn, "b", project="beta", days_ago=0)
    add_session(conn, "old", project="alpha", days_ago=30)
    assert {s["id"] for s in sm.select_sessions(conn, project="alpha")} == {"a", "old"}
    assert {s["id"] for s in sm.select_sessions(conn, days=7)} == {"a", "b"}
    assert [s["id"] for s in sm.select_sessions(conn, limit=1)] == ["a"] or \
           [s["id"] for s in sm.select_sessions(conn, limit=1)] == ["b"]


def test_select_explicit_ids_ignore_min_messages(conn):
    add_session(conn, "short", n_msgs=2)
    assert [s["id"] for s in sm.select_sessions(conn, session_ids=["short"])] == ["short"]


# --- transcript ------------------------------------------------------------

def test_transcript_is_redacted(conn):
    add_session(conn, "s1", n_msgs=2)
    conn.execute("UPDATE messages SET content = ? WHERE id = ?",
                 ("my key is AKIAIOSFODNN7EXAMPLE ok", "s1-0"))
    conn.commit()
    t = sm.build_transcript(conn, "s1")
    assert "AKIAIOSFODNN7EXAMPLE" not in t
    assert "[user]" in t


def test_transcript_truncates_middle(conn):
    add_session(conn, "s1", n_msgs=200)
    t = sm.build_transcript(conn, "s1", max_chars=1000)
    assert len(t) < 1300
    assert "message number 0" in t              # the ask is kept
    assert "message number 199" in t            # the outcome is kept
    assert "messages omitted" in t


def test_transcript_empty_when_no_text(conn):
    add_session(conn, "s1", n_msgs=0)
    assert sm.build_transcript(conn, "s1") == ""


# --- model call ------------------------------------------------------------

def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_generate_summary_request_and_parse():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": '  "Fixed login."\n'}}]})

    out = sm.generate_summary("[user] hi", model="m1", base_url="http://localhost:11434/v1/",
                              api_key="k", client=_client(handler))
    assert out == "Fixed login."
    assert seen["url"] == "http://localhost:11434/v1/chat/completions"
    assert seen["auth"] == "Bearer k"
    assert seen["body"]["model"] == "m1"
    assert "[user] hi" in seen["body"]["messages"][1]["content"]


def test_generate_summary_errors():
    bad = _client(lambda r: httpx.Response(200, json={"nope": 1}))
    with pytest.raises(ValueError):
        sm.generate_summary("t", client=bad)
    empty = _client(lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "  "}}]}))
    with pytest.raises(ValueError):
        sm.generate_summary("t", client=empty)
    boom = _client(lambda r: httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        sm.generate_summary("t", client=boom)


def test_clean_summary_strips_reasoning_and_caps_length():
    assert sm.clean_summary("<think>hmm</think>\nDid the thing.") == "Did the thing."
    long = ("Sentence one is here. " * 100).strip()
    out = sm.clean_summary(long)
    assert len(out) <= sm.MAX_SUMMARY_CHARS + 3
    assert out.endswith(".")


def test_is_local_url():
    assert sm.is_local_url("http://localhost:11434/v1")
    assert sm.is_local_url("http://127.0.0.1:8000/v1")
    assert not sm.is_local_url("https://api.openai.com/v1")


# --- CLI -------------------------------------------------------------------

def test_cli_dry_run_calls_no_model(conn, monkeypatch):
    add_session(conn, "ses_dry000001", n_msgs=6)

    def explode(*a, **k):
        raise AssertionError("model must not be called on --dry-run")
    monkeypatch.setattr(sm, "generate_summary", explode)

    res = CliRunner().invoke(cli, ["summarize", "--dry-run"])
    assert res.exit_code == 0, res.output
    assert "ses_dry000001" in res.output
    assert row(conn, "ses_dry000001")["summary"] is None


def test_cli_summarize_stores_and_marks_count(conn, monkeypatch):
    add_session(conn, "ses_aaaa000001", n_msgs=6)
    monkeypatch.setattr(sm, "generate_summary", lambda transcript, **k: "Did a thing.")
    res = CliRunner().invoke(cli, ["summarize"])
    assert res.exit_code == 0, res.output
    r = row(conn, "ses_aaaa000001")
    assert r["summary"] == "Did a thing."
    assert r["summary_message_count"] == 6

    # Second run: nothing to do.
    res = CliRunner().invoke(cli, ["summarize"])
    assert "Nothing to summarize" in res.output


def test_cli_remote_endpoint_requires_confirmation(conn, monkeypatch):
    add_session(conn, "ses_bbbb000001", n_msgs=6)
    monkeypatch.setattr(sm, "generate_summary", lambda transcript, **k: "x")
    res = CliRunner().invoke(cli, ["summarize", "--base-url", "https://api.example.com/v1"], input="n\n")
    assert res.exit_code != 0
    assert row(conn, "ses_bbbb000001")["summary"] is None

    res = CliRunner().invoke(cli, ["summarize", "--base-url", "https://api.example.com/v1", "--yes"])
    assert res.exit_code == 0, res.output
    assert row(conn, "ses_bbbb000001")["summary"] == "x"


def test_cli_stops_when_server_unreachable(conn, monkeypatch):
    for i in range(3):
        add_session(conn, f"ses_cccc00000{i}", n_msgs=6)
    calls = []

    def refuse(transcript, **k):
        calls.append(1)
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(sm, "generate_summary", refuse)

    res = CliRunner().invoke(cli, ["summarize"])
    assert res.exit_code != 0
    assert len(calls) == 1                      # bailed after the first connection error
    assert "Stopping" in res.output


def test_cli_named_session_with_provider_summary_needs_force(conn, monkeypatch):
    add_session(conn, "ses_dddd000001", n_msgs=6, summary="Provider text.")
    monkeypatch.setattr(sm, "generate_summary", lambda transcript, **k: "Generated.")

    res = CliRunner().invoke(cli, ["summarize", "ses_dddd000001"])
    assert "Nothing to summarize" in res.output
    assert row(conn, "ses_dddd000001")["summary"] == "Provider text."

    res = CliRunner().invoke(cli, ["summarize", "ses_dddd000001", "--force"])
    assert res.exit_code == 0, res.output
    assert row(conn, "ses_dddd000001")["summary"] == "Generated."


def test_session_show_and_list_display_summary(conn):
    add_session(conn, "ses_eeee000001", n_msgs=6, summary="Reworked [auth] flow.")
    res = CliRunner().invoke(cli, ["session", "show", "ses_eeee000001"])
    assert res.exit_code == 0, res.output
    assert "Reworked [auth] flow." in res.output     # brackets not eaten as markup

    res = CliRunner().invoke(cli, ["session", "list", "--summaries"], env={"COLUMNS": "200"})
    assert "Reworked" in res.output
    res = CliRunner().invoke(cli, ["session", "list"], env={"COLUMNS": "200"})
    assert "Reworked" not in res.output


# --- anthropic / bedrock backends -------------------------------------------

class _FakeAnthropic:
    def __init__(self):
        self.calls = []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        block = type("B", (), {"type": "text", "text": "Anthropic summary."})()
        return type("R", (), {"content": [block]})()


class _FakeBedrock:
    def __init__(self):
        self.calls = []

    def converse(self, **kw):
        self.calls.append(kw)
        return {"output": {"message": {"content": [{"text": "Bedrock summary."}]}}}


def test_anthropic_backend(monkeypatch):
    fake = _FakeAnthropic()
    monkeypatch.setattr(sm, "_anthropic_client", lambda api_key, timeout: fake)
    out = sm.generate_summary("[user] hi", backend="anthropic")
    assert out == "Anthropic summary."
    call = fake.calls[0]
    assert call["model"] == sm.DEFAULT_ANTHROPIC_MODEL
    assert call["system"] == sm.SYSTEM_PROMPT
    assert "[user] hi" in call["messages"][0]["content"]


def test_bedrock_backend_uses_converse(monkeypatch):
    fake = _FakeBedrock()
    monkeypatch.setattr(sm, "_bedrock_client", lambda region: fake)
    out = sm.generate_summary("[user] hi", backend="bedrock", model="us.anthropic.x-v1:0")
    assert out == "Bedrock summary."
    call = fake.calls[0]
    assert call["modelId"] == "us.anthropic.x-v1:0"
    assert call["system"] == [{"text": sm.SYSTEM_PROMPT}]
    assert call["inferenceConfig"]["maxTokens"] == sm.MAX_OUTPUT_TOKENS


def test_bedrock_requires_model(monkeypatch):
    monkeypatch.delenv("SPOOLING_SUMMARY_MODEL", raising=False)
    with pytest.raises(ValueError, match="bedrock needs a model id"):
        sm.resolve_model("bedrock", None)
    monkeypatch.setenv("SPOOLING_SUMMARY_MODEL", "from-env")
    assert sm.resolve_model("bedrock", None) == "from-env"


def test_unknown_backend():
    with pytest.raises(ValueError, match="unknown backend"):
        sm.generate_summary("t", backend="nope")


def test_hosted_backends_are_not_local():
    assert sm.is_local_backend("openai", "http://localhost:11434/v1")
    assert not sm.is_local_backend("openai", "https://api.openai.com/v1")
    assert not sm.is_local_backend("anthropic", "http://localhost:11434/v1")
    assert not sm.is_local_backend("bedrock", "http://localhost:11434/v1")


def test_is_fatal_error():
    class ClientError(Exception):
        def __init__(self, code):
            self.response = {"Error": {"Code": code}}

    class NoCredentialsError(Exception):
        pass

    assert sm.is_fatal_error(httpx.ConnectError("x"))
    assert sm.is_fatal_error(ClientError("AccessDeniedException"))
    assert sm.is_fatal_error(NoCredentialsError())
    assert not sm.is_fatal_error(ClientError("ThrottlingException"))
    assert not sm.is_fatal_error(ValueError("empty summary"))
    req = httpx.Request("POST", "http://x")
    assert sm.is_fatal_error(httpx.HTTPStatusError("x", request=req, response=httpx.Response(401, request=req)))
    assert not sm.is_fatal_error(httpx.HTTPStatusError("x", request=req, response=httpx.Response(500, request=req)))


def test_cli_bedrock_asks_confirmation_and_passes_args(conn, monkeypatch):
    add_session(conn, "ses_ffff000001", n_msgs=6)
    seen = {}

    def fake(transcript, **k):
        seen.update(k)
        return "ok"
    monkeypatch.setattr(sm, "generate_summary", fake)

    res = CliRunner().invoke(cli, ["summarize", "--backend", "bedrock", "--model", "m", "--region", "us-east-2"], input="n\n")
    assert res.exit_code != 0 and "is not local" in res.output
    assert row(conn, "ses_ffff000001")["summary"] is None

    res = CliRunner().invoke(cli, ["summarize", "--backend", "bedrock", "--model", "m", "--region", "us-east-2", "-y"])
    assert res.exit_code == 0, res.output
    assert seen["backend"] == "bedrock" and seen["region"] == "us-east-2" and seen["model"] == "m"


def test_cli_bedrock_without_model_is_a_clear_error(conn, monkeypatch):
    monkeypatch.delenv("SPOOLING_SUMMARY_MODEL", raising=False)
    res = CliRunner().invoke(cli, ["summarize", "--backend", "bedrock"])
    assert res.exit_code != 0
    assert "bedrock needs a model id" in res.output


def test_cli_stops_on_fatal_error_for_any_backend(conn, monkeypatch):
    for i in range(3):
        add_session(conn, f"ses_gggg00000{i}", n_msgs=6)
    calls = []

    class NoCredentialsError(Exception):
        pass

    def boom(transcript, **k):
        calls.append(1)
        raise NoCredentialsError("Unable to locate credentials")
    monkeypatch.setattr(sm, "generate_summary", boom)
    res = CliRunner().invoke(cli, ["summarize", "--backend", "anthropic", "-y"])
    assert res.exit_code != 0
    assert len(calls) == 1


def test_bedrock_client_honors_aws_region_env(monkeypatch):
    import sys
    import types

    seen = {}
    fake_boto3 = types.SimpleNamespace(client=lambda name, **kw: seen.update(name=name, **kw) or object())
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    sm._bedrock_client.cache_clear()

    monkeypatch.setenv("AWS_REGION", "us-east-2")
    sm._bedrock_client(None)
    assert seen == {"name": "bedrock-runtime", "region_name": "us-east-2"}

    seen.clear()
    sm._bedrock_client("eu-west-1")             # explicit region wins
    assert seen["region_name"] == "eu-west-1"
    sm._bedrock_client.cache_clear()
