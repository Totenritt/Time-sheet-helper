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
from pathlib import Path
from typing import Callable

import uvicorn

from tsh.config import credentials, loader
from tsh.jira.client import JiraClient
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
    try:
        asyncio.run(_run_async(state, http_port))
    finally:
        state.close()
