"""Tests for tsh.cli.reconcile."""
from __future__ import annotations
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from tsh.cli.main import cli
from tsh.config import loader
from tsh.storage import db as db_module
from tsh.storage import time_entries
from tsh.core.models import TimeEntry


@pytest.fixture
def tsh_home(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TSH_CONFIG_DIR", str(tmp_path))
    return tmp_path


def _insert_pending(reason: str, ticket: str = "SFXS-1", *,
                    start_at: datetime, end_at: datetime) -> int:
    conn = db_module.connect(loader.config_dir() / "tsh.db")
    try:
        eid = time_entries.insert(
            conn,
            TimeEntry(
                id=None, ticket_key=ticket, start_at=start_at, end_at=end_at,
                kind="work", note="", jira_worklog_id=None, pushed_at=None,
                created_at=start_at, updated_at=start_at,
            ),
        )
        time_entries.update(conn, eid, pending_reconciliation=1, reconciliation_reason=reason)
        conn.commit()
    finally:
        conn.close()
    return eid


def test_reconcile_json_lists_pending_newest_first(tsh_home: Path) -> None:
    base = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)
    _insert_pending("sleep", "OLD-1",
                    start_at=base, end_at=base + timedelta(minutes=10))
    _insert_pending("orphaned_active", "NEW-2",
                    start_at=base + timedelta(hours=2),
                    end_at=base + timedelta(hours=2, minutes=10))
    result = CliRunner().invoke(cli, ["reconcile", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [e["ticket_key"] for e in payload] == ["NEW-2", "OLD-1"]
    assert payload[0]["reconciliation_reason"] == "orphaned_active"


def test_reconcile_json_empty_returns_empty_list(tsh_home: Path) -> None:
    result = CliRunner().invoke(cli, ["reconcile", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.output) == []


def test_reconcile_interactive_not_work_clears_flag_and_inserts_gap(tsh_home: Path) -> None:
    base = datetime(2026, 5, 14, 22, 0, tzinfo=timezone.utc)
    end = datetime(2026, 5, 14, 22, 42, tzinfo=timezone.utc)
    eid = _insert_pending("sleep", "SFXS-1234", start_at=base, end_at=end)

    result = CliRunner().invoke(cli, ["reconcile", str(eid)], input="n\n")
    assert result.exit_code == 0, result.output

    conn = db_module.connect(loader.config_dir() / "tsh.db")
    try:
        assert time_entries.count_pending_reconciliation(conn) == 0
        rows = conn.execute("SELECT * FROM time_entries ORDER BY id").fetchall()
        assert len(rows) == 2
        gap = rows[1]
        assert gap["kind"] == "not_work"
        assert gap["start_at"] == end
    finally:
        conn.close()


def test_reconcile_interactive_same_extends_through_gap(tsh_home: Path) -> None:
    base = datetime(2026, 5, 14, 22, 0, tzinfo=timezone.utc)
    end = datetime(2026, 5, 14, 22, 42, tzinfo=timezone.utc)
    eid = _insert_pending("sleep", "SFXS-1234", start_at=base, end_at=end)

    result = CliRunner().invoke(cli, ["reconcile", str(eid)], input="s\n")
    assert result.exit_code == 0, result.output

    conn = db_module.connect(loader.config_dir() / "tsh.db")
    try:
        rows = conn.execute("SELECT * FROM time_entries").fetchall()
        assert len(rows) == 1
        assert rows[0]["pending_reconciliation"] == 0
        assert rows[0]["reconciliation_reason"] is None
        # end_at should be past the original 'end' (extended to "now").
        assert rows[0]["end_at"] > end
    finally:
        conn.close()


def test_reconcile_interactive_different_inserts_work_gap(tsh_home: Path) -> None:
    base = datetime(2026, 5, 14, 22, 0, tzinfo=timezone.utc)
    end = datetime(2026, 5, 14, 22, 42, tzinfo=timezone.utc)
    eid = _insert_pending("sleep", "SFXS-1234", start_at=base, end_at=end)

    # 'd' prompts for a chosen ticket on a second line.
    result = CliRunner().invoke(cli, ["reconcile", str(eid)], input="d\nSFXS-9999\n")
    assert result.exit_code == 0, result.output

    conn = db_module.connect(loader.config_dir() / "tsh.db")
    try:
        rows = conn.execute("SELECT * FROM time_entries ORDER BY id").fetchall()
        assert len(rows) == 2
        gap = rows[1]
        assert gap["kind"] == "work"
        assert gap["ticket_key"] == "SFXS-9999"
        assert gap["start_at"] == end
    finally:
        conn.close()


def test_reconcile_skip_leaves_flag_set(tsh_home: Path) -> None:
    base = datetime(2026, 5, 14, 22, 0, tzinfo=timezone.utc)
    end = datetime(2026, 5, 14, 22, 42, tzinfo=timezone.utc)
    eid = _insert_pending("sleep", "SFXS-1234", start_at=base, end_at=end)
    result = CliRunner().invoke(cli, ["reconcile", str(eid)], input="k\n")
    assert result.exit_code == 0
    conn = db_module.connect(loader.config_dir() / "tsh.db")
    try:
        assert time_entries.count_pending_reconciliation(conn) == 1
    finally:
        conn.close()


def test_reconcile_unknown_id_errors(tsh_home: Path) -> None:
    result = CliRunner().invoke(cli, ["reconcile", "9999"])
    assert result.exit_code != 0
    assert "no entry" in result.output.lower() or "9999" in result.output


def test_reconcile_id_for_non_pending_entry_errors(tsh_home: Path) -> None:
    base = datetime(2026, 5, 14, 22, 0, tzinfo=timezone.utc)
    end = datetime(2026, 5, 14, 22, 42, tzinfo=timezone.utc)
    conn = db_module.connect(loader.config_dir() / "tsh.db")
    try:
        eid = time_entries.insert(
            conn,
            TimeEntry(
                id=None, ticket_key="SFXS-X", start_at=base, end_at=end,
                kind="work", note="", jira_worklog_id=None, pushed_at=None,
                created_at=base, updated_at=base,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    # Entry exists but pending_reconciliation = 0.
    result = CliRunner().invoke(cli, ["reconcile", str(eid)])
    assert result.exit_code != 0
