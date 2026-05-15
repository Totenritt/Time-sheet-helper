# Dogfood cheat-sheet — 2026-05-15

Phases 1–8 of the plan are merged on `master`. The CLI works end-to-end (manual log + push), the live-timer daemon (`tsh tray`) works, autostart and git-hook switching ship in Phase 8, and sleep/idle reconciliation is fully surfaced. The GUI was dropped in the Phase 7–8 revision — interactions are CLI-only or via the system tray menu.

## Set up

```powershell
cd "C:\Users\Casey Luo\Documents\SWS\timesheet_helper"
uv sync --all-extras                     # one-time / after pulling new commits
uv run pytest -q                         # confirm 374 still pass
```

`uv run <cmd>` invokes the project venv automatically. All `tsh` commands below
can be prefixed with `uv run` (e.g. `uv run tsh status`) instead of activating
the venv. If you prefer to activate, `.\.venv\Scripts\activate` still works.

## Background install (one-time)

Make the tracker behave like a native Windows app — start at login, no terminal needed:

```powershell
tsh autostart enable                     # writes Startup shortcut + starts daemon now
tsh autostart status                     # confirm enabled + daemon running
tsh autostart disable                    # removes the shortcut (leaves a running daemon alone)
```

After `enable`, the tray icon appears on every login automatically — no terminal needed. To stop a running daemon, use `tsh quit`.

For headless launch in a single session (no autostart):

```powershell
tsh tray --detach                        # spawns via pythonw.exe, no console window, exits parent
```

## Git auto-switch (per-repo)

Install a git hook so `git checkout` (or your `gco` alias) auto-switches the timer when a branch name contains a Jira ticket key:

```powershell
cd C:\path\to\some-project
tsh hook install                         # writes .git/hooks/post-checkout
tsh hook status                          # confirm
```

After install, `git checkout feature/SFXS-1234-fix-bug` silently calls `tsh switch SFXS-1234`. Branches without a matching ticket key (e.g. `chore/cleanup`) produce a single stderr line and do nothing — the hook never blocks checkout.

Override the regex for projects with a different ticket convention:

```powershell
tsh config set git.branch_pattern '<your-regex>'
```

Manual switching (`tsh switch SFXS-9999`) still works in repos that don't have the hook installed.

## One-time auth (skip if already done)

```powershell
tsh auth login                           # prompts for base URL, email, API token
tsh auth test                            # expect: "Authenticated as <your name>"
```

Token goes to Windows Credential Manager (service `tsh-jira`); base URL + email go to `~/.tsh/config.toml`.

## Two ways to log time

### A. Manual log + end-of-day push (Phase 5 — proven)

```powershell
tsh log SFXS-XXXX 30m -m "fixed bug"     # records a finished entry
tsh log SFXS-XXXX 1h30m -m "code review" # 1h30m, 45m, 2h all parse
tsh review                               # see today's table with raw + rounded
tsh edit <id>                            # opens entry in $EDITOR as TOML
tsh delete <id>                          # only works on draft entries

tsh push --dry-run                       # MUST: verify started has +1100/+1000
tsh push                                 # real push to Jira
```

### B. Live timer (Phase 6+)

Start the daemon (or rely on autostart — see "Background install" above):

```powershell
tsh tray --detach                        # headless: no console window
```

In a terminal:

```powershell
tsh start SFXS-1073 -m "auth refactor"
tsh status                               # SFXS-1073: 30s elapsed (idle: clear)
tsh switch SFXS-1234 -m "helping colleague"
tsh tasks                                # in-progress (from Jira) + recents
tsh stop                                 # close current
```

End of day, same as A:

```powershell
tsh review                               # see what the daemon recorded
tsh push --dry-run                       # verify
tsh push                                 # ship it
```

Stop the daemon when done:

```powershell
tsh quit                                 # or right-click tray icon → Quit
```

## What the tray menu does

- **(top, greyed out):** current task + elapsed (`tsh — SFXS-1073 — 1h 15m`)
- **Switch task...** — use `tsh switch` from a terminal
- **Pause** — same as `tsh stop`
- **Quit** — graceful shutdown

