# Timesheet Helper — Implementation Plan

> Original plan file (also kept at `~/.claude/plans/crispy-zooming-charm.md`). This copy lives in the repo for project-local visibility and progress tracking.

## Status — 2026-04-27

**Phases 1–4 complete (12 of 25 tasks). 198 tests passing on `master`.**

Resume next session at **Phase 5 — CLI MVP**, starting with Task 13 (`tsh/cli/main.py` + `auth.py` + `config_cmd.py`).

### Completed work

| # | Task | Module | Tests | Commit(s) |
|---|---|---|---|---|
| 1.1 | Project skeleton | `pyproject.toml`, package layout, ruff/pytest/mypy | 1 (smoke) | `139a022` |
| 1.2 | Config loader | `tsh/config/loader.py` | 8 | `c192229` |
| 1.3 | Credentials wrapper | `tsh/config/credentials.py` | 5 | `f863ebb` |
| 2.1 | Models | `tsh/core/models.py` | 19 | `182fd27`, `f609c71`, `acfb6f0` |
| 2.2 | Timezone helpers | `tsh/core/timezones.py` | 16 | `7c05313` |
| 2.3 | Time rounding math | `tsh/core/time_math.py` | 26 | `f59802d` |
| 2.4 | Idle reconciliation | `tsh/core/reconcile.py` | 24 | `46a3c33` |
| 3.1 | DB + migrations | `tsh/storage/db.py`, `migrations/001_initial.sql` | 15 | `cca85a8`, `7fba4af` |
| 3.2 | `time_entries` repo | `tsh/storage/time_entries.py` | 26 | `2d7c9e1`, `cb87694` |
| 3.3 | `tickets_cache` repo | `tsh/storage/tickets_cache.py` | 17 | `bf461f6` |
| 4.1 | Jira client | `tsh/jira/client.py` | 21 | `46d6920`, `611cc29` |
| 4.2 | Push orchestration | `tsh/jira/push.py` | 20 | `ed61877`, `212b7f3` |

**What's actually working today:**
- Full pure core (timer state model, timezone discipline, rounding math, idle reconciliation logic) — no I/O.
- Full storage layer (SQLite + migrations + both repositories) with naive-datetime rejection at the boundary.
- Full Jira client (4 endpoints, basic auth, 1-retry on 5xx/connection, typed exception hierarchy).
- Push orchestration (plan/execute split, dry-run, per-entry failure isolation, rate limit).
- The `started` ISO string is built only by `tsh.core.timezones.to_jira_started` — single source of truth verified across the test suite.
- `pyproject.toml` declares all production deps; `tomli-w`, `httpx`, `respx`, `keyring`, `pytest`, `tzdata` (Windows) installed in the venv.

**Notable design choices made during implementation:**
- Field order in `TimeEntry` puts `note` last (defaulted) so dataclass default-ordering rules are satisfied; commented at the field declaration. Spec field order is preserved in the docstring.
- Migrations carry their own `PRAGMA user_version = N;` as the last line of each `.sql` file, so the version bump is atomic with the schema changes.
- `connect()` sets `row_factory = sqlite3.Row` and creates the parent directory automatically.
- `JiraClient` accepts an injectable `httpx.Client` and `_sleep` callable for testability; otherwise builds defaults.
- `plan_push` skips entries that round to 0 seconds (no point sending a 0-time worklog).
- `_sleep: Callable[[float], None]` (not the `callable` builtin) — caught in code review, fixed.

**Cross-cutting invariants enforced and tested:**
- No naive `datetime` can enter or leave the storage layer (adapter + converter both guard).
- `to_jira_started` is the only producer of the worklog `started` string. Verified by full-tree grep during code review.
- `JiraClient` exposes no `PUT` or `DELETE` — only the 4 endpoints in spec §11.
- DST forward (Oct 5 2025) and DST back (Apr 6 2025) Sydney transitions are tested in `test_timezones.py`.

### How to resume

