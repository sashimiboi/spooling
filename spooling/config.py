"""Configuration for Spooling."""

import os
from pathlib import Path

# Legacy session data directory (JSONL-format sessions)
SESSIONS_DIR = Path.home() / ".sessions"
SESSIONS_PROJECTS_DIR = SESSIONS_DIR / "projects"

# Snowflake Cortex Code data directory. Sessions live in
# ~/.snowflake/cortex/conversations/<uuid>.history.jsonl with a sidecar
# <uuid>.json carrying title, working_directory, git info, and timestamps.
CORTEX_DIR = Path.home() / ".snowflake" / "cortex"
CORTEX_CONVERSATIONS_DIR = CORTEX_DIR / "conversations"

# opencode (sst/opencode) data directory. Single SQLite DB at
# ~/.local/share/opencode/opencode.db with session/message/part tables
# (Drizzle-managed). Parts carry the Vercel AI SDK UIMessage payload.
OPENCODE_DIR = Path.home() / ".local" / "share" / "opencode"
OPENCODE_DB = OPENCODE_DIR / "opencode.db"

# Database
DB_HOST = os.getenv("SPOOLING_DB_HOST", "localhost")
DB_PORT = int(os.getenv("SPOOLING_DB_PORT", "5434"))
DB_NAME = os.getenv("SPOOLING_DB_NAME", "spooling")
DB_USER = os.getenv("SPOOLING_DB_USER", "spooling")
DB_PASSWORD = os.getenv("SPOOLING_DB_PASSWORD", "spooling")

DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

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
