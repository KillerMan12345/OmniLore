"""
Tests for Context Budget Governance & Chunk Unfolding (Idea 1)
==============================================================
Verifies that:
1. Token estimator runs fast without dependencies on CJK and Latin text.
2. Search results are governed to fit within token_budget without losing metadata anchors.
3. Oversized code blocks and body text are structurally folded with helpful guidance.
4. Downstream agents can invoke get_document_asset(asset_type="CHUNK") or get_asset_chunk
   to retrieve the raw, un-folded content on demand.
5. Authorization RLS boundaries are enforced when fetching chunks.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from omnilore.bootstrap import ensure_bootstrapped
from omnilore.context_budget import estimate_tokens, fold_chunk_content, govern_search_results
from omnilore.engine import OmniLoreEngine, OmniLoreMCPGateway, PermissionDeniedError


class TestContextBudgetGovernance(unittest.TestCase):
    def test_estimate_tokens_accuracy(self) -> None:
        # CJK estimation: 10 CJK characters should yield roughly 7-8 tokens
        cjk_text = "这是一段测试中文文本内容"
        self.assertTrue(5 <= estimate_tokens(cjk_text) <= 12)

        # Latin estimation: roughly 4-5 chars per token
        latin_text = "The quick brown fox jumps over the lazy dog"
        self.assertTrue(8 <= estimate_tokens(latin_text) <= 18)

        # Empty string
        self.assertEqual(estimate_tokens(""), 0)

    def test_fold_chunk_content_code_block(self) -> None:
        large_code = (
            "Here is the implementation:\n"
            "```python\n"
            + "\n".join([f"    x_{i} = calculate_value({i}) * 3.14159" for i in range(25)])
            + "\n```\n"
            "End of section."
        )
        chunk_id = "chk_test_101"
        folded, is_truncated = fold_chunk_content(large_code, chunk_id, target_tokens=40)
        self.assertTrue(is_truncated)
        self.assertIn("代码块已折叠: python, 共 25 行", folded)
        self.assertIn(f'get_asset_chunk(chunk_id="{chunk_id}")', folded)

    def test_govern_search_results_unconstrained(self) -> None:
        chunks = [
            {"chunk_id": "c1", "doc_id": "d1", "chunk_text": "Short text 1"},
            {"chunk_id": "c2", "doc_id": "d2", "chunk_text": "Short text 2"},
        ]
        res = govern_search_results(chunks, token_budget=0)
        self.assertFalse(res["budget_governance"]["applied"])
        self.assertEqual(res["results"][0]["chunk_text"], "Short text 1")

    def test_govern_search_results_applied(self) -> None:
        oversized_text = "核心概念说明：" + ("详细论述内容若干字，展开分析。" * 60)
        chunks = [
            {"chunk_id": "c1", "doc_id": "d1", "chunk_text": oversized_text},
            {"chunk_id": "c2", "doc_id": "d2", "chunk_text": "简单短句。"},
        ]
        res = govern_search_results(chunks, token_budget=200)
        gov = res["budget_governance"]
        self.assertTrue(gov["applied"])
        self.assertTrue(gov["estimated_tokens_after"] < gov["estimated_tokens_before"])
        self.assertTrue(res["results"][0]["truncated"])
        self.assertFalse(res["results"][1]["truncated"])


class TestEndToEndSearchBudgetAndUnfolding(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name) / "OmniLore_Data"
        self.engine = OmniLoreEngine(self.data_dir)
        self.boot = ensure_bootstrapped(self.engine, self.data_dir)
        self.gateway = OmniLoreMCPGateway(self.engine)

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_search_knowledge_with_budget_and_unfold_chunk(self) -> None:
        # Ingest a document containing code and long text
        long_content = (
            "# 系统架构设计规范\n\n"
            "本文档详细说明了微服务架构下的治理规则。\n\n"
            "```python\n"
            + "\n".join([f"def service_handler_{i}(request):\n    return process_event({i})\n" for i in range(20)])
            + "```\n\n"
            + ("架构治理细则补充说明。" * 40)
        )

        res = await self.gateway.ingest_document_or_file(
            self.boot.raw_token,
            content_bytes=long_content.encode("utf-8"),
            filename="arch_spec.md",
            target_space_id=self.boot.space_id,
        )
        task = res["task"]
        self.assertEqual(task["task_state"], "COMMITTED")

        # 1. Search with strict token_budget=300
        search_res = await self.gateway.search_knowledge(
            self.boot.raw_token,
            query="微服务 架构治理",
            token_budget=300,
        )
        self.assertIn("budget_governance", search_res)
        self.assertTrue(search_res["budget_governance"]["applied"])
        self.assertTrue(len(search_res["results"]) > 0)

        hit = search_res["results"][0]
        chunk_id = hit["chunk_id"]
        # Confirm folding took place
        self.assertTrue(hit.get("truncated", False))
        self.assertIn("折叠", hit["chunk_text"])

        # 2. Downstream agent unfolds via get_document_asset(asset_type="CHUNK")
        unfolded_via_doc = await self.gateway.get_document_asset(
            self.boot.raw_token,
            doc_id=hit["doc_id"],
            asset_type="CHUNK",
            chunk_id=chunk_id,
        )
        self.assertEqual(unfolded_via_doc["chunk_id"], chunk_id)
        self.assertIn("def service_handler_0", unfolded_via_doc["chunk_text"])
        self.assertNotIn("代码块已折叠", unfolded_via_doc["chunk_text"])

        # 3. Unfold directly via gateway.get_asset_chunk
        unfolded_direct = await self.gateway.get_asset_chunk(
            self.boot.raw_token,
            chunk_id=chunk_id,
        )
        self.assertEqual(unfolded_direct["chunk_id"], chunk_id)
        self.assertIn("def service_handler_0", unfolded_direct["chunk_text"])
        self.assertNotIn("代码块已折叠", unfolded_direct["chunk_text"])

        # 4. Unauthorized token is denied
        with self.assertRaises(PermissionDeniedError):
            await self.gateway.get_asset_chunk("invalid_token", chunk_id=chunk_id)
