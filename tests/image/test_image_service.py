"""ImageService 业务逻辑测试（memory 后端）。

覆盖：
- register_image：首次自动默认、re-register 保留默认 + 更新 spec、第二版本非默认
- query：分组、层次化、default 字段
- get_default_version：显式默认 + 未设取最新
- set_default：清旧置新
- resolve_launch_spec：带/不带 version、抽取镜像级字段
- deregister：无在用→删、有在用→409、删默认→补最新、镜像不存在→404
- repo delete stub：未配置 A2X_REGISTRY_REPO_BASE 不阻塞
"""

from __future__ import annotations

import pytest

from a2x_registry.image.errors import ImageInUseError, ImageNotFoundError
from a2x_registry.image.service import ImageService

from .conftest import make_spec


# ── register_image ──────────────────────────────────────────────

def test_register_first_version_becomes_default(image_svc: ImageService):
    """框架首个版本自动置 is_default=1。"""
    result = image_svc.register_image("opencode", "v0.2.0", make_spec())
    assert result == {
        "framework": "opencode",
        "framework_version": "v0.2.0",
        "is_default": True,
        "status": "registered",
    }


def test_register_second_version_not_default(image_svc: ImageService):
    """框架已有默认版本时，新版本 is_default=0。"""
    image_svc.register_image("opencode", "v0.2.0", make_spec())
    result = image_svc.register_image("opencode", "v0.1.0", make_spec(cpu=500))
    assert result["is_default"] is False
    assert result["status"] == "registered"


def test_register_reregister_preserves_default_and_updates_spec(image_svc: ImageService):
    """re-register 同 (fw, ver)：保留 is_default、更新 spec、status=updated。"""
    image_svc.register_image("opencode", "v0.2.0", make_spec(cpu=1000))
    # re-register with new spec
    result = image_svc.register_image("opencode", "v0.2.0", make_spec(cpu=2000))
    assert result["is_default"] is True
    assert result["status"] == "updated"
    # spec 已更新
    spec = image_svc.resolve_launch_spec("opencode", "v0.2.0")
    assert spec["cpu"] == 2000


def test_register_empty_framework_rejected(image_svc: ImageService):
    with pytest.raises(Exception):
        image_svc.register_image("", "v0.2.0", make_spec())


# ── query ───────────────────────────────────────────────────────

def test_query_groups_by_framework(image_svc: ImageService):
    image_svc.register_image("opencode", "v0.2.0", make_spec())
    image_svc.register_image("opencode", "v0.1.0", make_spec(cpu=500))
    image_svc.register_image("ninequery", "v1.0.0", make_spec())

    groups = image_svc.query()
    fw_names = {g["framework"] for g in groups}
    assert fw_names == {"opencode", "ninequery"}

    oc = next(g for g in groups if g["framework"] == "opencode")
    assert oc["default"] == "v0.2.0"
    assert len(oc["versions"]) == 2
    # 版本降序（最新在前）
    assert oc["versions"][0]["framework_version"] == "v0.2.0"
    assert oc["versions"][0]["is_default"] is True
    assert oc["versions"][1]["framework_version"] == "v0.1.0"
    assert oc["versions"][1]["is_default"] is False


def test_query_filter_by_framework(image_svc: ImageService):
    image_svc.register_image("opencode", "v0.2.0", make_spec())
    image_svc.register_image("ninequery", "v1.0.0", make_spec())

    groups = image_svc.query(framework="opencode")
    assert len(groups) == 1
    assert groups[0]["framework"] == "opencode"


def test_query_empty_returns_empty_list(image_svc: ImageService):
    assert image_svc.query() == []


# ── get_default_version ─────────────────────────────────────────

def test_get_default_version_explicit(image_svc: ImageService):
    image_svc.register_image("opencode", "v0.2.0", make_spec())
    image_svc.register_image("opencode", "v0.1.0", make_spec(cpu=500))
    assert image_svc.get_default_version("opencode") == "v0.2.0"


def test_get_default_version_falls_back_to_latest(image_svc: ImageService):
    """无显式默认时取最新版（降序首个）。"""
    image_svc.register_image("opencode", "v0.1.0", make_spec())
    image_svc.register_image("opencode", "v0.2.0", make_spec())
    # 手动清掉默认
    from a2x_registry.common.ids import image_sid
    sid = image_sid("opencode", "v0.1.0")
    image_svc._table_svc.patch("镜像注册表", sid, {"is_default": 0})
    sid2 = image_sid("opencode", "v0.2.0")
    image_svc._table_svc.patch("镜像注册表", sid2, {"is_default": 0})
    # 无默认 → 取最新版 v0.2.0
    assert image_svc.get_default_version("opencode") == "v0.2.0"


