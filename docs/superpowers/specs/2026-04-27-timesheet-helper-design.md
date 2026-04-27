# Timesheet Helper — Design Spec

**Date:** 2026-04-27
**Status:** Approved for planning
**Owner:** Casey Luo

## 1. Goal

A lightweight desktop application for Windows that makes it nearly effortless to log working time to Jira in 15-minute blocks. The user logs time continuously through the day with minimal interruption, reviews and edits at end of day, then pushes the day's entries to Jira in a single batch.

## 2. Constraints and principles

- **Lightweight** — small footprint, no heavyweight services, fast to start.
- **Safety first for Jira data** — local drafts; explicit, reviewable push; dry-run available; never modify or delete existing Jira worklogs.
- **GUI primary, CLI alternative** — both interfaces drive the same engine.
- **Set-and-forget tracking** — once a task is selected, the timer runs until the user explicitly switches or stops, with idle detection as the safety net.
- **Single user, single machine** — no multi-user, no sync.
- **MVP first** — Jira API token can be added later for live testing.

## 3. Architecture

One application, two front-ends, one source of truth.

```
+----------------------------------------------------+
|  Tracker (the long-running app)                    |
|  - System tray icon (status: idle / SFXS-1073 ...) |
|  - pywebview window (HTML/CSS/JS) -- the GUI       |
|  - Idle-detection loop (polls Win32 every ~30s)    |
|  - Local HTTP API on 127.0.0.1:<port>              |
+----------------------------------------------------+
        ^                              ^
        | HTTP (when tracker running)  | direct read/write
        |                              |
+--------------+              +------------------+
|     CLI      |              |  SQLite file      |
|  (`tsh ...`) |------------->|  ~/.tsh/tsh.db    |
+--------------+              +------------------+
                                       ^
                                       |
                              +-------------------+
                              |  Jira REST API    |
                              |  (only on push)   |
                              +-------------------+
```

### 3.1 Process model

- **The Tracker is the GUI app.** No separate daemon, no Windows service. Launching the app starts the tracker; quitting (via tray menu or `tsh quit`) stops it.
- **Closing the GUI window does NOT quit the tracker.** Window close minimizes to tray; the tray icon and timer keep running. Standard Windows tray-app pattern.
- **Background while logged in** is supported. Background across Windows logout is out of scope for MVP (would require a Windows Service).
- **Optional autostart**: `tsh config set app.autostart true` writes a `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` entry.

### 3.2 CLI <-> Tracker behavior

- **Tracker running**: CLI calls the local HTTP API (e.g., `tsh switch SFXS-1073`).
- **Tracker not running**: CLI works on SQLite directly for non-live operations (`tsh log`, `tsh review`, `tsh push`, `tsh edit`). Live-timer commands (`tsh switch`, `tsh start`) error with "tracker not running — start the app first."

### 3.3 State persistence

SQLite is the source of truth. The tracker's in-memory active-timer state is mirrored into the active row in `time_entries` and flushed every 60 seconds. Maximum data loss on crash: ~1 minute.

### 3.4 Module layout

```
tsh/
  core/        # pure logic: timer state, time math, models -- no I/O
  storage/     # SQLite read/write + migrations
  jira/        # Jira API client
  tracker/     # tray icon + idle loop + HTTP server
  gui/         # pywebview-hosted HTML/CSS/JS app
  cli/         # click commands
  config/      # config file + keyring access
```

The `core/` module contains pure functions over data — no I/O, no side effects. This is where most logic lives and where the bulk of unit tests target.

## 4. Tech stack

- **Language**: Python 3.11+
- **CLI**: `click`
- **GUI**: `pywebview` rendering an HTML/CSS/JS single-page app in the OS webview
- **Tray icon**: `pystray`
- **Local HTTP API**: `fastapi` + `uvicorn` (or `flask` — final pick at planning time)
- **Storage**: stdlib `sqlite3`
- **Jira client**: `httpx` (avoids the bulkier `jira` library; we only use four endpoints)
- **Idle detection**: `pywin32` (`GetLastInputInfo`)
- **Credential storage**: `keyring`
- **Distribution**: `pyinstaller` single `.exe`

Rationale: fastest path to MVP. Single language across CLI, engine, GUI logic. Batteries-included libraries for every requirement. If lightweight distribution becomes a hard requirement later, the engine logic ports cleanly to Go (Wails) without redesign.

## 5. Data model

Two tables. Migrations as plain SQL files in `tsh/storage/migrations/`, gated by a `schema_version` PRAGMA. Run pending migrations on startup.

### 5.1 `time_entries`

