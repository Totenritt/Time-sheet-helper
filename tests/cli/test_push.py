"""Tests for tsh push command (Phase 5.3)."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import httpx
import pytest
import respx
from click.testing import CliRunner

from tsh.cli.main import cli
from tsh.cli.review import _parse_day
from tsh.config import credentials, loader
from tsh.storage import time_entries
from tsh.core.models import TimeEntry

BASE_URL = "https://example.atlassian.net"
EMAIL = "user@example.com"
TOKEN = "myapitoken"
TZ = "Australia/Sydney"

# Fixed test date: 2026-04-27 03:00 UTC = 2026-04-27 13:00 AEST (+1000)
# April is AEST (UTC+10), not AEDT (+11), so offset is +1000.
_BASE_UTC = datetime(2026, 4, 27, 3, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_conn(isolated_config):
    """Open the DB for the isolated config dir."""
    from tsh.cli._db import open_db
    return open_db()


def _insert_draft(
    conn,
    *,
    ticket: str = "SFXS-1",
    note: str = "",
    start_at: datetime | None = None,
    duration_seconds: int = 900,  # 15 minutes
    day: date | None = None,
):
    """Insert a draft (unpushed) time entry and return its id.

    If ``day`` is given, the entry is placed at midnight-ish of that day in
    Australia/Sydney, otherwise ``_BASE_UTC`` is used.
    """
    if start_at is None:
        if day is not None:
            from zoneinfo import ZoneInfo
            from datetime import time as dtime
            local_start = datetime.combine(day, dtime(9, 0), tzinfo=ZoneInfo(TZ))
            start_at = local_start.astimezone(timezone.utc)
        else:
            start_at = _BASE_UTC
    end_at = start_at + timedelta(seconds=duration_seconds)
    now = datetime.now(timezone.utc)
    entry = TimeEntry(
        id=None,
        ticket_key=ticket,
        start_at=start_at,
        end_at=end_at,
        kind="work",
        note=note,
        jira_worklog_id=None,
        pushed_at=None,
        created_at=now,
        updated_at=now,
    )
    eid = time_entries.insert(conn, entry)
    conn.commit()
    return eid


def _setup_auth(fake_keyring):
    """Write Jira config and set a token in the fake keyring."""
    loader.set("jira.base_url", BASE_URL)
    loader.set("jira.email", EMAIL)
    credentials.set_token(EMAIL, TOKEN)


# ---------------------------------------------------------------------------
# Test 1: --dry-run, no drafts → "No drafts to push" + exit 0; no HTTP
# ---------------------------------------------------------------------------


@respx.mock
def test_dry_run_no_drafts(runner: CliRunner, isolated_config):
    result = runner.invoke(cli, ["push", "--day", "2026-04-27", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "No drafts to push" in result.output
    # respx.mock with no routes registered; any HTTP would raise
    assert respx.calls.call_count == 0


# ---------------------------------------------------------------------------
# Test 2: --dry-run with one draft → DRY RUN table; no HTTP; entry NOT pushed
# ---------------------------------------------------------------------------


@respx.mock
def test_dry_run_one_draft_table(runner: CliRunner, isolated_config, fake_keyring):
    conn = _make_conn(isolated_config)
    try:
        eid = _insert_draft(conn, ticket="SFXS-99", note="dry test", day=date(2026, 4, 27))
    finally:
        conn.close()

    result = runner.invoke(cli, ["push", "--day", "2026-04-27", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "DRY RUN" in result.output
    assert "SFXS-99" in result.output
    # The table truncates started_iso to 25 chars; verify the prefix is correct
    # (2026-04-27T09:00 = _BASE_UTC 03:00 UTC converted to AEST +1000)
    assert "2026-04-27T09:00:00" in result.output, (
        f"Expected Sydney-localised started in output; got:\n{result.output}"
    )
    # No HTTP
    assert respx.calls.call_count == 0

    # Entry must NOT be marked pushed
    conn2 = _make_conn(isolated_config)
    try:
        e = time_entries.get(conn2, eid)
    finally:
        conn2.close()
    assert e is not None
    assert e.pushed_at is None


# ---------------------------------------------------------------------------
# Test 3: --dry-run --json → valid JSON list with required fields
# ---------------------------------------------------------------------------


@respx.mock
def test_dry_run_json_output(runner: CliRunner, isolated_config, fake_keyring):
    conn = _make_conn(isolated_config)
    try:
        eid = _insert_draft(conn, ticket="SFXS-88", note="json test", day=date(2026, 4, 27))
    finally:
        conn.close()

    result = runner.invoke(cli, ["push", "--day", "2026-04-27", "--dry-run", "--json"])
    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    assert isinstance(rows, list)
    assert len(rows) == 1
    row = rows[0]
    assert row["ticket"] == "SFXS-88"
    assert row["ok"] is True
    assert row["dry_run"] is True
    assert eid in row["entry_ids"]
    assert row["started"] is not None
    assert "+1000" in row["started"]
    assert respx.calls.call_count == 0


# ---------------------------------------------------------------------------
# Test 4: real push, no drafts → "No drafts to push" + exit 0
# ---------------------------------------------------------------------------


def test_real_push_no_drafts(runner: CliRunner, isolated_config, fake_keyring):
    _setup_auth(fake_keyring)
    result = runner.invoke(cli, ["push", "--day", "2026-04-27"])
    assert result.exit_code == 0, result.output
    assert "No drafts to push" in result.output


# ---------------------------------------------------------------------------
# Test 5: real push, no auth configured → stderr "Jira not configured", exit 2
# ---------------------------------------------------------------------------


def test_real_push_no_auth_config(runner: CliRunner, isolated_config, fake_keyring):
    # No jira config set; defaults have empty base_url and email
    result = runner.invoke(cli, ["push", "--day", "2026-04-27"])
    assert result.exit_code == 2, result.output
    assert "Jira not configured" in result.output


# ---------------------------------------------------------------------------
# Test 6: real push, auth configured but no token in keyring → exit 2
# ---------------------------------------------------------------------------


def test_real_push_no_token(runner: CliRunner, isolated_config, fake_keyring):
    loader.set("jira.base_url", BASE_URL)
    loader.set("jira.email", EMAIL)
    # No token stored in keyring
    result = runner.invoke(cli, ["push", "--day", "2026-04-27"])
    assert result.exit_code == 2, result.output
    assert "No token in keyring" in result.output
    assert EMAIL in result.output


# ---------------------------------------------------------------------------
# Test 7: real push, one draft, success → exit 0; "OK" line; entry marked pushed
# ---------------------------------------------------------------------------


@respx.mock
def test_real_push_one_draft_success(runner: CliRunner, isolated_config, fake_keyring):
    _setup_auth(fake_keyring)
    conn = _make_conn(isolated_config)
    try:
        eid = _insert_draft(conn, ticket="SFXS-10", note="coding", day=date(2026, 4, 27))
    finally:
        conn.close()

    respx.post(f"{BASE_URL}/rest/api/3/issue/SFXS-10/worklog").mock(
        return_value=httpx.Response(201, json={"id": "wl-42"})
    )

    result = runner.invoke(cli, ["push", "--day", "2026-04-27"])
    assert result.exit_code == 0, result.output
    assert "OK" in result.output
    assert "SFXS-10" in result.output

    conn2 = _make_conn(isolated_config)
    try:
        e = time_entries.get(conn2, eid)
    finally:
        conn2.close()
    assert e is not None
    assert e.pushed_at is not None
    assert e.jira_worklog_id == "wl-42"


# ---------------------------------------------------------------------------
# Test 8: real push, two drafts on different tickets, second fails → exit 1
# ---------------------------------------------------------------------------


@respx.mock
def test_real_push_two_tickets_second_fails(runner: CliRunner, isolated_config, fake_keyring):
    _setup_auth(fake_keyring)
    conn = _make_conn(isolated_config)
    try:
        eid1 = _insert_draft(
            conn,
            ticket="SFXS-11",
            note="first",
            start_at=_BASE_UTC,
            duration_seconds=900,
        )
        eid2 = _insert_draft(
            conn,
            ticket="SFXS-12",
            note="second",
            start_at=_BASE_UTC + timedelta(hours=2),
            duration_seconds=900,
        )
    finally:
        conn.close()

    respx.post(f"{BASE_URL}/rest/api/3/issue/SFXS-11/worklog").mock(
        return_value=httpx.Response(201, json={"id": "wl-11"})
    )
    respx.post(f"{BASE_URL}/rest/api/3/issue/SFXS-12/worklog").mock(
        return_value=httpx.Response(400, text="bad request")
    )

    result = runner.invoke(cli, ["push", "--day", "2026-04-27"])
    assert result.exit_code == 1, result.output
    # Both rows appear in output
    assert "SFXS-11" in result.output
    assert "SFXS-12" in result.output
    assert "OK" in result.output
    assert "FAIL" in result.output

    conn2 = _make_conn(isolated_config)
    try:
        e1 = time_entries.get(conn2, eid1)
        e2 = time_entries.get(conn2, eid2)
    finally:
        conn2.close()
    # First entry pushed
    assert e1.pushed_at is not None
    assert e1.jira_worklog_id == "wl-11"
    # Second entry still draft
    assert e2.pushed_at is None
    assert e2.jira_worklog_id is None


# ---------------------------------------------------------------------------
# Test 9: two drafts on SAME ticket → aggregate into one worklog; both marked
# ---------------------------------------------------------------------------


@respx.mock
def test_real_push_same_ticket_aggregated(runner: CliRunner, isolated_config, fake_keyring):
    _setup_auth(fake_keyring)
    conn = _make_conn(isolated_config)
    try:
        eid1 = _insert_draft(
            conn,
            ticket="SFXS-20",
            note="morning",
            start_at=_BASE_UTC,
            duration_seconds=900,
        )
        eid2 = _insert_draft(
            conn,
            ticket="SFXS-20",
            note="afternoon",
            start_at=_BASE_UTC + timedelta(hours=3),
            duration_seconds=900,
        )
    finally:
        conn.close()

    # Only ONE worklog POST expected (both entries aggregate into one)
    route = respx.post(f"{BASE_URL}/rest/api/3/issue/SFXS-20/worklog").mock(
        return_value=httpx.Response(201, json={"id": "wl-aggregate"})
    )

    result = runner.invoke(cli, ["push", "--day", "2026-04-27"])
    assert result.exit_code == 0, result.output
    assert route.call_count == 1

    conn2 = _make_conn(isolated_config)
    try:
        e1 = time_entries.get(conn2, eid1)
        e2 = time_entries.get(conn2, eid2)
    finally:
        conn2.close()
    # Both entries pushed with SAME jira_worklog_id
    assert e1.pushed_at is not None
    assert e2.pushed_at is not None
    assert e1.jira_worklog_id == "wl-aggregate"
    assert e2.jira_worklog_id == "wl-aggregate"


# ---------------------------------------------------------------------------
# Test 10: _parse_day import smoke test
# ---------------------------------------------------------------------------


def test_parse_day_import_smoke():
    """_parse_day is importable from tsh.cli.review and works correctly."""
    d = _parse_day("2026-04-27", TZ)
    assert d == date(2026, 4, 27)


# ---------------------------------------------------------------------------
# Test 11: --day isolates to the correct day
# ---------------------------------------------------------------------------


@respx.mock
def test_push_day_flag_isolates_correct_day(runner: CliRunner, isolated_config, fake_keyring):
    _setup_auth(fake_keyring)
    conn = _make_conn(isolated_config)
    try:
        # Entry on target day (2026-04-27)
        eid_target = _insert_draft(conn, ticket="SFXS-30", day=date(2026, 4, 27))
        # Entry on a different day (2026-04-26)
        eid_other = _insert_draft(conn, ticket="SFXS-31", day=date(2026, 4, 26))
    finally:
        conn.close()

    respx.post(f"{BASE_URL}/rest/api/3/issue/SFXS-30/worklog").mock(
        return_value=httpx.Response(201, json={"id": "wl-target"})
    )

    result = runner.invoke(cli, ["push", "--day", "2026-04-27"])
    assert result.exit_code == 0, result.output

    conn2 = _make_conn(isolated_config)
    try:
        e_target = time_entries.get(conn2, eid_target)
        e_other = time_entries.get(conn2, eid_other)
    finally:
        conn2.close()
    # Only the target-day entry was pushed
    assert e_target.pushed_at is not None
    assert e_target.jira_worklog_id == "wl-target"
    # The other day entry was not touched
    assert e_other.pushed_at is None
