# OmniLore v2.3 测试计划、用例矩阵与实测验证报告

> **文档编号：** CH-TEST-2026-v2.3  
> **测试执行日期：** 2026-10-22  
> **测试环境：** Linux (Python 3.11.2, `unittest.IsolatedAsyncioTestCase`, `PyYAML`, `Pillow`, `pdfplumber`, `pypdf`)  
> **编写原则：** 100% 基于事实记录，明确区分“在当前 Linux 运行时中物理执行的测试”与“在本地 Docker + 云端 API 环境中联调的事项”

> **当前状态补充：** 本报告保留 2026-10-22 的 19 项验收基线；当前仓库全量套件已扩展为 140 项。
> 最新能力证据见 [`FEATURE_STATUS.md`](FEATURE_STATUS.md)，公开路线图进度见
> [`ROADMAP_STATUS.md`](ROADMAP_STATUS.md)。新增用例不会反向改写本报告的历史执行日期。

## 1. 测试架构与执行边界说明（基于事实披露）

### 1.1 在当前 Linux 运行环境中 100% 物理执行并验证通过的部分

1. **真实磁盘文件网关与操作系统级攻击防御：**
   - 在临时文件系统上真实创建 `00_Inbox/`、`01_Processing/`、`02_Vault/spaces/<space_id>/wiki/`、`ops/cas/` 目录结构。
   - 真实执行文件字节写入、SHA-256 完整性校验与 `os.replace` 原子移动。
   - 真实调用 `os.symlink` 创建指向受控目录外部宿主文件的软链接，验证 `SecureVaultStorage` 在解析路径每一级分段时对软链接与 `../` 路径穿越的物理拦截。
2. **真实多格式解析与 AST 语法树提取：**
   - 使用 `Pillow` 在内存中生成真实 PNG 二进制图像并验证分辨率/格式提取。
   - 使用 Python 标准库 `ast` 解析 Markdown 代码块中的类与异步方法定义（如 `PaymentCallbackController`、`verify_webhook_signature`）。
   - 使用正则表达式与 `PyYAML` 执行健康指标结构化提取、敏感信息（手机号/API 密钥）脱敏替换与 Frontmatter Schema 强校验。
3. **真实异步状态机、故障注入与断点自愈：**
   - 在 `STAGED` 阶段通过 `simulate_crash_after_stage=True` 真实抛出运行时异常中断协程，验证未完成发布的 `PENDING_PUBLISH` 文档在检索中不可见，随后通过 `OutboxRecoveryDaemon` 从 Outbox 恢复并完成最终提交。
4. **真实快照备份、SHA-256 逐文件校验恢复与 JSON-RPC 2.0 协议路由：**
   - 真实将状态与磁盘文件备份到快照目录、生成 `manifest.json` 哈希表、执行全量向量索引重建、再从备份目录核对 SHA-256 恢复并运行 `reconcile_consistency` 对账。

### 1.2 采用内存级仿真（需在本地宿主机执行真实外部联调）的两个外部组件

| 外部组件 | 当前自动化测试中的仿真方式 | 宿主机生产联调验证方式 |
|---|---|---|
| **PostgreSQL 16 (`pgvector` + `ltree` + RLS)** | `RLSDatabaseEngine` 在内存中模拟权限语义；另有一次人工真实 PG 三探针，只覆盖策略挂载与跨空间读过滤 | `tools/verify_rls_pg.py` 可在空库独立应用 `sql/001` + `sql/002` 并探测策略。**当前 CLI / MCP 尚未实例化 `AsyncpgRepository`，`DATABASE_URL` 也未接入应用运行时；启动 compose 不等于业务请求已走 PostgreSQL。** |
| **阿里云百炼大模型 API (`DashScope`)** | 未设置 `DASHSCOPE_API_KEY` 时走确定性分类/编译与本地向量；远程路径用假 transport 验证请求形状与降级 | 如需自行联调，必须把 Key 载入**进程环境变量**（本地运行不自动读取 `.env`）。仓库当前没有真实网络调用证据，故不能把配置步骤写成已验证结果 |

## 2. PRD 第 14 章量化验收指标追踪矩阵（Traceability Matrix）

