"""Configuration for Spool."""

import os
from pathlib import Path

# Claude Code data directory
CLAUDE_DIR = Path.home() / ".claude"
CLAUDE_PROJECTS_DIR = CLAUDE_DIR / "projects"

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
DB_HOST = os.getenv("SPOOL_DB_HOST", "localhost")
DB_PORT = int(os.getenv("SPOOL_DB_PORT", "5434"))
DB_NAME = os.getenv("SPOOL_DB_NAME", "spool")
DB_USER = os.getenv("SPOOL_DB_USER", "spool")
DB_PASSWORD = os.getenv("SPOOL_DB_PASSWORD", "spool")

DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

# Embeddings
EMBEDDING_MODEL = os.getenv("SPOOL_EMBEDDING_MODEL", "all-MiniLM-L6-v2")
EMBEDDING_DIM = 384
CHUNK_SIZE = 500  # chars per chunk for embedding

# Server
UI_HOST = os.getenv("SPOOL_UI_HOST", "127.0.0.1")
UI_PORT = int(os.getenv("SPOOL_UI_PORT", "3001"))

# Token estimation (rough heuristic: ~4 chars per token)
CHARS_PER_TOKEN = 4

# Model pricing per 1M tokens (input, output)
MODEL_PRICING = {
    "claude-opus-4-6": (15.0, 75.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5-20251001": (0.80, 4.0),
}
DEFAULT_PRICING = (3.0, 15.0)
