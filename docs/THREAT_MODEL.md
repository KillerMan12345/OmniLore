# 威胁模型 THREAT_MODEL

> **我们公开边界，因为我们做安全。**
> 本文回答三个问题：**信任边界在哪**、**能防什么**、**防不住什么**。
> 与 [`docs/FEATURE_STATUS.md`](docs/FEATURE_STATUS.md)（能力—证据对照）配合阅读。

对外版本：**`v0.3.0-alpha`**（预发布，仅供评估、研究与贡献）。
未宣称任何合规认证（SOC2 / ISO / 等保），措辞严格限定为「技术可追溯」。

---

## 1. 资产与信任边界

| # | 资产 | 说明 |
|---|---|---|
| A1 | 原始证据（CAS 资产） | SHA-256 内容寻址的不可变字节，落 `$OMNILORE_DATA_DIR/ops/cas/` |
| A2 | Markdown 知识页 | 人类可审阅的 Obsidian 页面，落 `02_Vault/spaces/<space_id>/wiki/` |
| A3 | 结构化状态 | 文档/任务/版本/成员/token 元数据，落在数据库（默认内存引擎，容器模式为 PostgreSQL） |
| A4 | 凭据 | Owner token、成员 token、`ops/bootstrap.json` |

信任边界共四道：

```text
[ 调用方：MCP 客户端 / CLI / n8n ]
        │  ① 入口边界：token 必须存在、未过期、未被撤销
        ▼
[ 授权层：四维权限交集 ]
        │  ② 权限边界：Token ∩ Membership ∩ 空间继承 ∩ Profile/Category
        ▼
[ 引擎层：命令级事务（默认内存仿真引擎 / 容器模式 PostgreSQL + RLS） ]
        │  ③ 数据边界：越权行在命令层被过滤，而不是在应用层「查出来再丢掉」
        ▼
[ 文件网关：SecureVaultStorage ]
        │  ④ 文件系统边界：绝对路径 / `..` / 符号链接一律拒绝
        ▼
[ 磁盘：CAS + Vault ]
```

**边界之外的东西，我们一律声明防不住**（见第 3 节）。

---

## 2. 能防什么（在本仓库有自动化证据的部分）

| 威胁 | 防御 | 证据状态 |
|---|---|---|
| 跨空间 / 跨 Profile 越权读取 | 四维权限交集，命令级按 `(space_id, category)` 过滤 | `Simulated`（内存仿真引擎） |
| 路径穿越读取 vault 外文件 | `SecureVaultStorage._verify_safe_subpath`：拒绝绝对路径、`..`/`.`、符号链接 | `Tested-real`（真实文件系统 + 三平台） |
| 符号链接把 vault 指向外部目录 | 逐级 `islink` 检查，并逐段拒绝链接链 | `Tested-real` |
| token 撤销后仍能访问 | 进程内缓存主动失效（实测 `< 1ms`），下次请求即生效 | `Simulated` |
| AI 提取的健康指标被当成事实 | 默认 `NEEDS_CHECK` + 显式提示，须人工 `FIX_EXTRACTED_FIELD` 才转 `HUMAN_VERIFIED` | `Simulated` |
| 跨空间晋升时敏感信息外泄 | 晋升前扫描并脱敏身份证号、手机号、银行卡号、`sk-`/`tok-`/`ak-` | `Implemented`（纯函数 + 用例） |
| 静默把降级伪装成正常 | `retrieval_mode` / `degraded_reason` / `synthesis_degraded` / `synthesis_note` 正交上报 | `Tested-real` |
| Windows CRLF 破坏 SHA-256 对账 | 全部文本读写统一 UTF-8 + LF（`omnilore.textio`） | `Tested-real`（`windows-latest` 门禁） |

> **`Simulated` 的含义**：证据来自与 PostgreSQL RLS **同构的内存仿真引擎**，
> 真实 PostgreSQL 上没有 CI 证据。详见 [`docs/FEATURE_STATUS.md`](docs/FEATURE_STATUS.md)。
>
> **唯一例外**：「真实 PostgreSQL RLS 策略与跨空间读拦截」是 `Tested-real` ——
> 依据 2026-10-24 在真实 PostgreSQL 16.15 上的一次**人工**验证（3 条探针 PASS）。
> 它是**一次性证据，不是 CI 门禁**；完整输出与「它没有证明什么」见
> [`docs/FEATURE_STATUS.md` 第 6 节](FEATURE_STATUS.md)。

