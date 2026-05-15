"""Tests for tsh.cli.hook — per-repo git post-checkout install."""
from __future__ import annotations
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from tsh.cli.main import cli


@pytest.fixture
def git_repo(tmp_path: Path, monkeypatch):
    """Initialize a throwaway git repo and chdir into it."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_hook_install_writes_post_checkout(git_repo: Path):
    result = CliRunner().invoke(cli, ["hook", "install"])
    assert result.exit_code == 0, result.output
    hook_path = git_repo / ".git" / "hooks" / "post-checkout"
    assert hook_path.exists()
    content = hook_path.read_text()
    assert "tsh-managed post-checkout hook" in content
    assert "tsh switch --from-branch" in content


def test_hook_install_idempotent_when_ours(git_repo: Path):
    CliRunner().invoke(cli, ["hook", "install"])
    result = CliRunner().invoke(cli, ["hook", "install"])
    assert result.exit_code == 0
    content = (git_repo / ".git" / "hooks" / "post-checkout").read_text()
    assert "tsh-managed post-checkout hook" in content


def test_hook_install_refuses_on_conflict(git_repo: Path):
    hook_path = git_repo / ".git" / "hooks" / "post-checkout"
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    hook_path.write_text("#!/bin/sh\necho 'some other hook'\n")
    result = CliRunner().invoke(cli, ["hook", "install"])
    assert result.exit_code != 0
    assert "conflict" in result.output.lower() or "exists" in result.output.lower()
    # Original content preserved.
    assert "echo 'some other hook'" in hook_path.read_text()


def test_hook_install_force_overwrites_conflict(git_repo: Path):
    hook_path = git_repo / ".git" / "hooks" / "post-checkout"
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    hook_path.write_text("#!/bin/sh\necho 'foreign'\n")
    result = CliRunner().invoke(cli, ["hook", "install", "--force"])
    assert result.exit_code == 0
    assert "tsh-managed" in hook_path.read_text()


def test_hook_uninstall_removes_ours(git_repo: Path):
    CliRunner().invoke(cli, ["hook", "install"])
    result = CliRunner().invoke(cli, ["hook", "uninstall"])
    assert result.exit_code == 0
    hook_path = git_repo / ".git" / "hooks" / "post-checkout"
    assert not hook_path.exists()


def test_hook_uninstall_refuses_foreign(git_repo: Path):
    hook_path = git_repo / ".git" / "hooks" / "post-checkout"
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    hook_path.write_text("#!/bin/sh\necho 'foreign'\n")
    result = CliRunner().invoke(cli, ["hook", "uninstall"])
    assert result.exit_code != 0
    assert hook_path.exists()
    assert "echo 'foreign'" in hook_path.read_text()


def test_hook_status_not_installed(git_repo: Path):
    result = CliRunner().invoke(cli, ["hook", "status"])
    assert result.exit_code == 0
    assert "not installed" in result.output.lower()


def test_hook_status_installed(git_repo: Path):
    CliRunner().invoke(cli, ["hook", "install"])
    result = CliRunner().invoke(cli, ["hook", "status"])
    assert result.exit_code == 0
    assert "installed" in result.output.lower()
    assert "not installed" not in result.output.lower()


def test_hook_status_conflicting(git_repo: Path):
    hook_path = git_repo / ".git" / "hooks" / "post-checkout"
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    hook_path.write_text("#!/bin/sh\necho 'foreign'\n")
    result = CliRunner().invoke(cli, ["hook", "status"])
    assert "conflict" in result.output.lower()


def test_hook_install_outside_repo_fails(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["hook", "install"])
    assert result.exit_code != 0
    out = result.output.lower()
    assert "not a git repository" in out or "not in a repo" in out
