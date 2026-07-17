"""InstanceService — instance management business logic.

Pure recorder: register / update / deregister / query / expire_node.
Does not invoke the runtime (元戎) or make decisions for the gateway.

Persistence goes through ``RegistryTableService`` (SQL backend); this
service does not hold a backend/store directly. The ``data`` JSON column
holds ``{address, created_at, last_active_at}`` — these are not promoted
columns, so ``update_instance`` must merge ``address`` into the existing
``data`` dict before patching.

``status`` (运行 / 异常) is never persisted — it is derived per-query
from a node-heartbeat callback injected via ``set_heartbeat_check``.
When no callback is injected (P0-4 standalone, or heartbeat module not
loaded), all instances are considered healthy (运行). P0-5 will inject
the real per-node expiration check.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from a2x_registry.common.ids import now_iso
from a2x_registry.register.service import RegistryTableService

from .errors import InstanceNotFoundError, InstanceValidationError

logger = logging.getLogger(__name__)

# Named registry identifier (must match the name created by startup.py
# in appliance mode; stored in the ``registry`` column of the instance
# table and is part of the design-spec data contract).
INSTANCE_REGISTRY = "实例注册表"

# Accepted instance kinds (OpenAPI enum).
_VALID_KINDS = ("三方", "九问")

# Required fields for register_instance.
_REQUIRED_FIELDS = (
    "service_id", "kind", "framework", "framework_version",
    "node", "address", "user",
)

# Callback type: (node_ip) -> is_expired
NodeExpiredCheck = Callable[[str], bool]


class InstanceService:
    """Instance management business layer.

    Injects ``RegistryTableService`` for persistence. A heartbeat
    expiration callback (``set_heartbeat_check``) is optionally injected
    for ``_derive_status``; when absent, all instances are 运行.
    """

    __slots__ = ("_table_svc", "_is_node_expired")

    def __init__(self, table_svc: RegistryTableService) -> None:
        self._table_svc = table_svc
        self._is_node_expired: Optional[NodeExpiredCheck] = None

    # ------------------------------------------------------------------
    # Heartbeat injection (P0-5 will wire the real callback)
    # ------------------------------------------------------------------

    def set_heartbeat_check(self, callback: Optional[NodeExpiredCheck]) -> None:
        """Inject (or clear) the node-expiration callback for _derive_status.

        ``callback(node_ip) -> bool``: True if the node is expired/unhealthy.
        ``None`` resets to the default (all healthy → 运行).
        """
        self._is_node_expired = callback

    # ------------------------------------------------------------------
    # register_instance (idempotent upsert)
    # ------------------------------------------------------------------

    def register_instance(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        """Upsert an instance by ``service_id``.

        On re-register: preserves ``created_at`` from the existing row,
        refreshes ``last_active_at``, and overwrites node/address. All
        promoted columns (kind/framework/framework_version/node/user) are
        refreshed from the provided entry.
        """
        self._validate_entry(entry)

        sid = entry["service_id"]
        now = now_iso()

        # Preserve created_at on re-register (data is overwritten on upsert).
        existing = self._table_svc.query(
            INSTANCE_REGISTRY, {"service_id": sid}
        )
        if existing:
            old_data = existing[0].get("data", {}) or {}
            created_at = old_data.get("created_at", now)
        else:
            created_at = now

        db_entry = {
            "service_id": sid,
            "kind": entry["kind"],
            "framework": entry["framework"],
            "framework_version": entry["framework_version"],
            "node": entry["node"],
            "user": entry["user"],
            "data": {
                "address": entry["address"],
                "created_at": created_at,
                "last_active_at": now,
            },
        }
        stored = self._table_svc.register(INSTANCE_REGISTRY, db_entry)
        logger.info("register_instance %s (node=%s)", sid, entry["node"])
        return self._to_entry(stored)

    # ------------------------------------------------------------------
    # update_instance (partial update: node / address)
    # ------------------------------------------------------------------

    def update_instance(
        self, service_id: str, fields: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Partially update an instance's node and/or address.

        ``address`` lives in the ``data`` JSON, so it is merged into the
        existing data dict before patching. ``last_active_at`` is refreshed
        whenever ``address`` changes (migration = activity).

        Raises ``InstanceNotFoundError`` if the instance does not exist.
        """
        has_node = fields.get("node") is not None
        has_address = fields.get("address") is not None
        if not has_node and not has_address:
            raise InstanceValidationError(
                "at least one of node/address must be provided"
            )

        existing = self._table_svc.query(
            INSTANCE_REGISTRY, {"service_id": service_id}
        )
        if not existing:
            raise InstanceNotFoundError(
                f"instance '{service_id}' not found"
            )

        row = existing[0]
        data = dict(row.get("data", {}) or {})

        patch_fields: Dict[str, Any] = {}
        if has_node:
            patch_fields["node"] = fields["node"]
        if has_address:
            data["address"] = fields["address"]
            data["last_active_at"] = now_iso()
            patch_fields["data"] = data

        updated = self._table_svc.patch(INSTANCE_REGISTRY, service_id, patch_fields)
        logger.info("update_instance %s (fields=%s)", service_id, sorted(patch_fields))
        return self._to_entry(updated)

    # ------------------------------------------------------------------
    # deregister_instance (idempotent delete)
    # ------------------------------------------------------------------

    def deregister_instance(self, service_id: str) -> Dict[str, Any]:
        """Delete an instance by ``service_id``. Idempotent.

        Returns ``{service_id, deleted}`` where ``deleted`` is True if a
        row was removed, False if it was already absent.
        """
        deleted = self._table_svc.deregister(INSTANCE_REGISTRY, service_id)
        logger.info(
            "deregister_instance %s (deleted=%s)", service_id, deleted
        )
        return {"service_id": service_id, "deleted": deleted}

    # ------------------------------------------------------------------
    # list_instances (query + derive status)
    # ------------------------------------------------------------------

    def list_instances(
        self,
        filter: Optional[Dict[str, Any]] = None,
        include_unhealthy: bool = False,
    ) -> List[Dict[str, Any]]:
        """Query instances with optional equality filters on promoted columns.

        When ``include_unhealthy`` is False (default), instances whose
        derived status is 异常 are excluded from the result.
        """
        rows = self._table_svc.query(INSTANCE_REGISTRY, filter)
        result: List[Dict[str, Any]] = []
        for row in rows:
            entry = self._to_entry(row)
            if not include_unhealthy and entry["status"] == "异常":
                continue
            result.append(entry)
        return result

    # ------------------------------------------------------------------
    # expire_node (heartbeat sweeper callback)
    # ------------------------------------------------------------------

    def expire_node(self, node: str) -> None:
        """Delete all instances on a node. Called by the heartbeat sweeper
        when a node exceeds the grace period. Idempotent.
        """
        rows = self._table_svc.query(INSTANCE_REGISTRY, {"node": node})
        for row in rows:
            self._table_svc.deregister(INSTANCE_REGISTRY, row["service_id"])
        logger.info("expire_node %s (removed=%d)", node, len(rows))

    # ------------------------------------------------------------------
    # distinct_nodes (restart recovery for P0-5)
    # ------------------------------------------------------------------

    def distinct_nodes(self) -> List[str]:
        """Return sorted distinct node IPs that have registered instances.

        Used by P0-5 ``recover_from_persisted(distinct_nodes)`` to rebuild
        per-node heartbeat leases after a registry restart.
        """
        rows = self._table_svc.query(INSTANCE_REGISTRY)
        return sorted({r["node"] for r in rows if r.get("node")})

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------

    def _derive_status(self, node: str) -> str:
        """Derive instance status from the node heartbeat.

        ``异常`` if the injected heartbeat callback says the node is
        expired; ``运行`` otherwise (including when no callback is set).
        """
        if self._is_node_expired is not None and self._is_node_expired(node):
            return "异常"
        return "运行"

    @staticmethod
    def _validate_entry(entry: Dict[str, Any]) -> None:
        """Validate required fields and kind enum."""
        for field in _REQUIRED_FIELDS:
            val = entry.get(field)
            if val is None or val == "":
                raise InstanceValidationError(
                    f"missing required field: {field}"
                )
        if entry["kind"] not in _VALID_KINDS:
            raise InstanceValidationError(
                f"invalid kind: {entry['kind']!r}, must be one of {_VALID_KINDS}"
            )

    def _to_entry(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """Convert a DB row (merged entry dict) into an InstanceEntry dict
        with derived status.
        """
        data = row.get("data", {}) or {}
        node = row.get("node", "")
        return {
            "service_id": row["service_id"],
            "kind": row["kind"],
            "framework": row["framework"],
            "framework_version": row["framework_version"],
            "node": node,
            "address": data.get("address", ""),
            "user": row["user"],
            "created_at": data.get("created_at", ""),
            "last_active_at": data.get("last_active_at", ""),
            "status": self._derive_status(node),
        }
