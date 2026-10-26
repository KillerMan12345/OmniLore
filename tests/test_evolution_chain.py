"""
Tests for Causal Evolution Chain & MRP Knowledge Compilation (Idea 3)
=====================================================================
Verifies that:
1. Version evolution DAG links versions with causal pointers (supersedes & reverts).
2. ASCII evolution trace summarizes line counts and version transitions cleanly.
3. Pre-merge proposed change plans inspect diffs, detect impacted headings, and evaluate merge risk.
"""

from __future__ import annotations

import unittest

from omnilore.evolution_chain import CausalEvolutionDAG, compile_proposed_change_plan


class TestCausalEvolutionChain(unittest.TestCase):
    def test_causal_evolution_dag_lineage_and_render(self) -> None:
        dag = CausalEvolutionDAG(series_id="ser_arch_guide")

        # 1. Root version v1.0
        n1 = dag.add_version(
            version_key="vk_001_initial",
            version_label="v1.0",
            doc_id="doc_1",
            file_hash="hash_aaa",
            committed_at="2026-10-01T10:00:00Z",
            diff_summary={"added": 120, "deleted": 0},
            summary_note="首次发布架构指南",
        )
        self.assertEqual(n1.version_label, "v1.0")

        # 2. Update version v2.0 superseding v1.0
        n2 = dag.add_version(
            version_key="vk_002_update",
            version_label="v2.0",
            doc_id="doc_2",
            file_hash="hash_bbb",
            committed_at="2026-10-15T14:30:00Z",
            supersedes_version_keys=["vk_001_initial"],
            diff_summary={"added": 45, "deleted": 10},
            summary_note="补充分布式事务规范",
        )
        self.assertEqual(n2.supersedes_version_keys, ["vk_001_initial"])

        # 3. Revert version v3.0 reverting v2.0
        n3 = dag.add_version(
            version_key="vk_003_rollback",
            version_label="v3.0",
            doc_id="doc_3",
            file_hash="hash_ccc",
            committed_at="2026-10-20T09:00:00Z",
            reverts_version_key="vk_002_update",
            diff_summary={"added": 10, "deleted": 45},
            summary_note="回滚分布式事务改动",
        )
        self.assertEqual(n3.reverts_version_key, "vk_002_update")

        # 4. Lineage verification
        lineage = dag.get_lineage()
        self.assertEqual(len(lineage), 3)
        self.assertEqual(lineage[2]["reverts_version_key"], "vk_002_update")

        # 5. ASCII trace
        rendered = dag.render_ascii_lineage()
        self.assertIn("ser_arch_guide", rendered)
        self.assertIn("REVERT(vk_002_update)", rendered)
        self.assertIn("SUPERSEDES(vk_001_initial)", rendered)

    def test_compile_proposed_change_plan_no_diff(self) -> None:
        text = "# 标题\n正文内容保持不变。"
        plan = compile_proposed_change_plan(text, text)
        self.assertFalse(plan.has_changes)
        self.assertEqual(plan.risk_level, "LOW")
        self.assertEqual(plan.added_lines, 0)
        self.assertEqual(plan.deleted_lines, 0)

    def test_compile_proposed_change_plan_structural_diff(self) -> None:
        current = (
            "# 接口安全标准\n\n"
            "## 1. 认证鉴权\n使用 JWT Bearer 令牌。\n\n"
            "## 2. 访问限流\n每分钟限流 60 次。\n"
        )
        incoming = (
            "# 接口安全标准\n\n"
            "## 1. 认证鉴权\n升级为 mTLS 双方互认证机制。\n\n"
            "## 2. 访问限流\n每分钟限流 120 次。\n\n"
            "## 3. 审计留痕\n所有高危调用必须留存 SHA-256 CAS 记录。\n"
        )
        plan = compile_proposed_change_plan(current, incoming)
        self.assertTrue(plan.has_changes)
        self.assertTrue(plan.added_lines > 0)
        self.assertIn("## 1. 认证鉴权", plan.impacted_headings)
        self.assertIn("## 3. 审计留痕", plan.impacted_headings)
        self.assertIn("unified_diff", str(plan))
