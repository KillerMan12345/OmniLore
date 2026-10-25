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

- **公开路线图进度页**：新增 [`docs/ROADMAP_STATUS.md`](docs/ROADMAP_STATUS.md)，公开 8 周窗口的
  S1–S5 里程碑、G0 / G2 / G3 门禁、维护者 / 首发后 / Q2 待办，并明确 `integration-rls`
  与 arm64 job 都尚未首次运行、封闭验证当前样本量为 0。
- **R-13 能力状态矩阵（四态标注）**：新增 [`docs/FEATURE_STATUS.md`](docs/FEATURE_STATUS.md)，
  README 里的**每一条能力**都标注 `Implemented` / `Tested-real` / `Simulated` / `Planned`
  四态之一，逐条对应，并由测试守护「README ↔ 矩阵 ↔ CHANGELOG 三处一致」。
  当前 **32 条**能力标注：`Implemented` 11 条、`Tested-real` 8 条、`Simulated` 12 条、`Planned` 1 条。
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

**可审计证据包（R-05m）与 RLS 策略注释（R-08b 补课）**

- **R-05m `omnilore audit-bundle`**：对某个文档或某个空间导出
  `audit-bundle.json` + `audit-bundle.verify.md`。内容包含文档本体信息
  （`doc_id` / `space_id` / `version_key` / `version_label` / `content_revision`）、
  每个原始证据的 SHA-256 与原始文件名/字节数/入库时间、版本链（`version_key` 序列 +
  每版内容哈希）、生成信息（导出时间 / 工具版本 / 数据目录**名**）。
  **哈希一定从磁盘字节重算**再与库里记录比对：不一致标 `mismatch`、文件缺失标 `missing`，
  两种情况都以**退出码 3**（已验证但结论为否）结束 —— 不会输出「看起来正常但没校验」的包；
  范围内没有结构化元数据时 **fail-closed**（退出码 1）并给出可执行的下一步。
  bundle 刻意不含 token / token_hash / 绝对路径，`how_to_verify` 写明
  「拿哪个文件、用什么算法、与哪个字段比对」以及「它不证明什么」。
  **跨进程可用**：内存引擎在提交时把「文档 ↔ 原始资产」最小映射写到
  `<数据目录>/ops/index/`（`assets.json` + `document_assets.json`），
  所以 `omnilore demo` 之后**另起一个进程**也能导出。该索引是**派生物**
  （可从 vault frontmatter + `ops/cas/` + 数据库重建，删掉不丢原始证据），
  且**哈希校验绝不信任索引** —— 仍然读真实字节重算再比对。索引缺失时
  fail-closed（退出码 1）并提示「先运行 `omnilore demo`」，不把「还没入库」
  说成「数据损坏」。
  配套 `make verify-audit`（`tools/verify_audit_bundle.py`）：入库 → 导出 →
  **改 CAS 里 1 个字节 → `mismatch` + exit 3** → 改回 → 恢复 `verified`，
  用实测证明证据包真的在校验（而不是把库里的值抄一遍）。
  另有 **4 条跨进程用例**（`TestAuditBundleCrossProcess`，真实 `subprocess`：
  `bootstrap`+`demo` 在进程 A，`audit-bundle` 在进程 B/C/D）：跨进程导出 verified、
  跨进程篡改 → `mismatch` + exit 3、删索引 → exit 1 + 提示先入库、
  `ops/index/` 内无 token / 绝对路径。
- **R-08b 补课：21 条 RLS 策略注释**：`sql/001_omnilore_schema_and_rls.sql` 里的
  **21 条**策略逐条补上四段注释 —— ① 保护什么（表 + 读/写动作）；② 依赖哪个维度
  （并注明**只有 space + category 两维被真实 PG 探针验证过**，`authz.current_user_id()`
  与 membership / profile 维度未验证）；③ 防的具体攻击场景；④ 不覆盖什么。
  **只加注释、不改逻辑**：策略语句摘要被冻结在测试里，改动逻辑会让
  `TestRlsPolicyComments` 直接变红，提醒先重跑真实 PG 探针再更新证据。

**向量来源可替换（R-04b）与平台一致性（R-11b）**

