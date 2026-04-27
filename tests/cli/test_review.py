"""Tests for tsh log/review/edit/delete commands (Phase 5.2)."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone, timedelta
from unittest.mock import patch

import click
import pytest
from click.testing import CliRunner

from tsh.cli.main import cli
from tsh.cli.review import _parse_duration, _parse_day
from tsh.core.timezones import today_in_display_tz
from tsh.storage import db, time_entries
from tsh.core.models import TimeEntry


TZ = "Australia/Sydney"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_conn(isolated_config):
    """Open the DB for the isolated config dir."""
    from tsh.cli._db import open_db
    return open_db()


def _insert_entry(conn, *, ticket="SFXS-1", note="", pushed=False, active=False):
    """Insert a minimal time entry; returns the new id."""
    now = datetime.now(timezone.utc)
    end = None if active else now
    entry = TimeEntry(
        id=None,
        ticket_key=ticket,
        start_at=now - timedelta(minutes=45),
        end_at=end,
        kind="work",
        note=note,
        jira_worklog_id=None,
        pushed_at=None,
        created_at=now,
        updated_at=now,
    )
    new_id = time_entries.insert(conn, entry)
    if pushed:
        time_entries.mark_pushed(conn, new_id, "JW-999", now)
    conn.commit()
    return new_id


# ---------------------------------------------------------------------------
# 1-6: Duration parsing
# ---------------------------------------------------------------------------


def test_parse_duration_45m():
    assert _parse_duration("45m") == 2700


def test_parse_duration_1h():
    assert _parse_duration("1h") == 3600


def test_parse_duration_1h30m():
    assert _parse_duration("1h30m") == 5400


def test_parse_duration_0m_raises():
    with pytest.raises(click.BadParameter):
        _parse_duration("0m")


def test_parse_duration_invalid_raises():
    with pytest.raises(click.BadParameter):
        _parse_duration("invalid")


def test_parse_duration_empty_raises():
    with pytest.raises(click.BadParameter):
        _parse_duration("")


# ---------------------------------------------------------------------------
# 7-11: Day parsing
# ---------------------------------------------------------------------------


def test_parse_day_none_returns_today():
    today = today_in_display_tz(tz=TZ)
    assert _parse_day(None, TZ) == today


def test_parse_day_today_string():
    today = today_in_display_tz(tz=TZ)
    assert _parse_day("today", TZ) == today


def test_parse_day_yesterday():
    today = today_in_display_tz(tz=TZ)
    assert _parse_day("yesterday", TZ) == today - timedelta(days=1)


def test_parse_day_iso_date():
    assert _parse_day("2026-04-27", TZ) == date(2026, 4, 27)


def test_parse_day_garbage_raises():
    with pytest.raises(click.BadParameter):
        _parse_day("garbage", TZ)


# ---------------------------------------------------------------------------
# 12-13: log command
# ---------------------------------------------------------------------------


def test_log_writes_row_and_prints_message(runner: CliRunner, isolated_config):
    result = runner.invoke(cli, ["log", "SFXS-1", "45m", "-m", "test note"])
    assert result.exit_code == 0, result.output
    assert "Logged entry" in result.output
    assert "SFXS-1" in result.output
    assert "45m" in result.output

    conn = _make_conn(isolated_config)
    try:
        entries = time_entries.list_by_day(conn, today_in_display_tz(tz=TZ), tz=TZ)
    finally:
        conn.close()

    assert len(entries) == 1
    e = entries[0]
    assert e.ticket_key == "SFXS-1"
    assert e.note == "test note"
    duration = int((e.end_at - e.start_at).total_seconds())
    assert duration == 2700  # 45 * 60


def test_log_no_note_works(runner: CliRunner, isolated_config):
    result = runner.invoke(cli, ["log", "SFXS-2", "1h"])
    assert result.exit_code == 0, result.output
    assert "Logged entry" in result.output

    conn = _make_conn(isolated_config)
    try:
        entries = time_entries.list_by_day(conn, today_in_display_tz(tz=TZ), tz=TZ)
    finally:
        conn.close()

    assert len(entries) == 1
    assert entries[0].note == ""


# ---------------------------------------------------------------------------
# 14-16: review command
# ---------------------------------------------------------------------------


def test_review_empty_db(runner: CliRunner, isolated_config):
    result = runner.invoke(cli, ["review"])
    assert result.exit_code == 0, result.output
    assert "No entries" in result.output


def test_review_shows_entry_in_table(runner: CliRunner, isolated_config):
    # log an entry first
    log_result = runner.invoke(cli, ["log", "SFXS-10", "30m", "-m", "review test"])
    assert log_result.exit_code == 0

    result = runner.invoke(cli, ["review"])
    assert result.exit_code == 0, result.output
    assert "SFXS-10" in result.output
    # Table should have raw and rounded columns header
    assert "Raw" in result.output
    assert "Round" in result.output
    # Duration should appear (30m)
    assert "30m" in result.output


def test_review_json_output(runner: CliRunner, isolated_config):
    log_result = runner.invoke(cli, ["log", "SFXS-11", "1h", "-m", "json test"])
    assert log_result.exit_code == 0

    result = runner.invoke(cli, ["review", "--json"])
    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    assert isinstance(rows, list)
    assert len(rows) == 1
    row = rows[0]
    assert row["ticket"] == "SFXS-11"
    assert "id" in row
    assert "raw_seconds" in row
    assert "rounded_seconds" in row
    assert "note" in row
    assert row["note"] == "json test"
    assert row["raw_seconds"] == 3600


# ---------------------------------------------------------------------------
# 17-20: edit command
# ---------------------------------------------------------------------------


def test_edit_unknown_id_exits_1(runner: CliRunner, isolated_config):
    result = runner.invoke(cli, ["edit", "9999"])
    assert result.exit_code == 1
    assert "No entry with id 9999" in result.output


def test_edit_pushed_entry_refused(runner: CliRunner, isolated_config):
    conn = _make_conn(isolated_config)
    try:
        eid = _insert_entry(conn, pushed=True)
    finally:
        conn.close()

    result = runner.invoke(cli, ["edit", str(eid)])
    assert result.exit_code == 1
    assert "already pushed" in result.output


def test_edit_no_changes(runner: CliRunner, isolated_config):
    conn = _make_conn(isolated_config)
    try:
        eid = _insert_entry(conn, note="original")
    finally:
        conn.close()

    # click.edit returning None means no changes (editor not opened / same text)
    with patch("click.edit", return_value=None):
        result = runner.invoke(cli, ["edit", str(eid)])
    assert result.exit_code == 0, result.output
    assert "No changes" in result.output


def test_edit_updates_note(runner: CliRunner, isolated_config):
    import tomllib
    import tomli_w

    conn = _make_conn(isolated_config)
    try:
        eid = _insert_entry(conn, note="original note")
    finally:
        conn.close()

    def fake_edit(text, extension=None):
        """Return TOML with note changed."""
        data = tomllib.loads(text)
        data["note"] = "updated note"
        return tomli_w.dumps(data)

    with patch("click.edit", side_effect=fake_edit):
        result = runner.invoke(cli, ["edit", str(eid)])
    assert result.exit_code == 0, result.output
    assert f"Updated entry {eid}" in result.output

    conn2 = _make_conn(isolated_config)
    try:
        updated = time_entries.get(conn2, eid)
    finally:
        conn2.close()
    assert updated is not None
    assert updated.note == "updated note"


# ---------------------------------------------------------------------------
# 21-23: delete command
# ---------------------------------------------------------------------------


def test_delete_draft_entry(runner: CliRunner, isolated_config):
    conn = _make_conn(isolated_config)
    try:
        eid = _insert_entry(conn)
    finally:
        conn.close()

    result = runner.invoke(cli, ["delete", str(eid)])
    assert result.exit_code == 0, result.output
    assert f"Deleted entry {eid}" in result.output

    conn2 = _make_conn(isolated_config)
    try:
        assert time_entries.get(conn2, eid) is None
    finally:
        conn2.close()


def test_delete_pushed_entry_refused(runner: CliRunner, isolated_config):
    conn = _make_conn(isolated_config)
    try:
        eid = _insert_entry(conn, pushed=True)
    finally:
        conn.close()

    result = runner.invoke(cli, ["delete", str(eid)])
    assert result.exit_code == 1
    # error message goes to stderr (mixed into output by default CliRunner)
    assert "Cannot delete" in result.output or "pushed" in result.output.lower()


def test_delete_unknown_id(runner: CliRunner, isolated_config):
    result = runner.invoke(cli, ["delete", "8888"])
    assert result.exit_code == 1
    assert "8888" in result.output or "No time_entry" in result.output
