# 注册中心 API · 手工测试 curl 命令合集

> 基于 [registry_openapi.yaml](./registry_openapi.yaml) (v0.1.0)，覆盖一体机场景全部接口。
> 默认服务器：`http://127.0.0.1:8000`（设计文档 §4.1 默认 localhost）。
> 命令可直接复制到 bash 运行；每个接口选 1-2 个典型参数组合。

## 约定

- 成功响应只列关键字段，完整形状见 OpenAPI schema。
- 错误路径在每个接口末尾标注典型一种。
- 联调场景见末尾 §6。
- **数据库验证**：每个 curl 步骤后给出 `sqlite3` 命令，直接查 `$A2X_REGISTRY_DB` 验证落库效果。环境变量约定：
  ```bash
  # 730 单机 SQLite，路径由 registry.env 的 A2X_REGISTRY_HOME 决定
  export A2X_REGISTRY_DB="${A2X_REGISTRY_HOME:-/var/lib/a2x-registry}/registry.db"
  ```
- 表结构真源：[a2x_registry/common/db.py](./a2x_registry/common/db.py) 的 `SCHEMA_SQL`（4 表：`registry_meta` / `service` / `image` / `instance`）。
- **心跳活性不入库**（内存态）：§3 的验证只能查 `instance` 表是否被 sweeper 剔除，不能直接查心跳本身。

---

## 1. 镜像管理 `/api/images`

### 1.1 注册镜像

**接口**：`POST /api/images`
**场景**：镜像处理模块适配完成后，登记引用 + 元戎运行规格。

```bash
curl -X POST http://127.0.0.1:8000/api/images \
  -H "Content-Type: application/json" \
  -d '{
    "framework": "opencode",
    "framework_version": "v0.2.0",
    "spec": {
      "rootfs": {
        "type": "image",
        "imageurl": "harbor.local/adapted/opencode:v0.2.0-mod1.3",
        "workdir": "/app"
      },
      "cpu": 1000,
      "memory": 2048,
      "ports": [{"port": 8080, "protocol": "tcp"}],
      "env": {"A2X_LLM_KEY": "${A2X_LLM_KEY}"},
      "image_module_version": "v1.3"
    },
    "uploaded_by": "user-01"
  }'
```

**预期响应** `200`：
```json
{"framework": "opencode", "framework_version": "v0.2.0", "status": "registered"}
```

**效果**：按 `framework + framework_version` 幂等 upsert；该 framework 首次注册时自动置为默认版本。
**错误**：`spec.rootfs.imageurl` 缺失 → `400 {"detail":"spec.rootfs.imageurl 缺失"}`。

**数据库验证**：
```bash
# 1. image 表新增一行（registry='镜像注册表'），data JSON 含 imageurl/cpu
sqlite3 "$A2X_REGISTRY_DB" \
  "SELECT framework, framework_version, is_default,
          json_extract(data,'\$.rootfs.imageurl') AS imageurl,
          json_extract(data,'\$.cpu') AS cpu
   FROM image WHERE registry='镜像注册表' AND framework='opencode';"
# 预期：opencode|v0.2.0|1|harbor.local/adapted/opencode:v0.2.0-mod1.3|1000

# 2. registry_meta 已登记 '镜像注册表'（启动期 create_registry）
sqlite3 "$A2X_REGISTRY_DB" \
  "SELECT registry, kind FROM registry_meta WHERE registry='镜像注册表';"
# 预期：镜像注册表|image
```

### 1.2 查询镜像（按 framework 过滤）

**接口**：`GET /api/images?framework={fw}`

```bash
curl 'http://127.0.0.1:8000/api/images?framework=opencode'
```

**预期响应** `200`：
```json
[{
  "framework": "opencode",
  "default": "v0.2.0",
  "versions": [{"framework_version": "v0.2.0", "image_module_version": "v1.3", ...}]
}]
```

**效果**：按 framework 分组、内含多版本。不传 `?framework=` 返回全部。

