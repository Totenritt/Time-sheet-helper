"""Tests for tsh.storage.tickets_cache."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from tsh.storage import tickets_cache as tc


def _utc(year=2026, month=4, day=27, hour=12, minute=0, second=0):
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# upsert + get
# ---------------------------------------------------------------------------


class TestUpsertGet:
    def test_upsert_and_get_round_trips(self, db_conn):
        """After upsert, get returns a TicketCacheEntry with all fields matching.
        last_used_at is initialised to last_fetched_at on first insert.
        """
        fetched_at = _utc()
        tc.upsert(db_conn, "SFXS-1", "Make pie", "In Progress", "casey@example.com", fetched_at)
        entry = tc.get(db_conn, "SFXS-1")

        assert entry is not None
        assert entry.ticket_key == "SFXS-1"
        assert entry.summary == "Make pie"
        assert entry.status == "In Progress"
        assert entry.assignee_email == "casey@example.com"
        assert entry.last_fetched_at == fetched_at
        assert entry.last_used_at == fetched_at  # initialised to last_fetched_at

    def test_get_nonexistent_returns_none(self, db_conn):
        """get on an unknown ticket key returns None."""
        assert tc.get(db_conn, "nonexistent") is None

    def test_second_upsert_updates_metadata_but_not_last_used_at(self, db_conn):
        """Re-upserting with a newer fetch time updates summary/status/assignee_email/
        last_fetched_at. last_used_at is NOT changed (stays at the initial value).
        """
        first_fetch = _utc(hour=10)
        tc.upsert(db_conn, "SFXS-1", "Make pie", "In Progress", "casey@example.com", first_fetch)

        second_fetch = _utc(hour=11)
        tc.upsert(db_conn, "SFXS-1", "Make cake", "Done", "bob@example.com", second_fetch)

        entry = tc.get(db_conn, "SFXS-1")
        assert entry is not None
        assert entry.summary == "Make cake"
        assert entry.status == "Done"
        assert entry.assignee_email == "bob@example.com"
        assert entry.last_fetched_at == second_fetch
        # last_used_at was NOT touched — still the initial value
        assert entry.last_used_at == first_fetch


# ---------------------------------------------------------------------------
# mark_used
# ---------------------------------------------------------------------------


class TestMarkUsed:
    def test_mark_used_updates_last_used_at(self, db_conn):
        """After upsert + mark_used, last_used_at reflects the given time."""
        tc.upsert(db_conn, "SFXS-1", "Make pie", "In Progress", "casey@example.com", _utc())
        used_at = _utc(hour=15)
        tc.mark_used(db_conn, "SFXS-1", used_at)

        entry = tc.get(db_conn, "SFXS-1")
        assert entry is not None
        assert entry.last_used_at == used_at

    def test_mark_used_no_when_uses_now_utc(self, db_conn):
        """mark_used with no when arg uses the current UTC time (within 5 seconds)."""
        tc.upsert(db_conn, "SFXS-1", "Make pie", "In Progress", "casey@example.com", _utc())
        tc.mark_used(db_conn, "SFXS-1")

        entry = tc.get(db_conn, "SFXS-1")
        assert entry is not None
        diff = abs((entry.last_used_at - datetime.now(timezone.utc)).total_seconds())
        assert diff < 5

    def test_mark_used_unknown_ticket_is_noop(self, db_conn):
        """mark_used on an unknown ticket key raises no exception and creates no row."""
        tc.mark_used(db_conn, "UNKNOWN-99", _utc())  # should not raise
        assert tc.get(db_conn, "UNKNOWN-99") is None

    def test_mark_used_naive_datetime_raises_value_error(self, db_conn):
        """mark_used with a naive datetime raises ValueError (adapter rejects it)."""
        tc.upsert(db_conn, "SFXS-1", "Make pie", "In Progress", "casey@example.com", _utc())
        naive = datetime(2026, 4, 27, 12, 0, 0)  # no tzinfo
        with pytest.raises(ValueError):
            tc.mark_used(db_conn, "SFXS-1", naive)


# ---------------------------------------------------------------------------
# recents
# ---------------------------------------------------------------------------


class TestRecents:
    def test_empty_cache_returns_empty_list(self, db_conn):
        """recents() on an empty cache returns []."""
        assert tc.recents(db_conn) == []

    def test_recents_ordered_by_last_used_at_desc(self, db_conn):
        """Three tickets with distinct last_used_at values come back most-recent first."""
        tc.upsert(db_conn, "SFXS-1", "Alpha", "In Progress", "a@x.com", _utc(hour=9))
        tc.upsert(db_conn, "SFXS-2", "Beta",  "In Progress", "b@x.com", _utc(hour=9))
        tc.upsert(db_conn, "SFXS-3", "Gamma", "In Progress", "c@x.com", _utc(hour=9))

        tc.mark_used(db_conn, "SFXS-1", _utc(hour=10))
        tc.mark_used(db_conn, "SFXS-2", _utc(hour=12))
        tc.mark_used(db_conn, "SFXS-3", _utc(hour=11))

        results = tc.recents(db_conn)
        keys = [r.ticket_key for r in results]
        assert keys == ["SFXS-2", "SFXS-3", "SFXS-1"]

    def test_recents_limit(self, db_conn):
        """recents(limit=2) returns at most 2 entries."""
        for i in range(5):
            tc.upsert(db_conn, f"SFXS-{i}", f"Ticket {i}", "Open", f"u{i}@x.com", _utc(hour=i))
            tc.mark_used(db_conn, f"SFXS-{i}", _utc(hour=i))

        results = tc.recents(db_conn, limit=2)
        assert len(results) == 2

    def test_recents_default_limit_is_10(self, db_conn):
        """Default limit is 10: with 12 cached tickets, recents() returns 10."""
        for i in range(12):
            tc.upsert(db_conn, f"SFXS-{i}", f"Ticket {i}", "Open", f"u{i}@x.com", _utc(hour=0))
            tc.mark_used(db_conn, f"SFXS-{i}", _utc(hour=i))

        results = tc.recents(db_conn)
        assert len(results) == 10


# ---------------------------------------------------------------------------
# is_fresh
# ---------------------------------------------------------------------------


class TestIsFresh:
    def test_unknown_ticket_returns_false(self, db_conn):
        """is_fresh returns False for a ticket that isn't in the cache."""
        assert tc.is_fresh(db_conn, "UNKNOWN-1") is False

    def test_fetched_exactly_now_returns_true(self, db_conn):
        """A ticket fetched at exactly now is fresh."""
        now = _utc()
        tc.upsert(db_conn, "SFXS-1", "Fresh", "In Progress", "a@x.com", now)
        assert tc.is_fresh(db_conn, "SFXS-1", now=now) is True

    def test_fetched_4_min_ago_returns_true(self, db_conn):
        """A ticket fetched 4 minutes ago is fresh within the default 5-min TTL."""
        now = _utc(hour=12)
        fetched = now - timedelta(minutes=4)
        tc.upsert(db_conn, "SFXS-1", "Ticket", "Open", "a@x.com", fetched)
        assert tc.is_fresh(db_conn, "SFXS-1", now=now) is True

    def test_fetched_6_min_ago_returns_false(self, db_conn):
        """A ticket fetched 6 minutes ago is stale (past the default 5-min TTL)."""
        now = _utc(hour=12)
        fetched = now - timedelta(minutes=6)
        tc.upsert(db_conn, "SFXS-1", "Ticket", "Open", "a@x.com", fetched)
        assert tc.is_fresh(db_conn, "SFXS-1", now=now) is False

    def test_custom_ttl_30s_ago_true(self, db_conn):
        """Custom ttl_minutes=1: ticket fetched 30s ago is fresh."""
        now = _utc(hour=12)
        fetched = now - timedelta(seconds=30)
        tc.upsert(db_conn, "SFXS-1", "Ticket", "Open", "a@x.com", fetched)
        assert tc.is_fresh(db_conn, "SFXS-1", ttl_minutes=1, now=now) is True

    def test_custom_ttl_2min_ago_false(self, db_conn):
        """Custom ttl_minutes=1: ticket fetched 2 minutes ago is stale."""
        now = _utc(hour=12)
        fetched = now - timedelta(minutes=2)
        tc.upsert(db_conn, "SFXS-1", "Ticket", "Open", "a@x.com", fetched)
        assert tc.is_fresh(db_conn, "SFXS-1", ttl_minutes=1, now=now) is False
