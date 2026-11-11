# 治理动作与 CLI

治理能力有两条入口：MCP 的 `review_and_fix_knowledge`（9 类动作）和 CLI 子命令。
两者共用同一个 `OmniLoreMCPGateway`，权限判定一致。

## CLI 命令一览

| 命令 | 作用 |
|---|---|
| `omnilore bootstrap` | 幂等首启，签发 owner token，打印 MCP 配置 |
| `omnilore demo` | 60 秒零 Key 冒烟（收录 + 检索） |
| `omnilore demo --full` | 完整治理链路（双版本 / 撤销 / 重分类 / 复核） |
| `omnilore version` | 打印外部与内部版本号 |
| `omnilore audit-bundle` | 导出可独立复核的证据包 |
| `omnilore plan <file>` | MRP 变更预审（风险评级、受影响小节、diff） |
| `omnilore history <doc_id>` | 打印版本因果拓扑谱图 |
| `omnilore verify-citation --doc-id … --text …` | 引用真伪三态核验 |
| `omnilore telemetry` | 查看 / 开关 opt-in 匿名遥测 |

除 `version` 外都支持 `--data-dir`、`--space-id`、`--token`；多数支持 `--json`。

## 完整治理 Demo 的五步

```
[1/5] 收录 v1.0（固定 version_key）
[2/5] 收录 v2.0（同一版本链，构造 SUPERSEDES）
[3/5] REVERT_VERSION：把 v1.0 恢复为 current，v2.0 标 REVERTED
[4/5] RECLASSIFY：把恢复后的文档迁到另一个合法 category
[5/5] 复核：EVOLUTION_DAG 拓扑 + 一次全新检索命中当前版本
```

每一步都断言真实状态转换：v2 必须生成新 `doc_id`、DAG 节点数必须 ≥2、
`current_doc_id` 必须等于 v1、迁后的 `category` 必须生效。任何一步不符即抛
`DEMO_INTEGRITY_FAILED` 并以非零码退出 —— 不输出「看起来正常」的报告。

这条链路是 G2 门禁（打 tag 前手动跑 demo 并存档输出）与第 0 波封闭验证的
可复跑素材。它不是自动化 CI 证据的替代品，四态口径仍以状态矩阵为准。

## 9 类受控动作

| 动作 | 需要的关键 payload |
|---|---|
| `APPROVE_REVIEW` | `version_key` / `version_label`（把 `NEEDS_REVIEW` 任务提交为版本） |
| `RECLASSIFY` | `new_space_id` + `new_category`（同时校验源与目标的写权限） |
| `SPLIT_DOCUMENT` | `splits`（至少 2 个定义） |
| `FIX_EXTRACTED_FIELD` | 待修正字段 |
| `REVERT_VERSION` | `expected_current_doc_id` + `rollback_to_doc_id`（CAS 式并发保护） |
| `PROMOTE_COPY` | `dest_space_id` / `dest_category` |
| `CANCEL_TASK` | — |
| `INSPECT_PLAN` | `proposed_markdown` |
| `VERIFY_CITATION` | `citation_text`（可附 `verbatim_quote` / `asset_type`） |

所有动作都在事务内写审计记录。`REVERT_VERSION` 若发现
`series.current_doc_id` 已被并发改动，抛 `VersionConflictError` 而不是覆盖别人的提交。

## 退出码语义

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 命令/运行时错误，或证据包导出失败 |
| 3 | 「已验证但结论为否」—— `audit-bundle` 出现哈希不一致，或 `verify-citation` 判定引用未证实 |

把 3 与 1 分开，脚本才能区分「内容被动过」和「命令写错了」。
`verify-citation` 在文本与 `--json` 两种模式下对未证实引用**都**返回 3。

## 跨进程边界

默认内存引擎每个 CLI 进程都是全新实例。`plan` / `history` / `verify-citation`
会用 `load_runtime_index` 把 `documents` / `document_series` / `assets` /
`document_assets` 回填进 `db._state`，但**只在内存引擎且 `documents` 为空时**生效，
PG 后端与同进程已有数据不覆盖。索引只是派生物、只负责定位，正文始终由
`storage.read_wiki_by_record` 从磁盘真实字节读取。

> 边界：跨进程回填没有接入 CI 的真实子进程自动化用例，状态标 `Implemented`
> 而非 `Tested-real`。
