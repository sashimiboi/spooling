"""Spool Cloud: push local sessions up to api.spooling.ai."""

import json
import os
import signal
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click
import httpx
from rich.console import Console

from spool.db import get_connection
from spool.redact import redact_messages, redact_traces

console = Console()

DEFAULT_API = "https://api.spooling.ai"
CONFIG_PATH = Path.home() / ".config" / "spool" / "cloud.json"


def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text())
    except Exception:
        return {}


def _save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    os.chmod(CONFIG_PATH, 0o600)


def _auth_headers() -> dict:
    cfg = _load_config()
    key = cfg.get("api_key") or os.environ.get("SPOOL_CLOUD_API_KEY")
    if not key:
        raise click.ClickException("Not logged in. Run `spool cloud login --key sk_...` first.")
    return {"Authorization": f"Bearer {key}"}


def _api_base() -> str:
    cfg = _load_config()
    return cfg.get("api_url") or os.environ.get("SPOOL_CLOUD_URL") or DEFAULT_API


@click.group()
def cloud():
    """Spooling Cloud: push local sessions to api.spooling.ai."""


@cloud.command("login")
@click.option("--key", required=True, help="API key minted at app.spooling.ai/settings/api-keys")
@click.option("--api-url", default=None, help=f"Override API base (default {DEFAULT_API})")
def cloud_login(key: str, api_url: str | None):
    """Store a Spooling Cloud API key in ~/.config/spool/cloud.json."""
    cfg = _load_config()
    cfg["api_key"] = key.strip()
    if api_url:
        cfg["api_url"] = api_url.rstrip("/")
    _save_config(cfg)

    base = cfg.get("api_url") or DEFAULT_API
    try:
        r = httpx.get(f"{base}/v1/stats", headers={"Authorization": f"Bearer {cfg['api_key']}"}, timeout=10)
        r.raise_for_status()
        stats = r.json()
        console.print(f"[green]Logged in to {base}[/green]")
        console.print(f"  sessions in cloud: [bold]{stats.get('sessions', 0)}[/bold]")
    except Exception as e:
        console.print(f"[red]Saved key, but /v1/stats check failed: {e}[/red]")


@cloud.command("status")
def cloud_status():
    """Show current login + cloud stats."""
    cfg = _load_config()
    if not cfg.get("api_key"):
        console.print("[yellow]Not logged in.[/yellow] Run `spool cloud login --key sk_...`.")
        return
    base = _api_base()
    try:
        r = httpx.get(f"{base}/v1/stats", headers=_auth_headers(), timeout=10)
        r.raise_for_status()
        s = r.json()
        console.print(f"API: [cyan]{base}[/cyan]")
        console.print(f"  sessions: [bold]{s.get('sessions', 0)}[/bold]")
        console.print(f"  messages: [bold]{s.get('messages', 0)}[/bold]")
        console.print(f"  providers: [bold]{s.get('providers', 0)}[/bold]")
        console.print(f"  cost: [bold]${s.get('cost', 0):.2f}[/bold]")
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")


@cloud.command("logout")
def cloud_logout():
    """Remove the stored API key."""
    cfg = _load_config()
    cfg.pop("api_key", None)
    _save_config(cfg)
    console.print("[green]Logged out.[/green]")