## Idle / sleep / overnight gaps

The daemon polls `GetLastInputInfo` every 30s plus watches for wall-clock jumps (machine sleep / hibernate). It also detects orphaned active timers on startup (e.g. from a previous session that crashed).

In all three cases, the affected entry is closed at the user's last keyboard/mouse input before the gap and flagged for reconciliation. On next interaction you'll see:

```
⚠ 1 pending reconciliation — run `tsh reconcile`
```

Walk through it interactively:

```powershell
tsh reconcile                            # walk all pending entries
tsh reconcile <id>                       # resolve a single entry
tsh reconcile --json                     # list pending as JSON, no prompts (for scripts)
```

For each entry you choose:
- `[s]` Same work — extend the original entry through the gap
- `[d]` Different ticket — close original, log the gap separately
- `[n]` Not work — close original, mark the gap as non-work
- `[k]` Skip — leave the flag set; resurfaces next run

To test idle detection faster:

```powershell
tsh quit                                 # stop daemon first
tsh config set time.idle_threshold_minutes 1
tsh tray --detach                        # restart with the new threshold
tsh config set time.idle_threshold_minutes 10   # restore after testing
```

## Watch-outs (things to flag if they go wrong)

| Symptom | Likely cause |
|---|---|
| `tsh push` puts entry on the **wrong day** in Jira | Timezone bug in `to_jira_started`. Capture `tsh push --dry-run` output. |
| `tsh push` succeeds but Jira shows wrong **time** | Same — `started` field has wrong offset. |
| Wrong rounded total | Check `tsh config get time.rounding` (default `nearest`) and `time.rounding_minutes` (default `15`). |
| `tsh auth test` fails with 401 | Token expired. Run `tsh auth login` again. |
| `tsh auth test` fails with "not configured" | Check `tsh config get jira.base_url` doesn't have trailing slash. |
| `tsh tray` says "tracker already running" but it isn't | Stale daemon on port 42024 — `tsh quit`, or change port via `tsh config set app.http_port <n>`. |
| Tray icon doesn't appear on Windows | Pillow/pystray issue. The HTTP API still works. |
| `tsh status` shows `⚠ N pending reconciliations` | Daemon detected idle return, machine sleep, or recovered an orphaned timer. Run `tsh reconcile`. |

## Where state lives

- **Time entries + ticket cache:** `~/.tsh/tsh.db` (SQLite)
- **Config:** `~/.tsh/config.toml` (inspectable / editable)
- **API token:** Windows Credential Manager, service `tsh-jira`, account = your Jira email

## Inspecting state

```powershell
tsh config get jira.base_url
tsh config get time.idle_threshold_minutes
tsh status --json                        # current active + idle state
tsh tasks --json                         # picker contents
tsh review --day yesterday --json        # raw entry data
tsh reconcile --json                     # pending reconciliation entries (if any)
```

To browse the DB directly:

```powershell
sqlite3 $env:USERPROFILE\.tsh\tsh.db ".tables"
sqlite3 $env:USERPROFILE\.tsh\tsh.db "SELECT id, ticket_key, start_at, end_at, kind, pushed_at FROM time_entries ORDER BY start_at DESC LIMIT 20;"
```

## When you find bugs

The work is on `master` with one commit per task and no force-pushes. To roll back any single change:

```powershell
git log --oneline                        # find the bad commit
git revert <sha>                         # creates a new revert commit
```

Or just file the issue — each commit is fairly self-contained.

## Where to resume building

Phases 7 and 8 (daemon hardening + git switching) shipped on 2026-05-15. The GUI was dropped in the same revision — see `docs/superpowers/specs/2026-05-15-headless-daemon-improvements-design.md` for that decision.

Phase 9 (PyInstaller packaging — single-binary build for `tsh.exe` + `tsh-tray.exe`) is the remaining work. See `docs/plans/2026-05-15-headless-daemon-improvements.md` for the implementation breakdown.
