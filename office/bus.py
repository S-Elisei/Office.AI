"""Message bus: delivers messages to agents through a hook, a piggyback, or a turn."""

from __future__ import annotations

import logging
import math
import os
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from office import commands, core, db, git, marks, wiki_files
from office.adapters import RUNTIMES, adapter_for, shared
from office.adapters import agy, codex
from office.adapters.base import name_quota_windows
from office.config import Config
from office.process import AgentTurn, run_turn, turn_env

_log = logging.getLogger("office.bus")

# How often the loop looks at the clock.
TICK_SECONDS = 1.0

# A burst of messages should cost one turn, not one turn each.
COALESCE_SECONDS = 3.0

# Fuse: an agent that would be woken more often than this has its extra wakes
# collapsed into the next one. Nothing is dropped.
FUSE_TURNS = 6
FUSE_WINDOW_SECONDS = 300.0

# One delivery carries at most this many common-chat lines, the newest ones.
COMMON_CHAT_DELIVERED = 50

# How long an agent may produce no output at all before the office mentions it
# to its manager (_report_silent_turn). Stored in minutes.
SILENCE_NOTICE_SETTING = "silence_notice_minutes"
SILENCE_NOTICE_DEFAULT_MINUTES = 20.0

# Quota is a property of the vendor account, not of a turn: the office polls
# it on its own schedule and no agent spends a turn to learn it.
QUOTA_POLL_SECONDS = 60.0

# How often the progress check (_check_progress) runs.
PROGRESS_CHECK_SECONDS = 60.0

# Every runtime, on the same footing.
QUOTA_RUNTIMES = ("claude", "codex", "agy")

# How often to re-read what each vendor will accept as a model. Its own daemon
# thread, and one round at startup.
MODEL_POLL_SECONDS = 3600.0

# The office's one-shot timer for its run/pause switch: a settings row holding the
# state to switch to, "run" or "pause", and the epoch second, separated by a space.
# Empty when no timer is set.
SWITCH_TIMER_SETTING = "switch_timer"

# The event kinds that can change what this module has to do.
_WAKING_KINDS = frozenset({"messages"})


@dataclass
class _AgentState:
    lock: threading.Lock = field(default_factory=threading.Lock)
    turn = None  # the AgentTurn currently running, if any
    wake_times: list[float] = field(default_factory=list)
    drain_at: float | None = None
    # Set when the hook cache has advanced the watermark past a queue no turn
    # has drained yet.
    owes_turn: bool = False
    # True once this turn's silence has been reported to the agent's manager
    # (_report_silent_turn); cleared when the next turn is installed.
    silence_reported: bool = False
    # The last exception that escaped _tick_agent for this agent: a signature
    # (_failure_signature), the text the owner is shown, and the wall-clock
    # instant it was first seen.
    tick_failure: str | None = None
    tick_failure_detail: str = ""
    tick_failure_at: float = 0.0
    # True for exactly as long as a compaction of this agent's session is
    # running. _start_turn refuses while it is set.
    compacting: bool = False


