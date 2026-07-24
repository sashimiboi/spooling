"""Cloudflare tunnel support for exposing local MCP servers."""

import re
import subprocess
import time
from typing import Optional, Tuple

from rich.console import Console

console = Console()

# Pattern to match Cloudflare tunnel URL (strict: only lowercase alphanumeric + hyphens)
TUNNEL_URL_PATTERN = re.compile(r"^https://[a-z0-9]{2,63}-[a-z0-9]{2,63}-[a-z0-9]{2,63}\.trycloudflare\.com$")


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
    no_autoupdate: bool = True,
) -> Optional[str]:
    """Start a Cloudflare quick tunnel and return the public URL.

    Args:
        port: Local port to expose (1-65535)
        name: Optional name for the tunnel (for display purposes)
        no_autoupdate: Disable cloudflared auto-updates

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
        console.print("  Docs:    https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/")
        return None

    cmd = ["cloudflared", "tunnel", "--url", f"http://localhost:{port}"]
    if no_autoupdate:
        cmd.append("--no-autoupdate")

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

        # Read output until we find the URL or process exits
        url = None
        start_time = time.time()
        timeout = 30  # seconds

        while time.time() - start_time < timeout:
            line = process.stdout.readline()
            if not line:
                break

            # Print cloudflared output for visibility
            console.print(f"[dim]{line.rstrip()}[/dim]")

            # Check for tunnel URL
            match = TUNNEL_URL_PATTERN.search(line.strip())
            if match:
                url = match.group(0)
                break

        if url:
            console.print()
            console.print(f"[green]Tunnel started![/green]")
            console.print(f"  URL: [bold]{url}[/bold]")
            console.print(f"  Local: http://localhost:{port}")
            console.print()
            console.print("Press [bold]Ctrl+C[/bold] to stop the tunnel")

            # Wait for Ctrl+C
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
    no_autoupdate: bool = True,
) -> Tuple[Optional[subprocess.Popen], Optional[str]]:
    """Start a tunnel in the background and return the process and URL.

    Args:
        port: Local port to expose (1-65535)
        no_autoupdate: Disable cloudflared auto-updates

    Returns:
        A tuple of (process, url) if successful, (None, None) otherwise
    """
    if not _validate_port(port):
        return None, None

    if not check_cloudflared():
        return None, None

    cmd = ["cloudflared", "tunnel", "--url", f"http://localhost:{port}"]
    if no_autoupdate:
        cmd.append("--no-autoupdate")

    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    # Read output until we find the URL
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
