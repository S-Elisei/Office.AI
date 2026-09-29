"""MCP server: the agent-facing tool surface."""

from __future__ import annotations

import inspect
import time
from datetime import datetime, timezone

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.concurrency import run_in_threadpool

from office import commands, core, db, stages, wallets, wiki_files

# --------------------------------------------------------------------------- the free drain seam


def _default_drain_hook(agent_name: str) -> str | None:
    return None


_drain_hook = _default_drain_hook


def set_drain_hook(fn) -> None:
    """Call once, at hub startup, with a sync callable(agent_name) -> str | None
    returning whatever free-drain text is pending for that participant (the
    bus's Bus.piggyback). Appended to every tool result before it goes back
    over the wire. Passing None restores the no-op.
    """
    global _drain_hook
    _drain_hook = fn or _default_drain_hook


def _no_supervisor(agent_name: str, actor: str) -> dict:
    """The default of the seams whose subject is a live session."""
    return {"ok": False, "reason": "no supervisor is wired up in this process"}


_stop_hook = _no_supervisor


def set_stop_hook(fn) -> None:
    """Call once, at hub startup, with a sync callable(agent_name, actor) -> dict
    that ends that agent's running turn (the bus's Bus.stop_agent).
    """
    global _stop_hook
    _stop_hook = fn or _no_supervisor


_compact_hook = _no_supervisor


def set_compact_hook(fn) -> None:
    """Call once, at hub startup, with a sync callable(agent_name, actor) -> dict
    that compacts that agent's session right now (the bus's Bus.compact).
    """
    global _compact_hook
    _compact_hook = fn or _no_supervisor


_new_session_hook = _no_supervisor


def set_new_session_hook(fn) -> None:
    """Call once, at hub startup, with a sync callable(agent_name, actor) -> dict
    that drops that agent's conversation, but only while nothing is running on
    its session (the bus's Bus.new_session).
    """
    global _new_session_hook
    _new_session_hook = fn or _no_supervisor


_fire_hook = _no_supervisor


def set_fire_hook(fn) -> None:
    """Call once, at hub startup, with a sync callable(agent_name, actor) -> dict
    that removes that agent, but only while nothing is running on its session
    (the bus's Bus.fire_agent).
    """
    global _fire_hook
    _fire_hook = fn or _no_supervisor


# --------------------------------------------------------------------------- identity & role


def _agent_from_ctx(ctx) -> str:
    if ctx.request is None:
        raise RuntimeError("no HTTP request on this MCP call — identity comes from the URL path")
    return ctx.request.path_params["agent"]


def _role_for(conn, name: str) -> str:
    """`agents.kind`, or 'executor' — the least-privileged role — for a name
    nothing has hired yet. Never raises: an unrecognized caller simply sees
    the smaller, safer tool list rather than an error at tools/list time.
    """
    row = db.query_one(conn, "SELECT kind FROM agents WHERE name = ?", (name,))
    return row["kind"] if row is not None else "executor"


def _agent_row(conn, name: str) -> dict | None:
    row = db.query_one(conn, "SELECT * FROM agents WHERE name = ?", (name,))
    return dict(row) if row is not None else None


def _name_of(conn, agent_id: int) -> str:
    return db.query_one(conn, "SELECT name FROM agents WHERE id = ?", (agent_id,))["name"]


def _named_agent(conn, args: dict, key: str) -> dict:
    """The agent `args[key]` names. Refuses a missing argument and a name that
    is not an agent's."""
    _require(args, key)
    row = _agent_row(conn, args[key])
    if row is None:
        raise ValueError(f"no such agent '{args[key]}' — roster() shows the team")
    return row


def _require(args: dict, *keys: str) -> None:
    missing = [k for k in keys if args.get(k) in (None, "")]
    if missing:
        raise ValueError(f"missing required argument(s): {', '.join(missing)}")


def _reject_unknown(tool: str, args: dict) -> None:
    """Refuse an argument the tool's schema does not declare. The refusal prints
    what the tool does take."""
    declared = set(_TOOLS[tool].input_schema.get("properties", {}))
    unknown = sorted(set(args) - declared)
    if unknown:
        raise ValueError(
            f"{tool}: unknown argument(s) {', '.join(unknown)} — it takes "
            f"{', '.join(sorted(declared))}"
        )


# --------------------------------------------------------------------------- handlers
#
# Every handler: (conn, config, agent_name, role, args) -> str. Raise
# ValueError/PermissionError/KeyError with a short, actionable sentence —
# on_call_tool turns it into an is_error tool result, not a stack trace.


def _h_say(conn, config, agent_name, role, args) -> str:
    _require(args, "to", "text")
    core.send_message(conn, agent_name, args["to"], args["text"])
    return f"sent to {args['to']}"


def _h_chat(conn, config, agent_name, role, args) -> str:
    """The common chat, backwards from the newest line."""
    before = args.get("before_id")
    messages, has_older = core.chat_history(conn, before_id=before)
    if not messages:
        return "nothing older in the common chat" if before else "the common chat is empty"
    lines = [f"[{m['id']}] {m['sender']}: {m['body']}" for m in messages]
    if has_older:
        lines.append(f"(older lines exist — chat(before_id={messages[0]['id']}))")
    return "\n".join(lines)


def _h_remind(conn, config, agent_name, role, args) -> str:
    """One message, sent later, waking whoever it is addressed to; or one of the
    caller's own taken back before it goes."""
    op = args.get("op")
    if op == "set":
        _require(args, "to", "text", "in_seconds")
        wake = core.schedule_message(
            conn, agent_name, args["to"], args["text"], args["in_seconds"]
        )
        return f"reminder {wake['id']}: sends to {wake['recipient']} {_fmt_stamp(wake['due_at'])}"
    if op == "cancel":
        _require(args, "reminder_id")
        if core.cancel_scheduled_message(
            conn, args["reminder_id"], sender=agent_name, actor=agent_name
        ):
            return f"reminder {args['reminder_id']} cancelled"
        raise ValueError(
            f"no reminder {args['reminder_id']} of yours is waiting to go — roster() lists "
            "the ones you set"
        )
    raise ValueError(f"remind: unknown op '{op}' — expected set or cancel")


def _h_expect(conn, config, agent_name, role, args) -> str:
    """An answer awaited from a local service, and the address it is posted to."""
    _require(args, "about", "within_seconds")
    expectation = core.open_expectation(conn, agent_name, args["about"], args["within_seconds"])
    return (
        f"expecting '{expectation['about']}' until {_fmt_stamp(expectation['due_at'])}. "
        "Give the service this address to notify: "
        f"http://127.0.0.1:{config.port}/hooks/{expectation['token']}"
    )


_TASK_LIST_LINES = 200
_TASK_LIST_CHARS = 16000


def _capped(lines: list[str], left_out: str) -> list[str]:
    """At most _TASK_LIST_LINES of `lines` and _TASK_LIST_CHARS characters, the
    first line whatever its size, then a line counting the rest, worded by
    `left_out`."""
    shown: list[str] = []
    size = 0
    for line in lines:
        if len(shown) == _TASK_LIST_LINES or (shown and size + len(line) > _TASK_LIST_CHARS):
            break
        shown.append(line)
        size += len(line) + 1
    if len(shown) < len(lines):
        shown.append(f"{len(lines) - len(shown)} more {left_out}")
    return shown


def _read_task(conn, task_id) -> str:
    found = core.get_task(conn, task_id)
    if found is None:
        raise ValueError(f"no such task {task_id} — task(op=list) shows the board")
    task = found["task"]
    lines = [f"#{task['id']} [{task['status']}] {task['title']}"]
    if found["chain"]:
        lines.append("Under " + ", under ".join(f"#{t['id']} {t['title']}" for t in found["chain"]))
    else:
        lines.append("Top level.")
    lines.append(task["body"] if task["body"] else "(no body)")
    if task["result"]:
        lines.append(f"Result:\n{task['result']}")
    if found["children"]:
        lines.append(f"Children ({len(found['children'])}):")
        lines.extend(
            _capped(
                ["  " + core.task_line(t) for t in found["children"]],
                f"child task(s) left out — task(op=list, parent={task['id']}) with status or query narrows them",
            )
        )
    for heading, deps in (
        ("Depends on (they finish first)", found["depends_on"]),
        ("Needed by (they wait for this one)", found["needed_by"]),
    ):
        if deps:
            lines.append(f"{heading}:")
            lines.extend(f"  #{t['id']} [{t['status']}] {t['title']}" for t in deps)
    if found["works"]:
        lines.append("Works:")
        lines.extend(
            f"  work {w['id']}: {w['agent']}, assigned by {w['assigner']} "
            f"[{ {'running': 'open', 'done': 'reported'}.get(w['status'], w['status']) }]"
            for w in found["works"]
        )
    if found["tickets"]:
        lines.append("Tickets:")
        lines.extend(f"  ticket {t['id']} [{t['status']}] {t['title']}" for t in found["tickets"])
    return "\n".join(lines)


def _h_task(conn, config, agent_name, role, args) -> str:
    op = args.get("op")
    if op == "list":
        tasks = core.list_tasks(
            conn, parent=args.get("parent") or None, status=args.get("status"), query=args.get("query") or None
        )
        if not tasks:
            if args.get("status"):
                return "no task matches"
            return (
                "no task matches among those neither done nor cancelled — status=all includes them"
            )
        return "\n".join(
            _capped([core.task_line(t) for t in tasks], "task(s) left out — narrow with parent, status or query")
        )
    if op == "read":
        _require(args, "task_id")
        return _read_task(conn, args["task_id"])
    if op == "create":
        _require(args, "title")
        task = core.create_task(
            conn, args["title"], args.get("body"), args.get("status", "idea"), actor=agent_name,
            parent=args.get("parent") or None,
        )
        return f"task {task['id']} created: {task['title']} [{task['status']}]"
    if op == "update":
        _require(args, "task_id")
        task = core.update_task(
            conn, args["task_id"], title=args.get("title"), body=args.get("body"), parent=args.get("parent"),
            actor=agent_name,
        )
        return f"task {task['id']} updated"
    if op == "move":
        _require(args, "task_id", "status")
        if args["status"] == "cancelled" and role not in _MANAGER_ROLES:
            raise PermissionError(
                "cancelling a task is the director's and the leads' — ask your manager"
            )
        if args["status"] in core.CLOSED_TASK_STATUSES:
            _require(args, "result")
            task = core.close_task(
                conn, args["task_id"], args["result"], actor=agent_name, status=args["status"]
            )
            return f"task {task['id']} {task['status']}: {task['result']}"
        task = core.move_task(conn, args["task_id"], args["status"], args.get("position"), actor=agent_name)
        return f"task {task['id']} moved to {task['status']}"
    if op == "link":
        # Task-to-task sequencing only -- ticket-to-task linking lives on the
        # ticket side (ticket(op=link)).
        _require(args, "task_id", "depends_on")
        if args.get("remove"):
            core.unlink_tasks(conn, args["depends_on"], args["task_id"], actor=agent_name)
            return f"task {args['task_id']} no longer depends on task {args['depends_on']}"
        core.link_tasks(conn, args["depends_on"], args["task_id"], actor=agent_name)
        return f"task {args['task_id']} now depends on task {args['depends_on']}"
    raise ValueError(f"task: unknown op '{op}' — expected list, read, create, update, move, or link")


