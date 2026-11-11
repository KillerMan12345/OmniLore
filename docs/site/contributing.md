# 贡献指南

> 参与前请先读 [`CONTRIBUTING.md`](../../CONTRIBUTING.md)（DCO 与许可）、
> [`docs/HANDOVER.md`](../HANDOVER.md)（红线与剩余任务）和
> [`docs/FEATURE_STATUS.md`](../FEATURE_STATUS.md)（四态口径）。

## 开发环境

```bash
pip install -e .
make test
make lint
```

所有 commit 必须带 DCO 签名：

```bash
git commit -s -m "fix: ..."
```

## 从哪里入手

| 难度 | 方向 | 说明 |
|---|---|---|
| 低 | `omnilore/parsers.py` | 纯函数，输入输出明确，适合第一个 PR |
| 低 | `docs/site/` 文档站 | 发现术语不一致、链接错误、边界没讲清楚 |
| 中 | `tests/` 新增离线用例 | 项目对「守卫必须有效」要求很高，见下 |
| 中 | `omnilore/context_budget.py` / `citation.py` | 可独立测试的纯逻辑模块 |
| 高 | `omnilore/engine.py` | 全库最大文件，改动前先读 HANDOVER |

## 改代码时必须同时做的事

1. **改能力 → 同时改三处**：README 宣称 / `docs/FEATURE_STATUS.md` 矩阵 / `CHANGELOG.md`。
   矩阵与 README 的能力条目必须 1:1（有守卫）。
2. **改测试数量 → 六处数字同步**：README / `docs/FEATURE_STATUS.md` /
   `docs/ROADMAP_STATUS.md` / `docs/HANDOVER.md` / `CHANGELOG.md` /
   `.github/workflows/ci.yml`。组成拆分之和必须等于总数。
3. **新增守卫 → 必须做变异验证**：故意把实现改坏，守卫必须变红；把结果写进 PR 描述。
   空转守卫比没有守卫更危险。
4. **LF 与 UTF-8**：全仓文本 LF-only；用 `write_text_canonical` 写文件，别用
   `Path.write_text()`（Windows 会悄悄写 CRLF）。
5. **CHANGELOG 只有一个版本段** `[v0.3.0-alpha]`。不新建 `[Unreleased]`，
   不写迭代计数、缺陷编号、分轮标记。

## 诚实标注规则

- 禁止 `production` / `stable` / `企业就绪` 一类措辞；首发一律 `v0.3.0-alpha`。
- 真实 PG RLS 有 CI 证据的**只有** 3 条探针覆盖范围，其余一律 `Simulated`。
- CI 类条目升 `Tested-real` 的门槛是**连续 ≥2 次绿灯**，单次绿灯只是「恰好没红」。
- 任何「CI 已验证」的表述都要说明覆盖了什么、没覆盖什么。
- 平台专属命令（`icacls` / `whoami` / `mklink` / `psql`）必须**先判平台再调用**，
  不能「先调用再按 `os.name` 分支」。
- `tools/mutation_check.py` 只能在副本上跑，历史上它改坏过活仓库。

## 提交 PR 前的自查

```bash
make verify
make verify-honesty
make verify-lf
```

再回答三个问题：

1. 我宣称的能力，证据在哪里？四态标对了吗？
2. 我新增的守卫，改坏实现后真的会红吗？
3. 我有没有把「内存仿真引擎上跑通」写成「真实 PostgreSQL 已验证」？

## 报告安全问题

请走仓库的 **Security Advisory**（私密披露），不要开公开 Issue。
信任边界与已知「防不住什么」见 [`docs/THREAT_MODEL.md`](../THREAT_MODEL.md)。
