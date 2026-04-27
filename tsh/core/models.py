"""Core domain models for tsh.

All datetime fields must be timezone-aware. Frozen dataclasses are used so
instances are hashable and accidental mutation is caught at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

Kind = Literal["work", "not_work", "idle_unresolved"]

_VALID_KINDS: frozenset[str] = frozenset({"work", "not_work", "idle_unresolved"})


def _require_aware(field_name: str, value: datetime) -> None:
    """Raise ValueError if *value* is None or a naive datetime (tzinfo is None).

    Use this for fields that are typed as required (non-optional) datetime.
    """
    if value is None:
        raise ValueError(
            f"'{field_name}' is required and must not be None."
        )
    if value.tzinfo is None:
        raise ValueError(
            f"'{field_name}' must be a timezone-aware datetime, "
            f"but received a naive datetime: {value!r}"
        )


def _require_optional_aware(field_name: str, value: datetime | None) -> None:
    """Raise ValueError if *value* is a naive datetime (tzinfo is None).

    Use this for fields that are typed as optional (datetime | None); None is
    allowed and passes through without error.
    """
    if value is not None and value.tzinfo is None:
        raise ValueError(
            f"'{field_name}' must be a timezone-aware datetime, "
            f"but received a naive datetime: {value!r}"
        )


@dataclass(frozen=True)
class TimeEntry:
    """A single tracked time interval.

    Fields
    ------
    id              -- database PK; None for unsaved entries.
    ticket_key      -- Jira issue key (e.g. 'PROJ-123'); None for non-work periods.
    start_at        -- timezone-aware start time (required).
    end_at          -- timezone-aware end time; None means the timer is still running.
    note            -- free-text comment that becomes the Jira worklog body.
    kind            -- one of 'work', 'not_work', 'idle_unresolved'.
    jira_worklog_id -- Jira worklog ID; None until pushed.
    pushed_at       -- when the entry was pushed to Jira; None until pushed.
    created_at      -- timezone-aware creation timestamp.
    updated_at      -- timezone-aware last-update timestamp.
    """

    id: int | None
    ticket_key: str | None
    start_at: datetime
    end_at: datetime | None
    kind: Kind
    jira_worklog_id: str | None
    pushed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    # Moved to end of field list to satisfy dataclass default-ordering rules;
    # logically the 5th field per spec §5.1.
    note: str = field(default="")

    def __post_init__(self) -> None:
        # Validate kind at runtime (Literal alone does not enforce this).
        if self.kind not in _VALID_KINDS:
            raise ValueError(
                f"Invalid kind {self.kind!r}. Must be one of {sorted(_VALID_KINDS)}."
            )

        # Validate timezone-awareness for every datetime field.
        # Required fields: None is not accepted.
        _require_aware("start_at", self.start_at)
        _require_aware("created_at", self.created_at)
        _require_aware("updated_at", self.updated_at)
        # Optional fields: None is accepted, but a naive datetime is not.
        _require_optional_aware("end_at", self.end_at)
        _require_optional_aware("pushed_at", self.pushed_at)

    @property
    def is_active(self) -> bool:
        """True when the timer is still running (end_at has not been set)."""
        return self.end_at is None

    @property
    def is_pushed(self) -> bool:
        """True when the entry has been pushed to Jira."""
        return self.pushed_at is not None


@dataclass(frozen=True)
class TicketCacheEntry:
    """A cached snapshot of a Jira issue, used to avoid redundant API calls.

    Fields
    ------
    ticket_key      -- Jira issue key (e.g. 'PROJ-123').
    summary         -- issue summary / title.
    status          -- current workflow status (e.g. 'In Progress').
    assignee_email  -- email address of the current assignee.
    last_fetched_at -- when this record was last refreshed from Jira (tz-aware).
    last_used_at    -- when this ticket was last referenced by the user (tz-aware).
    """

    ticket_key: str
    summary: str
    status: str
    assignee_email: str
    last_fetched_at: datetime
    last_used_at: datetime

    def __post_init__(self) -> None:
        _require_aware("last_fetched_at", self.last_fetched_at)
        _require_aware("last_used_at", self.last_used_at)
