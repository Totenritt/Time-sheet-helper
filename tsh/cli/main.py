"""tsh CLI entry point — defines the click group and registers subcommands."""

from __future__ import annotations
import click

from tsh import __version__
from tsh.cli import auth, config_cmd, review


@click.group()
@click.version_option(__version__, prog_name="tsh")
def cli() -> None:
    """tsh — Jira time-tracking helper."""


cli.add_command(auth.auth_group, name="auth")
cli.add_command(config_cmd.config_group, name="config")
cli.add_command(review.log)
cli.add_command(review.review)
cli.add_command(review.edit)
cli.add_command(review.delete)
