"""Tests for tsh.storage.time_entries."""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

from tsh.core.models import TimeEntry
from tsh.storage import time_entries as te

UTC = timezone.utc

# ---------------------------------------------------------------------------
# Test helper
# ---------------------------------------------------------------------------


def _entry(
    start_at=None,
    end_at=None,
    ticket_key="SFXS-1073",
    kind="work",
    note="",
    **kw,
):
    """Construct a TimeEntry for tests, defaulting to a sane work entry."""
    now = datetime.now(UTC)
    return TimeEntry(
        id=None,
        ticket_key=ticket_key,
        start_at=start_at if start_at is not None else now,
        end_at=end_at,
        kind=kind,  # type: ignore[arg-type]
        note=note,
        jira_worklog_id=kw.get("jira_worklog_id"),
        pushed_at=kw.get("pushed_at"),
        created_at=kw.get("created_at", now),
        updated_at=kw.get("updated_at", now),
    )


# ---------------------------------------------------------------------------
# insert + get
# ---------------------------------------------------------------------------


class TestInsertGet:
    def test_insert_returns_positive_int_and_get_round_trips(self, db_conn):
        """insert returns a positive integer id; get returns matching TimeEntry."""
        entry = _entry(note="first entry")
        entry_id = te.insert(db_conn, entry)

        assert isinstance(entry_id, int)
        assert entry_id > 0

        result = te.get(db_conn, entry_id)
        assert result is not None
        assert result.id == entry_id
        assert result.ticket_key == entry.ticket_key
        assert result.note == "first entry"
        assert result.kind == "work"
        # Datetimes must be tz-aware UTC after round-trip
        assert result.start_at.tzinfo is not None
        assert result.created_at.tzinfo is not None
        assert result.updated_at.tzinfo is not None
        # Same instant preserved
        assert result.start_at.timestamp() == entry.start_at.timestamp()

    def test_get_nonexistent_returns_none(self, db_conn):
        """get with an id that doesn't exist returns None."""
        result = te.get(db_conn, 999_999)
        assert result is None


# ---------------------------------------------------------------------------
# get_active
# ---------------------------------------------------------------------------


class TestGetActive:
    def test_get_active_returns_none_when_no_entries(self, db_conn):
        """get_active returns None when the table is empty."""
        assert te.get_active(db_conn) is None

    def test_get_active_returns_none_when_only_closed_entries(self, db_conn):
        """get_active returns None when all work entries have an end_at."""
        closed_time = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
        entry = _entry(end_at=closed_time)
        te.insert(db_conn, entry)
        assert te.get_active(db_conn) is None

    def test_get_active_returns_open_work_entry(self, db_conn):
        """get_active returns the single active work entry."""
        entry = _entry()
        entry_id = te.insert(db_conn, entry)
        result = te.get_active(db_conn)
        assert result is not None
        assert result.id == entry_id
        assert result.kind == "work"
        assert result.end_at is None

    def test_get_active_ignores_closed_entries(self, db_conn):
        """get_active ignores closed work entries even if end_at is recent."""
        closed_time = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
        # Insert a closed entry
        te.insert(db_conn, _entry(end_at=closed_time))
        # Insert another closed entry
        te.insert(db_conn, _entry(end_at=closed_time))
        assert te.get_active(db_conn) is None

    def test_get_active_ignores_non_work_kinds_even_when_open(self, db_conn):
        """get_active ignores 'not_work' and 'idle_unresolved' entries even with end_at=None."""
        te.insert(db_conn, _entry(kind="not_work", ticket_key=None))
        te.insert(db_conn, _entry(kind="idle_unresolved", ticket_key=None))
        assert te.get_active(db_conn) is None


# ---------------------------------------------------------------------------
# end_active
# ---------------------------------------------------------------------------


class TestEndActive:
    def test_end_active_closes_open_entry_and_returns_it(self, db_conn):
        """end_active closes the active entry, updates updated_at, returns it."""
        entry = _entry()
        entry_id = te.insert(db_conn, entry)

        end_time = datetime(2026, 4, 27, 15, 0, tzinfo=UTC)
        updated = te.end_active(db_conn, end_time)

        assert updated is not None
        assert updated.id == entry_id
        assert updated.end_at is not None
        assert updated.end_at.timestamp() == end_time.timestamp()
        # updated_at should be bumped
        assert updated.updated_at.timestamp() >= entry.updated_at.timestamp()

    def test_end_active_returns_none_when_no_active_entry(self, db_conn):
        """end_active returns None when there is no active entry — no error."""
        end_time = datetime(2026, 4, 27, 15, 0, tzinfo=UTC)
        result = te.end_active(db_conn, end_time)
        assert result is None

    def test_end_active_leaves_no_active_entry(self, db_conn):
        """After end_active, get_active returns None."""
        te.insert(db_conn, _entry())
        end_time = datetime(2026, 4, 27, 15, 0, tzinfo=UTC)
        te.end_active(db_conn, end_time)
        assert te.get_active(db_conn) is None


