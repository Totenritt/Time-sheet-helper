"""Tests for tsh.storage.db — migration runner, adapters, and tx()."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tsh.storage import db

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

UTC = timezone.utc

# A timestamp well clear of DST ambiguity; Sydney is UTC+10 in April.
_SYDNEY_TZ = datetime(2026, 4, 27, 14, 30, tzinfo=UTC).astimezone(
    __import__("zoneinfo").ZoneInfo("Australia/Sydney")
)

_NOW_UTC = datetime(2026, 4, 27, 14, 30, tzinfo=UTC)

_INSERT_ENTRY = """
    INSERT INTO time_entries (ticket_key, start_at, end_at, note, kind,
                              created_at, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?)
"""

_NOW_STR = _NOW_UTC.isoformat()  # "2026-04-27T14:30:00+00:00"


def _row(conn: sqlite3.Connection, sql: str, params: tuple = ()):
    """Execute *sql* and return the first row."""
    return conn.execute(sql, params).fetchone()


# ---------------------------------------------------------------------------
# Migration runner
# ---------------------------------------------------------------------------


class TestMigrationRunner:
    def test_fresh_db_user_version_is_1(self, tmp_path):
        """Opening a fresh db runs migration 001 and sets user_version=1."""
        conn = db.connect(tmp_path / "a.db")
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            assert version == 1
        finally:
            conn.close()

    def test_tables_exist_after_migration(self, tmp_path):
        """Both tables are present in sqlite_master after migration."""
        conn = db.connect(tmp_path / "b.db")
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            assert "time_entries" in tables
            assert "tickets_cache" in tables
        finally:
            conn.close()

    def test_migration_is_idempotent(self, tmp_path):
        """Opening the same file twice leaves version at 1, no error."""
        path = tmp_path / "c.db"
        conn1 = db.connect(path)
        conn1.close()
        conn2 = db.connect(path)
        try:
            version = conn2.execute("PRAGMA user_version").fetchone()[0]
            assert version == 1
        finally:
            conn2.close()

    def test_one_active_partial_unique_index(self, db_conn):
        """Inserting two open (end_at=NULL) rows with kind='work' raises IntegrityError."""
        db_conn.execute(
            _INSERT_ENTRY,
            ("PROJ-1", _NOW_STR, None, "", "work", _NOW_STR, _NOW_STR),
        )
        with pytest.raises(sqlite3.IntegrityError):
            db_conn.execute(
                _INSERT_ENTRY,
                ("PROJ-2", _NOW_STR, None, "", "work", _NOW_STR, _NOW_STR),
            )

    def test_one_active_allows_multiple_closed_rows(self, db_conn):
        """Multiple closed rows (end_at set) with the same kind must be allowed."""
        closed = "2026-04-27T13:00:00+00:00"
        for i in range(3):
            db_conn.execute(
                _INSERT_ENTRY,
                (f"PROJ-{i}", _NOW_STR, closed, "", "work", _NOW_STR, _NOW_STR),
            )
        # No IntegrityError means the index correctly limits only NULL end_at.

    def test_kind_check_constraint(self, db_conn):
        """Inserting kind='nonsense' raises IntegrityError."""
        with pytest.raises(sqlite3.IntegrityError):
            db_conn.execute(
                _INSERT_ENTRY,
                ("PROJ-99", _NOW_STR, None, "", "nonsense", _NOW_STR, _NOW_STR),
            )


# ---------------------------------------------------------------------------
# Datetime adapter / converter
# ---------------------------------------------------------------------------


class TestDatetimeAdapterConverter:
    def _insert_and_read_back(
        self, conn: sqlite3.Connection, start_at: datetime
    ) -> datetime:
        """Insert a row with *start_at* and return the read-back value."""
        now = _NOW_UTC
        conn.execute(
            _INSERT_ENTRY,
            ("PROJ-RT", start_at, None, "", "work", now, now),
        )
        row = _row(conn, "SELECT start_at FROM time_entries WHERE ticket_key='PROJ-RT'")
        return row[0]

    def test_round_trip_utc_datetime(self, db_conn):
        """UTC aware datetime survives a round-trip through the adapter/converter."""
        result = self._insert_and_read_back(db_conn, _NOW_UTC)
        assert isinstance(result, datetime)
        assert result.tzinfo is not None, "read-back must be tz-aware"
        # Same instant
        assert result.timestamp() == _NOW_UTC.timestamp()

    def test_round_trip_sydney_datetime(self, db_conn):
        """Sydney-aware datetime is stored as UTC and read back as a UTC-aware datetime."""
        sydney_dt = _SYDNEY_TZ
        result = self._insert_and_read_back(db_conn, sydney_dt)
        assert isinstance(result, datetime)
        assert result.tzinfo is not None, "read-back must be tz-aware"
        # Preserves the same instant
        assert result.timestamp() == sydney_dt.timestamp()

    def test_naive_datetime_raises_value_error(self, db_conn):
        """Inserting a naive datetime into a TIMESTAMP column raises ValueError."""
        naive = datetime(2026, 4, 27, 14, 30)  # no tzinfo
        assert naive.tzinfo is None
        with pytest.raises(ValueError, match="naive datetime"):
            db_conn.execute(
                _INSERT_ENTRY,
                ("PROJ-NAIVE", naive, None, "", "work", _NOW_UTC, _NOW_UTC),
            )


# ---------------------------------------------------------------------------
# tx() context manager
# ---------------------------------------------------------------------------


class TestTxContextManager:
    def test_tx_commits_on_normal_exit(self, db_conn):
        """Data inserted inside tx() is visible after the block exits normally."""
        with db.tx(db_conn):
            db_conn.execute(
                _INSERT_ENTRY,
                ("PROJ-TX1", _NOW_UTC, None, "", "work", _NOW_UTC, _NOW_UTC),
            )
        row = _row(
            db_conn,
            "SELECT ticket_key FROM time_entries WHERE ticket_key='PROJ-TX1'",
        )
        assert row is not None
        assert row[0] == "PROJ-TX1"

    def test_tx_rolls_back_on_exception(self, db_conn):
        """Data inserted inside tx() is NOT visible when an exception propagates."""
        with pytest.raises(RuntimeError, match="boom"):
            with db.tx(db_conn):
                db_conn.execute(
                    _INSERT_ENTRY,
                    ("PROJ-TX2", _NOW_UTC, None, "", "work", _NOW_UTC, _NOW_UTC),
                )
                raise RuntimeError("boom")

        row = _row(
            db_conn,
            "SELECT ticket_key FROM time_entries WHERE ticket_key='PROJ-TX2'",
        )
        assert row is None, "rolled-back row must not be visible"


# ---------------------------------------------------------------------------
# connect() — parent directory creation (fix 2)
# ---------------------------------------------------------------------------


def test_connect_creates_parent_directory(tmp_path):
    """connect() must create missing ancestor directories automatically."""
    nested = tmp_path / "deep" / "nest" / "tsh.db"
    conn = db.connect(nested)
    try:
        assert nested.exists()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# connect() — row_factory (fix 3)
# ---------------------------------------------------------------------------


def test_connect_sets_row_factory_to_row(tmp_path):
    """Connections returned by connect() must support name-based column access."""
    conn = db.connect(tmp_path / "x.db")
    try:
        cursor = conn.execute("SELECT 1 AS n")
        row = cursor.fetchone()
        assert row["n"] == 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Migration atomicity (fix 1)
# ---------------------------------------------------------------------------


def test_migration_atomicity_on_partial_failure(tmp_path, monkeypatch):
    """A migration that fails to load must not advance user_version past the last good state."""
    # Patch MIGRATIONS so that after the real migration 1 there is a migration 2
    # that references a non-existent SQL file.  The FileNotFoundError raised
    # during the file-read must leave user_version at 1, not 2.
    monkeypatch.setattr(
        db,
        "MIGRATIONS",
        [(1, "001_initial.sql"), (2, "missing.sql")],
    )
    with pytest.raises(FileNotFoundError):
        db.connect(tmp_path / "partial.db")

    # Open a raw connection (no migrations) to inspect the user_version.
    raw = sqlite3.connect(str(tmp_path / "partial.db"))
    try:
        version = raw.execute("PRAGMA user_version").fetchone()[0]
        assert version == 1, (
            f"user_version should be 1 (first migration applied) but got {version}"
        )
    finally:
        raw.close()


# ---------------------------------------------------------------------------
# _convert_datetime naive guard (fix 4)
# ---------------------------------------------------------------------------


def test_convert_datetime_rejects_naive_stored_value(tmp_path):
    """Reading a TIMESTAMP column that holds a naive ISO string must raise ValueError."""
    conn = db.connect(tmp_path / "naive.db")
    try:
        # Insert a naive ISO string directly as a string literal, bypassing
        # the datetime adapter so no ValueError fires at insert time.
        conn.execute(
            "INSERT INTO time_entries (start_at, kind, created_at, updated_at) "
            "VALUES (?, 'work', ?, ?)",
            (
                "2026-04-27T14:30:00",          # naive — no offset
                "2026-04-27T14:30:00+00:00",
                "2026-04-27T14:30:00+00:00",
            ),
        )
        conn.commit()
        with pytest.raises(ValueError, match="no timezone"):
            conn.execute("SELECT start_at FROM time_entries").fetchone()
    finally:
        conn.close()
