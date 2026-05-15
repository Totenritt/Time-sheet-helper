# Headless Daemon Improvements — Design Spec

**Date:** 2026-05-15
**Status:** Approved for planning
**Owner:** Casey Luo
**Supersedes:** Phase 7 (GUI) of the 2026-04-27 plan; layers onto Phases 1–6 already on `master`.

## 1. Goal

After roughly two weeks of dogfooding the CLI + tracker daemon shipped through Phase 6, three rough edges dominate the daily experience:

1. **Sleep breaks idle detection.** When the laptop sleeps with a timer running, the daemon's idle loop is suspended along with the rest of the system. On wake, the loop resumes; the timer is still "active" and silently accumulates fake hours.
2. **Manual `tsh switch` is the dominant data-quality problem.** Most work corresponds to a git branch whose name encodes the Jira ticket. Forgetting to switch is easy.
3. **Foreground `tsh tray` is awkward.** Requiring a terminal window to stay open conflicts with the "lightweight, set-and-forget" promise.

This spec defines the work to fix all three without re-introducing a GUI. The CLI plus tray icon remain the only user surfaces.

## 2. Constraints and principles

- **Sleep is a special reason code, not a special state.** It feeds the same `pending_reconciliation` queue idle does; one reconciliation surface (`tsh reconcile`) handles all reason codes.
- **Detection uses `time.time()` deltas, not Win32 power events.** Wall-clock advances through sleep; monotonic doesn't. Testable with a fake clock; no extra dependency.
- **Per-repo opt-in for git hooks, no global state.** No `core.hooksPath`, no HKLM registry writes. Per-user Startup folder is the only system-wide artefact, and it's discoverable from Task Manager → Startup.
- **Don't block routine commands on pending reconciliations.** A warning on `tsh status` / `tsh start` / `tsh switch` is enough; forcing a reconciliation dialog first-thing-in-the-morning when the user just wants to start work is the wrong tradeoff.
- **The 4-hour `idle_autostopped` path stays.** Sleep detection only fires on clock jumps; if the daemon is awake and the user is foreground-idle for 4+ hours (laptop on, user away), `idle_autostopped` is the safety net.

## 3. Architecture

The work fits into the existing modules — no new top-level concepts.

```
Sleep + recovery (correctness)
├─ tracker/idle.py        : extend the poll loop to detect wall-clock jumps
├─ tracker/runner.py      : add startup recovery for orphaned active entries
├─ storage/time_entries   : new columns reconciliation_reason, pending_reconciliation
└─ cli/reconcile.py       : NEW — interactive command to resolve pending entries

Git switching (ergonomics)
├─ cli/tracking.py        : add `tsh switch --from-branch` flag
└─ cli/hook.py            : NEW — `tsh hook install/uninstall/status`

Background launch (UX)
├─ cli/tray.py            : add `--detach` (re-spawn via pythonw, no console)
└─ cli/autostart.py       : NEW — `tsh autostart enable/disable/status`
```

## 4. Storage change

Migration `002_reconciliation_reason.sql`:

```sql
ALTER TABLE time_entries ADD COLUMN reconciliation_reason TEXT;
ALTER TABLE time_entries ADD COLUMN pending_reconciliation INTEGER NOT NULL DEFAULT 0;
PRAGMA user_version = 2;
```

The Phase-6 `pending_reconciliation` was an in-memory field on `TrackerState`. Persisting it is necessary because the sleep case puts a record into the pending queue *before* the daemon is even running again (see startup recovery, §5.2).

`reconciliation_reason` is a free-text column populated with one of:
- `'sleep'` — clock-jump observed in the idle loop (§5.1).
- `'orphaned_active'` — stale active entry found on daemon startup (§5.2).

Note: routine `pending → clear` idle returns are NOT persisted as pending reconciliations under the no-GUI design — they auto-accept as work, since the user is actively at the keyboard when they return and has the chance to manually `tsh stop` or `tsh switch` if the gap should be classified differently. Only the two cases where the user was demonstrably NOT at the keyboard (machine sleep, daemon-killed-mid-session) get queued for `tsh reconcile`.

`NULL` means "not pending" (combined with `pending_reconciliation = 0`).

Two new repository functions in `storage/time_entries.py`:

- `list_pending_reconciliation(conn) -> list[TimeEntry]` — returns rows where `pending_reconciliation = 1`, ordered by `start_at` DESC.
- `count_pending_reconciliation(conn) -> int` — used by the warning banner on `tsh status` / `tsh start` / `tsh switch`. Separate from `list_` to avoid hydrating full rows for the common no-pending case.

