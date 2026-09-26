"""Command supervisor: run a command in an agent's workspace with a deadline."""

from __future__ import annotations

import asyncio
import atexit
import logging
import os
import shutil
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

from office.adapters.shared import office_dir
from office.process import _IS_WINDOWS, _child_env, _kill_tree

_log = logging.getLogger("office.commands")

# The deadline, in seconds, for one call — both the default and the ceiling.
DEADLINE_SECONDS = 150.0

# How many commands one agent may have running at once. Finished commands do
# not count — only what is still alive.
MAX_RUNNING_PER_AGENT = 3

# How much output the TOOL RESULT carries, in characters. The result carries
# the TAIL and says plainly how much was dropped — and the rest is in the log
# file below, under the workspace's `.office/`, named in the result.
RESULT_TAIL_CHARS = 32_000

# How much of one run's output the LOG FILE keeps, in characters.
#
# **When it blows, the file is INCOMPLETE and every reader has to be told so.**
# The cap writes its own last line into the file, and the tool result stops
# saying the file holds the whole of the output (office/mcp.py, _run_result).
LOG_FILE_MAX_CHARS = 16_000_000

# How long the reaper waits for the output reader after the process is gone.
_READER_JOIN_SECONDS = 10.0

# Where the full output goes, under the workspace's `.office/`.
_RUNS_SUBDIR = "runs"

# ---------------------------------------------------------------- the shell
#
# Whatever this machine has, taken off PATH.
#
# OFFICE_SHELL overrides it outright. Same shape as office/config.py's
# _detect_cli: an environment override, then a PATH lookup, then a documented
# default.
#
# The FLAGS are chosen by which interpreter was found, not by which operating
# system we are on: pwsh and powershell take `-Command`, everything else takes
# `-c`.

_POWERSHELL_NAMES = frozenset({"pwsh", "powershell"})

_shell_cache: str | None = None


def _shell_exe() -> str:
    """The shell this module runs commands through, resolved once and logged.

    Windows: `pwsh` if PATH has one, else `powershell`. POSIX: the user's own
    `$SHELL` when it names a real, executable file, else `/bin/sh`. OFFICE_SHELL
    beats both.

    Refuses only when there is nothing at all, and then it says so in a sentence
    the agent can act on.
    """
    global _shell_cache
    if _shell_cache is not None:
        return _shell_cache

    override = os.environ.get("OFFICE_SHELL")
    if override:
        found = shutil.which(override) or (override if Path(override).exists() else None)
        if found is None:
            raise ValueError(
                f"OFFICE_SHELL is set to {override!r} and there is no such program. "
                "Tell the owner; do not work around it."
            )
        candidates = [found]
    elif _IS_WINDOWS:
        candidates = [shutil.which("pwsh"), shutil.which("powershell")]
    else:
        # It is checked rather than trusted: it can name something that is not
        # a shell at all or a path that no longer exists.
        login = os.environ.get("SHELL")
        usable = login and os.path.isabs(login) and os.access(login, os.X_OK) \
            and Path(login).name not in ("false", "nologin")
        candidates = [login if usable else None, "/bin/sh"]

    for candidate in candidates:
        if candidate and Path(candidate).exists():
            _shell_cache = str(candidate)
            _log.info("commands will run through %s", _shell_cache)
            return _shell_cache

    raise ValueError(
        "this machine has no shell the office can find (looked for "
        + ", ".join(repr(c) for c in candidates if c)
        + "). Set OFFICE_SHELL. Tell the owner; do not work around it."
    )


def _shell_argv(command: str) -> list[str]:
    """The command as the argv of whatever shell _shell_exe found."""
    exe = _shell_exe()
    if Path(exe).stem.lower() in _POWERSHELL_NAMES:
        return [exe, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command]
    return [exe, "-c", command]


def popen_shell(command: str, cwd: Path, env: dict[str, str]) -> subprocess.Popen:
    """`command` through the shell, in `cwd`, with stdout and stderr on one text pipe."""
    # start_new_session puts the shell in a process group of its own.
    #
    # On Windows the flags buy the same isolation from a console Ctrl-C
    # aimed at the hub, and no console window of the command's own.
    if _IS_WINDOWS:
        kwargs = {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        }
    else:
        kwargs = {"start_new_session": True}
    return subprocess.Popen(
        _shell_argv(command),
        cwd=str(cwd),
        env=env,
        # DEVNULL, not a pipe: a command that asks a question gets EOF
        # and fails now. Nothing here is interactive.
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        # One pipe for both streams: the reader gets the output in the
        # order the command actually produced it.
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        shell=False,  # the shell is argv[0]
        **kwargs,
    )