- **R-04b `EmbeddingProvider` 抽象**：向量来源不再绑死阿里云百炼。
  - `OMNILORE_EMBEDDING_PROVIDER`：`dashscope`（默认，行为与抽象前**逐字一致**）/
    `openai`（任意 OpenAI 兼容端点 —— OpenAI、Azure OpenAI、**Ollama**、vLLM、LM Studio、
    硅基流动）/ `none`（显式只用 BM25，`degraded_reason` 会说明这是配置选择而非故障）。
  - `OMNILORE_EMBEDDING_BASE_URL` 指向自建端点；**本地回环地址（localhost / 127.0.0.1 /
    host.docker.internal）不需要 API Key**（Ollama 开箱可用）。鉴权顺序：
    `OMNILORE_EMBEDDING_API_KEY` → `OPENAI_API_KEY` → 本地端点免鉴权。
  - **四个曾经的「预留、不读取」变量现已真正生效**：`OMNILORE_PRIMARY_MODEL` /
    `OMNILORE_ESCALATED_MODEL`（进入 chat 请求体的 `model`）、`OMNILORE_EMBEDDING_MODEL` /
    `OMNILORE_EMBEDDING_DIM`（进入 embeddings 请求体）。`.env.example` 里那段「预留、未读取」
    的说明已删除；`omnilore bootstrap` 会回显**实际生效**的 provider / 模型 / 维度 / 是否降级，
    拼错的变量名仍会被点名。
  - **维度不一致必须响亮失败**：配置维度与 provider 实际返回不一致时抛
    `EmbeddingConfigurationError` 并给出该改哪个变量，**绝不截断/补零**（那会静默产出错误相似度）。
  - **降级链零退化**：provider 未配置 / 401 / 超时 / 断网 → 本次请求降级 `keyword_only`，
    `degraded_reason` 如实上报，检索结果仍由 BM25 给出；配置错（维度/provider 名）不属于
    「供应商故障」，会被单独归因为配置错误。
  - 证据边界：用例用 `monkeypatch` 的假 HTTP 端点验证请求体与维度，**没有对真实 OpenAI /
    Ollama 端点发起过请求**，因此标 `Implemented`，不标 `Tested-real`。
- **R-11b 统一数据目录**：复核了数据目录的解析路径 —— CLI、MCP 服务端、运行时索引、
  证据包输出、引导状态全部基于 `resolve_data_dir` 的同一个根；新增静态守卫禁止任何模块
  自己调用 `.expanduser()` 或直接读 `OMNILORE_DATA_DIR`。
- **R-11b ARM64 验证**：`.github/workflows/ci.yml` 新增独立 `arm64` job
  （`ubuntu-24.04-arm`，GitHub 公共仓库免费 runner），跑同一套离线用例 + `ruff` + 冒烟；
  `integration-rls` **刻意保留在 x86**（不在新架构上再叠 service container 这个变量）。
  ⚠️ 标注口径：该 job **尚未经过首次真实运行**，故为 `Implemented` 而非 `Tested-real`。
- **R-11b README 支持架构声明**：新增「支持架构」小节，写明 x86_64（CI 真实执行）与
  arm64（CI 配置已就位、待首次绿灯）各自的验证方式。

**PostgreSQL 安全连接层（安全角色 + RLS 生效验证）**

- **安全 PG 运行时连接层 `omnilore/pg_runtime.py`**（新增）：设置 `DATABASE_URL` 时由 MCP
  stdio 服务端构造 `PgRuntime` 并建立连接，随后**强制校验连接角色非超级用户、无
  `BYPASSRLS`、是策略目标角色（`omnilore_gateway` / `omnilore_worker`）成员**，任一不满足
  即**拒绝启动**（fails closed）。为什么必须这样：PostgreSQL 超级用户与 `BYPASSRLS` 角色
  **默认绕过 RLS** —— 应用若以这类角色连接，结果不是报错，而是「读写一切正常、权限隔离
  完全失效」的假安全，比连不上更危险。事务内 `SET LOCAL ROLE` 切到策略角色，并以 `app.*`
  GUC 注入请求上下文，`注入失败即中止事务`（默认拒绝，不放行）。
  **未设置 `DATABASE_URL` 时行为与之前完全一致**：仍是内存引擎，不连任何 PG（由
  `tests/test_pg_runtime_wiring.py` 钉住）。
