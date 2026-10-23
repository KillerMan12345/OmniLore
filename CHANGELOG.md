# 变更日志 CHANGELOG

本文件记录 OmniLore 对外可见的变更。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本语义遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

> **版本口径说明**
> 对外统一自称 **`v0.3.0-alpha`**；由于 Python 打包生态要求 PEP 440，
> `pyproject.toml` 与 `omnilore.__version__` 中记录的机读版本号为 **`0.3.0a0`**。
> 两者是同一个版本，差异纯粹来自打包工具的格式要求。详见 [`docs/DECISIONS.md`](docs/DECISIONS.md)。

---

## [v0.3.0-alpha] — 首个开源版本

OmniLore 是一个以**不可变原始证据（SHA-256 CAS）**为基础、以**可审阅的 Obsidian Markdown 知识页**
为人类主要编辑界面、以 **PostgreSQL 行级安全策略（RLS）** 提供命令级权限的知识中枢。

定位：**One auditable knowledge base, for every agent** —— 任何智能体（Cursor / Claude Desktop /
自建 Agent / n8n）都可通过 MCP 调用同一份知识，且每条回答都能追溯到原始证据。

### Added（新增）

- **`omnilore` CLI**
  - `omnilore bootstrap`：零 API Key 初始化数据目录、创建默认空间与 owner token，
    并打印可直接粘贴的 MCP 客户端配置。
  - `omnilore demo`：投一份文档，再把它搜出来 —— 60 秒自证「这条答案凭什么可信」。
  - `omnilore version`：打印对外版本与 PEP 440 版本。
  - 入口脚本 `omnilore <cmd>` 与 `python -m omnilore <cmd>` 两种方式均可调用。
- **MCP stdio 服务**：暴露 7 个工具（`search_knowledge` / `get_document_asset` /
  `ingest_document_or_file` / `commit_memory_note` / `get_ingestion_task` /
  `review_and_fix_knowledge` / `query_health_trends`），任何 MCP 客户端可直接挂载。
  新环境首次启动自动完成引导。
- **零密钥降级契约**：未配置 API Key 时检索自动走 BM25 / 关键词路径，
  返回值中 `retrieval_mode` 为 `keyword_only`、`degraded_reason` 如实说明降级原因。
  关键词分词采用与本地确定性嵌入一致的 **CJK 二元组**策略，保证中文查询可命中。
- **降级正交上报**：`degraded_reason` 只描述**检索**是否降级；答案合成的降级独立为
  `synthesis_degraded` 与 `synthesis_note`，两者不混在一起。
- **合规文件包**：`LICENSE`（Apache-2.0 完整原文）、`NOTICE`、`CONTRIBUTING.md`（含 DCO）、
  `SECURITY.md`、`CODE_OF_CONDUCT.md`、`.github/PULL_REQUEST_TEMPLATE.md`、`docs/DECISIONS.md`。
- **离线 CI**：Python 3.11 × 3.12 × {ubuntu, windows}-latest 矩阵，跑 53 个测试 + ruff +
  DCO 校验 + 静态红线守卫，全程离线、不使用任何 Secret。
- **测试套件**：53 项 —— 10 项安全与 RLS 验收、9 项全阶段验收、34 项首发专项
  （引导链路、LF 规范化、版本号一致、零 Key 降级、CLI/MCP 路径一致、权限收紧）。
- **`sql/002_omnilore_bootstrap_seed.sql`**：PostgreSQL 模式下的最小 seed。

### Fixed（修复）

- **跨平台的 SHA-256 对账一致性**：此前部分文本写入未统一行尾，而一致性对账按
  字节计算哈希 —— 在 Windows 上任何含换行的文档都会导致「磁盘字节 ≠ 被哈希的字节」，
  使对账恒为失败。现所有文本读写统一为 UTF-8 + LF，并由 CI 静态守卫与
  `windows-latest` 门禁防止回归。这是对「可审计」这个核心卖点的正确性修复。
- **MCP 入口点不可用**：`omnilore-mcp` 此前指向一个 `async` 函数，而 console script
  不会被 await —— 进程只输出一条告警就退出，客户端按 README 配置必然挂载失败。
  现改为同步入口，`python -m omnilore.mcp_stdio_server` 与入口脚本行为一致。
- **Windows 上 token 文件的权限收紧**：`chmod 0600` 在 Windows 无法表达 POSIX 权限位。
  现通过 SID 授权 + 移除继承 ACE + 分步校验 + 失败回滚实现；实测最终仅保留
  本账户的完全控制，且 owner 始终可读可写。该权衡已在 `SECURITY.md` 中披露。
- **数据目录解析漂移**：CLI 与 MCP 服务此前各自解析数据目录，在 `~` 或相对路径下
  会指向不同位置，导致 CLI 签发的 token 在 MCP 进程中失效。现统一由
  `resolve_data_dir()` 解析。

### Changed（变更）

- **版本号**：统一为对外 `v0.3.0-alpha`（PEP 440 机读 `0.3.0a0`）。
- **`docker-compose.yml`**：镜像 tag 全部锁定；补齐 `DATABASE_URL` 与引导相关环境变量；
  n8n 移出默认启动（改为 `--profile integrations`，它是可选集成而非首启必需）。
- **`.env.example`**：明确标注 `DASHSCOPE_API_KEY` 为**可选** —— 零 Key 也要能跑。
- **README**：新增首屏「60 秒验证」三段命令（全程零 API Key）与路径决策图。

---

## Known limitations（已知限制）

公开发布前请一并了解这些边界 —— **我们公开边界，因为我们做安全**。

- **这是预发布版本（alpha）**，仅供评估、研究与贡献使用，请勿放入任何重要业务流程。
- **默认数据库是内存引擎**：它与 PostgreSQL RLS 策略同构，但进程退出后状态清空。
  真实 PostgreSQL 模式需自行应用 `sql/001` + `sql/002`。
- **真实 PG RLS 尚无 CI 证据**：RLS 逻辑当前由同构的内存引擎验证，未接入真实
  PostgreSQL 的自动化集成测试。在拿到 CI 证据前，相关能力对外一律标注为 `Simulated`。
- **未宣称任何合规认证**（SOC2 / ISO / 等保）。措辞严格限定为「技术可追溯」。
- **opt-in 匿名遥测仅落决策**（见 `docs/DECISIONS.md`），尚未实现，排期在后续版本。
- **`omnilore/engine.py` 的架构级重构未做**：它是全库最大的源文件，也是贡献者的
  心理门槛；重构排期在贡献者规模上来之后，本次遵循变更最小化原则未动。