def _collect_sessions(
    limit: int,
    since: datetime | None,
    project: str | None = None,
    cwd_substr: str | None = None,
    title_substr: str | None = None,
) -> list[dict]:
    """Read up to `limit` sessions newer than `since` from the local DB.

    Filters (all case-insensitive, all optional):
      ``project``      session's project column equals this exactly
      ``cwd_substr``   session's cwd contains this substring
      ``title_substr`` session's title contains this substring (useful when
                      Claude Code was launched from the home dir so the cwd
                      is generic but the project name is in the title)
    """
    conn = get_connection()
    try:
        clauses: list[str] = []
        params: list = []
        if since is not None:
            clauses.append("(started_at IS NULL OR started_at >= %s)")
            params.append(since)
        if project is not None:
            clauses.append("LOWER(project) = LOWER(%s)")
            params.append(project)
        if cwd_substr is not None:
            clauses.append("cwd ILIKE %s")
            params.append(f"%{cwd_substr}%")
        if title_substr is not None:
            clauses.append("title ILIKE %s")
            params.append(f"%{title_substr}%")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = conn.execute(
            f"""SELECT id, provider_id, project, title, cwd, started_at, ended_at,
                       message_count, tool_call_count,
                       estimated_input_tokens, estimated_output_tokens, estimated_cost_usd,
                       model
                FROM sessions
                {where}
                ORDER BY started_at DESC NULLS LAST
                LIMIT %s""",
            tuple(params),
        ).fetchall()
        sessions = []
        for r in rows:
            sid = r["id"]
            msgs = conn.execute(
                """SELECT role, content, timestamp,
                          ROW_NUMBER() OVER (ORDER BY timestamp NULLS LAST, id) - 1 AS seq
                   FROM messages WHERE session_id = %s
                   ORDER BY timestamp NULLS LAST, id""",
                (sid,),
            ).fetchall()
            sessions.append({
                "id": sid,
                "provider_id": r["provider_id"],
                "project": r["project"],
                "title": r["title"],
                "cwd": r["cwd"],
                "started_at": r["started_at"].isoformat() if r["started_at"] else None,
                "ended_at": r["ended_at"].isoformat() if r["ended_at"] else None,
                "message_count": r["message_count"] or 0,
                "tool_call_count": r["tool_call_count"] or 0,
                "input_tokens": r["estimated_input_tokens"] or 0,
                "output_tokens": r["estimated_output_tokens"] or 0,
                "estimated_cost_usd": float(r["estimated_cost_usd"] or 0),
                "model": r["model"],
                "messages": [
                    {
                        "role": m["role"],
                        "content": (m["content"] or "")[:20000],
                        "sequence": int(m["seq"]),
                        "timestamp": m["timestamp"].isoformat() if m["timestamp"] else None,
                    }
                    for m in msgs
                ],
            })
        return sessions
    finally:
        conn.close()


