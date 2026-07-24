"""Parse session JSONL files from ~/.sessions/projects/.

Outputs both the legacy `ParsedSession` (messages + tool counts) and a
`Trace` built from parentUuid/isSidechain, so ingest can write to both the
legacy tables and the new traces/spans tables in the same pass.
"""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from spooling.config import SESSIONS_PROJECTS_DIR, CHARS_PER_TOKEN
from spooling.tracing import (
    Span,
    SpanKind,
    SpanStatus,
    Trace,
    TraceBuilder,
)

_SYSTEM_PREFIXES = re.compile(
    r"^(<(local-command-caveat|system-reminder|command-name|local-command-stdout)>|<!\[CDATA)"
)
_XML_TAG_STRIP = re.compile(r"<[^>]+>[^<]*</[^>]+>\s*")


@dataclass
class ToolCallDetail:
    tool_use_id: str
    name: str
    input_summary: str
    result_preview: str = ""
    tool_input_raw: dict | None = None


@dataclass
class ParsedMessage:
    uuid: str
    session_id: str
    role: str
    content: str
    timestamp: datetime | None
    cwd: str | None = None
    git_branch: str | None = None
    tools_used: list[str] = field(default_factory=list)
    tool_details: list[ToolCallDetail] = field(default_factory=list)
    estimated_tokens: int = 0


@dataclass
class ParsedSession:
    session_id: str
    project: str
    messages: list[ParsedMessage] = field(default_factory=list)
    started_at: datetime | None = None
    ended_at: datetime | None = None
    cwd: str | None = None
    git_branch: str | None = None
    agent_version: str | None = None
    model: str | None = None
    title: str | None = None
    provider_id: str = "jsonl-session"
    trace: Optional[Trace] = None

    @property
    def message_count(self) -> int:
        return len(self.messages)

    @property
    def tool_call_count(self) -> int:
        return sum(len(m.tools_used) for m in self.messages)

    @property
    def estimated_input_tokens(self) -> int:
        return sum(m.estimated_tokens for m in self.messages if m.role == "user")

    @property
    def estimated_output_tokens(self) -> int:
        return sum(m.estimated_tokens for m in self.messages if m.role == "assistant")


def _summarize_tool_input(name: str, inp: dict) -> str:
    """Create a one-line summary of a tool call's input."""
    def _short_path(p: str) -> str:
        parts = p.rsplit("/", 2)
        return "/".join(parts[-2:]) if len(parts) > 2 else p

    if name == "Read":
        path = inp.get("file_path", "")
        offset = inp.get("offset")
        limit = inp.get("limit")
        if offset and limit:
            return f"{path}:{offset}-{offset + limit}"
        return path
    if name in ("Edit", "Write"):
        return inp.get("file_path", "")
    if name == "Bash":
        cmd = inp.get("command", "")
        return cmd[:120]
    if name == "Grep":
        pattern = inp.get("pattern", "")
        path = _short_path(inp.get("path", "")) if inp.get("path") else ""
        gl = inp.get("glob", "")
        parts = [f'"{pattern}"']
        if path:
            parts.append(f"in {path}")
        if gl:
            parts.append(f"({gl})")
        return " ".join(parts)
    if name == "Glob":
        return inp.get("pattern", "")
    if name == "Agent":
        return inp.get("description", "")[:100]
    if name in ("WebSearch", "WebFetch"):
        return inp.get("query", inp.get("url", ""))[:120]
    if name == "Skill":
        return inp.get("skill", "")
    if name == "TodoWrite":
        todos = inp.get("todos", [])
        return f"{len(todos)} items"
    if name == "LSP":
        return inp.get("operation", "")
    # Fallback: show first key=value
    for k, v in inp.items():
        return f"{k}={str(v)[:80]}"
    return ""


def _extract_content(message: dict) -> str:
    """Extract text content from a message object."""
    msg = message.get("message", {})
    content = msg.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    parts.append(f"[tool: {block.get('name', 'unknown')}]")
                elif block.get("type") == "tool_result":
                    pass
        return "\n".join(parts)
    return str(content) if content else ""


def _extract_tool_uses(message: dict) -> list[dict]:
    """Return the tool_use content blocks from an assistant message."""
    msg = message.get("message", {})
    content = msg.get("content", "")
    if not isinstance(content, list):
        return []
    return [
        b for b in content
        if isinstance(b, dict) and b.get("type") == "tool_use"
    ]


