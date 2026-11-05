"""Tests for the Claude Code provider (~/.claude/projects transcripts)."""

import json
import os
import time
from datetime import datetime, timezone

import pytest

from spooling import db as db_mod
from spooling import ingest
from spooling.parser import parse_session_file
from spooling.providers import PROVIDER_REGISTRY, get_provider
from spooling.providers import base as base_mod
from spooling.providers.claude_code import ClaudeCodeProvider

SID1 = "11111111-aaaa-4bbb-8ccc-000000000001"
SID2 = "22222222-aaaa-4bbb-8ccc-000000000002"
CWD = "/Users/dev/code/my-app"


def _rec(type_, uuid, parent, ts, **kw):
    r = {
        "type": type_, "uuid": uuid, "parentUuid": parent, "isSidechain": False,
        "timestamp": ts, "cwd": CWD, "gitBranch": "main", "version": "2.1.0",
        "sessionId": SID1,
    }
    r.update(kw)
    return r


def _usage(i, o, cr=0, cw=0):
    return {"input_tokens": i, "output_tokens": o,
            "cache_read_input_tokens": cr, "cache_creation_input_tokens": cw}


def transcript(sid: str = SID1) -> list[dict]:
    """A realistic session: placeholder msg first, then prompt, tool use, answer."""
    recs = [
        {"type": "permission-mode", "permissionMode": "default", "sessionId": SID1},
        {"type": "file-history-snapshot", "messageId": "x", "snapshot": {}},
        # Locally generated placeholder: model "<synthetic>" must not become the session model.
        _rec("assistant", "a0", None, "2026-11-01T10:00:00Z",
             message={"role": "assistant", "model": "<synthetic>",
                      "content": [{"type": "text", "text": "No response requested."}],
                      "usage": _usage(0, 0)}),
        _rec("user", "u1", "a0", "2026-11-01T10:00:05Z",
             message={"role": "user", "content": "Fix the login bug in auth.ts"}),
        _rec("assistant", "a1", "u1", "2026-11-01T10:00:10Z",
             message={"role": "assistant", "model": "claude-sonnet-4-5",
                      "content": [
                          {"type": "text", "text": "Let me look at the file."},
                          {"type": "tool_use", "id": "toolu_1", "name": "Read",
                           "input": {"file_path": "/Users/dev/code/my-app/auth.ts"}}],
                      "usage": _usage(100, 50, cr=2000, cw=500)}),
        _rec("user", "u2", "a1", "2026-11-01T10:00:12Z",
             message={"role": "user", "content": [
                 {"type": "tool_result", "tool_use_id": "toolu_1", "content": "export function login() {}"}]}),
        _rec("assistant", "a2", "u2", "2026-11-01T10:00:20Z",
             message={"role": "assistant", "model": "claude-sonnet-4-5",
                      "content": [{"type": "text", "text": "Fixed."}],
                      "usage": _usage(10, 20, cr=2600, cw=0)}),
        {"type": "ai-title", "title": "Fix login", "sessionId": SID1},
    ]
    # Message uuids are globally unique in real transcripts; keep the fixture honest.
    for r in recs:
        for k in ("uuid", "parentUuid"):
            if r.get(k):
                r[k] = f"{sid[:8]}-{r[k]}"
        if "sessionId" in r:
            r["sessionId"] = sid
    return recs


def write_session(root, project_dir, session_id, records, mtime=None):
    d = root / "projects" / project_dir
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{session_id}.jsonl"
    f.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    if mtime:
        os.utime(f, (mtime, mtime))
    return f


@pytest.fixture
def claude_home(tmp_path, monkeypatch):
    home = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    # No user path overrides leaking in from the dev machine.
    monkeypatch.setattr(base_mod, "PATH_OVERRIDES_FILE", tmp_path / "paths.json")
    return home


def test_registered():
    assert PROVIDER_REGISTRY["claude-code"] is ClaudeCodeProvider
    assert get_provider("claude-code").name == "Claude Code"


def test_default_path_follows_claude_config_dir(claude_home):
    assert ClaudeCodeProvider().default_data_path() == claude_home / "projects"


