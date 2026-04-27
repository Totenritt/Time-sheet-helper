"""Tests for the top-level CLI group."""

from click.testing import CliRunner

from tsh.cli.main import cli
from tsh import __version__


def test_version(runner: CliRunner) -> None:
    """tsh --version prints version with prog_name 'tsh'."""
    result = runner.invoke(cli, ["--version"])
    assert result.exit_code == 0
    assert "tsh" in result.output
    assert __version__ in result.output


def test_help_lists_subcommands(runner: CliRunner) -> None:
    """tsh --help lists auth and config subcommands."""
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "auth" in result.output
    assert "config" in result.output