- **`tools/verify_rls_app_role.py`**（新增）：用**应用角色**在真实 PostgreSQL 上复现 RLS
  三条的可复跑证据脚本（P-a 未授权 0 行 / P-b 授权后可见以排除空表假阳性 / P-c 连接角色
  非超用户且无 BYPASSRLS）。它补上了原 `tools/verify_rls_pg.py` 的缺口 —— 后者用超级用户
  `omnilore_migrator` 连接后 `SET ROLE`，无法证明「应用会用的角色」连接时 RLS 生效。
  退出码语义与既有探针一致：0=通过 / 2=未验证 / 3=已验证但结论为否。
- **新增安全应用角色 `omnilore_app`**（部署侧，`sql/` 红线文件未改动）：
  `LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS`，授予 `omnilore_gateway` /
  `omnilore_worker` 成员资格；应用不再直接以 `NOLOGIN` 的策略角色登录，改为事务内
  `SET LOCAL ROLE` 切换。**已知事实**：`omnilore_migrator` 是 docker 镜像的 bootstrap 超级
  用户且**无法降权**（`ALTER ROLE ... NOSUPERUSER` 报 bootstrap user 必须保留 SUPERUSER），
  因此安全依靠「应用只用 `omnilore_app`」+「`PgRuntime` 强制拒绝超用户」两重保证。
- ⚠️ **标注口径与边界**：本条在能力矩阵中标 **`Tested-real`**，依据是 2026-10-25 在真实
  PostgreSQL 16.15 上以 `omnilore_app` 的**人工一次性**验证（**非 CI**，三条探针 PASS，
  超用户连接被拒；脚本可复跑）。它证明的是「**安全连接这条路径上 RLS 确实生效**」，
  **不等于**「业务数据已持久化」—— `OmniLoreEngine` 的业务数据层**仍然走内存
  `RLSDatabaseEngine`**，`AsyncpgRepository` 仍未被业务路径实例化；把这次接线读成
  「已接通 PostgreSQL 持久化 / 可多人共享」是误读。

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
- **CI 门禁**：Python 3.11 × 3.12 × {ubuntu, windows}-latest 的**离线**矩阵，跑 145 个测试 +
  ruff + DCO 校验 + 静态红线守卫，全程不使用任何 Secret；**另有一个独立的 `integration-rls`
  job 需要真实 PostgreSQL service container**（见上），它是全仓唯一需要外部服务的门禁，
  同样不使用任何 Secret。
- **测试套件**：145 项 —— 10 项安全与 RLS 验收、9 项全阶段验收、34 项首发专项、
  57 项路线图专项（主权 / 诚实标注 / 贡献入口 / 真实 PG 集成 CI 配置守卫 /
  证据包导出与篡改测试 / 21 条 RLS 策略注释守卫）、30 项 W6 专项
  （provider 抽象 / 四个环境变量真生效 / 维度守卫 / 降级回归 / 统一数据目录 / arm64 配置）、
  5 项 PG 运行时接线专项（`test_pg_runtime_wiring.py`：内存引擎默认 / 无 `DATABASE_URL`
  时不接 PG / 拒绝超用户与 `BYPASSRLS` 连接角色 —— **离线 mock，不连真实 PG**）。
  140 项业务用例跑在 `RLSDatabaseEngine` **内存仿真引擎**上，与 PostgreSQL RLS 同构但
  **不是真实 PostgreSQL**；新增的 5 项接线用例同样离线（mock），不产生真实 PG 证据。

### Changed（变更）

- **版本号为 `v0.3.0-alpha`**：对外自称 `v0.3.0-alpha`，PEP 440 机读 `0.3.0a0`（同一个版本）。
- **`docker-compose.yml`**：镜像 tag 全部锁定到确定版本；包含 `DATABASE_URL` 与引导相关环境变量；
  n8n 默认不启动（`--profile integrations` 才启用 —— 它是可选集成，不是首启必需）。