| 验收 ID | 指标名称 | PRD 目标值 | 对应自动化测试方法 | 实测结果 |
|---|---|---|---|---|
| **AC-S01** | 未授权 SQL/接口读写 | 0 次成功 | `test_01` & `test_ac_s01_s02_s03` | **PASS（0 次越权）** |
| **AC-S02** | Work Profile 读取家庭/健康类别 | 0 次成功 | `test_01` & `test_ac_s01_s02_s03` | **PASS（0 次召回/读取拒绝）** |
| **AC-S03** | Family Profile 读取个人私库或企业内容 | 0 次成功 | `test_01` & `test_ac_s01_s02_s03` | **PASS（0 次越权）** |
| **AC-S04** | 非法跨空间/跨类别更新 `space_id` / `category` | 0 次成功 | `test_02` & `test_ac_s04` | **PASS（触发 `WITH CHECK` 拒绝）** |
| **AC-S05** | 路径穿越、裸路径与符号链接读取 | 0 次成功 | `test_02` & `test_ac_s05` | **PASS（全部拦截）** |
| **AC-S06** | Token 撤销与成员移除传播时延 | ≤ 5 秒 | `test_06` & `test_ac_s06_s07` | **PASS（主动清缓存，进程内 < 1ms）** |
| **AC-S07** | 权限变更与跨空间操作审计覆盖率 | 100% 记录 | `test_04`, `test_06`, `test_07` | **PASS（100% 写入 `audit_events`）** |
| **AC-D01** | 相同文件改名连续重放 10 次 | 仅产生 1 个有效文档 | `test_03` & `test_ac_d01_d02` | **PASS（文档数=1，任务数=1）** |
| **AC-D02** | 台账追加幂等（`ledger_event_id`） | 仅追加 1 次 | `test_03` & `test_ac_d01_d02` | **PASS（台账记录数=1）** |
| **AC-D03** | 每个文档系列当前版本唯一性 | 始终唯一，冲突抛错 | `test_03` & `test_ac_d03_k04` | **PASS（并发旧指针触发 `VersionConflictError`）** |
| **AC-D04** | 阶段中断后 Outbox 恢复 | 100% 可恢复且未完成前不可见 | `test_ac_d04_d05_crash_after_stage_and_outbox_recovery` | **PASS（恢复前召回=0，恢复后状态 `COMMITTED`）** |
| **AC-D05** | 文件与数据库状态一致性对账 | 0 幽灵记录 / 0 孤儿文件 | `test_08` & `test_ac_d04_d05` | **PASS（`is_consistent = True`）** |
| **AC-D06** | Obsidian Markdown 双向同步防死循环与冲突保护 | 0 死循环，并发生成冲突副本 | `test_ac_d06_obsidian_sync_loop_guard_protected_fields_and_conflict_copy` | **PASS（自写返回 `NOOP`，并发生成 `.conflict_r1.md`）** |
| **AC-K01/02** | 正式 Wiki 来源关联与引用可定位率 | 100% | `test_01` ~ `test_08` | **PASS（100% 绑定 `document_assets` 与原文校验）** |
| **AC-K04** | 未请求历史时旧版本默认误召回 | 0 次 | `test_03` & `test_ac_d03_k04` | **PASS（`SUPERSEDED` 版本默认召回=0）** |
| **AC-K06/07** | 跨边界混合内容处理 | 100% 进入 `NEEDS_REVIEW` | `test_04` & `test_ac_k06_k07` | **PASS（100% 隔离待确认）** |
| **AC-AI01/02/03**| 模型统一入口、Schema 校验与越权字段拦截 | 100% 拦截非法字段 | `test_ac_ai02_ai03_ai05_ai06` | **PASS（注入 `space_id` 触发 `MODEL_SCHEMA_INVALID`）** |
| **AC-AI04** | Embedding 版本、维度与索引批次可追踪 | 100% 记录并支持重建 | `test_08` | **PASS（支持 `v4-1024` 升级重建至 `index_version=2`）** |
| **AC-AI05/06** | 敏感类别未授权外发拦截与脱敏日志 | 0 次未授权外发，0 明文密钥日志 | `test_ac_ai02_ai03_ai05_ai06` | **PASS（拦截记录 `POLICY_BLOCKED`，日志无原文/Token）** |


### 3.9 `test_09_n8n_workflows_and_internal_worker_gateway`
- **覆盖目标**：离线验证 `n8n/workflows/*.json` 三份模板的 JSON 结构、零第三方 API Key / 零 BYPASSRLS 凭据约束（D-11、6.4.3、7.1），以及 `omnilore/http_worker_api.py` 内部 Worker 网关的入库、状态查询、Outbox 恢复与全量备份。
- **实测结果**：`PASS`（仅结构与内部网关；**未在真实 n8n 实例导入、启动或运行**）
