# Spool

Local session tracker and semantic search for AI coding assistants.

Track your AI coding sessions across **Claude Code**, **OpenAI Codex CLI**, **GitHub Copilot**, **Cursor**, **Windsurf**, **Kiro**, **Google Antigravity**, and **opencode**, all in one place. Get usage stats, cost estimates, per-provider breakdowns, semantic search via pgvector, and a built-in AI chat agent to explore your history.

**Website:** [spooling.ai](https://spooling.ai)

---

## Prerequisites

- **Python 3.11+**
- **Node.js 18+**
- **Docker** (for PostgreSQL + pgvector)
- **Ollama** (optional, for free local AI chat) or an **Anthropic API key**

---

## Quick Start

```bash
git clone <repo-url> spool
cd spool

# 1. Start the database
docker-compose up -d   # or `docker compose up -d` if using Docker Compose V2

#docker-compose up -d  

# 2. Install Python backend (pulls strands-agents, strands-agents-evals,
#    ollama, mcp, and everything else declared in pyproject.toml).
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# 3. Install Ollama + pull the chat and judge models so the chat agent
#    and Strands eval rubrics work out of the box with no API key.
brew install ollama
ollama serve &
ollama pull gemma3:4b    # chat agent default
ollama pull qwen2.5:7b   # tool-capable Strands eval judge

# 4. Check which providers are detected
spool init

# 5. Sync sessions from all detected providers
spool sync              # with embeddings (slower, enables semantic search)
spool sync --no-embed   # without embeddings (faster, for initial setup)

# 6. Install UI dependencies
cd ui && npm install && cd ..

# 7. Start everything
spool ui             # API on :3002, MCP on :3004, GUI on :3003

# 8. (Optional) Connect an MCP-compatible agent to Spool. The MCP server
#    runs automatically with `spool ui` over streamable-HTTP at
#    http://127.0.0.1:3004/mcp, so any agent (Claude Code, Codex, Cursor,
#    web agents) can connect by URL. Example for Claude Code:
claude mcp add --transport http spool http://127.0.0.1:3004/mcp
```

Open **http://localhost:3003** and you're in.

---

## Connect Spool to your AI coding agent

`spool ui` automatically exposes an MCP server at `http://127.0.0.1:3004/mcp` (HTTP streamable transport). Any MCP-speaking agent can connect to it and pull context from your local KB mid-conversation. The agent gets tools like `spool_search`, `spool_recent_sessions`, `spool_get_session`, `spool_workspace_stats`, and `spool_top_projects`.

### Claude Code

```bash
claude mcp add spool http://127.0.0.1:3004/mcp --transport http
claude mcp list   # confirm "spool: ... ✓ Connected"
```

Then in any Claude Code session, type `/mcp` to see the registered tools. Try:

> *"Use spool_search to find anything in my sessions about the auth refactor."*

> *"Summarize what I worked on this week with spool_recent_sessions."*

### Cursor / Windsurf / Codex / Antigravity

Edit your client's MCP config (usually `~/.cursor/mcp.json`, `~/.codeium/windsurf/mcp_config.json`, or the equivalent) and add:

```json
{
  "mcpServers": {
    "spool": {
      "type": "http",
      "url": "http://127.0.0.1:3004/mcp"
    }
  }
}
```

Restart the client. The `spool` server and its tools should appear in the MCP panel.

### Generic JSON-RPC smoke test

```bash
curl -s http://127.0.0.1:3004/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | jq
```

Should return the five `spool_*` tools. If you get "connection refused", `spool ui` is not running (or its ports got squatted, run `lsof -ti :3004 | xargs kill -9` and retry).

---

## CLI Usage

All CLI commands require the venv to be active and the database running.

```bash
source .venv/bin/activate
```

### `spool init`

Check database connection and show which AI coding tool providers are detected on your system. This scans default paths for Claude Code, Codex CLI, GitHub Copilot, Cursor, and Windsurf session data.

```bash
spool init
```

### `spool sync`

Parse and ingest sessions from all connected providers into the database. Chunks and embeds message content into pgvector for semantic search.

```bash
spool sync                      # Full sync with embeddings
spool sync --no-embed           # Skip embeddings (faster initial sync)
spool sync -p claude-code       # Sync only Claude Code sessions
spool sync -p codex             # Sync only Codex CLI sessions
```

### `spool stats`

Show usage statistics - sessions, messages, tool calls, tokens, costs, broken down by project and day.

```bash
spool stats             # Overview + last 7 days
spool stats --week      # Weekly breakdown
spool stats --days 30   # Last 30 days
```

### `spool search <query>`

Semantic search across all your session history using natural language.

```bash
spool search "snowflake connector"
spool search "authentication bug" -n 5
spool search "database migration" -p ~/myproject
```

Options:
- `-n, --limit` - Number of results (default: 10)
- `-p, --project` - Filter by project name

### `spool watch`

Watch all connected provider directories for new session data and auto-sync in real time.

```bash
spool watch
```

### `spool serve`

Start the API server only (for when you want to run the GUI separately).

```bash
spool serve                    # Default: http://127.0.0.1:3002
spool serve --port 8080        # Custom port
spool serve --host 0.0.0.0     # Bind to all interfaces
```

### `spool ui`

Start the API server, the MCP HTTP server, and the Next.js UI together.

```bash
spool ui
```

### `spool mcp`

Start the Spool MCP server on its own. Defaults to streamable-HTTP at
`http://127.0.0.1:3004/mcp`, which any MCP-compatible agent can connect to
by URL. `spool ui` already launches this alongside the API, so you only
need to run it directly when you want the MCP server without the GUI.

```bash
spool mcp              # streamable-HTTP at http://127.0.0.1:3004/mcp (default)
spool mcp --stdio      # stdio transport, for stdio-only clients
```

---

## Spooling Cloud (optional)

By default Spool stays 100% local. If you also want your sessions in the
hosted workspace at [spooling.ai](https://spooling.ai) (so teammates can
search the same pool, or you can chat with sessions from any browser),
the CLI ships with a `spool cloud` subcommand.

### One-time setup

1. Mint an API key in the GUI at `app.spooling.ai/settings/api-keys`
   (looks like `sk_live_...`).
2. Save it locally:

   ```bash
   spool cloud login --key sk_live_...
   ```

   The key is stored at `~/.config/spool/cloud.json` with `0600` perms.
   You can override the API base with `--api-url` or the
   `SPOOL_CLOUD_URL` env var (default: `https://api.spooling.ai`).

### Push once

Send the most recent local sessions up to the cloud:

```bash
spool push                 # 100 sessions, batches of 20
spool push --limit 500     # bigger backfill
```

The server upserts by session id, so re-running is safe.

### Watch (continuous push)

Stream new and updated sessions to the cloud on a timer. Stop with
Ctrl+C.

```bash
spool cloud watch                 # every 60s, 1000 sessions/cycle
spool cloud watch --interval 30   # tighter cadence
spool cloud watch --lookback 60   # widen the overlap window if you edit old sessions
```

What it does each cycle:

1. Reads `last_push_at` from `~/.config/spool/cloud.json`.
2. Queries local sessions where `started_at >= last_push_at - lookback`
   (default lookback: 10 minutes, so messages appended to an in-progress
   session get re-uploaded).
3. POSTs to `/v1/sessions/batch` in chunks of `--batch` (default 20).
4. Advances `last_push_at` to the cycle start on success.

Cycles with no new work are silent. If a push fails the watermark is
not advanced, so the next cycle retries the same window.

### Status / logout

```bash
spool cloud status   # show what is in the cloud + stored API base
spool cloud logout   # remove the stored API key
```

### Auto-start at login (macOS)

Run `spool cloud watch` as a launchd agent so it survives reboots:

```bash
cat > ~/Library/LaunchAgents/ai.spooling.cloud-watch.plist <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key><string>ai.spooling.cloud-watch</string>
    <key>ProgramArguments</key>
    <array>
      <string>/usr/local/bin/spool</string>
      <string>cloud</string>
      <string>watch</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>/tmp/spool-cloud-watch.log</string>
    <key>StandardErrorPath</key><string>/tmp/spool-cloud-watch.log</string>
  </dict>
</plist>
PLIST
launchctl load ~/Library/LaunchAgents/ai.spooling.cloud-watch.plist
```

(Adjust the `spool` path with `which spool` if it lives elsewhere.)

---

## MCP Endpoint

Spool exposes an MCP server so any AI agent can query your session history
as a context source. The server runs over **streamable-HTTP** at
`http://127.0.0.1:3004/mcp` and is started automatically alongside `spool
ui`.

Drop this into your MCP client config (`~/.mcp.json`, Claude Code, Cursor,
Codex, or any other streamable-HTTP capable agent):

```json
{
  "mcpServers": {
    "spool": {
      "url": "http://127.0.0.1:3004/mcp"
    }
  }
}
```

Or register it with Claude Code directly:

```bash
claude mcp add --transport http spool http://127.0.0.1:3004/mcp
```

The **Settings** page in the GUI shows the endpoint URL, a copy button, and
the full config snippet so you don't have to remember it.

### Tools exposed

| Tool | Purpose |
|------|---------|
| `list_traces` | Recent Spool traces, filterable by provider/project |
| `get_trace` | Full detail for one trace: header, spans, eval scores |
| `search_sessions` | Semantic search over embedded session chunks |
| `get_stats` | Top-line stats: traces, tokens, cost, errors |
| `get_top_vendors` | Most-used external vendors by tool-call count |
| `list_evals` | Recent eval runs, optionally filtered by rubric |
| `list_rubrics` | All configured Strands eval rubrics |
| `run_eval` | Run a rubric against a trace and persist the result |

### Stdio (legacy clients)

For MCP clients that only speak stdio, run `spool mcp --stdio` and register
it with the command-based form:

```bash
claude mcp add spool $(pwd)/.venv/bin/spool mcp --stdio
```

---

## GUI

The Spool GUI runs on **http://localhost:3003** and includes:

| Page | Description |
|------|-------------|
| **Dashboard** | Overview stats, per-provider breakdown, daily activity chart, projects, top tools, recent sessions |
| **Sessions** | Browse all sessions with provider labels, filtering, click into any session for full conversation view |
| **Search** | Semantic search across all session history with similarity scores |
| **Analytics** | Charts for daily usage, cost trends, token usage, tool distribution, filterable by provider (AG Charts) |
| **Chat** | AI assistant that can answer questions about your session data (RAG-powered) |
| **Connections** | Connect/disconnect AI coding tools (Claude Code, Codex, Copilot, Cursor, Windsurf) |
| **Settings** | Configure the AI chat provider (Ollama or Anthropic) |

### Running the GUI

```bash
# Terminal 1: API server
source .venv/bin/activate
spool serve

# Terminal 2: Next.js dev server
cd ui
npm run dev
```

Or use `spool ui` to start both at once.

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
spool/
├── docker-compose.yml       # PostgreSQL + pgvector
├── init.sql                 # Database schema
├── pyproject.toml           # Python package config
├── spool/                   # Python backend
│   ├── cli.py               # Click CLI
│   ├── config.py            # Configuration
│   ├── db.py                # Database connection
│   ├── providers/           # Provider plugins (claude_code, codex, copilot, cursor, windsurf)
│   ├── parser.py            # Claude Code JSONL parser
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
| `SPOOL_DB_HOST` | `localhost` | Database host |
| `SPOOL_DB_PORT` | `5434` | Database port |
| `SPOOL_DB_NAME` | `spool` | Database name |
| `SPOOL_DB_USER` | `spool` | Database user |
| `SPOOL_DB_PASSWORD` | `spool` | Database password |
| `SPOOL_EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | Sentence transformer model |
| `SPOOL_UI_HOST` | `127.0.0.1` | API server host |
| `ANTHROPIC_API_KEY` | - | Anthropic API key (alternative to setting in UI) |

---

## Supported Providers

| Provider | Data Location | Format |
|----------|--------------|--------|
| **Claude Code** | `~/.claude/projects/` | UUID-named JSONL files with conversation history, tool calls, git context |
| **OpenAI Codex CLI** | `~/.codex/sessions/` | `rollout-*.jsonl` files organized by date |
| **GitHub Copilot** | `~/Library/Application Support/Code/User/workspaceStorage/` | Chat session JSON from VS Code |
| **Cursor** | `~/Library/Application Support/Cursor/User/workspaceStorage/` | Chat and composer sessions from SQLite |
| **Windsurf** | `~/Library/Application Support/Windsurf/User/workspaceStorage/` | Chat and Cascade sessions from SQLite |
| **Kiro** | `~/Library/Application Support/Kiro/User/workspaceStorage/` | AWS Kiro chat and agent sessions from SQLite |
| **Google Antigravity** | `~/Library/Application Support/Antigravity/User/workspaceStorage/` | Antigravity chat and agent sessions from SQLite |
| **opencode** | `~/.local/share/opencode/opencode.db` | sst/opencode SQLite database with session/message/part tables (Vercel AI SDK part shape) |

Run `spool init` to see which providers are detected on your system.

No data is sent to external servers. Everything runs locally.