class Bus:
    def __init__(self, conn: sqlite3.Connection, config: Config) -> None:
        self.conn = conn
        self.config = config
        self._agents: dict[str, _AgentState] = {}
        self._agents_lock = threading.Lock()
        # Set when the owner stops the director's turn, cleared when the director's
        # next turn starts. In memory.
        self._director_stopped = False
        # The office's switch. A bus that has not been started runs; start() pauses it.
        # While it is False, _tick_agent starts no turn but one for an owner's direct
        # message, and the progress check does not run. In memory.
        self.running = True
        # The progress check's chains, by stuck agent (_check_progress), and the id
        # of the last message telling the director that nothing moves. In memory.
        self._stalls: dict[str, dict] = {}
        self._idle_told = 0
        self._progress_due = time.monotonic() + PROGRESS_CHECK_SECONDS
        self._wakeup = threading.Event()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        for warning in self.health():
            _log.warning("%s", warning)
        self._install_agy_mcp()
        # A timer whose moment passed while the hub was down is dropped.
        timer = self.switch_timer()
        if timer is not None and timer[1] <= time.time():
            self.cancel_switch_timer()
        self.set_running(False, "the hub started")
        self._thread = threading.Thread(target=self._loop, daemon=True, name="office-bus")
        self._thread.start()
        # The first round is the bus loop's; this thread carries every round after it.
        threading.Thread(target=self._quota_loop, daemon=True, name="office-quota").start()
        # Same two-thread shape as quota.
        threading.Thread(target=self.poll_models_now, daemon=True, name="office-models-first").start()
        threading.Thread(target=self._models_loop, daemon=True, name="office-models").start()

    def _install_agy_mcp(self) -> None:
        """Register agy's stdio MCP shim, once. Gives agy agents office tools.

        The shim is a stdio server spawned per turn and reads its endpoint from
        that turn's environment; one registration serves every agent.
        `agy mcp remove office` undoes it.
        """
        if not self.config.agy_bin:
            return
        try:
            if not agy.install_mcp_server(self.config.agy_bin):
                _log.warning("could not register agy's office MCP server; agy agents will have no tools")
        except Exception:
            _log.exception("could not register agy's office MCP server")

    def _quota_loop(self) -> None:
        """Ask all three vendors what is left. Free: no turn, no tokens."""
        while not self._stopping.wait(QUOTA_POLL_SECONDS):
            self.poll_quota_now()

    def poll_quota_now(self) -> None:
        """One round of quota polling, out of band. Also the round the bus loop runs
                before its first tick."""
        for runtime in QUOTA_RUNTIMES:
            self._poll_runtime(runtime)
        core.expire_passed_quota(self.conn)

    def _poll_runtime(self, runtime: str) -> None:
        """Poll one runtime's quota: in each round, and as soon as a turn or a
                compaction on it ends. Never raises."""
        try:
            adapter = adapter_for(runtime, self.config)
        except Exception:
            return  # not configured on this machine
        try:
            # Named by window (5h / week) before it is stored, for all three runtimes
            # (office/adapters/base.py).
            polled = adapter.poll_quota(self._office_session_ids(runtime))
            if polled:
                core.record_quota(self.conn, name_quota_windows(polled))
        except Exception:
            _log.exception("quota poll failed for %s", runtime)

    def _office_session_ids(self, runtime: str) -> list[str]:
        """The vendor session ids the office itself started on this runtime.

                Only codex uses them: its quota is read from a rollout file.
        """
        rows = db.query(
            self.conn,
            "SELECT session_id FROM agents WHERE runtime = ? AND session_id IS NOT NULL",
            (runtime,),
        )
        return [r["session_id"] for r in rows if r["session_id"]]

    def _models_loop(self) -> None:
        """Ask each vendor what it will accept as a model. Free: no turn, no tokens.
        """
        while not self._stopping.wait(MODEL_POLL_SECONDS):
            self.poll_models_now()

    def poll_models_now(self) -> None:
        """One round of catalogue polling, out of band. Also the startup round.

                Never on the tick thread.
        """
        for runtime in RUNTIMES:
            try:
                adapter = adapter_for(runtime, self.config)
            except Exception:
                continue  # not configured on this machine
            try:
                models = adapter.list_models()
            except Exception:
                _log.exception("model catalogue poll failed for %s", runtime)
                continue
            if models is None:
                # Could not read. Not the same as "this runtime has no models".
                continue
            try:
                core.record_models(self.conn, runtime, models)
            except Exception:
                _log.exception("could not record the model catalogue for %s", runtime)

    def health(self) -> list[str]:
        """Environment problems the office can see but cannot fix itself.

                Re-read on every call rather than cached, so a warning stops the moment
                its condition clears.
        """
        warnings = [w for w in (codex.shim_warning(self.config.codex_bin),) if w]
        # First in the list: the office reporting that it cannot run one of its
        # own people.
        for name, detail, since in self._tick_failures():
            warnings.append(
                f"the office's own tick over {name} failed at {_fmt_epoch(since)} with {detail}, "
                f"and {name} has started no turn since. This is not the vendor refusing and not "
                "quota: the office cannot get as far as spawning that agent, and it will keep "
                "failing the same way until whatever it names is fixed. The hub log holds the "
                "traceback."
            )
        # A marked process still running once the sweep is done.
        warnings.extend(marks.survivor_warnings())
        state = git.project_state(self.config)
        if state != "ready":
            warnings.append(
                f"the project repository is '{state}': "
                + (
                    "nothing has been imported or created yet"
                    if state == "absent"
                    else "it exists but has no branch and no donor, so a clone of it would be empty"
                )
                + ". Hiring anyone clones a workspace, so it will fail until this is set up "
                "on the main page."
            )
        # The directory the office delivers into has stopped being a repository.
        # Read off the disk on every call.
        delivery = core.get_delivery(self.conn, self.config)
        if state == "ready" and git.owner_state(self.config) == "none":
            warnings.append(
                f"finished work is meant to go to {delivery['repo']} — the folder the office "
                "keeps its own data in — and that folder is not a git repository. A merge is "
                "refused until it is one again, and the pull request stays open."
            )
        # agy's hook command can be unrunnable and say so nowhere. Asked of the
        # paths as they stand; it clears itself if anything moves.
        for row in db.query(
            self.conn,
            "SELECT a.name AS name, w.path AS path FROM agents a "
            "JOIN workspaces w ON w.owner_agent_id = a.id WHERE a.runtime = 'agy'",
        ):
            hook = agy.hook_warning(row["name"], row["path"])
            if hook:
                warnings.append(hook)
        blind = [
            row["name"]
            for row in db.query(
                self.conn,
                "SELECT name FROM agents WHERE runtime = 'codex' AND context_used IS NULL "
                "AND session_id IS NOT NULL",
            )
        ]
        if blind:
            warnings.append(
                f"context fill is unknown for codex agent(s) {blind}: their session files under "
                f"{codex.codex_home()}/sessions could not be read or held no token_count event. "
                "The office reports no number rather than an estimate, so their managers are "
                "staffing those agents blind. Check that codex is writing rollout files and "
                "that nothing passes --ephemeral, which switches them off."
            )
        # A directory under ws/ the office did not create.
        stray_dirs = core.stray_workspace_dirs(self.conn, self.config.ws_dir)
        if stray_dirs:
            warnings.append(
                f"the office did not create these directories under {self.config.ws_dir}: "
                f"{', '.join(stray_dirs)}. Every workspace it hands out is cloned and recorded, so "
                "anything else here was put there by hand — what has happened is an agent cloning "
                "the project to read another agent's branch, which leaves a clone with a "
                "push-capable remote to the office's own repository sitting in the data root."
            )
        # A runtime that is configured but has never answered leaves the managers
        # with no ceiling to reason about.
        answered = {row["runtime"] for row in db.query(self.conn, "SELECT DISTINCT runtime FROM quota")}
        silent = []
        for runtime in QUOTA_RUNTIMES:
            try:
                adapter_for(runtime, self.config)
            except Exception:
                continue  # not configured on this machine; not a warning
            if runtime not in answered:
                silent.append(runtime)
        if silent:
            warnings.append(
                f"no quota reading yet for {silent}: the director and the leads see no remaining "
                "quota for those runtimes and cannot weigh them when distributing work. codex has nothing "
                "to report until it has run one turn (its quota is read from the session file it "
                "writes); for the others this means the free /usage call failed."
            )
        return warnings

    def stop(self) -> None:
        self._stopping.set()
        self._wakeup.set()
        if self._thread:
            self._thread.join(timeout=10)

    def notify(self, kind: str) -> None:
        """Nudge from the hub's notifier.

                The kind is all that is taken, and only to decide whether the nudge is
                worth a tick.
        """
        # Only one kind can mean work for this module (_WAKING_KINDS).
        if kind not in _WAKING_KINDS:
            return
        self._schedule()

    def _schedule(self) -> None:
        self._wakeup.set()

    # -- the office's switch -----------------------------------------------

    def set_running(self, running: bool, cause: str, actor: str | None = None) -> None:
        """Set the office running or paused, and journal the flip with its cause.
                Setting the state it is already in does nothing.

                Paused, no turn starts, for a keep-alive or any message, except a
                direct message from the owner to its addressee; turns already running
                go on, and messages are still stored and wait. Switching to running
                drops the progress check's chains and puts its first check a full
                interval away.
        """
        if self.running == running:
            return
        self.running = running
        if running:
            self._stalls.clear()
            self._progress_due = time.monotonic() + PROGRESS_CHECK_SECONDS
        core.record_notice(
            self.conn, "info",
            f"The office is now {'running' if running else 'paused'} ({cause}).",
            actor=actor,
        )
        self._schedule()

    def switch_timer(self) -> tuple[str, int] | None:
        """The pending timer as ("run" or "pause", epoch second), or None."""
        raw = core.get_setting(self.conn, SWITCH_TIMER_SETTING, "")
        if not raw:
            return None
        to, at = raw.split()
        return to, int(at)

    def set_switch_timer(self, to: str, at: int, actor: str) -> None:
        """Set the one-shot timer to switch the office to `to`, "run" or "pause", at
                the epoch second `at`, replacing a pending one."""
        core.set_setting(self.conn, SWITCH_TIMER_SETTING, f"{to} {at}", actor=actor)

    def cancel_switch_timer(self, actor: str | None = None) -> None:
        core.set_setting(self.conn, SWITCH_TIMER_SETTING, "", actor=actor)

    def _fire_due_switch(self, now: float) -> None:
        """Switch the office as the timer says once its moment has come, and clear
                the timer."""
        timer = self.switch_timer()
        if timer is None or timer[1] > now:
            return
        self.cancel_switch_timer()
        self.set_running(timer[0] == "run", "by the timer")

    # -- the free piggyback drain -----------------------------------------

    def piggyback(self, agent_name: str) -> str | None:
        """Whatever is queued, to be appended to an office tool's result.

        The cheapest drain there is: the agent is already paying for this round
        trip. Returns None when the hook got there first, which is fine — it
        means the same text reached the agent a moment earlier.
        """
        return shared.claim_inbox(self._workspace_of(agent_name), agent_name)

    # -- delivery ----------------------------------------------------------
    #
    # core.send_message() is the one writer, and it derives the channel from
    # the recipient name itself.

    def _loop(self) -> None:
        # The first quota round runs to its end before the first tick.
        self.poll_quota_now()
        while not self._stopping.is_set():
            self._wakeup.wait(timeout=TICK_SECONDS)
            self._wakeup.clear()
            if self._stopping.is_set():
                return
            try:
                self._tick()
            except Exception:
                # The loop survives anything, and says so.
                _log.exception("bus tick failed")

    def _tick(self) -> None:
        now = time.monotonic()
        # Before the agents are read: a timer that runs the office is honoured in
        # this same pass.
        self._fire_due_switch(time.time())
        # Before the queue is read, not after: a wake sent on this tick reaches
        # its recipient in this same pass.
        try:
            self._send_due_wakes()
        except Exception:
            _log.exception("could not send due wakes")
        try:
            self._expire_due_expectations()
        except Exception:
            _log.exception("could not close due expectations")
        # One read for the whole pass. One maximum, over `messages`: that is what
        # "caught up" is measured against.
        head = db.query_one(
            self.conn, "SELECT COALESCE(MAX(id), 0) AS m FROM messages"
        )
        for agent in self._all_agents():
            try:
                self._tick_agent(agent, now, head["m"])
            except Exception as exc:
                # Per agent, not per pass: adapter_for() raises for a missing binary and
                # that condition is stable. The exception is named to the owner as well
                # as logged.
                _log.exception("bus tick failed for %s", agent["name"])
                self._note_tick_failure(agent, exc)
        if self.running and now >= self._progress_due:
            self._progress_due = now + PROGRESS_CHECK_SECONDS
            try:
                self._check_progress()
            except Exception:
                _log.exception("progress check failed")

    def _tick_agent(self, agent: dict, now: float, max_message: int) -> None:
            state = self._state(agent["name"])
            caught_up = (agent["last_seen_message_id"] or 0) >= max_message
            pending = _Pending() if caught_up else self._undelivered(agent)
            running = state.turn is not None and state.turn.alive
            if running and not state.silence_reported:
                # Read every time rather than cached, and only inside this branch.
                threshold = self._silence_notice_seconds()
                if threshold is not None and state.turn.quiet_for >= threshold:
                    self._report_silent_turn(agent, state)
            if pending.anything:
                # The inbox is a forward cache for a turn already in progress: a hook
                # only fires on the agent's own activity, and an idle agent has none.
                if running:
                    self._cache_for_hook(agent, pending)
                if pending.guaranteed:
                    # Common chat never buys a turn of its own; a direct message
                    # does, after the coalescing window folds the burst into one.
                    state.owes_turn = True
                    if state.drain_at is None:
                        state.drain_at = now + COALESCE_SECONDS

            elif not state.owes_turn:
                # Examined and found irrelevant for this agent. Moving the watermark
                # past it is what stops a quiet executor re-reading the same tail once a
                # second.
                #
                # Skipped while a turn is owed: that queue must not be marked seen
                # before it is delivered.
                self._advance(agent, pending)

            if (
                state.owes_turn and state.drain_at is not None and now >= state.drain_at
                and (self.running or pending.from_owner)
            ):
                # The timer is cleared only by a turn that actually started,
                # and re-armed in a finally.
                started = False
                try:
                    started = self._start_turn(agent)
                finally:
                    if started:
                        state.owes_turn = False
                        state.drain_at = None
                    else:
                        # Mid-turn, fused, not provisioned yet, or a raised
                        # exception. The debt stands and the queue keeps growing;
                        # this timer is what brings it back.
                        state.drain_at = now + COALESCE_SECONDS

    # -- the office's alarm ------------------------------------------------

    def _send_due_wakes(self) -> None:
        """Send the deferred messages that have come due, and forget them.

                A wake is an ordinary message with a moment attached, sent through
                core.send_message like any other.

                A wake whose moment passed while the hub was down fires on the first
                tick after it starts. The row is deleted after the send, not before.
        """
        for wake in core.due_scheduled_messages(self.conn):
            try:
                core.send_message(self.conn, wake["sender"], wake["recipient"], wake["body"])
            except ValueError:
                # The only thing core.send_message refuses is a recipient that names
                # nobody, which here means an agent fired since the wake was set.
                self._report_lost_wake(wake)
            except Exception:
                _log.exception("could not send the deferred message %s", wake["id"])
                continue
            core.drop_scheduled_message(self.conn, wake["id"])

    def _report_lost_wake(self, wake: dict) -> None:
        """Tell the sender that the message it set for later has nowhere to go.
        """
        sender = wake["sender"]
        if sender == core.OFFICE_SENDER:
            return
        if db.query_one(self.conn, "SELECT 1 FROM agents WHERE name = ?", (sender,)) is None:
            _log.info(
                "nobody to tell that a deferred message for %s could not be sent: "
                "%s is not an agent of this office", wake["recipient"], sender,
            )
            return
        try:
            core.send_message(self.conn, core.OFFICE_SENDER, sender, _lost_wake_notice(wake))
        except Exception:
            _log.exception(
                "could not tell %s that its deferred message for %s was not sent",
                sender, wake["recipient"],
            )

    def _expire_due_expectations(self) -> None:
        """Close every expectation whose due time has passed, telling its agent.

        One that came due while the hub was down closes on the first tick after
        it starts.
        """
        for expectation in core.due_expectations(self.conn):
            core.expire_expectation(
                self.conn, expectation["id"], _expired_expectation_notice(expectation["about"])
            )

    # -- reading the queue -------------------------------------------------

    def _undelivered(self, agent: dict) -> "_Pending":
        """Everything past this agent's watermark, rendered down to what it should
                actually see.

                Read by id; relevance is decided in _Pending.build.
        """
        messages = db.query(
            self.conn,
            "SELECT * FROM messages WHERE id > ? ORDER BY id",
            (agent["last_seen_message_id"] or 0,),
        )
        return _Pending.build(agent, messages, core.get_owner_name(self.conn))

    def _cache_for_hook(self, agent: dict, pending: "_Pending") -> None:
        """Write the queue where the hook will find it, and mark it delivered.

                Only for a turn that is already running.
        """
        shared.deliver(self._workspace_of(agent["name"]), pending.render(), agent["name"],
                       agent["last_seen_message_id"] or 0)
        self._advance(agent, pending)

    def _mark_turn_start(self, agent: dict, pending: "_Pending") -> None:
        """Advance past what this turn was handed, recording where the hand-over
                began.

                One statement, not _advance() plus a second write.
        """
        before_message = agent["last_seen_message_id"] or 0
        seen_message = max(pending.last_message_id, before_message)
        agent["last_seen_message_id"] = seen_message
        with db.transaction(self.conn) as conn:
            conn.execute(
                "UPDATE agents SET last_seen_message_id = ?, turn_start_message_id = ? WHERE id = ?",
                (seen_message, before_message, agent["id"]),
            )

    def _close_turn(self, name: str, reason: str | None, resume_after: int | None) -> None:
        """Close out a turn: give back what it never took, drop its spawn-time
                marker and, on a death, answer for what it was handed.

                `reason` is None for a clean end and the death's reason otherwise,
                `hub_restart` included. `resume_after` is the time of return of a
                quota death.

                Whatever the agent's inbox still holds for it is taken back and the
                watermark moves back to the lowest mark those lines were written after:
                it is unread again and rides the next turn.

                On a death every sender of a direct message in the turn's range is told
                it was not processed.

                Never raises: _watch calls it before clearing `state.turn`.
        """
        try:
            self._close_turn_inner(name, reason, resume_after)
        except Exception:
            _log.exception("could not close out %s's turn", name)

    def _close_turn_inner(self, name: str, reason: str | None, resume_after: int | None) -> None:
        """The body of _close_turn; everything worth saying is up there."""
        agent = db.query_one(
            self.conn,
            "SELECT id, runtime, last_seen_message_id, turn_start_message_id "
            "FROM agents WHERE name = ?",
            (name,),
        )
        if agent is None or agent["turn_start_message_id"] is None:
            return  # no turn in flight as far as the database is concerned
        try:
            last_seen = agent["last_seen_message_id"] or 0
            floor = shared.take_back(self._workspace_of(name), name)
            if floor is not None:
                with db.transaction(self.conn) as conn:
                    conn.execute(
                        "UPDATE agents SET last_seen_message_id = "
                        "MIN(COALESCE(last_seen_message_id, 0), ?) WHERE id = ?",
                        (floor, agent["id"]),
                    )
                last_seen = min(last_seen, floor)
            if reason is not None:
                # The range the spawn-time column opens: everything this turn was
                # handed. Only direct messages are answered for.
                handed = db.query(
                    self.conn,
                    "SELECT sender, body FROM messages WHERE id > ? AND id <= ? "
                    "AND channel = 'dm' AND recipient = ? AND sender != ? ORDER BY id",
                    (agent["turn_start_message_id"], last_seen, name, name),
                )
                _log.info("%s's turn ended as %s; %d direct message(s) in its range",
                          name, reason, len(handed))
                self._report_undelivered(name, agent["id"], reason, resume_after, handed)
        finally:
            # The notices go out first and the marker is dropped in a finally:
            # nothing else ever clears this column.
            with db.transaction(self.conn) as conn:
                conn.execute(
                    "UPDATE agents SET turn_start_message_id = NULL WHERE id = ?",
                    (agent["id"],),
                )
            core.drop_closed_expectations(self.conn, name)

    def _report_undelivered(
        self, name: str, agent_id: int, reason: str, resume_after: int | None, handed: list
    ) -> None:
        """One notice per sender whose direct message died with `name`'s turn.

                Three kinds of sender are never told: the office itself, a sender that
                has since been fired, and the agent itself. A service's message is
                given back to `name` instead, whole, as a quiet line. The brief of
                `name`'s current work is not listed.

                One sender's notice failing does not cost the others theirs.
        """
        if not handed:
            return
        brief = None
        work = core.current_work(self.conn, agent_id)
        if work is not None:
            assigner = db.query_one(
                self.conn, "SELECT name FROM agents WHERE id = ?", (work["assigned_by_agent_id"],)
            )["name"]
            brief = (assigner, core.brief_message(work["brief"], work["branch"]))
        owner = core.get_owner_name(self.conn)
        lost: dict[str, list[str]] = {}
        for row in handed:
            sender = row["sender"]
            if sender == core.OFFICE_SENDER:
                continue
            if (sender, row["body"]) == brief:
                continue
            lost.setdefault(sender, []).append(row["body"])
        for sender, bodies in lost.items():
            if sender.startswith(core.HOOK_SENDER_PREFIX):
                for body in bodies:
                    try:
                        core.tell_quietly(self.conn, name, _lost_hook_notice(sender, body))
                    except Exception:
                        _log.exception(
                            "could not give %s back the message from %s", name, sender
                        )
                continue
            if sender == owner:
                _log.info(
                    "%d message(s) from the owner died with %s's turn (%s) — the owner is "
                    "told by the interface, never by a message",
                    len(bodies), name, reason,
                )
                continue
            if db.query_one(self.conn, "SELECT 1 FROM agents WHERE name = ?", (sender,)) is None:
                _log.info(
                    "nobody to tell that %d message(s) for %s went unprocessed: "
                    "%s is not a participant any more", len(bodies), name, sender,
                )
                continue
            try:
                core.send_message(
                    self.conn, core.OFFICE_SENDER, sender,
                    _undelivered_notice(name, reason, resume_after, bodies),
                )
            except Exception:
                _log.exception(
                    "could not tell %s that a message for %s went unprocessed", sender, name
                )

    def _silence_notice_seconds(self) -> float | None:
        """The owner's silence threshold in seconds, or None for "never report".

                Absent, empty or unparseable means the default; zero or less means
                never.

                Never raises: it is called from the tick, once per running agent per
                second.
        """
        raw = (core.get_setting(self.conn, SILENCE_NOTICE_SETTING, "") or "").strip()
        try:
            minutes = float(raw)
        except ValueError:
            minutes = SILENCE_NOTICE_DEFAULT_MINUTES
        if not math.isfinite(minutes):
            # float() accepts "nan" and "inf" and neither is a number of minutes:
            # they belong with the typos, not with zero.
            minutes = SILENCE_NOTICE_DEFAULT_MINUTES
        if minutes <= 0:
            return None
        return minutes * 60.0

    def _report_silent_turn(self, agent: dict, state: "_AgentState") -> None:
        """A running agent that has said nothing for the owner's silence threshold,
                told to its manager as a message from `office` with the last few things
                it was seen doing.

                It has no power over the agent: no stop, no flag, nothing written
                anywhere but a message.

                The director's own silence is not reported.

                Never raises, and marks itself done before it tries anything that can
                fail.
        """
        state.silence_reported = True
        name = agent["name"]
        try:
            turn = state.turn
            if turn is None or not turn.alive:
                # It ended between the tick's check and here.
                return
            manager = core.manager_name(self.conn, agent["id"])
            if manager is None:
                _log.info("the director has been quiet for %.0fs; the owner's page already shows it",
                          turn.quiet_for)
                return
            # Parsed off the ring buffer before send_message opens its
            # transaction, never inside one: no writing transaction is held
            # across anything slower than it has to be.
            actions = _last_actions(turn)
            _log.info("%s has been quiet for %.0fs; telling %s", name, turn.quiet_for, manager)
            core.send_message(
                self.conn, core.OFFICE_SENDER, manager,
                _silent_turn_notice(name, turn.quiet_for, turn.running_for, actions),
            )
        except Exception:
            _log.exception("could not report that %s has gone quiet", name)

    def _advance(self, agent: dict, pending: "_Pending") -> None:
        """Mark everything in `pending` delivered.

                The watermark has three writers. This and _mark_turn_start run on the
                bus thread and only move it forward; _close_turn runs on a turn's
                watcher and moves it back to what the turn never took.
        """
        before_message = agent["last_seen_message_id"] or 0
        seen_message = max(pending.last_message_id, before_message)
        if seen_message == before_message:
            return  # nothing moved; do not take the write lock to say so
        # Written only over the value `agent` was read with: a watermark moved back
        # since then stays where it was moved.
        with db.transaction(self.conn) as conn:
            moved = conn.execute(
                "UPDATE agents SET last_seen_message_id = ? "
                "WHERE id = ? AND COALESCE(last_seen_message_id, 0) = ?",
                (seen_message, agent["id"], before_message),
            ).rowcount
        if moved:
            agent["last_seen_message_id"] = seen_message

    # -- the turn drain ----------------------------------------------------

    def _start_turn(self, agent: dict) -> bool:
        name = agent["name"]
        state = self._state(name)
        if not state.lock.acquire(blocking=False):
            return False
        try:
            if state.turn is not None:
                # The environment never interrupts a turn in progress, and a turn whose
                # process has exited still counts until _watch has closed it out.
                return False
            if state.compacting:
                # A compaction is a second process on this agent's session: a message
                # that arrives mid-compaction must not raise a turn on it.
                return False
            # Read under the lock, after the check on state.turn: _close_turn moves
            # the watermark back before _watch clears state.turn.
            agent = db.query_one(self.conn, "SELECT * FROM agents WHERE id = ?", (agent["id"],))
            if agent is None:
                return False  # fired since the tick read it
            agent = dict(agent)
            if core.quota_hold(self.conn, agent["runtime"], agent["model"]) is not None:
                return False  # messages wait for the time of return
            ws = self._workspace_of(name)
            if ws is None:
                return False  # nothing to run in yet; assign() will come back to it
            if not self._fuse_allows(state):
                return False

            adapter = adapter_for(agent["runtime"], self.config)
            adapter.prepare_session(str(ws), name)

            session_id, resume = self._session_for(agent)
            system = self._system_prompt(agent)
            if adapter.takes_system_prompt:
                # Written every turn and read only at the start of a session.
                shared.system_prompt_path(ws).write_text(system, encoding="utf-8")
                system = ""

            fresh = self._undelivered(agent)
            prompt = self._compose(agent, fresh, resume, system)
            if not prompt:
                # Nothing left to send: the hook took it mid-turn. The debt
                # is discharged, not deferred.
                return True
            wiki_files.sync(self.conn, self.config, ws.name)
            state.wake_times.append(time.monotonic())
            core.resume_paused_work(self.conn, agent["id"])
            usage = self._usage_meta(agent, session_id, "turn")
            usage.row.update(
                new_session=int(not resume),
                woken_by=",".join(fresh.senders) or None,
                chat_lines=fresh.chat_count,
                quiet_lines=fresh.quiet_count,
            )
            (self.config.tails_dir / f"{name}.log").unlink(missing_ok=True)
            # No SQLite transaction is open here, deliberately: everything the
            # turn needed from the database was read and committed above.
            turn = run_turn(
                adapter,
                str(ws),
                agent["model"],
                prompt,
                name,
                effort=agent["effort"],
                office_url=self._mcp_endpoint(name),
                session_id=session_id,
                resume=resume,
                tail_path=self.config.tails_dir / f"{name}.log",
            )
            state.turn = turn
            # Cleared here, under the same lock that installs the turn.
            state.silence_reported = False
            # A turn exists, so whatever the office last failed to do for this agent
            # it has since managed. This also retires the health warning.
            state.tick_failure = None
            state.tick_failure_detail = ""
            state.tick_failure_at = 0.0
            # Watermark only once the process exists. The same write opens the range
            # this turn will have to be answered for.
            self._mark_turn_start(agent, fresh)
            if agent["kind"] == "director":
                self._director_stopped = False
            self._set_status(agent["id"], "running")
            threading.Thread(target=self._watch, args=(name, turn, usage), daemon=True).start()
            return True
        finally:
            state.lock.release()

    def _session_for(self, agent: dict) -> tuple[str | None, bool]:
        """Which session this turn joins, and whether it is a continuation.

                claude takes an id we choose, minted and stored before the process
                exists. agy and codex issue their own, so a fresh turn passes nothing.
        """
        existing = agent["session_id"]
        if existing:
            return existing, True
        if agent["runtime"] != "claude":
            return None, False
        minted = str(uuid.uuid4())
        with db.transaction(self.conn) as conn:
            conn.execute("UPDATE agents SET session_id = ? WHERE id = ?", (minted, agent["id"]))
        return minted, False

    def _fuse_allows(self, state: _AgentState) -> bool:
        cutoff = time.monotonic() - FUSE_WINDOW_SECONDS
        state.wake_times = [t for t in state.wake_times if t > cutoff]
        # Over the cap the extra wake collapses rather than queueing: the
        # messages stay pending and ride the next turn that does happen.
        return len(state.wake_times) < FUSE_TURNS

    def _watch(self, name: str, turn, usage: "_Usage") -> None:
        """Follow one turn: session id, context, usage, death, and, once it is
                over, the end of anything the office was running for that agent.
        """
        for event in turn.stream(timeout=None):
            if event.session_id:
                self._remember_session(name, event.session_id)
            usage.take(event)
            if event.kind == "compact":
                _log.info("%s compacted (%s): %s -> %s tokens", name, event.text,
                          event.context_before, event.context_used)
        # Not a limit on the turn: the turn is already over, its stream ended.
        turn.wait(timeout=30)

        # The turn's process is gone, so anything the office was running on this
        # agent's behalf has nobody left to read it.
        try:
            commands.kill_agent(name, f"{name}'s turn ended")
        except Exception:
            _log.exception("could not stop %s's supervised commands", name)

        # Everything else this turn started, at any depth.
        marks.sweep(marks.agent_scope(name), name)

        reason = turn.death.reason if turn.death is not None else None
        runtime, model = usage.row["runtime"], usage.row["model"]
        state = self._state(name)
        # Everything up to the finally happens while state.turn still names this
        # turn, which is what _start_turn refuses on.
        try:
            resume_after = None
            if reason == "quota_exhausted":
                # The time of return, once per death, as the vendor's refusal names it.
                resume_after = turn.adapter.quota_reset(turn.death.error)
                if resume_after is not None:
                    core.hold_for_quota(self.conn, runtime, model, resume_after)
            self._close_turn(name, reason, resume_after)
            agent = db.query_one(self.conn, "SELECT id FROM agents WHERE name = ?", (name,))
            if agent:
                self._record_context(agent["id"], turn.context_used, turn.context_limit)
                self._set_status(agent["id"], "idle")
            if reason is not None:
                self._record_death(name, turn, resume_after)
        finally:
            # Unconditionally: _start_turn refuses for as long as this is set.
            state.turn = None

        # Whatever became of this turn, the owner's journal gets one line about
        # it.
        self._journal_turn_end(name, reason, runtime, model, resume_after)
        self._record_usage(usage, turn)
        self._poll_runtime(runtime)
        # Anything that arrived while the turn ran is now deliverable.
        self._schedule()

    def _set_status(self, agent_id: int, status: str) -> None:
        try:
            core.set_agent_status(self.conn, agent_id, status, actor="office")
        except Exception:
            _log.exception("could not set status %s for agent %s", status, agent_id)

    def _record_context(self, agent_id: int, used, limit) -> None:
        """The context figures roster() and the pages show."""
        if used is None and limit is None:
            return
        with db.transaction(self.conn) as conn:
            conn.execute(
                "UPDATE agents SET context_used = COALESCE(?, context_used), "
                "context_limit = COALESCE(?, context_limit) WHERE id = ?",
                (used, limit, agent_id),
            )

    def _usage_meta(self, agent: dict, session_id: str | None, process: str) -> "_Usage":
        """What the usage_turns row of a process about to start records of it
                from the start: the agent, its running work and the moment.
        """
        work = db.query_one(
            self.conn,
            "SELECT id, task_id FROM works WHERE agent_id = ? AND status = 'running' "
            "ORDER BY id DESC",
            (agent["id"],),
        )
        return _Usage(
            row={
                "agent": agent["name"],
                "agent_kind": agent["kind"],
                "runtime": agent["runtime"],
                "model": agent["model"],
                "effort": agent["effort"],
                "session_id": session_id,
                "process": process,
                "work_id": work["id"] if work else None,
                "task_id": work["task_id"] if work else None,
                "started_at": _utc_stamp(),
            },
        )

    def _record_usage(self, usage: "_Usage", turn) -> None:
        """The usage_turns row of a process that has ended, with its requests and
                compactions, in one transaction. Never raises.
        """
        row = dict(usage.row)
        try:
            adapter = turn.adapter
            row["session_id"] = turn.session_id or row["session_id"]
            requests = list(usage.requests.values())
            if hasattr(adapter, "turn_requests"):
                requests = adapter.turn_requests(row["session_id"], row["started_at"])
            totals = (
                adapter.turn_totals(requests) if hasattr(adapter, "turn_totals") else usage.totals
            ) or {}
            row.update({f"{key}_tokens": totals.get(key) for key in _USAGE_COUNTS})
            row.update(
                ended_at=_utc_stamp(),
                outcome=turn.death.reason if turn.death is not None else "clean",
                context_used=turn.context_used,
            )
            with db.transaction(self.conn) as conn:
                turn_id = conn.execute(
                    f"INSERT INTO usage_turns ({', '.join(row)}) "
                    f"VALUES ({', '.join('?' for _ in row)})",
                    tuple(row.values()),
                ).lastrowid
                conn.executemany(
                    "INSERT INTO usage_requests (turn_id, seq, at, model, input_tokens, "
                    "cache_read_tokens, cache_write_tokens, cache_write_1h_tokens, "
                    "output_tokens, thinking_tokens, tools) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (turn_id, seq, r["at"], r["model"], r["input"] or 0,
                         r["cache_read"] or 0, r["cache_write"], r["cache_write_1h"],
                         r["output"], r["thinking"],
                         ",".join(r["tools"]) if r["tools"] else None)
                        for seq, r in enumerate(requests, start=1)
                    ],
                )
                conn.executemany(
                    "INSERT INTO usage_compactions (turn_id, at, trigger, pre_tokens, "
                    "post_tokens) VALUES (?, ?, ?, ?, ?)",
                    [(turn_id, *c) for c in usage.compactions],
                )
        except Exception:
            _log.exception("could not record the usage of %s's %s", row["agent"], row["process"])

    # -- compaction ---------------------------------------------------------
    #
    # Compaction is possible only while the agent is free, and only on claude.

    def busy_reason(self, agent_name: str) -> str | None:
        """What a vendor process is doing on this agent's session at this instant,
                in words, or None when nothing is.

                Two conditions: a turn, and a compaction.
        """
        state = self._state(agent_name)
        if state.compacting:
            return "a compaction of this session is running"
        # `state.turn is not None`, not `turn.alive`: a process that has
        # exited still counts until _watch has closed the turn out.
        if state.turn is not None:
            return "a turn is running on this session"
        return None

    def compaction_blocker(self, agent_name: str) -> str | None:
        """Why `agent_name` cannot be compacted at this instant, or None.

                The one place that decides: the interface and the tool both ask it, so
                there are no two answers to one question.
        """
        agent = db.query_one(self.conn, "SELECT * FROM agents WHERE name = ?", (agent_name,))
        if agent is None:
            return f"no agent named {agent_name!r}"
        try:
            adapter = adapter_for(agent["runtime"], self.config)
        except Exception as exc:
            return f"{agent['runtime']} is not usable on this machine: {exc}"
        if not hasattr(adapter, "compact_command"):
            # Named per runtime rather than as one sentence about "unsupported".
            return (
                f"{agent['runtime']} cannot compact a session on request: "
                + (
                    "codex exposes compaction only through its app-server, which this office "
                    "does not use"
                    if agent["runtime"] == "codex"
                    else "it has no such command"
                )
                + "; agent(op=new_session) is the other lever."
            )
        if not agent["session_id"]:
            return "no session to compact yet"
        held = core.quota_hold(self.conn, agent["runtime"], agent["model"])
        if held is not None:
            return (
                f"{agent['runtime']} · {agent['model']} refused a turn for quota, and nothing "
                f"runs on it before {_fmt_epoch(held)}"
            )
        if self._workspace_of(agent_name) is None:
            return "no workspace"
        busy = self.busy_reason(agent_name)
        if busy is not None:
            # The one condition compaction shares with firing, asked of the one
            # predicate that knows it (busy_reason above).
            return f"compaction is only possible while the agent is free — {busy} right now"
        return None

    def compact(self, agent_name: str, actor: str | None = None) -> dict:
        """Summarize an agent's session in place, keeping the session and its id.

                Asked for, never automatic, and carried out now or not at all: there is
                no queue and nothing is deferred.

                Answers with the before and after token counts, or with the reason it
                did not run.
        """
        state = self._state(agent_name)
        with state.lock:
            blocker = self.compaction_blocker(agent_name)
            if blocker is not None:
                return {"ok": False, "reason": blocker}
            state.compacting = True
        try:
            return self._compact_now(agent_name, actor)
        finally:
            with state.lock:
                state.compacting = False
            # Anything that arrived while the session was busy is deliverable
            # again; _start_turn has been refusing for the duration.
            self._schedule()

    def _compact_now(self, agent_name: str, actor: str | None) -> dict:
        """The body of compact(), with the claim already held.
        """
        agent = db.query_one(self.conn, "SELECT * FROM agents WHERE name = ?", (agent_name,))
        ws = self._workspace_of(agent_name)
        adapter = adapter_for(agent["runtime"], self.config)

        session_id = agent["session_id"]
        url = self._mcp_endpoint(agent_name)
        before = after = None
        usage = self._usage_meta(agent, session_id, "compact")
        turn = AgentTurn(
            adapter,
            adapter.compact_command(str(ws), agent["model"], agent["effort"], url, session_id),
            str(ws), "", env=turn_env(adapter, agent_name, session_id, url), agent=agent_name,
        )
        turn.start()
        for event in turn.stream(timeout=600):
            usage.take(event)
            if event.kind == "compact":
                before, after = event.context_before, event.context_used
        turn.wait(timeout=60)
        # A compaction is a vendor process in the agent's workspace like any
        # other, and no watcher follows it.
        marks.sweep(marks.agent_scope(agent_name), f"{agent_name}'s compaction")
        self._record_usage(usage, turn)
        self._poll_runtime(agent["runtime"])
        if turn.death is not None:
            return {"ok": False, "reason": (
                f"the compaction died: {turn.death.reason}. Nothing changed.")}
        if before is None:
            return {"ok": False, "reason": (
                f"claude declined to compact (exit {turn.exit_code}): too little conversation "
                "to summarize yet. Nothing changed.")}
        self._record_context(agent["id"], after, None)
        freed = before - (after or 0)
        try:
            core.record_notice(
                self.conn, "info",
                f"Compacted {agent_name}'s session: {before} -> {after} tokens "
                f"({freed} freed).",
                actor=actor,
            )
        except Exception:
            _log.exception("could not journal the compaction of %s's session", agent_name)
        return {"ok": True, "before": before, "after": after, "freed": freed}

    # -- visibility and the stop button --

    def turn_status(self, agent_name: str) -> dict | None:
        """What the live turn of one agent looks like right now, or None.

                Elapsed silence is reported as a figure, never as a verdict.
        """
        turn = self._state(agent_name).turn
        if turn is None or not turn.alive:
            return None
        return {
            "agent": agent_name,
            "pid": turn.pid,
            "running_for": turn.running_for,
            "quiet_for": turn.quiet_for,
        }

    def turn_tail(self, agent_name: str) -> list[str] | None:
        """The live turn's own output lines, oldest first, or None if no turn runs.

                Lines, not rows: the caller parses them.
        """
        turn = self._state(agent_name).turn
        if turn is None or not turn.alive:
            return None
        return turn.tail()

    def live_turns(self) -> dict[str, dict]:
        """turn_status() for every agent that has one. What the monitor reads."""
        with self._agents_lock:
            names = list(self._agents)
        live = {}
        for name in names:
            status = self.turn_status(name)
            if status is not None:
                live[name] = status
        return live

    def stop_agent(self, agent_name: str, actor: str) -> dict:
        """End an agent's turn on purpose, and fail its work as 'killed'.

                Not to be confused with stop(), which shuts the bus itself down.

                The work is failed BEFORE the kill, and the order is load-bearing:
                kill() blocks until the process is gone and _watch would otherwise
                record the death first.

                Only a work still 'running' is touched; one that has reported is left
                alone.
        """
        turn = self._state(agent_name).turn
        if turn is None or not turn.alive:
            return {"ok": False, "reason": f"{agent_name} has no turn running"}
        agent = db.query_one(
            self.conn, "SELECT id, kind FROM agents WHERE name = ?", (agent_name,)
        )
        if agent is None:
            return {"ok": False, "reason": f"no agent named {agent_name!r}"}
        if agent["kind"] == "director" and actor == core.get_owner_name(self.conn):
            self._director_stopped = True
        work = db.query_one(
            self.conn,
            "SELECT id FROM works WHERE agent_id = ? AND status = 'running' ORDER BY id DESC",
            (agent["id"],),
        )
        if work is not None:
            core.fail_work(
                self.conn, work["id"], "killed", output_tail="\n".join(turn.tail()), actor=actor
            )
            # Whoever hears about a failed work hears about this one, unless it is
            # the one who asked for it.
            self._tell_work_ended(work["id"], agent_name, "killed", caused_by=actor)
        turn.kill()
        return {"ok": True, "work_id": work["id"] if work else None}

    def new_session(self, agent_name: str, actor: str) -> dict:
        """Drop an agent's conversation (core.start_new_session), but only while
                nothing is speaking on its session: a turn or a compaction would write
                its session id back. Decided and done under the lock that starts turns.
        """
        state = self._state(agent_name)
        with state.lock:
            busy = self.busy_reason(agent_name)
            if busy is not None:
                return {"ok": False, "reason": (
                    f"{busy} — {agent_name}'s session cannot be dropped until it ends."
                )}
            agent = db.query_one(self.conn, "SELECT id FROM agents WHERE name = ?", (agent_name,))
            if agent is None:
                return {"ok": False, "reason": f"no agent named {agent_name!r}"}
            core.start_new_session(self.conn, agent["id"])
        return {"ok": True}

    def fire_agent(self, agent_name: str, actor: str) -> dict:
        """Remove an agent, but only while nothing is speaking on its session.

                A turn or a compaction both count. The decision and the deletion happen
                under the lock that starts turns, so there is no gap between them.

                core.fire() owns every other refusal.
        """
        state = self._state(agent_name)
        with state.lock:
            busy = self.busy_reason(agent_name)
            if busy is not None:
                return {"ok": False, "reason": (
                    f"{busy} — {agent_name} cannot be fired until it ends."
                )}
            agent = db.query_one(self.conn, "SELECT id FROM agents WHERE name = ?", (agent_name,))
            if agent is None:
                return {"ok": False, "reason": f"no agent named {agent_name!r}"}
            try:
                core.fire(self.conn, agent["id"], actor=actor)
            except ValueError as exc:
                # core's own refusals, chiefly "still has active work(s)".
                return {"ok": False, "reason": str(exc)}
            with self._agents_lock:
                self._agents.pop(agent_name, None)
        return {"ok": True}

    def _remember_session(self, name: str, session_id: str) -> None:
        with db.transaction(self.conn) as conn:
            conn.execute(
                "UPDATE agents SET session_id = ? WHERE name = ? AND (session_id IS NULL OR session_id != ?)",
                (session_id, name, session_id),
            )

    def _record_death(self, name: str, turn, resume_after: int | None) -> None:
        """Record a classified death, through core like every other mutation, and
                tell whoever has to hear of it.

                Only a work still 'running' is touched. A quota death pauses the work
                rather than failing it; a context overflow additionally clears the
                session id.

                A lead's death for any reason but a stop is told to its manager, in the
                same message as the work's notice when that goes to the same manager.
        """
        reason = turn.death.reason
        # recipient -> the notices for it, in order
        notices: dict[str, list[str]] = {}
        try:
            agent = db.query_one(
                self.conn, "SELECT id, kind FROM agents WHERE name = ?", (name,)
            )
            if agent is None:
                return  # fired once its turn was over and before this ran
            work = db.query_one(
                self.conn,
                "SELECT id FROM works WHERE status = 'running' AND agent_id = ? ORDER BY id DESC",
                (agent["id"],),
            )
            if work is not None:
                # actor stays None throughout: the supervisor observed this, no
                # participant caused it, and core reads None as "the system".
                if reason == "quota_exhausted":
                    # Not a failure: the vendor stopped answering. The work stands down
                    # with the reason and the time it could resume.
                    core.pause_work(
                        self.conn, work["id"], "quota_exhausted", resume_after=resume_after,
                    )
                else:
                    core.fail_work(
                        self.conn, work["id"], reason, output_tail="\n".join(turn.death.tail)
                    )
                told = self._work_ended_text(
                    work["id"], name, reason, paused=reason == "quota_exhausted",
                    resume_after=resume_after,
                )
                if told is not None:
                    notices.setdefault(told[0], []).append(told[1])
            if agent["kind"] == "lead" and reason != "killed":
                notices.setdefault(core.manager_name(self.conn, agent["id"]), []).append(
                    _lead_turn_died_notice(name, reason, resume_after)
                )
            for recipient, texts in notices.items():
                core.send_message(self.conn, core.OFFICE_SENDER, recipient, "\n\n".join(texts))
        except Exception:
            _log.exception("could not record %s's death (%s)", name, reason)
        if reason == "context_overflow":
            # The session is unusable; the next turn must start a fresh one.
            with db.transaction(self.conn) as conn:
                conn.execute("UPDATE agents SET session_id = NULL WHERE name = ?", (name,))

    def _work_ended_text(
        self,
        work_id: int,
        agent_name: str,
        reason: str,
        *,
        paused: bool = False,
        resume_after=None,
        caused_by: str | None = None,
    ) -> tuple[str, str] | None:
        """(recipient, text) of the notice that a work failed or went to pause.

                The recipient is core.work_notice_recipient's. For a work the director
                assigned itself the notice is written to the owner's journal here
                instead, and None is returned. Nothing is said to whoever caused it
                with a stop, `caused_by`, the owner included.
        """
        recipient = core.work_notice_recipient(self.conn, work_id)
        brief = db.query_one(self.conn, "SELECT brief FROM works WHERE id = ?", (work_id,))["brief"]
        if recipient is None:
            if caused_by != core.get_owner_name(self.conn):
                core.record_notice(
                    self.conn, "info",
                    _work_ended_journal(work_id, agent_name, brief, reason, paused, resume_after),
                )
            return None
        if recipient == caused_by:
            return None
        return recipient, _work_ended_notice(work_id, agent_name, brief, reason, paused, resume_after)

    def _tell_work_ended(self, work_id: int, agent_name: str, reason: str, *, caused_by: str) -> None:
        """Send the notice that a work was failed by a stop (_work_ended_text).
                Never raises."""
        try:
            told = self._work_ended_text(work_id, agent_name, reason, caused_by=caused_by)
            if told is not None:
                core.send_message(self.conn, core.OFFICE_SENDER, told[0], told[1])
        except Exception:
            _log.exception(
                "could not tell anybody that work %s (%s) ended as %s", work_id, agent_name, reason
            )

    # -- the progress check -------------------------------------------------

    def _check_progress(self) -> None:
        """Tell the chain about every agent whose open works nothing moves, and the
                director when nothing in the office moves.

                An agent answers for the works _answerer gives it, and is stuck when it
                answers for some and _has_path finds nothing that will move them. The
                chain: level 0 is the agent, 1 its manager, and so on up to the
                director; one above the director is the owner's journal. A level whose
                runtime and model are held for quota is passed over, and so is the
                director's while the owner has stopped it. `_stalls` keeps, per stuck
                agent, its works as last found, the level told last, that message and
                MAX(messages.id) then; works found different drop the entry.

                Silent without a director.
        """
        director = db.query_one(self.conn, "SELECT * FROM agents WHERE kind = 'director'")
        if director is None:
            return
        agents = {r["id"]: dict(r) for r in db.query(self.conn, "SELECT * FROM agents")}
        ids = {a["name"]: a["id"] for a in agents.values()}
        answering: dict[int, list[dict]] = {}
        for row in db.query(self.conn, "SELECT * FROM works ORDER BY id"):
            answerer = self._answerer(dict(row), ids, director["id"])
            if answerer is not None:
                answering.setdefault(answerer, []).append(dict(row))
        head = db.query_one(self.conn, "SELECT COALESCE(MAX(id), 0) AS m FROM messages")["m"]
        told_director = False
        for agent in agents.values():
            works = answering.get(agent["id"], [])
            signature = ",".join(f"{w['id']}:{w['status']}" for w in works)
            stall = self._stalls.get(agent["name"])
            if stall is not None and stall["signature"] != signature:
                del self._stalls[agent["name"]]
                stall = None
            if not works or self._has_path(agent, works, agents):
                continue
            told = self._tell_stall(agent, works, signature, stall, agents, head)
            told_director = told_director or told == director["name"]
        if not told_director:
            self._tell_idle(director, agents)

    def _answerer(self, work: dict, ids: dict[str, int], director_id: int) -> int | None:
        """The agent a work waits on: its holder while it runs; its assigner once it
                has reported; for a pause or a failure, whoever is told of it
                (core.work_notice_recipient), the director for a work of its own.
                None for a work of the director's own that the owner stopped.
        """
        if work["status"] == "running":
            return work["agent_id"]
        if work["status"] == "done":
            return work["assigned_by_agent_id"]
        told = core.work_notice_recipient(self.conn, work["id"])
        if told is not None:
            return ids[told]
        return None if work["fail_reason"] == "killed" else director_id

    def _has_path(self, agent: dict, works: list[dict], agents: dict[int, dict]) -> bool:
        """Whether something will move `agent`'s works: a turn of its own, or of the
                other side of one of them; a direct message it has not read; a
                deferred message it set or will get, or an expectation it holds open;
                a running work it assigned to somebody else; or a direct message of
                its own that its addressee has not taken yet — for the owner, one he
                has not written back after.
        """
        name = agent["name"]
        if self._state(name).turn is not None:
            return True
        if agent["kind"] == "director" and self._director_stopped:
            return True
        for work in works:
            for other in (work["agent_id"], work["assigned_by_agent_id"]):
                if other != agent["id"] and self._state(agents[other]["name"]).turn is not None:
                    return True
        if db.query_one(
            self.conn,
            "SELECT 1 WHERE EXISTS (SELECT 1 FROM messages WHERE channel = 'dm' "
            "AND recipient = ? AND id > ?) "
            "OR EXISTS (SELECT 1 FROM scheduled_messages WHERE sender = ? OR recipient = ?) "
            "OR EXISTS (SELECT 1 FROM expectations WHERE agent_id = ? AND closed_at IS NULL) "
            "OR EXISTS (SELECT 1 FROM works WHERE assigned_by_agent_id = ? AND agent_id != ? "
            "AND status = 'running')",
            (name, agent["last_seen_message_id"] or 0, name, name, agent["id"], agent["id"],
             agent["id"]),
        ) is not None:
            return True
        owner = core.get_owner_name(self.conn)
        by_name = {a["name"]: a for a in agents.values()}
        for sent in db.query(
            self.conn,
            "SELECT recipient, MAX(id) AS id FROM messages WHERE channel = 'dm' AND sender = ? "
            "AND recipient != ? GROUP BY recipient",
            (name, name),
        ):
            if sent["recipient"] == owner:
                if db.query_one(
                    self.conn,
                    "SELECT 1 FROM messages WHERE channel = 'dm' AND sender = ? AND recipient = ? "
                    "AND id > ?",
                    (owner, name, sent["id"]),
                ) is None:
                    return True
                continue
            other = by_name.get(sent["recipient"])
            if other is None:
                continue
            if sent["id"] > (other["last_seen_message_id"] or 0):
                return True
            start = other["turn_start_message_id"]
            if start is not None and sent["id"] > start:
                return True
        return False

    def _tell_stall(
        self, agent: dict, works: list[dict], signature: str, stall: dict | None,
        agents: dict[int, dict], head: int,
    ) -> str | None:
        """The next word about a stuck agent, as the chain in `stall` stands: the
                name it went to, or None when nothing went or it went to the journal.

                No word while the last one has not been taken by its addressee — read,
                and no turn of its running — or while that addressee has a deferred
                message set since then waiting. Taken, it goes one level up; but back
                to level 1 when the agent has had a direct message since. After the
                journal, nothing until the agent has a direct message.
        """
        name = agent["name"]
        if stall is None:
            level = 0
        elif stall["message_id"] is None:
            if not self._written_to_since(name, stall["head"]):
                return None
            level = 1
        else:
            told = db.query_one(
                self.conn, "SELECT recipient, created_at FROM messages WHERE id = ?",
                (stall["message_id"],),
            )
            addressee = next((a for a in agents.values() if a["name"] == told["recipient"]), None)
            if addressee is not None:
                if (
                    (addressee["last_seen_message_id"] or 0) < stall["message_id"]
                    or self._state(addressee["name"]).turn is not None
                ):
                    return None
                if db.query_one(
                    self.conn,
                    "SELECT 1 FROM scheduled_messages WHERE sender = ? AND created_at > ?",
                    (addressee["name"], told["created_at"]),
                ) is not None:
                    return None
            level = 1 if self._written_to_since(name, stall["message_id"]) else stall["level"] + 1
        chain = [agent]
        while chain[-1]["manager_agent_id"] is not None:
            chain.append(agents[chain[-1]["manager_agent_id"]])
        while level < len(chain):
            step = chain[level]
            if step["kind"] == "director" and self._director_stopped:
                level = len(chain)
            elif core.quota_hold(self.conn, step["runtime"], step["model"]) is None:
                break
            else:
                level += 1
        holders = {a["id"]: a["name"] for a in agents.values()}
        message_id = None
        if level >= len(chain):
            core.record_notice(self.conn, "critical", _stall_journal(name, works, holders))
        else:
            message_id = core.send_message(
                self.conn, core.OFFICE_SENDER, chain[level]["name"],
                _stall_notice(name, works, holders, own=level == 0),
            )["id"]
        self._stalls[name] = {
            "signature": signature, "level": level, "message_id": message_id, "head": head,
        }
        return chain[level]["name"] if message_id is not None else None

    def _written_to_since(self, name: str, message_id: int) -> bool:
        """Whether `name` has had a direct message after `message_id`."""
        return db.query_one(
            self.conn,
            "SELECT 1 FROM messages WHERE channel = 'dm' AND recipient = ? AND id > ?",
            (name, message_id),
        ) is not None

    def _tell_idle(self, director: dict, agents: dict[int, dict]) -> None:
        """Tell the director that nothing in the office moves: no turn, no unread
                direct message, no deferred message, no open expectation and no
                running work anywhere, and a direct message other than the director's
                with the owner since the director last wrote to the owner or was last told
                this. Not while the owner has stopped the director.
        """
        if self._director_stopped or any(
            self._state(a["name"]).turn is not None for a in agents.values()
        ):
            return
        if db.query_one(
            self.conn,
            "SELECT 1 WHERE EXISTS (SELECT 1 FROM messages m JOIN agents a ON a.name = m.recipient "
            "WHERE m.channel = 'dm' AND m.id > COALESCE(a.last_seen_message_id, 0)) "
            "OR EXISTS (SELECT 1 FROM scheduled_messages) "
            "OR EXISTS (SELECT 1 FROM expectations WHERE closed_at IS NULL) "
            "OR EXISTS (SELECT 1 FROM works WHERE status = 'running')",
        ) is not None:
            return
        owner = core.get_owner_name(self.conn)
        name = director["name"]
        wrote = db.query_one(
            self.conn,
            "SELECT COALESCE(MAX(id), 0) AS m FROM messages WHERE channel = 'dm' "
            "AND sender = ? AND recipient = ?",
            (name, owner),
        )["m"]
        if db.query_one(
            self.conn,
            "SELECT 1 FROM messages WHERE channel = 'dm' AND id > ? "
            "AND NOT (sender IN (?, ?) AND recipient IN (?, ?))",
            (max(wrote, self._idle_told), name, owner, name, owner),
        ) is None:
            return
        holders = {a["id"]: a["name"] for a in agents.values()}
        works = [dict(r) for r in db.query(self.conn, "SELECT * FROM works ORDER BY id")]
        lines = ["[office] Nothing in the office is moving."]
        if works:
            lines.append("Open works:")
            lines.extend(_work_lines(works, holders))
        self._idle_told = core.send_message(
            self.conn, core.OFFICE_SENDER, name, "\n".join(lines)
        )["id"]

    # -- the owner's journal ----------------------------------------------

    def _journal_turn_end(
        self, name: str, reason: str | None, runtime: str, model: str, resume_after: int | None
    ) -> None:
        """One line in the owner's journal about a turn that has died, its severity
                depending on whose turn it was.
        """
        try:
            agent = db.query_one(self.conn, "SELECT kind FROM agents WHERE name = ?", (name,))
            if agent is None:
                return  # fired while its own turn was ending; nothing to name
            if reason == "quota_exhausted":
                severity, text = _quota_wall_notice(
                    name, agent["kind"], runtime, model, resume_after
                )
                core.record_notice(self.conn, severity, text)
                return
            if reason is not None:
                severity, text = _turn_death_notice(name, agent["kind"], reason)
                core.record_notice(self.conn, severity, text)
        except Exception:
            _log.exception("could not journal the end of %s's turn", name)

    def _note_tick_failure(self, agent: dict, exc: BaseException) -> None:
        """The office's own pass over one agent raised. Tell the owner, once per
                cause, and leave the fact standing where health() can see it.

                Cleared by that agent's next started turn.
        """
        try:
            state = self._state(agent["name"])
            signature = _failure_signature(exc)
            if signature == state.tick_failure:
                # Same cause, already in the journal.
                return
            state.tick_failure = signature
            state.tick_failure_detail = _failure_detail(exc)
            state.tick_failure_at = time.time()
            turn = state.turn
            severity, text = _tick_failure_notice(
                agent["name"], agent["kind"], turn is not None and turn.alive,
                state.tick_failure_detail,
            )
            core.record_notice(self.conn, severity, text)
        except Exception:
            _log.exception("could not journal the tick failure for %s", agent.get("name"))

    def _tick_failures(self) -> list[tuple[str, str, float]]:
        """(name, what it said, when it was first seen) for every agent whose last
                pass raised and who has started no turn since.
        """
        with self._agents_lock:
            states = sorted(self._agents.items())
        return [
            (name, state.tick_failure_detail, state.tick_failure_at)
            for name, state in states
            if state.tick_failure is not None
        ]

    # -- hub restart -------------------------------------------------------

    def recover_after_restart(self) -> None:
        """Every work whose assignee had a turn in progress died with the hub. Say
                so: to the director, all of them; to every other agent that would be
                told of such a work (core.work_notice_recipient) or of a lead's dead
                turn, those.

                The predicate is `agents.turn_start_message_id IS NOT NULL`: the column
                is set when a turn spawns and cleared when it ends.

                Runs before the tick starts, and before the app serves anything.
        """
        # Asked before the close-out loop below, which clears the column this
        # reads.
        interrupted = db.query(
            self.conn,
            "SELECT w.id, w.brief, a.name AS agent FROM works w "
            "JOIN agents a ON a.id = w.agent_id "
            "WHERE w.status = 'running' AND a.turn_start_message_id IS NOT NULL",
        )
        killed_leads = db.query(
            self.conn,
            "SELECT a.name, m.name AS manager FROM agents a JOIN agents m ON m.id = a.manager_agent_id "
            "WHERE a.kind = 'lead' AND a.turn_start_message_id IS NOT NULL ORDER BY a.name",
        )
        # recipient -> (its interrupted works, its leads whose turn died)
        told: dict[str, tuple[list, list[str]]] = {}
        for work in interrupted:
            recipient = core.work_notice_recipient(self.conn, work["id"])
            if recipient is not None:
                told.setdefault(recipient, ([], []))[0].append(work)
        for lead in killed_leads:
            told.setdefault(lead["manager"], ([], []))[1].append(lead["name"])
        # Through core, not raw SQL: core owns the only writer into `events`.
        for work in interrupted:
            tail_file = self.config.tails_dir / f"{work['agent']}.log"
            tail = tail_file.read_text(encoding="utf-8", errors="replace") if tail_file.exists() else None
            try:
                core.fail_work(self.conn, work["id"], "hub_restart", output_tail=tail)
            except Exception:
                _log.exception("could not record the hub restart for work %s", work["id"])

        # The owner's journal gets the same facts the director's message is
        # built from, out of the same composer.
        mid_turn_director = db.query_one(
            self.conn,
            "SELECT name FROM agents WHERE kind = 'director' "
            "AND turn_start_message_id IS NOT NULL",
        )
        try:
            if mid_turn_director is not None:
                core.record_notice(
                    self.conn, "critical", _restart_killed_director(interrupted)
                )
            else:
                core.record_notice(self.conn, "info", _restart_summary(interrupted))
        except Exception:
            _log.exception("could not journal the hub restart")

        # Every agent that still carries a spawn-time watermark was mid-turn
        # when the hub went down.
        for row in db.query(
            self.conn,
            "SELECT name FROM agents WHERE turn_start_message_id IS NOT NULL",
        ):
            self._close_turn(row["name"], "hub_restart", None)

        for row in db.query(self.conn, "SELECT id FROM agents WHERE status = 'running'"):
            self._set_status(row["id"], "idle")

        director = db.query_one(self.conn, "SELECT name FROM agents WHERE kind = 'director'")
        if director is None:
            return
        # The director hears of every interrupted work; everybody else only of
        # what it would have been told of. Nobody is written to when nothing was
        # interrupted.
        director_leads = told.get(director["name"], ([], []))[1]
        messages = {}
        if interrupted or director_leads:
            messages[director["name"]] = (list(interrupted), director_leads)
        for recipient, (works, leads) in told.items():
            if recipient != director["name"]:
                messages[recipient] = (works, leads)
        for recipient, (works, leads) in messages.items():
            try:
                # Sent as "office", the same participant name the inbox and the
                # notices already use.
                core.send_message(
                    self.conn, core.OFFICE_SENDER, recipient, _restart_delta(works, leads)
                )
            except Exception:
                _log.exception("could not deliver the restart delta to %s", recipient)
        self._schedule()

    # -- composition -------------------------------------------------------

    def _system_prompt(self, agent: dict) -> str:
        template = _template(agent["kind"])
        fields = {
            "name": agent["name"],
            # A system prompt is fixed when the session is created (_start_turn).
            # The team, the quota and the model catalogue are roster()'s.
            "rules": _rules(self.conn, agent["kind"]),
            "workspace": str(self._workspace_of(agent["name"]) or "(none yet)"),
            "sandbox": str(self._sandbox_of(agent["name"]) or "(none yet)"),
            # Through core, like every other reader of the owner's name.
            "owner": core.get_owner_name(self.conn),
            "instructions": agent["instructions"] or "(none)",
        }
        # format_map with a full dict ignores the fields a template does not
        # mention.
        return template.format_map(fields)

    def _compose(self, agent: dict, fresh: "_Pending", resume: bool, system: str) -> str:
        parts = []
        if not resume:
            # For agy and codex the role text rides in the first message of the
            # session. Once, not per turn.
            if system:
                parts.append(system)
            # A session that has never run gets the picture once and deltas forever
            # after.
            parts.append(_snapshot(self.conn, agent))
        if fresh.anything:
            parts.append(fresh.render())
        return "\n\n".join(p for p in parts if p)

    # -- small helpers -----------------------------------------------------

    def _all_agents(self) -> list[dict]:
        return [dict(r) for r in db.query(self.conn, "SELECT * FROM agents WHERE status != 'provisioning'")]

    def _state(self, name: str) -> _AgentState:
        with self._agents_lock:
            if name not in self._agents:
                self._agents[name] = _AgentState()
            return self._agents[name]

    def _mcp_endpoint(self, name: str) -> str:
        """This agent's own office endpoint: the one and only MCP server any agent
                gets, and never optional.

                Identity is the URL path. The trailing slash is load-bearing: without
                it the hub answers with a redirect, and the agy shim will not replay a
                POST on one.
        """
        return f"http://127.0.0.1:{self.config.port}/mcp/{name}/"

    def _workspace_of(self, name: str) -> Path | None:
        row = db.query_one(
            self.conn,
            "SELECT w.path FROM workspaces w JOIN agents a ON a.id = w.owner_agent_id WHERE a.name = ?",
            (name,),
        )
        if row is None:
            return None
        path = Path(row["path"])
        return path if path.exists() else None

    def _sandbox_of(self, name: str) -> Path | None:
        """The agent's sandbox: the workspace's own directory under scratch/."""
        ws = self._workspace_of(name)
        if ws is None:
            return None
        return self.config.scratch_dir / ws.name