def _extract_tool_results(message: dict) -> list[dict]:
    """Return the tool_result content blocks from a user message.

    Used by the Trace builder which needs the raw blocks to match against
    open tool spans and carry the full text/error info.
    """
    msg = message.get("message", {})
    content = msg.get("content", "")
    if not isinstance(content, list):
        return []
    return [
        b for b in content
        if isinstance(b, dict) and b.get("type") == "tool_result"
    ]


def _extract_tool_result_previews(message: dict) -> dict[str, str]:
    """Return {tool_use_id: preview_text} for a user message's tool_results.

    Used by the legacy tool_details view. Previews are clipped at 500 chars
    so large file reads don't blow up the sessions table.
    """
    results: dict[str, str] = {}
    for block in _extract_tool_results(message):
        tool_use_id = block.get("tool_use_id", "")
        result_content = block.get("content", "")
        if isinstance(result_content, list):
            text_parts = [
                b.get("text", "")
                for b in result_content
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            result_content = "\n".join(text_parts)
        if tool_use_id and isinstance(result_content, str):
            results[tool_use_id] = result_content[:500]
    return results


def _format_edit_diff(inp: dict) -> str:
    """Format an Edit tool input as a unified diff with interleaved hunks."""
    old = inp.get("old_string", "")
    new = inp.get("new_string", "")
    if not old and not new:
        return ""
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    diff = difflib.unified_diff(old_lines, new_lines, lineterm="")
    lines = []
    for line in diff:
        stripped = line.rstrip("\n")
        if not stripped:
            continue
        if stripped.startswith("---") or stripped.startswith("+++") or stripped.startswith("@@"):
            continue
        lines.append(stripped)
    return "\n".join(lines)[:2000]


def _extract_tool_details(message: dict) -> list[ToolCallDetail]:
    """Extract detailed tool call info from an assistant message."""
    msg = message.get("message", {})
    content = msg.get("content", "")
    details = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                name = block.get("name", "unknown")
                inp = block.get("input", {}) or {}
                result_preview = ""
                if name == "Edit":
                    result_preview = _format_edit_diff(inp)
                elif name == "Write":
                    result_preview = (inp.get("content") or "")[:2000]
                details.append(ToolCallDetail(
                    tool_use_id=block.get("id", ""),
                    name=name,
                    input_summary=_summarize_tool_input(name, inp),
                    result_preview=result_preview,
                ))
    return details


def _parse_timestamp(raw: str | int | float | None) -> datetime | None:
    """Parse various timestamp formats."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(raw / 1000, tz=timezone.utc)
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _cost_for_usage(model: str | None, usage: dict | None) -> float:
    if not usage:
        return 0.0
    from spooling.pricing import get_rates
    rates = get_rates(model)
    return rates.cost(
        input_tokens=usage.get("input_tokens") or 0,
        output_tokens=usage.get("output_tokens") or 0,
        cache_write_tokens=usage.get("cache_creation_input_tokens") or 0,
        cache_read_tokens=usage.get("cache_read_input_tokens") or 0,
    )


def _tool_result_text(block: dict) -> tuple[str, bool | None]:
    """Return (text, is_error) for a tool_result block."""
    content = block.get("content")
    is_error = block.get("is_error")
    if isinstance(content, str):
        return content, is_error
    if isinstance(content, list):
        parts = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(c.get("text", ""))
            elif isinstance(c, str):
                parts.append(c)
        return "\n".join(parts), is_error
    return "", is_error


def parse_session_file(file_path: Path) -> ParsedSession | None:
    """Parse a single session JSONL file into messages + trace."""
    session_id = file_path.stem
    project = file_path.parent.name

    # --- Pass 1: load all records into memory keyed by uuid -------------
    records: list[dict] = []
    by_uuid: dict[str, dict] = {}

    try:
        with open(file_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("type") not in ("user", "assistant"):
                    continue
                records.append(record)
                uid = record.get("uuid")
                if uid:
                    by_uuid[uid] = record
    except Exception as e:
        print(f"Error reading {file_path}: {e}")
        return None

    if not records:
        return None

    # Session-wide metadata from first record with it.
    cwd = git_branch = agent_version = model = None
    for r in records:
        cwd = cwd or r.get("cwd")
        git_branch = git_branch or r.get("gitBranch")
        agent_version = agent_version or r.get("version")
        if not model:
            m = (r.get("message") or {}).get("model")
            if m:
                model = m

    # --- Build ParsedMessage list (legacy path, with tool details) -------
    messages: list[ParsedMessage] = []
    pending_tool_details: list[ToolCallDetail] = []
    for record in records:
        rec_type = record["type"]  # "user" or "assistant"

        # Pair tool_result previews against the previous assistant's
        # tool_details before we filter empty-content user records out.
        if rec_type == "user" and pending_tool_details:
            previews = _extract_tool_result_previews(record)
            for td in pending_tool_details:
                if td.tool_use_id in previews and not td.result_preview:
                    td.result_preview = previews[td.tool_use_id]
            pending_tool_details = []

        content = _extract_content(record)
        if not content.strip():
            continue

        tools = (
            [b.get("name", "unknown") for b in _extract_tool_uses(record)]
            if rec_type == "assistant"
            else []
        )
        tool_details: list[ToolCallDetail] = []
        if rec_type == "assistant":
            tool_details = _extract_tool_details(record)
            pending_tool_details = tool_details

        ts = _parse_timestamp(record.get("timestamp"))
        est_tokens = max(1, len(content) // CHARS_PER_TOKEN)

        messages.append(ParsedMessage(
            uuid=record.get("uuid", ""),
            session_id=session_id,
            role=rec_type,
            content=content,
            timestamp=ts,
            cwd=record.get("cwd"),
            git_branch=record.get("gitBranch"),
            tools_used=tools,
            tool_details=tool_details,
            estimated_tokens=est_tokens,
        ))

    if not messages:
        return None

    # Title from first user message that isn't a system reminder or caveat.
    first_user = next(
        (m for m in messages if m.role == "user" and not _SYSTEM_PREFIXES.match(m.content.strip())),
        None,
    )
    title = None
    if first_user:
        clean = _XML_TAG_STRIP.sub("", first_user.content).strip()
        clean = clean or first_user.content.strip()
        title = clean[:80].replace("\n", " ").strip()
        if len(clean) > 80:
            title += "..."

    timestamps = [m.timestamp for m in messages if m.timestamp]
    started_at = min(timestamps) if timestamps else None
    ended_at = max(timestamps) if timestamps else None

    # --- Build the Trace -------------------------------------------------
    trace = _build_trace(
        session_id=session_id,
        project=project,
        records=records,
        by_uuid=by_uuid,
        cwd=cwd,
        git_branch=git_branch,
        model=model,
        title=title,
    )

    return ParsedSession(
        session_id=session_id,
        project=project,
        messages=messages,
        started_at=started_at,
        ended_at=ended_at,
        cwd=cwd,
        git_branch=git_branch,
        agent_version=agent_version,
        model=model,
        title=title,
        trace=trace,
    )


def _walk_to_primary(uuid_: str, by_uuid: dict[str, dict]) -> str | None:
    """Walk parentUuid chain until we hit a non-sidechain record.

    Returns the uuid of the primary-chain assistant message that spawned
    this sidechain (or None if the chain ends outside sidechain-land).
    """
    seen = set()
    cur = uuid_
    while cur and cur not in seen:
        seen.add(cur)
        rec = by_uuid.get(cur)
        if not rec:
            return None
        if not rec.get("isSidechain"):
            return cur
        cur = rec.get("parentUuid")
    return None


def _build_trace(
    session_id: str,
    project: str,
    records: list[dict],
    by_uuid: dict[str, dict],
    cwd: str | None,
    git_branch: str | None,
    model: str | None,
    title: str | None,
) -> Trace:
    """Build a Trace with span tree from session records.

    Structure:
        session (root)
          ├─ llm_call       (per assistant msg, with usage/cost)
          ├─ tool           (per tool_use inside an assistant msg; closed by its tool_result)
          └─ agent          (created for each Task tool_use; parents the sidechain sub-trace)
                ├─ llm_call
                └─ tool ...
    """
    tb = TraceBuilder(
        provider_id="jsonl-session",
        session_id=session_id,
        project=project,
        cwd=cwd,
        git_branch=git_branch,
        model=model,
        trace_id=f"trace-{session_id}",
    )

    session_start = _parse_timestamp(records[0].get("timestamp")) if records else None
    root = tb.start_session(
        name=title or f"Session {session_id[:8]}",
        started_at=session_start,
    )

    # Map: Task tool_use_id -> agent Span (so sidechain messages parented under the right agent)
    agent_by_tool_id: dict[str, Span] = {}
    # Map: primary assistant msg uuid containing the Task -> agent Span
    agent_by_primary_uuid: dict[str, Span] = {}
    # Map: open tool span by tool_use id (closed when tool_result arrives)
    open_tools: dict[str, Span] = {}

    for rec in records:
        uid = rec.get("uuid", "")
        rec_type = rec.get("type")
        ts = _parse_timestamp(rec.get("timestamp"))
        is_side = bool(rec.get("isSidechain"))

        # Determine this record's parent span in the tree.
        parent_span: Span = root
        if is_side:
            primary_uid = _walk_to_primary(uid, by_uuid)
            if primary_uid and primary_uid in agent_by_primary_uuid:
                parent_span = agent_by_primary_uuid[primary_uid]
            # else: fall through to root (orphan sidechain — rare)

        if rec_type == "assistant":
            msg = rec.get("message") or {}
            usage = msg.get("usage") or {}
            model_id = msg.get("model") or model

            input_tokens = usage.get("input_tokens") or 0
            cache_write = usage.get("cache_creation_input_tokens") or 0
            cache_read = usage.get("cache_read_input_tokens") or 0
            output_tokens = usage.get("output_tokens") or 0
            cost = _cost_for_usage(model_id, usage)

            llm_span = tb.start_llm_call(
                parent=parent_span,
                name="assistant.turn",
                started_at=ts,
                model=model_id,
                sidechain=is_side,
                message_uuid=uid,
            )
            tb.end_span(
                llm_span,
                ended_at=ts,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read,
                cache_write_tokens=cache_write,
                cost_usd=cost,
            )

            # Open a span for every tool_use in this assistant turn.
            for tu in _extract_tool_uses(rec):
                tool_name = tu.get("name", "unknown")
                tool_id = tu.get("id") or f"tu-{uid}-{tool_name}"
                tool_input = tu.get("input") if isinstance(tu.get("input"), dict) else None

                if tool_name == "Task":
                    # Agent span — child of whatever parent_span is.
                    sub_type = (tool_input or {}).get("subagent_type") or "generic"
                    prompt = (tool_input or {}).get("prompt") or (tool_input or {}).get("description")
                    agent = tb.start_agent(
                        parent=parent_span,
                        name=f"agent:{sub_type}",
                        started_at=ts,
                        agent_type=sub_type,
                        agent_prompt=prompt,
                        task_tool_id=tool_id,
                    )
                    agent_by_tool_id[tool_id] = agent
                    agent_by_primary_uuid[uid] = agent
                    # Also track it as an open "tool" so the tool_result closes the agent.
                    open_tools[tool_id] = agent
                else:
                    tool_span = tb.start_tool(
                        parent=parent_span,
                        name=f"tool:{tool_name}",
                        tool_name=tool_name,
                        started_at=ts,
                        tool_input=tool_input,
                        tool_use_id=tool_id,
                    )
                    open_tools[tool_id] = tool_span

        elif rec_type == "user":
            # A user record may wrap tool_results; close the matching tool spans.
            for tr in _extract_tool_results(rec):
                tool_use_id = tr.get("tool_use_id") or ""
                span = open_tools.pop(tool_use_id, None)
                if span is None:
                    continue
                text, is_error = _tool_result_text(tr)
                status = SpanStatus.ERROR if is_error else SpanStatus.OK
                tb.end_span(
                    span,
                    ended_at=ts,
                    status=status,
                    tool_output=text[:4000] if text else None,
                    tool_is_error=bool(is_error) if is_error is not None else None,
                )

    # Close any still-open tool spans with the last known timestamp.
    last_ts = None
    for rec in reversed(records):
        last_ts = _parse_timestamp(rec.get("timestamp"))
        if last_ts:
            break
    for span in open_tools.values():
        tb.end_span(span, ended_at=last_ts, status=SpanStatus.OK)

    return tb.finalize()


def discover_session_files() -> list[Path]:
    """Find all session JSONL files in the projects directory."""
    if not SESSIONS_PROJECTS_DIR.exists():
        return []
    files = []
    for project_dir in SESSIONS_PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        for f in project_dir.glob("*.jsonl"):
            name = f.stem
            if len(name) == 36 and name.count("-") == 4:
                files.append(f)
    return sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)
