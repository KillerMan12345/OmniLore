"""Tests for MCP Asset Evolution DAG & Governance Plan/Citation Actions.

Verifies:
1. `get_document_asset(asset_type="EVOLUTION_DAG")` retrieves the causal version DAG.
2. `review_and_fix_knowledge(action="INSPECT_PLAN")` compiles an MRP change plan.
3. `review_and_fix_knowledge(action="VERIFY_CITATION")` performs citation grounding.
"""

from pathlib import Path
import tempfile
import unittest

from omnilore.bootstrap import ensure_bootstrapped
from omnilore.engine import (
    OmniLoreEngine,
    OmniLoreMCPGateway,
    PermissionDeniedError,
    ReviewAction,
)


class TestMcpDagAndGovernanceActions(unittest.IsolatedAsyncioTestCase):
    """End-to-end tests for evolution DAG asset and governance review actions."""

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name).resolve() / "OmniLore_Data"
        self.engine = OmniLoreEngine(self.data_dir)
        self.boot = ensure_bootstrapped(self.engine, self.data_dir)
        self.gateway = OmniLoreMCPGateway(self.engine)
        self.token = self.boot.raw_token
        self.space_id = self.boot.space_id

        # Ingest an initial document v1.0
        doc_content = (
            "# 核心网关设计规范\n\n"
            "## 1. 架构总览\n\n"
            "系统采用高可用分布式架构，单节点请求超时设置为 500ms。\n\n"
            "## 2. 认证协议\n\n"
            "客户端必须使用 HMAC-SHA256 签名传输请求头。\n"
        )
        ingest_res = await self.gateway.ingest_document_or_file(
            self.token,
            filename="gateway_spec.md",
            content_bytes=doc_content.encode("utf-8"),
            target_space_id=self.space_id,
            target_category="01_Work",
            version_label="v1.0",
        )
        self.doc_id = ingest_res["task"].get("result_doc_id") or ingest_res["task"].get("doc_id")
        self.task_id = ingest_res["task"]["task_id"]

        # If task needs review, approve it
        if ingest_res["task"]["task_state"] == "NEEDS_REVIEW":
            approve_res = await self.gateway.review_and_fix_knowledge(
                self.token,
                action=ReviewAction.APPROVE_REVIEW,
                target_id=self.task_id,
                payload={},
            )
            self.doc_id = approve_res.get("result_doc_id") or self.doc_id

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    async def test_get_document_asset_evolution_dag(self) -> None:
        """get_document_asset with asset_type='EVOLUTION_DAG' returns causal version DAG."""
        res = await self.gateway.get_document_asset(
            self.token,
            doc_id=self.doc_id,
            asset_type="EVOLUTION_DAG",
        )
        self.assertEqual(res["doc_id"], self.doc_id)
        self.assertEqual(res["asset_type"], "EVOLUTION_DAG")
        self.assertGreaterEqual(res["total_versions"], 1)
        self.assertIn("root_version_key", res)
        self.assertIn("ascii_graph", res)
        self.assertIn("nodes", res)
        # DAG 节点必须是真实、可校验的版本节点：根/最新版本键都能落到节点上，
        # 每个节点带完整身份字段，ASCII 谱图非空且含有根版本键。
        self.assertIsInstance(res["nodes"], dict)
        self.assertEqual(len(res["nodes"]), res["total_versions"])
        self.assertIn(res["root_version_key"], res["nodes"])
        self.assertIn(res["latest_version_key"], res["nodes"])
        for version_key, node in res["nodes"].items():
            self.assertEqual(version_key, node["version_key"])
            self.assertEqual(node["doc_id"], version_key)
            self.assertIn("version_label", node)
            self.assertIn("committed_at", node)
        self.assertTrue(res["ascii_graph"].strip())
        self.assertIn(res["root_version_key"][:8], res["ascii_graph"])

    async def test_review_action_inspect_plan(self) -> None:
        """review_and_fix_knowledge with INSPECT_PLAN returns MRP change plan."""
        proposed = (
            "# 核心网关设计规范\n\n"
            "## 1. 架构总览\n\n"
            "系统采用高可用分布式架构，超时时间调整为 800ms。\n\n"
            "## 2. 认证协议\n\n"
            "客户端支持 OAuth2 与 HMAC-SHA256 混合认证。\n\n"
            "## 3. 限流降级\n\n"
            "采用滑动窗口限流算法。\n"
        )
        plan_res = await self.gateway.review_and_fix_knowledge(
            self.token,
            action=ReviewAction.INSPECT_PLAN,
            target_id=self.doc_id,
            payload={"proposed_markdown": proposed},
        )
        self.assertEqual(plan_res["action"], "INSPECT_PLAN")
        self.assertEqual(plan_res["doc_id"], self.doc_id)
        self.assertIn(plan_res["risk_level"], ("LOW", "MEDIUM", "HIGH"))
        self.assertTrue(plan_res["recommendation"])
        self.assertTrue(plan_res["has_changes"])
        self.assertGreater(plan_res["added_lines"], 0)
        self.assertTrue(any("3. 限流降级" in s for s in plan_res["sections_affected"]))
        # 对外别名与 dataclass 真实字段必须严格一致，防止 CLI / MCP 语义漂移。
        self.assertEqual(plan_res["sections_affected"], plan_res["impacted_headings"])
        self.assertIsInstance(plan_res["unified_diff"], str)
        # 目标文档不存在时必须 fail-closed：不允许拿空串当基线生成看似正常的计划。
        with self.assertRaises(PermissionDeniedError):
            await self.gateway.review_and_fix_knowledge(
                self.token,
                action=ReviewAction.INSPECT_PLAN,
                target_id="doc_does_not_exist_zzz",
                payload={"proposed_markdown": proposed},
            )

    async def test_review_action_verify_citation(self) -> None:
        """review_and_fix_knowledge with VERIFY_CITATION verifies citations."""
        # Genuine citation
        report_true = await self.gateway.review_and_fix_knowledge(
            self.token,
            action=ReviewAction.VERIFY_CITATION,
            target_id=self.doc_id,
            payload={"citation_text": "系统采用高可用分布式架构，单节点超时为500ms"},
        )
        self.assertEqual(report_true["action"], "VERIFY_CITATION")
        self.assertEqual(report_true["doc_id"], self.doc_id)
        self.assertEqual(report_true["status"], report_true["verification_status"])
        self.assertIn(report_true["status"], ("VERIFIED", "PARTIALLY_VERIFIED"))
        self.assertGreaterEqual(report_true["overlap_score"], 0.45)
        self.assertEqual(report_true["overlap_score"], report_true["similarity_score"])
        self.assertTrue(report_true["is_grounded"])

        # Fabricated citation
        report_false = await self.gateway.review_and_fix_knowledge(
            self.token,
            action=ReviewAction.VERIFY_CITATION,
            target_id=self.doc_id,
            payload={"citation_text": "全系统必须使用量子加密通信技术进行同步"},
        )
        self.assertEqual(report_false["status"], "UNVERIFIED_OR_MISQUOTED")
        self.assertLess(report_false["overlap_score"], 0.45)
        self.assertFalse(report_false["is_grounded"])


if __name__ == "__main__":
    unittest.main()
