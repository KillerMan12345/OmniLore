# OmniLore — 可复核的验证入口
# =============================================================================
# 为什么有这个文件：[docs/FEATURE_STATUS.md](docs/FEATURE_STATUS.md) 里每一条
# `Simulated` 标注都必须挂一条「用户可以自己复跑」的验证命令。把命令集中在这里，
# 是为了让「宣称 → 证据」之间只隔一条命令，而不是隔一段口头承诺。
#
# 全部 verify-* 目标**离线可跑**（不需要数据库、不需要 API Key），
# 唯一例外是 `verify-rls-pg`，它需要真实 PostgreSQL，且环境不具备时会**非零退出**。
#
# 没有 make 的环境（例如原生 Windows）：每个目标下面的 echo 都会打印出等价命令，
# 直接复制执行即可。
# =============================================================================

PY ?= python
RUFF ?= $(PY) -m ruff

.DEFAULT_GOAL := help
.PHONY: help verify test verify-rls verify-rls-pg verify-sync verify-paths \
        verify-parsers verify-redaction verify-ops verify-launch verify-mcp \
        verify-degradation verify-lf verify-search verify-honesty lint

help: ## 列出所有验证入口
	@echo "OmniLore 验证入口（全部离线，除 verify-rls-pg）"
	@echo "  make verify            全量测试（离线）"
	@echo "  make verify-rls        权限/RLS/版本/同步（内存仿真引擎：只证明逻辑）"
	@echo "  make verify-rls-pg     真实 PostgreSQL RLS 探针（需 psql + DATABASE_URL，缺失即失败）"
	@echo "  make verify-sync       Obsidian 同步：受保护字段可配 + 冲突副本隐藏目录"
	@echo "  make verify-paths      文件网关：路径穿越 / 符号链接防御"
	@echo "  make verify-parsers    多格式入库与代码符号提取"
	@echo "  make verify-redaction  跨空间晋升脱敏"
	@echo "  make verify-ops        备份 / 恢复 / 索引重建 / 一致性对账"
	@echo "  make verify-launch     60 秒验证：bootstrap + demo 端到端"
	@echo "  make verify-mcp        MCP stdio 握手（真实管道）"
	@echo "  make verify-degradation 零密钥降级与正交上报"
	@echo "  make verify-search      检索路径（注意：hybrid 在 CI 里是桩，无真实网络调用）"
	@echo "  make verify-lf         跨平台 LF 规范化"
	@echo "  make verify-honesty    诚实标注守卫：README <-> 状态矩阵 <-> CHANGELOG"
	@echo "  make lint              ruff 静态检查"

verify: test ## 全量测试
test:
	@echo ">> $(PY) -m unittest discover -s tests -p \"test_*.py\" -v"
	@$(PY) -m unittest discover -s tests -p "test_*.py" -v

# ---------------------------------------------------------------------------
# 权限 / RLS / 版本 / 同步 —— 全部跑在内存仿真引擎上（Simulated）
# ---------------------------------------------------------------------------
verify-rls:
	@echo ">> 内存仿真引擎（RLSDatabaseEngine），非真实 PostgreSQL —— 结果只证明逻辑正确性"
	@echo ">> $(PY) -m unittest tests.test_omnilore_phase0_acceptance -v"
	@$(PY) -m unittest tests.test_omnilore_phase0_acceptance -v

verify-sync:
	@echo ">> $(PY) -m unittest tests.test_omnilore_phase0_acceptance.TestOmniLorePhase0Acceptance.test_ac_d06_obsidian_sync_loop_guard_protected_fields_and_conflict_copy -v"
	@$(PY) -m unittest tests.test_omnilore_phase0_acceptance.TestOmniLorePhase0Acceptance.test_ac_d06_obsidian_sync_loop_guard_protected_fields_and_conflict_copy -v
	@echo ">> $(PY) -m unittest tests.test_w2_w3_roadmap.TestVaultSovereignty -v"
	@$(PY) -m unittest tests.test_w2_w3_roadmap.TestVaultSovereignty -v

verify-paths:
	@echo ">> $(PY) -m unittest tests.test_omnilore_phase0_acceptance.TestOmniLorePhase0Acceptance.test_ac_s05_path_traversal_and_symlink_defense -v"
	@$(PY) -m unittest tests.test_omnilore_phase0_acceptance.TestOmniLorePhase0Acceptance.test_ac_s05_path_traversal_and_symlink_defense -v

