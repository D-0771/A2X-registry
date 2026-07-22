"""镜像管理测试 fixtures。

仅验证 sqlite memory 模式。每个测试拿到独立的 memory backend + schema
+ 镜像/实例注册表，互不干扰。
"""

from __future__ import annotations

import pytest

from a2x_registry.common.db import connect, init_schema
from a2x_registry.register.service import RegistryTableService
from a2x_registry.image.service import ImageService
from a2x_registry.image.deps import set_image_service


@pytest.fixture
def table_svc():
    backend = connect({"kind": "memory"})
    init_schema(backend.conn)
    svc = RegistryTableService(backend)
    svc.create_registry("images", "image")
    svc.create_registry("instances", "instance")
    yield svc


@pytest.fixture
def image_svc(table_svc):
    svc = ImageService(table_svc)
    set_image_service(svc)
    yield svc
    set_image_service(None)


def make_spec(
    imageurl: str = "harbor.local/adapted/opencode:v0.2.0",
    cpu: int = 1000,
    memory: int = 2048,
) -> dict:
    """构造一个最小可用的元戎运行规格（V2 扁平，无 rootfs 包装）。"""
    return {
        "imageurl": imageurl,
        "workdir": "/app",
        "mounts": [{"source": "/data/agent", "target": "/data"}],
        "cpu": cpu,
        "memory": memory,
        "ports": [{"port": 8080, "protocol": "tcp"}],
        "env": {"A2X_LLM_KEY": "${A2X_LLM_KEY}"},
        "image_module_version": "v1.3",
    }