"""What a stage run's snapshot takes from a workspace and leaves in it, what a
switch and a preparation leave in a stage's tree, and what a run brings back.

Every repository here is invented: a few text files, one ignored directory, and
files under an LFS pattern whose bytes are made up.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from office import commands, git, stages

MAIN = "main"
MARK = "test-mark"
IGNORED = "Library/cache.dat"
HOOKS = ("pre-push", "post-index-change", "reference-transaction")
LFS_FILTERS = ["-c", "filter.lfs.process=git-lfs filter-process",
               "-c", "filter.lfs.required=true"]
#: The first bytes of an LFS pointer file.
POINTER = b"version https://git-lfs"
#: What a file under the LFS pattern holds.
CONTENT = bytes(range(256)) * 40
#: What a run writes over a file under the LFS pattern; `CHANGED_EXPR` makes it in Python.
CHANGED = bytes(range(255, -1, -1)) * 40
CHANGED_EXPR = "bytes(range(255,-1,-1))*40"
#: A path under the LFS pattern with a space in it, in the base commit.
SPACED = "sub dir/odd 1.bin"
#: A path under the LFS pattern that a glob would read as matching SPACED.
BRACKETED = "sub dir/odd [1].bin"
#: Must not be the name of an agent or a stage in the office whose id marks.office_id()
#: returns during these tests.
AGENT = "stage-test-agent"
STAGE = "stage-test"


def _git(args, cwd, env=None):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=120, env=env
    )


def _project(config, *, large_file: bool = False):
    """project.git from an owner's repository holding a.txt, a .gitignore of Library/ and a
    .gitattributes putting *.bin under LFS; with `large_file`, also big.bin and SPACED
    holding CONTENT."""
    owner = git.owner_repo(config)
    _git(["init", f"--initial-branch={MAIN}", "."], owner)
    (owner / ".gitignore").write_text("Library/\n", encoding="utf-8", newline="\n")
    (owner / ".gitattributes").write_text("*.bin filter=lfs diff=lfs merge=lfs -text\n",
                                          encoding="utf-8", newline="\n")
    (owner / "a.txt").write_text("one\n", encoding="utf-8", newline="\n")
    paths = [".gitignore", ".gitattributes", "a.txt"]
    if large_file:
        (owner / "big.bin").write_bytes(CONTENT)
        (owner / SPACED).parent.mkdir()
        (owner / SPACED).write_bytes(CONTENT)
        paths += ["big.bin", SPACED]
    _git([*LFS_FILTERS, "add", *paths], owner)
    _git(["-c", "user.name=o", "-c", "user.email=o@example.com", "commit", "-m", "base"], owner)
    git.init_project(config, branch=MAIN)


def _tree_of(workspace: Path, scratch: Path, *kept: str) -> str:
    """The tree git would record for the workspace's files, read through a copy of its index;
    the paths in `kept` as the index has them."""
    index = scratch / "tree.index"
    shutil.copyfile(workspace / ".git" / "index", index)
    env = {**os.environ, "GIT_INDEX_FILE": str(index)}
    _git(["add", "-A", "--", ".", *[f":(exclude,literal){p}" for p in kept]], workspace, env)
    return _git(["write-tree"], workspace, env).stdout.strip()


def _install_hooks(repo: Path, marker: Path) -> None:
    """Hooks in the repository's own hooks directory that each append their name to `marker`."""
    for name in HOOKS:
        hook = repo / ".git" / "hooks" / name
        hook.write_text(f'#!/bin/sh\necho {name} >> "{marker.as_posix()}"\n',
                        encoding="utf-8", newline="\n")
        hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _ran(marker: Path) -> list[str]:
    return marker.read_text(encoding="utf-8").split() if marker.exists() else []


def test_a_snapshot_takes_the_working_tree_and_leaves_the_workspace_as_it_was(config, tmp_path):
    _project(config)
    ws = git.create_workspace(config, "exec1")
    tree = Path(ws.path)
    (tree / "a.txt").write_text("one\ntwo\n", encoding="utf-8", newline="\n")
    (tree / "new.meta").write_text("guid: 1\n", encoding="utf-8", newline="\n")
    (tree / "Library").mkdir()
    (tree / IGNORED).write_text("warm\n", encoding="utf-8")

    status_before = _git(["status", "--porcelain", "--ignored"], tree).stdout
    head_before = _git(["rev-parse", "HEAD"], tree).stdout
    index = tree / ".git" / "index"
    index_before = index.read_bytes()
    marker = tmp_path / "hooks-ran"
    _install_hooks(tree, marker)

    s = stages.snapshot(config, tree, "unit", ws.id, MARK, config.stages_dir / ".t.index")

    assert _ran(marker) == []
    assert index.read_bytes() == index_before
    assert _git(["rev-parse", "HEAD"], tree).stdout == head_before
    assert _git(["status", "--porcelain", "--ignored"], tree).stdout == status_before

    project = git.project_git(config)
    assert _git(["rev-parse", f"refs/office/stage/unit/{ws.id}"], project).stdout.strip() == s
    assert _git(["rev-parse", f"{s}^"], tree).stdout == head_before
    taken = _git(["ls-tree", "-r", "--name-only", s], tree).stdout.split()
    assert "new.meta" in taken
    assert IGNORED not in taken
    assert _git(["show", f"{s}:a.txt"], tree).stdout == "one\ntwo\n"

    # Control: the same `add -A` on the workspace's own index, with its hooks.
    _git(["add", "-A"], tree)
    assert "post-index-change" in _ran(marker)
    assert _git(["status", "--porcelain", "--ignored"], tree).stdout != status_before


