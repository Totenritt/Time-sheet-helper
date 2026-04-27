"""Tests for tsh.core.timezones — timezone conversion helpers."""

from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from tsh.core.timezones import (
    DEFAULT_DISPLAY_TZ,
    ensure_aware,
    to_display,
    to_jira_started,
    to_utc,
    today_in_display_tz,
)

SYDNEY = ZoneInfo("Australia/Sydney")


# ---------------------------------------------------------------------------
# 1. ensure_aware returns an aware datetime unchanged
# ---------------------------------------------------------------------------

def test_ensure_aware_returns_aware_unchanged() -> None:
    dt = datetime(2026, 4, 27, 14, 30, 0, tzinfo=timezone.utc)
    result = ensure_aware(dt)
    assert result is dt


# ---------------------------------------------------------------------------
# 2. ensure_aware raises ValueError for naive datetimes
# ---------------------------------------------------------------------------

def test_ensure_aware_raises_for_naive() -> None:
    naive = datetime(2026, 4, 27, 14, 30, 0)
    with pytest.raises(ValueError):
        ensure_aware(naive)


# ---------------------------------------------------------------------------
# 3. to_utc converts Sydney-aware datetime to correct UTC instant
#    2026-04-27 14:30:00 Sydney AEST (+1000) → 2026-04-27 04:30:00 UTC
# ---------------------------------------------------------------------------

def test_to_utc_converts_sydney_to_utc() -> None:
    sydney_dt = datetime(2026, 4, 27, 14, 30, 0, tzinfo=SYDNEY)
    utc_dt = to_utc(sydney_dt)
    assert utc_dt.tzinfo is not None
    assert utc_dt.utcoffset().total_seconds() == 0
    assert utc_dt == datetime(2026, 4, 27, 4, 30, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 4. to_utc raises ValueError for naive datetimes
# ---------------------------------------------------------------------------

def test_to_utc_raises_for_naive() -> None:
    naive = datetime(2026, 4, 27, 14, 30, 0)
    with pytest.raises(ValueError):
        to_utc(naive)


# ---------------------------------------------------------------------------
# 5. to_display converts UTC datetime to Sydney wall-clock time
# ---------------------------------------------------------------------------

def test_to_display_converts_utc_to_sydney() -> None:
    # 2026-04-27 04:30:00 UTC → 2026-04-27 14:30:00 Sydney AEST (+1000)
    utc_dt = datetime(2026, 4, 27, 4, 30, 0, tzinfo=timezone.utc)
    sydney_dt = to_display(utc_dt)
    assert sydney_dt.tzinfo is not None
    assert sydney_dt.year == 2026
    assert sydney_dt.month == 4
    assert sydney_dt.day == 27
    assert sydney_dt.hour == 14
    assert sydney_dt.minute == 30
    assert sydney_dt.second == 0


# ---------------------------------------------------------------------------
# 6. to_jira_started produces exact string for known fixed Sydney time
# ---------------------------------------------------------------------------

def test_to_jira_started_exact_string_sydney_input() -> None:
    dt = datetime(2026, 4, 27, 14, 30, 0, tzinfo=SYDNEY)
    result = to_jira_started(dt)
    assert result == "2026-04-27T14:30:00.000+1000"


# ---------------------------------------------------------------------------
# 7. to_jira_started includes millisecond precision (3 digits)
# ---------------------------------------------------------------------------

def test_to_jira_started_millisecond_precision() -> None:
    # 123000 microseconds = 123 milliseconds
    dt = datetime(2026, 4, 27, 14, 30, 0, 123000, tzinfo=SYDNEY)
    result = to_jira_started(dt)
    assert result == "2026-04-27T14:30:00.123+1000"


# ---------------------------------------------------------------------------
# 8. to_jira_started converts UTC input to Sydney offset
# ---------------------------------------------------------------------------

def test_to_jira_started_converts_utc_input_to_sydney() -> None:
    # 2026-04-27 04:30:00 UTC → 2026-04-27 14:30:00 Sydney AEST (+1000)
    utc_dt = datetime(2026, 4, 27, 4, 30, 0, tzinfo=timezone.utc)
    result = to_jira_started(utc_dt)
    assert result == "2026-04-27T14:30:00.000+1000"


# ---------------------------------------------------------------------------
# 9. to_jira_started rejects naive datetimes
# ---------------------------------------------------------------------------

def test_to_jira_started_rejects_naive() -> None:
    naive = datetime(2026, 4, 27, 14, 30, 0)
    with pytest.raises(ValueError):
        to_jira_started(naive)


# ---------------------------------------------------------------------------
# 10. DST forward: Oct 5 2025 02:00 Sydney clocks jump to 03:00
#     - Before jump: 01:30 → offset +1000 (AEST)
#     - After jump: 03:30 → offset +1100 (AEDT)
# ---------------------------------------------------------------------------

def test_dst_forward_before_jump() -> None:
    dt = datetime(2025, 10, 5, 1, 30, 0, tzinfo=SYDNEY)
    result = to_jira_started(dt)
    assert result.endswith("+1000"), f"Expected +1000 offset, got: {result}"


def test_dst_forward_after_jump() -> None:
    dt = datetime(2025, 10, 5, 3, 30, 0, tzinfo=SYDNEY)
    result = to_jira_started(dt)
    assert result.endswith("+1100"), f"Expected +1100 offset, got: {result}"


# ---------------------------------------------------------------------------
# 11. DST back: Apr 6 2025 03:00 Sydney clocks fall back to 02:00
#     - Before fall-back: 01:30 → offset +1100 (AEDT)
#     - After fall-back: 04:00 → offset +1000 (AEST)
# ---------------------------------------------------------------------------

def test_dst_back_before_fall_back() -> None:
    dt = datetime(2025, 4, 6, 1, 30, 0, tzinfo=SYDNEY)
    result = to_jira_started(dt)
    assert result.endswith("+1100"), f"Expected +1100 offset, got: {result}"


def test_dst_back_after_fall_back() -> None:
    dt = datetime(2025, 4, 6, 4, 0, 0, tzinfo=SYDNEY)
    result = to_jira_started(dt)
    assert result.endswith("+1000"), f"Expected +1000 offset, got: {result}"


# ---------------------------------------------------------------------------
# 12. today_in_display_tz: midnight crossing (UTC 13:00 = Sydney 00:00 next day)
# ---------------------------------------------------------------------------

def test_today_in_display_tz_midnight_crossing() -> None:
    # 2026-04-27 14:00:00 UTC = 2026-04-28 00:00:00 AEST (+1000)
    now = datetime(2026, 4, 27, 14, 0, 0, tzinfo=timezone.utc)
    result = today_in_display_tz(now=now)
    assert result == date(2026, 4, 28)


# ---------------------------------------------------------------------------
# 13. today_in_display_tz: one minute before midnight still same day
# ---------------------------------------------------------------------------

def test_today_in_display_tz_before_midnight() -> None:
    # 2026-04-27 13:59:00 UTC = 2026-04-27 23:59:00 AEST (+1000)
    now = datetime(2026, 4, 27, 13, 59, 0, tzinfo=timezone.utc)
    result = today_in_display_tz(now=now)
    assert result == date(2026, 4, 27)


# ---------------------------------------------------------------------------
# 14. today_in_display_tz raises ValueError for naive now
# ---------------------------------------------------------------------------

def test_today_in_display_tz_raises_for_naive() -> None:
    naive = datetime(2026, 4, 27, 14, 0, 0)
    with pytest.raises(ValueError):
        today_in_display_tz(now=naive)
