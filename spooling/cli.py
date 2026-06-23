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
@click.option("--cloud", "cloud_mode", is_flag=True, help="Search cloud workspace instead of local DB")
def search(query, limit, project, cloud_mode):
    """Semantic search across session history."""
    if cloud_mode:
        from spooling.cloud import cloud_search
        results = cloud_search(query, limit=limit, project=project)
    else:
        from spooling.search import search as do_search
        results = do_search(query, limit=limit, project=project)

    if not results:
        console.print("[yellow]No results found.[/yellow]")
        return

    for i, r in enumerate(results, 1):
        source = r.get("_source", "local")
        similarity = f"{r['similarity']:.1%}"
        project_name = r["project"] or "unknown"
        role = r["role"]
        ts = r["timestamp"] or ""

        console.print(
            f"\n[bold]{i}.[/bold] [{similarity}] "
            f"[dim]{project_name}[/dim] "
            f"[{'green' if role == 'user' else 'blue'}]{role}[/{'green' if role == 'user' else 'blue'}] "
            f"[dim]{ts[:19]}[/dim]"
            + (f" [cyan](cloud)[/cyan]" if source == "cloud" else "")
        )
        if r["title"]:
            console.print(f"   [dim]Session:[/dim] {r['title']}")
        console.print(f"   {r['content']}")


@cli.command()
@click.option("--week", is_flag=True, help="Show weekly breakdown")
@click.option("--days", default=7, help="Number of days for daily stats")
@click.option("--cloud", "cloud_mode", is_flag=True, help="Show stats from cloud workspace")
def stats(week, days, cloud_mode):
    """Show usage statistics."""
    if cloud_mode:
        from spooling.cloud import cloud_stats
        data = cloud_stats()
        if not data:
            console.print("[yellow]No cloud stats available. Run 'spooling cloud login' first.[/yellow]")
            return
        console.print(Panel(
            f"Sessions: [bold]{data.get('sessions', 0)}[/bold]  |  "
            f"Cost: [bold]${float(data.get('cost', 0)):.2f}[/bold] est.",
            title="[bold]Spooling Cloud Overview[/bold]",
            style="cyan",
        ))
        return

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
def cost():
    """Show cost details and track spending in $."""
    pass


@cost.command("overview")
@click.option("--provider", "-p", default=None, help="Filter by provider id")
@click.option("--days", type=int, default=None, help="Only look at the last N days")
def cost_overview(provider, days):
    """Aggregate cost broken down by input/output/cache."""
    from spooling.stats import get_cost_summary

    c = get_cost_summary(provider=provider, days=days)
    total_tokens = c["input_tokens"] + c["output_tokens"]

    console.print(Panel(
        f"Total cost:  [bold]${c['cost_usd']:.2f}[/bold]\n"
        f"Input tokens:  {c['input_tokens']:,}\n"
        f"Output tokens: {c['output_tokens']:,}\n"
        f"Cache read:    {c['cache_read_tokens']:,}\n"
        f"Cache write:   {c['cache_write_tokens']:,}\n"
        f"Total tokens:  [bold]{total_tokens:,}[/bold]",
        title="[bold]Cost Overview[/bold]",
        style="green",
    ))


@cost.command("breakdown")
@click.option("--days", type=int, default=None, help="Only look at the last N days")
@click.option("--by", "group_by", type=click.Choice(["provider", "model", "project"]),
              default="provider", help="Breakdown dimension")
def cost_breakdown(days, group_by):
    """Cost broken down by provider, model, or project."""
    from spooling.stats import get_cost_by_provider, get_cost_by_model, get_cost_by_project

    lookup = {
        "provider": (get_cost_by_provider, "Provider"),
        "model": (get_cost_by_model, "Model"),
        "project": (get_cost_by_project, "Project"),
    }
    fn, label = lookup[group_by]
    rows = fn(days=days)

    if not rows:
        console.print("[yellow]No cost data found.[/yellow]")
        return

    table = Table(title=f"Cost by {label}", show_lines=False)
    table.add_column(label, style="cyan")
    table.add_column("Sessions", justify="right")
    table.add_column("In Tokens", justify="right")
    table.add_column("Out Tokens", justify="right")
    table.add_column("Cache R", justify="right")
    table.add_column("Cache W", justify="right")
    table.add_column("Cost", justify="right")

    for r in rows:
        name = r.get("provider_id") or r.get("model") or r.get("project") or "unknown"
        if group_by == "project":
            name = _clean_project(name)
        table.add_row(
            str(name)[:40],
            str(r.get("sessions", r.get("traces", r.get("calls", 0)))),
            f"{int(r.get('input_tokens', 0)):,}",
            f"{int(r.get('output_tokens', 0)):,}",
            f"{int(r.get('cache_read_tokens', 0)):,}",
            f"{int(r.get('cache_write_tokens', 0)):,}",
            f"${float(r.get('cost', 0)):.2f}",
        )
    console.print(table)


