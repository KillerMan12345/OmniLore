# 能力状态矩阵 FEATURE_STATUS

> **我们公开边界，因为我们做安全。**
> 本页是 OmniLore 的「宣称—证据」对照表。README 里出现的**每一条能力**，
> 在这里都必须有对应的一行、一个状态、一条可复跑的验证命令。
> 有一条对不上，就是 bug —— 由 `tests/test_w2_w3_roadmap.py` 自动守护。

对外版本：**`v0.3.0-alpha`**（预发布，仅供评估、研究与贡献）。

---

## 1. 四态标注的定义

| 状态 | 定义 | 判定依据 |
|---|---|---|
| `Implemented` | 代码已实现，仓库内有自动化用例覆盖 | 用例存在且通过，但运行在**进程内内存引擎**上，无真实外部依赖 |
| `Tested-real` | 验证发生在**真实外部依赖**之上 | CI 里有可复核的真实证据（真实子进程、真实跨平台文件系统、真实第三方解析库）；**或**一次性人工真实依赖验证 —— 后者必须在备注里写明「人工 / 非 CI」，不得含糊 |
| `Simulated` | 只在**与 PostgreSQL RLS 同构的内存仿真引擎**上验证 | 真实 PostgreSQL **没有 CI 证据**；这是本项目最需要被如实标注的一类 |
| `Planned` | 尚未实现，或仅有模板而未经自动化验证 | 代码未落地，或落地了但没有自动化证据 |

**红线：真实 PG 的例外只有两条，且必须写明各自「证明了什么 / 没证明什么」。**
两条例外的**证据性质不同**，不许混为一谈（证据记录见第 6 节与第 6.1 节）：

1. **「真实 PostgreSQL RLS 策略与跨空间读拦截」—— 现在由 CI 持续验证。**
   2026-10-24 在**真实 PostgreSQL 16.15** 上以策略角色跑通 3 条探针（策略挂载 / 跨空间
   不可见 / 授权后可见）；**自 2026-10-26 起，`.github/workflows/ci.yml` 的 `integration-rls`
   job 已在 CI 上真实运行并连续 2 次绿灯**（Run #1 `8d0ecc3`、Run #2 `5d513fe`），
   每次 push 都起一个干净的真实 PostgreSQL 16（`pgvector/pgvector:0.8.0-pg16`）重跑同三条
   探针。**它证明的是 `sql/001` 的 SQL 策略本身在真实 PG 上生效**；**不证明** CLI / MCP
   业务运行时已接入 PostgreSQL，**不证明**结构化状态已持久化（业务数据层仍是内存引擎，
   见第 2.1 节）。因此「真实 PostgreSQL 集成 CI」这条同步升为 `Tested-real`，但备注里必须
   写明「只跑独立 SQL 探针、不覆盖业务运行时接线」。
2. **「PostgreSQL 安全连接层与 RLS 生效验证」—— 仍然只是人工一次性证据、非 CI。**
   2026-10-25 / 2026-10-26 在**真实 PostgreSQL 16.15** 上以应用角色 `omnilore_app` 走运行时
   连接层跑通 3 条探针（未授权 0 行 / 授权后可见 / 连接角色非超用户且无 BYPASSRLS）。
   **验证脚本 `tools/verify_rls_app_role.py` 没有接进 CI**，未来也不会因为
   `integration-rls` job 绿灯而自动升级。

**「CI 类条目升 `Tested-real`」的门槛**：**连续 ≥2 次绿灯**（单次绿灯可能是"恰好没红"，
不构成可复现信号）。`integration-rls` 已多次绿灯、`arm64` job 已 3 次绿灯（Run #2 `5d513fe`、#4 `cb45932`、#5 `effbb02`），
其中 #4/#5 为连续两次 —— **二者均已达门槛，故均为 `Tested-real`**。

其余依赖真实 PostgreSQL 的条目（四维交集 / Profile 隔离 / 撤销失效 / 组织树继承等）
一律维持 `Simulated` —— 探针没有覆盖它们，不许顺手抬高。

---

## 2. 测试跑在什么上面（请先读这一段）

本仓库的全部测试用例都跑在 `omnilore.engine.RLSDatabaseEngine` 上 —— 一个**内存仿真
引擎**。它与 PostgreSQL 的 RLS 策略**同构**（同样的 `(space_id, category)` 策略语义、
同样的命令级权限收口），但**它不是真实 PostgreSQL**：

