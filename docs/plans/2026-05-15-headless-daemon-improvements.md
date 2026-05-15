# Headless Daemon Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden the headless tracker daemon to survive machine sleep, auto-switch on git branch checkout, and run as a true Windows background app — implementing the design at `docs/superpowers/specs/2026-05-15-headless-daemon-improvements-design.md`.

**Architecture:** Three additive slices on top of the Phase-1-through-6 daemon: (a) a storage migration + sleep-aware idle loop + startup recovery + `tsh reconcile`, (b) a `tsh switch --from-branch` primitive driven by a per-repo `post-checkout` hook installed via `tsh hook install`, (c) a `tsh tray --detach` mode that re-spawns through `pythonw.exe` plus a `tsh autostart` command that writes a Startup-folder shortcut.

**Tech Stack:** Python 3.11+, click, httpx, SQLite (stdlib), FastAPI, asyncio, pywin32 (`win32com.client` for shortcut creation), pytest + pytest-mock. No new top-level dependencies — `pywebview` will be removed during this work since the GUI is dropped.

---

## Pre-flight

- [ ] **Confirm starting state**

  Run from project root:

  ```powershell
  uv sync --all-extras
  uv run pytest -q
  ```

  Expected: 311 tests pass. If not, stop and investigate before doing any work.

- [ ] **Drop the now-unused pywebview dependency**

  Edit `pyproject.toml` — remove the `pywebview` line under `[project.dependencies]` (if present). Then:

  ```powershell
  uv lock
  uv sync --all-extras
  uv run pytest -q
  ```

  Expected: 311 tests still pass. Commit:

  ```bash
  git add pyproject.toml uv.lock
  git commit -m "chore(deps): drop pywebview (GUI dropped per 2026-05-15 spec)"
  ```

  If `pywebview` was never in `pyproject.toml`, skip the commit silently.

---

## Phase A — Storage: reconciliation columns + repo functions

### Task 20.0: Migration 002 — pending_reconciliation columns

**Files:**
- Create: `tsh/storage/migrations/002_reconciliation_reason.sql`
- Modify: `tsh/storage/db.py` (MIGRATIONS list — add the new entry)
- Test: `tests/storage/test_db.py`

- [ ] **Step 1: Write the failing test**

  Append to `tests/storage/test_db.py`:

  ```python
  def test_migration_002_adds_reconciliation_columns(tmp_path: Path) -> None:
      conn = db_module.connect(tmp_path / "tsh.db")
      cols = {row["name"] for row in conn.execute("PRAGMA table_info(time_entries)")}
      assert "reconciliation_reason" in cols
      assert "pending_reconciliation" in cols
      version = conn.execute("PRAGMA user_version").fetchone()[0]
      assert version >= 2


  def test_migration_002_pending_reconciliation_defaults_zero(tmp_path: Path) -> None:
      conn = db_module.connect(tmp_path / "tsh.db")
      now = datetime.now(timezone.utc)
      conn.execute(
          """INSERT INTO time_entries
             (ticket_key, start_at, kind, created_at, updated_at)
             VALUES ('X-1', ?, 'work', ?, ?)""",
          (now, now, now),
      )
      row = conn.execute(
          "SELECT pending_reconciliation, reconciliation_reason FROM time_entries"
      ).fetchone()
      assert row["pending_reconciliation"] == 0
      assert row["reconciliation_reason"] is None
  ```

  (Make sure `from datetime import datetime, timezone` is imported at the top of the test file.)

