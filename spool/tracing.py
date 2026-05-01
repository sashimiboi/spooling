"""Tracing primitives: Trace, Span, Event.

Every provider parser emits one Trace per session. The trace owns a tree of
Spans rooted at a single `session` span. Spans can have children — an `agent`
span parents the tool/llm_call spans it invoked. `llm_call` spans carry token
and cost metrics; `tool` spans carry inputs/outputs; `agent` spans carry a
subagent_type and the prompt that spawned them.

Provider parsers build a Trace with TraceBuilder, which handles span ids,
parent linking, sequence numbers, depth, and roll-ups. Ingest then flattens
the tree to rows for `traces`, `spans`, `span_events`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from spool.classifiers import classify


class SpanKind(str, Enum):
    SESSION = "session"
    AGENT = "agent"
    TOOL = "tool"
    LLM_CALL = "llm_call"
    EVAL = "eval"
    STEP = "step"


class SpanStatus(str, Enum):
    OK = "ok"
    ERROR = "error"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


@dataclass
class SpanEvent:
    name: str
    timestamp: Optional[datetime] = None
    attrs: dict[str, Any] = field(default_factory=dict)


@dataclass
class Span:
    id: str
    trace_id: str
    kind: SpanKind
    name: str
    parent_id: Optional[str] = None
    status: SpanStatus = SpanStatus.OK
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    depth: int = 0
    sequence: int = 0

    # LLM metrics (rolled up for agent/session from descendants)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    model: Optional[str] = None

    # Tool details
    tool_name: Optional[str] = None
    tool_input: Optional[dict[str, Any]] = None
    tool_output: Optional[str] = None
    tool_is_error: Optional[bool] = None

    # Agent details
    agent_type: Optional[str] = None
    agent_prompt: Optional[str] = None

    # Classification
    vendor: Optional[str] = None
    category: Optional[str] = None

    attrs: dict[str, Any] = field(default_factory=dict)
    events: list[SpanEvent] = field(default_factory=list)
    children: list["Span"] = field(default_factory=list)

    @property
    def duration_ms(self) -> Optional[int]:
        if self.started_at and self.ended_at:
            delta = self.ended_at - self.started_at
            return max(0, int(delta.total_seconds() * 1000))
        return None

    def add_event(self, name: str, timestamp: Optional[datetime] = None, **attrs) -> None:
        self.events.append(SpanEvent(name=name, timestamp=timestamp, attrs=dict(attrs)))


@dataclass
class Trace:
    id: str
    session_id: str
    provider_id: str
    project: Optional[str] = None
    title: Optional[str] = None
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    cwd: Optional[str] = None
    git_branch: Optional[str] = None
    model: Optional[str] = None
    root: Optional[Span] = None
    spans: list[Span] = field(default_factory=list)
    attrs: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_ms(self) -> Optional[int]:
        if self.started_at and self.ended_at:
            return max(0, int((self.ended_at - self.started_at).total_seconds() * 1000))
        return None


class TraceBuilder:
    """Incrementally build a Trace and its Span tree.

    Usage pattern:
        tb = TraceBuilder(provider_id="claude-code", session_id=...)
        root = tb.start_session(name=..., started_at=...)
        llm = tb.start_llm_call(parent=root, ...); tb.end_span(llm, ...)
        agent = tb.start_agent(parent=root, agent_type=..., ...)
        tool = tb.start_tool(parent=agent, tool_name=..., ...)
        tb.end_span(tool, ...); tb.end_span(agent, ...)
        trace = tb.finalize()
    """

    def __init__(
        self,
        provider_id: str,
        session_id: str,
        project: Optional[str] = None,
        cwd: Optional[str] = None,
        git_branch: Optional[str] = None,
        model: Optional[str] = None,
        trace_id: Optional[str] = None,
    ):
        self.trace = Trace(
            id=trace_id or f"trace-{session_id}",
            session_id=session_id,
            provider_id=provider_id,
            project=project,
            cwd=cwd,
            git_branch=git_branch,
            model=model,
        )
        self._seq = 0

    # --- span factories --------------------------------------------------

    def _next_seq(self) -> int:
        s = self._seq
        self._seq += 1
        return s

    def _new_span(
        self,
        kind: SpanKind,
        name: str,
        parent: Optional[Span],
        started_at: Optional[datetime],
        **kwargs,
    ) -> Span:
        span = Span(
            id=kwargs.pop("span_id", None) or f"span-{uuid.uuid4().hex[:16]}",
            trace_id=self.trace.id,
            kind=kind,
            name=name,
            parent_id=parent.id if parent else None,
            started_at=started_at,
            depth=(parent.depth + 1) if parent else 0,
            sequence=self._next_seq(),
            **kwargs,
        )
        self.trace.spans.append(span)
        if parent is not None:
            parent.children.append(span)
        return span

    def start_session(
        self,
        name: str,
        started_at: Optional[datetime] = None,
        **attrs,
    ) -> Span:
        span = self._new_span(SpanKind.SESSION, name, None, started_at, attrs=dict(attrs))
        self.trace.root = span
        if started_at and not self.trace.started_at:
            self.trace.started_at = started_at
        return span

    def start_agent(
        self,
        parent: Span,
        name: str,
        started_at: Optional[datetime] = None,
        agent_type: Optional[str] = None,
        agent_prompt: Optional[str] = None,
        span_id: Optional[str] = None,
        **attrs,
    ) -> Span:
        return self._new_span(
            SpanKind.AGENT, name, parent, started_at,
            agent_type=agent_type,
            agent_prompt=agent_prompt,
            vendor="agent",
            category="agent",
            attrs=dict(attrs),
            span_id=span_id,
        )

    def start_tool(
        self,
        parent: Span,
        name: str,
        tool_name: str,
        started_at: Optional[datetime] = None,
        tool_input: Optional[dict[str, Any]] = None,
        span_id: Optional[str] = None,
        vendor: Optional[str] = None,
        category: Optional[str] = None,
        **attrs,
    ) -> Span:
        cls = classify(tool_name)
        return self._new_span(
            SpanKind.TOOL, name, parent, started_at,
            tool_name=tool_name,
            tool_input=tool_input,
            vendor=vendor or cls.vendor,
            category=category or cls.category,
            attrs=dict(attrs),
            span_id=span_id,
        )

    def start_llm_call(
        self,
        parent: Span,
        name: str,
        started_at: Optional[datetime] = None,
        model: Optional[str] = None,
        span_id: Optional[str] = None,
        **attrs,
    ) -> Span:
        return self._new_span(
            SpanKind.LLM_CALL, name, parent, started_at,
            model=model,
            attrs=dict(attrs),
            span_id=span_id,
        )

    # --- finishing -------------------------------------------------------

    def end_span(
        self,
        span: Span,
        ended_at: Optional[datetime] = None,
        status: SpanStatus = SpanStatus.OK,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        cost_usd: float = 0.0,
        tool_output: Optional[str] = None,
        tool_is_error: Optional[bool] = None,
    ) -> None:
        span.ended_at = ended_at
        span.status = status
        span.input_tokens = input_tokens
        span.output_tokens = output_tokens
        span.cache_read_tokens = cache_read_tokens
        span.cache_write_tokens = cache_write_tokens
        span.cost_usd = cost_usd
        if tool_output is not None:
            span.tool_output = tool_output
        if tool_is_error is not None:
            span.tool_is_error = tool_is_error

    def finalize(self) -> Trace:
        """Close out the trace: compute end times, roll up tokens/cost."""
        spans = self.trace.spans
        if not spans:
            return self.trace

        # Trace end = max ended_at of any span, else max started_at.
        ends = [s.ended_at for s in spans if s.ended_at]
        starts = [s.started_at for s in spans if s.started_at]
        if ends:
            self.trace.ended_at = max(ends)
        elif starts:
            self.trace.ended_at = max(starts)
        if starts and not self.trace.started_at:
            self.trace.started_at = min(starts)

        # Root session span inherits trace start/end if missing.
        if self.trace.root is not None:
            root = self.trace.root
            if not root.started_at:
                root.started_at = self.trace.started_at
            if not root.ended_at:
                root.ended_at = self.trace.ended_at

        # Roll up token/cost from llm_call spans into their ancestor agent
        # and session spans (walk up parent_id chain).
        by_id = {s.id: s for s in spans}
        for s in spans:
            if s.kind != SpanKind.LLM_CALL:
                continue
            p_id = s.parent_id
            while p_id:
                parent = by_id.get(p_id)
                if parent is None:
                    break
                if parent.kind in (SpanKind.AGENT, SpanKind.SESSION):
                    parent.input_tokens += s.input_tokens
                    parent.output_tokens += s.output_tokens
                    parent.cache_read_tokens += s.cache_read_tokens
                    parent.cache_write_tokens += s.cache_write_tokens
                    parent.cost_usd += s.cost_usd
                p_id = parent.parent_id

        return self.trace


def build_flat_trace_from_messages(
    provider_id: str,
    session_id: str,
    project: Optional[str],
    title: Optional[str],
    messages: list,  # list[ParsedMessage], typed loosely to avoid import cycle
    cwd: Optional[str] = None,
    git_branch: Optional[str] = None,
    model: Optional[str] = None,
) -> Trace:
    """Build a flat Trace from a provider's ParsedMessage list.

    Providers whose data we can't tree-reconstruct (Copilot, Cursor, Windsurf,
    most Codex sessions) use this: one llm_call span per assistant turn and
    one tool span per tool name mentioned in that turn. The session span is
    the root; there are no agent spans because these providers don't expose
    subagent boundaries.

    Cost per turn is computed via ``spool.pricing.get_rates(model)`` so
    Gemini Code Assist and other non-Claude providers get real per-model
    rates instead of falling through to a hardcoded Sonnet default.
    """
    from spool.pricing import get_rates
    rates = get_rates(model, provider_id=provider_id)
    tb = TraceBuilder(
        provider_id=provider_id,
        session_id=session_id,
        project=project,
        cwd=cwd,
        git_branch=git_branch,
        model=model,
        trace_id=f"trace-{session_id}",
    )

    first_ts = next((m.timestamp for m in messages if m.timestamp), None)
    root = tb.start_session(
        name=title or f"{provider_id}:{session_id[:8]}",
        started_at=first_ts,
    )

    for m in messages:
        ts = m.timestamp
        if m.role == "assistant":
            # Estimated-token-based cost (no real usage for these providers).
            est_in = 0  # handled on user msgs
            est_out = getattr(m, "estimated_tokens", 0) or 0
            cost = rates.cost(input_tokens=est_in, output_tokens=est_out)

            llm_span = tb.start_llm_call(
                parent=root,
                name="assistant.turn",
                started_at=ts,
                model=model,
                message_uuid=getattr(m, "uuid", None),
            )
            tb.end_span(
                llm_span,
                ended_at=ts,
                input_tokens=est_in,
                output_tokens=est_out,
                cost_usd=cost,
            )

            for tool_name in getattr(m, "tools_used", []) or []:
                tool_span = tb.start_tool(
                    parent=root,
                    name=f"tool:{tool_name}",
                    tool_name=tool_name,
                    started_at=ts,
                )
                tb.end_span(tool_span, ended_at=ts)
        else:
            # User turn — credit estimated input tokens against nothing
            # specific; totals get rolled into the root via a lightweight
            # llm-less "step" span so traces aren't missing the user side.
            # We record nothing here to keep llm_count honest.
            pass

    return tb.finalize()


def compute_trace_metrics(trace: Trace) -> dict[str, Any]:
    """Aggregate metrics across all spans in a trace for the traces row."""
    span_count = len(trace.spans)
    agent_count = sum(1 for s in trace.spans if s.kind == SpanKind.AGENT)
    tool_count = sum(1 for s in trace.spans if s.kind == SpanKind.TOOL)
    llm_count = sum(1 for s in trace.spans if s.kind == SpanKind.LLM_CALL)
    error_count = sum(1 for s in trace.spans if s.status == SpanStatus.ERROR)

    llm_spans = [s for s in trace.spans if s.kind == SpanKind.LLM_CALL]
    input_tokens = sum(s.input_tokens for s in llm_spans)
    output_tokens = sum(s.output_tokens for s in llm_spans)
    cache_read = sum(s.cache_read_tokens for s in llm_spans)
    cache_write = sum(s.cache_write_tokens for s in llm_spans)
    cost = sum(s.cost_usd for s in llm_spans)

    # Vendor rollup across tool spans only — we don't count the session's
    # own llm_call/agent spans as "vendors", only external integrations.
    vendor_counts: dict[str, int] = {}
    for s in trace.spans:
        if s.kind == SpanKind.TOOL and s.vendor:
            vendor_counts[s.vendor] = vendor_counts.get(s.vendor, 0) + 1
    top_vendors = sorted(
        [{"vendor": v, "uses": n} for v, n in vendor_counts.items()],
        key=lambda d: d["uses"], reverse=True,
    )[:10]

    return {
        "span_count": span_count,
        "agent_count": agent_count,
        "tool_count": tool_count,
        "llm_count": llm_count,
        "error_count": error_count,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "cost_usd": cost,
        "vendor_count": len(vendor_counts),
        "top_vendors": top_vendors,
    }
