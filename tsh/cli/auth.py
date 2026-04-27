"""tsh auth — login/logout/test against Jira."""

from __future__ import annotations
import click

from tsh.config import credentials, loader
from tsh.jira.client import JiraAuthError, JiraClient, JiraError


@click.group()
def auth_group() -> None:
    """Manage Jira credentials."""


@auth_group.command("login")
@click.option("--base-url", prompt="Jira base URL", help="e.g. https://yourco.atlassian.net")
@click.option("--email", prompt="Jira email", help="Email associated with the API token")
@click.option(
    "--token",
    prompt="Jira API token",
    hide_input=True,
    help="API token from id.atlassian.com",
)
def login(base_url: str, email: str, token: str) -> None:
    """Save Jira credentials. Token goes to OS keyring; URL+email to config."""
    # Strip trailing slashes from base_url for cleanliness.
    base_url = base_url.rstrip("/")
    loader.set("jira.base_url", base_url)
    loader.set("jira.email", email)
    credentials.set_token(email, token)
    click.echo(f"Saved credentials for {email} at {base_url}")


@auth_group.command("logout")
def logout() -> None:
    """Remove the stored Jira API token. Leaves base_url and email intact."""
    cfg = loader.load()
    email = cfg["jira"].get("email", "")
    if not email:
        click.echo("No email configured — nothing to remove.", err=True)
        raise click.exceptions.Exit(0)
    credentials.delete_token(email)
    click.echo(f"Removed token for {email}.")


@auth_group.command("test")
def test() -> None:
    """Verify Jira credentials by calling /rest/api/3/myself."""
    cfg = loader.load()
    base_url = cfg["jira"].get("base_url", "")
    email = cfg["jira"].get("email", "")
    if not base_url or not email:
        click.echo(
            "Jira not configured. Run `tsh auth login` first.",
            err=True,
        )
        raise click.exceptions.Exit(2)
    token = credentials.get_token(email)
    if not token:
        click.echo(
            f"No token in keyring for {email}. Run `tsh auth login` to set it.",
            err=True,
        )
        raise click.exceptions.Exit(2)
    client = JiraClient(base_url=base_url, email=email, token=token)
    try:
        me = client.auth_test()
    except JiraAuthError as e:
        click.echo(f"Authentication failed: {e}", err=True)
        raise click.exceptions.Exit(1)
    except JiraError as e:
        click.echo(f"Jira error: {e}", err=True)
        raise click.exceptions.Exit(1)
    finally:
        client.close()
    name = me.get("displayName", email)
    click.echo(f"Authenticated as {name}")
