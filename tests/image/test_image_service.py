"""ImageService 业务逻辑测试（memory 后端，V2 扁平模型）。

覆盖：
- register_image：首次自动默认、re-register 保留默认 + 更新 spec、第二版本非默认、version_key 落库
- query：扁平返回、framework/uploaded_by 过滤、分页、total 计数
- get_default_version：显式默认 + 未设取最新
- set_default：清旧置新
- resolve_launch_spec：带/不带 version、扁平 spec（无 rootfs）
- deregister：无在用→删、有在用→409、删默认→补最新、镜像不存在→404
"""

from __future__ import annotations

import pytest

from a2x_registry.image.errors import ImageInUseError, ImageNotFoundError
from a2x_registry.image.service import ImageService

from .conftest import make_spec


def _default_user() -> str:
    return "user-01"


# ── register_image ──────────────────────────────────────────────

def test_register_first_version_becomes_default(image_svc: ImageService):
    result = image_svc.register_image(
        "opencode", "v0.2.0", make_spec(), _default_user(),
    )
    assert result == {
        "framework": "opencode",
        "framework_version": "v0.2.0",
        "is_default": True,
        "status": "registered",
    }


def test_register_second_version_not_default(image_svc: ImageService):
    image_svc.register_image("opencode", "v0.2.0", make_spec(), _default_user())
    result = image_svc.register_image(
        "opencode", "v0.1.0", make_spec(cpu=500), _default_user(),
    )
    assert result["is_default"] is False
    assert result["status"] == "registered"


def test_register_reregister_preserves_default_and_updates_spec(image_svc: ImageService):
    image_svc.register_image("opencode", "v0.2.0", make_spec(cpu=1000), _default_user())
    result = image_svc.register_image(
        "opencode", "v0.2.0", make_spec(cpu=2000), _default_user(),
    )
    assert result["is_default"] is True
    assert result["status"] == "updated"
    spec = image_svc.resolve_launch_spec("opencode", "v0.2.0")
    assert spec["cpu"] == 2000


def test_register_empty_framework_rejected(image_svc: ImageService):
    with pytest.raises(Exception):
        image_svc.register_image("", "v0.2.0", make_spec(), _default_user())


def test_register_version_key_is_stored(image_svc: ImageService):
    """V2: version_key is computed and stored as promoted column."""
    image_svc.register_image("opencode", "v0.2.0", make_spec(), _default_user())
    image_svc.register_image("opencode", "v0.10.0", make_spec(), _default_user())
    rows, total = image_svc.query(framework="opencode")
    # v0.10.0 should sort before v0.2.0 (version_key DESC)
    versions = [r["framework_version"] for r in rows]
    assert versions == ["v0.10.0", "v0.2.0"]


# ── query (flat + paginated) ────────────────────────────────────

def test_query_flat_returns_rows(image_svc: ImageService):
    image_svc.register_image("opencode", "v0.2.0", make_spec(), _default_user())
    image_svc.register_image("ninequery", "v1.0.0", make_spec(), _default_user())
    rows, total = image_svc.query()
    assert total == 2
    assert len(rows) == 2
    frameworks = {r["framework"] for r in rows}
    assert frameworks == {"opencode", "ninequery"}


def test_query_filter_by_framework(image_svc: ImageService):
    image_svc.register_image("opencode", "v0.2.0", make_spec(), _default_user())
    image_svc.register_image("ninequery", "v1.0.0", make_spec(), _default_user())
    rows, total = image_svc.query(framework="opencode")
    assert total == 1
    assert rows[0]["framework"] == "opencode"


def test_query_filter_by_uploaded_by(image_svc: ImageService):
    image_svc.register_image("opencode", "v0.2.0", make_spec(), "alice")
    image_svc.register_image("ninequery", "v1.0.0", make_spec(), "bob")
    rows, total = image_svc.query(uploaded_by="alice")
    assert total == 1
    assert rows[0]["uploaded_by"] == "alice"


def test_query_pagination(image_svc: ImageService):
    """size=2, page=1 on 3 rows -> return 2 rows, total=3."""
    for fw, ver in [
        ("opencode", "v0.3.0"),
        ("opencode", "v0.2.0"),
        ("opencode", "v0.1.0"),
    ]:
        image_svc.register_image(fw, ver, make_spec(), _default_user())
    rows, total = image_svc.query(size=2, page=1)
    assert total == 3
    assert len(rows) == 2


def test_query_pagination_page2(image_svc: ImageService):
    for fw, ver in [
        ("opencode", "v0.3.0"),
        ("opencode", "v0.2.0"),
        ("opencode", "v0.1.0"),
    ]:
        image_svc.register_image(fw, ver, make_spec(), _default_user())
    rows, total = image_svc.query(size=2, page=2)
    assert total == 3
    assert len(rows) == 1


def test_query_empty_returns_empty(image_svc: ImageService):
    rows, total = image_svc.query()
    assert rows == []
    assert total == 0


# ── get_default_version ─────────────────────────────────────────

def test_get_default_version_explicit(image_svc: ImageService):
    image_svc.register_image("opencode", "v0.2.0", make_spec(), _default_user())
    image_svc.register_image("opencode", "v0.1.0", make_spec(cpu=500), _default_user())
    assert image_svc.get_default_version("opencode") == "v0.2.0"


