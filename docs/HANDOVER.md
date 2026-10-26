# 交接文档（Handover）

> 面向**在外部 agent IDE 中接手本项目的下一个 agent**（也适用于新贡献者）。
> 目标：**读完即可开工，不需要向任何人追问背景，也不依赖本机任何未入库的文件。**

| 项 | 值 |
|---|---|
| 最后核实 | 2026-10-26 |
| 对应 commit | 见 `git log` 最新一条（本文数字以该 commit 为准） |
| 版本 | `v0.3.0-alpha`（PEP 440：`0.3.0a0`） |
| 公开仓库 | https://github.com/KillerMan12345/OmniLore |
| 测试 | 147 项，0 failure / 0 error / 0 skip |
| 追踪文件 | 68 个（19 个 Python 源码 / 10 份文档 / 7 个测试文件 / 4 个工具） |
| CI | 7 个 job，其中 `windows-latest` 的 2 个**仍失败**（见 T1） |

---

## 0. 前 10 分钟：接手 agent 该做什么

```bash
# 1. 取代码
git clone https://github.com/KillerMan12345/OmniLore.git && cd OmniLore

# 2. 建环境（Python ≥ 3.11；CI 覆盖 3.11 / 3.12）
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
python -m pip install -e ".[dev]"

# 3. 确认基线绿（必须 147 项、0 skip）
python -m unittest discover -s tests -p "test_*.py" -v

# 4. 确认零 Key 端到端可用
omnilore bootstrap && omnilore demo            # 无需任何 API Key

# 5. 静态检查（必须含 tools/）
ruff check omnilore tests tools
```

**然后按这个顺序读**（每份职责不同，不要只读一份）：

| 顺序 | 文档 | 看什么 |
|---|---|---|
| 1 | 本文件 | 上下文、任务清单、红线、操作日志 |
| 2 | `docs/FEATURE_STATUS.md` | 每条能力**有没有证据**（单一事实来源） |
| 3 | `docs/ROADMAP_STATUS.md` | 进度、门禁、待办分类 |
| 4 | `docs/DECISIONS.md` | 六项拍板决策的**理由** |
| 5 | `docs/THREAT_MODEL.md` | 能防什么 / 防不住什么 |

**易变数字（测试数、能力条数、四态统计）一律以 `docs/FEATURE_STATUS.md` 与 `README.md` 为准**，本文件只记快照。

### 0.1 外部 IDE 拿不到什么（重要，别去追）

以下只存在于开发者的本机工作目录，**不随仓库分发**。如果你在外部 IDE 里看不到，属正常：

| 缺什么 | 影响 | 应对 |
|---|---|---|
| `.workbuddy/memory/`（agent 工作记忆） | 无 | 本文件已涵盖必要结论 |
| `deliverables/`（内部竞品与策略文档） | 无 | 首发规划的历史材料，**不是当前事实来源** |
| 本机执行日志目录（见 `.gitignore`） | 无 | 历次真实 PG 验证的原始输出，结论已并入 `FEATURE_STATUS` 第 6 / 6.1 节 |
| WSL2 / docker / 真实 PostgreSQL | 影响 T3、T4 | 需要真实 PG 时自建：`docker compose up -d postgres` |

⚠️ **如果你在外部 IDE 里恰好能看到 `deliverables/` 或 `.workbuddy/memory/`**：它们写于 2026-10-23/24，其中「真实 PG RLS 无 CI 证据 / 单次手动验证 ≠ CI 证据」一类表述**已于 2026-10-26 被 CI 实际运行证伪**。那是历史记录，不是现状。现状以 `docs/FEATURE_STATUS.md` 为准。

---

## 1. 项目目标与定位

**一句话**：`OmniLore — One auditable knowledge base, for every agent.`
一份可被任何智能体调用、且每条回答都能追溯到原始证据的知识库。

### 1.1 三层定位（决定所有取舍）

