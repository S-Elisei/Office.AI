"""The tool surface an agent reaches through the MCP entry points."""

from __future__ import annotations

import asyncio
import subprocess

import mcp.types as types
from mcp.server.context import ServerRequestContext
from starlette.applications import Starlette
from starlette.requests import Request

from office import core, db
from office import mcp as office_mcp
from office import wiki_files

#: An argument no schema declares, and a name no registry holds.
_UNDECLARED = "not_a_declared_argument"
_NO_SUCH_TOOL = "no-tool-answers-to-this"


def add_agent(conn, name, kind, manager=None):
    with db.transaction(conn) as c:
        c.execute(
            "INSERT INTO agents (name, kind, manager_agent_id, runtime, model, status) "
            "VALUES (?, ?, (SELECT id FROM agents WHERE name = ?), 'claude', 'stub-model', 'idle')",
            (name, kind, manager),
        )


def agent_row(conn, name):
    return db.query_one(conn, "SELECT * FROM agents WHERE name = ?", (name,))


def _context(conn, config, agent):
    """The context the MCP transport builds for a call on `/mcp/{agent}`."""
    app = Starlette()
    app.state.db = conn
    app.state.config = config
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": f"/mcp/{agent}",
        "raw_path": f"/mcp/{agent}".encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 1),
        "server": ("127.0.0.1", 80),
        "app": app,
        "path_params": {"agent": agent},
    }
    return ServerRequestContext(
        session=None,
        lifespan_context={},
        protocol_version=types.LATEST_PROTOCOL_VERSION,
        method="tools/call",
        request=Request(scope),
    )


def offered_to(conn, config, agent) -> list[str]:
    result = asyncio.run(
        office_mcp.on_list_tools(
            _context(conn, config, agent), types.PaginatedRequestParams()
        )
    )
    return [tool.name for tool in result.tools]


def answer_to(conn, config, agent, name, arguments) -> tuple[bool, str]:
    result = asyncio.run(
        office_mcp.on_call_tool(
            _context(conn, config, agent),
            types.CallToolRequestParams(name=name, arguments=arguments),
        )
    )
    return bool(result.is_error), "".join(
        block.text for block in result.content if isinstance(block, types.TextContent)
    )


def row_counts(conn) -> dict[str, int]:
    tables = [
        row["name"]
        for row in db.query(conn, "SELECT name FROM sqlite_master WHERE type = 'table'")
    ]
    return {
        name: db.query_one(conn, f"SELECT COUNT(*) AS n FROM {name}")["n"]
        for name in tables
    }


def test_every_tool_an_executor_is_offered_answers_for_its_own_arguments(conn, config):
    add_agent(conn, "hand", "executor")
    offered = offered_to(conn, config, "hand")
    assert offered

    before = row_counts(conn)
    for name in offered:
        failed, text = answer_to(conn, config, "hand", name, {_UNDECLARED: "x"})
        assert failed, name
        assert _UNDECLARED in text, (name, text)
    assert row_counts(conn) == before


def test_what_a_role_is_offered_beyond_the_role_below_it_is_refused_to_that_role(conn, config):
    add_agent(conn, "boss", "director")
    add_agent(conn, "chief", "lead", manager="boss")
    add_agent(conn, "hand", "executor", manager="chief")

    to_executor = set(offered_to(conn, config, "hand"))
    to_lead = set(offered_to(conn, config, "chief"))
    to_director = set(offered_to(conn, config, "boss"))
    assert to_executor < to_lead < to_director

    before = row_counts(conn)
    for caller, withheld in (("hand", to_director - to_executor), ("chief", to_director - to_lead)):
        for name in sorted(withheld):
            failed, refusal = answer_to(conn, config, caller, name, {})
            assert failed, (caller, name)
            assert caller in refusal, (caller, name, refusal)

            _, allowed = answer_to(conn, config, "boss", name, {})
            assert "boss" not in allowed, (name, allowed)
    assert row_counts(conn) == before