# ---------------------------------------------------------------------------
# list_by_day
# ---------------------------------------------------------------------------


class TestListByDay:
    def test_empty_when_no_entries(self, db_conn):
        """list_by_day returns [] when no entries exist."""
        result = te.list_by_day(db_conn, date(2026, 4, 27))
        assert result == []

    def test_tz_boundary_filtering(self, db_conn):
        """Entries are filtered by calendar day in Sydney tz.

        2026-04-27 Sydney is UTC+10 (AEST).
        Day boundaries in UTC:
          - 2026-04-26 14:00 UTC  (2026-04-27 00:00 AEST)  → start of day
          - 2026-04-27 14:00 UTC  (2026-04-28 00:00 AEST)  → end of day (exclusive)

        An entry at 13:30 UTC on 2026-04-27 is 23:30 AEST on 2026-04-27 → IN the day.
        An entry at 14:00 UTC on 2026-04-27 is 00:00 AEST on 2026-04-28 → NOT in the day.
        An entry at 13:59 UTC on 2026-04-26 is 23:59 AEST on 2026-04-26 → NOT in the day.
        """
        in_day = datetime(2026, 4, 27, 13, 30, tzinfo=UTC)  # 23:30 AEST same day
        after_day = datetime(2026, 4, 27, 14, 0, tzinfo=UTC)  # 00:00 AEST next day
        before_day = datetime(2026, 4, 26, 13, 59, tzinfo=UTC)  # 23:59 AEST prev day
        # Give each entry an end_at so they are all closed — the partial unique
        # index only allows one open work entry at a time.
        one_hour = timedelta(hours=1)

        id_in = te.insert(db_conn, _entry(start_at=in_day, end_at=in_day + one_hour, ticket_key="SFXS-100"))
        te.insert(db_conn, _entry(start_at=after_day, end_at=after_day + one_hour, ticket_key="SFXS-200"))
        te.insert(db_conn, _entry(start_at=before_day, end_at=before_day + one_hour, ticket_key="SFXS-300"))

        result = te.list_by_day(db_conn, date(2026, 4, 27), tz="Australia/Sydney")
        assert len(result) == 1
        assert result[0].id == id_in
        assert result[0].ticket_key == "SFXS-100"

    def test_entries_ordered_by_start_at(self, db_conn):
        """Entries are returned in ascending start_at order."""
        t1 = datetime(2026, 4, 27, 0, 0, tzinfo=UTC)   # 10:00 AEST
        t2 = datetime(2026, 4, 27, 1, 0, tzinfo=UTC)   # 11:00 AEST
        t3 = datetime(2026, 4, 27, 2, 0, tzinfo=UTC)   # 12:00 AEST
        one_hour = timedelta(hours=1)

        # Insert in reverse order to prove sorting isn't insert-order.
        # All entries are closed (end_at set) so they don't collide with the
        # partial unique index that only allows one open work entry.
        id3 = te.insert(db_conn, _entry(start_at=t3, end_at=t3 + one_hour, ticket_key="SFXS-3"))
        id1 = te.insert(db_conn, _entry(start_at=t1, end_at=t1 + one_hour, ticket_key="SFXS-1"))
        id2 = te.insert(db_conn, _entry(start_at=t2, end_at=t2 + one_hour, ticket_key="SFXS-2"))

        result = te.list_by_day(db_conn, date(2026, 4, 27), tz="Australia/Sydney")
        assert [r.id for r in result] == [id1, id2, id3]

    def test_entry_crossing_midnight_belongs_to_start_day(self, db_conn):
        """An entry that starts on 2026-04-27 AEST appears under 2026-04-27 even if end_at is next day."""
        # 10:00 AEST on 2026-04-27 = 00:00 UTC on 2026-04-27
        start = datetime(2026, 4, 27, 0, 0, tzinfo=UTC)
        # end 25 hours later — well into the next calendar day
        end = start + timedelta(hours=25)
        entry_id = te.insert(db_conn, _entry(start_at=start, end_at=end))

        result_correct_day = te.list_by_day(db_conn, date(2026, 4, 27), tz="Australia/Sydney")
        result_next_day = te.list_by_day(db_conn, date(2026, 4, 28), tz="Australia/Sydney")

        assert any(r.id == entry_id for r in result_correct_day)
        assert not any(r.id == entry_id for r in result_next_day)


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


