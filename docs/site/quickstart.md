# 快速上手（零 API Key）

前置：Python 3.11+。全程不需要云 API Key、不需要 Docker、不需要 PostgreSQL。

```bash
pip install -e .
omnilore bootstrap
omnilore demo
```

`bootstrap` 幂等：创建数据目录、默认空间、owner token，并打印 MCP 客户端配置片段。
`demo` 自动 bootstrap → 收录一份示例 Markdown → 用关键词检索把它找回来，成功时
输出 `retrieval_mode=keyword_only`（未配置向量源时的确定性降级，不是故障）。

## 看清楚每一步在做什么

```bash
omnilore version                    # 外部版本 v0.3.0-alpha / 内部 PEP 440
omnilore demo --json                # 机器可读结论（含 found_demo_document）
omnilore demo --full                # 完整治理链路，见下
omnilore audit-bundle --out ./out   # 导出可独立复核的证据包
```

**完整治理 Demo**（`Implemented`，离线验证）会把两个版本提交到同一版本链，
执行 `REVERT_VERSION` 回退、执行 `RECLASSIFY` 迁移分类，再用因果 DAG 与一次
全新检索复核结果。任一步状态不符即失败退出，不打印「看起来成功」的摘要。

```bash
omnilore demo --full --json
```

## 数据在哪里

默认 `./OmniLore_Data`；可用 `--data-dir` 或 `OMNILORE_DATA_DIR` 指定。
人类可读的知识页在 `02_Vault/spaces/<space_id>/wiki/`，frontmatter 承载系统字段
（`doc_id` / `series_id` / `version_key` 等）。`ops/index/` 是运行时派生的定位索引，
**不是权威数据**：证据包校验会重新打开磁盘字节计算 SHA-256，不信任索引里的哈希。

## 三个常用治理命令

```bash
omnilore plan ./draft.md                                   # 变更预审（风险评级 / 受影响小节）
omnilore history <doc_id>                                  # 版本因果拓扑
omnilore verify-citation --doc-id <doc_id> --text "……"    # 引用真伪三态判定
```

`verify-citation` 对 `UNVERIFIED_OR_MISQUOTED` 的结果以退出码 3 失败关闭，
文本与 `--json` 两种模式一致。

## 自检与开关

```bash
omnilore telemetry            # 查看 opt-in 匿名遥测状态（默认关闭）
omnilore telemetry --show     # 打印完整原始 payload 供自查
make test                     # 全量离线测试
make lint                     # ruff 静态检查
```

## 接 MCP 客户端

`omnilore bootstrap` 会打印一段可直接粘贴的配置（stdio 传输）。
MCP 工具口径为 **7 个**，按 `tier` 分为 `query`（3 个只读）与 `governance`（4 个写入/审计）。
详见 [MCP 工具与分层治理](mcp-tools.md)。

> 边界提醒：默认内存引擎的**结构化状态不跨进程持久化**。跨进程读文档时，
> CLI 会用 `ops/index/` 回填定位信息，正文始终从磁盘真实字节读取；这不等于数据库持久化。
