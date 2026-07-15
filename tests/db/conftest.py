"""tests/db 数据层测试共享 fixtures。

三种后端契约测试 fixtures：sqlite（tmp 文件）、memory（``:memory:``）、
rqlite（启动临时 rqlited 进程）。所有后端共用同一份 SCHEMA_SQL + 参数化 SQL，
契约测试在三种后端上跑同一组断言。

预制 .db 文件由 `build_fixtures.sh` 用 sqlite3 CLI 生成（schema 真源来自
`a2x_registry/common/db.py` 的 `SCHEMA_SQL`）：
- `fixtures/empty.db`     —— 仅 schema，无数据
- `fixtures/appliance.db` —— schema + appliance 样例数据（只读）
"""

from __future__ import annotations

import json
import shutil
import socket
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterator

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ── 预制 .db 文件路径（只读） ─────────────────────────────────

@pytest.fixture
def empty_db_path() -> Path:
    """预制 empty.db 路径（仅 schema，无数据）。"""
    p = FIXTURES_DIR / "empty.db"
    assert p.exists(), (
        f"{p} 不存在；请运行 `bash tests/db/build_fixtures.sh` 重新生成"
    )
    return p


@pytest.fixture
def appliance_db_path() -> Path:
    """预制 appliance.db 路径（schema + 样例数据，只读校验用）。"""
    p = FIXTURES_DIR / "appliance.db"
    assert p.exists(), (
        f"{p} 不存在；请运行 `bash tests/db/build_fixtures.sh` 重新生成"
    )
    return p


# ── 只读连接（校验预制 fixture） ──────────────────────────────

@pytest.fixture
def appliance_conn(appliance_db_path) -> Iterator[sqlite3.Connection]:
    """只读连接到 appliance.db；测试结束自动关闭。"""
    conn = sqlite3.connect(
        f"file:{appliance_db_path}?mode=ro", uri=True
    )
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


# ── 可写连接（隔离 CRUD 测试，不污染预制 fixture） ────────────

@pytest.fixture
def fresh_conn(tmp_path) -> Iterator[sqlite3.Connection]:
    """全新空 db（tmp_path），按源码 init_schema 建齐 4 表。

    复用 `a2x_registry.common.db.init_schema` —— 测试与源码共用同一份
    SCHEMA_SQL 真源，避免漂移。每个 CRUD 测试拿到的都是干净库，互不影响；
    测试结束 tmp_path 自动清理。
    """
    from a2x_registry.common.db import init_schema

    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def appliance_writable_copy(tmp_path, appliance_db_path) -> Iterator[sqlite3.Connection]:
    """appliance.db 的可写副本（tmp_path）—— 需要在样例数据上做变更测试时用。

    原始 fixture 保持只读；本 fixture 先复制到 tmp_path 再开可写连接。
    """
    import shutil
    dst = tmp_path / "appliance_copy.db"
    shutil.copy2(appliance_db_path, dst)
    conn = sqlite3.connect(str(dst))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


# ── 三后端契约测试 fixtures ──────────────────────────────────
# sqlite / memory / rqlite 三个 fixture 共用同一组测试断言（参数化 via
# `backend_factory`），共享同一份 SCHEMA_SQL。rqlite 后端启动一个临时
# rqlited 进程，session 范围内所有测试复用同一个进程。

