# OmniLore v2.3 用户故事与验收场景手册（User Stories）

> **文档编号：** CH-US-2026-v2.3  
> **更新日期：** 2026-10-22  
> **覆盖范围：** 个人知识管理者（Alice）、家庭协作者（Bob/Dave）、企业研发成员（Charlie）、系统运维管理员四大角色，共 16 个端到端用户故事，100% 映射已实现代码与自动化测试

## 1. 角色画像（Personas）与令牌配置

| 角色代号 | 角色身份 | 所持令牌 Profile | 授权空间与角色 | 允许类别（Category） |
|---|---|---|---|---|
| **Alice（办公态）** | 个人知识库所有者（在公司 IDE 使用） | `WORK` (`tok_alice_work_secret_001`) | `sp_personal_alice` (`ADMIN`), `sp_org_root` (`ADMIN`), `sp_org_eng` (`ADMIN`) | 仅 `01_Work`（硬性隔离个人生活、家庭与健康） |
| **Alice（全能态）** | 个人知识库所有者（在个人可信电脑使用） | `OMNI` (`tok_alice_omni_secret_002`) | `sp_personal_alice` (`ADMIN`), `sp_family_home` (`ADMIN`), `sp_org_*` (`ADMIN`) | `01_Work`, `02_Life`, `03_Family`, `04_Health_Archive` |
| **Bob / Dave** | 家庭共享空间成员（配偶/长辈） | `FAMILY` (`tok_bob_family_secret_003`) | `sp_family_home` (`READER`) | 仅 `03_Family`, `04_Health_Archive` |
| **Charlie** | 企业后端研发工程师 | `WORK` (`tok_charlie_work_secret_004`) | `sp_org_backend` (`WRITER`, 开启 `inherit_org_public_read=True`) | `01_Work`（自动向上继承读取 `sp_org_eng` 与 `sp_org_root`） |

## 2. Epic 1：Phase 1 个人知识入库、检索与版本演进（US-01 ~ US-06）

- **US-01（多格式资料入库与 10 次幂等重放）：** 作为个人知识管理者（Alice），我希望通过 MCP 工具投递 Markdown、PDF、PNG 架构图或对话总结笔记，且在网络超时重复提交或文件被改名重复投递时不会产生重复文档或重复台账。验证结果：相同内容改名连续重放 10 次仅生成 1 条文档与 1 条台账记录（`test_03` & `test_ac_d01_d02`）。
- **US-02（混合信号与跨边界内容自动隔离复核）：** 作为个人知识管理者（Alice），当随手投递（`target_space_id="AUTO"`）的笔记中同时混杂内部 API 规范和个人体检血糖数据时，系统自动转入 `NEEDS_REVIEW` 待确认队列且对常规检索不可见（`test_04` & `test_ac_k06_k07`）。
- **US-03（同一空间下 Work Profile 与家庭/健康类别的严格隔离）：** 作为在办公 IDE 中使用 `WORK` 令牌的开发者（Alice），即使个人空间里存有家庭保单和体检报告，`WORK` 令牌搜索或通过 `doc_id` 读取 `03_Family`/`04_Health_Archive` 均返回 0 结果或抛出 `PermissionDeniedError`（`test_01` & `test_ac_s01_s02_s03`）。
- **US-04（逻辑文档系列零停机版本替代、增量补充与一键回滚）：** 作为技术负责人（Alice），当网关鉴权规范从 `v1.0` 升级到 `v2.0` 或挂载 `SUPPLEMENT_DOC` 补充说明时，默认搜索仅召回 `v2.0` 及其有效补充页（旧版本误召回为 0），并发冲突时抛出 `VersionConflictError`，且支持通过 `REVERT_VERSION` 一键回滚至 `v1.0`（`test_03` & `test_ac_d03_k04`）。
- **US-05（按代码符号精确关联检索）：** 作为研发人员（Alice），入库含 Python 代码块或接口定义的 ADR 文档时，系统通过 `ast` 提取类名/函数名（如 `PaymentCallbackController`），支持按 `code_symbol` 精确关联检索（`test_05`）。
- **US-06（人工审核批准、重分类与多意图文档拆分）：** 作为知识库维护者（Alice），支持通过 `review_and_fix_knowledge` 执行 `APPROVE_REVIEW`（批准入库）、`RECLASSIFY`（跨类别/跨空间迁移文档并同步更新切片）与 `SPLIT_DOCUMENT`（将混合任务拆分为多篇独立文档）（`test_04`）。

