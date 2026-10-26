# OmniLore 公开路线图进度

> **我们公开边界，因为我们做安全。**
> 本页只记录可由公开仓库复核的进度与尚未完成的门禁，不引用内部材料，也不把“配置已写好”说成“已经运行通过”。
> 能力证据的逐条口径以 [`FEATURE_STATUS.md`](FEATURE_STATUS.md) 为准。

## 1. 首发窗口与当前进度

当前计划采用 **8 周首发窗口**。首发前的工作分为五个里程碑：

| 里程碑 | 目标 | 当前状态 | 公开证据与边界 |
|---|---|---|---|
| **S1：合规与首启解锁** | 合规文件、零密钥引导、跨平台文本正确性、离线门禁 | **已完成** | 合规文件、`bootstrap` / `demo`、Windows LF 门禁、离线测试矩阵配置均已入库；当前全量套件 151 项通过 |
| **S2：诚实标注与社区入口** | 四态能力矩阵、威胁模型、笔记主权开关、贡献入口 | **已完成** | README、能力矩阵与 CHANGELOG 的状态统计一致；六套 Issue 模板与六条贡献路径已提供 |
| **S3：真实 RLS 证据** | 真实 PostgreSQL 探针与自动化持续取证 | **持续取证已跑通；数据面迁移仍未做** | **① 策略探针已由 CI 持续验证**：`integration-rls` job 已在 GitHub Actions 上真实运行并**连续 2 次绿灯**（2026-10-26，Run #1 `8d0ecc3` / Run #2 `5d513fe`，3 条探针均 PASS）—— 它只证明 `sql/001` 的 SQL 策略在真实 PG 上生效，**不等于业务运行时已接入 PG**；**② 应用角色走运行时连接层的探针仍是人工一次性、非 CI**（2026-10-25 / 2026-10-26 两套环境，验证脚本未接进 CI）；安全连接层已落地，但业务数据层仍**尚未**接入 PostgreSQL 仓储层（详见第 5 节） |
| **S4：最小可传播演示** | 零密钥入库、检索、证据包导出与传播素材 | **技术链路完成，物料待补** | `bootstrap` → `demo` → `audit-bundle` 可跨进程运行并校验篡改；README hero GIF 仍需维护者录制与剪辑 |
| **S5：第 0 波封闭验证** | 由真实新用户验证首启成功率与首次价值时间 | **尚未开始** | 尚无 10–20 名真人封闭验证样本，不能对外宣称首次运行成功率或 TTFV 数值 |

## 2. 已完成项

### 合规、首启与离线可用

- Apache-2.0 许可证、NOTICE、DCO、行为准则、安全披露与 PR 模板已就位。
- `omnilore bootstrap`、`omnilore demo` 与模块调用入口可在零 API Key 环境运行。
- 缺少或失效的向量服务不会阻断首次检索；返回值会如实区分检索降级与答案合成降级。
- 文本写入统一为 UTF-8 + LF，Windows 上的哈希对账回归有自动化守卫。
- 镜像版本已固定，n8n 已移出默认启动路径；三份 n8n JSON 与内部 Worker 网关已有离线自动化覆盖。

### 笔记主权与安全边界

- 受保护 frontmatter 字段可收窄或整体关闭，`doc_id` 作为身份锚点始终受保护。
- 冲突副本默认写入 `.omnilore/conflicts/`，并允许配置其他 vault 内相对目录。
- 能力矩阵与威胁模型已公开；真实 PostgreSQL、内存仿真、外部模型与跨系统事务的边界均有明确说明。
- 21 条 RLS 策略具备逐条安全注释，策略语句有静态回归守卫。
- PostgreSQL **安全连接层**已落地：应用只用 `omnilore_app`（非超用户、无 `BYPASSRLS`）连接，
  事务内 `SET LOCAL ROLE` + 注入 RLS GUC，超用户连接拒绝启动；RLS 生效已在真实 PG 16.15 上
  以应用角色人工验证**两次**（2026-10-25 / 2026-10-26，两套独立环境，均为**非 CI**的一次性
  验证）。**业务数据层仍走内存引擎**，不因此宣称持久化。

