"""Turn supervisor: one vendor process per turn."""

from __future__ import annotations

import atexit
import logging
import os
import queue
import re
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from office import git, marks
from office.adapters import shared
from office.adapters.base import Event, context_limit_for

_log = logging.getLogger("office.process")

TAIL_LINES = 200
TAIL_FLUSH_SECONDS = 5.0

# How long a process may go on living after it has itself announced the end of
# its turn, before this module closes it.
LINGER_GRACE_SECONDS = 15.0

# How often an adapter that reports occupancy OUT OF BAND is asked for it.
CONTEXT_POLL_SECONDS = 5.0

# POST-MORTEM ONLY. A death at or above this much of the window reads as
# `context_overflow`. Nothing consults it while a turn is alive.
CONTEXT_OVERFLOW_AT = 0.85

_QUOTA_SIGNS = re.compile(
    r"usage limit|rate.?limit|quota|resource[ _]exhausted|insufficient_quota|\b429\b",
    re.IGNORECASE,
)

# A vendor writing about itself on stderr. Anything the vendor stamps with its
# own name becomes an event.
_VENDOR_MARKS = re.compile(r"^\s*(\[agy\]|codex:|claude:|warning:|warn:)", re.IGNORECASE)

_IS_WINDOWS = os.name == "nt"

