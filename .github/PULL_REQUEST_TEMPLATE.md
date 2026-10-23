# Pull Request

> 提交前请先阅读 [CONTRIBUTING.md](../CONTRIBUTING.md)。
> 当前版本：`v0.3.0-alpha`（内部代际代号 v2.3，PEP 440：`0.3.0a0`）。

## 1. 变更类型

- [ ] Bug 修复
- [ ] 新功能
- [ ] 文档 / 事实修正
- [ ] 测试补充
- [ ] SQL / 迁移脚本
- [ ] 配置项（docker-compose / .env.example / CI）
- [ ] 其他：

## 2. DCO（必填）

- [ ] **我已在每一个 commit 上签署 DCO**，即 commit message 含
      `Signed-off-by: Your Name <you@example.com>`（使用 `git commit -s` 自动生成）。
      我声明我有权利以本项目的 Apache-2.0 许可证提交这些改动，且内容符合
      [DCO 1.1](https://developercertificate.org/) 的 (a)~(d) 条款。

补签命令备忘：

```bash
git commit -s                      # 新提交
git commit --amend -s              # 给最后一个提交补签
git rebase --signoff main          # 给整条分支批量补签
```

## 3. 自检清单

- [ ] `python -m unittest discover -s tests -p "test_*.py" -v` 全绿
      （如有 skip，请在下方写明**原因**，不允许静默吞掉失败）
- [ ] 所有磁盘写入走 `write_text_canonical`，读取走 `read_text_canonical`
      **（禁止 `write_text(..., encoding="utf-8")` 不带 `newline=''`）**
- [ ] 零 API Key 路径未被破坏：`omnilore bootstrap && omnilore demo` 仍全程跑通
- [ ] README / CHANGELOG / 版本号（如需）已同步更新
- [ ] 未引入 `production` / `stable` / `企业就绪` 等不符合 alpha 阶段的措辞

## 4. 关联 Issue

Closes #

## 5. 描述

<!-- 做了什么、为什么做、怎么验证。请把验证命令与关键输出贴在这里。 -->

## 6. 截图或输出（可选）

```text
（粘贴 `omnilore demo` 或测试的关键输出）
```

## 7. 我改了 `engine.py` 吗？

- [ ] 没有 —— 走的是 CONTRIBUTING 第 3 节的六条绕核心路径之一（请注明第 __ 条）
- [ ] 改了 —— 已在描述中说明动机，且是最小改动（架构级重构请改投 Issue）
