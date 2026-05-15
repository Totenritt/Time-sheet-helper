"""Tests for tsh.cli.tray — tsh tray / tsh quit commands."""

from __future__ import annotations

import subprocess
import httpx
import pytest
import respx

from click.testing import CliRunner

from tsh.cli.tray import tray, quit_cmd


BASE = "http://127.0.0.1:42024"

_STATUS_RESPONSE = {
    "active": None,
    "elapsed_seconds": 0,
    "idle_status": "clear",
    "idle_started_at": None,
}


# ---------------------------------------------------------------------------
# 11. tsh tray when tracker is already running → prints "tracker already running"
#     without calling runner.run
# ---------------------------------------------------------------------------

@respx.mock
def test_tray_already_running(runner: CliRunner, isolated_config, monkeypatch):
    respx.get(f"{BASE}/status").mock(
        return_value=httpx.Response(200, json=_STATUS_RESPONSE)
    )

    # Monkeypatch runner.run so it would raise if called.
    run_called = []

    def fake_run():
        run_called.append(True)
        raise AssertionError("runner.run should not have been called")

    import tsh.tracker.runner as runner_module
    monkeypatch.setattr(runner_module, "run", fake_run)

    result = runner.invoke(tray, [])

    assert result.exit_code == 0, result.output
    assert "tracker already running" in result.output
    assert run_called == []  # run was never called


# ---------------------------------------------------------------------------
# 12. tsh quit when tracker is running → POSTs /shutdown
# ---------------------------------------------------------------------------

@respx.mock
def test_quit_when_running(runner: CliRunner, isolated_config):
    respx.get(f"{BASE}/status").mock(
        return_value=httpx.Response(200, json=_STATUS_RESPONSE)
    )
    respx.post(f"{BASE}/shutdown").mock(
        return_value=httpx.Response(200, json={"shutting_down": True})
    )

    result = runner.invoke(quit_cmd, [])

    assert result.exit_code == 0, result.output
    assert "tracker stopped" in result.output
    # Verify POST /shutdown was actually called.
    assert respx.calls.call_count == 2  # GET /status + POST /shutdown


# ---------------------------------------------------------------------------
# 13. tsh quit when tracker is not running → "tracker not running" + exit 0
# ---------------------------------------------------------------------------

@respx.mock
def test_quit_tracker_not_running(runner: CliRunner, isolated_config):
    respx.get(f"{BASE}/status").mock(side_effect=httpx.ConnectError("refused"))

    result = runner.invoke(quit_cmd, [])

    assert result.exit_code == 0, result.output
    assert "tracker not running" in result.output


# ---------------------------------------------------------------------------
# 20.9 tsh tray --detach tests
# ---------------------------------------------------------------------------

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
    # Either pythonw.exe (dev) or tsh-tray.exe (frozen, future).
    assert cmd[0].lower().endswith("pythonw.exe") or cmd[0].lower().endswith("tsh-tray.exe")
    if cmd[0].lower().endswith("pythonw.exe"):
        # In dev mode we should be running -m tsh tray.
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
