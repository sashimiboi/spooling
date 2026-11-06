"""`spooling cloud whoami` / `cloud teams` (GET /v1/me), and the workspace line in `cloud status`."""

import json

import httpx
import pytest
from click.testing import CliRunner

from spooling import cloud
from spooling.cli import cli

ME = {
    "user": {"id": "u1", "email": "dev@example.com", "name": "Dev"},
    "workspace": {"id": "w1", "name": "Dev", "kind": "personal", "plan": "free", "role": "owner",
                  "members": 1, "current": True},
    "workspaces": [
        {"id": "w1", "name": "Dev", "kind": "personal", "plan": "free", "role": "owner", "members": 1, "current": True},
        {"id": "w2", "name": "Toebox", "kind": "team", "plan": "team", "role": "admin", "members": 4, "current": False},
        {"id": "w3", "name": "[red]Odd[/red] Name", "kind": "team", "plan": "free", "role": "member", "members": 2, "current": False},
    ],
}
STATS = {"sessions": 12, "messages": 340, "providers": 2, "cost": 1.5}


def run(*args):
    return CliRunner().invoke(cli, ["cloud", *args])


@pytest.fixture
def server(monkeypatch):
    """Fake Spooling Cloud: path -> (status, json). Records the paths requested."""
    routes = {"/v1/me": (200, ME), "/v1/stats": (200, STATS)}
    seen = []
    monkeypatch.setattr(cloud, "_auth_headers", lambda: {"Authorization": "Bearer sk_test"})
    monkeypatch.setattr(cloud, "_api_base", lambda: "https://api.example")
    monkeypatch.setattr(cloud, "_load_config", lambda: {"api_key": "sk_test"})

    def fake_get(url, headers=None, params=None, timeout=None):
        path = url.replace("https://api.example", "")
        seen.append(path)
        status, body = routes[path]
        return httpx.Response(status, json=body, request=httpx.Request("GET", url))

    monkeypatch.setattr(cloud.httpx, "get", fake_get)
    return type("S", (), {"routes": routes, "seen": seen})


def test_whoami_shows_account_workspace_and_role(server):
    r = run("whoami")
    assert r.exit_code == 0
    assert "dev@example.com" in r.output and "Dev" in r.output
    assert "Personal" in r.output and "owner" in r.output and "1 member," in r.output
    assert "spooling push" in r.output


def test_whoami_json(server):
    assert json.loads(run("whoami", "--json").output) == ME


def test_teams_lists_all_workspaces_and_marks_the_current_one(server):
    r = run("teams")
    assert r.exit_code == 0
    for name in ("Toebox", "Personal", "Team", "admin", "member"):
        assert name in r.output
    current_rows = [l for l in r.output.splitlines() if "<- this key" in l]
    assert len(current_rows) == 1 and "Dev" in current_rows[0]


def test_teams_does_not_interpret_markup_in_names(server):
    assert "[red]Odd[/red] Name" in run("teams").output


def test_teams_json_is_the_workspace_list(server):
    data = json.loads(run("teams", "--json").output)
    assert [w["name"] for w in data] == ["Dev", "Toebox", "[red]Odd[/red] Name"]
    assert [w["current"] for w in data] == [True, False, False]


def test_older_server_without_v1_me_explains_and_exits_nonzero(server):
    server.routes["/v1/me"] = (404, {"detail": "Not Found"})
    for cmd in ("whoami", "teams"):
        r = run(cmd)
        assert r.exit_code == 1
        assert "older than the CLI" in r.output and "cloud status" in r.output


def test_rejected_key_is_explained(server):
    server.routes["/v1/me"] = (401, {"detail": "invalid or revoked key"})
    r = run("whoami")
    assert r.exit_code == 1 and "401" in r.output and "cloud login" in r.output


def test_unreachable_server_exits_nonzero(server, monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectError("connection refused")
    monkeypatch.setattr(cloud.httpx, "get", boom)
    r = run("teams")
    assert r.exit_code == 1 and "log in" not in r.output.lower()


def test_status_shows_workspace_name_when_server_supports_it(server):
    r = run("status")
    assert r.exit_code == 0
    assert "workspace" in r.output and "Dev" in r.output and "sessions" in r.output
    assert "/v1/me" in server.seen


def test_status_still_works_against_an_older_server(server):
    server.routes["/v1/me"] = (404, {"detail": "Not Found"})
    r = run("status")
    assert r.exit_code == 0
    assert "sessions" in r.output and "workspace:" not in r.output


def test_status_ignores_a_failing_v1_me(server, monkeypatch):
    server.routes["/v1/me"] = (500, {"detail": "boom"})
    r = run("status")
    assert r.exit_code == 0 and "sessions" in r.output
