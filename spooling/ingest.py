"""Ingestion pipeline - parse AI coding sessions from multiple providers and store in pgvector."""

import json
from pathlib import Path

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn

from spooling.db import get_connection
from spooling.parser import ParsedSession
from spooling.embeddings import embed_texts, chunk_text
from spooling.providers import get_provider, get_all_providers
from spooling.tracing import Trace, compute_trace_metrics


def _scrub(s):
    """Strip NUL bytes PostgreSQL text cols can't hold."""
    if s is None:
        return None
    if isinstance(s, str):
        return s.replace("\x00", "")
    return s

console = Console()


def _estimate_cost(
    input_tokens: int,
    output_tokens: int,
    model: str | None,
    provider_id: str | None = None,
) -> float:
    """Session-level cost using the LiteLLM-backed rate table.

    Routes through ``spooling.pricing.get_rates`` so non-default providers
    (Gemini, GPT, etc.) get real per-model rates instead of falling
    through the fallback default. Passing
    ``provider_id`` lets the pricing layer substitute that provider's
    default model when the parser couldn't capture one (e.g. Kiro's
    ``auto``, or Copilot sessions that don't expose the model).
    """
    from spooling.pricing import get_rates
    rates = get_rates(model, provider_id=provider_id)
    return rates.cost(input_tokens=input_tokens, output_tokens=output_tokens)


def _get_synced_files(conn) -> dict[str, int]:
    """Get map of file_path -> last_size for already-synced files."""
    rows = conn.execute("SELECT file_path, last_size FROM sync_state").fetchall()
    return {r["file_path"]: r["last_size"] for r in rows}


def _mark_synced(conn, file_path: str, size: int, provider_id: str = "jsonl-session"):
    conn.execute(
        "INSERT INTO sync_state (file_path, last_size, provider_id) VALUES (%s, %s, %s) "
        "ON CONFLICT (file_path) DO UPDATE SET last_size = %s, provider_id = %s, last_synced_at = now()",
        (file_path, size, provider_id, size, provider_id),
    )


