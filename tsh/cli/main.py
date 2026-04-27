"""tsh CLI entry point — defines the click group and registers subcommands."""

from __future__ import annotations
import click

from tsh import __version__


@click.group()
@click.version_option(__version__, prog_name="tsh")
def cli() -> None:
    """tsh — Jira time-tracking helper."""


# Subcommand group registration: auth, config, more in later phases.
def _register_subcommands() -> None:
    from tsh.cli import auth, config_cmd
    cli.add_command(auth.auth_group, name="auth")
    cli.add_command(config_cmd.config_group, name="config")


_register_subcommands()