# Session-scoped variables are dropped. Credentials and config-dir variables are
# left alone.
_SCRUB_PREFIXES = ("CLAUDE_CODE_", "CLAUDE_SESSION", "CODEX_SESSION", "AGY_SESSION")
_SCRUB_EXACT = frozenset(
    {"CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "CLAUDE_AGENT_SDK_VERSION"}
)


def _child_env(
    overrides: dict[str, str] | None,
    agent: str,
    turn_id: str | None = None,
    *,
    mark: str | None = None,
) -> dict[str, str]:
    """The environment for a process the office spawns on an agent's behalf.

    `agent` puts office.marks' mark into it. `turn_id` rides along so a log
    line can name the turn; nothing matches on it. `mark` replaces the mark
    `agent` and `turn_id` would make.

    The mark, git.block_outbound_push() and git.ceiling() are written LAST,
    after the caller's overrides: none of them is a value a caller may replace.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in _SCRUB_EXACT and not key.startswith(_SCRUB_PREFIXES)
    }
    if overrides:
        env.update(overrides)
    git.block_outbound_push(env)
    env["GIT_CEILING_DIRECTORIES"] = git.ceiling()
    env[marks.MARK_ENV] = mark or marks.mark_for(agent, turn_id)
    return env

_turn_id_lock = threading.Lock()
_turn_id_seq = 0


def _new_turn_id() -> str:
    global _turn_id_seq
    with _turn_id_lock:
        _turn_id_seq += 1
        return f"{int(time.time())}-{_turn_id_seq}"


class ProcessGone(RuntimeError):
    """The turn's process died before its message could be handed over."""


@dataclass
class Death:
    reason: str  # context_overflow | quota_exhausted | tool_error | crash | killed
    tail: list[str] = field(default_factory=list)

# Every live child. Children of children are covered by the tree kill below.
_live: set["AgentTurn"] = set()
_live_lock = threading.Lock()


def _kill_tree(pid: int) -> None:
    """Kill the child and anything it spawned that is still reachable from it."""
    if _IS_WINDOWS:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    else:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def shutdown_all() -> None:
    """Take every running turn down with the hub."""
    with _live_lock:
        survivors = list(_live)
    for turn in survivors:
        turn.kill()

atexit.register(shutdown_all)


class AgentTurn:
    """One vendor process, running one turn.

    **Stopping what the turn leaves behind belongs to the caller.** This
    class ends its own process and the tree still hanging off it; the caller
    writes `marks.sweep(marks.agent_scope(agent), agent)` once the turn is
    over.
    """

    def __init__(
        self,
        adapter,
        cmd: list[str],
        cwd: str,
        prompt: str,
        agent: str,
        env: dict[str, str] | None = None,
        tail_path: Path | None = None,
        context_limit: int | None = None,
    ) -> None:
        self.adapter = adapter
        self.cmd = cmd
        self.cwd = cwd
        self.prompt = prompt
        self.env = env
        # Whose turn this is; required.
        self.agent = agent
        # This turn, as a log line can name it: the second it started and a
        # counter that separates two turns starting in the same second. It goes
        # into the mark and is never matched on.
        self.turn_id = _new_turn_id()
        self.tail_path = Path(tail_path) if tail_path else None
        self.events: queue.Queue[Event | None] = queue.Queue()
        self.session_id: str | None = None
        self.death: Death | None = None
        # The vendor signalled the turn completed.
        self.turn_ended = False
        # What that turn-end event said about itself: None for a turn the vendor
        # declared finished, its own words for one it declared failed. Kept apart
        # from `_last_error`, which any stderr line can overwrite afterwards.
        self.turn_end_error: str | None = None
        self.finished = threading.Event()

        # time.monotonic() of the last byte from either stream.
        self.last_output_at: float = time.monotonic()
        self.started_at: float = time.monotonic()

        self._proc: subprocess.Popen | None = None
        # _tail and _tail_dirty are touched by four threads: the two stream
        # readers, the flusher, and _reap. Every read and every write of either
        # goes through _tail_lock. _reap is the single exception: it runs after
        # both readers have been joined and after _reaped stopped the flusher.
        self._tail: deque[str] = deque(maxlen=TAIL_LINES)
        self._tail_dirty = False
        self._tail_lock = threading.Lock()
        self._reaped = threading.Event()
        self._readers: list[threading.Thread] = []
        self._killed = False
        # A turn-end event has been seen. Set once, by the stdout reader (_absorb).
        self._turn_end_seen = threading.Event()
        # The linger reaper killed this process after it outlived its own
        # turn-end event. NOT a kill in the sense `_killed` means: nobody asked,
        # nothing failed, and `_reap` reads it as a normal end.
        self._lingered = False
        self._exit_code: int | None = None
        self._context_used: int | None = None
        # Whatever the caller resolved for this model (run_turn), else the
        # runtime's own smallest window. Either way the CLI overrides it if it
        # reports a real window mid-stream.
        self._context_limit: int | None = context_limit or context_limit_for(adapter.runtime)
        self._last_error: str | None = None
        # An adapter whose occupancy does not ride its own stream. Absent on the
        # other two, and this stays None for them.
        self._context_poller = getattr(adapter, "poll_context", None)

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._proc is not None:
            raise RuntimeError("already started")
        kwargs = {}
        if _IS_WINDOWS:
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True

        env = _child_env(self.env, self.agent, self.turn_id)

        try:
            self._proc = subprocess.Popen(
                self.cmd,
                cwd=self.cwd,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                shell=False,
                **kwargs,
            )
        except OSError as exc:
            self._fail_before_start(f"spawn failed: {exc}")
            raise

        with _live_lock:
            _live.add(self)

        # The message goes in as the process's only input. The close must happen
        # before we read.
        #
        # An empty prompt writes NOTHING and just closes.
        try:
            if self.prompt:
                self._proc.stdin.write(self.adapter.encode_message(self.prompt))
                self._proc.stdin.flush()
            self._proc.stdin.close()
        except (OSError, ValueError) as exc:
            self.kill()
            self._fail_before_start(f"delivery failed: {exc}")
            raise ProcessGone(str(exc)) from exc

        self.started_at = self.last_output_at = time.monotonic()
        if self._context_poller is not None:
            threading.Thread(target=self._context_loop, daemon=True,
                             name="office-context-poll").start()
        self._readers = [
            threading.Thread(target=self._read_stdout, daemon=True),
            threading.Thread(target=self._read_stderr, daemon=True),
        ]
        for thread in self._readers:
            thread.start()
        threading.Thread(target=self._flush_loop, daemon=True).start()
        threading.Thread(target=self._linger_loop, daemon=True,
                         name="office-turn-linger").start()
        threading.Thread(target=self._reap, daemon=True).start()

    def kill(self) -> None:
        self._killed = True
        proc = self._proc
        if proc.poll() is None:
            _kill_tree(proc.pid)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    def wait(self, timeout: float | None = None) -> bool:
        """Block until the turn is fully reaped. True if it finished in time."""
        return self.finished.wait(timeout)

    # -- state -------------------------------------------------------------

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    @property
    def exit_code(self) -> int | None:
        return self._exit_code

    @property
    def context_used(self) -> int | None:
        return self._context_used

    @property
    def context_limit(self) -> int | None:
        return self._context_limit

    @property
    def context_fraction(self) -> float | None:
        if not self._context_limit or self._context_used is None:
            return None
        return self._context_used / self._context_limit

    @property
    def quiet_for(self) -> float:
        """Seconds since this turn last said anything on either stream."""
        return max(0.0, time.monotonic() - self.last_output_at)

    @property
    def running_for(self) -> float:
        """Seconds since the process started."""
        return max(0.0, time.monotonic() - self.started_at)

    def tail(self) -> list[str]:
        # Same lock the two reader threads append under and _flush_tail() reads
        # under: the deque alone is not the shared state, the pair
        # (deque, _tail_dirty) is.
        with self._tail_lock:
            return list(self._tail)

    def stream(self, timeout: float | None = None):
        """Yield events as they arrive, ending when the turn's process exits."""
        while True:
            event = self.events.get(timeout=timeout)
            if event is None:
                return
            yield event

    # -- internals ---------------------------------------------------------

    def _fail_before_start(self, message: str) -> None:
        # _reap never runs on this path; the live set is cleared here.
        with _live_lock:
            _live.discard(self)
        self.death = Death(reason="crash", tail=[message])
        self.events.put(Event(kind="error", error=message))
        self.events.put(None)
        self.finished.set()

    def _read_stdout(self) -> None:
        for line in iter(self._proc.stdout.readline, ""):
            self.last_output_at = time.monotonic()
            with self._tail_lock:
                self._tail.append(line.rstrip("\r\n"))
                self._tail_dirty = True
            try:
                event = self.adapter.parse_line(line)
            except Exception as exc:  # a malformed line must never kill the reader
                self.events.put(Event(kind="error", error=f"parse failed: {exc}"))
                continue
            if event is None:
                continue
            self._absorb(event)
            self.events.put(event)

    def _read_stderr(self) -> None:
        for line in iter(self._proc.stderr.readline, ""):
            self.last_output_at = time.monotonic()
            stripped = line.rstrip("\r\n")
            with self._tail_lock:
                self._tail.append("stderr: " + stripped)
                self._tail_dirty = True
            if stripped.lower().startswith("error:") or _QUOTA_SIGNS.search(stripped):
                self._last_error = stripped
                self.events.put(Event(kind="error", error=stripped))
            elif _VENDOR_MARKS.search(stripped):
                # Not _last_error: a warning is not the cause of a death.
                self.events.put(Event(kind="error", error=stripped))

    def _poll_context_once(self) -> None:
        """Ask the adapter for an out-of-band context reading, if it has one.

        Silent on every failure: the last good figure stands.
        """
        if self._context_poller is None or not self.session_id:
            return
        try:
            event = self._context_poller(self.session_id)
        except Exception:  # noqa: BLE001 - never fatal
            return
        if event is None:
            return
        self._absorb(event)
        self.events.put(event)

    def _context_loop(self) -> None:
        # Waits on the reaped flag rather than sleeping.
        while not self._reaped.wait(CONTEXT_POLL_SECONDS):
            self._poll_context_once()

    def _absorb(self, event: Event) -> None:
        if event.session_id and not self.session_id:
            self.session_id = event.session_id
        if event.context_used is not None:
            # Plain assignment, never a max(): the reading is not monotonic. A
            # drop is not an error and not a signal.
            self._context_used = event.context_used
        if event.context_limit is not None:
            self._context_limit = event.context_limit  # the CLI always beats the floor
        if event.error:
            self._last_error = event.error
        if event.kind == "turn_end":
            self.turn_ended = True
            self.turn_end_error = event.error or None
            # Last, after the two fields _reap reads once this wakes _linger_loop.
            self._turn_end_seen.set()

    def _linger_loop(self) -> None:
        """Close a process that has outlived the turn it announced was over.

        Starts only at the vendor's own turn-end event, and waits on
        `_reaped` rather than on the clock. Kills only a process `poll()`
        shows alive.

        Does NOT go through `kill()`. `_lingered` is set before the kill, and
        `_reap` reads a lingered process as a normal end rather than a death.
        """
        while not self._turn_end_seen.wait(1.0):
            if self._reaped.is_set():
                return
        if self._reaped.wait(LINGER_GRACE_SECONDS):
            return  # reaped inside the grace period: the ordinary case
        proc = self._proc
        if proc.poll() is not None:
            return  # gone already; _reap is simply still finishing up
        self._lingered = True
        _log.info(
            "%s (%s): the process is still alive %.0fs after its turn-end event; killing it",
            self.agent, self.adapter.runtime, LINGER_GRACE_SECONDS,
        )
        _kill_tree(proc.pid)

    def _flush_loop(self) -> None:
        # Waits on the reaped flag rather than sleeping.
        while not self._reaped.wait(TAIL_FLUSH_SECONDS):
            self._flush_tail()

    def _flush_tail(self) -> None:
        if not self.tail_path:
            return
        with self._tail_lock:
            if not self._tail_dirty:
                return
            self._tail_dirty = False
            self.tail_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.tail_path.with_suffix(self.tail_path.suffix + ".tmp")
            tmp.write_text("\n".join(self._tail), encoding="utf-8", errors="replace")
            tmp.replace(self.tail_path)

    def _reap(self) -> None:
        for thread in self._readers:
            thread.join()
        exit_code = self._proc.wait()
        self._exit_code = exit_code
        # Last reading before anything is decided: _classify's overflow branch
        # reads the fraction this sets.
        self._poll_context_once()
        self._reaped.set()  # stops the flusher and the context poller
        with _live_lock:
            _live.discard(self)

        # `exit_code == 0` carries every turn that ends the way a turn is meant
        # to. `_killed` overrides everything. `_lingered` is the process the
        # linger reaper closed; its exit code is our own kill's and is not read,
        # so the vendor's turn-end statement decides instead.
        #
        # The branch decides only whether this turn is a death. The tail file is
        # written either way, below it.
        if self._killed or not (
            exit_code == 0 or (self._lingered and not self.turn_end_error)
        ):
            self.death = Death(reason=self._classify(), tail=list(self._tail))
        # Written for every turn. office/bus.py deletes it for a turn that spoke.
        self._tail_dirty = True
        self._flush_tail()
        self._tail.clear()
        self.events.put(None)  # end of stream
        self.finished.set()

    def _classify(self) -> str:
        # `_killed` and not "the process was killed": the linger reaper also ends
        # a process with _kill_tree and leaves this flag alone. Only kill() sets
        # it.
        if self._killed:
            return "killed"
        text = self._last_error or ""
        # _QUOTA_SIGNS against the vendor's own error text is the only signal
        # for this.
        #
        # Widen the pattern; do not add a second source.
        if _QUOTA_SIGNS.search(text):
            return "quota_exhausted"
        fraction = self.context_fraction
        if fraction is not None and fraction >= CONTEXT_OVERFLOW_AT:
            # This branch runs only once the process is already gone.
            return "context_overflow"
        if text:
            return "tool_error"
        return "crash"


def run_turn(
    adapter,
    ws: str,
    model: str,
    prompt: str,
    agent: str,
    effort: str | None = None,
    office_url: str | None = None,
    session_id: str | None = None,
    resume: bool = False,
    env: dict[str, str] | None = None,
    tail_path: Path | None = None,
) -> AgentTurn:
    """Start one turn.

    `resume` separates the first turn of a session from every later one. It
    is not redundant with `session_id`; the caller has to say which.

    `office_url` is this agent's own office MCP endpoint. `agent` goes into
    the child's environment as OFFICE_AGENT.

    The adapter's own `child_env()` goes in first, under everything the
    caller passes. Each runtime states its own timeout explicitly.
    """
    cmd = adapter.build_command(ws, model, effort, office_url, session_id, resume)
    identity = dict(getattr(adapter, "child_env", dict)())
    identity[shared.AGENT_ENV] = agent
    if session_id:
        identity["OFFICE_SESSION"] = session_id
    if office_url:
        identity["OFFICE_MCP_URL"] = office_url
    turn = AgentTurn(adapter, cmd, ws, prompt, agent,
                     env={**identity, **(env or {})},
                     tail_path=tail_path,
                     context_limit=context_limit_for(adapter.runtime, model))
    turn.start()
    return turn
