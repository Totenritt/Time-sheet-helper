"""Internal: locate and open the tsh sqlite database from CLI commands."""

from __future__ import annotations
import os
import sqlite3
from pathlib import Path

from tsh.storage import db


def db_path() -> Path:
    """Return the configured DB file path.

    Uses ``$TSH_CONFIG_DIR/tsh.db`` if the env var is set (so tests can isolate),
    otherwise ``~/.tsh/tsh.db``.
    """
    override = os.environ.get("TSH_CONFIG_DIR")
    if override:
        return Path(override) / "tsh.db"
    return Path.home() / ".tsh" / "tsh.db"


def open_db() -> sqlite3.Connection:
    """Open the configured DB with migrations applied."""
    return db.connect(db_path())
