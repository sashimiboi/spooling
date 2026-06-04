CREATE EXTENSION IF NOT EXISTS vector;

-- Sessions parsed from AI coding tools
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    provider_id TEXT,
    project TEXT,
    cwd TEXT,
    git_branch TEXT,
    started_at TIMESTAMPTZ,
    ended_at TIMESTAMPTZ,
    message_count INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0,
    estimated_input_tokens INTEGER DEFAULT 0,
    estimated_output_tokens INTEGER DEFAULT 0,
    estimated_cost_usd REAL DEFAULT 0,
    agent_version TEXT,
    model TEXT,
    title TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_sessions_provider ON sessions(provider_id);
CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at);

-- Individual messages within a session
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    session_id TEXT REFERENCES sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL,  -- user, assistant
    content TEXT,
    timestamp TIMESTAMPTZ,
    tools_used JSONB DEFAULT '[]',
    cwd TEXT,
    git_branch TEXT,
    estimated_tokens INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp);

-- Tool calls extracted from assistant messages
CREATE TABLE IF NOT EXISTS tool_calls (
    id SERIAL PRIMARY KEY,
    session_id TEXT REFERENCES sessions(id) ON DELETE CASCADE,
    message_id TEXT REFERENCES messages(id) ON DELETE CASCADE,
    tool_name TEXT NOT NULL,
    tool_input TEXT,
    tool_result_preview TEXT,
    timestamp TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_tool_calls_session ON tool_calls(session_id);
CREATE INDEX IF NOT EXISTS idx_tool_calls_tool ON tool_calls(tool_name);

-- Embeddings for semantic search
CREATE TABLE IF NOT EXISTS chunks (
    id SERIAL PRIMARY KEY,
    session_id TEXT REFERENCES sessions(id) ON DELETE CASCADE,
    message_id TEXT,
    content TEXT NOT NULL,
    role TEXT,
    project TEXT,
    timestamp TIMESTAMPTZ,
    embedding vector(384),
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_chunks_session ON chunks(session_id);
CREATE INDEX IF NOT EXISTS idx_chunks_embedding ON chunks USING ivfflat (embedding vector_cosine_ops) WITH (lists = 170);

-- Providers / connections for multi-tool support
CREATE TABLE IF NOT EXISTS providers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL,  -- codex, copilot, cursor, windsurf, custom
    status TEXT DEFAULT 'connected',  -- connected, disconnected, error
    data_path TEXT,  -- where session data lives on disk
    icon TEXT,
    config JSONB DEFAULT '{}',
    session_count INTEGER DEFAULT 0,
    last_synced_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Default providers are auto-detected on first run.

-- Chat sessions with the Spool assistant
CREATE TABLE IF NOT EXISTS chat_sessions (
    id TEXT PRIMARY KEY,
    title TEXT,
    model TEXT,
    provider TEXT,
    message_count INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id SERIAL PRIMARY KEY,
    chat_session_id TEXT REFERENCES chat_sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_chat_messages_session ON chat_messages(chat_session_id);

-- Sync state to track what's been ingested
CREATE TABLE IF NOT EXISTS sync_state (
    file_path TEXT PRIMARY KEY,
    provider_id TEXT,
    last_size BIGINT DEFAULT 0,
    last_synced_at TIMESTAMPTZ DEFAULT now()
);

-- ============================================================
-- Observability: traces, spans, events, evals
-- See migrations/001_observability.sql for the canonical source.
-- ============================================================

CREATE TABLE IF NOT EXISTS traces (
    id TEXT PRIMARY KEY,
    session_id TEXT,
    provider_id TEXT NOT NULL,
    project TEXT,
    title TEXT,
    started_at TIMESTAMPTZ,
    ended_at TIMESTAMPTZ,
    duration_ms BIGINT,
    span_count INTEGER DEFAULT 0,
    agent_count INTEGER DEFAULT 0,
    tool_count INTEGER DEFAULT 0,
    llm_count INTEGER DEFAULT 0,
    error_count INTEGER DEFAULT 0,
    total_input_tokens BIGINT DEFAULT 0,
    total_output_tokens BIGINT DEFAULT 0,
    total_cache_read_tokens BIGINT DEFAULT 0,
    total_cache_write_tokens BIGINT DEFAULT 0,
    total_cost_usd NUMERIC(12, 6) DEFAULT 0,
    cwd TEXT,
    git_branch TEXT,
    model TEXT,
    vendor_count INTEGER DEFAULT 0,
    top_vendors JSONB DEFAULT '[]'::jsonb,
    attrs JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_traces_session ON traces(session_id);
CREATE INDEX IF NOT EXISTS idx_traces_provider ON traces(provider_id);
CREATE INDEX IF NOT EXISTS idx_traces_started ON traces(started_at DESC);

CREATE TABLE IF NOT EXISTS spans (
    id TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL REFERENCES traces(id) ON DELETE CASCADE,
    parent_id TEXT REFERENCES spans(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT DEFAULT 'ok',
    started_at TIMESTAMPTZ,
    ended_at TIMESTAMPTZ,
    duration_ms BIGINT,
    depth INTEGER DEFAULT 0,
    sequence INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    cost_usd NUMERIC(12, 6) DEFAULT 0,
    model TEXT,
    tool_name TEXT,
    tool_input JSONB,
    tool_output TEXT,
    tool_is_error BOOLEAN,
    agent_type TEXT,
    agent_prompt TEXT,
    vendor TEXT,
    category TEXT,
    attrs JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_spans_vendor ON spans(vendor) WHERE vendor IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_spans_category ON spans(category) WHERE category IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_spans_trace ON spans(trace_id);
CREATE INDEX IF NOT EXISTS idx_spans_parent ON spans(parent_id);
CREATE INDEX IF NOT EXISTS idx_spans_kind ON spans(kind);
CREATE INDEX IF NOT EXISTS idx_spans_started ON spans(started_at);
CREATE INDEX IF NOT EXISTS idx_spans_tool_name ON spans(tool_name) WHERE kind = 'tool';
CREATE INDEX IF NOT EXISTS idx_spans_agent_type ON spans(agent_type) WHERE kind = 'agent';

CREATE TABLE IF NOT EXISTS span_events (
    id BIGSERIAL PRIMARY KEY,
    span_id TEXT NOT NULL REFERENCES spans(id) ON DELETE CASCADE,
    trace_id TEXT NOT NULL REFERENCES traces(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    timestamp TIMESTAMPTZ,
    attrs JSONB DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_span_events_span ON span_events(span_id);
CREATE INDEX IF NOT EXISTS idx_span_events_trace ON span_events(trace_id);

CREATE TABLE IF NOT EXISTS eval_rubrics (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    kind TEXT NOT NULL,
    target_kind TEXT NOT NULL DEFAULT 'trace',
    config JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS evals (
    id BIGSERIAL PRIMARY KEY,
    rubric_id TEXT NOT NULL REFERENCES eval_rubrics(id) ON DELETE CASCADE,
    trace_id TEXT REFERENCES traces(id) ON DELETE CASCADE,
    span_id TEXT REFERENCES spans(id) ON DELETE CASCADE,
    score NUMERIC(6, 3),
    passed BOOLEAN,
    label TEXT,
    rationale TEXT,
    judge_model TEXT,
    judge_cost_usd NUMERIC(12, 6) DEFAULT 0,
    run_at TIMESTAMPTZ DEFAULT now(),
    attrs JSONB DEFAULT '{}'::jsonb,
    CONSTRAINT evals_target_check CHECK (trace_id IS NOT NULL OR span_id IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_evals_rubric ON evals(rubric_id);
CREATE INDEX IF NOT EXISTS idx_evals_trace ON evals(trace_id);
CREATE INDEX IF NOT EXISTS idx_evals_span ON evals(span_id);
CREATE INDEX IF NOT EXISTS idx_evals_run_at ON evals(run_at DESC);

INSERT INTO eval_rubrics (id, name, description, kind, target_kind, config) VALUES
    ('tool-error-rate',      'Tool error rate',       'Fraction of tool spans that errored.',                             'function', 'trace', '{}'),
    ('agent-success',        'Agent success',         'Did the subagent return without error and produce output?',       'function', 'span',  '{"span_kind":"agent"}'),
    ('llm-judge-helpfulness','LLM judge: helpfulness','LLM grades the assistant turn on a 0-1 helpfulness scale.',       'llm_judge','span',  '{"span_kind":"llm_call"}')
ON CONFLICT (id) DO NOTHING;
