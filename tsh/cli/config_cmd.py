"""tsh config — get/set config values."""

from __future__ import annotations
import json
import click

from tsh.config import loader


@click.group()
def config_group() -> None:
    """Read or modify ~/.tsh/config.toml."""


@config_group.command("get")
@click.argument("key")
def get(key: str) -> None:
    """Print the value of a dotted config key (e.g. `tsh config get jira.email`)."""
    try:
        value = loader.get(key)
    except KeyError:
        click.echo(f"Unknown config key: {key}", err=True)
        raise click.exceptions.Exit(1)
    # Render scalars and collections both readably.
    if isinstance(value, (str, int, float, bool)) or value is None:
        click.echo(value)
    else:
        click.echo(json.dumps(value, indent=2, default=str))


@config_group.command("set")
@click.argument("key")
@click.argument("value")
def set_(key: str, value: str) -> None:
    """Set a dotted config key. Value is parsed as JSON if possible (so booleans
    and integers work), otherwise stored as a string."""
    parsed: object
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        parsed = value
    loader.set(key, parsed)
    click.echo(f"Set {key} = {parsed!r}")
