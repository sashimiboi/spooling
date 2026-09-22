"""Cloudflare tunnel support for exposing local MCP servers."""

import re
import subprocess
import time
from typing import Optional, Tuple

from rich.console import Console

console = Console()

# Pattern to match Cloudflare quick tunnel URLs
TUNNEL_URL_PATTERN = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def check_cloudflared() -> bool:
    """Check if cloudflared is installed."""
    try:
        result = subprocess.run(
            ["cloudflared", "version"],
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
) -> Optional[str]:
    """Start a Cloudflare quick tunnel and return the public URL.

    Args:
        port: Local port to expose (1-65535)
        name: Optional name for the tunnel (for display purposes)

    Returns:
        The tunnel URL if successful, None otherwise
    """
    if not _validate_port(port):
        console.print(f"[red]Invalid port: {port}. Must be 1-65535.[/red]")
        return None

    if not check_cloudflared():
        console.print("[red]cloudflared is not installed.[/red]")
        console.print("Install it:")
        console.print("  macOS:   [bold]brew install cloudflared[/bold]")
        console.print("  Linux:   [bold]curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /usr/local/bin/cloudflared && chmod +x /usr/local/bin/cloudflared[/bold]")
        console.print("  Windows: [bold]winget install cloudflare.cloudflared[/bold]")
        return None

    cmd = ["cloudflared", "tunnel", "--no-autoupdate", "--url", f"http://localhost:{port}"]

    console.print(f"[bold]Starting Cloudflare tunnel to localhost:{port}...[/bold]")

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
        timeout = 30

        while time.time() - start_time < timeout:
            line = process.stdout.readline()
            if not line:
                break

            match = TUNNEL_URL_PATTERN.search(line.strip())
            if match:
                url = match.group(0)
                break

        if url:
            console.print()
            console.print(f"[green]Tunnel started![/green]")
            console.print()
            console.print(f"  [bold cyan]MCP endpoint:[/bold cyan] [bold]{url}/mcp[/bold]")
            console.print()
            console.print(f"  [dim]Tunnel: {url}[/dim]")
            console.print(f"  [dim]Local:  http://localhost:{port}[/dim]")
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
        console.print("[red]cloudflared not found in PATH.[/red]")
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
) -> Tuple[Optional[subprocess.Popen], Optional[str]]:
    """Start a tunnel in the background and return the process and URL.

    Args:
        port: Local port to expose (1-65535)

    Returns:
        A tuple of (process, url) if successful, (None, None) otherwise
    """
    if not _validate_port(port):
        return None, None

    if not check_cloudflared():
        return None, None

    cmd = ["cloudflared", "tunnel", "--no-autoupdate", "--url", f"http://localhost:{port}"]

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