- 它不是用 `CREATE POLICY` / `SET LOCAL ROLE` 实现的，而是用 Python 谓词实现的同构判定；
- 它没有真实的 MVCC、锁、事务隔离级别与 `pgvector` 索引行为；
- 进程退出后结构化状态清空（磁盘上的原始资产与 Markdown 知识页保留）。

所以：**「168 个测试全绿」证明的是逻辑正确性，不等于「真实 PG 上的 RLS 已验证」。**
其中绝大多数业务用例跑在内存仿真引擎上，少数几条守卫只做静态文本/配置判定（不碰引擎，
例如 `make` 目标存在性、仓库 LF、compose 启动超用户角色名），
另有 5 个 `test_pg_runtime_wiring.py` 用例用
**离线 mock** 钉住 PG 运行时接线（默认内存引擎 / 拒绝超用户与 BYPASSRLS 连接角色），
同样不连真实 PG。
本页的例外只有两处，且**证据性质不同**：①「真实 PostgreSQL RLS 策略与跨空间读拦截」
起自 2026-10-24 在真实 PostgreSQL 16.15 上的手动验证（完整输出见第 6 节），**现已由 CI
的 `integration-rls` job 持续验证**（自 2026-10-26 起连续 2 次绿灯）；
②「PostgreSQL 安全连接层与 RLS 生效验证」是 2026-10-25 / 2026-10-26 在同一版本真实
PostgreSQL 16.15 上以应用角色 `omnilore_app` 走运行时连接层的**手动、一次性**证据
（完整输出见第 6.1 节，**非 CI**）。它们证明了「这套策略在真实 PG 上确实生效」，
但都不等于业务运行时已接入 PG。

**持续取证已经跑起来了**：`.github/workflows/ci.yml` 的 `integration-rls` job 每次 push
都会起真实 PostgreSQL 16 service container（`pg_isready` 健康检查，零 Secret，
exit 2/3 都会让 job 变红），**并已于 2026-10-26 连续 2 次真实绿灯**。
**它只跑独立 SQL 探针，不覆盖 CLI / MCP 业务运行时的接线** —— 因此本页只据此升级
「真实 PostgreSQL RLS 策略与跨空间读拦截」与「真实 PostgreSQL 集成 CI」两条，其余条目
一律不许跟涨。这句话在 README、本页、CHANGELOG 三处口径一致（由 `make verify-honesty` 守护）。

---

## 2.1 PostgreSQL 运行时边界（必须单独说明）

仓库已经包含 PostgreSQL DDL、seed、`AsyncpgRepository`、容器编排、独立 RLS 探针，以及
2026-10-25 新增的**安全运行时连接层** `omnilore/pg_runtime.py`。当前接线状态拆成两层看：

**已接入的一层（安全连接，不是数据路径）**
设置 `DATABASE_URL` 时，MCP stdio 服务端会构造 `PgRuntime` 并建立连接，随后强制校验连接
角色**非超级用户、无 `BYPASSRLS`、是策略目标角色（`omnilore_gateway` / `omnilore_worker`）成员**，
任一不满足即拒绝启动（fails closed）；事务内 `SET LOCAL ROLE` 切到策略角色，并以 `app.*` GUC
注入请求上下文，注入失败即中止事务。这一层的 RLS 生效已在真实 PostgreSQL 16.15 上以应用
角色验证过一次（第 6.1 节）。

**尚未接入的一层（业务数据，仍然是内存）**
`OmniLoreEngine`、CLI 与 MCP 服务端**都没有**根据 `DATABASE_URL` 实例化 `AsyncpgRepository`，
业务请求仍进入 `RLSDatabaseEngine`。原因是 engine 的查询谓词是任意 Python lambda，且多处
直接访问 `self.db._state[...]`，无法靠「替换后端引擎」无缝切到 SQL，必须逐业务方法重写 ——
那是独立的 4–6 人日工程，尚未做。因此：

- `docker compose up` 会启动 PostgreSQL 并应用 SQL，但**不等于** CLI / MCP 的结构化状态已写入 PG；
- 容器内应用进程退出后，结构化业务状态仍会清空；容器模式目前不能宣称为持久化、多人共享方案；
- 第 6 节与第 6.1 节的真实 PG 证据只证明**策略挂载、跨空间读拦截与安全连接层上的 RLS 生效**，
  不证明业务读写已经走 PostgreSQL。

