"""Repository for the tickets_cache table.

All functions take a sqlite3.Connection whose row_factory is sqlite3.Row and
whose datetime adapter/converter are registered (as set up by
tsh.storage.db.connect).  Transaction management is the caller's
responsibility — this module does not open, commit, or close connections.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from tsh.core.models import TicketCacheEntry


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------


def _row_to_entry(row: sqlite3.Row) -> TicketCacheEntry:
    """Convert a sqlite3.Row from tickets_cache into a TicketCacheEntry."""
    return TicketCacheEntry(
        ticket_key=row["ticket_key"],
        summary=row["summary"],
        status=row["status"],
        assignee_email=row["assignee_email"],
        last_fetched_at=row["last_fetched_at"],
        last_used_at=row["last_used_at"],
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def upsert(
    conn: sqlite3.Connection,
    ticket_key: str,
    summary: str,
    status: str,
    assignee_email: str,
    last_fetched_at: datetime,
) -> None:
    """Insert or update the cache row for `ticket_key`.

    Updates summary/status/assignee_email/last_fetched_at on each call.
    DOES NOT touch last_used_at — that's mark_used's job.
    On insert (first time we see this ticket), last_used_at is initialized
    to last_fetched_at so it has a value (even though it doesn't represent
    real usage yet).
    """
    conn.execute(
        """
        INSERT INTO tickets_cache
            (ticket_key, summary, status, assignee_email, last_fetched_at, last_used_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticket_key) DO UPDATE SET
            summary          = excluded.summary,
            status           = excluded.status,
            assignee_email   = excluded.assignee_email,
            last_fetched_at  = excluded.last_fetched_at
        """,
        (ticket_key, summary, status, assignee_email, last_fetched_at, last_fetched_at),
    )


def get(conn: sqlite3.Connection, ticket_key: str) -> TicketCacheEntry | None:
    """Return the cache entry for the given ticket, or None if not cached."""
    row = conn.execute(
        "SELECT * FROM tickets_cache WHERE ticket_key = ?", (ticket_key,)
    ).fetchone()
    if row is None:
        return None
    return _row_to_entry(row)


def mark_used(
    conn: sqlite3.Connection, ticket_key: str, when: datetime | None = None
) -> None:
    """Update last_used_at for the given ticket. Idempotent: silent no-op
    if the ticket isn't in the cache yet (caller can pre-upsert if needed).

    `when` defaults to datetime.now(timezone.utc) — must be aware otherwise.
    """
    if when is None:
        when = datetime.now(timezone.utc)
    # The adapter rejects naive datetimes; ValueError propagates to the caller.
    conn.execute(
        "UPDATE tickets_cache SET last_used_at = ? WHERE ticket_key = ?",
        (when, ticket_key),
    )


def recents(conn: sqlite3.Connection, limit: int = 10) -> list[TicketCacheEntry]:
    """Return up to `limit` cached tickets ordered by last_used_at descending.

    Useful for the picker's "recently logged" section.
    """
    rows = conn.execute(
        "SELECT * FROM tickets_cache ORDER BY last_used_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [_row_to_entry(row) for row in rows]


def is_fresh(
    conn: sqlite3.Connection,
    ticket_key: str,
    ttl_minutes: int = 5,
    now: datetime | None = None,
) -> bool:
    """Return True if the ticket is cached and last_fetched_at is within
    ttl_minutes of `now` (default: datetime.now(timezone.utc)).

    Returns False if the ticket isn't cached or the cache is stale.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    entry = get(conn, ticket_key)
    if entry is None:
        return False
    age = now - entry.last_fetched_at
    return age <= timedelta(minutes=ttl_minutes)
