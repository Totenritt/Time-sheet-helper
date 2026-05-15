"""tsh autostart enable/disable/status — Startup-folder shortcut management."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import click

from tsh.cli.tray import _is_running, _windowless_python


SHORTCUT_NAME = "tsh.lnk"
DESCRIPTION = "Timesheet Helper background tracker"


def _startup_folder() -> Path:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise click.ClickException("APPDATA env var not set; cannot locate Startup folder")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def _shortcut_path() -> Path:
    return _startup_folder() / SHORTCUT_NAME


def _wscript_shell():
    """Lazy import — pywin32 isn't available on non-Windows test hosts."""
    import win32com.client  # type: ignore[import-not-found]
    return win32com.client.Dispatch("WScript.Shell")


def _read_shortcut_target(shortcut_path: Path) -> str | None:
    """Return the TargetPath of the .lnk, or None on error."""
    try:
        shell = _wscript_shell()
        shortcut = shell.CreateShortcut(str(shortcut_path))
        return shortcut.TargetPath
    except Exception:
        return None


def _shortcut_is_ours(shortcut_path: Path) -> bool:
    target = _read_shortcut_target(shortcut_path)
    if target is None:
        return False
    t = target.lower()
    return t.endswith("pythonw.exe") or t.endswith("tsh-tray.exe")


def _daemon_running() -> bool:
    return _is_running()


def _spawn_detached() -> None:
    """Start the tracker in detached mode (same logic as `tsh tray --detach`)."""
    windowless = _windowless_python()
    if getattr(sys, "frozen", False):
        cmd = [windowless]
    else:
        cmd = [windowless, "-m", "tsh", "tray"]
    subprocess.Popen(
        cmd,
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
        close_fds=True,
        cwd=str(Path.home()),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


@click.group("autostart")
def autostart_group() -> None:
    """Manage the Windows Startup-folder shortcut for the tracker."""


@autostart_group.command("enable")
def enable() -> None:
    """Write the Startup shortcut and start the daemon if not running."""
    shortcut_path = _shortcut_path()
    shortcut_path.parent.mkdir(parents=True, exist_ok=True)
    windowless = _windowless_python()

    shell = _wscript_shell()
    shortcut = shell.CreateShortcut(str(shortcut_path))
    shortcut.TargetPath = windowless
    if getattr(sys, "frozen", False):
        shortcut.Arguments = ""
    else:
        shortcut.Arguments = "-m tsh tray"
    shortcut.WorkingDirectory = str(Path.home())
    shortcut.Description = DESCRIPTION
    shortcut.save()
    click.echo(f"enabled: shortcut written to {shortcut_path}")

    if not _daemon_running():
        _spawn_detached()
        click.echo("daemon started (detached)")
    else:
        click.echo("daemon already running")


@autostart_group.command("disable")
def disable() -> None:
    """Remove the Startup shortcut (only if it's ours)."""
    shortcut_path = _shortcut_path()
    if not shortcut_path.exists():
        click.echo("autostart already disabled")
        return
    if not _shortcut_is_ours(shortcut_path):
        raise click.ClickException(
            f"{shortcut_path} was not created by tsh; "
            f"remove it manually if you want it gone"
        )
    shortcut_path.unlink()
    click.echo(f"removed {shortcut_path}")


@autostart_group.command("status")
def status_cmd() -> None:
    """Print autostart and daemon state."""
    shortcut_path = _shortcut_path()
    if shortcut_path.exists() and _shortcut_is_ours(shortcut_path):
        click.echo("Startup shortcut: enabled")
        click.echo("Run on next login: yes")
    else:
        click.echo("Startup shortcut: disabled")
        click.echo("Run on next login: no")
    click.echo(f"Daemon: {'running' if _daemon_running() else 'not running'}")
