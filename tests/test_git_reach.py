"""Which repository git finds from a directory under the office's data directory, in an
agent's environment and in the hub's own calls.

The repositories here are invented: empty ones made with `git init`.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from office import git
from office.process import _child_env


def _toplevel(cwd: Path, env: dict[str, str]) -> tuple[int, str]:
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=str(cwd), env=env,
        capture_output=True, text=True, timeout=60,
    )
    return proc.returncode, proc.stdout.strip()


def test_from_the_sandbox_git_does_not_reach_the_owners_repository(config, monkeypatch):
    monkeypatch.setenv("OFFICE_ROOT", str(config.root))
    owner = config.root.parent
    subprocess.run(["git", "init", "."], cwd=str(owner), capture_output=True, timeout=60)
    workspace = config.ws_dir / "ws-1"
    workspace.mkdir()
    subprocess.run(["git", "init", "."], cwd=str(workspace), capture_output=True, timeout=60)
    sandbox = config.scratch_dir / "ws-1"
    sandbox.mkdir()

    agent = _child_env(None, "exec1")
    assert _toplevel(sandbox, agent)[0] != 0
    code, top = _toplevel(workspace, agent)
    assert code == 0 and Path(top).resolve() == workspace.resolve()
    assert git.git(["rev-parse", "--show-toplevel"], cwd=sandbox, check=False).returncode != 0

    # Control: git without the office's environment climbs into the owner's repository.
    plain = {k: v for k, v in os.environ.items() if k != "GIT_CEILING_DIRECTORIES"}
    code, top = _toplevel(sandbox, plain)
    assert code == 0 and Path(top).resolve() == owner.resolve()
