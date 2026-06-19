# Spooling

Local session tracker and semantic search for AI coding assistants.

Track your AI coding sessions across **OpenAI Codex CLI**, **GitHub Copilot**, **Cursor**, **Windsurf**, **Kiro**, **Google Antigravity**, and **opencode**, all in one place. Get usage stats, cost estimates, per-provider breakdowns, semantic search via pgvector, and a built-in AI chat agent to explore your history.

**Website:** [spooling.ai](https://spooling.ai)

---

## Prerequisites

- **Python 3.11+**
- **Node.js 18+**
- **Docker** (for PostgreSQL + pgvector)
- **pipx** or **uv** (optional, alternative install methods)
- **Ollama** (optional, for free local AI chat) or an **Anthropic API key**

---

## How it works

**Four commands. Zero cloud.**

### 01 &nbsp; Clone & start the database

```bash
git clone https://github.com/sashimiboi/spooling && cd spooling
docker compose up -d   # postgres + pgvector :5434
```

### 02 &nbsp; Install backend + UI

Choose one:

```bash
# pip (recommended)
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

# pipx
pipx install .

# uv
uv sync
```

Then install the UI:

```bash
cd ui && npm install && cd ..
```

### 03 &nbsp; Detect providers & sync

```bash
spooling init      # scan for available providers
spooling sync      # embed every session into pgvector
```

### 04 &nbsp; Search & explore

```bash
spooling ui        # API :3002 · MCP :3004 · GUI :3003
spooling search "that redis race condition"
```

Open **http://localhost:3003** and you're in.

---

## Connect Spooling to your AI coding agent

`spooling ui` automatically exposes an MCP server at `http://127.0.0.1:3004/mcp` (HTTP streamable transport). Any MCP-speaking agent can connect to it and pull context from your local KB mid-conversation. The agent gets tools like `spooling_search`, `spooling_recent_sessions`, `spooling_get_session`, `spooling_workspace_stats`, and `spooling_top_projects`.

### Cursor / Windsurf / Codex / Antigravity

Edit your client's MCP config (usually `~/.cursor/mcp.json`, `~/.codeium/windsurf/mcp_config.json`, or the equivalent) and add:

```json
{
  "mcpServers": {
    "spooling": {
      "type": "http",
      "url": "http://127.0.0.1:3004/mcp"
    }
  }
}
```

Restart the client. The `spooling` server and its tools should appear in the MCP panel.

### Generic JSON-RPC smoke test

```bash
curl -s http://127.0.0.1:3004/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | jq
```

Should return the five `spool_*` tools. If you get "connection refused", `spooling ui` is not running (or its ports got squatted, run `lsof -ti :3004 | xargs kill -9` and retry).

---

## CLI Usage

All CLI commands require the venv to be active and the database running.

```bash
source .venv/bin/activate
```

### `spooling init`

Check database connection and show which AI coding tool providers are detected on your system. This scans default paths for Codex CLI, GitHub Copilot, Cursor, and Windsurf session data.

```bash
spooling init
```

### `spooling sync`

Parse and ingest sessions from all connected providers into the database. Chunks and embeds message content into pgvector for semantic search.

```bash
spooling sync                      # Full sync with embeddings
spooling sync --no-embed           # Skip embeddings (faster initial sync)
spooling sync -p cursor            # Sync only Cursor sessions
spooling sync -p codex             # Sync only Codex CLI sessions
```

### `spooling stats`

Show usage statistics - sessions, messages, tool calls, tokens, costs, broken down by project and day.

```bash
spooling stats             # Overview + last 7 days
spooling stats --week      # Weekly breakdown
spooling stats --days 30   # Last 30 days
```

### `spooling search <query>`

Semantic search across all your session history using natural language.

```bash
spooling search "snowflake connector"
spooling search "authentication bug" -n 5
spooling search "database migration" -p ~/myproject
```

Options:
- `-n, --limit` - Number of results (default: 10)
- `-p, --project` - Filter by project name

### `spooling watch`

Watch all connected provider directories for new session data and auto-sync in real time.

```bash
spooling watch
```

### `spooling serve`

Start the API server only (for when you want to run the GUI separately).

```bash
spooling serve                    # Default: http://127.0.0.1:3002
spooling serve --port 8080        # Custom port
spooling serve --host 0.0.0.0     # Bind to all interfaces
```

### `spooling ui`

Start the API server, the MCP HTTP server, and the Next.js UI together.

```bash
spooling ui
```

### `spooling mcp`

Start the Spooling MCP server on its own. Defaults to streamable-HTTP at
`http://127.0.0.1:3004/mcp`, which any MCP-compatible agent can connect to
by URL. `spooling ui` already launches this alongside the API, so you only
need to run it directly when you want the MCP server without the GUI.

```bash
spooling mcp              # streamable-HTTP at http://127.0.0.1:3004/mcp (default)
spooling mcp --stdio      # stdio transport, for stdio-only clients
```

---

## Spooling Cloud (optional)

By default Spooling stays 100% local. If you also want your sessions in the
hosted workspace at [spooling.ai](https://spooling.ai) (so teammates can
search the same pool, or you can chat with sessions from any browser),
the CLI ships with a `spooling cloud` subcommand.

### One-time setup

1. Mint an API key in the GUI at `app.spooling.ai/settings/api-keys`
   (looks like `sk_live_...`).
2. Save it locally:

   ```bash
   spooling cloud login --key sk_live_...
   ```

   The key is stored at `~/.config/spooling/cloud.json` with `0600` perms.
   You can override the API base with `--api-url` or the
   `SPOOLING_CLOUD_URL` env var (default: `https://api.spooling.ai`).

### Push once

Send the most recent local sessions up to the cloud:

```bash
spooling push                 # 100 sessions, batches of 20
spooling push --limit 500     # bigger backfill
```

The server upserts by session id, so re-running is safe.

### Watch (continuous push)

Stream new and updated sessions to the cloud on a timer. Stop with
Ctrl+C.

```bash
spooling cloud watch                 # every 60s, 1000 sessions/cycle
spooling cloud watch --interval 30   # tighter cadence
spooling cloud watch --lookback 60   # widen the overlap window if you edit old sessions
```

What it does each cycle:

1. Reads `last_push_at` from `~/.config/spooling/cloud.json`.
2. Queries local sessions where `started_at >= last_push_at - lookback`
   (default lookback: 10 minutes, so messages appended to an in-progress
   session get re-uploaded).
3. POSTs to `/v1/sessions/batch` in chunks of `--batch` (default 20).
4. Advances `last_push_at` to the cycle start on success.

Cycles with no new work are silent. If a push fails the watermark is
not advanced, so the next cycle retries the same window.

### Cloud API endpoints (programmatic access)

The cloud API at `api.spooling.ai` uses `/v1/` endpoints. Requests require a Bearer token via the `Authorization` header:

```bash
# Stats
curl -s "https://api.spooling.ai/v1/stats" \
  -H "Authorization: Bearer $SPOOLING_KEY"

# Search (when deployed — falls back to local search on self-hosted)
curl -s "https://api.spooling.ai/api/search?q=migration&limit=10" \
  -H "Authorization: Bearer $SPOOLING_KEY"
```

**Important**: `app.spooling.ai` is the Next.js frontend. For API access, use `api.spooling.ai` directly or set `API_URL=https://api.spooling.ai` when deploying the frontend so its `/api/*` rewrites reach the backend.

### Status / logout

```bash
spooling cloud status   # show what is in the cloud + stored API base
spooling cloud logout   # remove the stored API key
```

### Auto-start at login (macOS)

Run `spooling cloud watch` as a launchd agent so it survives reboots:

```bash
cat > ~/Library/LaunchAgents/ai.spooling.cloud-watch.plist <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key><string>ai.spooling.cloud-watch</string>
    <key>ProgramArguments</key>
    <array>
      <string>/usr/local/bin/spooling</string>
      <string>cloud</string>
      <string>watch</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>/tmp/spooling-cloud-watch.log</string>
    <key>StandardErrorPath</key><string>/tmp/spooling-cloud-watch.log</string>
  </dict>
</plist>
PLIST
launchctl load ~/Library/LaunchAgents/ai.spooling.cloud-watch.plist
```

(Adjust the `spooling` path with `which spooling` if it lives elsewhere.)

---

## MCP Endpoint

Spooling exposes an MCP server so any AI agent can query your session history
as a context source. The server runs over **streamable-HTTP** at
`http://127.0.0.1:3004/mcp` and is started automatically alongside `spooling
ui`.

Drop this into your MCP client config (`~/.mcp.json`, Cursor,
Codex, or any other streamable-HTTP capable agent):

```json
{
  "mcpServers": {
    "spooling": {
      "url": "http://127.0.0.1:3004/mcp"
    }
  }
}
```

Or register it with an MCP client's config file.

The **Settings** page in the GUI shows the endpoint URL, a copy button, and
the full config snippet so you don't have to remember it.

### Tools exposed

| Tool | Purpose |
|------|---------|
| `list_traces` | Recent Spooling traces, filterable by provider/project |
| `get_trace` | Full detail for one trace: header, spans, eval scores |
| `search_sessions` | Semantic search over embedded session chunks |
| `get_stats` | Top-line stats: traces, tokens, cost, errors |
| `get_top_vendors` | Most-used external vendors by tool-call count |
| `list_evals` | Recent eval runs, optionally filtered by rubric |
| `list_rubrics` | All configured Strands eval rubrics |
| `run_eval` | Run a rubric against a trace and persist the result |

### Stdio (legacy clients)

For MCP clients that only speak stdio, run `spooling mcp --stdio` and register
it with the command-based form:

```bash
# MCP config for stdio transport
```

---

## GUI

The Spooling GUI runs on **http://localhost:3003** and includes:

| Page | Description |
|------|-------------|
| **Dashboard** | Overview stats, per-provider breakdown, daily activity chart, projects, top tools, recent sessions |
| **Sessions** | Browse all sessions with provider labels, filtering, click into any session for full conversation view |
| **Search** | Semantic search across all session history with similarity scores |
| **Analytics** | Charts for daily usage, cost trends, token usage, tool distribution, filterable by provider (AG Charts) |
| **Chat** | AI assistant that can answer questions about your session data (RAG-powered) |
| **Connections** | Connect/disconnect AI coding tools (Codex, Copilot, Cursor, Windsurf) |
| **Settings** | Configure the AI chat provider (Ollama or Anthropic) |

### Running the GUI

```bash
# Terminal 1: API server
source .venv/bin/activate
spooling serve

# Terminal 2: Next.js dev server
cd ui
npm run dev
```

Or use `spooling ui` to start both at once.

---

## Chat Agent Setup

The chat page lets you ask questions about your coding sessions in natural language. It uses RAG - retrieves relevant context from pgvector before answering.

### Option A: Ollama (free, local)

```bash
# Install Ollama
brew install ollama

# Start the server
ollama serve

# Pull a model
ollama pull gemma3:4b
```

Go to **Settings** in the GUI and select Ollama. The model will auto-detect.

### Option B: Anthropic API (bring your own key)

Go to **Settings** in the GUI, select Anthropic, and paste your API key from [console.anthropic.com](https://console.anthropic.com).

Available models: Sonnet, Haiku, Opus.

---

## Architecture

```
spooling/
├── docker-compose.yml       # PostgreSQL + pgvector
├── init.sql                 # Database schema
├── pyproject.toml           # Python package config
├── spooling/                   # Python backend
│   ├── cli.py               # Click CLI
│   ├── config.py            # Configuration
│   ├── db.py                # Database connection
│   ├── providers/           # Provider plugins (codex, copilot, cursor, windsurf)
│   ├── parser.py            # Session JSONL parser
│   ├── embeddings.py        # sentence-transformers (all-MiniLM-L6-v2)
│   ├── ingest.py            # Sync pipeline
│   ├── search.py            # pgvector semantic search
│   ├── stats.py             # Usage statistics
│   ├── watcher.py           # File watcher (watchdog)
│   ├── agent.py             # Chat agent (Ollama + Anthropic)
│   └── server.py            # FastAPI API server
└── ui/                      # Next.js frontend
    ├── next.config.js       # API proxy to :3002
    └── src/
        ├── components/      # shadcn/ui components
        ├── lib/             # API helpers
        └── app/(app)/       # Pages (dashboard, sessions, search, etc.)
```

### Stack

| Layer | Technology |
|-------|-----------|
| Database | PostgreSQL 16 + pgvector (Docker) |
| Embeddings | sentence-transformers / all-MiniLM-L6-v2 (local) |
| Backend | Python, FastAPI, Click |
| Frontend | Next.js 14, shadcn/ui, Tailwind CSS, AG Charts |
| Chat AI | Ollama (local) or Anthropic API |

### Ports

| Service | Port |
|---------|------|
| PostgreSQL | 5434 |
| API Server | 3002 |
| GUI | 3003 |
| MCP Server (streamable-HTTP) | 3004 |

---

## Environment Variables

All optional - defaults work out of the box for local development.

| Variable | Default | Description |
|----------|---------|-------------|
| `SPOOLING_DB_HOST` | `localhost` | Database host |
| `SPOOLING_DB_PORT` | `5434` | Database port |
| `SPOOLING_DB_NAME` | `spooling` | Database name |
| `SPOOLING_DB_USER` | `spooling` | Database user |
| `SPOOLING_DB_PASSWORD` | `spooling` | Database password |
| `SPOOLING_EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | Sentence transformer model |
| `SPOOLING_UI_HOST` | `127.0.0.1` | API server host |
| `ANTHROPIC_API_KEY` | - | Anthropic API key (alternative to setting in UI) |
| `API_URL` | `http://127.0.0.1:3002` | Backend API URL for Next.js rewrites (`/api/*`→`<API_URL>/api/*`). Set to `https://api.spooling.ai` in production. |

---

## Supported Providers

| Provider | Data Location | Format |
|----------|--------------|--------|
| **JSONL Sessions** | `~/.sessions/projects/` | UUID-named JSONL files with conversation history, tool calls, git context |
| **OpenAI Codex CLI** | `~/.codex/sessions/` | `rollout-*.jsonl` files organized by date |
| **GitHub Copilot** | `~/Library/Application Support/Code/User/workspaceStorage/` | Chat session JSON from VS Code |
| **Cursor** | `~/Library/Application Support/Cursor/User/workspaceStorage/` | Chat and composer sessions from SQLite |
| **Windsurf** | `~/Library/Application Support/Windsurf/User/workspaceStorage/` | Chat and Cascade sessions from SQLite |
| **Kiro** | `~/Library/Application Support/Kiro/User/workspaceStorage/` | AWS Kiro chat and agent sessions from SQLite |
| **Google Antigravity** | `~/Library/Application Support/Antigravity/User/workspaceStorage/` | Antigravity chat and agent sessions from SQLite |
| **opencode** | `~/.local/share/opencode/opencode.db` | sst/opencode SQLite database with session/message/part tables (Vercel AI SDK part shape) |

Run `spooling init` to see which providers are detected on your system.

No data is sent to external servers. Everything runs locally.