1. **可审计是核心卖点**：原始证据用 SHA-256 做 CAS 存储，每条回答可回溯到原始字节。
2. **治理层，不是替代品**：不做第 N 个 RAG / 向量库。检索交给 RAGFlow / Dify，OmniLore 负责权限、版本、证据链与审计。
3. **agent 无关**：不绑定任何单一 Agent，任何 MCP 客户端都能挂载同一份知识。

### 1.2 服务的人群

| 代号 | 谁 | 为什么重要 |
|---|---|---|
| P1 | 关注安全与合规的使用者 | 买「可审计」的人 |
| **P2** | **Obsidian 重度用户** | **首要服务人群**，社区话语权最高；本项目会在其笔记里写入系统字段，主权敏感度极高 |
| P3 | 研发 / MCP 用户 | **流量入口**（不是首要服务对象，别搞反了） |

### 1.3 当前定位状态（必须如实传达）

**业务数据仍走内存引擎 `RLSDatabaseEngine`，PostgreSQL 尚未承载业务读写。**
因此**不得宣称持久化、不得宣称多人共享**。容器模式目前只是「容器基础设施技术预览」。
唯一的例外是安全连接层（见 2.3），它做连接与自检，不做业务数据路径。

---

## 2. 技术栈与关键设计决策

### 2.1 技术栈

| 层 | 选型 |
|---|---|
| 语言 | Python ≥ 3.11（CI 覆盖 3.11 / 3.12） |
| 默认数据引擎 | `RLSDatabaseEngine` —— 与 PostgreSQL RLS **同构的内存引擎**（不持久化） |
| 目标数据库 | PostgreSQL 16 + `pgvector` + `ltree`（`sql/001`：15 张表、21 条策略） |
| 异步驱动 | `asyncpg` |
| Agent 接口 | MCP stdio 服务（7 个工具） |
| 人类界面 | Obsidian 风格 Markdown vault（frontmatter 承载系统字段） |
| 向量来源 | DashScope（默认）/ OpenAI 兼容（含 Ollama、vLLM、LM Studio）/ 显式停用 / 本地确定性兜底 |
| CI | GitHub Actions，5 类 job（test 矩阵 ×4、arm64、lint、dco、integration-rls） |

### 2.2 六项拍板决策（详见 `docs/DECISIONS.md`）

| 编号 | 决策 | 落地约束 |
|---|---|---|
| D1 | 许可证 **Apache-2.0**（+ NOTICE + DCO + 独立商标政策） | 不改自定义许可 |
| D2 | 对外版本 `v0.3.0-alpha`，内部代码代际仍记 v2.3 | 版本号四处一致 |
| D3 | 接受 opt-in 匿名遥测（默认关闭） | 实现排 Q2，当前 `Planned` |
| D4 | 首发窗口 8 周（0.5 FTE） | 排期依据 |
| D5 | 项目名 **OmniLore**（原 ContextHub 在 GitHub 有 157 个同名仓库） | **仓库内不得再出现 `contexthub` 字面量**，仅 CHANGELOG / DECISIONS 的过去式叙述例外 |
| D6 | 多存储后端**方向认可、冻结到首发后，首发窗口零动作** | 仓库内不应存在多后端适配层代码 |

### 2.3 几个「反直觉但必须遵守」的设计

