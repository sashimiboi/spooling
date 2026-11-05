"""Tests for friendly cloud errors: DNS / refused / TLS / HTTP status handling,
fail-fast preflight, and non-zero exit codes for `spooling push`."""

import json
import socket
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from click.testing import CliRunner

from spooling import cloud
from spooling.cli import cli


# --- helpers -----------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    status = 200
    body = {"sessions": 3, "messages": 9, "providers": 1, "cost": 1.5,
            "accepted": 1, "rejected": 0}
    seen: list[str] = []

    def log_message(self, *a):  # silence
        pass

    def _go(self):
        type(self).seen.append(f"{self.command} {self.path}")
        payload = json.dumps(type(self).body if type(self).status == 200 else {"detail": "nope"}).encode()
        self.send_response(type(self).status)
        if type(self).status == 429:
            self.send_header("Retry-After", "42")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = do_DELETE = _go


@pytest.fixture
def server():
    """Run a throwaway HTTP server; set .status to choose the response code."""
    handler = type("H", (_Handler,), {"status": 200, "seen": []})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    srv.handler = handler
    srv.url = f"http://127.0.0.1:{srv.server_address[1]}"
    yield srv
    srv.shutdown()


@pytest.fixture
def logged_in(tmp_path, monkeypatch):
    """Isolated config so tests never touch the real ~/.config/spooling."""
    cfg = tmp_path / "cloud.json"
    monkeypatch.setattr(cloud, "CONFIG_PATH", cfg)
    monkeypatch.delenv("SPOOLING_CLOUD_URL", raising=False)
    monkeypatch.delenv("SPOOLING_CLOUD_API_KEY", raising=False)

    def point_at(url):
        cfg.write_text(json.dumps({"api_key": "sk_test", "api_url": url}))

    point_at(cloud.DEFAULT_API)
    return point_at


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def run(*args):
    return CliRunner().invoke(cli, list(args))


def one_session():
    return [{"id": "s1", "provider_id": "claude-code", "project": "p", "title": "t",
             "messages": [], "message_count": 0}]


# --- DNS ---------------------------------------------------------------------

def _no_dns(monkeypatch):
    def boom(*a, **k):
        raise socket.gaierror(8, "nodename nor servname provided, or not known")
    monkeypatch.setattr(socket, "getaddrinfo", boom)


def test_push_fails_fast_on_dns_before_any_work(logged_in, monkeypatch):
    _no_dns(monkeypatch)
    monkeypatch.setattr(cloud, "_sync_local", lambda *a, **k: pytest.fail("synced before preflight"))
    monkeypatch.setattr(cloud, "_collect_sessions", lambda **k: pytest.fail("collected before preflight"))

    r = run("push", "--with-spans")

    assert r.exit_code == 1
    assert "Can't reach Spooling Cloud at https://api.spooling.ai" in r.output
    assert "doesn't resolve" in r.output and "api.spooling.ai" in r.output
    assert "--api-url" in r.output and "SPOOLING_CLOUD_URL" in r.output
    assert "Nothing was sent" in r.output
    assert "Errno" not in r.output          # the raw error is gone


def test_push_dry_run_needs_no_network(logged_in, monkeypatch):
    _no_dns(monkeypatch)
    monkeypatch.setattr(cloud, "_collect_sessions", lambda **k: one_session())
    r = run("push", "--dry-run", "--no-sync")
    assert r.exit_code == 0
    assert "would land in" in r.output


@pytest.mark.parametrize("cmd", [["cloud", "pull"], ["cloud", "sync"], ["cloud", "delete", "--all", "--yes"]])
def test_other_network_commands_preflight_too(logged_in, monkeypatch, cmd):
    _no_dns(monkeypatch)
    r = run(*cmd)
    assert r.exit_code == 1 and "doesn't resolve" in r.output


