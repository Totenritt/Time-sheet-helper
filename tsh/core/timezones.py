"""Single source of truth for Jira-compatible datetime handling.

All functions that interact with Jira's REST API should use this module to
avoid the timezone mismatch bugs that broke previous automation attempts.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

DEFAULT_DISPLAY_TZ = "Australia/Sydney"


def ensure_aware(dt: datetime) -> datetime:
    """Return dt unchanged if timezone-aware; raise ValueError if naive.

    Used as a guard at boundaries where naive datetimes would cause silent bugs.
    """
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(
            f"Expected a timezone-aware datetime, got naive: {dt!r}"
        )
    return dt


def to_utc(dt: datetime) -> datetime:
    """Convert dt to UTC. Raises ValueError if dt is naive."""
    ensure_aware(dt)
    return dt.astimezone(timezone.utc)


def to_display(dt: datetime, tz: str = DEFAULT_DISPLAY_TZ) -> datetime:
    """Convert dt to the display timezone. Raises ValueError if dt is naive."""
    ensure_aware(dt)
    return dt.astimezone(ZoneInfo(tz))


def to_jira_started(dt: datetime, tz: str = DEFAULT_DISPLAY_TZ) -> str:
    """Format dt as the ISO 8601 string Jira's worklog API expects.

    Format: 'YYYY-MM-DDTHH:MM:SS.fff±HHMM' (millisecond precision, explicit
    offset, NO colon in the offset). Examples:
        '2026-04-27T14:30:00.000+1100'
        '2026-07-15T09:00:00.123+1000'

    The dt is converted into the display timezone before formatting, so the
    'started' value Jira records matches what the user saw in their UI.
    Raises ValueError if dt is naive.
    """
    ensure_aware(dt)
    local_dt = dt.astimezone(ZoneInfo(tz))
    milliseconds = local_dt.microsecond // 1000
    # strftime("%z") returns "+HHMM" with no colon — exactly what Jira expects
    offset = local_dt.strftime("%z")
    return (
        f"{local_dt.strftime('%Y-%m-%dT%H:%M:%S')}"
        f".{milliseconds:03d}"
        f"{offset}"
    )


def today_in_display_tz(
    now: datetime | None = None, tz: str = DEFAULT_DISPLAY_TZ
) -> date:
    """Return the calendar date 'today' in the display timezone.

    If now is provided, it is treated as the current instant (must be aware);
    otherwise datetime.now(timezone.utc) is used.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    else:
        ensure_aware(now)
    return now.astimezone(ZoneInfo(tz)).date()
