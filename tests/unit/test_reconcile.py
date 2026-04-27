"""Tests for tsh.core.reconcile — reconcile_idle."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tsh.core.models import TimeEntry
from tsh.core.reconcile import reconcile_idle

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_BASE = datetime(2025, 6, 1, 9, 0, 0, tzinfo=timezone.utc)
_IDLE_AT = _BASE + timedelta(hours=1)       # 10:00 UTC
_RETURNED_AT = _IDLE_AT + timedelta(minutes=30)  # 10:30 UTC


def _make_active_entry(
    ticket_key: str = "SFXS-1073",
    note: str = "",
    start_at: datetime = _BASE,
) -> TimeEntry:
    """Build an active TimeEntry (end_at=None, kind='work') with UTC-aware datetimes."""
    return TimeEntry(
        id=42,
        ticket_key=ticket_key,
        start_at=start_at,
        end_at=None,
        kind="work",
        jira_worklog_id=None,
        pushed_at=None,
        created_at=_BASE,
        updated_at=_BASE,
        note=note,
    )


# ---------------------------------------------------------------------------
# Happy path: 'same'
# ---------------------------------------------------------------------------

def test_same_returns_list_of_active_unchanged() -> None:
    active = _make_active_entry()
    result = reconcile_idle(active, _IDLE_AT, _RETURNED_AT, "same")
    assert len(result) == 1
    assert result[0] is active  # identity, not copy


# ---------------------------------------------------------------------------
# Happy path: 'different'
# ---------------------------------------------------------------------------

def test_different_returns_three_entries() -> None:
    active = _make_active_entry()
    result = reconcile_idle(active, _IDLE_AT, _RETURNED_AT, "different", chosen_ticket="COL-99")
    assert len(result) == 3


def test_different_entry0_is_closed_original() -> None:
    active = _make_active_entry()
    result = reconcile_idle(active, _IDLE_AT, _RETURNED_AT, "different", chosen_ticket="COL-99")
    closed = result[0]
    assert closed.id == active.id
    assert closed.ticket_key == active.ticket_key
    assert closed.end_at == _IDLE_AT
    assert closed.kind == "work"


def test_different_entry1_is_gap_on_chosen_ticket() -> None:
    active = _make_active_entry()
    result = reconcile_idle(active, _IDLE_AT, _RETURNED_AT, "different", chosen_ticket="COL-99")
    gap = result[1]
    assert gap.id is None
    assert gap.ticket_key == "COL-99"
    assert gap.start_at == _IDLE_AT
    assert gap.end_at == _RETURNED_AT
    assert gap.kind == "work"


def test_different_entry2_is_resumed_original() -> None:
    active = _make_active_entry()
    result = reconcile_idle(active, _IDLE_AT, _RETURNED_AT, "different", chosen_ticket="COL-99")
    resumed = result[2]
    assert resumed.id is None
    assert resumed.ticket_key == active.ticket_key
    assert resumed.start_at == _RETURNED_AT
    assert resumed.end_at is None
    assert resumed.kind == "work"


# ---------------------------------------------------------------------------
# Happy path: 'not_work'
# ---------------------------------------------------------------------------

def test_not_work_returns_three_entries() -> None:
    active = _make_active_entry()
    result = reconcile_idle(active, _IDLE_AT, _RETURNED_AT, "not_work")
    assert len(result) == 3


def test_not_work_entry0_is_closed_original() -> None:
    active = _make_active_entry()
    result = reconcile_idle(active, _IDLE_AT, _RETURNED_AT, "not_work")
    closed = result[0]
    assert closed.id == active.id
    assert closed.end_at == _IDLE_AT


def test_not_work_entry1_is_gap_not_work() -> None:
    active = _make_active_entry()
    result = reconcile_idle(active, _IDLE_AT, _RETURNED_AT, "not_work")
    gap = result[1]
    assert gap.id is None
    assert gap.ticket_key is None
    assert gap.start_at == _IDLE_AT
    assert gap.end_at == _RETURNED_AT
    assert gap.kind == "not_work"


def test_not_work_entry2_is_resumed_original() -> None:
    active = _make_active_entry()
    result = reconcile_idle(active, _IDLE_AT, _RETURNED_AT, "not_work")
    resumed = result[2]
    assert resumed.id is None
    assert resumed.ticket_key == active.ticket_key
    assert resumed.start_at == _RETURNED_AT
    assert resumed.end_at is None
    assert resumed.kind == "work"


# ---------------------------------------------------------------------------
# Validation failures
# ---------------------------------------------------------------------------

def test_active_already_closed_raises() -> None:
    closed_entry = TimeEntry(
        id=1,
        ticket_key="SFXS-1073",
        start_at=_BASE,
        end_at=_IDLE_AT,
        kind="work",
        jira_worklog_id=None,
        pushed_at=None,
        created_at=_BASE,
        updated_at=_BASE,
    )
    with pytest.raises(ValueError):
        reconcile_idle(closed_entry, _IDLE_AT, _RETURNED_AT, "same")


def test_active_kind_not_work_raises() -> None:
    not_work_entry = TimeEntry(
        id=1,
        ticket_key=None,
        start_at=_BASE,
        end_at=None,
        kind="not_work",
        jira_worklog_id=None,
        pushed_at=None,
        created_at=_BASE,
        updated_at=_BASE,
    )
    with pytest.raises(ValueError):
        reconcile_idle(not_work_entry, _IDLE_AT, _RETURNED_AT, "same")


def test_idle_started_before_active_start_raises() -> None:
    active = _make_active_entry()
    too_early = _BASE - timedelta(minutes=1)
    with pytest.raises(ValueError):
        reconcile_idle(active, too_early, _RETURNED_AT, "same")


def test_returned_at_equal_to_idle_raises() -> None:
    active = _make_active_entry()
    with pytest.raises(ValueError):
        reconcile_idle(active, _IDLE_AT, _IDLE_AT, "same")


def test_returned_at_before_idle_raises() -> None:
    active = _make_active_entry()
    before = _IDLE_AT - timedelta(seconds=1)
    with pytest.raises(ValueError):
        reconcile_idle(active, _IDLE_AT, before, "same")


def test_different_with_chosen_ticket_none_raises() -> None:
    active = _make_active_entry()
    with pytest.raises(ValueError):
        reconcile_idle(active, _IDLE_AT, _RETURNED_AT, "different", chosen_ticket=None)


def test_different_with_chosen_ticket_empty_raises() -> None:
    active = _make_active_entry()
    with pytest.raises(ValueError):
        reconcile_idle(active, _IDLE_AT, _RETURNED_AT, "different", chosen_ticket="")


def test_different_with_valid_chosen_ticket_succeeds() -> None:
    active = _make_active_entry()
    result = reconcile_idle(active, _IDLE_AT, _RETURNED_AT, "different", chosen_ticket="COL-99")
    assert len(result) == 3


def test_not_work_with_non_none_chosen_ticket_is_silently_ignored() -> None:
    active = _make_active_entry()
    # should not raise; chosen_ticket is irrelevant for not_work
    result = reconcile_idle(active, _IDLE_AT, _RETURNED_AT, "not_work", chosen_ticket="COL-99")
    assert len(result) == 3
    # gap entry must still have ticket_key=None (not the provided COL-99)
    assert result[1].ticket_key is None


def test_naive_idle_started_at_raises() -> None:
    active = _make_active_entry()
    naive = datetime(2025, 6, 1, 10, 0, 0)  # no tzinfo
    with pytest.raises(ValueError):
        reconcile_idle(active, naive, _RETURNED_AT, "same")


def test_naive_returned_at_raises() -> None:
    active = _make_active_entry()
    naive = datetime(2025, 6, 1, 10, 30, 0)  # no tzinfo
    with pytest.raises(ValueError):
        reconcile_idle(active, _IDLE_AT, naive, "same")


# ---------------------------------------------------------------------------
# Continuity properties
# ---------------------------------------------------------------------------

def test_different_entries_are_gap_free() -> None:
    active = _make_active_entry()
    result = reconcile_idle(active, _IDLE_AT, _RETURNED_AT, "different", chosen_ticket="COL-99")
    assert result[0].end_at == result[1].start_at
    assert result[1].end_at == result[2].start_at


def test_not_work_entries_are_gap_free() -> None:
    active = _make_active_entry()
    result = reconcile_idle(active, _IDLE_AT, _RETURNED_AT, "not_work")
    assert result[0].end_at == result[1].start_at
    assert result[1].end_at == result[2].start_at


def test_different_resumed_entry_preserves_note() -> None:
    active = _make_active_entry(note="reviewing PR")
    result = reconcile_idle(active, _IDLE_AT, _RETURNED_AT, "different", chosen_ticket="COL-99")
    assert result[2].note == "reviewing PR"


def test_not_work_resumed_entry_preserves_note() -> None:
    active = _make_active_entry(note="writing tests")
    result = reconcile_idle(active, _IDLE_AT, _RETURNED_AT, "not_work")
    assert result[2].note == "writing tests"
