"""Fixtures for storage tests."""
from __future__ import annotations

import sqlite3

import pytest

from tsh.storage import db


@pytest.fixture()
def db_conn() -> sqlite3.Connection:
    """Return an in-memory SQLite connection that has been migrated."""
    conn = db.connect(":memory:")
    yield conn
    conn.close()