The existing `update()` allowlist (§Phase-3 storage spec) gains both new columns so they can be updated through the same path.

## 5. Sleep detection and recovery

### 5.1 Sleep detection in the idle loop

In `tracker/idle.py`, at the top of every poll iteration:

1. Capture `now = time.time()` and `last_input_ts = GetLastInputInfo()`.
2. Compute `delta = now - prev_now` where `prev_now` is the previous iteration's timestamp.
3. Compute `sleep_threshold = max(3 * poll_interval_seconds, 90s)`.
4. If `delta < sleep_threshold`: normal tick. Run existing idle state machine. Update `prev_now`, `prev_last_input_ts`.
5. If `delta >= sleep_threshold`: treat as sleep.

On a sleep event:

- **Active timer running**: close the entry at `prev_last_input_ts` (the last input observation from *before* the gap, not `now`). Write `reconciliation_reason = 'sleep'`, `pending_reconciliation = 1`. Log: `"sleep detected: closed active SFXS-1234 at <iso>; queued for reconciliation"`.
- **No active timer**: resync state. No flag set. No log line (avoid log noise on routine sleep/wake cycles when no timer was running).
- **Active timer already in `idle_pending` state when sleep happened**: don't double-flag. Treat the sleep as a continuation of the pending window; update `reconciliation_reason` from `'idle'` to `'sleep'` (the sleep signal is more specific) and use the same close timestamp logic.

#### Why not Win32 power events

`pywin32`'s `WTSRegisterSessionNotification` / `PowerModeChanged` would give an explicit "we slept" signal, but they require a hidden window message pump to receive Win32 messages, which is significant glue in an asyncio context. `time.time()` jump is observable by the loop itself with no IPC. A fake-clock test fixture is one parameter to the poll function; a power-event test would need to mock the Win32 callback chain.

### 5.2 Stale-entry recovery on daemon startup

In `tracker/runner.py`, *before* opening the HTTP port:

