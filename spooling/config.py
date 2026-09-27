"""Configuration for Spooling."""

import os
import sys
from pathlib import Path

# Legacy session data directory (JSONL-format sessions)
SESSIONS_DIR = Path.home() / ".sessions"
SESSIONS_PROJECTS_DIR = SESSIONS_DIR / "projects"

# Snowflake Cortex Code data directory.
CORTEX_DIR = Path.home() / ".snowflake" / "cortex"
CORTEX_CONVERSATIONS_DIR = CORTEX_DIR / "conversations"

# opencode (sst/opencode) data directory. Single SQLite DB at
# ~/.local/share/opencode/opencode.db
OPENCODE_DIR = Path.home() / ".local" / "share" / "opencode"
OPENCODE_DB = OPENCODE_DIR / "opencode.db"

# ---------------------------------------------------------------------------
# SQLite database path
# ---------------------------------------------------------------------------
# XDG-style on Linux/macOS: ~/.local/share/spooling/spooling.db
# Windows:                   %APPDATA%\spooling\spooling.db
# Override with SPOOLING_DB env var.

def _default_db_path() -> Path:
    if env := os.getenv("SPOOLING_DB"):
        return Path(env)
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home()))
    else:
        base = Path.home() / ".local" / "share"
    return base / "spooling" / "spooling.db"

DB_PATH: Path = _default_db_path()

# Embeddings
EMBEDDING_MODEL = os.getenv("SPOOLING_EMBEDDING_MODEL", "all-MiniLM-L6-v2")
EMBEDDING_DIM = 384
CHUNK_SIZE = 500  # chars per chunk for embedding

# Server
UI_HOST = os.getenv("SPOOLING_UI_HOST", "127.0.0.1")
UI_PORT = int(os.getenv("SPOOLING_UI_PORT", "3001"))

# Token estimation (rough heuristic: ~4 chars per token)
CHARS_PER_TOKEN = 4

# Default model pricing per 1M tokens (input, output)
DEFAULT_PRICING = (3.0, 15.0)

# Known model pricing overrides; falls back to LiteLLM pricing API otherwise.
MODEL_PRICING: dict[str, tuple[float, float]] = {}