def _h_work(conn, config, agent_name, role, args) -> str:
    """Your own current work: look at it, or finish it."""
    op = args.get("op")
    me = _agent_row(conn, agent_name)

    if op == "show" and args.get("work") not in (None, ""):
        return _show_work(conn, args["work"])
    if op == "show":
        work = core.current_work(conn, me["id"])
        if work is None:
            return "you have no work assigned — a work starts when assign() names you"
        own = work["assigned_by_agent_id"] == me["id"]
        assigner = _name_of(conn, work["assigned_by_agent_id"])
        line = (
            f"work {work['id']} [{_WORK_STATUS_WORDS.get(work['status'], work['status'])}] "
            f"on branch '{work['branch']}', assigned by "
            f"{'you' if own else assigner}:\n{work['brief']}"
        )
        if work["status"] == "done" and own:
            line += (
                "\nYou have reported this work, which you assigned yourself; it stays on the "
                "books until you close it with work_close."
            )
        elif work["status"] == "done":
            line += f"\nYou have reported this work; {assigner} has not closed it yet."
        elif work["status"] == "failed" and own:
            line += (
                f"\nThe turn on this work died ({work['fail_reason']}). Take it up again with "
                f"work_reassign(work={work['id']}, to_agent={agent_name}, workspace='inherit')."
            )
        elif work["status"] == "failed":
            told = core.work_notice_recipient(conn, work["id"]) or core.get_owner_name(conn)
            line += (
                f"\nThe turn on this work died ({work['fail_reason']}). Tell {told} where you "
                "had got to; carry on once they hand it back to you."
            )
        return line

    if op != "finish":
        raise ValueError(f"work: unknown op '{op}' — expected show or finish")

    _require(args, "summary")
    # Whatever work this agent has, in whatever state — the same answer
    # work(op=show) gives.
    work = core.current_work(conn, me["id"])
    if work is None:
        raise ValueError("you have no work to finish — a work starts when assign() names you")

    pr_info = args.get("pr")
    note = ""
    if pr_info:
        _require(pr_info, "title", "target_branch")
        # The PR's source branch is whatever this publish actually pushed, read
        # off the working tree by git.publish -- never a name out of the database.
        published = core.publish_workspace(conn, config, me["id"])
        pr = core.create_pr(
            conn, title=pr_info["title"], body=pr_info.get("body"),
            source_branch=published.branch,
            target_branch=pr_info["target_branch"], author_agent_id=me["id"],
        )
        if pr["republished"]:
            note = (
                f" PR {pr['id']} updated: {pr['title']} ({pr['source_branch']} -> "
                f"{pr['target_branch']}){_republished(pr_info.get('body'))}."
            )
        else:
            note = f" PR {pr['id']} opened ({pr['source_branch']} -> {pr['target_branch']})."
        left = _left_behind(published)
        if left:
            note += f" {left}"
            # Onto the report as well, marked as the office's own words inside
            # the assignee's text.
            args["summary"] = args["summary"] + "\n\n[office] " + left

    core.finish_work(conn, work["id"], summary=args["summary"], actor=agent_name)
    if work["assigned_by_agent_id"] == me["id"]:
        return (
            f"work {work['id']} reported to nobody: you assigned it yourself, and it stays on "
            f"the books until you close it with work_close.{note}"
        )
    return (
        f"work {work['id']} reported to {_name_of(conn, work['assigned_by_agent_id'])} as a "
        f"message from you.{note}"
    )


#: How a work's stored status reads in an answer.
_WORK_STATUS_WORDS = {"running": "open", "done": "reported"}


def _show_work(conn, work_id) -> str:
    """work(op=show, work=N): any work in full, for anyone."""
    row = db.query_one(conn, "SELECT * FROM works WHERE id = ?", (work_id,))
    if row is None:
        raise ValueError(f"no work {work_id}: it has been closed, or the number is wrong")
    work = dict(row)
    status = _WORK_STATUS_WORDS.get(work["status"], work["status"])
    lines = [
        f"work {work['id']} [{status}]: {_name_of(conn, work['agent_id'])}, assigned by "
        f"{_name_of(conn, work['assigned_by_agent_id'])}, on branch '{work['branch']}'"
        + (f", for task {work['task_id']}" if work["task_id"] else "")
    ]
    if work["status"] == "failed":
        lines.append(f"Failed: {work['fail_reason']}")
    elif work["status"] == "paused":
        lines.append(
            f"Paused: {work['pause_reason']}"
            + (f", earliest resume {_fmt_epoch(work['resume_after'])}" if work["resume_after"] else "")
        )
    lines += ["Brief:", work["brief"]]
    if work["output_tail"]:
        lines += ["Output tail, stored when it last failed:", work["output_tail"]]
    else:
        lines.append("No output tail is stored on it.")
    return "\n".join(lines)


def _left_behind(published) -> str:
    """What a publish did not take with it, for the agent that published.

    Nothing pushes inside a submodule; a submodule holding commits of its own
    keeps them. Empty on an ordinary publish, and the callers drop the empty
    string.
    """
    if not published.unpublished_submodules:
        return ""
    return (
        f"NOT published: your commits inside {', '.join(published.unpublished_submodules)} — "
        "nothing pushes to a submodule. Only the main repository was published. Move the work "
        "into the main repository, or say so in your report."
    )


def _republished(body) -> str:
    """The tail of the answer for a create that updated a PR already open."""
    return ", published again" + (", its description replaced" if body is not None else "")


def _h_pr(conn, config, agent_name, role, args) -> str:
    op = args.get("op")
    if op in ("merge", "close") and role not in _MANAGER_ROLES:
        raise PermissionError(
            f"pr(op={op}) is the director's and the leads' — ask your manager to {op} it"
        )
    if op == "create":
        # source_branch is never an argument: a PR comes from the caller's own
        # workspace, on whatever branch that tree is standing. Publish it first,
        # and take the branch name from what that publish pushed.
        _require(args, "title", "target_branch")
        me = _agent_row(conn, agent_name)
        published = core.publish_workspace(conn, config, me["id"])
        pr = core.create_pr(
            conn, title=args["title"], body=args.get("body"),
            source_branch=published.branch,
            target_branch=args["target_branch"], author_agent_id=me["id"],
        )
        left = _left_behind(published)
        if pr["republished"]:
            done, again = "updated", _republished(args.get("body"))
        else:
            done, again = "opened", ""
        return (
            f"PR {pr['id']} {done}: {pr['title']} ({pr['source_branch']} -> "
            f"{pr['target_branch']}){again}"
            + (f". {left}" if left else "")
        )
    if op == "comment":
        _require(args, "pr_id", "comment")
        core.comment_pr(conn, args["pr_id"], agent_name, args["comment"])
        return f"comment added to PR {args['pr_id']}"
    if op == "merge":
        # delete_branch is required, not defaulted: the branch either survives
        # the merge or it does not, and which one happens is the caller's to
        # state rather than the office's to assume.
        _require(args, "pr_id", "delete_branch")
        result = core.merge_pr(
            conn, config, args["pr_id"], actor=agent_name,
            delete_branch=args["delete_branch"],
        )
        if result["status"] == "merged":
            # A merge into the main line goes to the owner's repository before it
            # is recorded here. Said only when a delivery actually happened.
            delivered = (result.get("delivery") or {}).get("status") == "delivered"
            tail = " and delivered to the owner's repository" if delivered else ""
            # What became of the source branch: deleted, kept on purpose, or
            # still standing after a delete was asked for.
            if result.get("branch_deleted"):
                tail += f", and '{result['source_branch']}' was deleted"
            elif not args["delete_branch"]:
                tail += f", and '{result['source_branch']}' was kept"
            else:
                tail += f", and '{result['source_branch']}' is still there"
            # detail is present only when something about the merge needs saying.
            detail = result.get("detail") or ""
            return (
                f"PR {args['pr_id']} merged: {result['commit'][:10]}{tail}"
                + (f". {detail}" if detail else "")
            )
        if result["status"] == "up_to_date":
            # The branch is dealt with here too — the row is deleted on this
            # answer exactly as it is on a merge.
            if result.get("branch_deleted"):
                branch = f" '{result['source_branch']}' was deleted."
            elif not args["delete_branch"]:
                branch = f" '{result['source_branch']}' was kept."
            else:
                branch = f" '{result['source_branch']}' is still there."
            # Not "closed": op=close is its own answer.
            return f"PR {args['pr_id']} needed no merge and is off the list: {result['detail']}{branch}"
        # behind, blocked, diverged or missing: the PR is still open and the
        # detail is the whole of what to do about it. blocked and diverged are
        # the owner's repository refusing.
        owners = (
            f"This is {core.get_owner_name(conn)}'s to clear in his own repository — send it to "
            "him as it stands and change nothing there. "
            if result["status"] in ("blocked", "diverged") else ""
        )
        return f"PR {args['pr_id']} not merged ({result['status']}): {owners}{result['detail']}"
    if op == "close":
        # No delete_branch here, unlike merge: closing does not touch git.
        _require(args, "pr_id")
        result = core.close_pr(conn, args["pr_id"], actor=agent_name)
        return (
            f"PR {args['pr_id']} closed without merging: {result['title']}. "
            f"Nothing was merged and '{result['source_branch']}' still stands."
        )
    if op == "list":
        prs = core.list_prs(conn)
        if not prs:
            return "no open PRs"
        return "\n".join(
            f"PR {p['id']}: {p['title']} ({p['source_branch']} -> {p['target_branch']}) "
            f"[{p['status']}] by {p['author_name']}, {p['comment_count']} comment(s)"
            for p in prs
        )
    if op == "read":
        # list gives the count; this gives the words.
        _require(args, "pr_id")
        pr = core.get_pr(conn, args["pr_id"])
        if pr is None:
            raise ValueError(f"no such PR {args['pr_id']} — pr(op=list) shows what is open")
        lines = [
            f"PR {pr['id']} [{pr['status']}] {pr['title']} "
            f"({pr['source_branch']} -> {pr['target_branch']}, by {pr['author_name']})"
        ]
        if pr["body"]:
            lines.append(pr["body"])
        if pr["comments"]:
            lines.append(f"Comments ({len(pr['comments'])}), oldest first:")
            lines.extend(f"  {c['author']}: {c['body']}" for c in pr["comments"])
        else:
            lines.append("No comments.")
        return "\n".join(lines)
    raise ValueError(
        f"pr: unknown op '{op}' — expected create, comment, merge, close, list, or read"
    )


