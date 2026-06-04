-- Spool observability migration: traces, spans, events, evals.
-- Idempotent; safe to run against an existing database. Does not touch
-- the legacy sessions/messages/tool_calls tables — those keep working.

-- Traces: one per provider session. Lives alongside sessions 1:1 so we can
-- aggregate by trace or join back to the legacy session row.
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
    attrs JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_traces_session ON traces(session_id);
CREATE INDEX IF NOT EXISTS idx_traces_provider ON traces(provider_id);
CREATE INDEX IF NOT EXISTS idx_traces_started ON traces(started_at DESC);

-- Spans: the tree. parent_id references another span (nullable for roots).
-- kind ∈ session | agent | tool | llm_call | eval | step
-- start/end are nullable because some sources don't record end times — we
-- fall back to estimates at query time.
CREATE TABLE IF NOT EXISTS spans (
    id TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL REFERENCES traces(id) ON DELETE CASCADE,
    parent_id TEXT REFERENCES spans(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT DEFAULT 'ok',  -- ok | error | timeout | cancelled
    started_at TIMESTAMPTZ,
    ended_at TIMESTAMPTZ,
    duration_ms BIGINT,
    depth INTEGER DEFAULT 0,
    sequence INTEGER DEFAULT 0,  -- monotonic order within a trace
    -- Token + cost metrics for llm_call spans; rolled up for agent/session.
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    cost_usd NUMERIC(12, 6) DEFAULT 0,
    model TEXT,
    -- Tool-specific
    tool_name TEXT,
    tool_input JSONB,
    tool_output TEXT,
    tool_is_error BOOLEAN,
    -- Agent-specific (e.g., subagent_type from Task input)
    agent_type TEXT,
    agent_prompt TEXT,
    -- Free-form attrs blob (OTel-shaped); anything not hot-querying above.
    attrs JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_spans_trace ON spans(trace_id);
CREATE INDEX IF NOT EXISTS idx_spans_parent ON spans(parent_id);
CREATE INDEX IF NOT EXISTS idx_spans_kind ON spans(kind);
CREATE INDEX IF NOT EXISTS idx_spans_started ON spans(started_at);
CREATE INDEX IF NOT EXISTS idx_spans_tool_name ON spans(tool_name) WHERE kind = 'tool';
CREATE INDEX IF NOT EXISTS idx_spans_agent_type ON spans(agent_type) WHERE kind = 'agent';

-- Events: point-in-time things inside a span (log line, tool_use boundary,
-- user message boundary). Kept narrow so we don't duplicate the full message
-- body here — that still lives in messages/chunks.
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

-- Eval rubrics: catalog of graders we can run over spans/traces.
CREATE TABLE IF NOT EXISTS eval_rubrics (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    kind TEXT NOT NULL,  -- function | llm_judge
    target_kind TEXT NOT NULL DEFAULT 'trace',  -- trace | span
    config JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Eval results: one row per (rubric, span/trace) run.
CREATE TABLE IF NOT EXISTS evals (
    id BIGSERIAL PRIMARY KEY,
    rubric_id TEXT NOT NULL REFERENCES eval_rubrics(id) ON DELETE CASCADE,
    trace_id TEXT REFERENCES traces(id) ON DELETE CASCADE,
    span_id TEXT REFERENCES spans(id) ON DELETE CASCADE,
    score NUMERIC(6, 3),  -- 0..1 canonical, or rubric-defined
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

-- Seed a few rubrics so the UI has something to show on first load.
INSERT INTO eval_rubrics (id, name, description, kind, target_kind, config) VALUES
    ('tool-error-rate',      'Tool error rate',       'Fraction of tool spans that errored.',                             'function', 'trace', '{}'),
    ('agent-success',        'Agent success',         'Did the subagent return without error and produce output?',       'function', 'span',  '{"span_kind":"agent"}'),
    ('llm-judge-helpfulness','LLM judge: helpfulness','LLM grades the assistant turn on a 0-1 helpfulness scale.',       'llm_judge','span',  '{"span_kind":"llm_call"}')
ON CONFLICT (id) DO NOTHING;