def _collect_traces(session_ids: list[str]) -> list[dict]:
    """Pull traces + spans + span_events for a list of session_ids.

    Each trace becomes a JSON-serializable dict ready for the
    /v1/traces/batch endpoint. Spans are sorted by sequence so the
    server's parent-before-child invariant holds, and span_events
    ride along on each span.
    """
    if not session_ids:
        return []
    conn = get_connection()
    try:
        traces = conn.execute(
            """SELECT id, session_id, provider_id, project, title,
                      started_at, ended_at, duration_ms,
                      span_count, agent_count, tool_count, llm_count, error_count,
                      total_input_tokens, total_output_tokens,
                      total_cache_read_tokens, total_cache_write_tokens,
                      total_cost_usd, cwd, git_branch, model,
                      vendor_count, top_vendors, attrs
               FROM traces WHERE session_id = ANY(%s)""",
            (session_ids,),
        ).fetchall()

        out: list[dict] = []
        for t in traces:
            tid = t["id"]
            spans = conn.execute(
                """SELECT id, parent_id, kind, name, status,
                          started_at, ended_at, duration_ms, depth, sequence,
                          input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
                          cost_usd, model, tool_name, tool_input, tool_output, tool_is_error,
                          agent_type, agent_prompt, vendor, category, attrs
                   FROM spans WHERE trace_id = %s ORDER BY sequence""",
                (tid,),
            ).fetchall()

            spans_payload: list[dict] = []
            for s in spans:
                events = conn.execute(
                    """SELECT name, timestamp, attrs
                       FROM span_events WHERE span_id = %s ORDER BY id""",
                    (s["id"],),
                ).fetchall()
                spans_payload.append({
                    "id": s["id"],
                    "parent_id": s["parent_id"],
                    "kind": s["kind"],
                    "name": s["name"],
                    "status": s["status"] or "ok",
                    "started_at": s["started_at"].isoformat() if s["started_at"] else None,
                    "ended_at": s["ended_at"].isoformat() if s["ended_at"] else None,
                    "duration_ms": int(s["duration_ms"] or 0),
                    "depth": int(s["depth"] or 0),
                    "sequence": int(s["sequence"] or 0),
                    "input_tokens": int(s["input_tokens"] or 0),
                    "output_tokens": int(s["output_tokens"] or 0),
                    "cache_read_tokens": int(s["cache_read_tokens"] or 0),
                    "cache_write_tokens": int(s["cache_write_tokens"] or 0),
                    "cost_usd": float(s["cost_usd"] or 0),
                    "model": s["model"],
                    "tool_name": s["tool_name"],
                    "tool_input": s["tool_input"],
                    "tool_output": (s["tool_output"] or "")[:200000] if s["tool_output"] else None,
                    "tool_is_error": bool(s["tool_is_error"]),
                    "agent_type": s["agent_type"],
                    "agent_prompt": (s["agent_prompt"] or "")[:200000] if s["agent_prompt"] else None,
                    "vendor": s["vendor"],
                    "category": s["category"],
                    "attrs": s["attrs"] or {},
                    "events": [
                        {
                            "name": e["name"],
                            "timestamp": e["timestamp"].isoformat() if e["timestamp"] else None,
                            "attrs": e["attrs"] or {},
                        }
                        for e in events
                    ],
                })

            out.append({
                "id": tid,
                "session_id": t["session_id"],
                "provider_id": t["provider_id"],
                "project": t["project"],
                "title": t["title"],
                "started_at": t["started_at"].isoformat() if t["started_at"] else None,
                "ended_at": t["ended_at"].isoformat() if t["ended_at"] else None,
                "duration_ms": int(t["duration_ms"] or 0),
                "span_count": int(t["span_count"] or 0),
                "agent_count": int(t["agent_count"] or 0),
                "tool_count": int(t["tool_count"] or 0),
                "llm_count": int(t["llm_count"] or 0),
                "error_count": int(t["error_count"] or 0),
                "total_input_tokens": int(t["total_input_tokens"] or 0),
                "total_output_tokens": int(t["total_output_tokens"] or 0),
                "total_cache_read_tokens": int(t["total_cache_read_tokens"] or 0),
                "total_cache_write_tokens": int(t["total_cache_write_tokens"] or 0),
                "total_cost_usd": float(t["total_cost_usd"] or 0),
                "cwd": t["cwd"],
                "git_branch": t["git_branch"],
                "model": t["model"],
                "vendor_count": int(t["vendor_count"] or 0),
                "top_vendors": t["top_vendors"] or [],
                "attrs": t["attrs"] or {},
                "spans": spans_payload,
            })
        return out
    finally:
        conn.close()


