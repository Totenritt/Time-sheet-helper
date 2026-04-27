"""Time rounding utilities for tsh.

Rounding is applied only at push time, on a copy — raw second-precision values
stay in SQLite untouched. Per spec §6.4, rounding is sum-then-round per ticket
per day, never round-then-sum.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable, Literal

from tsh.core.models import TimeEntry

RoundingMode = Literal["nearest", "up"]


def round_seconds(seconds: int, mode: RoundingMode = "nearest", minutes: int = 15) -> int:
    """Round a duration in seconds to the nearest (or up to the next) bucket.

    Bucket size is `minutes` minutes (so default = 15 minutes = 900 seconds).
    Returns the rounded value in seconds.

    Mode 'nearest': standard half-up rounding. Exactly half a bucket rounds up.
    Mode 'up': any value > 0 rounds up to the next bucket boundary.
    Both modes: 0 returns 0; negative input raises ValueError.
    """
    if seconds < 0:
        raise ValueError(f"seconds must be non-negative, got {seconds!r}")
    if seconds == 0:
        return 0

    bucket = minutes * 60

    if mode == "nearest":
        # Half-up: floor((seconds + bucket//2) / bucket) * bucket
        return ((seconds + bucket // 2) // bucket) * bucket
    else:  # mode == "up"
        # Ceiling division: ceil(seconds / bucket) * bucket
        return -(-seconds // bucket) * bucket


def sum_then_round_per_ticket(
    entries: Iterable[TimeEntry],
    mode: RoundingMode = "nearest",
    minutes: int = 15,
) -> dict[str, int]:
    """Group entries by ticket_key, sum each group's elapsed seconds, round each total.

    Only entries with kind='work', a non-None ticket_key, and a non-None end_at
    (i.e. completed work entries) are included. Active entries (end_at is None)
    and non-work entries (kind != 'work') are silently skipped — callers that
    need the active entry's elapsed time should compute it explicitly.

    Returns a dict {ticket_key: rounded_seconds}.
    """
    totals: dict[str, int] = defaultdict(int)

    for entry in entries:
        if entry.kind != "work":
            continue
        if entry.ticket_key is None:
            continue
        if entry.end_at is None:
            continue
        elapsed = int((entry.end_at - entry.start_at).total_seconds())
        totals[entry.ticket_key] += elapsed

    return {key: round_seconds(secs, mode=mode, minutes=minutes) for key, secs in totals.items()}
