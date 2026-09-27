"""Stages: working trees the office owns and agents run commands on one at a time."""

from __future__ import annotations

import atexit
import logging
import os
import re
import shutil
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from office import commands, core, db, git, marks
from office.config import Config
from office.process import _child_env, _kill_tree

_log = logging.getLogger("office.stages")

NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")

#: How many lines of a failed preparation's output its reason keeps.
REASON_LINES = 200

#: How many changed paths outside LFS a run's result names.
RESULT_PATHS = 20

_REF_PREFIX = "refs/office/stage/"

#: A stage's LFS filters: content in the tree, not pointers.
_LFS_ORDINARY = {
    "filter.lfs.process": "git-lfs filter-process",
    "filter.lfs.smudge": "git-lfs smudge -- %f",
    "filter.lfs.clean": "git-lfs clean -- %f",
    "filter.lfs.required": "true",
}

_RECREATE = "The stage is unusable until it is deleted and created again."

_RESET_AGAIN = "The stage is unusable until it is reset."

#: How long a kill waits for a run or a preparation to end before it gives up on it.
_KILL_WAIT_SECONDS = 10.0

_HUB_RESTART = "The hub restarted while the stage was being prepared."

#: Makes git-lfs print its progress to a pipe.
_LFS_PROGRESS = {"GIT_LFS_FORCE_PROGRESS": "1"}


# --------------------------------------------------------------------------- in memory


class _Preparation:
    """One preparation of one stage: clone (for a new stage), switch, `prepare`."""

    def __init__(self, stage: str) -> None:
        self.mark = marks.preparation_mark(stage)
        self.started_at = time.monotonic()
        self.last_output_at = self.started_at
        self.proc = None
        self.cancelled = threading.Event()
        self.thread: threading.Thread | None = None


@dataclass
class _Live:
    """What the office holds about a stage outside its row."""

    queue: list = field(default_factory=list)  # StageRun, waiting, in order
    running: "StageRun | None" = None
    pending: str | None = None  # reset | delete
    preparation: _Preparation | None = None


#: Guards _state, _runs and every StageRun's place in them. Never held across a
#: subprocess call.
_lock = threading.RLock()
_state: dict[str, _Live] = {}
#: Every stage run not yet finished, from the moment it is enrolled.
_runs: set["StageRun"] = set()
#: Serialises create, reset and delete, and the pending operation a run's end
#: carries out. Taken before _lock, never inside it.
_ops_lock = threading.Lock()
#: Set at hub exit. Nothing new starts and nothing is written after it.
_closing = threading.Event()


def _live(name: str) -> _Live:
    """Call with _lock held."""
    return _state.setdefault(name, _Live())


# --------------------------------------------------------------------------- git for a stage


def _git(args: list[str], cwd, mark: str, *, env: dict[str, str] | None = None,
         check: bool = False, input: str | None = None):
    """git with hooks switched off and `mark` on every process it starts."""
    return git.git(
        ["-c", f"core.hooksPath={git.NO_HOOKS}", *args],
        cwd=cwd, env_extra={marks.MARK_ENV: mark, **(env or {})}, check=check, input=input,
    )


def _step(args: list[str], cwd, mark: str, note, env: dict[str, str] | None = None) -> str | None:
    """_git's rules, with every line of output handed to `note` as it arrives.

    Returns once git has exited and its output has been read, the read waiting
    at most commands._READER_JOIN_SECONDS past the exit. git's output on
    failure, its last REASON_LINES lines; else None.
    """
    proc = git.spawn(["-c", f"core.hooksPath={git.NO_HOOKS}", *args], cwd=cwd,
                     env_extra={marks.MARK_ENV: mark, **(env or {})})
    said: deque[str] = deque(maxlen=REASON_LINES)
    said_lock = threading.Lock()

    def read() -> None:
        for line in iter(proc.stdout.readline, ""):
            with said_lock:
                said.append(line)
            note(line)

    reader = threading.Thread(target=read, daemon=True, name=f"office-stage-git-{mark}")
    reader.start()
    code = proc.wait()
    reader.join(commands._READER_JOIN_SECONDS)
    if code == 0:
        return None
    with said_lock:
        output = "".join(said).strip()
    return f"git {' '.join(args[:2])} failed:\n{output}"


def _said(proc) -> str:
    return (proc.stdout + proc.stderr).strip()


def _ref(stage: str, workspace_id: str) -> str:
    return f"{_REF_PREFIX}{stage}/{workspace_id}"


