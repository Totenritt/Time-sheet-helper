"""Tests for tsh.core.models — TimeEntry and TicketCacheEntry."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tsh.core.models import TicketCacheEntry, TimeEntry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _aware() -> datetime:
    """Return a timezone-aware datetime (UTC now)."""
    return datetime.now(timezone.utc)


def _naive() -> datetime:
    """Return a naive datetime (no tzinfo)."""
    return datetime(2024, 1, 1, 12, 0, 0)


def _make_entry(**overrides) -> TimeEntry:
    """Build a minimal valid TimeEntry, with optional field overrides."""
    now = _aware()
    defaults = dict(
        id=None,
        ticket_key="PROJ-1",
        start_at=now,
        end_at=None,
        note="",
        kind="work",
        jira_worklog_id=None,
        pushed_at=None,
        created_at=now,
        updated_at=now,
    )
    defaults.update(overrides)
    return TimeEntry(**defaults)


# ---------------------------------------------------------------------------
# 1. TimeEntry constructs successfully with all-aware datetimes
# ---------------------------------------------------------------------------

def test_time_entry_constructs_with_aware_datetimes() -> None:
    now = _aware()
    entry = TimeEntry(
        id=42,
        ticket_key="PROJ-123",
        start_at=now,
        end_at=now,
        note="did some work",
        kind="work",
        jira_worklog_id="wl-99",
        pushed_at=now,
        created_at=now,
        updated_at=now,
    )
    assert entry.id == 42
    assert entry.ticket_key == "PROJ-123"
    assert entry.note == "did some work"
    assert entry.kind == "work"
    assert entry.jira_worklog_id == "wl-99"


def test_note_defaults_to_empty_string() -> None:
    now = _aware()
    entry = TimeEntry(
        id=None,
        ticket_key="PROJ-1",
        start_at=now,
        end_at=None,
        kind="work",
        jira_worklog_id=None,
        pushed_at=None,
        created_at=now,
        updated_at=now,
    )
    assert entry.note == ""


# ---------------------------------------------------------------------------
# 2. is_active property
# ---------------------------------------------------------------------------

def test_is_active_true_when_end_at_is_none() -> None:
    entry = _make_entry(end_at=None)
    assert entry.is_active is True


def test_is_active_false_when_end_at_is_set() -> None:
    entry = _make_entry(end_at=_aware())
    assert entry.is_active is False


# ---------------------------------------------------------------------------
# 3. is_pushed property
# ---------------------------------------------------------------------------

def test_is_pushed_false_when_pushed_at_is_none() -> None:
    entry = _make_entry(pushed_at=None)
    assert entry.is_pushed is False


def test_is_pushed_true_when_pushed_at_is_set() -> None:
    entry = _make_entry(pushed_at=_aware())
    assert entry.is_pushed is True


# ---------------------------------------------------------------------------
# 4. Rejects naive start_at
# ---------------------------------------------------------------------------

def test_rejects_naive_start_at() -> None:
    with pytest.raises(ValueError, match="start_at"):
        _make_entry(start_at=_naive())


# ---------------------------------------------------------------------------
# 5. Rejects naive end_at (when provided)
# ---------------------------------------------------------------------------

def test_rejects_naive_end_at() -> None:
    with pytest.raises(ValueError, match="end_at"):
        _make_entry(end_at=_naive())


# ---------------------------------------------------------------------------
# 6. Rejects naive pushed_at (when provided)
# ---------------------------------------------------------------------------

def test_rejects_naive_pushed_at() -> None:
    with pytest.raises(ValueError, match="pushed_at"):
        _make_entry(pushed_at=_naive())


# ---------------------------------------------------------------------------
# 7. Rejects invalid kind
# ---------------------------------------------------------------------------

def test_rejects_invalid_kind() -> None:
    with pytest.raises(ValueError, match="kind"):
        _make_entry(kind="invalid_kind")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 8. TimeEntry is hashable (frozen dataclass)
# ---------------------------------------------------------------------------

def test_time_entry_is_hashable() -> None:
    entry = _make_entry()
    s = {entry}
    assert entry in s


# ---------------------------------------------------------------------------
# 9. TicketCacheEntry constructs successfully with aware datetimes
# ---------------------------------------------------------------------------

def test_ticket_cache_entry_constructs_with_aware_datetimes() -> None:
    now = _aware()
    entry = TicketCacheEntry(
        ticket_key="PROJ-42",
        summary="Fix the thing",
        status="In Progress",
        assignee_email="dev@example.com",
        last_fetched_at=now,
        last_used_at=now,
    )
    assert entry.ticket_key == "PROJ-42"
    assert entry.summary == "Fix the thing"
    assert entry.status == "In Progress"
    assert entry.assignee_email == "dev@example.com"


# ---------------------------------------------------------------------------
# 10. TicketCacheEntry rejects naive last_fetched_at
# ---------------------------------------------------------------------------

def test_ticket_cache_entry_rejects_naive_last_fetched_at() -> None:
    now = _aware()
    with pytest.raises(ValueError, match="last_fetched_at"):
        TicketCacheEntry(
            ticket_key="PROJ-42",
            summary="Fix the thing",
            status="In Progress",
            assignee_email="dev@example.com",
            last_fetched_at=_naive(),
            last_used_at=now,
        )


# ---------------------------------------------------------------------------
# 11. TicketCacheEntry rejects naive last_used_at
# ---------------------------------------------------------------------------

def test_ticket_cache_entry_rejects_naive_last_used_at() -> None:
    now = _aware()
    with pytest.raises(ValueError, match="last_used_at"):
        TicketCacheEntry(
            ticket_key="PROJ-42",
            summary="Fix the thing",
            status="In Progress",
            assignee_email="dev@example.com",
            last_fetched_at=now,
            last_used_at=_naive(),
        )


# ---------------------------------------------------------------------------
# 12. Required datetime fields reject None
# ---------------------------------------------------------------------------

def test_time_entry_rejects_none_start_at() -> None:
    with pytest.raises(ValueError, match="start_at"):
        _make_entry(start_at=None)  # type: ignore[arg-type]


def test_time_entry_rejects_none_created_at() -> None:
    with pytest.raises(ValueError, match="created_at"):
        _make_entry(created_at=None)  # type: ignore[arg-type]


def test_time_entry_rejects_none_updated_at() -> None:
    with pytest.raises(ValueError, match="updated_at"):
        _make_entry(updated_at=None)  # type: ignore[arg-type]


def test_ticket_cache_entry_rejects_none_last_fetched_at() -> None:
    now = _aware()
    with pytest.raises(ValueError, match="last_fetched_at"):
        TicketCacheEntry(
            ticket_key="PROJ-42",
            summary="Fix the thing",
            status="In Progress",
            assignee_email="dev@example.com",
            last_fetched_at=None,  # type: ignore[arg-type]
            last_used_at=now,
        )


def test_ticket_cache_entry_rejects_none_last_used_at() -> None:
    now = _aware()
    with pytest.raises(ValueError, match="last_used_at"):
        TicketCacheEntry(
            ticket_key="PROJ-42",
            summary="Fix the thing",
            status="In Progress",
            assignee_email="dev@example.com",
            last_fetched_at=now,
            last_used_at=None,  # type: ignore[arg-type]
        )