def _h_note(conn, config, agent_name, role, args) -> str:
    op = args.get("op")
    kind = args.get("kind")
    if kind not in ("wiki", "rule"):
        raise ValueError("note: kind must be 'wiki' or 'rule'")

    if kind == "wiki":
        ws = core.workspace_of(conn, _agent_row(conn, agent_name)["id"])["id"]
        if op == "list":
            # The index, never the bodies.
            pages = core.list_wiki_pages(conn)
            lines = [f"Pages are files under {wiki_files.wiki_dir(config, ws)}."]
            lines += [
                f"{p['path']} [{p['category']}] {p['title']} (v{p['version']}, "
                f"{p['comment_count']} comment(s))"
                for p in pages
            ]
            return "\n".join(lines if pages else [*lines, "The wiki is empty."])
        if op == "read":
            _require(args, "path")
            wiki_files.sync_page(conn, config, ws, args["path"])
            page = core.get_wiki_page(conn, args["path"])
            if page is None:
                raise ValueError(f"no wiki page '{args['path']}' — note(op=list, kind=wiki) shows what exists")
            lines = [
                f"{page['path']} [{page['category']}] {page['title']} (v{page['version']}, "
                f"last edited by {page['updated_by']})",
                wiki_files.state(conn, config, ws, page["path"], page["version"]),
            ]
            if page["comments"]:
                lines.append(f"Comments ({len(page['comments'])}), oldest first:")
                # Every comment says which version it was written against, and
                # says outright when that is not the version above.
                for c in page["comments"]:
                    # The numbers and not a conclusion drawn from them: `version`
                    # also moves for a title-only edit and for an undo.
                    behind = f" — page is now v{page['version']}" if c["page_version"] < page["version"] else ""
                    lines.append(f"  {c['author']} (on v{c['page_version']}{behind}): {c['body']}")
            else:
                lines.append("No comments.")
            return "\n".join(lines)
        if op == "comment":
            # The version is not an argument: core stamps the page's current one
            # onto the comment.
            _require(args, "path", "comment")
            comment = core.comment_wiki_page(conn, args["path"], agent_name, args["comment"])
            return f"comment added to wiki '{args['path']}', against version {comment['page_version']}"
        if op == "write":
            _require(args, "path")
            return wiki_files.publish(
                conn, config, ws, args["path"], category=args.get("category"),
                title=args.get("title"), actor=agent_name,
            )
        if op == "undo":
            _require(args, "path")
            page = core.undo_wiki_page(conn, args["path"], actor=agent_name)
            wiki_files.sync_page(conn, config, ws, args["path"])
            return (
                f"wiki '{page['path']}' put back to the body it had before its last write, "
                f"saved at version {page['version']}. "
                + wiki_files.state(conn, config, ws, page["path"], page["version"])
            )
        if op == "delete":
            _require(args, "path")
            copy = db.query_one(
                conn, "SELECT version FROM wiki_copies WHERE workspace_id = ? AND path = ?",
                (ws, args["path"]),
            )
            core.delete_wiki_page(
                conn, args["path"], copy["version"] if copy else 0, actor=agent_name
            )
            wiki_files.sync_page(conn, config, ws, args["path"])
            return f"wiki '{args['path']}' deleted"
        raise ValueError(
            f"note: unknown op '{op}' for kind=wiki — expected list, read, comment, write, undo "
            f"or delete; the pages are files under {wiki_files.wiki_dir(config, ws)}"
        )

    # Everyone reads the rules; writing them is the director's alone.
    if op == "list":
        return core.render_rules(conn)
    if op not in ("create", "update", "delete"):
        raise ValueError(
            f"note: unknown op '{op}' for kind=rule — expected list, or create, update or delete "
            "for the director"
        )
    if role != "director":
        raise ValueError(
            "the project's rules are the director's to write — say() the director, or put it in "
            "the wiki"
        )

    if op == "create":
        # title is REQUIRED here, though the column is nullable.
        _require(args, "title", "text")
        rule = core.create_rule(conn, args["text"], agent_name, title=args["title"])
        return f"rule {rule['id']} created"
    if op == "update":
        # ...and OPTIONAL here, meaning "leave the title as it is" (core's
        # update_rule COALESCEs it).
        _require(args, "rule_id", "text")
        rule = core.update_rule(conn, args["rule_id"], args["text"], title=args.get("title"), actor=agent_name)
        return f"rule {rule['id']} updated"
    # op == "delete"
    _require(args, "rule_id")
    core.delete_rule(conn, args["rule_id"], actor=agent_name)
    return f"rule {args['rule_id']} deleted"


def _h_ticket(conn, config, agent_name, role, args) -> str:
    op = args.get("op")
    if op == "create":
        _require(args, "title", "addressee", "kind")
        ticket = core.create_ticket(
            conn, title=args["title"], body=args.get("body"), author=agent_name, addressee=args["addressee"],
            kind=args["kind"], task_id=args.get("task_id"),
        )
        return f"ticket {ticket['id']} opened, addressed to {ticket['addressee']}"
    if op == "comment":
        _require(args, "ticket_id", "comment")
        core.comment_ticket(conn, args["ticket_id"], agent_name, args["comment"])
        return f"comment added to ticket {args['ticket_id']}"
    if op == "resolve":
        _require(args, "ticket_id", "resolution")
        ticket = core.resolve_ticket(conn, args["ticket_id"], resolved_by=agent_name, resolution=args["resolution"])
        return f"ticket {ticket['id']} resolved: {ticket['resolution']}"
    if op == "list":
        status = args.get("status") or "open"
        tickets = core.list_tickets(
            conn, status=None if status == "all" else status, addressee=args.get("addressee")
        )
        if not tickets:
            return "no tickets" if status == "all" else f"no {status} tickets"
        return "\n".join(
            f"ticket {t['id']} [{t['status']}] {t['title']} (author={t['author']}, addressee={t['addressee']})"
            for t in tickets
        )
    if op == "read":
        # list finds the ticket; this is the only place its words are.
        _require(args, "ticket_id")
        ticket = core.get_ticket(conn, args["ticket_id"])
        if ticket is None:
            raise ValueError(f"no such ticket {args['ticket_id']} — ticket(op=list) shows what is open")
        lines = [
            f"ticket {ticket['id']} [{ticket['status']}] {ticket['title']} "
            f"({ticket['kind']}, author={ticket['author']}, addressee={ticket['addressee']})"
        ]
        if ticket["task_id"]:
            lines.append(f"About task {ticket['task_id']}.")
        lines.append(ticket["body"] if ticket["body"] else "(no body — the title is the whole of it)")
        if ticket["status"] == "resolved":
            lines.append(f"Resolved by {ticket['resolved_by']}: {ticket['resolution']}")
        if ticket["comments"]:
            lines.append(f"Comments ({len(ticket['comments'])}), oldest first:")
            lines.extend(f"  {c['author']}: {c['body']}" for c in ticket["comments"])
        else:
            lines.append("No comments.")
        return "\n".join(lines)
    if op == "link":
        _require(args, "ticket_id", "task_id")
        ticket = core.link_ticket_task(conn, args["ticket_id"], args["task_id"], actor=agent_name)
        return f"ticket {ticket['id']} linked to task {args['task_id']}"
    raise ValueError(f"ticket: unknown op '{op}' — expected create, comment, resolve, list, read, or link")


# -- run: the one asynchronous handler ----------------------------------------
#
# Everything else in this module is a synchronous function that on_call_tool
# hands to run_in_threadpool. This one is a coroutine: it awaits the deadline
# instead of sleeping through it (commands.Command.wait_async, an
# asyncio.Event woken from the reaper thread), and holds no worker at all
# while it waits.
#
# The uniform signature is NOT broken — (conn, config, agent_name, role, args)
# -> str, the same five arguments, the same return. What differs is only
# await-ness, and every blocking step inside still goes to the threadpool:
# no part of this runs on the event loop or the bus thread.


def _workspace_for(conn, agent_name: str) -> dict:
    me = _agent_row(conn, agent_name)
    ws = core.workspace_of(conn, me["id"])
    if ws is None:
        raise ValueError("you have no workspace, so there is nowhere to run a command")
    return ws


def _run_seconds(args: dict) -> float:
    """The deadline for this one call, defaulted and bounded.

    Refused rather than clamped when it is too large. The refusal states the
    number and the ceiling.
    """
    raw = args.get("seconds")
    if raw in (None, ""):
        return commands.DEADLINE_SECONDS
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"run: seconds must be a number of seconds, not {raw!r}") from None
    if seconds <= 0:
        raise ValueError("run: seconds must be greater than zero")
    if seconds > commands.DEADLINE_SECONDS:
        raise ValueError(
            f"run: {seconds:g}s is over the {commands.DEADLINE_SECONDS:g}s ceiling; op=wait waits "
            "again."
        )
    return seconds


def _run_result(cmd) -> str:
    """One command's state as the agent will read it.

    Says which of the three things happened without leaving the fourth to be
    inferred: finished, stopped by somebody, or still running — and 'still
    running' is stated as a fact about the command rather than as a failure of
    the call.
    """
    tail, dropped = cmd.output()
    file_chars, file_dropped = cmd.log_state()
    on_stage = isinstance(cmd, stages.StageRun)
    waiting = cmd.waiting() if on_stage else None
    lines = []
    if cmd.killed_by is not None:
        lines.append(f"{cmd.handle} was stopped ({cmd.killed_by}) after {cmd.elapsed:.0f}s.")
    elif waiting:
        lines.append(waiting)
    elif cmd.running:
        lines.append(
            f"{cmd.handle} is STILL RUNNING after {cmd.elapsed:.0f}s — the deadline passed, "
            f"nothing was killed. Last output {cmd.quiet_for:.0f}s ago."
        )
    elif on_stage and cmd.exit_code is None:
        lines.append(f"{cmd.handle} ended after {cmd.elapsed:.0f}s without running the command.")
    else:
        lines.append(f"{cmd.handle} finished after {cmd.elapsed:.0f}s, exit code {cmd.exit_code}.")
    # This result must never send a reader to a file for "the whole of it"
    # when the file was itself cut short.
    if dropped and file_dropped:
        lines.append(
            f"[{dropped} characters of earlier output are not in this result, AND the file "
            f"named below is itself incomplete: it stopped at {file_chars} characters and a "
            f"further {file_dropped} were never written anywhere. What survives is the "
            "beginning of the run, in the file, and its last lines, below. The middle is gone.]"
        )
    elif dropped:
        lines.append(
            f"[{dropped} characters of earlier output are not in this result — the tail is "
            "below, the whole of it is in the file named at the end]"
        )
    lines.append(tail.rstrip() if tail.strip() else "(no output)")
    if cmd.output_truncated_at_the_end:
        lines.append(
            "[the office stopped following this run's output before the pipe closed — something "
            "the command started outlived it and still holds the pipe open — so the END of the "
            "output above is missing, and so is the end of the file below. What is there is "
            "real; there was more.]"
        )
    if on_stage:
        lines.extend(cmd.report())
    if cmd.running:
        lines.append(
            f"run(op=wait, handle='{cmd.handle}') waits again; run(op=stop, "
            f"handle='{cmd.handle}') kills it and everything it started."
        )
    if file_dropped:
        lines.append(f"Output file (TRUNCATED, first {file_chars} characters only): {cmd.log_path}")
    else:
        lines.append(f"Full output: {cmd.log_path}")
    return "\n".join(lines)


def _run_list(cmds: list) -> str:
    """Every command this agent has going, one line each.

    One line per command, with the figure a decision is actually made on —
    how long it has been quiet — and nothing else. The output belongs to
    op=wait.
    """
    if not cmds:
        return "you have run nothing this turn."
    lines = []
    for cmd in cmds:
        on_stage = isinstance(cmd, stages.StageRun)
        if cmd.killed_by is not None:
            state = f"stopped ({cmd.killed_by})"
        elif on_stage and cmd.waiting():
            state = "WAITING for the stage"
        elif cmd.running:
            state = f"RUNNING, quiet for {cmd.quiet_for:.0f}s"
        elif on_stage and cmd.exit_code is None:
            state = "ended without running the command"
        else:
            state = f"finished, exit code {cmd.exit_code}"
        where = f" on stage '{cmd.stage}'" if on_stage else ""
        command = cmd.command if len(cmd.command) <= 120 else cmd.command[:117] + "..."
        # `elapsed` is measured from the start for a finished command too.
        lines.append(f"{cmd.handle}  [{state}]  started {cmd.elapsed:.0f}s ago{where}: {command}")
    running = [c.handle for c in cmds if c.running]
    if running:
        lines.append(
            f"Still going: {', '.join(running)}. run(op=wait, handle=...) to look, "
            "run(op=stop, handle=...) to end one."
        )
    return "\n".join(lines)


