"""Git: the office's repositories, agent workspaces, merges and delivery."""

from __future__ import annotations

import logging
import os
import re
import shutil
import stat
import subprocess
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from office.config import Config

log = logging.getLogger(__name__)

#: Held by every writer of a branch in project.git that the office itself moves:
#: merge(), for the whole of build-deliver-record, and take_from_owner().
_merge_lock = threading.Lock()

#: Held by init_project for the whole of its work.
_setup_lock = threading.Lock()


class GitError(RuntimeError):
    def __init__(self, argv: list[str], returncode: int, output: str) -> None:
        self.argv = argv
        self.returncode = returncode
        self.output = output
        super().__init__(f"git {' '.join(argv)} -> {returncode}\n{output.strip()}")


@dataclass
class Intake:
    """What came back from the owner's repository.

    ``off`` nothing to take from — no repository recorded, or no branch to take
    into; ``nothing`` the office already had what came back;
    ``moved`` the office's branch was fast-forwarded onto his; ``diverged`` the
    two histories are not one line; ``failed`` the intake did not run to the end
    and the office's branch was not moved. ``detail`` is a sentence for whoever
    asked.
    """

    status: str  # off | nothing | moved | diverged | failed
    branch: str = ""
    commit: str = ""
    detail: str = ""


@dataclass
class Workspace:
    id: str
    path: str


@dataclass
class PublishResult:
    branch: str
    #: Submodules holding commits that are not reachable from their own
    #: remote-tracking refs.
    unpublished_submodules: list[str] = field(default_factory=list)


@dataclass
class Delivery:
    status: str  # off | delivered | refused
    detail: str = ""
    repo: str = ""
    branch: str = ""


@dataclass
class MergeResult:
    status: str  # merged | behind | up_to_date | blocked | diverged | missing
    commit: str = ""
    detail: str = ""
    delivery: Delivery | None = None
    intake: Intake | None = None
    #: Whether the source branch was asked for and is gone.
    source_deleted: bool = False

# --------------------------------------------------------------------------- process


def _env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    env["GIT_CLONE_PROTECTION_ACTIVE"] = "false"
    env["GIT_TERMINAL_PROMPT"] = "0"
    if extra:
        env.update(extra)
    return env


