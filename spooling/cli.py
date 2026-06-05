"""Spooling CLI - track and search your AI coding assistant sessions."""

import re

import click
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

console = Console()


def _clean_project(name: str) -> str:
    """Turn '-Users-username-path-to-project' into '~/path/to/project'."""
    return re.sub(r"-Users-[^-]+-", "~/", name).replace("-", "/")


@click.group()
@click.version_option(package_name="spooling")
def cli():
    """Spooling - local session tracker for AI coding assistants."""
    pass


@cli.command()
def init():
    """Check database connection and show provider status."""
    from spooling.db import check_db
    from spooling.config import DATABASE_URL
    from spooling.providers import get_all_providers

    console.print(Panel("[bold]Spooling[/bold] - Session Tracker", style="blue"))

    # Check DB
    if check_db():
        console.print("[green]Database connected[/green]")
    else:
        console.print("[red]Cannot connect to database.[/red]")
        console.print(f"  URL: {DATABASE_URL}")
        console.print("  Run: [bold]docker compose up -d[/bold]")
        return

    # Check all providers
    providers = get_all_providers()
    table = Table(show_lines=False, title="Providers")
    table.add_column("Provider", style="cyan")
    table.add_column("Status")
    table.add_column("Path", style="dim")

    for type_id, provider in providers.items():
        available = provider.is_available()
        status = "[green]available[/green]" if available else "[dim]not found[/dim]"
        if provider.is_remote:
            path_str = "[dim](remote API — connect via GUI)[/dim]"
        else:
            if available:
                files = provider.discover_session_files()
                status = f"[green]{len(files)} session files[/green]"
            path_str = str(provider.resolved_data_path())
        table.add_row(provider.name, status, path_str)

    console.print(table)
    console.print("\nRun [bold]spooling sync[/bold] to ingest sessions from all available providers.")


@cli.command()
@click.option("--no-embed", is_flag=True, help="Skip embedding (faster sync)")
@click.option("--provider", "-p", default=None, help="Only sync a specific provider (jsonl-session, codex, cursor, copilot, windsurf)")
def sync(no_embed, provider):
    """Sync AI coding sessions to the database."""
    from spooling.ingest import sync as do_sync
    do_sync(embed=not no_embed, provider_filter=provider)


@cli.command()
def watch():
    """Watch for new session data and auto-sync."""
    from spooling.watcher import watch as do_watch
    do_watch()


@cli.command()
@click.argument("query")
@click.option("-n", "--limit", default=10, help="Number of results")
@click.option("-p", "--project", default=None, help="Filter by project")
def search(query, limit, project):
    """Semantic search across session history."""
    from spooling.search import search as do_search

    results = do_search(query, limit=limit, project=project)

    if not results:
        console.print("[yellow]No results found.[/yellow]")
        return

    for i, r in enumerate(results, 1):
        similarity = f"{r['similarity']:.1%}"
        project_name = r["project"] or "unknown"
        role = r["role"]
        ts = r["timestamp"] or ""

        console.print(
            f"\n[bold]{i}.[/bold] [{similarity}] "
            f"[dim]{project_name}[/dim] "
            f"[{'green' if role == 'user' else 'blue'}]{role}[/{'green' if role == 'user' else 'blue'}] "
            f"[dim]{ts[:19]}[/dim]"
        )
        if r["title"]:
            console.print(f"   [dim]Session:[/dim] {r['title']}")
        console.print(f"   {r['content']}")