这是实现边界，不是部署提示。后续只有在业务数据层完成 SQL 迁移并增加端到端 PostgreSQL 用例后，
才能升级口径 —— **连上了、RLS 也验证了，都不等于「数据已持久化」**。

---

## 3. 状态矩阵（与 README 逐条对应）

| 能力 | 状态 | README 位置 | 验证命令 | 备注 |
|---|---|---|---|---|
| 不可变原始证据（SHA-256 CAS） | `Implemented` | 核心能力 / 基础 | `make verify-ops` | 真实文件系统上的哈希校验与恢复演练；**不可变性无专门负向测试** |
| 四维权限交集 | `Simulated` | 核心能力 / Phase 0 | `make verify-rls` | 内存仿真引擎；真实 PG 探针只覆盖 space + category 两维（`authz.can_read_scope`），membership / profile 维度**未做探针**，故不升级 |
| Profile 硬性隔离 | `Simulated` | 核心能力 / Phase 0 | `make verify-rls` | 同上；profile 维度由引擎层判定、不由 SQL 策略表达，2026-10-24 的三条探针**未覆盖** profile 切换 |
| 撤销即时失效 | `Simulated` | 核心能力 / Phase 0 | `make verify-rls` | `< 1ms` 为进程内缓存实测，非 PG 侧实测；PG 探针不涉及撤销链路，故不升级 |
| 文件网关路径防御 | `Tested-real` | 核心能力 / Phase 0 | `make verify-paths` | 真实文件系统（symlink / Windows junction 回退）；已有 Linux / Windows 证据，当前没有 macOS CI，不宣称 macOS 已实跑 |
| 真实 PostgreSQL RLS 策略与跨空间读拦截 | `Tested-real` | 核心能力 / Phase 0 | `make verify-rls-pg` | 证据性质已由「2026-10-24 一次性人工验证」升级为 **CI 持续验证**：`.github/workflows/ci.yml` 的 `integration-rls` job 每次 push 都在干净的真实 PostgreSQL 16 上重跑同 3 条探针（7 张核心表 `rowsecurity=true` / 跨空间行未授权不可见 / 授权后同一行可见），**自 2026-10-26 起已连续 2 次绿灯**（Run #1 `8d0ecc3`、Run #2 `5d513fe`）。**边界（必须一并读）**：CI 绿灯证明的是 `sql/001` 的 **SQL 策略本身**在真实 PG 上生效，**不等于 CLI / MCP 业务运行时已接入 PostgreSQL**，也**不等于**结构化状态已持久化 —— 业务读写仍走内存 `RLSDatabaseEngine`（见第 2.1 节）。「21 条策略注释」补课（R-08b）已落地：每条策略上方四段注释（保护什么 / 依赖维度 / 防什么 / 不覆盖什么），且策略逻辑被冻结（`TestRlsPolicyComments` 摘要不符即红） |
| 多格式入库 | `Implemented` | 核心能力 / Phase 1 | `make verify-parsers` | Markdown / 代码解析与真实 `Pillow` PNG 入库有自动化用例；PDF 分支实现了 `pdfplumber` → `pypdf` 回退，但当前测试套件**没有构造或入库真实 PDF**，因此不标 `Tested-real` |
| 混合检索（BM25 + 向量 + 代码符号） | `Simulated` | 核心能力 / Phase 1 | `make verify-search` | ⚠️ `hybrid` 路径在 CI 里是 **monkeypatch 桩**（无真实网络调用、无真实 1024 维向量）；`keyword_only` 路径才是真实执行的 |
| AST 代码符号提取 | `Implemented` | 核心能力 / Phase 1 | `make verify-parsers` | 纯函数，无外部依赖 |
| 两阶段零停机版本控制 | `Simulated` | 核心能力 / Phase 1 | `make verify-rls` | 版本切换的状态机在内存引擎上验证 |
| 七类审核动作 | `Simulated` | 核心能力 / Phase 1 | `make verify-rls` | 含 `PROMOTE_COPY` 脱敏预览 |
| Obsidian 双向同步 | `Simulated` | 核心能力 / Phase 1 | `make verify-sync` | 防死循环 / 受保护字段 / 冲突副本 |
| 受保护字段白名单可配 | `Implemented` | 核心能力 / Phase 1 | `make verify-sync` | `OMNILORE_PROTECTED_FRONTMATTER`，可整体关闭 |
| 冲突副本落隐藏目录 | `Implemented` | 核心能力 / Phase 1 | `make verify-sync` | 默认 `.omnilore/conflicts/`，可改 `OMNILORE_CONFLICT_DIR` |
| 家庭成员管理 | `Simulated` | 核心能力 / Phase 2 | `make verify-rls` | 邀请 / 更新 / 撤销 |
| 健康指标提取与人工核验 | `Simulated` | 核心能力 / Phase 2 | `make verify-rls` | `NEEDS_CHECK` → `HUMAN_VERIFIED` |
| 知识巡检 lint | `Simulated` | 核心能力 / Phase 2 | `make verify-rls` | 只出建议，不静默改写 |
| 组织树只读继承 | `Simulated` | 核心能力 / Phase 3 | `make verify-rls` | `ltree` 继承逻辑在内存引擎上验证；三级组织树的真实 PG 探针**未做**，故不升级 |
| 跨空间晋升脱敏 | `Implemented` | 核心能力 / Phase 3 | `make verify-redaction` | 纯函数 + 用例；脱敏规则需人工持续维护 |
| 备份恢复与一致性对账 | `Simulated` | 核心能力 / Section 12 | `make verify-ops` | SHA-256 manifest 与蓝绿索引重建 |
| 审计证据包导出（audit-bundle） | `Tested-real` | 核心能力 / Section 12 | `make verify-audit` | **跨进程可用**（2026-10-24 修复）：入库时把「文档 ↔ 原始资产」最小映射写到 `ops/index/`（`assets.json` + `document_assets.json`，**派生物**：可从 vault frontmatter + CAS 目录 + 数据库重建），`omnilore demo` 之后另起进程即可导出。证据来自 **4 条真实子进程用例**（`TestAuditBundleCrossProcess`：导出 verified/exit 0、**改 1 字节 → 该资产 `mismatch` + exit 3**、删索引 → exit 1 + 提示先入库、索引无 token/绝对路径）+ `tools/verify_audit_bundle.py` 的 6 条离线断言（含「改回字节恢复 verified」排除假阳性）。**哈希校验不信任索引**：一律读真实字节重算再与 `sha256_hash` 比对。**边界**：真实 PostgreSQL 后端上的持久化取证**尚未做**（当前索引只服务本地/内存模式） |
| 60 秒验证端到端 | `Tested-real` | 核心能力 / 其他宣称 | `make verify-launch` | CI 以真实子进程跑 `bootstrap` + `demo` |
| MCP stdio 工具暴露 | `Tested-real` | 核心能力 / 其他宣称 | `make verify-mcp` | CI 以真实管道完成 JSON-RPC 握手 |
| 零密钥降级如实上报 | `Tested-real` | 核心能力 / 其他宣称 | `make verify-degradation` | `retrieval_mode` / `degraded_reason` 正交 |
| 跨平台 LF 规范化 | `Tested-real` | 核心能力 / 其他宣称 | `make verify-lf` | `windows-latest` 上跑同一套离线用例 + LF 守卫。**边界**：该 job 自 Run #2 起一直红（根因=en-US runner 的 cp1252 代码页，已修但**未推送复验**），Windows 侧回归门禁**当前尚未真正生效**；Windows 证据来自开发者本机真实 NTFS 全量运行 |
| opt-in 匿名遥测 | `Planned` | 核心能力 / 尚未实现 | — | 决策已接受，实现排期 Q2 |
| 模型 provider 抽象（R-04b） | `Implemented` | 核心能力 / 其他宣称 | `make verify-provider` | 向量来源可替换：`OMNILORE_EMBEDDING_PROVIDER` = `dashscope`（默认，行为与抽象前**逐字一致**）/ `openai`（任意 OpenAI 兼容端点：OpenAI / Azure / **Ollama** / vLLM / LM Studio）/ `none`；`OMNILORE_EMBEDDING_BASE_URL` + `OMNILORE_EMBEDDING_API_KEY` 指向自建端点（本地回环地址免 key）。四个曾经的「预留、不读取」变量（`OMNILORE_PRIMARY_MODEL` / `_ESCALATED_MODEL` / `_EMBEDDING_MODEL` / `_EMBEDDING_DIM`）**现已生效**，由 `ModelAdapterConfig.__post_init__` 读取，并有测试断言它们进入**真实请求体**。**证据边界**：用例用 `monkeypatch` 的假 HTTP 端点验证请求体/维度/降级，**没有对真实 OpenAI / Ollama 端点发过请求**，故不标 `Tested-real` |
| ARM64 架构验证（R-11b） | `Tested-real` | 核心能力 / 其他宣称 | —（CI `arm64` job） | CI 新增独立 `arm64` job（`ubuntu-24.04-arm`）跑同一套离线用例 + `ruff` + 冒烟；`integration-rls` 刻意留在 x86。**已 3 次绿灯**（Run #2 `5d513fe`、#4 `cb45932`、#5 `effbb02`，其中 #4/#5 连续），达到「连续 ≥2 次绿灯」的升级门槛，故标 `Tested-real`。**边界**：绿灯只覆盖**离线套件 + `ruff` + 零密钥冒烟**，**不覆盖真实 PostgreSQL 业务数据面** |
| 统一数据目录解析（R-11b） | `Implemented` | 核心能力 / 其他宣称 | `make verify` | 只有一个解析入口 `omnilore.pathutil.resolve_data_dir`；`ops/index` / `ops/bootstrap.json` / `ops/audit_bundles` 同根。静态守卫（`TestUnifiedDataDir`）禁止任何模块自己调 `.expanduser()` 或直接读 `OMNILORE_DATA_DIR` |
| n8n 工作流（3 条） | `Implemented` | 核心能力 / 其他边界明确的能力 | `make verify` | `test_09_n8n_workflows_and_internal_worker_gateway` 自动验证三份 JSON 的结构、无硬编码凭据，以及内部 Worker 网关的入库 / 恢复 / lint / 备份；**尚未在真实 n8n 实例导入、启动和运行**，故不标 `Tested-real` |
| 真实 PostgreSQL 集成 CI | `Tested-real` | 核心能力 / 其他宣称 | `make verify-rls-pg` | 已接入 Actions 的 `integration-rls` job（`pgvector/pgvector:0.8.0-pg16` service container + `pg_isready -U -d` 健康检查 + 零 Secret；exit 2「未验证」/ exit 3「结论为否」**逐码**转成 job 失败，无 `continue-on-error`）。**它已真实运行并连续 2 次绿灯**（Run #1 `8d0ecc3`、Run #2 `5d513fe`，3 条探针均 PASS），达到「连续 ≥2 次绿灯」的门槛，故由 `Implemented` 升 `Tested-real`。**边界**：该 job 只跑**独立 SQL 探针**（`tools/verify_rls_pg.py`），**不覆盖 CLI / MCP 业务运行时的接线**，也**不证明**业务数据已走 PostgreSQL |
| PostgreSQL 安全连接层与 RLS 生效验证 | `Tested-real` | 核心能力 / 其他宣称 | `python tools/verify_rls_app_role.py` | `omnilore/pg_runtime.py` 的 `PgRuntime`：设置 `DATABASE_URL` 时由 MCP stdio 服务端建立连接，强制连接角色**非超级用户、无 `BYPASSRLS`、是策略目标角色成员**，任一不满足即**拒绝启动**（fails closed —— 这类角色会静默绕过 RLS，比连不上更危险）；事务内 `SET LOCAL ROLE` 切到策略角色，并以 `app.*` GUC 注入请求上下文，注入失败即中止事务。证据：**2026-10-25 / 2026-10-26 在真实 PostgreSQL 16.15 上以应用角色 `omnilore_app` 的两次一次性人工验证（两套独立环境）**（**非 CI**，验证脚本未接进 CI），三条探针全部 PASS（未授权空间 0 行 / 授权后 1 行以排除空表假阳性 / 连接角色 `superuser=false, bypassrls=false`），超级用户 `omnilore_migrator` 连接被拒绝启动；输出见第 6.1 节，可复跑脚本 `tools/verify_rls_app_role.py`（需真实 PG + 应用角色）。**离线自动化只有 5 条 mock 用例**（`tests/test_pg_runtime_wiring.py`：默认内存引擎、无 `DATABASE_URL` 时不接 PG、拒绝超用户 / BYPASSRLS 连接角色），**不连真实 PG**。**边界（必须一并读）**：该连接只做安全接入与启动自检，**业务数据层仍走内存 `RLSDatabaseEngine`** —— 本条**不**证明业务状态已持久化到 PostgreSQL，也**不**证明可多人共享；混合检索未接入运行时 |