async def _h_run(conn, config, agent_name, role, args) -> str:
    op = args.get("op")

    if op == "start":
        _require(args, "command")
        seconds = _run_seconds(args)
        began = time.monotonic()
        workspace = await run_in_threadpool(_workspace_for, conn, agent_name)
        if args.get("stage"):
            # The snapshot is taken inside this call; the deadline counts it.
            cmd = await run_in_threadpool(
                stages.start_run, conn, config, agent_name, workspace, args["stage"],
                args["command"],
            )
        else:
            # Spawning is a process creation and a log file — short, but
            # blocking, and the loop serves every other agent.
            cmd = await run_in_threadpool(
                commands.start, agent_name, workspace["path"], args["command"])
        await cmd.wait_async(max(0.0, seconds - (time.monotonic() - began)))
        return _run_result(cmd)

    if op == "wait":
        _require(args, "handle")
        seconds = _run_seconds(args)
        cmd = await run_in_threadpool(commands.get, agent_name, args["handle"])
        await cmd.wait_async(seconds)
        return _run_result(cmd)

    if op == "stop":
        _require(args, "handle")
        cmd = await run_in_threadpool(commands.get, agent_name, args["handle"])
        await run_in_threadpool(cmd.kill, f"{agent_name} asked for it")
        # kill() returns when the processes are gone; the reaper thread has
        # still to collect the exit code and the reader thread to drain what is
        # left in the pipe. Seconds at most — both threads are already unblocked.
        await cmd.wait_async(5.0)
        return _run_result(cmd)

    if op == "list":
        cmds = await run_in_threadpool(commands.list_for, agent_name)
        return _run_list(cmds)

    raise ValueError(f"run: unknown op '{op}' — expected start, wait, stop, or list")


# -- roster and the managers' tools --------------------------------------------


def _fmt_epoch(value) -> str:
    """A stored reset time in the machine's own zone, or "unknown".

    Adapters normalise every vendor's answer to epoch seconds UTC, and store
    NULL when it could not be turned into an instant or has already passed --
    "unknown" is that NULL, not a formatting failure.

    Shown local, not UTC, and the zone is named in the string. The same rule
    the web pages follow (office/web/templating.py, fmt_time).
    """
    if not value:
        return "unknown"
    try:
        local = datetime.fromtimestamp(int(value), tz=timezone.utc).astimezone()
        return local.strftime("%Y-%m-%d %H:%M") + " local"
    except (ValueError, OSError, OverflowError):
        return str(value)


def _first_line(body: str | None, limit: int = 100) -> str:
    """Enough of a message to recognise it by, on one line of a listing.

    A wake's or a brief's whole text is not repeated here: the line is there to
    tell one entry from another, and work(op=show, work=N) gives a brief in full.
    """
    text = (body or "").strip()
    first = text.splitlines()[0] if text else ""
    if len(first) > limit:
        return first[: limit - 1].rstrip() + "…"
    return first + (" …" if len(text.splitlines()) > 1 else "")


def _fmt_stamp(value: str | None) -> str:
    """One of the schema's own ISO-8601 UTC timestamps, in the machine's zone.

    _fmt_epoch's counterpart for the other shape a stored instant comes in: quota
    reset times are epoch seconds, and everything written in the schema's
    strftime shape — a wake's due_at among them — is this.
    """
    if not value:
        return "unknown"
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone().strftime("%Y-%m-%d %H:%M") + " local"


def _h_roster(conn, config, agent_name, role, args) -> str:
    """The team as a tree for everybody; for a manager, everything staffing its
    subtree turns on; for the director, also the directories under ws/ the
    office did not create.

    **This is the only place any of it is said.** None of it rides in a system
    prompt.

    A manager's answer carries every work on the books whose assignee is in its
    subtree or which it assigned, the failed ones included, and a REPORTED work
    stands on it until its assigner closes it.
    """
    snap = core.roster(conn)
    me = next(a for a in snap["agents"] if a["name"] == agent_name)
    manager = role in _MANAGER_ROLES
    reach = core.subtree_ids(conn, me["id"]) if manager else set()
    reach_names = {a["name"] for a in snap["agents"] if a["id"] in reach}
    lines = ["Team:"]
    for a in snap["agents"]:
        line = "  " * (a["depth"] + 1) + a["name"]
        if a["title"]:
            line += f" — {a['title']}"
        line += f" ({a['kind']}/{a['runtime']}/{a['model']})"
        if a["id"] == me["id"]:
            line += " (you)"
        elif a["id"] in reach:
            ctx = f"{a['context_used']}/{a['context_limit']}" if a["context_limit"] else "n/a"
            line += f" status={a['status']} context={ctx}"
        lines.append(line)
    # The caller's own instructions and nobody else's.
    own = _agent_row(conn, agent_name)["instructions"]
    lines.append(f"Your standing instructions, as they stand now:\n{own}" if own
                 else "Your standing instructions: (none)")
    ws = core.workspace_of(conn, me["id"])
    lines.append(f"Your workspace: {ws['path']}; your sandbox: {config.scratch_dir / ws['id']}")
    # Everyone sees what it set itself; a manager also what its subtree set.
    wakes = [
        w for w in core.scheduled_messages(conn)
        if w["sender"] == agent_name or w["sender"] in reach_names
    ]
    if wakes:
        lines.append("Reminders not yet sent:")
        for w in wakes:
            lines.append(
                f"  [{w['id']}] {_fmt_stamp(w['due_at'])}: from {w['sender']} to {w['recipient']} "
                f"— {_first_line(w['body'])}"
            )
    expectations = [
        e for e in core.open_expectations(conn)
        if e["agent"] == agent_name or e["agent"] in reach_names
    ]
    if expectations:
        lines.append("Open expectations:")
        for e in expectations:
            lines.append(
                f"  {e['agent']}: {e['about']} — opened {_fmt_stamp(e['opened_at'])}, "
                f"due {_fmt_stamp(e['due_at'])}"
            )
    lines.extend(_stage_lines(conn, config, role))
    if not manager:
        return "\n".join(lines)
    works = [
        w for w in snap["works"]
        if w["agent_id"] in reach or w["assigned_by_agent_id"] == me["id"]
    ]
    if works:
        # Failures and pauses are here, not only in the message the office
        # sent when they happened. The failure or pause detail is on the
        # line, and for a pause so is the earliest resume time.
        lines.append("Work:")
        for w in works:
            # The stored 'running' is printed as "open": the column means the
            # work is open and nobody has reported it. Whether a turn is
            # actually running is the supervisor's, which core.roster() does
            # not read.
            state = "open" if w["status"] == "running" else w["status"]
            assigner = "you" if w["assigned_by_agent_id"] == me["id"] else w["assigner_name"]
            line = (
                f"  work {w['id']}: {w['agent_name']} — {_first_line(w['brief'], 160)} [{state}] "
                f"(branch {w['branch']}, assigned by {assigner})"
            )
            if w["status"] == "done" and w["assigned_by_agent_id"] == me["id"]:
                line += (
                    " reported and waiting on you: close it with work_close, or send it back with "
                    f"work_reassign(work={w['id']}, to_agent={w['agent_name']}, workspace=inherit)"
                )
            elif w["status"] == "done":
                line += f" reported, waiting on {w['assigner_name']}"
            elif w["status"] == "failed":
                line += f" failed: {w['fail_reason']}; its output tail: work(op=show, work={w['id']})"
            elif w["status"] == "paused":
                line += f" paused: {w['pause_reason']}"
                if w["resume_after"]:
                    line += f", earliest resume {_fmt_epoch(w['resume_after'])}"
            lines.append(line)
    if snap["quota"] or snap["holds"]:
        # Bucket names are the vendor's own and are printed untranslated: the
        # office does not decide which limit means what.
        lines.append("Quota:")
        for q in snap["quota"]:
            # The vendor's own two numbers and nothing derived: how much
            # remains, and when it comes back.
            lines.append(
                f"  {q['runtime']} · {q['label']}: {q['remaining_fraction']:.0%} remaining, "
                f"resets {_fmt_epoch(q['reset_time'])}"
            )
        lines.extend(_wallet_lines(conn))
        for h in snap["holds"]:
            lines.append(
                f"  held: {h['runtime']} · {h['model']} — no turn starts on it before "
                f"{_fmt_epoch(h['until'])}; it refused one for quota"
            )
    lines.extend(_price_list_lines(conn))
    lines.append(f"Roles in effect: {', '.join(core.work_roles(conn))}.")
    lines.append(f"Complexities in effect: {', '.join(core.work_complexities(conn))}.")
    if snap["blocked_tasks"]:
        lines.append("Blocked (looks ready but its dependency isn't done):")
        for b in snap["blocked_tasks"]:
            lines.append(f"  task {b['id']} '{b['title']}' [{b['status']}] blocked by task {b['blocking_task_id']} '{b['blocking_title']}'")
    # The same renderer core.hire() refuses with.
    catalogue = core.models_catalogue(conn)
    if catalogue:
        lines.append("Models each runtime accepts:")
        lines.append(catalogue)
    strays = core.stray_workspace_dirs(conn, config.ws_dir) if role == "director" else []
    if strays:
        lines.append(
            "Also in the workspace directory, next to the team's workspaces — not created "
            f"by the office and belonging to no agent: {', '.join(strays)}."
        )
    # Its own sentence, never folded into the strays above: these are the
    # office's own workspaces, left standing by a firing with the work still in
    # them, and the line above says of its list that the office did not make it.
    ownerless = core.ownerless_workspaces(conn)
    if ownerless:
        lines.append(
            "Workspaces the office keeps that belong to nobody, with whatever their last "
            "owner left in them: "
            f"{', '.join(w['id'] for w in ownerless)}. "
            "agent(op=hire, adopt_workspace_id=<id>) hands one to a new agent."
        )
    return "\n".join(lines)


def _units(value: float) -> str:
    """A wallet figure: whole units from a hundred up, one decimal below."""
    return f"{value:.0f}" if abs(value) >= 100 else f"{value:.1f}"


def _wallet_lines(conn) -> list[str]:
    """roster()'s wallets, one line per runtime with a weekly limit."""
    owned = wallets.overview(conn)
    if not owned:
        return []
    lines = ["  Wallets (payday = the weekly reset):"]
    for w in owned:
        line = (
            f"    {w['currency']} ({w['runtime']}): {_units(w['left'])} of {_units(w['share'])} left "
            f"until payday, {_fmt_epoch(w['payday'])}"
        )
        if w["window"] is not None:
            line += (
                f"; {_units(w['window_left'])} of {_units(w['window'])} left in the current "
                f"window, until {_fmt_epoch(w['next_window'])}"
            )
        lines.append(line)
    return lines