@cost.command("daily")
@click.option("--days", default=30, help="Number of days to show")
@click.option("--provider", "-p", default=None, help="Filter by provider id")
def cost_daily(days, provider):
    """Daily cost trend."""
    from spooling.stats import get_daily_stats
    rows = get_daily_stats(days=days, provider=provider)

    if not rows:
        console.print("[yellow]No daily cost data.[/yellow]")
        return

    table = Table(title=f"Daily Cost (last {days} days)", show_lines=False)
    table.add_column("Date", style="cyan")
    table.add_column("Sessions", justify="right")
    table.add_column("Tokens", justify="right")
    table.add_column("Cost", justify="right")

    for r in rows:
        table.add_row(
            str(r["day"]),
            str(r["sessions"]),
            f"{int(r.get('total_tokens', 0)):,}",
            f"${float(r['cost']):.2f}",
        )
    console.print(table)


@cost.command("monthly")
@click.option("--months", default=12, help="Number of months to show")
def cost_monthly(months):
    """Monthly cost trend."""
    from spooling.stats import get_monthly_cost
    rows = get_monthly_cost(months=months)

    if not rows:
        console.print("[yellow]No monthly cost data.[/yellow]")
        return

    table = Table(title=f"Monthly Cost (last {months} months)", show_lines=False)
    table.add_column("Month", style="cyan")
    table.add_column("Sessions", justify="right")
    table.add_column("Tokens", justify="right")
    table.add_column("Cost", justify="right")

    for r in rows:
        month_str = r["month"].strftime("%Y-%m") if r["month"] else "unknown"
        table.add_row(
            month_str,
            str(r["sessions"]),
            f"{int(r.get('total_tokens', 0)):,}",
            f"${float(r['cost']):.2f}",
        )
    console.print(table)


@cost.command("session")
@click.argument("session_id")
def cost_session(session_id):
    """Detailed cost breakdown for a single session."""
    from spooling.stats import get_session_cost_detail

    detail = get_session_cost_detail(session_id)
    if not detail:
        console.print(f"[yellow]Session not found: {session_id}[/yellow]")
        return

    title = detail["title"] or "(no title)"
    console.print(Panel(
        f"Session:     [bold]{detail['session_id'][:12]}...[/bold]\n"
        f"Provider:    {detail['provider_id']}\n"
        f"Project:     {detail['project'] or '—'}\n"
        f"Model:       {detail['model'] or '—'}\n"
        f"Messages:    {detail['message_count']}\n"
        f"Tool calls:  {detail['tool_call_count']}\n"
        f"Input tokens:  {detail['tokens']['input']:,}\n"
        f"Output tokens: {detail['tokens']['output']:,}\n"
        f"Cache read:    {detail['tokens']['cache_read']:,}\n"
        f"Cache write:   {detail['tokens']['cache_write']:,}\n"
        f"Cost:        [bold]${detail['cost_usd']:.4f}[/bold]",
        title=f"[bold]Session Cost[/bold] — {title[:60]}",
        style="green",
    ))

    if detail["per_model"]:
        table = Table(title="Per-model cost breakdown", show_lines=False)
        table.add_column("Model", style="cyan")
        table.add_column("Cost", justify="right")
        for model_name, model_cost in sorted(detail["per_model"].items(),
                                              key=lambda x: x[1], reverse=True):
            table.add_row(model_name, f"${model_cost:.4f}")
        console.print(table)

    if detail["llm_calls"]:
        table = Table(title="LLM call details", show_lines=False)
        table.add_column("#", justify="right")
        table.add_column("Model")
        table.add_column("In", justify="right")
        table.add_column("Out", justify="right")
        table.add_column("CR", justify="right")
        table.add_column("CW", justify="right")
        table.add_column("Cost", justify="right")
        for i, sp in enumerate(detail["llm_calls"], 1):
            table.add_row(
                str(i),
                sp["model"] or "—",
                str(sp["input_tokens"]),
                str(sp["output_tokens"]),
                str(sp["cache_read_tokens"]),
                str(sp["cache_write_tokens"]),
                f"${float(sp['cost_usd'] or 0):.4f}",
            )
        console.print(table)