- **`.env.example`**：`DASHSCOPE_API_KEY` 标注为**可选** —— 零 Key 也要能跑。
- **README 首屏**：含「60 秒验证」三段命令（全程零 API Key）与路径决策图。
- **README 核心能力小节按四态标注组织**：每条能力都带 `Implemented` / `Tested-real` /
  `Simulated` / `Planned` 徽章，不存在没有状态标注的裸宣称。
- **测试口径三处一致**：README、本文件、`docs/FEATURE_STATUS.md` 均写明 ——
  140 项业务用例跑在 `omnilore.engine.RLSDatabaseEngine` 上，这是一个**内存仿真引擎**，
  与 PostgreSQL RLS **同构但不是真实 PostgreSQL**；新增的 5 项 PG 运行时接线用例用
  **离线 mock**，也不连真实 PG；因此 RLS 相关条目一律标 `Simulated`
  （两条 `Tested-real` 例外均为真实 PG 上的人工一次性证据，见能力矩阵第 6 / 6.1 节）。
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
- **多格式入库证据降级为 `Implemented`**：自动化用例真实覆盖 Markdown / 代码与 `Pillow` PNG，
  但当前没有构造或入库真实 PDF，故不能把 `pdfplumber` / `pypdf` 分支写成 `Tested-real`。
- **n8n 工作流证据修正为 `Implemented`**：三份 JSON 的结构、无硬编码凭据与内部 Worker
  网关已有离线自动化用例；真实 n8n 实例导入 / 启动 / 运行仍未验证，因此不标 `Tested-real`。
- **容器运行时边界已公开**：compose 会启动 PostgreSQL、应用 SQL 并转发 `DATABASE_URL`。
  设置该变量时 MCP 会建立**安全 PG 连接层**（安全角色 + 事务内 `SET LOCAL ROLE` + RLS GUC，
  超用户连接拒绝启动），但业务路径**没有**实例化 `AsyncpgRepository`，业务读写仍走
  `RLSDatabaseEngine`；容器模式现阶段不能宣称结构化状态持久化或多人共享。
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
- **当前业务数据层是内存引擎（内存仿真）**：`omnilore.engine.RLSDatabaseEngine` 与 PostgreSQL
  RLS 策略同构，但**不是真实 PostgreSQL**，且进程退出后状态清空。
  设置 `DATABASE_URL` 时 MCP 会额外建立一条**安全 PG 连接层**（非超用户、无 `BYPASSRLS`
  的应用角色 + 事务内 `SET LOCAL ROLE` + RLS GUC 注入），但它只做安全接入与启动自检，
  **尚未**承载业务读写 —— 业务路径仍未实例化 `AsyncpgRepository`；
  **容器模式也不能作为持久化运行模式**。
- **真实 PG RLS 的持续证据还在等首次 CI 绿灯**：`integration-rls` job 已接入（每次 push 在
  干净的真实 PostgreSQL 16 上重跑同三条探针），但它**尚未经过首次真实运行验证**；在此之前
  只有两次人工一次性验证（2026-10-24 策略探针、2026-10-25 应用角色运行时连接层探针，
  均**非 CI**）。因此仅「真实 PostgreSQL RLS 策略与跨空间读拦截」与「PostgreSQL 安全连接层
  与 RLS 生效验证」为 `Tested-real`、「真实 PostgreSQL 集成 CI」为 `Implemented`，其余依赖
  真实 PG 的能力一律 `Simulated`（逐条见 [`docs/FEATURE_STATUS.md`](docs/FEATURE_STATUS.md)，
  可自行复跑 `make verify-*` 与 `python tools/verify_rls_app_role.py`）。
- **文件系统与数据库不承诺跨系统事务原子性**：用 Outbox 补偿与一致性对账把不一致显式报出，
  详见 [`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md)。
- **未宣称任何合规认证**（SOC2 / ISO / 等保）。措辞严格限定为「技术可追溯」。
- **opt-in 匿名遥测仅落决策**（见 `docs/DECISIONS.md`），尚未实现，排期在后续版本。
- **`omnilore/engine.py` 的架构级重构未做**：它是全库最大的源文件，也是贡献者的
  心理门槛；重构排期在贡献者规模上来之后，本次遵循变更最小化原则未动。