@cli.command()
@click.option("--week", is_flag=True, help="Show weekly breakdown")
@click.option("--days", default=7, help="Number of days for daily stats")
def stats(week, days):
    """Show usage statistics."""
    from spooling.stats import get_overview, get_daily_stats

    overview = get_overview()
    s = overview["summary"]

    if not s or s.get("total_sessions", 0) == 0:
        console.print("[yellow]No sessions synced yet. Run 'spooling sync' first.[/yellow]")
        return

    # Overview panel
    total_tokens = s["total_input_tokens"] + s["total_output_tokens"]
    console.print(Panel(
        f"Sessions: [bold]{s['total_sessions']}[/bold]  |  "
        f"Messages: [bold]{s['total_messages']}[/bold]  |  "
        f"Tool calls: [bold]{s['total_tool_calls']}[/bold]\n"
        f"Tokens: [bold]{total_tokens:,}[/bold] est.  |  "
        f"Cost: [bold]${float(s['total_cost_usd']):.2f}[/bold] est.",
        title="[bold]Spooling Overview[/bold]",
        style="blue",
    ))

    # Projects table
    if overview["projects"]:
        table = Table(title="Projects", show_lines=False)
        table.add_column("Project", style="cyan")
        table.add_column("Sessions", justify="right")
        table.add_column("Messages", justify="right")
        table.add_column("Est. Cost", justify="right")
        for p in overview["projects"][:10]:
            proj = _clean_project(p["project"])
            table.add_row(
                proj,
                str(p["sessions"]),
                str(int(p["messages"] or 0)),
                f"${float(p['cost'] or 0):.2f}",
            )
        console.print(table)

    # Top tools
    if overview["top_tools"]:
        table = Table(title="Top Tools", show_lines=False)
        table.add_column("Tool", style="magenta")
        table.add_column("Uses", justify="right")
        for t in overview["top_tools"][:10]:
            table.add_row(t["tool_name"], str(t["uses"]))
        console.print(table)

    # Daily stats
    if week or days:
        daily = get_daily_stats(days=days if not week else 7)
        if daily:
            table = Table(title=f"Daily Usage (last {days if not week else 7} days)", show_lines=False)
            table.add_column("Date")
            table.add_column("Sessions", justify="right")
            table.add_column("Messages", justify="right")
            table.add_column("Tool Calls", justify="right")
            table.add_column("Tokens", justify="right")
            table.add_column("Cost", justify="right")
            for d in daily:
                table.add_row(
                    str(d["day"]),
                    str(d["sessions"]),
                    str(int(d["messages"])),
                    str(int(d["tool_calls"])),
                    f"{int(d['total_tokens']):,}",
                    f"${float(d['cost']):.2f}",
                )
            console.print(table)

    # Recent sessions
    if overview["recent_sessions"]:
        table = Table(title="Recent Sessions", show_lines=False)
        table.add_column("Started", style="dim")
        table.add_column("Project", style="cyan")
        table.add_column("Title")
        table.add_column("Msgs", justify="right")
        table.add_column("Cost", justify="right")
        for r in overview["recent_sessions"]:
            proj = _clean_project(r["project"] or "")
            ts = r["started_at"].strftime("%m/%d %H:%M") if r["started_at"] else ""
            title = (r["title"] or "")[:50]
            table.add_row(
                ts, proj, title,
                str(r["message_count"]),
                f"${float(r['estimated_cost_usd'] or 0):.2f}",
            )
        console.print(table)


@cli.group()
def eval():
    """Run eval rubrics over traces/spans."""
    pass


@eval.command("list")
def eval_list():
    """List all eval rubrics."""
    from spooling.db import get_connection
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, name, kind, target_kind, description FROM eval_rubrics ORDER BY id"
    ).fetchall()
    conn.close()
    table = Table(title="Eval Rubrics")
    table.add_column("ID", style="cyan")
    table.add_column("Name")
    table.add_column("Kind")
    table.add_column("Target")
    table.add_column("Description", style="dim")
    for r in rows:
        table.add_row(r["id"], r["name"], r["kind"], r["target_kind"], r["description"] or "")
    console.print(table)


@eval.command("run")
@click.option("--rubric", required=True, help="Rubric id")
@click.option("--trace", default=None, help="Run against a single trace id")
@click.option("--days", default=None, type=int, help="Run against all traces from the last N days")
def eval_run(rubric, trace, days):
    """Run a rubric against one trace or a batch."""
    from spooling.evals import run_rubric, run_rubric_bulk
    from datetime import datetime, timezone, timedelta

    if trace:
        result = run_rubric(rubric, trace)
        if result is None:
            console.print(f"[yellow]No eval recorded for {trace}[/yellow]")
        else:
            console.print(f"[green]Eval {result} recorded for {trace}[/green]")
        return

    since = None
    if days:
        since = datetime.now(timezone.utc) - timedelta(days=days)
    result = run_rubric_bulk(rubric, since=since)
    console.print(result)


