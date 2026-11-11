# 架构与边界

## 分层

```
Agent / MCP 客户端
      │  MCP stdio (JSON-RPC, 7 tools, query/governance tiers)
      ▼
OmniLoreMCPGateway  ── 权限解析、动作白名单、参数映射
      ▼
OmniLoreEngine      ── 入库 Saga、检索、合成、审计、治理动作
      ├── parsers / context_budget / citation / citation_guard / evolution_chain
      ├── storage (CAS 原始资产 + Vault Markdown)
      ├── embedding_provider (DashScope / OpenAI 兼容 / 显式停用 / 本地兜底)
      └── db: RLSDatabaseEngine（内存仿真，默认）｜安全 PG 连接层（可选，未承载业务读写）
```

`omnilore/storage_contract.py` 定义最小存储后端契约（`StorageBackend` Protocol +
`StorageCapabilities` 能力自省）与 `EngineStorageAdapter`，用于对齐内存引擎与 PG 引擎；
**本版本没有切换到多后端运行时**。

## 关键设计取舍

| 取舍 | 为什么 |
|---|---|
| 所有文本 UTF-8 + LF | SHA-256 按字节对账，CRLF 会让对账恒失败 |
| 降级原因拆两个正交字段 | 检索降级与合成降级混在一起会读不懂 |
| 证据包不信任索引哈希 | 索引是派生物，被改就会背书假数据 |
| 连接 PG 拒绝超级用户 | 超用户 / `BYPASSRLS` 会绕过 RLS，宁可拒绝启动 |
| 向量维度不一致直接报错 | 静默截断会产生「错误但看起来正常」的相似度 |
| `doc_id` 永不放开 | 身份锚点，属正确性问题 |
| 新能力挂既有工具参数 | MCP 工具口径恒 7 个，下游客户端不用跟着升级 |
| 守卫必须做变异验证 | 空转守卫比没有守卫更危险 |

## 真实边界（务必如实传达）

1. **业务数据层是内存仿真引擎**。进程退出后结构化状态清空；磁盘上的 Vault
   Markdown 与 CAS 资产仍在，但结构化记录需要重建。不能宣称持久化或多人共享。
2. **PostgreSQL 证据范围有限**。`sql/001` 的 21 条策略里，只有 3 条探针覆盖的
   space + category 维度有真实 PG 的 CI 证据（`integration-rls` job）。它证明的是
   **SQL 策略生效**，不是业务运行时已接 PG。
3. **混合检索的 `hybrid` 向量路径在 CI 里是 monkeypatch 桩**，整体标 `Simulated`；
   `keyword_only` 路径真实执行。
4. **文件系统与数据库不承诺跨系统事务原子性**，用 Outbox 补偿 + 一致性对账
   显式暴露不一致。详见 [`docs/THREAT_MODEL.md`](../THREAT_MODEL.md)。
5. **匿名遥测本版本无网络外发**。只实现本地 opt-in 计数与 `--show` 自查，
   `transport.implemented` 恒为 `false`。
6. **未宣称任何合规认证**（SOC2 / ISO / 等保），措辞限定为「技术可追溯」。

## 验证版图

| 类型 | 入口 |
|---|---|
| 全量离线测试 | `make test` |
| 静态检查 | `make lint` |
| 诚实口径守卫 | `make verify-honesty` |
| LF / 路径 / MCP / 降级等专项 | `make help` 列出的 `verify-*` 目标 |
| 真实 PG RLS 探针 | CI 的 `integration-rls` job（唯一需要外部服务的门禁） |

CI 矩阵覆盖 Python 3.11 / 3.12 的 ubuntu / windows、arm64、lint、DCO 与真实 PG 探针。
CI 绿灯只覆盖离线套件、ruff、零密钥冒烟与那 3 条 RLS 探针，**不覆盖真实 PG 业务数据面**。

## 明确未做的事

- 把业务读写迁到 PostgreSQL 数据面（核心未决项）。
- `engine.py` 的架构级拆分（条件触发：贡献者规模上来之后）。
- 文档站之外的内容发布、合规认证、真实用户验证。

决策背景见 [`docs/DECISIONS.md`](../DECISIONS.md)，剩余任务见
[`docs/HANDOVER.md`](../HANDOVER.md)。