**数据库验证**：
```bash
# 对照 image 表中该 framework 全部版本行 + 默认版本指针
sqlite3 "$A2X_REGISTRY_DB" \
  "SELECT framework_version, is_default,
          json_extract(data,'\$.image_module_version') AS mod_ver
   FROM image WHERE registry='镜像注册表' AND framework='opencode'
   ORDER BY is_default DESC, framework_version;"
# 预期首行：v0.2.0|1|v1.3（is_default=1 排在前）
```

### 1.3 取运行规格（gateway 拉起前调用）

**接口**：`GET /api/images/{framework}/launch-spec?version={ver}`

```bash
# 默认版本
curl http://127.0.0.1:8000/api/images/opencode/launch-spec

# 指定版本
curl 'http://127.0.0.1:8000/api/images/opencode/launch-spec?version=v0.2.0'
```

**预期响应** `200`：
```json
{
  "framework": "opencode", "framework_version": "v0.2.0",
  "rootfs": {"type": "image", "imageurl": "harbor.local/adapted/opencode:v0.2.0-mod1.3"},
  "cpu": 1000, "memory": 2048,
  "ports": [{"port": 8080, "protocol": "tcp"}],
  "env": {"A2X_LLM_KEY": "${A2X_LLM_KEY}"}
}
```

**效果**：返回元戎 Docker 沙箱运行规格；不带 version 取默认版本。
**错误**：framework 或版本不存在 → `404 {"detail":"..."}`。

**数据库验证**：
```bash
# 默认版本路径：查 is_default=1 那一行的 data
sqlite3 "$A2X_REGISTRY_DB" \
  "SELECT framework_version,
          json_extract(data,'\$.rootfs.imageurl') AS imageurl,
          json_extract(data,'\$.cpu') AS cpu,
          json_extract(data,'\$.memory') AS memory
   FROM image
   WHERE registry='镜像注册表' AND framework='opencode' AND is_default=1;"
# 预期：v0.2.0|harbor.local/adapted/opencode:v0.2.0-mod1.3|1000|2048
```

### 1.4 设默认版本

**接口**：`PUT /api/images/{framework}/default`

```bash
# 假设此前已注册 opencode 的 v0.1.0 与 v0.2.0 两个版本
curl -X PUT http://127.0.0.1:8000/api/images/opencode/default \
  -H "Content-Type: application/json" \
  -d '{"framework_version": "v0.2.0"}'
```

**预期响应** `200`：
```json
{"framework": "opencode", "default": "v0.2.0", "status": "updated"}
```

**效果**：清该 framework 旧 `is_default`、置新版为 1。
**错误**：framework 不存在 → `404`。

**数据库验证**：
```bash
# 同 framework 下应恰有一行 is_default=1（新版），其他均为 0
sqlite3 "$A2X_REGISTRY_DB" \
  "SELECT framework_version, is_default
   FROM image WHERE registry='镜像注册表' AND framework='opencode'
   ORDER BY framework_version;"
# 预期（两版本场景）：v0.1.0|0  /  v0.2.0|1
```

### 1.5 注销镜像

**接口**：`DELETE /api/images/{framework}/{version}`

```bash
curl -X DELETE http://127.0.0.1:8000/api/images/opencode/v0.2.0
```

**预期响应** `200`：
```json
{"framework": "opencode", "framework_version": "v0.2.0", "status": "deregistered"}
```

**效果**：先校验无在用实例 → 删镜像仓文件 → 删条目（删的是默认版本则把最新版补为默认）。
**错误**：仍有在用实例 → `409 {"code":"image_in_use","detail":"2 个实例仍在用","instances":[...]}`。

**数据库验证**：
```bash
# 1. image 表对应行已删除
sqlite3 "$A2X_REGISTRY_DB" \
  "SELECT COUNT(*) FROM image
   WHERE registry='镜像注册表' AND framework='opencode' AND framework_version='v0.2.0';"
# 预期：0

# 2. 若删的是默认版本，应自动补一个新默认（同 framework 下剩余行恰一行 is_default=1）
sqlite3 "$A2X_REGISTRY_DB" \
  "SELECT COUNT(*) FROM image
   WHERE registry='镜像注册表' AND framework='opencode' AND is_default=1;"
# 预期：1（如还有其他版本）或 0（如该 framework 已无任何版本）
```

