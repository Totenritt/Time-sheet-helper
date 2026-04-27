"""tsh log/review/edit/delete — manage stored time entries."""

from __future__ import annotations
import json
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable
from zoneinfo import ZoneInfo

import click
import tomllib
import tomli_w

from tsh.cli._db import open_db
from tsh.config import loader
from tsh.core import time_math
from tsh.core.models import TimeEntry
from tsh.core.timezones import to_display, today_in_display_tz
from tsh.storage import time_entries


# --- duration parsing --------------------------------------------------------

_DURATION_RE = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?$")


def _parse_duration(s: str) -> int:
    """Parse '45m', '1h', '1h30m' to seconds. Raises click.BadParameter on bad input."""
    if not s:
        raise click.BadParameter("duration is required, e.g. 45m or 1h30m")
    match = _DURATION_RE.match(s.strip())
    if not match or match.group(0) == "":
        raise click.BadParameter(f"invalid duration {s!r}; expected like 45m or 1h30m")
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    if hours == 0 and minutes == 0:
        raise click.BadParameter(f"duration {s!r} must be > 0")
    return (hours * 60 + minutes) * 60


# --- day parsing -------------------------------------------------------------

def _parse_day(s: str | None, tz: str) -> date:
    """'today', 'yesterday', or YYYY-MM-DD. Default 'today' if None."""
    s = (s or "today").lower()
    today = today_in_display_tz(tz=tz)
    if s == "today":
        return today
    if s == "yesterday":
        return today - timedelta(days=1)
    try:
        return date.fromisoformat(s)
    except ValueError as e:
        raise click.BadParameter(
            f"invalid --day {s!r}; expected today, yesterday, or YYYY-MM-DD"
        ) from e


# --- time parsing for --at ---------------------------------------------------

def _parse_at(s: str | None, day: date, tz: str) -> datetime | None:
    """Parse --at HH:MM into an aware datetime on `day` in `tz`. None → now-utc."""
    if s is None:
        return None
    try:
        hh, mm = s.split(":", 1)
        t = time(hour=int(hh), minute=int(mm))
    except ValueError as e:
        raise click.BadParameter(f"invalid --at {s!r}; expected HH:MM") from e
    return datetime.combine(day, t, tzinfo=ZoneInfo(tz))


# --- formatting helpers ------------------------------------------------------

def _fmt_duration_seconds(s: int) -> str:
    """Format seconds as '1h 15m' / '45m' / '0m'."""
    hours, rem = divmod(s, 3600)
    minutes = rem // 60
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _fmt_hhmm_local(dt: datetime, tz: str) -> str:
    return to_display(dt, tz=tz).strftime("%H:%M")


# --- log ---------------------------------------------------------------------

@click.command("log")
@click.argument("ticket")
@click.argument("duration")
@click.option("-m", "--note", default="", help="Worklog note for this entry.")
@click.option("--at", "at_time", default=None, help="End time HH:MM in display tz; default = now.")
def log(ticket: str, duration: str, note: str, at_time: str | None) -> None:
    """Manually log a finished entry: tsh log SFXS-1073 45m -m \"PR review\"."""
    cfg = loader.load()
    tz = cfg["time"]["display_timezone"]
    dur = _parse_duration(duration)
    today = today_in_display_tz(tz=tz)
    end_dt_local = _parse_at(at_time, today, tz)
    end_dt = end_dt_local.astimezone(timezone.utc) if end_dt_local else datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(seconds=dur)
    now_utc = datetime.now(timezone.utc)
    entry = TimeEntry(
        id=None,
        ticket_key=ticket,
        start_at=start_dt,
        end_at=end_dt,
        kind="work",
        note=note,
        jira_worklog_id=None,
        pushed_at=None,
        created_at=now_utc,
        updated_at=now_utc,
    )
    conn = open_db()
    try:
        new_id = time_entries.insert(conn, entry)
        conn.commit()
    finally:
        conn.close()
    click.echo(f"Logged entry {new_id}: {ticket} {_fmt_duration_seconds(dur)}")


# --- review ------------------------------------------------------------------

@click.command("review")
@click.option("--day", default=None, help="today | yesterday | YYYY-MM-DD")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of a table.")
def review(day: str | None, as_json: bool) -> None:
    """Show entries for a day with raw and rounded durations."""
    cfg = loader.load()
    tz = cfg["time"]["display_timezone"]
    mode = cfg["time"]["rounding"]
    bucket = cfg["time"]["rounding_minutes"]
    target_day = _parse_day(day, tz)
    conn = open_db()
    try:
        entries = time_entries.list_by_day(conn, target_day, tz=tz)
    finally:
        conn.close()
    if as_json:
        rows = [_to_review_dict(e, tz, mode, bucket) for e in entries]
        click.echo(json.dumps(rows, indent=2, default=str))
        return
    _print_review_table(entries, target_day, tz, mode, bucket)


def _to_review_dict(e: TimeEntry, tz: str, mode: str, bucket: int) -> dict:
    raw_seconds = int((e.end_at - e.start_at).total_seconds()) if e.end_at else 0
    rounded = time_math.round_seconds(raw_seconds, mode=mode, minutes=bucket)
    return {
        "id": e.id,
        "ticket": e.ticket_key,
        "kind": e.kind,
        "start": _fmt_hhmm_local(e.start_at, tz) if e.start_at else None,
        "end": _fmt_hhmm_local(e.end_at, tz) if e.end_at else None,
        "raw_seconds": raw_seconds,
        "rounded_seconds": rounded,
        "note": e.note,
        "pushed": e.is_pushed,
    }