#: The number keys of Event.request and Event.totals, each a `<key>_tokens` column.
_USAGE_COUNTS = ("input", "cache_read", "cache_write", "cache_write_1h", "output", "thinking")


def _utc_stamp() -> str:
    """Now, in the schema's ISO-8601 UTC form."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


@dataclass
class _Usage:
    """One vendor process as its usage_turns row will record it: the columns known
        as it started, and what its stream reported."""

    row: dict
    # key -> request, in order of first appearance.
    requests: dict = field(default_factory=dict)
    # (at, trigger, pre_tokens, post_tokens)
    compactions: list[tuple] = field(default_factory=list)
    totals: dict | None = None

    def take(self, event) -> None:
        """Fold one event in. Events with one request key are one request: the
            later numbers stand, the tools add up, the first moment stays."""
        request = event.request
        if request is not None:
            known = self.requests.get(request["key"])
            merged = {**request, "at": request["at"] or _utc_stamp()}
            if known is not None:
                merged["at"] = known["at"]
                if known["tools"] is not None:
                    merged["tools"] = list(dict.fromkeys(known["tools"] + request["tools"]))
            self.requests[request["key"]] = merged
        if event.kind == "compact":
            self.compactions.append(
                (_utc_stamp(), event.text, event.context_before, event.context_used)
            )
        if event.totals is not None:
            self.totals = event.totals


@dataclass
class _Pending:
    """One agent's undelivered queue, already filtered down to what it should
        see.

        Messages only.
    """

    lines: list[str] = field(default_factory=list)
    guaranteed: bool = False
    last_message_id: int = 0
    # The distinct senders of its direct messages, in order.
    senders: list[str] = field(default_factory=list)
    # Whether one of its direct messages is the owner's.
    from_owner: bool = False
    chat_count: int = 0
    quiet_count: int = 0

    @property
    def anything(self) -> bool:
        return bool(self.lines)

    def render(self) -> str:
        return "\n".join(self.lines)

    @classmethod
    def build(cls, agent: dict, messages, owner: str) -> "_Pending":
        pending = cls()
        name = agent["name"]
        chat_lines: list[tuple[int, int]] = []  # (index in lines, message id)
        for row in messages:
            # The id first, the decision after: a row this agent is not
            # shown still has to be passed.
            pending.last_message_id = max(pending.last_message_id, row["id"])
            if row["sender"] == name and row["recipient"] != name:
                # Your own message is not news to you — unless you addressed it
                # to yourself, which is what a wake is (core.schedule_message):
                # sender and recipient are the same agent, and the whole point of
                # the row is that it comes back to them later.
                continue
            if row["channel"] == "dm":
                if row["recipient"] != name:
                    continue  # two other people's conversation
                # A direct message is the one thing worth a turn of its own.
                pending.guaranteed = True
                if row["sender"] not in pending.senders:
                    pending.senders.append(row["sender"])
                if row["sender"] == owner:
                    pending.from_owner = True
                # Both prefixes say what they are in words.
                if row["sender"] == name:
                    # The wake it set for itself, come due. Labelled as what it
                    # is: "direct message from <your own name>" reads as somebody
                    # else's until the name is recognised as one's own.
                    pending.lines.append(f"[the wake you set] {row['body']}")
                else:
                    pending.lines.append(f"[direct message from {row['sender']}] {row['body']}")
            elif row["recipient"] is not None:
                # The common channel addressed to one person: the office's own quiet
                # line (core.tell_quietly). Shown to nobody else.
                if row["recipient"] != name:
                    continue
                pending.quiet_count += 1
                pending.lines.append(row["body"])
            else:
                pending.chat_count += 1
                chat_lines.append((len(pending.lines), row["id"]))
                pending.lines.append(f"[common chat, from {row['sender']}] {row['body']}")

        left_out = len(chat_lines) - COMMON_CHAT_DELIVERED
        if left_out > 0:
            dropped = {index for index, _ in chat_lines[:left_out]}
            pending.lines = [
                f"[common chat] {left_out} earlier lines are not shown here; "
                f"chat(before_id={chat_lines[left_out][1]}) can read them.",
                *(line for index, line in enumerate(pending.lines) if index not in dropped),
            ]
        return pending


_PROMPT_DIR = Path(__file__).parent / "prompts"


def _template(kind: str) -> str:
    """The system prompt template for an agent of `kind`: director, lead or
    executor. Its placeholders are {name}, {owner}, {rules}, {workspace},
    {sandbox} and {instructions}."""
    return (_PROMPT_DIR / f"{kind}.md").read_text(encoding="utf-8")


def _rules(conn: sqlite3.Connection, kind: str) -> str:
    """The standing conventions, substituted into every template on every turn
        of every agent, as core.render_rules() prints them. The director's also
        says that the number is the address note(op=update|delete, kind=rule)
        takes.
    """
    rules = core.render_rules(conn)
    if kind == "director" and rules != core.NO_RULES:
        rules += "\n([n] is the rule_id note(op=update|delete, kind=rule) takes.)"
    return rules


def _fmt_epoch(value) -> str:
    """A stored epoch reset time in the machine's own zone. Never a guess: the
    adapters store NULL when a vendor's answer could not be turned into one,
    and callers check for that before asking. Local rather than UTC, with the
    zone named, for the reason office/mcp.py's _fmt_epoch gives."""
    try:
        local = datetime.fromtimestamp(int(value), tz=timezone.utc).astimezone()
        return local.strftime("%Y-%m-%d %H:%M") + " local"
    except (TypeError, ValueError, OSError, OverflowError):
        return str(value)