# ---------------------------------------------------------------------------
# 解析器 / 脱敏 / 运维 —— 真实第三方库或纯函数
# ---------------------------------------------------------------------------
verify-parsers:
	@echo ">> $(PY) -m unittest tests.test_full_omnilore_suite.TestOmniLoreFullSuite.test_05_image_ingestion_code_symbols_and_obsidian_dashboard -v"
	@$(PY) -m unittest tests.test_full_omnilore_suite.TestOmniLoreFullSuite.test_05_image_ingestion_code_symbols_and_obsidian_dashboard -v

verify-redaction:
	@echo ">> $(PY) -m unittest tests.test_full_omnilore_suite.TestOmniLoreFullSuite.test_07_phase3_org_tree_inheritance_and_redacted_promotion -v"
	@$(PY) -m unittest tests.test_full_omnilore_suite.TestOmniLoreFullSuite.test_07_phase3_org_tree_inheritance_and_redacted_promotion -v

verify-ops:
	@echo ">> $(PY) -m unittest tests.test_full_omnilore_suite.TestOmniLoreFullSuite.test_08_backup_restore_index_rebuild_and_jsonrpc_server -v"
	@$(PY) -m unittest tests.test_full_omnilore_suite.TestOmniLoreFullSuite.test_08_backup_restore_index_rebuild_and_jsonrpc_server -v

# ---------------------------------------------------------------------------
# 首发链路 —— CI 里以真实子进程 / 真实管道验证（Tested-real）
# ---------------------------------------------------------------------------
verify-launch:
	@echo ">> $(PY) -m unittest tests.test_w0_launch.TestCliEntryPoint -v"
	@$(PY) -m unittest tests.test_w0_launch.TestCliEntryPoint -v

verify-mcp:
	@echo ">> $(PY) -m unittest tests.test_w0_launch.TestMcpStdioEntrypoint -v"
	@$(PY) -m unittest tests.test_w0_launch.TestMcpStdioEntrypoint -v

verify-degradation:
	@echo ">> $(PY) -m unittest tests.test_w0_launch.TestZeroKeyDegradation -v"
	@$(PY) -m unittest tests.test_w0_launch.TestZeroKeyDegradation -v

verify-search:
	@echo ">> 注意：hybrid（向量）路径的 CI 用例是 monkeypatch 桩，没有真实网络调用 ——"
	@echo "   keyword_only（BM25）路径才是真实执行的。本目标不能作为 hybrid 的证据。"
	@echo ">> $(PY) -m unittest tests.test_full_omnilore_suite.TestOmniLoreFullSuite.test_01_profile_and_category_rls_isolation -v"
	@$(PY) -m unittest tests.test_full_omnilore_suite.TestOmniLoreFullSuite.test_01_profile_and_category_rls_isolation -v
	@echo ">> $(PY) -m unittest tests.test_w0_launch.TestZeroKeyDegradation -v"
	@$(PY) -m unittest tests.test_w0_launch.TestZeroKeyDegradation -v

verify-lf:
	@echo ">> $(PY) -m unittest tests.test_w0_launch.TestCanonicalTextIO -v"
	@$(PY) -m unittest tests.test_w0_launch.TestCanonicalTextIO -v

# ---------------------------------------------------------------------------
# 诚实标注守卫 —— README / 状态矩阵 / CHANGELOG 三处必须一致
# ---------------------------------------------------------------------------
verify-honesty:
	@echo ">> $(PY) -m unittest tests.test_w2_w3_roadmap -v"
	@$(PY) -m unittest tests.test_w2_w3_roadmap -v
	@echo ">> $(PY) -m unittest tests.test_w0_launch.TestReadmeClaimAccuracy -v"
	@$(PY) -m unittest tests.test_w0_launch.TestReadmeClaimAccuracy -v

# ---------------------------------------------------------------------------
# 真实 PostgreSQL —— 唯一需要外部依赖的目标
# 环境不具备时**必须**失败（exit 2「未验证」），绝不假装验证过；
# 探针跑通但结论为否是 exit 3。CI 的 `integration-rls` job 跑的就是下面这一行 ——
# 文档、Makefile、CI 三处必须是同一条命令，改一处就要改三处。
# ---------------------------------------------------------------------------
verify-rls-pg:
	@echo ">> $(PY) tools/verify_rls_pg.py（真实 PostgreSQL：exit 0 通过 / 2 未验证 / 3 结论为否）"
	@$(PY) tools/verify_rls_pg.py

lint:
	@echo ">> $(RUFF) check omnilore tests tools"
	@$(RUFF) check omnilore tests tools