### 可审计链路

- 原始字节使用 SHA-256 内容寻址存储。
- `audit-bundle` 会从磁盘真实字节重新计算哈希，不信任索引中的校验结果。
- 证据包已支持跨进程导出；篡改一个字节会得到 `mismatch` 与退出码 3，索引缺失时会保守失败而不是产出残缺包。
- 证据包不包含 token、token hash 或绝对路径。

### 可替换向量来源与平台一致性

- 向量来源支持 DashScope、OpenAI 兼容端点与显式关闭；OpenAI / Ollama 的真实端点尚未验证，因此状态仍为 `Implemented`。
- 模型名与向量维度环境变量已真正进入请求配置；维度不一致会响亮失败，不截断也不补零。
- 数据目录解析已经统一，CLI、MCP、运行时索引、引导状态与证据包基于同一个根目录。
- arm64 CI job 已配置，并于 2026-10-26 在 CI 上**首次绿灯**（Run #2，commit `5d513fe`）；但只有 1 次绿灯、样本尚少，仍标 `Implemented`，**不宣称 arm64 已验证**。

### 社区入口

- CONTRIBUTING 已提供六条绕开核心大文件的贡献路径。
- 六套结构化 Issue 模板、DCO 说明与自动标签分流已就位。
- 当前仍未建立 good first issue 库；该项属于首发后工作。

## 3. 进行中 / 待验证

| 项目 | 已完成到哪里 | 仍缺什么 |
|---|---|---|
| **真实 PostgreSQL 集成 CI** | `integration-rls` job、镜像、健康检查、空库守卫与退出码语义均已配置，**并已于 2026-10-26 真实运行、连续 2 次绿灯** | 该 job 只跑**独立 SQL 探针**，**不覆盖 CLI / MCP 业务运行时接线**；它不构成业务数据已走 PostgreSQL 的证据（见第 5 节） |
| **arm64 CI** | `ubuntu-24.04-arm` job 已配置全量离线测试、ruff 与零密钥冒烟，**并已于 2026-10-26 首次绿灯**（Run #2，commit `5d513fe`） | 只有 1 次绿灯、**样本尚少**，维持 `Implemented`；本项目对 CI 类条目升 `Tested-real` 的门槛是**连续 ≥2 次绿灯** |
| **windows-latest 离线矩阵（py3.11 / py3.12）** | 与 `ubuntu-latest` 跑同一套 151 个离线用例 + `ruff` | **根因已定位并修复，CI 复验未做**：Run #2（`5d513fe`）两个 job 红的原因是 **en-US runner 的 cp1252 代码页** —— stdout 被管道接管时 CPython 用 locale 编码，子进程打印中文即 `UnicodeEncodeError`（`PYTHONIOENCODING=cp1252` 可在本机复现，修复前 8 failures + 1 error）。已修：产品入口 `pin_utf8_std_streams()`、CI 开 `PYTHONUTF8=1`、测试钉住子进程 UTF-8，并新增 2 条代码页守卫（变异 2/2 变红）。**但尚未推送、尚未重新跑 CI**，所以在转绿之前，Windows 仍只能写「本地已验证」，**不得写成「CI 已验证」** |
| **PostgreSQL 应用运行时** | 安全连接层已落地：`omnilore/pg_runtime.py` 会读 `DATABASE_URL`，强制连接角色非超用户 / 无 `BYPASSRLS` / 是策略角色成员（否则拒绝启动），事务内 `SET LOCAL ROLE` + 注入 RLS GUC；RLS 生效已在真实 PG 16.15 上以应用角色人工验证一次 | **业务数据层仍未接入**：CLI / MCP 尚未根据 `DATABASE_URL` 实例化 `AsyncpgRepository`，业务读写仍走内存 `RLSDatabaseEngine`，容器内业务状态仍随进程结束而清空，**不能宣称持久化或多人共享**。数据面迁移（逐业务方法重写为 SQL）是独立的 4–6 人日工程，见第 5 节 |
| **macOS** | 代码以 Python 3.11+ 和跨平台路径规则为兼容目标 | 当前 CI 只有 Ubuntu / Windows，没有 macOS job，不宣称 macOS 已实跑 |
| **PDF 入库** | `pdfplumber` → `pypdf` 回退实现已存在 | 当前自动化套件没有构造或入库真实 PDF，因此“多格式入库”整体状态为 `Implemented`，不是 `Tested-real` |
| **n8n 工作流** | JSON 结构、无硬编码凭据与内部 Worker 网关已有离线用例 | 尚未在真实 n8n 实例导入、启动和运行，不标 `Tested-real` |
| **外部向量端点** | OpenAI 兼容请求体、配置、维度守卫与故障降级已有假 transport 用例 | 没有真实 OpenAI / Ollama 网络调用证据 |
| **传播素材** | 可审计演示链路和证据包输出已就位 | README hero GIF 尚未录制 |

