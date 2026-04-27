"""Tests for tsh.tracker.tray IconActions and helpers."""

from __future__ import annotations
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

from tsh.tracker.server import TrackerState
from tsh.tracker.tray import (
    IconActions,
    _create_icon_image,
    _format_elapsed,
    _status_text,
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "tsh.db"


@pytest.fixture
def state(db_path):
    s = TrackerState(db_path=db_path, jira_client_factory=lambda: None)
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def actions(state):
    return IconActions(
        state=state,
        on_switch_request=Mock(),
        on_show_main_window=Mock(),
        on_quit=Mock(),
    )


# --- _format_elapsed ---

def test_format_elapsed_zero():
    assert _format_elapsed(0) == "0m"

def test_format_elapsed_minutes_only():
    assert _format_elapsed(900) == "15m"

def test_format_elapsed_hours_and_minutes():
    assert _format_elapsed(4500) == "1h 15m"

def test_format_elapsed_exact_hour():
    assert _format_elapsed(3600) == "1h 0m"


# --- _status_text ---

def test_status_text_idle(state):
    assert _status_text(state) == "tsh — Idle"

def test_status_text_active(state):
    state.start("SFXS-1073", "PR review")
    text = _status_text(state)
    assert "SFXS-1073" in text
    # Elapsed will be 0m given the entry was just created.
    assert text.startswith("tsh — SFXS-1073 — ")


# --- _create_icon_image ---

def test_create_icon_image_active_returns_image():
    img = _create_icon_image(active=True)
    assert img.size == (64, 64)
    assert img.mode == "RGBA"

def test_create_icon_image_idle_returns_image():
    img = _create_icon_image(active=False)
    assert img.size == (64, 64)


# --- IconActions ---

def test_actions_is_active_false_initially(actions):
    assert actions.is_active() is False

def test_actions_is_active_true_after_start(actions, state):
    state.start("SFXS-1073", "")
    assert actions.is_active() is True

def test_handle_pause_stops_active(actions, state):
    state.start("SFXS-1073", "")
    assert state.get_active() is not None
    actions.handle_pause()
    state.refresh()
    assert state.get_active() is None

def test_handle_pause_with_no_active_does_not_raise(actions):
    # No active timer; pause is a no-op.
    actions.handle_pause()  # no exception

def test_handle_switch_request_calls_callback(actions):
    actions.handle_switch_request()
    actions._on_switch_request.assert_called_once()

def test_handle_show_main_window_calls_callback(actions):
    actions.handle_show_main_window()
    actions._on_show_main_window.assert_called_once()

def test_handle_quit_calls_callback(actions):
    actions.handle_quit()
    actions._on_quit.assert_called_once()

def test_status_text_via_action(actions, state):
    state.start("SFXS-1", "")
    text = actions.status_text()
    assert "SFXS-1" in text