| 设计 | 为什么 |
|---|---|
| **所有文本读写必须 UTF-8 + LF** | 一致性对账按**字节**算 SHA-256，CRLF 会让对账恒失败。`.gitattributes` 钉死行尾，有 AST 守卫 |
| **降级原因拆成两个正交字段** | `retrieval_mode` + `degraded_reason` 只描述**检索**；`synthesis_degraded` + `synthesis_note` 只描述**答案合成**。合并会让「hybrid + 检索降级非空」变成读不懂的组合 |
| **审计证据包绝不信任索引里的哈希** | `ops/index/` 只提供「asset_id → 文件路径」定位，校验时仍 `open("rb")` 重算。否则索引被改，bundle 就背书假数据 |
| **连接 PostgreSQL 必须拒绝超级用户** | 超级用户与 `BYPASSRLS` 角色会**绕过 RLS**。宁可拒绝启动，也不要「连上了但权限形同虚设」 |
| **向量维度不一致直接报错，不截断补零** | 静默截断会产生**错误但看起来正常的相似度**，比报错危险得多 |
| **`doc_id` 永不放开** | 身份锚点，属正确性问题，不是用户主权问题 |
| **CI 类条目升 `Tested-real` 的门槛 = 连续 ≥2 次绿灯** | 单次绿灯只是「恰好没红」，不构成可复现信号。此门槛已写入 `FEATURE_STATUS` 第 1 节 |

---

## 3. 当前进度快照

### 3.1 已完成（均已在远端）

| 主题 | 内容 | 证据 |
|---|---|---|
| 合规包 | Apache-2.0 + NOTICE + CONTRIBUTING(DCO) + SECURITY + CoC + PR 模板 | — |
| 首启解锁 | `omnilore bootstrap` + seed SQL + 跨进程 token 复用 | 端到端实跑 |
| 跨平台 | `textio.py` 统一 LF，消除 CRLF 对账失败 | 生成文件 CRLF = 0 |
| 零 Key 降级 | 无 Key 走 BM25；供应商故障也降级不抛异常 | 离线可跑 |
| 笔记主权（R-16） | frontmatter 白名单可配（可整体关闭）、冲突副本落隐藏目录、`doc_id` 永不开 | 行为实测 |
| 诚实标注（R-13） | 32 条能力四态标注 + 威胁模型公开页 + 19 个 Makefile 目标 | 守卫测试 |
| 贡献入口（R-10a） | 6 条绕开 `engine.py` 的路径 + 6 套可统计 Issue 表单 + 自动打标 | — |
| 审计证据包（R-05m） | `omnilore audit-bundle` 导出可带走的证据包 | 跨进程篡改实测 |
| 21 条 RLS 策略注释 | 每条注明保护什么 / 依赖哪维 / 防什么 / 不覆盖什么 | 逻辑冻结摘要 |
| 真实 PG 集成 CI（R-08b） | `integration-rls` 每次 push 在真实 PG 16 上跑 3 条探针 | **CI 连续 2 次绿灯** |
| 多 provider（R-04b） | DashScope / OpenAI 兼容 / Ollama / 停用 / 本地兜底 | 默认行为零变化有断言 |
| 平台收敛（R-11b） | 数据目录唯一来源 + arm64 CI job | arm64 已 1 次绿灯 |
| PostgreSQL 安全连接层 | 强制非超用户连接 + `SET LOCAL ROLE` + GUC fails closed | 真实 PG 人工验证 3 条 |

### 3.2 CI 实况（Run #2，commit `5d513fe`）

| job | 状态 |
|---|---|
| test (ubuntu-latest, py3.11 / py3.12) | ✅ |
| test (**windows-latest, py3.11 / py3.12**) | ❌ **未决 → T1** |
| arm64 (ubuntu-24.04-arm, py3.12) | ✅ 首次绿灯 |
| lint | ✅ |
| DCO sign-off | ✅ |
| **integration-rls（真实 PostgreSQL 16 + 探针）** | ✅ **3 条探针全部 PASS（exit 0）** |

### 3.3 真实 PostgreSQL 上的两类证据（**性质不同，别混用**）

