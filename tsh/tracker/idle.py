"""Idle detection loop for the tsh tracker.

Polls the Windows idle-input timer and updates TrackerState.idle through
three transitions:

    clear      → pending          (input idle for >= threshold)
    pending    → clear (returned) (input observed; GUI prompts user)
    pending    → autostopped      (max idle reached without return)

The actual reconciliation modal lives in the GUI / pywebview layer; this
module only signals the state change.

Win32 input source is pywin32.win32api.GetLastInputInfo + GetTickCount.
For testing, the input provider is injectable.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from tsh.config import loader
from tsh.storage import db as db_module
from tsh.storage import time_entries
from tsh.tracker.server import IdleState, TrackerState


logger = logging.getLogger(__name__)


# Default poll interval. Spec says "every 30 seconds." Tests pass smaller values.
DEFAULT_POLL_INTERVAL_SECONDS = 30.0

# Minimum wall-clock jump to count as a machine sleep event.
SLEEP_THRESHOLD_FLOOR_SECONDS = 90.0


def _win32_idle_seconds() -> float:
    """Real-platform implementation: seconds since the user's last input.

    Imports pywin32 lazily so the module is importable on non-Windows hosts
    (e.g. CI where pywin32 isn't installed). Tests inject a fake provider
    so this function is never called from the test suite.
    """
    import win32api  # type: ignore[import-not-found]

    last_input_ticks = win32api.GetLastInputInfo()
    current_ticks = win32api.GetTickCount()
    # Tick counters are 32-bit wraparound milliseconds; the modular subtraction
    # handles the wrap automatically.
    delta_ms = (current_ticks - last_input_ticks) & 0xFFFFFFFF
    return delta_ms / 1000.0


@dataclass
class IdleConfig:
    """Knobs for the idle loop. Defaults pulled from config.toml at construction."""

    threshold_minutes: int = 10
    max_idle_minutes_before_autostop: int = 240
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS

    @classmethod
    def from_config(cls) -> "IdleConfig":
        cfg = loader.load()
        time_cfg = cfg["time"]
        return cls(
            threshold_minutes=int(time_cfg["idle_threshold_minutes"]),
            max_idle_minutes_before_autostop=int(
                time_cfg["max_idle_minutes_before_autostop"]
            ),
            poll_interval_seconds=float(
                time_cfg.get("idle_poll_interval_seconds", DEFAULT_POLL_INTERVAL_SECONDS)
            ),
        )


class IdleLoop:
    """Polls the input source and mutates TrackerState.idle accordingly.

    Construct with a TrackerState and (optionally) custom config / providers.
    Run via ``await loop.run()`` from within the tracker's asyncio loop;
    cancel the task to stop.

    Public attributes (read-only from outside):
      pending_reconciliation: bool — set True on the pending→clear transition.
        The GUI/CLI consumes this to know whether to surface the modal.
        It clears when acknowledge_reconciliation() is called.
    """

    def __init__(
        self,
        state: TrackerState,
        config: IdleConfig | None = None,
        idle_seconds_provider: Callable[[], float] = _win32_idle_seconds,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.state = state
        self.config = config or IdleConfig.from_config()
        self._idle_seconds = idle_seconds_provider
        self._clock = clock
        self.pending_reconciliation = False
        self._prev_now: datetime | None = None
        self._prev_last_input_at: datetime | None = None
        self._sleep_threshold_seconds = max(
            3 * self.config.poll_interval_seconds, SLEEP_THRESHOLD_FLOOR_SECONDS
        )

    # --- public scheduling --------------------------------------------------

    async def run(self) -> None:
        """Run the loop forever until cancelled."""
        try:
            while True:
                self.tick()
                await asyncio.sleep(self.config.poll_interval_seconds)
        except asyncio.CancelledError:
            return

    # --- core state machine (synchronous; deterministic for tests) ----------

    def tick(self) -> None:
        """Single poll iteration. Reads the input clock and applies transitions."""
        try:
            idle_seconds = self._idle_seconds()
        except Exception:
            # If the Win32 call ever fails, log and skip this tick — never crash the loop.
            logger.exception("idle provider raised; skipping tick")
            return

        now = self._clock()
        last_input_at = now - timedelta(seconds=idle_seconds)

        # Sleep detection: wall-clock jumped further than poll interval allows.
        if self._prev_now is not None:
            delta = (now - self._prev_now).total_seconds()
            if delta >= self._sleep_threshold_seconds:
                self._handle_sleep()
                self._prev_now = now
                self._prev_last_input_at = last_input_at
                return
        self._prev_now = now
        self._prev_last_input_at = last_input_at

        threshold_seconds = self.config.threshold_minutes * 60
        max_seconds = self.config.max_idle_minutes_before_autostop * 60
        active = self.state.get_active()

        if self.state.idle.status == "clear":
            if active is not None and idle_seconds >= threshold_seconds:
                # Transition to pending. idle_started_at = now - idle_seconds.
                idle_started_at = now - timedelta(seconds=idle_seconds)
                # Clamp idle_started_at to active.start_at so reconcile_idle's
                # invariants hold even if the user was already idle when they
                # started the timer (rare, but possible if the tracker boots
                # while the user is afk).
                if active.start_at and idle_started_at < active.start_at:
                    idle_started_at = active.start_at
                self.state.idle = IdleState(
                    status="pending", started_at=idle_started_at
                )
            return

        if self.state.idle.status == "pending":
            if idle_seconds < threshold_seconds:
                # User came back. Clear the pending state and signal reconciliation.
                self.state.idle = IdleState(status="clear")
                self.pending_reconciliation = True
                return
            if idle_seconds >= max_seconds:
                # Autostop: close active at idle_started_at and mark autostopped.
                self._autostop(now)
                return
            # Still idle, still under max — no transition.
            return

        if self.state.idle.status == "autostopped":
            if idle_seconds < threshold_seconds:
                # User returned after autostop. Clear the autostop marker; the
                # active timer is already closed in storage. Caller surfaces a
                # gentler "you were idle since X" notification on next /status.
                self.state.idle = IdleState(status="clear")
                self.pending_reconciliation = True
            return

    # --- helpers -------------------------------------------------------------

    def _autostop(self, now: datetime) -> None:
        """Close the active timer at idle.started_at; mark idle.autostopped."""
        idle_started = self.state.idle.started_at
        if idle_started is None:
            return  # defensive — shouldn't happen
        conn = self.state._connection()
        try:
            with db_module.tx(conn):
                time_entries.end_active(conn, idle_started)
        except Exception:
            logger.exception("autostop end_active failed; idle stays pending")
            return
        self.state.refresh()
        self.state.idle = IdleState(
            status="autostopped",
            started_at=idle_started,
            autostopped_at=now,
        )

    def _handle_sleep(self) -> None:
        """Wall-clock jumped past the sleep threshold: treat as machine sleep.

        Close active entry (if any) at the PREVIOUS tick's last_input_at — the
        user's last keystroke before the gap. Flag for reconciliation with
        reason='sleep'. No active timer = no-op.
        """
        close_at = self._prev_last_input_at
        if close_at is None:
            return  # defensive — we set this every tick after the provider call

        self.state.refresh()
        active = self.state.get_active()
        if active is None:
            return

        # Clamp: end_at must not precede start_at.
        if active.start_at and close_at < active.start_at:
            close_at = active.start_at

        conn = self.state._connection()
        try:
            with db_module.tx(conn):
                closed = time_entries.end_active(conn, close_at)
                assert closed is not None
                time_entries.update(
                    conn, closed.id,  # type: ignore[arg-type]
                    pending_reconciliation=1,
                    reconciliation_reason="sleep",
                )
        except Exception:
            logger.exception("sleep-handler close failed; leaving timer active")
            return

        self.state.refresh()
        self.state.idle = IdleState(status="clear")
        self.pending_reconciliation = True
        logger.info(
            "sleep detected: closed active %s at %s; queued for reconciliation",
            active.ticket_key, close_at.isoformat(),
        )

    # --- API for the GUI/CLI to mark the prompt acknowledged ----------------

    def acknowledge_reconciliation(self) -> None:
        """Called by the GUI/CLI after surfacing the modal so we don't re-prompt."""
        self.pending_reconciliation = False
