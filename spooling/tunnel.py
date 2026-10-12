"""Cloudflare tunnel support for exposing local MCP servers."""

import os
import platform
import re
import stat
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Optional, Tuple

from rich.console import Console

console = Console()

# Pattern to match Cloudflare quick tunnel URLs
TUNNEL_URL_PATTERN = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")

# Where we cache the auto-downloaded binary
_BIN_DIR = Path.home() / ".spooling" / "bin"
_CLOUDFLARED_BIN = _BIN_DIR / ("cloudflared.exe" if platform.system() == "Windows" else "cloudflared")

# Download URL templates per platform/arch
_DOWNLOAD_URLS: dict[tuple[str, str], str] = {
    ("Darwin",  "arm64"):  "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-arm64",
    ("Darwin",  "x86_64"): "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-amd64",
    ("Linux",   "aarch64"):"https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64",
    ("Linux",   "x86_64"): "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64",
    ("Windows", "AMD64"):  "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe",
}


def _cloudflared_cmd() -> Optional[str]:
    """Return the path to a usable cloudflared binary, or None."""
    # 1. Already on PATH
    try:
        result = subprocess.run(["cloudflared", "version"], capture_output=True, timeout=5)
        if result.returncode == 0:
            return "cloudflared"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # 2. Previously auto-downloaded
    if _CLOUDFLARED_BIN.exists():
        try:
            result = subprocess.run([str(_CLOUDFLARED_BIN), "version"], capture_output=True, timeout=5)
            if result.returncode == 0:
                return str(_CLOUDFLARED_BIN)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    return None


def _auto_install_cloudflared() -> Optional[str]:
    """Download the cloudflared binary for this platform into ~/.spooling/bin/.

    Returns the path to the binary on success, None on failure.
    """
    system = platform.system()
    machine = platform.machine()
    url = _DOWNLOAD_URLS.get((system, machine))

    if not url:
        console.print(f"[red]No cloudflared binary available for {system}/{machine}.[/red]")
        console.print("Install manually: https://developers.cloudflare.com/cloudflared/install/")
        return None

    _BIN_DIR.mkdir(parents=True, exist_ok=True)

    console.print(f"[bold]Downloading cloudflared for {system}/{machine}...[/bold]")
    try:
        urllib.request.urlretrieve(url, _CLOUDFLARED_BIN)
    except Exception as e:
        console.print(f"[red]Download failed:[/red] {e}")
        return None

    # Make executable on Unix
    if system != "Windows":
        _CLOUDFLARED_BIN.chmod(_CLOUDFLARED_BIN.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    # Verify
    try:
        result = subprocess.run([str(_CLOUDFLARED_BIN), "version"], capture_output=True, timeout=5)
        if result.returncode == 0:
            console.print(f"[green]cloudflared installed to {_CLOUDFLARED_BIN}[/green]")
            return str(_CLOUDFLARED_BIN)
    except Exception:
        pass

    console.print("[red]Downloaded binary did not run correctly.[/red]")
    return None


def check_cloudflared() -> bool:
    """Check if cloudflared is available (on PATH or auto-downloaded)."""
    return _cloudflared_cmd() is not None


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

    cmd_bin = _cloudflared_cmd()
    if not cmd_bin:
        cmd_bin = _auto_install_cloudflared()
    if not cmd_bin:
        return None

    cmd = [cmd_bin, "tunnel", "--no-autoupdate", "--url", f"http://localhost:{port}"]

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

    cmd_bin = _cloudflared_cmd()
    if not cmd_bin:
        cmd_bin = _auto_install_cloudflared()
    if not cmd_bin:
        return None, None

    cmd = [cmd_bin, "tunnel", "--no-autoupdate", "--url", f"http://localhost:{port}"]

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