| column | type | notes |
|---|---|---|
| `id` | INTEGER PK | |
| `ticket_key` | TEXT | e.g. `SFXS-1073`, NULL for non-work entries |
| `start_at` | TIMESTAMP NOT NULL | UTC ISO 8601, second precision |
| `end_at` | TIMESTAMP | NULL = currently active timer |
| `note` | TEXT | becomes the Jira worklog comment |
| `kind` | TEXT NOT NULL | `work` \| `not_work` \| `idle_unresolved` |
| `jira_worklog_id` | TEXT | NULL until pushed |
| `pushed_at` | TIMESTAMP | NULL until pushed (UTC) |
| `created_at`, `updated_at` | TIMESTAMP | bookkeeping (UTC) |

Partial unique index ensures only one active entry at a time:
```sql
CREATE UNIQUE INDEX one_active ON time_entries(kind) WHERE end_at IS NULL;
```

**All timestamps stored in UTC**, ISO 8601 with explicit offset. No naive datetimes ever leave the storage layer.

**Storing actual second-precision timestamps (not 15m blocks):** Editing is easier, no fidelity loss. 15-minute rounding happens only at push time, on a copy.

### 5.2 `tickets_cache`

| column | type | notes |
|---|---|---|
| `ticket_key` | TEXT PK | `SFXS-1073` |
| `summary` | TEXT | for display in picker |
| `status` | TEXT | `In Progress`, `Done`, etc. |
| `assignee_email` | TEXT | for "yours" vs "colleague's" indicator |
| `last_fetched_at` | TIMESTAMP | TTL — refresh after 5 min |
| `last_used_at` | TIMESTAMP | drives the "recents" list |

`last_used_at` is updated to `now()` whenever a `time_entries` row is created (start, switch, or `tsh log`) referencing this ticket — i.e., the moment the user expresses intent to log time against the ticket, regardless of whether that entry is later pushed.

Recents query: `SELECT * FROM tickets_cache ORDER BY last_used_at DESC LIMIT 10`. Picker deduplicates: tickets returned by the in-progress JQL are shown first; recents that aren't already in the in-progress list appear below.

### 5.3 What is NOT in SQLite

- **Config** → `~/.tsh/config.toml` (see Section 9).
- **Jira API token** → Windows Credential Manager via `keyring`. Never on disk in plaintext.
- **Active-timer in-memory state** → mirrored to active SQLite row; flushed every 60s.

### 5.4 How idle handling reshapes data

When the user returns from idle and answers the reconciliation prompt, all changes happen in a single transaction:

- **Same task** → no change. The active row's `end_at` stays NULL.
- **Different task** → close the active row at `idle_started_at`; insert a new entry for the idle period with the chosen ticket; insert a fresh active row for the originally-active task starting at `now()`.
- **Not work** → close the active row at `idle_started_at`; insert a `kind='not_work'` row covering the gap; insert a fresh active row resuming the previous task at `now()`.

## 6. User flows

### 6.1 Start of day