## 4. 尚未开始

### 需要维护者完成

- **（已完成）** 推送并观察 `integration-rls` 与 arm64 job 的首次真实运行：两者均已在 Run #2
  （`5d513fe`）转绿 —— `integration-rls` 连续 2 次绿灯、arm64 首次绿灯。
- **（待复验）** `windows-latest` 上 py3.11 / py3.12 两个 job 的失败已定位并修复（根因=en-US
  runner 的 **cp1252 代码页**：stdout 走管道时子进程打印中文抛 `UnicodeEncodeError`），
  修复含产品入口 `pin_utf8_std_streams()`、CI 的 `PYTHONUTF8=1`、测试侧子进程 UTF-8 固定，
  以及 2 条守卫（变异 2/2 变红）。**但改动尚未推送、CI 尚未复跑** —— 推送后需确认两个 job
  转绿，在此之前 Windows 只能写“本地已验证”，不得写成“CI 已验证”。
- **（安全连接层部分已完成，剩余项如下）** 完成 PostgreSQL 运行时接线的**数据面迁移**并通过评审：把 `OmniLoreEngine` 的逐业务方法从内存 `RLSDatabaseEngine`（含直接访问 `self.db._state[...]` 的位置）重写为参数化 SQL，并在真实 PG 上用应用角色验证 RLS 不退化（见第 5 节）。安全模型设计（登录角色与最小权限、`SET LOCAL ROLE` 切换、请求上下文注入、fails closed 自检、「RLS 确实生效」的验证方式）**已完成并落地**，`omnilore_migrator` 无法降权的问题已用「应用只用 `omnilore_app` + `PgRuntime` 强制拒绝超用户」两重保证绕开。
- 录制基于真实 `bootstrap` → `demo` → `audit-bundle` 输出的 README hero GIF。
- 招募并执行 10–20 名真人的第 0 波封闭验证；主持人不应替参与者完成步骤。
- 完成 tag 前的诚实标注复核、手动 demo / 证据包冒烟与发布物料检查。

### 首发后

- 补齐完整审核链路演示、示例文件套装与更完整的演示物料。
- 建设最小文档站与中英双语入口。
- 建立 10–20 个可认领的 good first issue，并用真实认领情况评估贡献入口。

### Q2 / 条件触发

- 扩展 Policy-aware MCP 字段与工具，但必须先保持现有 7 个工具的兼容性。
- 补齐 Obsidian 治理桥接的信任徽章、笔记内审核与 dry-run 体验。
- 实现严格 opt-in、默认关闭且可自证 payload 的匿名遥测。
- 仅在外部贡献规模达到触发条件后拆分核心大文件，避免为尚不存在的协作规模过度重构。