```powershell
cd "C:\Users\Casey Luo\Documents\SWS\timesheet_helper"
.venv\Scripts\activate
pytest -q                    # confirm 198 still pass
```

Then dispatch Task 13 (Phase 5.1: CLI auth + config commands). The plan task descriptions below are the canonical specs to brief the next implementer subagent.

### Process notes for next session

- Used **strict subagent-driven-development** for Phases 3 and 4 (implementer + spec review + code quality review per task) — caught real bugs (silent `None` passthrough on required datetime fields, missing parent-dir creation in `connect`, wrong type annotation on `_sleep`).
- Plan calls for **lighter cadence (implementer + one combined review)** for Phases 5–7 since user-facing CLI/tracker/GUI bugs are louder and easier to catch by running the tool than engine bugs.
- Test count grew from 1 → 198. Anything below ~150 means something has been deleted; investigate before continuing.
- All commits are conventional (`feat:`, `fix:`, `test:`, `chore:`, `docs:`) and per-task — easy to revert if needed.

---

## Context

We have an approved design spec at `docs/superpowers/specs/2026-04-27-timesheet-helper-design.md` (commit `4719dc6`). The plan below translates that spec into an executable build sequence.

**What we're building:** A Windows desktop tool (`tsh`) that tracks Jira-loggable time with idle detection and end-of-day batch push to Jira. GUI is the primary interface, CLI is an equivalent alternative. Both drive the same engine; SQLite is the source of truth.

**Why this order:** The plan is sequenced so you reach a *usable headless CLI MVP at Phase 5*, before any tracker or GUI work. That gives you something to validate against your real Jira workspace as soon as you have an API token. The tracker (HTTP server + idle loop + tray) and GUI are layered on top of an already-working core.

**Tech stack (locked from spec):** Python 3.11+, `click`, `httpx`, `keyring`, stdlib `sqlite3`, `pywin32`, `pystray`, `pywebview`, `fastapi`+`uvicorn`, `pyinstaller`. Tests with `pytest` + `respx`.

---

## File layout (target)

```
tsh/
  __init__.py
  __main__.py                       # CLI entry: python -m tsh
  config/
    loader.py                       # read/write ~/.tsh/config.toml, defaults, schema       [DONE]
    credentials.py                  # keyring wrapper (service: tsh-jira)                   [DONE]
  core/
    models.py                       # TimeEntry, TicketCacheEntry dataclasses               [DONE]
    timezones.py                    # to_jira_started, today_in_display_tz, ensure_aware    [DONE]
    time_math.py                    # round_seconds, sum_then_round_per_ticket              [DONE]
    reconcile.py                    # pure idle-reconciliation transition functions         [DONE]
  storage/
    db.py                           # connect(), run_migrations(), tx context mgr           [DONE]
    migrations/
      001_initial.sql               # time_entries + tickets_cache + indexes                [DONE]
    time_entries.py                 # repository: insert/get_active/end_active/list_by_day  [DONE]
    tickets_cache.py                # repository: upsert/get/recents/mark_used              [DONE]
  jira/
    client.py                       # httpx client: 4 endpoints                             [DONE]
    push.py                         # orchestrate push: plan + execute                      [DONE]
  tracker/
    server.py                       # FastAPI app: /status, /switch, /start, /stop, ...
    idle.py                         # GetLastInputInfo poll loop, idle state machine
    tray.py                         # pystray icon, menu, lifecycle
    runner.py                       # ties server + idle + tray + GUI launcher
  gui/
    app.py                          # pywebview window, JS<->Python bridge
    webroot/
      index.html
      app.js                        # SPA: today view, picker, idle modal, review/push
      style.css                     # WCAG-AA palette, spacing scale
  cli/
    main.py                         # click group, lazy-loads subcommand modules           [NEXT]
    auth.py                         # auth login/logout/test                                [NEXT]
    config_cmd.py                   # config get/set                                        [NEXT]
    tracking.py                     # start/switch/stop/status/tasks (HTTP-first)
    review.py                       # review/log/edit/delete
    push.py                         # push (with --dry-run)
    tray.py                         # tray (start), quit
tests/
  unit/                             # pure core tests (most coverage lives here)            [DONE]
  storage/                          # integration tests against temp SQLite                 [DONE]
  jira/                             # respx-mocked client + push tests                      [DONE]
  cli/                              # CliRunner smoke tests
  conftest.py                       # fixtures: temp config, temp db, frozen time
docs/
  plans/2026-04-27-timesheet-helper.md   # this file
  superpowers/specs/2026-04-27-timesheet-helper-design.md
  test-plan.md                      # manual end-to-end test plan (Phase 8)
pyproject.toml                      # hatchling, deps, ruff, pytest, mypy                   [DONE]
README.md                                                                                   [DONE]
```

