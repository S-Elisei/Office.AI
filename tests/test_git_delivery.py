"""Where the two repositories stand after a merge the owner's repository refuses,
and after one it takes."""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

from office import git

MAIN = "main"
BRANCH = "feature-x"
ADDED = "added.txt"


def _git(args, cwd):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=120
    )


def _head_of(repo, branch):
    proc = _git(["rev-parse", "--verify", f"refs/heads/{branch}"], repo)
    return proc.stdout.strip() if proc.returncode == 0 else None


def _write(path, text):
    """One line ending, LF, whatever this platform would otherwise write."""
    path.write_text(text, encoding="utf-8", newline="\n")


def _owner_with_history(path, outside, monkeypatch):
    """The owner's repository, with one commit on `MAIN`, in the shape a plain
    clone leaves it in: a CRLF working tree against an LF index.

    `core.autocrlf` sits above the repository, where an owner-path call cannot
    read it, and the machine's own configuration is out of reach of every git
    this test runs.
    """
    conversion = outside / "gitconfig"
    conversion.write_text("[core]\n\tautocrlf = true\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(conversion))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(outside / "no-system-config"))

    _git(["init", f"--initial-branch={MAIN}", "."], path)
    _git(["config", "user.name", "owner"], path)
    _git(["config", "user.email", "owner@example.com"], path)
    _write(path / "README.md", "start\n")
    _git(["-c", "core.autocrlf=false", "add", "README.md"], path)
    _git(["commit", "-m", "start"], path)

    (path / "README.md").unlink()
    _git(["checkout", "--", "README.md"], path)
    assert (path / "README.md").read_bytes().count(b"\r\n") == 1


def _publish_a_branch(config):
    """A branch in the office repository that adds one file to `MAIN`."""
    tree = Path(git.create_workspace(config, "exec1").path)
    _git(["switch", "-c", BRANCH], tree)
    _write(tree / ADDED, "payload\n")
    _git(["add", ADDED], tree)
    _git(["commit", "-m", "add a file"], tree)
    git.publish(config, tree)


def _refuse_delivery(owner):
    """A pre-receive hook in the owner's repository that takes nothing."""
    hooks = owner / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    hook = hooks / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8", newline="\n")
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return hook


def test_a_merge_the_owners_repository_refuses_moves_neither_repository(
    config, tmp_path_factory, monkeypatch
):
    owner = git.owner_repo(config)
    _owner_with_history(owner, tmp_path_factory.mktemp("outside"), monkeypatch)
    git.init_project(config, branch=MAIN)
    _publish_a_branch(config)

    project = git.project_git(config)
    office_before = _head_of(project, MAIN)
    owner_before = _head_of(owner, MAIN)
    assert office_before and owner_before

    hook = _refuse_delivery(owner)
    git.merge(config, BRANCH, MAIN)

    assert _head_of(project, MAIN) == office_before
    assert _head_of(owner, MAIN) == owner_before
    assert not (owner / ADDED).exists()

    hook.unlink()
    git.merge(config, BRANCH, MAIN)

    delivered = _head_of(owner, MAIN)
    assert delivered != owner_before
    assert _head_of(project, MAIN) == delivered
    assert "payload" in (owner / ADDED).read_text(encoding="utf-8")