| 条目 | 状态 | 证据性质 | 覆盖 / 不覆盖 |
|---|---|---|---|
| 真实 PostgreSQL RLS 策略与跨空间读拦截 | `Tested-real` | **CI 持续验证**（`integration-rls`，自 2026-10-26 连续 2 次绿灯） | ✅ 证明 `sql/001` 的 21 条 SQL 策略在真实 PG 生效<br>❌ **不证明业务运行时已接入 PG，不证明持久化** |
| PostgreSQL 安全连接层与 RLS 生效验证 | `Tested-real` | **人工一次性、非 CI**（脚本 `tools/verify_rls_app_role.py` 未接 CI） | ✅ 应用角色走运行时连接路径时 RLS 生效<br>❌ 同样不证明业务数据持久化 |

**只有 space + category 两维被真实探针验证过。** membership / profile / 撤销即时失效 / 组织树只读继承**均无真实 PG 探针**，一律维持 `Simulated`。

---

## 4. 重要文件路径地图

### 4.1 源码（`omnilore/`，19 个文件）

| 文件 | 行数 | 职责 / 注意 |
|---|---|---|
| `engine.py` | 3489 | **全库最大**，主引擎 + 业务逻辑。贡献者心理门槛来源；架构级重构条件化到贡献者 ≥10 人才做 |
| `audit_bundle.py` | 900 | 审计证据包导出 |
| `bootstrap.py` | 691 | 首启引导、token 签发、**文件权限收紧**（Windows `icacls` 分支） |
| `embedding_provider.py` | 446 | provider 抽象（4 种实现） |
| `cli.py` | 441 | CLI：`bootstrap` / `demo` / `version` / `audit-bundle` |
| `mcp_stdio_server.py` | 382 | MCP stdio，7 个工具；设了 `DATABASE_URL` 才建安全 PG 连接 |
| `parsers.py` | 359 | 多格式解析（纯函数，**新贡献者入手首选**） |
| `ops_manager.py` | 239 | 备份恢复 / 运维 |
| `__init__.py` | 236 | 导出（含 PEP 562 惰性导出，避免循环导入） |
| `runtime_index.py` | 213 | 运行时索引 `ops/index/`（**派生物**，非权威数据） |
| `pg_runtime.py` | 197 | 安全 PG 连接层（拒绝超用户 / `SET LOCAL ROLE` / GUC） |
| `dashscope_client.py` | 188 | `ModelAdapter` + HTTP 传输（所有 provider 共用，便于 monkeypatch） |
| `pg_repository.py` | 148 | `AsyncpgRepository`（**已定义但无业务调用**，数据面迁移主战场） |
| `textio.py` | 129 | 统一 LF 的文本 I/O |
| `http_worker_api.py` | 100 | Worker HTTP 接口 |
| `daemons.py` | 92 | 守护进程 |
| `pathutil.py` | 67 | `resolve_data_dir()` —— 数据目录解析**唯一来源** |
| `version.py` | 27 | 版本号单一来源 |
| `__main__.py` | 17 | `python -m omnilore` |

### 4.2 其他关键路径

| 路径 | 说明 |
|---|---|
| `sql/001_omnilore_schema_and_rls.sql` | 15 张表 + **21 条 `CREATE POLICY`**（逐条注释；策略逻辑被冻结摘要钉住，改逻辑会让既有验证作废） |
| `sql/002_omnilore_bootstrap_seed.sql` | 首启种子数据 |
| `.github/workflows/ci.yml` | 5 类 job；`integration-rls` 是唯一需要外部服务的门禁 |
| `.github/workflows/triage.yml` | Issue 自动打标（「我已放弃」→ `P0-escape`，「疑似越权」→ `security-triage`） |
| `.github/ISSUE_TEMPLATE/` | 6 套表单（下拉单选优先，保证可统计） |
| `tools/verify_rls_pg.py` | 真实 PG 探针，**fail-closed**：0 通过 / 2 未验证 / 3 结论为否 |
| `tools/verify_rls_app_role.py` | 以应用角色复现 RLS 三条（人工可复跑，**未接 CI**） |
| `tools/verify_audit_bundle.py` | 证据包 6 条断言（含「改 1 字节 → mismatch」） |
| `tools/mutation_check.py` | 变异测试工具。**必须副本模式**（历史上曾改坏活仓库） |
| `Makefile` | 19 个目标（`help` + 17 个 `verify-*` + `test` + `lint`） |
| `n8n/workflows/` | 3 条工作流 JSON（结构已验证，未真实导入运行） |
| `tests/` | 7 个文件：9 / 10 / 5 / 34 / **59** / 30 = **147** |

