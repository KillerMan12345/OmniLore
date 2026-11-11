# MCP 工具与分层治理

OmniLore 通过 **stdio JSON-RPC** 提供 MCP 服务。`tools/list` 默认返回全部 **7 个工具**
（口径固定，不随能力增加而增加）；可传可选 `tier` 过滤，完全向后兼容。

| 工具 | `tier` | 作用 |
|---|---|---|
| `search_knowledge` | `query` | 混合检索 + 答案合成 + Token 预算治理 + 引用巡检 |
| `get_document_asset` | `query` | 取 WIKI 正文 / RAW_ASSET 原始资产 / CHUNK 未折叠块 / EVOLUTION_DAG 因果拓扑 |
| `query_health_trends` | `query` | 只读健康指标趋势 |
| `ingest_document_or_file` | `governance` | 收录文档或文件 |
| `commit_memory_note` | `governance` | 提交记忆笔记 |
| `get_ingestion_task` | `governance` | 查询入库任务状态 |
| `review_and_fix_knowledge` | `governance` | 执行受控治理动作（9 类，见下） |

`query` = 3 个只读消费工具，`governance` = 4 个写入/审计工具。
新能力优先**挂到既有工具的参数或动作上**，而不是新增工具 —— 这样下游客户端
不需要跟着升级工具清单。

## `search_knowledge` 的关键参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `token_budget` | 1500 | 返回分块的总 Token 预算；`0` 或负数为不限 |
| `verify_citations` | `true` | 是否对合成答案做自动引用巡检 |
| `include_history` | `false` | 是否包含历史版本 |
| `space_ids` / `categories` | 无 | 检索范围过滤 |
| `code_symbol` | 无 | 代码符号过滤 |

响应中值得关注的字段：`results`、`retrieval_mode`、`degraded_reason`、
`synthesis_degraded`、`synthesis_note`、`budget_governance`、
`citation_verifications`、`citation_grounding`、`unverified_citations_detected`。

> 引用巡检在预算折叠**之前**执行，避免折叠后的文本造成假的 `UNVERIFIED` 判定。

## `get_document_asset` 的四种资产

| `asset_type` | 返回 |
|---|---|
| `WIKI` | 完整知识页 Markdown（默认） |
| `RAW_ASSET` | 不可变原始资产的字节定位 |
| `CHUNK` | 未折叠的原文分块（配合 `chunk_id`） |
| `EVOLUTION_DAG` | 版本因果拓扑：`nodes` / `ascii_graph` / 根与最新版本键 |

所有分支都经过 RLS `select_rows` 与显式 `scope.can_read`，客户端裸路径一律拒绝。

## `review_and_fix_knowledge` 的 9 类动作

`APPROVE_REVIEW`、`RECLASSIFY`、`SPLIT_DOCUMENT`、`FIX_EXTRACTED_FIELD`、
`REVERT_VERSION`、`PROMOTE_COPY`、`CANCEL_TASK`、`INSPECT_PLAN`、`VERIFY_CITATION`。

- `INSPECT_PLAN`：对 `proposed_markdown` 生成 `unified_diff`、`risk_level`
  （`LOW` / `MEDIUM` / `HIGH`）、`sections_affected` 与合并建议；目标文档不存在时
  **失败关闭**，不会拿空串当基线伪造计划。
- `VERIFY_CITATION`：对 `citation_text` 做三态事实核验；payload 的字段名在网关内
  映射为 `claim_text`，响应同时给出真实字段与 `status` / `overlap_score` 别名。

动作枚举由 `ReviewAction` 动态生成，新增动作不会出现「schema 里有但被白名单拒掉」。

## 接线注意

- 设置 `DATABASE_URL` 时，MCP 会额外建立一条**安全 PG 连接层**：连接角色必须是
  非超级用户、无 `BYPASSRLS`、是策略角色成员，否则拒绝启动；事务内 `SET LOCAL ROLE`
  并注入 RLS GUC。**这条连接尚未承载业务读写**，业务数据仍走内存引擎。
- token 由 `omnilore bootstrap` 签发并写入 `ops/bootstrap.json`；
  `OMNILORE_AUTO_BOOTSTRAP=0` 只关闭「自动创建」，**已存在的 bootstrap.json 仍会加载**。
- 任何「CI 已验证」的说法都要说明覆盖范围，见
  [`docs/FEATURE_STATUS.md`](../FEATURE_STATUS.md)。
