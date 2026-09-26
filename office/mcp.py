"""MCP server: the agent-facing tool surface."""

from __future__ import annotations

import inspect
import time
from datetime import datetime, timezone

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.concurrency import run_in_threadpool

from office import commands, core, db, stages

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
    """The default of the three seams whose subject is a live session."""
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
    """One message, sent later, waking whoever it is addressed to."""
    _require(args, "to", "text", "in_seconds")
    wake = core.schedule_message(conn, agent_name, args["to"], args["text"], args["in_seconds"])
    return f"queued for {wake['recipient']}, sends {_fmt_stamp(wake['due_at'])}"


def _h_task(conn, config, agent_name, role, args) -> str:
    op = args.get("op")
    if op == "create":
        _require(args, "title")
        task = core.create_task(conn, args["title"], args.get("body"), args.get("status", "idea"), actor=agent_name)
        return f"task {task['id']} created: {task['title']} [{task['status']}]"
    if op == "update":
        _require(args, "task_id")
        task = core.update_task(conn, args["task_id"], title=args.get("title"), body=args.get("body"), actor=agent_name)
        return f"task {task['id']} updated"
    if op == "move":
        _require(args, "task_id", "status")
        if args["status"] == "done":
            _require(args, "result")
            task = core.close_task(conn, args["task_id"], args["result"], actor=agent_name)
            return f"task {task['id']} closed: {task['result']}"
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
    raise ValueError(f"task: unknown op '{op}' — expected create, update, move, or link")


def _h_work(conn, config, agent_name, role, args) -> str:
    """Your own current work: look at it, or finish it."""
    op = args.get("op")
    me = _agent_row(conn, agent_name)

    if op == "show":
        work = core.current_work(conn, me["id"])
        if work is None:
            return "you have no work assigned — the director's assign() is what starts one"
        line = f"work {work['id']} [{work['status']}] on branch '{work['branch']}':\n{work['brief']}"
        if work["status"] == "paused":
            line += f"\nPaused: {work['pause_reason']}"
        elif work["status"] == "done":
            line += (
                "\nYou have already reported this work; the director has not closed it. If they "
                "have asked for more, do it and report again — work(op=finish) replaces what "
                "your last report said."
            )
        elif work["status"] == "failed":
            # A failed work is still the assignment.
            line += (
                f"\nThe turn on this work died ({work['fail_reason']}); the work itself still "
                "stands. Carry on, or tell the director if it cannot be carried on."
            )
        return line

    if op != "finish":
        raise ValueError(f"work: unknown op '{op}' — expected show or finish")

    _require(args, "summary")
    # Whatever work this agent has, in whatever state — the same answer
    # work(op=show) gives.
    work = core.current_work(conn, me["id"])
    if work is None:
        raise ValueError("you have no work to finish — the director's assign() starts one")

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
        note = f" PR {pr['id']} opened ({pr['source_branch']} -> {pr_info['target_branch']})."
        left = _left_behind(published)
        if left:
            note += f" {left}"
            # Onto the report as well, marked as the office's own words inside
            # the executor's text.
            args["summary"] = args["summary"] + "\n\n[office] " + left

    core.finish_work(conn, work["id"], summary=args["summary"], actor=agent_name)
    return (
        f"work {work['id']} reported: {args['summary']}.{note} "
        "Your summary has gone to the director as a message from you. The work stays on the "
        "books until they close it; if they send it back, carry on with it and report again."
    )


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