---

## 2. 实例管理 `/api/instances`

### 2.1 注册实例（三方）

**接口**：`POST /api/instances`
**场景**：gateway 拿 launch-spec、调元戎拉起后，带落点注册。

```bash
curl -X POST http://127.0.0.1:8000/api/instances \
  -H "Content-Type: application/json" \
  -d '{
    "service_id": "generic_3f9a1b2c",
    "kind": "三方",
    "framework": "opencode",
    "framework_version": "v0.2.0",
    "node": "192.168.0.12",
    "address": "10.244.1.7:4096",
    "user": "user-01"
  }'
```

**预期响应** `200`：
```json
{
  "service_id": "generic_3f9a1b2c",
  "kind": "三方", "framework": "opencode", "framework_version": "v0.2.0",
  "address": "10.244.1.7:4096", "node": "192.168.0.12", "user": "user-01",
  "created_at": "2026-07-06T10:00:00Z",
  "last_active_at": "2026-07-06T10:00:00Z",
  "status": "运行"
}
```

**效果**：`service_id` 幂等 upsert；重发即覆盖。`service_id` 由 `instance_sid(user, framework)` 派生（每用户每框架一个实例）。

**数据库验证**：
```bash
# 1. instance 表新增一行（registry='实例注册表'），data JSON 含 address
sqlite3 "$A2X_REGISTRY_DB" \
  "SELECT service_id, kind, framework, framework_version, node, \"user\",
          json_extract(data,'\$.address') AS address,
          json_extract(data,'\$.created_at') AS created_at
   FROM instance
   WHERE registry='实例注册表' AND service_id='generic_3f9a1b2c';"
# 预期：generic_3f9a1b2c|三方|opencode|v0.2.0|192.168.0.12|user-01|10.244.1.7:4096|2026-07-06T10:00:00Z

# 2. registry_meta 已登记 '实例注册表'
sqlite3 "$A2X_REGISTRY_DB" \
  "SELECT registry, kind FROM registry_meta WHERE registry='实例注册表';"
# 预期：实例注册表|instance
```

### 2.2 注册实例（九问）

```bash
curl -X POST http://127.0.0.1:8000/api/instances \
  -H "Content-Type: application/json" \
  -d '{
    "service_id": "generic_9c21d4e5",
    "kind": "九问",
    "framework": "jiuwen-report",
    "framework_version": "v1.0.0",
    "node": "192.168.0.11",
    "address": "10.244.2.3:8080",
    "user": "user-02"
  }'
```

**效果**：九问流程与三方一致，仅 `kind=九问`、framework 来自一体机预置镜像注册表。

**数据库验证**：
```bash
# instance 表中 kind='九问' 行
sqlite3 "$A2X_REGISTRY_DB" \
  "SELECT service_id, framework, framework_version, node, \"user\"
   FROM instance WHERE registry='实例注册表' AND kind='九问';"
# 预期：generic_9c21d4e5|jiuwen-report|v1.0.0|192.168.0.11|user-02
```

### 2.3 查询实例（按 node 过滤 + 含异常）

**接口**：`GET /api/instances?node={ip}&include_unhealthy={bool}`

```bash
# 默认只回运行中
curl http://127.0.0.1:8000/api/instances

# 按节点过滤 + 含异常（运维诊断用）
curl 'http://127.0.0.1:8000/api/instances?node=192.168.0.12&include_unhealthy=true'
```

**效果**：`status` 由 node 心跳派生（运行 / 异常），不落库；`include_unhealthy=false` 默认只回运行。
**支持的 filter key**：`include_unhealthy` / `node` / `framework` / `kind` / `user`（白名单，其他 key 返回 `400`）。

