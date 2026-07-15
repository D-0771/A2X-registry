-- 一体机 Agent OS 注册中心 · 表结构（构建预制 .db 用）
-- 真源在 a2x_registry/common/db.py 的 SCHEMA_SQL；本文件由 build_fixtures.sh 自动生成。
--    请勿直接编辑本文件；改 schema 请改 a2x_registry/common/db.py。

-- 一体机 Agent OS 注册中心 · 表结构（权威）
-- 来源：一体机Agent OS registry实现文档.md §3.2
-- 用 CREATE TABLE/INDEX IF NOT EXISTS —— 启动预建齐幂等（开发计划 P0-1 要求）。

-- 注册表登记：有哪些命名注册表 + 各自 kind + 配置
CREATE TABLE IF NOT EXISTS registry_meta (
  registry TEXT PRIMARY KEY,             -- 'toolret'/'publicmcp'/'default'/'镜像注册表'/'实例注册表'
  kind     TEXT NOT NULL,                -- service | image | instance
  config   TEXT                          -- JSON：service 类存 register_config/vector_config/taxonomy_hash
);

-- 服务（A2X：generic/a2a/skill）——发现 / 分类基于它
CREATE TABLE IF NOT EXISTS service (
  registry    TEXT NOT NULL,
  service_id  TEXT NOT NULL,
  type        TEXT NOT NULL,             -- generic | a2a | skill
  source      TEXT NOT NULL,             -- user_config | api_config | ephemeral | skill_folder
  name        TEXT,                      -- 热：分类 LLM 输入 / 过滤
  description TEXT,                      -- 热：分类 LLM 输入
  data        TEXT NOT NULL,             -- JSON：service_data / agent_card / skill_data
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL,
  PRIMARY KEY (registry, service_id)
);
CREATE INDEX IF NOT EXISTS idx_service_type ON service(registry, type);

-- 镜像（一行一版本）
CREATE TABLE IF NOT EXISTS image (
  registry          TEXT NOT NULL,
  service_id        TEXT NOT NULL,               -- image_sid(framework, framework_version)
  framework         TEXT NOT NULL,               -- 热：按框架查
  framework_version TEXT NOT NULL,               -- 热：按版本查
  is_default        INTEGER NOT NULL DEFAULT 0,  -- 该框架默认版本标记（每框架恰一行=1）
  data              TEXT NOT NULL,               -- JSON {rootfs, cpu, memory, ports, env, image_module_version}
  PRIMARY KEY (registry, service_id)
);
CREATE INDEX IF NOT EXISTS idx_image_fw     ON image(registry, framework);
CREATE INDEX IF NOT EXISTS idx_image_fw_ver ON image(registry, framework, framework_version);

-- 实例（status 不落库，查询时据 node 心跳派生）
CREATE TABLE IF NOT EXISTS instance (
  registry          TEXT NOT NULL,
  service_id        TEXT NOT NULL,       -- instance_sid(user, framework)
  kind              TEXT NOT NULL,       -- 三方 | 九问
  framework         TEXT,
  framework_version TEXT,
  node              TEXT,                -- 热：node 批量剔除 / 按节点查
  "user"            TEXT,                -- 热：按用户 ID 查该用户实例
  data              TEXT NOT NULL,       -- JSON {address, created_at, last_active_at}
  PRIMARY KEY (registry, service_id)
);
CREATE INDEX IF NOT EXISTS idx_instance_node ON instance(registry, node);
CREATE INDEX IF NOT EXISTS idx_instance_fw   ON instance(registry, framework, framework_version);
CREATE INDEX IF NOT EXISTS idx_instance_user ON instance(registry, "user");
