"""Tests for tsh.config.loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from tsh.config import loader


@pytest.fixture(autouse=True)
def isolated_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point TSH_CONFIG_DIR at a fresh temp dir for every test."""
    monkeypatch.setenv("TSH_CONFIG_DIR", str(tmp_path))
    return tmp_path


def test_load_with_no_file_returns_defaults(isolated_config_dir: Path) -> None:
    cfg = loader.load()
    assert cfg["time"]["display_timezone"] == "Australia/Sydney"
    assert cfg["time"]["rounding_minutes"] == 15
    assert cfg["time"]["idle_threshold_minutes"] == 10
    assert cfg["time"]["max_idle_minutes_before_autostop"] == 240
    assert cfg["app"]["http_port"] == 42024
    assert cfg["app"]["autostart"] is False
    assert cfg["jira"]["base_url"] == ""


def test_set_then_get_roundtrip(isolated_config_dir: Path) -> None:
    loader.set("jira.email", "casey@example.com")
    assert loader.get("jira.email") == "casey@example.com"


def test_set_persists_to_disk(isolated_config_dir: Path) -> None:
    loader.set("jira.base_url", "https://example.atlassian.net")
    # Re-read from disk by calling load() afresh.
    cfg = loader.load()
    assert cfg["jira"]["base_url"] == "https://example.atlassian.net"


def test_set_preserves_other_values(isolated_config_dir: Path) -> None:
    loader.set("jira.email", "a@example.com")
    loader.set("time.idle_threshold_minutes", 5)
    cfg = loader.load()
    assert cfg["jira"]["email"] == "a@example.com"
    assert cfg["time"]["idle_threshold_minutes"] == 5
    # Defaults still present for unset keys
    assert cfg["time"]["display_timezone"] == "Australia/Sydney"


def test_get_unknown_key_raises(isolated_config_dir: Path) -> None:
    with pytest.raises(KeyError, match="nope.field"):
        loader.get("nope.field")


def test_invalid_toml_raises_clear_error(isolated_config_dir: Path) -> None:
    cfg_file = isolated_config_dir / "config.toml"
    cfg_file.write_text("this is = = not valid toml\n", encoding="utf-8")
    with pytest.raises(loader.ConfigError, match="Invalid TOML"):
        loader.load()


def test_env_var_overrides_default_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    custom = tmp_path / "custom-config-dir"
    monkeypatch.setenv("TSH_CONFIG_DIR", str(custom))
    loader.set("jira.email", "x@y.com")
    assert (custom / "config.toml").exists()


def test_atomic_write_no_temp_left_behind(isolated_config_dir: Path) -> None:
    loader.set("jira.email", "x@y.com")
    leftover = list(isolated_config_dir.glob("*.tmp"))
    assert leftover == []