**数据库验证**：
```bash
# 对照 instance 表中该 node 上的全部实例（status 不落库，需结合内存态心跳派生）
sqlite3 "$A2X_REGISTRY_DB" \
  "SELECT service_id, kind, framework, node, \"user\",
          json_extract(data,'\$.address') AS address
   FROM instance WHERE registry='实例注册表' AND node='192.168.0.12';"
# 预期：列出该 node 全部实例（含已派生为'异常'但未剔除的行；超 grace_period 被剔除后此处为空）

# 索引命中校验（EXPLAIN QUERY PLAN 应走 idx_instance_node）
sqlite3 "$A2X_REGISTRY_DB" \
  "EXPLAIN QUERY PLAN
   SELECT * FROM instance WHERE registry='实例注册表' AND node='192.168.0.12';"
# 预期：SEARCH ... USING INDEX idx_instance_node (registry=? AND node=?)
```

### 2.4 变更实例（元戎迁移后）

**接口**：`PATCH /api/instances/{service_id}`
**场景**：gateway 在元戎迁移、node/address 改变时更新；service_id 不变。

```bash
curl -X PATCH http://127.0.0.1:8000/api/instances/generic_3f9a1b2c \
  -H "Content-Type: application/json" \
  -d '{"node": "192.168.0.20", "address": "10.244.3.9:4096"}'
```

**预期响应** `200`：返回更新后的 InstanceEntry（新 node + address，service_id 不变）。
**错误**：service_id 不存在 → `404`。

**数据库验证**：
```bash
# instance 表中该 service_id 的 node 列 + data.address 已更新
sqlite3 "$A2X_REGISTRY_DB" \
  "SELECT service_id, node, json_extract(data,'\$.address') AS address
   FROM instance WHERE registry='实例注册表' AND service_id='generic_3f9a1b2c';"
# 预期：generic_3f9a1b2c|192.168.0.20|10.244.3.9:4096

# 旧 node='192.168.0.12' 下应已无该实例
sqlite3 "$A2X_REGISTRY_DB" \
  "SELECT COUNT(*) FROM instance
   WHERE registry='实例注册表' AND service_id='generic_3f9a1b2c' AND node='192.168.0.12';"
# 预期：0
```

### 2.5 注销实例

**接口**：`DELETE /api/instances/{service_id}`

```bash
curl -X DELETE http://127.0.0.1:8000/api/instances/generic_3f9a1b2c
```

**预期响应** `200`：
```json
{"service_id": "generic_3f9a1b2c", "deleted": true}
```

**效果**：删注册条目（元戎停止由 gateway 完成）；幂等——再删一次返回 `deleted: false`。

**数据库验证**：
```bash
# 1. instance 表该行已删除
sqlite3 "$A2X_REGISTRY_DB" \
  "SELECT COUNT(*) FROM instance
   WHERE registry='实例注册表' AND service_id='generic_3f9a1b2c';"
# 预期：0

# 2. 再次 DELETE 后再查（幂等验证）：依然 0，且响应体 deleted=false
curl -s -X DELETE http://127.0.0.1:8000/api/instances/generic_3f9a1b2c
# 预期响应：{"service_id":"generic_3f9a1b2c","deleted":false}
```

---

## 3. node 心跳 `/api/nodes/{node}/heartbeat`

> **心跳活性不入库**（设计文档 §2.3 / §3.2.5）：租约存进程内存 `_node_leases`。
> 因此数据库验证只能间接验证：①心跳未触发剔除时该 node 实例仍在表内；②超 `grace_period` 未续时 `HeartbeatSweeper` 调 `instance.expire_node` 删除该 node 全部实例（写副作用落库）。

### 3.1 node 心跳续租（空 body）

**接口**：`POST /api/nodes/{node}/heartbeat`
**场景**：一体机周期性续租，节点级——一次覆盖该 node 全部实例。

```bash
curl -X POST http://127.0.0.1:8000/api/nodes/192.168.0.12/heartbeat \
  -H "Content-Type: application/json" \
  -d '{}'
```

