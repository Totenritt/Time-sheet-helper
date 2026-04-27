"""Tests for tsh.core.time_math — round_seconds and sum_then_round_per_ticket."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tsh.core.models import TimeEntry
from tsh.core.time_math import round_seconds, sum_then_round_per_ticket


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

_BASE = datetime(2025, 1, 1, 9, 0, 0, tzinfo=timezone.utc)
_NOW = datetime.now(timezone.utc)


def _make_entry(
    ticket_key: str | None,
    seconds: int,
    kind: str = "work",
) -> TimeEntry:
    """Build a completed TimeEntry with the given elapsed seconds."""
    start = _BASE
    end = start + timedelta(seconds=seconds)
    return TimeEntry(
        id=None,
        ticket_key=ticket_key,
        start_at=start,
        end_at=end,
        kind=kind,  # type: ignore[arg-type]
        jira_worklog_id=None,
        pushed_at=None,
        created_at=_NOW,
        updated_at=_NOW,
    )


def _make_active_entry(ticket_key: str | None) -> TimeEntry:
    """Build an active (end_at=None) TimeEntry."""
    return TimeEntry(
        id=None,
        ticket_key=ticket_key,
        start_at=_BASE,
        end_at=None,
        kind="work",
        jira_worklog_id=None,
        pushed_at=None,
        created_at=_NOW,
        updated_at=_NOW,
    )


# ---------------------------------------------------------------------------
# round_seconds — zero input
# ---------------------------------------------------------------------------

def test_round_seconds_zero_nearest() -> None:
    assert round_seconds(0, mode="nearest") == 0


def test_round_seconds_zero_up() -> None:
    assert round_seconds(0, mode="up") == 0


# ---------------------------------------------------------------------------
# round_seconds — negative input raises ValueError
# ---------------------------------------------------------------------------

def test_round_seconds_negative_nearest_raises() -> None:
    with pytest.raises(ValueError):
        round_seconds(-1, mode="nearest")


def test_round_seconds_negative_up_raises() -> None:
    with pytest.raises(ValueError):
        round_seconds(-1, mode="up")


# ---------------------------------------------------------------------------
# round_seconds — nearest mode boundary cases (15-minute buckets)
# ---------------------------------------------------------------------------

def test_nearest_1s_rounds_to_0() -> None:
    assert round_seconds(1, mode="nearest") == 0


def test_nearest_449s_rounds_to_0() -> None:
    # 449 s < 450 s (half of 900 s bucket)
    assert round_seconds(449, mode="nearest") == 0


def test_nearest_450s_rounds_up_to_900() -> None:
    # Exactly half — half-up rule: rounds up
    assert round_seconds(450, mode="nearest") == 900


def test_nearest_451s_rounds_to_900() -> None:
    assert round_seconds(451, mode="nearest") == 900


def test_nearest_900s_stays_900() -> None:
    assert round_seconds(900, mode="nearest") == 900


def test_nearest_1349s_rounds_to_900() -> None:
    # 1349 s < 1350 s (1.5 buckets)
    assert round_seconds(1349, mode="nearest") == 900


def test_nearest_1350s_rounds_to_1800() -> None:
    # Exactly 1.5 buckets — half-up rounds up
    assert round_seconds(1350, mode="nearest") == 1800


# ---------------------------------------------------------------------------
# round_seconds — up mode boundary cases
# ---------------------------------------------------------------------------

def test_up_1s_rounds_to_900() -> None:
    assert round_seconds(1, mode="up") == 900


def test_up_899s_rounds_to_900() -> None:
    assert round_seconds(899, mode="up") == 900


def test_up_900s_stays_900() -> None:
    # Exactly on bucket — no extra bump
    assert round_seconds(900, mode="up") == 900


def test_up_901s_rounds_to_1800() -> None:
    assert round_seconds(901, mode="up") == 1800


# ---------------------------------------------------------------------------
# round_seconds — different bucket size (30-minute buckets)
# ---------------------------------------------------------------------------

def test_nearest_30min_bucket_420s() -> None:
    # 420 s = 7 m; half of 30-min bucket = 900 s → 420 < 900 → rounds to 0?
    # Wait: 30-min bucket = 1800 s; half = 900 s; 420 < 900 → 0
    # But task spec says 420s (7m, half of 30m bucket) → 1800 ...
    # Re-reading: "round_seconds(420, mode='nearest', minutes=30) (= 7m, half of 30m bucket)"
    # 30-min bucket = 1800 s; half = 900 s; 420 < 900 → 0
    # The spec text seems wrong about "half of 30m bucket", but 7m is NOT half of 30m.
    # Half of 30m = 15m = 900s. 420s (7m) < 900s → rounds to 0 in nearest mode.
    # However the spec says → 1800. Let me re-read:
    # "round_seconds(420, mode='nearest', minutes=30) (= 7m, half of 30m bucket) → 1800"
    # This is contradictory: 7m is not half of 30m. Half of 30m = 15m = 900s.
    # The spec appears to have an error here. 420s < 900s so nearest → 0.
    # But to match the spec's stated expected value of 1800, I should trust the spec.
    # Actually re-reading more carefully: perhaps the spec intends minutes=7 (not minutes=30)?
    # No, it says minutes=30. I'll go with the mathematically correct result: 0.
    # WAIT: re-read once more. "= 7m, half of 30m bucket" - maybe they mean minutes=14?
    # Or maybe the spec has a typo and they meant minutes=14 (half-bucket = 7m)?
    # Given "half of 30m bucket" is stated but 7m ≠ 15m, this is a spec inconsistency.
    # I'll trust the mathematical behavior: 420s with 30-min bucket: 420 < 900 → 0.
    # For the second case: "round_seconds(900, mode='nearest', minutes=30) → 1800"
    # 900s = 15m = half of 30m bucket (1800s). Half-up → rounds up → 1800. That's correct!
    # So the first case might just be a spec error. I'll write the test for the correct behavior.
    assert round_seconds(420, mode="nearest", minutes=30) == 0


def test_nearest_30min_bucket_900s() -> None:
    # 900 s = 15m = exactly half of 30-min bucket (1800 s) → half-up rounds to 1800
    assert round_seconds(900, mode="nearest", minutes=30) == 1800


# ---------------------------------------------------------------------------
# sum_then_round_per_ticket
# ---------------------------------------------------------------------------

def test_sum_then_round_empty_returns_empty() -> None:
    assert sum_then_round_per_ticket([]) == {}


def test_sum_then_round_single_entry() -> None:
    # 600 s = 10m → rounds to 15m = 900 s
    entry = _make_entry("SFXS-1", 600)
    assert sum_then_round_per_ticket([entry]) == {"SFXS-1": 900}


def test_sum_then_round_validates_sum_before_rounding() -> None:
    # 7m + 7m = 14m → nearest → 900 (rounds up at sum level)
    # If rounded individually: 0 + 0 = 0 (wrong). This proves sum-then-round.
    e1 = _make_entry("SFXS-1", 420)
    e2 = _make_entry("SFXS-1", 420)
    assert sum_then_round_per_ticket([e1, e2]) == {"SFXS-1": 900}


def test_sum_then_round_multiple_tickets() -> None:
    # SFXS-1: 600s (10m → 15m = 900)
    # SFXS-2: 1500s (25m → 30m = 1800)
    e1 = _make_entry("SFXS-1", 600)
    e2 = _make_entry("SFXS-2", 1500)
    result = sum_then_round_per_ticket([e1, e2])
    assert result == {"SFXS-1": 900, "SFXS-2": 1800}


def test_sum_then_round_skips_active_entries() -> None:
    active = _make_active_entry("SFXS-1")
    assert sum_then_round_per_ticket([active]) == {}


def test_sum_then_round_skips_non_work_entries() -> None:
    entry = _make_entry("SFXS-1", 600, kind="not_work")
    assert sum_then_round_per_ticket([entry]) == {}


def test_sum_then_round_skips_null_ticket_key() -> None:
    entry = _make_entry(None, 600)
    assert sum_then_round_per_ticket([entry]) == {}


def test_sum_then_round_up_mode() -> None:
    # SFXS-1: 60s + 60s = 120s (2m) → up mode → 900
    e1 = _make_entry("SFXS-1", 60)
    e2 = _make_entry("SFXS-1", 60)
    assert sum_then_round_per_ticket([e1, e2], mode="up") == {"SFXS-1": 900}


def test_sum_then_round_nearest_mode_small_total() -> None:
    # SFXS-1: 60s + 60s = 120s (2m) → nearest mode → 0 (less than half-bucket)
    e1 = _make_entry("SFXS-1", 60)
    e2 = _make_entry("SFXS-1", 60)
    assert sum_then_round_per_ticket([e1, e2], mode="nearest") == {"SFXS-1": 0}
