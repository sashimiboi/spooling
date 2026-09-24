"""Tests for spooling.tunnel_auth — token generation, persistence, and loading."""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from spooling.tunnel_auth import (
    TOKEN_ENV_VAR,
    TOKEN_PREFIX,
    _TOKEN_FILE,
    generate_token,
    get_or_create_token,
    load_token,
    save_token,
)


# ---------------------------------------------------------------------------
# generate_token
# ---------------------------------------------------------------------------

def test_generate_token_has_prefix():
    tok = generate_token()
    assert tok.startswith(TOKEN_PREFIX), f"Expected 'sk_' prefix, got: {tok!r}"


def test_generate_token_sufficient_entropy():
    tok = generate_token()
    # sk_ (3) + 43 chars of base64url from 32 bytes → total >= 46
    assert len(tok) >= 46, f"Token too short: {len(tok)}"


def test_generate_token_is_unique():
    tokens = {generate_token() for _ in range(20)}
    assert len(tokens) == 20, "Duplicate token generated"


def test_generate_token_urlsafe_chars():
    import re
    tok = generate_token()
    # After the prefix, only URL-safe base64 chars (no +, /)
    suffix = tok[len(TOKEN_PREFIX):]
    assert re.fullmatch(r"[A-Za-z0-9\-_]+", suffix), f"Non-URL-safe chars in: {suffix!r}"


# ---------------------------------------------------------------------------
# save_token / load_token (file-based)
# ---------------------------------------------------------------------------

def test_save_and_load_roundtrip(tmp_path, monkeypatch):
    token_file = tmp_path / "tunnel.json"
    monkeypatch.setattr("spooling.tunnel_auth._TOKEN_FILE", token_file)
    monkeypatch.setattr("spooling.tunnel_auth._CONFIG_DIR", tmp_path)
    # Ensure env var doesn't interfere
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)

    tok = generate_token()
    save_token(tok)

    assert token_file.exists()
    loaded = load_token()
    assert loaded == tok


def test_save_token_sets_restrictive_permissions(tmp_path, monkeypatch):
    token_file = tmp_path / "tunnel.json"
    monkeypatch.setattr("spooling.tunnel_auth._TOKEN_FILE", token_file)
    monkeypatch.setattr("spooling.tunnel_auth._CONFIG_DIR", tmp_path)
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)

    save_token(generate_token())
    mode = token_file.stat().st_mode & 0o777
    assert mode == 0o600, f"Expected 0o600 permissions, got {oct(mode)}"


def test_save_token_preserves_other_keys(tmp_path, monkeypatch):
    token_file = tmp_path / "tunnel.json"
    monkeypatch.setattr("spooling.tunnel_auth._TOKEN_FILE", token_file)
    monkeypatch.setattr("spooling.tunnel_auth._CONFIG_DIR", tmp_path)
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)

    token_file.write_text(json.dumps({"other_key": "keep_me"}))
    token_file.chmod(0o600)

    save_token("sk_newtoken")
    data = json.loads(token_file.read_text())
    assert data["other_key"] == "keep_me"
    assert data["token"] == "sk_newtoken"


def test_load_token_returns_none_when_no_file_no_env(tmp_path, monkeypatch):
    token_file = tmp_path / "nonexistent.json"
    monkeypatch.setattr("spooling.tunnel_auth._TOKEN_FILE", token_file)
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)

    assert load_token() is None


def test_load_token_handles_corrupt_file(tmp_path, monkeypatch):
    token_file = tmp_path / "tunnel.json"
    token_file.write_text("not json {{{{")
    monkeypatch.setattr("spooling.tunnel_auth._TOKEN_FILE", token_file)
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)

    assert load_token() is None


# ---------------------------------------------------------------------------
# load_token — env var priority
# ---------------------------------------------------------------------------

def test_env_var_takes_priority_over_file(tmp_path, monkeypatch):
    token_file = tmp_path / "tunnel.json"
    monkeypatch.setattr("spooling.tunnel_auth._TOKEN_FILE", token_file)
    monkeypatch.setattr("spooling.tunnel_auth._CONFIG_DIR", tmp_path)

    file_tok = "sk_from_file"
    save_token(file_tok)

    env_tok = "sk_from_env_var"
    monkeypatch.setenv(TOKEN_ENV_VAR, env_tok)

    assert load_token() == env_tok


def test_env_var_used_when_no_file(tmp_path, monkeypatch):
    token_file = tmp_path / "nonexistent.json"
    monkeypatch.setattr("spooling.tunnel_auth._TOKEN_FILE", token_file)
    env_tok = "sk_only_env"
    monkeypatch.setenv(TOKEN_ENV_VAR, env_tok)

    assert load_token() == env_tok


# ---------------------------------------------------------------------------
# get_or_create_token
# ---------------------------------------------------------------------------

def test_get_or_create_creates_when_missing(tmp_path, monkeypatch):
    token_file = tmp_path / "tunnel.json"
    monkeypatch.setattr("spooling.tunnel_auth._TOKEN_FILE", token_file)
    monkeypatch.setattr("spooling.tunnel_auth._CONFIG_DIR", tmp_path)
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)

    tok, created = get_or_create_token()
    assert created is True
    assert tok.startswith(TOKEN_PREFIX)
    assert token_file.exists()


def test_get_or_create_returns_existing(tmp_path, monkeypatch):
    token_file = tmp_path / "tunnel.json"
    monkeypatch.setattr("spooling.tunnel_auth._TOKEN_FILE", token_file)
    monkeypatch.setattr("spooling.tunnel_auth._CONFIG_DIR", tmp_path)
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)

    existing = "sk_already_here"
    save_token(existing)

    tok, created = get_or_create_token()
    assert created is False
    assert tok == existing


def test_get_or_create_is_idempotent(tmp_path, monkeypatch):
    token_file = tmp_path / "tunnel.json"
    monkeypatch.setattr("spooling.tunnel_auth._TOKEN_FILE", token_file)
    monkeypatch.setattr("spooling.tunnel_auth._CONFIG_DIR", tmp_path)
    monkeypatch.delenv(TOKEN_ENV_VAR, raising=False)

    tok1, _ = get_or_create_token()
    tok2, created2 = get_or_create_token()
    assert tok1 == tok2
    assert created2 is False