def test_a_switch_keeps_ignored_files_and_removes_untracked_ones(config):
    _project(config)
    tree = config.stages_dir / "unit"
    assert stages._clone(config, tree, MARK, lambda text: None) is None
    (tree / "Library").mkdir()
    (tree / IGNORED).write_text("warm\n", encoding="utf-8")
    (tree / "stray.txt").write_text("left behind\n", encoding="utf-8")

    assert stages._switch(config, tree, f"refs/heads/{MAIN}", None, MARK, lambda text: None) is None

    assert (tree / IGNORED).exists()
    assert not (tree / "stray.txt").exists()

    # Control: git's own clean with -x takes the ignored file.
    _git(["clean", "-fdx"], tree)
    assert not (tree / IGNORED).exists()


def test_a_preparation_fills_a_tree_its_clone_left_with_pointers(config, conn):
    _project(config, large_file=True)
    tree = config.stages_dir / STAGE
    assert stages._clone(config, tree, MARK, lambda text: None) is None
    # Control: the clone alone leaves the large file as a pointer.
    assert (tree / "big.bin").read_bytes().startswith(POINTER)

    stages._prepare(conn, config, STAGE, "", False, stages._Preparation(STAGE))

    assert (tree / "big.bin").read_bytes() == CONTENT


def test_a_run_brings_back_what_it_wrote_large_files_included_and_the_commands_reproduce_it(
    config, conn, tmp_path
):
    _project(config, large_file=True)
    ws = git.create_workspace(config, AGENT)
    workspace = Path(ws.path)
    stages.create(conn, config, STAGE, "", actor="director")
    with stages._lock:
        preparation = stages._state[STAGE].preparation
    preparation.thread.join(300)
    python = Path(sys.executable).as_posix()
    run = stages.start_run(
        conn, config, AGENT, {"id": ws.id, "path": ws.path}, STAGE,
        f"{python} -c \"open('gen.bin','wb').write(bytes(range(256))*40);"
        f"open('big.bin','wb').write({CHANGED_EXPR});"
        f"open('{BRACKETED}','wb').write({CHANGED_EXPR});"
        f"open('x.meta','w').write('guid:1\\n');open('a.txt','a').write('two\\n')\"",
    )
    assert run._finished.wait(300)

    assert run.exit_code == 0
    assert sorted(run.changed) == ["a.txt", "big.bin", "gen.bin", BRACKETED, "x.meta"]
    assert sorted(run.lfs) == ["big.bin", "gen.bin", BRACKETED]
    project = git.project_git(config)
    assert _git(["rev-parse", f"refs/office/stage/{STAGE}/{ws.id}"], project).stdout.strip() \
        == run.change
    assert _git(["cat-file", "-p", f"{run.change}:gen.bin"], project).stdout.startswith(
        POINTER.decode())
    change_tree = _git(["rev-parse", f"{run.change}^{{tree}}"], project).stdout.strip()
    # Control: before the commands, the workspace does not hold the change.
    assert _tree_of(workspace, tmp_path) != change_tree
    # The agent's own edit after the snapshot, under a path the bracketed one would match
    # as a glob.
    (workspace / SPACED).write_bytes(b"the agent's edit")

    given = [line.strip() for line in run.report() if line.startswith("  git ")]
    for line in given:
        done = subprocess.run(commands._shell_argv(line), cwd=str(workspace),
                              capture_output=True, text=True, timeout=120)
        assert done.returncode == 0, (line, done.stdout + done.stderr)

    assert _tree_of(workspace, tmp_path, SPACED) == change_tree
    assert (workspace / "gen.bin").read_bytes() == CONTENT
    assert (workspace / "big.bin").read_bytes() == CHANGED
    assert (workspace / BRACKETED).read_bytes() == CHANGED
    assert (workspace / SPACED).read_bytes() == b"the agent's edit"


def test_publish_runs_no_hook_of_the_workspace(config, tmp_path):
    _project(config)
    ws = git.create_workspace(config, "exec1")
    tree = Path(ws.path)
    marker = tmp_path / "hooks-ran"
    _install_hooks(tree, marker)
    _git(["switch", "-c", "feature"], tree)
    (tree / "b.txt").write_text("b\n", encoding="utf-8", newline="\n")
    _git(["add", "b.txt"], tree)
    _git(["commit", "-m", "b"], tree)
    marker.unlink(missing_ok=True)

    git.publish(config, tree)

    assert _ran(marker) == []
    project = git.project_git(config)
    assert (_git(["rev-parse", "refs/heads/feature"], project).stdout
            == _git(["rev-parse", "HEAD"], tree).stdout)

    # Control: a push of the same branch from the workspace's own git.
    _git(["push", "origin", "HEAD:refs/heads/feature-copy"], tree)
    assert {"pre-push", "reference-transaction"} <= set(_ran(marker))
