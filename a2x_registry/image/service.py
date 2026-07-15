"""ImageService — image management business logic.

Responsibilities:
- ``register_image``: insert one row (framework+version, idempotent
  upsert); set ``is_default=1`` when the framework has no default yet;
  on re-register keep the original default flag and only update spec.
- ``query``: group by framework and return
  ``{framework, default, versions}`` hierarchically.
- ``deregister``: first verify no in-use instances (query the instance
  registry); if none, delete the repo image file (stub) and delete the
  row; if the deregistered one was the default, promote the latest
  remaining version to default.
- ``set_default`` / ``get_default_version``: default-version management.
- ``resolve_launch_spec``: assemble the launch spec for the gateway.

Persistence goes through ``RegistryTableService``; this service does not
hold a backend/store directly. Repository deletion is a stub (contract
not confirmed; logs only, non-blocking).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from a2x_registry.common.ids import image_sid
from a2x_registry.register.service import RegistryTableService

from .errors import ImageInUseError, ImageNotFoundError, ImageValidationError

logger = logging.getLogger(__name__)

# Named registry identifiers (must match the names created by startup.py
# in appliance mode; they are stored in the ``registry`` column of the
# image/instance tables and are part of the design-spec data contract).
IMAGE_REGISTRY = "镜像注册表"
INSTANCE_REGISTRY = "实例注册表"

# Image-level fields returned by launch-spec (extracted from the data JSON).
_LAUNCH_SPEC_FIELDS = ("rootfs", "cpu", "memory", "ports", "env")

# Image repository deletion endpoint (contract not confirmed; stub only).
_ENV_REPO_BASE = "A2X_REGISTRY_REPO_BASE"


class ImageService:
    """Image management business layer. Injects ``RegistryTableService``
    for persistence.

    Pure recorder: does not call the image-processing module or the
    runtime. Repository deletion is a configurable stub.
    """

    __slots__ = ("_table_svc",)

    def __init__(self, table_svc: RegistryTableService) -> None:
        self._table_svc = table_svc

    # ------------------------------------------------------------------
    # register_image
    # ------------------------------------------------------------------

    def register_image(
        self,
        framework: str,
        framework_version: str,
        spec: Dict[str, Any],
        uploaded_by: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Insert one row (framework+version, idempotent upsert).

        - re-register: keep the original ``is_default``, only update spec.
        - new registration: set ``is_default=1`` when the framework has
          no default yet, otherwise ``0``.
        Returns ``{framework, framework_version, is_default, status}``.
        """
        if not framework or not framework_version:
            raise ImageValidationError("framework and framework_version must not be empty")

        sid = image_sid(framework, framework_version)
        existing = self._table_svc.query(
            IMAGE_REGISTRY,
            {"framework": framework, "framework_version": framework_version},
        )
        if existing:
            # re-register: keep the original is_default
            is_default = bool(existing[0].get("is_default"))
            status = "updated"
        else:
            is_default = not self._has_default(framework)
            status = "registered"

        data = dict(spec)
        if uploaded_by is not None:
            data["uploaded_by"] = uploaded_by

        entry = {
            "service_id": sid,
            "framework": framework,
            "framework_version": framework_version,
            "is_default": 1 if is_default else 0,
            "data": data,
        }
        self._table_svc.register(IMAGE_REGISTRY, entry)
        logger.info(
            "register_image %s@%s (is_default=%s, status=%s)",
            framework, framework_version, is_default, status,
        )
        return {
            "framework": framework,
            "framework_version": framework_version,
            "is_default": is_default,
            "status": status,
        }

    # ------------------------------------------------------------------
    # query (hierarchical)
    # ------------------------------------------------------------------

    def query(self, framework: Optional[str] = None) -> List[Dict[str, Any]]:
        """Group by framework and return ``{framework, default, versions:[…]}``.

        Optional ``framework`` filter. ``default`` is the framework's
        current default version (None if unset).
        """
        flt: Optional[Dict[str, Any]] = (
            {"framework": framework} if framework else None
        )
        rows = self._table_svc.query(IMAGE_REGISTRY, flt)

        groups: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            groups.setdefault(row["framework"], []).append(row)

        result: List[Dict[str, Any]] = []
        for fw, fw_rows in groups.items():
            default = self._pick_default_version(fw_rows)
            versions = [
                self._row_to_version_entry(r) for r in self._sort_versions(fw_rows)
            ]
            result.append({
                "framework": fw,
                "default": default,
                "versions": versions,
            })
        return result

    # ------------------------------------------------------------------
    # deregister
    # ------------------------------------------------------------------

    def deregister(self, framework: str, framework_version: str) -> Dict[str, Any]:
        """Deregister an image version.

        First verify no in-use instances (query the instance registry by
        framework+version); raise ``ImageInUseError`` if any. Otherwise
        delete the repo image file (stub) and delete the row; if the
        deregistered one was the default, promote the latest remaining
        version to default.
        """
        rows = self._table_svc.query(
            IMAGE_REGISTRY,
            {"framework": framework, "framework_version": framework_version},
        )
        if not rows:
            raise ImageNotFoundError(
                f"image {framework}@{framework_version} not found"
            )
        target = rows[0]
        was_default = bool(target.get("is_default"))

        # In-use instance check
        in_use = self._table_svc.query(
            INSTANCE_REGISTRY,
            {"framework": framework, "framework_version": framework_version},
        )
        if in_use:
            raise ImageInUseError(
                f"image {framework}@{framework_version} still has "
                f"{len(in_use)} in-use instance(s); cannot deregister"
            )

        # Repo image file deletion (stub; contract not confirmed)
        repo_deleted = self._delete_repo_image(target.get("data", {}))

        sid = image_sid(framework, framework_version)
        self._table_svc.deregister(IMAGE_REGISTRY, sid)

        # If the default was removed, promote the latest remaining version
        if was_default:
            self._promote_latest_default(framework)

        logger.info(
            "deregister_image %s@%s (was_default=%s, repo_deleted=%s)",
            framework, framework_version, was_default, repo_deleted,
        )
        return {
            "framework": framework,
            "framework_version": framework_version,
            "status": "deregistered",
            "repo_deleted": repo_deleted,
        }

    # ------------------------------------------------------------------
    # set_default / get_default_version
    # ------------------------------------------------------------------

    def set_default(self, framework: str, framework_version: str) -> Dict[str, Any]:
        """Set the default version: clear the framework's old
        ``is_default`` flags, then set the target version to 1."""
        sid = image_sid(framework, framework_version)
        # Verify the target row exists
        rows = self._table_svc.query(
            IMAGE_REGISTRY,
            {"framework": framework, "framework_version": framework_version},
        )
        if not rows:
            raise ImageNotFoundError(
                f"image {framework}@{framework_version} not found"
            )

        # Clear all is_default flags for this framework
        fw_rows = self._table_svc.query(IMAGE_REGISTRY, {"framework": framework})
        for r in fw_rows:
            if bool(r.get("is_default")):
                self._table_svc.patch(
                    IMAGE_REGISTRY, r["service_id"], {"is_default": 0}
                )
        # Set the target as default
        self._table_svc.patch(IMAGE_REGISTRY, sid, {"is_default": 1})
        logger.info("set_default %s -> %s", framework, framework_version)
        return {
            "framework": framework,
            "default": framework_version,
            "status": "updated",
        }

    def get_default_version(self, framework: str) -> str:
        """Get the default version: ``WHERE framework=? AND is_default=1``;
        if unset, fall back to the latest version."""
        rows = self._table_svc.query(IMAGE_REGISTRY, {"framework": framework})
        if not rows:
            raise ImageNotFoundError(f"framework {framework} has no image records")
        default = self._pick_default_version(rows)
        if default is not None:
            return default
        # No default set → fall back to the latest version
        return self._sort_versions(rows)[0]["framework_version"]

    # ------------------------------------------------------------------
    # resolve_launch_spec
    # ------------------------------------------------------------------

    def resolve_launch_spec(
        self, framework: str, version: Optional[str] = None
    ) -> Dict[str, Any]:
        """Assemble the launch spec
        ``{framework, framework_version, rootfs, cpu, memory, ports, env}``.

        When ``version`` is omitted, the framework's default version is
        used. Looks up a single row by framework+version and extracts
        the fields.
        """
        ver = version or self.get_default_version(framework)
        rows = self._table_svc.query(
            IMAGE_REGISTRY,
            {"framework": framework, "framework_version": ver},
        )
        if not rows:
            raise ImageNotFoundError(
                f"image {framework}@{ver} not found"
            )
        data = rows[0].get("data", {}) or {}
        spec: Dict[str, Any] = {
            "framework": framework,
            "framework_version": ver,
        }
        for k in _LAUNCH_SPEC_FIELDS:
            if k in data:
                spec[k] = data[k]
        return spec

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------

    def _has_default(self, framework: str) -> bool:
        """Whether the framework already has a default-version row."""
        rows = self._table_svc.query(
            IMAGE_REGISTRY, {"framework": framework, "is_default": 1}
        )
        return bool(rows)

    @staticmethod
    def _pick_default_version(rows: List[Dict[str, Any]]) -> Optional[str]:
        """Pick the version with is_default=1 from the row set; None if absent."""
        for r in rows:
            if bool(r.get("is_default")):
                return r["framework_version"]
        return None

    @staticmethod
    def _sort_versions(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Sort by framework_version descending (latest first)."""
        return sorted(
            rows, key=lambda r: r.get("framework_version", ""), reverse=True
        )

    def _promote_latest_default(self, framework: str) -> None:
        """After the default was removed, promote the latest remaining
        version to default. No-op if no rows remain."""
        rows = self._table_svc.query(IMAGE_REGISTRY, {"framework": framework})
        if not rows:
            return
        latest = self._sort_versions(rows)[0]
        self._table_svc.patch(
            IMAGE_REGISTRY, latest["service_id"], {"is_default": 1}
        )

    @staticmethod
    def _row_to_version_entry(row: Dict[str, Any]) -> Dict[str, Any]:
        """Convert a DB row into a query-output version entry."""
        data = row.get("data", {}) or {}
        return {
            "framework_version": row["framework_version"],
            "is_default": bool(row.get("is_default")),
            "rootfs": data.get("rootfs", {}),
            "cpu": data.get("cpu", 0),
            "memory": data.get("memory", 0),
            "ports": data.get("ports", []),
            "env": data.get("env", {}),
            "image_module_version": data.get("image_module_version"),
        }

    def _delete_repo_image(self, data: Dict[str, Any]) -> bool:
        """Repo image file deletion stub (contract not confirmed).

        When ``A2X_REGISTRY_REPO_BASE`` is not configured, logs a warning
        and returns False (does not block deregistration). Replace with
        the real call once the contract is confirmed.
        """
        repo_base = os.environ.get(_ENV_REPO_BASE, "").strip()
        if not repo_base:
            logger.warning(
                "A2X_REGISTRY_REPO_BASE not configured; skipping repo "
                "image file deletion (stub)"
            )
            return False
        rootfs = data.get("rootfs", {}) or {}
        imageurl = rootfs.get("imageurl")
        logger.info(
            "[stub] repo image deletion not implemented: "
            "repo_base=%s imageurl=%s", repo_base, imageurl
        )
        return False
