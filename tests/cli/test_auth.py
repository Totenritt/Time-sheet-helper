"""Tests for `tsh auth` subcommands."""

import pytest
from click.testing import CliRunner

from tsh.cli.main import cli
from tsh.config import credentials, loader
from tsh.jira.client import JiraAuthError, JiraServerError


# ---------------------------------------------------------------------------
# login
# ---------------------------------------------------------------------------


def test_login_saves_config_and_token(
    runner: CliRunner, isolated_config, fake_keyring
) -> None:
    """login writes base_url + email to config and token to keyring."""
    result = runner.invoke(
        cli,
        [
            "auth", "login",
            "--base-url", "https://example.atlassian.net",
            "--email", "user@example.com",
            "--token", "mytoken123",
        ],
    )
    assert result.exit_code == 0
    assert "user@example.com" in result.output

    assert loader.get("jira.base_url") == "https://example.atlassian.net"
    assert loader.get("jira.email") == "user@example.com"
    assert credentials.get_token("user@example.com") == "mytoken123"


def test_login_strips_trailing_slash(
    runner: CliRunner, isolated_config, fake_keyring
) -> None:
    """login strips trailing slashes from base_url."""
    result = runner.invoke(
        cli,
        [
            "auth", "login",
            "--base-url", "https://example.atlassian.net/",
            "--email", "user@example.com",
            "--token", "tok",
        ],
    )
    assert result.exit_code == 0
    assert loader.get("jira.base_url") == "https://example.atlassian.net"


# ---------------------------------------------------------------------------
# logout
# ---------------------------------------------------------------------------


def test_logout_removes_token(
    runner: CliRunner, isolated_config, fake_keyring
) -> None:
    """logout after login removes the token from keyring; email stays in config."""
    # Set up: login first
    runner.invoke(
        cli,
        [
            "auth", "login",
            "--base-url", "https://example.atlassian.net",
            "--email", "user@example.com",
            "--token", "mytoken123",
        ],
    )
    assert credentials.get_token("user@example.com") == "mytoken123"

    result = runner.invoke(cli, ["auth", "logout"])
    assert result.exit_code == 0
    assert "user@example.com" in result.output
    assert credentials.get_token("user@example.com") is None
    # Email should still be in config
    assert loader.get("jira.email") == "user@example.com"


def test_logout_with_no_email_configured(
    runner: CliRunner, isolated_config, fake_keyring
) -> None:
    """logout with no email configured writes message to stderr and exits 0."""
    result = runner.invoke(cli, ["auth", "logout"])
    assert result.exit_code == 0
    # The message goes to stderr; CliRunner mixes by default unless mix_stderr=False
    # With default CliRunner, stderr is mixed into output
    assert "No email configured" in result.output or "nothing to remove" in result.output.lower()


# ---------------------------------------------------------------------------
# test
# ---------------------------------------------------------------------------


def test_auth_test_no_config(
    runner: CliRunner, isolated_config, fake_keyring
) -> None:
    """test with no config: writes 'Jira not configured' to stderr, exits 2."""
    result = runner.invoke(cli, ["auth", "test"])
    assert result.exit_code == 2
    assert "Jira not configured" in result.output


def test_auth_test_no_token(
    runner: CliRunner, isolated_config, fake_keyring
) -> None:
    """test with config but no token: writes hint, exits 2."""
    loader.set("jira.base_url", "https://example.atlassian.net")
    loader.set("jira.email", "user@example.com")
    # No token set in keyring

    result = runner.invoke(cli, ["auth", "test"])
    assert result.exit_code == 2
    assert "No token" in result.output or "token" in result.output.lower()


def test_auth_test_success(
    runner: CliRunner, isolated_config, fake_keyring, monkeypatch
) -> None:
    """test success path: prints 'Authenticated as <displayName>'."""
    loader.set("jira.base_url", "https://example.atlassian.net")
    loader.set("jira.email", "user@example.com")
    fake_keyring[("tsh-jira", "user@example.com")] = "mytoken"

    class FakeJiraClient:
        def __init__(self, *args, **kwargs):
            pass

        def auth_test(self):
            return {"displayName": "Casey Luo"}

        def close(self):
            pass

    monkeypatch.setattr("tsh.cli.auth.JiraClient", FakeJiraClient)

    result = runner.invoke(cli, ["auth", "test"])
    assert result.exit_code == 0
    assert "Authenticated as Casey Luo" in result.output


def test_auth_test_auth_error(
    runner: CliRunner, isolated_config, fake_keyring, monkeypatch
) -> None:
    """test JiraAuthError: stderr has message, exit 1."""
    loader.set("jira.base_url", "https://example.atlassian.net")
    loader.set("jira.email", "user@example.com")
    fake_keyring[("tsh-jira", "user@example.com")] = "badtoken"

    class FakeJiraClient:
        def __init__(self, *args, **kwargs):
            pass

        def auth_test(self):
            raise JiraAuthError("invalid token")

        def close(self):
            pass

    monkeypatch.setattr("tsh.cli.auth.JiraClient", FakeJiraClient)

    result = runner.invoke(cli, ["auth", "test"])
    assert result.exit_code == 1
    assert "Authentication failed" in result.output
    assert "invalid token" in result.output


def test_auth_test_server_error(
    runner: CliRunner, isolated_config, fake_keyring, monkeypatch
) -> None:
    """test JiraServerError: stderr has 'Jira error', exit 1."""
    loader.set("jira.base_url", "https://example.atlassian.net")
    loader.set("jira.email", "user@example.com")
    fake_keyring[("tsh-jira", "user@example.com")] = "sometoken"

    class FakeJiraClient:
        def __init__(self, *args, **kwargs):
            pass

        def auth_test(self):
            raise JiraServerError("503")

        def close(self):
            pass

    monkeypatch.setattr("tsh.cli.auth.JiraClient", FakeJiraClient)

    result = runner.invoke(cli, ["auth", "test"])
    assert result.exit_code == 1
    assert "Jira error" in result.output
