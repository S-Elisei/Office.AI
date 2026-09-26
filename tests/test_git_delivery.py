"""Where the two repositories stand after a merge the owner's repository refuses,
and after one it takes.

The repositories are invented: a README, a text file, and two files under an LFS
pattern whose bytes are made up.
"""

from __future__ import annotations

import hashlib
import stat
import subprocess
from pathlib import Path

from office import git

MAIN = "main"
BRANCH = "feature-x"
ADDED = "added.txt"
#: A large file already in the owner's history, and one the branch adds.
OLD_LARGE, OLD_BYTES = "old.bin", bytes(range(256)) * 50
NEW_LARGE, NEW_BYTES = "new.bin", bytes(reversed(range(256))) * 50


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
    """The owner's repository, with one commit on `MAIN` holding OLD_LARGE under
    LFS, in the shape a plain clone leaves it in: a CRLF working tree against an
    LF index.

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
    _write(path / ".gitattributes", "*.bin filter=lfs diff=lfs merge=lfs -text\n")
    (path / OLD_LARGE).write_bytes(OLD_BYTES)
    _git(["-c", "filter.lfs.process=git-lfs filter-process", "-c", "filter.lfs.required=true",
          "add", ".gitattributes", OLD_LARGE], path)
    _git(["commit", "-m", "start"], path)

    (path / "README.md").unlink()
    _git(["checkout", "--", "README.md"], path)
    assert (path / "README.md").read_bytes().count(b"\r\n") == 1


def _publish_a_branch(config):
    """A branch in the office repository that adds a text file and a large file to
    `MAIN`, published from a workspace that never pulled OLD_LARGE's content."""
    tree = Path(git.create_workspace(config, "exec1").path)
    _git(["switch", "-c", BRANCH], tree)
    _write(tree / ADDED, "payload\n")
    (tree / NEW_LARGE).write_bytes(NEW_BYTES)
    _git(["add", ADDED, NEW_LARGE], tree)
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
    assert not (owner / NEW_LARGE).exists()

    hook.unlink()
    git.merge(config, BRANCH, MAIN)

    delivered = _head_of(owner, MAIN)
    assert delivered != owner_before
    assert _head_of(project, MAIN) == delivered
    assert "payload" in (owner / ADDED).read_text(encoding="utf-8")
    assert (owner / NEW_LARGE).read_bytes() == NEW_BYTES


def test_a_merge_is_delivered_after_the_owner_committed_two_versions_between_intakes(
    config, tmp_path_factory, monkeypatch
):
    owner = git.owner_repo(config)
    _owner_with_history(owner, tmp_path_factory.mktemp("outside"), monkeypatch)
    git.init_project(config, branch=MAIN)
    between, last = bytes([1]) * 12_800, bytes([2]) * 12_800
    for version in (between, last):
        (owner / OLD_LARGE).write_bytes(version)
        _git(["add", OLD_LARGE], owner)
        _git(["commit", "-m", "a new version"], owner)
    _publish_a_branch(config)

    # The version between the two intakes is in the owner's store and not the office's.
    stored = hashlib.sha256(between).hexdigest()
    assert not (git.project_git(config) / "lfs" / "objects" / stored[:2] / stored[2:4]
                / stored).exists()

    git.merge(config, BRANCH, MAIN)

    assert _head_of(owner, MAIN) == _head_of(git.project_git(config), MAIN)
    assert (owner / NEW_LARGE).read_bytes() == NEW_BYTES
    assert (owner / OLD_LARGE).read_bytes() == last