---

## Build phases

Each phase is a coherent milestone; run the test suite green before moving on. Commit per task within a phase.

### Phase 1 — Foundation (3 tasks) ✅ DONE

1. **Project skeleton.** Create `pyproject.toml` (hatchling, Python 3.11+), `tsh/` package with empty submodule `__init__.py` files, `tests/conftest.py`, ruff + pytest + mypy config. Wire `tsh = "tsh.__main__:main"` console script. Add `pytest -q` smoke that imports `tsh` and passes. Commit.
2. **`tsh/config/loader.py`.** Loads `~/.tsh/config.toml` with the schema in spec §9, fills defaults, atomic write on `set`. Tests: missing-file gives defaults, set-then-get round-trips, invalid TOML raises a clear error, env var `TSH_CONFIG_DIR` overrides path (for tests).
3. **`tsh/config/credentials.py`.** Thin `keyring` wrapper: `set_token(email, token)`, `get_token(email)`, `delete_token(email)`. Tests: monkeypatch `keyring.set_password`/`get_password` so the suite never touches the real OS keyring.

### Phase 2 — Pure core (4 tasks) ✅ DONE

4. **`tsh/core/models.py`.** Frozen dataclasses: `TimeEntry(id, ticket_key, start_at, end_at, note, kind, jira_worklog_id, pushed_at)` and `TicketCacheEntry(ticket_key, summary, status, assignee_email, last_fetched_at, last_used_at)`. `kind: Literal['work','not_work','idle_unresolved']`. All datetimes typed as `datetime` (not str). Tests: construction validates, `is_active` property, `is_pushed` property.
5. **`tsh/core/timezones.py`.** `ensure_aware(dt)` raises if naive; `to_utc(dt)`; `to_display(dt, tz='Australia/Sydney')`; `to_jira_started(dt)` returns `'YYYY-MM-DDTHH:MM:SS.fff±HHMM'` in display tz; `today_in_display_tz(now=None, tz='Australia/Sydney')` returns `date`. Tests: naive datetime rejected; round-trip aware; **DST forward (Oct 5 2025 02:00 Sydney)** test; **DST back (Apr 6 2025 03:00 Sydney)** test; midnight-crossing case.
6. **`tsh/core/time_math.py`.** `round_seconds(seconds, mode='nearest', minutes=15)` returning rounded seconds; `sum_then_round_per_ticket(entries, mode, minutes)` returning `dict[ticket_key, rounded_seconds]`. Mode `'up'` rounds any non-zero up to next bucket; `'nearest'` standard half-up. Tests cover boundary cases (exactly on bucket, just below, just above, zero, multi-entry sum-then-round vs round-then-sum divergence).
7. **`tsh/core/reconcile.py`.** Pure transition function `reconcile_idle(active: TimeEntry, idle_started_at, returned_at, choice: Literal['same','different','not_work'], chosen_ticket=None) -> list[TimeEntry]` returning the new entry list (1 entry for 'same', 3 for 'different'/'not_work'). No I/O. Tests: each branch produces correct entries with correct timestamps and kinds.

### Phase 3 — Storage (3 tasks) ✅ DONE