def test_get_default_version_framework_not_found(image_svc: ImageService):
    with pytest.raises(ImageNotFoundError):
        image_svc.get_default_version("nonexistent")


# ── set_default ─────────────────────────────────────────────────

def test_set_default_clears_old_and_sets_new(image_svc: ImageService):
    image_svc.register_image("opencode", "v0.2.0", make_spec())  # default
    image_svc.register_image("opencode", "v0.1.0", make_spec(cpu=500))

    result = image_svc.set_default("opencode", "v0.1.0")
    assert result == {"framework": "opencode", "default": "v0.1.0", "status": "updated"}
    assert image_svc.get_default_version("opencode") == "v0.1.0"


def test_set_default_target_not_found(image_svc: ImageService):
    image_svc.register_image("opencode", "v0.2.0", make_spec())
    with pytest.raises(ImageNotFoundError):
        image_svc.set_default("opencode", "v9.9.9")


# ── resolve_launch_spec ─────────────────────────────────────────

def test_resolve_launch_spec_with_version(image_svc: ImageService):
    image_svc.register_image("opencode", "v0.2.0", make_spec(cpu=1500))
    spec = image_svc.resolve_launch_spec("opencode", "v0.2.0")
    assert spec["framework"] == "opencode"
    assert spec["framework_version"] == "v0.2.0"
    assert spec["cpu"] == 1500
    assert spec["memory"] == 2048
    assert spec["rootfs"]["imageurl"] == "harbor.local/adapted/opencode:v0.2.0"
    assert spec["ports"] == [{"port": 8080, "protocol": "tcp"}]
    assert spec["env"] == {"A2X_LLM_KEY": "${A2X_LLM_KEY}"}
    # image_module_version 不在 launch-spec 输出中
    assert "image_module_version" not in spec


def test_resolve_launch_spec_uses_default_version(image_svc: ImageService):
    image_svc.register_image("opencode", "v0.2.0", make_spec(cpu=1000))
    image_svc.register_image("opencode", "v0.1.0", make_spec(cpu=500))
    spec = image_svc.resolve_launch_spec("opencode")  # 不带 version
    assert spec["framework_version"] == "v0.2.0"  # 默认版本
    assert spec["cpu"] == 1000


def test_resolve_launch_spec_not_found(image_svc: ImageService):
    with pytest.raises(ImageNotFoundError):
        image_svc.resolve_launch_spec("nonexistent")


# ── deregister ──────────────────────────────────────────────────

def test_deregister_removes_version(image_svc: ImageService):
    image_svc.register_image("opencode", "v0.2.0", make_spec())
    result = image_svc.deregister("opencode", "v0.2.0")
    assert result["status"] == "deregistered"
    assert image_svc.query() == []


def test_deregister_default_promotes_latest(image_svc: ImageService):
    """删默认版本后，剩余最新版补为默认。"""
    image_svc.register_image("opencode", "v0.2.0", make_spec())  # default
    image_svc.register_image("opencode", "v0.1.0", make_spec(cpu=500))

    image_svc.deregister("opencode", "v0.2.0")
    # v0.1.0 应被补为默认
    assert image_svc.get_default_version("opencode") == "v0.1.0"


def test_deregister_non_default_keeps_default(image_svc: ImageService):
    image_svc.register_image("opencode", "v0.2.0", make_spec())  # default
    image_svc.register_image("opencode", "v0.1.0", make_spec(cpu=500))

    image_svc.deregister("opencode", "v0.1.0")
    assert image_svc.get_default_version("opencode") == "v0.2.0"


def test_deregister_in_use_raises_409(image_svc: ImageService):
    """有在用实例时注销 → ImageInUseError。"""
    image_svc.register_image("opencode", "v0.2.0", make_spec())
    # 注册一个在用实例
    image_svc._table_svc.register("实例注册表", {
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
    # 镜像仍存在
    assert len(image_svc.query()) == 1


def test_deregister_not_found(image_svc: ImageService):
    with pytest.raises(ImageNotFoundError):
        image_svc.deregister("nonexistent", "v0.0.0")


def test_deregister_repo_stub_does_not_block(image_svc: ImageService, monkeypatch):
    """未配置 A2X_REGISTRY_REPO_BASE 时 repo 删除 stub 不阻塞注销。"""
    monkeypatch.delenv("A2X_REGISTRY_REPO_BASE", raising=False)
    image_svc.register_image("opencode", "v0.2.0", make_spec())
    result = image_svc.deregister("opencode", "v0.2.0")
    assert result["repo_deleted"] is False
    assert result["status"] == "deregistered"