def test_a_lead_reaches_nothing_outside_its_own_subtree(conn, config):
    add_agent(conn, "boss", "director")
    add_agent(conn, "north", "lead", manager="boss")
    add_agent(conn, "south", "lead", manager="boss")
    add_agent(conn, "n-hand", "executor", manager="north")
    add_agent(conn, "s-hand", "executor", manager="south")
    failed, text = answer_to(
        conn, config, "south", "assign", {"agent": "s-hand", "brief": "dig", "branch": "main"}
    )
    assert not failed, text
    work_id = db.query_one(conn, "SELECT id FROM works")["id"]
    core.create_pr(
        conn, title="dug", body=None, source_branch="dig", target_branch="main",
        author_agent_id=agent_row(conn, "s-hand")["id"],
    )
    pr_id = db.query_one(conn, "SELECT id FROM prs")["id"]

    before = row_counts(conn)
    reaching_out = [
        ("assign", {"agent": "s-hand", "brief": "dig elsewhere", "branch": "main"}),
        ("agent", {"op": "instruct", "agent": "s-hand", "text": "stop digging"}),
        ("agent", {"op": "instructions", "agent": "boss"}),
        ("agent", {"op": "new_session", "agent": "s-hand"}),
        ("agent", {"op": "fire", "agent": "south"}),
        ("agent", {"op": "move", "agent": "n-hand", "manager": "south"}),
        ("agent", {"op": "move", "agent": "s-hand", "manager": "north"}),
        ("work_close", {"work": work_id, "summary": "taken"}),
        ("work_reassign", {"work": work_id, "to_agent": "n-hand", "workspace": "fresh"}),
        ("pr", {"op": "close", "pr_id": pr_id}),
    ]
    for name, arguments in reaching_out:
        failed, refusal = answer_to(conn, config, "north", name, arguments)
        assert failed, (name, arguments, refusal)
        assert "under you" in refusal, (name, arguments, refusal)
    assert row_counts(conn) == before
    assert agent_row(conn, "s-hand")["instructions"] is None
    assert agent_row(conn, "n-hand")["manager_agent_id"] == agent_row(conn, "north")["id"]

    # Control: the same kind of call inside its own subtree goes through.
    failed, text = answer_to(
        conn, config, "north", "agent", {"op": "instruct", "agent": "n-hand", "text": "dig north"}
    )
    assert not failed, text
    assert agent_row(conn, "n-hand")["instructions"] == "dig north"


def test_an_agent_cancels_its_own_pending_reminder_and_no_one_elses(conn, config):
    add_agent(conn, "boss", "director")
    add_agent(conn, "hand", "executor", manager="boss")
    git_workspace(conn, config, "boss", "ws-boss")
    set_reminder = {"op": "set", "to": "hand", "text": "look again", "in_seconds": 3600}
    failed, text = answer_to(conn, config, "boss", "remind", set_reminder)
    assert not failed, text
    bosses = db.query_one(conn, "SELECT id FROM scheduled_messages")["id"]
    assert f"[{bosses}]" in answer_to(conn, config, "boss", "roster", {})[1]

    failed, refusal = answer_to(
        conn, config, "hand", "remind", {"op": "cancel", "reminder_id": bosses}
    )
    assert failed
    assert db.query_one(conn, "SELECT COUNT(*) AS n FROM scheduled_messages")["n"] == 1

    failed, text = answer_to(
        conn, config, "boss", "remind", {"op": "cancel", "reminder_id": bosses}
    )
    assert not failed, text
    assert db.query_one(conn, "SELECT COUNT(*) AS n FROM scheduled_messages")["n"] == 0


def test_a_move_is_refused_while_a_failed_work_would_leave_its_assigner_behind(conn, config):
    add_agent(conn, "boss", "director")
    add_agent(conn, "north", "lead", manager="boss")
    add_agent(conn, "south", "lead", manager="boss")
    add_agent(conn, "hand", "executor", manager="north")
    failed, text = answer_to(
        conn, config, "north", "assign", {"agent": "hand", "brief": "dig", "branch": "main"}
    )
    assert not failed, text
    work_id = db.query_one(conn, "SELECT id FROM works")["id"]
    core.fail_work(conn, work_id, "crash")

    failed, refusal = answer_to(
        conn, config, "boss", "agent", {"op": "move", "agent": "hand", "manager": "south"}
    )
    assert failed
    assert f"work {work_id}" in refusal
    assert agent_row(conn, "hand")["manager_agent_id"] == agent_row(conn, "north")["id"]

    failed, text = answer_to(conn, config, "north", "work_dismiss", {"work": work_id})
    assert not failed, text
    failed, text = answer_to(
        conn, config, "boss", "agent", {"op": "move", "agent": "hand", "manager": "south"}
    )
    assert not failed, text
    assert agent_row(conn, "hand")["manager_agent_id"] == agent_row(conn, "south")["id"]
    told = db.query_one(
        conn, "SELECT body FROM messages WHERE recipient = 'hand' ORDER BY id DESC"
    )["body"]
    assert "south" in told


