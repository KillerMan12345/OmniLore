"""
Tests for Citation Grounding & Anti-Hallucination Verification (Idea 2)
======================================================================
Verifies that:
1. Exact and fuzzy quotes are identified against authentic source documents.
2. Fabricated claims are diagnosed as UNVERIFIED_OR_MISQUOTED.
3. End-to-end gateway.verify_citation respects authorization boundaries.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from omnilore.bootstrap import ensure_bootstrapped
from omnilore.citation import normalize_text, verify_citation_content
from omnilore.engine import OmniLoreEngine, OmniLoreMCPGateway, PermissionDeniedError


class TestCitationGroundingUnit(unittest.TestCase):
    def test_normalize_text(self) -> None:
        raw = "## 1. 核心架构（Microservices Architecture）: 500ms 超时！"
        norm = normalize_text(raw)
        self.assertEqual(norm, "1核心架构microservicesarchitecture500ms超时")

    def test_verify_citation_content_exact(self) -> None:
        source = (
            "第一章 背景介绍\n"
            "第二章 核心规约\n"
            "所有微服务接口必须在 500ms 内返回响应，超时将熔断。\n"
            "第三章 部署要求\n"
        )
        res = verify_citation_content(
            claim_text="微服务接口超时时间是 500ms",
            source_content=source,
            verbatim_quote="所有微服务接口必须在 500ms 内返回响应",
        )
        self.assertEqual(res["verification_status"], "VERIFIED")
        self.assertTrue(res["is_grounded"])
        self.assertEqual(res["line_number"], 3)
        self.assertIn("500ms", res["matched_snippet"])

    def test_verify_citation_content_hallucinated(self) -> None:
        source = "本文档仅讨论前端单页应用的路由与状态管理。"
        res = verify_citation_content(
            claim_text="后端使用的是 Rust 编写的高并发 RPC 引擎，QPS 达到 10 万",
            source_content=source,
        )
        self.assertEqual(res["verification_status"], "UNVERIFIED_OR_MISQUOTED")
        self.assertFalse(res["is_grounded"])
        self.assertTrue(res["similarity_score"] < 0.3)


class TestEndToEndCitationGrounding(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name) / "OmniLore_Data"
        self.engine = OmniLoreEngine(self.data_dir)
        self.boot = ensure_bootstrapped(self.engine, self.data_dir)
        self.gateway = OmniLoreMCPGateway(self.engine)

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_gateway_verify_citation_e2e(self) -> None:
        doc_text = (
            "# 个人健康管理指引\n\n"
            "成年人静息心率正常范围为 60 至 100 次/分。\n"
            "长期有氧运动者的心率可能降至 50 次/分左右。\n"
        )
        ingest_res = await self.gateway.ingest_document_or_file(
            self.boot.raw_token,
            content_bytes=doc_text.encode("utf-8"),
            filename="health_guide.md",
            target_space_id=self.boot.space_id,
        )
        doc_id = ingest_res["task"]["result_doc_id"]
        self.assertIsNotNone(doc_id)

        # 1. Grounded citation check -> VERIFIED
        verified_res = await self.gateway.verify_citation(
            self.boot.raw_token,
            doc_id=doc_id,
            claim_text="心率正常范围",
            verbatim_quote="成年人静息心率正常范围为 60 至 100 次/分",
        )
        self.assertEqual(verified_res["verification_status"], "VERIFIED")
        self.assertTrue(verified_res["is_grounded"])

        # 2. Fabricated claim check -> UNVERIFIED
        hallucination_res = await self.gateway.verify_citation(
            self.boot.raw_token,
            doc_id=doc_id,
            claim_text="每天必须服用 500mg 维生素C与大量抗生素",
        )
        self.assertEqual(hallucination_res["verification_status"], "UNVERIFIED_OR_MISQUOTED")
        self.assertFalse(hallucination_res["is_grounded"])

        # 3. Access control: invalid token is rejected
        with self.assertRaises(PermissionDeniedError):
            await self.gateway.verify_citation(
                "invalid_token",
                doc_id=doc_id,
                claim_text="心率",
            )