### 4.3 不随公开仓库分发的内容（已 `.gitignore`）

- `deliverables/` —— 内部竞品分析与产品策略（历史材料）
- 本机执行日志目录 —— 历次真实 PG 验证原始输出
- `.workbuddy/` —— agent 工作记忆
- 含本机数据库口令的备注文件（**不要取消忽略，不要提交**）

---

## 5. 已完成产出的归档索引

| 产出 | 位置 |
|---|---|
| 六项拍板决策与依据 | `docs/DECISIONS.md`（D1–D6） |
| 能力四态标注 + 真实 PG 证据记录 | `docs/FEATURE_STATUS.md`（32 条；第 6 / 6.1 节） |
| 安全边界（能防 / 防不住） | `docs/THREAT_MODEL.md` |
| 进度、里程碑、门禁、待办 | `docs/ROADMAP_STATUS.md` |
| 需求矩阵 `FR-01`~`FR-19` | `docs/01_Product_Requirements_Document_v2.3.md` |
| 架构设计 | `docs/02_Architecture_and_Engineering_Design_v2.3.md` |
| 用户故事与验收标准 | `docs/03_User_Stories_and_Scenarios_v2.3.md` |
| 测试计划与验证报告 | `docs/04_Test_Plan_and_Verification_Report_v2.3.md` |
| 版本变更记录 | `CHANGELOG.md`（**单一段落 `[v0.3.0-alpha]`，禁止新建 `[Unreleased]`**） |
| 21 条 RLS 策略设计说明 | `sql/001_omnilore_schema_and_rls.sql` 第 6 节注释 |
| 可复跑验证入口 | `Makefile` 的 17 个 `verify-*` 目标 |
| **本交接文档与操作日志** | `docs/HANDOVER.md`（本文件，第 9 节） |

---

## 6. 剩余任务清单

优先级：**P0** 阻塞发布或违反红线 ｜ **P1** 首发窗口内 ｜ **P2** 首发后 ｜ **Q2** 下季度
执行者：**A** = agent 可自主完成 ｜ **M** = 维护者（需真人 / 凭证 / 出境）｜ **A+M** = 需配合

### T1（P0，A+M）修 `windows-latest` 的 2 个 CI job

- **现状**：`test (py3.11/3.12 on windows-latest)` 在 step 5「Run the full offline test suite」退出码 1。开发者本机 Windows 跑 147 全绿，故为 runner 环境差异。
- **已证伪的假设（别再试）**：临时目录带**显式** `Everyone` ACE 不会导致失败 —— 父目录的显式 ACE 在子文件上表现为继承型（带 `(I)`），`/inheritance:r` 照样清除。
- **依赖**：**必须先拿到 CI 日志**。取日志唯一途径是维护者从网页复制（job logs API 需 admin，返回 403）：
  `https://github.com/KillerMan12345/OmniLore/actions/runs/40257380070/job/121362029547`
- **验收**：推送后 CI 全绿（含 windows-latest 两个 job），且 147 项仍 0 skip。

### T2（P0，A）~~修正被 CI 证伪的证据口径~~ → **已完成**

`integration-rls`（CI 持续验证，2 次绿灯）、`真实 PostgreSQL 集成 CI`（升 `Tested-real`）、arm64（维持 `Implemented`，仅 1 次绿灯、样本尚少）的口径已在 README / FEATURE_STATUS / ROADMAP_STATUS / THREAT_MODEL / CHANGELOG / ci.yml / `verify_rls_pg.py` 六处同步；守卫断言重写为 5 条并用 **7 条变异测试**验证有效（7/7 变红）。