## 5. PostgreSQL 运行时接线：安全模型设计已完成，数据面迁移仍是缺口

本节分两半：**5.1–5.3 的安全模型设计已经落地**（连接层、角色、GUC 注入、验证方式）；
**5.1 里剩下的业务数据层迁移仍是缺口**。公开发布前请不要把后者理解成“补一个 repository 实例化”。

### 5.1 现状

**已完成：安全连接层**
- `omnilore/pg_runtime.py` 的 `PgRuntime` 会读 `DATABASE_URL`，强制连接角色**非超级用户、无
  `BYPASSRLS`、是策略目标角色成员**，任一不满足即拒绝启动（fails closed）；事务内
  `SET LOCAL ROLE` 切到 `omnilore_gateway` / `omnilore_worker`，并以 `app.*` GUC 注入请求上下文，
  注入失败即中止事务。未设置 `DATABASE_URL` 时行为与之前完全一致（仍内存引擎）。
- 新建了安全应用角色 `omnilore_app`（`LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS`，
  授予两个策略角色的成员资格），绕开了“`omnilore_gateway` 是 `NOLOGIN` 无法登录”的坑。
- RLS 在这条连接上确实生效，已在真实 PostgreSQL 16.15 上以 `omnilore_app` 人工验证**两次**
  （2026-10-25 与 2026-10-26，两套独立环境）：P-a 未授权 0 行 / P-b 授权后可见（排除空表假阳性）
  / P-c 角色属性安全；以超级用户连接会被拒绝启动（fails closed 生效）。第二次复现同时核对了
  `sql/001` 建出的 **21 条策略**与 **7 张启用 RLS 的表**，并做了对照实验：本地 `bootstrap` +
  `demo` 跑通后，容器里只有探针行、没有业务数据 —— 这正是不宣称持久化的实证。
  两次均为人工一次性验证，**非 CI 持续证据**。
- ✅ **已修的配置隐患（配置层已改，容器实跑验收待做）**：`docker-compose.yml` 曾把
  `POSTGRES_USER` 设为 `omnilore_migrator`，与 `sql/001` 里 `CREATE ROLE omnilore_migrator NOLOGIN`
  **同名**。initdb 会先把它建成可登录的超级用户，而 `sql/001` 的 `DO $$ IF NOT EXISTS $$`
  块随后**跳过创建**——结果是这个本应 `NOLOGIN` 的迁移/属主角色变成了一个**可登录的超级用户**。
  现在 compose 与 `.env.example` 的 `POSTGRES_USER` 已改为 `postgres`（与 CI 的 `integration-rls`
  一致），`omnilore_migrator` 只由 `sql/001` 按 `NOLOGIN` 创建；`sql/001` 另加 **fail closed**
  检查（发现该角色可登录或为超用户即 `RAISE EXCEPTION`），并有守卫测试锁住「compose 值 == CI 值」
  「`.env.example` 不漂移」「不与任何 `NOLOGIN` 角色同名」三件事。
  **边界**：该改动**未在真实 docker 环境实跑过**（本仓库验证环境无 docker），
  因此「角色确为 `NOLOGIN` 且非超用户」「21 条策略仍建出」「探针仍 3 条 PASS」三条均**未取证**；
  已有旧数据卷**不会**被自动修正（旧卷里该角色仍是超用户），需 `docker compose down -v` 重建。

**仍是缺口：业务数据层**
- `OmniLoreEngine`、CLI 与 MCP 服务端始终使用内存仿真引擎；业务读写**没有**走 `AsyncpgRepository`，
  容器里的业务状态仍随进程结束而清空。
- 原因不是“忘了实例化”，而是**无法无缝切换**：engine 的查询谓词是任意 Python lambda
  （`select_rows(table, predicate)`），且有多处直接访问 `self.db._state[...]`，
  必须**逐业务方法重写**为参数化 SQL —— 那是独立的 4–6 人日工程，尚未做。