def _work_ended_notice(
    work_id: int, agent_name: str, brief: str, reason: str, paused: bool, resume_after
) -> str:
    """What the office says to the work's assigner, or to its assignee's manager,
    about a work that failed or paused.
    """
    head = f"[office] work {work_id} ({agent_name}): {_excerpt(brief)}".rstrip(": ")
    if paused:
        when = _fmt_epoch(resume_after) if resume_after else None
        return (
            f"{head}\nPaused: {reason}" + (f", earliest resume {when}" if when else "") + ". "
            f"{agent_name}'s next turn resumes it: write to {agent_name} after the time of "
            "return."
        )
    return (
        f"{head}\nFailed: {reason}. work(op=show, work={work_id}) shows its output tail; "
        "work_reassign(workspace='inherit') carries its tree on, to the same agent included."
    )


def _work_ended_journal(
    work_id: int, agent_name: str, brief: str, reason: str, paused: bool, resume_after
) -> str:
    """The owner's journal line about a work the director assigned itself that
    failed or paused."""
    head = f"Work {work_id} ({agent_name}): {_excerpt(brief)}".rstrip(": ")
    if paused:
        when = _fmt_epoch(resume_after) if resume_after else None
        return f"{head} — paused: {reason}" + (f", earliest resume {when}." if when else ".")
    return f"{head} — failed: {reason}. The output tail is on the work record."