# ---------------------------------------------------------------- one command


class Command:
    """One supervised command: a shell, its whole process tree, and its output.

    Threads: one reader (drains the merged stdout/stderr pipe into the tail ring
    and the log file) and one reaper (waits for the process, then briefly for the
    reader, collects the exit code and wakes whoever is waiting). Both daemon.
    The reaper ends when the process does — it waits on that one thing without a
    bound. A command whose tree survived its kill is reported as running and
    stays that way. The READER may not end at all; see _wait_process, whose wait
    on the reader is bounded.
    """

    def __init__(
        self, handle: str, agent: str, command: str, workspace: Path
    ) -> None:
        self.handle = handle
        self.agent = agent
        self.command = command
        self.workspace = workspace
        self.log_path = office_dir(workspace) / _RUNS_SUBDIR / f"{handle}.log"

        self.started_at = time.monotonic()
        self.last_output_at = time.monotonic()
        self.exit_code: int | None = None
        # Somebody asked for this to die — op=stop, or the end of the agent's
        # turn. Distinct from an exit code of its own.
        self.killed_by: str | None = None

        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._log = None
        self._finished = threading.Event()
        # _tail, _dropped, _log and last_output_at are written by the reader
        # thread and read by whichever thread is formatting a result. Every
        # touch of them goes through this lock.
        self._lock = threading.Lock()
        self._tail: deque[str] = deque()
        self._tail_chars = 0
        self._dropped_chars = 0
        # Characters written to the log file, and characters the cap refused to
        # write. The second being non-zero is what makes the file incomplete.
        self._file_chars = 0
        self._file_dropped_chars = 0
        self._file_capped = False
        # The reaper gave up waiting for the reader: something in the tree
        # outlived the process and still holds its output pipe.
        self._reader_stuck = False
        self._waiters: list = []

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self._open_log(f"$ {self.command}\n")
        try:
            self._spawn(self.workspace, _child_env(None, self.agent))
        except (OSError, ValueError):
            self._close_log()
            raise

        with _registry_lock:
            _live.add(self)

        threading.Thread(target=self._reap, daemon=True, name=f"office-reap-{self.handle}").start()

    def _open_log(self, header: str) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log = open(self.log_path, "w", encoding="utf-8", errors="replace", newline="")
        self._log.write(header)
        self._log.flush()
        self._file_chars = len(header)

    def _spawn(self, cwd: Path, env: dict[str, str]) -> None:
        """Start the shell running self.command in `cwd`, and the reader on its output."""
        self._proc = popen_shell(self.command, cwd, env)
        self._reader = threading.Thread(
            target=self._read, daemon=True, name=f"office-run-{self.handle}")
        self._reader.start()

    def _read(self) -> None:
        stream = self._proc.stdout
        try:
            for line in iter(stream.readline, ""):
                self._append(line)
        except (OSError, ValueError):
            # The pipe was torn out from under the read. The reaper is already
            # on its way to the exit code.
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

    def _append(self, line: str) -> None:
        """One line of output: into the tail ring and the log file."""
        with self._lock:
            self.last_output_at = time.monotonic()
            self._tail.append(line)
            self._tail_chars += len(line)
            while self._tail_chars > RESULT_TAIL_CHARS and len(self._tail) > 1:
                gone = self._tail.popleft()
                self._tail_chars -= len(gone)
                self._dropped_chars += len(gone)
            self._write_log(line)

    def _reap(self) -> None:
        self._finish(self._wait_process())

    def _wait_process(self) -> int:
        """The process's exit code, once it and then the reader are done.

        The wait on the reader is BOUNDED.
        """
        code = self._proc.wait()
        self._reader.join(_READER_JOIN_SECONDS)
        if self._reader.is_alive():
            _log.error(
                "%s: %s exited but something it started still holds its output pipe; "
                "the end of its output may be missing", self.agent, self.handle,
            )
            with self._lock:
                self._reader_stuck = True
        return code

    def _finish(self, code: int | None) -> None:
        """Record the exit code, close the log and wake whoever waits on this command."""
        with self._lock:
            self.exit_code = code
            self._close_log()
        self._finished.set()
        with self._lock:
            waiters = list(self._waiters)
        for wake in waiters:
            wake()

    def _write_log(self, line: str) -> None:
        """Append one line to the log file, up to LOG_FILE_MAX_CHARS.

        Called with _lock held. Past the cap nothing more is written and the
        file is closed — but not before a last line saying so, inside the file
        itself. Everything after that line is lost.
        """
        if self._file_capped:
            # Still counting after the file was closed.
            self._file_dropped_chars += len(line)
            return
        if self._log is None:
            return
        if self._file_chars + len(line) > LOG_FILE_MAX_CHARS:
            self._file_capped = True
            self._file_dropped_chars += len(line)
            self._log.write(
                f"\n[office] this run passed the {LOG_FILE_MAX_CHARS} character output cap. "
                "Nothing after this line was written. The rest of the output is GONE - it is "
                "not kept anywhere else; only the last lines of it reached the tool result.\n"
            )
            self._close_log()
            return
        self._log.write(line)
        self._log.flush()
        self._file_chars += len(line)

    def _close_log(self) -> None:
        if self._log is not None:
            try:
                self._log.flush()
                self._log.close()
            except OSError:
                pass
            self._log = None

    def kill(self, by: str) -> None:
        """End the whole tree. Idempotent, and safe on a command already dead."""
        proc = self._proc
        if proc is None or self._finished.is_set():
            # Already over. Not marked as killed: a run that finished on its own
            # and was then stopped a moment later reported its own exit code.
            return
        self.killed_by = by
        if proc.poll() is None:
            _kill_tree(proc.pid)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _log.error("%s: %s survived the tree kill", self.agent, self.handle)

    def release(self) -> None:
        """Let go of what this command holds: its log file.

        Normally closed already — _reap closes it the moment the process is
        known dead. This is the other way out: a command the hub let go of
        without ever reaping, which is shutdown_all's path and kill_agent's on a
        process that outlived its kill. Idempotent.
        """
        with self._lock:
            self._close_log()
        with _registry_lock:
            _live.discard(self)

    # -- state -------------------------------------------------------------

    @property
    def running(self) -> bool:
        return not self._finished.is_set()

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def quiet_for(self) -> float:
        """Seconds since the last byte arrived on either stream.

        Reported, never acted on.
        """
        with self._lock:
            return max(0.0, time.monotonic() - self.last_output_at)

    def output(self) -> tuple[str, int]:
        """The tail the result will carry, and how many characters were dropped
        ahead of it."""
        with self._lock:
            return "".join(self._tail), self._dropped_chars

    @property
    def output_truncated_at_the_end(self) -> bool:
        """True when the office stopped following this run's output before the
        pipe closed. See _reap and _READER_JOIN_SECONDS."""
        return self._reader_stuck

    def log_state(self) -> tuple[int, int]:
        """Characters in the log file, and characters the cap kept out of it.

        The second one being non-zero is the only thing that makes the file an
        incomplete record.
        """
        with self._lock:
            return self._file_chars, self._file_dropped_chars

    async def wait_async(self, timeout: float) -> bool:
        """Await the command's end, up to `timeout`. True if it ended in time.

        This is what keeps a 150-second deadline off the threadpool. Waiting
        here costs an asyncio.Event and nothing else.

        The wake comes from the reaper thread; it goes through the loop with
        call_soon_threadsafe. A loop already closed means the hub is going down
        and there is no longer anybody to wake; that is the only thing the
        RuntimeError below can mean.
        """
        if self._finished.is_set():
            return True
        loop = asyncio.get_running_loop()
        event = asyncio.Event()

        def _wake() -> None:
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                pass

        with self._lock:
            if self._finished.is_set():
                return True
            self._waiters.append(_wake)
        try:
            await asyncio.wait_for(event.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            with self._lock:
                if _wake in self._waiters:
                    self._waiters.remove(_wake)


# ---------------------------------------------------------------- the registry

# Reentrant: `start()` below holds it across Command.start(), which takes it
# again to add itself to _live.
_registry_lock = threading.RLock()
# handle -> Command, for every command this hub has started and not yet let go
# of. Finished ones stay until the agent's turn ends.
_commands: dict[str, Command] = {}
# Every command holding OS resources. It covers only the exits that run code.
_live: set[Command] = set()
_next_handle = 0


def _new_handle() -> str:
    global _next_handle
    with _registry_lock:
        _next_handle += 1
        return f"run-{_next_handle}"


def _admit(agent: str) -> None:
    """Refuse a new command for an agent at MAX_RUNNING_PER_AGENT. Call with the lock held."""
    running = [c for c in _commands.values() if c.agent == agent and c.running]
    if len(running) >= MAX_RUNNING_PER_AGENT:
        names = ", ".join(c.handle for c in running)
        raise ValueError(
            f"you already have {len(running)} commands running ({names}) and the limit is "
            f"{MAX_RUNNING_PER_AGENT}. Stop one you no longer need — run(op=stop, "
            f"handle='{running[0].handle}') — before starting another."
        )


def start(agent: str, workspace: Path | str, command: str) -> Command:
    """Spawn `command` in `workspace` on `agent`'s behalf. Never waits."""
    # Counting, spawning and registering under ONE hold of the lock. The lock is
    # an RLock: Command.start() acquires it again to join _live.
    #
    # It also fixes the order against kill_agent, which takes the same lock: a
    # turn ending now either sees this command fully registered and kills it, or
    # runs entirely before it exists.
    with _registry_lock:
        _admit(agent)
        # Inside the hold: a refused start does not consume a handle number
        # (_new_handle takes the same reentrant lock).
        cmd = Command(_new_handle(), agent, command, Path(workspace))
        cmd.start()
        _commands[cmd.handle] = cmd
    _log.info("%s started %s: %s", agent, cmd.handle, command)
    return cmd


def enroll(agent: str, make) -> Command:
    """Register the command `make(handle)` builds, under the same count as start().

    `make` runs with the registry lock held and must not block. The command is
    registered and live from here on: kill_agent and shutdown_all reach it, and
    it counts towards MAX_RUNNING_PER_AGENT until it finishes.
    """
    with _registry_lock:
        _admit(agent)
        cmd = make(_new_handle())
        _commands[cmd.handle] = cmd
        _live.add(cmd)
    _log.info("%s enrolled %s: %s", agent, cmd.handle, cmd.command)
    return cmd


def discard(cmd: Command) -> None:
    """Forget a finished command whose start was refused after it was enrolled."""
    with _registry_lock:
        _commands.pop(cmd.handle, None)
        _live.discard(cmd)


def get(agent: str, handle: str) -> Command:
    """The agent's own command by handle. Raises ValueError otherwise."""
    with _registry_lock:
        cmd = _commands.get(handle)
    if cmd is None:
        raise ValueError(
            f"no command with handle {handle!r} — handles do not survive the end of the "
            "turn that started them, and everything that turn started was stopped with it."
        )
    if cmd.agent != agent:
        raise ValueError(f"{handle!r} belongs to {cmd.agent}, not to you")
    return cmd


def list_for(agent: str) -> list[Command]:
    """This agent's own commands, oldest first. Never anyone else's.

    Same order as the handles were issued.
    """
    with _registry_lock:
        mine = [c for c in _commands.values() if c.agent == agent]
    return sorted(mine, key=lambda c: c.started_at)


def kill_agent(agent: str, by: str) -> int:
    """Stop everything `agent` has running and forget its handles. Returns how
    many were still alive.

    Called at the end of every turn (office/bus.py, _watch). Unconditional.
    """
    with _registry_lock:
        mine = [c for c in _commands.values() if c.agent == agent]
        for cmd in mine:
            _commands.pop(cmd.handle, None)
    killed = 0
    for cmd in mine:
        if cmd.running:
            killed += 1
            _log.info("%s's turn ended with %s still running; stopping it", agent, cmd.handle)
            cmd.kill(by)
        cmd.release()
    return killed


def shutdown_all() -> None:
    """Take every supervised command down with the hub.

    The orderly path, and the only one that ends a command at the moment the hub
    ends. A hub that is TERMINATED never reaches this line at all.
    """
    with _registry_lock:
        survivors = list(_live)
    for cmd in survivors:
        cmd.kill("hub shutdown")
        cmd.release()


atexit.register(shutdown_all)
