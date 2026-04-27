"""Idle reconciliation logic for tsh.

When the user returns from an idle period, this module computes the list of
TimeEntry objects the storage layer should atomically persist, based on the
user's categorisation of the idle gap.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from typing import Literal

from tsh.core.models import TimeEntry
from tsh.core.timezones import ensure_aware

ReconcileChoice = Literal["same", "different", "not_work"]


def reconcile_idle(
    active: TimeEntry,
    idle_started_at: datetime,
    returned_at: datetime,
    choice: ReconcileChoice,
    chosen_ticket: str | None = None,
) -> list[TimeEntry]:
    """Compute the new entry list after an idle reconciliation.

    Inputs:
      - `active`: the currently-active TimeEntry (must have end_at=None and kind='work').
      - `idle_started_at`: when the user went idle (timezone-aware).
      - `returned_at`: when the user came back (timezone-aware, > idle_started_at).
      - `choice`: how the user categorized the gap.
      - `chosen_ticket`: required (and validated) when choice='different'; ignored otherwise.

    Returns the list of entries the storage layer should write atomically.

      - 'same'      → [active]                    (unchanged)
      - 'different' → [closed_original, gap_on_chosen_ticket, resumed_original]
      - 'not_work'  → [closed_original, not_work_gap, resumed_original]

    Raises:
      - ValueError if `active.end_at` is not None (not actually active)
      - ValueError if `active.kind` is not 'work'
      - ValueError if `idle_started_at` < `active.start_at`
      - ValueError if `returned_at <= idle_started_at`
      - ValueError if `choice == 'different'` and `chosen_ticket` is None or empty
      - ValueError if any datetime is naive
    """
    # --- Validate datetime awareness ---
    ensure_aware(idle_started_at)
    ensure_aware(returned_at)

    # --- Validate active entry ---
    if active.end_at is not None:
        raise ValueError(
            f"'active' must be an open entry (end_at=None), "
            f"but end_at={active.end_at!r}"
        )
    if active.kind != "work":
        raise ValueError(
            f"'active' must have kind='work', but kind={active.kind!r}"
        )

    # --- Validate temporal ordering ---
    if idle_started_at < active.start_at:
        raise ValueError(
            f"'idle_started_at' ({idle_started_at!r}) must not be before "
            f"'active.start_at' ({active.start_at!r})"
        )
    if returned_at <= idle_started_at:
        raise ValueError(
            f"'returned_at' ({returned_at!r}) must be strictly after "
            f"'idle_started_at' ({idle_started_at!r})"
        )

    # --- Validate choice-specific requirements ---
    if choice == "different":
        if not chosen_ticket:
            raise ValueError(
                "'chosen_ticket' must be a non-empty string when choice='different'"
            )

    # --- 'same': nothing changes ---
    if choice == "same":
        return [active]

    # --- Shared entries for 'different' and 'not_work' ---
    closed_original = replace(active, end_at=idle_started_at, updated_at=returned_at)

    resumed_original = TimeEntry(
        id=None,
        ticket_key=active.ticket_key,
        start_at=returned_at,
        end_at=None,
        kind="work",
        note=active.note,
        jira_worklog_id=None,
        pushed_at=None,
        created_at=returned_at,
        updated_at=returned_at,
    )

    if choice == "different":
        gap_entry = TimeEntry(
            id=None,
            ticket_key=chosen_ticket,
            start_at=idle_started_at,
            end_at=returned_at,
            kind="work",
            note="",
            jira_worklog_id=None,
            pushed_at=None,
            created_at=returned_at,
            updated_at=returned_at,
        )
    else:  # 'not_work'
        gap_entry = TimeEntry(
            id=None,
            ticket_key=None,
            start_at=idle_started_at,
            end_at=returned_at,
            kind="not_work",
            note="",
            jira_worklog_id=None,
            pushed_at=None,
            created_at=returned_at,
            updated_at=returned_at,
        )

    return [closed_original, gap_entry, resumed_original]