@cli.command()
@click.option("--host", default=None, help="Host to bind to")
@click.option("--port", default=None, type=int, help="Port to bind to")
def serve(host, port):
    """Start the API server."""
    from spooling.config import UI_HOST
    from spooling.server import app
    import uvicorn

    h = host or UI_HOST
    p = port or 3002
    console.print(f"[bold]Spooling API[/bold] at http://{h}:{p}")
    console.print("Start the UI with: [bold]cd ui && npm run dev[/bold]")
    uvicorn.run(app, host=h, port=p, log_level="warning")


@cli.group()
def otel():
    """Ingest OTel/Strands spans from external sources into Spooling."""
    pass


@otel.command("ingest")
@click.option("--file", "path", required=True, type=click.Path(exists=True), help="OTLP JSON export file")
@click.option("--provider", "provider_id", default="otel-remote", help="Provider id to tag the trace with")
@click.option("--project", default=None, help="Project name")
def otel_ingest(path, provider_id, project):
    """Ingest an OTLP/JSON spans file as a Spooling trace."""
    from spooling.remote_otel import ingest_otlp_json_file
    try:
        trace_id = ingest_otlp_json_file(path, provider_id=provider_id, project=project)
        console.print(f"[green]Ingested:[/green] {trace_id}")
    except Exception as e:
        console.print(f"[red]Ingest failed:[/red] {e}")
        raise SystemExit(1)


@cli.group()
def experiment():
    """Create and run Strands experiments (cases + evaluators)."""
    pass


@experiment.command("create")
@click.option("--file", "path", required=True, type=click.Path(exists=True), help="Path to a JSON spec")
def experiment_create(path):
    """Register an experiment from a JSON file."""
    from spooling.experiments import load_spec_from_file, create_experiment
    spec = load_spec_from_file(path)
    eid = create_experiment(spec)
    console.print(f"[green]Experiment created: {eid}[/green] ({spec.name})")


@experiment.command("list")
def experiment_list():
    """List experiments."""
    from spooling.experiments import list_experiments
    rows = list_experiments()
    if not rows:
        console.print("[yellow]No experiments yet. Create one with 'spooling experiment create --file ...'[/yellow]")
        return
    table = Table(title="Experiments")
    table.add_column("ID", style="cyan")
    table.add_column("Name")
    table.add_column("Cases", justify="right")
    table.add_column("Evaluators", justify="right")
    table.add_column("Created", style="dim")
    for r in rows:
        table.add_row(
            r["id"], r["name"],
            str(r["case_count"]), str(r["evaluator_count"]),
            str(r["created_at"])[:19],
        )
    console.print(table)


@experiment.command("run")
@click.option("--id", "experiment_id", required=True, help="Experiment id")
def experiment_run(experiment_id):
    """Run an experiment and persist the report."""
    from spooling.experiments import run_experiment, load_run
    console.print(f"[bold]Running experiment {experiment_id}...[/bold]")
    try:
        run_id = run_experiment(experiment_id)
    except Exception as e:
        console.print(f"[red]Run failed:[/red] {e}")
        raise SystemExit(1)
    run = load_run(run_id)
    console.print(f"[green]Run complete: {run_id}[/green]")
    if run and run.get("overall_scores"):
        console.print("Scores:")
        for name, score in run["overall_scores"].items():
            console.print(f"  - {name}: {score}")


@experiment.command("show")
@click.option("--run", "run_id", required=True, help="Run id")
def experiment_show(run_id):
    """Show the report for a past run."""
    from spooling.experiments import load_run
    run = load_run(run_id)
    if not run:
        console.print(f"[red]Run not found: {run_id}[/red]")
        return
    console.print(f"[bold]Run {run_id}[/bold] ({run['status']})")
    console.print(f"Experiment: {run['experiment_id']}")
    console.print(f"Started:    {run['started_at']}")
    console.print(f"Finished:   {run['finished_at']}")
    if run.get("error"):
        console.print(f"[red]Error:[/red] {run['error']}")
    if run.get("overall_scores"):
        table = Table(title="Overall scores")
        table.add_column("Evaluator", style="cyan")
        table.add_column("Score", justify="right")
        for k, v in run["overall_scores"].items():
            table.add_row(k, f"{v:.3f}" if isinstance(v, (int, float)) else str(v))
        console.print(table)


@cli.group()
def pricing():
    """Manage the LiteLLM-backed model pricing table."""
    pass


