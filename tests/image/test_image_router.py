"""镜像管理 router 测试（memory 后端，TestClient）。

覆盖端点 + HTTP 状态码映射：
- POST /api/images → 200 registered / updated
- GET /api/images → 分组列表
- GET /api/images/{fw}/launch-spec → 200 / 404
- PUT /api/images/{fw}/default → 200 / 404
- DELETE /api/images/{fw}/{ver} → 200 / 409 在用 / 404 不存在
- 未装配镜像模块（image_svc 未注入）→ 404
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from a2x_registry.image.deps import set_image_service
from a2x_registry.image.router import router as image_router

from .conftest import make_spec


def _make_app() -> FastAPI:
    """构建只挂 image router 的最小 app。"""
    app = FastAPI()
    app.include_router(image_router)
    return app


@pytest.fixture
def client(image_svc):
    """TestClient，image_svc fixture 已注入全局 deps。"""
    app = _make_app()
    return TestClient(app)


def _register(client, fw="opencode", ver="v0.2.0", spec=None):
    if spec is None:
        spec = make_spec()
    return client.post("/api/images", json={
        "framework": fw, "framework_version": ver, "spec": spec,
    })


# ── POST /api/images ────────────────────────────────────────────

def test_post_register_first_default(client):
    r = _register(client)
    assert r.status_code == 200
    body = r.json()
    assert body["framework"] == "opencode"
    assert body["framework_version"] == "v0.2.0"
    assert body["is_default"] is True
    assert body["status"] == "registered"


def test_post_reregister_updated(client):
    _register(client)
    r = _register(client, spec=make_spec(cpu=2000))
    assert r.status_code == 200
    assert r.json()["status"] == "updated"


# ── GET /api/images ─────────────────────────────────────────────

def test_get_list_grouped(client):
    _register(client, ver="v0.2.0")
    _register(client, ver="v0.1.0", spec=make_spec(cpu=500))
    r = client.get("/api/images")
    assert r.status_code == 200
    groups = r.json()
    assert len(groups) == 1
    assert groups[0]["framework"] == "opencode"
    assert groups[0]["default"] == "v0.2.0"
    assert len(groups[0]["versions"]) == 2


def test_get_list_filter_framework(client):
    _register(client, fw="opencode")
    _register(client, fw="ninequery", ver="v1.0.0")
    r = client.get("/api/images", params={"framework": "opencode"})
    assert r.status_code == 200
    groups = r.json()
    assert len(groups) == 1
    assert groups[0]["framework"] == "opencode"


def test_get_list_empty(client):
    r = client.get("/api/images")
    assert r.status_code == 200
    assert r.json() == []


# ── GET /api/images/{fw}/launch-spec ────────────────────────────

def test_get_launch_spec_with_version(client):
    _register(client, spec=make_spec(cpu=1500))
    r = client.get("/api/images/opencode/launch-spec", params={"version": "v0.2.0"})
    assert r.status_code == 200
    body = r.json()
    assert body["framework"] == "opencode"
    assert body["framework_version"] == "v0.2.0"
    assert body["cpu"] == 1500
    assert body["rootfs"]["imageurl"] == "harbor.local/adapted/opencode:v0.2.0"


def test_get_launch_spec_default_version(client):
    _register(client, ver="v0.2.0", spec=make_spec(cpu=1000))
    _register(client, ver="v0.1.0", spec=make_spec(cpu=500))
    r = client.get("/api/images/opencode/launch-spec")
    assert r.status_code == 200
    assert r.json()["framework_version"] == "v0.2.0"
    assert r.json()["cpu"] == 1000


def test_get_launch_spec_not_found(client):
    r = client.get("/api/images/nonexistent/launch-spec")
    assert r.status_code == 404


# ── PUT /api/images/{fw}/default ────────────────────────────────

def test_put_set_default(client):
    _register(client, ver="v0.2.0")
    _register(client, ver="v0.1.0", spec=make_spec(cpu=500))
    r = client.put("/api/images/opencode/default", json={"framework_version": "v0.1.0"})
    assert r.status_code == 200
    assert r.json()["default"] == "v0.1.0"
    # 确认生效
    r2 = client.get("/api/images/opencode/launch-spec")
    assert r2.json()["framework_version"] == "v0.1.0"


def test_put_set_default_not_found(client):
    _register(client, ver="v0.2.0")
    r = client.put("/api/images/opencode/default", json={"framework_version": "v9.9.9"})
    assert r.status_code == 404


# ── DELETE /api/images/{fw}/{ver} ───────────────────────────────

def test_delete_deregister(client):
    _register(client)
    r = client.delete("/api/images/opencode/v0.2.0")
    assert r.status_code == 200
    assert r.json()["status"] == "deregistered"
    assert client.get("/api/images").json() == []


def test_delete_in_use_409(client, image_svc):
    _register(client)
    # 注册在用实例
    image_svc._table_svc.register("实例注册表", {
        "service_id": "generic_abc123",
        "kind": "三方",
        "framework": "opencode",
        "framework_version": "v0.2.0",
        "node": "node-1",
        "user": "user-01",
        "data": {},
    })
    r = client.delete("/api/images/opencode/v0.2.0")
    assert r.status_code == 409


def test_delete_not_found(client):
    r = client.delete("/api/images/nonexistent/v0.0.0")
    assert r.status_code == 404


# ── 未装配镜像模块 → 404 ─────────────────────────────────────────

def test_routes_404_when_not_assembled():
    """image_svc 未注入时所有路由返回 404。"""
    set_image_service(None)
    app = _make_app()
    c = TestClient(app)
    assert c.get("/api/images").status_code == 404
    assert c.post("/api/images", json={
        "framework": "x", "framework_version": "v1", "spec": make_spec(),
    }).status_code == 404
