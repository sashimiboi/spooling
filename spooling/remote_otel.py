"""Ingest OTel / Strands Sessions into Spooling's traces tables.

This is the "remote trace provider" path. It gives Spooling a way to absorb
spans that weren't produced by parsing a provider's on-disk JSONL —
including:

  - live Strands Agent runs (captured via StrandsEvalsTelemetry's in-memory
    exporter, then fed here)
  - OTLP/JSON exports from an OTel collector (map via LangChainOtelSessionMapper
    or OpenInferenceSessionMapper, then feed the resulting Session here)
  - CloudWatch Logs Insights exports (use CloudWatchSessionMapper first)

The core helper is `ingest_strands_session(session, provider_id, project)`
which walks a Strands `Session` object, converts every span into a spool
Span, and writes the whole thing via the same `_store_trace` path that
provider parsers use.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from spooling.db import get_connection
from spooling.ingest import _store_trace, _scrub
from spooling.tracing import (
    Trace,
    Span,
    SpanKind,
    SpanStatus,
    compute_trace_metrics,
)
from spooling.classifiers import classify


def _dt(ts) -> Optional[datetime]:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts
    if isinstance(ts, (int, float)):
        # OTel uses nanoseconds since epoch for ReadableSpan timings.
        if ts > 1e15:
            return datetime.fromtimestamp(ts / 1e9, tz=timezone.utc)
        if ts > 1e12:
            return datetime.fromtimestamp(ts / 1e3, tz=timezone.utc)
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    return None


def session_to_trace(
    session,
    provider_id: str,
    project: Optional[str] = None,
    title: Optional[str] = None,
) -> Trace:
    """Convert a Strands `Session` (from a mapper or in-memory exporter)
    into a Spooling `Trace` ready for `_store_trace`."""
    session_id = getattr(session, "session_id", None) or uuid.uuid4().hex
    trace_id = f"trace-{session_id}"

    spool_trace = Trace(
        id=trace_id,
        session_id=session_id,
        provider_id=provider_id,
        project=project,
        title=title,
    )
    spool_spans: list[Span] = []
    session_root = Span(
        id=f"{trace_id}-root",
        trace_id=trace_id,
        parent_id=None,
        kind=SpanKind.SESSION,
        name=title or f"{provider_id} session",
        depth=0,
        sequence=0,
    )
    spool_spans.append(session_root)
    seq = 1

    # Each Strands Session has a list of Trace objects; each Trace has
    # a list of spans (union of AgentInvocation / Inference / ToolExecution).
    traces = getattr(session, "traces", []) or []
    min_start: Optional[datetime] = None
    max_end: Optional[datetime] = None

    for t in traces:
        for s in getattr(t, "spans", []) or []:
            span_info = getattr(s, "span_info", None)
            span_id = getattr(span_info, "span_id", None) or f"span-{uuid.uuid4().hex[:12]}"
            parent_id = getattr(span_info, "parent_span_id", None) or session_root.id
            started = _dt(getattr(span_info, "start_time", None))
            ended = _dt(getattr(span_info, "end_time", None))
            if started and (min_start is None or started < min_start):
                min_start = started
            if ended and (max_end is None or ended > max_end):
                max_end = ended

            kind_enum = getattr(s, "span_type", None)
            kind_name = getattr(kind_enum, "name", "") or ""

            if kind_name == "TOOL_EXECUTION":
                tool_call = getattr(s, "tool_call", None)
                tool_result = getattr(s, "tool_result", None)
                tool_name = getattr(tool_call, "name", None) or "unknown"
                tool_input = getattr(tool_call, "arguments", None) or {}
                tool_output_text = getattr(tool_result, "content", None) or ""
                err = getattr(tool_result, "error", None)
                status = SpanStatus.ERROR if err else SpanStatus.OK
                cls = classify(tool_name)
                spool_spans.append(Span(
                    id=span_id,
                    trace_id=trace_id,
                    parent_id=parent_id,
                    kind=SpanKind.TOOL,
                    name=f"tool:{tool_name}",
                    status=status,
                    started_at=started,
                    ended_at=ended,
                    depth=1,
                    sequence=seq,
                    tool_name=tool_name,
                    tool_input=tool_input if isinstance(tool_input, dict) else None,
                    tool_output=tool_output_text[:4000] if isinstance(tool_output_text, str) else None,
                    tool_is_error=bool(err),
                    vendor=cls.vendor,
                    category=cls.category,
                ))

            elif kind_name == "INFERENCE":
                meta = getattr(s, "metadata", {}) or {}
                model = meta.get("model") if isinstance(meta, dict) else None
                spool_spans.append(Span(
                    id=span_id,
                    trace_id=trace_id,
                    parent_id=parent_id,
                    kind=SpanKind.LLM_CALL,
                    name="assistant.turn",
                    started_at=started,
                    ended_at=ended,
                    depth=1,
                    sequence=seq,
                    model=model,
                ))

            elif kind_name == "AGENT_INVOCATION":
                user_prompt = getattr(s, "user_prompt", None) or ""
                agent_response = getattr(s, "agent_response", None) or ""
                spool_spans.append(Span(
                    id=span_id,
                    trace_id=trace_id,
                    parent_id=parent_id,
                    kind=SpanKind.AGENT,
                    name="agent",
                    started_at=started,
                    ended_at=ended,
                    depth=1,
                    sequence=seq,
                    agent_type="strands",
                    agent_prompt=str(user_prompt)[:2000] if user_prompt else None,
                    vendor="agent",
                    category="agent",
                    tool_output=str(agent_response)[:2000] if agent_response else None,
                ))

            seq += 1

    if min_start:
        session_root.started_at = min_start
        spool_trace.started_at = min_start
    if max_end:
        session_root.ended_at = max_end
        spool_trace.ended_at = max_end

    spool_trace.root = session_root
    spool_trace.spans = spool_spans

    # Re-parent any span whose parent_id points outside our span set to
    # the session root. OTel parent ids often reference spans from other
    # traces we're not importing; if we leave them dangling Postgres will
    # reject the FK.
    known_ids = {s.id for s in spool_spans}
    for s in spool_spans:
        if s.parent_id and s.parent_id not in known_ids:
            s.parent_id = session_root.id

    return spool_trace


def ingest_strands_session(
    session,
    provider_id: str,
    project: Optional[str] = None,
    title: Optional[str] = None,
) -> str:
    """Convert a Strands Session to a Spooling Trace and persist it. Returns trace_id."""
    trace = session_to_trace(session, provider_id, project=project, title=title)

    # Roll up token/cost metrics. For remote OTel sessions we rarely have
    # usage counts, so metrics are best-effort.
    _ = compute_trace_metrics(trace)

    conn = get_connection()
    try:
        # Upsert a minimal legacy sessions row so the row is reachable via
        # existing session-detail endpoints.
        conn.execute(
            """INSERT INTO sessions (
                id, provider_id, project, started_at, ended_at,
                message_count, tool_call_count, estimated_input_tokens,
                estimated_output_tokens, estimated_cost_usd, title
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                provider_id = EXCLUDED.provider_id,
                started_at = EXCLUDED.started_at,
                ended_at = EXCLUDED.ended_at,
                title = EXCLUDED.title""",
            (
                trace.session_id, trace.provider_id, _scrub(project),
                trace.started_at, trace.ended_at,
                0, 0, 0, 0, 0, _scrub(title),
            ),
        )
        _store_trace(conn, trace)
        conn.commit()
    finally:
        conn.close()
    return trace.id


# --- File-based ingest helpers --------------------------------------------

def ingest_otlp_json_file(path: str, provider_id: str, project: Optional[str] = None) -> str:
    """Ingest an OTLP/JSON spans dump via the LangChain OTel mapper.

    The incoming file should be a JSON export of OTel spans in the standard
    shape produced by `opentelemetry-exporter-otlp-proto-http` dumps. We
    pass it through LangChainOtelSessionMapper since it's the most liberal
    mapper for generic OTel payloads.
    """
    from strands_evals.mappers import LangChainOtelSessionMapper

    data = json.loads(Path(path).read_text())
    mapper = LangChainOtelSessionMapper()
    # Most mappers accept either a list of ReadableSpans or the raw OTLP
    # shape via their `map_to_session` method; we pass whatever's in the
    # file.
    session_id = f"otel-{uuid.uuid4().hex[:10]}"
    session = mapper.map_to_session(data, session_id=session_id)
    return ingest_strands_session(
        session, provider_id=provider_id, project=project,
        title=f"OTLP import from {Path(path).name}",
    )
