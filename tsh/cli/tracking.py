"""tsh start / switch / stop / status / tasks — live-timer commands.

These ALL require the tracker to be running (talk to it over local HTTP).
If the tracker is not running, they print "tracker not running — start the
app first." and exit non-zero.
"""

from __future__ import annotations
import json as json_module
import sys

import click
import httpx

from tsh.cli.tray import _is_running, _tracker_url


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


@click.command("start")
@click.argument("ticket")
@click.option("-m", "--note", default="", help="Worklog note for this entry.")
def start(ticket: str, note: str) -> None:
    """Start a new active timer for the given ticket."""
    entry = _post("/start", {"ticket_key": ticket, "note": note})
    click.echo(f"Started {entry['ticket_key']} (id={entry['id']})")


@click.command("switch")
@click.argument("ticket")
@click.option("-m", "--note", default="", help="Worklog note for the new entry.")
def switch(ticket: str, note: str) -> None:
    """End the active timer (if any) and start a new one."""
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
