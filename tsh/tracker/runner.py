"""Orchestrator for the tsh tracker.

Wires up: TrackerState + uvicorn + IdleLoop + tray icon. One asyncio event
loop owns everything; the tray icon runs on a thread (pystray's design)
and signals shutdown via a thread-safe event.

Public entry point: ``run()``.
"""

from __future__ import annotations
import asyncio
import logging
import threading
from datetime import datetime as _datetime
from datetime import timedelta as _timedelta
from datetime import timezone as _tz
from pathlib import Path
from typing import Callable

import uvicorn

from tsh.config import credentials, loader
from tsh.jira.client import JiraClient
from tsh.storage import db as db_module
from tsh.storage import time_entries
from tsh.tracker.idle import IdleConfig, IdleLoop
from tsh.tracker.server import TrackerState, create_app
from tsh.tracker.tray import IconActions, build_icon, refresh_icon


logger = logging.getLogger(__name__)

REFRESH_INTERVAL_SECONDS = 1.0
FLUSH_INTERVAL_SECONDS = 60.0


def _db_path() -> Path:
    return loader.config_dir() / "tsh.db"


def _build_jira_client_factory() -> Callable[[], JiraClient | None]:
    """Closure that constructs a JiraClient if credentials are present, else None."""
    def factory() -> JiraClient | None:
        cfg = loader.load()
        base_url = cfg["jira"].get("base_url", "")
        email = cfg["jira"].get("email", "")
        if not base_url or not email:
            return None
        token = credentials.get_token(email)
        if not token:
            return None
        return JiraClient(base_url=base_url, email=email, token=token)
    return factory


def recover_stale_active(
    state: TrackerState,
    *,
    stale_threshold_minutes: int,
    idle_threshold_minutes: int,
    now: _datetime | None = None,
) -> None:
    """Close an orphaned active entry left from a previous daemon session.

    Called by run() BEFORE the HTTP port opens. If the active entry's start_at
    is older than stale_threshold_minutes, close at start_at + idle_threshold
    (best-effort: that's when idle would have first triggered had the daemon
    been running) and flag for reconciliation with reason='orphaned_active'.
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
        close_at = now  # safety net: don't set end_at in the future
    with db_module.tx(conn):
        closed = time_entries.end_active(conn, close_at)
        if closed is None:
            logger.error("recover_stale_active: end_active returned None unexpectedly")
            return
        time_entries.update(
            conn, closed.id,
            pending_reconciliation=1,
            reconciliation_reason="orphaned_active",
        )
    state.refresh()
    logger.info(
        "recovered orphaned active %s (started %.1fh ago) — closed at %s; queued for reconciliation",
        active.ticket_key, age / 3600, close_at.isoformat(),
    )


async def _run_async(state: TrackerState, http_port: int) -> None:
    """The async heart of the runner. Returns when shutdown is requested."""
    shutdown_event = asyncio.Event()

    # Provide a thread-safe shutdown trigger for the tray (which runs on its own thread).
    loop = asyncio.get_running_loop()

    def trigger_shutdown_threadsafe() -> None:
        loop.call_soon_threadsafe(shutdown_event.set)

    # ---- Build app + uvicorn ----
    app = create_app(state, shutdown_callback=trigger_shutdown_threadsafe)
    config = uvicorn.Config(app, host="127.0.0.1", port=http_port, log_level="warning")
    server = uvicorn.Server(config)

    # ---- Idle loop ----
    idle_loop = IdleLoop(state=state, config=IdleConfig.from_config())

    # ---- Tray actions ----
    actions = IconActions(
        state=state,
        on_switch_request=lambda: logger.info("tray: switch requested (GUI not built yet)"),
        on_show_main_window=lambda: logger.info("tray: show main window requested (GUI not built yet)"),
        on_quit=trigger_shutdown_threadsafe,
    )
    icon = build_icon(actions)

    # Run the tray icon on its own thread (pystray.Icon.run() blocks).
    tray_thread = threading.Thread(target=icon.run, daemon=True, name="tsh-tray")

    # ---- Periodic tasks ----
    async def refresh_tray():
        try:
            while not shutdown_event.is_set():
                try:
                    refresh_icon(icon, actions)
                except Exception:
                    logger.exception("tray refresh failed")
                await asyncio.sleep(REFRESH_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            return

    async def periodic_flush():
        try:
            while not shutdown_event.is_set():
                state.flush()
                await asyncio.sleep(FLUSH_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            return

    server_task = asyncio.create_task(server.serve(), name="tsh-uvicorn")
    idle_task = asyncio.create_task(idle_loop.run(), name="tsh-idle")
    refresh_task = asyncio.create_task(refresh_tray(), name="tsh-refresh")
    flush_task = asyncio.create_task(periodic_flush(), name="tsh-flush")

    # Start tray after uvicorn so /status responds when the menu first renders.
    await asyncio.sleep(0.1)
    tray_thread.start()

    # Wait for shutdown trigger.
    await shutdown_event.wait()

    # ---- Shutdown ----
    logger.info("tracker shutdown requested")
    server.should_exit = True
    icon.stop()
    for task in (idle_task, refresh_task, flush_task):
        task.cancel()
    await asyncio.gather(server_task, idle_task, refresh_task, flush_task, return_exceptions=True)
    state.close()


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