def _price_list_lines(conn) -> list[str]:
    """roster()'s price list: the owner's note, the two lists' rows with the cells
    joined by ` | `, and the rule about work that is not on them."""
    owner = core.get_owner_name(conn)
    note = (core.get_setting(conn, core.PRICE_LIST_NOTE_SETTING, "") or "").splitlines()
    works = [
        "    " + " | ".join(row[column] for column in core.PRICE_WORKS_COLUMNS)
        for row in core.price_works(conn)
    ]
    own = [
        "    " + " | ".join(row[column] for column in core.PRICE_OWN_COLUMNS)
        for row in core.price_own(conn)
    ]
    return [
        "Price list (typical / with margin):",
        *(f"  {line}" for line in note if line.strip()),
        "  Works you assign:",
        *(works or ["    (empty)"]),
        "  What a lead spends on its own:",
        *(own or ["    (empty)"]),
        f"Work that is not in the price list is not assigned, unless the list is empty or {owner} "
        "has instructed otherwise.",
    ]


def _stage_lines(conn, config, role) -> list[str]:
    """roster()'s stages: name, state and queue for everybody; for the director also
    the preparation command, the reason it broke, a pending operation and free space."""
    lines = []
    listed = stages.overview(conn)
    if listed:
        lines.append("Stages:")
    for s in listed:
        line = f"  {s['name']} [{s['state']}]"
        if s["running"]:
            r = s["running"]
            line += f" running: {r['agent']} for {r['running_for']:.0f}s"
        if s["waiting"]:
            line += f"; waiting: {', '.join(s['waiting'])}"
        if role == "director":
            if s["pending"]:
                line += f"; {s['pending']} pending"
            line += f"; prepare: {s['prepare'] or '(none)'}"
            if s["reason"]:
                line += f"\n    reason: {s['reason']}"
        lines.append(line)
    if role == "director":
        free = stages.free_space(config) / 1e9
        lines.append(f"Free space on the drive holding {config.root}: {free:.1f} GB.")
    return lines


def _h_stage(conn, config, agent_name, role, args) -> str:
    op = args.get("op")
    if op == "create":
        _require(args, "name")
        stages.create(conn, config, args["name"], args.get("prepare") or "", actor=agent_name)
        return (
            f"stage '{args['name']}' is being cloned and prepared. roster() shows when it is "
            "ready, or why it broke."
        )
    if op == "reset":
        _require(args, "name")
        return stages.reset(conn, config, args["name"], actor=agent_name)
    if op == "delete":
        _require(args, "name")
        return stages.delete(conn, config, args["name"], actor=agent_name)
    raise ValueError(f"stage: unknown op '{op}' — expected create, reset, or delete")


def _hire_title(args: dict) -> str:
    """The `title` a hire takes: required, and one line."""
    _require(args, "title")
    title = args["title"].strip()
    if "\n" in title:
        raise ValueError("title is one line, such as 'Economy architect'")
    return title


def _h_agent(conn, config, agent_name, role, args) -> str:
    # A manager's tool. Every op that names an existing agent takes one in the
    # caller's subtree; a hire makes the caller the new agent's manager and
    # makes a lead or an executor, never a director.
    op = args.get("op")
    me = _agent_row(conn, agent_name)
    if op == "hire":
        _require(args, "name", "runtime", "model")
        hired = core.hire(
            conn, config, name=args["name"], runtime=args["runtime"], model=args["model"],
            effort=args.get("effort"), kind="lead" if args.get("lead") else "executor",
            title=_hire_title(args), manager_agent_id=me["id"],
            adopt_workspace_id=args.get("adopt_workspace_id"), actor=agent_name,
        )
        return f"{hired['kind']} '{hired['name']}' hired under you, status={hired['status']}"
    if op == "fire":
        target = _named_agent(conn, args, "agent")
        core.check_in_subtree(conn, me, target, "agent(op=fire)")
        # Through the supervisor, not core.fire directly: what would be
        # destroyed is a process's session, and only the bus can decide that
        # nothing is running on it AND delete the row without letting a turn
        # start in between — it does both under the lock it starts turns with.
        # This call can wait while a turn is being spawned; every handler
        # here runs in a threadpool (on_call_tool).
        result = _fire_hook(target["name"], agent_name)
        if not result.get("ok"):
            raise ValueError(result.get("reason", "could not fire"))
        return f"agent '{target['name']}' fired"
    if op == "save_profile":
        _require(args, "name", "runtime", "model")
        profile = core.save_profile(
            conn, args["name"], runtime=args["runtime"], model=args["model"], effort=args.get("effort"),
        )
        return f"profile '{profile['name']}' saved ({profile['runtime']}/{profile['model']})"
    if op == "hire_from_profile":
        _require(args, "profile", "name")
        profile = core.get_profile(conn, args["profile"])
        if profile is None:
            raise ValueError(f"no such profile '{args['profile']}'")
        hired = core.hire(
            conn, config, name=args["name"], runtime=profile["runtime"], model=profile["model"],
            effort=profile["effort"], kind="lead" if args.get("lead") else "executor",
            title=_hire_title(args), manager_agent_id=me["id"],
            adopt_workspace_id=args.get("adopt_workspace_id"), actor=agent_name,
        )
        return (
            f"{hired['kind']} '{hired['name']}' hired under you from profile "
            f"'{profile['name']}', status={hired['status']}"
        )
    if op == "list_profiles":
        profiles = core.list_profiles(conn)
        if not profiles:
            return "no saved profiles"
        return "\n".join(f"profile '{p['name']}': {p['runtime']}/{p['model']} (effort={p['effort']})" for p in profiles)
    if op == "instruct":
        target = _named_agent(conn, args, "agent")
        if "text" not in args:
            raise ValueError("missing required argument(s): text — empty text clears them")
        core.check_in_subtree(conn, me, target, "agent(op=instruct)")
        if not core.set_instructions(conn, target["id"], args["text"], actor=agent_name):
            return f"'{target['name']}'s standing instructions already read exactly that; nothing changed"
        done = "cleared" if not args["text"].strip() else "replaced"
        return (
            f"'{target['name']}'s standing instructions are {done}. It gets a quiet line saying so, "
            "which starts no turn."
        )
    if op == "instructions":
        target = _named_agent(conn, args, "agent")
        core.check_in_subtree(conn, me, target, "agent(op=instructions)")
        if target["instructions"] is None:
            return f"'{target['name']}' has no standing instructions"
        return f"'{target['name']}'s standing instructions:\n{target['instructions']}"
    if op == "move":
        target = _named_agent(conn, args, "agent")
        manager = _named_agent(conn, args, "manager")
        core.move_agent(conn, target["id"], manager["id"], actor=agent_name)
        return (
            f"'{target['name']}' now reports to '{manager['name']}', with everyone under it. "
            f"{target['name']} gets a quiet line saying so, which starts no turn; nobody else is told."
        )
    if op == "compact":
        # Runs now or not at all. Nothing is queued and nothing is retried:
        # compaction is claude-only and possible only while the agent is
        # free, both of which are the runtimes' own limits.
        #
        # Nobody compacts ITSELF through this tool: this call is being made
        # from inside the very turn that would have to be over first.
        _require(args, "agent")
        if args["agent"] == agent_name:
            raise ValueError(
                "you cannot compact your own session. "
                + _ask_for_yourself(conn, me, "the compact button on the main page")
            )
        target = _named_agent(conn, args, "agent")
        core.check_in_subtree(conn, me, target, "agent(op=compact)")
        result = _compact_hook(target["name"], agent_name)
        if not result.get("ok"):
            raise ValueError(result.get("reason", "could not compact"))
        return (
            f"'{target['name']}' compacted: {result['before']} -> {result['after']} tokens "
            f"({result['freed']} freed). The session and its id are unchanged."
        )
    if op == "new_session":
        # One step, and the whole of it: the conversation goes, the workspace,
        # its branch and its working tree stay. The next turn finds no
        # session to resume and starts from the full state snapshot. This is
        # the answer to an executor whose window is filling — compact is the
        # other one, and a different mechanism: it KEEPS the conversation and
        # has the vendor summarize it.
        target = _named_agent(conn, args, "agent")
        core.check_in_subtree(conn, me, target, "agent(op=new_session)")
        result = _new_session_hook(target["name"], agent_name)
        if not result.get("ok"):
            raise ValueError(result.get("reason", "could not drop the session"))
        return (
            f"'{target['name']}' starts a fresh session on its next turn; its workspace is "
            "untouched."
        )
    if op == "stop":
        # The only thing in the system that can end a turn early. The office
        # has no wall-clock limit, no silence watchdog and no context
        # threshold, on purpose (office/process.py). This is that decision,
        # and a manager above the agent is positioned to make it: it can see
        # the last-output age the same way the monitor does.
        #
        # You cannot stop your own turn: this call is being made from inside
        # it.
        _require(args, "agent")
        if args["agent"] == agent_name:
            raise ValueError(
                "you cannot stop your own turn. "
                + _ask_for_yourself(conn, me, "the stop button beside your chat on the main page")
            )
        target = _named_agent(conn, args, "agent")
        core.check_in_subtree(conn, me, target, "agent(op=stop)")
        result = _stop_hook(target["name"], agent_name)
        if not result.get("ok"):
            raise ValueError(result.get("reason", "could not stop"))
        if result.get("work_id") is None:
            return f"'{target['name']}' stopped; it had no active work to fail"
        return (
            f"'{target['name']}' stopped: work {result['work_id']} is failed with reason 'killed'. "
            f"work(op=show, work={result['work_id']}) shows its output tail."
        )
    raise ValueError(
        f"agent: unknown op '{op}' — expected hire, fire, stop, compact, new_session, instruct, "
        "instructions, move, save_profile, hire_from_profile, or list_profiles"
    )


def _ask_for_yourself(conn, me: dict, button: str) -> str:
    """Who can do to `me` what it cannot do to itself: its manager, or for the
    director the owner with `button`."""
    manager = core.manager_name(conn, me["id"])
    if manager is not None:
        return f"Ask {manager}, who can do it for you."
    return f"Ask {core.get_owner_name(conn)} to use {button}."


def _one_of(name: str, value, allowed: list[str]) -> None:
    """Refuse a value that is not one of `allowed`, naming both."""
    if value not in allowed:
        raise ValueError(
            f"{name} '{value}' is not in effect — assign again with one of: {', '.join(allowed)}"
        )


def _h_assign(conn, config, agent_name, role, args) -> str:
    # task is optional: filing a kanban card is not a precondition for
    # giving someone a job.
    _require(args, "brief", "branch")
    target = _named_agent(conn, args, "agent")
    if target["kind"] == "executor":
        _require(args, "role", "complexity")
        _one_of("role", args["role"], core.work_roles(conn))
        _one_of("complexity", args["complexity"], core.work_complexities(conn))
    elif args.get("role") is not None or args.get("complexity") is not None:
        raise ValueError(
            f"'{target['name']}' is a {target['kind']}: a work for a {target['kind']} takes no "
            "role or complexity — assign again without them"
        )
    work = core.assign_work(
        conn, agent_id=target["id"], brief=args["brief"], task_id=args.get("task_id"), branch=args["branch"],
        role=args.get("role"), complexity=args.get("complexity"), actor=agent_name,
    )
    pending = " (workspace still provisioning — it attaches once ready)" if work["workspace_id"] is None else ""
    if target["name"] == agent_name:
        return f"work {work['id']} assigned to you on branch '{work['branch']}'; nothing was sent."
    return (
        f"work {work['id']} assigned to {target['name']} on branch '{work['branch']}'{pending}; "
        "the brief has gone to them as a message from you."
    )


