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

> **我们公开边界，因为我们做安全。** 本清单里的每条能力都按「宣称 ↔ 证据」严格对齐：
> 宁可降级标注，也不虚标。逐条证据与可复跑的验证命令见
> [`docs/FEATURE_STATUS.md`](docs/FEATURE_STATUS.md)。

### Added（新增）

**产品能力**

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
- **`sql/002_omnilore_bootstrap_seed.sql`**：PostgreSQL 模式下的最小 seed。

**诚实标注与贡献入口**

- **R-13 能力状态矩阵（四态标注）**：新增 [`docs/FEATURE_STATUS.md`](docs/FEATURE_STATUS.md)，
  README 里的**每一条能力**都标注 `Implemented` / `Tested-real` / `Simulated` / `Planned`
  四态之一，逐条对应，并由测试守护「README ↔ 矩阵 ↔ CHANGELOG 三处一致」。
  当前 **28 条**能力标注：`Implemented` 6 条、`Tested-real` 7 条、`Simulated` 12 条、`Planned` 3 条。
  ⚠️ 其中「混合检索（BM25 + 向量 + 代码符号）」标 `Simulated` —— `hybrid`（1024 维向量）
  路径在 CI 里是 monkeypatch 桩（无真实网络调用），只有 `keyword_only` 真实执行。