8. **`tsh/storage/db.py` + `migrations/001_initial.sql`.** Migration runner reads `PRAGMA user_version`, applies pending `.sql` files in order, sets new version (embedded in the SQL file for atomicity). SQL creates `time_entries`, `tickets_cache`, and the partial unique index `one_active`. Connection helper opens with `PARSE_DECLTYPES`, `foreign_keys=ON`, `row_factory=sqlite3.Row`, registers UTC datetime adapter/converter, creates the parent directory. Tests: fresh db gets schema, idempotent re-run, partial unique index rejects two active rows.
9. **`tsh/storage/time_entries.py`.** Repository functions: `insert`, `get_active`, `end_active(end_at)`, `list_by_day(day, tz)`, `get(id)`, `update(id, **fields)` (rejects unknown field names against an allowlist), `delete(id)` (refuses if `pushed_at` not null), `mark_pushed(id, jira_worklog_id, pushed_at)`. Each function takes a `Connection`. Tests against an in-memory SQLite via fixture.
10. **`tsh/storage/tickets_cache.py`.** `upsert(ticket_key, summary, status, assignee_email, last_fetched_at)`, `mark_used(ticket_key, when=None)`, `get(ticket_key)`, `recents(limit=10)`, `is_fresh(ticket_key, ttl_minutes=5)`. Tests cover TTL boundary, recents ordering, missing ticket returns None.

### Phase 4 — Jira integration (2 tasks) ✅ DONE

11. **`tsh/jira/client.py`.** `JiraClient(base_url, email, token)` with methods `auth_test()`, `search_in_progress(jql_override=None)`, `get_issue(key)`, `post_worklog(key, started_iso, time_spent_seconds, comment)`. Uses `httpx.Client`, basic auth with email+token, 1 retry on 5xx/network with 1s backoff, no retry on 4xx. Typed exception hierarchy: `JiraError` / `JiraAuthError` / `JiraNotFoundError` / `JiraClientError` / `JiraServerError`. Tests use `respx` to assert exact request payloads (especially the `started` string format and the `Authorization` header).
12. **`tsh/jira/push.py`.** `plan_push(entries, mode, minutes, tz) -> list[PlannedPush]` (pure: groups, rounds, builds payloads with `to_jira_started`, combines notes); `execute_push(planned, client, *, dry_run=False, sleep_ms=100) -> list[PushResult]`. Push results have `entry_ids`, `ticket_key`, `ok`, `jira_worklog_id`, `error`, `dry_run`. On success, repository's `mark_pushed` is called by the caller (kept out of this module so it stays unit-testable). Tests: dry-run produces payloads without HTTP calls; per-entry failure isolation; rate-limit sleep observable via fake clock.

### Phase 5 — CLI MVP (3 tasks) — *headless usable system after Phase 5*  ⬅ NEXT

13. **`tsh/cli/main.py` + `auth.py` + `config_cmd.py`.** Click group `tsh`. Subcommands: `tsh auth login` (prompts for base_url, email, token; writes URL+email to TOML, token to keyring), `tsh auth logout`, `tsh auth test` (prints "Authenticated as <name>"), `tsh config get <key>`, `tsh config set <key> <value>` (dotted keys: `jira.email`, `time.idle_threshold_minutes`, etc.). Tests with `CliRunner` mocking `keyring` and config dir.
14. **`tsh/cli/review.py`.** `tsh log <ticket> <duration> [-m note] [--at TIME]` (parses `45m`, `1h30m`); `tsh review [--day today|yesterday|YYYY-MM-DD] [--json]` prints a table per spec §6.4 with raw and rounded times; `tsh edit <id>` opens TOML in `$EDITOR` (use `click.edit`), parses back, validates, updates; `tsh delete <id>`. None of these need the tracker; all hit SQLite directly via repositories. Tests with `CliRunner`.
15. **`tsh/cli/push.py`.** `tsh push [--day today] [--dry-run] [--json]`. Reads drafts via repo, calls `plan_push`, then `execute_push` (or just prints the planned payloads if `--dry-run`). Prints per-entry result table; non-zero exit if any failures. Tests with `respx` against the full path: log → review → push.