def _h_pr(conn, config, agent_name, role, args) -> str:
    op = args.get("op")
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
        return (
            f"PR {pr['id']} opened: {pr['title']} ({pr['source_branch']} -> {pr['target_branch']})"
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
        # detail is the whole of what to do about it.
        return f"PR {args['pr_id']} not merged ({result['status']}): {result['detail']}"
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
        if op == "list":
            # The index, never the bodies.
            pages = core.list_wiki_pages(conn)
            if not pages:
                return "wiki is empty"
            return "\n".join(
                f"{p['path']} [{p['category']}] {p['title']} (v{p['version']}, "
                f"{p['comment_count']} comment(s))"
                for p in pages
            )
        if op == "read":
            _require(args, "path")
            page = core.get_wiki_page(conn, args["path"])
            if page is None:
                raise ValueError(f"no wiki page '{args['path']}' — note(op=list, kind=wiki) shows what exists")
            # The version is part of the answer, not decoration: op=write
            # refuses a version it did not agree with.
            lines = [
                f"{page['path']} [{page['category']}] {page['title']} (v{page['version']}, "
                f"last edited by {page['updated_by']})",
                "",
                page["body"],
                "",
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
            # One write op, not "create" and "update": core.note_wiki takes an
            # expected version, and that number is the whole of the distinction —
            # omit it (or 0) and the page must not exist yet, give it and the
            # page must be at exactly that version.
            _require(args, "path", "category", "title", "text")
            page = core.note_wiki(
                conn, path=args["path"], category=args["category"], title=args["title"], body=args["text"],
                updated_by=agent_name, expected_version=args.get("expected_version"),
            )
            return f"wiki '{page['path']}' saved at version {page['version']}"
        if op == "undo":
            _require(args, "path")
            page = core.undo_wiki_page(conn, args["path"], actor=agent_name)
            return (
                f"wiki '{page['path']}' put back to the body it had before its last write, "
                f"saved at version {page['version']}"
            )
        if op == "delete":
            _require(args, "path", "expected_version")
            core.delete_wiki_page(conn, args["path"], args["expected_version"], actor=agent_name)
            return f"wiki '{args['path']}' deleted"
        raise ValueError(
            f"note: unknown op '{op}' for kind=wiki — expected list, read, comment, write, undo, or delete"
        )

    # **Rules are the director's.** They are the one thing an agent writes that
    # lands in everybody else's system prompt, and they announce themselves to
    # nobody: a rule created, changed or deleted here is read by the whole office
    # from its next turn and by the owner only if he opens the page. Every other
    # power one agent has over another — hiring, assigning, moving work — is
    # already director-only (_DIRECTOR_TOOL_NAMES); this one reaches further than
    # any of them. The tool itself stays shared; its other half, the wiki, is
    # everyone's.
    if role != "director":
        raise ValueError(
            "the project's rules are the director's to write — say() them what you think should "
            "stand, or put it in the wiki with note(kind=wiki)"
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
    if op == "delete":
        _require(args, "rule_id")
        core.delete_rule(conn, args["rule_id"], actor=agent_name)
        return f"rule {args['rule_id']} deleted"
    if op in ("list", "read"):
        # Not an omission: every rule is already in your system prompt verbatim.
        # The wiki is the opposite case — it is not in the prompt at all.
        raise ValueError(
            "note: rules are not read through this tool — all of them are already in your system "
            "prompt under project rules. list/read exist for kind=wiki only."
        )
    raise ValueError(f"note: unknown op '{op}' for kind=rule — expected create, update, or delete")


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
        tickets = core.list_tickets(conn, status=args.get("status"), addressee=args.get("addressee"))
        if not tickets:
            return "no tickets"
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
            f"run: {seconds:g}s is over the {commands.DEADLINE_SECONDS:g}s ceiling. That ceiling "
            "is your own runtime's: it destroys an office tool call that takes longer than three "
            "minutes, and the office would never get to answer you at all. Wait again with "
            "op=wait instead — you can do that as many times as you like."
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
            f"handle='{cmd.handle}') kills it and everything it started. Decide which — "
            "how long it has been quiet is evidence, not a verdict."
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
        return "you have not run anything this turn. run(op=start, command=...) starts one."
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


# -- director-only ------------------------------------------------------------


def _fmt_epoch(value) -> str:
    """A stored reset time in the machine's own zone, or "unknown".

    Adapters normalise every vendor's answer to epoch seconds UTC, and store
    NULL when it could not be turned into an instant -- "unknown" is that
    NULL, not a formatting failure.

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

    A wake's whole text is the sender's own and it is not repeated here: the
    roster block is a list of what the office is going to do, and the line is
    there to tell one entry from another.
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
    """Who exists, and — for the director — everything staffing turns on.

    **This is the only place any of it is said.** None of it rides in a system
    prompt. An executor gets the team and stops; there is nothing else here it
    can act on.

    The director's answer also adds the works that DIED — failed ones — and a
    REPORTED work stands on this list until the director closes it.
    """
    snap = core.roster(conn)
    lines = ["Team:"]
    for a in snap["agents"]:
        if role != "director":
            lines.append(f"  {a['name']} ({a['kind']}/{a['runtime']}/{a['model']})")
            continue
        ctx = f"{a['context_used']}/{a['context_limit']}" if a["context_limit"] else "n/a"
        lines.append(f"  {a['name']} ({a['kind']}/{a['runtime']}/{a['model']}) status={a['status']} context={ctx}")
    # Above the role split: this is the one block both roles have business
    # with. An executor's own wakes are the only record anywhere of what it
    # arranged to be told later. What differs is reach — the director sees
    # the office's, an executor sees the ones it set itself.
    wakes = core.scheduled_messages(conn, sender=None if role == "director" else agent_name)
    if wakes:
        lines.append("Pending wakes:")
        for w in wakes:
            lines.append(
                f"  {_fmt_stamp(w['due_at'])}: from {w['sender']} to {w['recipient']} — {_first_line(w['body'])}"
            )
    lines.extend(_stage_lines(conn, config, role))
    if role != "director":
        return "\n".join(lines)
    if snap["works"]:
        # Failures and pauses are here, not only in the message the office
        # sent when they happened. The failure or pause detail is on the
        # line, and for a pause so is the earliest resume time.
        lines.append("Work:")
        for w in snap["works"]:
            # The stored 'running' is printed as "open" — the same word
            # office/bus.py's _WORK_STATE_WORDS renders into the prompt
            # roster as "on". The value in the column means "open and
            # nobody has reported it" and nothing more (office/schema.sql on
            # works.status); whether a turn is actually spinning is derived
            # from the supervisor's live-turn map, which core.roster() has
            # no access to. One word, two grammatical shapes: "on" where a
            # preposition is wanted, "open" where a label is.
            state = "open" if w["status"] == "running" else w["status"]
            line = f"  work {w['id']}: {w['agent_name']} — {w['brief']} [{state}] (branch {w['branch']})"
            if w["status"] == "done":
                # The one line on this list that is an instruction to its reader:
                # a reported work is the director's own move and it is the only
                # participant that can make it.
                line += " reported and waiting on you: work_close it, or write back for more"
            elif w["status"] == "failed":
                line += f" failed: {w['fail_reason']}; output tail on the work record"
            elif w["status"] == "paused":
                line += f" paused: {w['pause_reason']}"
                if w["resume_after"]:
                    line += f", earliest resume {_fmt_epoch(w['resume_after'])}"
            lines.append(line)
    if snap["quota"]:
        # Bucket names are the vendor's own and are printed untranslated: the
        # office does not decide which limit means what.
        lines.append("Quota:")
        for q in snap["quota"]:
            resets = _fmt_epoch(q["reset_time"])
            # The vendor's own two numbers and nothing derived: how much
            # remains, and when it comes back.
            lines.append(
                f"  {q['runtime']} · {q['label']}: {q['remaining_fraction']:.0%} remaining, resets {resets}"
            )
    if snap["blocked_tasks"]:
        lines.append("Blocked (looks ready but its dependency isn't done):")
        for b in snap["blocked_tasks"]:
            lines.append(f"  task {b['id']} '{b['title']}' [{b['status']}] blocked by task {b['blocking_task_id']} '{b['blocking_title']}'")
    # The same renderer core.hire() refuses with.
    catalogue = core.models_catalogue(conn)
    if catalogue:
        lines.append("Models each runtime accepts:")
        lines.append(catalogue)
    strays = core.stray_workspace_dirs(conn, config.ws_dir)
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
            "Hand one to a new agent with agent(op=hire, adopt_workspace_id=<id>) instead of "
            "letting it clone a fresh tree."
        )
    return "\n".join(lines)


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


def _h_agent(conn, config, agent_name, role, args) -> str:
    # Authority split: the human hires/replaces/fires the director; the
    # director hires/replaces/fires executors. This tool is director-only
    # already (_DIRECTOR_TOOL_NAMES); hire() below never takes a `kind`
    # argument at all -- it always creates an executor, never a second
    # director. fire() additionally refuses a director-kind target.
    op = args.get("op")
    if op == "hire":
        _require(args, "name", "runtime", "model")
        hired = core.hire(
            conn, config, name=args["name"], runtime=args["runtime"], model=args["model"],
            effort=args.get("effort"),
            adopt_workspace_id=args.get("adopt_workspace_id"), actor=agent_name,
        )
        return f"agent '{hired['name']}' hired, status={hired['status']}"
    if op == "fire":
        _require(args, "agent")
        target = _agent_row(conn, args["agent"])
        if target is None:
            raise ValueError(f"no such agent '{args['agent']}'")
        if target["kind"] == "director":
            raise PermissionError("only the owner can replace or fire the director")
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
            effort=profile["effort"],
            adopt_workspace_id=args.get("adopt_workspace_id"), actor=agent_name,
        )
        return f"agent '{hired['name']}' hired from profile '{profile['name']}', status={hired['status']}"
    if op == "list_profiles":
        profiles = core.list_profiles(conn)
        if not profiles:
            return "no saved profiles"
        return "\n".join(f"profile '{p['name']}': {p['runtime']}/{p['model']} (effort={p['effort']})" for p in profiles)
    if op == "compact":
        # Runs now or not at all. Nothing is queued and nothing is retried:
        # compaction is claude-only and possible only while the agent is
        # free, both of which are the runtimes' own limits.
        #
        # The director cannot compact ITSELF through this tool: this call is
        # being made from inside the very turn that would have to be over
        # first. The owner's button on the main page is how the director's
        # session gets compacted, between its turns.
        _require(args, "agent")
        if args["agent"] == agent_name:
            raise ValueError(
                "you cannot compact your own session from inside a turn on it — claude "
                "refuses to compact a session a turn is running on, and this call is that "
                f"turn. Ask {core.get_owner_name(conn)} to use the button on the main page."
            )
        target = _agent_row(conn, args["agent"])
        if target is None:
            raise ValueError(f"no such agent '{args['agent']}'")
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
        _require(args, "agent")
        target = _agent_row(conn, args["agent"])
        if target is None:
            raise ValueError(f"no such agent '{args['agent']}'")
        core.start_new_session(conn, target["id"])
        return (
            f"'{target['name']}' will start a fresh session on its next turn. Its workspace, "
            "branch and working tree are untouched; the conversation is gone and the new "
            "session begins from the full state snapshot."
        )
    if op == "stop":
        # The only thing in the system that can end a turn early. The office
        # has no wall-clock limit, no silence watchdog and no context
        # threshold, on purpose (office/process.py). This is that decision,
        # and the director is the one positioned to make it: it can see the
        # last-output age the same way the monitor does.
        #
        # You cannot stop your own turn: this call is being made from inside
        # it. The owner's button in the monitor covers that case.
        _require(args, "agent")
        if args["agent"] == agent_name:
            raise ValueError(
                "you cannot stop your own turn — this call is that turn. "
                f"Ask {core.get_owner_name(conn)} to use the stop button on the works page."
            )
        target = _agent_row(conn, args["agent"])
        if target is None:
            raise ValueError(f"no such agent '{args['agent']}'")
        result = _stop_hook(target["name"], agent_name)
        if not result.get("ok"):
            raise ValueError(result.get("reason", "could not stop"))
        if result.get("work_id") is None:
            return f"'{target['name']}' stopped; it had no active work to fail"
        return (
            f"'{target['name']}' stopped: work {result['work_id']} is failed with reason 'killed'. "
            "Its output tail is on the work record."
        )
    raise ValueError(
        f"agent: unknown op '{op}' — expected hire, fire, stop, save_profile, hire_from_profile, "
        "list_profiles, compact, or new_session"
    )


def _h_assign(conn, config, agent_name, role, args) -> str:
    # task is optional: filing a kanban card is not a precondition for
    # giving someone a job. Link one after the fact with task(op=link) if it
    # turns out to matter.
    _require(args, "agent", "brief", "branch")
    target = _agent_row(conn, args["agent"])
    if target is None:
        raise ValueError(f"no such agent '{args['agent']}'")
    work = core.assign_work(
        conn, agent_id=target["id"], brief=args["brief"], task_id=args.get("task_id"), branch=args["branch"],
        actor=agent_name,
    )
    pending = " (workspace still provisioning — it attaches once ready)" if work["workspace_id"] is None else ""
    return (
        f"work {work['id']} assigned to {target['name']} on branch '{work['branch']}'{pending}. "
        "Your brief has been sent to them as a message from you."
    )


def _h_work_reassign(conn, config, agent_name, role, args) -> str:
    _require(args, "work", "to_agent", "workspace")
    target = _agent_row(conn, args["to_agent"])
    if target is None:
        raise ValueError(f"no such agent '{args['to_agent']}'")
    work = core.reassign_work(
        conn, config, args["work"], target["id"], workspace=args["workspace"], actor=agent_name
    )
    return f"work {work['id']} reassigned to {target['name']} ({args['workspace']} workspace)"


def _h_work_close(conn, config, agent_name, role, args) -> str:
    """Director-only, and the other half of an executor's work(op=finish).

    A work is an obligation the director created; it ends when the director says
    it ends, on a report it accepts or on a decision that the rest belongs to a
    later stage. Nobody closes their own work; `work` is the tool for the work
    an agent is DOING.

    Deliberately its own word next to work_dismiss: accepting delivered work and
    writing off a corpse are different acts. core.close_work() refuses a
    `failed` work and names work_dismiss in the refusal.
    """
    _require(args, "work")
    work = core.close_work(conn, args["work"], summary=args.get("summary"), actor=agent_name)
    where = f" Its result is on task {work['task_id']}." if args.get("summary") and work["task_id"] else ""
    return (
        f"work {work['id']} closed.{where} It is off the books; nobody was told — "
        "if the agent that did it should know, write to them."
    )


def _h_work_dismiss(conn, config, agent_name, role, args) -> str:
    """Director-only, and deliberately not an op on the `work` tool.

    `work` is an agent's own work — show it, finish it. A failed work stands
    until the person who has to act on it says he has read it, and that
    person is the director.
    """
    _require(args, "work")
    work = core.dismiss_work(conn, args["work"], actor=agent_name)
    return (
        f"work {work['id']} written off (it had failed: {work['fail_reason']}). "
        "Its output tail is gone with it; the task, if it had one, is untouched."
    )


_SHARED_TOOL_NAMES = ("say", "chat", "remind", "task", "work", "pr", "note", "ticket", "run", "roster")
# roster is shared and answers differently by role (_h_roster): the team to
# everybody, and to the director everything staffing turns on besides. It is
# the only place any of that is said — none of it is printed into a system
# prompt.
_DIRECTOR_TOOL_NAMES = ("agent", "assign", "work_close", "work_reassign", "work_dismiss", "stage")

_HANDLERS = {
    "say": _h_say,
    "chat": _h_chat,
    "remind": _h_remind,
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
        description="Send a message. Both kinds are delivered — the difference is who pays. "
        "to='all' posts to the common chat: everyone gets it, labelled with your name, injected "
        "into a turn already running and otherwise riding whatever turn that reader takes next. "
        "It wakes nobody, so it is not how you ask for something. Any other name is a direct "
        "message and does buy that participant a turn: it reaches them, or the office tells you "
        "it did not. Nothing is ever re-sent automatically.",
        input_schema={
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Participant name, or 'all' for the common chat."},
                "text": {"type": "string"},
            },
            "required": ["to", "text"],
        },
    ),
    "chat": types.Tool(
        name="chat",
        description="Read back the common chat, newest page first, oldest line of the page at the "
        "top. This is NOT a way to find out whether something happened — messages arrive on "
        "their own and there is nothing to poll; it is how you look at what was said when "
        "somebody refers to it. Common chat only: direct messages are already in this session, "
        "and a session that does not have them is a different conversation. Pass before_id (the "
        "bracketed number on a line) to step further back.",
        input_schema={
            "type": "object",
            "properties": {
                "before_id": {
                    "type": "integer",
                    "description": "Optional: show the page immediately older than this line's number.",
                },
            },
        },
    ),
    "remind": types.Tool(
        name="remind",
        description="Send a message later. The office holds it and sends it when the time comes, "
        "as a message from you — so it reaches whoever it is addressed to and wakes them, exactly "
        "as one sent by hand would. to may be your own name: that is how you arrange to carry on "
        "later. 'all' is not a recipient here — the common chat wakes nobody, so a deferred post "
        "to it would arrive and do nothing. in_seconds counts forward from now, at most 604800 "
        "(seven days).",
        input_schema={
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Participant name — your own included."},
                "text": {"type": "string"},
                "in_seconds": {
                    "type": "integer",
                    "description": "How long from now, in seconds. 0 to 604800 (seven days).",
                },
            },
            "required": ["to", "text", "in_seconds"],
        },
    ),
    "task": types.Tool(
        name="task",
        description="Manage kanban tasks. A task is a card on the board — one piece of the project, "
        "at whatever size the owner asks for. Works are the assignments made against it: a task may "
        "gather several, and each one closed appends its result line to the task. "
        "move with status='done' requires result and closes the task "
        "(its work records collapse into that one line; it is refused while a work on that task "
        "has reported and is waiting to be closed). link records that task_id depends on "
        "depends_on (it must finish first) -- this is what makes 'planned' mean something; pass "
        "remove=true to drop a link. To attach a ticket to a task, use ticket(op=link) instead.",
        input_schema={
            "type": "object",
            "properties": {
                "op": {"type": "string", "enum": ["create", "update", "move", "link"]},
                "task_id": {"type": "integer", "description": "Required for update, move, link (the dependent task)."},
                "title": {"type": "string"},
                "body": {"type": "string"},
                "status": {
                    "type": "string",
                    "enum": ["idea", "planned", "needs_clarification", "in_progress", "paused", "done"],
                    "description": "Required for move.",
                },
                "position": {"type": "integer", "description": "Optional order within the column, for move."},
                "result": {"type": "string", "description": "One-line outcome, required for move to status=done."},
                "depends_on": {"type": "integer", "description": "Required for link: the task that must finish first."},
                "remove": {"type": "boolean", "description": "Optional for link: true drops the dependency instead of adding it."},
            },
            "required": ["op"],
        },
    ),
    "work": types.Tool(
        name="work",
        description="Your own current work: show | finish. show gives the brief you were assigned "
        "and the branch it is to be done on — use it whenever you are no longer certain of "
        "either; the brief came to you as the director's message, and a long conversation can "
        "summarize that message away. finish REPORTS the work and optionally opens a PR for it in "
        "the same call (publishes your branch first); its summary is delivered to the director as "
        "your own message and wakes them, so you do not need to write to them separately. You do "
        "not close your own work — the director does, once they have accepted the report — so if "
        "they send it back, carry on and finish it again.",
        input_schema={
            "type": "object",
            "properties": {
                "op": {"type": "string", "enum": ["show", "finish"]},
                "summary": {
                    "type": "string",
                    "description": "Required for finish. Your report, delivered to the director as "
                    "a message from you, so write it as one; there is no need to send it again "
                    "with say().",
                },
                "pr": {
                    "type": "object",
                    "description": "Optional for finish: open a PR in the same call.",
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
        description="Pull requests: create | comment | merge | close | list | read. PRs are always against "
        "the superproject, always from your own workspace and its current branch -- there is no "
        "separate publish step, create does it for you. merge takes a branch only when it "
        "already contains its target: if it does not, bring the target into your workspace, "
        "settle the conflict there and publish again. merge also decides what becomes of the "
        "source branch and will not run without being told: pass delete_branch=true to take it "
        "out of the office repository once the merge is in, or false to leave it standing. "
        "close is the other ending: the request is "
        "withdrawn unmerged, for work that is not wanted or has been superseded. It touches no "
        "branch -- the commits stay where they are -- and the request is gone, so close one "
        "rather than leaving it standing for somebody to review again. "
        "Opening, commenting on, merging and closing a PR sends nobody "
        "anything -- review comments come in batches, and who needs waking for them is your "
        "call: say() them. list shows what is open with a comment count; read gives one PR with "
        "the text of every comment on it, which is the only place a review can be read at all.",
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
                    "description": "Required for merge: true takes the source branch out of the "
                    "office repository once the merge is in, false leaves it standing.",
                },
            },
            "required": ["op"],
        },
    ),
    "note": types.Tool(
        name="note",
        description="The knowledge base. kind=wiki is the team's single shared wiki — versioned "
        "markdown pages, the same ones the humans see, living only in the office: list | read | "
        "comment | write | undo | delete. There are no wiki files in your workspace; read a page here. "
        "list gives paths, categories, titles, versions and comment counts without bodies, so finding a page is "
        "cheap; read returns one page's body, its version and every comment on it. comment takes a "
        "path and the remark, and is how a page is reviewed or questioned without editing it: the "
        "version you are commenting against is recorded for you, so a later reader can see that a "
        "comment was written about text the page no longer has. Commenting on a page sends nobody "
        "anything — say() whoever should know. write is the one way to save a "
        "page, new or existing — expected_version is what tells them apart: omit it (or 0) for a "
        "page that does not exist yet, otherwise pass the version read just gave you, and a "
        "mismatch is refused rather than overwriting somebody. delete needs it too. So read "
        "immediately before you write. undo takes a path and puts the page back to the body it "
        "had before its last write — one step and one step only, which is the whole of the "
        "wiki's history; it is itself a write, so an undo can be undone. **kind=rule is the "
        "director's alone** and refused for anybody else. It is the flat list of standing statements the whole "
        "office works under, and a rule is a short title plus a paragraph: create | update | delete "
        "only — every rule is already in your system prompt under project rules, rendered "
        "'- [rule_id] Title: text', so there is nothing to read back and nothing to look up to "
        "edit one: the bracketed number there is the rule_id. create takes title and text both: the "
        "title is the line the office refers to that rule by ('No build step', 'Branch naming'), so "
        "write a name, not a summary of the paragraph. update takes rule_id and text, and takes "
        "title only when you mean to rename it — leave it out and the rule keeps the name it has.",
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
                    "description": "Wiki page path, e.g. 'runbooks/deploy'. Required for read, comment, "
                    "write, undo, delete.",
                },
                "comment": {"type": "string", "description": "Wiki only: the remark, required for comment."},
                "category": {"type": "string", "description": "Wiki only: required for write."},
                "title": {
                    "type": "string",
                    "description": "Wiki: page title, required for write. Rule: the short line "
                    "naming what the rule is about — required for create, and on update only to "
                    "rename it.",
                },
                "text": {"type": "string", "description": "Wiki page body, or the rule's paragraph."},
                "expected_version": {
                    "type": "integer",
                    "description": "Wiki only. Omit (or 0) for a page that does not exist yet; "
                    "otherwise the version op=read reported. Required for delete.",
                },
                "rule_id": {
                    "type": "integer",
                    "description": "Rule only: required for update/delete. It is the [n] shown "
                    "against each rule under project rules in your system prompt.",
                },
            },
            "required": ["op", "kind"],
        },
    ),
    "ticket": types.Tool(
        name="ticket",
        description="Tickets gate a decision on the addressee: create | comment | resolve | list | "
        "read | link. Only the addressee or the human owner may resolve a ticket. link "
        "sets/changes which task this ticket is about (tickets.task_id) -- the same thing create's "
        "optional task_id sets. A ticket wakes nobody and sends nothing: it is a gate that waits, "
        "and whether it is worth somebody's attention right now is yours to decide -- say() them if "
        "it is. list is the index: ids, statuses and titles, no bodies. read gives one ticket in "
        "full -- the body it was filed with, its resolution if it has one, and the text of every "
        "comment -- and since a ticket announces nothing to anybody, it is the only way to learn "
        "what a ticket addressed to you actually says.",
        input_schema={
            "type": "object",
            "properties": {
                "op": {"type": "string", "enum": ["create", "comment", "resolve", "list", "read", "link"]},
                "ticket_id": {"type": "integer", "description": "Required for comment, resolve, read, link."},
                "title": {"type": "string", "description": "Required for create."},
                "body": {"type": "string"},
                "addressee": {
                    "type": "string",
                    "description": "Required for create (who must resolve this); optional filter for list.",
                },
                "kind": {"type": "string", "description": "Required for create, e.g. 'question' or 'bug'."},
                "task_id": {"type": "integer", "description": "Optional for create; required for link."},
                "comment": {"type": "string", "description": "Required for comment."},
                "resolution": {"type": "string", "description": "Required for resolve."},
                "status": {"type": "string", "enum": ["open", "resolved"], "description": "Filter, for list."},
            },
            "required": ["op"],
        },
    ),
    "run": types.Tool(
        name="run",
        description="Run a command in your workspace under a deadline, and always get an answer "
        "back. This is for TESTS AND TRIAL RUNS -- things that can hang. It is NOT a general "
        "shell: reading files, editing them and ordinary quick commands stay on your own tools, "
        "where they are faster. start runs the command and waits up to 150 seconds; if it "
        "finished you get its exit code and its output, and if it did not you get the output so "
        "far, how long ago the last line arrived, and a handle. THE DEADLINE KILLS NOTHING -- the "
        "command is still running and the decision is yours: wait on the handle again (op=wait), "
        "as many times as you like, or kill it (op=stop) and report that the run did not finish. "
        "op=list gives you back your own handles and what each one is doing, for when you no "
        "longer have them. Stop whatever you started before your turn ends. The output of every "
        "run is written to a file the result names, so you can grep it -- and the result says so "
        "if that file was cut short too. "
        "stage runs the command on a stage instead of in your workspace: a working tree the "
        "office keeps prepared, which the team uses one run at a time (roster() lists the "
        "stages). What runs there is your working tree as it stands at this call: your commits, "
        "your uncommitted changes and your untracked files that are not ignored. A run waiting "
        "for its turn counts as running, and your workspace can have one run on each stage. Have "
        "the command write the files you want from it into the folder the OFFICE_ARTIFACTS "
        "environment variable names. That folder is emptied when your workspace's next run on "
        "the same stage starts: move out of it whatever must outlive that. What the command "
        "changed in the stage's tree comes back in the result as a commit: bring it into your "
        "working copy with the commands the result gives, before your next run on that stage.",
        input_schema={
            "type": "object",
            "properties": {
                "op": {"type": "string", "enum": ["start", "wait", "stop", "list"]},
                "command": {
                    "type": "string",
                    "description": "Required for start. One shell command line, run in your "
                    "workspace, or on the stage (PowerShell on Windows).",
                },
                "stage": {
                    "type": "string",
                    "description": "Optional for start: the name of the stage to run the command "
                    "on instead of your workspace.",
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
                    "default and the ceiling -- a longer one is refused, because your own "
                    "runtime destroys an office tool call that takes more than three minutes.",
                },
            },
            "required": ["op"],
        },
    ),
    "roster": types.Tool(
        name="roster",
        description="Who is on the team — and, for the director, everything staffing turns on. "
        "**This is the only place any of it is said**: none of it is in your system prompt, "
        "which is fixed when your session is created and knows nothing of anyone hired since. "
        "An executor gets the team and nothing else. The director's answer adds each agent's "
        "context fill, every work that is not finished — running, paused or failed, with the "
        "reason it stopped and, for a pause, the earliest it could resume — remaining quota per "
        "runtime with its reset time, and tasks that look ready but whose dependency is not done. "
        "Everyone gets the stages: each one's state, who is running on it and who is waiting; "
        "the director also gets how each is prepared, why a broken one broke, a reset or delete "
        "waiting on a run, and the free space on the office's drive. "
        "Your prompt carries part of the same picture as it stood at the start of this turn — "
        "call this when you need it current, or when you need the failures, which the prompt "
        "does not carry at all.",
        input_schema={"type": "object", "properties": {}},
    ),
    "agent": types.Tool(
        name="agent",
        description="Director only. hire | fire | stop | save_profile | hire_from_profile | "
        "list_profiles | compact | new_session. hire needs the full composition (no defaults): name, runtime "
        "and model, plus effort where the runtime has one. That is the whole composition — there "
        "is no toolset to choose and no MCP server to attach: which built-in tools an agent "
        "carries is not configurable, and every agent gets the office's own MCP server and only "
        "that one, automatically. An agent cannot be hired under the owner's name, nor under the "
        "office's own name. fire refuses while the agent still has active work, and refuses "
        "while anything is running on its session — a turn, or a compaction: firing removes the "
        "session that process is speaking on, so wait for it to end, or stop the turn first. "
        "stop kills an agent's turn right now and, if it had an active work, fails that "
        "work as 'killed' — the office has no time limit on a turn, so a wedged agent stays "
        "wedged until you decide it is. save_profile remembers a composition by name; hire_from_profile hires "
        "using a saved one. compact has the runtime summarize a session in place, keeping it and "
        "its id — CLAUDE ONLY, and only while that agent is free: claude will not compact a "
        "session a turn is running on, agy cannot compact at all, and codex offers it only "
        "through an app-server this office does not use. It runs immediately and tells you the "
        "before and after token counts; nothing is queued, so a compact refused because the "
        "agent is mid-turn is refused, not remembered — ask again when it is idle. You cannot "
        "compact yourself. new_session throws the conversation away instead and keeps "
        "the workspace, branch and working tree — the next turn starts from a full state "
        "snapshot; it is the only one of the two that works on every runtime.",
        input_schema={
            "type": "object",
            "properties": {
                "op": {
                    "type": "string",
                    "enum": [
                        "hire", "fire", "stop", "save_profile", "hire_from_profile",
                        "list_profiles", "compact", "new_session",
                    ],
                },
                "name": {"type": "string", "description": "Required for hire, save_profile, hire_from_profile (the new agent's name)."},
                "runtime": {"type": "string", "enum": ["claude", "codex", "agy"], "description": "Required for hire, save_profile."},
                "model": {"type": "string", "description": "Required for hire, save_profile."},
                "effort": {"type": "string", "description": "Optional, runtime-dependent."},
                "adopt_workspace_id": {
                    "type": "string",
                    "description": "Optional for hire/hire_from_profile: take over an existing workspace instead of cloning.",
                },
                "agent": {"type": "string", "description": "Required for fire, stop, compact, new_session."},
                "profile": {"type": "string", "description": "Required for hire_from_profile: the saved profile's name."},
            },
            "required": ["op"],
        },
    ),
    "assign": types.Tool(
        name="assign",
        description="Director only. Assign work to an agent on a named branch — the branch is always "
        "yours to name, the office never guesses one. Naming it is all the office does with it: the "
        "branch is recorded in the assignment and the agent creates it in its own workspace with "
        "ordinary git. Nothing is reserved, so say in the brief if the name matters. The brief is "
        "delivered to the agent as your own message, with the branch name, and that is what starts "
        "them — you do not need to write to them separately. task_id is optional and can only be set "
        "here: a work goes onto the board when it is assigned, and there is no way to put one there "
        "afterwards. Returns at once even if the agent is still provisioning.",
        input_schema={
            "type": "object",
            "properties": {
                "agent": {"type": "string"},
                "brief": {
                    "type": "string",
                    "description": "The job, in your own words. Delivered to the agent as a direct "
                    "message from you and nothing else is sent, so write it as the instruction it "
                    "is; there is no need to repeat it with say().",
                },
                "task_id": {"type": "integer", "description": "The kanban task this work is for. Settable only here."},
                "branch": {"type": "string"},
            },
            "required": ["agent", "brief", "branch"],
        },
    ),
    "work_close": types.Tool(
        name="work_close",
        description="Director only. Close a work you assigned: the work record goes and the "
        "summary you pass becomes its line on the task it belonged to. This is how a work ENDS "
        "— an executor's work(op=finish) is a report, not a close, and the work stands on your "
        "roster until you do this. Close it when you accept the result, or when you have decided "
        "the rest of it belongs to a later job. If the report is not good enough, do not close "
        "it: write to the executor and they carry on with the same work. This is not "
        "work_dismiss, which writes off a work that died — and it refuses a failed work for that "
        "reason: a work that died delivered no result and cannot be closed as though it had.",
        input_schema={
            "type": "object",
            "properties": {
                "work": {"type": "integer", "description": "The work's id, as roster() shows it."},
                "summary": {
                    "type": "string",
                    "description": "The accepted result in your own words, one line. Appended to "
                    "the task's result if this work had a task; the only thing that survives the "
                    "work record.",
                },
            },
            "required": ["work"],
        },
    ),
    "work_dismiss": types.Tool(
        name="work_dismiss",
        description="Director only. Write off a work that has already failed: the work record and "
        "its output tail are deleted. This is NOT closing a task — the task, if the work even had "
        "one, keeps its status, its result and its place on the board — and the history of the "
        "death is not lost: it was written to the owner's journal when it happened and stays "
        "there. What goes is the work row, which would otherwise stand in roster() and on the "
        "works page for the life of the office, plus the output tail, which is why you write a "
        "work off after you have read it and not before — the copy the supervisor keeps on disk "
        "(the office's tails/<agent>.log) outlives this call, but only until that agent's next "
        "turn overwrites it. Refused on a work that is running, paused or reported: "
        "stop a running one, reassign or close a paused one, close a reported one (work_close) — "
        "only what has already died can be written off.",
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
        description="Director only. Hand a work to another agent. workspace=inherit keeps the same "
        "working tree and is the only option that carries the work over: commits, uncommitted "
        "changes and the branch the previous agent was standing on all come with it. "
        "workspace=fresh means start this brief again — the new agent gets a clean clone on the "
        "project's default branch, and whatever the previous agent had not published stays behind "
        "in its own tree, out of reach.",
        input_schema={
            "type": "object",
            "properties": {
                "work": {"type": "integer"},
                "to_agent": {"type": "string"},
                "workspace": {"type": "string", "enum": ["inherit", "fresh"]},
            },
            "required": ["work", "to_agent", "workspace"],
        },
    ),
    "stage": types.Tool(
        name="stage",
        description="Director only. create | reset | delete a stage: a working tree the office "
        "owns, cloned from the project and prepared once, on which agents run commands one at a "
        "time with run(op=start, stage=<name>). create takes name and prepare, one shell command "
        "line that may be empty; it returns at once, and the stage is 'preparing' while the "
        "office clones it, puts it on the main branch's tip and runs prepare in it. Exit code 0 "
        "makes it 'ready'; anything else makes it 'broken', and roster() shows why. A preparing "
        "or broken stage turns runs away. reset puts the stage back on the main branch's tip and "
        "runs prepare again, in the tree it already has. delete removes the stage, its tree and "
        "its refs. On a stage with a run in progress, reset and delete happen when that run ends, "
        "and the runs waiting behind it are turned away at once. To start a stage from nothing, "
        "delete it and create it again. Write in the rules or the wiki what each stage is for "
        "and which commands run on it.",
        input_schema={
            "type": "object",
            "properties": {
                "op": {"type": "string", "enum": ["create", "reset", "delete"]},
                "name": {
                    "type": "string",
                    "description": "Required. Lowercase letters, digits and hyphens, starting with "
                    "a letter or digit, at most 40 characters.",
                },
                "prepare": {
                    "type": "string",
                    "description": "For create: one shell command line run in the stage's tree "
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
    names = list(_SHARED_TOOL_NAMES)
    if role == "director":
        names += _DIRECTOR_TOOL_NAMES
    return types.ListToolsResult(tools=[_TOOLS[name] for name in names])


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
    if name in _DIRECTOR_TOOL_NAMES and role != "director":
        return _error_result(f"'{name}' is director-only; {agent_name} is an executor and cannot call it")

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
