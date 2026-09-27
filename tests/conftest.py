"""Shared test plumbing."""

from __future__ import annotations

import os
import threading

import pytest

from office import db
from office.config import Config

FIXTURES_DIR = os.path.dirname(__file__)


@pytest.fixture
def config(tmp_path):
    """A Config rooted in a fresh tmp dir."""
    cfg = Config(
        root=tmp_path / "office-data",
        port=0,
        claude_bin=None,
        codex_bin=None,
        agy_bin=None,
    )
    cfg.ensure_layout()
    return cfg


@pytest.fixture
def conn(tmp_path):
    """A real SQLite connection against a throwaway file, schema applied."""
    connection = db.connect(tmp_path / "test.db")
    db.init_schema(connection)
    yield connection
    # A turn's watcher writes to the database after its process ends: closing the
    # connection under it crashes the interpreter.
    for thread in threading.enumerate():
        if thread.name.endswith("(_watch)"):
            thread.join(timeout=30)
    connection.close()