def _lead_turn_died_notice(name: str, reason: str, resume_after) -> str:
    """What the office says to a lead's manager when the lead's turn died."""
    if reason == "quota_exhausted":
        when = _fmt_epoch(resume_after) if resume_after else None
        cause = "its runtime is out of quota" + (f", earliest return {when}" if when else "")
    elif reason == "context_overflow":
        cause = "its context overflowed, and its next turn starts a fresh session"
    else:
        cause = reason
    return f"[office] {name}'s turn died: {cause}. What it was in the middle of stopped there."


def _fmt_duration(seconds: float) -> str:
    """Coarse, like the counters on the owner's pages.
    """
    total = int(max(0, seconds))
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m {total % 60}s"
    return f"{total // 3600}h {(total % 3600) // 60}m"


def _last_actions(turn, limit: int = 5) -> list[str]:
    """The last few things a live turn was seen doing, out of its own tail.

        Read from the in-memory ring buffer (AgentTurn.tail) and parsed with the
        turn's own adapter. Never raises.
    """
    # (phase, text) with the text unabbreviated: the pairing below compares
    # the raw text on each side.
    rows: list[tuple[str, str]] = []
    for line in turn.tail():
        if line.startswith("stderr: "):
            # office/process.py writes every stderr line into the buffer with this
            # prefix.
            rows.append(("stderr", line[len("stderr: "):]))
            continue
        try:
            event = turn.adapter.parse_line(line)
        except Exception:  # noqa: BLE001 — see the docstring
            continue
        if event is None:
            continue
        if event.kind == "tool":
            text = event.text or "tool call"
            phase = event.phase or "called"
            if (
                phase == "finished"
                and rows
                and rows[-1][0] == "started"
                and (text.startswith(rows[-1][1]) or rows[-1][1].startswith(text))
            ):
                rows[-1] = (phase, text)
                continue
            rows.append((phase, text))
        elif event.kind == "error":
            rows.append(("error", event.error or event.text or "error"))
    return [f"- {phase}: {_excerpt(text)}" for phase, text in rows[-limit:]]