> **Milestone:** With Phase 5 complete and a real Jira API token in `keyring`, you can use the tool from the terminal end-to-end. Ship it to yourself for a week of dogfooding before building the tracker/GUI.

### Phase 6 — Tracker (4 tasks)

16. **`tsh/tracker/server.py`.** FastAPI app exposing `GET /status`, `POST /start {ticket_key, note}`, `POST /switch {ticket_key, note}`, `POST /stop`, `GET /tasks` (in-progress JQL + recents, deduped per spec §5.2), `POST /idle/return {choice, chosen_ticket?}`. Bound to `127.0.0.1:<config http_port>`. State held in a `TrackerState` object (in-memory active-entry view) with a 60-second flush task that writes to SQLite. Tests with FastAPI's `TestClient`.
17. **`tsh/tracker/idle.py`.** Background asyncio task polling `pywin32.win32api.GetLastInputInfo()` every 30s. State machine: `active` → `idle_pending` (after `idle_threshold_minutes`) → `idle_resolved` (on input return) or `idle_autostopped` (after `max_idle_minutes_before_autostop`). On idle return: notify `TrackerState` to surface a reconciliation prompt to GUI/notification. Tests inject a fake input-time provider so the loop is deterministic.
18. **`tsh/tracker/tray.py`.** `pystray.Icon` with menu items per spec §3 (current task display, Switch, Pause, Show, Quit). On Quit: graceful shutdown (`uvicorn.Server.should_exit = True` + cancel idle task). Tests skipped for tray rendering itself (manual); test the menu-action handlers directly.
19. **`tsh/tracker/runner.py` + `cli/tray.py`.** `runner.run()` starts uvicorn, idle task, tray icon, GUI window in one event loop. `tsh tray` launches the runner; idempotent — checks the http_port; if already bound, focuses existing window via an HTTP call instead. `tsh quit` calls `POST /shutdown` (or sends SIGTERM on the saved PID). **Now rewire** `tsh/cli/tracking.py` (start/switch/stop/status/tasks) to talk to the running tracker via httpx; if no tracker is running, error with the spec's message.

### Phase 7 — GUI (4 tasks)

20. **`tsh/gui/app.py` + `webroot/index.html`/`app.js`/`style.css`.** Pywebview window loads `webroot/index.html`. JS calls `pywebview.api.<method>` which proxies to the local HTTP API. **Style baseline:** define CSS variables for color palette (near-black `#111` titles on white per `feedback_visual_contrast.md`), 4/8/16/24px spacing scale, single font stack, button + card components. Implement **today view**: header section (active task + elapsed timer counting in JS), entries table, footer (totals + Push buttons disabled until Phase 7 task 23). Tests: snapshot the DOM produced by JS for a fixture state.
21. **Picker + switch.** Implement task picker as a popover over the header's Switch button. Lists in-progress tickets first, recents below, deduped. Free-text input resolves via `GET /rest/api/3/issue/{key}` proxy on Start. Selecting a ticket and clicking Start calls `POST /switch` (or `POST /start` if no active timer). Visual states: loading, error ("Ticket not found"), success.
22. **Idle reconciliation modal.** Center-overlaid card per the approved mockup. Three options ranked Same → Different → Not work. Sydney time labels visible. Submitting calls `POST /idle/return`. The tracker pushes a `pywebview` window-show on idle return; modal opens automatically.
23. **Review + push UI.** Inline editing of the entries table (start, end, ticket, note) — debounced PATCH to repository via local API. Footer buttons enabled when at least one draft exists. Dry Run opens a modal showing the exact payloads. Push opens a confirmation, then a results modal (per-entry ✓/✗). Pushed rows re-render as read-only.

### Phase 8 — Polish (2 tasks)

