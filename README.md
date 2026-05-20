# tsh — Timesheet Helper

A lightweight Windows CLI + tray app for tracking time against Jira tickets
in 15-minute blocks. Log work manually or via a live timer, then push
worklogs to Jira at end of day.

See `docs/superpowers/specs/2026-04-27-timesheet-helper-design.md` for the
design spec and `docs/plans/2026-04-27-timesheet-helper.md` for the
implementation history.

## Install

Pick **one** of the two install routes below. The first is what you want
unless you plan to hack on tsh itself.

### Option A — Install system-wide (recommended)

`uv tool install` puts `tsh.exe` in `%USERPROFILE%\.local\bin` (on user
PATH), so the command works from **any** terminal, from GUI git clients
(GitLens, SourceTree, VS Code Git), and from the Windows Startup shortcut
— no activated venv required.

```powershell
# Install uv once if you don't have it:
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# Then from a fresh terminal (or one where `uv` is already on PATH):
cd "C:\path\to\timesheet_helper"
uv tool install --editable .             # tsh.exe → %USERPROFILE%\.local\bin
uv tool update-shell                     # one-time: adds ~\.local\bin to PATH if missing
```

Open a **new** terminal after `update-shell` so the PATH change takes
effect, then verify:

```powershell
where.exe tsh                            # should print ...\.local\bin\tsh.exe
tsh --version
```

`--editable` means `git pull` is enough to upgrade — no re-install needed.
To remove later: `uv tool uninstall tsh`.

### Option B — Dev checkout (only if you're working on tsh itself)

```powershell
cd "C:\path\to\timesheet_helper"
uv sync --all-extras                     # one-time / after pulling new commits
uv run pytest -q                         # confirm tests pass
```

`uv run <cmd>` invokes the project venv automatically. All `tsh` commands
below can be prefixed with `uv run` (e.g. `uv run tsh status`) instead of
activating the venv. Heads-up: in this mode `tsh` is **not** on the
system PATH — GUI git clients won't find it, and the git hook needs you
to also do `uv tool install --editable .` (Option A) for branch-switching
to work outside a venv terminal.

## One-time auth

### Step 1 — get a Jira API token

1. Go to **https://id.atlassian.com/manage-profile/security/api-tokens**
   (sign in with your Atlassian account if prompted).
2. Click one of:
   - **Create API token** — classic, full-account token. Simplest option.
   - **Create API token with scopes** — recommended, least-privilege.
     - Give it a **Name** (e.g. `tsh-timesheet-helper`).
     - Pick an **Expiration date** (max 365 days — calendar a renewal).
     - **App:** select **Jira**.
     - **Scopes:** tick exactly these three classic scopes and nothing
       else (tsh only ever calls `/myself`, `/search`, `/issue/{key}`,
       and `POST /issue/{key}/worklog`):

       | Scope             | Used by tsh for                                |
       |-------------------|------------------------------------------------|
       | `read:jira-user`  | `tsh auth test` (calls `/rest/api/3/myself`)   |
       | `read:jira-work`  | `tsh tasks`, `tsh start <KEY>` (search + issue)|
       | `write:jira-work` | `tsh push` (creates worklogs)                  |

3. Click **Create**, then **Copy** the token immediately — Atlassian
   only shows it once. Paste it somewhere safe for the next step.

### Step 2 — log in

```powershell
tsh auth login                           # prompts for base URL, email, API token
tsh auth test                            # expect: "Authenticated as <your name>"
```

If `tsh auth test` returns **401 Unauthorized**, the token's scopes are
wrong — the most common miss is forgetting `read:jira-user`, since that
gates `/myself` (which is the first call `tsh auth test` makes). Mint a
new token with all three scopes above.

Token goes to Windows Credential Manager (service `tsh-jira`); base URL + email go to `~/.tsh/config.toml`.

## Background tracker (autostart)

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

### Hook doesn't fire from GitLens / SourceTree / VS Code Git?

If the hook is installed (`tsh hook status` confirms) and switching from
the **terminal** works but switching via **GitLens** or another GUI git
client doesn't move the timer, it's almost always one of:

1. **`tsh` isn't on PATH for the GUI's process.** GUI clients inherit the
   user PATH, not your venv's PATH. If you installed via Option B
   (`uv sync`), `tsh.exe` lives only inside `.venv\Scripts` and GitLens
   can't see it. **Fix:** also run `uv tool install --editable .` (Option A
   above), then restart the GUI client so it picks up the new PATH.
2. **The tracker daemon isn't running.** The hook calls `tsh switch
   --from-branch`, which exits silently when the daemon is down. Confirm
   with `tsh autostart status`.

Quick sanity check: open a terminal at the repo root and run
`./.git/hooks/post-checkout '' '' 1` — that emulates what git invokes.
If it errors with "tsh: command not found", you're in case 1.

## Two ways to log time

### A. Manual log + end-of-day push

```powershell
tsh log SFXS-XXXX 30m -m "fixed bug"     # records a finished entry
tsh log SFXS-XXXX 1h30m -m "code review" # 1h30m, 45m, 2h all parse
tsh review                               # see today's table with raw + rounded
tsh edit <id>                            # opens entry in $EDITOR as TOML
tsh delete <id>                          # only works on draft entries

tsh push --dry-run                       # MUST: verify started has +1100/+1000
tsh push                                 # real push to Jira
```

### B. Live timer

Start the daemon (or rely on autostart — see "Background tracker" above):

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

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `tsh push` puts entry on the **wrong day** in Jira | Timezone bug in `to_jira_started`. Capture `tsh push --dry-run` output. |
| `tsh push` succeeds but Jira shows wrong **time** | Same — `started` field has wrong offset. |
| Wrong rounded total | Check `tsh config get time.rounding` (default `nearest`) and `time.rounding_minutes` (default `15`). |
| `tsh auth test` fails with 401 | Token expired or missing scopes. Re-run `tsh auth login` with a token that has `read:jira-user` + `read:jira-work` + `write:jira-work`. |
| `tsh auth test` fails with "not configured" | Check `tsh config get jira.base_url` doesn't have trailing slash. |
| `tsh tray` says "tracker already running" but it isn't | Stale daemon on port 42024 — `tsh quit`, or change port via `tsh config set app.http_port <n>`. |
| Tray icon doesn't appear on Windows | Pillow/pystray issue. The HTTP API still works. |
| `tsh status` shows `⚠ N pending reconciliations` | Daemon detected idle return, machine sleep, or recovered an orphaned timer. Run `tsh reconcile`. |
| GitLens / SourceTree branch switch doesn't move timer | See "Hook doesn't fire from GitLens / SourceTree / VS Code Git?" above. |

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

## Development

This project uses [uv](https://docs.astral.sh/uv/) for dependency and venv management.
After Option B above, the common workflows are:

```powershell
uv run pytest -q           # run the full test suite
uv run tsh --help          # invoke the CLI from the source tree

uv add <package>           # add a runtime dependency (writes pyproject.toml + uv.lock + installs)
uv add --dev <package>     # add a dev-only dependency
uv remove <package>        # remove a dependency
uv lock                    # regenerate uv.lock after editing pyproject.toml by hand
```

The repo ships `pyproject.toml` and `uv.lock`. Both are checked in for reproducible installs.
