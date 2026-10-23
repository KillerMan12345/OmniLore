# 变更日志 CHANGELOG

本文件记录 OmniLore 对外可见的变更。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本语义遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

> **版本口径说明**
> 对外统一自称 **`v0.3.0-alpha`**；由于 Python 打包生态要求 PEP 440，
> `pyproject.toml` 与 `omnilore.__version__` 中记录的机读版本号为 **`0.3.0a0`**。
> 两者是同一个版本，差异纯粹来自打包工具的格式要求。详见 [`docs/DECISIONS.md`](docs/DECISIONS.md)。

---

## [Unreleased] — W2 / W3 路线图（R-16 主权 · R-13 诚实标注 · R-10a 贡献入口）

> **我们公开边界，因为我们做安全。**
> 本轮三项改动的共同目标只有一个：**让「宣称」与「证据」严格对齐**。
> 宁可降级标注，也不虚标。

### Added（新增）

- **R-13 能力状态矩阵（四态标注）**：新增 [`docs/FEATURE_STATUS.md`](docs/FEATURE_STATUS.md)，
  README 里出现的**每一条能力**都标注 `Implemented` / `Tested-real` / `Simulated` / `Planned`
  四态之一，逐条对应，并由测试守护「README ↔ 矩阵 ↔ CHANGELOG 三处一致」。
  当前 **28 条**能力标注：`Implemented` 5 条、`Tested-real` 7 条、`Simulated` 12 条、`Planned` 4 条。
  ⚠️ 其中「混合检索（BM25 + 向量 + 代码符号）」为**本次严过关补上的一条真裸宣称**：
  首屏定义句宣称了它，但矩阵里原本没有这一行，且 `hybrid` 路径在 CI 里是 monkeypatch 桩
  （无真实网络调用），故标 `Simulated`。同时补上「不可变原始证据（SHA-256 CAS）」一行。
