"""tsh CLI entry point — defines the click group and registers subcommands."""

from __future__ import annotations
import click

from tsh import __version__
from tsh.cli import auth, config_cmd, push as push_cmd, review, tracking
from tsh.cli import tray as tray_cmd


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
cli.add_command(push_cmd.push)
cli.add_command(tracking.start)
cli.add_command(tracking.switch)
cli.add_command(tracking.stop)
cli.add_command(tracking.status)
cli.add_command(tracking.tasks)
cli.add_command(tray_cmd.tray)
cli.add_command(tray_cmd.quit_cmd, name="quit")