1. Launch app → tracker process starts → tray icon appears → main window opens.
2. Tracker fetches in-progress tickets from Jira (or uses cache if <5min old) and shows them in the picker, with locally-cached recents underneath.
3. User clicks a ticket (or types a key for a colleague's), enters an optional note, clicks Start.
4. New `time_entries` row inserted: `start_at = now()`, `end_at = NULL`, `kind = 'work'`.
5. Tray icon updates to show active task and elapsed time.

If yesterday ended without a manual stop, the launch screen offers "Resume SFXS-1073 (last task)" as one click but never auto-resumes — the user confirms.

### 6.2 Switch task

Three equivalent entry points hit the same `POST /switch` endpoint:
- GUI window: type/click new ticket, hit Switch
- Tray menu: "Switch task..." → mini picker
- CLI: `tsh switch SFXS-1234 "Helping with auth refactor"`

Behavior:
1. Active row gets `end_at = now()`.
2. New row inserted for the new ticket, `start_at = now()`, `end_at = NULL`.
3. Tray icon updates immediately.

### 6.3 Idle and return

1. Tracker polls `GetLastInputInfo()` every 30 seconds.
2. After **idle threshold** (default 10 min, configurable), tracker records `idle_started_at = now() - threshold` and starts watching for return.
3. **If idle exceeds max** (default 4 hours): tracker auto-stops the active timer at `idle_started_at`, sets active row's `end_at`, and surfaces a "you were idle since X — please reconcile" notification on next launch/return.
4. **On return** (input detected after idle threshold but before max): GUI window pops up (or, if minimized, a Windows toast fires) with the **idle reconciliation modal**:

   > "You were idle for 47 minutes (1:13pm → 2:00pm Sydney). What was that?"
   >
   > - **Same task — SFXS-1073** (single click)
   > - **Different task** (inline picker)
   > - **Not work** (lunch, break)

5. After confirmation, the active timer resumes from `now()`.

### 6.4 End-of-day review and push

1. User opens window, scrolls to the review section (or `tsh review`).
2. Today's entries grouped by ticket, with:
   - Raw time (e.g., "1h 7m")
   - Rounded time (e.g., "1h 15m" — preview of what gets pushed)
   - Note (editable inline)
   - Per-row checkbox to include/exclude from push
3. User edits inline; edits commit to SQLite immediately.
4. User clicks **Push to Jira** (or `tsh push`):
   - Confirmation modal shows totals about to be pushed.
   - **Dry run** available as a separate button / `tsh push --dry-run` — prints exact API payloads without firing.
   - On confirm: each entry POSTs to `/rest/api/3/issue/{key}/worklog` with `started`, `timeSpentSeconds` (rounded), `comment`.
   - Successful pushes get `jira_worklog_id` and `pushed_at` set.
   - Failures stay as drafts with an error badge; user can retry.
5. Pushed entries become read-only in the review screen. To fix a pushed entry the user deletes the worklog in Jira's UI; the entry returns to draft.

### 6.5 Edit a past entry

`tsh edit <ID>` opens the entry in `$EDITOR` as TOML. Save and close → SQLite updated. GUI offers the same edit affordance inline. Pushed entries are read-only.

## 7. CLI command set

### 7.1 Daily commands (need tracker running)

| Command | Description |
|---|---|
| `tsh start <TICKET> [-m "note"]` | Start a new active timer. Errors if one is already active. |
| `tsh switch <TICKET> [-m "note"]` | End current timer and start a new one. |
| `tsh stop` | End current timer. |
| `tsh status` | Show current active task, elapsed time, today's totals. |
| `tsh tasks` | Show the picker list (in-progress + recents) as a table. |

### 7.2 Review and push (work whether tracker is running or not)

| Command | Description |
|---|---|
| `tsh review [--day today\|yesterday\|YYYY-MM-DD]` | Print entries for that day. |
| `tsh log <TICKET> <DURATION> [-m "note"] [--at TIME]` | Manually add a finished entry. |
| `tsh edit <ID>` | Open entry in `$EDITOR` as TOML. |
| `tsh delete <ID>` | Delete a draft entry. Pushed entries refuse. |
| `tsh push [--day ...] [--dry-run]` | Push drafts for a day. |

### 7.3 Lifecycle and config

| Command | Description |
|---|---|
| `tsh tray` | Start the tracker. Idempotent. |
| `tsh quit` | Stop the tracker. |
| `tsh config get/set <key> [value]` | Read/write `config.toml` keys. |
| `tsh auth login` | Prompt for Jira email + token; store token in OS keyring. |
| `tsh auth logout` | Remove token from keyring. |
| `tsh auth test` | Verify Jira connectivity. |

### 7.4 Output conventions

- Plain, human-readable tables by default. No spinners, no decorative color.
- Every read command supports `--json` for piping.
- Errors go to stderr with non-zero exit codes.

## 8. GUI

Single-page layout (no tabs):

- **Header section** (light grey background, pinned at top): active task display, elapsed timer, Switch and Stop buttons.
- **Body section**: today's entries as a table — start/end times, ticket, note, duration. Editable inline. Non-work periods rendered greyed out with the label "Lunch", "Break", etc.
- **Footer section** (light grey background): summary line ("3 drafts · 4h 29m total"), Dry Run button, Push to Jira button.

The **idle reconciliation modal** appears as a center-overlaid card when the user returns from idle, with three vertical options ranked by likelihood (Same task → Different task → Not work). All times shown in Sydney local with the timezone label visible.

The **task picker** appears either in a dedicated window section on first start, or as a popover when the user clicks Switch. Shows in-progress tickets first, recents second, and an input field for typing a ticket key directly.

**Visual quality requirements:**
- WCAG AA contrast (≥4.5:1 normal text, ≥3:1 bold/large).
- Real typography, spacing, and component design at build time — not raw HTML defaults. (See `feedback_visual_contrast.md` in project memory.)

## 9. Configuration

`~/.tsh/config.toml`:

```toml
[jira]
base_url = "https://yourcompany.atlassian.net"
email = "you@company.com"
default_project_key = "SFXS"
# Optional override for the picker's "what tickets are mine" query.
# Defaults to: assignee = currentUser() AND status = "In Progress"
# picker_jql = "assignee = currentUser() AND sprint in openSprints()"

[time]
display_timezone = "Australia/Sydney"
rounding = "nearest"            # "nearest" | "up"
rounding_minutes = 15
idle_threshold_minutes = 10
max_idle_minutes_before_autostop = 240

[app]
autostart = false
http_port = 42024
```

Token is stored separately in Windows Credential Manager via `keyring`, service `tsh-jira`, account = email.

## 10. Timezone discipline

This is a load-bearing concern — a previous automation attempt was broken by Jira-vs-local timezone mismatch.

- All `datetime` values in code are timezone-aware. Naive datetimes are rejected at the storage boundary with a type guard.
- Single helper `to_jira_started(dt: datetime) -> str` produces ISO 8601 with explicit Sydney offset (e.g., `2026-04-27T14:30:00.000+1100`). Every worklog POST uses it. No other code path constructs the `started` field.
- Single helper `today_in_display_tz() -> date` returns "today" in Sydney time. All "today" / "yesterday" boundaries use it.
- **Mandatory tests:**
  - At least one test for an entry spanning a Sydney DST transition (early April / early October).
  - At least one test for a worklog whose start crosses midnight Sydney time.

## 11. Jira integration & safety

### 11.1 API endpoints used (the entire surface)

| Endpoint | When | Notes |
|---|---|---|
| `GET /rest/api/3/myself` | `tsh auth test` | Validate credentials. |
| `GET /rest/api/3/search` (JQL: `assignee = currentUser() AND status = "In Progress"`) | Picker refresh | Cached 5 min. JQL is overridable via `[jira] picker_jql` in config. |
| `GET /rest/api/3/issue/{key}` | Resolving an unknown ticket | Cache result. |
| `POST /rest/api/3/issue/{key}/worklog` | Push | One per draft entry. |

**Notably absent: `DELETE` and `PUT` on worklogs.** MVP only writes new worklogs — never modifies or removes. Single biggest "won't corrupt Jira" guardrail.

### 11.2 Push semantics

- **Idempotency**: Worklogs pushed in order; on any HTTP error, that entry stays draft, the rest continue. Per-entry result table at the end.
- **Dry run**: Prints exact payloads with `started` strings visible for timezone verification.
- **Rate limiting**: 100ms sleep between worklog POSTs (well under Atlassian's ~10 req/sec budget).
- **Retries**: 1 retry on 5xx and connection errors with 1s backoff. No retries on 4xx.

### 11.3 Failure modes

| Failure | UI behavior |
|---|---|
| Network down | Push button disabled with tooltip. Tracker keeps working. |
| Token expired / 401 | Banner: "Re-authenticate with `tsh auth login`." |
| Ticket key doesn't exist (404) | Picker shows "Ticket not found" inline; no tracking starts. |
| Single worklog POST fails | Entry stays draft with red "Failed: <reason>" badge; others push normally. |
| Tracker crashes mid-day | At most ~1 minute lost. Restart picks up active row from SQLite. |

## 12. Testing approach

- **Pure-function core (`tsh/core/`)** — exhaustive unit tests with no mocks. This is where the bulk of correctness logic lives (time math, rounding, idle reconciliation transitions, picker ordering).
- **Storage layer** — integration tests against a temp SQLite file.
- **Jira client** — integration tests against `respx` (httpx mock transport). Mandatory tests for the timezone helpers.
- **CLI** — `click.testing.CliRunner`, smoke tests per command.
- **GUI** — manual testing for MVP. Automated end-to-end testing of pywebview is high-effort and low-value at this stage.
- **End-to-end** — one manual test plan documented in `docs/test-plan.md` covering: start of day, switch, idle reconciliation, end-of-day push, restart-after-crash recovery.

## 13. Out of scope (explicit non-goals for MVP)

- Editing or deleting already-pushed worklogs from inside the tool
- Pulling existing worklogs back from Jira (no two-way sync)
- Multi-project / multi-Jira-instance / multi-user support
- Sprint reporting, time analytics, exports
- Mobile / web access
- Calendar integration (auto-pause for meetings)
- Search inside Jira (`tsh find <query>`)
- Background-while-logged-out (Windows Service)
- LLM features

## 14. Open questions deferred to planning

- Final pick between FastAPI and Flask for the local HTTP server (both work; FastAPI gives free OpenAPI docs which are useful for testing the CLI<->tracker contract).
- Exact strategy for the multi-day rounding reconciliation (sum-then-round vs round-then-sum) — current plan is sum-then-round per ticket per day.
- Whether the picker should resolve unknown ticket keys eagerly (on type) or lazily (on Start click). Default to lazy for MVP.
