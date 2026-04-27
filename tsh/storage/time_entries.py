"""Repository for the time_entries table.

All functions take a sqlite3.Connection whose row_factory is sqlite3.Row and
whose datetime adapter/converter are registered (as set up by
tsh.storage.db.connect).  Transaction management is the caller's
responsibility — this module does not open, commit, or close connections.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from tsh.core.models import Kind, TimeEntry

# Fields the caller is not allowed to set via update().
_READONLY_FIELDS = frozenset({"id", "created_at", "updated_at"})

# All mutable fields accepted by update().
_MUTABLE_FIELDS = frozenset(
    {"ticket_key", "start_at", "end_at", "note", "kind", "jira_worklog_id", "pushed_at"}
)

UTC = timezone.utc


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------


def _row_to_entry(row: sqlite3.Row) -> TimeEntry:
    """Convert a sqlite3.Row from time_entries into a TimeEntry instance."""
    return TimeEntry(
        id=row["id"],
        ticket_key=row["ticket_key"],
        start_at=row["start_at"],
        end_at=row["end_at"],
        kind=row["kind"],  # type: ignore[arg-type]
        note=row["note"] or "",
        jira_worklog_id=row["jira_worklog_id"],
        pushed_at=row["pushed_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def insert(conn: sqlite3.Connection, entry: TimeEntry) -> int:
    """Insert a new entry. Returns the auto-assigned id.

    The TimeEntry's id field is ignored on insert (use update() to modify).
    Raises sqlite3.IntegrityError if the partial unique index (one_active)
    would be violated, or if the kind CHECK constraint fails.
    """
    cursor = conn.execute(
        """
        INSERT INTO time_entries
            (ticket_key, start_at, end_at, note, kind,
             jira_worklog_id, pushed_at, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            entry.ticket_key,
            entry.start_at,
            entry.end_at,
            entry.note,
            entry.kind,
            entry.jira_worklog_id,
            entry.pushed_at,
            entry.created_at,
            entry.updated_at,
        ),
    )
    return cursor.lastrowid  # type: ignore[return-value]


def get(conn: sqlite3.Connection, entry_id: int) -> TimeEntry | None:
    """Return the entry with the given id, or None if not found."""
    row = conn.execute(
        "SELECT * FROM time_entries WHERE id = ?", (entry_id,)
    ).fetchone()
    if row is None:
        return None
    return _row_to_entry(row)


def get_active(conn: sqlite3.Connection) -> TimeEntry | None:
    """Return the currently-active work entry (kind='work' AND end_at IS NULL),
    or None if there is no active timer.
    """
    row = conn.execute(
        "SELECT * FROM time_entries WHERE kind = 'work' AND end_at IS NULL"
    ).fetchone()
    if row is None:
        return None
    return _row_to_entry(row)


def end_active(conn: sqlite3.Connection, end_at: datetime) -> TimeEntry | None:
    """Close the currently-active work entry by setting its end_at and
    updated_at fields. Returns the updated entry, or None if there was no
    active timer.

    end_at must be timezone-aware (the adapter enforces this).
    """
    active = get_active(conn)
    if active is None:
        return None
    now = datetime.now(UTC)
    conn.execute(
        "UPDATE time_entries SET end_at = ?, updated_at = ? WHERE id = ?",
        (end_at, now, active.id),
    )
    return get(conn, active.id)  # type: ignore[arg-type]


def list_by_day(
    conn: sqlite3.Connection, day: date, tz: str = "Australia/Sydney"
) -> list[TimeEntry]:
    """Return all entries whose start_at falls on the given calendar day in tz.

    Entries are returned in start_at order.
    'Day' is interpreted as midnight-to-midnight in tz, then converted to UTC
    for the SQL comparison. Pushed and draft entries both included.
    """
    zone = ZoneInfo(tz)
    start_of_day_local = datetime.combine(day, time.min, tzinfo=zone)
    end_of_day_local = start_of_day_local + timedelta(days=1)

    # Convert boundaries to UTC for the SQL WHERE clause.
    start_utc = start_of_day_local.astimezone(UTC)
    end_utc = end_of_day_local.astimezone(UTC)

    rows = conn.execute(
        """
        SELECT * FROM time_entries
        WHERE start_at >= ? AND start_at < ?
        ORDER BY start_at ASC
        """,
        (start_utc, end_utc),
    ).fetchall()
    return [_row_to_entry(row) for row in rows]


def update(conn: sqlite3.Connection, entry_id: int, **fields: Any) -> TimeEntry:
    """Update arbitrary fields on the given entry. Sets updated_at to now-UTC.

    Allowed fields: ticket_key, start_at, end_at, note, kind, jira_worklog_id,
    pushed_at. Attempting to update id, created_at, or updated_at directly
    raises ValueError. Returns the updated entry.

    Raises ValueError if entry_id does not exist.
    """
    # Guard: reject bookkeeping columns.
    for key in fields:
        if key in _READONLY_FIELDS:
            raise ValueError(
                f"Cannot update read-only field '{key}' via update(). "
                f"Allowed fields: {sorted(_MUTABLE_FIELDS)}."
            )

    # Verify the entry exists.
    if get(conn, entry_id) is None:
        raise ValueError(f"No time_entry with id={entry_id}.")

    now = datetime.now(UTC)
    # Always bump updated_at.
    set_clauses = [f"{key} = ?" for key in fields]
    set_clauses.append("updated_at = ?")

    values = list(fields.values())
    values.append(now)
    values.append(entry_id)

    conn.execute(
        f"UPDATE time_entries SET {', '.join(set_clauses)} WHERE id = ?",  # noqa: S608
        values,
    )
    return get(conn, entry_id)  # type: ignore[return-value]


def delete(conn: sqlite3.Connection, entry_id: int) -> None:
    """Delete the entry with the given id.

    Raises ValueError if the entry has been pushed (pushed_at is not None) —
    the spec forbids deleting pushed entries from inside the tool.
    Raises ValueError if entry_id does not exist.
    """
    entry = get(conn, entry_id)
    if entry is None:
        raise ValueError(f"No time_entry with id={entry_id}.")
    if entry.is_pushed:
        raise ValueError(
            f"Cannot delete pushed entry id={entry_id} "
            f"(pushed_at={entry.pushed_at!r}). Unpush it in Jira first."
        )
    conn.execute("DELETE FROM time_entries WHERE id = ?", (entry_id,))


def mark_pushed(
    conn: sqlite3.Connection,
    entry_id: int,
    jira_worklog_id: str,
    pushed_at: datetime,
) -> TimeEntry:
    """Mark the entry as pushed to Jira. Returns the updated entry.

    Raises ValueError if entry_id does not exist.
    Raises ValueError if the entry is already pushed (idempotency guard).
    """
    entry = get(conn, entry_id)
    if entry is None:
        raise ValueError(f"No time_entry with id={entry_id}.")
    if entry.is_pushed:
        raise ValueError(
            f"Entry id={entry_id} is already pushed "
            f"(jira_worklog_id={entry.jira_worklog_id!r}, "
            f"pushed_at={entry.pushed_at!r})."
        )
    now = datetime.now(UTC)
    conn.execute(
        """
        UPDATE time_entries
        SET jira_worklog_id = ?, pushed_at = ?, updated_at = ?
        WHERE id = ?
        """,
        (jira_worklog_id, pushed_at, now, entry_id),
    )
    return get(conn, entry_id)  # type: ignore[return-value]
