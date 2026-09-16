"""Per-request accessors for state."""

from __future__ import annotations

import sqlite3
from urllib.parse import parse_qsl

from fastapi import Request

from office import core
from office.config import Config

def get_db(request: Request) -> sqlite3.Connection:
    return request.app.state.db


def get_config(request: Request) -> Config:
    return request.app.state.config


def get_bus(request: Request):
    return request.app.state.bus


def get_owner_name(conn: sqlite3.Connection) -> str:
    """The human owner's name in the shared participant namespace."""
    return core.get_owner_name(conn)


async def read_form(request: Request) -> dict[str, str]:
    """Parse an application/x-www-form-urlencoded POST body.

    Collapses a repeated key to its last value.
    """
    body = await request.body()
    return dict(parse_qsl(body.decode("utf-8"), keep_blank_values=True))
