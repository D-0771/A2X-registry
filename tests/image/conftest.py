"""镜像管理测试 fixtures。

仅验证 sqlite memory 模式（按用户要求，暂时跳过 rqlite 和本地 sqlite 文件）。
每个测试拿到独立的 memory backend + schema + 镜像/实例注册表，互不干扰。
"""

from __future__ import annotations

import pytest

from a2x_registry.common.db import connect, init_schema
from a2x_registry.register.service import RegistryTableService
from a2x_registry.image.service import ImageService
from a2x_registry.image.deps import set_image_service


@pytest.fixture
def table_svc():
    """独立的 memory backend + schema + 镜像/实例注册表。

    每个测试全新的 :memory: 库，无残留。同时建实例注册表供 deregister
    在用校验测试。
    """
    backend = connect({"kind": "memory"})
    init_schema(backend.conn)
    svc = RegistryTableService(backend)
    svc.create_registry("镜像注册表", "image")
    svc.create_registry("实例注册表", "instance")
    yield svc


@pytest.fixture
def image_svc(table_svc):
    """ImageService 装配好 table_svc。同时注入全局 deps 供 router 测试。"""
    svc = ImageService(table_svc)
    set_image_service(svc)
    yield svc
    set_image_service(None)


def make_spec(
    imageurl: str = "harbor.local/adapted/opencode:v0.2.0",
    cpu: int = 1000,
    memory: int = 2048,
) -> dict:
    """构造一个最小可用的元戎运行规格。"""
    return {
        "rootfs": {"type": "image", "imageurl": imageurl, "workdir": "/app"},
        "cpu": cpu,
        "memory": memory,
        "ports": [{"port": 8080, "protocol": "tcp"}],
        "env": {"A2X_LLM_KEY": "${A2X_LLM_KEY}"},
        "image_module_version": "v1.3",
    }