- **R-13 威胁模型公开页**：新增 [`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md)，
  写明信任边界、**能防什么**、**防不住什么**。已披露的关键边界：
  **文件系统与数据库之间不承诺跨系统事务原子性**（用 Outbox 补偿 + 一致性对账把不一致显式暴露，而不是假装原子提交）。
- **R-13 可复跑的验证命令**：`Makefile` 把每条 `Simulated` 标注挂到一条真实命令上
  （`make verify-rls` / `make verify-sync` / `make verify-honesty` …）。
  `make verify-rls-pg` 是唯一需要真实 PostgreSQL 的目标，**环境不具备时以非零码退出并说明「未验证」**，
  绝不假装验证过。
- **R-10a 六套 Issue 模板**：`.github/ISSUE_TEMPLATE/` 下的 6 套表单以**下拉单选**为主
  （自由文本无法做分布统计）。`install-failure` 是首启成功率的唯一数据源，含「卡住多久？」；
  「我已放弃」经 `install-escape` 模板与 `.github/workflows/triage.yml` **自动**打 `P0-escape`；
  `retrieval-quality` 的「疑似越权」选项**自动**打 `security-triage`。
  安全漏洞仍不走 Issue 模板，走 GitHub Security Advisory。
- **合规文件包**：`LICENSE`（Apache-2.0 完整原文）、`NOTICE`、`CONTRIBUTING.md`（含 DCO）、
  `SECURITY.md`、`CODE_OF_CONDUCT.md`、`.github/PULL_REQUEST_TEMPLATE.md`、`docs/DECISIONS.md`。

**用户主权（可配、可关）**

- **R-16 受保护 frontmatter 字段白名单可配**：`OMNILORE_PROTECTED_FRONTMATTER` 可增删
  受保护系统字段，设为 `none` 即**整体关闭保护**（唯一不可关闭的是身份锚点 `doc_id`）。
  配置写错字段名会**响亮失败**，不会被静默当成「关掉保护」。
- **R-16 冲突副本落隐藏目录**：并发冲突副本落 `.omnilore/conflicts/`（点号开头 =
  **Obsidian 默认忽略**；注意 Windows 资源管理器仍可见，NTFS 不因点号隐藏目录），
  不散落在用户的 vault 里；可用 `OMNILORE_CONFLICT_DIR` 改，该配置禁止绝对路径与 `..`。
- **R-16 README 首屏主权声明**：显著位置写明「**我们会在你的笔记里写入 9 个系统字段 —— 你可以关掉**」。
  预先声明本身就是差异化，不藏着。

**CI 与测试**

- **R-08b 真实 PostgreSQL RLS 集成 CI**：`.github/workflows/ci.yml` 新增独立 job
  `integration-rls` —— 每次 push 用 `pgvector/pgvector:0.8.0-pg16` service container
  起一个**干净的真实 PostgreSQL 16**（`pg_isready -U postgres -d omnilore_verify`
  健康检查 + 20 次重试窗口），执行 `python tools/verify_rls_pg.py`，即
  `make verify-rls-pg` 的同一条命令。**零 Secret**，只用 GitHub 提供的 runner 与公共镜像。
  **失败即红**：探针的 `exit 2`（未验证：缺 psql / 目标库非空 / DDL 跑不动）与
  `exit 3`（已验证但结论为否）都被**逐码**转成 job 失败，不设 `continue-on-error`；
  退出码取自 python 进程（`PIPESTATUS[0]`）而不是 `tee`；成功路径还额外校验日志里
  真的出现了 3 条 `[PASS]`，防止「exit 0 但其实没跑探针」的假绿。
  失败时另跑一步诊断（`pg_class.relrowsecurity` / `pg_policy` / documents 表结构），
  只在失败时执行，避免污染成功日志。
  ⚠️ 标注口径：`真实 PostgreSQL 集成 CI` 为 **`Implemented`**，不是 `Tested-real` ——
  **配置正确 ≠ 已经跑过**；首次 CI 绿灯之前，RLS 条目也不得写成「已由 CI 持续验证」。
  配套 7 项配置守卫（`TestCiIntegrationConfig`，只读 YAML、不执行 docker）分别防：
  镜像 tag 写成不存在的 `pg16-v0.8.0`、健康检查漏 `-U`/`-d`、把 DDL 预挂进 initdb 钩子
  导致探针 `exit 2`、`continue-on-error`／吞退出码造成的假绿、以及新 job 挤掉原有离线矩阵。
- **CI 门禁**：Python 3.11 × 3.12 × {ubuntu, windows}-latest 的**离线**矩阵，跑 91 个测试 +
  ruff + DCO 校验 + 静态红线守卫，全程不使用任何 Secret；**另有一个独立的 `integration-rls`
  job 需要真实 PostgreSQL service container**（见上），它是全仓唯一需要外部服务的门禁，
  同样不使用任何 Secret。
- **测试套件**：91 项 —— 10 项安全与 RLS 验收、9 项全阶段验收、34 项首发专项、
  38 项路线图专项（主权 / 诚实标注 / 贡献入口 / 真实 PG 集成 CI 配置守卫）。
  全部跑在 `RLSDatabaseEngine` **内存仿真引擎**上，与 PostgreSQL RLS 同构但**不是真实 PostgreSQL**。

### Changed（变更）

- **版本号为 `v0.3.0-alpha`**：对外自称 `v0.3.0-alpha`，PEP 440 机读 `0.3.0a0`（同一个版本）。
- **`docker-compose.yml`**：镜像 tag 全部锁定到确定版本；包含 `DATABASE_URL` 与引导相关环境变量；
  n8n 默认不启动（`--profile integrations` 才启用 —— 它是可选集成，不是首启必需）。
- **`.env.example`**：`DASHSCOPE_API_KEY` 标注为**可选** —— 零 Key 也要能跑。
- **README 首屏**：含「60 秒验证」三段命令（全程零 API Key）与路径决策图。
- **README 核心能力小节按四态标注组织**：每条能力都带 `Implemented` / `Tested-real` /
  `Simulated` / `Planned` 徽章，不存在没有状态标注的裸宣称。
- **测试口径三处一致**：README、本文件、`docs/FEATURE_STATUS.md` 均写明 ——
  全部测试用例跑在 `omnilore.engine.RLSDatabaseEngine` 上，这是一个**内存仿真引擎**，
  与 PostgreSQL RLS **同构但不是真实 PostgreSQL**；因此 RLS 相关条目一律标 `Simulated`。
- **测试守卫覆盖「宣称 ↔ 证据」的每一处断点**：`tests/test_w2_w3_roadmap.py` 逐条守住
  主权配置可见性（4 条）、`docker-compose` 环境变量转发、镜像 tag 命名规则、
  README 全文裸宣称（2 条）、RLS 升级红线、非空库 fail-closed、真实 PG 集成 CI 配置（7 条）。
- **「真实 PostgreSQL RLS 策略与跨空间读拦截」标 `Tested-real`**：范围严格限定为
  **策略挂载 + 跨空间读拦截**，依据 2026-10-24 在真实 PostgreSQL 16.15 上的人工验证
  （3 条探针全部 PASS：7 张核心表 `rowsecurity=true` / 跨空间行未授权不可见 / 授权后同一行可见）。
  证据记录（含探针完整输出与「它没有证明什么」）内联在
  [`docs/FEATURE_STATUS.md` 第 6 节](docs/FEATURE_STATUS.md)，**不引用**任何 gitignored 的执行日志。
  证据强度：这是一次性**人工**验证；持续取证由 `integration-rls` job 承担，**首次绿灯**后才成立。
- **升级范围严格限定**：`四维权限交集`、`Profile 硬性隔离`、`撤销即时失效`、`组织树只读继承`、
  `混合检索` 等**未被这三条探针覆盖**，一律维持 `Simulated`，并由测试守住「不许跟涨」。
- **`tools/verify_rls_pg.py` 对非空库 fail-closed**：目标库已有本项目的表时，
  以 `exit 2` 停下并给出「新建空库 `omnilore_verify`」的可执行指引；
  **绝不**自动 `DROP SCHEMA` / 清库（那可能删掉真实数据）。

### Fixed（修复）

**权限与安全**

- **Windows 上 token 文件的权限**：`chmod 0600` 无法表达 POSIX 权限位，改用
  SID 授权 + 移除继承 ACE + 分步校验 + 失败回滚；最终仅保留本账户的完全控制，
  且 owner 始终可读可写。该权衡已在 `SECURITY.md` 中披露。
- **`doc_id` 身份锚点不可被放开**：`parse_protected_frontmatter_keys` 的所有返回路径
  统一过 `_with_identity_anchor()`，任何配置都无法关闭对 `doc_id` 的保护（逐分支断言）。
- **`tools/verify_rls_pg.py` 的 0/2/3 三码契约**：`psql` 若是 `.bat` 之类包装脚本，
  `CreateProcess` 无法直接执行 —— 因此校验可执行文件扩展名 + `OSError` 兜底，
  统一走 `EXIT_NOT_VERIFIED`：「没装客户端」属于**未验证**，不会伪装成「验证失败」的崩溃。

**数据完整性与跨平台行为**

- **SHA-256 对账在 Windows 上可用**：一致性对账按**字节**计算哈希，因此所有文本读写
  统一为 UTF-8 + LF —— 否则任何含换行的文档都会「磁盘字节 ≠ 被哈希的字节」，对账恒失败。
  由 CI 静态守卫（AST 扫描未规范化的 `.write_text()`）与 `windows-latest` 门禁防回归。
- **数据目录只有一个解析入口**：`resolve_data_dir()` 统一解析。CLI 与 MCP 服务各自解析时，
  在 `~` 或相对路径下会指向不同位置，导致 CLI 签发的 token 在 MCP 进程中失效。
- **仓库文本统一为 LF**：`.gitattributes` 约束下，索引与工作区均为 `i/lf` / `w/lf`。

**入口与工具链**

- **MCP 入口点可用**：console script 不会 await `async` 函数，因此 `omnilore-mcp` 为**同步**入口；
  `python -m omnilore.mcp_stdio_server` 与入口脚本行为一致。
- **`lint` 覆盖 `tools/`**：`make lint` 与 CI 都扫 `omnilore tests tools`。

**配置可见性（不静默失效）**

- **受保护字段由配置决定，而不是硬拒绝**：用户在 Obsidian 里修改受保护字段时，
  是否被拦由 `OMNILORE_PROTECTED_FRONTMATTER` 决定（`none` = 整体关闭保护）；
  并发冲突副本落 `.omnilore/conflicts/`，不污染 vault。
- **两个主权开关在容器 / `.env` 模式下同样生效**：`docker-compose.yml` 转发
  `OMNILORE_PROTECTED_FRONTMATTER` 与 `OMNILORE_CONFLICT_DIR`；`omnilore bootstrap`
  每次启动在 **stderr** 回显**实际生效值**。
- **变量名拼错会被点名**：回显会列出「本项目不认识」的 `OMNILORE_*`
  （如 `OMNILORE_PROTECTED_FRONTMATTRE`），静默失效变成可见失效。
- **`.env` 不自动加载**：本进程只读环境变量；README 与 `.env.example` 都写明这一点，
  并给出本地模式的载入方式。

**文档与宣称一致性**

- **混合检索标 `Simulated`**：矩阵里有这一行；`hybrid`（1024 维向量）路径在 CI 里是
  monkeypatch 桩（无真实网络调用），只有 `keyword_only` 真实执行 ——
  首屏与降级组合表旁同步标注该证据边界。
- **裸宣称守卫覆盖 README 全文**：段落 + 紧随其后的引用块为一个宣称单元，跳过代码块；
  首屏 / 降级组合表 / 模式对比表另单独守护。
- **裸宣称守卫对表格逐行判定**：整张表算一个单元时，块内任意一行有证据词就会放过
  被插入的一行裸宣称 —— 因此逐行判定，6 行为此补齐真实证据口径。
- **威胁模型措辞守卫**：要求「不」与「承诺跨系统事务原子性」出现在**同一行**，
  且落在「防不住什么」小节内 —— 单靠词面出现不足以成立（把「不承诺」翻成「承诺」仍会绿）。
- **README 的「N 项」逐文件核对**：`test_w0_launch` 的逐文件项数守卫覆盖全部四个测试文件，
  README 上的数字不可被随意改写。

**容器与镜像**

- **postgres 镜像 tag 为 `0.8.0-pg16`**：pgvector 官方命名规则是
  `<pgvector版本>-pg<PG主版本>`，写成 `pg16-v0.8.0` 在 Docker Hub **不存在**，
  `docker compose up -d --build` 会在 pull 阶段必失败。守卫要求 tag 匹配该规则且不允许 `:latest`。

---

## Known limitations（已知限制）

公开发布前请一并了解这些边界 —— **我们公开边界，因为我们做安全**。

- **这是预发布版本（alpha）**，仅供评估、研究与贡献使用，请勿放入任何重要业务流程。
- **默认数据库是内存引擎（内存仿真）**：`omnilore.engine.RLSDatabaseEngine` 与 PostgreSQL
  RLS 策略同构，但**不是真实 PostgreSQL**，且进程退出后状态清空。
  真实 PostgreSQL 模式需自行应用 `sql/001` + `sql/002`。
- **真实 PG RLS 的持续证据还在等首次 CI 绿灯**：`integration-rls` job 已接入（每次 push 在
  干净的真实 PostgreSQL 16 上重跑同三条探针），但它**尚未经过首次真实运行验证**；在此之前
  只有 2026-10-24 的一次性人工验证。因此仅「真实 PostgreSQL RLS 策略与跨空间读拦截」为
  `Tested-real`、「真实 PostgreSQL 集成 CI」为 `Implemented`，其余依赖真实 PG 的能力一律
  `Simulated`（逐条见 [`docs/FEATURE_STATUS.md`](docs/FEATURE_STATUS.md)，可自行复跑 `make verify-*`）。
- **文件系统与数据库不承诺跨系统事务原子性**：用 Outbox 补偿与一致性对账把不一致显式报出，
  详见 [`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md)。
- **未宣称任何合规认证**（SOC2 / ISO / 等保）。措辞严格限定为「技术可追溯」。
- **opt-in 匿名遥测仅落决策**（见 `docs/DECISIONS.md`），尚未实现，排期在后续版本。
- **`omnilore/engine.py` 的架构级重构未做**：它是全库最大的源文件，也是贡献者的
  心理门槛；重构排期在贡献者规模上来之后，本次遵循变更最小化原则未动。