def _silent_turn_notice(agent_name: str, quiet_for: float, running_for: float, actions: list[str]) -> str:
    """What the office says to an agent's manager when the agent has gone quiet
        (_report_silent_turn).
    """
    head = (
        f"[office] {agent_name} has produced no output for {_fmt_duration(quiet_for)} "
        f"(its turn has been running {_fmt_duration(running_for)})."
    )
    body = (
        "Nothing has been done to the agent; a long test or a slow build looks the same from "
        "outside."
    )
    if actions:
        evidence = "The last things it was seen doing:\n" + "\n".join(actions)
    else:
        evidence = "Nothing in the last 200 lines of its output is a tool call."
    return f"{head}\n{body}\n{evidence}\nNothing else will look."


_WORK_STATUS_WORDS = {"running": "open", "done": "reported"}


def _work_lines(works: list[dict], holders: dict[int, str]) -> list[str]:
    """One line per work: number, holder, status, the start of its brief."""
    return [
        f"  - work {w['id']} ({holders[w['agent_id']]}, "
        f"{_WORK_STATUS_WORDS.get(w['status'], w['status'])}): {_excerpt(w['brief'])}"
        for w in works
    ]


def _stall_notice(name: str, works: list[dict], holders: dict[int, str], *, own: bool) -> str:
    """The word to `name`, or to a manager above it, that nothing moves the works
    `name` answers for."""
    whose = "you answer for" if own else f"{name} answers for"
    return "\n".join(
        [f"[office] Nothing moves these works {whose}:", *_work_lines(works, holders)]
    )


