"""Settings: owner_name, the silence threshold, the wallets and the work
complexities (core.set_setting), the two price lists (core.set_price_lists) and
their note, the director's standing instructions (core.set_instructions), and the
director's live runtime/model/effort (core.update_agent_model).
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Request

from office import core, db, git, wallets
from office.adapters import installed
from office.bus import SILENCE_NOTICE_DEFAULT_MINUTES, SILENCE_NOTICE_SETTING
from office.web.deps import (
    get_bus, get_config, get_db, get_owner_name, read_form, read_form_lists
)
from office.web.templating import templates

router = APIRouter()

RUNTIMES = ["claude", "codex", "agy"]

# The model <select>'s last option on claude.
# Choosing it opens the free-text box.
# It can never be saved.
CUSTOM_MODEL = "__custom__"

# The header cells of the two price lists' tables.
WORKS_COLUMNS = ["Role", "Complexity", "Size", "Model", "Price (typical / with margin)"]
OWN_COLUMNS = ["What", "Size", "Price (typical / with margin)"]

# The names of the fields a row's cells post, in column order.
WORKS_FIELDS = ["works_role", "works_complexity", "works_size", "works_model", "works_price"]
OWN_FIELDS = ["own_what", "own_size", "own_price"]
COMPLEXITY_FIELD = "complexity"


def _director(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return db.query_one(conn, "SELECT * FROM agents WHERE kind = 'director' LIMIT 1")


def _fmt_minutes(minutes: float) -> str:
    return str(int(minutes)) if float(minutes).is_integer() else str(minutes)


def _editor(
    id: str, fields: list[str], columns: list[str], rows: list[list[str]], *, table: bool = True
) -> dict:
    """What macros.html's `table_editor` draws: one input per cell, named by the
    column's entry in `fields`; a list (`table` false) has no header row."""
    return {"id": id, "fields": fields, "columns": columns, "rows": rows, "table": table}


def _table_rows(
    lists: dict[str, list[str]], fields: list[str], columns: tuple[str, ...]
) -> list[dict]:
    """The rows a table editor posted: the fields zipped by position, each cell
    trimmed."""
    return [
        dict(zip(columns, (cell.strip() for cell in cells)))
        for cells in zip(*(lists.get(f, []) for f in fields))
    ]


def _cost_note(
    conn: sqlite3.Connection,
    director: sqlite3.Row | None,
    runtime: str,
    model: str,
    effort: str | None,
) -> tuple[str, str]:
    """Read-only classification via core.update_agent_model(dry_run=True).
    Returns ("none", "") when there's no director yet.
    """
    if director is None:
        return "none", ""
    try:
        result = core.update_agent_model(
            conn, director["id"], runtime=runtime, model=model, effort=effort, dry_run=True
        )
    except ValueError as exc:
        return "refused", str(exc)
    return result["cost"], result["note"]


def _model_fields(
    conn: sqlite3.Connection,
    director: sqlite3.Row | None,
    *,
    prefix: str,
    runtime: str,
    model: str,
    effort: str | None,
    model_runtime: str | None = None,
    mode_custom: bool = False,
    force_list: bool = False,
) -> dict:
    """Everything partials/model_fields.html needs to draw the runtime, model
    and effort controls for one set of selections.
    `model_runtime` is the runtime the incoming model belongs to.
    """
    if model_runtime is not None and model_runtime != runtime:
        model, effort, mode_custom = "", None, False

    catalogue = core.list_models(conn, runtime)

    if force_list:
        mode_custom = False
        if not any(r["model_id"] == model for r in catalogue):
            model, effort = "", None

    row = next((r for r in catalogue if r["model_id"] == model), None)

    custom = bool(catalogue) and (
        (mode_custom and runtime == "claude") or (bool(model) and row is None)
    )

    cost, note = _cost_note(conn, director, runtime, model, effort) if model else ("none", "")
    return {
        "prefix": prefix,
        "runtimes": RUNTIMES,
        "m_runtime": runtime,
        "m_model": model,
        "m_effort": effort,
        "m_catalogue": catalogue,
        "m_custom": custom,
        "m_known": row is not None,
        "m_efforts": row["efforts"] if row else [],
        "cost": cost,
        "note": note,
    }