def _clone(config: Config, tree: Path, mark: str, note) -> str | None:
    """Clone a new stage tree, its large files as pointers. git's output on failure, else None."""
    return _step([*git.clone_args(config, tree), "--progress"], None, mark, note,
                 {"GIT_LFS_SKIP_SMUDGE": "1"})


def _configure(config: Config, tree: Path, mark: str, note) -> str | None:
    """Write a stage's configuration, the submodules' included. git's output on failure, else None.

    It is a workspace's, with the LFS filters in their ordinary mode.
    """
    for args in (
        *[["config", key, value]
          for key, value in {**git.clone_settings(config), **_LFS_ORDINARY}.items()],
        ["submodule", "foreach", "--recursive", git.submodule_lfs_command(config, _LFS_ORDINARY)],
    ):
        proc = _git(args, tree, mark)
        note(_said(proc))
        if proc.returncode != 0:
            return f"git {' '.join(args[:2])} failed:\n{_said(proc)}"
    return None


def _fill(tree: Path, mark: str, note) -> str | None:
    """Replace every LFS pointer in the tree, submodules' included, with its content.

    git's output on failure, else None.
    """
    return (_step(["lfs", "pull"], tree, mark, note, _LFS_PROGRESS)
            or _step(["submodule", "foreach", "--recursive", "git lfs pull"], tree, mark, note,
                     _LFS_PROGRESS))


def _switch(config: Config, tree: Path, ref: str, oid: str | None, mark: str, note) -> str | None:
    """Remove the lock files git left in the tree, then put it at `oid`, or at what `ref`
    names in project.git.

    Returns git's output, or the operating system's, on failure; else None.
    """
    project = git._posix(git.project_git(config))
    removed = _remove_git_locks(tree)
    if removed:
        note(f"removed lock files git left behind: {', '.join(removed)}")
    try:
        failure = _step(["fetch", "--progress", "--no-recurse-submodules", project, ref], tree,
                        mark, note)
        if failure is not None:
            return failure
        target = oid or _git(["rev-parse", "FETCH_HEAD"], tree, mark).stdout.strip()
        for args in (
            ["checkout", "--progress", "--force", target],
            ["submodule", "update", "--progress", "--init", "--recursive", "--force"],
            ["clean", "-fd"],
            ["submodule", "foreach", "--recursive", "git clean -fd"],
        ):
            failure = _step(args, tree, mark, note, _LFS_PROGRESS)
            if failure is not None:
                return failure
    except OSError as exc:
        return f"{tree}: {exc.strerror or exc}"
    return None


def _remove_git_locks(tree: Path) -> list[str]:
    """Delete every `*.lock` file under the tree's .git, submodules' included."""
    removed = []
    for dirpath, dirnames, filenames in os.walk(tree / ".git"):
        dirnames[:] = [d for d in dirnames if d not in ("objects", "lfs")]
        for name in filenames:
            if name.endswith(".lock"):
                path = Path(dirpath, name)
                try:
                    path.unlink()
                    removed.append(path.relative_to(tree).as_posix())
                except OSError as exc:
                    _log.warning("could not remove %s: %s", path, exc)
    return removed


def _record_tree(repo: Path, parent: str, message: str, mark: str,
                 index: Path | None = None) -> tuple[str, str]:
    """`add -A` and `write-tree` on `repo`'s index, or on `index`; a commit on `parent`.

    Returns (commit, tree). Raises git.GitError.
    """
    env = {"GIT_INDEX_FILE": str(index)} if index is not None else {}
    _git(["add", "-A"], repo, mark, env=env, check=True)
    tree = _git(["write-tree"], repo, mark, env=env, check=True).stdout.strip()
    commit = _git(["commit-tree", tree, "-p", parent, "-m", message], repo, mark,
                  env=git._OFFICE_IDENTITY, check=True).stdout.strip()
    return commit, tree


def _push(config: Config, repo: Path, commit: str, ref: str, mark: str) -> None:
    """Point `ref` in project.git at `commit`, whatever it named before. Raises git.GitError."""
    _git(["push", "--recurse-submodules=no", git._posix(git.project_git(config)),
          f"+{commit}:{ref}"], repo, mark, check=True)


