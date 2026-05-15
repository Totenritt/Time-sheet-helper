"""tsh tracker — FastAPI HTTP server.

The tracker is a long-running process that holds an in-memory view of the
active timer (mirrored to SQLite) and exposes a small HTTP API on
127.0.0.1. Both the CLI (Task 19) and the GUI (Phase 7) drive the same
endpoints.

Idle detection (Task 17), tray icon (Task 18), and the asyncio runner that
schedules the 60-second flush (Task 19) are separate modules; this file
intentionally has no asyncio scheduling — the FlushTask interface is just
``state.flush()`` which the runner calls on a timer.
"""

from __future__ import annotations
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from tsh.config import loader
from tsh.core.models import TimeEntry
from tsh.core.reconcile import ReconcileChoice, reconcile_idle
from tsh.core.timezones import today_in_display_tz  # noqa: F401 — available for callers
from tsh.jira.client import JiraClient, JiraError
from tsh.storage import db as db_module
from tsh.storage import tickets_cache, time_entries


# ----- TrackerState ---------------------------------------------------------


IdleStatus = Literal["clear", "pending", "autostopped"]


@dataclass
class IdleState:
    """In-memory idle tracking. Idle detection updates this; reconciliation reads it."""
    status: IdleStatus = "clear"
    started_at: datetime | None = None  # when the user went idle
    autostopped_at: datetime | None = None  # when threshold breached without return


