# Dogfood cheat-sheet — 2026-04-27

Phases 1-6 of the plan are merged on `master`. The CLI works end-to-end (manual log + push) and the live-timer daemon (`tsh tray`) works. The GUI is **not built yet** — interactions are CLI-only or via the system tray menu.

## Set up

```powershell
cd "C:\Users\Casey Luo\Documents\SWS\timesheet_helper"
uv sync --all-extras                     # one-time / after pulling new commits
uv run pytest -q                         # confirm 311 still pass
```

`uv run <cmd>` invokes the project venv automatically. All `tsh` commands below
can be prefixed with `uv run` (e.g. `uv run tsh status`) instead of activating
the venv. If you prefer to activate, `.\.venv\Scripts\activate` still works.

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

### B. Live timer (Phase 6 — new, untested in real use)

In **terminal 1** (this one blocks):

```powershell
tsh tray                                 # daemon starts, tray icon appears
                                         # leave this terminal open
```

In **terminal 2** (or three):

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
- **Switch task...** — currently a no-op log line (GUI is Phase 7; until then, use `tsh switch` from a terminal)
- **Pause** — same as `tsh stop`
- **Show main window** — no-op for now (GUI is Phase 7)
- **Quit** — graceful shutdown

## Idle detection (background)

- Every 30s the daemon checks Win32 `GetLastInputInfo`
- If you've been idle ≥ 10m AND have an active timer → state goes to `pending`
- When you return → state clears + `pending_reconciliation = True`
- **No reconciliation modal exists yet** (Phase 7 territory). For now the flag is set and not surfaced; the active timer keeps running through the gap. If you want to test idle behaviour, you'll see it in the JSON: `tsh status --json` shows `idle_status` and `idle_started_at`.
- If you go idle past 4h (default `time.max_idle_minutes_before_autostop`) the timer auto-stops at the moment idle started. You'll see a closed entry in `tsh review` ending at that time.

To test idle behaviour faster:

```powershell
tsh quit                                 # stop the daemon first
tsh config set time.idle_threshold_minutes 1
tsh tray                                 # restart with the new threshold
# now any 1-minute idle period flips state to pending
tsh config set time.idle_threshold_minutes 10   # restore default after testing
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
| Manual log says "no active timer" but tray claims one | Tracker's in-memory state went stale — restart it. Will be fixed when 60s flush + restart-on-launch logic ships. |

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

Or just file the issue — there are 19 commits, each fairly self-contained.

## Where to resume building

`docs/plans/2026-04-27-timesheet-helper.md` has the canonical progress and next-task pointer. Phase 7 is the GUI (4 tasks) — the reconciliation modal lands there, plus the today view, picker popover, and review/push UI. Phase 8 is packaging (`pyinstaller` to a single `.exe`).