def test_get_default_version_falls_back_to_latest(image_svc: ImageService):
    image_svc.register_image("opencode", "v0.1.0", make_spec(), _default_user())
    image_svc.register_image("opencode", "v0.2.0", make_spec(), _default_user())
    from a2x_registry.common.ids import image_sid
    sid = image_sid("opencode", "v0.1.0")
    image_svc._table_svc.patch("images", sid, {"is_default": 0})
    sid2 = image_sid("opencode", "v0.2.0")
    image_svc._table_svc.patch("images", sid2, {"is_default": 0})
    assert image_svc.get_default_version("opencode") == "v0.2.0"


def test_get_default_version_framework_not_found(image_svc: ImageService):
    with pytest.raises(ImageNotFoundError):
        image_svc.get_default_version("nonexistent")


# ── set_default ─────────────────────────────────────────────────

def test_set_default_clears_old_and_sets_new(image_svc: ImageService):
    image_svc.register_image("opencode", "v0.2.0", make_spec(), _default_user())
    image_svc.register_image("opencode", "v0.1.0", make_spec(cpu=500), _default_user())
    result = image_svc.set_default("opencode", "v0.1.0")
    assert result == {"framework": "opencode", "default": "v0.1.0", "status": "updated"}
    assert image_svc.get_default_version("opencode") == "v0.1.0"


def test_set_default_target_not_found(image_svc: ImageService):
    image_svc.register_image("opencode", "v0.2.0", make_spec(), _default_user())
    with pytest.raises(ImageNotFoundError):
        image_svc.set_default("opencode", "v9.9.9")


# ── resolve_launch_spec ─────────────────────────────────────────

def test_resolve_launch_spec_with_version(image_svc: ImageService):
    image_svc.register_image("opencode", "v0.2.0", make_spec(cpu=1500), _default_user())
    spec = image_svc.resolve_launch_spec("opencode", "v0.2.0")
    assert spec["framework"] == "opencode"
    assert spec["framework_version"] == "v0.2.0"
    assert spec["cpu"] == 1500
    assert spec["memory"] == 2048
    assert spec["imageurl"] == "harbor.local/adapted/opencode:v0.2.0"
    assert spec["ports"] == [{"port": 8080, "protocol": "tcp"}]
    assert spec["env"] == {"A2X_LLM_KEY": "${A2X_LLM_KEY}"}
    # V2: no rootfs wrapper
    assert "rootfs" not in spec


def test_resolve_launch_spec_uses_default_version(image_svc: ImageService):
    image_svc.register_image("opencode", "v0.2.0", make_spec(cpu=1000), _default_user())
    image_svc.register_image("opencode", "v0.1.0", make_spec(cpu=500), _default_user())
    spec = image_svc.resolve_launch_spec("opencode")
    assert spec["framework_version"] == "v0.2.0"
    assert spec["cpu"] == 1000


def test_resolve_launch_spec_not_found(image_svc: ImageService):
    with pytest.raises(ImageNotFoundError):
        image_svc.resolve_launch_spec("nonexistent")


# ── deregister ──────────────────────────────────────────────────

def test_deregister_removes_version(image_svc: ImageService):
    image_svc.register_image("opencode", "v0.2.0", make_spec(), _default_user())
    result = image_svc.deregister("opencode", "v0.2.0")
    assert result["status"] == "deregistered"
    rows, _ = image_svc.query()
    assert rows == []


def test_deregister_default_promotes_latest(image_svc: ImageService):
    image_svc.register_image("opencode", "v0.2.0", make_spec(), _default_user())
    image_svc.register_image("opencode", "v0.1.0", make_spec(cpu=500), _default_user())
    image_svc.deregister("opencode", "v0.2.0")
    assert image_svc.get_default_version("opencode") == "v0.1.0"


def test_deregister_non_default_keeps_default(image_svc: ImageService):
    image_svc.register_image("opencode", "v0.2.0", make_spec(), _default_user())
    image_svc.register_image("opencode", "v0.1.0", make_spec(cpu=500), _default_user())
    image_svc.deregister("opencode", "v0.1.0")
    assert image_svc.get_default_version("opencode") == "v0.2.0"


def test_deregister_in_use_raises_409(image_svc: ImageService):
    image_svc.register_image("opencode", "v0.2.0", make_spec(), _default_user())
    image_svc._table_svc.register("instances", {
        "service_id": "generic_abc123",
        "kind": "三方",
        "framework": "opencode",
        "framework_version": "v0.2.0",
        "node": "node-1",
        "user": "user-01",
        "data": {},
    })
    with pytest.raises(ImageInUseError):
        image_svc.deregister("opencode", "v0.2.0")
    rows, _ = image_svc.query()
    assert len(rows) == 1


def test_deregister_not_found(image_svc: ImageService):
    with pytest.raises(ImageNotFoundError):
        image_svc.deregister("nonexistent", "v0.0.0")


def test_deregister_repo_stub_does_not_block(image_svc: ImageService, monkeypatch):
    monkeypatch.delenv("A2X_REGISTRY_REPO_BASE", raising=False)
    image_svc.register_image("opencode", "v0.2.0", make_spec(), _default_user())
    result = image_svc.deregister("opencode", "v0.2.0")
    assert result["repo_deleted"] is False
    assert result["status"] == "deregistered"