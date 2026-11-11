# 核心概念

## 一条知识的生命周期

```
原始文件 (bytes)
   │  SHA-256 → CAS Asset（不可变，内容寻址）
   ▼
IngestionTask（幂等键 = 用户 + 文件哈希 + 路由 + 操作类型）
   │  解析 / 路由 / 可选人工审核
   ▼
Wiki 知识页 (Markdown + frontmatter)
   │  切块 → document_chunks（带 doc_id / page_number / heading_path 锚点）
   ▼
检索结果 → 答案合成 → 引用巡检报告
```

**CAS Asset**：原始字节只写一次，之后按哈希引用。改一个字节就会得到不同的哈希，
证据包复核因此能发现篡改。文件系统与数据库不在同一事务里，所以项目用 Outbox
补偿 + `reconcile_consistency` 把不一致**显式暴露**，不假装原子提交。

## 空间、类别与 RLS

- `space_id` 是隔离边界；`category` 是空间内的分类（默认四类：`01_Work`、
  `02_Life`、`03_Family`、`04_Health_Archive`）。
- 权限判定是「角色策略 ∩ 成员授权 ∩ 空间允许类别」的交集。
- `sql/001_omnilore_schema_and_rls.sql` 定义 15 张表与 21 条 `CREATE POLICY`。
  真实 PostgreSQL 上**只有 3 条探针覆盖的范围**有 CI 证据（`integration-rls` job，
  连续绿灯）；其余维度一律 `Simulated`。详见 [`docs/FEATURE_STATUS.md`](../FEATURE_STATUS.md)。
- `doc_id` 是身份锚点，永不放开；`series_id` 串起同一文档的所有版本。

## 版本与因果演化

每个不可变版本在 `documents` 表占一行，`document_series` 记录 `current_doc_id`。
`SUPERSEDES` 关系表达「新版本取代旧版本」。`REVERT_VERSION` 把当前版本标为
`REVERTED`、把目标版本恢复为 `PUBLISHED_CURRENT`，并写审计记录。

> **诚实边界**：`REVERTED` 是一个**扁平状态标记**，引擎没有「被哪一版撤销」的指针。
> 因果 DAG 不会伪造这条边；被撤销的版本在谱图里标注状态与「已回退，当前版本 X」。
> `get_document_asset(asset_type="EVOLUTION_DAG")` 返回 `nodes` / `ascii_graph` /
> `root_version_key` / `latest_version_key`。

## 两条正交的降级轴

| 轴 | 字段 | 含义 |
|---|---|---|
| 检索 | `retrieval_mode`、`degraded_reason` | `hybrid` 还是 `keyword_only`，以及为何降级 |
| 答案合成 | `synthesis_degraded`、`synthesis_note` | 合成是否用了兜底模型/离线答案 |

两者**不合并**：合并会让「hybrid + 检索降级非空」变成读不懂的组合。

## Token 预算与按需折叠

`search_knowledge` 默认按命中条数均分 `token_budget`。超出配额时按
「先折叠长代码块（保留首行与行数）→ 再截断长正文」降级，但**始终保留**
`doc_id` / `chunk_id` / `page_number` / `heading_path` 锚点。
需要无损原文时，用 `get_asset_chunk` 或 `get_document_asset(asset_type="CHUNK")`
二级下钻。预算分配明细随 `budget_governance` 返回。

## 引用巡检（默认开启）

答案合成后，`citation_guard` 会抽取结构化引用 / `[ref: ...]` / 引句（完全没有标注时
按句兜底），逐条与**本次检索的未折叠证据块**比对，输出
`citation_verifications`、`citation_grounding` 与 `unverified_citations_detected`。
三态为 `VERIFIED` / `PARTIALLY_VERIFIED` / `UNVERIFIED_OR_MISQUOTED`。
巡检只**新增报告字段**，不改写答案、不改写 `has_sufficient_evidence`、不改写降级轴；
可用 `verify_citations=False` 关闭。
