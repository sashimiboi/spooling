"""Watch AI coding tool directories for new session data and auto-sync."""

import time

from rich.console import Console
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileModifiedEvent, FileCreatedEvent

from spool.db import get_connection
from spool.ingest import _store_session, _embed_session, _mark_synced
from spool.providers import get_provider, get_all_providers
from spool.providers.base import Provider

console = Console()


class MultiProviderHandler(FileSystemEventHandler):
    """Handle new or modified session files across all providers."""

    def __init__(self, provider: Provider, embed: bool = True):
        self.provider = provider
        self.embed = embed
        self._debounce: dict[str, float] = {}

    def _should_process(self, path: str) -> bool:
        now = time.time()
        if path in self._debounce and now - self._debounce[path] < 5:
            return False
        self._debounce[path] = now
        return True

    def _handle(self, path: str):
        if not self._should_process(path):
            return

        from pathlib import Path
        file_path = Path(path)

        # Let the provider try to parse it
        sessions = self.provider.parse_session_file(file_path)
        if not sessions:
            return

        console.print(
            f"[blue][{self.provider.name}] Detected change:[/blue] {file_path.name[:30]}..."
        )

        try:
            conn = get_connection()
            total_msgs = 0
            total_chunks = 0
            for session in sessions:
                _store_session(conn, session)
                total_msgs += session.message_count
                if self.embed:
                    total_chunks += _embed_session(conn, session)
            _mark_synced(conn, str(file_path), file_path.stat().st_size, self.provider.type_id)
            conn.commit()
            conn.close()
            console.print(
                f"  [green]Synced {len(sessions)} sessions, {total_msgs} messages, {total_chunks} chunks[/green]"
            )
        except Exception as e:
            console.print(f"  [red]Error: {e}[/red]")

    def on_modified(self, event):
        if isinstance(event, FileModifiedEvent):
            self._handle(event.src_path)

    def on_created(self, event):
        if isinstance(event, FileCreatedEvent):
            self._handle(event.src_path)


def watch(embed: bool = True):
    """Watch all connected provider directories for changes and auto-sync."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, type, data_path FROM providers WHERE status = 'connected' AND type != 'agent'"
    ).fetchall()
    conn.close()

    # Build list of providers to watch
    watch_targets = []
    for row in rows:
        provider = get_provider(row["type"])
        if not provider:
            continue
        data_path = provider.resolved_data_path()
        if row.get("data_path"):
            from pathlib import Path
            custom = Path(row["data_path"]).expanduser()
            if custom.exists():
                data_path = custom
        if data_path.exists():
            watch_targets.append((provider, data_path))

    # Fallback: if no providers connected, at least watch Claude Code
    if not watch_targets:
        from spool.providers.claude_code import ClaudeCodeProvider
        cc = ClaudeCodeProvider()
        cc_path = cc.resolved_data_path()
        if cc_path.exists():
            watch_targets.append((cc, cc_path))

    if not watch_targets:
        console.print("[red]No provider data directories found to watch.[/red]")
        return

    observer = Observer()
    for provider, data_path in watch_targets:
        console.print(f"[bold]Watching[/bold] {provider.name}: {data_path}")
        handler = MultiProviderHandler(provider, embed=embed)
        observer.schedule(handler, str(data_path), recursive=True)

    console.print("Press Ctrl+C to stop.\n")
    observer.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
        console.print("\n[yellow]Stopped watching.[/yellow]")
    observer.join()
