"""Tests for `spooling tools`, `spooling agents` and `spooling traces`
(spooling.insights + the CLI commands)."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from click.testing import CliRunner

from spooling import db as db_mod
from spooling import insights
from spooling.cli import cli

NOW = datetime.now(timezone.utc)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "spooling.db")
    c = db_mod.get_connection()
    yield c
    c.close()


def add_trace(conn, tid, sid, *, title, days_ago=0, project="proj", provider="claude-code"):
    started = (NOW - timedelta(days=days_ago)).isoformat()
    conn.execute(
        "INSERT INTO sessions (id, provider_id, project, title, started_at) VALUES (?,?,?,?,?)",
        (sid, provider, project, title, started),
    )
    conn.execute(
        "INSERT INTO traces (id, session_id, provider_id, project, title, started_at) VALUES (?,?,?,?,?,?)",
        (tid, sid, provider, project, title, started),
    )


def add_span(conn, sid, tid, kind, *, parent=None, name=None, tool=None, agent=None,
             err=0, ms=100, tokens=0, cost=0.0):
    conn.execute(
        """INSERT INTO spans (id, trace_id, parent_id, kind, name, tool_name, agent_type,
                              tool_is_error, duration_ms, input_tokens, output_tokens, cost_usd)
           VALUES (?,?,?,?,?,?,?,?,?,?,0,?)""",
        (sid, tid, parent, kind, name or tool or agent or kind, tool, agent, err, ms, tokens, cost),
    )


@pytest.fixture
def seeded(conn):
    # trace A: root -> explore agent (Read x2, Grep) ; root -> Bash (top level, one error)
    add_trace(conn, "tA", "ses_aaaa1111", title="Fix login bug", days_ago=1)
    add_span(conn, "A-root", "tA", "session")
    add_span(conn, "A-ex", "tA", "agent", parent="A-root", name="agent:explore", agent="explore", tokens=100, cost=0.5)
    add_span(conn, "A-r1", "tA", "tool", parent="A-ex", tool="Read", ms=100)
    add_span(conn, "A-r2", "tA", "tool", parent="A-ex", tool="Read", ms=300)
    add_span(conn, "A-g1", "tA", "tool", parent="A-ex", tool="Grep")
    add_span(conn, "A-b1", "tA", "tool", parent="A-root", tool="Bash", err=1)
    # trace B (older): root -> build agent -> Bash ; agent has no agent_type, falls back to name
    add_trace(conn, "tB", "ses_bbbb2222", title="Refactor billing", days_ago=40, project="other", provider="opencode")
    add_span(conn, "B-root", "tB", "session")
    add_span(conn, "B-bd", "tB", "agent", parent="B-root", name="build", agent=None, tokens=50, cost=0.25)
    add_span(conn, "B-b1", "tB", "tool", parent="B-bd", tool="Bash")
    # trace C: no agents at all
    add_trace(conn, "tC", "ses_cccc3333", title="Quick question", days_ago=2)
    add_span(conn, "C-root", "tC", "session")
    add_span(conn, "C-r1", "tC", "tool", parent="C-root", tool="Read")
    conn.commit()


def by_name(rows):
    return {r["name"]: r for r in rows}


class TestToolStats:
    def test_counts_errors_and_traces(self, seeded):
        t = by_name(insights.tool_stats())
        assert t["Read"]["uses"] == 3 and t["Read"]["traces"] == 2
        assert t["Bash"]["uses"] == 2 and t["Bash"]["errors"] == 1
        assert t["Read"]["avg_ms"] == pytest.approx(166.67, abs=0.1)

    def test_sorted_by_uses_and_limited(self, seeded):
        rows = insights.tool_stats(limit=1)
        assert [r["name"] for r in rows] == ["Read"]

    def test_agent_filter_is_subtree_only(self, seeded):
        t = by_name(insights.tool_stats(agents=["explore"]))
        assert set(t) == {"Read", "Grep"}          # top-level Bash and trace C's Read excluded
        assert t["Read"]["uses"] == 2

    def test_agent_filter_falls_back_to_span_name(self, seeded):
        assert set(by_name(insights.tool_stats(agents=["build"]))) == {"Bash"}

    def test_days_project_provider_filters(self, seeded):
        assert by_name(insights.tool_stats(days=7))["Bash"]["uses"] == 1      # trace B is 40 days old
        other = by_name(insights.tool_stats(project="other"))
        assert set(other) == {"Bash"} and other["Bash"]["uses"] == 1
        assert by_name(insights.tool_stats(provider="opencode"))["Bash"]["uses"] == 1


class TestAgentStats:
    def test_runs_tokens_cost(self, seeded):
        a = by_name(insights.agent_stats())
        assert set(a) == {"explore", "build"}
        assert a["explore"]["runs"] == 1 and a["explore"]["tokens"] == 100
        assert a["explore"]["cost"] == pytest.approx(0.5)

    def test_tool_filter_is_subtree_only(self, seeded):
        assert set(by_name(insights.agent_stats(tools=["Grep"]))) == {"explore"}
        assert set(by_name(insights.agent_stats(tools=["Bash"]))) == {"build"}  # explore's Bash is not under it

    def test_days_filter(self, seeded):
        assert set(by_name(insights.agent_stats(days=7))) == {"explore"}

    def test_none_when_no_agents(self, conn):
        add_trace(conn, "t1", "s1", title="x")
        add_span(conn, "r", "t1", "session")
        conn.commit()
        assert insights.agent_stats() == []


class TestListTraces:
    def test_newest_first_with_agents(self, seeded):
        rows = insights.list_traces()
        assert [r["id"] for r in rows] == ["tA", "tC", "tB"]
        assert by_name([{"name": r["id"], **r} for r in rows])["tA"]["agents"] == ["explore"]
        assert rows[1]["agents"] == []

    def test_session_id_search_full_prefix_and_substring(self, seeded):
        assert [r["id"] for r in insights.list_traces(query="ses_bbbb2222")] == ["tB"]
        assert [r["id"] for r in insights.list_traces(query="bbbb")] == ["tB"]
        assert [r["id"] for r in insights.list_traces(session="ses_aaaa")] == ["tA"]

    def test_query_matches_title_and_agent_name(self, seeded):
        assert [r["id"] for r in insights.list_traces(query="billing")] == ["tB"]
        assert [r["id"] for r in insights.list_traces(query="explore")] == ["tA"]

    def test_agent_and_tool_filters_are_trace_level(self, seeded):
        assert [r["id"] for r in insights.list_traces(agents=["build"])] == ["tB"]
        assert [r["id"] for r in insights.list_traces(agents=["build", "explore"])] == ["tA", "tB"]
        assert [r["id"] for r in insights.list_traces(tools=["Grep"])] == ["tA"]
        assert [r["id"] for r in insights.list_traces(agents=["explore"], tools=["Bash"])] == ["tA"]
        assert insights.list_traces(agents=["explore"], tools=["Nope"]) == []

    def test_other_filters_and_limit(self, seeded):
        assert [r["id"] for r in insights.list_traces(provider="opencode")] == ["tB"]
        assert [r["id"] for r in insights.list_traces(days=7)] == ["tA", "tC"]
        assert len(insights.list_traces(limit=1)) == 1


class TestCli:
    def run(self, *args):
        return CliRunner().invoke(cli, list(args))

    def test_tools_table_and_json(self, seeded):
        r = self.run("tools")
        assert r.exit_code == 0 and "Read" in r.output and "Bash" in r.output
        data = json.loads(self.run("tools", "--agent", "explore", "--json").output)
        assert {d["name"] for d in data} == {"Read", "Grep"}

    def test_agents_table_and_json(self, seeded):
        assert "explore" in self.run("agents").output
        data = json.loads(self.run("agents", "--tool", "Grep", "--json").output)
        assert [d["name"] for d in data] == ["explore"]

    def test_agents_empty_explains_why(self, conn):
        r = self.run("agents")
        assert r.exit_code == 0 and "agent spans found locally" in r.output

    def test_traces_filters(self, seeded):
        data = json.loads(self.run("traces", "--agent", "build", "--json").output)
        assert [d["id"] for d in data] == ["tB"]
        data = json.loads(self.run("traces", "ses_cccc", "--json").output)
        assert [d["id"] for d in data] == ["tC"]
        data = json.loads(self.run("traces", "--tool", "Grep", "--days", "7", "--json").output)
        assert [d["id"] for d in data] == ["tA"]

    def test_cloud_rejects_local_only_filters(self, seeded):
        r = self.run("tools", "--cloud", "--days", "7")
        assert r.exit_code == 2 and "can't be combined with --cloud" in r.output
        r = self.run("agents", "--cloud", "--tool", "Bash")
        assert r.exit_code == 2

    def test_cloud_mode_renders_server_data(self, seeded, monkeypatch):
        from spooling import cloud
        monkeypatch.setattr(cloud, "cloud_tools", lambda limit=20: [
            {"tool_name": "Bash", "uses": 12, "sessions": 3, "errors": 1}])
        monkeypatch.setattr(cloud, "cloud_agents", lambda: [{"agent_type": "explore", "uses": 7}])
        assert "Bash" in self.run("tools", "--cloud").output
        assert json.loads(self.run("agents", "--cloud", "--json").output) == [{"agent_type": "explore", "uses": 7}]


class TestCloudErrors:
    def run(self, *args):
        return CliRunner().invoke(cli, list(args))

    def test_unreachable_server_exits_nonzero_with_reason(self, conn, monkeypatch):
        import httpx
        from spooling import cloud
        monkeypatch.setattr(cloud, "_auth_headers", lambda: {"Authorization": "Bearer sk_x"})

        def boom(*a, **k):
            raise httpx.ConnectError("connection refused")
        monkeypatch.setattr(cloud.httpx, "get", boom)
        for cmd in (["tools", "--cloud"], ["agents", "--cloud"]):
            r = self.run(*cmd)
            assert r.exit_code == 1
            assert "log in" not in r.output.lower()   # a network error must not read as "log in"

    def test_rejected_key_explained(self, conn, monkeypatch):
        import httpx
        from spooling import cloud
        monkeypatch.setattr(cloud, "_auth_headers", lambda: {"Authorization": "Bearer sk_x"})
        req = httpx.Request("GET", "https://api.example/v1/tools")

        def reject(*a, **k):
            return httpx.Response(401, text="bad key", request=req)
        monkeypatch.setattr(cloud.httpx, "get", reject)
        r = self.run("tools", "--cloud")
        assert r.exit_code == 1 and "401" in r.output


# ---------------------------------------------------------------------------
# MCP servers, --search, case-insensitive names
# ---------------------------------------------------------------------------

@pytest.fixture
def seeded_mcp(seeded, conn):
    """seeded + MCP calls: spool (root + under explore), github (under build), a Drive server with underscores."""
    add_span(conn, "A-m1", "tA", "tool", parent="A-root", tool="mcp__spool__search_sessions", ms=200)
    add_span(conn, "A-m2", "tA", "tool", parent="A-ex", tool="mcp__spool__get_session", err=1, ms=400)
    add_span(conn, "A-m3", "tA", "tool", parent="A-ex", tool="mcp__spool__search_sessions", ms=300)
    add_span(conn, "B-m1", "tB", "tool", parent="B-bd", tool="mcp__github__create_issue", ms=500)
    add_span(conn, "C-m1", "tC", "tool", parent="C-root", tool="mcp__claude_ai_Google_Drive__search_files")
    conn.commit()


class TestMcpHelpers:
    def test_server_of(self):
        assert insights.mcp_server_of("mcp__spool__search_sessions") == "spool"
        assert insights.mcp_server_of("mcp__claude_ai_Google_Drive__search") == "claude_ai_Google_Drive"
        assert insights.mcp_server_of("MCP__Spool__x") == "Spool"
        assert insights.mcp_server_of("Bash") is None
        assert insights.mcp_server_of(None) is None

    def test_group_mcp_tools(self):
        rows = [{"tool_name": "mcp__spool__a", "uses": 5, "errors": 1},
                {"tool_name": "mcp__spool__b", "uses": 2, "errors": 0},
                {"tool_name": "mcp__gh__c", "uses": 9, "errors": 0},
                {"tool_name": "Bash", "uses": 100, "errors": 3}]
        g = insights.group_mcp_tools(rows)
        assert [(x["name"], x["tools"], x["calls"], x["errors"]) for x in g] == [
            ("gh", 1, 9, 0), ("spool", 2, 7, 1)]


class TestMcpStats:
    def test_groups_by_server(self, seeded_mcp):
        m = by_name(insights.mcp_stats())
        assert set(m) == {"spool", "github", "claude_ai_Google_Drive"}
        assert m["spool"]["tools"] == 2 and m["spool"]["calls"] == 3
        assert m["spool"]["traces"] == 1 and m["spool"]["errors"] == 1
        assert m["spool"]["avg_ms"] == pytest.approx(300)
        assert [r["name"] for r in insights.mcp_stats()][0] == "spool"      # most calls first

    def test_empty_without_mcp_calls(self, seeded):
        assert insights.mcp_stats() == []

    def test_search_matches_server_or_tool(self, seeded_mcp):
        assert set(by_name(insights.mcp_stats(search="GITHUB"))) == {"github"}
        assert set(by_name(insights.mcp_stats(search="search_"))) == {"spool", "claude_ai_Google_Drive"}

    def test_filters(self, seeded_mcp):
        assert set(by_name(insights.mcp_stats(days=7))) == {"spool", "claude_ai_Google_Drive"}  # github is on old trace B
        assert set(by_name(insights.mcp_stats(provider="opencode"))) == {"github"}


class TestToolFiltersMcpAndSearch:
    def test_mcp_filter_is_server_substring_case_insensitive(self, seeded_mcp):
        assert set(by_name(insights.tool_stats(mcps=["SPOOL"]))) == {
            "mcp__spool__search_sessions", "mcp__spool__get_session"}
        assert set(by_name(insights.tool_stats(mcps=["spool", "github"]))) == {
            "mcp__spool__search_sessions", "mcp__spool__get_session", "mcp__github__create_issue"}

    def test_underscore_and_percent_in_filter_are_literal(self, seeded_mcp):
        assert set(by_name(insights.tool_stats(mcps=["ai_Google"]))) == {"mcp__claude_ai_Google_Drive__search_files"}
        assert insights.tool_stats(mcps=["claude%"]) == []        # % is not a wildcard
        assert insights.tool_stats(mcps=["ai_Go_gle"]) == []      # _ is not a wildcard

    def test_rows_report_their_mcp_server(self, seeded_mcp):
        t = by_name(insights.tool_stats())
        assert t["mcp__github__create_issue"]["mcp_server"] == "github"
        assert t["Bash"]["mcp_server"] is None

    def test_search_by_name(self, seeded_mcp):
        assert set(by_name(insights.tool_stats(search="READ"))) == {"Read"}
        assert set(by_name(insights.tool_stats(search="session"))) == {
            "mcp__spool__search_sessions", "mcp__spool__get_session"}
        assert insights.tool_stats(search="r_ad") == []           # _ is literal

    def test_agent_filter_is_case_insensitive_and_combines_with_mcp(self, seeded_mcp):
        assert set(by_name(insights.tool_stats(agents=["EXPLORE"]))) == {
            "Read", "Grep", "mcp__spool__get_session", "mcp__spool__search_sessions"}
        assert set(by_name(insights.tool_stats(agents=["explore"], mcps=["spool"]))) == {
            "mcp__spool__get_session", "mcp__spool__search_sessions"}
        assert insights.tool_stats(agents=["build"], mcps=["spool"]) == []


class TestAgentFiltersMcpAndSearch:
    def test_mcp_filter_is_subtree_only(self, seeded_mcp):
        assert set(by_name(insights.agent_stats(mcps=["spool"]))) == {"explore"}
        assert set(by_name(insights.agent_stats(mcps=["github"]))) == {"build"}
        assert insights.agent_stats(mcps=["Google_Drive"]) == []      # that call is outside any agent

    def test_tool_filter_case_insensitive(self, seeded_mcp):
        assert set(by_name(insights.agent_stats(tools=["grep"]))) == {"explore"}

    def test_tool_and_mcp_must_both_match(self, seeded_mcp):
        assert set(by_name(insights.agent_stats(tools=["Grep"], mcps=["spool"]))) == {"explore"}
        assert insights.agent_stats(tools=["Grep"], mcps=["github"]) == []

    def test_search(self, seeded_mcp):
        assert set(by_name(insights.agent_stats(search="EXPL"))) == {"explore"}
        assert insights.agent_stats(search="zzz") == []


class TestTracesMcp:
    def test_mcp_filter_and_per_trace_servers(self, seeded_mcp):
        rows = insights.list_traces(mcps=["spool"])
        assert [r["id"] for r in rows] == ["tA"]
        assert rows[0]["mcps"] == ["spool"] and rows[0]["agents"] == ["explore"]
        all_rows = {r["id"]: r for r in insights.list_traces()}
        assert all_rows["tB"]["mcps"] == ["github"]
        assert all_rows["tC"]["mcps"] == ["claude_ai_Google_Drive"]

    def test_free_text_matches_mcp_server_name(self, seeded_mcp):
        assert [r["id"] for r in insights.list_traces(query="github")] == ["tB"]
        assert [r["id"] for r in insights.list_traces(query="Google_Drive")] == ["tC"]

    def test_agent_and_tool_names_ignore_case(self, seeded_mcp):
        assert [r["id"] for r in insights.list_traces(agents=["EXPLORE"])] == ["tA"]
        assert [r["id"] for r in insights.list_traces(tools=["bash"])] == ["tA", "tB"]

    def test_all_filter_kinds_must_match(self, seeded_mcp):
        assert [r["id"] for r in insights.list_traces(agents=["build"], mcps=["github"])] == ["tB"]
        assert insights.list_traces(agents=["build"], mcps=["spool"]) == []
        assert [r["id"] for r in insights.list_traces(tools=["Bash"], mcps=["spool"])] == ["tA"]


class TestMcpCli:
    def run(self, *args):
        return CliRunner().invoke(cli, list(args))

    def test_mcps_server_table(self, seeded_mcp):
        r = self.run("mcps")
        assert r.exit_code == 0 and "spool" in r.output and "github" in r.output
        data = json.loads(self.run("mcps", "--json").output)
        assert [d["name"] for d in data][0] == "spool" and data[0]["calls"] == 3

    def test_mcps_server_argument_lists_tools(self, seeded_mcp):
        data = json.loads(self.run("mcps", "spool", "--json").output)
        assert {d["name"] for d in data} == {"mcp__spool__search_sessions", "mcp__spool__get_session"}
        assert "No MCP tools found" in self.run("mcps", "nope").output

    def test_mcps_empty_explains(self, seeded):
        r = self.run("mcps")
        assert r.exit_code == 0 and "No MCP tool calls found" in r.output

    def test_tools_mcp_and_search_options(self, seeded_mcp):
        data = json.loads(self.run("tools", "--mcp", "github", "--json").output)
        assert [d["name"] for d in data] == ["mcp__github__create_issue"]
        data = json.loads(self.run("tools", "-s", "get_sess", "--json").output)
        assert [d["name"] for d in data] == ["mcp__spool__get_session"]

    def test_agents_mcp_and_search_options(self, seeded_mcp):
        assert [d["name"] for d in json.loads(self.run("agents", "--mcp", "github", "--json").output)] == ["build"]
        assert [d["name"] for d in json.loads(self.run("agents", "-s", "EXP", "--json").output)] == ["explore"]

    def test_traces_mcp_option_and_mcp_column(self, seeded_mcp):
        data = json.loads(self.run("traces", "--mcp", "github", "--json").output)
        assert [d["id"] for d in data] == ["tB"] and data[0]["mcps"] == ["github"]
        assert "MCP" in self.run("traces").output

    def test_cloud_tools_search_and_mcp_are_client_side(self, seeded, monkeypatch):
        from spooling import cloud
        rows = [{"tool_name": "Bash", "uses": 9, "sessions": 3, "errors": 0},
                {"tool_name": "mcp__spool__search_sessions", "uses": 7, "sessions": 2, "errors": 1},
                {"tool_name": "mcp__github__create_issue", "uses": 4, "sessions": 1, "errors": 0}]
        monkeypatch.setattr(cloud, "cloud_tools", lambda limit=20: rows)
        assert [d["tool_name"] for d in json.loads(self.run("tools", "--cloud", "--mcp", "git", "--json").output)] == [
            "mcp__github__create_issue"]
        assert [d["tool_name"] for d in json.loads(self.run("tools", "--cloud", "-s", "BASH", "--json").output)] == ["Bash"]

    def test_cloud_mcps_groups_by_server(self, seeded, monkeypatch):
        from spooling import cloud
        monkeypatch.setattr(cloud, "cloud_tools", lambda limit=20: [
            {"tool_name": "Bash", "uses": 99, "sessions": 9, "errors": 0},
            {"tool_name": "mcp__spool__a", "uses": 5, "sessions": 2, "errors": 1},
            {"tool_name": "mcp__spool__b", "uses": 2, "sessions": 1, "errors": 0},
            {"tool_name": "mcp__gh__c", "uses": 9, "sessions": 1, "errors": 0}])
        data = json.loads(self.run("mcps", "--cloud", "--json").output)
        assert [(d["name"], d["tools"], d["calls"]) for d in data] == [("gh", 1, 9), ("spool", 2, 7)]
        data = json.loads(self.run("mcps", "spool", "--cloud", "--json").output)
        assert [d["tool_name"] for d in data] == ["mcp__spool__a", "mcp__spool__b"]

    def test_cloud_agents_search(self, seeded, monkeypatch):
        from spooling import cloud
        monkeypatch.setattr(cloud, "cloud_agents", lambda: [
            {"agent_type": "explore", "uses": 7}, {"agent_type": "build", "uses": 3}])
        assert [d["agent_type"] for d in json.loads(self.run("agents", "--cloud", "-s", "BUI", "--json").output)] == ["build"]
