"""Aggregates every page router into one."""

from __future__ import annotations

from fastapi import APIRouter

from . import (
    chat, dm, files, hooks, kanban, knowledge, main, mobile_devices, monitor, nav, notices,
    prs, reading, rules, settings, switch, team, tickets,
)

router = APIRouter()

for _module in (
    chat, dm, files, hooks, kanban, knowledge, main, mobile_devices, monitor, nav, notices,
    prs, reading, rules, settings, switch, team, tickets,
):
    router.include_router(_module.router)