24. **`docs/test-plan.md`.** Document the manual end-to-end test plan per spec §12: start of day, switch, idle reconciliation (same/different/not-work), end-of-day push (and dry-run), restart-after-crash recovery, autostart toggle, timezone visual sanity (Sydney label everywhere).
25. **PyInstaller packaging.** `tsh/__main__.py` becomes the entry. Add `tsh.spec` with hidden imports for `pywin32`, `pystray`, `pywebview`. Include `tsh/gui/webroot/` as a data file. Build target: `dist/tsh.exe` single binary. Add a `make build` (or `scripts/build.ps1`) shortcut. Smoke: launch the built `.exe`, confirm tray icon appears and CLI works.

---

## Critical files to reference during execution

- **Spec:** `docs/superpowers/specs/2026-04-27-timesheet-helper-design.md` — every task implements a piece of this. Re-read the relevant section before starting a task.
- **Visual feedback memory:** `~/.claude/projects/C--Users-Casey-Luo-Documents-SWS-timesheet-helper/memory/feedback_visual_contrast.md` — WCAG-AA contrast and design polish requirements for Phase 7.
- **Approved mockups (gitignored):** `.superpowers/brainstorm/3020-1777258290/content/main-layout.html` (single-page chosen) and `idle-modal-v2.html` (idle modal layout).

---

## Cross-cutting rules

- **TDD for `tsh/core/`, `tsh/storage/`, `tsh/jira/`, and `tsh/cli/`:** test first, fail, implement, pass, commit. The tracker and GUI lean more on integration/manual tests per spec §12.
- **No naive datetimes leave `tsh/storage/`.** Type guard at the storage boundary (already enforced in `db.py`).
- **Single source for the `started` ISO string:** only `tsh/core/timezones.py:to_jira_started`. No other file may construct that format.
- **No `PUT`/`DELETE` to Jira worklogs anywhere.** Code review rule.
- **Commit per task.** Conventional commit prefixes: `feat:`, `test:`, `refactor:`, `chore:`, `docs:`.

---

## Verification (end-to-end)

After each phase:
```
pytest -q
ruff check .
mypy tsh
```
All three should pass before the phase is "done." (Note: `ruff` and `mypy` haven't been run yet; install them via `pip install -e ".[dev]"` if not present.)

After **Phase 5** (CLI MVP):
1. `tsh auth login` against your real Jira workspace.
2. `tsh auth test` shows "Authenticated as Casey Luo".
3. `tsh log SFXS-XXXX 30m -m "manual smoke test"`.
4. `tsh review` shows the entry with raw=30m, rounded=30m.
5. `tsh push --dry-run` prints the payload — **manually verify the `started` field has `+1100` (or `+1000` per current Sydney offset)**.
6. `tsh push` succeeds; check Jira UI to confirm the worklog appears at the right time.

After **Phase 6** (tracker):
1. `tsh tray` starts the icon; main window opens.
2. `tsh switch SFXS-XXXX` from a separate terminal updates the tray icon's task within a second.
3. Lock the screen for 11 minutes; on unlock, the GUI shows the idle modal with Sydney times.
4. Kill the tracker process; restart with `tsh tray`; confirm the active timer was recovered (within the 60s flush window).

After **Phase 7** (GUI):
1. Walk through the manual test plan in `docs/test-plan.md` start to finish.

After **Phase 8** (packaging):
1. Build `dist/tsh.exe`. Move it to a clean folder. Run it. Tray + GUI work without a Python install on the path.

---

## Out of scope for this plan

Mirrors spec §13: no edit/delete of pushed worklogs, no two-way Jira sync, no multi-project, no analytics, no calendar integration, no search-inside-Jira, no Windows Service for background-while-logged-out, no LLM features.

## Open decisions deferred

- FastAPI vs Flask for `tracker/server.py` — plan assumes FastAPI; flag at start of Phase 6 if a downside surfaces (e.g., uvicorn + pyinstaller friction).
- Whether picker resolves unknown ticket keys eagerly (on type) or lazily (on Start). Plan defaults to **lazy**.
- Sum-then-round vs round-then-sum for multi-entry-per-ticket rounding. Plan defaults to **sum-then-round per ticket per day** (implemented in `time_math.py`).
