"""tsh start / switch / stop / status / tasks — live-timer commands.

These ALL require the tracker to be running (talk to it over local HTTP).
If the tracker is not running, they print "tracker not running — start the
app first." and exit non-zero.
"""

from __future__ import annotations
import json as json_module
import re
import subprocess
import sys

import click
import httpx

from tsh.cli.tray import _is_running, _tracker_url
from tsh.config import loader
from tsh.storage import db as db_module
from tsh.storage import time_entries


def _post(path: str, body: dict | None = None) -> dict:
    if not _is_running():
        click.echo("tracker not running — start the app first.", err=True)
        sys.exit(2)
    try:
        r = httpx.post(_tracker_url() + path, json=body or {}, timeout=5.0)
    except httpx.RequestError as exc:
        click.echo(f"tracker request failed: {exc}", err=True)
        sys.exit(1)
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail", r.text)
        except Exception:
            detail = r.text
        click.echo(f"tracker error ({r.status_code}): {detail}", err=True)
        sys.exit(1)
    return r.json()


def _get(path: str) -> dict | list:
    if not _is_running():
        click.echo("tracker not running — start the app first.", err=True)
        sys.exit(2)
    try:
        r = httpx.get(_tracker_url() + path, timeout=5.0)
    except httpx.RequestError as exc:
        click.echo(f"tracker request failed: {exc}", err=True)
        sys.exit(1)
    if r.status_code >= 400:
        click.echo(f"tracker error ({r.status_code}): {r.text}", err=True)
        sys.exit(1)
    return r.json()


def _warn_if_pending() -> None:
    """Print one-line warning when pending_reconciliation entries exist.

    Talks to SQLite directly (not the tracker) so it works even when the
    daemon is down. Silent on any error — the warning is best-effort.
    """
    try:
        conn = db_module.connect(loader.config_dir() / "tsh.db")
    except Exception:
        return
    try:
        n = time_entries.count_pending_reconciliation(conn)
    except Exception:
        return
    finally:
        conn.close()
    if n > 0:
        plural = "s" if n != 1 else ""
        click.echo(f"⚠ {n} pending reconciliation{plural} — run `tsh reconcile`")


def _current_branch() -> str | None:
    """Return current git branch name, or None if not in a repo / git missing / detached HEAD."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=".",
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if out.returncode != 0:
        return None
    name = out.stdout.strip()
    if not name or name == "HEAD":
        return None
    return name


def _ticket_from_branch(branch: str) -> str | None:
    """Extract the first ticket key matching the configured branch_pattern."""
    pattern = loader.load()["git"]["branch_pattern"]
    m = re.search(pattern, branch)
    return m.group(0) if m else None


@click.command("start")
@click.argument("ticket")
@click.option("-m", "--note", default="", help="Worklog note for this entry.")
def start(ticket: str, note: str) -> None:
    """Start a new active timer for the given ticket."""
    _warn_if_pending()
    entry = _post("/start", {"ticket_key": ticket, "note": note})
    click.echo(f"Started {entry['ticket_key']} (id={entry['id']})")


@click.command("switch")
@click.argument("ticket", required=False)
@click.option("-m", "--note", default="", help="Worklog note for the new entry.")
@click.option("--from-branch", "from_branch", is_flag=True,
              help="Extract ticket key from current git branch (no positional needed).")
def switch(ticket: str | None, note: str, from_branch: bool) -> None:
    """End the active timer (if any) and start a new one."""
    if from_branch:
        if ticket is not None:
            raise click.UsageError("--from-branch is exclusive of the positional TICKET")
        branch = _current_branch()
        if branch is None:
            return  # not a repo / detached / git missing — silent
        parsed = _ticket_from_branch(branch)
        if parsed is None:
            click.echo(f"no ticket in branch '{branch}'", err=True)
            return
        if not _is_running():
            click.echo("tracker not running — start with `tsh tray`", err=True)
            return
        current = _get("/status")
        active = current.get("active") if isinstance(current, dict) else None
        if active and active.get("ticket_key") == parsed:
            return  # already on this ticket — idempotent
        ticket = parsed

    if ticket is None:
        raise click.UsageError("missing TICKET (or pass --from-branch)")

    _warn_if_pending()
    entry = _post("/switch", {"ticket_key": ticket, "note": note})
    click.echo(f"Switched to {entry['ticket_key']} (id={entry['id']})")


@click.command("stop")
def stop() -> None:
    """Stop the active timer."""
    res = _post("/stop")
    if res.get("closed") is None:
        click.echo("no active timer")
    else:
        e = res["closed"]
        click.echo(f"Stopped {e['ticket_key']} (id={e['id']})")


@click.command("status")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
def status(as_json: bool) -> None:
    """Show the current active task and elapsed time."""
    _warn_if_pending()
    res = _get("/status")
    if as_json:
        click.echo(json_module.dumps(res, indent=2))
        return
    if res.get("active") is None:
        click.echo("no active timer")
        return
    a = res["active"]
    click.echo(f"{a['ticket_key']}: {res['elapsed_seconds']}s elapsed (idle: {res['idle_status']})")


@click.command("tasks")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON.")
def tasks(as_json: bool) -> None:
    """List in-progress tickets + recents (the picker list)."""
    items = _get("/tasks")
    if as_json:
        click.echo(json_module.dumps(items, indent=2))
        return
    if not items:
        click.echo("no tickets")
        return
    for item in items:
        marker = "*" if item["source"] == "in_progress" else " "
        click.echo(f"{marker} {item['ticket_key']:<14}  {item['summary']}")
