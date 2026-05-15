"""tsh reconcile — interactive resolution of pending reconciliation entries."""
from __future__ import annotations

import json as json_module
import sys
from datetime import datetime, timezone
from typing import Optional

import click

from tsh.config import loader
from tsh.core.models import TimeEntry
from tsh.storage import db as db_module
from tsh.storage import time_entries


def _open_conn():
    return db_module.connect(loader.config_dir() / "tsh.db")


def _entry_to_dict(e: TimeEntry, reconciliation_reason: str | None) -> dict:
    return {
        "id": e.id,
        "ticket_key": e.ticket_key,
        "start_at": e.start_at.isoformat() if e.start_at else None,
        "end_at": e.end_at.isoformat() if e.end_at else None,
        "kind": e.kind,
        "note": e.note,
        "reconciliation_reason": reconciliation_reason,
    }


def _list_pending_with_reason(conn) -> list[tuple[TimeEntry, str | None]]:
    """Return list of (TimeEntry, reason_str) tuples, newest start_at first."""
    rows = conn.execute(
        """
        SELECT * FROM time_entries
        WHERE pending_reconciliation = 1
        ORDER BY start_at DESC
        """
    ).fetchall()
    out: list[tuple[TimeEntry, str | None]] = []
    for row in rows:
        entry = time_entries.get(conn, row["id"])
        assert entry is not None
        out.append((entry, row["reconciliation_reason"]))
    return out


def _prompt_choice(entry: TimeEntry, reason: str | None) -> str:
    """Print the entry summary and prompt for s/d/n/k."""
    click.echo(f"\n[{entry.id}] {entry.ticket_key} — {reason} —")
    click.echo(
        f"    started {entry.start_at.isoformat()}, "
        f"closed {entry.end_at.isoformat() if entry.end_at else '(open)'}"
    )
    click.echo("    Was that gap:")
    click.echo("      [s] Same work — extend this entry through the gap")
    click.echo(f"      [d] Different ticket — close {entry.ticket_key}, log the gap separately")
    click.echo("      [n] Not work — close, mark the gap as 'not_work'")
    click.echo("      [k] Skip for now")
    return click.prompt("Choice", type=click.Choice(["s", "d", "n", "k"]))


def _apply(conn, entry: TimeEntry, choice: str, chosen_ticket: Optional[str]) -> None:
    """Apply user's choice and clear the pending flag.

    Pending entries are already CLOSED by the sleep handler / startup recovery;
    we don't re-open them. 'same' extends end_at; 'different' / 'not_work'
    insert a new entry spanning end_at -> now. Original always stays closed.
    """
    assert entry.end_at is not None
    assert entry.id is not None
    now = datetime.now(timezone.utc)

    with db_module.tx(conn):
        if choice == "s":
            time_entries.update(
                conn, entry.id,
                end_at=now,
                pending_reconciliation=0,
                reconciliation_reason=None,
            )
        elif choice == "n":
            time_entries.insert(
                conn,
                TimeEntry(
                    id=None, ticket_key=None,
                    start_at=entry.end_at, end_at=now,
                    kind="not_work", note="reconciled gap",
                    jira_worklog_id=None, pushed_at=None,
                    created_at=now, updated_at=now,
                ),
            )
            time_entries.update(
                conn, entry.id,
                pending_reconciliation=0,
                reconciliation_reason=None,
            )
        elif choice == "d":
            assert chosen_ticket is not None
            time_entries.insert(
                conn,
                TimeEntry(
                    id=None, ticket_key=chosen_ticket,
                    start_at=entry.end_at, end_at=now,
                    kind="work", note="reconciled gap",
                    jira_worklog_id=None, pushed_at=None,
                    created_at=now, updated_at=now,
                ),
            )
            time_entries.update(
                conn, entry.id,
                pending_reconciliation=0,
                reconciliation_reason=None,
            )
        else:
            raise click.ClickException(f"unknown choice {choice!r}")


@click.command("reconcile")
@click.argument("entry_id", required=False, type=int)
@click.option("--json", "as_json", is_flag=True, help="Emit pending list as JSON; no prompts.")
def reconcile(entry_id: int | None, as_json: bool) -> None:
    """Walk pending reconciliation entries and resolve them interactively."""
    conn = _open_conn()
    try:
        if as_json:
            pending = _list_pending_with_reason(conn)
            click.echo(json_module.dumps(
                [_entry_to_dict(e, r) for e, r in pending], indent=2, default=str,
            ))
            return

        if entry_id is not None:
            entry = time_entries.get(conn, entry_id)
            if entry is None:
                click.echo(f"no entry with id={entry_id}", err=True)
                sys.exit(1)
            row = conn.execute(
                "SELECT reconciliation_reason, pending_reconciliation FROM time_entries WHERE id = ?",
                (entry_id,),
            ).fetchone()
            if not row["pending_reconciliation"]:
                click.echo(f"entry {entry_id} is not pending reconciliation", err=True)
                sys.exit(1)
            pending = [(entry, row["reconciliation_reason"])]
        else:
            pending = _list_pending_with_reason(conn)

        if not pending:
            click.echo("no pending reconciliations")
            return

        click.echo(f"{len(pending)} pending reconciliations:")
        for entry, reason in pending:
            choice = _prompt_choice(entry, reason)
            if choice == "k":
                continue
            chosen_ticket = None
            if choice == "d":
                chosen_ticket = click.prompt("New ticket for the gap")
            _apply(conn, entry, choice, chosen_ticket)
            click.echo(f"  resolved [{entry.id}] {entry.ticket_key}")
    finally:
        conn.close()
