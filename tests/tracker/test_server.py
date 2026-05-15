"""Tests for tsh.tracker.server."""

from __future__ import annotations
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from tsh.storage import db as db_module
from tsh.storage import tickets_cache, time_entries
from tsh.core.models import TimeEntry
from tsh.tracker.server import IdleState, TrackerState, create_app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "tsh.db"


@pytest.fixture
def state(db_path: Path) -> Iterator[TrackerState]:
    s = TrackerState(db_path=db_path, jira_client_factory=lambda: None)
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def client(state: TrackerState) -> TestClient:
    return TestClient(create_app(state))


# ---------------------------------------------------------------------------
# Fake Jira client
# ---------------------------------------------------------------------------


class FakeJiraClient:
    def __init__(self, issues=None, raise_on_search=False):
        self.issues = issues or []
        self.raise_on_search = raise_on_search
        self.closed = False

    def search_in_progress(self, jql_override=None):
        if self.raise_on_search:
            from tsh.jira.client import JiraServerError
            raise JiraServerError("test")
        return self.issues

    def close(self):
        self.closed = True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

UTC = timezone.utc


def _make_issue(key: str, summary: str = "Summary", status: str = "In Progress") -> dict:
    return {
        "key": key,
        "fields": {
            "summary": summary,
            "status": {"name": status},
        },
    }


def _insert_ticket(conn, ticket_key: str, summary: str = "A ticket") -> None:
    """Insert a ticket into tickets_cache so recents() returns it."""
    now = datetime.now(UTC)
    tickets_cache.upsert(
        conn,
        ticket_key=ticket_key,
        summary=summary,
        status="In Progress",
        assignee_email="user@example.com",
        last_fetched_at=now,
    )
    conn.commit()


# ---------------------------------------------------------------------------
# /status tests
# ---------------------------------------------------------------------------


