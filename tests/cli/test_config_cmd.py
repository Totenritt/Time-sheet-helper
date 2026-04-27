"""Tests for `tsh config` subcommands."""

import json

import pytest
from click.testing import CliRunner

from tsh.cli.main import cli
from tsh.config import loader


def test_config_get_default_value(
    runner: CliRunner, isolated_config
) -> None:
    """config get returns the default value when no override set."""
    result = runner.invoke(cli, ["config", "get", "time.idle_threshold_minutes"])
    assert result.exit_code == 0
    assert result.output.strip() == "10"


def test_config_get_unknown_key(
    runner: CliRunner, isolated_config
) -> None:
    """config get unknown.key writes error to stderr, exits 1."""
    result = runner.invoke(cli, ["config", "get", "unknown.key"])
    assert result.exit_code == 1
    assert "Unknown config key" in result.output or "unknown" in result.output.lower()


def test_config_set_and_get_string(
    runner: CliRunner, isolated_config
) -> None:
    """config set then get returns the stored string value."""
    set_result = runner.invoke(
        cli, ["config", "set", "jira.email", "casey@example.com"]
    )
    assert set_result.exit_code == 0

    get_result = runner.invoke(cli, ["config", "get", "jira.email"])
    assert get_result.exit_code == 0
    assert get_result.output.strip() == "casey@example.com"


def test_config_set_integer_parsed_as_json(
    runner: CliRunner, isolated_config
) -> None:
    """config set integer value is parsed as JSON (int), get returns '5'."""
    set_result = runner.invoke(
        cli, ["config", "set", "time.idle_threshold_minutes", "5"]
    )
    assert set_result.exit_code == 0

    get_result = runner.invoke(cli, ["config", "get", "time.idle_threshold_minutes"])
    assert get_result.exit_code == 0
    assert get_result.output.strip() == "5"


def test_config_set_boolean_parsed_as_json(
    runner: CliRunner, isolated_config
) -> None:
    """config set 'true' parses as boolean True."""
    set_result = runner.invoke(
        cli, ["config", "set", "app.autostart", "true"]
    )
    assert set_result.exit_code == 0
    # The stored value should be the boolean True
    assert loader.get("app.autostart") is True


def test_config_get_nested_dict_prints_json(
    runner: CliRunner, isolated_config
) -> None:
    """config get of a nested section prints JSON."""
    result = runner.invoke(cli, ["config", "get", "jira"])
    assert result.exit_code == 0
    # Output should be valid JSON
    parsed = json.loads(result.output.strip())
    assert isinstance(parsed, dict)
    assert "base_url" in parsed
    assert "email" in parsed
