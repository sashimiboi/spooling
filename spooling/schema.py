"""SQLite schema DDL for Spooling.

All tables are created with IF NOT EXISTS so this is safe to run on every
startup — it's a no-op when the schema is already present.
"""

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL,
    project TEXT,
    cwd TEXT,
    git_branch TEXT,
    started_at TEXT,
    ended_at TEXT,
    message_count INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0,
    estimated_input_tokens INTEGER DEFAULT 0,
    estimated_output_tokens INTEGER DEFAULT 0,
    estimated_cost_usd REAL DEFAULT 0,
    agent_version TEXT,
    model TEXT,
    title TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_provider ON sessions(provider_id);
CREATE INDEX IF NOT EXISTS idx_sessions_started_at ON sessions(started_at);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role TEXT,
    content TEXT,
    timestamp TEXT,
    tools_used TEXT DEFAULT '[]',
    cwd TEXT,
    git_branch TEXT,
    estimated_tokens INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);

CREATE TABLE IF NOT EXISTS tool_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    message_id TEXT,
    tool_name TEXT,
    tool_input TEXT,
    tool_result_preview TEXT,
    timestamp TEXT
);

CREATE INDEX IF NOT EXISTS idx_tool_calls_session ON tool_calls(session_id);
CREATE INDEX IF NOT EXISTS idx_tool_calls_tool_name ON tool_calls(tool_name);

-- Embedding stored as packed float32 BLOB (384 floats = 1536 bytes).
-- Search uses numpy brute-force cosine similarity — fast enough for
-- local datasets (typically < 50k chunks) and needs no extra deps.
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    message_id TEXT,
    content TEXT,
    role TEXT,
    project TEXT,
    timestamp TEXT,
    embedding BLOB
);

CREATE INDEX IF NOT EXISTS idx_chunks_session ON chunks(session_id);
CREATE INDEX IF NOT EXISTS idx_chunks_project ON chunks(project);