def _h_work_reassign(conn, config, agent_name, role, args) -> str:
    _require(args, "work", "workspace")
    target = _named_agent(conn, args, "to_agent")
    work = core.reassign_work(
        conn, config, args["work"], target["id"], workspace=args["workspace"], actor=agent_name
    )
    return (
        f"work {work['id']} reassigned to {target['name']} ({args['workspace']} workspace); you "
        "are its assigner. Nobody was woken — write to whoever should act."
    )


def _h_work_close(conn, config, agent_name, role, args) -> str:
    """A manager's, and the other half of an assignee's work(op=finish).

    A work ends when a manager with reach over it says it ends, on a report it
    accepts or on a decision that the rest belongs to a later stage. `work` is
    the tool for the work an agent is DOING.

    Deliberately its own word next to work_dismiss: accepting delivered work and
    writing off a corpse are different acts. core.close_work() refuses a
    `failed` work and names work_dismiss in the refusal.
    """
    _require(args, "work")
    work = core.close_work(conn, args["work"], summary=args.get("summary"), actor=agent_name)
    where = f" Its result is on task {work['task_id']}." if args.get("summary") and work["task_id"] else ""
    assignee = _name_of(conn, work["agent_id"])
    if assignee == agent_name:
        return f"work {work['id']} closed.{where}"
    return (
        f"work {work['id']} closed.{where} {assignee} is told with its next turn, not woken — "
        "write to them if it changes what they have going now."
    )


def _h_work_dismiss(conn, config, agent_name, role, args) -> str:
    """A manager's, and deliberately not an op on the `work` tool.

    `work` is an agent's own work — show it, finish it. A failed work stands
    until a manager with reach over it says it has read it.
    """
    _require(args, "work")
    work = core.dismiss_work(conn, args["work"], actor=agent_name)
    return f"work {work['id']} written off (failed: {work['fail_reason']})."


_SHARED_TOOL_NAMES = (
    "say", "chat", "remind", "expect", "task", "work", "pr", "note", "ticket", "run", "roster",
)
# roster is shared and answers differently by role (_h_roster). It is the only
# place any of that is said — none of it is printed into a system prompt.
_MANAGER_ROLES = ("director", "lead")
_MANAGER_TOOL_NAMES = ("agent", "assign", "work_close", "work_reassign", "work_dismiss")
_DIRECTOR_TOOL_NAMES = ("stage",)


def _tool_names_for(role: str) -> list[str]:
    names = list(_SHARED_TOOL_NAMES)
    if role in _MANAGER_ROLES:
        names += _MANAGER_TOOL_NAMES
    if role == "director":
        names += _DIRECTOR_TOOL_NAMES
    return names

_HANDLERS = {
    "say": _h_say,
    "chat": _h_chat,
    "remind": _h_remind,
    "expect": _h_expect,
    "task": _h_task,
    "work": _h_work,
    "pr": _h_pr,
    "note": _h_note,
    "ticket": _h_ticket,
    "run": _h_run,
    "roster": _h_roster,
    "agent": _h_agent,
    "assign": _h_assign,
    "work_close": _h_work_close,
    "work_reassign": _h_work_reassign,
    "work_dismiss": _h_work_dismiss,
    "stage": _h_stage,
}


# --------------------------------------------------------------------------- schemas
#
# Compound op-based tools — few tools, each with a richer argument object,
# rather than one tool per operation.

