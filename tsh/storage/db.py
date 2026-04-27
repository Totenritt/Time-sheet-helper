"""SQLite connection helper, migration runner, and transaction context.

All TIMESTAMP columns are stored as ISO 8601 UTC strings.  The adapter/
converter pair enforces that no naive datetimes ever leave or enter the
storage layer (spec §5.3, §10).
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

# ---------------------------------------------------------------------------
# Migrations registry
# ---------------------------------------------------------------------------

# Each entry is (target_user_version, sql_filename).
# The runner applies a migration only when PRAGMA user_version < target.
MIGRATIONS: list[tuple[int, str]] = [
    (1, "001_initial.sql"),
]

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


# ---------------------------------------------------------------------------
# Datetime adapter / converter
# ---------------------------------------------------------------------------


def _adapt_datetime(dt: datetime) -> str:
    """Convert datetime to ISO 8601 string for storage. Rejects naive datetimes."""
    if dt.tzinfo is None:
        raise ValueError(
            f"naive datetime {dt!r} cannot be stored — convert to UTC first"
        )
    # Always store as UTC; preserves the instant regardless of input zone.
    return dt.astimezone(timezone.utc).isoformat()


def _convert_datetime(value: bytes) -> datetime:
    """Parse stored TIMESTAMP back into an aware UTC datetime."""
    dt = datetime.fromisoformat(value.decode("ascii"))
    if dt.tzinfo is None:
        raise ValueError(
            f"stored TIMESTAMP {value!r} has no timezone — expected UTC ISO 8601"
        )
    return dt


# WARNING: register_adapter is process-wide. Importing this module makes the
# naive-datetime guard apply to every sqlite3 connection in this Python
# process, not just connections created via tsh.storage.db.connect().
sqlite3.register_adapter(datetime, _adapt_datetime)
sqlite3.register_converter("TIMESTAMP", _convert_datetime)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Open a SQLite connection configured for the tsh storage layer.

    - Foreign keys enabled.
    - ``PARSE_DECLTYPES`` so columns declared TIMESTAMP are returned as
      ``datetime`` objects via the registered converter.
    - Custom UTC adapter/converter that REQUIRES timezone-aware datetimes
      (naive datetimes raise ``ValueError`` at bind time).
    - ``run_migrations()`` invoked automatically (idempotent).
    """
    if str(db_path) != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(db_path),
        detect_types=sqlite3.PARSE_DECLTYPES,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    run_migrations(conn)
    return conn


def run_migrations(conn: sqlite3.Connection) -> None:
    """Apply pending migrations based on PRAGMA user_version.

    Each migration in ``MIGRATIONS`` is applied in order whenever the
    current ``user_version`` is less than the migration's target version.
    After applying, ``user_version`` is updated to the target.  Calling
    this function on an already-migrated database is a no-op.
    """
    current_version: int = conn.execute("PRAGMA user_version").fetchone()[0]

    for target_version, sql_filename in MIGRATIONS:
        if current_version >= target_version:
            continue

        sql = (MIGRATIONS_DIR / sql_filename).read_text(encoding="utf-8")
        conn.executescript(sql)
        current_version = target_version


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Transaction context: commit on normal exit, rollback on exception.

    ``sqlite3``'s built-in ``with conn:`` does this; this wrapper exists so
    callers can write ``with tx(conn):`` for readability and we have a single
    place to add tracing or savepoint logic later.
    """
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
