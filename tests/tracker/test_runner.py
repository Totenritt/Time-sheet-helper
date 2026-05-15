"""Tests for tsh.tracker.runner — covers startup recovery."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tsh.tracker import runner
from tsh.tracker.server import TrackerState
from tsh.storage import time_entries
from tsh.core.models import TimeEntry


@pytest.fixture
def state(tmp_path: Path):
    s = TrackerState(db_path=tmp_path / "tsh.db", jira_client_factory=lambda: None)
    try:
        yield s
    finally:
        s.close()


def _insert_active(state: TrackerState, ticket: str, start_at: datetime) -> int:
    conn = state._connection()
    eid = time_entries.insert(
        conn,
        TimeEntry(
            id=None, ticket_key=ticket, start_at=start_at, end_at=None,
            kind="work", note="", jira_worklog_id=None, pushed_at=None,
            created_at=start_at, updated_at=start_at,
        ),
    )
    conn.commit()
    return eid


def test_recovery_closes_18h_old_active_entry(state: TrackerState) -> None:
    now = datetime.now(timezone.utc)
    start_at = now - timedelta(hours=18)
    _insert_active(state, "SFXS-old", start_at)

    runner.recover_stale_active(
        state,
        stale_threshold_minutes=120,
        idle_threshold_minutes=10,
        now=now,
    )

    conn = state._connection()
    active = time_entries.get_active(conn)
    assert active is None
    row = conn.execute(
        "SELECT end_at, pending_reconciliation, reconciliation_reason "
        "FROM time_entries WHERE ticket_key = 'SFXS-old'"
    ).fetchone()
    assert row["pending_reconciliation"] == 1
    assert row["reconciliation_reason"] == "orphaned_active"
    assert row["end_at"] == start_at + timedelta(minutes=10)


def test_recovery_leaves_30m_old_entry_alone(state: TrackerState) -> None:
    now = datetime.now(timezone.utc)
    start_at = now - timedelta(minutes=30)
    _insert_active(state, "SFXS-fresh", start_at)

    runner.recover_stale_active(
        state,
        stale_threshold_minutes=120,
        idle_threshold_minutes=10,
        now=now,
    )

    conn = state._connection()
    active = time_entries.get_active(conn)
    assert active is not None
    assert active.ticket_key == "SFXS-fresh"
    assert time_entries.count_pending_reconciliation(conn) == 0


def test_recovery_noop_when_no_active_entry(state: TrackerState) -> None:
    runner.recover_stale_active(
        state,
        stale_threshold_minutes=120,
        idle_threshold_minutes=10,
        now=datetime.now(timezone.utc),
    )
    conn = state._connection()
    assert time_entries.get_active(conn) is None
    assert time_entries.count_pending_reconciliation(conn) == 0


def test_recovery_clamps_close_at_to_now_when_idle_threshold_overshoots(state: TrackerState) -> None:
    """If idle_threshold pushes close_at past now, fall back to now."""
    now = datetime.now(timezone.utc)
    # Start 3h ago, but idle_threshold is 10h: start_at + 10h would be 7h in the future.
    start_at = now - timedelta(hours=3)
    _insert_active(state, "SFXS-clamp", start_at)

    runner.recover_stale_active(
        state,
        stale_threshold_minutes=120,
        idle_threshold_minutes=10 * 60,  # 10 hours
        now=now,
    )

    conn = state._connection()
    row = conn.execute(
        "SELECT end_at FROM time_entries WHERE ticket_key = 'SFXS-clamp'"
    ).fetchone()
    # end_at should be clamped to now, NOT start_at + 10h.
    assert row["end_at"] == now
