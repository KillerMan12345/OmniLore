# OmniLore 文档站

> `v0.3.0-alpha` — 预发布版本，仅供评估、研究与贡献使用。每条能力的真实状态以
> [`docs/FEATURE_STATUS.md`](../FEATURE_STATUS.md) 的四态矩阵为准，本页不替代它。

OmniLore 是 **AI 原生可审计知识中枢**：用不可变的 SHA-256 CAS 原始证据作底座，
用 Obsidian 风格 Markdown 知识页作人类编辑界面，用 MCP stdio 向各类 Agent 提供
检索与治理能力。它做的是 RAGFlow / Dify / Obsidian 的**治理层**，不是替代品。

## 先读哪一张

| 你的目标 | 从这里开始 |
|---|---|
| 5 分钟内跑起来 | [快速上手](quickstart.md) |
| 理解证据链、空间、版本这些概念 | [核心概念](concepts.md) |
| 把 OmniLore 接到 Agent / MCP 客户端 | [MCP 工具与分层治理](mcp-tools.md) |
| 审核、重分类、撤销版本、预审变更 | [治理动作与 CLI](governance.md) |
| 核对引用真伪、导出证据包 | [证据与完整性](evidence-integrity.md) |
| 判断它适合什么、不适合什么 | [架构与边界](architecture.md) |
| 提交第一个 PR | [贡献指南](contributing.md) |

## 三条必须记住的边界

1. **业务数据层当前是内存仿真引擎**（`RLSDatabaseEngine`），进程退出后结构化状态清空。
   真实 PostgreSQL 的 RLS 策略有 CI 探针证据，但**不等于业务读写已接入 PG**。
2. **混合检索的向量路径在 CI 里是桩**，四态标 `Simulated`；零 Key 时会降级为关键词
   （BM25）检索，降级原因通过 `retrieval_mode` / `degraded_reason` 正交上报。
3. **本版本没有网络遥测外发**。opt-in 匿名遥测只实现本地匿名计数与 `--show` 自查，
   默认关闭，`OMNILORE_TELEMETRY=0` 可全局关闭。

## 状态与决策

- 能力四态与可复跑命令：[`docs/FEATURE_STATUS.md`](../FEATURE_STATUS.md)
- 公开进度与门禁：[`docs/ROADMAP_STATUS.md`](../ROADMAP_STATUS.md)
- 六项拍板决策：[`docs/DECISIONS.md`](../DECISIONS.md)
- 安全边界与「防不住什么」：[`docs/THREAT_MODEL.md`](../THREAT_MODEL.md)
- 接手继续开发：[`docs/HANDOVER.md`](../HANDOVER.md)
- 版本变更：[`CHANGELOG.md`](../../CHANGELOG.md)
- English overview: [`README.en.md`](../../README.en.md)