- [ ] **Step 2: Run tests to verify they fail**

  ```powershell
  uv run pytest tests/storage/test_db.py::test_migration_002_adds_reconciliation_columns tests/storage/test_db.py::test_migration_002_pending_reconciliation_defaults_zero -v
  ```

  Expected: FAIL (no migration 002, columns don't exist).

- [ ] **Step 3: Write the migration SQL**

  Create `tsh/storage/migrations/002_reconciliation_reason.sql`:

  ```sql
  ALTER TABLE time_entries ADD COLUMN reconciliation_reason TEXT;
  ALTER TABLE time_entries ADD COLUMN pending_reconciliation INTEGER NOT NULL DEFAULT 0;

  CREATE INDEX time_entries_pending_reconciliation
      ON time_entries(pending_reconciliation)
      WHERE pending_reconciliation = 1;

  PRAGMA user_version = 2;
  ```

- [ ] **Step 4: Register the migration**

  In `tsh/storage/db.py`, change `MIGRATIONS` to:

  ```python
  MIGRATIONS: list[tuple[int, str]] = [
      (1, "001_initial.sql"),
      (2, "002_reconciliation_reason.sql"),
  ]
  ```

- [ ] **Step 5: Run tests to verify they pass**

  ```powershell
  uv run pytest tests/storage/test_db.py -v
  ```

  Expected: all green, including the two new tests.

- [ ] **Step 6: Commit**

  ```bash
  git add tsh/storage/migrations/002_reconciliation_reason.sql tsh/storage/db.py tests/storage/test_db.py
  git commit -m "feat(storage): migration 002 — reconciliation_reason + pending_reconciliation columns"
  ```

---

### Task 20.1: Repository functions for the reconciliation queue

**Files:**
- Modify: `tsh/storage/time_entries.py`
- Test: `tests/storage/test_time_entries.py`

- [ ] **Step 1: Write the failing tests**

  Append to `tests/storage/test_time_entries.py`:

  ```python
  def test_count_pending_reconciliation_empty(conn) -> None:
      assert time_entries.count_pending_reconciliation(conn) == 0


  def test_count_pending_reconciliation_counts_flagged_rows(conn) -> None:
      now = datetime.now(timezone.utc)
      for ticket in ("X-1", "X-2", "X-3"):
          conn.execute(
              """INSERT INTO time_entries
                 (ticket_key, start_at, end_at, kind, created_at, updated_at,
                  pending_reconciliation, reconciliation_reason)
                 VALUES (?, ?, ?, 'work', ?, ?, ?, ?)""",
              (ticket, now, now, now, now,
               1 if ticket != "X-3" else 0,
               "sleep" if ticket != "X-3" else None),
          )
      assert time_entries.count_pending_reconciliation(conn) == 2


  def test_list_pending_reconciliation_returns_newest_first(conn) -> None:
      base = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)
      for i, ticket in enumerate(("OLD-1", "MID-2", "NEW-3")):
          conn.execute(
              """INSERT INTO time_entries
                 (ticket_key, start_at, end_at, kind, created_at, updated_at,
                  pending_reconciliation, reconciliation_reason)
                 VALUES (?, ?, ?, 'work', ?, ?, 1, 'sleep')""",
              (ticket, base + timedelta(hours=i), base + timedelta(hours=i, minutes=30),
               base, base),
          )
      result = time_entries.list_pending_reconciliation(conn)
      assert [e.ticket_key for e in result] == ["NEW-3", "MID-2", "OLD-1"]


  def test_update_allows_reconciliation_columns(conn) -> None:
      now = datetime.now(timezone.utc)
      cursor = conn.execute(
          """INSERT INTO time_entries (ticket_key, start_at, kind, created_at, updated_at)
             VALUES ('X-1', ?, 'work', ?, ?)""",
          (now, now, now),
      )
      eid = cursor.lastrowid
      updated = time_entries.update(
          conn, eid, pending_reconciliation=1, reconciliation_reason="sleep"
      )
      # We get the updated row back; verify via raw SQL since TimeEntry doesn't expose
      # the new fields yet (we'll fix that in Task 20.3 if needed).
      row = conn.execute(
          "SELECT pending_reconciliation, reconciliation_reason FROM time_entries WHERE id = ?",
          (eid,),
      ).fetchone()
      assert row["pending_reconciliation"] == 1
      assert row["reconciliation_reason"] == "sleep"
  ```

- [ ] **Step 2: Run tests to verify they fail**

  ```powershell
  uv run pytest tests/storage/test_time_entries.py::test_count_pending_reconciliation_empty tests/storage/test_time_entries.py::test_count_pending_reconciliation_counts_flagged_rows tests/storage/test_time_entries.py::test_list_pending_reconciliation_returns_newest_first tests/storage/test_time_entries.py::test_update_allows_reconciliation_columns -v
  ```

  Expected: AttributeError for `count_pending_reconciliation` / `list_pending_reconciliation`, ValueError ("Unknown field") for the update test.

- [ ] **Step 3: Extend the `update()` allowlist**

  In `tsh/storage/time_entries.py`, change `_MUTABLE_FIELDS`:

  ```python
  _MUTABLE_FIELDS = frozenset(
      {
          "ticket_key", "start_at", "end_at", "note", "kind",
          "jira_worklog_id", "pushed_at",
          "pending_reconciliation", "reconciliation_reason",
      }
  )
  ```

- [ ] **Step 4: Add the new repository functions**

  Append to `tsh/storage/time_entries.py`:

  ```python
  def count_pending_reconciliation(conn: sqlite3.Connection) -> int:
      """Number of entries currently flagged pending_reconciliation = 1.

      Cheap COUNT(*) hitting the partial index. Used by the warning banner
      on `tsh status` / `tsh start` / `tsh switch` so we don't hydrate full
      rows in the common no-pending case.
      """
      row = conn.execute(
          "SELECT COUNT(*) AS n FROM time_entries WHERE pending_reconciliation = 1"
      ).fetchone()
      return int(row["n"])


  def list_pending_reconciliation(conn: sqlite3.Connection) -> list[TimeEntry]:
      """Return entries with pending_reconciliation = 1, newest start_at first.

      Used by `tsh reconcile` to walk pending cases. Caller resolves each via
      core.reconcile.reconcile_idle, writes the resulting entries, then clears
      the flag with `update(..., pending_reconciliation=0, reconciliation_reason=None)`.
      """
      rows = conn.execute(
          """
          SELECT * FROM time_entries
          WHERE pending_reconciliation = 1
          ORDER BY start_at DESC
          """
      ).fetchall()
      return [_row_to_entry(row) for row in rows]
  ```

- [ ] **Step 5: Run tests to verify they pass**

  ```powershell
  uv run pytest tests/storage/test_time_entries.py -v
  ```

  Expected: all green, including the four new tests.

- [ ] **Step 6: Commit**

  ```bash
  git add tsh/storage/time_entries.py tests/storage/test_time_entries.py
  git commit -m "feat(storage): list/count_pending_reconciliation + update allowlist for new columns"
  ```

---

## Phase B — Idle loop: sleep detection

### Task 20.2: Detect clock-jump and close active timer at last input

**Files:**
- Modify: `tsh/tracker/idle.py`
- Test: `tests/tracker/test_idle.py`

- [ ] **Step 1: Write the failing test — sleep with active timer**

  Append to `tests/tracker/test_idle.py`. First, a helper that inserts an active entry:

  ```python
  def _insert_active(state: TrackerState, ticket: str, start_at: datetime) -> int:
      """Insert an active (end_at NULL) work entry; return id."""
      conn = state._connection()
      from tsh.core.models import TimeEntry  # local import to avoid cycle in test header
      entry = TimeEntry(
          id=None, ticket_key=ticket, start_at=start_at, end_at=None,
          kind="work", note="", jira_worklog_id=None, pushed_at=None,
          created_at=start_at, updated_at=start_at,
      )
      eid = time_entries.insert(conn, entry)
      conn.commit()
      state.refresh()
      return eid
  ```

  Then the test:

  ```python
  def test_sleep_detected_with_active_timer_closes_at_last_input(
      state: TrackerState, fake_provider, fixed_clock
  ) -> None:
      provider, set_idle = fake_provider
      clock, set_now = fixed_clock

      start_at = datetime(2026, 5, 14, 21, 0, tzinfo=timezone.utc)
      _insert_active(state, "SFXS-1234", start_at)

      # Establish a baseline tick: now=22:00, idle=10s (user active 10s ago).
      set_now(datetime(2026, 5, 14, 22, 0, 0, tzinfo=timezone.utc))
      set_idle(10.0)
      loop = _make_loop(state, fake_provider, fixed_clock)
      loop.tick()
      assert state.idle.status == "clear"

      # Now simulate sleep: 8 hours later, idle reading is "fresh" (sleep ate the gap).
      set_now(datetime(2026, 5, 15, 6, 0, 0, tzinfo=timezone.utc))
      set_idle(5.0)
      loop.tick()

      # Active entry should be closed at the last-input time captured BEFORE the gap:
      # baseline now (22:00:00) minus idle_seconds (10s) = 21:59:50.
      conn = state._connection()
      active = time_entries.get_active(conn)
      assert active is None, "active entry should have been closed"

      rows = conn.execute(
          "SELECT ticket_key, end_at, pending_reconciliation, reconciliation_reason "
          "FROM time_entries WHERE ticket_key = 'SFXS-1234'"
      ).fetchall()
      assert len(rows) == 1
      assert rows[0]["pending_reconciliation"] == 1
      assert rows[0]["reconciliation_reason"] == "sleep"
      assert rows[0]["end_at"] == datetime(2026, 5, 14, 21, 59, 50, tzinfo=timezone.utc)
  ```

- [ ] **Step 2: Write the failing test — normal tick is unaffected**

  ```python
  def test_normal_tick_does_not_trigger_sleep_handler(
      state: TrackerState, fake_provider, fixed_clock
  ) -> None:
      provider, set_idle = fake_provider
      clock, set_now = fixed_clock
      start_at = datetime(2026, 5, 15, 9, 0, tzinfo=timezone.utc)
      _insert_active(state, "SFXS-7", start_at)

      set_now(datetime(2026, 5, 15, 9, 5, 0, tzinfo=timezone.utc))
      set_idle(5.0)
      loop = _make_loop(state, fake_provider, fixed_clock)
      loop.tick()

      # Advance 30s — normal tick, no sleep handling.
      set_now(datetime(2026, 5, 15, 9, 5, 30, tzinfo=timezone.utc))
      set_idle(35.0)
      loop.tick()

      conn = state._connection()
      assert time_entries.get_active(conn) is not None
      assert time_entries.count_pending_reconciliation(conn) == 0
  ```

- [ ] **Step 3: Write the failing test — sleep with no active timer**

  ```python
  def test_sleep_with_no_active_timer_is_noop(
      state: TrackerState, fake_provider, fixed_clock
  ) -> None:
      provider, set_idle = fake_provider
      clock, set_now = fixed_clock

      set_now(datetime(2026, 5, 14, 22, 0, 0, tzinfo=timezone.utc))
      set_idle(5.0)
      loop = _make_loop(state, fake_provider, fixed_clock)
      loop.tick()

      set_now(datetime(2026, 5, 15, 6, 0, 0, tzinfo=timezone.utc))
      set_idle(5.0)
      loop.tick()

      conn = state._connection()
      assert time_entries.count_pending_reconciliation(conn) == 0
  ```

- [ ] **Step 4: Write the failing test — already-pending entry gets reason='sleep'**

  ```python
  def test_sleep_during_idle_pending_updates_reason_to_sleep(
      state: TrackerState, fake_provider, fixed_clock
  ) -> None:
      provider, set_idle = fake_provider
      clock, set_now = fixed_clock
      start_at = datetime(2026, 5, 14, 12, 0, tzinfo=timezone.utc)
      _insert_active(state, "SFXS-99", start_at)

      # Tick 1: cross idle threshold → pending.
      set_now(datetime(2026, 5, 14, 12, 15, 0, tzinfo=timezone.utc))
      set_idle(11 * 60.0)  # idle 11 minutes (threshold is 10)
      loop = _make_loop(state, fake_provider, fixed_clock)
      loop.tick()
      assert state.idle.status == "pending"

      # Tick 2: sleep. The active entry should be closed at last_input
      # (12:15:00 - 11min = 12:04:00) with reason 'sleep', NOT 'idle'.
      set_now(datetime(2026, 5, 14, 20, 15, 0, tzinfo=timezone.utc))
      set_idle(5.0)
      loop.tick()

      conn = state._connection()
      row = conn.execute(
          "SELECT end_at, reconciliation_reason FROM time_entries WHERE ticket_key = 'SFXS-99'"
      ).fetchone()
      assert row["reconciliation_reason"] == "sleep"
      assert row["end_at"] == datetime(2026, 5, 14, 12, 4, 0, tzinfo=timezone.utc)
  ```

- [ ] **Step 5: Run tests to verify they fail**

  ```powershell
  uv run pytest tests/tracker/test_idle.py -v -k "sleep"
  ```

  Expected: all four FAIL.

- [ ] **Step 6: Implement sleep detection in `IdleLoop`**

  Edit `tsh/tracker/idle.py`. First add a helper near the top of the module (after the constants):

  ```python
  SLEEP_THRESHOLD_FLOOR_SECONDS = 90.0  # min gap to call "sleep" even if poll is tiny
  ```

  In `IdleLoop.__init__`, add two trackers and a sleep_threshold:

  ```python
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
          # Wall-clock + last-input bookkeeping for sleep detection.
          self._prev_now: datetime | None = None
          self._prev_last_input_at: datetime | None = None
          self._sleep_threshold_seconds = max(
              3 * self.config.poll_interval_seconds, SLEEP_THRESHOLD_FLOOR_SECONDS
          )
  ```

  Rewrite the top of `tick()` to do sleep detection before the existing state machine. Replace the current `tick()` body's beginning (the part before `if self.state.idle.status == "clear":`) with:

  ```python
      def tick(self) -> None:
          """Single poll iteration. Reads the input clock and applies transitions."""
          try:
              idle_seconds = self._idle_seconds()
          except Exception:
              logger.exception("idle provider raised; skipping tick")
              return

          now = self._clock()
          last_input_at = now - timedelta(seconds=idle_seconds)

          # ---- Sleep detection ----
          if self._prev_now is not None:
              delta = (now - self._prev_now).total_seconds()
              if delta >= self._sleep_threshold_seconds:
                  self._handle_sleep(now)
                  # Update bookkeeping AFTER the sleep handler so the next tick
                  # uses the post-recovery baseline.
                  self._prev_now = now
                  self._prev_last_input_at = last_input_at
                  return
          self._prev_now = now
          self._prev_last_input_at = last_input_at

          threshold_seconds = self.config.threshold_minutes * 60
          max_seconds = self.config.max_idle_minutes_before_autostop * 60
          active = self.state.get_active()

          if self.state.idle.status == "clear":
              # ... (unchanged)
  ```

  Keep the existing `if self.state.idle.status == "clear"`, `"pending"`, `"autostopped"` blocks below as they are.

  Then add the sleep handler as a new method on `IdleLoop`:

  ```python
      def _handle_sleep(self, now: datetime) -> None:
          """Wall-clock jumped past the sleep threshold: treat as machine sleep.

          If there's an active timer, close it at the previous tick's
          last_input_at (the user's last keystroke BEFORE the gap) and flag
          for reconciliation with reason='sleep'. No active timer = no-op.
          """
          close_at = self._prev_last_input_at
          if close_at is None:
              # Shouldn't happen — we set _prev_last_input_at every tick we
              # got past the provider call. Defensive only.
              return

          conn = self.state._connection()
          self.state.refresh()
          active = self.state.get_active()
          if active is None:
              return

          # Clamp: end_at must not precede start_at.
          if active.start_at and close_at < active.start_at:
              close_at = active.start_at

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
          logger.info(
              "sleep detected: closed active %s at %s; queued for reconciliation",
              active.ticket_key, close_at.isoformat(),
          )
  ```

- [ ] **Step 7: Run tests to verify they pass**

  ```powershell
  uv run pytest tests/tracker/test_idle.py -v
  ```

  Expected: every test green, including the existing idle tests (no regressions) and the four new sleep tests.

- [ ] **Step 8: Commit**

  ```bash
  git add tsh/tracker/idle.py tests/tracker/test_idle.py
  git commit -m "feat(tracker): sleep detection — close active at last-input on clock jump"
  ```

---

## Phase C — Runner: stale-entry startup recovery

### Task 20.3: Recover orphaned active entry on tracker startup

**Files:**
- Modify: `tsh/tracker/runner.py`
- Modify: `tsh/config/loader.py` (add `time.stale_active_threshold_minutes` default)
- Test: `tests/tracker/test_runner.py` (create if it doesn't exist)

- [ ] **Step 1: Add config default**

  Edit `tsh/config/loader.py` — extend the `time` block in `DEFAULTS`:

  ```python
      "time": {
          "display_timezone": "Australia/Sydney",
          "rounding": "nearest",
          "rounding_minutes": 15,
          "idle_threshold_minutes": 10,
          "max_idle_minutes_before_autostop": 240,
          "stale_active_threshold_minutes": 120,
      },
  ```

- [ ] **Step 2: Write the failing tests**

  Create `tests/tracker/test_runner.py` if missing; otherwise append:

  ```python
  """Tests for tsh.tracker.runner — covers startup recovery."""

  from __future__ import annotations
  from datetime import datetime, timedelta, timezone
  from pathlib import Path

  import pytest

  from tsh.tracker import runner
  from tsh.tracker.server import TrackerState
  from tsh.storage import db as db_module
  from tsh.storage import time_entries
  from tsh.core.models import TimeEntry


  @pytest.fixture
  def state(tmp_path: Path):
      s = TrackerState(db_path=tmp_path / "tsh.db", jira_client_factory=lambda: None)
      try:
          yield s
      finally:
          s.close()


  def _insert_active(state: TrackerState, ticket: str, start_at: datetime) -> int:
      conn = state._connection()
      eid = time_entries.insert(
          conn,
          TimeEntry(
              id=None, ticket_key=ticket, start_at=start_at, end_at=None,
              kind="work", note="", jira_worklog_id=None, pushed_at=None,
              created_at=start_at, updated_at=start_at,
          ),
      )
      conn.commit()
      return eid


  def test_recovery_closes_18h_old_active_entry(state: TrackerState) -> None:
      now = datetime.now(timezone.utc)
      start_at = now - timedelta(hours=18)
      _insert_active(state, "SFXS-old", start_at)

      runner.recover_stale_active(
          state,
          stale_threshold_minutes=120,
          idle_threshold_minutes=10,
          now=now,
      )

      conn = state._connection()
      active = time_entries.get_active(conn)
      assert active is None
      row = conn.execute(
          "SELECT end_at, pending_reconciliation, reconciliation_reason "
          "FROM time_entries WHERE ticket_key = 'SFXS-old'"
      ).fetchone()
      assert row["pending_reconciliation"] == 1
      assert row["reconciliation_reason"] == "orphaned_active"
      # end_at = start_at + 10 minutes
      assert row["end_at"] == start_at + timedelta(minutes=10)


  def test_recovery_leaves_30m_old_entry_alone(state: TrackerState) -> None:
      now = datetime.now(timezone.utc)
      start_at = now - timedelta(minutes=30)
      _insert_active(state, "SFXS-fresh", start_at)

      runner.recover_stale_active(
          state,
          stale_threshold_minutes=120,
          idle_threshold_minutes=10,
          now=now,
      )

      conn = state._connection()
      active = time_entries.get_active(conn)
      assert active is not None
      assert active.ticket_key == "SFXS-fresh"
      assert time_entries.count_pending_reconciliation(conn) == 0


  def test_recovery_noop_when_no_active_entry(state: TrackerState) -> None:
      # No active entry at all; nothing should change.
      runner.recover_stale_active(
          state,
          stale_threshold_minutes=120,
          idle_threshold_minutes=10,
          now=datetime.now(timezone.utc),
      )
      conn = state._connection()
      assert time_entries.get_active(conn) is None
      assert time_entries.count_pending_reconciliation(conn) == 0
  ```

- [ ] **Step 3: Run tests to verify they fail**

  ```powershell
  uv run pytest tests/tracker/test_runner.py -v
  ```

  Expected: AttributeError — `runner.recover_stale_active` doesn't exist.

- [ ] **Step 4: Implement `recover_stale_active`**

  Add to `tsh/tracker/runner.py`:

  ```python
  from datetime import timedelta as _timedelta
  from datetime import datetime as _datetime
  from datetime import timezone as _tz
  from tsh.storage import db as db_module
  from tsh.storage import time_entries


  def recover_stale_active(
      state: TrackerState,
      *,
      stale_threshold_minutes: int,
      idle_threshold_minutes: int,
      now: _datetime | None = None,
  ) -> None:
      """Close an orphaned active entry left from a previous daemon session.

      Called by run() BEFORE the HTTP port opens. If an active entry exists
      and is older than stale_threshold_minutes, close it at
      start_at + idle_threshold_minutes (best-effort: that's when idle would
      have first triggered had the daemon been running) and flag it
      pending_reconciliation with reason='orphaned_active'.
      """
      now = now or _datetime.now(_tz.utc)
      conn = state._connection()
      active = time_entries.get_active(conn)
      if active is None or active.start_at is None:
          return
      age = (now - active.start_at).total_seconds()
      if age < stale_threshold_minutes * 60:
          return

      close_at = active.start_at + _timedelta(minutes=idle_threshold_minutes)
      if close_at > now:
          # Safety net (shouldn't happen given the age check above).
          close_at = now
      with db_module.tx(conn):
          closed = time_entries.end_active(conn, close_at)
          assert closed is not None
          time_entries.update(
              conn, closed.id,  # type: ignore[arg-type]
              pending_reconciliation=1,
              reconciliation_reason="orphaned_active",
          )
      state.refresh()
      logger.info(
          "recovered orphaned active %s (started %.1fh ago) — closed at %s; queued for reconciliation",
          active.ticket_key, age / 3600, close_at.isoformat(),
      )
  ```

- [ ] **Step 5: Wire recovery into `run()`**

  In `tsh/tracker/runner.py`, edit `run()` so that just after `state = TrackerState(...)` and before `asyncio.run(...)`:

  ```python
  def run() -> None:
      """Synchronous entry point. Reads config, builds state, dispatches the async loop."""
      cfg = loader.load()
      http_port = int(cfg["app"]["http_port"])
      state = TrackerState(
          db_path=_db_path(),
          jira_client_factory=_build_jira_client_factory(),
      )
      # Recover any orphaned active entry BEFORE opening the HTTP port.
      try:
          recover_stale_active(
              state,
              stale_threshold_minutes=int(cfg["time"]["stale_active_threshold_minutes"]),
              idle_threshold_minutes=int(cfg["time"]["idle_threshold_minutes"]),
          )
      except Exception:
          logger.exception("startup recovery failed; continuing anyway")
      try:
          asyncio.run(_run_async(state, http_port))
      finally:
          state.close()
  ```

- [ ] **Step 6: Run tests to verify they pass**

  ```powershell
  uv run pytest tests/tracker/test_runner.py -v
  ```

  Expected: green.

- [ ] **Step 7: Run the full suite for regression check**

  ```powershell
  uv run pytest -q
  ```

  Expected: still green; everything else unaffected.

- [ ] **Step 8: Commit**

  ```bash
  git add tsh/tracker/runner.py tsh/config/loader.py tests/tracker/test_runner.py
  git commit -m "feat(tracker): startup recovery — close orphaned active timer on daemon start"
  ```

---

## Phase D — CLI reconciliation + warning banners + endpoint rewire

### Task 20.4: `tsh reconcile` (interactive + JSON + single-id)

**Files:**
- Create: `tsh/cli/reconcile.py`
- Modify: `tsh/cli/main.py`
- Create: `tests/cli/test_reconcile.py`

- [ ] **Step 1: Write the failing tests — list + JSON mode**

  Create `tests/cli/test_reconcile.py`:

  ```python
  """Tests for tsh.cli.reconcile."""
  from __future__ import annotations
  import json
  from datetime import datetime, timedelta, timezone
  from pathlib import Path

  import pytest
  from click.testing import CliRunner

  from tsh.cli.main import cli
  from tsh.config import loader
  from tsh.storage import db as db_module
  from tsh.storage import time_entries
  from tsh.core.models import TimeEntry


  @pytest.fixture
  def tsh_home(tmp_path: Path, monkeypatch):
      monkeypatch.setenv("TSH_CONFIG_DIR", str(tmp_path))
      return tmp_path


  def _insert_pending(reason: str, ticket: str = "SFXS-1", *,
                      start_at: datetime, end_at: datetime) -> int:
      conn = db_module.connect(loader.config_dir() / "tsh.db")
      eid = time_entries.insert(
          conn,
          TimeEntry(
              id=None, ticket_key=ticket, start_at=start_at, end_at=end_at,
              kind="work", note="", jira_worklog_id=None, pushed_at=None,
              created_at=start_at, updated_at=start_at,
          ),
      )
      time_entries.update(conn, eid, pending_reconciliation=1, reconciliation_reason=reason)
      conn.commit()
      conn.close()
      return eid


  def test_reconcile_json_lists_pending_newest_first(tsh_home: Path) -> None:
      base = datetime(2026, 5, 15, 12, 0, tzinfo=timezone.utc)
      _insert_pending("sleep", "OLD-1",
                      start_at=base, end_at=base + timedelta(minutes=10))
      _insert_pending("orphaned_active", "NEW-2",
                      start_at=base + timedelta(hours=2),
                      end_at=base + timedelta(hours=2, minutes=10))
      result = CliRunner().invoke(cli, ["reconcile", "--json"])
      assert result.exit_code == 0
      payload = json.loads(result.output)
      assert [e["ticket_key"] for e in payload] == ["NEW-2", "OLD-1"]
      assert payload[0]["reconciliation_reason"] == "orphaned_active"


  def test_reconcile_json_empty_returns_empty_list(tsh_home: Path) -> None:
      result = CliRunner().invoke(cli, ["reconcile", "--json"])
      assert result.exit_code == 0
      assert json.loads(result.output) == []
  ```

- [ ] **Step 2: Write the failing tests — interactive choices**

  Append to the same file:

  ```python
  def test_reconcile_interactive_not_work_clears_flag_and_inserts_gap(tsh_home: Path) -> None:
      base = datetime(2026, 5, 14, 22, 0, tzinfo=timezone.utc)
      end = datetime(2026, 5, 14, 22, 42, tzinfo=timezone.utc)
      eid = _insert_pending("sleep", "SFXS-1234", start_at=base, end_at=end)

      result = CliRunner().invoke(cli, ["reconcile", str(eid)], input="n\n")
      assert result.exit_code == 0, result.output

      conn = db_module.connect(loader.config_dir() / "tsh.db")
      try:
          assert time_entries.count_pending_reconciliation(conn) == 0
          original = time_entries.get(conn, eid)
          assert original is not None
          # 'not_work' branch: original stays closed at end; one new not_work entry
          # spans end → returned_at; no new 'work' entry.
          rows = conn.execute("SELECT * FROM time_entries ORDER BY id").fetchall()
          assert len(rows) == 2
          gap = rows[1]
          assert gap["kind"] == "not_work"
          assert gap["start_at"] == end
      finally:
          conn.close()


  def test_reconcile_interactive_same_extends_through_gap(tsh_home: Path) -> None:
      base = datetime(2026, 5, 14, 22, 0, tzinfo=timezone.utc)
      end = datetime(2026, 5, 14, 22, 42, tzinfo=timezone.utc)
      eid = _insert_pending("sleep", "SFXS-1234", start_at=base, end_at=end)

      result = CliRunner().invoke(cli, ["reconcile", str(eid)], input="s\n")
      assert result.exit_code == 0, result.output

      conn = db_module.connect(loader.config_dir() / "tsh.db")
      try:
          # 'same' branch: original's end_at extends to returned_at (now);
          # no extra entry.
          rows = conn.execute("SELECT * FROM time_entries").fetchall()
          assert len(rows) == 1
          assert rows[0]["pending_reconciliation"] == 0
          assert rows[0]["reconciliation_reason"] is None
      finally:
          conn.close()


  def test_reconcile_skip_leaves_flag_set(tsh_home: Path) -> None:
      base = datetime(2026, 5, 14, 22, 0, tzinfo=timezone.utc)
      end = datetime(2026, 5, 14, 22, 42, tzinfo=timezone.utc)
      eid = _insert_pending("sleep", "SFXS-1234", start_at=base, end_at=end)
      result = CliRunner().invoke(cli, ["reconcile", str(eid)], input="k\n")
      assert result.exit_code == 0
      conn = db_module.connect(loader.config_dir() / "tsh.db")
      try:
          assert time_entries.count_pending_reconciliation(conn) == 1
      finally:
          conn.close()
  ```

- [ ] **Step 3: Run tests to verify they fail**

  ```powershell
  uv run pytest tests/cli/test_reconcile.py -v
  ```

  Expected: FAIL — `reconcile` subcommand doesn't exist.

- [ ] **Step 4: Implement `tsh/cli/reconcile.py`**

  Create `tsh/cli/reconcile.py`:

  ```python
  """tsh reconcile — interactive resolution of pending reconciliation entries."""
  from __future__ import annotations

  import json as json_module
  import sys
  from datetime import datetime, timezone
  from typing import Optional

  import click

  from tsh.core.models import TimeEntry
  from tsh.storage import db as db_module
  from tsh.storage import time_entries
  from tsh.config import loader


  def _open_conn():
      return db_module.connect(loader.config_dir() / "tsh.db")


  def _entry_to_dict(e: TimeEntry, reconciliation_reason: str | None) -> dict:
      return {
          "id": e.id,
          "ticket_key": e.ticket_key,
          "start_at": e.start_at.isoformat() if e.start_at else None,
          "end_at": e.end_at.isoformat() if e.end_at else None,
          "kind": e.kind,
          "note": e.note,
          "reconciliation_reason": reconciliation_reason,
      }


  def _list_pending_with_reason(conn):
      """Return list of (TimeEntry, reason_str) tuples, newest first."""
      rows = conn.execute(
          """
          SELECT * FROM time_entries
          WHERE pending_reconciliation = 1
          ORDER BY start_at DESC
          """
      ).fetchall()
      out = []
      for row in rows:
          e = time_entries.get(conn, row["id"])
          assert e is not None
          out.append((e, row["reconciliation_reason"]))
      return out


  def _prompt_choice(entry: TimeEntry, reason: str | None) -> str:
      """Show the entry summary and return 's'/'d'/'n'/'k'."""
      click.echo(f"\n[{entry.id}] {entry.ticket_key} — {reason} —")
      click.echo(
          f"    started {entry.start_at.isoformat()}, "
          f"closed {entry.end_at.isoformat() if entry.end_at else '(open)'}"
      )
      click.echo("    Was that gap:")
      click.echo("      [s] Same work — extend this entry through the gap")
      click.echo(f"      [d] Different ticket — close {entry.ticket_key}, log the gap separately")
      click.echo("      [n] Not work — close, mark the gap as 'not_work'")
      click.echo("      [k] Skip for now")
      return click.prompt("Choice", type=click.Choice(["s", "d", "n", "k"]))


  def _apply(conn, entry: TimeEntry, choice: str, chosen_ticket: Optional[str]) -> None:
      """Apply user's choice and clear the pending flag.

      Unlike core.reconcile.reconcile_idle (designed for the live idle-return case
      where the active timer is still open), pending entries are already CLOSED
      by the sleep handler / startup recovery. So:
        - 'same': extend end_at to now.
        - 'different': insert a work entry for chosen_ticket spanning end_at → now.
        - 'not_work': insert a not_work entry spanning end_at → now.
      Original is never re-opened; there's no 'resumed' entry.
      """
      assert entry.end_at is not None
      assert entry.id is not None
      now = datetime.now(timezone.utc)

      with db_module.tx(conn):
          if choice == "s":
              time_entries.update(
                  conn, entry.id,
                  end_at=now,
                  pending_reconciliation=0,
                  reconciliation_reason=None,
              )
          elif choice == "n":
              time_entries.insert(
                  conn,
                  TimeEntry(
                      id=None, ticket_key=None,
                      start_at=entry.end_at, end_at=now,
                      kind="not_work", note="reconciled gap",
                      jira_worklog_id=None, pushed_at=None,
                      created_at=now, updated_at=now,
                  ),
              )
              time_entries.update(
                  conn, entry.id,
                  pending_reconciliation=0,
                  reconciliation_reason=None,
              )
          elif choice == "d":
              assert chosen_ticket is not None
              time_entries.insert(
                  conn,
                  TimeEntry(
                      id=None, ticket_key=chosen_ticket,
                      start_at=entry.end_at, end_at=now,
                      kind="work", note="reconciled gap",
                      jira_worklog_id=None, pushed_at=None,
                      created_at=now, updated_at=now,
                  ),
              )
              time_entries.update(
                  conn, entry.id,
                  pending_reconciliation=0,
                  reconciliation_reason=None,
              )
          else:
              raise click.ClickException(f"unknown choice {choice!r}")


  @click.command("reconcile")
  @click.argument("entry_id", required=False, type=int)
  @click.option("--json", "as_json", is_flag=True, help="Emit pending list as JSON; no prompts.")
  def reconcile(entry_id: int | None, as_json: bool) -> None:
      """Walk pending reconciliation entries and resolve them interactively."""
      conn = _open_conn()
      try:
          if as_json:
              pending = _list_pending_with_reason(conn)
              click.echo(json_module.dumps(
                  [_entry_to_dict(e, r) for e, r in pending], indent=2, default=str,
              ))
              return

          if entry_id is not None:
              entry = time_entries.get(conn, entry_id)
              if entry is None:
                  click.echo(f"no entry with id={entry_id}", err=True)
                  sys.exit(1)
              row = conn.execute(
                  "SELECT reconciliation_reason, pending_reconciliation FROM time_entries WHERE id = ?",
                  (entry_id,),
              ).fetchone()
              if not row["pending_reconciliation"]:
                  click.echo(f"entry {entry_id} is not pending reconciliation", err=True)
                  sys.exit(1)
              pending = [(entry, row["reconciliation_reason"])]
          else:
              pending = _list_pending_with_reason(conn)

          if not pending:
              click.echo("no pending reconciliations")
              return

          click.echo(f"{len(pending)} pending reconciliations:")
          for entry, reason in pending:
              choice = _prompt_choice(entry, reason)
              if choice == "k":
                  continue
              chosen_ticket = None
              if choice == "d":
                  chosen_ticket = click.prompt("New ticket for the gap")
              _apply(conn, entry, choice, chosen_ticket)
              click.echo(f"  resolved [{entry.id}] {entry.ticket_key}")
      finally:
          conn.close()
  ```

- [ ] **Step 5: Register the command**

  Edit `tsh/cli/main.py` — import and register:

  ```python
  from tsh.cli import auth, config_cmd, push as push_cmd, reconcile as reconcile_cmd, review, tracking
  ...
  cli.add_command(reconcile_cmd.reconcile)
  ```

- [ ] **Step 6: Run tests to verify they pass**

  ```powershell
  uv run pytest tests/cli/test_reconcile.py -v
  ```

  Expected: green for all six tests.

- [ ] **Step 7: Commit**

  ```bash
  git add tsh/cli/reconcile.py tsh/cli/main.py tests/cli/test_reconcile.py
  git commit -m "feat(cli): tsh reconcile — walk and resolve pending reconciliation entries"
  ```

---

### Task 20.5: Warning banner on status / start / switch

**Files:**
- Modify: `tsh/cli/tracking.py`
- Test: `tests/cli/test_tracking.py`

- [ ] **Step 1: Write the failing tests**

  Append to `tests/cli/test_tracking.py` (add imports for `time_entries`, `db_module`, `TimeEntry`, `loader`, `datetime`, `timedelta`, `timezone` if not already present):

  ```python
  def test_status_shows_warning_when_pending_reconciliation(tsh_home, running_tracker):
      # Insert a pending entry directly via the DB.
      conn = db_module.connect(loader.config_dir() / "tsh.db")
      now = datetime.now(timezone.utc)
      eid = time_entries.insert(
          conn,
          TimeEntry(id=None, ticket_key="X-1", start_at=now - timedelta(hours=1),
                    end_at=now - timedelta(minutes=30), kind="work", note="",
                    jira_worklog_id=None, pushed_at=None,
                    created_at=now, updated_at=now),
      )
      time_entries.update(conn, eid, pending_reconciliation=1, reconciliation_reason="sleep")
      conn.commit()
      conn.close()

      result = CliRunner().invoke(cli, ["status"])
      assert "1 pending reconciliation" in result.output


  def test_status_no_warning_when_clean(tsh_home, running_tracker):
      result = CliRunner().invoke(cli, ["status"])
      assert "pending reconciliation" not in result.output
  ```

  (Use whatever existing fixtures the test file uses for `tsh_home` and `running_tracker`. If the tracker fixture starts an actual server, reuse it; otherwise mock `_is_running`/`_get` so the test focuses on the warning logic.)

- [ ] **Step 2: Run tests to verify they fail**

  ```powershell
  uv run pytest tests/cli/test_tracking.py -v -k "pending_reconciliation or warning"
  ```

  Expected: FAIL (no warning printed).

- [ ] **Step 3: Add the warning helper and call it from status/start/switch**

  Edit `tsh/cli/tracking.py`. Add at the top, near `_post`:

  ```python
  from tsh.storage import db as db_module
  from tsh.storage import time_entries
  from tsh.config import loader


  def _warn_if_pending() -> None:
      """Print a one-line warning when there are pending reconciliations.

      Talks to SQLite directly (not the tracker) — works even if the tracker
      is down. Silent on any error; the warning is best-effort.
      """
      try:
          conn = db_module.connect(loader.config_dir() / "tsh.db")
      except Exception:
          return
      try:
          n = time_entries.count_pending_reconciliation(conn)
      except Exception:
          return
      finally:
          conn.close()
      if n > 0:
          plural = "s" if n != 1 else ""
          click.echo(
              f"⚠ {n} pending reconciliation{plural} — run `tsh reconcile`",
              err=False,
          )
  ```

  Then invoke `_warn_if_pending()` at the top of the `status`, `start`, and `switch` command bodies (before the `_post` / `_get` call).

- [ ] **Step 4: Run tests to verify they pass**

  ```powershell
  uv run pytest tests/cli/test_tracking.py -v -k "pending_reconciliation or warning"
  ```

  Expected: green.

- [ ] **Step 5: Run full CLI suite for regression**

  ```powershell
  uv run pytest tests/cli/ -q
  ```

  Expected: still green.

- [ ] **Step 6: Commit**

  ```bash
  git add tsh/cli/tracking.py tests/cli/test_tracking.py
  git commit -m "feat(cli): warn on pending reconciliations from status/start/switch"
  ```

---

### Task 20.6: Endpoint rewire — `/idle/return` writes reconciliation_reason

**Files:**
- Modify: `tsh/tracker/server.py`
- Test: `tests/tracker/test_server.py`

- [ ] **Step 1: Write the failing test**

  Append to `tests/tracker/test_server.py`:

  ```python
  def test_idle_return_clears_pending_flag_in_storage(client_with_pending):
      """When the user resolves via /idle/return, pending_reconciliation must clear in DB."""
      client, state, conn = client_with_pending  # fixture provides these
      # state.idle is set to pending; an active row exists.
      r = client.post("/idle/return", json={"choice": "not_work"})
      assert r.status_code == 200
      # The active row should now be flag-cleared (no entry should still have flag=1).
      assert time_entries.count_pending_reconciliation(conn) == 0
  ```

  Add the fixture to the same file (or to `tests/tracker/conftest.py`):

  ```python
  @pytest.fixture
  def client_with_pending(state):
      """TrackerState with an active entry already in 'pending' idle state."""
      from tsh.core.models import TimeEntry
      from tsh.storage import time_entries
      now = datetime.now(timezone.utc)
      start = now - timedelta(minutes=30)
      time_entries.insert(
          state._connection(),
          TimeEntry(id=None, ticket_key="SFXS-X", start_at=start, end_at=None,
                    kind="work", note="", jira_worklog_id=None, pushed_at=None,
                    created_at=start, updated_at=start),
      )
      state._connection().commit()
      state.refresh()
      state.idle = IdleState(status="pending", started_at=now - timedelta(minutes=15))
      from fastapi.testclient import TestClient
      app = create_app(state)
      client = TestClient(app)
      yield client, state, state._connection()
  ```

- [ ] **Step 2: Run the test to verify it fails**

  ```powershell
  uv run pytest tests/tracker/test_server.py::test_idle_return_clears_pending_flag_in_storage -v
  ```

  Expected: FAIL — flag stays at 0 (was never set in this test path), or if `reconcile_idle_return` doesn't touch the flag column, the assertion may pass accidentally. To make the test honest, the fixture should pre-set the flag to 1 on the active row. Adjust the fixture:

  ```python
      eid = time_entries.insert(state._connection(), ...)
      time_entries.update(state._connection(), eid,
                          pending_reconciliation=1, reconciliation_reason="idle")
      state._connection().commit()
  ```

  Now rerun — the test should fail because the existing `reconcile_idle_return` doesn't clear the flag.

- [ ] **Step 3: Update `reconcile_idle_return` to clear the flag**

  In `tsh/tracker/server.py`, inside `TrackerState.reconcile_idle_return`, after closing the original entry (in both the 'same' and other branches), clear the flag. The easiest spot: at the very end of the function, before `self.idle = IdleState()`, do:

  ```python
          # Clear pending_reconciliation on the (now-closed) original entry.
          # The original row keeps its id; the choice path either updated end_at
          # in-place ('same') or via the closed-entry update above. Either way,
          # 'active' is the row to clear.
          conn = self._connection()
          if active.id is not None:
              time_entries.update(
                  conn, active.id,
                  pending_reconciliation=0,
                  reconciliation_reason=None,
              )
              conn.commit()
  ```

  (Wrap inside the existing try/except's outer scope, but outside the `with db_module.tx(conn):` block since `update` opens its own implicit transaction — or alternatively call it inside the `tx` block before commit.)

- [ ] **Step 4: Run the test to verify it passes**

  ```powershell
  uv run pytest tests/tracker/test_server.py::test_idle_return_clears_pending_flag_in_storage -v
  ```

  Expected: green.

- [ ] **Step 5: Run the full tracker suite for regression**

  ```powershell
  uv run pytest tests/tracker/ -q
  ```

  Expected: green.

- [ ] **Step 6: Commit**

  ```bash
  git add tsh/tracker/server.py tests/tracker/test_server.py
  git commit -m "fix(tracker): /idle/return clears pending_reconciliation in storage"
  ```

---

## Phase E — Git switching: `tsh switch --from-branch`

### Task 20.7: Branch-driven switch primitive + config

**Files:**
- Modify: `tsh/config/loader.py` (add `git.branch_pattern` default)
- Modify: `tsh/cli/tracking.py` (extend `switch` command)
- Test: `tests/cli/test_tracking.py`

- [ ] **Step 1: Add config default**

  Edit `tsh/config/loader.py` — add a top-level `git` block to `DEFAULTS`:

  ```python
  DEFAULTS: dict[str, dict[str, Any]] = {
      ...
      "git": {
          "branch_pattern": r"[A-Z][A-Z0-9_]+-\d+",
      },
  }
  ```

- [ ] **Step 2: Write the failing tests**

  Append to `tests/cli/test_tracking.py`:

  ```python
  @pytest.mark.parametrize(
      "branch,expected_ticket",
      [
          ("feature/SFXS-1234-fix", "SFXS-1234"),
          ("SFXS-1234", "SFXS-1234"),
          ("feature/SFXS-1234/SFXS-5678-rebase", "SFXS-1234"),  # first match wins
          ("bugfix/ABC-9-fix", "ABC-9"),
      ],
  )
  def test_switch_from_branch_parses_and_posts(branch, expected_ticket,
                                                tsh_home, running_tracker, mocker):
      mocker.patch("tsh.cli.tracking._current_branch", return_value=branch)
      post_mock = mocker.patch("tsh.cli.tracking._post",
                                return_value={"ticket_key": expected_ticket, "id": 99})
      get_mock = mocker.patch("tsh.cli.tracking._get",
                                return_value={"active": None, "elapsed_seconds": 0,
                                              "idle_status": "clear", "idle_started_at": None})
      result = CliRunner().invoke(cli, ["switch", "--from-branch"])
      assert result.exit_code == 0, result.output
      post_mock.assert_called_once()
      args = post_mock.call_args
      assert args.args[0] == "/switch"
      assert args.args[1]["ticket_key"] == expected_ticket


  @pytest.mark.parametrize(
      "branch",
      ["sfxs-1234", "chore/cleanup", "release/2026-05-15", "main", "master"],
  )
  def test_switch_from_branch_no_match_exits_silently(branch, tsh_home, running_tracker, mocker):
      mocker.patch("tsh.cli.tracking._current_branch", return_value=branch)
      post_mock = mocker.patch("tsh.cli.tracking._post")
      result = CliRunner().invoke(cli, ["switch", "--from-branch"])
      assert result.exit_code == 0
      post_mock.assert_not_called()
      # Quiet stderr line is fine; just no HTTP call.


  def test_switch_from_branch_no_repo_exits_silently(tsh_home, running_tracker, mocker):
      mocker.patch("tsh.cli.tracking._current_branch", return_value=None)
      post_mock = mocker.patch("tsh.cli.tracking._post")
      result = CliRunner().invoke(cli, ["switch", "--from-branch"])
      assert result.exit_code == 0
      post_mock.assert_not_called()


  def test_switch_from_branch_idempotent_when_already_active(tsh_home, running_tracker, mocker):
      mocker.patch("tsh.cli.tracking._current_branch", return_value="feature/SFXS-1234-x")
      mocker.patch("tsh.cli.tracking._get",
                    return_value={"active": {"ticket_key": "SFXS-1234"},
                                  "elapsed_seconds": 60, "idle_status": "clear",
                                  "idle_started_at": None})
      post_mock = mocker.patch("tsh.cli.tracking._post")
      result = CliRunner().invoke(cli, ["switch", "--from-branch"])
      assert result.exit_code == 0
      post_mock.assert_not_called()
  ```

- [ ] **Step 3: Run tests to verify they fail**

  ```powershell
  uv run pytest tests/cli/test_tracking.py -v -k "from_branch"
  ```

  Expected: FAIL — `--from-branch` flag doesn't exist.

- [ ] **Step 4: Implement `_current_branch` + extend `switch`**

  Edit `tsh/cli/tracking.py`:

  ```python
  import re
  import subprocess
  from typing import Optional


  def _current_branch() -> Optional[str]:
      """Return the current git branch name, or None if not in a repo / git unavailable / detached HEAD."""
      try:
          out = subprocess.run(
              ["git", "rev-parse", "--abbrev-ref", "HEAD"],
              cwd=".",
              capture_output=True,
              text=True,
              timeout=2.0,
          )
      except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
          return None
      if out.returncode != 0:
          return None
      name = out.stdout.strip()
      if not name or name == "HEAD":  # detached
          return None
      return name


  def _ticket_from_branch(branch: str) -> Optional[str]:
      """Extract the first ticket key matching the configured branch_pattern."""
      pattern = loader.load()["git"]["branch_pattern"]
      m = re.search(pattern, branch)
      return m.group(0) if m else None
  ```

  Add `from tsh.config import loader` at the top (if not already present).

  Replace the `switch` command with:

  ```python
  @click.command("switch")
  @click.argument("ticket", required=False)
  @click.option("-m", "--note", default="", help="Worklog note for the new entry.")
  @click.option("--from-branch", "from_branch", is_flag=True,
                help="Extract ticket key from current git branch instead of taking a positional argument.")
  def switch(ticket: str | None, note: str, from_branch: bool) -> None:
      """End the active timer (if any) and start a new one."""
      if from_branch:
          if ticket is not None:
              raise click.UsageError("--from-branch is exclusive of the positional TICKET")
          branch = _current_branch()
          if branch is None:
              return  # silent exit 0: not a repo / detached HEAD / git missing
          parsed = _ticket_from_branch(branch)
          if parsed is None:
              click.echo(f"no ticket in branch '{branch}'", err=True)
              return
          # Idempotency: skip if same ticket already active.
          if not _is_running():
              click.echo("tracker not running — start with `tsh tray`", err=True)
              return
          current = _get("/status")
          active = current.get("active") if isinstance(current, dict) else None
          if active and active.get("ticket_key") == parsed:
              return  # already on this ticket
          ticket = parsed

      _warn_if_pending()
      if ticket is None:
          raise click.UsageError("missing TICKET (or pass --from-branch)")
      entry = _post("/switch", {"ticket_key": ticket, "note": note})
      click.echo(f"Switched to {entry['ticket_key']} (id={entry['id']})")
  ```

- [ ] **Step 5: Run tests to verify they pass**

  ```powershell
  uv run pytest tests/cli/test_tracking.py -v -k "from_branch"
  ```

  Expected: green.

- [ ] **Step 6: Run full CLI suite for regression**

  ```powershell
  uv run pytest tests/cli/ -q
  ```

  Expected: green.

- [ ] **Step 7: Commit**

  ```bash
  git add tsh/cli/tracking.py tsh/config/loader.py tests/cli/test_tracking.py
  git commit -m "feat(cli): tsh switch --from-branch (parse ticket from current git branch)"
  ```

---

## Phase F — Git hook installer

### Task 20.8: `tsh hook install / uninstall / status`

**Files:**
- Create: `tsh/cli/hook.py`
- Modify: `tsh/cli/main.py`
- Create: `tests/cli/test_hook.py`

- [ ] **Step 1: Write the failing tests**

  Create `tests/cli/test_hook.py`:

  ```python
  """Tests for tsh.cli.hook — per-repo git post-checkout install."""
  from __future__ import annotations
  import os
  import subprocess
  from pathlib import Path

  import pytest
  from click.testing import CliRunner

  from tsh.cli.main import cli


  @pytest.fixture
  def git_repo(tmp_path: Path, monkeypatch):
      """Initialize a throwaway git repo in tmp_path and chdir into it."""
      subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
      monkeypatch.chdir(tmp_path)
      return tmp_path


  def test_hook_install_writes_post_checkout(git_repo: Path):
      result = CliRunner().invoke(cli, ["hook", "install"])
      assert result.exit_code == 0, result.output
      hook_path = git_repo / ".git" / "hooks" / "post-checkout"
      assert hook_path.exists()
      content = hook_path.read_text()
      assert "tsh-managed post-checkout hook" in content
      assert "tsh switch --from-branch" in content


  def test_hook_install_idempotent_when_ours(git_repo: Path):
      CliRunner().invoke(cli, ["hook", "install"])
      result = CliRunner().invoke(cli, ["hook", "install"])
      assert result.exit_code == 0
      # Sanity: file still has our content.
      content = (git_repo / ".git" / "hooks" / "post-checkout").read_text()
      assert "tsh-managed post-checkout hook" in content


  def test_hook_install_refuses_on_conflict(git_repo: Path):
      hook_path = git_repo / ".git" / "hooks" / "post-checkout"
      hook_path.parent.mkdir(parents=True, exist_ok=True)
      hook_path.write_text("#!/bin/sh\necho 'some other hook'\n")
      result = CliRunner().invoke(cli, ["hook", "install"])
      assert result.exit_code != 0
      assert "conflicting" in result.output.lower() or "exists" in result.output.lower()
      # Original content preserved.
      assert "echo 'some other hook'" in hook_path.read_text()


  def test_hook_install_force_overwrites_conflict(git_repo: Path):
      hook_path = git_repo / ".git" / "hooks" / "post-checkout"
      hook_path.parent.mkdir(parents=True, exist_ok=True)
      hook_path.write_text("#!/bin/sh\necho 'foreign'\n")
      result = CliRunner().invoke(cli, ["hook", "install", "--force"])
      assert result.exit_code == 0
      assert "tsh-managed" in hook_path.read_text()


  def test_hook_uninstall_removes_ours(git_repo: Path):
      CliRunner().invoke(cli, ["hook", "install"])
      result = CliRunner().invoke(cli, ["hook", "uninstall"])
      assert result.exit_code == 0
      hook_path = git_repo / ".git" / "hooks" / "post-checkout"
      assert not hook_path.exists()


  def test_hook_uninstall_refuses_to_remove_foreign(git_repo: Path):
      hook_path = git_repo / ".git" / "hooks" / "post-checkout"
      hook_path.parent.mkdir(parents=True, exist_ok=True)
      hook_path.write_text("#!/bin/sh\necho 'foreign'\n")
      result = CliRunner().invoke(cli, ["hook", "uninstall"])
      assert result.exit_code != 0
      assert hook_path.exists()
      assert "echo 'foreign'" in hook_path.read_text()


  def test_hook_status_reports_correctly(git_repo: Path):
      r1 = CliRunner().invoke(cli, ["hook", "status"])
      assert "not installed" in r1.output

      CliRunner().invoke(cli, ["hook", "install"])
      r2 = CliRunner().invoke(cli, ["hook", "status"])
      assert "installed" in r2.output and "not installed" not in r2.output


  def test_hook_install_outside_repo_fails(tmp_path: Path, monkeypatch):
      monkeypatch.chdir(tmp_path)
      result = CliRunner().invoke(cli, ["hook", "install"])
      assert result.exit_code != 0
      assert "not a git repository" in result.output.lower() or "not in a repo" in result.output.lower()
  ```

- [ ] **Step 2: Run tests to verify they fail**

  ```powershell
  uv run pytest tests/cli/test_hook.py -v
  ```

  Expected: FAIL — `hook` subcommand doesn't exist.

- [ ] **Step 3: Implement `tsh/cli/hook.py`**

  Create `tsh/cli/hook.py`:

  ```python
  """tsh hook install/uninstall/status — per-repo git post-checkout integration."""
  from __future__ import annotations

  import subprocess
  import sys
  from pathlib import Path

  import click


  SENTINEL = "# tsh-managed post-checkout hook"

  HOOK_CONTENT = f"""#!/bin/sh
  {SENTINEL} — do not edit; manage with `tsh hook install/uninstall`
  # Args: $1=prev_HEAD $2=new_HEAD $3=branch_checkout_flag (1 if branch)
  [ "$3" = "1" ] || exit 0
  tsh switch --from-branch >/dev/null 2>&1
  exit 0
  """


  def _git_dir() -> Path | None:
      """Return Path to the current repo's .git directory, or None if not in a repo."""
      try:
          out = subprocess.run(
              ["git", "rev-parse", "--git-dir"],
              capture_output=True, text=True, timeout=2.0,
          )
      except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
          return None
      if out.returncode != 0:
          return None
      return Path(out.stdout.strip()).resolve()


  def _hook_path(git_dir: Path) -> Path:
      return git_dir / "hooks" / "post-checkout"


  @click.group("hook")
  def hook_group() -> None:
      """Per-repo git hook management for `tsh switch --from-branch`."""


  @hook_group.command("install")
  @click.option("--force", is_flag=True, help="Overwrite an existing non-tsh hook.")
  def install(force: bool) -> None:
      """Write .git/hooks/post-checkout in the current repo."""
      git_dir = _git_dir()
      if git_dir is None:
          click.echo("error: not a git repository", err=True)
          sys.exit(1)
      hook_path = _hook_path(git_dir)
      hook_path.parent.mkdir(parents=True, exist_ok=True)

      if hook_path.exists():
          existing = hook_path.read_text()
          if SENTINEL in existing:
              # Idempotent re-install — rewrite to refresh content if it drifted.
              hook_path.write_text(HOOK_CONTENT)
              hook_path.chmod(0o755)
              click.echo(f"hook already installed at {hook_path} (refreshed)")
              return
          if not force:
              click.echo(
                  f"error: conflicting hook exists at {hook_path}\n"
                  f"--- existing content ---\n{existing}\n--- end ---\n"
                  f"re-run with --force to overwrite",
                  err=True,
              )
              sys.exit(1)

      hook_path.write_text(HOOK_CONTENT)
      hook_path.chmod(0o755)
      click.echo(f"installed {hook_path}")


  @hook_group.command("uninstall")
  def uninstall() -> None:
      """Remove the post-checkout hook if it's the one we wrote."""
      git_dir = _git_dir()
      if git_dir is None:
          click.echo("error: not a git repository", err=True)
          sys.exit(1)
      hook_path = _hook_path(git_dir)
      if not hook_path.exists():
          click.echo("no hook to uninstall")
          return
      if SENTINEL not in hook_path.read_text():
          click.echo(
              f"error: hook at {hook_path} was not installed by tsh; "
              f"remove it manually if you want it gone",
              err=True,
          )
          sys.exit(1)
      hook_path.unlink()
      click.echo(f"removed {hook_path}")


  @hook_group.command("status")
  def status_cmd() -> None:
      """Print whether the current repo has the tsh hook installed."""
      git_dir = _git_dir()
      if git_dir is None:
          click.echo("not a git repository")
          return
      hook_path = _hook_path(git_dir)
      if not hook_path.exists():
          click.echo("not installed")
          return
      content = hook_path.read_text()
      if SENTINEL in content:
          click.echo(f"installed at {hook_path}")
      else:
          click.echo(
              f"conflicting hook present at {hook_path} "
              f"(run `tsh hook install --force` to replace)"
          )
  ```

- [ ] **Step 4: Register the command group**

  Edit `tsh/cli/main.py`:

  ```python
  from tsh.cli import auth, config_cmd, hook, push as push_cmd, reconcile as reconcile_cmd, review, tracking
  ...
  cli.add_command(hook.hook_group, name="hook")
  ```

- [ ] **Step 5: Run tests to verify they pass**

  ```powershell
  uv run pytest tests/cli/test_hook.py -v
  ```

  Expected: green. (Note: these tests shell out to real `git init`; ensure git is on PATH in the test environment — it is, since `uv run` inherits the user shell PATH.)

- [ ] **Step 6: Commit**

  ```bash
  git add tsh/cli/hook.py tsh/cli/main.py tests/cli/test_hook.py
  git commit -m "feat(cli): tsh hook install/uninstall/status (per-repo post-checkout)"
  ```

---

## Phase G — Background launch: `tsh tray --detach` + logging

### Task 20.9: `--detach` re-spawn via pythonw + rotating log file

**Files:**
- Modify: `tsh/cli/tray.py`
- Modify: `tsh/tracker/runner.py` (rotating log handler init)
- Test: `tests/cli/test_tray_cmd.py`

- [ ] **Step 1: Write the failing tests**

  Append to `tests/cli/test_tray_cmd.py`:

  ```python
  def test_tray_detach_uses_pythonw_and_no_console(tmp_path, monkeypatch, mocker):
      monkeypatch.setenv("TSH_CONFIG_DIR", str(tmp_path))
      mocker.patch("tsh.cli.tray._is_running", return_value=False)
      popen_mock = mocker.patch("subprocess.Popen")

      from click.testing import CliRunner
      from tsh.cli.main import cli
      result = CliRunner().invoke(cli, ["tray", "--detach"])
      assert result.exit_code == 0
      popen_mock.assert_called_once()
      args, kwargs = popen_mock.call_args
      cmd = args[0]
      assert cmd[0].lower().endswith("pythonw.exe"), f"expected pythonw.exe, got {cmd[0]}"
      assert "-m" in cmd and "tsh" in cmd and "tray" in cmd
      import subprocess as sp
      flags = kwargs.get("creationflags", 0)
      assert flags & sp.CREATE_NO_WINDOW
      assert flags & sp.DETACHED_PROCESS


  def test_tray_detach_noop_when_already_running(tmp_path, monkeypatch, mocker):
      monkeypatch.setenv("TSH_CONFIG_DIR", str(tmp_path))
      mocker.patch("tsh.cli.tray._is_running", return_value=True)
      popen_mock = mocker.patch("subprocess.Popen")

      from click.testing import CliRunner
      from tsh.cli.main import cli
      result = CliRunner().invoke(cli, ["tray", "--detach"])
      assert result.exit_code == 0
      assert "already running" in result.output
      popen_mock.assert_not_called()
  ```

- [ ] **Step 2: Run tests to verify they fail**

  ```powershell
  uv run pytest tests/cli/test_tray_cmd.py -v -k "detach"
  ```

  Expected: FAIL — `--detach` flag doesn't exist.

- [ ] **Step 3: Implement `--detach`**

  Edit `tsh/cli/tray.py`:

  ```python
  import os
  import subprocess
  import sys
  from pathlib import Path


  def _windowless_python() -> str:
      """Return the path to pythonw.exe in the current interpreter's directory.

      In PyInstaller-frozen mode (Phase 9), this will be replaced by the sibling
      tsh-tray.exe; we'll branch on sys.frozen at that point. For now (dev mode):
      """
      if getattr(sys, "frozen", False):
          # Frozen: assume sibling tsh-tray.exe in the same directory.
          here = Path(sys.executable).parent
          candidate = here / "tsh-tray.exe"
          if candidate.exists():
              return str(candidate)
          # Fall back to sys.executable (likely the console exe — better than crashing).
          return sys.executable
      # Dev mode: locate pythonw.exe in the same directory as the current python.exe.
      py = Path(sys.executable)
      pyw = py.with_name("pythonw.exe")
      if pyw.exists():
          return str(pyw)
      # Last resort: hope pythonw is on PATH.
      return "pythonw.exe"


  @click.command("tray")
  @click.option("--detach", is_flag=True, help="Re-spawn windowless and exit; tray runs detached.")
  def tray(detach: bool) -> None:
      """Start the tracker (idempotent — exits cleanly if already running)."""
      if _is_running():
          click.echo("tracker already running")
          return

      if detach:
          windowless = _windowless_python()
          # Args differ for frozen vs dev mode:
          if getattr(sys, "frozen", False):
              cmd = [windowless]  # tsh-tray.exe is its own entry point
          else:
              cmd = [windowless, "-m", "tsh", "tray"]
          subprocess.Popen(
              cmd,
              creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
              close_fds=True,
              cwd=os.getcwd(),
          )
          click.echo("tracker starting (detached)")
          return

      # Lazy import so `tsh --help` doesn't spin up uvicorn / pystray.
      from tsh.tracker import runner
      runner.run()
  ```

- [ ] **Step 4: Add rotating log file setup**

  Edit `tsh/tracker/runner.py`. At the top, add:

  ```python
  import logging.handlers


  def _setup_file_logging() -> None:
      """Add a rotating-file handler under ~/.tsh/logs/ — only when stdout is not a TTY."""
      if sys.stdout.isatty():
          return
      log_dir = loader.config_dir() / "logs"
      log_dir.mkdir(parents=True, exist_ok=True)
      handler = logging.handlers.RotatingFileHandler(
          log_dir / "tsh-tray.log",
          maxBytes=1024 * 1024,  # 1 MB
          backupCount=3,
          encoding="utf-8",
      )
      handler.setFormatter(
          logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
      )
      root = logging.getLogger()
      # Avoid duplicate handlers if run() is called twice.
      if not any(isinstance(h, logging.handlers.RotatingFileHandler)
                  for h in root.handlers):
          root.addHandler(handler)
      root.setLevel(logging.INFO)
  ```

  Add `import sys` to the runner imports.

  Call `_setup_file_logging()` as the first line of `run()`:

  ```python
  def run() -> None:
      _setup_file_logging()
      cfg = loader.load()
      ...
  ```

- [ ] **Step 5: Run tests to verify they pass**

  ```powershell
  uv run pytest tests/cli/test_tray_cmd.py -v -k "detach"
  ```

  Expected: green.

- [ ] **Step 6: Run full CLI suite for regression**

  ```powershell
  uv run pytest tests/cli/ -q
  ```

  Expected: green.

- [ ] **Step 7: Commit**

  ```bash
  git add tsh/cli/tray.py tsh/tracker/runner.py tests/cli/test_tray_cmd.py
  git commit -m "feat(cli): tsh tray --detach (pythonw re-spawn) + rotating log file"
  ```

---

## Phase H — Autostart command

### Task 20.10: `tsh autostart enable/disable/status`

**Files:**
- Create: `tsh/cli/autostart.py`
- Modify: `tsh/cli/main.py`
- Create: `tests/cli/test_autostart.py`

- [ ] **Step 1: Write the failing tests**

  Create `tests/cli/test_autostart.py`:

  ```python
  """Tests for tsh.cli.autostart — Startup-folder shortcut management."""
  from __future__ import annotations
  import os
  from pathlib import Path

  import pytest
  from click.testing import CliRunner

  from tsh.cli.main import cli


  @pytest.fixture
  def fake_startup(tmp_path: Path, monkeypatch):
      """Pretend the Windows Startup folder lives under tmp_path."""
      appdata = tmp_path / "AppData" / "Roaming"
      startup = appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
      startup.mkdir(parents=True, exist_ok=True)
      monkeypatch.setenv("APPDATA", str(appdata))
      return startup


  def test_autostart_status_disabled_by_default(fake_startup, mocker):
      mocker.patch("tsh.cli.autostart._daemon_running", return_value=False)
      result = CliRunner().invoke(cli, ["autostart", "status"])
      assert result.exit_code == 0
      assert "disabled" in result.output.lower()


  def test_autostart_enable_writes_shortcut(fake_startup, mocker):
      """Verify CreateShortcut is called with the right TargetPath / Arguments."""
      saved = {}

      class FakeShortcut:
          def __init__(self):
              self._fields = {}
          def __setattr__(self, name, value):
              if name == "_fields":
                  object.__setattr__(self, name, value)
              else:
                  self._fields[name] = value
          def save(self):
              saved.update(self._fields)

      class FakeShell:
          def CreateShortcut(self, path):
              saved["path"] = path
              return FakeShortcut()

      mocker.patch(
          "tsh.cli.autostart._wscript_shell",
          return_value=FakeShell(),
      )
      mocker.patch("tsh.cli.autostart._spawn_detached")
      mocker.patch("tsh.cli.autostart._daemon_running", return_value=False)

      result = CliRunner().invoke(cli, ["autostart", "enable"])
      assert result.exit_code == 0, result.output
      assert saved["TargetPath"].lower().endswith("pythonw.exe") or \
              saved["TargetPath"].lower().endswith("tsh-tray.exe")
      assert "tsh" in saved["Arguments"] and "tray" in saved["Arguments"]
      assert "Startup" in saved["path"]


  def test_autostart_enable_starts_daemon_if_not_running(fake_startup, mocker):
      mocker.patch("tsh.cli.autostart._wscript_shell")
      spawn_mock = mocker.patch("tsh.cli.autostart._spawn_detached")
      mocker.patch("tsh.cli.autostart._daemon_running", return_value=False)

      CliRunner().invoke(cli, ["autostart", "enable"])
      spawn_mock.assert_called_once()


  def test_autostart_disable_removes_only_our_shortcut(fake_startup, mocker):
      """If the .lnk's TargetPath matches our sentinel, remove it. Otherwise refuse."""
      our_lnk = fake_startup / "tsh.lnk"
      our_lnk.write_bytes(b"placeholder")
      mocker.patch("tsh.cli.autostart._shortcut_is_ours", return_value=True)
      result = CliRunner().invoke(cli, ["autostart", "disable"])
      assert result.exit_code == 0
      assert not our_lnk.exists()


  def test_autostart_disable_refuses_foreign_shortcut(fake_startup, mocker):
      foreign = fake_startup / "tsh.lnk"
      foreign.write_bytes(b"placeholder")
      mocker.patch("tsh.cli.autostart._shortcut_is_ours", return_value=False)
      result = CliRunner().invoke(cli, ["autostart", "disable"])
      assert result.exit_code != 0
      assert foreign.exists()
  ```

- [ ] **Step 2: Run tests to verify they fail**

  ```powershell
  uv run pytest tests/cli/test_autostart.py -v
  ```

  Expected: FAIL — `autostart` doesn't exist.

- [ ] **Step 3: Implement `tsh/cli/autostart.py`**

  Create `tsh/cli/autostart.py`:

  ```python
  """tsh autostart enable/disable/status — Startup-folder shortcut management."""
  from __future__ import annotations

  import os
  import subprocess
  import sys
  from pathlib import Path

  import click

  from tsh.cli.tray import _is_running, _windowless_python


  SHORTCUT_NAME = "tsh.lnk"
  DESCRIPTION = "Timesheet Helper background tracker"


  def _startup_folder() -> Path:
      appdata = os.environ.get("APPDATA")
      if not appdata:
          raise click.ClickException("APPDATA env var not set; cannot locate Startup folder")
      return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


  def _shortcut_path() -> Path:
      return _startup_folder() / SHORTCUT_NAME


  def _wscript_shell():
      """Lazy import — pywin32 isn't available in test env on non-Windows hosts."""
      import win32com.client  # type: ignore[import-not-found]
      return win32com.client.Dispatch("WScript.Shell")


  def _read_shortcut_target(shortcut_path: Path) -> str | None:
      """Return the TargetPath of the .lnk, or None on error."""
      try:
          shell = _wscript_shell()
          shortcut = shell.CreateShortcut(str(shortcut_path))  # opens existing
          return shortcut.TargetPath
      except Exception:
          return None


  def _shortcut_is_ours(shortcut_path: Path) -> bool:
      target = _read_shortcut_target(shortcut_path)
      if target is None:
          return False
      target_lower = target.lower()
      return target_lower.endswith("pythonw.exe") or target_lower.endswith("tsh-tray.exe")


  def _daemon_running() -> bool:
      return _is_running()


  def _spawn_detached() -> None:
      """Start the tracker in detached mode (same logic as `tsh tray --detach`)."""
      windowless = _windowless_python()
      if getattr(sys, "frozen", False):
          cmd = [windowless]
      else:
          cmd = [windowless, "-m", "tsh", "tray"]
      subprocess.Popen(
          cmd,
          creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
          close_fds=True,
          cwd=str(Path.home()),
      )


  @click.group("autostart")
  def autostart_group() -> None:
      """Manage the Windows Startup-folder shortcut for the tracker."""


  @autostart_group.command("enable")
  def enable() -> None:
      """Write the Startup shortcut and start the daemon if not running."""
      shortcut_path = _shortcut_path()
      shortcut_path.parent.mkdir(parents=True, exist_ok=True)
      windowless = _windowless_python()

      shell = _wscript_shell()
      shortcut = shell.CreateShortcut(str(shortcut_path))
      shortcut.TargetPath = windowless
      if getattr(sys, "frozen", False):
          shortcut.Arguments = ""
      else:
          shortcut.Arguments = "-m tsh tray"
      shortcut.WorkingDirectory = str(Path.home())
      shortcut.Description = DESCRIPTION
      shortcut.save()
      click.echo(f"enabled: shortcut written to {shortcut_path}")

      if not _daemon_running():
          _spawn_detached()
          click.echo("daemon started (detached)")
      else:
          click.echo("daemon already running")


  @autostart_group.command("disable")
  def disable() -> None:
      """Remove the Startup shortcut (only if it's ours)."""
      shortcut_path = _shortcut_path()
      if not shortcut_path.exists():
          click.echo("autostart already disabled")
          return
      if not _shortcut_is_ours(shortcut_path):
          click.echo(
              f"error: {shortcut_path} was not created by tsh; "
              f"remove it manually if you want it gone",
              err=True,
          )
          raise click.exceptions.Exit(1)
      shortcut_path.unlink()
      click.echo(f"removed {shortcut_path}")


  @autostart_group.command("status")
  def status_cmd() -> None:
      """Print whether autostart is enabled and whether the daemon is running."""
      shortcut_path = _shortcut_path()
      if shortcut_path.exists() and _shortcut_is_ours(shortcut_path):
          click.echo("Startup shortcut: enabled")
          click.echo("Run on next login: yes")
      else:
          click.echo("Startup shortcut: disabled")
          click.echo("Run on next login: no")
      click.echo(f"Daemon: {'running' if _daemon_running() else 'not running'}")
  ```

- [ ] **Step 4: Register the command group**

  Edit `tsh/cli/main.py`:

  ```python
  from tsh.cli import auth, autostart, config_cmd, hook, push as push_cmd, reconcile as reconcile_cmd, review, tracking
  ...
  cli.add_command(autostart.autostart_group, name="autostart")
  ```

- [ ] **Step 5: Run tests to verify they pass**

  ```powershell
  uv run pytest tests/cli/test_autostart.py -v
  ```

  Expected: green. (Note: the tests fully mock `_wscript_shell` and `_spawn_detached`; pywin32 isn't actually invoked.)

- [ ] **Step 6: Run full suite for regression**

  ```powershell
  uv run pytest -q
  ```

  Expected: all green; total test count up by roughly 30+.

- [ ] **Step 7: Commit**

  ```bash
  git add tsh/cli/autostart.py tsh/cli/main.py tests/cli/test_autostart.py
  git commit -m "feat(cli): tsh autostart enable/disable/status (Startup-folder shortcut)"
  ```

---

## Phase I — Documentation

### Task 20.11: Update dogfood cheat-sheet and project plan

**Files:**
- Modify: `docs/dogfood-cheatsheet.md`
- Modify: `docs/plans/2026-04-27-timesheet-helper.md`

- [ ] **Step 1: Update the cheat-sheet**

  Edit `docs/dogfood-cheatsheet.md`. Replace the watch-out for "Manual log says 'no active timer' but tray claims one" with two new sections (insert before the "Where state lives" section):

  ```markdown
  ## Background install (one-time)

  ```powershell
  tsh autostart enable                     # writes Startup shortcut + starts daemon now
  tsh autostart status                     # prints enabled/disabled + running state
  tsh autostart disable                    # removes the shortcut (leaves a running daemon alone)
  ```

  After `enable`, the tray icon appears on every login automatically — no terminal needed.

  ## Git auto-switch (per-repo)

  ```powershell
  cd C:\path\to\some-project
  tsh hook install                         # writes .git/hooks/post-checkout
  tsh hook status                          # confirm
  ```

  After install, `git checkout feature/SFXS-1234-x` (or any branch with a Jira ticket key in its name) silently calls `tsh switch SFXS-1234`. Branches without a matching ticket key produce a quiet stderr line and do nothing — your `gco` workflow stays unblocked.

  Override the branch regex for projects with a different ticket convention:

  ```powershell
  tsh config set git.branch_pattern '<your-regex>'
  ```

  ## Sleep / overnight gaps

  If the laptop sleeps with a timer running, on next interaction you'll see:

  ```
  ⚠ 1 pending reconciliation — run `tsh reconcile`
  ```

  Walk through it:

  ```powershell
  tsh reconcile                            # interactive
  tsh reconcile --json                     # list pending as JSON, no prompts
  tsh reconcile 123                        # resolve a single entry by id
  ```

  The original timer is auto-closed at the time of your last keyboard/mouse input before sleep — you just classify the gap.
  ```

  Also update the Phase-6 watch-outs table row for idle/reconciliation:

  ```markdown
  | `tsh status` shows `⚠ N pending reconciliations` | Daemon detected a sleep, idle return, or recovered an orphaned timer. Run `tsh reconcile`. |
  ```

- [ ] **Step 2: Mark Phase 7 + Phase 8 progress in the project plan**

  Edit `docs/plans/2026-04-27-timesheet-helper.md` — update the status header:

  ```markdown
  ## Status — 2026-05-22 (Phase 7 + 8 complete)

  **Phases 1–8 complete. ~340+ tests passing on `master`.**

  Headless daemon hardened: machine-sleep detected and reconciled, orphaned active timers recovered on daemon startup, git checkout drives auto-switching via per-repo post-checkout hook, autostart at Windows login via Startup folder.

  Resume at **Phase 9 — PyInstaller packaging** (single-binary build for `tsh.exe` + `tsh-tray.exe`).
  ```

  Update the completed-work table — append rows for tasks 20.0 through 20.11 (or just task 20–26 from the existing plan numbering — match whichever scheme the file already uses).

  Update the "[NEXT]" annotations in the file-layout section: change `cli/reconcile.py` from `[NEXT]` to `[DONE]`, same for `cli/hook.py`, `cli/autostart.py`; remove the `; --from-branch NEXT` / `; --detach + autostart NEXT` qualifiers in the tray/tracking lines.

- [ ] **Step 3: Commit**

  ```bash
  git add docs/dogfood-cheatsheet.md docs/plans/2026-04-27-timesheet-helper.md
  git commit -m "docs: cover autostart, git hook, and reconcile workflows in cheat-sheet + plan"
  ```

---

## Final verification

- [ ] **Run the full suite once more**

  ```powershell
  uv run pytest -q
  ```

  Expected: all green. Test count should be ~340+ (up from 311).

- [ ] **Smoke-check the new commands work**

  ```powershell
  uv run tsh --help                        # `reconcile`, `hook`, `autostart` listed
  uv run tsh reconcile --json              # prints `[]` (or pending list)
  uv run tsh hook status                   # works inside this repo (will say "not installed")
  uv run tsh autostart status              # prints disabled (until you `enable`)
  ```

- [ ] **Manual dogfood: prove sleep handling works end-to-end**

  - `uv run tsh tray --detach`
  - `uv run tsh start SFXS-XXXX`
  - Lock the screen and sleep the laptop (Win+L, then close lid).
  - In the morning, run `uv run tsh status` — expect the warning banner.
  - `uv run tsh reconcile` — walk through the pending entry, choose "not work".
  - `uv run tsh review` — confirm the closed entry's `end_at` is when you last touched the laptop the previous evening, not when you woke up.

---

## Self-review notes

Verified against the spec at `docs/superpowers/specs/2026-05-15-headless-daemon-improvements-design.md`:

| Spec section | Plan task |
|---|---|
| §4 storage migration + new columns | Task 20.0 |
| §4 list/count_pending_reconciliation + update allowlist | Task 20.1 |
| §5.1 sleep detection in idle loop | Task 20.2 |
| §5.2 stale-entry recovery on startup | Task 20.3 |
| §5.3 `tsh reconcile` interactive + JSON + single-id | Task 20.4 |
| §5.4 warning banners on status/start/switch | Task 20.5 |
| §5.5 `/idle/return` endpoint consistency | Task 20.6 |
| §6.1 `tsh switch --from-branch` primitive | Task 20.7 |
| §6.2 `git.branch_pattern` config | Task 20.7 |
| §6.3 `tsh hook install/uninstall/status` | Task 20.8 |
| §7.1 `tsh tray --detach` | Task 20.9 |
| §7.2 rotating log file | Task 20.9 |
| §7.3 `tsh autostart enable/disable/status` | Task 20.10 |
| §8 documentation updates | Task 20.11 |
| §9 testing approach | Embedded in each task's Step-1 test code |