def _push_trace_batches(
    traces: list[dict],
    batch_size: int,
    base: str,
    headers: dict,
    log,
) -> tuple[int, int, int, str | None]:
    """POST traces+spans+events to /v1/traces/batch in chunks.

    Returns ``(accepted, rejected, spans_inserted, error)``. The
    cloud rejects traces whose session_id isn't in this workspace,
    so push sessions FIRST and only push traces for those that
    landed.
    """
    if not traces:
        return 0, 0, 0, None
    accepted = 0
    rejected = 0
    spans_total = 0
    with httpx.Client(timeout=180) as client:
        for i in range(0, len(traces), batch_size):
            chunk = traces[i:i + batch_size]
            try:
                r = client.post(
                    f"{base}/v1/traces/batch",
                    headers=headers,
                    json={"traces": chunk},
                )
                if r.status_code == 404:
                    return (
                        accepted,
                        rejected,
                        spans_total,
                        "Cloud doesn't support `/v1/traces/batch` yet (server is older than 2026-04-30). Ask your admin to redeploy.",
                    )
                r.raise_for_status()
                data = r.json()
                acc = int(data.get("accepted", 0))
                rej = int(data.get("rejected", 0))
                spans = int(data.get("spans_inserted", 0))
                accepted += acc
                rejected += rej
                spans_total += spans
                if rej:
                    log(
                        f"  pushed {acc} trace(s), {spans} span(s)  "
                        f"[yellow]rejected {rej}[/yellow]"
                    )
                else:
                    log(f"  pushed {acc} trace(s), {spans} span(s)")
            except httpx.HTTPStatusError as e:
                return (
                    accepted,
                    rejected,
                    spans_total,
                    f"HTTP {e.response.status_code}: {e.response.text[:200]}",
                )
            except Exception as e:
                return accepted, rejected, spans_total, str(e)
    return accepted, rejected, spans_total, None


def _push_batches(
    sessions: list[dict],
    batch: int,
    base: str,
    headers: dict,
    log,
    copy: bool = False,
) -> tuple[int, int, str | None]:
    """POST sessions to /v1/sessions/batch in chunks.

    Returns ``(accepted, rejected, error)``. The cloud rejects sessions
    whose IDs are already owned by another workspace; the CLI surfaces
    that count so the user can react (use ``--copy`` to share into the
    new workspace as fresh rows, or ``spool cloud delete`` to free the
    IDs in the source workspace).

    When ``copy=True`` the server rewrites session IDs deterministically
    so the same source session can live in multiple workspaces.
    """
    if not sessions:
        return 0, 0, None
    total = 0
    rejected = 0
    with httpx.Client(timeout=60) as client:
        for i in range(0, len(sessions), batch):
            chunk = sessions[i:i + batch]
            try:
                payload: dict = {"sessions": chunk}
                if copy:
                    payload["copy"] = True
                r = client.post(f"{base}/v1/sessions/batch", headers=headers, json=payload)
                r.raise_for_status()
                data = r.json()
                accepted = data.get("accepted", 0)
                rej = data.get("rejected", 0)
                total += accepted
                rejected += rej
                if rej:
                    log(f"  pushed {accepted} sessions  [yellow]rejected {rej}[/yellow]")
                else:
                    log(f"  pushed {accepted} sessions")
            except httpx.HTTPStatusError as e:
                return total, rejected, f"HTTP {e.response.status_code}: {e.response.text[:200]}"
            except Exception as e:
                return total, rejected, str(e)
    return total, rejected, None