def _stall_journal(name: str, works: list[dict], holders: dict[int, str]) -> str:
    """The owner's line when nobody in the chain above `name` has moved them."""
    return "\n".join([
        f"Nothing moves these works {name} answers for, and nobody above {name} has moved "
        "them:",
        *_work_lines(works, holders),
    ])


def _undelivered_notice(recipient: str, reason: str, resume_after, bodies: list[str]) -> str:
    """The office's answer to a sender whose messages died with a turn.

        One notice per sender, not per message.
    """
    again = "send it again if it still matters"
    if reason == "quota_exhausted":
        cause = "its runtime is out of quota"
        if resume_after:
            again += f"; {recipient} is back at {_fmt_epoch(resume_after)}"
    elif reason == "hub_restart":
        cause = "the hub restarted and the turn died with it"
    else:
        cause = f"the turn died: {reason}"
    what = "message was" if len(bodies) == 1 else f"{len(bodies)} messages were"
    lines = [
        f"[office] Your {what} not processed by {recipient} — {cause}. "
        f"Nothing is re-delivered automatically; {again}."
    ]
    lines.extend(f"  - {_excerpt(body)}" for body in bodies)
    return "\n".join(lines)


def _lost_hook_notice(sender: str, body: str) -> str:
    """A service's message that died with a turn, as the quiet line that gives it
    back to the agent it was for."""
    return f"[office] A turn of yours ended before it processed this message from {sender}:\n{body}"


