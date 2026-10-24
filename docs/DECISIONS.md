# OmniLore 关键决策记录 (DECISIONS)

> 本文件记录开源首发 W0 阶段由主理人拍板的**六项不可随意推翻的决策**，
> 以及它们落到代码 / 文档中的具体位置。变更本文件需要单独 PR 并说明动机。
>
> 当前对外版本：`v0.3.0-alpha`（内部代际代号 v2.3，PEP 440 内部版本号 `0.3.0a0`）。

| 编号 | 决策 | 结论 | 落地位置 |
|---|---|---|---|
| D1 | 开源许可证 | **Apache-2.0**（不采用任何自定义开放许可） | `LICENSE`、`NOTICE`、`pyproject.toml`、`CONTRIBUTING.md` |
| D2 | 版本号 | 对外统一 `v0.3.0-alpha`；内部代际仍记 v2.3 | `pyproject.toml`、`omnilore/__init__.py`、`CHANGELOG.md`、README |
| D3 | 遥测 | 接受 **opt-in 匿名遥测**；W0 只落决策，Q2 再实现 | 第 3 节（约束清单）、`CHANGELOG.md` |
| D4 | 首发窗口 | **8 周**，投入 0.5 FTE | 第 4 节、`docs/ROADMAP_STATUS.md` |
| D5 | 项目命名 | **OmniLore** | 第 5 节、全仓重命名 |
| D6 | 多存储后端 | **方向认可，冻结到首发后**；首发窗口零动作 | 第 6 节 |

---

## 1. D1 — 许可证：Apache-2.0

**决定**：采用 Apache License 2.0 **标准原文**，配套 `NOTICE` 文件、DCO
`Signed-off-by` 要求，以及独立的商标政策说明。**明确不采用**任何自定义开放许可
（包括但不限于修改版 MIT、SSPL、BSL、“ Commons Clause” 类附加条款）。

**理由要点**

- Apache-2.0 提供明确的**专利授权**与**贡献者条款**，企业法务可直接放行；
- 许可证必须是**未经改动的原文**，否则不再是那个被公认的许可证；
- 商标不属于 Apache-2.0 授权范围，因此必须另设独立的商标政策。

**落地要求**

- `LICENSE`：Apache-2.0 完整标准文本（取字 `apache.org` 原文），**不得摘要、不得占位**；
- `NOTICE`：说明项目归属、许可证、第三方依赖与商标政策；
- 每个 commit 带 `git commit -s` 的 DCO 签名；CI 校验；
- `pyproject.toml` 标注 `license = "Apache-2.0"` 与 `license-files`。

---

## 2. D2 — 版本号：`v0.3.0-alpha`

**决定**：对外统一以 **`v0.3.0-alpha`** 自称；工程内部的「代际」仍记为 **v2.3**
（保留历史文档 `*_v2.3.md` 命名与 PRD 追踪矩阵的连续性）。

### PEP 440 的取舍（重要）

Python 打包生态要求版本号符合 PEP 440，而 `0.3.0-alpha` **不是**合法 PEP 440 字符串
（合法写法是 `0.3.0a0`）。因此：

| 位置 | 取值 | 原因 |
|---|---|---|
| `pyproject.toml` → `version` | **`0.3.0a0`** | setuptools/pip 只接受 PEP 440，`0.3.0-alpha` 会在构建期报错 |
| `omnilore/__init__.py` → `__version__` | `0.3.0a0` | 与包元数据保持一致 |
| `omnilore/__init__.py` → `__version_display__` / `__version_label__` | `v0.3.0-alpha` / `0.3.0-alpha` | 对外展示与 MCP `serverInfo` 使用 |
| `mcp_stdio_server.py` → `serverInfo.version` | `0.3.0-alpha` | MCP 握手对版本串格式无 PEP 440 约束 |
| README / CHANGELOG / 文档 | `v0.3.0-alpha` | 对外口径 |