def git(
    args: list[str],
    *,
    cwd: Path | str | None = None,
    env_extra: dict[str, str] | None = None,
    check: bool = True,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run git without a shell."""
    argv = ["git", "-c", "protocol.file.allow=always", *args]
    log.debug("git %s (cwd=%s)", " ".join(args), cwd)
    proc = subprocess.run(
        argv,
        cwd=str(cwd) if cwd else None,
        env=_env(env_extra),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if check and proc.returncode != 0:
        raise GitError(args, proc.returncode, proc.stdout + proc.stderr)
    return proc


def _out(args: list[str], cwd: Path | str, **kw) -> str:
    return git(args, cwd=cwd, **kw).stdout.strip()


def _posix(path: Path) -> str:
    """Forward slashes, for anything that becomes a git address or config value."""
    return path.resolve().as_posix()


def _rmtree(path: Path) -> None:
    def _force(func, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        func(target)

    shutil.rmtree(path, onexc=_force)

# ------------------------------------------------------- the owner's repository

#: A minute is the cap on every call that addresses the owner's repository and
#: does not move objects.
OWNER_TIMEOUT = 60

#: Returned by _owner_git in place of an exit status when nothing answered.
_NO_ANSWER = 124

#: A path that is never created. GIT_CONFIG_GLOBAL and GIT_CONFIG_SYSTEM point
#: at it on every call towards the owner's repository, so those calls run
#: without the user's and the machine's git configuration.
_NO_CONFIG = str(Path(__file__).resolve().parent / "no-such-git-config")

#: Set alongside it. Nothing on this path may wait for a person.
_OWNER_ENV = {
    "GIT_CONFIG_GLOBAL": _NO_CONFIG,
    "GIT_CONFIG_SYSTEM": _NO_CONFIG,
    "GIT_ASKPASS": "office-asks-for-no-credentials",
    "SSH_ASKPASS": "office-asks-for-no-credentials",
    "GCM_INTERACTIVE": "Never",
}


def _owner_git(
    args: list[str], cwd: Path | str, *, timeout: int | None = OWNER_TIMEOUT
) -> tuple[int, str]:
    """Run git for an operation that touches the owner's repository, without raising.

    Callers turn the answer into a sentence; nothing on this path may reach
    anybody as a traceback. Pass ``timeout=None`` only where the call moves
    objects into a path that has already answered.
    """
    try:
        proc = git(args, cwd=cwd, check=False, env_extra=_OWNER_ENV, timeout=timeout)
    except subprocess.TimeoutExpired:
        return _NO_ANSWER, f"no answer within {timeout} seconds"
    return proc.returncode, (proc.stdout + proc.stderr).strip()

_CHECKED_OUT = re.compile(r"refusing to update checked out branch", re.IGNORECASE)
_UNSTAGED = re.compile(r"Working directory has unstaged changes", re.IGNORECASE)
_STAGED = re.compile(r"has staged changes", re.IGNORECASE)
_TREE_UPDATE = re.compile(r"Could not update working tree to new HEAD", re.IGNORECASE)
_NON_FAST_FORWARD = re.compile(
    r"non-fast-forward|fetch first|stale info|Up-to-date check failed", re.IGNORECASE
)

#: Ends every refusal that leaves work behind.
_LATER = (
    " The work is complete in the office; the merge can be repeated once this is cleared, "
    "and it then carries everything that piled up meanwhile."
)


def _refusal(repo: str, branch: str, rc: int, output: str) -> str:
    """One sentence for a delivery git would not take, for the owner to act on."""
    if rc == _NO_ANSWER:
        return (
            f"{repo} did not answer within {OWNER_TIMEOUT} seconds, so nothing was sent."
            + _LATER
        )
    if _CHECKED_OUT.search(output):
        return (
            f"{repo} is not set up to take delivered work yet, so git refused the push. Press "
            "'Set up delivery' on the office's settings page and the office will set the one "
            "git setting that allows it." + _LATER
        )
    if _UNSTAGED.search(output):
        return (
            f"There are uncommitted changes in {repo}, so git refused the whole push and left "
            "your files exactly as they are. Commit them or put them aside with `git stash`."
            + _LATER
        )
    if _STAGED.search(output):
        return (
            f"There are staged changes in {repo}, so git refused the whole push and left your "
            "files exactly as they are. Commit them or put them aside with `git stash`."
            + _LATER
        )
    if _TREE_UPDATE.search(output):
        return (
            f"git could not update the files in {repo}, so the branch was not moved. The usual "
            "cause is a file this work brings already lying there untracked: git will not write "
            f"over it. Look for it in {repo} and move or delete it." + _LATER
        )
    if _NON_FAST_FORWARD.search(output):
        return (
            f"'{branch}' in {repo} has commits the office does not have, so the branch was not "
            "moved — git never writes over them. Bring them into the project yourself, or move "
            "that branch aside." + _LATER
        )
    return f"Could not send to {repo}: {output}{_LATER}"


def owner_repo(config: Config) -> Path:
    """The owner's repository: the directory the office's data directory is in.

    The address of every delivery and every intake. Derived and never recorded.
    """
    return config.root.parent


def configure_owner_repo(config: Config) -> tuple[str, str]:
    """Make the owner's repository able to receive delivered work.

    Two settings, in his repository. ``init_project`` calls it; the button on
    the settings page calls it again. Returns (status, detail): 'set' when the
    key was written, 'refused' otherwise, and the detail is a sentence for the
    page that carries the button.

    ``receive.denyCurrentBranch=updateInstead`` is what writes a delivered
    branch into the files and not only into the history.

    The LFS filters go in beside it, in the repository's own config. Where
    git-lfs is not on this machine the command fails and nothing is written.
    """
    repo = str(owner_repo(config))
    rc, output = _owner_git(
        ["config", "--local", "receive.denyCurrentBranch", "updateInstead"], cwd=repo
    )
    if rc != 0:
        return "refused", f"Could not write the setting in {repo}: {output}"
    _owner_git(["lfs", "install", "--local"], cwd=repo)
    return "set", (
        f"Done. When the office sends work to {repo}, git writes the new files into that folder "
        "as well as the history, as long as nothing there is uncommitted. To undo it, run "
        "`git config --unset receive.denyCurrentBranch` in that folder."
    )


def owner_takes_delivery(config: Config) -> bool:
    """Whether the owner's repository will write a delivered branch into its files.

    ``receive.denyCurrentBranch`` read back out of that repository. Spawns
    git: call it from a worker thread.
    """
    repo = owner_repo(config)
    rc, output = _owner_git(
        ["config", "--local", "--get", "receive.denyCurrentBranch"], cwd=repo
    )
    return rc == 0 and output.strip().lower() == "updateinstead"

#: Where the owner's branch lands in project.git. Outside refs/heads/, so it is
#: not a branch anybody can clone, publish to or merge.
_OWNER_REF = "refs/office/owner"


def take_from_owner(config: Config, *, branch: str) -> Intake:
    """Fetch the owner's branch and fast-forward the office's onto it.

    The office's branch moves only when his contains it. Two histories that
    have each moved on are 'diverged' and left alone.

    Takes the merge lock, so it must not be called from inside merge() — that
    one calls _take_from_owner directly.
    """
    with _merge_lock:
        return _take_from_owner(config, branch=branch)


def _take_from_owner(config: Config, *, branch: str) -> Intake:
    """take_from_owner's body. The caller must hold the merge lock.

    Never raises: every outcome is a status and a sentence.
    """
    repo = str(owner_repo(config))
    if not branch:
        return Intake("off", detail="The office has no branch to take anything into yet.")

    project = project_git(config)
    rc, git_dir = _owner_git(["rev-parse", "--absolute-git-dir"], cwd=repo)
    if rc != 0:
        return Intake(
            "off",
            branch=branch,
            detail=f"{repo} is not a git repository, so there is nothing to take in: {git_dir}",
        )

    # Uncapped, here and at the fetch of the large files below.
    rc, output = _owner_git(
        ["fetch", "--no-tags", _posix(Path(repo)), f"+refs/heads/{branch}:{_OWNER_REF}"],
        cwd=project,
        timeout=None,
    )
    if rc != 0:
        return Intake(
            "failed", branch=branch, detail=f"Could not read '{branch}' from {repo}: {output}"
        )

    incoming = _out(["rev-parse", "--verify", _OWNER_REF], project, check=False)
    mine = _out(["rev-parse", "--verify", f"refs/heads/{branch}"], project, check=False)
    if incoming == mine:
        return Intake("nothing", branch=branch, commit=mine, detail=_nothing_new(repo, branch))
    if git(["merge-base", "--is-ancestor", mine, incoming],
           cwd=project, check=False).returncode == 0:
        # Only here, where the branch is about to move.
        rc, output = _owner_git(
            ["lfs", "fetch", f"file:///{_posix(Path(git_dir))}", _OWNER_REF],
            cwd=project,
            timeout=None,
        )
        if rc != 0:
            # The branch is not moved onto a commit whose large files are
            # missing.
            return Intake(
                "failed",
                branch=branch,
                detail=f"The large files behind '{branch}' did not come across from {repo}, so "
                       f"nothing was taken in: {output}",
            )
        # The old value is given, so a branch that moved between the read above
        # and this write is not written over.
        moved = git(["update-ref", f"refs/heads/{branch}", incoming, mine],
                    cwd=project, check=False)
        if moved.returncode != 0:
            return Intake(
                "failed",
                branch=branch,
                detail=f"'{branch}' could not be moved onto the work from {repo}: "
                       f"{(moved.stdout + moved.stderr).strip()}",
            )
        return Intake(
            "moved",
            branch=branch,
            commit=incoming,
            detail=f"'{branch}' has moved to {incoming[:10]} — the office has taken in the work "
                   f"that was in {repo}.",
        )
    if git(["merge-base", "--is-ancestor", incoming, mine],
           cwd=project, check=False).returncode == 0:
        return Intake("nothing", branch=branch, commit=mine, detail=_nothing_new(repo, branch))
    return Intake(
        "diverged",
        branch=branch,
        commit=mine,
        detail=(
            f"'{branch}' in {repo} and '{branch}' in the office have each moved on from a commit "
            "they once shared, so neither contains the other: the history in your own repository "
            "has been rewritten. The office does not repair this — put the two back on one line "
            f"in {repo}, and nothing here moves until you do."
        ),
    )


def _nothing_new(repo: str, branch: str) -> str:
    return f"Nothing new in {repo}: the office already has everything on '{branch}'."

# --------------------------------------------------------------------------- layout


def project_git(config: Config) -> Path:
    return config.repo_dir / "project.git"


def _branch_names(git_dir: Path) -> list[str]:
    """Branch names in the repository whose git directory this is.

    Read off the filesystem rather than with git. Safe to call from the event
    loop. Both ref stores count: the files under refs/heads/ and the lines of
    packed-refs.
    """
    if not (git_dir / "HEAD").exists():
        return []
    found: set[str] = set()
    heads = git_dir / "refs" / "heads"
    if heads.is_dir():
        for ref in heads.rglob("*"):
            if ref.is_file():
                found.add(ref.relative_to(heads).as_posix())
    packed = git_dir / "packed-refs"
    if packed.is_file():
        try:
            for line in packed.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith(("#", "^")):
                    continue
                _, _, ref = line.partition(" ")
                ref = ref.strip()
                if ref.startswith("refs/heads/"):
                    found.add(ref[len("refs/heads/"):])
        except OSError:
            pass
    return sorted(found)


def branches(config: Config) -> list[str]:
    """Branch names in project.git; empty for a bare repo with no commits."""
    return _branch_names(project_git(config))


def owner_state(config: Config) -> str:
    """What is at the owner's repository, for the setup form and for health:

      none   no git repository there
      empty  a repository holding no branch
      repo   a repository with a branch

    By files only, no subprocess: this is read from the event loop. A `.git`
    that is a file names a git directory elsewhere and is not followed; such a
    repository counts as one with a branch.
    """
    git_dir = owner_repo(config) / ".git"
    if not git_dir.exists():
        return "none"
    if not git_dir.is_dir():
        return "repo"
    return "repo" if _branch_names(git_dir) else "empty"


def project_state(config: Config) -> str:
    """Where the office's repository layout has got to:

      absent  no repo/project.git
      empty   the bare repo holds no branch, or there is no donor to clone from
      ready   a branch and a donor, so a workspace can be cloned

    By files only, no subprocess: this gates web routes, async ones included.
    """
    if not (project_git(config) / "HEAD").exists():
        return "absent"
    if not branches(config):
        return "empty"
    if not (config.seed_dir / ".git").exists():
        return "empty"
    return "ready"


def default_branch(config: Config) -> str:
    """The branch project.git's own HEAD names, or ''.

    The office keeps no record of which branch is the main line; project.git's
    HEAD is that statement and this reads it. The name is checked against the
    refs: a name no ref carries is not an answer, and no other branch stands in
    for it.
    """
    head = project_git(config) / "HEAD"
    if not head.is_file():
        return ""
    text = head.read_text(encoding="utf-8", errors="replace").strip()
    prefix = "ref: refs/heads/"
    if not text.startswith(prefix):
        return ""
    name = text[len(prefix):].strip()
    return name if name in branches(config) else ""

# --------------------------------------------------------------------------- setup


def _lfs_push(source: Path, destination: Path, ref: str = "") -> tuple[int, str]:
    """Send LFS objects from one repository into another over a file:// address.

    With a ref, only what that revision needs; without one, everything.
    """
    url = f"file:///{_posix(destination)}"
    args = ["lfs", "push", url, ref] if ref else ["lfs", "push", "--all", url]
    proc = git(args, cwd=source, check=False)
    output = (proc.stdout + proc.stderr).strip()
    if proc.returncode != 0:
        log.warning("lfs push %s -> %s: %s", source, destination, output)
    return proc.returncode, output


def _first_commit(config: Config, project: Path, branch: str, readme: str) -> None:
    """Give a new empty bare repo one commit.

    The commit holds one file, README.md, and its text is the caller's: the
    office writes no words of its own into the project. Built in a throwaway
    tree, removed again here. Identity is passed per command and never read from
    configuration.
    """
    tree = config.repo_dir / ".bootstrap"
    if tree.exists():
        _rmtree(tree)
    tree.mkdir(parents=True)
    try:
        git(["init", f"--initial-branch={branch}", str(tree)])
        # One line ending, LF, whatever the text arrived with and whatever
        # platform this is: the file goes straight into a commit.
        text = readme.replace("\r\n", "\n").replace("\r", "\n")
        (tree / "README.md").write_text(
            text if text.endswith("\n") else text + "\n", encoding="utf-8", newline="\n"
        )
        git(["add", "README.md"], cwd=tree)
        git(
            ["-c", "user.name=office", "-c", "user.email=office@office.local",
             "commit", "-m", "Initial commit"],
            cwd=tree,
        )
        git(["push", _posix(project), f"HEAD:refs/heads/{branch}"], cwd=tree)
    finally:
        _rmtree(tree)


def _protect_donor(seed: Path) -> None:
    """The donor must never prune."""
    git(["config", "gc.auto", "0"], cwd=seed)
    git(["config", "gc.pruneExpire", "never"], cwd=seed)
    git(
        ["submodule", "foreach", "--recursive",
         "git config gc.auto 0 && git config gc.pruneExpire never"],
        cwd=seed,
        check=False,
    )


def _build_donor(config: Config) -> Path:
    """(Re)create the donor clone.

    A normal clone and never a bare one.
    """
    seed = config.seed_dir
    _rmtree(seed)
    git(
        ["clone", "--recurse-submodules", _posix(project_git(config)), str(seed)],
        env_extra={"GIT_LFS_SKIP_SMUDGE": "1"},
    )
    git(["config", "lfs.storage", _posix(config.lfs_dir)], cwd=seed)
    _protect_donor(seed)
    return seed

#: The cap on a donor refresh.
DONOR_TIMEOUT = 300


def _refresh_donor(config: Config) -> None:
    """Bring the donor up to date, before anything is cloned from it.

    Never raises and never fails its caller.
    """
    seed = config.seed_dir
    try:
        git(
            ["fetch", "--recurse-submodules", "--prune", "origin"],
            cwd=seed,
            check=False,
            timeout=DONOR_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        log.warning("donor refresh gave up after %s seconds", DONOR_TIMEOUT)

#: Refuses every push to the branch project.git's own HEAD names. The name is
#: read at push time rather than written in, so it follows HEAD.
_PRE_RECEIVE = """#!/bin/sh
main=$(git symbolic-ref --quiet HEAD) || exit 0
status=0
while read -r _old _new ref
do
\tif [ "$ref" = "$main" ]
\tthen
\t\techo "'${main#refs/heads/}' is the office's main branch, and only a merge writes to it. Put this work on a branch of your own (git switch -c <name>), push that, and open a pull request into it." >&2
\t\tstatus=1
\tfi
done
exit $status
"""


def _install_pre_receive(project: Path) -> None:
    """Put the hook that refuses pushes to the delivery target into project.git.

    It is what stands between any push — publish()'s and a hand-typed one alike
    — and the branch merge() delivers.
    """
    hooks = project / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    path = hooks / "pre-receive"
    path.write_text(_PRE_RECEIVE, encoding="utf-8", newline="\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _exclude_office_data(config: Config) -> None:
    """Keep the office's data directory out of the owner's working tree."""
    owner = owner_repo(config)
    _, git_dir = _owner_git(["rev-parse", "--absolute-git-dir"], cwd=owner)
    _append_exclude(Path(git_dir), (f"{config.root.name}/",))


def _point_head(project: Path, branch: str) -> None:
    """Make ``branch`` the branch project.git's HEAD names.

    The branch is created at the commit HEAD is on when there is no branch of
    that name. For a repository this module has just created and for no other.
    """
    ref = f"refs/heads/{branch}"
    if git(["rev-parse", "--verify", ref], cwd=project, check=False).returncode != 0:
        git(["update-ref", ref, _out(["rev-parse", "--verify", "HEAD"], project)], cwd=project)
    git(["symbolic-ref", "HEAD", ref], cwd=project)


def init_project(config: Config, *, branch: str, readme: str = "") -> dict[str, object]:
    """Create repo/project.git and the donor, out of whatever is at the owner's
    repository, and make that repository able to take delivered work.

    Which of three things it does is read off that directory, not asked: a
    repository with commits is cloned into project.git and the copy's remote
    dropped, keeping history, tags, submodules and LFS objects; a repository
    with no commits gets an empty bare and a first commit; a directory that is
    no repository yet is made one first and then treated the same.

    ``branch`` becomes the branch project.git's HEAD names in every case, and
    is created at the source's HEAD when the clone brought no branch of that
    name. ``readme`` is the text of the first commit's only file, and is
    required wherever that commit is made.

    Refuses if project.git already exists, and refuses at once while another
    caller is inside it; the check and the writing are held under one lock. A
    failure anywhere after the check removes project.git and the donor.
    """
    if not _setup_lock.acquire(blocking=False):
        raise GitError(
            ["init_project"], 1,
            "a project is already being set up; wait for it to finish.",
        )
    try:
        config.ensure_layout()
        project = project_git(config)
        if (project / "HEAD").exists():
            raise GitError(
                ["init_project"], 1,
                f"The office already has its project repository at {project}, so nothing was "
                "changed. To set the project up again, delete that folder and press this again.",
            )
        if git(["check-ref-format", "--branch", branch], check=False).returncode != 0:
            raise GitError(
                ["init_project"], 1, f"'{branch}' is not a name git will take for a branch."
            )
        owner = owner_repo(config)
        # Which of the three cases this is, and everything refused about it,
        # before anything is written into the owner's folder.
        owner_has_git = (owner / ".git").exists()
        owner_has_history = (
            owner_has_git and _owner_git(["rev-parse", "--verify", "HEAD"], cwd=owner)[0] == 0
        )
        if not owner_has_history and not readme.strip():
            raise GitError(
                ["init_project"], 1,
                f"There is no history in {owner} yet, so the office starts it — and the "
                "first commit needs a README. Write one and press this again.",
            )
        try:
            if not owner_has_git:
                git(["init", f"--initial-branch={branch}", str(owner)], env_extra=_OWNER_ENV)
            _exclude_office_data(config)
            status, detail = configure_owner_repo(config)
            if status != "set":
                raise GitError(["init_project"], 1, detail)
            if owner_has_history:
                git(["clone", "--bare", _posix(owner), str(project)])
                git(["remote", "remove", "origin"], cwd=project, check=False)
                rc, output = _lfs_push(owner, project)
                if rc != 0:
                    raise GitError(
                        ["init_project"], rc,
                        f"The history of {owner} came across, but its large files did not, so "
                        f"the import was undone: {output}",
                    )
                _point_head(project, branch)
            else:
                git(["init", "--bare", f"--initial-branch={branch}", str(project)])
                _first_commit(config, project, branch, readme)
            _install_pre_receive(project)
            seed = _build_donor(config)
        except BaseException:
            for leftover in (project, config.seed_dir):
                # Either may not have been reached before the failure.
                if leftover.exists():
                    _rmtree(leftover)
            raise
    finally:
        _setup_lock.release()

    return {"project": str(project), "seed": str(seed)}

# --------------------------------------------------------------------------- workspaces

_LFS_SKIP = {
    "filter.lfs.smudge": "git-lfs smudge --skip -- %f",
    "filter.lfs.process": "git-lfs filter-process --skip",
}

#: Kept out of the project's diff in every workspace: the two directories the
#: office and the runtimes it starts write into a workspace.
_OFFICE_EXCLUDES = (".office/", ".agents/")

#: Every beginning of an address by which a push could leave this machine.
_OUTBOUND = ("https://", "http://", "ssh://", "git://", "git@")

#: What such an address is rewritten to: an absolute path that is never created,
#: so a push onto it reaches nothing. Must contain no dot.
_NOWHERE = "/office/submodules-are-read-only/"


def block_outbound_push(env: dict[str, str]) -> None:
    """Add git configuration to `env` that rewrites every outbound push address
    to an unreachable path.

    For the environment of an agent's process and for no other: it applies to
    every git invocation of a process that carries it and of that process's
    children, in whatever repository the invocation runs in, including
    submodules that do not exist yet. Only pushing is rewritten. Configuration
    already in GIT_CONFIG_COUNT is kept.
    """
    start = max(0, int(env.get("GIT_CONFIG_COUNT") or 0))
    for offset, prefix in enumerate(_OUTBOUND):
        index = start + offset
        env[f"GIT_CONFIG_KEY_{index}"] = f"url.{_NOWHERE}.pushInsteadOf"
        env[f"GIT_CONFIG_VALUE_{index}"] = prefix
    env["GIT_CONFIG_COUNT"] = str(start + len(_OUTBOUND))

#: One line of `git submodule status`: a status character, the object id, a
#: space, the path, and an optional ` (describe)` suffix. The path is taken
#: whole, spaces included.
_SUBMODULE_LINE = re.compile(r"^[ +\-U][0-9a-f]+ (?P<path>.+?)(?: \([^()]*\))?$")


def _submodules(repo: Path) -> list[str]:
    """Submodule paths relative to the superproject, recursively."""
    proc = git(["submodule", "status", "--recursive"], cwd=repo, check=False)
    paths = []
    for line in proc.stdout.splitlines():
        match = _SUBMODULE_LINE.match(line.rstrip("\r\n"))
        if match:
            paths.append(match.group("path"))
    return paths


def _append_exclude(git_dir: Path, lines: tuple[str, ...]) -> None:
    """Add lines to info/exclude of this git directory, skipping ones already in it.

    Never .gitignore.
    """
    path = git_dir / "info" / "exclude"
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    missing = [line for line in lines if line not in existing.splitlines()]
    if not missing:
        return
    separator = "" if existing == "" or existing.endswith("\n") else "\n"
    path.write_text(
        existing + separator + "".join(f"{line}\n" for line in missing), encoding="utf-8"
    )


def _exclude_office(repo: Path) -> None:
    """Write _OFFICE_EXCLUDES into .git/info/exclude, here and in each submodule."""
    found = [_out(["rev-parse", "--absolute-git-dir"], repo)]
    for sub_path in _submodules(repo):
        sub = repo / sub_path
        if (sub / ".git").exists():
            found.append(_out(["rev-parse", "--absolute-git-dir"], sub, check=False))

    for answer in found:
        # An empty answer names no directory.
        if answer:
            _append_exclude(Path(answer), _OFFICE_EXCLUDES)


def _set_identity(repo: Path, agent: str) -> None:
    git(["config", "user.name", agent], cwd=repo)
    git(["config", "user.email", f"{agent}@office.local"], cwd=repo)


def create_workspace(
    config: Config,
    owner: str,
    *,
    on_intake: Callable[[Intake], None] | None = None,
) -> Workspace:
    """Clone a workspace, borrowing objects from the donor.

    The directory is keyed by the workspace id and never by the agent: a
    transfer must not move it on disk.

    No branch argument and no checkout: the clone lands on whatever
    project.git calls its default branch, and from then on the branch is the
    agent's business.

    The owner's branch is taken in first and the donor refreshed second.
    ``on_intake`` is handed the intake the moment it is taken, ahead of
    everything here that can raise.
    """
    ws_id = f"ws-{uuid.uuid4().hex[:12]}"
    path = config.ws_dir / ws_id

    intake = take_from_owner(config, branch=default_branch(config))
    if intake.status in ("diverged", "failed"):
        log.warning("intake before %s: %s", ws_id, intake.detail)
    if on_intake is not None:
        on_intake(intake)
    _refresh_donor(config)

    # --dissociate is never passed: the workspace must go on borrowing.
    args = [
        "clone", "--recurse-submodules",
        "--reference-if-able", _posix(config.seed_dir / ".git"),
        "-c", "submodule.alternateLocation=superproject",
        _posix(project_git(config)), str(path),
    ]
    git(args, env_extra={"GIT_LFS_SKIP_SMUDGE": "1"})

    endpoint = f"file:///{_posix(project_git(config))}"
    settings = {
        "submodule.recurse": "true",
        "fetch.recurseSubmodules": "on-demand",
        "diff.submodule": "log",
        "status.submoduleSummary": "true",
        "lfs.storage": _posix(config.lfs_dir),
        "protocol.file.allow": "always",
        # Both keys, and the address is written out rather than named by a
        # remote.
        "lfs.url": endpoint,
        "lfs.pushurl": endpoint,
        # A workspace holds pointers; an agent that needs the contents of an
        # asset runs `git lfs pull -I <path>` itself.
        **_LFS_SKIP,
    }
    for key, value in settings.items():
        git(["config", key, value], cwd=path)
    _set_identity(path, owner)

    # Every value quoted.
    sub_cmd = " && ".join(
        [f'git config lfs.storage "{_posix(config.lfs_dir)}"']
        + [f'git config {k} "{v}"' for k, v in _LFS_SKIP.items()]
    )
    git(["submodule", "foreach", "--recursive", sub_cmd], cwd=path, check=False)
    _exclude_office(path)
    # The sandbox is a sibling of the clone, not a directory inside it.
    (config.scratch_dir / ws_id).mkdir(parents=True, exist_ok=True)
    return Workspace(ws_id, str(path))


def transfer_workspace(path: Path | str, to_agent: str) -> None:
    """Hand a workspace to another agent.

    Configuration only: the working tree is left exactly as the previous owner
    left it, uncommitted changes and current branch alike. Its path does not
    change.
    """
    _set_identity(Path(path), to_agent)

# --------------------------------------------------------------------------- publish


def publish(config: Config, path: Path | str) -> PublishResult:
    """Push a workspace's branch to project.git.

    The branch is not an argument: it is whatever the working tree is standing
    on, and that tree is the only record of it anywhere. It comes back in the
    result.

    Nothing is pushed inside a submodule. A submodule holding commits that are
    not reachable from its own remote-tracking refs is named in the result.

    Refuses a detached HEAD. A push to the branch project.git's HEAD names is
    refused by project.git's own pre-receive hook (_install_pre_receive).
    """
    repo = Path(path)
    branch = _out(["rev-parse", "--abbrev-ref", "HEAD"], repo)
    if branch == "HEAD":
        raise GitError(
            ["publish"], 1,
            "this workspace is on a detached HEAD, so there is no branch name to publish "
            "under. Create a branch (git switch -c <name>) and publish again.",
        )
    result = PublishResult(branch)

    for sub_path in _submodules(repo):
        stranded = _out(
            ["rev-list", "-n", "1", "HEAD", "--not", "--remotes"], repo / sub_path, check=False
        )
        if stranded:
            result.unpublished_submodules.append(sub_path)

    # --recurse-submodules=no: nothing inside a submodule is pushed anywhere,
    # and that decision is made here rather than left to the workspace's config.
    args = ["push", "--recurse-submodules=no", "origin", f"HEAD:refs/heads/{branch}"]
    pushed = git(args, cwd=repo, check=False)
    if pushed.returncode != 0:
        output = (pushed.stdout + pushed.stderr).strip()
        raise GitError(args, pushed.returncode, f"{_publish_refusal(branch, output)}\n{output}")
    rc, output = _lfs_push(repo, project_git(config), branch)
    if rc != 0:
        raise GitError(
            ["publish"], rc,
            f"branch '{branch}' reached the office repository, but the large files it points at "
            f"did not leave {repo}, so nothing can be merged from it yet: {output}",
        )
    return result


def _publish_refusal(branch: str, output: str) -> str:
    """One sentence in front of git's own text, for the agent that published."""
    if "[rejected]" in output and _NON_FAST_FORWARD.search(output):
        return (
            f"branch '{branch}' in the office repository has moved ahead — somebody else is "
            "writing to it. Fetch it and reconcile (merge or rebase) before publishing again, "
            "or publish under a branch name of your own."
        )
    return f"could not publish branch '{branch}' to the office repository."

# --------------------------------------------------------------------------- merge

#: The identity of the merge commit, handed to commit-tree in its environment.
_OFFICE_IDENTITY = {
    "GIT_AUTHOR_NAME": "office",
    "GIT_AUTHOR_EMAIL": "office@office.local",
    "GIT_COMMITTER_NAME": "office",
    "GIT_COMMITTER_EMAIL": "office@office.local",
}


def _owner_conversion(repo: str) -> list[str]:
    """`core.autocrlf` as the owner's own git resolves it, as a push argument.

    Read with his configuration visible, and empty where he has set none. The
    receiving side compares his working tree against his index under it.
    """
    value = git(["config", "--get", "core.autocrlf"], cwd=repo, check=False).stdout.strip()
    if not value:
        return []
    return [f"--receive-pack=git -c core.autocrlf={value} receive-pack"]


def _deliver(config: Config, *, branch: str, commit: str, source: str) -> Delivery:
    """Put one commit into the owner's repository, on his own branch.

    The destination is a filesystem path on this machine and never a remote
    name. The commit is on no branch here, so it is named by its oid.

    The objects the branch keeps outside its history go first, and the branch
    does not go at all if they do not arrive. ``source`` is the branch whose
    tree the commit carries, so it is the revision those objects belong to.
    """
    repo = str(owner_repo(config))
    project = project_git(config)
    rc, git_dir = _owner_git(["rev-parse", "--absolute-git-dir"], cwd=repo)
    if rc != 0:
        return Delivery("refused", detail=_refusal(repo, branch, rc, git_dir),
                        repo=repo, branch=branch)

    # The two transfers below are uncapped.
    url = f"file:///{_posix(Path(git_dir))}"
    rc, output = _owner_git(
        ["lfs", "push", url, f"refs/heads/{source}"], cwd=project, timeout=None
    )
    if rc != 0:
        return Delivery(
            "refused",
            detail=(
                f"The large files this work needs did not reach {repo}, so the branch was not "
                f"sent: {output}{_LATER}"
            ),
            repo=repo,
            branch=branch,
        )

    rc, output = _owner_git(
        ["push", "--recurse-submodules=no", "--no-follow-tags",
         *_owner_conversion(repo),
         _posix(Path(repo)), f"{commit}:refs/heads/{branch}"],
        cwd=project,
        timeout=None,
    )
    if rc != 0:
        log.warning("delivery to %s refused: %s", repo, output)
        return Delivery("refused", detail=_refusal(repo, branch, rc, output),
                        repo=repo, branch=branch)
    return Delivery("delivered", repo=repo, branch=branch)


def _drop_branch(project: Path, source_branch: str, source: str) -> tuple[bool, str]:
    """Take the source branch's name out of project.git. (deleted, sentence).

    Called only where every commit of the source is in the target, so the name
    is all that goes. The old value is passed to update-ref, so the delete is
    refused if anything moved the branch since the caller read it. A delete
    that does not happen is a sentence for whoever asked, never a raise: what
    the caller did before this stands.
    """
    dropped = git(
        ["update-ref", "-d", f"refs/heads/{source_branch}", source], cwd=project, check=False
    )
    if dropped.returncode == 0:
        return True, ""
    sentence = (
        f"git refused to delete '{source_branch}': {(dropped.stdout + dropped.stderr).strip()}."
    )
    log.warning("%s", sentence)
    return False, sentence


def merge(
    config: Config,
    source_branch: str,
    target_branch: str,
    *,
    message: str | None = None,
    delete_source: bool = False,
) -> MergeResult:
    """Take a published branch into another, in project.git and without a tree.

    The owner's own branch is taken in first. Histories that have diverged from
    his are 'diverged' and move nothing.

    Only a branch that already contains the target is taken, so the result's
    tree is exactly the source's tree. A target the source does not contain is
    'behind'. A source the target already contains is 'up_to_date'.

    The order is build, deliver, then record: 'merged' means the owner has it.
    A delivery refusal is 'blocked' and moves nothing. Only the office's main
    branch is delivered and only it is taken in; a merge into any other is
    internal.

    The last step, moving the office's own branch, does not raise: the result
    is 'merged' with a detail naming what git said.

    The result carries the intake on every outcome, including the ones that
    merge nothing.

    ``delete_source`` removes refs/heads/<source_branch> on the two outcomes
    that answer the request and at no other point: after the target branch has
    moved onto the merge commit, and on the 'up_to_date' the ancestor check
    reaches. A delete that does not happen is said in the detail; what the
    merge did stands either way.

    A branch that is not in project.git is 'missing' rather than a raise. So is
    a project.git whose HEAD names no branch that exists.
    """
    project = project_git(config)
    target_ref = f"refs/heads/{target_branch}"
    main = default_branch(config)
    with _merge_lock:
        if not main:
            return MergeResult(
                "missing",
                detail=(
                    f"The office repository at {project} has no main branch: its HEAD names "
                    "none that exists. Nothing is merged and nothing is delivered until that "
                    "is put right."
                ),
            )
        intake = (
            _take_from_owner(config, branch=target_branch)
            if target_branch == main
            else Intake("off")
        )
        if intake.status == "diverged":
            return MergeResult("diverged", detail=intake.detail, intake=intake)
        if intake.status == "failed":
            log.warning("intake before merging into %s: %s", target_branch, intake.detail)

        source = _out(
            ["rev-parse", "--verify", f"refs/heads/{source_branch}"], project, check=False
        )
        target = _out(["rev-parse", "--verify", target_ref], project, check=False)
        if not source or not target:
            return MergeResult(
                "missing",
                detail=(
                    f"There is no branch '{source_branch}' in the office repository — it was "
                    "deleted there, or it was never published. Publish it from the workspace it "
                    "is in and merge this request again."
                    if not source
                    else f"There is no branch '{target_branch}' in the office repository to "
                         "merge into."
                ),
                intake=intake,
            )
        if source == target:
            return MergeResult(
                "up_to_date",
                detail=f"{target_branch} is already at {source_branch}.",
                intake=intake,
            )
        if git(["merge-base", "--is-ancestor", target, source],
               cwd=project, check=False).returncode != 0:
            # Asked only here, off the path that merges: a source the target
            # already contains has nothing left to take in.
            if git(["merge-base", "--is-ancestor", source, target],
                   cwd=project, check=False).returncode == 0:
                # The source is an ancestor of the target, so its name is the
                # only thing a delete takes: the commits stay reachable from the
                # branch that survives.
                deleted, said = (
                    _drop_branch(project, source_branch, source)
                    if delete_source
                    else (False, "")
                )
                return MergeResult(
                    "up_to_date",
                    detail=(
                        f"Every commit on '{source_branch}' is already in '{target_branch}'. "
                        "There is nothing left to merge."
                    ) + (f" {said}" if said else ""),
                    intake=intake,
                    source_deleted=deleted,
                )
            return MergeResult(
                "behind",
                detail=(
                    f"'{source_branch}' does not contain '{target_branch}', which has moved on "
                    f"since it was branched. Bring {target_branch} into the workspace, settle "
                    "any conflict there, publish again, and merge this request again."
                ),
                intake=intake,
            )

        msg = message or f"Merge branch '{source_branch}' into {target_branch}"
        commit = _out(
            ["commit-tree", f"{source}^{{tree}}", "-p", target, "-p", source, "-m", msg],
            project,
            env_extra=_OFFICE_IDENTITY,
        )

        if target_branch == main:
            delivery = _deliver(
                config, branch=target_branch, commit=commit, source=source_branch,
            )
        else:
            delivery = Delivery("off")
        if delivery.status == "refused":
            return MergeResult("blocked", commit=commit, detail=delivery.detail,
                               delivery=delivery, intake=intake)

        moved = git(["update-ref", target_ref, commit, target], cwd=project, check=False)
        if moved.returncode != 0:
            detail = (
                f"The merge commit {commit[:10]} was built, but the office could not move "
                f"'{target_branch}' onto it: {(moved.stdout + moved.stderr).strip()}."
            )
            if delivery.status == "delivered":
                detail += (
                    f" It is in {delivery.repo}, and the next intake fast-forwards "
                    f"'{target_branch}' onto the branch there that carries it."
                )
            log.error("merge into %s: %s", target_branch, detail)
            return MergeResult(
                "merged", commit=commit, detail=detail, delivery=delivery, intake=intake
            )

        deleted, said = (
            _drop_branch(project, source_branch, source)
            if delete_source
            else (False, "")
        )
        return MergeResult(
            "merged", commit=commit, detail=said, delivery=delivery, intake=intake,
            source_deleted=deleted,
        )
