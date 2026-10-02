"""The files agents name by absolute path in their texts."""

from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

router = APIRouter()

_SANDBOX = {"Content-Security-Policy": "sandbox"}


@router.get("/file")
def file(path: str):
    """The file at an absolute path, served under `Content-Security-Policy: sandbox`."""
    target = Path(path)
    if not target.is_file():
        raise HTTPException(404, headers=_SANDBOX)
    media_type = None
    if target.suffix.lower() == ".glb" and mimetypes.guess_type(target)[0] is None:
        media_type = "model/gltf-binary"
    return FileResponse(target, media_type=media_type, headers=_SANDBOX)
