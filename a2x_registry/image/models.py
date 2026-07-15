"""Image management pydantic request / response models.

``spec`` is the launch spec (stored in the image registry table's ``data``
JSON column); its fields correspond to the runtime sandbox
(``openyuanrong_api_reference.md`` §4.7):
``rootfs`` / ``cpu`` / ``memory`` / ``ports`` / ``env`` / ``image_module_version``.
``spec`` is accepted loosely as a ``dict`` — the registry does not enforce
a strict schema, since the image-processing module already validated it at
production time; the registry only stores the reference.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ImageSpec(BaseModel):
    """Launch spec (stored in the image registry ``data`` column)."""

    rootfs: Dict[str, Any] = Field(..., description="rootfs spec: type/imageurl/workdir/mounts")
    cpu: int = Field(..., description="CPU quota in millicores (e.g. 1000 = 1 core)")
    memory: int = Field(..., description="Memory quota in MB")
    ports: List[Dict[str, Any]] = Field(default_factory=list, description="Port mapping list")
    env: Dict[str, Any] = Field(default_factory=dict, description="Environment variables")
    image_module_version: Optional[str] = Field(None, description="Image-processing module version")


class RegisterImageRequest(BaseModel):
    """``POST /api/images`` request body."""

    framework: str = Field(..., description="Framework name, e.g. opencode")
    framework_version: str = Field(..., description="Framework version, e.g. v0.2.0")
    spec: ImageSpec
    uploaded_by: Optional[str] = Field(None, description="Uploader identity")


class SetDefaultRequest(BaseModel):
    """``PUT /api/images/{framework}/default`` request body."""

    framework_version: str = Field(..., description="Version to set as default")


class ImageRegisterResponse(BaseModel):
    framework: str
    framework_version: str
    is_default: bool
    status: str  # "registered" | "updated"


class ImageVersionEntry(BaseModel):
    """Single version entry in query output."""

    framework_version: str
    is_default: bool
    rootfs: Dict[str, Any]
    cpu: int
    memory: int
    ports: List[Dict[str, Any]]
    env: Dict[str, Any]
    image_module_version: Optional[str] = None


class FrameworkGroup(BaseModel):
    """Query output grouped by framework."""

    framework: str
    default: Optional[str] = None
    versions: List[ImageVersionEntry]


class LaunchSpecResponse(BaseModel):
    """``GET /api/images/{framework}/launch-spec`` output (image-level launch spec)."""

    framework: str
    framework_version: str
    rootfs: Dict[str, Any]
    cpu: int
    memory: int
    ports: List[Dict[str, Any]]
    env: Dict[str, Any]


class DeregisterResponse(BaseModel):
    framework: str
    framework_version: str
    status: str  # "deregistered"
    repo_deleted: bool = False


class SetDefaultResponse(BaseModel):
    framework: str
    default: str
    status: str  # "updated"
