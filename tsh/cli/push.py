"""tsh push — push drafts for a day to Jira (with --dry-run)."""

from __future__ import annotations
import json
from datetime import datetime, timezone

import click

from tsh.cli._db import open_db
from tsh.cli.review import _parse_day  # reuse the today/yesterday/YYYY-MM-DD parser
from tsh.config import credentials, loader
from tsh.jira import push as jira_push
from tsh.jira.client import JiraClient, JiraError
from tsh.storage import time_entries


@click.command("push")
@click.option("--day", default=None, help="today | yesterday | YYYY-MM-DD")
@click.option("--dry-run", is_flag=True, help="Print planned payloads without sending to Jira.")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of a table.")
def push(day: str | None, dry_run: bool, as_json: bool) -> None:
    """Push the day's draft entries to Jira (sum-then-rounded per ticket)."""
    cfg = loader.load()
    tz = cfg["time"]["display_timezone"]
    mode = cfg["time"]["rounding"]
    bucket = cfg["time"]["rounding_minutes"]
    base_url = cfg["jira"].get("base_url", "")
    email = cfg["jira"].get("email", "")

    target_day = _parse_day(day, tz)

    # Build client only for the real-push path (dry-run never needs auth).
    if not dry_run:
        if not base_url or not email:
            click.echo("Jira not configured. Run `tsh auth login` first.", err=True)
            raise click.exceptions.Exit(2)
        token = credentials.get_token(email)
        if not token:
            click.echo(
                f"No token in keyring for {email}. Run `tsh auth login`.", err=True
            )
            raise click.exceptions.Exit(2)

    conn = open_db()
    try:
        entries = time_entries.list_by_day(conn, target_day, tz=tz)
        planned = jira_push.plan_push(entries, mode=mode, minutes=bucket, tz=tz)

        if not planned:
            click.echo(f"No drafts to push for {target_day} ({tz}).")
            return

        if dry_run:
            results = jira_push.execute_push(planned, client=None, dry_run=True)
            _emit(results, planned, as_json, dry_run=True)
            return

        client = JiraClient(base_url=base_url, email=email, token=token)
        try:
            results = jira_push.execute_push(planned, client, dry_run=False)
        except JiraError as exc:
            # Connection-level failure that escapes per-entry isolation —
            # surface it and exit non-zero.
            click.echo(f"Jira error: {exc}", err=True)
            raise click.exceptions.Exit(1) from exc
        finally:
            client.close()

        # Mark successful entries as pushed in storage. Each result references
        # the entry_ids that contributed to the worklog; on success, all of
        # them get the same jira_worklog_id and pushed_at.
        now_utc = datetime.now(timezone.utc)
        for r in results:
            if not r.ok or r.jira_worklog_id is None:
                continue
            for eid in r.entry_ids:
                time_entries.mark_pushed(conn, eid, r.jira_worklog_id, now_utc)
        conn.commit()

        _emit(results, planned, as_json, dry_run=False)

        # Non-zero exit if any push failed.
        if any(not r.ok for r in results):
            raise click.exceptions.Exit(1)
    finally:
        conn.close()


def _emit(
    results: list[jira_push.PushResult],
    planned: list[jira_push.PlannedPush],
    as_json: bool,
    *,
    dry_run: bool,
) -> None:
    plan_by_ids = {p.entry_ids: p for p in planned}
    if as_json:
        rows = []
        for r in results:
            p = plan_by_ids.get(r.entry_ids)
            rows.append(
                {
                    "entry_ids": list(r.entry_ids),
                    "ticket": r.ticket_key,
                    "ok": r.ok,
                    "dry_run": r.dry_run,
                    "jira_worklog_id": r.jira_worklog_id,
                    "error": r.error,
                    "started": p.started_iso if p else None,
                    "time_spent_seconds": p.time_spent_seconds if p else None,
                    "comment": p.comment if p else None,
                }
            )
        click.echo(json.dumps(rows, indent=2))
        return
    label = "DRY RUN" if dry_run else "PUSH RESULTS"
    click.echo(label)
    click.echo("-" * 78)
    click.echo(
        f"{'Ticket':<14}  {'Started':<28}  {'Spent':>7}  Status  Comment / Error"
    )
    for r in results:
        p = plan_by_ids.get(r.entry_ids)
        spent_min = (p.time_spent_seconds // 60) if p else 0
        spent = f"{spent_min // 60}h {spent_min % 60}m" if spent_min >= 60 else f"{spent_min}m"
        if r.dry_run:
            status = "PLAN"
            tail = (p.comment if p else "") or ""
        elif r.ok:
            status = "OK"
            tail = f"worklog={r.jira_worklog_id}"
        else:
            status = "FAIL"
            tail = r.error or ""
        started = p.started_iso if p else ""
        click.echo(
            f"{r.ticket_key:<14}  {started:<28}  {spent:>7}  {status:<6}  {tail}"
        )
    click.echo("-" * 78)