def _free_port() -> int:
    """Pick a free TCP port on 127.0.0.1 for rqlited to listen on."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _rqlite_ready(endpoint: str) -> bool:
    """Probe rqlite /leader until a leader is elected (max ~12s).

    rqlite HTTP responds before the Raft leader is elected, so just checking
    the port is not enough — writes return 503 "leader not found". The /leader
    endpoint returns a node object with ``"leader": true`` once election
    completes; before that it returns an empty body or a node with
    ``"leader": false``.
    """
    for _ in range(60):
        try:
            with urllib.request.urlopen(f"{endpoint}/leader", timeout=0.5) as r:
                if r.status != 200:
                    time.sleep(0.2)
                    continue
                raw = r.read().decode("utf-8").strip()
                if not raw:
                    time.sleep(0.2)
                    continue
                payload = json.loads(raw)
                if isinstance(payload, dict) and payload.get("leader") is True:
                    return True
        except (urllib.error.URLError, ConnectionError, OSError, json.JSONDecodeError):
            pass
        time.sleep(0.2)
    return False


@pytest.fixture(scope="session")
def rqlite_server(tmp_path_factory) -> Iterator[str]:
    """Start a single-node rqlited for the whole test session.

    Yields the HTTP endpoint (``http://127.0.0.1:<port>``). The process is
    terminated on teardown. Skips the session if ``rqlited`` is not on PATH
    or fails to elect a leader within the readiness window.
    """
    if shutil.which("rqlited") is None:
        pytest.skip("rqlited not installed; skip rqlite backend tests")

    data_dir = tmp_path_factory.mktemp("rqlite_data")
    log_path = tmp_path_factory.mktemp("rqlite_logs") / "rqlited.log"
    http_port = _free_port()
    raft_port = _free_port()
    # Capture stderr to a file so failure diagnostics are available.
    log_fh = open(log_path, "wb")
    proc = subprocess.Popen(
        [
            "rqlited",
            "-http-addr", f"127.0.0.1:{http_port}",
            "-raft-addr", f"127.0.0.1:{raft_port}",
            str(data_dir),
        ],
        stdout=log_fh,
        stderr=subprocess.STDOUT,
    )
    endpoint = f"http://127.0.0.1:{http_port}"
    try:
        if not _rqlite_ready(endpoint):
            log_fh.close()
            log_content = log_path.read_text(errors="replace")[-2000:]
            pytest.skip(
                f"rqlited failed to elect a leader within 12s. "
                f"Last log:\n{log_content}"
            )
        yield endpoint
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_fh.close()


@pytest.fixture
def sqlite_backend(tmp_path):
    """Fresh sqlite file backend with schema initialized."""
    from a2x_registry.common.db import connect, init_schema

    backend = connect({"kind": "sqlite", "path": str(tmp_path / "t.db")})
    init_schema(backend.conn)
    return backend


@pytest.fixture
def memory_backend():
    """In-memory sqlite backend with schema initialized (debug only)."""
    from a2x_registry.common.db import connect, init_schema

    backend = connect({"kind": "memory"})
    init_schema(backend.conn)
    return backend


@pytest.fixture
def rqlite_backend(rqlite_server):
    """rqlite backend with schema initialized; tables dropped on teardown.

    Re-initializes schema before each test (CREATE ... IF NOT EXISTS is
    idempotent). On teardown DROPs *every* user table (not just the 4 schema
    tables) because rqlite is session-scoped — tests often create scratch
    tables like ``t`` which must not leak into the next test.
    """
    from a2x_registry.common.db import connect, init_schema

    backend = connect({"kind": "rqlite", "endpoint": rqlite_server})
    init_schema(backend.conn)
    yield backend
    rows = backend.query("SELECT name FROM sqlite_master WHERE type='table'")
    for row in rows:
        backend.execute(f'DROP TABLE IF EXISTS "{row["name"]}"')


@pytest.fixture(params=["sqlite", "memory", "rqlite"])
def backend_factory(request):
    """Parametrized factory yielding (kind_name, backend) tuples.

    Each test runs once per backend kind. rqlite is skipped if rqlited is not
    installed (handled inside the rqlite_backend fixture).
    """
    if request.param == "sqlite":
        backend = request.getfixturevalue("sqlite_backend")
    elif request.param == "memory":
        backend = request.getfixturevalue("memory_backend")
    elif request.param == "rqlite":
        backend = request.getfixturevalue("rqlite_backend")
    else:
        pytest.fail(f"unknown backend param: {request.param}")
    return request.param, backend
