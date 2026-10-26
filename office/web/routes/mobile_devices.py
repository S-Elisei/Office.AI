"""Local registration and revocation of mobile devices."""

from fastapi import APIRouter, Request

from office import db
from office.mobile import register_device
from office.web.deps import get_config, get_db, read_form
from office.web.templating import templates

router = APIRouter()


def context(request: Request) -> dict:
    return {
        "request": request,
        "mobile_devices": db.query(get_db(request), "SELECT * FROM mobile_devices ORDER BY created_at"),
        "mobile_origin": get_config(request).mobile_origin,
        "mobile_port": get_config(request).mobile_port,
    }


@router.post("/settings/mobile")
async def add_device(request: Request):
    data = await read_form(request)
    register_device(get_db(request), data["name"].strip(), data["public_key"].strip())
    return templates.TemplateResponse(request, "partials/settings_mobile.html", context(request))


@router.post("/settings/mobile/{device}/revoke")
async def revoke_device(request: Request, device: str):
    db.execute(get_db(request), "DELETE FROM mobile_devices WHERE id = ?", (device,))
    auth = getattr(request.app.state, "mobile_auth", None)
    if auth is not None:
        auth.revoke(device)
    await request.app.state.broadcaster.publish("mobile_devices", {})
    return templates.TemplateResponse(request, "partials/settings_mobile.html", context(request))