1. Open the DB, call `time_entries.get_active()`.
2. If none: normal startup.
3. If found and `now - start_at < stale_active_threshold_minutes` (default 120m, configurable via `time.stale_active_threshold_minutes`): leave alone — this is a normal restart of a daemon that was killed seconds ago.
4. If found and older than the threshold: close it. Set `end_at = start_at + idle_threshold_minutes` (best-effort: that's when idle would have first triggered had the daemon been running). Set `reconciliation_reason = 'orphaned_active'`, `pending_reconciliation = 1`. Log: `"recovered orphaned active SFXS-1234 (started 18h ago) — closed at <iso>; queued for reconciliation"`.

This covers two cases the in-loop sleep detection misses:
- Daemon was killed before sleep happened — no clock-jump observation exists.
- Machine force-shutdown or crashed mid-active.

### 5.3 `tsh reconcile`

New module `cli/reconcile.py`. Interactive walk-through of pending entries:

```
$ tsh reconcile
2 pending reconciliations:

[1] SFXS-1234 — sleep — started 2026-05-14 22:15, closed 2026-05-14 22:42 (last input)
    Gap: 22:42 → 09:08 next morning (10h 26m)
    Was that gap:
      [s] Same work (extend SFXS-1234 through the gap)
      [d] Different ticket (closes SFXS-1234 at 22:42, asks for new ticket + start time)
      [n] Not work (closes at 22:42, gap stays as 'not_work')
      [k] Skip for now
    Choice [s/d/n/k]: n

[2] SFXS-5678 — orphaned_active — started 2026-05-13 14:00, closed 2026-05-13 14:10
    ...
```

Implementation:
- Iterates `time_entries.list_pending_reconciliation()` newest-first.
- Per entry, prints context based on `reconciliation_reason` (sleep / idle / orphaned_active framings differ slightly).
- Prompts via `click.prompt`; on a valid choice, calls `core.reconcile.reconcile_idle()` with the entry + gap window + choice, persists the resulting entry list via `time_entries`, clears the flag on the original.
- `[k] Skip` leaves the flag set; the entry resurfaces next run.
- `tsh reconcile --json` prints pending cases as JSON, no prompts (scripting hook).
- `tsh reconcile <id>` resolves just one entry by id.

### 5.4 Warnings on adjacent commands

`tsh status`, `tsh start`, `tsh switch` gain a one-line warning printed before their normal output when `count_pending_reconciliation() > 0`:

```
⚠ 2 pending reconciliations — run `tsh reconcile`
```

Informational only — doesn't block the action.

### 5.5 Endpoint consistency

The existing `POST /idle/return` endpoint (originally intended for the now-dropped GUI modal) is reworked to write `reconciliation_reason` and clear `pending_reconciliation` in storage, so HTTP-driven and CLI-driven resolution produce the same state. The endpoint stays in place for future tooling integration.

## 6. Git-driven switching

### 6.1 `tsh switch --from-branch` primitive

Extends `cli/tracking.py`. The CLI grows a `--from-branch` flag on the existing `switch` subcommand.

Flow:
1. If `--from-branch` is set and no positional ticket is given:
2. `branch = subprocess.run(['git', 'rev-parse', '--abbrev-ref', 'HEAD'], cwd=os.getcwd(), capture_output=True).stdout.strip()`, wrapped in `try/except`.
3. If git not on PATH, not in a repo, or HEAD detached → exit 0 silently (the hook fires in non-git directories too; we don't want noise).
4. Apply `git.branch_pattern` regex (default `[A-Z][A-Z0-9_]+-\d+`, case-sensitive, configurable) to the branch name; take the first match.
5. No match → exit 0, single stderr line: `"no ticket in branch '<name>'"`. Not an error: branches like `chore/cleanup` or `release/2026-05-15` are normal.
6. Match found → check active timer via `GET /status`.
   - If the active ticket already equals the parsed ticket → exit 0 silently (idempotency short-circuit; prevents every checkout from creating zero-second slivers).
   - Otherwise → `POST /switch {ticket_key, note: None}`.
7. Daemon not running → exit 0 with stderr `"tracker not running — start with \`tsh tray\`"`.

The flag works with or without a positional ticket: `tsh switch SFXS-1234` continues to work unchanged. `--from-branch` without a positional is the new combination.

### 6.2 Config addition

In `config/loader.py`, new section:

```toml
[git]
branch_pattern = "[A-Z][A-Z0-9_]+-\\d+"
```

Case-sensitive on purpose. Override per-project by editing `~/.tsh/config.toml` (no per-repo config — branch conventions tend to be org-wide).

### 6.3 `tsh hook install / uninstall / status`

New module `cli/hook.py`.

**`tsh hook install [--force]`**

1. Discover the current repo's `.git` directory via `git rev-parse --git-dir`. Refuses with non-zero exit if not in a repo.
2. Target path: `<.git>/hooks/post-checkout`.
3. If the target already exists with content unlike ours: refuse with non-zero exit and print the conflicting contents — unless `--force`, which overwrites.
4. Write the hook:

   ```bash
   #!/bin/sh
   # tsh-managed post-checkout hook — do not edit; manage with `tsh hook install/uninstall`
   # Args: $1=prev_HEAD $2=new_HEAD $3=branch_checkout_flag (1 if branch)
   [ "$3" = "1" ] || exit 0
   tsh switch --from-branch >/dev/null 2>&1
   exit 0
   ```

5. Set executable bit via `os.chmod(path, 0o755)`. Git for Windows / WSL honor the `#!/bin/sh` shebang.

The `[ "$3" = "1" ]` guard filters out post-checkout invocations from `git checkout <file>` (file checkout, not branch switch).

**`tsh hook uninstall`**

Reads the hook. If the sentinel comment (`# tsh-managed post-checkout hook`) is present, removes the file. Otherwise refuses — don't clobber a user-edited hook.

**`tsh hook status`**

Prints one of: `installed`, `not installed`, `conflicting hook present (run \`tsh hook install --force\` to replace)`.

### 6.4 Explicit non-goals for §6

- No global `core.hooksPath` configuration. Per-repo opt-in only.
- No zsh / bash / fish wrapper code in this repo. A user wanting their `gco` alias to also fire outside hook-installed repos can drop a shell function in their dotfiles; not a supported feature.
- No commit-message or branch-description ticket extraction. Branch name only.

## 7. Background launch and autostart

### 7.1 `tsh tray --detach`

`cli/tray.py` gains a `--detach` flag. When set:

1. Locate the windowless interpreter:
   - Dev mode: `pythonw_exe = Path(sys.executable).with_name('pythonw.exe')`.
   - Frozen (Phase 9): use the sibling `tsh-tray.exe` produced by PyInstaller `--noconsole`. Selected via `if getattr(sys, 'frozen', False): ...`.
2. Spawn the child:
   ```python
   subprocess.Popen(
       [windowless_exe, '-m', 'tsh', 'tray'],
       creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
       close_fds=True,
       cwd=os.getcwd(),
   )
   ```
3. Parent exits 0 once the child PID exists.

The child is plain `tsh tray` — same code path as today's foreground tray. The existing "tracker already running on port" check still applies and short-circuits if the daemon is already up; the parent prints `"tracker already running"` to stderr and exits 0.

### 7.2 Logging

Detached processes have no usable stdout/stderr. Add a rotating log file:

- Path: `~/.tsh/logs/tsh-tray.log`.
- `logging.handlers.RotatingFileHandler`, 1 MB per file, 3 backups.
- Initialised in `tracker/runner.py` startup when `sys.stdout` is not a TTY, when `--detach` was used, or when running frozen.
- INFO level by default; `tsh tray --detach --debug` bumps to DEBUG.
- Foreground `tsh tray` (no `--detach`) keeps printing to stdout as today; logging is additive, not replacement.

### 7.3 `tsh autostart enable / disable / status`

New module `cli/autostart.py`.

**Shortcut path:**
`Path(os.environ['APPDATA']) / 'Microsoft' / 'Windows' / 'Start Menu' / 'Programs' / 'Startup' / 'tsh.lnk'`

**`tsh autostart enable`**

1. Build the shortcut via `pywin32`:
   ```python
   shell = win32com.client.Dispatch('WScript.Shell')
   shortcut = shell.CreateShortcut(str(shortcut_path))
   shortcut.TargetPath = str(windowless_exe)        # pythonw.exe or tsh-tray.exe
   shortcut.Arguments = '-m tsh tray'                # empty for frozen exe
   shortcut.WorkingDirectory = str(Path.home())      # don't bind to dev checkout
   shortcut.IconLocation = str(tray_icon_path)
   shortcut.Description = 'Timesheet Helper background tracker'
   shortcut.save()
   ```
2. After writing, internally invoke `tsh tray --detach` (subprocess) so the user gets the icon immediately, not just at next login.
3. Idempotent: existing shortcut with the right TargetPath → no overwrite. With a different TargetPath → overwrite (handles upgrades from dev mode to frozen, or path changes).

**`tsh autostart disable`**

Removes the shortcut only if its TargetPath sentinel matches ours (don't delete a foreign `tsh.lnk` we didn't create). Does not kill a running daemon — use `tsh quit` for that.

**`tsh autostart status`**

Prints three lines:

```
Startup shortcut: enabled
Daemon: running (PID 1234)
Run on next login: yes
```

### 7.4 Why Startup folder, not Task Scheduler or Windows Service

- **Startup folder** is the standard Windows mechanism for "auto-launch at login" — what Slack, OneDrive, Discord use. User-mode, no special permissions. Visible in Task Manager → Startup, where the user can disable it independently of `tsh autostart disable`.
- **Task Scheduler** is more robust (restart on failure, run-at-times), but the registration is more complex (`schtasks` or COM), and the scheduled task is hidden from Task Manager's Startup tab — worse discoverability for the user.
- **Windows Service** stays out of scope (echoes the original spec §13). Requires admin install, pywin32 service wrapper, and runs even when no one is logged in — wrong shape for a personal time tracker.

## 8. Documentation updates

`docs/dogfood-cheatsheet.md` gains three new sections:

- **Background install (one-time)**: `tsh autostart enable`, what `tsh autostart status` shows, how to disable.
- **Git auto-switch (per-repo)**: `cd <project> && tsh hook install`, what the hook does, what the no-match stderr means, how to override the branch regex.
- **Sleep / overnight gaps**: after a sleep, `tsh status` shows `⚠ N pending reconciliations`; run `tsh reconcile` to walk through them.

The Phase 6 idle-detection row in the watch-outs table is rewritten to reflect that `pending_reconciliation` now has a CLI surface.

## 9. Testing approach

### Unit / integration tests (continue TDD pattern from Phases 1–5)

- **Idle loop with fake clock + fake input provider** (`tests/tracker/test_idle.py` extensions):
  - 30s tick → no sleep handling, normal idle state machine.
  - 8h tick with active timer → entry closed at `prev_last_input_ts`, `reason='sleep'`, flag set. Crucially: `end_at != now`.
  - 8h tick with no active timer → no-op, no flag.
  - 8h tick when entry was already in `idle_pending` → don't double-flag; reason updated to `'sleep'`.
- **Stale-entry recovery** (`tests/tracker/test_runner.py`):
  - Fixture inserts 18h-old active entry; runner closes it with `end_at = start + idle_threshold`, `reason='orphaned_active'`, flag set.
  - 30m-old active entry → left alone.
- **`tsh reconcile`** (`tests/cli/test_reconcile.py`, new): `CliRunner`-driven, covers each reason × each choice (sleep × {same, different, not_work, skip}; orphaned × {same, different, not_work, skip}; idle × {same, different, not_work, skip}).
- **`tsh reconcile --json`**: round-trips pending list as JSON, no prompts invoked.
- **Warning surfacing**: `tsh status` / `tsh start` / `tsh switch` show the warning when count > 0, suppress when 0.
- **`tsh switch --from-branch`** (`tests/cli/test_tracking.py` extensions): parametrised branch names — `feature/SFXS-1234-fix`, `SFXS-1234`, `feature/SFXS-1234/SFXS-5678-rebase` (first wins → `SFXS-1234`), `sfxs-1234` (no match), `chore/cleanup` (no match), `release/2026-05-15` (no match). `git rev-parse` subprocess and `httpx` client both mocked.
- **Idempotency**: active ticket == parsed ticket → no HTTP call (assert via mock count).
- **`tsh hook install/uninstall/status`** (`tests/cli/test_hook.py`, new) against a `pytest.tempdir`-built throwaway git repo: clean install, conflict refusal, `--force` overwrite, idempotent re-install, uninstall-only-if-ours.
- **`tsh tray --detach`** (`tests/cli/test_tray_cmd.py` extensions): `subprocess.Popen` mocked; assert windowless executable, `CREATE_NO_WINDOW`, correct args.
- **`tsh autostart enable/disable/status`** (`tests/cli/test_autostart.py`, new): `win32com.client.Dispatch` mocked; assert TargetPath, Arguments, WorkingDirectory on the constructed shortcut. Enable when shortcut exists with same target → no overwrite; different target → overwrite. Disable removes only if our sentinel matches.

### Manual / dogfood verification

After implementation, the test plan in `docs/test-plan.md` (Phase 9 deliverable) will cover:

- With an active timer, sleep the laptop overnight. On wake: `tsh status` shows ≥ 1 pending reconciliation; `tsh review` shows the entry closed at last-input (not at wake).
- Insert a 24h-old active entry directly into the DB; restart the tracker; confirm recovery log line and DB state.
- `tsh reconcile` walks each pending case interactively; each branch (same / different / not work) produces correct downstream entries.
- `tsh hook install` in a real project repo; `git checkout <branch-with-ticket>`; `tsh status` shows the new ticket within a second.
- `git checkout <branch-without-ticket>` produces no visible noise, only quiet stderr.
- `tsh tray --detach` returns the prompt immediately; tray icon visible; no console window.
- `tsh autostart enable`; reboot; tray appears at login.

## 10. Out of scope for this spec

- GUI work (was Phase 7). Dropped in the 2026-05-15 plan revision.
- Windows Service for background-while-logged-out (stays out of scope per original spec §13).
- Global `core.hooksPath` git configuration or shell-wrapper installation.
- Ticket extraction from commit messages, branch descriptions, or PR titles.
- Win32 power-event signal source (clock-jump heuristic deemed sufficient).
- Reconciliation UI beyond `click.prompt` — no TUI, no curses, no fzf integration.
- Auto-restart of the daemon on crash (could be a Task Scheduler addition later, but currently relies on the user re-running `tsh tray` after a hard crash).

## 11. Open questions deferred to implementation

- **Sleep-vs-idle threshold** (§5.1): `max(3 × poll_interval, 90s)` is a heuristic. If false positives appear (heavy CPU pauses on a slow battery), bump to `5 ×` or introduce `time.sleep_detection_seconds` config.
- **Stale-on-startup cutoff** (§5.2): default 2h. Make configurable via `time.stale_active_threshold_minutes` from day one if dogfooding shows 2h is wrong.
- **Branch regex case sensitivity** (§6.2): case-sensitive by default. If the user's org adopts mixed-case ticket conventions, flip the default or add a separate `git.branch_pattern_flags` config knob.

## 12. Relation to the plan

This spec is the design backing the revised Phase 7 (daemon hardening) and Phase 8 (git switching) in `docs/plans/2026-04-27-timesheet-helper.md`. The plan's task list (20–26) is the executable breakdown of this spec. Phase 9 (PyInstaller packaging) is unchanged by this spec but depends on §7's `tsh-tray.exe` build target.