### T3（P1，A+M）PostgreSQL 数据面迁移（业务运行时真正接 PG）

- **工作量 4–6 人日**。**不要当成「实例化一个 repository」**——`engine.py` 的查询谓词是任意 Python lambda，且多处直接访问 `self.db._state[...]`，无法靠替换后端引擎无缝切换，需逐业务方法重写。
- **依赖**：T1（CI 全绿）+ 可连的真实 PG。
- **验收**：
  1. 业务读写落 PostgreSQL；RLS 在应用路径上仍生效 —— **必须成对验证**：未授权 0 行 **且** 授权后同一条 ≥1 行（后者排除「表本来就是空的」假阳性）
  2. 不设 `DATABASE_URL` 时行为与现在完全一致（仍内存引擎），用测试钉住
  3. 147+ 项全绿
  4. **只有以上全部成立**，文档才允许改写「持久化」口径

### T4（P1，A）`docker-compose.yml` 角色同名隐患

- **现状**：compose 的 `POSTGRES_USER=omnilore_migrator` 与 `sql/001` 的 `CREATE ROLE omnilore_migrator NOLOGIN` **同名**。initdb 先把它建成**可登录的超级用户**，SQL 的 `IF NOT EXISTS` 块随后跳过创建，导致本应 `NOLOGIN` 的属主角色变成可登录超用户。
- **修法**：compose 改用其他 `POSTGRES_USER`，把 `omnilore_migrator` 留给 SQL 按 `NOLOGIN` 创建。
- **验收**：`docker compose up` 后该角色为 `NOLOGIN` 且非超用户；21 条策略仍建出；PG 探针仍 3 条 PASS。

### T5（P1，M）开启 Private vulnerability reporting

Settings → Security。`SECURITY.md` 的首选披露渠道依赖它；不开则漏洞报告只能走公开 Issue。

### T6（P1，M）hero GIF 素材

README 首屏动图，影响 clone 率。**必须真实录屏**，不得用生成素材替代（性质等同虚标）。

### T7（P0 门禁，M）第 0 波封闭验证（G3 门禁）

10–20 人 × 30 分钟 1v1，**主持人全程静默**（一旦提示，测的就不是产品）。
**未完成则不得公开宣传**。样本量限制：结果只能作 Go/No-Go 方向性依据，**严禁对外宣称「首次运行成功率 80%」**。

### T8（P2，M）打 `v0.3.0-alpha` tag

G2 门禁要求先手动跑一次 demo 并存档输出。

### T9（P2，A）首发后工程项

- R-05f 完整 Demo（RECLASSIFY / REVERT_VERSION 全链路，2.5 人日）
- R-09 文档站 8 页 + 中英双语 README（4.0 人日）
- R-10b 10–20 个 good first issue（1.0 人日）

### T10（Q2，A）

- R-06 Policy-aware MCP（工具 7 → 11，3.5 人日）
- R-07b Obsidian Governance Bridge 后半（5.0 人日）
- R-12 匿名遥测（4.0 人日，D3 兑现）
- R-15e `engine.py` 拆分（6.5 人日，**条件触发**：贡献者 ≥10 人才做）

### T11（P2，A）收尾杂项

- `docs/ROADMAP_STATUS.md` 章节编号从 4 直接跳到 7（缺 5、6），应重编号
- 清理更名遗留 docker 资源：`contexthub_pgdata` 卷、`contexthub_default` 网络（名字仍是旧名 ContextHub）
- **守卫覆盖缺口**：`test_every_make_target_mentioned_in_docs_exists` 只扫 `README.md` 与 `docs/FEATURE_STATUS.md`，不扫 `ROADMAP_STATUS.md` / `THREAT_MODEL.md` / `CHANGELOG.md` —— 在这三份里写不存在的 `make xxx` 不会被抓。建议扩到全部 tracked 文档，**扩完要变异验证**：在 `ROADMAP_STATUS.md` 写一个假目标，测试必须变红

