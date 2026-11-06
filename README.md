# Spooling

Local session tracker and semantic search for AI coding assistants.

Track your AI coding sessions across **Claude Code**, **OpenAI Codex CLI**, **GitHub Copilot**, **Cursor**, **Windsurf**, **Kiro**, **Google Antigravity**, and **opencode**, all in one place. Get usage stats, cost estimates, per-provider breakdowns, semantic search, and a built-in AI chat agent to explore your history.

**Website:** [spooling.ai](https://spooling.ai)

---

## Prerequisites

- **Python 3.11+**
- **pipx** or **uv** (optional, alternative install methods)
- **Ollama** (optional, for free local AI chat) or an **Anthropic API key**

No Docker, no PostgreSQL, no Node.js required.

---

## How it works

**Three steps. Local by default.**

### 01 &nbsp; Install

```bash
# pip
pip install spooling

# pipx (recommended — keeps it isolated)
pipx install spooling

# uv
uv tool install spooling
```

### 02 &nbsp; Detect providers & sync

```bash
spooling init      # scan for available providers
spooling sync      # embed every session for semantic search
```

### 03 &nbsp; Search & explore

```bash
spooling search "that redis race condition"
spooling session list                      # find a session and its id
spooling session show ses_e6310bd1 -m      # read that session's full text
spooling stats
spooling mcp       # MCP server at http://127.0.0.1:3004/mcp
```

---

## Connect Spooling to your AI coding agent

`spooling mcp` exposes an MCP server at `http://127.0.0.1:3004/mcp` (HTTP streamable transport). Any MCP-speaking agent can connect to it and pull context from your local KB mid-conversation. The agent gets tools like `search_sessions` (find a session), `get_session` (read its full conversation by id), `get_stats`, and `list_traces`. See [Tools exposed](#tools-exposed) for the full list.

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

Should return the tools listed under [Tools exposed](#tools-exposed). If you get "connection refused", `spooling mcp` (or `spooling ui`) is not running, or something else is using port 3004. Find it with `lsof -i :3004`.

---

## CLI Usage

### `spooling init`

Check that the local database opens and show which AI coding tool providers are detected on your system. This scans the default paths for every supported provider (see [Supported Providers](#supported-providers)).

```bash
spooling init
```

### `spooling sync`

Parse and ingest sessions from all connected providers into the local database. Chunks and embeds message content for semantic search.

```bash
spooling sync                      # Full sync with embeddings
spooling sync --no-embed           # Skip embeddings (faster initial sync)
spooling sync -p cursor            # Sync only Cursor sessions
spooling sync -p codex             # Sync only Codex CLI sessions
spooling sync -p claude-code       # Sync only Claude Code sessions
```

### `spooling stats`

Show usage statistics - sessions, messages, tool calls, tokens, costs, broken down by project and day.

```bash
spooling stats             # Overview + last 7 days
spooling stats --week      # Weekly breakdown
spooling stats --days 30   # Last 30 days
```

### `spooling tools`, `spooling agents`, `spooling mcps`, `spooling traces`

See which tools, MCP servers and agents your sessions use. These read the spans stored by `spooling sync`, and use the same names as the web app (an agent is its subagent type, falling back to its span name; an MCP tool is named `mcp__<server>__<tool>`).

```bash
spooling tools                       # Top tools: uses, traces, errors, avg time
spooling tools -s read               # Tools whose name contains "read"
spooling tools --agent explore       # Tools called by the "explore" agent
spooling tools --mcp github          # Tools of MCP servers matching "github"

spooling mcps                        # Every MCP server used: tools, calls, traces, errors
spooling mcps spool                  # The tools of servers matching "spool"

spooling agents                      # Agents: runs, traces, tokens, cost
spooling agents -s expl              # Agents whose name contains "expl"
spooling agents --tool Bash          # Agents whose runs called Bash
spooling agents --mcp github         # Agents whose runs used a GitHub MCP tool

spooling traces                      # Latest traces, with the agents and MCP servers each used
spooling traces ses_10dd             # Search title, project, session id, trace id, agent or MCP server name
spooling traces --agent explore --tool Bash --mcp spool --days 30
```

Common options: `--days N`, `--project TEXT`, `--provider/-p NAME`, `--limit/-n N`, and `--json` for scripting.

Matching rules: `--agent` and `--tool` match the exact name, ignoring case, and can be repeated (matches any). `--mcp` and `--search/-s` match part of the name, ignoring case. When you combine different filters on `traces`, a trace must satisfy all of them.

`tools --agent` and `agents --tool/--mcp` look inside an agent's own runs. `traces --agent/--tool/--mcp` matches whole traces, like the Traces page.

`--cloud` on `tools`, `agents` and `mcps` shows workspace-wide totals from Spooling Cloud. The cloud endpoints only return the 100 most-used tools, so `--search` and `--mcp` filter those client-side, and `--days/--project/--provider/--agent/--tool` can't be combined with `--cloud`.

Agent data is only recorded for providers that expose subagents (currently Claude Code, via its `Agent` tool, previously called `Task`), so `spooling agents` is empty for other providers. MCP calls are recognised by the `mcp__<server>__<tool>` name Claude Code uses; tools from other clients that name MCP tools differently (for example `server_tool`) appear in `spooling tools` but not in `spooling mcps`.

After upgrading, `spooling sync` re-reads your Claude Code sessions once to pick up agents. To force a re-read of any provider's files at any time, run `spooling sync --reparse` (optionally with `-p claude-code`).

Agent runs show duration and the prompt that started them. Their own tokens, cost and tool calls are not attributed yet, because Claude Code writes subagent activity to separate transcript files that Spooling does not read yet.

### `spooling search <query>`

Semantic search across all your session history using natural language. Each result shows the session title **and its session id**, so you can open it with `spooling session show`.

```bash
spooling search "snowflake connector"
spooling search "authentication bug" -n 5
spooling search "database migration" -p myproject
spooling search ses_e6310bd1                # a session id (or prefix) matches that session first
spooling search "login issue" --cloud       # search your cloud workspace instead
```

Session ids work in the query: a full id, a prefix such as `ses_e6310bd1`, or a UUID/hex prefix of 8+ characters. The matching session is ranked first and labelled `(id match)`. `--cloud` search is matched by the server, so ids are shown but not ranked first.

Options:
- `-n, --limit` - Number of results (default: 10)
- `-p, --project` - Filter by exact project name (see `spooling session list`)
- `--cloud` - Search the cloud workspace instead of the local database

### `spooling session`

List sessions and read one back by id.

```bash
spooling session list                         # newest first, with full session ids
spooling session list --title login -n 10     # filter by title substring
spooling session list --id e6310bd1           # filter by id substring
spooling session list --project myproject --cwd ~/code

spooling session show ses_e6310bd1            # summary: model, tokens, cost, tools used
spooling session show ses_e6310bd1 -m         # plus the full conversation text
spooling session show ses_e6310bd1 -m --max-chars 300   # truncate each message
```

`spooling session <id> -m` is shorthand for `spooling session show <id> -m`. `session show` accepts a full session id or any unique prefix. If a prefix matches several sessions, it lists them and exits so you can pick one.

### `spooling optimize`

Compact the local database. Removes duplicated tool-call rows left by older versions (before 0.2.6), truncates oversized previews, embeds any messages missing from search, and runs `VACUUM`. A backup is written next to the database first, and it is safe to re-run. `spooling sync` prints a hint when it finds duplicates.

```bash
spooling optimize
spooling optimize --no-backup --no-vacuum --no-embed   # skip individual steps
```

### `spooling watch`

Watch all connected provider directories for new session data and auto-sync in real time.

```bash
spooling watch
```

### `spooling serve`

Start the API server.

```bash
spooling serve                    # Default: http://127.0.0.1:3002
spooling serve --port 8080        # Custom port
spooling serve --host 0.0.0.0     # Bind to all interfaces
```

### `spooling ui`

Start the API server, the MCP HTTP server, and the Next.js UI together. Requires the `ui/` directory from the [source repo](https://github.com/sashimiboi/spooling) to be present. Set `SPOOLING_UI_DIR` to point at it if running from a non-standard location.

```bash
spooling ui        # API :3002 · MCP :3004 · GUI :3003
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

### `spooling tunnel`

Create a Cloudflare quick tunnel (no account required) to expose your local MCP server to the internet. Once started, it prints a public HTTPS URL you can drop straight into any remote MCP client config. Requires [`cloudflared`](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/) to be installed.

```bash
spooling tunnel                    # Expose Spooling MCP (port 3004, default)
spooling tunnel --port 8090        # Expose a custom local port
spooling tunnel -p 3000 -n my-api  # Named tunnel on port 3000
```

Options:
- `-p, --port` - Local port to expose (default: 3004)
- `-n, --name` - Label for display purposes

After the tunnel starts, it prints the public URL and an MCP config snippet ready to paste:

```json
{"mcpServers": {"spooling": {"type": "http", "url": "https://<random>.trycloudflare.com/mcp"}}}
```

Install `cloudflared` if not already present:

```bash
# macOS
brew install cloudflared

# Linux
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
  -o /usr/local/bin/cloudflared && chmod +x /usr/local/bin/cloudflared

# Windows
winget install cloudflare.cloudflared
```

### `spooling cost`

Detailed cost tracking and spend analysis. All subcommands read from the local database — no network calls.

```bash
spooling cost overview                          # Total spend: input/output/cache breakdown
spooling cost overview --provider cursor        # Filter to one provider
spooling cost overview --days 7                 # Last 7 days only

spooling cost breakdown                         # Cost by provider (default)
spooling cost breakdown --by model              # Cost by model
spooling cost breakdown --by project            # Cost by project
spooling cost breakdown --by model --days 30    # Model breakdown, last 30 days

spooling cost daily                             # Daily trend (last 30 days)
spooling cost daily --days 14                   # Last 14 days
spooling cost daily --provider codex            # Filter by provider

spooling cost monthly                           # Monthly trend (last 12 months)
spooling cost monthly --months 6                # Last 6 months

spooling cost session <session-id>              # Detailed cost for a single session

spooling cost recalc                            # Dry-run: re-price all sessions against current rates
spooling cost recalc --apply                    # Actually persist the repriced values
spooling cost recalc --session <id> --apply     # Reprice one session
spooling cost recalc --provider cursor --apply  # Reprice all sessions for a provider
```

### `spooling eval`

Run LLM-as-judge rubrics against your session traces.

```bash
spooling eval list                              # List all configured rubrics
spooling eval run --rubric <id> --trace <id>    # Score one trace
spooling eval run --rubric <id> --days 7        # Batch-score traces from the last 7 days
```

### `spooling experiment`

Create and run Strands-style experiments — a set of test cases evaluated by one or more rubrics.

```bash
spooling experiment list                        # List all experiments
spooling experiment create --file spec.json     # Register an experiment from a JSON spec
spooling experiment run --id <experiment-id>    # Run an experiment and persist the report
spooling experiment show --run <run-id>         # Show the results of a past run
```

### `spooling otel`

Ingest OpenTelemetry / Strands span exports from external sources into Spooling.

```bash
spooling otel ingest --file spans.json                         # Ingest an OTLP JSON export
spooling otel ingest --file spans.json --provider my-agent     # Tag with a custom provider id
spooling otel ingest --file spans.json --project myproject     # Tag with a project name
```

### `spooling pricing`

Manage the LiteLLM-backed model pricing table used for cost calculations.

```bash
spooling pricing show                           # Show cache status and source info
spooling pricing show claude-sonnet-4-5         # Show per-component rates for a model ($/Mtok)
spooling pricing refresh                        # Force-fetch latest rates into ~/.spool/model_prices.json
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
spooling push                 # syncs local sessions first, then pushes 100 in batches of 20
spooling push --limit 500     # bigger backfill
spooling push --no-sync       # push only what is already in the local database
spooling push --dry-run       # show what would be pushed
```

`push` ingests new provider sessions into the local database before uploading, so you don't need to run `spooling sync` first. The server upserts by session id, so re-running is safe.

### Push only some sessions, or to a specific team

**Which team gets the sessions** is decided by the API key you are logged in with. Each key belongs to one workspace (personal or a team), and `spooling push` sends to that workspace. Mint a key for the team in the app, then log in with it:

```bash
spooling cloud login --key sk_<team-key>     # replaces the stored key; later pushes go to this team
spooling cloud status                        # session/cost totals of the workspace this key belongs to
```

To push to a different team, log in with that team's key. Only one key is stored at a time. `cloud status` doesn't print the workspace name, so compare its totals with what the team shows in the app.

**Which sessions go up.** With no filters, `spooling push` sends the most recent `--limit` sessions from every project on the machine. Use these to send only some of them. Filters combine with AND:

| Option | Matches |
|---|---|
| `--project NAME` | Sessions whose project name is exactly `NAME` (find names with `spooling cost breakdown --by project`) |
| `--cwd TEXT` | Sessions whose working directory contains `TEXT` (case-insensitive) |
| `--title TEXT` | Sessions whose title contains `TEXT` (case-insensitive). Useful when an agent was launched from your home directory, so every session shares a generic working directory, but the project name is in the title |

```bash
spooling cloud login --key sk_<team-key>
spooling push --cwd toebox --dry-run     # preview what would be sent; nothing is uploaded
spooling push --cwd toebox               # send only sessions from the toebox directory
spooling push --project my-app --limit 200
spooling push --title islet              # sessions with "islet" in the title
```

**Share the same sessions with more than one team.** Add `--copy` to write the sessions into the workspace you are logged into as fresh rows, even if they were already pushed to another workspace. Re-running is safe:

```bash
spooling cloud login --key sk_<other-team-key>
spooling push --cwd toebox --copy
```

**Include trace data.** Add `--with-spans` to also push each session's trace and spans (per-LLM-call usage, cost, tool calls, MCP calls and agent runs). The Traces page in the app, its agent, tool and MCP filters, and `spooling agents --cloud` / `spooling mcps --cloud` all rely on it. `--with-spans` can't be combined with `--copy` yet.

**Secrets** (cloud and AWS keys, GitHub, OpenAI, Anthropic and Stripe tokens, PEM private keys, and `KEY=VALUE` lines whose key looks sensitive) are redacted on your machine before anything is uploaded. Pass `--no-redact` only if you are sure the content is clean.

`spooling cloud watch` has no filters: it keeps pushing every new or updated session to the workspace you are logged into. Use filtered `spooling push` runs when you need to keep some sessions out.

To bring sessions from the cloud back down, use `spooling cloud pull`, or `spooling cloud sync` for both directions.

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

# Search (matches session titles and ids)
curl -s "https://api.spooling.ai/v1/search?q=migration&limit=10" \
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
| `search_sessions` | Semantic search over embedded session chunks. Results include `session_id`; a session id in the query matches that session first |
| `get_session` | Full detail for one session by id or unique prefix, including the conversation text (`include_messages`, `max_messages`, `max_chars`) |
| `get_stats` | Top-line stats: traces, tokens, cost, errors |
| `get_top_vendors` | Most-used external vendors by tool-call count |
| `list_evals` | Recent eval runs, optionally filtered by rubric |
| `list_rubrics` | All configured Strands eval rubrics |
| `run_eval` | Run a rubric against a trace and persist the result |

### Stdio (legacy clients)

For MCP clients that only speak stdio, run `spooling mcp --stdio` and register
it with the command-based form:

```json
{
  "mcpServers": {
    "spooling": {
      "command": "spooling",
      "args": ["mcp", "--stdio"]
    }
  }
}
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

The chat page lets you ask questions about your coding sessions in natural language. It uses RAG — retrieves relevant context from the local database before answering.

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
├── pyproject.toml           # Python package config
├── spooling/                # Python backend
│   ├── cli.py               # Click CLI
│   ├── config.py            # Configuration
│   ├── db.py                # SQLite connection (PostgreSQL shim)
│   ├── schema.py            # SQLite schema DDL
│   ├── providers/           # Provider plugins (codex, copilot, cursor, windsurf, ...)
│   ├── parser.py            # Session JSONL parser
│   ├── embeddings.py        # sentence-transformers (all-MiniLM-L6-v2)
│   ├── ingest.py            # Sync pipeline
│   ├── search.py            # Numpy cosine similarity search (+ session-id matching)
│   ├── sessions.py          # Session id lookup helpers
│   ├── optimize.py          # Database dedupe / compaction
│   ├── stats.py             # Usage statistics
│   ├── watcher.py           # File watcher (watchdog)
│   ├── agent.py             # Chat agent (Ollama + Anthropic)
│   ├── tunnel.py            # Cloudflare quick tunnel support
│   └── server.py            # FastAPI API server
└── ui/                      # Next.js frontend (source repo only)
    ├── next.config.js       # API proxy to :3002
    └── src/
        ├── components/      # shadcn/ui components
        ├── lib/             # API helpers
        └── app/(app)/       # Pages (dashboard, sessions, search, etc.)
```

### Stack

| Layer | Technology |
|-------|-----------|
| Database | SQLite (no external dependencies) |
| Embeddings | sentence-transformers / all-MiniLM-L6-v2 (local) |
| Search | numpy cosine similarity |
| Backend | Python, FastAPI, Click |
| Frontend | Next.js 14, shadcn/ui, Tailwind CSS, AG Charts |
| Chat AI | Ollama (local) or Anthropic API |

### Ports

| Service | Port |
|---------|------|
| API Server | 3002 |
| GUI | 3003 |
| MCP Server (streamable-HTTP) | 3004 |

---

## Environment Variables

All optional — defaults work out of the box.

| Variable | Default | Description |
|----------|---------|-------------|
| `SPOOLING_DB` | `~/.local/share/spooling/spooling.db` | Path to the SQLite database file (`%APPDATA%\spooling\spooling.db` on Windows) |
| `SPOOLING_EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | Sentence transformer model |
| `SPOOLING_UI_HOST` | `127.0.0.1` | API server host |
| `SPOOLING_UI_DIR` | *(auto-detected)* | Path to the Next.js `ui/` directory (required for `spooling ui`) |
| `ANTHROPIC_API_KEY` | - | Anthropic API key (alternative to setting in the GUI) |

---

## Supported Providers

| Provider | Data Location | Format |
|----------|--------------|--------|
| **Claude Code** | `~/.claude/projects/` (or `$CLAUDE_CONFIG_DIR/projects`) | `<project>/<session-uuid>.jsonl` transcripts with per-message token usage, prompt-cache reads/writes, tool calls and git context |
| **JSONL Sessions** | `~/.sessions/projects/` | UUID-named JSONL files with conversation history, tool calls, git context |
| **OpenAI Codex CLI** | `~/.codex/sessions/` | `rollout-*.jsonl` files organized by date |
| **GitHub Copilot** | `~/Library/Application Support/Code/User/workspaceStorage/` | Chat session JSON from VS Code |
| **Cursor** | `~/Library/Application Support/Cursor/User/workspaceStorage/` | Chat and composer sessions from SQLite |
| **Windsurf** | `~/Library/Application Support/Windsurf/User/workspaceStorage/` | Chat and Cascade sessions from SQLite |
| **Kiro** | `~/Library/Application Support/Kiro/User/workspaceStorage/` | AWS Kiro chat and agent sessions from SQLite |
| **Google Antigravity** | `~/Library/Application Support/Antigravity/User/workspaceStorage/` | Antigravity chat and agent sessions from SQLite |
| **opencode** | `~/.local/share/opencode/opencode.db` | sst/opencode SQLite database with session/message/part tables (Vercel AI SDK part shape) |

Run `spooling init` to see which providers are detected on your system.

**Claude Code notes.** Sessions are detected automatically; there is nothing to connect. Cost is computed from each message's real token usage (including prompt-cache reads and writes) at the model's list price, so it reflects API-equivalent spend, not what a subscription plan charges. The project name is the session's working directory. To read transcripts from somewhere else, set `CLAUDE_CONFIG_DIR` or add `{"claude-code": "/path/to/projects"}` to `~/.config/spooling/paths.json`. Subagent transcripts (stored in separate files under `<session>/subagents/` by recent Claude Code versions) are not included yet.

Nothing leaves your machine unless you opt in: Spooling Cloud (`spooling push`), a tunnel (`spooling tunnel`), or the Anthropic chat provider. `spooling pricing refresh` downloads public model prices but sends none of your data.

