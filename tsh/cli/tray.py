"""tsh tray / tsh quit — start and stop the tracker process."""

from __future__ import annotations
import os
import subprocess
import sys
from pathlib import Path

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


def _windowless_python() -> str:
    """Return path to the windowless interpreter for detached spawning.

    Dev mode: pythonw.exe next to current python.exe.
    Frozen (PyInstaller): sibling tsh-tray.exe.
    """
    if getattr(sys, "frozen", False):
        here = Path(sys.executable).parent
        candidate = here / "tsh-tray.exe"
        if candidate.exists():
            return str(candidate)
        return sys.executable
    py = Path(sys.executable)
    pyw = py.with_name("pythonw.exe")
    if pyw.exists():
        return str(pyw)
    return "pythonw.exe"


@click.command("tray")
@click.option("--detach", is_flag=True, help="Re-spawn windowless and exit; tray runs detached.")
def tray(detach: bool) -> None:
    """Start the tracker (idempotent — exits cleanly if already running)."""
    if _is_running():
        click.echo("tracker already running")
        return

    if detach:
        windowless = _windowless_python()
        if getattr(sys, "frozen", False):
            cmd = [windowless]
        else:
            cmd = [windowless, "-m", "tsh", "tray"]
        subprocess.Popen(
            cmd,
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
            close_fds=True,
            cwd=os.getcwd(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        click.echo("tracker starting (detached)")
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