@cost.command("recalc")
@click.option("--session", "session_id", default=None, help="Single session id to recalc")
@click.option("--provider", "-p", default=None, help="Recalc all sessions for a provider")
@click.option("--apply", "do_apply", is_flag=True, default=False,
              help="Actually update rows (default is dry-run)")
def cost_recalc(session_id, provider, do_apply):
    """Re-price sessions/traces against the current LiteLLM rate table.

    Defaults to dry-run (no writes). Pass --apply to persist changes.
    """
    from spooling.stats import recalc_cost
    result = recalc_cost(session_id=session_id, provider=provider, dry_run=not do_apply)

    if do_apply:
        console.print(f"[green]Applied[/green] — {result['sessions_changed']} sessions + "
                      f"{result['traces_changed']} traces updated.")
    else:
        console.print(f"[yellow]Dry-run[/yellow] — pass [bold]--apply[/bold] to persist changes.")

    console.print(Panel(
        f"Sessions reviewed: {result['sessions_reviewed']}\n"
        f"Traces reviewed:   {result['traces_reviewed']}\n"
        f"Sessions changed:  {result['sessions_changed']}\n"
        f"Traces changed:    {result['traces_changed']}\n"
        f"Total old cost:    ${result['total_old_cost']:.4f}\n"
        f"Total new cost:    ${result['total_new_cost']:.4f}\n"
        f"Difference:        [bold]${result['total_diff']:.4f}[/bold]",
        title="[bold]Recalc Summary[/bold]",
        style="green" if abs(result['total_diff']) < 0.01 else "yellow",
    ))

    if result["changes"]:
        table = Table(title="Changes (first 50)", show_lines=False)
        table.add_column("Session", style="cyan")
        table.add_column("Old $", justify="right")
        table.add_column("New $", justify="right")
        table.add_column("Diff", justify="right")
        for ch in result["changes"][:50]:
            sid = (ch.get("session_id") or "")[:12]
            table.add_row(
                sid,
                f"${ch['old_cost']:.4f}",
                f"${ch['new_cost']:.4f}",
                f"[{'green' if ch['diff'] >= 0 else 'red'}]{ch['diff']:+.4f}[/{'green' if ch['diff'] >= 0 else 'red'}]",
            )
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
@click.option("--local", "local_mode", flag_value="local", default=True, help="Local DB only, no cloud fallback")
@click.option("--cloud", "local_mode", flag_value="cloud", help="Cloud only, skip local DB")
@click.option("--hybrid", "local_mode", flag_value="hybrid", help="Local DB with cloud fallback (default)")
def mcp(stdio, local_mode):
    """Launch the Spooling MCP server.

    Defaults to streamable-HTTP at http://127.0.0.1:3004/mcp so any
    MCP-compatible agent (Codex, Cursor, web agents) can
    connect by URL. Pass --stdio for stdio-only clients.

    Data source modes:
    \b
    --hybrid   Local DB with cloud fallback (default)
    --local    Local DB only
    --cloud    Cloud only
    """
    from spooling.mcp_server import set_mode, serve_http as _serve_http, serve_stdio as _serve_stdio, MCP_URL
    set_mode(local_mode or "hybrid")
    if stdio:
        console.print("[bold]Spooling MCP[/bold] over stdio")
        _serve_stdio()
    else:
        console.print(f"[bold]Spooling MCP[/bold] at {MCP_URL}")
        _serve_http()


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

    ui_dir = (
        os.environ.get("SPOOLING_UI_DIR")
        or os.path.join(os.path.dirname(os.path.dirname(__file__)), "ui")
    )
    if not os.path.isdir(ui_dir):
        ui_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "ui")
    if not os.path.isdir(ui_dir):
        ui_dir = os.path.join(os.path.expanduser("~"), "spooling", "ui")
    if not os.path.isdir(ui_dir):
        ui_dir = os.path.join(os.getcwd(), "ui")
    if not os.path.isdir(ui_dir):
        console.print("[red]UI directory not found. Set SPOOLING_UI_DIR or run from the repo root.[/red]")
        raise SystemExit(1)

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