@pricing.command("refresh")
def pricing_refresh():
    """Force-fetch the LiteLLM model pricing table into ~/.spool/model_prices.json."""
    from spooling import pricing as _pricing
    try:
        data = _pricing.refresh()
        console.print(f"[green]Pricing refreshed:[/green] {len(data)} models cached at {_pricing.CACHE_FILE}")
    except Exception as e:
        console.print(f"[red]Pricing refresh failed:[/red] {e}")
        raise SystemExit(1)


@pricing.command("show")
@click.argument("model", required=False)
def pricing_show(model):
    """Show the cached pricing for one model, or the source status if no model given."""
    from spooling import pricing as _pricing

    if not model:
        status = _pricing.table_status()
        table = Table(title="Pricing source")
        table.add_column("Key", style="cyan")
        table.add_column("Value")
        for k, v in status.items():
            table.add_row(k, str(v))
        console.print(table)
        return

    rates = _pricing.get_rates(model)
    table = Table(title=f"Rates for {model}")
    table.add_column("Component", style="cyan")
    table.add_column("$/Mtok", justify="right")
    for label, rate in [
        ("Input", rates.input),
        ("Output", rates.output),
        ("Cache write", rates.cache_write),
        ("Cache read", rates.cache_read),
    ]:
        table.add_row(label, f"${rate * 1_000_000:.2f}")
    console.print(table)


@cli.command()
@click.option("--stdio", is_flag=True, help="Use stdio transport (default is streamable-HTTP)")
def mcp(stdio):
    """Launch the Spooling MCP server.

    Defaults to streamable-HTTP at http://127.0.0.1:3004/mcp so any
    MCP-compatible agent (Codex, Cursor, web agents) can
    connect by URL. Pass --stdio for stdio-only clients.
    """
    if stdio:
        from spooling.mcp_server import serve_stdio
        console.print("[bold]Spooling MCP[/bold] over stdio")
        serve_stdio()
    else:
        from spooling.mcp_server import serve_http, MCP_URL
        console.print(f"[bold]Spooling MCP[/bold] at {MCP_URL}")
        serve_http()


def _check_ollama_preflight() -> None:
    """Ping Ollama at the configured URL and warn if it's down.

    Non-blocking: Spooling still starts because the user may have switched
    the chat agent to Anthropic, and LLM-judge evals are opt-in. The
    warning makes it obvious why evals/chat would fail with "All
    connection attempts failed" otherwise.
    """
    import urllib.request
    import urllib.error

    ollama_url = "http://localhost:11434"
    try:
        from spooling.db import get_connection
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT config FROM providers WHERE id = 'spooling-agent'"
            ).fetchone()
        finally:
            conn.close()
        cfg = (row["config"] if row and isinstance(row.get("config"), dict) else {}) if row else {}
        ollama_url = cfg.get("ollama_url") or ollama_url
    except Exception:
        pass

    try:
        urllib.request.urlopen(f"{ollama_url}/api/tags", timeout=1.5).read()
    except (urllib.error.URLError, TimeoutError, OSError):
        console.print(
            f"[yellow]  ! Ollama is not reachable at {ollama_url}.[/yellow]"
        )
        console.print(
            "[dim]    Chat and LLM-judge evals will fail until you run:[/dim] "
            "[bold]ollama serve[/bold]"
        )


@cli.command()
def ui():
    """Launch the API server, MCP HTTP server, and Next.js UI together."""
    import subprocess
    import os
    import sys

    ui_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "ui")

    console.print("[bold]Starting Spooling...[/bold]")
    console.print("  API:  http://127.0.0.1:3002")
    console.print("  MCP:  http://127.0.0.1:3004/mcp")
    console.print("  UI:   http://localhost:3003")

    # Preflight: warn if Ollama is down — otherwise chat + judge will fail
    # silently with "All connection attempts failed" from httpx.
    _check_ollama_preflight()

    api_proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "spooling.server:app", "--host", "127.0.0.1", "--port", "3002", "--log-level", "warning"],
    )

    mcp_proc = subprocess.Popen(
        [sys.executable, "-m", "spooling.mcp_server"],
    )

    try:
        subprocess.run(["npm", "run", "dev"], cwd=ui_dir)
    except KeyboardInterrupt:
        pass
    finally:
        api_proc.terminate()
        mcp_proc.terminate()


from spooling.cloud import cloud as _cloud_group, push as _push_cmd
cli.add_command(_cloud_group)
cli.add_command(_push_cmd)


if __name__ == "__main__":
    cli()
