"""What a push can reach from the environment an agent's process carries."""

from __future__ import annotations

import os
import socket
import subprocess
import threading
import time

from office import git as officegit
from office.process import _child_env

#: The interface every address in this module points at.
_LOOPBACK = "127.0.0.1"


def _git(args, cwd, env=None):
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _isolated(env: dict[str, str], tmp_path) -> dict[str, str]:
    """`env` with the machine's own git configuration out of reach."""
    nowhere = str(tmp_path / "no-such-git-config")
    return {
        **env,
        "GIT_CONFIG_GLOBAL": nowhere,
        "GIT_CONFIG_SYSTEM": nowhere,
        "GIT_TERMINAL_PROMPT": "0",
        "NO_PROXY": "*",
        "no_proxy": "*",
    }


def _source_repo(tmp_path):
    """A repository with one commit on a branch named `main`."""
    path = tmp_path / "source"
    path.mkdir()
    _git(["init", "--initial-branch=main", "."], path)
    (path / "f.txt").write_text("payload\n", encoding="utf-8")
    _git(["add", "f.txt"], path)
    _git(
        ["-c", "user.name=tester", "-c", "user.email=tester@example.com",
         "commit", "-m", "one"],
        path,
    )
    return path


def _rev(repo, ref):
    proc = _git(["rev-parse", "--verify", ref], repo)
    return proc.stdout.strip() if proc.returncode == 0 else None


def _address(prefix: str, port: int) -> str:
    """An address beginning with `prefix` and pointing at the loopback interface.

    A prefix that ends a URL scheme takes an explicit port; the scp-like form
    takes none.
    """
    if prefix.endswith("://"):
        return f"{prefix}{_LOOPBACK}:{port}/x.git"
    return f"{prefix}{_LOOPBACK}:x.git"


class _Listener:
    """A loopback port that counts the connections reaching it and closes each one."""

    def __init__(self) -> None:
        self._socket = socket.socket()
        self._socket.bind((_LOOPBACK, 0))
        self._socket.listen(16)
        self.port = self._socket.getsockname()[1]
        self.arrivals = 0
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                connection, _ = self._socket.accept()
            except OSError:
                return
            self.arrivals += 1
            connection.close()

    def wait_for_arrival(self, mark: int, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.arrivals > mark:
                return True
            time.sleep(0.01)
        return self.arrivals > mark

    def close(self) -> None:
        self._socket.close()


def test_a_push_from_an_agents_environment_opens_no_outbound_connection(tmp_path):
    source = _source_repo(tmp_path)
    bare = tmp_path / "destination.git"
    _git(["init", "--bare", str(bare)], tmp_path)

    blocked = _isolated(_child_env(None, "exec1"), tmp_path)
    ordinary = _isolated(dict(os.environ), tmp_path)

    control = _git(["push", bare.as_posix(), "main"], source, blocked)
    assert control.returncode == 0
    assert _rev(bare, "refs/heads/main") == _rev(source, "refs/heads/main")

    listener = _Listener()
    try:
        for index, prefix in enumerate(officegit._OUTBOUND):
            remote = _address(prefix, listener.port)

            mark = listener.arrivals
            subject = _git(["push", remote, "main"], source, blocked)
            assert subject.returncode != 0, prefix
            assert listener.arrivals == mark, prefix

            if str(listener.port) in remote:
                mark = listener.arrivals
                comparison = _git(["push", remote, "main"], source, ordinary)
                assert comparison.returncode != 0, prefix
                assert listener.wait_for_arrival(mark), prefix

            name = f"probe{index}"
            _git(["remote", "add", name, remote], source, ordinary)
            under_block = _git(
                ["remote", "get-url", "--push", name], source, blocked
            ).stdout.strip()
            under_ordinary = _git(
                ["remote", "get-url", "--push", name], source, ordinary
            ).stdout.strip()

            assert under_ordinary.startswith(prefix), prefix
            assert not under_block.startswith(tuple(officegit._OUTBOUND)), prefix
    finally:
        listener.close()
