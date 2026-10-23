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

**红线：真实 PG RLS 目前仍然没有 CI 证据。** 例外只有一条：2026-10-24 在
**真实 PostgreSQL 16.15** 上做过一次**人工**验证（3 条探针全部 PASS，证据记录见本文
第 6 节），因此仅「真实 PostgreSQL RLS 策略与跨空间读拦截」这一条允许标 `Tested-real`
（且备注里必须写明是人工验证、自动化 CI 待接入）。其余依赖真实 PostgreSQL 的条目
（四维交集 / Profile 隔离 / 撤销失效 / 组织树继承等）一律维持 `Simulated` ——
探针没有覆盖它们，不许顺手抬高。

---

## 2. 测试跑在什么上面（请先读这一段）

本仓库的全部测试用例都跑在 `omnilore.engine.RLSDatabaseEngine` 上 —— 一个**内存仿真
引擎**。它与 PostgreSQL 的 RLS 策略**同构**（同样的 `(space_id, category)` 策略语义、
同样的命令级权限收口），但**它不是真实 PostgreSQL**：

- 它不是用 `CREATE POLICY` / `SET LOCAL ROLE` 实现的，而是用 Python 谓词实现的同构判定；
- 它没有真实的 MVCC、锁、事务隔离级别与 `pgvector` 索引行为；
- 进程退出后结构化状态清空（磁盘上的原始资产与 Markdown 知识页保留）。

所以：**「84 个测试全绿」证明的是逻辑正确性，不等于「真实 PG 上的 RLS 已由 CI 持续验证」。**
本页的例外只有一处，且是**手动、一次性**证据：2026-10-24 在真实 PostgreSQL 16.15 上
跑了 `make verify-rls-pg` 的三条探针并全部 PASS（完整输出见第 6 节）。它证明了「这套
策略在真实 PG 上确实生效」，但它**不是 CI 门禁**，也不能证明未来不回归。
这句话在 README、本页、CHANGELOG 三处口径一致（由 `make verify-honesty` 守护）。

---

## 3. 状态矩阵（与 README 逐条对应）

| 能力 | 状态 | README 位置 | 验证命令 | 备注 |
|---|---|---|---|---|
| 不可变原始证据（SHA-256 CAS） | `Implemented` | 核心能力 / 基础 | `make verify-ops` | 真实文件系统上的哈希校验与恢复演练；**不可变性无专门负向测试** |
| 四维权限交集 | `Simulated` | 核心能力 / Phase 0 | `make verify-rls` | 内存仿真引擎；真实 PG 探针只覆盖 space + category 两维（`authz.can_read_scope`），membership / profile 维度**未做探针**，故不升级 |
| Profile 硬性隔离 | `Simulated` | 核心能力 / Phase 0 | `make verify-rls` | 同上；profile 维度由引擎层判定、不由 SQL 策略表达，2026-10-24 的三条探针**未覆盖** profile 切换 |
| 撤销即时失效 | `Simulated` | 核心能力 / Phase 0 | `make verify-rls` | `< 1ms` 为进程内缓存实测，非 PG 侧实测；PG 探针不涉及撤销链路，故不升级 |
| 文件网关路径防御 | `Tested-real` | 核心能力 / Phase 0 | `make verify-paths` | 真实文件系统（symlink / junction 回退），三平台真实执行 |
| 真实 PostgreSQL RLS 策略与跨空间读拦截 | `Tested-real` | 核心能力 / Phase 0 | `make verify-rls-pg` | 依据 **2026-10-24 真实 PostgreSQL 16.15 手动验证**（3 条探针全部 PASS：7 张核心表 `rowsecurity=true`；跨空间行未授权不可见；授权后同一行可见）；**自动化集成 CI 待接入**，单次手动验证 ≠ 持续 CI 证据 |
| 多格式入库 | `Tested-real` | 核心能力 / Phase 1 | `make verify-parsers` | 真实调用 `pdfplumber` / `pypdf` / `Pillow` |
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
| 60 秒验证端到端 | `Tested-real` | 核心能力 / 其他宣称 | `make verify-launch` | CI 以真实子进程跑 `bootstrap` + `demo` |
| MCP stdio 工具暴露 | `Tested-real` | 核心能力 / 其他宣称 | `make verify-mcp` | CI 以真实管道完成 JSON-RPC 握手 |
| 零密钥降级如实上报 | `Tested-real` | 核心能力 / 其他宣称 | `make verify-degradation` | `retrieval_mode` / `degraded_reason` 正交 |
| 跨平台 LF 规范化 | `Tested-real` | 核心能力 / 其他宣称 | `make verify-lf` | `windows-latest` 真实文件系统门禁 |
| opt-in 匿名遥测 | `Planned` | 核心能力 / 尚未实现 | — | 决策已接受，实现排期 Q2 |
| 模型 provider 抽象（R-04b） | `Planned` | 核心能力 / 尚未实现 | — | `OMNILORE_*_MODEL` 当前不被代码读取 |
| n8n 工作流（3 条） | `Planned` | 核心能力 / 尚未实现 | — | JSON 模板已提供，需在真实 n8n 实例验证 |
| 真实 PostgreSQL 集成 CI | `Planned` | 核心能力 / 尚未实现 | `make verify-rls-pg` | 接通后，RLS 条目才可能升为 `Tested-real` |

