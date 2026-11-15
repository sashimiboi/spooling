"""Sign the CLI in through the browser: ``spooling login``.

The API key is created HERE, on this machine, and only its sha256 is sent to Spooling. You approve the short code in
your browser (signed in, choosing the workspace), and the server registers that key for you. No secret ever travels
through the browser, the clipboard, or the server's responses, and there is nothing to copy and paste.
"""

from __future__ import annotations

import hashlib
import platform
import secrets
import socket
import time
import webbrowser

import click
import httpx
from rich.console import Console

console = Console()


class LoginError(click.ClickException):
    pass


def _new_key() -> str:
    return "sk_" + secrets.token_urlsafe(32)


def _client_name() -> str:
    try:
        host = socket.gethostname().split(".")[0]
    except Exception:  # noqa: BLE001
        host = "this computer"
    return f"{host} ({platform.system() or 'unknown OS'})"[:80]


def run_device_login(api_base: str, *, open_browser: bool = True, sleep=time.sleep, now=time.monotonic,
                     http=httpx, out=console) -> tuple[str, dict]:
    """Do the whole dance. Returns (api_key, workspace). Raises LoginError with a plain-English reason."""
    api_base = api_base.rstrip("/")
    key = _new_key()
    try:
        r = http.post(f"{api_base}/api/v1/auths/device/code", timeout=15, json={
            "client_name": _client_name(), "key_hash": hashlib.sha256(key.encode()).hexdigest(), "key_prefix": key[:12]})
    except httpx.HTTPError as e:
        raise LoginError(f"Could not reach {api_base}: {e}") from e
    if r.status_code == 429:
        raise LoginError("Too many login attempts from this network. Try again in a while.")
    if r.status_code != 200:
        raise LoginError(f"Spooling refused the login request ({r.status_code}).")
    d = r.json()

    url = d["verification_uri_complete"]
    out.print()
    out.print(f"  Your code:  [bold cyan]{d['user_code']}[/bold cyan]")
    out.print(f"  Approve it at  [underline]{url}[/underline]")
    if open_browser:
        try:
            if webbrowser.open(url):
                out.print("  [dim](opened in your browser)[/dim]")
        except Exception:  # noqa: BLE001
            pass
    out.print("  [dim]Waiting for approval... (Ctrl+C to cancel)[/dim]\n")

    interval = max(int(d.get("interval", 5)), 1)
    deadline = now() + int(d.get("expires_in", 600))
    while now() < deadline:
        sleep(interval)
        try:
            p = http.post(f"{api_base}/api/v1/auths/device/poll", json={"device_code": d["device_code"]}, timeout=15)
        except httpx.HTTPError:
            continue                                    # a blip: keep waiting until the code expires
        if p.status_code != 200:
            continue
        j = p.json()
        status = j.get("status")
        if status == "approved":
            return key, j.get("workspace") or {}
        if status == "denied":
            raise LoginError("The request was denied in the browser.")
        if status == "expired":
            raise LoginError("The code expired before it was approved. Run the command again.")
        if status == "slow_down":
            interval = int(j.get("interval", interval + 5))
    raise LoginError("Timed out waiting for approval. Run the command again.")