- 已知事实：**在既有旧数据卷里**，`omnilore_migrator` 是 docker 镜像的 bootstrap 超级用户，
  **无法降权**（`ALTER ROLE ... NOSUPERUSER` 报 bootstrap user 必须保留 SUPERUSER）。
  T4 修法之后新建的卷不再由 initdb 创建该角色（启动超用户改为 `postgres`），
  它只是 `sql/001` 建的 `NOLOGIN` 角色。无论新旧卷，安全都依靠两重保证：
  ① 应用一律用 `omnilore_app` 连接；② `PgRuntime` 强制拒绝超用户 / `BYPASSRLS` 连接角色。

### 5.2 风险：接错了比没接更危险

PostgreSQL 中**超级用户与 `BYPASSRLS` 角色默认绕过行级安全策略**。如果应用最终以这类角色连上数据库，会出现这样的结果：

- 应用连上 PostgreSQL、读写正常，看起来“接通成功”；
- 但 21 条策略根本不会被应用到业务请求上，跨空间、跨类别隔离形同虚设；
- 对外宣称的「RLS 提供命令级权限」在运行时是假的，而且**比当前未接通更难发现**——未接通时至少状态可查，错误接通会制造虚假安全感。

因此「连接成功」不能作为验收标准，验收必须落在**权限确实生效**上。

### 5.3 安全模型设计项（1–4、6 已落地；5 仍待数据面迁移时一并处理）

1. ✅ **登录角色与权限模型**：应用使用 `omnilore_app`（`NOSUPERUSER` / `NOBYPASSRLS`），
   通过 `omnilore_gateway` / `omnilore_worker` 的成员资格获得表级权限；
   `PgRuntime._assert_safe_role` 在**每次连接后**校验 `rolsuper` / `rolbypassrls` / 成员资格。
2. ✅ **`SET ROLE` 策略**：连接角色保持 `omnilore_app`；进入事务后先清空 `app.*` GUC，
   再 `SET LOCAL ROLE` 到策略目标角色，最后注入 GUC —— 顺序固定，避免残留上一次请求的上下文。
3. ✅ **请求上下文传递**：`app.current_user_id` / `allowed_read_spaces` / `allowed_read_scopes` /
   `allowed_write_spaces` / `allowed_write_scopes` 五个 GUC 在事务内注入，注入失败即中止事务
   （默认拒绝，不放行）。这五个值**目前还没有**与 engine 已解析的 `ResolvedScope` 打通 ——
   因为业务读写尚未走这条连接。
4. ✅ **凭据与密码管理**：口令只走环境变量 / gitignored 的 `.env`，不写进公开模板；
   连接串由 `DATABASE_URL` 提供。
5. ⏳ **迁移与初始化边界**：容器 initdb 已应用 schema，应用启动时不得重复建表，
   同时不得依赖自动 `DROP SCHEMA` —— 当前连接层不建表也不改 schema，
   但数据面迁移完成前这条无法端到端验证。
6. ✅ **验证方式**：`tools/verify_rls_app_role.py` 用**应用角色**在真实 PG 上跑负向 + 正向 + 角色属性
   三条探针（P-a / P-b / P-c），证明「RLS 真的生效」而不是「连接成功」；
   超用户连接被拒也已实测。**业务链路级断言仍缺** —— 那要等业务数据层真的走 PG。

### 5.4 明确结论

安全模型设计**已经完成并落地**，但它只覆盖了「连接」这一段。**业务数据层的 SQL 迁移是独立的
大型工程，尚未做**。因此本仓库当前对外口径保持为：