_TOOLS: dict[str, types.Tool] = {
    "say": types.Tool(
        name="say",
        description="Send a message. to='all' posts to the common chat. Everyone gets it with "
        "their next turn. It wakes nobody. Never use it to ask for something. Any other name "
        "is a direct message. It gives that participant a turn. It reaches them, or the office "
        "tells you it did not. Nothing is sent again.",
        input_schema={
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "A participant's name, or 'all' for the common chat."},
                "text": {"type": "string"},
            },
            "required": ["to", "text"],
        },
    ),
    "chat": types.Tool(
        name="chat",
        description="The common chat, newest page first. It tells you what was said. It does "
        "not tell you whether something happened: messages come to you on their own. "
        "before_id, the number in brackets on a line, goes further back.",
        input_schema={
            "type": "object",
            "properties": {
                "before_id": {
                    "type": "integer",
                    "description": "Optional. Shows the page just older than the line with this number.",
                },
            },
        },
    ),
    "remind": types.Tool(
        name="remind",
        description="Reminders: set | cancel. set sets a reminder. It sends text to `to` in "
        "in_seconds, as a message from you. It wakes the addressee when it arrives. 'all' is "
        "not a recipient here. set answers with the reminder's number. cancel cancels a "
        "reminder of yours that has not gone yet, by reminder_id. roster() lists yours with "
        "their numbers. Cancelling tells nobody.",
        input_schema={
            "type": "object",
            "properties": {
                "op": {"type": "string", "enum": ["set", "cancel"]},
                "to": {
                    "type": "string",
                    "description": "Required for set. A participant's name. Your own is allowed.",
                },
                "text": {"type": "string", "description": "Required for set."},
                "in_seconds": {
                    "type": "integer",
                    "description": "Required for set. Seconds from now: 0 to "
                    f"{core.MAX_WAKE_SECONDS} (seven days).",
                },
                "reminder_id": {
                    "type": "integer",
                    "description": "Required for cancel. The number roster() shows against a "
                    "reminder you set.",
                },
            },
            "required": ["op"],
        },
    ),
    "expect": types.Tool(
        name="expect",
        description="An address a local service notifies when a long job ends. Use it for a "
        "job that takes longer than a few minutes. Wait for a shorter job inside your turn. "
        "Give the service the address and end your turn. What the service sends there reaches "
        "you as a direct message from hook:<service>, headed with `about`. If nothing arrives "
        "within within_seconds, the address closes and you are told.",
        input_schema={
            "type": "object",
            "properties": {
                "about": {
                    "type": "string",
                    "description": "What you are waiting for: the job or the batch.",
                },
                "within_seconds": {
                    "type": "integer",
                    "description": f"How long to wait, in seconds: 0 to {core.MAX_WAKE_SECONDS}.",
                },
            },
            "required": ["about", "within_seconds"],
        },
    ),
    "task": types.Tool(
        name="task",
        description="The board of kanban tasks: list | read | create | update | move | link. "
        "A task may sit under a parent task. A task with no parent is top-level. Works are the "
        "assignments made on a task. Each work that is closed adds its result line to the task. "
        "list prints one line per task: id, status, title, the counts of its children (open / "
        "done; a cancelled child counts in neither), and the dependencies that are not done. "
        "With no parent and no query, list gives the top-level tasks. With parent, it gives "
        "that task's children. query is a regular expression, matched against titles, case "
        "ignored. query alone searches the whole board. query with parent searches everything "
        "under that task, at any depth. status keeps one status, or all. Without status, done "
        "and cancelled tasks are left out. A long answer is cut. Its last line says how many "
        "tasks were left out. "
        "read gives one task in full: body, result, the chain of parents up to the top, the "
        "children, the dependencies both ways, the works on it and the tickets linked to it. "
        "create takes an optional parent. "
        "update with parent moves a task under another task. parent=0 moves it to the top "
        "level. A task cannot go under itself or under a task inside it. "
        "A parent's status does not follow its children's. "
        "move with status='done' needs result and closes the task with that line. The works "
        "still open on the task are deleted with it. Their assignees are not told. It is "
        "refused while a work on the task has reported and waits to be closed. It is also "
        "refused while a work on the task is one you could not close with work_close: one you "
        "did not assign, whose assignee is not under you. "
        "move with status='cancelled' is the director's and the leads'. It needs result: the "
        "reason. It is refused while any work is on the task. A cancelled task still holds up "
        "the tasks that depend on it. "
        "link records that task_id depends on depends_on: depends_on must finish first. "
        "ticket(op=link) attaches a ticket to a task.",
        input_schema={
            "type": "object",
            "properties": {
                "op": {"type": "string", "enum": ["list", "read", "create", "update", "move", "link"]},
                "task_id": {
                    "type": "integer",
                    "description": "Required for read, update, move and link. For link, the dependent task.",
                },
                "title": {"type": "string"},
                "body": {"type": "string"},
                "parent": {
                    "type": "integer",
                    "description": "For list: the task whose children to list. Optional for create: the "
                    "task to file it under. For update: the task to move it under; 0 for the top level.",
                },
                "query": {
                    "type": "string",
                    "description": "For list: a regular expression matched against titles, case ignored.",
                },
                "status": {
                    "type": "string",
                    "enum": [
                        "idea", "planned", "needs_clarification", "in_progress", "paused", "done",
                        "cancelled", "all",
                    ],
                    "description": "Required for move. Optional for create; the default is idea. "
                    "Optional for list: one status, or all. Without it, list shows every status but "
                    "done and cancelled.",
                },
                "position": {"type": "integer", "description": "Optional for move: the order within the column."},
                "result": {
                    "type": "string",
                    "description": "One line. Required for move to done (the outcome) and to "
                    "cancelled (the reason).",
                },
                "depends_on": {"type": "integer", "description": "Required for link: the task that must finish first."},
                "remove": {"type": "boolean", "description": "Optional for link: true drops the dependency instead of adding it."},
            },
            "required": ["op"],
        },
    ),
    "work": types.Tool(
        name="work",
        description="Your own current work: show | finish. "
        "show gives the brief you were assigned, the branch to do it on, who assigned it and "
        "its status. show with work=<id> gives one work in full: assignee, assigner, branch, "
        "status, why it failed or paused, the brief and its stored output tail, for any work. "
        "finish REPORTS your work. With pr, it first publishes your branch and then opens the "
        "PR in the same call. If a PR from that branch into the same target is already open, "
        "finish updates that PR instead of opening a second one: its title, and its "
        "description when you pass a body. Without pr, nothing is published. The summary goes to whoever assigned the work, as "
        "your own message, and wakes them. Do not write to them separately. "
        "You do not close your own work. Whoever assigned it closes it. If they send it back, "
        "it is open again: carry on and finish it again. A work you assigned yourself is "
        "reported to nobody. Close it yourself with work_close.",
        input_schema={
            "type": "object",
            "properties": {
                "op": {"type": "string", "enum": ["show", "finish"]},
                "work": {
                    "type": "integer",
                    "description": "Optional for show: the id of any work to read in full. "
                    "Without it, show gives your own work.",
                },
                "summary": {
                    "type": "string",
                    "description": "Required for finish: your report. It goes to whoever assigned "
                    "the work, as a message from you.",
                },
                "pr": {
                    "type": "object",
                    "description": "Optional for finish: open a PR in the same call, or update the "
                    "one already open from this branch.",
                    "properties": {
                        "title": {"type": "string"},
                        "body": {"type": "string"},
                        "target_branch": {"type": "string"},
                    },
                    "required": ["title", "target_branch"],
                },
            },
            "required": ["op"],
        },
    ),
    "pr": types.Tool(
        name="pr",
        description="Pull requests: create | comment | merge | close | list | read. A PR is always "
        "against the superproject. It comes from your own workspace and its current branch. "
        "create publishes that branch itself. It never publishes the main branch. If a PR from "
        "that branch into the same target is already open, create publishes the branch again "
        "and answers with that PR. It does not open a second one. That is how you publish a "
        "branch again. The PR's title becomes the one you pass, and its description too when "
        "you pass a body. "
        "merge and close are the director's and the leads'. They work on a PR you opened, or on "
        "one an agent under you opened. merge takes a branch only when the branch already contains its "
        "target. If it does not, the branch must take its target in first, in the workspace it "
        "comes from, and be published again with create. merge requires delete_branch. close "
        "withdraws the PR unmerged and touches no branch. "
        "Opening, commenting on, merging and closing a PR sends nobody anything. say() whoever "
        "has to act on it. "
        "list shows the open PRs, each with a comment count. read gives one PR with the text of "
        "every comment on it.",
        input_schema={
            "type": "object",
            "properties": {
                "op": {
                    "type": "string",
                    "enum": ["create", "comment", "merge", "close", "list", "read"],
                },
                "pr_id": {
                    "type": "integer",
                    "description": "Required for comment, merge, close, read.",
                },
                "title": {"type": "string", "description": "Required for create."},
                "body": {"type": "string"},
                "target_branch": {"type": "string", "description": "Required for create."},
                "comment": {"type": "string", "description": "Required for comment."},
                "delete_branch": {
                    "type": "boolean",
                    "description": "Required for merge. true takes the source branch out of the "
                    "office repository once the merge is in. false leaves it standing.",
                },
            },
            "required": ["op"],
        },
    ),
    "note": types.Tool(
        name="note",
        description="The knowledge base. "
        "kind=wiki is the team's shared wiki of versioned markdown pages: list | read | comment "
        "| write | undo | delete. Every page is a file in your sandbox: wiki/<path>.md. The "
        "file is brought to the page's current version at the start of each of your turns and "
        "whenever you read that page. A file you have edited is left as it is. Read and search "
        "the pages there with your own file tools. "
        "list gives each page's path, category, title, version and comment count. "
        "read gives one page's version, who wrote it last, the state of your file and every "
        "comment on it. A file you deleted comes back as the page. "
        "write publishes your file of the page at path. A page that does not exist yet needs "
        "category and title. For an existing page, they rename it or re-file it. If the page "
        "has changed since your file's version, write merges the two. Where both changed the "
        "same lines, write publishes nothing. It marks those places in your file and says how "
        "many there are. Settle them, remove the markers and write again. "
        "comment takes path and comment. The page's version is recorded with the comment. "
        "Nobody is told: say() whoever has to act on it. "
        "undo puts the page back to the body it had before its last write. It goes back one "
        "step only, and it is itself a write. "
        "delete removes a page. It is refused if the page has changed since your file's "
        "version. "
        "kind=rule is the flat list of standing statements the whole office works under. A "
        "rule is a short title plus a paragraph. list gives every rule as it stands now, as "
        "'- [rule_id] Title: text'. Your system prompt carries the rules as they stood when "
        "your session began. Every change since then reached you as a line from the office. "
        "create | update | delete are **the director's alone**. create takes title and text. "
        "The title is a name ('No build step', 'Branch naming'), not a summary. update takes "
        "rule_id and text, and title only to rename the rule.",
        input_schema={
            "type": "object",
            "properties": {
                "op": {
                    "type": "string",
                    "enum": ["list", "read", "comment", "write", "undo", "create", "update", "delete"],
                },
                "kind": {"type": "string", "enum": ["wiki", "rule"]},
                "path": {
                    "type": "string",
                    "description": "Wiki page path, e.g. 'runbooks/deploy'. Its file is "
                    "wiki/runbooks/deploy.md in your sandbox. Required for read, comment, write, "
                    "undo, delete.",
                },
                "comment": {"type": "string", "description": "Wiki only. The remark. Required for comment."},
                "category": {
                    "type": "string",
                    "description": "Wiki: required to write a page that does not exist yet. For "
                    "an existing page, a new value re-files it.",
                },
                "title": {
                    "type": "string",
                    "description": "Wiki: required to write a page that does not exist yet. For "
                    "an existing page, a new value renames it. Rule: required for create. On "
                    "update, only to rename the rule.",
                },
                "text": {"type": "string", "description": "Rule only: the rule's paragraph."},
                "rule_id": {
                    "type": "integer",
                    "description": "Rule only. Required for update and delete: the [n] that "
                    "note(op=list, kind=rule) shows against each rule.",
                },
            },
            "required": ["op", "kind"],
        },
    ),
    "ticket": types.Tool(
        name="ticket",
        description="Tickets: create | comment | resolve | list | read | link. A ticket is a "
        "decision that waits on its addressee. Only the addressee or the human owner may "
        "resolve a ticket. link sets which task the ticket is about. A ticket wakes nobody and "
        "sends nothing. say() the addressee if it needs attention now. list is the index of the "
        "open tickets, unless status says otherwise: ids, statuses and titles, no bodies. read "
        "gives one ticket in full: its body, its resolution if it has one, and the text of "
        "every comment.",
        input_schema={
            "type": "object",
            "properties": {
                "op": {"type": "string", "enum": ["create", "comment", "resolve", "list", "read", "link"]},
                "ticket_id": {"type": "integer", "description": "Required for comment, resolve, read, link."},
                "title": {"type": "string", "description": "Required for create."},
                "body": {"type": "string"},
                "addressee": {
                    "type": "string",
                    "description": "Required for create: who must resolve it. Optional for list: a filter.",
                },
                "kind": {"type": "string", "description": "Required for create, e.g. 'question' or 'bug'."},
                "task_id": {"type": "integer", "description": "Optional for create. Required for link."},
                "comment": {"type": "string", "description": "Required for comment."},
                "resolution": {"type": "string", "description": "Required for resolve."},
                "status": {"type": "string", "enum": ["open", "resolved", "all"], "description": "Optional for list: open (the default), resolved or all."},
            },
            "required": ["op"],
        },
    ),
    "run": types.Tool(
        name="run",
        description="Run a command in your workspace under a deadline: start | wait | stop | "
        "list. It is for TESTS AND TRIAL RUNS. It is not a general shell: read files, edit "
        "them and run ordinary quick commands with your own tools. "
        "start runs the command and waits up to 150 seconds. If the command finished, you get "
        "its exit code and its output. If not, you get the output so far, how long ago the "
        "last line came, and a handle. THE DEADLINE KILLS NOTHING: the command is still "
        "running. op=wait waits on the handle again. op=stop kills it. op=list gives back your "
        "own handles and what each one is doing. The whole output is in a file the result "
        "names. "
        "stage runs the command on a stage instead of in your workspace. A stage is a working "
        "tree the office keeps prepared. The team uses it one run at a time. roster() lists "
        "the stages. What runs there is your working tree as it stands at this call: your "
        "commits, your uncommitted changes, and your untracked files that are not ignored. A "
        "run waiting for its turn counts as running. Your workspace can have one run on each "
        "stage. Files the command writes into the folder named by the OFFICE_ARTIFACTS "
        "environment variable are kept until your workspace's next run on that stage. What the "
        "command changed in the stage's tree comes back in the result as a commit, large files "
        "included, with the commands that bring it into your working copy.",
        input_schema={
            "type": "object",
            "properties": {
                "op": {"type": "string", "enum": ["start", "wait", "stop", "list"]},
                "command": {
                    "type": "string",
                    "description": "Required for start. One shell command line, run in your "
                    "workspace or on the stage (PowerShell on Windows).",
                },
                "stage": {
                    "type": "string",
                    "description": "Optional for start: the name of the stage to run the command "
                    "on, instead of your workspace.",
                },
                "handle": {
                    "type": "string",
                    "description": "Required for wait and stop. op=list gives the handles back "
                    "if you no longer have them.",
                },
                "seconds": {
                    "type": "number",
                    "maximum": 150,
                    "description": "Optional: wait fewer than 150 seconds this time. 150 is the "
                    "default and the ceiling. A longer wait is refused.",
                },
            },
            "required": ["op"],
        },
    ),
    "roster": types.Tool(
        name="roster",
        description="The team as it stands now. Everyone gets: the whole team as a tree, each "
        "agent under its manager, with its title; your own standing instructions as they "
        "stand now; your workspace and sandbox paths; the stages, each with its state, who is "
        "running on it and who is waiting; the reminders and open expectations you set. "
        "The director and a lead also get, for every agent under them: its status, how full "
        "its context window is, and the reminders and expectations it set. They also get: "
        "every work not finished (running, paused, reported or failed) whose assignee is under "
        "them or which they assigned, with who assigned it, why it stopped and, for a pause, "
        "the earliest it can resume; remaining quota per runtime, with its reset time; per "
        "runtime, its wallet in the runtime's currency (CL claude, CD codex, GM agy): what the team "
        "has left of its weekly share until payday and of the current 5-hour window; the owner's "
        "price list; the roles and the complexities in effect for assign; each runtime and "
        "model held for quota, with the time before which no turn starts on it; "
        "tasks that look ready but whose dependency is not done; the model catalogue; "
        "workspaces that belong to nobody. "
        "The director also gets: how each stage is prepared, why a broken one broke, a reset or "
        "delete waiting on a run, the free space on the office's drive, and directories in the "
        "workspace directory that the office did not create.",
        input_schema={"type": "object", "properties": {}},
    ),
    "agent": types.Tool(
        name="agent",
        description="The director's and the leads'. hire | fire | stop | compact | new_session | "
        "instruct | instructions | move | save_profile | hire_from_profile | list_profiles. "
        "Every op that names an existing agent takes an agent under you: anyone whose chain of "
        "managers reaches you, not yourself. "
        "hire makes you the new agent's manager. It takes name, title (one line naming the "
        "job, e.g. 'Economy architect'), runtime, model and, where the runtime has one, "
        "effort. Nothing has a default. lead=true hires a lead, who can hire, assign and "
        "manage under itself. Without it, the hire is an executor. "
        "fire refuses while the agent still has active work (running, paused, or reported and "
        "not closed), while anybody reports to it, while an open PR names it as author, and "
        "while a turn or a compaction is running on its session. Its failed works, the "
        "reminders it set and its open expectations go with it. "
        "instruct replaces the agent's standing instructions with text. Empty text clears "
        "them. The agent gets the change as a quiet line that gives it no turn. Write to it as "
        "well if it should act on them now. instructions shows them. "
        "move puts the agent, with everyone under it, under manager. manager is you or a lead "
        "under you. It cannot be the agent itself or anyone under it. move refuses while a "
        "work in the moved part of the team would be left with an assigner that is neither "
        "its assignee nor above it. The moved agent gets its new manager's name as a quiet "
        "line that gives it no turn. Nobody else is told. "
        "stop kills an agent's turn right now. If the agent had a running work, that work "
        "fails as 'killed'. "
        "save_profile saves a composition (runtime, model, effort) under a name, for every "
        "manager. list_profiles lists the saved profiles. hire_from_profile hires with a saved "
        "one, taking name, title and lead as hire does. "
        "compact has the runtime summarize a session in place. The session and its id stay. "
        "claude only. new_session throws the conversation away and keeps the workspace, branch "
        "and working tree. The agent's next turn starts from a full state snapshot, on every "
        "runtime. compact and new_session are refused while a turn or a compaction is running "
        "on the agent's session, and on yourself.",
        input_schema={
            "type": "object",
            "properties": {
                "op": {
                    "type": "string",
                    "enum": [
                        "hire", "fire", "stop", "compact", "new_session", "instruct",
                        "instructions", "move", "save_profile", "hire_from_profile",
                        "list_profiles",
                    ],
                },
                "name": {"type": "string", "description": "Required for hire and hire_from_profile: the new agent's name. Latin letters, digits, '.', '_' and '-', starting and ending with a letter or a digit. Required for save_profile: the profile's name."},
                "title": {
                    "type": "string",
                    "description": "Required for hire, hire_from_profile: one line naming the new agent's job.",
                },
                "lead": {
                    "type": "boolean",
                    "description": "Optional for hire, hire_from_profile: true hires a lead. Otherwise the hire is an executor.",
                },
                "runtime": {"type": "string", "enum": ["claude", "codex", "agy"], "description": "Required for hire, save_profile."},
                "model": {"type": "string", "description": "Required for hire, save_profile."},
                "effort": {"type": "string", "description": "Optional. It depends on the runtime."},
                "adopt_workspace_id": {
                    "type": "string",
                    "description": "Optional for hire/hire_from_profile: take over a workspace that belongs to nobody, as roster() lists them, instead of a new clone.",
                },
                "agent": {
                    "type": "string",
                    "description": "Required for fire, stop, compact, new_session, instruct, instructions, move: the agent acted on.",
                },
                "text": {
                    "type": "string",
                    "description": "Required for instruct: the standing instructions in full. Empty clears them.",
                },
                "manager": {"type": "string", "description": "Required for move: the new manager."},
                "profile": {"type": "string", "description": "Required for hire_from_profile: the saved profile's name."},
            },
            "required": ["op"],
        },
    ),
    "assign": types.Tool(
        name="assign",
        description="The director's and the leads'. Assign work, on a named branch, to yourself or "
        "to an agent under you. You name the branch. The agent creates it itself. Nothing is "
        "reserved. You are recorded as the work's assigner. A work for an executor takes a role, "
        "one of the roles of the works price list roster() shows (the default roles when that list "
        "is empty), and a complexity, one of the complexities roster() lists as in effect; a work "
        "for a lead or for the director takes neither. "
        "On another agent's work: its report comes to you, and so does a notice if it fails or "
        "pauses. The brief goes to the agent as your own message, with the branch name, and it "
        "starts them. Do not write to them separately. "
        "On a work assigned to yourself: nothing is sent, the report goes to nobody, and a "
        "notice if it fails or pauses goes to your manager (for the director, to the owner's "
        "journal). "
        "assign returns at once, even if the agent is still being set up.",
        input_schema={
            "type": "object",
            "properties": {
                "agent": {"type": "string", "description": "You, or an agent under you."},
                "brief": {
                    "type": "string",
                    "description": "The job. It goes to the agent as a direct message from you.",
                },
                "task_id": {"type": "integer", "description": "The kanban task this work is for. Only assign can set it."},
                "branch": {"type": "string"},
                "role": {
                    "type": "string",
                    "description": "Executors only: one of the roles of the works price list roster() shows.",
                },
                "complexity": {
                    "type": "string",
                    "description": "Executors only: one of the complexities roster() lists as in effect.",
                },
            },
            "required": ["agent", "brief", "branch"],
        },
    ),
    "work_close": types.Tool(
        name="work_close",
        description="The director's and the leads'. Close a work you assigned, or one whose "
        "assignee is under you. The work record goes. The summary you pass becomes its line on "
        "the task it belonged to. An assignee's work(op=finish) is a report, not a close. The "
        "work stands until you close it. Close it when you accept the result, or when you "
        "decide the rest of it belongs to a later job. To send it back instead, work_reassign "
        "it to the same agent with workspace=inherit, and write to them what is missing. A "
        "failed work is refused: work_dismiss writes it off; work_reassign hands it on.",
        input_schema={
            "type": "object",
            "properties": {
                "work": {"type": "integer", "description": "The work's id, as roster() shows it."},
                "summary": {
                    "type": "string",
                    "description": "The accepted result in your own words, one line. It is added "
                    "to the task's result if the work has a task.",
                },
            },
            "required": ["work"],
        },
    ),
    "work_dismiss": types.Tool(
        name="work_dismiss",
        description="The director's and the leads'. Write off a work that has already failed: "
        "one you assigned, or one whose assignee is under you. The work record and its stored "
        "output tail are deleted. Its task is untouched. work(op=show, work=<id>) reads the "
        "tail before it goes. Refused on a work that is open, paused or reported.",
        input_schema={
            "type": "object",
            "properties": {
                "work": {"type": "integer", "description": "The failed work's id, as roster() shows it."},
            },
            "required": ["work"],
        },
    ),
    "work_reassign": types.Tool(
        name="work_reassign",
        description="The director's and the leads'. Use it to: send a reported work back to its "
        "assignee; take up a failed work again; pass a work to another agent under you. "
        "Who: the work is one you assigned, or one whose assignee is under you. The recipient is "
        "an agent under you or the work's own assignee, and has no other open work. "
        "workspace=inherit: the recipient gets the work's workspace as it stands, with its "
        "commits, uncommitted changes and branch. The agent that had that workspace gets the "
        "recipient's former workspace in exchange. To the work's own assignee, which already "
        "holds its workspace, nothing moves. "
        "workspace=fresh: the work starts over. The recipient gets a clean clone on the "
        "project's default branch. The recipient's former workspace belongs to nobody. "
        "Whatever the previous holder had not published stays in that holder's tree. "
        "Refused: while the work's assignee is in a turn; while an agent whose workspace would "
        "change is in a turn (wait for the turn to end, or stop it with agent(op=stop)); while "
        "the recipient is still being set up; when it would change your own workspace (a "
        "work's code reaches your workspace when you fetch its published branch; a work that "
        "lies in your workspace is handed on with workspace=fresh once you have published its "
        "branch); when the work's workspace now belongs to a third agent that has an open work "
        "(use workspace=fresh). "
        "After it: the work is open again and you are its assigner. From then on its report "
        "and its failure and pause notices come to you. Every agent whose workspace changed "
        "gets a quiet line with its new workspace and sandbox paths. The line gives it no "
        "turn. Nobody is sent anything else: write to whoever has to act.",
        input_schema={
            "type": "object",
            "properties": {
                "work": {"type": "integer"},
                "to_agent": {
                    "type": "string",
                    "description": "An agent under you, or the work's own assignee.",
                },
                "workspace": {"type": "string", "enum": ["inherit", "fresh"]},
            },
            "required": ["work", "to_agent", "workspace"],
        },
    ),
    "stage": types.Tool(
        name="stage",
        description="Director only. create | reset | delete a stage. A stage is a working tree "
        "the office owns, cloned from the project and prepared once. Agents run commands on it "
        "one at a time with run(op=start, stage=<name>). "
        "create takes name and prepare, one shell command line that may be empty. It returns "
        "at once. The stage is 'preparing' while the office clones it, puts it on the main "
        "branch's tip and runs prepare in it. Exit code 0 makes it 'ready'. Anything else makes "
        "it 'broken', and roster() shows why. A preparing or broken stage turns runs away. "
        "reset puts the stage back on the main branch's tip and runs prepare again, in the tree "
        "it already has. "
        "delete removes the stage, its tree and its refs. "
        "On a stage with a run in progress, reset and delete happen when that run ends. The "
        "runs waiting behind it are turned away at once. "
        "To start a stage from nothing, delete it and create it again. "
        "Write in the rules or the wiki what each stage is for and which commands run on it.",
        input_schema={
            "type": "object",
            "properties": {
                "op": {"type": "string", "enum": ["create", "reset", "delete"]},
                "name": {
                    "type": "string",
                    "description": "Required. Lowercase letters, digits and hyphens, starting with "
                    "a letter or a digit, at most 40 characters.",
                },
                "prepare": {
                    "type": "string",
                    "description": "For create: one shell command line, run in the stage's tree "
                    "(PowerShell on Windows). Leave it out for a stage that needs no preparation.",
                },
            },
            "required": ["op", "name"],
        },
    ),
}


