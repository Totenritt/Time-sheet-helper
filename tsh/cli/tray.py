"""tsh tray / tsh quit — start and stop the tracker process."""

from __future__ import annotations
import sys

import click
import httpx

from tsh.config import loader


def _tracker_url() -> str:
    cfg = loader.load()
    port = int(cfg["app"]["http_port"])
    return f"http://127.0.0.1:{port}"


def _is_running() -> bool:
    """Try /status with a 250ms timeout. True iff the tracker is responding."""
    try:
        r = httpx.get(_tracker_url() + "/status", timeout=0.25)
        return r.status_code == 200
    except (httpx.ConnectError, httpx.TimeoutException, httpx.RequestError):
        return False


@click.command("tray")
def tray() -> None:
    """Start the tracker (idempotent — exits cleanly if already running)."""
    if _is_running():
        click.echo("tracker already running")
        return
    # Lazy import so `tsh --help` doesn't spin up uvicorn / pystray.
    from tsh.tracker import runner
    runner.run()


@click.command("quit")
def quit_cmd() -> None:
    """Stop the tracker. No-op if it isn't running."""
    if not _is_running():
        click.echo("tracker not running")
        return
    try:
        httpx.post(_tracker_url() + "/shutdown", timeout=2.0)
    except httpx.RequestError as exc:
        click.echo(f"shutdown failed: {exc}", err=True)
        sys.exit(1)
    click.echo("tracker stopped")
