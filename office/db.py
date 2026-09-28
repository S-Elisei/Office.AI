"""Thin SQLite layer: a connection, a schema loader, and plain query helpers."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"

_conn_lock = threading.RLock()


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))


def query(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    with _conn_lock:
        return conn.execute(sql, params).fetchall()


def query_one(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> sqlite3.Row | None:
    with _conn_lock:
        return conn.execute(sql, params).fetchone()


def execute(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> sqlite3.Cursor:
    with _conn_lock:
        return conn.execute(sql, params)


@contextmanager
def transaction(conn: sqlite3.Connection):
    """Multi-statement transaction. Commits on success, rolls back on exception."""
    with _conn_lock:
        conn.execute("BEGIN")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