# --------------------------------------------------------------------------- MCP wiring


def _error_result(message: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=message)], is_error=True)


async def on_list_tools(ctx, params) -> types.ListToolsResult:
    agent_name = _agent_from_ctx(ctx)
    state = ctx.request.app.state
    role = await run_in_threadpool(_role_for, state.db, agent_name)
    return types.ListToolsResult(tools=[_TOOLS[name] for name in _tool_names_for(role)])


async def on_call_tool(ctx, params) -> types.CallToolResult:
    agent_name = _agent_from_ctx(ctx)
    state = ctx.request.app.state
    name = params.name
    args = params.arguments or {}

    handler = _HANDLERS.get(name)
    if handler is None:
        return _error_result(f"unknown tool '{name}'")

    # Before the work and before the identity check: a mistyped argument is
    # the caller's mistake to see.
    try:
        _reject_unknown(name, args)
    except ValueError as exc:
        return _error_result(str(exc))

    # **Identity is the address, checked once, here.** One check for every
    # tool instead of a check inside some of them.
    if await run_in_threadpool(_agent_row, state.db, agent_name) is None:
        return _error_result(
            f"'{agent_name}' is not an agent of this office, so no tool here will run for it."
        )

    role = await run_in_threadpool(_role_for, state.db, agent_name)
    if name not in _tool_names_for(role):
        owners = "the director's" if name in _DIRECTOR_TOOL_NAMES else "the director's and the leads'"
        return _error_result(
            f"'{name}' is {owners}, and {agent_name} is {'a lead' if role == 'lead' else 'an executor'} "
            "— ask your manager for what it would do"
        )

    try:
        if inspect.iscoroutinefunction(handler):
            # `run` is the only coroutine handler here; it awaits instead of
            # blocking, and sends every blocking step of its own to the
            # threadpool.
            text = await handler(state.db, state.config, agent_name, role, args)
        else:
            # Off the event loop: handlers call core.py, and core.py calls
            # git.py for some ops -- a clone or a merge can take minutes, and
            # this is a single-process hub: a blocked loop stalls delivery
            # for every other agent too.
            text = await run_in_threadpool(handler, state.db, state.config, agent_name, role, args)
    except (ValueError, PermissionError, KeyError) as exc:
        return _error_result(str(exc))
    except Exception as exc:  # noqa: BLE001 - still a readable tool error, never a bare 500
        return _error_result(f"{name} failed: {exc}")

    # _drain_hook is Bus.piggyback -- file I/O (an atomic rename under
    # <ws>/.office/inbox), run off the loop.
    drained = await run_in_threadpool(_drain_hook, agent_name)
    if drained:
        text = f"{text}\n\n{drained}"
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)])


server = Server("office", version="0.1.0", on_list_tools=on_list_tools, on_call_tool=on_call_tool)

# stateless: identity is the URL path, not a negotiated session, and nothing
# here needs resumability. json_response: plain JSON in, JSON out -- no tool
# here streams progress.
session_manager = StreamableHTTPSessionManager(server, json_response=True, stateless=True)