class TestUpdate:
    def test_update_note_bumps_updated_at(self, db_conn):
        """Updating note changes note and bumps updated_at."""
        entry = _entry(note="original")
        entry_id = te.insert(db_conn, entry)
        original = te.get(db_conn, entry_id)

        updated = te.update(db_conn, entry_id, note="changed")
        assert updated.note == "changed"
        assert updated.updated_at.timestamp() >= original.updated_at.timestamp()

    def test_update_ticket_key(self, db_conn):
        """Updating ticket_key works correctly."""
        entry_id = te.insert(db_conn, _entry(ticket_key="SFXS-OLD"))
        updated = te.update(db_conn, entry_id, ticket_key="SFXS-NEW")
        assert updated.ticket_key == "SFXS-NEW"

    def test_update_unknown_id_raises_value_error(self, db_conn):
        """update with a non-existent id raises ValueError."""
        with pytest.raises(ValueError, match="999999"):
            te.update(db_conn, 999_999, note="x")

    def test_update_id_raises_value_error(self, db_conn):
        """Attempting to update 'id' raises ValueError."""
        entry_id = te.insert(db_conn, _entry())
        with pytest.raises(ValueError, match="id"):
            te.update(db_conn, entry_id, id=99)

    def test_update_created_at_raises_value_error(self, db_conn):
        """Attempting to update 'created_at' raises ValueError."""
        entry_id = te.insert(db_conn, _entry())
        with pytest.raises(ValueError, match="created_at"):
            te.update(db_conn, entry_id, created_at=datetime.now(UTC))

    def test_update_unknown_field_raises(self, db_conn):
        """Attempting to update an unknown field raises ValueError."""
        entry_id = te.insert(db_conn, _entry())
        with pytest.raises(ValueError, match="Unknown field"):
            te.update(db_conn, entry_id, colour="red")


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


class TestDelete:
    def test_delete_draft_entry_succeeds(self, db_conn):
        """Deleting a draft (unpushed) entry succeeds; subsequent get returns None."""
        entry_id = te.insert(db_conn, _entry())
        te.delete(db_conn, entry_id)
        assert te.get(db_conn, entry_id) is None

    def test_delete_pushed_entry_raises_value_error(self, db_conn):
        """Deleting a pushed entry raises ValueError."""
        pushed_at = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
        entry_id = te.insert(
            db_conn,
            _entry(pushed_at=pushed_at, jira_worklog_id="WL-42"),
        )
        with pytest.raises(ValueError, match="pushed"):
            te.delete(db_conn, entry_id)

    def test_delete_unknown_id_raises_value_error(self, db_conn):
        """Deleting a non-existent id raises ValueError."""
        with pytest.raises(ValueError, match="999999"):
            te.delete(db_conn, 999_999)


# ---------------------------------------------------------------------------
# mark_pushed
# ---------------------------------------------------------------------------


class TestMarkPushed:
    def test_mark_pushed_sets_fields_and_is_pushed_true(self, db_conn):
        """Marking a draft as pushed sets jira_worklog_id, pushed_at; is_pushed becomes True."""
        entry_id = te.insert(db_conn, _entry())
        pushed_at = datetime(2026, 4, 27, 16, 0, tzinfo=UTC)
        result = te.mark_pushed(db_conn, entry_id, "WL-100", pushed_at)

        assert result.jira_worklog_id == "WL-100"
        assert result.pushed_at is not None
        assert result.pushed_at.timestamp() == pushed_at.timestamp()
        assert result.is_pushed is True

    def test_mark_pushed_already_pushed_raises_value_error(self, db_conn):
        """Marking an already-pushed entry raises ValueError (idempotency guard)."""
        pushed_at = datetime(2026, 4, 27, 16, 0, tzinfo=UTC)
        entry_id = te.insert(
            db_conn,
            _entry(pushed_at=pushed_at, jira_worklog_id="WL-OLD"),
        )
        with pytest.raises(ValueError, match="already pushed"):
            te.mark_pushed(db_conn, entry_id, "WL-NEW", pushed_at)

    def test_mark_pushed_unknown_id_raises_value_error(self, db_conn):
        """Marking a non-existent id raises ValueError."""
        pushed_at = datetime(2026, 4, 27, 16, 0, tzinfo=UTC)
        with pytest.raises(ValueError, match="999999"):
            te.mark_pushed(db_conn, 999_999, "WL-X", pushed_at)