---

## 3. 防不住什么（诚实披露）

1. **文件系统与数据库之间不承诺跨系统事务原子性。**
   一次入库会同时改动磁盘（CAS 字节、Markdown 知识页）与数据库（结构化状态）。
   二者**不在同一个事务里**：崩溃可能留下「磁盘已写、数据库未提交」或反之的中间态。
   我们做了**补偿**而非**原子**：Outbox + `run_recovery_worker` 重放，以及
   `OperationsManager.reconcile_consistency` 做磁盘与数据库对账，
   把不一致**显式报出来**让你看见。**我们不会假装这是一次原子提交。**

2. **默认数据库是内存引擎，不提供持久化与并发隔离语义。**
   进程退出后结构化状态清空；它没有真实 MVCC、锁与事务隔离级别。
   需要真实隔离语义请走容器模式（PostgreSQL），并自行应用 `sql/001` + `sql/002`。

3. **真实 PostgreSQL RLS 仍无 CI 证据，只有一次性人工验证。**
   2026-10-24 在真实 PostgreSQL 16.15 上人工跑通三条探针（7 张核心表 `rowsecurity=true` /
   未授权时跨空间行不可见 / 授权后同一行可见），证明策略在真实 PG 上确实生效。
   但请注意它的边界：**没有**自动化测试守护（改坏了 CI 不会红）；探针**未覆盖**
   profile 维度、写入侧（`INSERT`/`UPDATE`/`DELETE`）策略、组织树继承与撤销链路。
   证据记录与「它没有证明什么」见 [`docs/FEATURE_STATUS.md` 第 6 节](FEATURE_STATUS.md)。
   **不要**把它读成「RLS 已被持续验证」。

4. **不防本机恶意软件 / 有本机文件权限的攻击者。**
   token 以明文持久化在 `$OMNILORE_DATA_DIR/ops/bootstrap.json`；
   我们在 Windows 上做了 ACL 收紧（仅本账户可读写），但这挡不住已拿到你账户权限的攻击者。

5. **不防 MCP 客户端本身。**
   token 交给客户端后，客户端可以在授权范围内做任何事 —— token 就是能力，
   每个工具首参均为 token，泄露即等于授权。请按最小权限签发。

6. **脱敏是规则匹配，不是分类分级引擎。**
   规则覆盖身份证号、手机号、银行卡号与常见密钥前缀；**新型或变形的敏感串会漏**。
   脱敏预览需要人工看一眼再放行。

7. **向量召回依赖外部供应商时，Prompt 侧的内容出境由你的配置决定。**
   配置 `DASHSCOPE_API_KEY` 后，检索与合成会把内容发给外部模型服务。
   未配置时（默认）全程本地，不走网络。

8. **未做审计日志的防篡改存储。**
   审计记录用于追溯与对账，不做 tamper-evident 链式哈希，也不做 WORM 存储。

9. **未做多租户隔离的强度证明。**
   组织树继承（`ORG_PUBLIC` 只读继承、`ORG_RESTRICTED` 不继承）在内存引擎上验证，
   未经真实 PG 与真实并发压力验证。

---

## 4. 已知取舍（为什么这么做）

| 取舍 | 理由 |
|---|---|
| 用补偿（Outbox + 对账）而不是跨系统 2PC | 2PC 需要额外的协调者与恢复协议，复杂度与运维成本远超 alpha 阶段的收益；**把不一致显式暴露**比假装原子更诚实 |
| 受保护 frontmatter 字段默认存在、但可关闭 | 可审计性需要系统字段，但笔记主权在用户手里；冲突在于「我们写字段」与「你的笔记」，解法是**预先声明 + 可关闭**，而不是硬拒绝 |
| 冲突副本落隐藏目录 | `.conflict_r*.md` 散落在 vault 会污染 Obsidian 用户的文件树；隐藏目录既保留证据又不打扰 |
| 错误不伪装成供应商故障 | 内部 bug 记 stderr 并如实归因；把 bug 说成「供应商挂了」会毁掉可审计性 |

---

## 5. 报告漏洞

**不要开 Issue。** 走 GitHub Security Advisory：
<https://github.com/KillerMan12345/OmniLore/security/advisories>
流程与响应时限见 [`SECURITY.md`](../SECURITY.md)。
