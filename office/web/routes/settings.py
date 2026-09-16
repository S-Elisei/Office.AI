"""Settings: owner_name, director instructions and the silence threshold
(core.set_setting), and the director's live runtime/model/effort
(core.update_agent_model).
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Request

from office import core, db, git
from office.bus import SILENCE_NOTICE_DEFAULT_MINUTES, SILENCE_NOTICE_SETTING
from office.web.deps import get_config, get_db, get_owner_name, read_form
from office.web.templating import templates

router = APIRouter()

RUNTIMES = ["claude", "codex", "agy"]

# The model <select>'s last option on claude.
# Choosing it opens the free-text box.
# It can never be saved.
CUSTOM_MODEL = "__custom__"


def _director(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return db.query_one(conn, "SELECT * FROM agents WHERE kind = 'director' LIMIT 1")


def _fmt_minutes(minutes: float) -> str:
    return str(int(minutes)) if float(minutes).is_integer() else str(minutes)


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
    """This page's two forms, drawn from what a refused submit posted where
    there is one and from storage otherwise.
    """
    conn = get_db(request)
    director = _director(conn)
    form = form or {}
    ctx = {
        "request": request,
        "active": "settings",
        "director": director,
        "instructions": form.get("instructions", core.get_setting(conn, "director_instructions", "")),
        "owner_name": form.get("owner_name", get_owner_name(conn)),
        # The stored row, or the default when there is no row.
        "silence_minutes": form.get(
            "silence_minutes",
            core.get_setting(conn, SILENCE_NOTICE_SETTING, "")
            or _fmt_minutes(SILENCE_NOTICE_DEFAULT_MINUTES),
        ),
        # partials/notice.html's contract: a refusal, a confirmation, or neither.
        "notice_error": error,
        "notice_ok": (applied_note or "Saved.") if saved else None,
    }
    if director is not None:
        # Only when there is a director.
        # The selections drawn are the posted ones where a refused change posted
        # some, and the director's own otherwise.
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
    """Owner-facing config only: owner_name, director_instructions and the
    silence threshold. All three are plain settings rows.
    """
    conn = get_db(request)
    data = await read_form(request)
    owner_name = data.get("owner_name", "").strip()
    instructions = data.get("instructions", "")
    silence_minutes = data.get("silence_minutes", "").strip()
    core.set_setting(conn, "owner_name", owner_name, actor=owner_name)
    core.set_setting(conn, "director_instructions", instructions, actor=owner_name)
    # Written verbatim, empty string included.
    core.set_setting(conn, SILENCE_NOTICE_SETTING, silence_minutes, actor=owner_name)
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