def _store_session(conn, session: ParsedSession):
    """Store a parsed session and its messages."""
    cost = _estimate_cost(
        session.estimated_input_tokens,
        session.estimated_output_tokens,
        session.model,
        provider_id=session.provider_id,
    )

    conn.execute(
        """INSERT INTO sessions (id, provider_id, project, cwd, git_branch, started_at, ended_at,
           message_count, tool_call_count, estimated_input_tokens, estimated_output_tokens,
           estimated_cost_usd, agent_version, model, title)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (id) DO UPDATE SET
           provider_id = EXCLUDED.provider_id,
           message_count = EXCLUDED.message_count,
           tool_call_count = EXCLUDED.tool_call_count,
           estimated_input_tokens = EXCLUDED.estimated_input_tokens,
           estimated_output_tokens = EXCLUDED.estimated_output_tokens,
           estimated_cost_usd = EXCLUDED.estimated_cost_usd,
           ended_at = EXCLUDED.ended_at,
           model = EXCLUDED.model,
           title = EXCLUDED.title""",
        (
            session.session_id, session.provider_id, _scrub(session.project), _scrub(session.cwd),
            _scrub(session.git_branch), session.started_at, session.ended_at, session.message_count,
            session.tool_call_count, session.estimated_input_tokens,
            session.estimated_output_tokens, cost, _scrub(session.agent_version),
            _scrub(session.model), _scrub(session.title),
        ),
    )

    # Upsert messages
    for msg in session.messages:
        if not msg.uuid:
            continue
        conn.execute(
            """INSERT INTO messages (id, session_id, role, content, timestamp, tools_used,
               cwd, git_branch, estimated_tokens)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (id) DO NOTHING""",
            (
                msg.uuid, session.session_id, msg.role, _scrub(msg.content),
                msg.timestamp, json.dumps(msg.tools_used), _scrub(msg.cwd),
                _scrub(msg.git_branch), msg.estimated_tokens,
            ),
        )

        # Store tool calls (with rich details when available).
        if getattr(msg, "tool_details", None):
            for td in msg.tool_details:
                conn.execute(
                    """INSERT INTO tool_calls (session_id, message_id, tool_name, tool_input, tool_result_preview, timestamp)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    (
                        session.session_id, msg.uuid, _scrub(td.name),
                        _scrub(td.input_summary),
                        _scrub(td.result_preview) or None,
                        msg.timestamp,
                    ),
                )
        else:
            for tool_name in msg.tools_used:
                conn.execute(
                    """INSERT INTO tool_calls (session_id, message_id, tool_name, timestamp)
                       VALUES (%s, %s, %s, %s)""",
                    (session.session_id, msg.uuid, _scrub(tool_name), msg.timestamp),
                )


def _store_trace(conn, trace: Trace):
    """Persist a Trace + its spans + span_events.

    Idempotent on trace_id: we DELETE existing spans/events for this trace
    (FK CASCADE handles span_events + evals rows), then re-insert. The
    traces row is upserted so aggregate metrics stay fresh.
    """
    if trace is None or not trace.spans:
        return

    m = compute_trace_metrics(trace)

    # Delete existing spans (cascades to span_events and evals).
    conn.execute("DELETE FROM spans WHERE trace_id = %s", (trace.id,))

    # Upsert the trace row.
    conn.execute(
        """INSERT INTO traces (
            id, session_id, provider_id, project, title,
            started_at, ended_at, duration_ms,
            span_count, agent_count, tool_count, llm_count, error_count,
            total_input_tokens, total_output_tokens,
            total_cache_read_tokens, total_cache_write_tokens,
            total_cost_usd, cwd, git_branch, model,
            vendor_count, top_vendors, attrs
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                  %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (id) DO UPDATE SET
            session_id = EXCLUDED.session_id,
            provider_id = EXCLUDED.provider_id,
            project = EXCLUDED.project,
            title = EXCLUDED.title,
            started_at = EXCLUDED.started_at,
            ended_at = EXCLUDED.ended_at,
            duration_ms = EXCLUDED.duration_ms,
            span_count = EXCLUDED.span_count,
            agent_count = EXCLUDED.agent_count,
            tool_count = EXCLUDED.tool_count,
            llm_count = EXCLUDED.llm_count,
            error_count = EXCLUDED.error_count,
            total_input_tokens = EXCLUDED.total_input_tokens,
            total_output_tokens = EXCLUDED.total_output_tokens,
            total_cache_read_tokens = EXCLUDED.total_cache_read_tokens,
            total_cache_write_tokens = EXCLUDED.total_cache_write_tokens,
            total_cost_usd = EXCLUDED.total_cost_usd,
            cwd = EXCLUDED.cwd,
            git_branch = EXCLUDED.git_branch,
            model = EXCLUDED.model,
            vendor_count = EXCLUDED.vendor_count,
            top_vendors = EXCLUDED.top_vendors,
            attrs = EXCLUDED.attrs
        """,
        (
            trace.id, trace.session_id, trace.provider_id,
            _scrub(trace.project), _scrub(trace.title),
            trace.started_at, trace.ended_at, trace.duration_ms,
            m["span_count"], m["agent_count"], m["tool_count"], m["llm_count"], m["error_count"],
            m["input_tokens"], m["output_tokens"],
            m["cache_read_tokens"], m["cache_write_tokens"],
            m["cost_usd"], _scrub(trace.cwd), _scrub(trace.git_branch), _scrub(trace.model),
            m["vendor_count"], json.dumps(m["top_vendors"]),
            json.dumps(trace.attrs or {}, default=str),
        ),
    )

    # Insert spans in sequence order so parent rows always exist first.
    for span in sorted(trace.spans, key=lambda s: s.sequence):
        conn.execute(
            """INSERT INTO spans (
                id, trace_id, parent_id, kind, name, status,
                started_at, ended_at, duration_ms, depth, sequence,
                input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
                cost_usd, model, tool_name, tool_input, tool_output, tool_is_error,
                agent_type, agent_prompt, vendor, category, attrs
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                      %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                span.id, span.trace_id, span.parent_id, span.kind.value, _scrub(span.name), span.status.value,
                span.started_at, span.ended_at, span.duration_ms, span.depth, span.sequence,
                span.input_tokens, span.output_tokens, span.cache_read_tokens, span.cache_write_tokens,
                span.cost_usd, span.model, _scrub(span.tool_name),
                json.dumps(span.tool_input, default=str) if span.tool_input is not None else None,
                _scrub(span.tool_output), span.tool_is_error,
                _scrub(span.agent_type), _scrub(span.agent_prompt),
                _scrub(span.vendor), _scrub(span.category),
                json.dumps(span.attrs or {}, default=str),
            ),
        )

        for ev in span.events:
            conn.execute(
                """INSERT INTO span_events (span_id, trace_id, name, timestamp, attrs)
                   VALUES (%s, %s, %s, %s, %s)""",
                (span.id, trace.id, ev.name, ev.timestamp, json.dumps(ev.attrs or {})),
            )

    # After spans land, sync the session's headline cost to the sum of its
    # llm_call span costs. Span cost is computed from real per-turn usage
    # (input/output/cache tokens) priced via the LiteLLM rate table, and is
    # the right number for "what would this workload cost on the API at list
    # price." Only override when we actually have real LLM call cost data;
    # providers without trace-level usage (Gemini Code Assist webview,
    # Kiro, etc., where llm_call costs come from message-char estimates)
    # still benefit, but sessions with zero llm_call cost keep their
    # existing chars/4 estimate untouched.
    if trace.session_id:
        conn.execute(
            """UPDATE sessions ss
               SET estimated_cost_usd = sub.span_cost
               FROM (
                 SELECT t.session_id, SUM(s.cost_usd)::numeric(10, 4) AS span_cost
                 FROM spans s JOIN traces t ON s.trace_id = t.id
                 WHERE t.session_id = %s
                   AND s.kind = 'llm_call'
                   AND s.cost_usd IS NOT NULL
                   AND s.cost_usd > 0
                   AND s.model IS NOT NULL
                   AND s.model <> '<synthetic>'
                 GROUP BY t.session_id
               ) sub
               WHERE ss.id = sub.session_id AND sub.span_cost > 0""",
            (trace.session_id,),
        )


def _embed_session(conn, session: ParsedSession):
    """Chunk and embed session messages into pgvector."""
    # Delete existing chunks for this session (re-embed on update)
    conn.execute("DELETE FROM chunks WHERE session_id = %s", (session.session_id,))

    all_chunks = []
    chunk_meta = []

    for msg in session.messages:
        if not msg.content.strip():
            continue
        chunks = chunk_text(msg.content)
        for chunk in chunks:
            all_chunks.append(chunk)
            chunk_meta.append({
                "session_id": session.session_id,
                "message_id": msg.uuid,
                "role": msg.role,
                "project": session.project,
                "timestamp": msg.timestamp,
            })

    if not all_chunks:
        return 0

    # Batch embed
    vectors = embed_texts(all_chunks)

    for chunk, vec, meta in zip(all_chunks, vectors, chunk_meta):
        conn.execute(
            """INSERT INTO chunks (session_id, message_id, content, role, project, timestamp, embedding)
               VALUES (%s, %s, %s, %s, %s, %s, %s::vector)""",
            (
                meta["session_id"], meta["message_id"], chunk,
                meta["role"], meta["project"], meta["timestamp"],
                str(vec),
            ),
        )

    return len(all_chunks)


def _get_connected_providers(conn) -> list[dict]:
    """Get all connected providers from the database."""
    rows = conn.execute(
        "SELECT id, type, data_path, config FROM providers WHERE status = 'connected' AND type != 'agent'"
    ).fetchall()
    return [dict(r) for r in rows]


def _sync_remote_provider(conn, provider, prov_info: dict, embed: bool) -> tuple[int, int, int]:
    """Sync one remote provider via iter_sessions; persist cursor back to config."""
    config = prov_info.get("config") or {}
    if isinstance(config, str):
        config = json.loads(config)
    state = config.get("sync_state") or {}

    total_sessions = 0
    total_messages = 0
    total_chunks = 0
    last_marker = None

    try:
        for session, marker in provider.iter_sessions(config=config, state=state):
            _store_session(conn, session)
            if session.trace is not None:
                _store_trace(conn, session.trace)
            total_messages += session.message_count
            total_sessions += 1
            if embed:
                total_chunks += _embed_session(conn, session)
            last_marker = marker
            # Advance the cursor opportunistically so a crash mid-sync
            # still makes progress on the next run.
            if marker.get("kind") == "remote" and marker.get("cursor"):
                state["updated_after"] = marker["cursor"]
                config["sync_state"] = state
                conn.execute(
                    "UPDATE providers SET config = %s WHERE id = %s",
                    (json.dumps(config), prov_info["id"]),
                )
                conn.commit()
    except Exception as e:
        console.print(f"[red]Remote sync failed for {provider.name}: {e}[/red]")

    return total_sessions, total_messages, total_chunks


def sync(embed: bool = True, provider_filter: str | None = None):
    """Sync sessions from all connected providers to the database."""
    conn = get_connection()
    connected = _get_connected_providers(conn)

    # Always supplement the DB-connected list with any provider that
    # `spooling init` would detect as locally available but doesn't yet have a
    # row in the providers table. Skip providers that DO have a row but
    # aren't 'connected' (user explicitly disabled). Lets a fresh install
    # of e.g. Kiro start syncing on the next `spooling sync` without forcing
    # the user back through the Connections page.
    known_types = {row["type"] for row in conn.execute("SELECT type FROM providers").fetchall()}
    for type_id, prov in get_all_providers().items():
        if type_id in known_types:
            continue
        if prov.is_available():
            connected.append({
                "id": type_id,
                "type": type_id,
                "data_path": str(prov.resolved_data_path()),
            })

    if provider_filter:
        connected = [p for p in connected if p["type"] == provider_filter]

    if not connected:
        console.print("[yellow]No providers to sync. Run 'spooling init' to see what's detected locally, or connect one via the UI.[/yellow]")
        conn.close()
        return

    synced = _get_synced_files(conn)
    grand_total_sessions = 0
    grand_total_messages = 0
    grand_total_chunks = 0

    for prov_info in connected:
        provider = get_provider(prov_info["type"])
        if not provider:
            console.print(f"[yellow]Unknown provider type: {prov_info['type']}[/yellow]")
            continue

        # Remote providers (GitLab, etc.) don't have files on disk —
        # delegate to iter_sessions and persist the cursor cleanly.
        if provider.is_remote:
            console.print(f"[bold]{provider.name}:[/bold] Fetching from API...")
            ns, nm, nc = _sync_remote_provider(conn, provider, prov_info, embed)
            if ns:
                console.print(
                    f"  [green]Synced {ns} sessions, {nm} messages, "
                    f"{nc} chunks embedded.[/green]"
                )
                conn.execute(
                    """UPDATE providers SET
                       session_count = (SELECT COUNT(*) FROM sessions WHERE provider_id = %s),
                       last_synced_at = now()
                       WHERE id = %s""",
                    (prov_info["id"], prov_info["id"]),
                )
                conn.commit()
            grand_total_sessions += ns
            grand_total_messages += nm
            grand_total_chunks += nc
            continue

        # Use custom data_path if set, otherwise default
        data_path = None
        if prov_info.get("data_path"):
            expanded = Path(prov_info["data_path"]).expanduser()
            if expanded.exists():
                data_path = expanded

        files = provider.discover_session_files(data_path)
        if not files:
            continue

        # Filter to new or changed files
        to_process = []
        for f in files:
            size = f.stat().st_size
            if str(f) not in synced or synced[str(f)] != size:
                to_process.append(f)

        if not to_process:
            continue

        console.print(
            f"[bold]{provider.name}:[/bold] Found {len(to_process)} new/updated session files."
        )

        total_messages = 0
        total_chunks = 0
        total_sessions = 0

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            console=console,
        ) as progress:
            task = progress.add_task(f"Syncing {provider.name}...", total=len(to_process))

            for f in to_process:
                sessions = provider.parse_session_file(f)
                for session in sessions:
                    _store_session(conn, session)
                    if session.trace is not None:
                        _store_trace(conn, session.trace)
                    total_messages += session.message_count
                    total_sessions += 1

                    if embed:
                        chunks = _embed_session(conn, session)
                        total_chunks += chunks

                _mark_synced(conn, str(f), f.stat().st_size, prov_info["type"])
                conn.commit()
                progress.advance(task)

        # Update provider stats
        conn.execute(
            """UPDATE providers SET
               session_count = (SELECT COUNT(*) FROM sessions WHERE provider_id = %s),
               last_synced_at = now()
               WHERE id = %s""",
            (prov_info["id"], prov_info["id"]),
        )
        conn.commit()

        grand_total_sessions += total_sessions
        grand_total_messages += total_messages
        grand_total_chunks += total_chunks

        console.print(
            f"  [green]Synced {total_sessions} sessions, "
            f"{total_messages} messages, "
            f"{total_chunks} chunks embedded.[/green]"
        )

    conn.close()

    if grand_total_sessions == 0:
        console.print("[green]All sessions already synced.[/green]")
    else:
        console.print(
            f"\n[green]Total: {grand_total_sessions} sessions, "
            f"{grand_total_messages} messages, "
            f"{grand_total_chunks} chunks embedded.[/green]"
        )
