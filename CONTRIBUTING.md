# 参与贡献 CONTRIBUTING

感谢你愿意让 OmniLore 变得更好。请先花 2 分钟读完本文——尤其是
[第 3 节：六条贡献路径](#3-六条贡献路径不必读懂-enginepy)。

> **当前状态：** `v0.3.0-alpha`（内部代际代号 v2.3）。alpha 阶段，
> 未达到生产就绪，接口可能在 minor 版本内调整。

---

## 1. 先跑起来（零 API Key，60 秒）

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e .
omnilore bootstrap     # 创建数据目录、默认空间与 owner token
omnilore demo          # 投一份文档，再把它搜出来
```

跑测试：

```bash
python -m unittest discover -s tests -p "test_*.py" -v
```

> **说明：** 在 Windows 等无符号链接权限的宿主上，2 个 AC-S05 断言会被
> **显式 skip**（`tests/_platform_support.py` 给出的原因）。这是宿主能力限制，
> 不是平台缺陷；Linux / macOS CI 上这些断言完整执行。请勿用
> `try/except: pass` 去“修”它们——那会掩盖真实缺陷。

---

## 2. 开发者原创证书 (DCO)

本项目采用 **DCO（Developer Certificate of Origin）**。你无需签署纸质协议，
只需在每个提交的 commit message 末尾追加一行 `Signed-off-by`，即表示你声明：

> 我有权以本项目采用的开源许可证提交这些改动，且提交内容符合 DCO 1.1 的
> (a)~(d) 条款。

**最简单的方式：`git commit -s`**（自动追加 `Signed-off-by: Your Name <you@example.com>`）。

```bash
git commit -s -m "fix(storage): 统一 LF 写入消除 Windows 对账不一致"
```

补签已有提交：

```bash
git rebase --signoff <base-branch>       # 为多个提交批量补签
git commit --amend -s                    # 为最后一个提交补签
```

CI 会校验变更集内每个提交的 `Signed-off-by`。校验失败时按上面的命令补签后 force-push
即可（仅限你自己的分支）。

### 首次公开推送前：请补签历史提交

仓库内的**历史提交早于 DCO 决策，没有 `Signed-off-by`**（例如初始导入提交）。
CI 的 DCO job 在**首次推送**（GitHub 给出的 `before` 为空或全零）时没有可靠的 base，
会降级为 **warning 不阻断**，因此它不会拦住你 —— 但这不代表历史提交合规。

在把仓库**首次公开推送**之前，请由维护者先补签历史提交：

```bash
git rebase --signoff --root          # 从根提交开始批量补签（会重写历史）
# 或只补最后一个：
git commit --amend -s --no-edit
```

> ⚠️ 重写历史会影响所有协作者，请在公开推送前、且仅在你自己的仓库上执行。

---

## 3. 六条贡献路径（不必读懂 `engine.py`）

`omnilore/engine.py` 有 3000+ 行，是全库最大的文件，也是新贡献者最大的心理门槛。
**以下六条路径刻意绕开了它**，任选其一即可完成一次有价值的贡献。

| # | 路径 | 你只需要碰这些文件 | 典型任务 | 上手成本 |
|---|---|---|---|---|
| 1 | **SQL / 迁移脚本** | `sql/*.sql` | 补 `INSERT`/索引/RLS 策略、写 `003_*.sql` 新迁移 | ⭐ |
| 2 | **n8n 工作流** | `n8n/workflows/*.json` | 新增导入即用工作流、修 JSON 里的 HTTP 节点 | ⭐ |
| 3 | **解析器（纯函数）** | `omnilore/parsers.py`、`tests/` | 新增格式支持、改进代码符号/健康指标提取 | ⭐⭐ |
| 4 | **测试用例** | `tests/*.py`、`tests/_platform_support.py` | 补边界用例、补负向安全用例、改进 skip 文案 | ⭐⭐ |
| 5 | **文档与示例** | `README.md`、`docs/*.md`、`CHANGELOG.md` | 校订事实、补 FR 追踪、写场景手册 | ⭐ |
| 6 | **CLI 与首次使用体验** | `omnilore/cli.py`、`omnilore/bootstrap.py` | 加子命令、改进输出、改零密钥引导流程 | ⭐⭐ |

### 路径细则

1. **SQL / 迁移脚本**：新增文件请按 `NNN_<主题>.sql` 命名并保证幂等
   （`IF NOT EXISTS` / `ON CONFLICT DO NOTHING`）。参考 `sql/002_omnilore_bootstrap_seed.sql`。
2. **n8n 工作流**：JSON 需能被直接导入；不要把账号名、token 或内网地址写进文件。
3. **解析器**：`parsers.py` 里的函数大多是**无状态纯函数**（输入字节/文本，输出结构化
   结果），可以独立阅读与测试，不用理解权限或事务。请为新增格式配套一个测试。
4. **测试用例**：优先补**负向安全用例**（越权、路径穿越、幂等重放）。新增 skip 必须带
   明确的、可被复核的原因。
5. **文档**：如果你发现 README 与代码矛盾，欢迎直接修——但请在描述里注明
   “文档事实修正”，不要顺带改动代码。
6. **CLI**：CLI 必须同时支持 `omnilore <cmd>` 与 `python -m omnilore <cmd>`
   两种调用方式；新增子命令请同步更新 README 首屏与 `CHANGELOG.md`。

> **如果你确实要改 `engine.py`**：请先开 Issue 说明动机。架构级重构已排期在 Q2 之后，
> 我们不希望在 alpha 阶段引入难以评审的大范围重写。

### 路径 6 之外还有一条：验证命令（`Makefile`）

`Makefile` 里的每个 `verify-*` 目标对应 [`docs/FEATURE_STATUS.md`](docs/FEATURE_STATUS.md)
里的一组能力标注。新增能力时请顺带确认：既有目标能不能覆盖它，不能就补一个目标。
没有 `make` 的环境（原生 Windows）直接照抄目标里 `echo` 出来的命令即可。

---

## 3.1 提 Issue：请用模板，且模板是「可统计」的

`.github/ISSUE_TEMPLATE/` 下有 6 套表单：

| 模板 | 用途 | 自动标签 |
|---|---|---|
| `install-failure.yml` | 装不上 / 60 秒验证没跑通（**首启成功率的唯一数据源**，含「卡住多久？」） | `install-failure` |
| `install-escape.yml` | 「我放弃了」逃生通道 | `install-failure` + `P0-escape` |
| `retrieval-quality.yml` | 召回 / 排序 / 答案不符；选项含「疑似越权」 | `retrieval-quality`（+`security-triage`） |
| `bug-report.yml` | 行为与文档不符 | `bug` |
| `feature-request.yml` | 新能力建议（按 persona 统计分布） | `enhancement` |
| `contribution-proposal.yml` | 想动手，先选路径 | `contribution` |

两条设计约束，改模板时请遵守：

1. **下拉单选优先于自由文本** —— 自由文本无法做分布统计，而我们要的是分布。
   新增问题请先问自己「能不能变成 3~7 个选项」。
2. **升级标签必须自动打** —— GitHub 表单不支持「按选项打标签」，因此映射放在
   `.github/workflows/triage.yml`（「我已放弃」→ `P0-escape`；「疑似越权」→ `security-triage`）。
   新增升级标记时，两处一起改。

**安全漏洞不走 Issue 模板**，走 GitHub Security Advisory（见 `SECURITY.md`）。

---

## 3.2 诚实标注：改了能力就要改三处

本项目的头号风险是「宣称与证据不匹配」。因此任何能力的新增或变更，都要同步三处：

1. `README.md`「核心能力」小节的**四态标注**（`Implemented` / `Tested-real` / `Simulated` / `Planned`）；
2. [`docs/FEATURE_STATUS.md`](docs/FEATURE_STATUS.md) 的矩阵行（含验证命令）；
3. `CHANGELOG.md` 的统计数字。

三处不一致会直接红：`make verify-honesty`（或 `python -m unittest tests.test_w2_w3_roadmap -v`）。

**判定口径**：只要验证跑在内存仿真引擎（`RLSDatabaseEngine`）上、真实外部依赖没有 CI 证据，
就标 `Simulated` —— **不许**标 `Tested-real`。宁可降级标注，也不虚标。

---

## 4. Pull Request 检查清单

提交 PR 前请确认：

- [ ] 每个 commit 都带 `Signed-off-by`（`git commit -s`）
- [ ] `python -m unittest discover -s tests -p "test_*.py" -v` 全绿（或 skip 有明确原因）
- [ ] 所有**文件落地写入**都走 `write_text_canonical`，读取走 `read_text_canonical`
      （禁止 `write_text(..., encoding="utf-8")` 不带 `newline=''`——会把 Windows 上的
      `file_hash` 对账搞坏，见 CHANGELOG 的 CRLF 条目）
- [ ] 没有引入新的网络依赖到“零 API Key”首次运行路径
- [ ] 涉及输出时用简体中文，且不含 `production` / `stable` / `企业就绪` 类措辞
- [ ] 新增/变更了能力宣称 → 已同步 README 四态标注、`docs/FEATURE_STATUS.md`、`CHANGELOG.md`
      三处，并跑过 `make verify-honesty`
- [ ] 新增了环境变量 → 已同步 `.env.example` 并标注**必填 / 可选**
- [ ] 版本号如需变更，同步更新 `pyproject.toml`、`omnilore/__init__.py`、
      `CHANGELOG.md` 三处

---

## 5. 编码约定

- 目标 Python **3.11+**；`from __future__ import annotations` 保持开启；
- 类型标注齐全，公共函数写 docstring（Google 风格）；
- 行宽上限 120，使用 `ruff`（`pip install -e ".[dev]"` 后运行 `ruff check .`）；
- 异常用 `OmniLoreError(code, message)`，不要用裸 `except: pass`；
- 新增环境变量必须同步到 `.env.example` 并标注**必填 / 可选**。

---

## 6. 评审响应 SLA

- **Issue 首响 ≤ 48 小时**
- **PR 首响 ≤ 48 小时**

若超过时限没有收到回复，欢迎在原线程礼貌地 `@` 一次维护者。

---

## 7. 商标说明

`OmniLore` 名称与标识不在 Apache-2.0 授权范围内。详见 `NOTICE` 的商标政策说明。