> 结论：**两者是同一版本**，差异纯粹来自 PEP 440 的机读要求。任何对外文案一律写
> `v0.3.0-alpha`；任何需要被 pip/setuptools 解析的地方一律写 `0.3.0a0`。

### 措辞红线

alpha 阶段**禁止**使用 `production` / `stable` / `企业就绪` / `production-ready`
等措辞。CI 与人工 review 都会检查。

---

## 3. D3 — 遥测：opt-in 匿名遥测被接受（本次不实现）

**决定**：**接受** opt-in 匿名遥测，但必须满足下列硬约束；**W0 阶段只落本文档，
不写一行遥测代码**，实现排期在 **Q2**。

### 设计约束（实现时必须全部满足，缺一不可）

1. **严格 opt-in**：必须显式 consent 才允许采集。默认状态为 **关闭 (off)**。
   任何“默认为真、用户去找开关”的设计都被禁止。
2. **默认关闭**：全新安装、全新 `omnilore bootstrap` 之后均不得采集；
   `omnilore demo` 也必须在不采集的前提下跑通。
3. **字段极简**：只采集匿名聚合计数（例如「是否发生 hob?」、「降级发生次数」、
   「子命令被调用次数」）。**禁止**采集：文档内容、标题、文件路径、查询词、
   `space_id`、用户 ID、token（含哈希）、IP、主机名、任何 PII。
4. **`--show` 可自证**：必须提供 `omnilore telemetry --show`（或等价开关）
   把**将要发送的完整原始 payload** 打印出来，让用户可以自行核对“就这些”。
5. **离线可用**：遥测失败、无网络、被代理拦截时一律静默放弃，**绝不能**阻塞主流程
   或打印误导性错误。
6. **随时可关**：环境变量 `OMNILORE_TELEMETRY=0` 必须一次性全局关闭，优先级最高。

**为什么现在不实现**：W0 的首要目标是「让陌生人第一次就能跑起来」。在还没有拿到
真实使用数据之前引入遥测，只会增加新用户的心理负担与合规风险。

---

## 4. D4 — 首发窗口：8 周（0.5 FTE）

**决定**：接受 **8 周**首发窗口，投入 **0.5 FTE**。

对 W0 范围的直接影响：

- **只做**解锁首启、跨平台正确性、文档真实性与 CI 门禁；
- **明确不做**：`engine.py` 的架构级重构（排期在 **Q2 之后**）、遥测实现（Q2）、
  n8n 作为默认启动项（已改为可选 profile）；
- 任何超出以上的需求，请走 Issue 排队，而不是扩大 W0 范围。

---

## 5. D5 — 项目命名：OmniLore

**决定**：项目由 `ContextHub` 更名为 **`OmniLore`**，在首次公开发布前完成全量重命名。

**理由**：调研了 11 个候选名，其中 **10 个在 GitHub 上已撞车** —— `contexthub` 有 157 个
同名仓库、含 2 个同领域竞品；`cognita` 被 `truefoundry/cognita`（RAG 框架）占死；
`substrate` 被 `agent-substrate` 占死；`agora` 被声网占死。`OmniLore` 只有 6 个匹配且
全是学生课程作业，是唯一干净的名字。

**语义**：`omni`（跨一切 / 通用，表达「不同智能体都可调用」）+ `lore`（积累的知识）。
对外一句话定位：**OmniLore — One auditable knowledge base, for every agent.**

**影响范围**：包名与模块目录、CLI 与 MCP 脚本名、MCP `serverInfo.name`、环境变量前缀
（`OMNILORE_`）、默认数据目录（`OmniLore_Data`）、SQL 角色与文件名、n8n 工作流文件名、
测试文件名与文档。版本号（`v0.3.0-alpha` / `0.3.0a0`）与内部代际代号（v2.3）**保持不变**。

**对外一致性红线**：仓库内不得再出现旧名字面量（`CONTEXTHUB_` / `ContextHub` /
`contexthub`）；CHANGELOG 中保留本条过去式叙述以说明更名缘由，不得删除。