class TestStatus:
    def test_empty_db_returns_no_active(self, client: TestClient) -> None:
        """1. Empty DB → status returns active=None, elapsed_seconds=0, idle_status='clear'."""
        resp = client.get("/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["active"] is None
        assert data["elapsed_seconds"] == 0
        assert data["idle_status"] == "clear"
        assert data["idle_started_at"] is None

    def test_after_start_returns_entry(self, client: TestClient) -> None:
        """2. After /start, status returns the entry with elapsed_seconds >= 0."""
        client.post("/start", json={"ticket_key": "PROJ-1"})
        resp = client.get("/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["active"] is not None
        assert data["active"]["ticket_key"] == "PROJ-1"
        assert data["elapsed_seconds"] >= 0


# ---------------------------------------------------------------------------
# /start tests
# ---------------------------------------------------------------------------


class TestStart:
    def test_start_new_ticket_creates_entry(
        self, client: TestClient, state: TrackerState
    ) -> None:
        """3. /start with new ticket → 200 with the entry; storage has 1 row with end_at=NULL."""
        resp = client.post("/start", json={"ticket_key": "PROJ-10", "note": "doing work"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["ticket_key"] == "PROJ-10"
        assert data["note"] == "doing work"
        assert data["end_at"] is None
        # Verify storage
        conn = state._connection()
        active = time_entries.get_active(conn)
        assert active is not None
        assert active.end_at is None
        assert active.ticket_key == "PROJ-10"

    def test_start_when_active_returns_409(self, client: TestClient) -> None:
        """4. /start when one is already active → 409 conflict."""
        client.post("/start", json={"ticket_key": "PROJ-1"})
        resp = client.post("/start", json={"ticket_key": "PROJ-2"})
        assert resp.status_code == 409
        assert "already active" in resp.json()["detail"]

    def test_start_updates_tickets_cache(
        self, client: TestClient, state: TrackerState
    ) -> None:
        """5. /start updates tickets_cache.last_used_at (assert via repository)."""
        # Pre-insert the ticket into cache so mark_used can update it
        conn = state._connection()
        _insert_ticket(conn, "PROJ-5")
        before = tickets_cache.get(conn, "PROJ-5")
        assert before is not None

        client.post("/start", json={"ticket_key": "PROJ-5"})
        after = tickets_cache.get(conn, "PROJ-5")
        assert after is not None
        # last_used_at should be updated to at-or-after the before value
        assert after.last_used_at >= before.last_used_at


# ---------------------------------------------------------------------------
# /switch tests
# ---------------------------------------------------------------------------


class TestSwitch:
    def test_switch_with_no_active_starts_new(
        self, client: TestClient, state: TrackerState
    ) -> None:
        """6. /switch with no active timer → starts one (effectively /start)."""
        resp = client.post("/switch", json={"ticket_key": "PROJ-20"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["ticket_key"] == "PROJ-20"
        assert data["end_at"] is None

    def test_switch_closes_old_and_opens_new(
        self, client: TestClient, state: TrackerState
    ) -> None:
        """7. /switch closes the existing active and opens a new one; old entry has end_at != None."""
        # Start first
        resp_start = client.post("/start", json={"ticket_key": "PROJ-A"})
        assert resp_start.status_code == 200
        old_id = resp_start.json()["id"]

        # Switch to another
        resp_switch = client.post("/switch", json={"ticket_key": "PROJ-B"})
        assert resp_switch.status_code == 200
        new_data = resp_switch.json()
        assert new_data["ticket_key"] == "PROJ-B"
        assert new_data["end_at"] is None

        # Old entry should now be closed
        conn = state._connection()
        old_entry = time_entries.get(conn, old_id)
        assert old_entry is not None
        assert old_entry.end_at is not None

    def test_switch_same_ticket_creates_new_entry(
        self, client: TestClient, state: TrackerState
    ) -> None:
        """8. /switch with same ticket → still creates a new entry."""
        resp1 = client.post("/start", json={"ticket_key": "PROJ-C"})
        assert resp1.status_code == 200
        id1 = resp1.json()["id"]

        resp2 = client.post("/switch", json={"ticket_key": "PROJ-C"})
        assert resp2.status_code == 200
        id2 = resp2.json()["id"]

        # Should be different entries
        assert id1 != id2
        # New entry is active, old is closed
        conn = state._connection()
        old = time_entries.get(conn, id1)
        assert old is not None
        assert old.end_at is not None


# ---------------------------------------------------------------------------
# /stop tests
# ---------------------------------------------------------------------------


class TestStop:
    def test_stop_active_timer(self, client: TestClient) -> None:
        """9. /stop with active → closes it; subsequent /status shows active=None."""
        client.post("/start", json={"ticket_key": "PROJ-30"})
        resp_stop = client.post("/stop")
        assert resp_stop.status_code == 200
        data = resp_stop.json()
        assert data["closed"] is not None
        assert data["closed"]["ticket_key"] == "PROJ-30"
        assert data["closed"]["end_at"] is not None

        # Status should now show no active timer
        resp_status = client.get("/status")
        assert resp_status.json()["active"] is None

    def test_stop_with_no_active(self, client: TestClient) -> None:
        """10. /stop with no active → returns {'closed': null} and doesn't error."""
        resp = client.post("/stop")
        assert resp.status_code == 200
        assert resp.json() == {"closed": None}


# ---------------------------------------------------------------------------
# /tasks tests
# ---------------------------------------------------------------------------


class TestTasks:
    def test_tasks_no_jira_returns_recents(
        self, db_path: Path
    ) -> None:
        """11. With jira_client_factory returning None: /tasks returns only recents."""
        state = TrackerState(db_path=db_path, jira_client_factory=lambda: None)
        conn = state._connection()
        _insert_ticket(conn, "REC-1", "Recent ticket 1")
        _insert_ticket(conn, "REC-2", "Recent ticket 2")
        c = TestClient(create_app(state))
        try:
            resp = c.get("/tasks")
            assert resp.status_code == 200
            data = resp.json()
            keys = [item["ticket_key"] for item in data]
            assert "REC-1" in keys
            assert "REC-2" in keys
            for item in data:
                assert item["source"] == "recent"
        finally:
            state.close()

    def test_tasks_with_jira_returns_in_progress_first(
        self, db_path: Path
    ) -> None:
        """12. With a fake JiraClient returning 2 in-progress: those come first, then recents (deduped)."""
        fake_issues = [
            _make_issue("JP-1", "Issue One"),
            _make_issue("JP-2", "Issue Two"),
        ]
        fake_client = FakeJiraClient(issues=fake_issues)
        state = TrackerState(
            db_path=db_path,
            jira_client_factory=lambda: FakeJiraClient(issues=fake_issues),
        )
        conn = state._connection()
        _insert_ticket(conn, "REC-10", "Recent only")
        c = TestClient(create_app(state))
        try:
            resp = c.get("/tasks")
            assert resp.status_code == 200
            data = resp.json()
            keys = [item["ticket_key"] for item in data]
            # In-progress come first
            assert keys.index("JP-1") < keys.index("REC-10")
            assert keys.index("JP-2") < keys.index("REC-10")
            # Sources are correct
            sources = {item["ticket_key"]: item["source"] for item in data}
            assert sources["JP-1"] == "in_progress"
            assert sources["JP-2"] == "in_progress"
            assert sources["REC-10"] == "recent"
        finally:
            state.close()

    def test_tasks_dedup_in_progress_wins(
        self, db_path: Path
    ) -> None:
        """13. If a recent ticket key matches an in-progress key, in-progress wins (single entry)."""
        shared_key = "BOTH-1"
        fake_issues = [_make_issue(shared_key, "From Jira")]
        state = TrackerState(
            db_path=db_path,
            jira_client_factory=lambda: FakeJiraClient(issues=fake_issues),
        )
        conn = state._connection()
        _insert_ticket(conn, shared_key, "From Cache")
        c = TestClient(create_app(state))
        try:
            resp = c.get("/tasks")
            assert resp.status_code == 200
            data = resp.json()
            matching = [item for item in data if item["ticket_key"] == shared_key]
            assert len(matching) == 1
            assert matching[0]["source"] == "in_progress"
        finally:
            state.close()

    def test_tasks_jira_error_falls_back_to_recents(
        self, db_path: Path
    ) -> None:
        """14. With a fake JiraClient raising JiraError: /tasks falls back to recents only."""
        state = TrackerState(
            db_path=db_path,
            jira_client_factory=lambda: FakeJiraClient(raise_on_search=True),
        )
        conn = state._connection()
        _insert_ticket(conn, "FALL-1", "Fallback ticket")
        c = TestClient(create_app(state))
        try:
            resp = c.get("/tasks")
            assert resp.status_code == 200
            data = resp.json()
            keys = [item["ticket_key"] for item in data]
            assert "FALL-1" in keys
            # No in_progress entries since Jira errored
            for item in data:
                assert item["source"] == "recent"
        finally:
            state.close()


# ---------------------------------------------------------------------------
# /idle/return tests
# ---------------------------------------------------------------------------


class TestIdleReturn:
    def _set_pending(self, state: TrackerState) -> datetime:
        """Set state.idle to pending with started_at equal to the active entry's start_at.

        Using active.start_at satisfies reconcile_idle's constraint
        (idle_started_at >= active.start_at). Since time has elapsed since the
        entry was inserted, returned_at (computed in the handler) will be strictly
        after idle_started_at, satisfying the second constraint too.
        If there is no active timer, uses a time safely in the past.
        """
        state.refresh()
        active = state.get_active()
        if active is not None:
            idle_start = active.start_at
        else:
            idle_start = datetime.now(UTC) - timedelta(seconds=30)
        state.idle = IdleState(status="pending", started_at=idle_start)
        return idle_start

    def test_idle_return_clear_status_returns_409(
        self, client: TestClient
    ) -> None:
        """15. With idle.status='clear': 409."""
        resp = client.post("/idle/return", json={"choice": "same"})
        assert resp.status_code == 409
        assert "pending" in resp.json()["detail"]

    def test_idle_return_same_no_db_changes(
        self, client: TestClient, state: TrackerState
    ) -> None:
        """16. choice='same': no DB changes; idle goes back to 'clear'; active stays open."""
        client.post("/start", json={"ticket_key": "SAME-1"})
        self._set_pending(state)

        conn = state._connection()
        before_active = time_entries.get_active(conn)
        assert before_active is not None

        resp = client.post("/idle/return", json={"choice": "same"})
        assert resp.status_code == 200
        data = resp.json()
        # For "same", reconcile_idle returns [active] unchanged
        assert len(data["written"]) == 1
        assert data["written"][0]["ticket_key"] == "SAME-1"
        assert data["written"][0]["end_at"] is None

        # Idle should be cleared
        assert state.idle.status == "clear"
        # Active timer should still be open
        after_active = time_entries.get_active(conn)
        assert after_active is not None
        assert after_active.id == before_active.id

    def test_idle_return_different_creates_3_entries(
        self, client: TestClient, state: TrackerState
    ) -> None:
        """17. choice='different' + chosen_ticket: 3 entries (closed + gap + resumed)."""
        client.post("/start", json={"ticket_key": "ORIG-1"})
        self._set_pending(state)

        resp = client.post(
            "/idle/return",
            json={"choice": "different", "chosen_ticket": "COL-99"},
        )
        assert resp.status_code == 200
        data = resp.json()
        written = data["written"]
        assert len(written) == 3

        # Entry 0: closed original
        assert written[0]["ticket_key"] == "ORIG-1"
        assert written[0]["end_at"] is not None

        # Entry 1: gap on COL-99
        assert written[1]["ticket_key"] == "COL-99"
        assert written[1]["end_at"] is not None

        # Entry 2: resumed active on ORIG-1 (no end_at)
        assert written[2]["ticket_key"] == "ORIG-1"
        assert written[2]["end_at"] is None

        # Idle should be cleared
        assert state.idle.status == "clear"

        # Only the resumed entry should be active now
        conn = state._connection()
        active = time_entries.get_active(conn)
        assert active is not None
        assert active.ticket_key == "ORIG-1"

    def test_idle_return_not_work_creates_3_entries(
        self, client: TestClient, state: TrackerState
    ) -> None:
        """18. choice='not_work': 3 entries (closed + not_work gap + resumed)."""
        client.post("/start", json={"ticket_key": "WORK-1"})
        self._set_pending(state)

        resp = client.post("/idle/return", json={"choice": "not_work"})
        assert resp.status_code == 200
        data = resp.json()
        written = data["written"]
        assert len(written) == 3

        # Entry 0: closed original work
        assert written[0]["ticket_key"] == "WORK-1"
        assert written[0]["end_at"] is not None
        assert written[0]["kind"] == "work"

        # Entry 1: not_work gap
        assert written[1]["kind"] == "not_work"
        assert written[1]["end_at"] is not None

        # Entry 2: resumed active on WORK-1
        assert written[2]["ticket_key"] == "WORK-1"
        assert written[2]["end_at"] is None
        assert written[2]["kind"] == "work"

        # Idle should be cleared
        assert state.idle.status == "clear"

    def test_idle_return_no_active_timer_clears_idle(
        self, client: TestClient, state: TrackerState
    ) -> None:
        """19. idle.status='pending' but no active timer: returns empty list, idle clears."""
        # Set idle pending without starting a timer
        self._set_pending(state)

        resp = client.post("/idle/return", json={"choice": "same"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["written"] == []

        # Idle should be cleared
        assert state.idle.status == "clear"


# ---------------------------------------------------------------------------
# /shutdown tests
# ---------------------------------------------------------------------------


class TestShutdown:
    def test_shutdown_endpoint_calls_callback(
        self, state: TrackerState
    ) -> None:
        """20. /shutdown is registered when shutdown_callback is provided; calling it invokes
        the callback and returns {'shutting_down': True}."""
        called = []

        def callback():
            called.append(True)

        client = TestClient(create_app(state, shutdown_callback=callback))
        resp = client.post("/shutdown")
        assert resp.status_code == 200
        assert resp.json() == {"shutting_down": True}
        assert called == [True]

    def test_shutdown_endpoint_not_registered_without_callback(
        self, client: TestClient
    ) -> None:
        """21. /shutdown is NOT registered when shutdown_callback is None (the default)."""
        # The default `client` fixture uses create_app(state) with no callback.
        resp = client.post("/shutdown")
        assert resp.status_code in (404, 405)  # route not registered


# ---------------------------------------------------------------------------
# /idle/return — pending_reconciliation flag clearing (Task 20.6)
# ---------------------------------------------------------------------------


class TestIdleReturnClearsPendingFlag:
    """POST /idle/return must clear pending_reconciliation=0 on the original row."""

    def _make_active_entry(self, state: TrackerState, ticket_key: str) -> int:
        """Insert an active entry and return its id."""
        from datetime import timedelta
        conn = state._connection()
        now = datetime.now(UTC)
        start = now - timedelta(minutes=30)
        entry = TimeEntry(
            id=None,
            ticket_key=ticket_key,
            start_at=start,
            end_at=None,
            kind="work",
            note="",
            jira_worklog_id=None,
            pushed_at=None,
            created_at=start,
            updated_at=start,
        )
        eid = time_entries.insert(conn, entry)
        conn.commit()
        state.refresh()
        return eid

    def _flag_pending(self, state: TrackerState, entry_id: int) -> None:
        """Pre-flag a row as pending_reconciliation=1 (simulating sleep/recovery path)."""
        conn = state._connection()
        time_entries.update(
            conn, entry_id,
            pending_reconciliation=1,
            reconciliation_reason="idle",
        )
        conn.commit()

    def _set_idle_pending(self, state: TrackerState) -> None:
        """Set state.idle to pending with a safe started_at."""
        from datetime import timedelta
        state.refresh()
        active = state.get_active()
        if active is not None:
            idle_start = active.start_at
        else:
            idle_start = datetime.now(UTC) - timedelta(seconds=30)
        state.idle = IdleState(status="pending", started_at=idle_start)

    def test_not_work_choice_clears_flag(self, db_path: Path) -> None:
        """22. choice='not_work': original row's pending_reconciliation must be cleared."""
        state = TrackerState(db_path=db_path, jira_client_factory=lambda: None)
        try:
            eid = self._make_active_entry(state, "SFXS-NW")
            self._flag_pending(state, eid)
            self._set_idle_pending(state)

            client = TestClient(create_app(state))
            r = client.post("/idle/return", json={"choice": "not_work"})
            assert r.status_code == 200, r.text

            conn = state._connection()
            row = conn.execute(
                "SELECT pending_reconciliation, reconciliation_reason FROM time_entries WHERE id = ?",
                (eid,),
            ).fetchone()
            assert row["pending_reconciliation"] == 0
            assert row["reconciliation_reason"] is None
            assert time_entries.count_pending_reconciliation(conn) == 0
        finally:
            state.close()

    def test_same_choice_clears_flag(self, db_path: Path) -> None:
        """23. choice='same': original row's flag must be cleared even though no entries are written."""
        state = TrackerState(db_path=db_path, jira_client_factory=lambda: None)
        try:
            eid = self._make_active_entry(state, "SFXS-SM")
            self._flag_pending(state, eid)
            self._set_idle_pending(state)

            client = TestClient(create_app(state))
            r = client.post("/idle/return", json={"choice": "same"})
            assert r.status_code == 200, r.text

            conn = state._connection()
            row = conn.execute(
                "SELECT pending_reconciliation, reconciliation_reason FROM time_entries WHERE id = ?",
                (eid,),
            ).fetchone()
            assert row["pending_reconciliation"] == 0
            assert row["reconciliation_reason"] is None
            assert time_entries.count_pending_reconciliation(conn) == 0
        finally:
            state.close()

    def test_clears_flag_idempotent_when_flag_already_zero(self, db_path: Path) -> None:
        """24. Rows with pending_reconciliation=0: clearing again is idempotent (no error)."""
        state = TrackerState(db_path=db_path, jira_client_factory=lambda: None)
        try:
            self._make_active_entry(state, "SFXS-IDP")
            # Do NOT flag the row — it stays at default 0.
            self._set_idle_pending(state)

            client = TestClient(create_app(state))
            r = client.post("/idle/return", json={"choice": "not_work"})
            assert r.status_code == 200, r.text
            # Should complete cleanly with count still 0.
            assert time_entries.count_pending_reconciliation(state._connection()) == 0
        finally:
            state.close()