def snapshot(config: Config, workspace: Path, stage: str, workspace_id: str, mark: str,
             index: Path) -> str:
    """The agent's working tree as commit S on its HEAD, pushed to the stage's ref for it.

    Built on `index`, a copy of the workspace's own index, which is removed
    afterwards. Tracked changes and untracked files that are not ignored go
    in. Raises git.GitError.
    """
    own = _git(["rev-parse", "--git-path", "index"], workspace, mark, check=True).stdout.strip()
    shutil.copyfile(workspace / own, index)
    try:
        commit, _ = _record_tree(workspace, "HEAD", f"stage {stage}: working tree of {workspace_id}",
                                 mark, index)
    finally:
        index.unlink(missing_ok=True)
    _push(config, workspace, commit, _ref(stage, workspace_id), mark)
    return commit


# --------------------------------------------------------------------------- the row


def _row(conn, name: str):
    return db.query_one(conn, "SELECT * FROM stages WHERE name = ?", (name,))


def _changed(conn, name: str, actor: str | None = None) -> None:
    """Tell the pages that what overview() returns for `name` has changed."""
    if _closing.is_set():
        return
    with db.transaction(conn):
        core._emit(conn, "stages", "stage", None, {"name": name}, actor=actor)


def _set_state(conn, name: str, state: str, reason: str | None = None,
               actor: str | None = None) -> None:
    if _closing.is_set():
        return
    with db.transaction(conn):
        db.execute(conn, "UPDATE stages SET state = ?, reason = ? WHERE name = ?",
                   (state, reason, name))
        core._emit(conn, "stages", "stage", None, {"name": name, "state": state}, actor=actor)