**当前统计（由测试守护，与 README / CHANGELOG 三处一致）：**
`Implemented` 9 条 ｜ `Tested-real` 10 条 ｜ `Simulated` 12 条 ｜ `Planned` 1 条 —— 合计 **32 条**。

---

## 4. 验证命令怎么跑

除 `verify-rls-pg` 与 `tools/verify_rls_app_role.py` 外，所有 `verify-*` 目标都在 `Makefile` 里且**离线可跑**（不需要数据库、不需要 API Key）：

```bash
make verify          # 全量测试（离线）
make verify-rls      # 权限/RLS/版本/同步相关（内存仿真引擎 —— 结果只证明逻辑）
make verify-sync     # Obsidian 同步：受保护字段可配 + 冲突副本隐藏目录
make verify-audit    # 证据包：改 CAS 里 1 个字节 → mismatch + 退出码 3（改回 → verified）
make verify-honesty  # 诚实标注守卫：README ↔ 本矩阵 ↔ CHANGELOG 三处一致
```

`make verify-rls-pg` 与 `python tools/verify_rls_app_role.py` 是**唯二需要真实 PostgreSQL**
的验证入口：前者需要 `psql` 与 `DATABASE_URL`，会真实应用 `sql/001` + `sql/002` 并执行
RLS 探针；后者用**应用角色**连接串（不能是超级用户）走运行时连接层复现 RLS 三条。
**环境不具备时它们都会以非零码退出并说明原因 —— 它们不会假装验证过。**