**预期响应** `200`：
```json
{"node": "192.168.0.12", "state": "healthy", "ttl_seconds": 90, "expires_at": 1751800000.0}
```

**效果**：首次心跳装租约、续租刷新 ttl；超 `grace_period` 未续则该 node 全部实例派生为 `异常`。

**数据库验证**：
```bash
# 心跳本身不入库 —— 验证 instance 表中该 node 实例仍存在（未被剔除）
sqlite3 "$A2X_REGISTRY_DB" \
  "SELECT service_id, node FROM instance
   WHERE registry='实例注册表' AND node='192.168.0.12';"
# 预期（续租期间）：列出该 node 全部实例，例如 generic_3f9a1b2c|192.168.0.12

# 若停止心跳超过 grace_period（默认 30s，见 §4.1）后再查：
#   HeartbeatSweeper 调 instance.expire_node → 该 node 全部实例行被删除
# 预期（超宽限）：无行返回
```

### 3.2 node 心跳（带状态透传）

```bash
curl -X POST http://127.0.0.1:8000/api/nodes/192.168.0.12/heartbeat \
  -H "Content-Type: application/json" \
  -d '{"status": "loaded"}'
```

**效果**：可选 `status` 字段透传业务状态，不影响租约本身。

**数据库验证**：
```bash
# 同 §3.1 —— 心跳活性不入库；透传 status 字段不写库
sqlite3 "$A2X_REGISTRY_DB" \
  "SELECT COUNT(*) FROM instance
   WHERE registry='实例注册表' AND node='192.168.0.12';"
# 预期：与 §3.1 一致（仅受 grace_period / 剔除影响）
```

---

## 4. 心跳租约配置 `/api/lease-config`（全局）

> **存储位置说明**：全局租约策略按开发计划 P0-5 落库到 `registry_meta` 的一行保留记录（如 `registry='__global__'`、`config` JSON 内嵌 `lease_config`）。具体键名以 P0-5 实现为准；下列 SQL 给出验证形态。

### 4.1 读全局租约策略

**接口**：`GET /api/lease-config`

```bash
curl http://127.0.0.1:8000/api/lease-config
```

**预期响应** `200`：
```json
{"enabled": true, "min_ttl": 10, "max_ttl": 3600, "grace_period": 30}
```

**数据库验证**：
```bash
# registry_meta 表中全局保留行的 config.lease_config
sqlite3 "$A2X_REGISTRY_DB" \
  "SELECT json_extract(config,'\$.lease_config.enabled')  AS enabled,
          json_extract(config,'\$.lease_config.min_ttl')  AS min_ttl,
          json_extract(config,'\$.lease_config.max_ttl')  AS max_ttl,
          json_extract(config,'\$.lease_config.grace_period') AS grace_period
   FROM registry_meta WHERE registry='__global__';"
# 预期：1|10|3600|30
```

### 4.2 改全局租约策略

**接口**：`POST /api/lease-config`

```bash
curl -X POST http://127.0.0.1:8000/api/lease-config \
  -H "Content-Type: application/json" \
  -d '{"enabled": true, "min_ttl": 10, "max_ttl": 3600, "grace_period": 60}'
```

**效果**：调整 `grace_period` 后，后续超宽限时间随之变化；已发的租约按新策略续期。

**数据库验证**：
```bash
# grace_period 应已更新为 60
sqlite3 "$A2X_REGISTRY_DB" \
  "SELECT json_extract(config,'\$.lease_config.grace_period')
   FROM registry_meta WHERE registry='__global__';"
# 预期：60

# 再读 API 对照（端到端一致性）
curl -s http://127.0.0.1:8000/api/lease-config
# 预期响应：{"enabled":true,"min_ttl":10,"max_ttl":3600,"grace_period":60}
```

---

## 5. 分布式高可用 `/api/ha/*`（后续版本，730 不实现）

