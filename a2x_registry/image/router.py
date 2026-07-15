"""FastAPI router for image management endpoints.

Routes (mounted at app level, prefix ``/api/images``):

    POST   /api/images                       register_image (image-processing module)
    GET    /api/images                       query (user; optional ?framework=)
    GET    /api/images/{framework}/launch-spec  resolve_launch_spec (gateway)
    PUT    /api/images/{framework}/default   set_default (user)
    DELETE /api/images/{framework}/{version} deregister (user; 409 if in use)

When the image module is not assembled (non-appliance mode), all routes
return 404.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from a2x_registry.register.errors import (
    NotFoundError,
    ValidationError,
)
from a2x_registry.register.errors import ImageInUseError, ExternalDependencyError

from .deps import get_image_service
from .models import (
    DeregisterResponse,
    FrameworkGroup,
    ImageRegisterResponse,
    LaunchSpecResponse,
    RegisterImageRequest,
    SetDefaultRequest,
    SetDefaultResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/images", tags=["image"])


def _resolve_service():
    """Return the assembled ImageService, or raise 404 if not assembled.

    404 (vs 503): from a non-appliance registry's perspective these
    routes do not exist at all, matching the fallback semantics of an
    uninitialized heartbeat module.
    """
    svc = get_image_service()
    if svc is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "Image module not assembled (non-appliance mode). "
                "Set A2X_REGISTRY_MODE=appliance to enable."
            ),
        )
    return svc


@router.post("", response_model=ImageRegisterResponse)
async def register_image(req: RegisterImageRequest):
    """Register an image version (caller: image-processing module). Idempotent upsert."""
    svc = _resolve_service()
    try:
        result = svc.register_image(
            framework=req.framework,
            framework_version=req.framework_version,
            spec=req.spec.model_dump(),
            uploaded_by=req.uploaded_by,
        )
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return result


@router.get("", response_model=list[FrameworkGroup])
async def list_images(framework: Optional[str] = Query(None)):
    """Query images (user), grouped by framework, hierarchical output."""
    svc = _resolve_service()
    return svc.query(framework=framework)


@router.get("/{framework}/launch-spec", response_model=LaunchSpecResponse)
async def get_launch_spec(
    framework: str,
    version: Optional[str] = Query(None),
):
    """Fetch the launch spec (caller: gateway); without version the default is used."""
    svc = _resolve_service()
    try:
        return svc.resolve_launch_spec(framework, version=version)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.put("/{framework}/default", response_model=SetDefaultResponse)
async def set_default(framework: str, req: SetDefaultRequest):
    """Set the default version (user)."""
    svc = _resolve_service()
    try:
        return svc.set_default(framework, req.framework_version)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.delete("/{framework}/{version}", response_model=DeregisterResponse)
async def deregister_image(framework: str, version: str):
    """Deregister an image version (user). Returns 409 if instances are in use."""
    svc = _resolve_service()
    try:
        return svc.deregister(framework, version)
    except NotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ImageInUseError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ExternalDependencyError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
