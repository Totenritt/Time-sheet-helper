"""Tests for tsh.cli.autostart — Startup-folder shortcut management."""
from __future__ import annotations
from pathlib import Path

import pytest
from click.testing import CliRunner

from tsh.cli.main import cli


@pytest.fixture
def fake_startup(tmp_path: Path, monkeypatch):
    """Pretend the Windows Startup folder lives under tmp_path."""
    appdata = tmp_path / "AppData" / "Roaming"
    startup = appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    startup.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("APPDATA", str(appdata))
    return startup


def test_autostart_status_disabled_by_default(fake_startup, mocker):
    mocker.patch("tsh.cli.autostart._daemon_running", return_value=False)
    result = CliRunner().invoke(cli, ["autostart", "status"])
    assert result.exit_code == 0
    out = result.output.lower()
    assert "disabled" in out
    assert "not running" in out


def test_autostart_enable_writes_shortcut(fake_startup, mocker):
    """CreateShortcut is called with correct TargetPath / Arguments / WorkingDirectory."""
    saved: dict = {}

    class FakeShortcut:
        def __init__(self):
            object.__setattr__(self, "_fields", {})

        def __setattr__(self, name, value):
            if name == "_fields":
                object.__setattr__(self, name, value)
            else:
                self._fields[name] = value

        def save(self):
            saved.update(self._fields)

    class FakeShell:
        def CreateShortcut(self, path):
            saved["path"] = path
            return FakeShortcut()

    mocker.patch("tsh.cli.autostart._wscript_shell", return_value=FakeShell())
    mocker.patch("tsh.cli.autostart._spawn_detached")
    mocker.patch("tsh.cli.autostart._daemon_running", return_value=False)

    result = CliRunner().invoke(cli, ["autostart", "enable"])
    assert result.exit_code == 0, result.output
    target_lower = saved["TargetPath"].lower()
    assert target_lower.endswith("pythonw.exe") or target_lower.endswith("tsh-tray.exe")
    assert "tsh" in saved["Arguments"] and "tray" in saved["Arguments"]
    assert "Startup" in saved["path"]


def test_autostart_enable_starts_daemon_if_not_running(fake_startup, mocker):
    mocker.patch("tsh.cli.autostart._wscript_shell")
    spawn_mock = mocker.patch("tsh.cli.autostart._spawn_detached")
    mocker.patch("tsh.cli.autostart._daemon_running", return_value=False)

    CliRunner().invoke(cli, ["autostart", "enable"])
    spawn_mock.assert_called_once()


def test_autostart_enable_skips_spawn_when_already_running(fake_startup, mocker):
    mocker.patch("tsh.cli.autostart._wscript_shell")
    spawn_mock = mocker.patch("tsh.cli.autostart._spawn_detached")
    mocker.patch("tsh.cli.autostart._daemon_running", return_value=True)

    CliRunner().invoke(cli, ["autostart", "enable"])
    spawn_mock.assert_not_called()


def test_autostart_disable_removes_our_shortcut(fake_startup, mocker):
    our_lnk = fake_startup / "tsh.lnk"
    our_lnk.write_bytes(b"placeholder")
    mocker.patch("tsh.cli.autostart._shortcut_is_ours", return_value=True)
    result = CliRunner().invoke(cli, ["autostart", "disable"])
    assert result.exit_code == 0
    assert not our_lnk.exists()


def test_autostart_disable_refuses_foreign_shortcut(fake_startup, mocker):
    foreign = fake_startup / "tsh.lnk"
    foreign.write_bytes(b"placeholder")
    mocker.patch("tsh.cli.autostart._shortcut_is_ours", return_value=False)
    result = CliRunner().invoke(cli, ["autostart", "disable"])
    assert result.exit_code != 0
    assert foreign.exists()


def test_autostart_disable_when_already_disabled(fake_startup, mocker):
    result = CliRunner().invoke(cli, ["autostart", "disable"])
    assert result.exit_code == 0
    assert "already disabled" in result.output.lower() or "no shortcut" in result.output.lower()