> 730 单机 SQLite 不实现 HA；接口契约已在 OpenAPI 标注 `[后续版本]`，与开发计划 P1-2「`ha/` 模块整体不建」一致。下列命令仅供后续版本联调参考。

### 5.1 查询当前成员集

```bash
curl http://127.0.0.1:8000/api/ha/members
```

**预期响应** `200`：
```json
{"members": ["192.168.0.11", "192.168.0.12", "192.168.0.13"]}
```

### 5.2 变更成员集（奇偶校验）

```bash
curl -X POST http://127.0.0.1:8000/api/ha/members \
  -H "Content-Type: application/json" \
  -d '{"members": ["192.168.0.11", "192.168.0.12"]}'
```

**预期响应** `200`：
```json
{"members": ["192.168.0.11"], "warning": "偶数台，未激活 192.168.0.12"}
```

**效果**：偶数成员集自动去尾 + 告警，保证 Raft 多数派为奇数。

### 5.3 查询当前主（leader）

```bash
curl http://127.0.0.1:8000/api/ha/leader
```

**预期响应** `200`：
```json
{"leader": "192.168.0.11"}
```

**效果**：任一节点据 Raft 权威回主；注册中心据此把 nginx 指向主，gateway 不直接调用。

> **数据库验证**：730 不实现 HA，无对应 SQLite 表；rqlite 阶段成员集 / leader 由 Raft 协议维护、不经业务表，跳过数据库验证。

---

## 6. 典型联调场景

### 场景 A：gateway 拉起一个新实例（端到端）

```bash
# 1. 取运行规格
curl http://127.0.0.1:8000/api/images/opencode/launch-spec

# 2. gateway 调元戎拉起（注册中心不参与），得到 address=10.244.1.7:4096

# 3. 注册实例
curl -X POST http://127.0.0.1:8000/api/instances \
  -H "Content-Type: application/json" \
  -d '{
    "service_id": "generic_3f9a1b2c",
    "kind": "三方", "framework": "opencode", "framework_version": "v0.2.0",
    "node": "192.168.0.12", "address": "10.244.1.7:4096", "user": "user-01"
  }'

# 4. 周期性 node 心跳（period = ttl/3）
curl -X POST http://127.0.0.1:8000/api/nodes/192.168.0.12/heartbeat \
  -H "Content-Type: application/json" -d '{}'
```

**数据库验证（场景 A）**：
```bash
# 端到端落库校验：实例入库 + 心跳未触发剔除
sqlite3 "$A2X_REGISTRY_DB" \
  "SELECT service_id, kind, framework, node,
          json_extract(data,'\$.address') AS address
   FROM instance
   WHERE registry='实例注册表' AND service_id='generic_3f9a1b2c';"
# 预期：generic_3f9a1b2c|三方|opencode|192.168.0.12|10.244.1.7:4096
```

### 场景 B：实例落点迁移

```bash
# 1. gateway 检测到元戎迁移、新 address
# 2. 更新实例条目（service_id 不变）
curl -X PATCH http://127.0.0.1:8000/api/instances/generic_3f9a1b2c \
  -H "Content-Type: application/json" \
  -d '{"node": "192.168.0.20", "address": "10.244.3.9:4096"}'

# 3. 新 node 立即心跳，避免被误判异常
curl -X POST http://127.0.0.1:8000/api/nodes/192.168.0.20/heartbeat \
  -H "Content-Type: application/json" -d '{}'
```

**数据库验证（场景 B）**：
```bash
# 1. 实例已迁到新 node，address 已更新；service_id 不变
sqlite3 "$A2X_REGISTRY_DB" \
  "SELECT service_id, node, json_extract(data,'\$.address') AS address
   FROM instance WHERE registry='实例注册表' AND service_id='generic_3f9a1b2c';"
# 预期：generic_3f9a1b2c|192.168.0.20|10.244.3.9:4096

# 2. 旧 node='192.168.0.12' 下已无该实例
sqlite3 "$A2X_REGISTRY_DB" \
  "SELECT COUNT(*) FROM instance
   WHERE registry='实例注册表' AND service_id='generic_3f9a1b2c' AND node='192.168.0.12';"
# 预期：0
```