> **设置 `DATABASE_URL` 时，应用会以安全角色连接真实 PostgreSQL 并做启动自检，且 RLS 在这条
> 连接上生效已在真实 PG 上人工验证过一次；但业务读写仍走内存 `RLSDatabaseEngine`，
> 结构化状态不会持久化到 PostgreSQL —— 任何「容器模式 = 真实 PostgreSQL 持久化引擎」
> 或「可多人共享」的表述都是虚标。**

---

## 6. Go / No-Go 门禁

| 门禁 | 状态 | 判定 |
|---|---|---|
| **G0：首启可达** | **已通过** | 当前仓库全量测试 151 项通过、0 failure、0 error、0 skip；ruff 通过；零密钥 `bootstrap` → `demo` → `audit-bundle` 跨进程链路退出码为 0 |
| **G2：打 tag 前复核** | **待通过** | 必须再次核对 README / 能力矩阵 / CHANGELOG 的诚实口径，并由维护者手动执行 demo 与证据包冒烟；**从未真实运行过的 CI job 不能写成已验证**（`integration-rls` 与 arm64 现已真实运行，可按实际绿灯次数陈述）。`windows-latest` 的失败根因已定位并修复，但**改动尚未推送、CI 尚未复跑**，因此仍不得宣称 Windows CI 已绿 —— 必须等推送后两个 job 真实转绿 |
| **G3：公开首发** | **待通过** | 只有第 0 波封闭验证达到方向性门槛且发布物料就绪后才可公开首发；当前没有真人样本，不能以仓库测试代替用户验证 |

## 7. 诚实口径

- 当前能力矩阵共 **32 条**：`Implemented` 10 条、`Tested-real` 9 条、`Simulated` 12 条、`Planned` 1 条。
- 151 项离线测试主要证明内存仿真路径、真实文件系统、真实子进程和纯函数逻辑；其中新增的 5 项 PG 运行时接线用例也是离线 mock。它们不等于真实 PostgreSQL 应用链路证据。
- 真实 PostgreSQL 的现有证据是**两条三探针记录，性质不同**：① 2026-10-24 策略挂载、未授权跨空间不可见、授权后可见 —— **已由 CI 的 `integration-rls` job 持续验证**（自 2026-10-26 起连续 2 次绿灯）；② 2026-10-25 / 2026-10-26 以应用角色走运行时连接层的未授权 0 行 / 授权后可见 / 连接角色非超用户无 `BYPASSRLS` —— **仍是人工一次性、非 CI**。两者都不覆盖 profile、写侧策略、组织树继承、撤销链路，也都**不覆盖业务读写到 PG 的数据面**；① 有 CI 绿灯也**不等于**业务运行时已接入 PostgreSQL。
- 当前**仍然**没有任何证据表明业务请求曾经经过 PostgreSQL：安全连接层只做连接与自检，业务数据仍走内存引擎。若未来以超级用户身份接通，`PgRuntime` 会直接拒绝启动（fails closed），因为那比未接通更危险（见第 5 节）。
- `integration-rls` 已于 2026-10-26 **连续 2 次真实绿灯**（Run #1 `8d0ecc3`、Run #2 `5d513fe`），arm64 **首次绿灯**（Run #2），二者都已不再只是“配置已就位”。但绿灯只覆盖它们各自跑的那部分：前者只跑独立 SQL 探针，后者只跑离线套件；**都不覆盖业务数据面**。`windows-latest` 的 py3.11 / py3.12 两个 job 在 Run #2 仍红；其根因（en-US runner 的 cp1252 代码页）已定位并修复，**但尚未推送复跑**，故当前仍按「未通过」计（详见第 3 节的 `windows-latest` 行）。
- 第 0 波封闭验证尚未开始，样本量为 0。即使未来只完成 10–20 人验证，也只能作为 Go/No-Go 的方向性依据，不能据此发布具有统计置信度的成功率或 TTFV 宣称。
- PDF、真实 n8n、真实 OpenAI / Ollama 与 macOS 均缺少对应的真实运行证据；各自状态不得越级。

**我们公开边界，因为我们做安全。**