def _context(
    request: Request,
    error: str | None = None,
    saved: bool = False,
    applied_note: str | None = None,
    form: dict[str, str] | None = None,
) -> dict:
    """This page's two forms, drawn from storage; `form` is what a refused change
    of the director's model posted, and only its runtime, model, effort and mode are
    read.
    """
    conn = get_db(request)
    director = _director(conn)
    form = form or {}
    ctx = {
        "request": request,
        "active": "settings",
        "director": director,
        "owner_name": get_owner_name(conn),
        # The stored row, or the default when there is no row.
        "silence_minutes": core.get_setting(conn, SILENCE_NOTICE_SETTING, "")
        or _fmt_minutes(SILENCE_NOTICE_DEFAULT_MINUTES),
        "wallet_rows": [
            {
                "currency": currency,
                "week_key": wallets.week_key(runtime),
                "window_key": wallets.window_key(runtime),
                "reserve_key": wallets.reserve_key(runtime),
                "remaining": wallets.week_remaining(conn, runtime),
            }
            for runtime, currency in wallets.CURRENCY.items()
            if installed(runtime, get_config(request))
        ],
        "price_list_note": core.get_setting(conn, core.PRICE_LIST_NOTE_SETTING, "") or "",
        "price_list_works": _editor(
            "works", WORKS_FIELDS, WORKS_COLUMNS,
            [[row[c] for c in core.PRICE_WORKS_COLUMNS] for row in core.price_works(conn)],
        ),
        "price_list_own": _editor(
            "own", OWN_FIELDS, OWN_COLUMNS,
            [[row[c] for c in core.PRICE_OWN_COLUMNS] for row in core.price_own(conn)],
        ),
        # The values in effect: the stored lines, or the defaults when there are none.
        "work_complexities": _editor(
            "complexities", [COMPLEXITY_FIELD], ["Complexity"],
            [[value] for value in core.work_complexities(conn)],
            table=False,
        ),
        # The stored value of every wallet field, by its setting key.
        "wallet_values": {
            key: core.get_setting(conn, key, "") or "" for key in wallets.SETTING_KEYS
        },
        # partials/notice.html's contract: a refusal, a confirmation, or neither.
        "notice_error": error,
        "notice_ok": (applied_note or "Saved.") if saved else None,
    }
    if director is not None:
        # Only when there is a director.
        # The selections drawn are the posted ones where a refused change posted
        # some, and the director's own otherwise.
        ctx["instructions"] = director["instructions"] or ""
        ctx.update(
            _model_fields(
                conn,
                director,
                prefix="s",
                runtime=form.get("runtime", director["runtime"]),
                model=form.get("model", director["model"]),
                effort=form.get("effort", director["effort"] or "") or None,
                mode_custom=form.get("model_mode", "") == "custom",
            )
        )
    return ctx


@router.get("/settings")
def settings_page(request: Request):
    return templates.TemplateResponse(
        request, "settings.html", {**_context(request), **_delivery_context(request)}
    )


@router.get("/fragments/settings")
def settings_fragment(request: Request):
    return templates.TemplateResponse(request, "partials/settings_form.html", _context(request))


@router.post("/settings")
async def save_settings(request: Request):
    """Owner-facing config only: owner_name, the silence threshold, the wallets'
    weekly limits, 5h windows and reserve, the two price lists with their note, and
    the work complexities.
    """
    conn = get_db(request)
    data = await read_form(request)
    lists = await read_form_lists(request)
    owner_name = data.get("owner_name", "").strip()
    silence_minutes = data.get("silence_minutes", "").strip()
    core.set_setting(conn, "owner_name", owner_name, actor=owner_name)
    # Written verbatim, empty string included.
    core.set_setting(conn, SILENCE_NOTICE_SETTING, silence_minutes, actor=owner_name)
    # Only the runtimes whose rows the page drew posted theirs.
    for key in wallets.SETTING_KEYS:
        if key in data:
            core.set_setting(conn, key, data[key].strip(), actor=owner_name)
    core.set_price_lists(
        conn,
        _table_rows(lists, WORKS_FIELDS, core.PRICE_WORKS_COLUMNS),
        _table_rows(lists, OWN_FIELDS, core.PRICE_OWN_COLUMNS),
        actor=owner_name,
    )
    # Written verbatim.
    core.set_setting(
        conn, core.PRICE_LIST_NOTE_SETTING, data.get(core.PRICE_LIST_NOTE_SETTING, ""),
        actor=owner_name,
    )
    complexities = [v.strip() for v in lists.get(COMPLEXITY_FIELD, [])]
    core.set_setting(
        conn, core.WORK_COMPLEXITIES_SETTING, "\n".join(complexities), actor=owner_name
    )
    return templates.TemplateResponse(request, "partials/settings_form.html", _context(request, saved=True))


