"""Spooling Python SDK — push traces from any third-party agent.

Usage:

    from spooling.sdk import SpoolTracer

    tracer = SpoolTracer(
        provider_id="my-langchain-agent",
        session_id="run-123",
        project="my-app",
        ingest_url="http://localhost:3002/api/traces/ingest",  # or None for direct DB
    )

    with tracer.session(name="Customer onboarding flow") as sess:
        with tracer.agent(name="triage", agent_type="triage", parent=sess) as ag:
            with tracer.llm_call(name="classify", model="gemma3:4b", parent=ag) as llm:
                llm.record_usage(input_tokens=420, output_tokens=35, cost_usd=0.001)
            with tracer.tool(name="create issue", tool_name="mcp__linear__create_issue",
                             parent=ag, tool_input={"title": "New user signup"}) as t:
                t.record_output("LIN-123", is_error=False)

    tracer.flush()   # POSTs /api/traces/ingest (or commits to local DB)

Context managers auto-time spans and handle parent chains via the stack. The
`parent` argument is optional — if omitted, the innermost open span is used.
Exceptions inside a with-block mark the span as errored.
"""

from __future__ import annotations

import json
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

from spooling.classifiers import classify
from spooling.tracing import Span, SpanKind, SpanStatus, Trace


class _SpanHandle:
    """Thin wrapper exposing record_usage / record_output on an open span."""

    def __init__(self, span: Span):
        self._span = span

    @property
    def id(self) -> str:
        return self._span.id

    @property
    def span(self) -> Span:
        return self._span

    def record_usage(
        self,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        cost_usd: float = 0.0,
        model: Optional[str] = None,
    ) -> None:
        self._span.input_tokens += input_tokens
        self._span.output_tokens += output_tokens
        self._span.cache_read_tokens += cache_read_tokens
        self._span.cache_write_tokens += cache_write_tokens
        self._span.cost_usd += cost_usd
        if model:
            self._span.model = model

    def record_output(
        self,
        output: Any,
        is_error: bool = False,
    ) -> None:
        if not isinstance(output, str):
            try:
                output = json.dumps(output, default=str)
            except Exception:
                output = str(output)
        self._span.tool_output = output[:4000]
        self._span.tool_is_error = is_error
        if is_error:
            self._span.status = SpanStatus.ERROR

    def set_attr(self, key: str, value: Any) -> None:
        self._span.attrs[key] = value


