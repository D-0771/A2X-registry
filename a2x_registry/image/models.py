"""Image management pydantic request / response models.

``spec`` is the launch spec (stored in the image registry table's ``data``
JSON column); its fields are **flat** (no ``rootfs`` wrapper) in V2 --
one row = one framework version, so the storage is flat and the API
surface is flat.

Fields correspond to the runtime sandbox (openyuanrong API §4.7):
``imageurl`` / ``workdir`` / ``mounts`` / ``cpu`` / ``memory`` /
``ports`` / ``env`` / ``image_module_version``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ImageSpec(BaseModel):
    """Launch spec (flat -- no ``rootfs`` wrapper in V2)."""

    imageurl: str = Field(..., description="Adapted image URL in the repo")
    workdir: Optional[str] = Field(None, description="Working directory")
    mounts: List[Dict[str, Any]] = Field(
        default_factory=list, description="Volume mounts"
    )
    cpu: int = Field(..., description="CPU quota in millicores (e.g. 1000 = 1 core)")
    memory: int = Field(..., description="Memory quota in MB")
    ports: List[Dict[str, Any]] = Field(
        default_factory=list, description="Port mapping list"
    )
    env: Dict[str, Any] = Field(
        default_factory=dict, description="Environment variables"
    )
    image_module_version: Optional[str] = Field(
        None, description="Image-processing module version"
    )


class RegisterImageRequest(BaseModel):
    """``POST /api/images`` request body."""

    framework: str = Field(..., description="Framework name, e.g. opencode")
    framework_version: str = Field(
        ..., description="Framework version, e.g. v0.2.0"
    )
    spec: ImageSpec
    uploaded_by: str = Field(..., description="Uploader identity")


class SetDefaultRequest(BaseModel):
    """``PUT /api/images/{framework}/default`` request body."""

    framework_version: str = Field(..., description="Version to set as default")


class ImageRegisterResponse(BaseModel):
    framework: str
    framework_version: str
    is_default: bool
    status: str  # "registered" | "updated"


class ImageEntry(BaseModel):
    """One flat row from the image registry (one framework version).

    ``framework`` and ``is_default`` are per-row fields; there is no
    framework-level grouping in the response. The frontend can group by
    ``framework`` client-side if needed.
    """

    framework: str
    framework_version: str
    is_default: bool
    image_module_version: Optional[str] = None
    imageurl: str
    workdir: Optional[str] = None
    mounts: List[Dict[str, Any]] = Field(default_factory=list)
    cpu: int
    memory: int
    ports: List[Dict[str, Any]] = Field(default_factory=list)
    env: Dict[str, Any] = Field(default_factory=dict)
    uploaded_by: Optional[str] = None
    created_at: Optional[str] = None


class LaunchSpecResponse(BaseModel):
    """``GET /api/images/{framework}/launch-spec`` output (flat, no rootfs)."""

    framework: str
    framework_version: str
    imageurl: str
    workdir: Optional[str] = None
    mounts: List[Dict[str, Any]] = Field(default_factory=list)
    cpu: int
    memory: int
    ports: List[Dict[str, Any]] = Field(default_factory=list)
    env: Dict[str, Any] = Field(default_factory=dict)


class DeregisterResponse(BaseModel):
    framework: str
    framework_version: str
    status: str  # "deregistered"
    repo_deleted: bool = False


class SetDefaultResponse(BaseModel):
    framework: str
    default: str
    status: str  # "updated"