def test_a_name_on_no_list_reaches_no_tool(conn, config):
    add_agent(conn, "hand", "executor")

    before = row_counts(conn)
    failed, text = answer_to(conn, config, "hand", _NO_SUCH_TOOL, {})
    assert failed
    assert _NO_SUCH_TOOL in text
    assert row_counts(conn) == before


_PAGE = "one\ntwo\nthree\nfour\nfive\n"


def _two_copies_of_a_page(conn, config):
    """Two agents with a sandbox each, both holding a copy of ops/deploy."""
    for agent, ws_id in (("first", "ws-first"), ("second", "ws-second")):
        add_agent(conn, agent, "executor")
        git_workspace(conn, config, agent, ws_id)
    core.note_wiki(
        conn, path="ops/deploy", category="ops", title="Deploy", body=_PAGE,
        updated_by="owner", expected_version=None,
    )
    for ws_id in ("ws-first", "ws-second"):
        wiki_files.sync(conn, config, ws_id)
    return (wiki_files.page_file(config, "ws-first", "ops/deploy"),
            wiki_files.page_file(config, "ws-second", "ops/deploy"))


def _publish(conn, config, agent):
    return answer_to(conn, config, agent, "note", {"op": "write", "kind": "wiki", "path": "ops/deploy"})


def test_a_published_file_is_merged_with_the_changes_made_since_its_copy(conn, config):
    first, second = _two_copies_of_a_page(conn, config)
    second.write_text(_PAGE.replace("five", "FIVE"), encoding="utf-8", newline="")
    failed, text = _publish(conn, config, "second")
    assert not failed, text

    first.write_text(_PAGE.replace("one", "ONE"), encoding="utf-8", newline="")
    wiki_files.sync(conn, config, "ws-first")
    assert first.read_text(encoding="utf-8") == _PAGE.replace("one", "ONE")
    failed, text = _publish(conn, config, "first")

    assert not failed, text
    merged = _PAGE.replace("one", "ONE").replace("five", "FIVE")
    page = core.get_wiki_page(conn, "ops/deploy")
    assert (page["version"], page["body"]) == (3, merged)
    assert first.read_text(encoding="utf-8") == merged
    wiki_files.sync(conn, config, "ws-second")
    assert second.read_text(encoding="utf-8") == merged


def test_overlapping_edits_publish_nothing_and_mark_only_the_overlap(conn, config):
    first, second = _two_copies_of_a_page(conn, config)
    second.write_text(_PAGE.replace("one", "uno"), encoding="utf-8", newline="")
    failed, text = _publish(conn, config, "second")
    assert not failed, text

    first.write_text(_PAGE.replace("one", "ein").replace("five", "FIVE"), encoding="utf-8",
                     newline="")
    failed, text = _publish(conn, config, "first")
    assert not failed and text.startswith("Nothing published"), text
    assert core.get_wiki_page(conn, "ops/deploy")["version"] == 2
    marked = first.read_text(encoding="utf-8")
    assert marked.count("<<<<<<< your copy") == 1
    assert "uno" in marked and "ein" in marked and "FIVE" in marked

    failed, _ = _publish(conn, config, "first")
    assert failed

    settled = _PAGE.replace("one", "ein").replace("five", "FIVE")
    first.write_text(settled, encoding="utf-8", newline="")
    failed, text = _publish(conn, config, "first")
    assert not failed, text
    page = core.get_wiki_page(conn, "ops/deploy")
    assert (page["version"], page["body"]) == (3, settled)