class SpoolTracer:
    """Build a Trace in memory and flush it to Spooling's ingest endpoint."""

    def __init__(
        self,
        provider_id: str,
        session_id: Optional[str] = None,
        project: Optional[str] = None,
        title: Optional[str] = None,
        cwd: Optional[str] = None,
        git_branch: Optional[str] = None,
        model: Optional[str] = None,
        ingest_url: Optional[str] = None,
        trace_id: Optional[str] = None,
    ):
        self.provider_id = provider_id
        self._session_id = session_id or f"sdk-{uuid.uuid4().hex[:12]}"
        self._trace_id = trace_id or f"trace-{self._session_id}"

        self.trace = Trace(
            id=self._trace_id,
            session_id=self._session_id,
            provider_id=provider_id,
            project=project,
            title=title,
            cwd=cwd,
            git_branch=git_branch,
            model=model,
        )
        self._stack: list[Span] = []
        self._seq = 0
        self.ingest_url = ingest_url or os.environ.get(
            "SPOOLING_INGEST_URL", "http://127.0.0.1:3002/api/traces/ingest"
        )
        self._flushed = False

    # --- internal helpers -------------------------------------------------

    def _next_seq(self) -> int:
        s = self._seq
        self._seq += 1
        return s

    def _make_span(
        self,
        kind: SpanKind,
        name: str,
        parent: Optional[Span],
        **kwargs,
    ) -> Span:
        parent_id = None
        depth = 0
        if parent is not None:
            parent_id = parent.id
            depth = parent.depth + 1
        elif self._stack:
            parent_id = self._stack[-1].id
            depth = self._stack[-1].depth + 1

        now = datetime.now(timezone.utc)
        span = Span(
            id=f"span-{uuid.uuid4().hex[:16]}",
            trace_id=self._trace_id,
            parent_id=parent_id,
            kind=kind,
            name=name,
            started_at=now,
            depth=depth,
            sequence=self._next_seq(),
            **kwargs,
        )
        self.trace.spans.append(span)
        if kind == SpanKind.SESSION and self.trace.root is None:
            self.trace.root = span
            if self.trace.started_at is None:
                self.trace.started_at = now
        self._stack.append(span)
        return span

    def _close_span(self, span: Span, error: Optional[BaseException] = None) -> None:
        span.ended_at = datetime.now(timezone.utc)
        if error is not None:
            span.status = SpanStatus.ERROR
            span.attrs.setdefault("error", f"{type(error).__name__}: {error}")
        # Pop from stack — tolerate out-of-order closes by removing target span.
        if self._stack and self._stack[-1].id == span.id:
            self._stack.pop()
        else:
            try:
                self._stack.remove(span)
            except ValueError:
                pass

    # --- context managers -------------------------------------------------

    @contextmanager
    def session(self, name: str, **attrs) -> Iterator[_SpanHandle]:
        span = self._make_span(SpanKind.SESSION, name, None, attrs=dict(attrs))
        try:
            yield _SpanHandle(span)
        except BaseException as e:
            self._close_span(span, e)
            raise
        else:
            self._close_span(span)

    @contextmanager
    def agent(
        self,
        name: str,
        agent_type: Optional[str] = None,
        agent_prompt: Optional[str] = None,
        parent: Optional[_SpanHandle | Span] = None,
        **attrs,
    ) -> Iterator[_SpanHandle]:
        parent_span = getattr(parent, "span", parent) if parent else None
        span = self._make_span(
            SpanKind.AGENT, name, parent_span,
            agent_type=agent_type,
            agent_prompt=agent_prompt,
            vendor="agent",
            category="agent",
            attrs=dict(attrs),
        )
        try:
            yield _SpanHandle(span)
        except BaseException as e:
            self._close_span(span, e)
            raise
        else:
            self._close_span(span)

    @contextmanager
    def tool(
        self,
        name: str,
        tool_name: str,
        tool_input: Optional[dict] = None,
        parent: Optional[_SpanHandle | Span] = None,
        vendor: Optional[str] = None,
        category: Optional[str] = None,
        **attrs,
    ) -> Iterator[_SpanHandle]:
        parent_span = getattr(parent, "span", parent) if parent else None
        cls = classify(tool_name)
        span = self._make_span(
            SpanKind.TOOL, name, parent_span,
            tool_name=tool_name,
            tool_input=tool_input,
            vendor=vendor or cls.vendor,
            category=category or cls.category,
            attrs=dict(attrs),
        )
        try:
            yield _SpanHandle(span)
        except BaseException as e:
            self._close_span(span, e)
            raise
        else:
            self._close_span(span)

    @contextmanager
    def llm_call(
        self,
        name: str = "assistant.turn",
        model: Optional[str] = None,
        parent: Optional[_SpanHandle | Span] = None,
        **attrs,
    ) -> Iterator[_SpanHandle]:
        parent_span = getattr(parent, "span", parent) if parent else None
        span = self._make_span(
            SpanKind.LLM_CALL, name, parent_span,
            model=model,
            attrs=dict(attrs),
        )
        try:
            yield _SpanHandle(span)
        except BaseException as e:
            self._close_span(span, e)
            raise
        else:
            self._close_span(span)

    # --- serialization & flushing ----------------------------------------

    def to_payload(self) -> dict:
        """Serialize this tracer's trace into the /api/traces/ingest shape."""
        ends = [s.ended_at for s in self.trace.spans if s.ended_at]
        if ends and self.trace.ended_at is None:
            self.trace.ended_at = max(ends)

        def _dt(d):
            return d.isoformat() if d else None

        spans_payload = []
        for s in self.trace.spans:
            spans_payload.append({
                "id": s.id,
                "parent_id": s.parent_id,
                "kind": s.kind.value,
                "name": s.name,
                "status": s.status.value,
                "started_at": _dt(s.started_at),
                "ended_at": _dt(s.ended_at),
                "sequence": s.sequence,
                "depth": s.depth,
                "input_tokens": s.input_tokens,
                "output_tokens": s.output_tokens,
                "cache_read_tokens": s.cache_read_tokens,
                "cache_write_tokens": s.cache_write_tokens,
                "cost_usd": s.cost_usd,
                "model": s.model,
                "tool_name": s.tool_name,
                "tool_input": s.tool_input,
                "tool_output": s.tool_output,
                "tool_is_error": s.tool_is_error,
                "agent_type": s.agent_type,
                "agent_prompt": s.agent_prompt,
                "vendor": s.vendor,
                "category": s.category,
                "attrs": s.attrs,
            })

        return {
            "id": self.trace.id,
            "session_id": self.trace.session_id,
            "provider_id": self.trace.provider_id,
            "project": self.trace.project,
            "title": self.trace.title,
            "cwd": self.trace.cwd,
            "git_branch": self.trace.git_branch,
            "model": self.trace.model,
            "started_at": _dt(self.trace.started_at),
            "ended_at": _dt(self.trace.ended_at),
            "attrs": self.trace.attrs,
            "spans": spans_payload,
        }

    def flush(self, *, direct_db: bool = False) -> dict:
        """Send the trace to Spooling.

        - `direct_db=False` (default): POST to self.ingest_url.
        - `direct_db=True`: write straight into the local Postgres via the
          same ingest pipeline used by providers. Requires spool installed.
        """
        self._flushed = True
        if direct_db:
            from spooling.db import get_connection
            from spooling.ingest import _store_trace

            conn = get_connection()
            try:
                _store_trace(conn, self.trace)
                conn.commit()
            finally:
                conn.close()
            return {"status": "ok", "mode": "direct_db", "trace_id": self.trace.id, "spans": len(self.trace.spans)}

        import httpx
        resp = httpx.post(self.ingest_url, json=self.to_payload(), timeout=30)
        resp.raise_for_status()
        return resp.json()

    def __enter__(self) -> "SpoolTracer":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if not self._flushed:
            try:
                self.flush()
            except Exception as e:
                print(f"[spooling.sdk] flush failed: {e}")