```bash
# ① 策略探针 —— 必须指向一个**空库**：脚本只建表、不清理既有对象（非空库会以 exit 2 停下并给出指引）
export DATABASE_URL=postgresql://omnilore_gateway:<pwd>@localhost:5432/omnilore_verify
export OMNILORE_MIGRATOR_DSN=postgresql://omnilore_migrator:<pwd>@localhost:5432/omnilore_verify
make verify-rls-pg

# ② 应用角色探针 —— DATABASE_URL 必须是**应用角色**（非超用户、无 BYPASSRLS），不是 migrator
export DATABASE_URL=postgresql://omnilore_app:<pwd>@localhost:5432/omnilore
export OMNILORE_MIGRATOR_DSN=postgresql://omnilore_migrator:<pwd>@localhost:5432/omnilore  # 可选：预置探针行
python tools/verify_rls_app_role.py   # 0=三条通过；2=未验证；3=已验证但结论为否
```

> 上表里所有 `Simulated` 条目**都没有真实 PG 证据**（两条 `Tested-real` 的例外是
> 「真实 PostgreSQL RLS 策略与跨空间读拦截」与「PostgreSQL 安全连接层与 RLS 生效验证」，
> 证据分别见第 6 节与第 6.1 节）。这是已知边界，不是遗漏。