- **R-13 威胁模型公开页**：新增 [`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md)，
  写明信任边界、**能防什么**、**防不住什么**。已披露的关键边界：
  **文件系统与数据库之间不承诺跨系统事务原子性**（用 Outbox 补偿 + 一致性对账把不一致显式暴露，而不是假装原子提交）。
- **R-13 可复跑的验证命令**：新增 `Makefile`，把每条 `Simulated` 标注挂到一条真实命令上
  （`make verify-rls` / `make verify-sync` / `make verify-honesty` …）。
  `make verify-rls-pg` 是唯一需要真实 PostgreSQL 的目标，**环境不具备时以非零码退出并说明「未验证」**，
  绝不假装验证过。
- **R-16 受保护 frontmatter 字段白名单可配**：新增 `OMNILORE_PROTECTED_FRONTMATTER`。
  可增删受保护系统字段，设为 `none` 即**整体关闭保护**（唯一不可关闭的是身份锚点 `doc_id`）。
  配置写错字段名会**响亮失败**，不会被静默当成「关掉保护」。
- **R-16 冲突副本落隐藏目录**：`.conflict_r*.md` 不再散落在用户的 vault 里，
  默认落到 `.omnilore/conflicts/`（点号开头 = **Obsidian 默认忽略**；注意 Windows
  资源管理器仍可见，NTFS 不因点号隐藏目录），
  可用 `OMNILORE_CONFLICT_DIR` 改；目录配置同样禁止绝对路径与 `..`。
- **R-16 README 首屏主权声明**：显著位置写明「**我们会在你的笔记里写入 9 个系统字段 —— 你可以关掉**」。
  预先声明本身就是差异化，不藏着。
- **R-10a 六套 Issue 模板**：`.github/ISSUE_TEMPLATE/` 新增 6 套表单，
  以**下拉单选**为主（自由文本无法做分布统计）。`install-failure` 是首启成功率的唯一数据源，
  含「卡住多久？」；「我已放弃」经 `install-escape` 模板与 `.github/workflows/triage.yml`
  **自动**打 `P0-escape`；`retrieval-quality` 的「疑似越权」选项**自动**打 `security-triage`。
  安全漏洞仍不走 Issue 模板，走 GitHub Security Advisory。

### Changed（变更）

- **README 核心能力小节重写为四态标注**：删掉了所有没有状态标注的裸宣称。
- **测试口径三处一致**：README、本文件、`docs/FEATURE_STATUS.md` 均写明 ——
  全部测试用例跑在 `omnilore.engine.RLSDatabaseEngine` 上，这是一个**内存仿真引擎**，
  与 PostgreSQL RLS **同构但不是真实 PostgreSQL**；因此 RLS 相关条目一律标 `Simulated`。
- **测试数量 53 → 84**：新增 `tests/test_w2_w3_roadmap.py`（31 项，覆盖主权、诚实标注、贡献入口）。

### Fixed（修复）

- **P2（Obsidian 重度用户）主权冲突**：此前受保护字段被硬拒绝修改（用户在自己的笔记里
  改字段却被系统拦下），且并发冲突副本直接污染 vault。两者都改为「用户可配置」。

### Fixed（修复 · QA 二轮验收 2×P1 + 10×P2）

- **P1 裸宣称「混合检索 / 1024 维向量」**：README 首屏与降级组合表宣称了混合检索，
  但状态矩阵里没有这一行，且 `hybrid` 路径在 CI 里是 monkeypatch 桩（无真实网络调用）。
  现补进矩阵并标 `Simulated`，首屏/组合表旁同步标注证据边界。
- **P1 R-16 两个开关在 `.env` / 容器模式下静默无效**：`docker-compose.yml` 补转发
  `OMNILORE_PROTECTED_FRONTMATTER` 与 `OMNILORE_CONFLICT_DIR`；`omnilore bootstrap`
  每次启动在 **stderr** 回显当前**实际生效**的受保护字段与冲突目录。
- **P2 变量名拼错无提示**：回显会点名「本项目不认识」的 `OMNILORE_*` 变量
  （如 `OMNILORE_PROTECTED_FRONTMATTRE`），静默失效变成可见失效。
- **P2 `README` 让用户「详见 `.env.example`」却没人读 `.env`**：README 与 `.env.example`
  均明确写出「本进程只读环境变量，不自动加载 `.env`」，并给出本地模式的载入方式。
- **P2 裸宣称守卫只扫「核心能力」小节**：扫描范围扩到 **README 全文**
  （段落 + 紧随其后的引用块为一个宣称单元，跳过代码块），并单独守护首屏 /
  降级组合表 / 模式对比表三段。
- **P2 `tools/verify_rls_pg.py` 在 `psql` 为 `.bat` 包装脚本时崩成 exit 1 + traceback**：
  改为校验可执行文件扩展名 + `OSError` 兜底，统一走 `EXIT_NOT_VERIFIED`（守住 0/2/3 三码契约）。
- **P2 `make lint` / CI 漏扫 `tools/`**：两处都补上 `tools`。
- **P2 用例数守卫漏一个文件**：`test_w0_launch` 的逐文件项数守卫补上
  `test_w2_w3_roadmap.py`，README 上「N 项」不再可被随意改写。
- **P2 威胁模型措辞守卫太弱**：把「**不**承诺跨系统事务原子性」翻成「承诺」曾仍然绿，
  现要求两个词出现在同一行且落在「防不住什么」小节内。
- **P2 `doc_id` 身份锚点只在部分分支有守卫**：`parse_protected_frontmatter_keys`
  的所有返回路径统一过 `_with_identity_anchor()`，并逐分支断言 `doc_id` 不可被放开。
- **工作区 CRLF 残留**：8 个文件（`.github/workflows/ci.yml`、`cli.py`、`engine.py`、
  `mcp_stdio_server.py`、`pathutil.py`、`textio.py`、`version.py`、`tools/mutation_check.py`）
  在加 `.gitattributes` 之前 checkout，工作区仍是 CRLF；已 `git add --renormalize`
  并统一为 LF（索引与工作区均为 `i/lf` / `w/lf`）。

### Changed（变更 · QA 二轮验收）

- **测试数量 84 项**：新增 9 条守卫（4 条主权配置可见性 / compose 转发 + 1 条镜像 tag 命名
  + 2 条全文裸宣称 + 1 条 RLS 升级红线 + 1 条非空库 fail-closed），
  `tests/test_w2_w3_roadmap.py` 由 22 项增至 31 项。

### Fixed（修复 · QA 三轮验收 P1-3 + P2）

- **P1 `docker-compose.yml` 的 postgres 镜像 tag 在 Docker Hub 不存在**：
  `pgvector/pgvector:pg16-v0.8.0` → **`pgvector/pgvector:0.8.0-pg16`**。pgvector 官方 tag
  规则是 `<pgvector版本>-pg<PG主版本>`，反着写会 `not found`；旧值让 README 承诺的
  `docker compose up -d --build` 在 pull 阶段必然失败（容器模式整条路径不可用）。
  新增守卫：tag 必须匹配该命名规则，且不允许 `:latest`。
- **P2 裸宣称守卫对表格是「块级」判定**：一整张表算一个单元时，只要块内任意一行有证据词，
  被插进来的一行裸宣称就会连带放过。现对**表格逐行**判定，并为 6 行原本靠「整块」蒙混的
  表格行补上真实证据口径（组合态表 2/3/4 行、测试文件表、文档索引表 2 行）。

### Changed（变更 · 真实 PostgreSQL 证据升级）

- **「真实 PostgreSQL RLS 策略与跨空间读拦截」从 `Simulated` 升为 `Tested-real`**：
  依据 2026-10-24 在真实 PostgreSQL 16.15 上的人工验证（3 条探针全部 PASS：7 张核心表
  `rowsecurity=true` / 跨空间行未授权不可见 / 授权后同一行可见）。
  证据记录（含探针完整输出与「它没有证明什么」）内联在
  [`docs/FEATURE_STATUS.md` 第 6 节](docs/FEATURE_STATUS.md)，**不引用**任何 gitignored 的执行日志。
  ⚠️ 这是**一次性人工**证据、**没有**自动化 CI 门禁：`真实 PostgreSQL 集成 CI` 仍为 `Planned`。
- **升级范围严格限定**：`四维权限交集`、`Profile 硬性隔离`、`撤销即时失效`、`组织树只读继承`、
  `混合检索` 等**未被这三条探针覆盖**，一律维持 `Simulated`，并由新增测试守住「不许跟涨」。
- **`tools/verify_rls_pg.py` 对非空库 fail-closed**：目标库已有本项目的表时，
  以 `exit 2` 停下并给出「新建空库 `omnilore_verify`」的可执行指引；
  **绝不**自动 `DROP SCHEMA` / 清库（那可能删掉真实数据）。

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
- **离线 CI**：Python 3.11 × 3.12 × {ubuntu, windows}-latest 矩阵，跑 84 个测试 + ruff +
  DCO 校验 + 静态红线守卫，全程离线、不使用任何 Secret。
- **测试套件**：84 项 —— 10 项安全与 RLS 验收、9 项全阶段验收、34 项首发专项、
  31 项 W2/W3 路线图专项（主权 / 诚实标注 / 贡献入口）。全部跑在 `RLSDatabaseEngine`
  **内存仿真引擎**上，与 PostgreSQL RLS 同构但**不是真实 PostgreSQL**。
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
- **默认数据库是内存引擎（内存仿真）**：`omnilore.engine.RLSDatabaseEngine` 与 PostgreSQL
  RLS 策略同构，但**不是真实 PostgreSQL**，且进程退出后状态清空。
  真实 PostgreSQL 模式需自行应用 `sql/001` + `sql/002`。
- **真实 PG RLS 尚无 CI 证据**：RLS 逻辑当前由同构的内存仿真引擎验证，未接入真实
  PostgreSQL 的自动化集成测试。在拿到 CI 证据前，相关能力对外一律标注为 `Simulated`
  （逐条见 [`docs/FEATURE_STATUS.md`](docs/FEATURE_STATUS.md)，可自行复跑 `make verify-*`）。
- **文件系统与数据库不承诺跨系统事务原子性**：用 Outbox 补偿与一致性对账把不一致显式报出，
  详见 [`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md)。
- **未宣称任何合规认证**（SOC2 / ISO / 等保）。措辞严格限定为「技术可追溯」。
- **opt-in 匿名遥测仅落决策**（见 `docs/DECISIONS.md`），尚未实现，排期在后续版本。
- **`omnilore/engine.py` 的架构级重构未做**：它是全库最大的源文件，也是贡献者的
  心理门槛；重构排期在贡献者规模上来之后，本次遵循变更最小化原则未动。