def test_default_path_is_dot_claude_without_env(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    assert ClaudeCodeProvider().default_data_path() == tmp_path / ".claude" / "projects"


def test_falls_back_to_xdg_location_when_dot_claude_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    (tmp_path / ".config" / "claude" / "projects").mkdir(parents=True)
    assert ClaudeCodeProvider().default_data_path() == tmp_path / ".config" / "claude" / "projects"


def test_paths_json_override_wins(claude_home, tmp_path, monkeypatch):
    custom = tmp_path / "elsewhere"
    (tmp_path / "paths.json").write_text(json.dumps({"claude-code": str(custom)}))
    assert ClaudeCodeProvider().resolved_data_path() == custom


def test_not_available_when_directory_missing(claude_home):
    assert ClaudeCodeProvider().is_available() is False
    assert ClaudeCodeProvider().discover_session_files() == []


def test_discovery_only_top_level_uuid_transcripts(claude_home):
    old = write_session(claude_home, "-Users-dev-code-my-app", SID1, transcript(), mtime=time.time() - 1000)
    new = write_session(claude_home, "-Users-dev-code-other", SID2, transcript(SID2))
    proj = claude_home / "projects" / "-Users-dev-code-my-app"
    # Things that must NOT be treated as sessions:
    (proj / SID1 / "subagents").mkdir(parents=True)
    (proj / SID1 / "subagents" / "agent-abc123.jsonl").write_text("{}\n")
    (proj / "skill-injections.jsonl").write_text("{}\n")          # non-uuid name
    (claude_home / "projects" / "stray.jsonl").write_text("{}\n")  # file directly in projects/

    files = ClaudeCodeProvider().discover_session_files()
    assert files == [new, old]  # newest first, nothing else


def test_parse_stamps_provider_on_session_and_trace(claude_home):
    f = write_session(claude_home, "-Users-dev-code-my-app", SID1, transcript())
    [s] = ClaudeCodeProvider().parse_session_file(f)
    assert s.provider_id == "claude-code"
    assert s.trace is not None and s.trace.provider_id == "claude-code"
    assert s.session_id == SID1


def test_project_is_working_directory_name(claude_home):
    f = write_session(claude_home, "-Users-dev-code-my-app", SID1, transcript())
    [s] = ClaudeCodeProvider().parse_session_file(f)
    assert s.project == "my-app"          # not "-Users-dev-code-my-app"
    assert s.cwd == CWD and s.git_branch == "main" and s.agent_version == "2.1.0"


def test_synthetic_placeholder_is_not_the_session_model(claude_home):
    f = write_session(claude_home, "-Users-dev-code-my-app", SID1, transcript())
    [s] = ClaudeCodeProvider().parse_session_file(f)
    assert s.model == "claude-sonnet-4-5"


def test_messages_tools_and_title(claude_home):
    f = write_session(claude_home, "-Users-dev-code-my-app", SID1, transcript())
    [s] = ClaudeCodeProvider().parse_session_file(f)
    assert [m.role for m in s.messages] == ["assistant", "user", "assistant", "assistant"]
    assert s.tool_call_count == 1
    read = next(m for m in s.messages if m.tool_details).tool_details[0]
    assert read.name == "Read" and read.input_summary.endswith("auth.ts")
    assert read.result_preview == "export function login() {}"  # paired from the tool_result
    assert s.title == "Fix the login bug in auth.ts"
    assert s.started_at == datetime(2026, 11, 1, 10, 0, 0, tzinfo=timezone.utc)


def test_token_usage_including_cache_flows_into_trace(claude_home):
    f = write_session(claude_home, "-Users-dev-code-my-app", SID1, transcript())
    [s] = ClaudeCodeProvider().parse_session_file(f)
    llm = [sp for sp in s.trace.spans if getattr(sp.kind, "value", sp.kind) == "llm_call"]
    assert len(llm) == 3
    assert sum(x.input_tokens for x in llm) == 110
    assert sum(x.output_tokens for x in llm) == 70
    assert sum(x.cache_read_tokens for x in llm) == 4600
    assert sum(x.cache_write_tokens for x in llm) == 500
    # Cost uses all four components at claude-sonnet-4-5 rates (3 / 15 / 3.75 / 0.30 per Mtok).
    real = [x for x in llm if x.model == "claude-sonnet-4-5"]
    expected = (110 * 3 + 70 * 15 + 500 * 3.75 + 4600 * 0.30) / 1e6
    assert sum(x.cost_usd for x in real) == pytest.approx(expected, rel=0.02)


def test_empty_or_garbage_files_yield_no_session(claude_home):
    f = write_session(claude_home, "-p", SID2, [{"type": "permission-mode"}])
    assert ClaudeCodeProvider().parse_session_file(f) == []
    g = claude_home / "projects" / "-p" / f"{SID1}.jsonl"
    g.write_text("not json\n\n{broken\n")
    assert ClaudeCodeProvider().parse_session_file(g) == []


def test_generic_session_file_parser_is_unchanged(claude_home):
    """The legacy jsonl-session path keeps its id and folder-name project."""
    f = write_session(claude_home, "-Users-dev-code-my-app", SID1, transcript())
    s = parse_session_file(f)
    assert s.provider_id == "jsonl-session" and s.trace.provider_id == "jsonl-session"
    assert s.project == "-Users-dev-code-my-app"


def test_sync_ingests_and_is_idempotent(claude_home, tmp_path, monkeypatch):
    monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "spooling.db")
    write_session(claude_home, "-Users-dev-code-my-app", SID1, transcript())
    write_session(claude_home, "-Users-dev-code-other", SID2, transcript(SID2))

    ingest.sync(embed=False, provider_filter="claude-code")

    conn = db_mod.get_connection()
    rows = conn.execute("SELECT id, provider_id, project, model FROM sessions ORDER BY id").fetchall()
    assert [(r["id"], r["provider_id"], r["project"], r["model"]) for r in rows] == [
        (SID1, "claude-code", "my-app", "claude-sonnet-4-5"),
        (SID2, "claude-code", "my-app", "claude-sonnet-4-5"),
    ]
    assert conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"] == 8
    assert [r["provider_id"] for r in conn.execute("SELECT DISTINCT provider_id FROM traces").fetchall()] == ["claude-code"]
    spans = conn.execute("SELECT COUNT(*) AS n FROM spans WHERE kind = 'llm_call'").fetchone()["n"]
    assert spans == 6
    conn.close()

    # Second run: nothing changed on disk, nothing re-ingested.
    ingest.sync(embed=False, provider_filter="claude-code")
    conn = db_mod.get_connection()
    assert conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"] == 2
    assert conn.execute("SELECT COUNT(*) AS n FROM spans WHERE kind = 'llm_call'").fetchone()["n"] == 6
    conn.close()
