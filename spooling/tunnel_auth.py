"""Token management for securing the Spooling tunnel.

Tokens are stored at ~/.config/spooling/tunnel.json (mode 0o600) and can be
overridden at any time via the SPOOLING_MCP_TOKEN environment variable.

Typical workflow
----------------
1. ``spooling token generate``  – create a token once, saved to disk.
2. ``spooling mcp``             – MCP server starts; reads and enforces the token.
3. ``spooling tunnel``          – starts the Cloudflare tunnel and prints the
                                  ready-to-paste MCP config snippet (with the token).
4. Paste the snippet into your MCP client / agent config.

The token is a ``sk_``-prefixed, URL-safe base64 string backed by 32 bytes of
``secrets.token_urlsafe`` entropy (~43 chars after the prefix, ~256 bits).
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Optional, Tuple

_CONFIG_DIR = Path.home() / ".config" / "spooling"
_TOKEN_FILE = _CONFIG_DIR / "tunnel.json"

TOKEN_ENV_VAR = "SPOOLING_MCP_TOKEN"
TOKEN_PREFIX = "sk_"


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------

def generate_token() -> str:
    """Return a fresh cryptographically-secure tunnel token."""
    return f"{TOKEN_PREFIX}{secrets.token_urlsafe(32)}"


def save_token(token: str) -> None:
    """Persist *token* to ``~/.config/spooling/tunnel.json`` (mode 0o600).

    Merges with any pre-existing keys in the file so other config is preserved.
    """
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if _TOKEN_FILE.exists():
        try:
            data = json.loads(_TOKEN_FILE.read_text())
        except Exception:
            pass
    data["token"] = token
    _TOKEN_FILE.write_text(json.dumps(data, indent=2))
    _TOKEN_FILE.chmod(0o600)


def load_token() -> Optional[str]:
    """Return the active tunnel token, or ``None`` if none is configured.

    Resolution order:

    1. ``SPOOLING_MCP_TOKEN`` environment variable (highest priority).
    2. ``~/.config/spooling/tunnel.json`` → ``"token"`` key.
    """
    env = os.environ.get(TOKEN_ENV_VAR)
    if env:
        return env
    if _TOKEN_FILE.exists():
        try:
            return json.loads(_TOKEN_FILE.read_text()).get("token")
        except Exception:
            pass
    return None


def get_or_create_token() -> Tuple[str, bool]:
    """Return ``(token, created)``.

    If a token already exists it is returned unchanged (``created=False``).
    Otherwise a new token is generated, saved, and returned (``created=True``).
    """
    existing = load_token()
    if existing:
        return existing, False
    token = generate_token()
    save_token(token)
    return token, True