def _print_review_table(
    entries: list[TimeEntry], day: date, tz: str, mode: str, bucket: int
) -> None:
    if not entries:
        click.echo(f"No entries for {day} ({tz}).")
        return
    click.echo(f"Day {day} ({tz})")
    click.echo("-" * 78)
    click.echo(f"{'ID':>4}  {'Time':<13}  {'Ticket':<14}  {'Raw':>7}  {'Round':>7}  Status  Note")
    total_raw = 0
    total_rounded_per_ticket: dict[str, int] = {}
    for e in entries:
        raw = int((e.end_at - e.start_at).total_seconds()) if e.end_at else 0
        rounded = time_math.round_seconds(raw, mode=mode, minutes=bucket)
        total_raw += raw
        ticket_label = e.ticket_key or ("-" if e.kind != "work" else "?")
        if e.ticket_key:
            total_rounded_per_ticket[e.ticket_key] = (
                total_rounded_per_ticket.get(e.ticket_key, 0) + raw
            )
        status = "pushed" if e.is_pushed else ("active" if e.is_active else "draft")
        time_range = (
            f"{_fmt_hhmm_local(e.start_at, tz)}-{_fmt_hhmm_local(e.end_at, tz)}"
            if e.end_at
            else f"{_fmt_hhmm_local(e.start_at, tz)}-now"
        )
        click.echo(
            f"{e.id:>4}  {time_range:<13}  {ticket_label:<14}  "
            f"{_fmt_duration_seconds(raw):>7}  {_fmt_duration_seconds(rounded):>7}  "
            f"{status:<6}  {e.note}"
        )
    click.echo("-" * 78)
    rounded_totals = {
        k: time_math.round_seconds(v, mode=mode, minutes=bucket)
        for k, v in total_rounded_per_ticket.items()
    }
    rounded_total = sum(rounded_totals.values())
    click.echo(
        f"Total raw {_fmt_duration_seconds(total_raw)}  |  "
        f"rounded (sum-then-round per ticket) {_fmt_duration_seconds(rounded_total)}"
    )


# --- edit --------------------------------------------------------------------

_EDITABLE_FIELDS = ("ticket_key", "start_at", "end_at", "note", "kind")


@click.command("edit")
@click.argument("entry_id", type=int)
def edit(entry_id: int) -> None:
    """Edit a draft entry interactively in $EDITOR (TOML format)."""
    conn = open_db()
    try:
        e = time_entries.get(conn, entry_id)
        if e is None:
            click.echo(f"No entry with id {entry_id}.", err=True)
            raise click.exceptions.Exit(1)
        if e.is_pushed:
            click.echo(
                f"Entry {entry_id} is already pushed; pushed entries are read-only.",
                err=True,
            )
            raise click.exceptions.Exit(1)
        # Render current entry as TOML.
        current = {
            "ticket_key": e.ticket_key or "",
            "start_at": e.start_at.isoformat(),
            "end_at": e.end_at.isoformat() if e.end_at else "",
            "note": e.note,
            "kind": e.kind,
        }
        text = tomli_w.dumps(current)
        edited = click.edit(text, extension=".toml")
        if edited is None or edited.strip() == text.strip():
            click.echo("No changes.")
            return
        try:
            data = tomllib.loads(edited)
        except tomllib.TOMLDecodeError as exc:
            click.echo(f"Invalid TOML: {exc}", err=True)
            raise click.exceptions.Exit(1) from exc
        # Compute updates: only changed fields, only allowed fields.
        # NOTE: a key omitted from the edited TOML is treated as "no change",
        # not "clear this field". To clear a string field, set it to "" in the
        # TOML; to clear end_at (resume the timer as active), set end_at = "".
        # Removing the line entirely will leave the stored value untouched.
        updates: dict = {}
        for key in _EDITABLE_FIELDS:
            if key not in data:
                continue
            new_val = data[key]
            if key == "ticket_key":
                new_val = new_val or None
            if key in ("start_at", "end_at"):
                if new_val == "":
                    new_val = None
                else:
                    try:
                        new_val = (
                            datetime.fromisoformat(new_val)
                            if isinstance(new_val, str)
                            else new_val
                        )
                    except ValueError as exc:
                        click.echo(f"Invalid {key}: {new_val!r}", err=True)
                        raise click.exceptions.Exit(1) from exc
                    if new_val is not None and new_val.tzinfo is None:
                        click.echo(f"{key} must include a timezone offset", err=True)
                        raise click.exceptions.Exit(1)
            updates[key] = new_val
        try:
            time_entries.update(conn, entry_id, **updates)
            conn.commit()
        except ValueError as exc:
            click.echo(f"Update failed: {exc}", err=True)
            raise click.exceptions.Exit(1) from exc
        click.echo(f"Updated entry {entry_id}.")
    finally:
        conn.close()


# --- delete ------------------------------------------------------------------

@click.command("delete")
@click.argument("entry_id", type=int)
def delete(entry_id: int) -> None:
    """Delete a draft entry. Pushed entries refuse."""
    conn = open_db()
    try:
        try:
            time_entries.delete(conn, entry_id)
            conn.commit()
        except ValueError as exc:
            click.echo(str(exc), err=True)
            raise click.exceptions.Exit(1) from exc
    finally:
        conn.close()
    click.echo(f"Deleted entry {entry_id}.")
