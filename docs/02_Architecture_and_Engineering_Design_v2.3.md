# OmniLore v2.3 系统架构与详细工程设计文档

> **文档编号：** CH-ARCH-2026-v2.3  
> **对应代码库版本：** 对外 `v0.3.0-alpha` / PEP 440 `0.3.0a0`（内部代际 v2.3）
> **更新日期：** 2026-10-22  
> **设计基线：** 严格对应 `sql/001_omnilore_schema_and_rls.sql` 与 `omnilore/` 包内已实现的全部工程模块

## 1. 总体架构与信任边界

### 1.1 系统分层拓扑

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│ 客户端接入层 (Untrusted Clients)                                              │
│  - IDE / Desktop Agent (Cursor, Claude Desktop 等，经 MCP JSON-RPC Stdio 接入)│
│  - Obsidian 本地编辑器 (直接编辑受控 02_Vault/spaces/<space_id>/wiki/*.md)     │
└──────────────────┬─────────────────────────────────────────┬─────────────────┘
                   │ JSON-RPC 2.0 (仅传 token + 受控 ID)      │ 本地文件系统事件
                   ▼                                         ▼
┌──────────────────────────────────────────┐  ┌────────────────────────────────┐
│ 协议与守护入口层                          │  │ 后台守护服务 (daemons.py)       │
│  - mcp_stdio_server.py (MCPJsonRpcServer)│  │  - ObsidianVaultWatcher        │
│  - OmniLoreMCPGateway (7 个受控工具)    │  │  - OutboxRecoveryDaemon        │
└──────────────────┬───────────────────────┘  └──────────────┬─────────────────┘
                   │                                         │
                   └───────────────────┬─────────────────────┘
                                       ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│ 核心业务与安全引擎层 (omnilore/engine.py)                                    │
│  - AuthorizationService：计算有效读写作用域 (Token ∩ Membership ∩ 继承 ∩ 类别) │
│  - profiled_transaction：注入事务级 PostgreSQL GUC 上下文变量                  │
│  - OmniLoreEngine：11 状态入库状态机、零停机版本切换、7 大审核动作、Lint 巡检 │
└───────┬──────────────────────────┬───────────────────────────┬───────────────┘
        │                          │                           │
        ▼                          ▼                           ▼
┌──────────────────────┐ ┌─────────────────────────┐ ┌─────────────────────────┐
│ 数据持久与行级安全层   │ │ 受控文件网关与多格式解析 │ │ 统一模型适配层           │
│  - 001_schema_rls.sql│ │  - SecureVaultStorage   │ │  - ModelAdapter         │
│  - pg_repository.py  │ │  - parsers.py (PDF/图像/ │ │  - dashscope_client.py  │
│  - RLSDatabaseEngine │ │    AST 符号/健康/脱敏)   │ │  - 外发隐私拦截与 Schema│
└──────────────────────┘ └─────────────────────────┘ └─────────────────────────┘
```

> **当前实现边界：** 上图同时展示设计目标与已落地模块，不表示入口已经把两种数据引擎
> 接成可切换后端。当前 `OmniLoreEngine`、CLI 与 MCP 服务端只实例化
> `RLSDatabaseEngine`，**业务读写仍然走内存引擎**；`pg_repository.py`、SQL 与 PostgreSQL
> 容器虽已存在，仍未被业务路径实例化。
> 设置 `DATABASE_URL` 时会额外建立一条**安全 PG 连接层**（`omnilore/pg_runtime.py`：连接角色
> 必须非超级用户、无 `BYPASSRLS`、是策略角色成员，否则拒绝启动；事务内 `SET LOCAL ROLE` +
> 注入 `app.*` GUC），但它只做连接与启动自检，**不承载业务读写**。
> 真实 PG 证据来自独立 RLS 探针与安全连接层的角色验证，不是业务端到端链路。

### 1.2 核心信任边界约束

1. **客户端输入零信任：** 客户端传入的所有参数、上传的文件字节、Obsidian 本地修改的 Markdown 文本，以及外部大模型返回的 JSON 结果，一律视为**不可信输入**。
2. **禁止客户端裸路径：** MCP 接口层（`get_document_asset` 等）仅接受服务端生成的 UUID（`doc_id`、`task_id`），任何包含 `/`、`\`、`..` 的参数在网关入口直接抛出 `PermissionDeniedError`。
3. **模型无权越界：** 大模型输出仅用于提供分类建议、正文摘要与结构化提取草稿；`ModelAdapter.generate_structured` 内置禁区字段拦截器，一旦模型输出试图携带 `space_id`、`doc_id`、`token`、`task_state` 等特权字段，立即判定为 `MODEL_SCHEMA_INVALID` 并拒绝执行。

## 2. 权限计算模型与 PostgreSQL 命令级 RLS 设计

### 2.1 多维权限交集计算公式

在 `AuthorizationService.resolve_scope`（位于 `omnilore/engine.py`）中，每次请求按以下确定性逻辑解析出不可变的 `ResolvedScope`：

```text
有效读取作用域 (read_scopes) =
    Token 允许的 (space_id, category)
  ∩ 当前未撤销 Membership 允许的 (space_id, category)
  ∩ Space 定义的 allowed_categories
  ∩ Profile 类型硬性隔离规则 (_profile_allowed_categories)
  ∪ (若 Membership.inherit_org_public_read == True:
       沿 parent_space_id 与 ltree 向上追溯的所有 ORG_PUBLIC 祖先空间的只读作用域)

有效写入作用域 (write_scopes) =
    Token 允许写入的 (space_id, category)
  ∩ 当前未撤销且角色为 WRITER/ADMIN 的 Membership 作用域
  ∩ Space 定义的 allowed_categories
  ∩ Profile 类型硬性隔离规则
```

### 2.2 事务级 RLS 上下文注入（`profiled_transaction`）

`profiled_transaction(pool, resolved_scope)` 使用 `@asynccontextmanager` 实现，在每个数据库事务开启时执行以下步骤：

1. **先清空上下文（默认拒绝）：** 将 `app.current_user_id`、`app.allowed_read_spaces`、`app.allowed_read_scopes`、`app.allowed_write_spaces`、`app.allowed_write_scopes` 全部重置为空字符串 `''`（事务级 `is_local = true`）。
2. **正则白名单校验：** 对所有 `space_id` 强制执行 `^[a-z0-9_]{3,64}$` 校验，杜绝逗号拼接注入。
3. **注入复合作用域：** 将 `space_id:category` 列表注入 `app.allowed_read_scopes` 与 `app.allowed_write_scopes`。
4. **命令级策略拦截（SQL 层）：** 在 `sql/001_omnilore_schema_and_rls.sql` 中，通过 `authz.can_read_scope(space_id, category)` 与 `authz.can_write_scope(space_id, category)` 为每张受保护表分别定义独立的 `FOR SELECT`、`FOR INSERT`、`FOR UPDATE`（同时含 `USING` 与 `WITH CHECK`）及 `FOR DELETE` 策略，并启用 `FORCE ROW LEVEL SECURITY`。

## 3. 核心数据库表结构设计（15 张表完整字典）

> 订正说明：`sql/001_omnilore_schema_and_rls.sql` 实际定义 **15** 张表
> （早期版本漏数了 `users`）：`users` / `spaces` / `space_memberships` /
> `access_tokens` / `assets` / `document_series` / `documents` /
> `document_assets` / `document_relations` / `document_chunks` /
> `ingestion_tasks` / `outbox_events` / `health_metrics` / `audit_events` /
> `model_runs`。

| 序号 | 表名 | 主键 | RLS 状态 | 核心字段与设计约束说明 |
|---:|---|---|---|---|
| 1 | `users` | `user_id (UUID)` | 认证只读 | 存储用户基本状态（`ACTIVE` / `SUSPENDED` / `DEACTIVATED`） |
| 2 | `spaces` | `space_id (TEXT)` | 认证只读 | 存储空间类型、`parent_space_id`、`org_tree_path (ltree)`（含 GIST 索引）、`external_llm_policy (JSONB)` 与 `default_ttl_days` |
| 3 | `space_memberships` | `membership_id (UUID)` | 认证管理 | `(user_id, space_id)` 唯一；记录角色、允许类别、`inherit_org_public_read` 与 `revoked_at` |
| 4 | `access_tokens` | `token_id (UUID)` | 认证管理 | 仅存 64 位 `token_hash (SHA-256)`，绝不存明文 Token；记录 `profile_type`、读写空间、允许类别与过期/撤销时间 |
| 5 | `assets` | `asset_id (UUID)` | 登录可读/可加 | 按 `sha256_hash` 唯一约束实现 CAS 去重；记录相对路径 `storage_rel_path`、`mime_type` 与字节大小 |
| 6 | `document_series` | `series_id (UUID)` | **FORCE RLS** | `(space_id, version_key)` 唯一；通过可延迟外键 `current_doc_id` 指向当前唯一生效版本，维护 `revision_number` |
| 7 | `documents` | `doc_id (UUID)` | **FORCE RLS** | 存储具体版本的标题、摘要、`wiki_rel_path`、`file_hash`、`content_revision`、`doc_status`、`is_current`、`outbound_links` 与 `ttl_expires_at`；含部分唯一索引 `idx_documents_one_current_per_series` |
| 8 | `document_assets` | `(doc_id, asset_id)` | **FORCE RLS** | 绑定文档与底层 CAS 原件，记录 `original_filename` 与 `source_locator (JSONB)`（页码、章节坐标） |
| 9 | `document_relations` | `relation_id (UUID)` | **FORCE RLS** | 记录文档间拓扑关系：`SUPPLEMENT`（补充）、`SUPERSEDES`（替代）、`PROMOTED_FROM`（跨空间晋升）、`SPLIT_FROM`（拆分）、`REFERENCES` |
| 10 | `document_chunks` | `chunk_id (UUID)` | **FORCE RLS** | **冗余 `space_id`、`category`、`is_current`** 以避免向量检索跨表过滤退化；包含生成列 `tsv (tsvector)`、`code_symbols (TEXT[])` 与 `embedding vector(1024)`，并在 `WHERE is_current = true` 上建立 GIN 与 HNSW 部分索引 |
| 11 | `ingestion_tasks` | `task_id (UUID)` | **FORCE RLS** | `idempotency_key` 唯一；记录 11 状态迁移、暂存路径、候选路由、`review_reason`、错误码及 Worker 租约字段 |
| 12 | `outbox_events` | `event_id (UUID)` | 受控读写 | 支撑文件发布与索引构建的可恢复异步事件表（`PENDING` / `PROCESSING` / `COMPLETED` / `DEAD_LETTER`） |
| 13 | `health_metrics` | `metric_id (UUID)` | **FORCE RLS** | 强制校验 `category = '04_Health_Archive'`；记录被检测人、指标名、原始提取值、归一化数值、单位、参考范围、测量日期、`verification_status`（默认 `NEEDS_CHECK`）及核验历史 `revision_history` |
| 14 | `audit_events` & `model_runs` | `audit_id` / `run_id` | 仅追加 | `audit_events` 记录所有关键状态变更前后摘要；`model_runs` 记录脱敏后的模型调用耗时、Token 数、估算成本与状态码 |

## 4. 入库状态机、可恢复 Saga 与零停机版本切换设计

### 4.1 11 状态合法迁移图

```text
RECEIVED ──► PRECHECKING ──► ROUTING ──► STAGED ──► COMMITTING ──► COMMITTED ──► REVERTED
                  │             │          │            │
                  ├──► NEEDS_REVIEW ───────┘            │
                  │      │                              │
                  │      └──► CANCELLED                 │
                  ▼             ▼                       ▼
            FAILED_FINAL   FAILED_RETRYABLE ◄───────────┘
```

### 4.2 消除“短暂检索不可见窗口”的两阶段 Saga 提交流程

1. **阶段一（准备与暂存事务 - `_stage_and_commit_task`）：**
   - 将上传原件与编译好的 Wiki Markdown 写入 `01_Processing/` 暂存区。
   - 在第一个数据库事务中插入新 `documents` 记录，状态设为 `PENDING_PUBLISH` 且 `is_current = False`；**此时 `document_series.current_doc_id` 仍保持指向旧的 `PUBLISHED_CURRENT` 版本，正在进行的在线检索不受任何影响**。
   - 在同一事务内写入 `outbox_events (event_type='PUBLISH_STAGED_FILES')`，并将任务状态置为 `STAGED`。
2. **阶段二（物理文件原子发布与向量计算 - `process_outbox_event`）：**
   - 将暂存原件校验 SHA-256 后通过 `os.replace` 原子移入 `ops/cas/`，将暂存 Wiki 原子移入 `02_Vault/spaces/<space_id>/wiki/`。
   - 调用 `ModelAdapter.embed_texts` 生成 1024 维归一化向量。
3. **阶段三（最终原子切换事务）：**
   - 在第二个数据库事务中锁定 `document_series` 行，校验 `expected_current_doc_id`（若并发任务已修改当前指针，则立即抛出 `VersionConflictError`）。
   - 原子执行：将旧版本置为 `SUPERSEDED (is_current=False)` 并将其切片置为 `is_current=False` -> 将新版本置为 `PUBLISHED_CURRENT (is_current=True)` -> 插入新版本 `document_chunks (is_current=True)` -> 更新 `document_series.current_doc_id` -> 幂等追加 `ledger_entries` -> 将 Outbox 置为 `COMPLETED`、任务置为 `COMMITTED`。