@router.post("/settings/director-instructions")
async def save_director_instructions(request: Request):
    """The director's standing instructions, written to its agents row."""
    conn = get_db(request)
    data = await read_form(request)
    core.set_instructions(
        conn, _director(conn)["id"], data.get("instructions", ""), actor=get_owner_name(conn)
    )
    return templates.TemplateResponse(request, "partials/settings_form.html", _context(request, saved=True))


@router.post("/fragments/model-fields")
async def model_fields_fragment(request: Request):
    """The runtime/model/effort group, redrawn for the selections posted.
    Read-only: it reads the catalogue and classifies the cost through
    core.update_agent_model(dry_run=True), and writes nothing.
    """
    conn = get_db(request)
    data = await read_form(request)
    model = data.get("model", "").strip()
    mode_custom = data.get("model_mode", "") == "custom"
    if model == CUSTOM_MODEL:
        model, mode_custom = "", True
    director = _director(conn)
    ctx = {
        "request": request,
        "director": director,
        **_model_fields(
            conn,
            director,
            prefix=data["prefix"],
            runtime=data.get("runtime", "").strip(),
            model=model,
            effort=data.get("effort", "").strip() or None,
            model_runtime=data.get("model_runtime", "").strip() or None,
            mode_custom=mode_custom,
            force_list=bool(data.get("model_list")),
        ),
    }
    return templates.TemplateResponse(request, "partials/model_fields.html", ctx)


@router.post("/settings/director-model")
async def apply_director_model(request: Request):
    conn = get_db(request)
    data = await read_form(request)
    director = _director(conn)
    runtime = data.get("runtime", "").strip()
    model = data.get("model", "").strip()
    effort = data.get("effort", "").strip() or None
    owner_name = get_owner_name(conn)
    busy = get_bus(request).busy_reason(director["name"]) if runtime != director["runtime"] else None
    if busy is not None:
        ctx = _context(
            request, form=data,
            error=f"The runtime cannot change while {busy}: wait for it to end, or stop it, and apply again.",
        )
        return templates.TemplateResponse(request, "partials/settings_form.html", ctx, status_code=400)
    try:
        result = core.update_agent_model(
            conn, director["id"], runtime=runtime, model=model, effort=effort, actor=owner_name
        )
    except ValueError as exc:
        ctx = _context(request, error=str(exc), form=data)
        return templates.TemplateResponse(request, "partials/settings_form.html", ctx, status_code=400)
    ctx = _context(request, saved=True, applied_note=result["note"])
    return templates.TemplateResponse(request, "partials/settings_form.html", ctx)


# ------------------------------------------------------------------ finished work

DELIVERY_FRAGMENT = "partials/settings_delivery.html"


def _delivery_context(
    request: Request, *, error: str | None = None, note: str | None = None
) -> dict:
    """Where the team's finished work goes.
    The path is shown and cannot be edited.
    Whether the repository is set up to take delivered work is read back out of
    it. That read spawns git; both routes here are plain `def`s.
    """
    conn = get_db(request)
    config = get_config(request)
    cfg = core.get_delivery(conn, config)
    return {
        "request": request,
        "delivery": {
            "repo": cfg["repo"],
            "branch": cfg["branch"],
            "last": cfg["last"],
            "office_repo": str(git.project_git(config)),
            "not_a_repo": git.owner_state(config) == "none",
            "configured": git.owner_takes_delivery(config),
        },
        "notice_error": error,
        "notice_ok": note,
    }


@router.get("/fragments/delivery")
def delivery_fragment(request: Request):
    return templates.TemplateResponse(request, DELIVERY_FRAGMENT, _delivery_context(request))


@router.post("/settings/project/configure")
def configure_owner_repo(request: Request):
    """Set up the owner's repository to take delivered work into its files.
    A plain `def`: it runs git against another repository.
    """
    status, detail = git.configure_owner_repo(get_config(request))
    if status == "set":
        return templates.TemplateResponse(request, DELIVERY_FRAGMENT, _delivery_context(request, note=detail))
    return templates.TemplateResponse(
        request, DELIVERY_FRAGMENT, _delivery_context(request, error=detail), status_code=400
    )