### 场景 C：镜像版本下线

```bash
# 1. 确认无在用实例
curl 'http://127.0.0.1:8000/api/instances?framework=opencode&framework_version=v0.2.0'

# 2. 注销镜像（返回 409 image_in_use 则先迁走实例）
curl -X DELETE http://127.0.0.1:8000/api/images/opencode/v0.2.0
```

**数据库验证（场景 C）**：
```bash
# 1. image 表对应版本行已删除
sqlite3 "$A2X_REGISTRY_DB" \
  "SELECT COUNT(*) FROM image
   WHERE registry='镜像注册表' AND framework='opencode' AND framework_version='v0.2.0';"
# 预期：0

# 2. 该 framework 下若仍有其他版本，应恰有一行 is_default=1（自动补默认）
sqlite3 "$A2X_REGISTRY_DB" \
  "SELECT framework_version, is_default
   FROM image WHERE registry='镜像注册表' AND framework='opencode'
   ORDER BY is_default DESC, framework_version;"
# 预期：剩余版本中恰一行 is_default=1；若该 framework 已无版本则空

# 3. 注销前的在用实例校验：instance 表中引用该 fw+ver 的行（DELETE 镜像前应已迁走）
sqlite3 "$A2X_REGISTRY_DB" \
  "SELECT COUNT(*) FROM instance
   WHERE registry='实例注册表' AND framework='opencode' AND framework_version='v0.2.0';"
# 预期（成功下线后）：0（镜像可删的前提就是无在用实例）
```

### 场景 D：node 故障 → 实例自动异常

```bash
# 1. node 192.168.0.12 心跳超 grace_period（默认 30s，可调）
curl -X POST http://127.0.0.1:8000/api/lease-config \
  -H "Content-Type: application/json" \
  -d '{"enabled": true, "min_ttl": 10, "max_ttl": 3600, "grace_period": 30}'

# 2. 查询该 node 实例（include_unhealthy=true 看异常项）
curl 'http://127.0.0.1:8000/api/instances?node=192.168.0.12&include_unhealthy=true'

# 3. 过宽限批量剔除由 HeartbeatSweeper 注入 instance.expire_node 自动完成，无需手工调用
```

**数据库验证（场景 D）**：
```bash
# 1. grace_period 已写入（策略生效前置条件）
sqlite3 "$A2X_REGISTRY_DB" \
  "SELECT json_extract(config,'\$.lease_config.grace_period')
   FROM registry_meta WHERE registry='__global__';"
# 预期：30

# 2. 等 grace_period 后再查 instance 表：HeartbeatSweeper 调 expire_node 应已删除该 node 全部实例
sqlite3 "$A2X_REGISTRY_DB" \
  "SELECT COUNT(*) FROM instance
   WHERE registry='实例注册表' AND node='192.168.0.12';"
# 预期（超宽限后）：0

# 3. 其他 node 上的实例不受影响（横向校验）
sqlite3 "$A2X_REGISTRY_DB" \
  "SELECT DISTINCT node FROM instance WHERE registry='实例注册表';"
# 预期：故障 node 不在列表中；其他 node 仍列出
```

---

## 7. 错误码速查

| HTTP | 场景 | 响应体 |
|------|------|--------|
| `400` | 注册镜像 spec.rootfs.imageurl 缺失 / filter key 不在白名单 | `{"detail":"..."}` |
| `404` | 取不存在的 framework launch-spec / PATCH 不存在的 service_id | `{"detail":"..."}` |
| `409` | 注销在用镜像 | `{"code":"image_in_use","detail":"...","instances":[...]}` |
| `502` | 注销镜像时镜像仓删除接口失败（外部依赖） | `{"detail":"..."}` |

> `401` / `403` 鉴权错误不在 730 范围（不启鉴权），后续版本启用 `auth/` 模块后补充。
