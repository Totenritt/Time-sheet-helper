"""Tests for tsh.tracker.idle — deterministic state machine + async run loop."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tsh.tracker.idle import IdleConfig, IdleLoop
from tsh.tracker.server import IdleState, TrackerState
from tsh.storage import time_entries


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "tsh.db"


@pytest.fixture
def state(db_path: Path):
    s = TrackerState(db_path=db_path, jira_client_factory=lambda: None)
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def fixed_clock():
    """Returns (clock_fn, set_now). set_now mutates what clock_fn returns."""
    current = [datetime(2026, 4, 27, 12, 0, 0, tzinfo=timezone.utc)]

    def clock():
        return current[0]

    def set_now(dt):
        current[0] = dt

    return clock, set_now


@pytest.fixture
def fake_provider():
    """Returns (provider_fn, set_seconds). The fn returns the most recent set value."""
    state_val = [0.0]

    def provider():
        return state_val[0]

    def set_seconds(s):
        state_val[0] = s

    return provider, set_seconds


def _make_loop(state, fake_provider, fixed_clock, **kw):
    """Helper: build an IdleLoop with injectable fakes and a fast poll interval."""
    provider, _ = fake_provider
    clock, _ = fixed_clock
    cfg = IdleConfig(
        threshold_minutes=10,
        max_idle_minutes_before_autostop=240,
        poll_interval_seconds=0.001,
    )
    return IdleLoop(
        state=state,
        config=cfg,
        idle_seconds_provider=provider,
        clock=clock,
        **kw,
    )


# ---------------------------------------------------------------------------
# Test 1: clear stays clear when no active timer (idle >> threshold)
# ---------------------------------------------------------------------------


def test_clear_stays_clear_when_no_active_timer(state, fake_provider, fixed_clock):
    """No /start was called — state stays 'clear' even if idle_seconds >> threshold."""
    _, set_seconds = fake_provider
    set_seconds(700)  # 700s >> 10-min threshold (600s), but no active timer

    loop = _make_loop(state, fake_provider, fixed_clock)
    loop.tick()

    assert state.idle.status == "clear"
    assert loop.pending_reconciliation is False


# ---------------------------------------------------------------------------
# Test 2: clear → pending when idle_seconds >= threshold AND active timer exists
# ---------------------------------------------------------------------------


def test_clear_to_pending_when_idle_exceeds_threshold(state, fake_provider, fixed_clock):
    """clear → pending: idle_seconds >= threshold AND there's an active timer.

    The fixed clock is set to *after* the active timer's start_at so the
    clamping guard doesn't interfere with started_at computation.
    """
    _, set_seconds = fake_provider
    clock, set_now = fixed_clock
    threshold_seconds = 10 * 60  # 600s

    # Start a timer and capture its real start_at
    state.start("PROJ-1", "doing work")
    state.refresh()
    active = state.get_active()
    assert active is not None
    active_start = active.start_at
    assert active_start is not None

    # Advance the fixed clock so that (now - threshold_seconds) is still after
    # active_start — this prevents the clamping path from triggering.
    new_now = active_start + timedelta(seconds=threshold_seconds + 10)
    set_now(new_now)

    # Set idle to exactly the threshold
    set_seconds(threshold_seconds)
    loop = _make_loop(state, fake_provider, fixed_clock)
    loop.tick()

    assert state.idle.status == "pending"
    expected_started_at = new_now - timedelta(seconds=threshold_seconds)
    assert state.idle.started_at == expected_started_at
    assert loop.pending_reconciliation is False


# ---------------------------------------------------------------------------
# Test 3: clear stays clear when idle_seconds < threshold
# ---------------------------------------------------------------------------


def test_clear_stays_clear_when_user_is_active(state, fake_provider, fixed_clock):
    """idle_seconds < threshold: stays clear even with an active timer."""
    _, set_seconds = fake_provider
    set_seconds(60)  # 1 minute, below 10-minute threshold

    state.start("PROJ-2", "")
    loop = _make_loop(state, fake_provider, fixed_clock)
    loop.tick()

    assert state.idle.status == "clear"


# ---------------------------------------------------------------------------
# Test 4: pending → clear when input returns; pending_reconciliation = True
# ---------------------------------------------------------------------------


def test_pending_to_clear_when_input_returns(state, fake_provider, fixed_clock):
    """pending → clear when idle_seconds < threshold; pending_reconciliation becomes True."""
    _, set_seconds = fake_provider

    # Set up pending state manually
    state.start("PROJ-3", "")
    state.idle = IdleState(
        status="pending",
        started_at=datetime(2026, 4, 27, 11, 50, 0, tzinfo=timezone.utc),
    )

    # Input returned — below threshold
    set_seconds(30)

    loop = _make_loop(state, fake_provider, fixed_clock)
    loop.tick()

    assert state.idle.status == "clear"
    assert loop.pending_reconciliation is True


# ---------------------------------------------------------------------------
# Test 5: pending stays pending when idle between threshold and max
# ---------------------------------------------------------------------------


def test_pending_stays_pending_between_threshold_and_max(state, fake_provider, fixed_clock):
    """Still idle between threshold and max → stays pending, no transitions."""
    _, set_seconds = fake_provider

    state.start("PROJ-4", "")
    state.idle = IdleState(
        status="pending",
        started_at=datetime(2026, 4, 27, 11, 50, 0, tzinfo=timezone.utc),
    )

    # 2 hours idle — above threshold (10 min), below max (240 min)
    set_seconds(2 * 3600)

    loop = _make_loop(state, fake_provider, fixed_clock)
    loop.tick()

    assert state.idle.status == "pending"
    assert loop.pending_reconciliation is False


# ---------------------------------------------------------------------------
# Test 6: pending → autostopped when idle_seconds >= max
# ---------------------------------------------------------------------------


def test_pending_to_autostopped_when_max_reached(state, fake_provider, fixed_clock):
    """pending → autostopped: closes active timer at idle_started_at."""
    _, set_seconds = fake_provider
    clock, _ = fixed_clock

    # Start a timer
    state.start("PROJ-5", "")
    state.refresh()
    active_before = state.get_active()
    assert active_before is not None

    idle_started = datetime(2026, 4, 27, 8, 0, 0, tzinfo=timezone.utc)
    state.idle = IdleState(status="pending", started_at=idle_started)

    # Max idle reached: 240 minutes = 14400s
    set_seconds(240 * 60)

    loop = _make_loop(state, fake_provider, fixed_clock)
    loop.tick()

    assert state.idle.status == "autostopped"
    assert state.idle.started_at == idle_started
    assert state.idle.autostopped_at == clock()

    # Active timer must be closed in storage at idle_started_at
    conn = state._connection()
    closed = time_entries.get(conn, active_before.id)
    assert closed is not None
    assert closed.end_at is not None
    assert closed.end_at == idle_started

    # No active timer remains
    assert time_entries.get_active(conn) is None


# ---------------------------------------------------------------------------
# Test 7: autostopped → clear when input returns; pending_reconciliation = True
# ---------------------------------------------------------------------------


def test_autostopped_to_clear_when_input_returns(state, fake_provider, fixed_clock):
    """autostopped → clear when idle_seconds < threshold; pending_reconciliation becomes True."""
    _, set_seconds = fake_provider

    state.idle = IdleState(
        status="autostopped",
        started_at=datetime(2026, 4, 27, 8, 0, 0, tzinfo=timezone.utc),
        autostopped_at=datetime(2026, 4, 27, 12, 0, 0, tzinfo=timezone.utc),
    )

    # User is back — well below threshold
    set_seconds(10)

    loop = _make_loop(state, fake_provider, fixed_clock)
    loop.tick()

    assert state.idle.status == "clear"
    assert loop.pending_reconciliation is True


# ---------------------------------------------------------------------------
# Test 8: idle_started_at clamps to active.start_at if computed would be earlier
# ---------------------------------------------------------------------------


def test_idle_started_at_clamps_to_active_start_at(state, fake_provider, fixed_clock):
    """If (now - idle_seconds) < active.start_at, idle_started_at is clamped to start_at."""
    _, set_seconds = fake_provider
    clock, set_now = fixed_clock

    # Start the timer and get its start_at
    state.start("PROJ-8", "")
    state.refresh()
    active = state.get_active()
    assert active is not None
    active_start = active.start_at
    assert active_start is not None

    # Advance clock to be after the active.start_at
    new_now = active_start + timedelta(seconds=5)
    set_now(new_now)

    # Report idle for longer than the active timer has been running
    # (e.g. 700s when the timer is only 5s old)
    set_seconds(700)

    loop = _make_loop(state, fake_provider, fixed_clock)
    loop.tick()

    assert state.idle.status == "pending"
    # started_at should be clamped to active.start_at, not new_now - 700s
    assert state.idle.started_at == active_start


# ---------------------------------------------------------------------------
# Test 9: provider exception → logs and returns without crashing; state unchanged
# ---------------------------------------------------------------------------


def test_provider_exception_does_not_crash_loop(state, fixed_clock):
    """If the idle_seconds_provider raises, the tick logs and returns; state unchanged."""
    def raising_provider():
        raise RuntimeError("simulated win32 failure")

    clock, _ = fixed_clock
    cfg = IdleConfig(threshold_minutes=10, max_idle_minutes_before_autostop=240, poll_interval_seconds=0.001)
    loop = IdleLoop(state=state, config=cfg, idle_seconds_provider=raising_provider, clock=clock)

    # Should not raise
    loop.tick()

    assert state.idle.status == "clear"
    assert loop.pending_reconciliation is False


# ---------------------------------------------------------------------------
# Test 10: acknowledge_reconciliation() clears pending_reconciliation
# ---------------------------------------------------------------------------


def test_acknowledge_reconciliation_clears_flag(state, fake_provider, fixed_clock):
    """acknowledge_reconciliation() sets pending_reconciliation back to False."""
    _, set_seconds = fake_provider

    state.start("PROJ-10", "")
    state.idle = IdleState(
        status="pending",
        started_at=datetime(2026, 4, 27, 11, 50, 0, tzinfo=timezone.utc),
    )
    set_seconds(30)  # input returned

    loop = _make_loop(state, fake_provider, fixed_clock)
    loop.tick()
    assert loop.pending_reconciliation is True

    loop.acknowledge_reconciliation()
    assert loop.pending_reconciliation is False


# ---------------------------------------------------------------------------
# Test 11: async run() calls tick() then sleeps; cancellation stops cleanly
# ---------------------------------------------------------------------------


async def test_run_loop_calls_tick_and_cancels_cleanly(state, fixed_clock):
    """run() keeps calling tick() until cancelled; CancelledError doesn't propagate."""
    counter = [0]

    def provider():
        counter[0] += 1
        return 0.0  # never idle — just count ticks

    clock, _ = fixed_clock
    cfg = IdleConfig(
        threshold_minutes=10,
        max_idle_minutes_before_autostop=240,
        poll_interval_seconds=0.001,
    )
    loop = IdleLoop(state=state, config=cfg, idle_seconds_provider=provider, clock=clock)

    task = asyncio.create_task(loop.run())
    await asyncio.sleep(0.02)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert counter[0] >= 2


# ---------------------------------------------------------------------------
# Test 12: IdleConfig.from_config() reads from the loaded config
# ---------------------------------------------------------------------------


def test_idle_config_from_config(monkeypatch, tmp_path):
    """IdleConfig.from_config() reads idle_threshold_minutes and max_idle_minutes_before_autostop."""
    from tsh.config import loader

    monkeypatch.setenv("TSH_CONFIG_DIR", str(tmp_path))
    loader.set("time.idle_threshold_minutes", 5)
    loader.set("time.max_idle_minutes_before_autostop", 60)

    cfg = IdleConfig.from_config()
    assert cfg.threshold_minutes == 5
    assert cfg.max_idle_minutes_before_autostop == 60