CREATE TABLE IF NOT EXISTS sync_state (
    file_path TEXT PRIMARY KEY,
    last_size INTEGER,
    provider_id TEXT,
    last_synced_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS providers (
    id TEXT PRIMARY KEY,
    name TEXT,
    type TEXT,
    data_path TEXT,
    icon TEXT,
    config TEXT DEFAULT '{}',
    status TEXT DEFAULT 'connected',
    session_count INTEGER DEFAULT 0,
    last_synced_at TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS traces (
    id TEXT PRIMARY KEY,
    session_id TEXT REFERENCES sessions(id) ON DELETE SET NULL,
    provider_id TEXT,
    project TEXT,
    title TEXT,
    started_at TEXT,
    ended_at TEXT,
    duration_ms INTEGER DEFAULT 0,
    span_count INTEGER DEFAULT 0,
    agent_count INTEGER DEFAULT 0,
    tool_count INTEGER DEFAULT 0,
    llm_count INTEGER DEFAULT 0,
    error_count INTEGER DEFAULT 0,
    total_input_tokens INTEGER DEFAULT 0,
    total_output_tokens INTEGER DEFAULT 0,
    total_cache_read_tokens INTEGER DEFAULT 0,
    total_cache_write_tokens INTEGER DEFAULT 0,
    total_cost_usd REAL DEFAULT 0,
    cwd TEXT,
    git_branch TEXT,
    model TEXT,
    vendor_count INTEGER DEFAULT 0,
    top_vendors TEXT DEFAULT '[]',
    attrs TEXT DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_traces_session ON traces(session_id);
CREATE INDEX IF NOT EXISTS idx_traces_provider ON traces(provider_id);
CREATE INDEX IF NOT EXISTS idx_traces_started_at ON traces(started_at);

CREATE TABLE IF NOT EXISTS spans (
    id TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL REFERENCES traces(id) ON DELETE CASCADE,
    parent_id TEXT,
    kind TEXT,
    name TEXT,
    status TEXT DEFAULT 'ok',
    started_at TEXT,
    ended_at TEXT,
    duration_ms INTEGER DEFAULT 0,
    depth INTEGER DEFAULT 0,
    sequence INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0,
    model TEXT,
    tool_name TEXT,
    tool_input TEXT,
    tool_output TEXT,
    tool_is_error INTEGER DEFAULT 0,
    agent_type TEXT,
    agent_prompt TEXT,
    vendor TEXT,
    category TEXT,
    attrs TEXT DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_spans_trace ON spans(trace_id);
CREATE INDEX IF NOT EXISTS idx_spans_kind ON spans(kind);

CREATE TABLE IF NOT EXISTS span_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    span_id TEXT NOT NULL REFERENCES spans(id) ON DELETE CASCADE,
    trace_id TEXT,
    name TEXT,
    timestamp TEXT,
    attrs TEXT DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_span_events_span ON span_events(span_id);

CREATE TABLE IF NOT EXISTS eval_rubrics (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT,
    target_kind TEXT,
    description TEXT,
    config TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS evals (
    id TEXT PRIMARY KEY,
    rubric_id TEXT REFERENCES eval_rubrics(id) ON DELETE SET NULL,
    trace_id TEXT REFERENCES traces(id) ON DELETE CASCADE,
    span_id TEXT,
    score REAL,
    passed INTEGER,
    label TEXT,
    rationale TEXT,
    judge_model TEXT,
    run_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_evals_trace ON evals(trace_id);
CREATE INDEX IF NOT EXISTS idx_evals_rubric ON evals(rubric_id);
CREATE INDEX IF NOT EXISTS idx_evals_run_at ON evals(run_at);

CREATE TABLE IF NOT EXISTS experiments (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    cases TEXT DEFAULT '[]',
    evaluators TEXT DEFAULT '[]',
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS experiment_runs (
    id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
    status TEXT DEFAULT 'running',
    started_at TEXT DEFAULT (datetime('now')),
    finished_at TEXT,
    error TEXT,
    reports TEXT DEFAULT '[]',
    overall_scores TEXT DEFAULT '{}',
    created_trace_ids TEXT DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_experiment_runs_exp ON experiment_runs(experiment_id);

CREATE TABLE IF NOT EXISTS chat_sessions (
    id TEXT PRIMARY KEY,
    title TEXT,
    model TEXT,
    provider TEXT,
    message_count INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_session_id TEXT NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    role TEXT,
    content TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_chat_messages_session ON chat_messages(chat_session_id);

CREATE TABLE IF NOT EXISTS mcp_connectors (
    id TEXT PRIMARY KEY,
    name TEXT,
    url TEXT,
    auth_header TEXT,
    transport TEXT DEFAULT 'http',
    status TEXT DEFAULT 'disconnected',
    last_error TEXT,
    last_checked_at TEXT,
    tools_json TEXT DEFAULT '[]',
    tool_count INTEGER DEFAULT 0,
    slug TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS data_sources (
    id TEXT PRIMARY KEY,
    type TEXT,
    name TEXT,
    credentials TEXT DEFAULT '{}',
    config TEXT DEFAULT '{}',
    status TEXT DEFAULT 'connected',
    last_error TEXT,
    last_synced_at TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS schema_cache (
    data_source_type TEXT PRIMARY KEY,
    schema_data TEXT,
    cached_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS saved_queries (
    id TEXT PRIMARY KEY,
    name TEXT,
    sql_text TEXT,
    description TEXT,
    data_source_type TEXT,
    folder_id TEXT REFERENCES query_folders(id) ON DELETE SET NULL,
    result_data TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS query_folders (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS dashboards (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    folder_id TEXT,
    user_id TEXT,
    workspace_id TEXT,
    widgets TEXT DEFAULT '[]',
    layouts TEXT DEFAULT '[]',
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS dashboard_folders (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    user_id TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
"""
