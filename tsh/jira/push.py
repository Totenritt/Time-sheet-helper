"""Push orchestration for tsh.

plan_push  – pure function: groups entries by ticket, sums elapsed seconds,
             rounds, builds the Jira ``started`` ISO string, and combines
             comments.  No I/O.

execute_push – I/O half: iterates planned pushes, calls JiraClient.post_worklog
              for each, sleeps 100 ms between calls (rate limit), and returns
              one PushResult per plan.  Supports dry_run mode that returns
              plans without touching Jira.

The caller (CLI in Phase 5) is responsible for calling
``time_entries.mark_pushed(...)`` for each successful PushResult.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from collections.abc import Callable, Iterable

from tsh.core.models import TimeEntry
from tsh.core.time_math import round_seconds, RoundingMode
from tsh.core.timezones import to_jira_started
from tsh.jira.client import JiraClient, JiraError


@dataclass(frozen=True)
class PlannedPush:
    """One worklog POST that's been planned but not yet sent."""

    entry_ids: tuple[int, ...]   # which time_entries.id values feed this push
    ticket_key: str
    started_iso: str             # the exact string Jira will receive
    time_spent_seconds: int      # rounded
    comment: str                 # combined comment string (may be empty)


@dataclass(frozen=True)
class PushResult:
    """Outcome of one planned push attempt."""

    entry_ids: tuple[int, ...]
    ticket_key: str
    ok: bool
    jira_worklog_id: str | None  # set on success
    error: str | None            # human-readable error message on failure
    dry_run: bool = False        # True when produced by execute_push(dry_run=True)


def plan_push(
    entries: Iterable[TimeEntry],
    *,
    mode: RoundingMode = "nearest",
    minutes: int = 15,
    tz: str = "Australia/Sydney",
) -> list[PlannedPush]:
    """Group entries by ticket, sum durations, round, build payloads.

    Only includes work entries (kind='work'), with non-None ticket_key,
    end_at set (closed), and pushed_at = None (drafts only).

    The ``started`` string is built from the EARLIEST ``start_at`` among the
    entries that contribute to each plan, formatted via to_jira_started(...).
    The ``comment`` is built by combining distinct non-empty notes (see below).
    Plans whose rounded total is 0 seconds are SKIPPED.

    Returns a list ordered by ``started_iso`` ascending.
    """
    # Group eligible entries by ticket_key, preserving insertion order.
    groups: dict[str, list[TimeEntry]] = {}
    for entry in entries:
        if entry.kind != "work":
            continue
        if entry.ticket_key is None:
            continue
        if entry.end_at is None:
            continue
        if entry.pushed_at is not None:
            continue
        groups.setdefault(entry.ticket_key, []).append(entry)

    plans: list[PlannedPush] = []
    for ticket_key, group in groups.items():
        # Sum elapsed seconds first, then round (spec §6.4).
        total_seconds = sum(
            int((e.end_at - e.start_at).total_seconds()) for e in group  # type: ignore[operator]
        )
        rounded = round_seconds(total_seconds, mode=mode, minutes=minutes)
        if rounded == 0:
            continue

        # Earliest start_at determines the worklog's "started" timestamp.
        earliest_start = min(e.start_at for e in group)
        started_iso = to_jira_started(earliest_start, tz=tz)

        # Combine distinct non-empty notes in source order.
        comment = _combine_notes(e.note for e in group)

        plans.append(
            PlannedPush(
                entry_ids=tuple(e.id for e in group),  # type: ignore[arg-type]
                ticket_key=ticket_key,
                started_iso=started_iso,
                time_spent_seconds=rounded,
                comment=comment,
            )
        )

    # Sort chronologically so POSTs happen in time order.
    plans.sort(key=lambda p: p.started_iso)
    return plans


def execute_push(
    planned: list[PlannedPush],
    client: JiraClient,
    *,
    dry_run: bool = False,
    sleep_ms: int = 100,
    _sleep: Callable[[float], None] = time.sleep,
) -> list[PushResult]:
    """Execute each planned push against Jira (or simulate in dry_run).

    Sleeps ``sleep_ms`` milliseconds BETWEEN successful or failed POSTs (not
    before the first, not after the last).

    Per-entry failure isolation: if one POST raises JiraError, that entry
    becomes an ``ok=False`` PushResult and the loop continues with the next.
    Other exception types (e.g. coding bugs) propagate.

    Returns a list of PushResult in the same order as ``planned``.
    """
    results: list[PushResult] = []

    for i, plan in enumerate(planned):
        # Sleep BETWEEN iterations (not before the first).
        if i > 0 and not dry_run:
            _sleep(sleep_ms / 1000.0)

        if dry_run:
            results.append(
                PushResult(
                    entry_ids=plan.entry_ids,
                    ticket_key=plan.ticket_key,
                    ok=True,
                    jira_worklog_id=None,
                    error=None,
                    dry_run=True,
                )
            )
            continue

        try:
            response = client.post_worklog(
                key=plan.ticket_key,
                started=plan.started_iso,
                time_spent_seconds=plan.time_spent_seconds,
                comment=plan.comment,
            )
            results.append(
                PushResult(
                    entry_ids=plan.entry_ids,
                    ticket_key=plan.ticket_key,
                    ok=True,
                    jira_worklog_id=str(response["id"]),
                    error=None,
                    dry_run=False,
                )
            )
        except JiraError as exc:
            results.append(
                PushResult(
                    entry_ids=plan.entry_ids,
                    ticket_key=plan.ticket_key,
                    ok=False,
                    jira_worklog_id=None,
                    error=str(exc),
                    dry_run=False,
                )
            )

    return results


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _combine_notes(notes: Iterable[str]) -> str:
    """Combine distinct non-empty notes, trimming whitespace, preserving order.

    Examples:
        ["A", "", "A", "B"]   → "A; B"
        ["A ", " A", "B"]     → "A; B"
        ["", ""]              → ""
    """
    seen: set[str] = set()
    parts: list[str] = []
    for raw in notes:
        note = raw.strip()
        if note and note not in seen:
            seen.add(note)
            parts.append(note)
    return "; ".join(parts)