## 3. Epic 2：Obsidian 本地双向同步与空间控制台（US-07 ~ US-09）

- **US-07（Obsidian 本地修改正文自动同步与防死循环）：** 系统写回文件时记录 `_last_synced_hash_by_doc`，文件监听器再次扫描到相同哈希时返回 `SKIPPED_SELF_WRITE_NOOP`；用户修改正文后自动递增 `content_revision` 并重建向量切片（`test_ac_d06`）。
- **US-08（Frontmatter 受保护字段防篡改与并发冲突副本保护）：** 用户在本地手改 `space_id`、`series_id` 或 `version_key` 时被拦截并返回 `REJECTED_PROTECTED_FIELD_MODIFICATION`；多设备基于过期 `content_revision` 修改时自动生成 `.conflict_r*.md` 冲突副本而不覆盖原文件（`test_ac_d06`）。
- **US-09（生成空间级 Obsidian 治理控制台）：** 调用 `generate_obsidian_dashboard` 自动生成 `_dashboard_控制台.md`，包含空间概览、`NEEDS_REVIEW` 队列、失败任务、当前有效版本表、近期审计与备份恢复演练状态 6 大视图（`test_05`）。

## 4. Epic 3：Phase 2 家庭协同、健康档案与知识 Lint（US-10 ~ US-12）

- **US-10（家庭空间成员邀请、角色分配与即时撤权）：** 家庭管理员邀请成员加入 `sp_family_home` 并分配角色；调用 `remove_family_member` 撤权后主动清除授权缓存，后续请求立即被拒绝（`test_06` & `test_ac_s06_s07`）。
- **US-11（体检报告结构化指标提取、风险提示与人工核验闭环）：** 上传体检报告自动提取空腹血糖、收缩压等指标，初始状态强制为 `NEEDS_CHECK` 并在查询时展示 `"AI 提取，尚未核验"`；经人工 `FIX_EXTRACTED_FIELD` 核验后转为 `"人工已核验"` 并记录 `revision_history`（`test_06`）。
- **US-12（TTL 过期文档、孤儿链接与未核验指标的只读 Lint 巡检）：** `run_knowledge_lint` 自动检测已超过 `ttl_expires_at` 的文档、悬空 `[[双链]]` 与未核验健康指标，输出只读建议清单而不静默改写文档（`test_06`）。

## 5. Epic 4：Phase 3 企业多级组织树与运维保障（US-13 ~ US-16）

- **US-13（企业多级组织树 `ltree` 上级公共空间只读继承）：** 下级空间 `sp_org_backend`（`org.eng.backend`）成员开启 `inherit_org_public_read` 后自动获得上级 `sp_org_eng`（`org.eng`）与 `sp_org_root`（`org`）的只读权限，但绝不继承写权限，且无法访问 `sp_org_secret`（`ORG_RESTRICTED`）（`test_07`）。
- **US-14（个人工作草稿脱敏预览并晋升发布至团队空间）：** `PROMOTE_COPY` 支持 `preview_only=True` 与 `apply_redaction=True`，自动将手机号与 `sk-` 密钥脱敏为 `[REDACTED_PHONE]` / `[REDACTED_SECRET]`，经 `explicit_user_confirm=True` 确认后创建目标空间副本并建立 `PROMOTED_FROM` 关联（`test_07`）。
- **US-15（进程崩溃后的 Outbox 自动恢复与一致性对账）：** 在 `STAGED` 阶段注入崩溃异常后，未完成文档对检索不可见；运行 `OutboxRecoveryDaemon` 后自动恢复至 `COMMITTED`，且 `reconcile_consistency()` 验证 0 幽灵记录、0 孤儿文件（`test_ac_d04_d05` & `test_08`）。
- **US-16（全量备份快照、SHA-256 校验恢复与向量索引跨版本重建）：** 支持一键创建带 `manifest.json` 的快照备份、升级重建向量索引（`index_version = 2`），并在恢复时逐文件校验 SHA-256（`test_08`）。