def _fmt(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {seconds % 3600 // 60}m"


# --------------------------------------------------------------------------- reading


def overview(conn) -> list[dict]:
    """Every stage, ordered by name."""
    rows = db.query(conn, "SELECT name, prepare, state, reason FROM stages ORDER BY name")
    now = time.monotonic()
    out = []
    with _lock:
        for row in rows:
            live = _state.get(row["name"]) or _Live()
            prep = live.preparation if row["state"] == "preparing" else None
            run = live.running
            out.append({
                "name": row["name"],
                "state": row["state"],
                "reason": row["reason"],
                "prepare": row["prepare"],
                "preparing_for": now - prep.started_at if prep else None,
                "preparing_quiet_for": now - prep.last_output_at if prep else None,
                "running": {
                    "agent": run.agent,
                    "running_for": now - run.took_at,
                    "quiet_for": run.quiet_for,
                } if run else None,
                "waiting": [r.agent for r in live.queue],
                "pending": live.pending,
            })
    return out


def free_space(config: Config) -> int:
    """Bytes free on the drive holding the office's data directory."""
    return shutil.disk_usage(config.root).free


def _closed(conn, name: str) -> str | None:
    """Why `name` takes no run now, or None. Call with _lock held."""
    row = _row(conn, name)
    if row is None:
        return f"there is no stage '{name}'. roster() lists the stages."
    live = _live(name)
    if live.pending is not None:
        return (f"stage '{name}' is being {'reset' if live.pending == 'reset' else 'deleted'} "
                "and takes no runs until that is done.")
    if row["state"] == "preparing":
        prep = live.preparation
        now = time.monotonic()
        return (f"stage '{name}' is being prepared: for {_fmt(now - prep.started_at)}, last "
                f"output {_fmt(now - prep.last_output_at)} ago. It takes runs once it is ready.")
    if row["state"] == "broken":
        return (f"stage '{name}' is broken: {row['reason']}\nOnly the director can reset it, or "
                "delete it and create it again.")
    return None


# --------------------------------------------------------------------------- preparation


def _begin_preparation(conn, config: Config, name: str, command: str, fresh: bool) -> None:
    """Start a preparation on its own thread. Call with _lock held."""
    prep = _Preparation(name)
    _live(name).preparation = prep
    prep.thread = threading.Thread(
        target=_prepare, args=(conn, config, name, command, fresh, prep),
        daemon=True, name=f"office-stage-{name}",
    )
    prep.thread.start()


def _prepare(conn, config: Config, name: str, command: str, fresh: bool,
             prep: _Preparation) -> None:
    tree = config.stages_dir / name
    tail: deque[str] = deque(maxlen=REASON_LINES)
    log_path = config.stages_dir / f"{name}.prepare.log"
    log_lock = threading.Lock()
    log = open(log_path, "w", encoding="utf-8", errors="replace", newline="")

    def note(text: str) -> None:
        with log_lock:
            prep.last_output_at = time.monotonic()
            if not text or log.closed:
                return
            log.write(text if text.endswith("\n") else text + "\n")
            log.flush()

    state, reason = "ready", None
    main = f"refs/heads/{git.default_branch(config)}"
    try:
        if fresh:
            failure = _clone(config, tree, prep.mark, note)
        elif not tree.is_dir():
            failure = f"{tree} holds no clone."
        else:
            found = _git(["rev-parse", "--git-dir"], tree, prep.mark)
            failure = None if found.returncode == 0 else f"{tree} holds no clone: {_said(found)}"
        if failure is not None:
            state, reason = "broken", f"{failure}\n{_RECREATE}"
        else:
            # The configuration must be in place before the switch; the fill runs
            # after it.
            for step in (lambda: _configure(config, tree, prep.mark, note),
                         lambda: _switch(config, tree, main, None, prep.mark, note),
                         lambda: _fill(tree, prep.mark, note)):
                if failure is None and not prep.cancelled.is_set():
                    failure = step()
            if failure is not None:
                state, reason = "broken", f"{failure}\n{_RESET_AGAIN}"
            elif command and not prep.cancelled.is_set():
                code = _run_prepare(prep, command, tree, note, tail)
                if code != 0:
                    state = "broken"
                    reason = (f"prepare exited with code {code}. The last {len(tail)} lines of "
                              f"its output follow; all of it is in {log_path}.\n" + "".join(tail))
    except (OSError, ValueError) as exc:
        state, reason = "broken", f"prepare could not be started: {exc}"
    finally:
        marks.sweep(prep.mark, f"the preparation of stage {name}")
        with log_lock:
            log.close()
        # A cancelled preparation keeps its place: whoever cancelled it replaces it.
        with _lock:
            if not prep.cancelled.is_set():
                _live(name).preparation = None
                _set_state(conn, name, state, reason)


def _run_prepare(prep: _Preparation, command: str, tree: Path, note, tail: deque) -> int:
    """Run `prepare` in the tree; every line to `note` and `tail`. Its exit code.

    What it leaves running is swept by the preparation's mark before this returns.
    """
    proc = commands.popen_shell(command, tree, _child_env(None, "office", mark=prep.mark))
    prep.proc = proc

    def read() -> None:
        for line in iter(proc.stdout.readline, ""):
            tail.append(line if line.endswith("\n") else line + "\n")
            note(line)

    reader = threading.Thread(target=read, daemon=True, name=f"office-prepare-{prep.mark}")
    reader.start()
    code = proc.wait()
    marks.sweep(prep.mark, f"the preparation of {prep.mark}")
    reader.join(commands._READER_JOIN_SECONDS)
    return code


def _cancel(prep: _Preparation) -> bool:
    """Kill a preparation and wait for its thread, at most _KILL_WAIT_SECONDS.

    False if the thread is still alive then.
    """
    prep.cancelled.set()
    proc = prep.proc
    if proc is not None and proc.poll() is None:
        _kill_tree(proc.pid)
    deadline = time.monotonic() + _KILL_WAIT_SECONDS
    while prep.thread.is_alive() and time.monotonic() < deadline:
        marks.sweep(prep.mark, f"the preparation of {prep.mark}")
        prep.thread.join(1.0)
    return not prep.thread.is_alive()


_SURVIVED = ("its preparation did not end when it was killed; what survived is in the owner's "
             "journal, and only the owner can stop it.")


# --------------------------------------------------------------------------- management


def create(conn, config: Config, name: str, prepare: str, actor: str) -> None:
    """Write the row as `preparing` and start the stage's first preparation, the clone included."""
    if not NAME.fullmatch(name):
        raise ValueError(
            f"'{name}' is not a stage name: lowercase letters, digits and hyphens, starting "
            "with a letter or digit, at most 40 characters."
        )
    with _ops_lock, _lock:
        if _row(conn, name) is not None:
            raise ValueError(f"there is already a stage '{name}'. reset it, or pick another name.")
        with db.transaction(conn):
            db.execute(conn, "INSERT INTO stages (name, prepare, state) VALUES (?, ?, 'preparing')",
                       (name, prepare))
            core._emit(conn, "stages", "stage", None, {"name": name, "state": "preparing"},
                       actor=actor)
        _state[name] = _Live()
        _begin_preparation(conn, config, name, prepare, fresh=True)


def reset(conn, config: Config, name: str, actor: str) -> str:
    """Switch the stage to the main branch's tip and prepare it again, now or after its run."""
    with _ops_lock:
        with _lock:
            if _row(conn, name) is None:
                raise ValueError(f"there is no stage '{name}'. roster() lists the stages.")
            live = _live(name)
            if live.pending == "delete":
                raise ValueError(f"stage '{name}' is already waiting to be deleted.")
            if live.running is not None:
                return _make_pending(conn, name, live, "reset", actor)
            live.pending = "reset"
        _reset_now(conn, config, name, actor)
    return f"stage '{name}' is preparing again, from the main branch's tip."


def delete(conn, config: Config, name: str, actor: str) -> str:
    """Move the tree aside and remove the row and the refs, now or after its run."""
    with _ops_lock:
        with _lock:
            if _row(conn, name) is None:
                raise ValueError(f"there is no stage '{name}'. roster() lists the stages.")
            live = _live(name)
            if live.running is not None:
                return _make_pending(conn, name, live, "delete", actor)
            live.pending = "delete"
        _delete_now(conn, config, name, actor, after_run=False)
    return f"stage '{name}' deleted."


def _make_pending(conn, name: str, live: _Live, op: str, actor: str) -> str:
    """Leave `op` to the end of the run in progress. Call with _lock held."""
    live.pending = op
    turned_away = list(live.queue)
    live.queue.clear()
    word = "reset" if op == "reset" else "deleted"
    after = " Start it again once the stage is ready." if op == "reset" else ""
    for run in turned_away:
        run._turn_away(f"stage '{name}' is being {word}; this run did not start.{after}")
    _changed(conn, name, actor)
    answer = (f"stage '{name}' has a run of {live.running.agent}'s on it; it will be {word} when "
              "that run ends, and takes no new runs until then.")
    if turned_away:
        answer += (f" {len(turned_away)} waiting run(s) were turned away: "
                   f"{', '.join(r.agent for r in turned_away)}.")
    return answer


def _reset_now(conn, config: Config, name: str, actor: str | None) -> None:
    """Call with _ops_lock held, _lock not held, and the stage's `pending` set to reset.

    `pending` is cleared whether the reset happens or not.
    """
    with _lock:
        prep = _live(name).preparation
    if prep is not None and not _cancel(prep):
        with _lock:
            _live(name).pending = None
        raise ValueError(f"stage '{name}' was not reset: {_SURVIVED}")
    with _lock:
        live = _live(name)
        live.pending = None
        _set_state(conn, name, "preparing", None, actor)
        _begin_preparation(conn, config, name, _row(conn, name)["prepare"], fresh=False)


def _delete_now(conn, config: Config, name: str, actor: str | None, *, after_run: bool) -> None:
    """Call with _ops_lock held, _lock not held, and the stage's `pending` set to delete.

    `pending` is cleared when the delete does not happen. A rename that fails
    raises with the operating system's sentence. A delete that was left to the
    end of a run, or that killed a preparation on the way, leaves the stage
    broken with that sentence.
    """
    with _lock:
        prep = _live(name).preparation
    if prep is not None and not _cancel(prep):
        with _lock:
            _live(name).pending = None
        raise ValueError(f"stage '{name}' was not deleted: {_SURVIVED}")
    tree = config.stages_dir / name
    if tree.exists():
        trash = config.stages_dir / ".trash"
        trash.mkdir(exist_ok=True)
        n = 1
        while (trash / f"{name}-{n}").exists():
            n += 1
        aside = trash / f"{name}-{n}"
        try:
            os.rename(tree, aside)
        except OSError as exc:
            sentence = (f"stage '{name}' was not deleted: its tree could not be moved aside "
                        f"({exc.strerror or exc}). Delete it again once nothing holds a file "
                        "in it.")
            with _lock:
                live = _live(name)
                live.pending = None
                if prep is not None or after_run:
                    live.preparation = None
                    _set_state(conn, name, "broken", sentence, actor)
                else:
                    _changed(conn, name, actor)
            raise ValueError(sentence) from None
        threading.Thread(target=_remove, args=(aside,), daemon=True,
                         name=f"office-stage-trash-{name}").start()
    project = git.project_git(config)
    mark = marks.preparation_mark(name)
    for ref in _git(["for-each-ref", "--format=%(refname)", f"{_REF_PREFIX}{name}"], project,
                    mark, check=True).stdout.split():
        dropped = _git(["update-ref", "-d", ref], project, mark)
        if dropped.returncode != 0:
            _log.warning("could not delete %s: %s", ref, _said(dropped))
    (config.stages_dir / f"{name}.prepare.log").unlink(missing_ok=True)
    with _lock:
        _state.pop(name, None)
        with db.transaction(conn):
            db.execute(conn, "DELETE FROM stages WHERE name = ?", (name,))
            core._emit(conn, "stages", "stage", None, {"name": name, "deleted": True},
                       actor=actor)


def _remove(path: Path) -> None:
    try:
        git._rmtree(path)
    except OSError:
        _log.exception("could not remove %s", path)


def _advance(conn, config: Config, name: str) -> None:
    """After a run ends: the pending operation, or the next run in the queue."""
    if _closing.is_set():
        return
    with _ops_lock:
        with _lock:
            live = _live(name)
            live.running = None
            op = live.pending
            if op is None and live.queue:
                _take(live, live.queue.pop(0))
        try:
            if op == "reset":
                _reset_now(conn, config, name, None)
            elif op == "delete":
                _delete_now(conn, config, name, None, after_run=True)
        except ValueError as exc:
            _log.warning("%s", exc)
    if op is None:
        _changed(conn, name)


def _take(live: _Live, run: "StageRun") -> None:
    """Give the stage to `run` and start it. Call with _lock held."""
    live.running = run
    run.took_at = time.monotonic()
    threading.Thread(target=run._go, daemon=True, name=f"office-stage-{run.handle}").start()


# --------------------------------------------------------------------------- runs


class StageRun(commands.Command):
    """A command run on a stage for an agent: snapshot, queue, switch, command,
    clearing, changes.

    Registered like any command from the moment it is enrolled; running (and so
    counted) until it finishes.
    """

    def __init__(self, handle: str, agent: str, command: str, workspace: dict,
                 stage: str, conn, config: Config) -> None:
        super().__init__(handle, agent, command, Path(workspace["path"]))
        self.stage = stage
        self.workspace_id = workspace["id"]
        self.mark = marks.run_mark(agent, handle)
        self.took_at: float | None = None
        self.snapshot: str | None = None
        #: Why the command never ran: a refusal after the snapshot, a turn-away
        #: from the queue, or a step before the command that failed.
        self.failure: str | None = None
        self.change: str | None = None
        self.changed: list[str] | None = None
        self.lfs: list[str] = []
        self.artifacts = config.scratch_dir / self.workspace_id / "stage" / stage
        self.artifact_count: int | None = None
        self._conn = conn
        self._config = config
        self._stop = threading.Event()

    def _say(self, text: str) -> None:
        self._append(f"[office] {text}\n")

    def _touch(self, _line: str) -> None:
        """Output of a step before the command: it counts for quiet_for and goes nowhere."""
        with self._lock:
            self.last_output_at = time.monotonic()

    # -- ending ------------------------------------------------------------

    def _end(self, code: int | None) -> None:
        with _lock:
            _runs.discard(self)
        self._finish(code)

    def _turn_away(self, sentence: str) -> None:
        """Answer a waiting run that will not start. Call with _lock held."""
        self.failure = sentence
        self._say(sentence)
        _runs.discard(self)
        self._finish(None)

    def kill(self, by: str) -> None:
        """Take the run out of the queue, or stop whichever step is in progress and
        wait for its clearing, at most _KILL_WAIT_SECONDS. Idempotent."""
        with _lock:
            if self._finished.is_set():
                return
            self.killed_by = by
            self._stop.set()
            live = _state.get(self.stage)
            queued = live is not None and self in live.queue
            if queued:
                live.queue.remove(self)
                self._say(f"taken out of the queue ({by})")
                _runs.discard(self)
                self._finish(None)
        if queued:
            _changed(self._conn, self.stage)
            return
        proc = self._proc
        if proc is not None and proc.poll() is None:
            _kill_tree(proc.pid)
        deadline = time.monotonic() + _KILL_WAIT_SECONDS
        while time.monotonic() < deadline:
            marks.sweep(self.mark, f"{self.agent}'s {self.handle}")
            if self._finished.wait(1.0):
                return
        _log.error("%s: %s did not end within %.0fs of its kill", self.agent, self.handle,
                   _KILL_WAIT_SECONDS)

    # -- state -------------------------------------------------------------

    def waiting(self) -> str | None:
        """A sentence on where this run stands in its queue, or None when it is not waiting."""
        with _lock:
            live = _state.get(self.stage)
            if self._finished.is_set() or live is None or self not in live.queue:
                return None
            ahead = live.queue.index(self)
            head = live.running
            who = (f"{head.agent} has had the stage for "
                   f"{_fmt(time.monotonic() - head.took_at)}") if head else "nothing has it yet"
        return (f"{self.handle} is WAITING for stage '{self.stage}' after {self.elapsed:.0f}s: "
                f"{ahead} run(s) ahead of it in the queue; {who}.")

    # -- the run -----------------------------------------------------------

    def _go(self) -> None:
        """Steps 4 to 9, on this run's own thread."""
        config = self._config
        tree = config.stages_dir / self.stage
        code = None
        exited = False
        emptied = False
        _changed(self._conn, self.stage, self.agent)
        try:
            self._say(f"stage '{self.stage}' is this run's now; switching it to {self.snapshot}")
            failure = _switch(config, tree, _ref(self.stage, self.workspace_id), self.snapshot,
                              self.mark, self._touch)
            if failure is not None and not self._stop.is_set():
                self.failure = f"The stage could not be switched to the snapshot. {failure}"
            if self.failure is None and not self._stop.is_set():
                try:
                    if self.artifacts.exists():
                        git._rmtree(self.artifacts)
                    self.artifacts.mkdir(parents=True)
                    emptied = True
                except OSError as exc:
                    self.failure = (f"The artifacts folder {self.artifacts} could not be "
                                    f"emptied: {exc.strerror or exc}")
            if self.failure is None and not self._stop.is_set():
                env = _child_env({"OFFICE_ARTIFACTS": str(self.artifacts)}, self.agent,
                                 mark=self.mark)
                self._say(f"running in {tree}")
                try:
                    self._spawn(tree, env)
                except (OSError, ValueError) as exc:
                    self.failure = f"The command could not be started: {exc}"
                else:
                    code = self._wait_process()
                    exited = not self._stop.is_set()
        finally:
            self._clear(tree)
            if exited:
                self._harvest(tree)
            if emptied:
                self.artifact_count = sum(len(files) for _, _, files in os.walk(self.artifacts))
            self._end(code)
            _advance(self._conn, config, self.stage)

    def _clear(self, tree: Path) -> None:
        """Step 7: sweep by the run's mark, then remove the lock files git left."""
        marks.sweep(self.mark, f"{self.agent}'s {self.handle}")
        removed = _remove_git_locks(tree)
        if removed:
            self._say(f"removed lock files git left behind: {', '.join(removed)}")

    def _harvest(self, tree: Path) -> None:
        """Step 8: what the command changed in the tree, as commit C on S."""
        try:
            commit, written = _record_tree(tree, self.snapshot,
                                           f"stage {self.stage}: {self.handle}", self.mark)
            before = _git(["rev-parse", f"{self.snapshot}^{{tree}}"], tree, self.mark,
                          check=True).stdout.strip()
            if written == before:
                self.changed = []
                return
            _push(self._config, tree, commit, _ref(self.stage, self.workspace_id), self.mark)
            names = _git(["diff", "--name-only", "-z", self.snapshot, commit], tree, self.mark,
                         check=True).stdout.split("\0")
            paths = [p for p in names if p]
            attrs = _git(["check-attr", "-z", "--stdin", "filter"], tree, self.mark,
                         check=True, input="\0".join(paths) + "\0").stdout.split("\0")
            lfs = {attrs[i] for i in range(0, len(attrs) - 2, 3) if attrs[i + 2] == "lfs"}
            self.change = commit
            self.changed = paths
            self.lfs = [p for p in paths if p in lfs]
        except git.GitError as exc:
            if not self._stop.is_set():
                self.failure = f"The changes could not be collected: {exc.output.strip()}"

    def report(self) -> list[str]:
        """What a stage run adds to run's result."""
        lines = []
        if self.failure:
            lines.append(self.failure)
        if self.snapshot:
            lines.append(f"Snapshot of your working tree: {self.snapshot}.")
        if self.changed == []:
            lines.append("The command changed nothing in the stage's tree.")
        elif self.changed:
            lines.extend(self._changes())
        if self.artifact_count is not None:
            lines.append(f"Artifacts: {self.artifacts} ({self.artifact_count} file(s)).")
        return lines

    def _changes(self) -> list[str]:
        config = self._config
        other = [p for p in self.changed if p not in self.lfs]
        lines = [f"The command changed {len(self.changed)} file(s) in the stage's tree, now "
                 f"commit {self.change}."]
        if other:
            shown = ", ".join(other[:RESULT_PATHS])
            more = f" (first {RESULT_PATHS} of {len(other)})" if len(other) > RESULT_PATHS else ""
            lines.append(f"Changed{more}: {shown}.")
        if self.lfs:
            lines.append(f"Changed, tracked by LFS: {', '.join(self.lfs)}.")
        patch = (config.scratch_dir / self.workspace_id / "stage" / f"{self.stage}.patch").as_posix()
        steps = [
            f"git fetch --no-recurse-submodules {git._posix(git.project_git(config))} "
            f"{_ref(self.stage, self.workspace_id)}",
        ]
        if other:
            steps += [
                f'git diff --binary {self.snapshot} {self.change} --output={patch} -- . '
                f'":(exclude,attr:filter=lfs)"',
                f"git apply {patch}",
            ]
        if self.lfs:
            quoted = " ".join(f"'{p}'" for p in self.lfs)
            steps.append(
                f'git -c "filter.lfs.process=git-lfs filter-process" restore '
                f"--source={self.change} --worktree -- {quoted}"
            )
        lines.append("To bring them into your working copy, run in your workspace:")
        lines.extend(f"  {step}" for step in steps)
        if other:
            lines.append(
                "If git apply refuses a file you have changed since the snapshot, git add the "
                f"files the patch changes and run git apply --3way {patch} instead."
            )
        return lines


def start_run(conn, config: Config, agent: str, workspace: dict, stage: str,
              command: str) -> StageRun:
    """Check the stage, snapshot the workspace and queue a run. Returns it enrolled.

    Raises ValueError for a stage that takes no run, a second run of this
    workspace on it, or a snapshot git refused.
    """
    with _lock:
        closed = _closed(conn, stage)
        if closed:
            raise ValueError(closed)
        for other in _runs:
            if other.stage == stage and other.workspace_id == workspace["id"]:
                raise ValueError(
                    f"your workspace already has {other.handle} on stage '{stage}'. Wait on it "
                    f"with run(op=wait, handle='{other.handle}') or stop it with "
                    f"run(op=stop, handle='{other.handle}')."
                )
        run = commands.enroll(
            agent, lambda handle: StageRun(handle, agent, command, workspace, stage, conn, config))
        _runs.add(run)
    run._open_log(f"$ {command}\n")
    run._say(f"on stage '{stage}'; taking a snapshot of the working tree")
    try:
        run.snapshot = snapshot(config, run.workspace, stage, run.workspace_id, run.mark,
                                config.stages_dir / f".{run.handle}.index")
    except (git.GitError, OSError) as exc:
        run._end(None)
        if run._stop.is_set():
            return run
        commands.discard(run)
        detail = exc.output.strip() if isinstance(exc, git.GitError) else str(exc)
        raise ValueError(f"the snapshot of your working tree failed: {detail}") from None
    with _lock:
        if run._stop.is_set():
            _runs.discard(run)
            run._finish(None)
            return run
        closed = _closed(conn, stage)
        if closed:
            _runs.discard(run)
            run._finish(None)
            commands.discard(run)
            raise ValueError(closed)
        live = _live(stage)
        live.queue.append(run)
        run._say(f"snapshot {run.snapshot} queued")
        if live.running is None:
            _take(live, live.queue.pop(0))
    _changed(conn, stage, agent)
    return run


# --------------------------------------------------------------------------- hub life


def recover(conn, config: Config) -> None:
    """At hub startup, after the sweep: `preparing` stages are broken with _HUB_RESTART,
    lock files git left in every stage tree are removed, so are the snapshots' temporary
    indexes and their lock files, and `.trash` is emptied on a thread of its own."""
    for row in db.query(conn, "SELECT name FROM stages WHERE state = 'preparing'"):
        _set_state(conn, row["name"], "broken", _HUB_RESTART)
    for row in db.query(conn, "SELECT name FROM stages"):
        for path in _remove_git_locks(config.stages_dir / row["name"]):
            _log.info("stage %s: removed %s", row["name"], path)
    for index in config.stages_dir.glob(".*.index*"):
        index.unlink()
    trash = config.stages_dir / ".trash"
    if trash.is_dir():
        threading.Thread(target=lambda: [_remove(p) for p in trash.iterdir()], daemon=True,
                         name="office-stage-trash").start()


def shutdown() -> None:
    """Stop every preparation and every stage run. Nothing starts or is written afterwards."""
    _closing.set()
    with _lock:
        preparations = [live.preparation for live in _state.values() if live.preparation]
        runs = list(_runs)
    for prep in preparations:
        _cancel(prep)
    for run in runs:
        run.kill("hub shutdown")
        run.release()


atexit.register(shutdown)