@dataclass
class TrackerState:
    """In-memory state mirrored to SQLite.

    The active TimeEntry is the source of truth for the live timer. It's
    held in memory between flushes so the server can answer /status quickly
    without a DB round-trip; on /start, /switch, /stop, /idle/return the
    storage is updated within the same request.

    The 60-second flush (called externally by the runner) re-reads the active
    entry from the DB, so a CLI that wrote directly to SQLite while the
    tracker was paused (e.g. "tsh log" outside the tracker) gets reflected.
    """

    db_path: object  # Path | str — passed to db.connect()
    jira_client_factory: Callable[[], JiraClient | None]  # builds a fresh client (uses keyring + config)
    idle: IdleState = field(default_factory=IdleState)
    _active_cache: TimeEntry | None = field(default=None, repr=False)
    _conn: sqlite3.Connection | None = field(default=None, repr=False)

    def _connection(self) -> sqlite3.Connection:
        """Lazily-open and reuse a single connection for this TrackerState instance."""
        if self._conn is None:
            self._conn = db_module.connect(self.db_path)
        return self._conn

    def close(self) -> None:
        """Close the DB connection. Idempotent."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def refresh(self) -> None:
        """Reload the active entry from SQLite into the cache."""
        self._active_cache = time_entries.get_active(self._connection())

    def get_active(self) -> TimeEntry | None:
        """Return the cached active entry (call refresh() first if you need fresh data)."""
        return self._active_cache

    def start(self, ticket_key: str, note: str) -> TimeEntry:
        """Open a new active timer. Errors if one is already active."""
        conn = self._connection()
        if time_entries.get_active(conn) is not None:
            raise ValueError("a timer is already active; switch or stop first")
        now_utc = datetime.now(timezone.utc)
        entry = TimeEntry(
            id=None,
            ticket_key=ticket_key,
            start_at=now_utc,
            end_at=None,
            kind="work",
            note=note,
            jira_worklog_id=None,
            pushed_at=None,
            created_at=now_utc,
            updated_at=now_utc,
        )
        new_id = time_entries.insert(conn, entry)
        conn.commit()
        # Reflect last_used_at on the ticket for the picker recents.
        tickets_cache.mark_used(conn, ticket_key, when=now_utc)
        conn.commit()
        self.refresh()
        return self._active_cache  # type: ignore[return-value]

    def stop(self) -> TimeEntry | None:
        """Close the active timer; returns the closed entry, or None if nothing was active."""
        conn = self._connection()
        now_utc = datetime.now(timezone.utc)
        closed = time_entries.end_active(conn, now_utc)
        conn.commit()
        self.refresh()
        return closed

    def switch(self, ticket_key: str, note: str) -> TimeEntry:
        """End the active timer (if any) and open a new one for ticket_key."""
        self.stop()
        return self.start(ticket_key, note)

    def flush(self) -> None:
        """60-second flush (called by the runner). For the MVP this is just a refresh —
        the active row's end_at stays NULL while it's running, and downstream queries
        compute elapsed from start_at to now. This hook exists so the runner has a
        clear method to call; future versions might write a heartbeat timestamp.
        """
        self.refresh()

    def reconcile_idle_return(
        self, choice: ReconcileChoice, chosen_ticket: str | None
    ) -> list[TimeEntry]:
        """Apply the user's choice for the current idle period.

        Updates SQLite atomically per spec §5.4 and clears the idle state.
        Returns the list of entries that were written/updated.
        """
        if self.idle.status != "pending":
            raise ValueError("no idle period is pending")
        idle_started_at = self.idle.started_at
        if idle_started_at is None:
            raise ValueError("idle.started_at is not set")
        active = time_entries.get_active(self._connection())
        if active is None:
            # Nothing was active — just clear the idle marker.
            self.idle = IdleState()
            self.refresh()
            return []
        returned_at = datetime.now(timezone.utc)
        new_entries = reconcile_idle(
            active, idle_started_at, returned_at, choice, chosen_ticket=chosen_ticket
        )
        conn = self._connection()
        try:
            with db_module.tx(conn):
                if choice == "same":
                    # No change. Still clear idle marker.
                    pass
                else:
                    # entries[0] = closed original (existing id, new end_at)
                    closed = new_entries[0]
                    assert closed.id is not None
                    time_entries.update(
                        conn, closed.id, end_at=closed.end_at
                    )
                    # entries[1] = gap entry (new)
                    gap = new_entries[1]
                    time_entries.insert(conn, gap)
                    # entries[2] = resumed original task as a fresh active row.
                    # We have to first ensure no other active row exists (the closed one
                    # was just closed, so we're clear). Then insert the new active.
                    resumed = new_entries[2]
                    time_entries.insert(conn, resumed)
        except Exception:
            # Re-raise so the HTTP layer surfaces it; idle stays pending so user can retry.
            raise

        # Clear pending_reconciliation on the original entry — symmetric with the
        # sleep handler / startup recovery paths that flag rows in storage.
        # Idempotent: clearing 0→0 is harmless; clears 1→0 when flagged by those paths.
        if active.id is not None:
            time_entries.update(
                conn, active.id,
                pending_reconciliation=0,
                reconciliation_reason=None,
            )
            conn.commit()

        self.idle = IdleState()
        self.refresh()
        return new_entries


# ----- Pydantic request/response models -------------------------------------


class StartRequest(BaseModel):
    ticket_key: str = Field(..., min_length=1)
    note: str = ""


class SwitchRequest(StartRequest):
    pass


class IdleReturnRequest(BaseModel):
    choice: Literal["same", "different", "not_work"]
    chosen_ticket: str | None = None


class StatusResponse(BaseModel):
    active: dict | None
    elapsed_seconds: int
    idle_status: IdleStatus
    idle_started_at: datetime | None


class TaskListItem(BaseModel):
    ticket_key: str
    summary: str
    status: str
    source: Literal["in_progress", "recent"]


# ----- App factory ----------------------------------------------------------


def _entry_to_dict(e: TimeEntry) -> dict:
    return {
        "id": e.id,
        "ticket_key": e.ticket_key,
        "start_at": e.start_at.isoformat() if e.start_at else None,
        "end_at": e.end_at.isoformat() if e.end_at else None,
        "kind": e.kind,
        "note": e.note,
        "jira_worklog_id": e.jira_worklog_id,
        "pushed_at": e.pushed_at.isoformat() if e.pushed_at else None,
    }


def create_app(
    state: TrackerState,
    *,
    shutdown_callback: Callable[[], None] | None = None,
) -> FastAPI:
    """Build a FastAPI app wired to the given TrackerState."""
    app = FastAPI(title="tsh tracker")

    @app.get("/status", response_model=StatusResponse)
    def get_status() -> StatusResponse:
        state.refresh()
        active = state.get_active()
        elapsed = 0
        if active and active.start_at:
            elapsed = int((datetime.now(timezone.utc) - active.start_at).total_seconds())
        return StatusResponse(
            active=_entry_to_dict(active) if active else None,
            elapsed_seconds=elapsed,
            idle_status=state.idle.status,
            idle_started_at=state.idle.started_at,
        )

    @app.post("/start")
    def post_start(req: StartRequest) -> dict:
        try:
            entry = state.start(req.ticket_key, req.note)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _entry_to_dict(entry)

    @app.post("/switch")
    def post_switch(req: SwitchRequest) -> dict:
        entry = state.switch(req.ticket_key, req.note)
        return _entry_to_dict(entry)

    @app.post("/stop")
    def post_stop() -> dict:
        closed = state.stop()
        if closed is None:
            return {"closed": None}
        return {"closed": _entry_to_dict(closed)}

    @app.get("/tasks")
    def get_tasks() -> list[TaskListItem]:
        """Return the picker list: in-progress tickets first, then recents (deduped)."""
        cfg = loader.load()
        items: list[TaskListItem] = []
        seen: set[str] = set()
        # In-progress from Jira (skip silently if no client / Jira down).
        client = state.jira_client_factory()
        if client is not None:
            try:
                issues = client.search_in_progress(jql_override=cfg["jira"].get("picker_jql"))
                for issue in issues:
                    key = issue.get("key", "")
                    if not key or key in seen:
                        continue
                    fields = issue.get("fields", {})
                    items.append(
                        TaskListItem(
                            ticket_key=key,
                            summary=fields.get("summary", ""),
                            status=(fields.get("status") or {}).get("name", ""),
                            source="in_progress",
                        )
                    )
                    seen.add(key)
            except JiraError:
                # Picker is best-effort — if Jira is unreachable, fall back to recents only.
                pass
            finally:
                client.close()
        # Recents from the local cache.
        recents = tickets_cache.recents(state._connection(), limit=10)
        for tc in recents:
            if tc.ticket_key in seen:
                continue
            items.append(
                TaskListItem(
                    ticket_key=tc.ticket_key,
                    summary=tc.summary,
                    status=tc.status,
                    source="recent",
                )
            )
            seen.add(tc.ticket_key)
        return items

    @app.post("/idle/return")
    def post_idle_return(req: IdleReturnRequest) -> dict:
        try:
            written = state.reconcile_idle_return(req.choice, req.chosen_ticket)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"written": [_entry_to_dict(e) for e in written]}

    if shutdown_callback is not None:
        @app.post("/shutdown")
        def post_shutdown() -> dict:
            shutdown_callback()
            return {"shutting_down": True}

    return app