**同一组探针也跑在 CI 里，而且已经真实绿灯**：`.github/workflows/ci.yml` 的 `integration-rls`
job 使用 `pgvector/pgvector:0.8.0-pg16` service container（`pg_isready -U postgres -d omnilore_verify`
健康检查，零 Secret），执行的就是 `python tools/verify_rls_pg.py`（与 `make verify-rls-pg`
同一条命令）。它把 exit 2（未验证）/ exit 3（结论为否）**逐码**转成 job 失败，
不设 `continue-on-error` —— 因此「没跑起来」不会被伪装成绿灯。
**运行结果：自 2026-10-26 起已连续 2 次绿灯**（Run #1 `8d0ecc3`、Run #2 `5d513fe`，
均为 3 条探针 PASS），达到本页「连续 ≥2 次绿灯」的门槛。
**它覆盖什么 / 不覆盖什么**：只覆盖 `sql/001` 的 SQL 策略在真实 PG 上是否生效；
**不覆盖** CLI / MCP 业务运行时是否接入 PostgreSQL，也**不覆盖** profile 维度、写侧策略、
组织树继承与撤销链路 —— 因此除了「真实 PostgreSQL RLS 策略与跨空间读拦截」与本条
「真实 PostgreSQL 集成 CI」，其余条目一律不许跟涨。

---

## 5. 与威胁模型的关系