---

## 7. 已知未决问题（接手前必读）

| # | 问题 | 影响 | 现状 |
|---|---|---|---|
| 1 | windows-latest CI 2 个 job 失败 | 发布门禁 | 待 CI 日志（T1） |
| 2 | 业务数据仍在内存引擎，PG 未承载业务读写 | 核心卖点不完整 | 文档已如实降级，**不得宣称持久化** |
| 3 | `omnilore_migrator` 无法降权 | 安全依赖两重保证 | docker 镜像保护 bootstrap 超用户，`ALTER ROLE ... NOSUPERUSER` 报 permission denied。安全靠「应用只用 `omnilore_app`」+「`PgRuntime` 拒绝超用户」 |
| 4 | compose 角色同名 | 容器模式角色被错误提权 | T4 |
| 5 | 21 条策略中只有 space + category 两维有真实探针 | 证据范围有限 | 其余维度维持 `Simulated` |

---

## 8. 硬性约定与红线（违反即返工）

1. **禁止虚标**。真实 PG RLS 有 CI 证据的**只有**那 3 条探针覆盖的范围；其余一律 `Simulated`。README 禁用 `production` / `stable` / `企业就绪`。
2. **数字四处同步**：README / CI step 名 / CHANGELOG / `FEATURE_STATUS`（外加 `ROADMAP_STATUS` 与 `docs/04` 也要跟着）。有测试守着，不一致会直接红。
3. **平台专属命令必须先判平台再调用**。`icacls` / `whoami` / `mklink` / `psql` 等，**绝不能「先调用再按 `os.name` 分支」**。本项目长期只在 Windows 开发，CI 的 Linux/ARM 是首次非 Windows 执行——这个坑已踩过一次（5 个 job 全红）。
4. **改能力必须同时改三处**：README 宣称 / `FEATURE_STATUS` 矩阵 / CHANGELOG。
5. **新增守卫必须用变异测试证明有效**：把实现故意改坏，测试必须变红。空转守卫比没有守卫更危险（已踩三次：断言 `startswith("~")` 但输入无 `~`；断言 ACE 不含 Everyone 但默认 ACL 本就没有；断言 `assertIn("exit 2")` 却被自己的日志文案满足）。
6. **角色名与库名是 `omnilore_*`（omni + lore）**，**不是** `omniloire_*`。DSN 一律从数据库读出后动态构造，**绝不手工拼写**（曾因多打一个 i 浪费大量排查轮次）。
7. **`tools/mutation_check.py` 只能在副本上跑**。历史上它直接改活仓库且不还原，差点把「首启归零 + token 文件不可读」的破坏带进发布。
8. **公开文档不得引用未入库的内部目录**（会成死链）。
9. **CHANGELOG 只有一个版本段** `[v0.3.0-alpha]`。维护者明确要求「这就是第一版，不要过程记录」——不得新建 `[Unreleased]`，不得写入迭代计数、缺陷编号、分轮标记。
10. **所有 commit 必须带 DCO 签名**（`-s`）。
11. **CI 类条目升 `Tested-real` 的门槛 = 连续 ≥2 次绿灯**（见 2.3）。
12. **任何「CI 已验证」的表述，必须同时说明它覆盖了什么、没覆盖什么**。

---

## 9. 操作日志（2026-10-26，按时间顺序）

