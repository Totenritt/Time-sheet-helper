"""tsh hook install/uninstall/status — per-repo git post-checkout integration."""
from __future__ import annotations

import subprocess
from pathlib import Path

import click


SENTINEL = "# tsh-managed post-checkout hook"

HOOK_CONTENT = f"""#!/bin/sh
{SENTINEL} — do not edit; manage with `tsh hook install/uninstall`
# Args: $1=prev_HEAD $2=new_HEAD $3=branch_checkout_flag (1 if branch)
[ "$3" = "1" ] || exit 0
tsh switch --from-branch >/dev/null 2>&1
exit 0
"""


def _git_dir() -> Path | None:
    """Return Path to the current repo's .git directory, or None if not in a repo."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            capture_output=True, text=True, timeout=2.0,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if out.returncode != 0:
        return None
    return Path(out.stdout.strip()).resolve()


def _hook_path(git_dir: Path) -> Path:
    return git_dir / "hooks" / "post-checkout"


@click.group("hook")
def hook_group() -> None:
    """Per-repo git hook management for `tsh switch --from-branch`."""


@hook_group.command("install")
@click.option("--force", is_flag=True, help="Overwrite an existing non-tsh hook.")
def install(force: bool) -> None:
    """Write .git/hooks/post-checkout in the current repo."""
    git_dir = _git_dir()
    if git_dir is None:
        raise click.ClickException("not a git repository")
    hook_path = _hook_path(git_dir)
    hook_path.parent.mkdir(parents=True, exist_ok=True)

    if hook_path.exists():
        existing = hook_path.read_text()
        if SENTINEL in existing:
            hook_path.write_text(HOOK_CONTENT)
            hook_path.chmod(0o755)
            click.echo(f"hook already installed at {hook_path} (refreshed)")
            return
        if not force:
            raise click.ClickException(
                f"conflicting hook exists at {hook_path}\n"
                f"--- existing content ---\n{existing}\n--- end ---\n"
                f"re-run with --force to overwrite"
            )

    hook_path.write_text(HOOK_CONTENT)
    hook_path.chmod(0o755)
    click.echo(f"installed {hook_path}")


@hook_group.command("uninstall")
def uninstall() -> None:
    """Remove the post-checkout hook if it's the one we wrote."""
    git_dir = _git_dir()
    if git_dir is None:
        raise click.ClickException("not a git repository")
    hook_path = _hook_path(git_dir)
    if not hook_path.exists():
        click.echo("no hook to uninstall")
        return
    if SENTINEL not in hook_path.read_text():
        raise click.ClickException(
            f"hook at {hook_path} was not installed by tsh; "
            f"remove it manually if you want it gone"
        )
    hook_path.unlink()
    click.echo(f"removed {hook_path}")


@hook_group.command("status")
def status_cmd() -> None:
    """Print whether the current repo has the tsh hook installed."""
    git_dir = _git_dir()
    if git_dir is None:
        click.echo("not a git repository")
        return
    hook_path = _hook_path(git_dir)
    if not hook_path.exists():
        click.echo("not installed")
        return
    content = hook_path.read_text()
    if SENTINEL in content:
        click.echo(f"installed at {hook_path}")
    else:
        click.echo(
            f"conflicting hook present at {hook_path} "
            f"(run `tsh hook install --force` to replace)"
        )
