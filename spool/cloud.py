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
) -> list[dict]:
    """Read up to `limit` sessions newer than `since` from the local DB.

    Optional ``project`` matches sessions whose ``project`` column equals
    that string (case-sensitive). Optional ``cwd_substr`` matches sessions
    whose ``cwd`` contains the substring (use this for path-based filtering
    when you have multiple projects with the same name).
    """
    conn = get_connection()
    try:
        clauses: list[str] = []
        params: list = []
        if since is not None:
            clauses.append("(started_at IS NULL OR started_at >= %s)")
            params.append(since)
        if project is not None:
            clauses.append("project = %s")
            params.append(project)
        if cwd_substr is not None:
            clauses.append("cwd LIKE %s")
            params.append(f"%{cwd_substr}%")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = conn.execute(
            f"""SELECT id, provider_id, project, title, cwd, started_at, ended_at,
                       message_count, tool_call_count,
                       estimated_input_tokens, estimated_output_tokens, estimated_cost_usd
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


def _push_batches(sessions: list[dict], batch: int, base: str, headers: dict, log) -> tuple[int, str | None]:
    """POST sessions to /v1/sessions/batch in chunks. Returns (accepted, error)."""
    if not sessions:
        return 0, None
    total = 0
    with httpx.Client(timeout=60) as client:
        for i in range(0, len(sessions), batch):
            chunk = sessions[i:i + batch]
            try:
                r = client.post(f"{base}/v1/sessions/batch", headers=headers, json={"sessions": chunk})
                r.raise_for_status()
                data = r.json()
                total += data.get("accepted", 0)
                log(f"  pushed {data.get('accepted', 0)} sessions")
            except httpx.HTTPStatusError as e:
                return total, f"HTTP {e.response.status_code}: {e.response.text[:200]}"
            except Exception as e:
                return total, str(e)
    return total, None


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
    help="Only push sessions whose working directory contains this substring. Useful when you have multiple checkouts of the same project.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show which sessions would be pushed and exit. No network call.",
)
def push(limit: int, batch: int, project: str | None, cwd_substr: str | None, dry_run: bool):
    """Push local sessions up to Spooling Cloud.

    Without filters, this pushes the most recent ``--limit`` sessions from
    every project on this laptop into the workspace your CLI is logged
    into. Pass ``--project`` or ``--cwd`` to scope which sessions go up.
    Common pattern: log in with a team-workspace key, push only that
    project's sessions, leave everything else local.
    """
    headers = _auth_headers()
    base = _api_base()
    sessions = _collect_sessions(
        limit=limit, since=None, project=project, cwd_substr=cwd_substr,
    )
    if not sessions:
        console.print("[yellow]No local sessions match.[/yellow]")
        return
    if dry_run:
        console.print(f"[dim]Dry run: {len(sessions)} session(s) would push to {base}[/dim]")
        for s in sessions[:20]:
            title = (s.get("title") or "(untitled)")[:60]
            console.print(f"  [cyan]{s.get('project') or '-'}[/cyan]  {title}")
        if len(sessions) > 20:
            console.print(f"  ... and {len(sessions) - 20} more")
        return
    total, err = _push_batches(sessions, batch, base, headers, console.print)
    if err:
        console.print(f"[red]{err}[/red]")
        return
    console.print(f"[green]Done.[/green] {total} sessions synced to {base}")


@cloud.command("watch")
@click.option("--interval", default=60, show_default=True, help="Seconds between push cycles")
@click.option("--limit", default=1000, show_default=True, help="Max sessions per cycle")
@click.option("--batch", default=20, show_default=True, help="Sessions per request")
@click.option("--lookback", default=10, show_default=True, help="Minutes to overlap on each cycle to catch updated sessions")
def cloud_watch(interval: int, limit: int, batch: int, lookback: int):
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
            total, err = _push_batches(sessions, batch, base, headers, console.print)
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
                console.print(f"  [green]✓[/green] {total} accepted · watermark → {watermark.strftime('%H:%M:%S')}")
        # else: silent — no new work this cycle.

        # Sleep in 1s slices so Ctrl+C is responsive even with long intervals.
        slept = 0
        while slept < interval and not stop["flag"]:
            time.sleep(1)
            slept += 1

    console.print("[green]Stopped.[/green]")