| # | 操作 | 涉及文件 | 原因 |
|---|---|---|---|
| 1 | 诊断推送被拒 | — | `git push` 报 `fetch first`；远端有 `8aeb48a Initial commit`（建仓库时勾选了 README），与本地 `--orphan` 重建的历史**无共同祖先** |
| 2 | 强推覆盖远端 | 远端 `main` | 远端仅一个 40 字节占位 README，本地 50KB 版本完整覆盖它；用 `--force-with-lease`（比 `--force` 安全） |
| 3 | 验证推送结果 | — | 10 个 commit、68 文件；敏感目录（deliverables / 日志目录 / 含口令备注 / .workbuddy）**均 0 条**；DCO 签名齐全 |
| 4 | 读 CI Run #1 | — | `integration-rls` **PASS**（3 探针 exit 0）✅；lint / DCO ✅；**5 个 test job 全红** |
| 5 | 复现失败 | — | 先在 Windows 克隆跑 → 全绿（排除代码问题）；再用 **WSL Python 3.12** 跑 → 复现 1 个 ERROR |
| 6 | 定位根因 | `tests/test_w0_launch.py:150` | `_assert_file_is_tightened()` **无条件调用 Windows 专属 `icacls`** 后才按 `os.name` 分支 → Linux 上 `FileNotFoundError`。同文件另一测试有守卫，唯独这个被两处复用的辅助函数没有 |
| 7 | 修复 + 双平台验证 | `tests/test_w0_launch.py` | 平台判断提到调用之前。Windows 全绿；WSL Linux 全绿 |
| 8 | 提交并推送 | `5d513fe` | CI Run #2：**5 红降到 2 红**；ubuntu / arm64 / integration-rls / lint / DCO 全绿，仅 windows-latest 两个 job 红 |
| 9 | 证伪一个假设 | — | 造带**显式** Everyone ACE 的临时目录跑权限测试 → 仍通过（父目录显式 ACE 在子文件上是继承型）。记入 T1 避免重复试错 |
| 10 | 核实交接所需事实 | — | git 历史、文件清单、测试分布、能力矩阵 10/9/12/1=32、Makefile 19 目标、CI 5 类 job、模块行数 |
| 11 | 发现新的文档—实现错位 | README / FEATURE_STATUS / ROADMAP_STATUS / THREAT_MODEL / CHANGELOG / ci.yml / `tools/verify_rls_pg.py` | CI 已连续 2 次绿灯，但文档仍写「CI 尚未接入 / 待首次绿灯」→ **红线违反且已公开** |
| 12 | 修正证据口径（T2） | 上述 7 处 + `tests/test_w2_w3_roadmap.py` | RLS 条目标为 CI 持续验证（附「不等于业务运行时已接入」限定）；集成 CI 升 `Tested-real`；arm64 维持 `Implemented`（1 次绿灯、样本尚少）；守卫断言重写为 5 条 |
| 13 | 变异测试验证新守卫 | — | 7 条变异 7/7 变红（含首轮漏判后补的「绿灯」正向 + 「待首次绿灯」反向断言） |
| 14 | 测试数随守卫增加 | README / CHANGELOG / FEATURE_STATUS / ROADMAP_STATUS / docs/04 / ci.yml（2 处 step 名） | 145 → **147**；`test_w2_w3_roadmap.py` 57 → **59** |
| 15 | **生成本交接文档** | `docs/HANDOVER.md`（新增） | 维护者要求 |
| 16 | 复核残留措辞 | — | 确认剩余「待首次绿灯 / 没有持续 CI 证据」共 9 处均为**历史叙述**或**仍成立的定义**（`Simulated` 的定义本就是「真实 PG 无 CI 证据」，两条例外已单独写明），**无新虚标** |
| 17 | 按「外部 agent IDE 接手」重写 | `docs/HANDOVER.md` | 外部 IDE 拿不到本机记忆、内部文档、WSL 环境 → 改为自包含：新增「前 10 分钟」快速开始、外部缺什么、环境要求 |
| 18 | 同步文档 | `README.md`（文档索引）、`CHANGELOG.md` | 保持与实际状态一致 |

**更早的历史**（W0 首发准备、W2–W3、R-05m、R-08b、R-04b、PG 安全连接层等）见 `git log` 与 `CHANGELOG.md`。
