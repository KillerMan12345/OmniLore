# 证据与完整性

## 三层可核对性

1. **字节层**：原始文件按 SHA-256 内容寻址写入 CAS，改 1 字节就会得到不同哈希。
2. **记录层**：`documents.file_hash` 记录知识页的规范字节哈希；所有读写统一 UTF-8 + LF，
   否则按字节算的对账会恒失败（`.gitattributes` 钉死行尾，并有 AST/LF 守卫）。
3. **报告层**：`audit-bundle` 导出 `audit-bundle.json` + `audit-bundle.verify.md`，
   每条检查都**重新打开磁盘字节重算哈希**，绝不把库里的值抄一遍。

```bash
omnilore audit-bundle --out ./out
make verify-audit
```

证据包出现 `mismatch` / `missing` / `unreadable` 时以退出码 **3** 结束（「已验证但
结论为否」），而不是打印一个看起来正常的包。

## 防幻觉引用校验

`omnilore/citation.py` 做多语言归一化（剥除标点与空格）后用滑动窗口 + 字符
bi-gram 交并比，把一段陈述与 CAS 原文 / Wiki 正文比对，输出三态：

| 状态 | 判定 |
|---|---|
| `VERIFIED` | 高度重合，可在原文中找到依据 |
| `PARTIALLY_VERIFIED` | 部分重合，需要人工复核 |
| `UNVERIFIED_OR_MISQUOTED` | 缺乏依据，可能是捏造或张冠李戴 |

报告字段：`verification_status` / `similarity_score` / `matched_snippet` /
`is_grounded` / `line_number` / `analysis`（经网关后另有 `status` /
`overlap_score` / `best_snippet` 别名）。

两条使用路径：

- **主动**：`omnilore verify-citation --doc-id <id> --text "…"`，或 MCP
  `VERIFY_CITATION` 动作。
- **默认自动**：`search_knowledge` 的 `citation_guard` 在答案合成后自动抽取引用片段
  并逐条核验，输出 `citation_verifications` 与 `unverified_citations_detected`
  （`Implemented`，离线验证）。它能识别捏造引用与「声明的 doc_id 与最佳匹配不符」。

> 边界：这是**离线文本相似度**核验，不是外部事实核查，也不连接任何搜索服务。
> 相似度阈值是启发式的，不能当作法律/医疗等场景的事实背书。

## 变更预审（MRP）

`INSPECT_PLAN` 对提议的 Markdown 生成 `unified_diff`、受影响小节
（`impacted_headings`，对外别名 `sections_affected`）、增删行数与
`LOW` / `MEDIUM` / `HIGH` 风险评级。比对前会剥除 WIKI 系统 frontmatter，
避免把「删掉 frontmatter」误判为高风险。目标文档不存在时失败关闭。

## 审计日志与因果链

每次治理动作都在事务内写审计记录（动作名、目标、前后状态）。
`EVOLUTION_DAG` 资产把 `documents` + `document_series` + `SUPERSEDES` 关系
渲染成 ASCII 谱图。

> **已知缺口（不伪造）**：`REVERTED` 是扁平状态标记，没有「被哪一版撤销」的指针；
> 跨进程时 `document_relations` 不落运行时索引，因果边退而从 `doc_status` 推断；
> `diff_summary` 因底层未持久化逐版本 diff 而恒为 `+0/-0`。这些边界在
> [`docs/FEATURE_STATUS.md`](../FEATURE_STATUS.md) 如实标注。

## 诚实标注的四态

| 状态 | 含义 |
|---|---|
| `Implemented` | 代码已实现，证据为离线用例或静态检查 |
| `Tested-real` | 有真实环境证据（CI 类条目需连续 ≥2 次绿灯） |
| `Simulated` | 只在内存仿真引擎/桩上验证过 |
| `Planned` | 决策已接受但尚未实现 |

任何「CI 已验证」的表述都必须同时说明覆盖了什么、没覆盖什么。