def test_the_board_lists_one_level_searches_all_levels_and_refuses_a_parent_inside_the_task(conn, config):
    add_agent(conn, "hand", "executor")

    def task(**arguments):
        failed, text = answer_to(conn, config, "hand", "task", arguments)
        assert not failed, text
        return text

    task(op="create", title="Game")
    task(op="create", title="Economy", parent=1)
    task(op="create", title="Gold mine", parent=2)
    task(op="create", title="Tooling")
    task(op="create", title="Old economy draft", parent=1)
    task(op="move", task_id=5, status="done", result="dropped")

    assert task(op="list") == "#1 [idea] Game — children: 1 open / 1 done\n#4 [idea] Tooling"
    assert task(op="list", parent=1) == "#2 [idea] Economy — children: 1 open / 0 done"
    assert task(op="list", parent=1, status="all") == (
        "#2 [idea] Economy — children: 1 open / 0 done\n#5 [done] Old economy draft"
    )
    assert task(op="list", query="ECONOMY") == "#2 [idea] Economy — children: 1 open / 0 done"
    assert task(op="list", query="economy", status="all") == (
        "#2 [idea] Economy — children: 1 open / 0 done\n#5 [done] Old economy draft"
    )
    assert task(op="list", parent=1, query="mine") == "#3 [idea] Gold mine"
    assert task(op="list", parent=4, query="mine") == (
        "no task matches among those neither done nor cancelled — status=all includes them"
    )
    failed, refusal = answer_to(
        conn, config, "hand", "task",
        {"op": "move", "task_id": 4, "status": "cancelled", "result": "not needed"},
    )
    assert failed and "the director's and the leads'" in refusal, refusal

    for arguments, chain in (
        ({"task_id": 1, "parent": 3}, "#3 under #2 under #1"),
        ({"task_id": 2, "parent": 2}, "#2"),
    ):
        failed, refusal = answer_to(conn, config, "hand", "task", {"op": "update", **arguments})
        assert failed, refusal
        assert f"the chain {chain} leads back to task {arguments['task_id']}" in refusal, refusal
    assert [r["parent_task_id"] for r in db.query(conn, "SELECT parent_task_id FROM tasks ORDER BY id")] == [
        None, 1, 2, None, 1,
    ]

    for n in range(office_mcp._TASK_LIST_LINES + 5):
        core.create_task(conn, f"Unit {n}")
    lines = task(op="list").splitlines()
    assert len(lines) == office_mcp._TASK_LIST_LINES + 1
    assert lines[-1] == "7 more task(s) left out — narrow with parent, status or query"


def git_workspace(conn, config, agent, ws_id):
    """A workspace row over a real, empty repository, owned by `agent`."""
    path = config.ws_dir / ws_id
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True, timeout=60)
    with db.transaction(conn) as c:
        c.execute(
            "INSERT INTO workspaces (id, path, owner_agent_id) "
            "VALUES (?, ?, (SELECT id FROM agents WHERE name = ?))",
            (ws_id, str(path), agent),
        )
    return path


def test_an_inherited_workspace_is_swapped_for_the_recipients_own(conn, config):
    add_agent(conn, "boss", "director")
    add_agent(conn, "first", "executor", manager="boss")
    add_agent(conn, "second", "executor", manager="boss")
    first_tree = git_workspace(conn, config, "first", "ws-first")
    second_tree = git_workspace(conn, config, "second", "ws-second")
    failed, text = answer_to(
        conn, config, "boss", "assign", {"agent": "first", "brief": "dig", "branch": "main"}
    )
    assert not failed, text
    work_id = db.query_one(conn, "SELECT id FROM works")["id"]

    failed, text = answer_to(
        conn, config, "boss", "work_reassign",
        {"work": work_id, "to_agent": "second", "workspace": "inherit"},
    )

    assert not failed, text
    owners = {
        r["id"]: r["name"]
        for r in db.query(
            conn,
            "SELECT w.id, a.name FROM workspaces w LEFT JOIN agents a ON a.id = w.owner_agent_id",
        )
    }
    assert owners == {"ws-first": "second", "ws-second": "first"}
    assert db.query_one(conn, "SELECT workspace_id FROM works")["workspace_id"] == "ws-first"
    for agent, tree, ws_id in (("second", first_tree, "ws-first"), ("first", second_tree, "ws-second")):
        told = db.query_one(
            conn,
            "SELECT body FROM messages WHERE recipient = ? AND sender = 'office' ORDER BY id DESC",
            (agent,),
        )["body"]
        assert str(tree) in told
        assert str(config.scratch_dir / ws_id) in told
        identity = subprocess.run(
            ["git", "config", "user.name"], cwd=tree, capture_output=True, text=True, timeout=60
        ).stdout.strip()
        assert identity == agent


def test_a_failed_work_is_read_in_full_only_within_reach(conn, config):
    add_agent(conn, "boss", "director")
    add_agent(conn, "north", "lead", manager="boss")
    add_agent(conn, "south", "lead", manager="boss")
    add_agent(conn, "s-hand", "executor", manager="south")
    failed, text = answer_to(
        conn, config, "south", "assign", {"agent": "s-hand", "brief": "dig", "branch": "main"}
    )
    assert not failed, text
    work_id = db.query_one(conn, "SELECT id FROM works")["id"]
    core.fail_work(conn, work_id, "crash", output_tail="the spade broke")

    failed, refusal = answer_to(conn, config, "north", "work", {"op": "show", "work": work_id})
    assert failed
    assert "the spade broke" not in refusal

    for reader in ("south", "boss", "s-hand"):
        failed, text = answer_to(conn, config, reader, "work", {"op": "show", "work": work_id})
        assert not failed, (reader, text)
        assert "the spade broke" in text
        assert "assigned by south" in text