@click.command()
@click.option("--limit", default=100, help="Max sessions to push per run")
@click.option("--batch", default=20, help="Sessions per request")
@click.option(
    "--project",
    default=None,
    help="Only push sessions whose project name matches exactly. Pair with `spool stats --by project` to discover names.",
)
@click.option(
    "--cwd",
    "cwd_substr",
    default=None,
    help="Only push sessions whose working directory contains this substring (case-insensitive).",
)
@click.option(
    "--title",
    "title_substr",
    default=None,
    help="Only push sessions whose title contains this substring (case-insensitive). Useful when Claude Code was launched from the home dir so all sessions share a generic cwd, but the project name is in the title (e.g. `--title islet`).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show which sessions would be pushed and exit. No network call.",
)
@click.option(
    "--copy",
    is_flag=True,
    help="Share sessions you've already pushed elsewhere. The cloud writes them as fresh rows in this workspace under deterministically rewritten IDs, so the same source session can live in multiple workspaces. Idempotent on repeat.",
)
@click.option(
    "--with-spans",
    "with_spans",
    is_flag=True,
    help="Also push trace + span data (per-LLM-call usage, model, cost, tool boundaries, agent boundaries) for each session. Required for the cloud admin GUI's Traces page to show meaningful data. Not yet compatible with --copy.",
)
@click.option(
    "--no-redact",
    "no_redact",
    is_flag=True,
    help="Skip the client-side secret redactor. By default spool scrubs secrets (Snowflake/AWS/GitHub/OpenAI/Anthropic/Stripe tokens, PEM private keys, KEY=VALUE lines whose key looks sensitive) before pushing. Use this only if you're sure the content is clean and you need raw values.",
)
def push(limit: int, batch: int, project: str | None, cwd_substr: str | None, title_substr: str | None, dry_run: bool, copy: bool, with_spans: bool, no_redact: bool):
    """Push local sessions up to Spooling Cloud.

    Without filters, this pushes the most recent ``--limit`` sessions from
    every project on this laptop into the workspace your CLI is logged
    into. Pass ``--project``, ``--cwd``, or ``--title`` to scope which
    sessions go up. Filters combine with AND.

    Two scoping patterns:

    \b
    1. Filtered push from one workspace — clean per-workspace separation:
       spool cloud login --key sk_<team-key>
       spool push --cwd toebox

    \b
    2. Copy share — same sessions live in multiple workspaces:
       spool cloud login --key sk_<team-key>
       spool push --cwd toebox --copy

    \b
    3. When the cwd is generic (e.g. Claude Code launched from ~):
       spool push --title islet --copy
    """
    if with_spans and copy:
        console.print(
            "[red]--with-spans is not compatible with --copy yet. "
            "Trace IDs reference span IDs which reference parent span IDs; "
            "remapping all of those for copy mode is a separate change. "
            "Run sessions with --copy first, then re-run without --copy "
            "in the destination workspace to push spans.[/red]"
        )
        return
    headers = _auth_headers()
    base = _api_base()
    sessions = _collect_sessions(
        limit=limit,
        since=None,
        project=project,
        cwd_substr=cwd_substr,
        title_substr=title_substr,
    )
    if not sessions:
        console.print("[yellow]No local sessions match.[/yellow]")
        return

    # Client-side secret redaction. Default-on; opt-out with --no-redact.
    if not no_redact:
        total_redactions = 0
        sessions_with_hits = 0
        for s in sessions:
            _, n = redact_messages(s.get("messages") or [])
            if n:
                total_redactions += n
                sessions_with_hits += 1
        if total_redactions:
            console.print(
                f"[dim]Redacted {total_redactions} secret(s) across "
                f"{sessions_with_hits} session(s) before push. "
                f"Use [bold]--no-redact[/bold] to disable.[/dim]"
            )

    if dry_run:
        mode = "copy" if copy else "push"
        if with_spans:
            mode = f"{mode} + spans"
        console.print(f"[dim]Dry run ({mode}): {len(sessions)} session(s) would land in {base}[/dim]")
        for s in sessions[:20]:
            title = (s.get("title") or "(untitled)")[:60]
            console.print(f"  [cyan]{s.get('project') or '-'}[/cyan]  {title}")
        if len(sessions) > 20:
            console.print(f"  ... and {len(sessions) - 20} more")
        if with_spans:
            traces = _collect_traces([s["id"] for s in sessions])
            span_count = sum(len(t.get("spans") or []) for t in traces)
            console.print(
                f"[dim]  + {len(traces)} trace(s) with {span_count} span(s) ready[/dim]"
            )
        return
    total, rejected, err = _push_batches(sessions, batch, base, headers, console.print, copy=copy)
    if err:
        console.print(f"[red]{err}[/red]")
        return
    if rejected:
        console.print(
            f"[green]Done.[/green] {total} sessions synced to {base} "
            f"([yellow]{rejected} rejected — IDs already owned by another workspace[/yellow])"
        )
        console.print(
            "[dim]To share these sessions into this workspace too, "
            "re-run with `--copy`. To move them instead (delete from the "
            "other workspace), see `spool cloud delete --help`.[/dim]"
        )
    else:
        console.print(f"[green]Done.[/green] {total} sessions synced to {base}")

    if with_spans and total > 0:
        # Push traces only for sessions that landed. Cloud will reject
        # any whose session isn't in this workspace anyway, but pre-
        # filtering keeps the request payload smaller.
        accepted_ids = [s["id"] for s in sessions]
        traces = _collect_traces(accepted_ids)
        if traces and not no_redact:
            _, span_redactions = redact_traces(traces)
            if span_redactions:
                console.print(
                    f"[dim]Redacted {span_redactions} secret(s) inside spans "
                    f"(tool_input/tool_output/agent_prompt/attrs) before push.[/dim]"
                )
        if not traces:
            console.print(
                "[dim]No traces found for those sessions (likely the "
                "session_id has no rows in the local traces table — "
                "re-run `spool sync` to regenerate).[/dim]"
            )
            return
        # Smaller per-batch fanout because each trace can carry many spans.
        trace_batch_size = max(1, min(20, batch // 2 or 5))
        console.print(f"[dim]Pushing {len(traces)} trace(s) with spans to {base}…[/dim]")
        t_acc, t_rej, t_spans, t_err = _push_trace_batches(
            traces, trace_batch_size, base, headers, console.print,
        )
        if t_err:
            console.print(f"[yellow]Trace push failed: {t_err}[/yellow]")
            return
        msg = f"[green]Done.[/green] {t_acc} trace(s) and {t_spans} span(s) synced"
        if t_rej:
            msg += f" ([yellow]{t_rej} trace(s) rejected[/yellow])"
        console.print(msg)


@cloud.command("watch")
@click.option("--interval", default=60, show_default=True, help="Seconds between push cycles")
@click.option("--limit", default=1000, show_default=True, help="Max sessions per cycle")
@click.option("--batch", default=20, show_default=True, help="Sessions per request")
@click.option("--lookback", default=10, show_default=True, help="Minutes to overlap on each cycle to catch updated sessions")
@click.option(
    "--no-redact",
    "no_redact",
    is_flag=True,
    help="Skip the client-side secret redactor (see `spool push --help`).",
)
def cloud_watch(interval: int, limit: int, batch: int, lookback: int, no_redact: bool):
    """Continuously push new local sessions to Spooling Cloud (Ctrl+C to stop)."""
    headers = _auth_headers()
    base = _api_base()

    cfg = _load_config()
    last = cfg.get("last_push_at")
    watermark: datetime | None = datetime.fromisoformat(last) if last else None

    stop = {"flag": False}
    def _handle(_sig, _frm):
        stop["flag"] = True
        console.print("\n[yellow]Stopping after current cycle…[/yellow]")
    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)

    console.print(f"[cyan]Watching local sessions → {base}[/cyan]")
    console.print(f"  interval: {interval}s · lookback: {lookback}m · starting watermark: {watermark or 'none (full first push)'}")

    while not stop["flag"]:
        cycle_started = datetime.now(timezone.utc)
        # Re-read each cycle so a manual `spool cloud login` change is picked up.
        headers = _auth_headers()
        since = (watermark - timedelta(minutes=lookback)) if watermark else None

        try:
            sessions = _collect_sessions(limit=limit, since=since)
        except Exception as e:
            console.print(f"[red]DB error: {e}[/red]")
            sessions = []

        if sessions:
            ts = cycle_started.strftime("%H:%M:%S")
            console.print(f"[dim]{ts}[/dim] {len(sessions)} candidate session(s) since {since or 'beginning'}")
            if not no_redact:
                redaction_count = 0
                for s in sessions:
                    _, n = redact_messages(s.get("messages") or [])
                    redaction_count += n
                if redaction_count:
                    console.print(f"  [dim]Redacted {redaction_count} secret(s) before push[/dim]")
            total, rejected, err = _push_batches(sessions, batch, base, headers, console.print)
            if err:
                console.print(f"[red]{err}[/red] (will retry next cycle)")
            else:
                # Advance watermark to the cycle start; the lookback window catches
                # sessions whose started_at slid backwards or whose messages were
                # appended after the original started_at.
                watermark = cycle_started
                cfg = _load_config()
                cfg["last_push_at"] = watermark.isoformat()
                _save_config(cfg)
                rej_note = f" · [yellow]{rejected} rejected (cross-workspace)[/yellow]" if rejected else ""
                console.print(f"  [green]✓[/green] {total} accepted{rej_note} · watermark → {watermark.strftime('%H:%M:%S')}")
        # else: silent — no new work this cycle.

        # Sleep in 1s slices so Ctrl+C is responsive even with long intervals.
        slept = 0
        while slept < interval and not stop["flag"]:
            time.sleep(1)
            slept += 1

    console.print("[green]Stopped.[/green]")


@cloud.command("delete")
@click.option(
    "--project",
    default=None,
    help="Delete cloud sessions whose project name matches exactly.",
)
@click.option(
    "--cwd",
    "cwd_substr",
    default=None,
    help="Delete cloud sessions whose working directory contains this substring.",
)
@click.option(
    "--session-id",
    "session_id",
    default=None,
    help="Delete a single cloud session by its id. Use when project/cwd filters can't isolate it (e.g. seed data with project=null).",
)
@click.option(
    "--all",
    "delete_all",
    is_flag=True,
    help="Delete every session in the workspace this key authenticates to. Requires --yes.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print which sessions would be deleted and exit. No network mutation.",
)
@click.option(
    "--yes",
    is_flag=True,
    help="Skip the confirmation prompt. Required with --all.",
)
def cloud_delete(project: str | None, cwd_substr: str | None, session_id: str | None, delete_all: bool, dry_run: bool, yes: bool):
    """Delete cloud sessions in the workspace this key authenticates to.

    Use this when a session ID is owned by the wrong workspace (typically
    after pushing personal sessions and then trying to push them again to
    a team workspace). Delete from the wrong workspace, then `spool push`
    under the right workspace's key.

    Filters scope what gets deleted. Without filters, --all is required.
    """
    if not (project or cwd_substr or session_id or delete_all):
        console.print("[red]Pass --project, --cwd, --session-id, or --all.[/red]")
        return
    if delete_all and not yes:
        console.print("[red]--all is destructive. Re-run with --yes to confirm.[/red]")
        return

    headers = _auth_headers()
    base = _api_base()
    params = {"dry_run": "true" if dry_run else "false"}
    if project:
        params["project"] = project
    if cwd_substr:
        params["cwd_substr"] = cwd_substr
    if session_id:
        params["id"] = session_id

    try:
        with httpx.Client(timeout=60) as client:
            r = client.delete(f"{base}/v1/sessions", headers=headers, params=params)
        if r.status_code == 404:
            console.print(
                "[red]This Spooling Cloud doesn't support `spool cloud delete` yet "
                "(server is older than 2026-04-27). Ask your admin to redeploy.[/red]"
            )
            return
        r.raise_for_status()
        data = r.json()
    except httpx.HTTPStatusError as e:
        console.print(f"[red]HTTP {e.response.status_code}: {e.response.text[:200]}[/red]")
        return
    except Exception as e:
        console.print(f"[red]{e}[/red]")
        return

    matched = data.get("matched", 0)
    deleted = data.get("deleted", 0)
    sessions = data.get("sessions") or []

    if dry_run:
        console.print(f"[dim]Dry run: {matched} session(s) would be deleted from {base}[/dim]")
        for s in sessions[:20]:
            title = (s.get("title") or "(untitled)")[:60]
            console.print(f"  [cyan]{s.get('project') or '-'}[/cyan]  {title}")
        if matched > 20:
            console.print(f"  ... and {matched - 20} more")
        return

    console.print(f"[green]Deleted[/green] {deleted} session(s) from {base}")