本矩阵回答「**哪条能力有哪种证据**」；
[`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md) 回答「**这套设计能防什么、防不住什么**」。
两者一起看，才能判断 OmniLore 是否适合放进你的流程。

**结论先说：这是预发布版本（alpha），请勿放入任何重要业务流程。**

---

## 6. 证据记录一：真实 PostgreSQL 上的 RLS 策略验证（2026-10-24）

**这是本仓库两条真实 PostgreSQL 证据之一。请按它的实际强度使用它。**

| 项 | 值 |
|---|---|
| 日期 | 2026-10-24 |
| 数据库 | 真实 **PostgreSQL 16.15**（官方 `pgvector/pgvector` 镜像，tag `0.8.0-pg16`） |
| 客户端 | `psql` 16.15 |
| 命令 | `make verify-rls-pg`（脚本 `tools/verify_rls_pg.py`，目标库为**空库** `omnilore_verify`） |
| 退出码 | `0`（三条探针全部 PASS） |
| 性质 | 起点是**人工执行、一次性**（2026-10-24）；**现在已由 CI 持续验证** —— 见下方「后续自动化」 |
| 后续自动化 | `.github/workflows/ci.yml` 的 `integration-rls` job 已接入同三条探针，并**已于 2026-10-26 真实运行、连续 2 次绿灯**（Run #1 `8d0ecc3`、Run #2 `5d513fe`）。**边界**：该 job 只跑独立 SQL 探针，**不等于业务运行时已接入 PostgreSQL / 数据已持久化** |

探针完整输出（内联自当时的运行记录，非事后补写）：

```text
======================================================================
真实 PostgreSQL RLS 探针结果
======================================================================
[PASS] P1 至少 7 张核心表已启用 RLS —— rowsecurity=true 的表：7
[PASS] P2 未授权时跨空间行不可见 —— 仅授权 sp_bootstrap_personal 时可见 sp_probe_hidden 行数：0
[PASS] P3 授权后同一行变为可见（排除假阳性） —— 追加授权 sp_probe_hidden 后可见行数：1

结论：已在真实 PostgreSQL 上取得 RLS 证据。
请把本次输出贴进 Issue/PR，作为 RLS 条目升级为 `Tested-real` 的依据。
注意：单次手动验证 ≠ CI 证据；要长期生效需接入自动化集成 CI。
VERIFY_EXIT=0
```

> 上面是 **2026-10-24 当时的原文输出**（未作改写）。其中「要长期生效需接入自动化集成 CI」
> 一句**已于 2026-10-26 成为现实**：同三条探针现由 `integration-rls` job 在 CI 上复跑，
> 已连续 2 次绿灯（见上文「CI 复跑记录」）。脚本当前的收尾提示也已同步更新。

三条探针各自证明了什么（以及**没有**证明什么）：

| 探针 | 证明了 | 没有证明 |
|---|---|---|
| P1 策略已挂载 | 7 张核心表在真实 PG 上 `rowsecurity = true`，策略真的被 `CREATE POLICY` 建出来了 | 策略语义是否正确（挂载 ≠ 生效） |
| P2 越权读被过滤 | 以 `omnilore_gateway` 角色、仅授权 `sp_bootstrap_personal` 时，`sp_probe_hidden` 的行**不可见** | 其它维度（profile / 成员关系变动 / 组织树继承）也被正确过滤 |
| P3 授权后可见 | 把该空间加入授权后同一行**变为可见** —— 与 P2 成对，排除「表里本来没数据」的假阳性 | 写入侧策略（`INSERT` / `UPDATE` / `DELETE`）的行级正确性 |

因此升级范围严格限定为「**策略挂载 + 跨空间读拦截**」这一条。
`四维权限交集`、`Profile 硬性隔离`、`撤销即时失效`、`组织树只读继承` 等条目**没有**被
这三条探针覆盖，维持 `Simulated`。

**CI 复跑记录（2026-10-26）**：上面这组探针已由 `.github/workflows/ci.yml` 的
`integration-rls` job 在 GitHub Actions 上真实运行 **2 次**（Run #1 `8d0ecc3`、Run #2
`5d513fe`），两次都是 3 条 `[PASS]`、exit 0，与本地输出一致。
请把「CI 绿灯」读成「这三条探针在真实 PostgreSQL 上**可复现地**通过」，
**不要**读成「业务运行时已接入 PostgreSQL / 数据已持久化」。

复现提示（踩过的两个坑，已写进脚本与 compose）：
1. 镜像 tag 必须是 `<pgvector版本>-pg<PG主版本>`（如 `0.8.0-pg16`）；写成 `pg16-v0.8.0` 会 pull 失败。
2. 目标库必须是**空库**；非空时脚本以 `exit 2` 停下并给出「新建 `omnilore_verify`」的指引，
   **不会**自动 `DROP SCHEMA` 清库（那可能删掉真实数据）。

---

## 6.1 证据记录二：真实 PostgreSQL 上的「应用角色 + 运行时连接层」RLS 验证（2026-10-25）

**这是本仓库第二条真实 PostgreSQL 证据。请按它的实际强度使用它 —— 尤其注意它「没有」证明什么。**

| 项 | 值 |
|---|---|
| 日期 | 2026-10-25 |
| 数据库 | 真实 **PostgreSQL 16.15**（`pgvector/pgvector:0.8.0-pg16` 容器，库 `omnilore`） |
| 连接角色 | `omnilore_app`（`LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS`，`omnilore_gateway` / `omnilore_worker` 成员） |
| 客户端 | `omnilore/pg_runtime.py` 的 `PgRuntime`（asyncpg）+ 对照用的 `psql` 16.15 |
| 命令 | `python tools/verify_rls_app_role.py` |
| 退出码 | `0`（三条探针全部 PASS） |
| 性质 | **人工执行、一次性**；**不是 CI 门禁**，不可视为「持续证据」 |
| 后续自动化 | **尚未**接入 CI；本条不因 `integration-rls` job 绿灯而自动升级（后者只跑独立 SQL 探针，与本条验证的不是同一条路径） |

探针完整输出：

```text
======================================================================
真实 PostgreSQL RLS（应用角色连接）验证结果
======================================================================
[PASS] P-a 未授权 sp_probe_hidden 时可见行数：0（期望 0）
[PASS] P-b 授权 sp_probe_hidden 后可见行数：1（期望 ≥1）
[PASS] P-c 连接角色 omnilore_app：superuser=False, bypassrls=False（期望均 false）

结论：已用应用角色在真实 PostgreSQL 上证明 RLS 生效。
```

另一条同期实测（fails closed）：以超级用户 `omnilore_migrator` 作为 `DATABASE_URL` 的用户时，
MCP stdio 服务端启动时被拒绝：

```text
[omnilore] DATABASE_URL 已设置，但安全连接失败：连接角色 omnilore_migrator 是超级用户，会绕过 RLS——拒绝在运行时使用
[omnilore] 拒绝启动（fails closed）：应用不得以会绕过 RLS 的角色连接 PostgreSQL。
```

三条探针各自证明了什么（以及**没有**证明什么）：

| 探针 | 证明了 | 没有证明 |
|---|---|---|
| P-a 未授权不可见 | 以**应用角色**（非超用户、无 BYPASSRLS）走 `SET LOCAL ROLE` + GUC 注入的路径时，未授权空间的行**不可见** | 其它维度（profile / 成员关系变动 / 组织树继承）也被正确过滤 |
| P-b 授权后可见 | 把该空间加入授权后同一行**变为可见** —— 与 P-a 成对，排除「表里本来没数据」的假阳性 | 写入侧策略（`INSERT` / `UPDATE` / `DELETE`）的行级正确性 |
| P-c 连接角色安全 | 运行时连接角色 `omnilore_app` 既非超级用户也无 `BYPASSRLS`，因此不会被静默绕过 RLS | 其它部署里运维不会拿超用户串去配 `DATABASE_URL`（这正是启动时强制校验存在的原因） |

**这条证据绝对不能被读成「业务数据已持久化到 PostgreSQL」。** 它验证的对象是
`PgRuntime` 这条**连接**；`OmniLoreEngine` 的业务读写仍然走内存 `RLSDatabaseEngine`，
`AsyncpgRepository` 仍未被业务路径实例化 —— 详见第 2.1 节。

另一个已知事实：**在既有旧数据卷里**，`omnilore_migrator` 是 docker 镜像的 bootstrap 超级用户，
**无法降权**（`ALTER ROLE ... NOSUPERUSER` 报 `permission denied: The bootstrap user must have the
SUPERUSER attribute`）。容器编排现已改为 `POSTGRES_USER=postgres`（与 CI 的 `integration-rls`
一致），新建的卷不会再由 initdb 创建该角色，它只是 `sql/001` 建的 `NOLOGIN` 角色；
`sql/001` 另加 fail-closed 检查（该角色若可登录或为超用户即 `RAISE EXCEPTION`）。
该配置改动**未在真实 docker 环境实跑验证**，旧卷也不会被自动修正（需 `docker compose down -v`）。
无论新旧卷，安全都依靠两重保证：① 应用一律用 `omnilore_app` 连接；
② `PgRuntime._assert_safe_role` 强制拒绝任何超用户 / BYPASSRLS 连接角色。
