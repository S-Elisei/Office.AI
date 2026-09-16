"""Aggregates every page router into one."""

from __future__ import annotations

from fastapi import APIRouter

from . import (
    chat, dm, kanban, knowledge, main, monitor, nav, notices, prs, reading, settings, team,
    tickets,
)

router = APIRouter()

for _module in (
    main, dm, kanban, tickets, monitor, chat, team, knowledge, prs, notices, settings, nav,
    reading,
):
    router.include_router(_module.router)
