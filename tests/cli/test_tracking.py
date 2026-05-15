"""Tests for tsh.cli.tracking — live-timer CLI commands."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from click.testing import CliRunner

from tsh.cli.main import cli
from tsh.cli.tracking import start, switch, stop, status, tasks


BASE = "http://127.0.0.1:42024"

# A minimal time-entry payload returned by the tracker.
_ENTRY = {
    "id": 1,
    "ticket_key": "SFXS-1",
    "start_at": "2026-04-27T10:00:00+00:00",
    "end_at": None,
    "kind": "work",
    "note": "",
    "jira_worklog_id": None,
    "pushed_at": None,
}

_STATUS_IDLE = {
    "active": None,
    "elapsed_seconds": 0,
    "idle_status": "clear",
    "idle_started_at": None,
}

_STATUS_ACTIVE = {
    "active": _ENTRY,
    "elapsed_seconds": 42,
    "idle_status": "clear",
    "idle_started_at": None,
}


def _mock_status_ok(url=BASE):
    """Register a /status mock that returns 200 (tracker is running)."""
    respx.get(f"{url}/status").mock(return_value=httpx.Response(200, json=_STATUS_IDLE))


# ---------------------------------------------------------------------------
# 1. tsh start SFXS-1 calls POST /start with the right body and prints "Started ..."
# ---------------------------------------------------------------------------

@respx.mock
def test_start_calls_post_and_prints(runner: CliRunner, isolated_config):
    _mock_status_ok()
    respx.post(f"{BASE}/start").mock(return_value=httpx.Response(200, json=_ENTRY))

    result = runner.invoke(start, ["SFXS-1"])

    assert result.exit_code == 0, result.output
    assert "Started SFXS-1" in result.output
    assert "(id=1)" in result.output
    # Verify the correct body was sent.
    sent = respx.calls.last.request
    import json as _json
    body = _json.loads(sent.content)
    assert body["ticket_key"] == "SFXS-1"


# ---------------------------------------------------------------------------
# 2. tsh switch SFXS-2 calls POST /switch
# ---------------------------------------------------------------------------

@respx.mock
def test_switch_calls_post_and_prints(runner: CliRunner, isolated_config):
    _mock_status_ok()
    entry2 = {**_ENTRY, "id": 2, "ticket_key": "SFXS-2"}
    respx.post(f"{BASE}/switch").mock(return_value=httpx.Response(200, json=entry2))

    result = runner.invoke(switch, ["SFXS-2"])

    assert result.exit_code == 0, result.output
    assert "Switched to SFXS-2" in result.output
    assert "(id=2)" in result.output


# ---------------------------------------------------------------------------
# 3. tsh stop with active timer
# ---------------------------------------------------------------------------

@respx.mock
def test_stop_with_active(runner: CliRunner, isolated_config):
    _mock_status_ok()
    closed_entry = {**_ENTRY, "end_at": "2026-04-27T11:00:00+00:00"}
    respx.post(f"{BASE}/stop").mock(
        return_value=httpx.Response(200, json={"closed": closed_entry})
    )

    result = runner.invoke(stop, [])

    assert result.exit_code == 0, result.output
    assert "Stopped SFXS-1" in result.output
    assert "(id=1)" in result.output


# ---------------------------------------------------------------------------
# 4. tsh stop with no active timer
# ---------------------------------------------------------------------------

@respx.mock
def test_stop_no_active(runner: CliRunner, isolated_config):
    _mock_status_ok()
    respx.post(f"{BASE}/stop").mock(
        return_value=httpx.Response(200, json={"closed": None})
    )

    result = runner.invoke(stop, [])

    assert result.exit_code == 0, result.output
    assert "no active timer" in result.output


# ---------------------------------------------------------------------------
# 5. tsh status prints active info
# ---------------------------------------------------------------------------

@respx.mock
def test_status_prints_active_info(runner: CliRunner, isolated_config):
    _mock_status_ok()
    respx.get(f"{BASE}/status").mock(return_value=httpx.Response(200, json=_STATUS_ACTIVE))

    result = runner.invoke(status, [])

    assert result.exit_code == 0, result.output
    assert "SFXS-1" in result.output
    assert "42s" in result.output
    assert "clear" in result.output


# ---------------------------------------------------------------------------
# 6. tsh status --json prints raw JSON
# ---------------------------------------------------------------------------

@respx.mock
def test_status_json_flag(runner: CliRunner, isolated_config):
    _mock_status_ok()
    respx.get(f"{BASE}/status").mock(return_value=httpx.Response(200, json=_STATUS_ACTIVE))

    result = runner.invoke(status, ["--json"])

    assert result.exit_code == 0, result.output
    import json as _json
    parsed = _json.loads(result.output)
    assert parsed["elapsed_seconds"] == 42


# ---------------------------------------------------------------------------
# 7. tsh tasks prints lines per item with * prefix on in_progress
# ---------------------------------------------------------------------------

_TASKS_LIST = [
    {"ticket_key": "SFXS-10", "summary": "In progress item", "status": "In Progress", "source": "in_progress"},
    {"ticket_key": "SFXS-20", "summary": "Recent item", "status": "In Progress", "source": "recent"},
]


@respx.mock
def test_tasks_lists_with_markers(runner: CliRunner, isolated_config):
    _mock_status_ok()
    respx.get(f"{BASE}/tasks").mock(return_value=httpx.Response(200, json=_TASKS_LIST))

    result = runner.invoke(tasks, [])

    assert result.exit_code == 0, result.output
    lines = result.output.strip().splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("*")
    assert "SFXS-10" in lines[0]
    assert lines[1].startswith(" ")
    assert "SFXS-20" in lines[1]


# ---------------------------------------------------------------------------
# 8. tsh tasks with empty list prints "no tickets"
# ---------------------------------------------------------------------------

@respx.mock
def test_tasks_empty_list(runner: CliRunner, isolated_config):
    _mock_status_ok()
    respx.get(f"{BASE}/tasks").mock(return_value=httpx.Response(200, json=[]))

    result = runner.invoke(tasks, [])

    assert result.exit_code == 0, result.output
    assert "no tickets" in result.output


# ---------------------------------------------------------------------------
# 9. tsh start when tracker is NOT running → exit 2 + stderr message
# ---------------------------------------------------------------------------

@respx.mock
def test_start_tracker_not_running(runner: CliRunner, isolated_config):
    # /status is not mocked; respx will raise httpx.ConnectError.
    respx.get(f"{BASE}/status").mock(side_effect=httpx.ConnectError("refused"))

    result = runner.invoke(start, ["SFXS-1"], catch_exceptions=False)

    assert result.exit_code == 2
    assert "tracker not running" in result.output


# ---------------------------------------------------------------------------
# 10. tsh start when tracker returns 409 → exit 1 + stderr "tracker error (409)"
# ---------------------------------------------------------------------------

@respx.mock
def test_start_tracker_returns_409(runner: CliRunner, isolated_config):
    _mock_status_ok()
    respx.post(f"{BASE}/start").mock(
        return_value=httpx.Response(409, json={"detail": "a timer is already active"})
    )

    result = runner.invoke(start, ["SFXS-1"])

    assert result.exit_code == 1
    assert "tracker error (409)" in result.output


# ---------------------------------------------------------------------------
# 11-14. Pending-reconciliation warning tests
# ---------------------------------------------------------------------------


def _insert_pending_entry(config_dir, ticket_key: str) -> None:
    """Helper: insert one pending-reconciliation entry into the test DB."""
    from tsh.storage import db as db_module
    from tsh.storage import time_entries
    from tsh.core.models import TimeEntry

    conn = db_module.connect(config_dir / "tsh.db")
    now = datetime.now(timezone.utc)
    try:
        eid = time_entries.insert(
            conn,
            TimeEntry(
                id=None, ticket_key=ticket_key,
                start_at=now - timedelta(hours=1),
                end_at=now - timedelta(minutes=30),
                kind="work", note="",
                jira_worklog_id=None, pushed_at=None,
                created_at=now, updated_at=now,
            ),
        )
        time_entries.update(conn, eid, pending_reconciliation=1, reconciliation_reason="sleep")
        conn.commit()
    finally:
        conn.close()


def test_status_shows_warning_when_pending_reconciliation(isolated_config, mocker):
    """When the DB has pending entries, status prints the warning before its normal output."""
    _insert_pending_entry(isolated_config, "X-1")

    mocker.patch("tsh.cli.tracking._is_running", return_value=True)
    mocker.patch("tsh.cli.tracking._get", return_value={
        "active": None, "elapsed_seconds": 0,
        "idle_status": "clear", "idle_started_at": None,
    })

    result = CliRunner().invoke(cli, ["status"])
    assert result.exit_code == 0
    assert "1 pending reconciliation" in result.output


def test_status_no_warning_when_clean(isolated_config, mocker):
    """No warning when no pending entries."""
    mocker.patch("tsh.cli.tracking._is_running", return_value=True)
    mocker.patch("tsh.cli.tracking._get", return_value={
        "active": None, "elapsed_seconds": 0,
        "idle_status": "clear", "idle_started_at": None,
    })
    result = CliRunner().invoke(cli, ["status"])
    assert result.exit_code == 0
    assert "pending reconciliation" not in result.output


def test_warning_pluralizes_correctly(isolated_config, mocker):
    """Two pending entries -> 'reconciliations' (plural)."""
    _insert_pending_entry(isolated_config, "X-1")
    _insert_pending_entry(isolated_config, "X-2")

    mocker.patch("tsh.cli.tracking._is_running", return_value=True)
    mocker.patch("tsh.cli.tracking._get", return_value={
        "active": None, "elapsed_seconds": 0,
        "idle_status": "clear", "idle_started_at": None,
    })
    result = CliRunner().invoke(cli, ["status"])
    assert "2 pending reconciliations" in result.output


def test_warning_appears_on_start_and_switch(isolated_config, mocker):
    """The warning fires on start and switch too, not just status."""
    _insert_pending_entry(isolated_config, "X-1")

    mocker.patch("tsh.cli.tracking._is_running", return_value=True)
    mocker.patch("tsh.cli.tracking._post", return_value={"id": 42, "ticket_key": "SFXS-1"})

    r1 = CliRunner().invoke(cli, ["start", "SFXS-1"])
    assert "1 pending reconciliation" in r1.output
    r2 = CliRunner().invoke(cli, ["switch", "SFXS-2"])
    assert "1 pending reconciliation" in r2.output


# ---------------------------------------------------------------------------
# 15+. tsh switch --from-branch
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "branch,expected_ticket",
    [
        ("feature/SFXS-1234-fix", "SFXS-1234"),
        ("SFXS-1234", "SFXS-1234"),
        ("feature/SFXS-1234/SFXS-5678-rebase", "SFXS-1234"),  # first match wins
        ("bugfix/ABC-9-fix", "ABC-9"),
    ],
)
def test_switch_from_branch_parses_and_posts(
    branch, expected_ticket, isolated_config, mocker
):
    mocker.patch("tsh.cli.tracking._current_branch", return_value=branch)
    mocker.patch("tsh.cli.tracking._is_running", return_value=True)
    mocker.patch("tsh.cli.tracking._get", return_value={
        "active": None, "elapsed_seconds": 0,
        "idle_status": "clear", "idle_started_at": None,
    })
    post_mock = mocker.patch("tsh.cli.tracking._post",
                              return_value={"ticket_key": expected_ticket, "id": 99})
    result = CliRunner().invoke(cli, ["switch", "--from-branch"])
    assert result.exit_code == 0, result.output
    post_mock.assert_called_once()
    args = post_mock.call_args
    # _post signature: _post(path, body=None)
    assert args.args[0] == "/switch"
    posted_body = args.args[1] if len(args.args) > 1 else args.kwargs.get("body", {})
    assert posted_body["ticket_key"] == expected_ticket


@pytest.mark.parametrize(
    "branch",
    ["sfxs-1234", "chore/cleanup", "release/2026-05-15", "main", "master"],
)
def test_switch_from_branch_no_match_exits_silently(branch, isolated_config, mocker):
    mocker.patch("tsh.cli.tracking._current_branch", return_value=branch)
    mocker.patch("tsh.cli.tracking._is_running", return_value=True)
    post_mock = mocker.patch("tsh.cli.tracking._post")
    result = CliRunner().invoke(cli, ["switch", "--from-branch"])
    assert result.exit_code == 0
    post_mock.assert_not_called()


def test_switch_from_branch_no_repo_exits_silently(isolated_config, mocker):
    mocker.patch("tsh.cli.tracking._current_branch", return_value=None)
    post_mock = mocker.patch("tsh.cli.tracking._post")
    result = CliRunner().invoke(cli, ["switch", "--from-branch"])
    assert result.exit_code == 0
    post_mock.assert_not_called()


def test_switch_from_branch_idempotent_when_already_active(isolated_config, mocker):
    mocker.patch("tsh.cli.tracking._current_branch", return_value="feature/SFXS-1234-x")
    mocker.patch("tsh.cli.tracking._is_running", return_value=True)
    mocker.patch("tsh.cli.tracking._get",
                  return_value={"active": {"ticket_key": "SFXS-1234"},
                                "elapsed_seconds": 60, "idle_status": "clear",
                                "idle_started_at": None})
    post_mock = mocker.patch("tsh.cli.tracking._post")
    result = CliRunner().invoke(cli, ["switch", "--from-branch"])
    assert result.exit_code == 0
    post_mock.assert_not_called()


def test_switch_from_branch_tracker_not_running_silent(isolated_config, mocker):
    mocker.patch("tsh.cli.tracking._current_branch", return_value="feature/SFXS-1234-x")
    mocker.patch("tsh.cli.tracking._is_running", return_value=False)
    post_mock = mocker.patch("tsh.cli.tracking._post")
    result = CliRunner().invoke(cli, ["switch", "--from-branch"])
    assert result.exit_code == 0
    post_mock.assert_not_called()