def _expired_expectation_notice(about: str) -> str:
    """The office's message to an agent whose expectation came due unanswered."""
    return (
        f"[office] Nothing came about '{about}' within the time you gave, and the address you "
        "handed out for it is closed; the service's own API can say what became of it."
    )


def _lost_wake_notice(wake: dict) -> str:
    """The office's answer to a sender whose deferred message has nowhere to go.
    """
    return "\n".join(
        [
            f"[office] The message you had set for {wake['recipient']} was not delivered: "
            f"{wake['recipient']} is no longer a participant. It has been dropped.",
            f"  - {_excerpt(wake['body'])}",
        ]
    )


def _excerpt(body: str, limit: int = 160) -> str:
    """One line of a message, enough to recognise it by and no more."""
    first = body.strip().splitlines()[0] if body.strip() else ""
    if len(first) > limit:
        first = first[: limit - 1].rstrip() + "…"
    elif len(body.strip().splitlines()) > 1:
        first += " …"
    return first


def _turn_death_notice(name: str, kind: str, reason: str) -> tuple[str, str]:
    """Severity and text for the owner's journal, for a turn that died of
        something other than quota.
    """
    who = "The director" if kind == "director" else name
    if reason == "killed":
        return "info", f"{who}'s turn was stopped on request."
    if kind == "director":
        return "critical", f"The director's turn failed: {reason}."
    return "info", f"{who}'s turn failed: {reason}."


_VARYING_NUMBER = re.compile(r"\d+")


def _failure_detail(exc: BaseException) -> str:
    """What the owner is shown about an exception: its class and its own words.
    """
    return f"{type(exc).__name__}: {exc}"


def _failure_signature(exc: BaseException) -> str:
    """The dedup key for a failing tick: is this the SAME failure as last time?

        The exception's class, plus its text with every run of digits replaced, so
        two failures that differ only in an id or a pid are one cause.
    """
    site = "?"
    tb = exc.__traceback__
    while tb is not None:
        site = f"{os.path.basename(tb.tb_frame.f_code.co_filename)}:{tb.tb_lineno}"
        tb = tb.tb_next
    return f"{type(exc).__name__}@{site}:{_VARYING_NUMBER.sub('#', str(exc))}"


def _tick_failure_notice(name: str, kind: str, running: bool, detail: str) -> tuple[str, str]:
    """Severity and text for the owner's journal, for a pass over one agent that
        raised.
    """
    if kind == "director" and not running:
        return "critical", (
            f"The office cannot start the director's turn: {detail}. This is the office's own "
            "tick failing, not the vendor and not quota, so nothing of the director's will run "
            "until it stops."
        )
    return "info", (
        f"The office's tick over {name} failed: {detail}. No new turn of {name}'s can start "
        "until that stops."
    )


def _quota_wall_notice(
    name: str, kind: str, runtime: str, model: str, resume_after
) -> tuple[str, str]:
    """Severity and text for a turn the vendor refused for quota.
    """
    held = (
        f"no turn starts on {runtime} · {model} before {_fmt_epoch(resume_after)}"
        if resume_after else f"{runtime} gave no return time"
    )
    if kind == "director":
        return "critical", f"The director is out of quota: {held}."
    return "info", f"{name}'s turn stopped: {runtime} is out of quota; {held}."


def _restart_summary(interrupted: list) -> str:
    """What a restart did, as facts: every turn in flight died with the hub, and
        these works went with them.
    """
    if not interrupted:
        return "The hub restarted. Nothing was in progress."
    return "\n".join(
        [
            "The hub restarted and every turn that was running died with it. "
            "These works are now marked failed with reason hub_restart; their output "
            "tails are attached to the work records:"
        ]
        + _restart_work_lines(interrupted)
    )


def _restart_work_lines(interrupted: list) -> list[str]:
    """The works a restart interrupted, one indented line each.
    """
    return [f"  - work {work['id']} ({work['agent']}): {_excerpt(work['brief'])}" for work in interrupted]


def _restart_killed_director(interrupted: list) -> str:
    """The owner's plate when the restart took the director's turn down with it.
    """
    lines = [
        "The hub restarted and the director's turn died with it. Nothing that turn was "
        "handed is re-delivered — if you had written to it, send it again."
    ]
    if interrupted:
        lines.append(
            "These works died with it too and are marked failed with reason hub_restart; "
            "their output tails are attached to the work records:"
        )
        lines.extend(_restart_work_lines(interrupted))
    return "\n".join(lines)


def _restart_delta(interrupted: list, leads: list[str]) -> str:
    """The office's message after a restart: the interrupted works and the leads
    reporting to the recipient whose turn died."""
    if interrupted:
        lines = [
            f"[office] {_restart_summary(interrupted)}",
            "work(op=show, work=<id>) shows a work's output tail.",
        ]
    else:
        lines = ["[office] The hub restarted and every turn that was running died with it."]
    if leads:
        lines.append(
            f"These leads of yours were in a turn, which died with the hub: {', '.join(leads)}."
        )
    lines.append(
        "work_reassign(workspace='inherit') carries a work on in the tree it has, to the same "
        "agent included."
    )
    return "\n".join(lines)


_SNAPSHOT_STATUS_ORDER = ("in_progress", "needs_clarification", "paused", "planned", "idea")
_SNAPSHOT_TASKS = 50


def _snapshot(conn: sqlite3.Connection, agent: dict) -> str:
    """The one-off picture a brand-new session gets instead of a delta.

        Assembled from what the office already holds: who this agent is, the
        team, this agent's own work and its task with the tasks above it, the open
        PRs, the top-level tasks not done, at most _SNAPSHOT_TASKS of them, and any
        open ticket addressed to it.
    """
    who = f"{agent['name']} ({agent['kind']}"
    if agent["title"]:
        who += f", {agent['title']}"
    who += ")"
    manager = core.manager_name(conn, agent["id"])
    if manager is not None:
        who += f", reporting to {manager}"
    lines = [f"[office] You are {who}. New session."]
    roster = core.team_tree(conn)
    lines.append("Team, each agent under its manager:")
    lines.extend(
        "  " * (r["depth"] + 1) + r["name"] + (f" — {r['title']}" if r["title"] else "")
        + f" ({r['kind']}/{r['runtime']}/{r['status']})"
        for r in roster
    )
    # core's own reader, not a query of our own.
    work = core.current_work(conn, agent["id"])
    if work:
        assigner = (
            "you" if work["assigned_by_agent_id"] == agent["id"]
            else db.query_one(
                conn, "SELECT name FROM agents WHERE id = ?", (work["assigned_by_agent_id"],)
            )["name"]
        )
        lines.append(
            f"Your work: work {work['id']} "
            f"[{ {'running': 'open', 'done': 'reported'}.get(work['status'], work['status']) }] "
            f"on branch {work['branch']}, "
            f"assigned by {assigner}: {work['brief']}"
        )

    # Open PRs. Merged and closed ones are deleted rather than archived.
    prs = db.query(
        conn,
        "SELECT p.id, p.title, p.source_branch, p.target_branch, p.status, a.name AS author "
        "FROM prs p LEFT JOIN agents a ON a.id = p.author_agent_id "
        "WHERE p.status = 'open' ORDER BY p.id",
    )
    if prs:
        lines.append("Open PRs:")
        lines.extend(
            f"  PR {r['id']} [{r['status']}] {r['title']} "
            f"({r['source_branch']} -> {r['target_branch']}, by {r['author'] or 'unknown'})"
            for r in prs
        )

    tasks = core.board(conn)
    if work and work["task_id"] is not None:
        lines.append("Your work's task, then each task above it:")
        lines.extend("  " + core.task_line(t) for t in core.task_chain(tasks, work["task_id"]))
    # Work under way first, ideas last: the cap must not spend itself on ideas.
    top = sorted(
        (t for t in tasks.values()
         if t["parent_task_id"] is None and t["status"] not in core.CLOSED_TASK_STATUSES),
        key=lambda t: _SNAPSHOT_STATUS_ORDER.index(t["status"]),
    )
    if top:
        lines.append("Top-level tasks not done:")
        lines.extend("  " + core.task_line(t) for t in top[:_SNAPSHOT_TASKS])
        if len(top) > _SNAPSHOT_TASKS:
            lines.append(f"  {len(top) - _SNAPSHOT_TASKS} more left out")
    lines.append("task(op=list) and task(op=read) read the board.")

    # Open tickets addressed to THIS participant, and only to it. A ticket wakes
    # nobody, so a fresh session has no other way to learn one is waiting.
    tickets = db.query(
        conn,
        "SELECT id, status, title, author FROM tickets "
        "WHERE status = 'open' AND addressee = ? ORDER BY id",
        (agent["name"],),
    )
    if tickets:
        # Only when there are some.
        lines.append("Open tickets addressed to you (read one with ticket(op=read, ticket_id=N)):")
        lines.extend(
            f"  ticket {r['id']} [{r['status']}] {r['title']} (from {r['author']})" for r in tickets
        )

    return "\n".join(lines)
