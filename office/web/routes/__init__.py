"""Aggregates every page router into one."""

from __future__ import annotations

from fastapi import APIRouter

from . import (
    chat, dm, files, hooks, kanban, knowledge, main, monitor, nav, notices, prs, reading, rules,
    settings, switch, team, tickets,
)

router = APIRouter()

for _module in (
    main, dm, kanban, tickets, monitor, chat, team, knowledge, rules, prs, notices, settings,
    nav, reading, hooks, switch, files,
):
    router.include_router(_module.router)