@pytest.mark.parametrize("msg", [
    "[Errno 8] nodename nor servname provided, or not known",      # macOS
    "[Errno -2] Name or service not known",                         # Linux
    "[Errno -3] Temporary failure in name resolution",
    "[Errno 11001] getaddrinfo failed",                             # Windows
])
def test_dns_detected_from_message_even_when_wrapped(msg):
    inner = OSError(msg)
    wrapped = httpx.ConnectError("boom")
    wrapped.__cause__ = inner
    assert cloud._is_dns_error(wrapped)
    assert "doesn't resolve" in cloud._explain_error(wrapped, "https://x.example")


# --- connection-level errors -------------------------------------------------

def test_connection_refused_is_explained(logged_in):
    logged_in(f"http://127.0.0.1:{free_port()}")
    r = run("cloud", "status")
    assert r.exit_code == 1
    assert "Connection refused" in r.output and "nothing is listening" in r.output


def test_timeout_and_tls_messages():
    t = cloud._explain_error(httpx.ReadTimeout("slow"), "https://x.example")
    assert "Timed out" in t
    inner = ssl.SSLError("CERTIFICATE_VERIFY_FAILED")
    e = httpx.ConnectError("tls")
    e.__cause__ = inner
    assert "TLS/SSL error" in cloud._explain_error(e, "https://x.example")


def test_explainer_never_raises_on_garbage():
    assert cloud._explain_error(ValueError("x"), "http://host:99999999")      # bad port
    assert cloud._explain_error(RuntimeError(""), "")                          # empty everything
    assert cloud._explain_error(Exception("[red]markup[/red]"), "https://x")   # rich markup in message


# --- HTTP status codes -------------------------------------------------------

@pytest.mark.parametrize("status,needle", [
    (401, "rejected your API key"),
    (403, "isn't allowed"),
    (429, "rate limited, retry in 42s"),
    (500, "not a problem with your data"),
])
def test_http_errors_are_explained_and_exit_nonzero(logged_in, server, status, needle):
    server.handler.status = status
    logged_in(server.url)
    r = run("cloud", "status")
    assert r.exit_code == 1
    assert needle in r.output


def test_status_success(logged_in, server):
    logged_in(server.url)
    r = run("cloud", "status")
    assert r.exit_code == 0
    assert "sessions: 3" in r.output


# --- push exit codes ---------------------------------------------------------

def test_push_success_exits_zero(logged_in, server, monkeypatch):
    logged_in(server.url)
    monkeypatch.setattr(cloud, "_collect_sessions", lambda **k: one_session())
    r = run("push", "--no-sync")
    assert r.exit_code == 0 and "Done." in r.output
    assert "POST /v1/sessions/batch" in server.handler.seen


@pytest.mark.parametrize("status", [401, 500])
def test_push_server_error_exits_nonzero(logged_in, server, monkeypatch, status):
    server.handler.status = status
    logged_in(server.url)
    monkeypatch.setattr(cloud, "_collect_sessions", lambda **k: one_session())
    r = run("push", "--no-sync")
    assert r.exit_code == 1
    assert "Done." not in r.output


def test_push_connection_refused_exits_nonzero(logged_in, monkeypatch):
    logged_in(f"http://127.0.0.1:{free_port()}")
    monkeypatch.setattr(cloud, "_collect_sessions", lambda **k: one_session())
    r = run("push", "--no-sync")
    assert r.exit_code == 1 and "Connection refused" in r.output


def test_login_check_failure_still_saves_key_and_explains(tmp_path, monkeypatch):
    monkeypatch.setattr(cloud, "CONFIG_PATH", tmp_path / "cloud.json")
    port = free_port()
    r = run("cloud", "login", "--key", "sk_new", "--api-url", f"http://127.0.0.1:{port}")
    assert "couldn't verify it" in r.output and "Connection refused" in r.output
    assert json.loads((tmp_path / "cloud.json").read_text())["api_key"] == "sk_new"
