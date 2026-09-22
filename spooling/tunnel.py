"""localtunnel support for exposing local MCP servers."""

import re
import subprocess
import time
from typing import Optional, Tuple

from rich.console import Console

console = Console()

# Pattern to match localtunnel URLs
TUNNEL_URL_PATTERN = re.compile(r"https://[a-z0-9-]+\.loca\.lt")


def check_localtunnel() -> bool:
    """Check if the lt CLI is installed."""
    try:
        result = subprocess.run(
            ["lt", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _validate_port(port: int) -> bool:
    """Validate port number is in valid range."""
    return 1 <= port <= 65535


def start_tunnel(
    port: int,
    name: Optional[str] = None,
    subdomain: Optional[str] = None,
) -> Optional[str]:
    """Start a localtunnel and return the public URL.

    Args:
        port: Local port to expose (1-65535)
        name: Optional name for the tunnel (for display purposes)
        subdomain: Requested subdomain on loca.lt (e.g. "spooling-mcp").
                   Not guaranteed if already taken.

    Returns:
        The tunnel URL if successful, None otherwise
    """
    if not _validate_port(port):
        console.print(f"[red]Invalid port: {port}. Must be 1-65535.[/red]")
        return None

    if not check_localtunnel():
        console.print("[red]localtunnel (lt) is not installed.[/red]")
        console.print("Install it with:")
        console.print("  [bold]npm install -g localtunnel[/bold]")
        return None

    cmd = ["lt", "--port", str(port)]
    if subdomain:
        cmd += ["--subdomain", subdomain]

    console.print(f"[bold]Starting localtunnel to localhost:{port}...[/bold]")

    process = None
    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        url = None
        start_time = time.time()
        timeout = 30  # seconds

        while time.time() - start_time < timeout:
            line = process.stdout.readline()
            if not line:
                break

            console.print(f"[dim]{line.rstrip()}[/dim]")

            match = TUNNEL_URL_PATTERN.search(line.strip())
            if match:
                url = match.group(0)
                break

        if url:
            console.print()
            console.print(f"[green]Tunnel started![/green]")
            console.print(f"  URL: [bold]{url}[/bold]")
            console.print(f"  MCP: [bold]{url}/mcp[/bold]")
            console.print(f"  Local: http://localhost:{port}")
            console.print()
            console.print("Press [bold]Ctrl+C[/bold] to stop the tunnel")

            try:
                process.wait()
            except KeyboardInterrupt:
                console.print("\n[yellow]Stopping tunnel...[/yellow]")
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                console.print("[green]Tunnel stopped.[/green]")
        else:
            console.print("[red]Failed to get tunnel URL within timeout.[/red]")
            process.terminate()
            process.wait(timeout=5)
            return None

        return url

    except FileNotFoundError:
        console.print("[red]lt not found in PATH.[/red]")
        return None
    except Exception as e:
        console.print(f"[red]Tunnel error:[/red] {e}")
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        return None


def start_tunnel_background(
    port: int,
    subdomain: Optional[str] = None,
) -> Tuple[Optional[subprocess.Popen], Optional[str]]:
    """Start a tunnel in the background and return the process and URL.

    Args:
        port: Local port to expose (1-65535)
        subdomain: Requested subdomain on loca.lt

    Returns:
        A tuple of (process, url) if successful, (None, None) otherwise
    """
    if not _validate_port(port):
        return None, None

    if not check_localtunnel():
        return None, None

    cmd = ["lt", "--port", str(port)]
    if subdomain:
        cmd += ["--subdomain", subdomain]

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    url = None
    start_time = time.time()
    timeout = 30

    while time.time() - start_time < timeout:
        line = process.stdout.readline()
        if not line:
            break

        match = TUNNEL_URL_PATTERN.search(line.strip())
        if match:
            url = match.group(0)
            break

    return process, url
