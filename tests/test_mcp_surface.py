"""The tool surface an agent reaches through the MCP entry points."""

from __future__ import annotations

import asyncio

import mcp.types as types
from mcp.server.context import ServerRequestContext
from starlette.applications import Starlette
from starlette.requests import Request

from office import db
from office import mcp as office_mcp

#: An argument no schema declares, and a name no registry holds.
_UNDECLARED = "not_a_declared_argument"
_NO_SUCH_TOOL = "no-tool-answers-to-this"


def add_agent(conn, name, kind):
    with db.transaction(conn) as c:
        c.execute(
            "INSERT INTO agents (name, kind, runtime, model, status) "
            "VALUES (?, ?, 'claude', 'stub-model', 'idle')",
            (name, kind),
        )


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


def test_what_a_director_is_offered_beyond_an_executor_is_refused_to_an_executor(conn, config):
    add_agent(conn, "hand", "executor")
    add_agent(conn, "boss", "director")

    to_executor = set(offered_to(conn, config, "hand"))
    to_director = set(offered_to(conn, config, "boss"))
    assert to_executor < to_director

    before = row_counts(conn)
    for name in sorted(to_director - to_executor):
        failed, refusal = answer_to(conn, config, "hand", name, {})
        assert failed, name
        assert "hand" in refusal, (name, refusal)

        _, allowed = answer_to(conn, config, "boss", name, {})
        assert "boss" not in allowed, (name, allowed)
    assert row_counts(conn) == before


def test_a_name_on_no_list_reaches_no_tool(conn, config):
    add_agent(conn, "hand", "executor")

    before = row_counts(conn)
    failed, text = answer_to(conn, config, "hand", _NO_SUCH_TOOL, {})
    assert failed
    assert _NO_SUCH_TOOL in text
    assert row_counts(conn) == before
