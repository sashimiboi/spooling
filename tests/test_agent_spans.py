"""Claude Code subagent calls ("Agent", formerly "Task") become agent spans,
and sessions synced before that fix are re-read once."""

import json

import pytest
from click.testing import CliRunner

from spooling import db as db_mod
from spooling import ingest, insights
from spooling.cli import cli
from spooling.parser import parse_session_file
from spooling.providers import base as base_mod

SID = "33333333-aaaa-4bbb-8ccc-000000000003"
CWD = "/Users/dev/code/my-app"


def _rec(type_, uuid, parent, ts, **kw):
    r = {"type": type_, "uuid": uuid, "parentUuid": parent, "isSidechain": False,
         "timestamp": ts, "cwd": CWD, "gitBranch": "main", "version": "2.1.0", "sessionId": SID}
    r.update(kw)
    return r


def session_with_subagent(tool_name: str, subagent_type: str | None = "Explore"):
    inp = {"description": "Audit the app", "prompt": "Do a thorough audit"}
    if subagent_type:
        inp["subagent_type"] = subagent_type
    return [
        _rec("user", "u1", None, "2026-11-01T10:00:00Z",
             message={"role": "user", "content": "audit the app"}),
        _rec("assistant", "a1", "u1", "2026-11-01T10:00:05Z",
             message={"role": "assistant", "model": "claude-sonnet-4-5", "content": [
                 {"type": "tool_use", "id": "toolu_agent", "name": tool_name, "input": inp},
                 {"type": "tool_use", "id": "toolu_bash", "name": "Bash", "input": {"command": "ls"}}],
                 "usage": {"input_tokens": 10, "output_tokens": 5}}),
        _rec("user", "u2", "a1", "2026-11-01T10:00:30Z",
             message={"role": "user", "content": [
                 {"type": "tool_result", "tool_use_id": "toolu_agent", "content": "audit done"},
                 {"type": "tool_result", "tool_use_id": "toolu_bash", "content": "a b c"}]}),
    ]


def write(root, records):
    d = root / "projects" / "-Users-dev-code-my-app"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{SID}.jsonl"
    f.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return f


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(h))
    monkeypatch.setattr(base_mod, "PATH_OVERRIDES_FILE", tmp_path / "paths.json")
    monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "spooling.db")
    return h


def agent_spans(conn):
    return [dict(r) for r in conn.execute(
        "SELECT name, agent_type, agent_prompt FROM spans WHERE kind = 'agent'").fetchall()]


class TestParser:
    @pytest.mark.parametrize("tool", ["Agent", "Task"])
    def test_subagent_tool_creates_agent_span(self, home, tool):
        f = write(home, session_with_subagent(tool))
        trace = parse_session_file(f, provider_id="claude-code", project_from_cwd=True).trace
        agents = [s for s in trace.spans if s.kind.value == "agent"]
        assert len(agents) == 1
        assert agents[0].agent_type == "Explore"
        assert agents[0].agent_prompt == "Do a thorough audit"
        # the agent call is not also recorded as a plain tool span
        assert not [s for s in trace.spans if s.kind.value == "tool" and s.tool_name in ("Agent", "Task")]
        # other tools are unaffected
        assert [s.tool_name for s in trace.spans if s.kind.value == "tool"] == ["Bash"]

    def test_missing_subagent_type_defaults_to_generic(self, home):
        f = write(home, session_with_subagent("Agent", subagent_type=None))
        trace = parse_session_file(f, provider_id="claude-code", project_from_cwd=True).trace
        assert [s.agent_type for s in trace.spans if s.kind.value == "agent"] == ["generic"]

    def test_agent_span_is_closed_by_its_result(self, home):
        f = write(home, session_with_subagent("Agent"))
        trace = parse_session_file(f, provider_id="claude-code", project_from_cwd=True).trace
        agent = next(s for s in trace.spans if s.kind.value == "agent")
        assert agent.ended_at is not None and agent.duration_ms > 0


class TestReread:
    def _old_sync(self, home):
        """Simulate a database synced by a version that ignored 'Agent' calls."""
        f = write(home, session_with_subagent("Agent"))
        ingest.sync(embed=False, provider_filter="claude-code")
        conn = db_mod.get_connection()
        # What an old install looks like: spans parsed the old way, no revision recorded.
        conn.execute("DELETE FROM spans WHERE kind = 'agent'")
        conn.execute("DELETE FROM sync_state WHERE file_path LIKE '__parser_revision__:%'")
        conn.commit()
        assert agent_spans(conn) == []
        conn.close()
        return f

    def test_fresh_install_records_revision_without_rereading(self, home, capsys):
        write(home, session_with_subagent("Agent"))
        ingest.sync(embed=False, provider_filter="claude-code")
        conn = db_mod.get_connection()
        assert len(agent_spans(conn)) == 1
        rev = conn.execute("SELECT last_size FROM sync_state WHERE file_path = '__parser_revision__:claude-code'").fetchone()
        assert rev["last_size"] == 2
        conn.close()
        assert "improved parsing" not in capsys.readouterr().out

    def test_upgrade_rereads_old_sessions_once(self, home, capsys):
        self._old_sync(home)
        capsys.readouterr()

        ingest.sync(embed=False, provider_filter="claude-code")
        conn = db_mod.get_connection()
        assert [a["agent_type"] for a in agent_spans(conn)] == ["Explore"]
        # no duplicate sessions/traces from the re-read
        assert conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"] == 1
        assert conn.execute("SELECT COUNT(*) AS n FROM traces").fetchone()["n"] == 1
        conn.close()
        assert "improved parsing" in capsys.readouterr().out

        # second sync: revision recorded, nothing is re-read any more
        ingest.sync(embed=False, provider_filter="claude-code")
        assert "improved parsing" not in capsys.readouterr().out

    def test_reparse_flag_forces_reread_of_unchanged_files(self, home):
        write(home, session_with_subagent("Agent"))
        ingest.sync(embed=False, provider_filter="claude-code")
        conn = db_mod.get_connection()
        conn.execute("DELETE FROM spans WHERE kind = 'agent'")
        conn.commit()
        conn.close()

        ingest.sync(embed=False, provider_filter="claude-code")           # unchanged -> skipped
        conn = db_mod.get_connection()
        assert agent_spans(conn) == []
        conn.close()

        ingest.sync(embed=False, provider_filter="claude-code", reparse=True)
        conn = db_mod.get_connection()
        assert len(agent_spans(conn)) == 1
        conn.close()

    def test_other_providers_not_forced(self, home):
        """A revision bump on claude-code must not make other providers re-read."""
        from spooling.providers.opencode import OpencodeProvider
        assert OpencodeProvider.parser_revision == 0
        conn = db_mod.get_connection()
        assert ingest._needs_reread(conn, {}, "opencode", 0, reparse=False) is False
        conn.close()

    def test_end_to_end_agents_command(self, home):
        self._old_sync(home)
        r = CliRunner().invoke(cli, ["agents", "--json"])
        assert json.loads(r.output) == []
        ingest.sync(embed=False, provider_filter="claude-code")
        data = json.loads(CliRunner().invoke(cli, ["agents", "--json"]).output)
        assert [(d["name"], d["runs"]) for d in data] == [("Explore", 1)]

    def test_sync_cli_reparse_flag(self, home, monkeypatch):
        seen = {}
        monkeypatch.setattr(ingest, "sync", lambda **kw: seen.update(kw))
        CliRunner().invoke(cli, ["sync", "--reparse", "-p", "claude-code", "--no-embed"])
        assert seen == {"embed": False, "provider_filter": "claude-code", "reparse": True}