**当前统计（由测试守护，与 README / CHANGELOG 三处一致）：**
`Implemented` 5 条 ｜ `Tested-real` 7 条 ｜ `Simulated` 12 条 ｜ `Planned` 4 条 —— 合计 **28 条**。

---

## 4. 验证命令怎么跑

所有 `verify-*` 目标都在 `Makefile` 里，**离线可跑**（不需要数据库、不需要 API Key）：

```bash
make verify          # 全量测试（离线）
make verify-rls      # 权限/RLS/版本/同步相关（内存仿真引擎 —— 结果只证明逻辑）
make verify-sync     # Obsidian 同步：受保护字段可配 + 冲突副本隐藏目录
make verify-honesty  # 诚实标注守卫：README ↔ 本矩阵 ↔ CHANGELOG 三处一致
```

`make verify-rls-pg` 是**唯一需要真实 PostgreSQL** 的目标：它需要 `psql` 与
`DATABASE_URL`，会真实应用 `sql/001` + `sql/002` 并执行 RLS 探针。
**环境不具备时它会以非零码退出并说明原因 —— 它不会假装验证过。**

```bash
# 必须指向一个**空库**：脚本只建表、不清理既有对象（非空库会以 exit 2 停下并给出指引）
export DATABASE_URL=postgresql://omnilore_gateway:<pwd>@localhost:5432/omnilore_verify
export OMNILORE_MIGRATOR_DSN=postgresql://omnilore_migrator:<pwd>@localhost:5432/omnilore_verify
make verify-rls-pg
```

> 上表里所有 `Simulated` 条目**都没有真实 PG 证据**（唯一例外是「真实 PostgreSQL RLS
> 策略与跨空间读拦截」那条 `Tested-real`，证据见第 6 节）。这是已知边界，不是遗漏。

---

## 5. 与威胁模型的关系

本矩阵回答「**哪条能力有哪种证据**」；
[`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md) 回答「**这套设计能防什么、防不住什么**」。
两者一起看，才能判断 OmniLore 是否适合放进你的流程。

**结论先说：这是预发布版本（alpha），请勿放入任何重要业务流程。**

---

## 6. 证据记录：真实 PostgreSQL 上的 RLS 验证（2026-10-24）

**这是本仓库唯一一条真实 PostgreSQL 证据。请按它的实际强度使用它。**

| 项 | 值 |
|---|---|
| 日期 | 2026-10-24 |
| 数据库 | 真实 **PostgreSQL 16.15**（官方 `pgvector/pgvector` 镜像，tag `0.8.0-pg16`） |
| 客户端 | `psql` 16.15 |
| 命令 | `make verify-rls-pg`（脚本 `tools/verify_rls_pg.py`，目标库为**空库** `omnilore_verify`） |
| 退出码 | `0`（三条探针全部 PASS） |
| 性质 | **人工执行、一次性**；不是 CI 门禁，**不可**视为「持续证据」 |

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

三条探针各自证明了什么（以及**没有**证明什么）：

| 探针 | 证明了 | 没有证明 |
|---|---|---|
| P1 策略已挂载 | 7 张核心表在真实 PG 上 `rowsecurity = true`，策略真的被 `CREATE POLICY` 建出来了 | 策略语义是否正确（挂载 ≠ 生效） |
| P2 越权读被过滤 | 以 `omnilore_gateway` 角色、仅授权 `sp_bootstrap_personal` 时，`sp_probe_hidden` 的行**不可见** | 其它维度（profile / 成员关系变动 / 组织树继承）也被正确过滤 |
| P3 授权后可见 | 把该空间加入授权后同一行**变为可见** —— 与 P2 成对，排除「表里本来没数据」的假阳性 | 写入侧策略（`INSERT` / `UPDATE` / `DELETE`）的行级正确性 |

因此升级范围严格限定为「**策略挂载 + 跨空间读拦截**」这一条。
`四维权限交集`、`Profile 硬性隔离`、`撤销即时失效`、`组织树只读继承` 等条目**没有**被
这三条探针覆盖，维持 `Simulated`。

复现提示（踩过的两个坑，已写进脚本与 compose）：
1. 镜像 tag 必须是 `<pgvector版本>-pg<PG主版本>`（如 `0.8.0-pg16`）；写成 `pg16-v0.8.0` 会 pull 失败。
2. 目标库必须是**空库**；非空时脚本以 `exit 2` 停下并给出「新建 `omnilore_verify`」的指引，
   **不会**自动 `DROP SCHEMA` 清库（那可能删掉真实数据）。
