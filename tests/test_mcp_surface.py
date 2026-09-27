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


def test_a_wiki_search_numbers_its_lines_and_caps_them_per_page(conn, config):
    add_agent(conn, "hand", "executor")
    pages = {
        "runbooks/deploy": ("Deploy", "Build first.\nThen Deploy to staging.\nDone."),
        "runbooks/log": ("Log", "\n".join(f"deploy {i}" for i in range(25))),
        "runbooks/misc": ("Misc", "Nothing here."),
        "runbooks/release": ("Deployment checklist", "Tag.\nPush."),
    }
    for path, (title, body) in pages.items():
        failed, text = answer_to(
            conn, config, "hand", "note",
            {"op": "write", "kind": "wiki", "path": path, "category": "ops", "title": title, "text": body},
        )
        assert not failed, text

    failed, text = answer_to(conn, config, "hand", "note", {"op": "search", "kind": "wiki", "query": "DEPLOY"})

    assert not failed, text
    assert text == "\n".join(
        [
            "runbooks/deploy Deploy (v1)",
            "2:Then Deploy to staging.",
            "",
            "runbooks/log Log (v1)",
            *(f"{i + 1}:deploy {i}" for i in range(20)),
            "(5 more matching line(s) on this page)",
            "",
            "runbooks/release Deployment checklist (v1)",
        ]
    )


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
