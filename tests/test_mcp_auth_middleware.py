"""Tests for _BearerTokenMiddleware in spooling.mcp_server."""

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from spooling.mcp_server import _BearerTokenMiddleware

_TOKEN = "sk_test_token_abc123"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_app(token: str) -> Starlette:
    """Minimal Starlette app wrapped with BearerTokenMiddleware."""

    async def homepage(request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/", homepage), Route("/mcp", homepage)])
    app.add_middleware(_BearerTokenMiddleware, token=token)
    return app


# ---------------------------------------------------------------------------
# Happy-path
# ---------------------------------------------------------------------------

def test_valid_bearer_token_is_accepted():
    client = TestClient(_make_app(_TOKEN), raise_server_exceptions=True)
    resp = client.get("/mcp", headers={"Authorization": f"Bearer {_TOKEN}"})
    assert resp.status_code == 200
    assert resp.text == "ok"


def test_valid_token_on_root_path():
    client = TestClient(_make_app(_TOKEN), raise_server_exceptions=True)
    resp = client.get("/", headers={"Authorization": f"Bearer {_TOKEN}"})
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Rejection cases
# ---------------------------------------------------------------------------

def test_missing_auth_header_returns_401():
    client = TestClient(_make_app(_TOKEN), raise_server_exceptions=True)
    resp = client.get("/mcp")
    assert resp.status_code == 401


def test_wrong_token_returns_401():
    client = TestClient(_make_app(_TOKEN), raise_server_exceptions=True)
    resp = client.get("/mcp", headers={"Authorization": "Bearer sk_wrong_token"})
    assert resp.status_code == 401


def test_empty_bearer_value_returns_401():
    client = TestClient(_make_app(_TOKEN), raise_server_exceptions=True)
    resp = client.get("/mcp", headers={"Authorization": "Bearer "})
    assert resp.status_code == 401


def test_non_bearer_scheme_returns_401():
    client = TestClient(_make_app(_TOKEN), raise_server_exceptions=True)
    resp = client.get("/mcp", headers={"Authorization": f"Token {_TOKEN}"})
    assert resp.status_code == 401


def test_basic_auth_scheme_returns_401():
    client = TestClient(_make_app(_TOKEN), raise_server_exceptions=True)
    resp = client.get("/mcp", headers={"Authorization": "Basic dXNlcjpwYXNz"})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# WWW-Authenticate header
# ---------------------------------------------------------------------------

def test_401_includes_www_authenticate_header():
    client = TestClient(_make_app(_TOKEN), raise_server_exceptions=True)
    resp = client.get("/mcp")
    assert "WWW-Authenticate" in resp.headers
    assert resp.headers["WWW-Authenticate"] == "Bearer"


# ---------------------------------------------------------------------------
# Token edge-cases
# ---------------------------------------------------------------------------

def test_token_with_padding_spaces_rejected():
    """Tokens with leading/trailing spaces must not match the stored token."""
    client = TestClient(_make_app(_TOKEN), raise_server_exceptions=True)
    resp = client.get("/mcp", headers={"Authorization": f"Bearer  {_TOKEN} "})
    assert resp.status_code == 401


def test_partial_token_rejected():
    client = TestClient(_make_app(_TOKEN), raise_server_exceptions=True)
    resp = client.get("/mcp", headers={"Authorization": f"Bearer {_TOKEN[:10]}"})
    assert resp.status_code == 401


def test_different_token_instances_are_independent():
    """Two apps with different tokens must not accept each other's tokens."""
    tok_a = "sk_token_aaa"
    tok_b = "sk_token_bbb"
    client_a = TestClient(_make_app(tok_a), raise_server_exceptions=True)
    client_b = TestClient(_make_app(tok_b), raise_server_exceptions=True)

    assert client_a.get("/mcp", headers={"Authorization": f"Bearer {tok_a}"}).status_code == 200
    assert client_b.get("/mcp", headers={"Authorization": f"Bearer {tok_b}"}).status_code == 200
    assert client_a.get("/mcp", headers={"Authorization": f"Bearer {tok_b}"}).status_code == 401
    assert client_b.get("/mcp", headers={"Authorization": f"Bearer {tok_a}"}).status_code == 401