---

## 6. D6 — 多存储后端：方向认可，冻结到首发后

**决定**（2026-10-24，用户「大漠」拍板）：认可「多存储后端可切换」为产品方向
—— **A 本地 Git / B GitHub 远程+薄层 / C 混合 / D PostgreSQL RLS / E 内存仿真**
五后端最终统一在同一套 API 下由使用者按场景选用。但**冻结到首发窗口之后**。
当前只有 E 被 CLI / MCP 业务运行时实际使用；D 已有 SQL、参数化仓储层与独立真实 PG 探针，
但尚未接入应用入口，不能把「仓储层文件存在」写成「PostgreSQL 运行模式已可用」。
**8 周首发窗口内对本方向零动作**——不进需求池的 P0/P1、不改 README 能力口径、
不动架构、不写适配层代码。

**理由**

1. **产能算术**：首发窗口的全部产能已承诺给三个产品目标（首启可达性 / 诚实可信度 /
   社区可参与性），P0 仅剩约 1~2 人日余量；多后端 MVP（契约对齐 + Git 后端 + GitHub 薄层）
   是数人周级工程，混入即挤占 P0。
2. **叙事稀释风险**：A/B 后端只有仓库级权限隔离。若首发期对外讲「多后端」，
   `docs/FEATURE_STATUS.md` 必须增加后端维度，否则「命令级 RLS」宣称对 git 后端用户
   构成虚标——违反项目头号红线。
3. **零 Key 卖点张力**：B 后端需要 GitHub token，与当前「零 API Key 可跑」
   （`Tested-real`）存在叙事冲突；启用前 `docs/THREAT_MODEL.md` 必须补 token 一节
   （scope 最小化、泄露面、撤销）。

**首发后重启的前置条件（缺一不可）**

1. `docs/FEATURE_STATUS.md` 增加「后端维度」，能力宣称按后端分列；
2. `docs/THREAT_MODEL.md` 补 GitHub token 安全边界；
3. 与「版本撤销/因果指针」增强**合并评审**——后端契约的
   `version_history` / `revert_version` 必须为演化链模型预留，不得冻结当前扁平版本模型；
4. 从 MVP 第一步（契约对齐 E 内存 / D PostgreSQL 双引擎）开始，不跳级；
5. 重启时重新评估「跨后端迁移」是否作为对外差异点（CAS 内容寻址使迁移理论无损）。

**首发窗口内唯一被允许的相关动作**：无。README 不加 `Planned` 口径——
alpha 首发叙事聚焦治理差异化，多后端故事会稀释定位；首发后再讲。

---

## 7. 派生约定

### 7.1 跨平台文件写入：一律 LF

所有 markdown / 控制台 / manifest 的落盘**必须**走
`omnilore.textio.write_text_canonical`，读取必须走 `read_text_canonical`。
原因：`Path.write_text()` 未传 `newline=""` 时会在 Windows 把 `\n` 写成 `\r\n`，
导致磁盘字节与计算 `documents.file_hash` 的内存字节不一致，进而使
`OperationsManager.reconcile_consistency` 恒为 `is_consistent=False`
（见 CHANGELOG 的 A-3 条目与 CI 的 `windows-latest` 门禁）。

### 7.2 零 API Key 降级

未配置 `DASHSCOPE_API_KEY` 时，检索自动降级为纯 BM25 / 关键词检索，
`ModelAdapter.retrieval_mode` 返回 `keyword_only`，`degraded_reason` 返回非空说明，
并由 `search_knowledge` 结果透传给调用方。降级**不允许**以异常形式失败。

### 7.3 Windows 宿主能力限制

符号链接相关断言在无法创建 symlink 的宿主上**显式 skip** 并给出可复核的原因，
这是宿主能力限制而非平台缺陷（见 `tests/_platform_support.py`）。
禁止使用静默 `try/except` 掩盖真实缺陷。
