"""Tests for default-on answer-synthesis citation grounding (direction 2).

Verifies that grounding is automatic (not opt-in), works on un-folded evidence,
detects fabricated / misattributed quotes, can be explicitly disabled, and never
changes the two orthogonal degradation axes.
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from omnilore.bootstrap import ensure_bootstrapped
from omnilore.citation_guard import (
    build_citation_verifications,
    extract_answer_claims,
)
from omnilore.engine import OmniLoreEngine, OmniLoreMCPGateway

DOC_TEXT = (
    "# 核心网关设计规范\n\n"
    "## 1. 架构总览\n\n"
    "系统采用高可用分布式架构，单节点请求超时设置为 500ms。\n\n"
    "## 2. 认证协议\n\n"
    "客户端必须使用 HMAC-SHA256 签名传输请求头。\n"
)

CHUNKS = [
    {
        "chunk_id": "chk_a_0",
        "doc_id": "doc_spec_a",
        "chunk_text": "系统采用高可用分布式架构，单节点请求超时设置为 500ms。",
    },
    {
        "chunk_id": "chk_b_0",
        "doc_id": "doc_spec_b",
        "chunk_text": "客户端必须使用 HMAC-SHA256 签名传输请求头。",
    },
]


class TestClaimExtraction(unittest.TestCase):
    def test_extract_deduplicates_markers_quotes_and_citations(self) -> None:
        answer = (
            "系统采用高可用分布式架构，单节点请求超时设置为 500ms [ref: doc_spec_a]。"
            "另外「客户端必须使用 HMAC-SHA256 签名传输请求头」也是要求。"
        )
        citations = [
            {"doc_id": "doc_spec_a", "quote": "系统采用高可用分布式架构，单节点请求超时设置为 500ms"},
        ]
        claims = extract_answer_claims(answer, citations)
        texts = [c["claim_text"] for c in claims]
        # The structured citation and the [ref:] sentence normalize to the same
        # text, so dedup must keep exactly one of them.
        self.assertEqual(len([t for t in texts if "高可用分布式架构" in t]), 1)
        self.assertTrue(any(c["source"] == "answer_quote" for c in claims))
        self.assertTrue(any(c["declared_doc_id"] == "doc_spec_a" for c in claims))
        self.assertLessEqual(len(claims), 8)

    def test_extract_falls_back_to_sentences_without_any_marker(self) -> None:
        claims = extract_answer_claims("第一句没有任何标注，但仍需被巡检。", [])
        self.assertTrue(claims)
        self.assertEqual(claims[0]["source"], "answer_sentence")
        self.assertIsNone(claims[0]["declared_doc_id"])


class TestGroundingReport(unittest.TestCase):
    def test_grounded_quote_is_verified(self) -> None:
        res = build_citation_verifications(
            "系统采用高可用分布式架构。",
            [{"doc_id": "doc_spec_a", "quote": "系统采用高可用分布式架构，单节点请求超时设置为 500ms"}],
            CHUNKS,
        )
        self.assertEqual(res["citation_grounding"]["total_claims"], 1)
        self.assertFalse(res["unverified_citations_detected"])
        top = res["citation_verifications"][0]
        self.assertEqual(top["verification_status"], "VERIFIED")
        self.assertEqual(top["matched_doc_id"], "doc_spec_a")
        self.assertEqual(top["matched_chunk_id"], "chk_a_0")
        self.assertTrue(top["is_grounded"])

    def test_fabricated_quote_triggers_warning(self) -> None:
        res = build_citation_verifications(
            "全系统必须使用量子加密通信技术进行同步。",
            [{"doc_id": "doc_spec_a", "quote": "全系统必须使用量子加密通信技术进行同步"}],
            CHUNKS,
        )
        self.assertTrue(res["unverified_citations_detected"])
        self.assertFalse(res["citation_grounding"]["all_grounded"])
        self.assertEqual(
            res["citation_verifications"][0]["verification_status"],
            "UNVERIFIED_OR_MISQUOTED",
        )

    def test_misattributed_document_is_flagged(self) -> None:
        res = build_citation_verifications(
            "认证协议说明。",
            [{"doc_id": "doc_spec_a", "quote": "客户端必须使用 HMAC-SHA256 签名传输请求头"}],
            CHUNKS,
        )
        top = res["citation_verifications"][0]
        self.assertTrue(top["doc_id_mismatch"])
        self.assertEqual(top["matched_doc_id"], "doc_spec_b")
        self.assertEqual(top["verification_status"], "UNVERIFIED_OR_MISQUOTED")
        self.assertTrue(res["unverified_citations_detected"])

    def test_disabled_guard_reports_honestly(self) -> None:
        res = build_citation_verifications("任意陈述", [], CHUNKS, enabled=False)
        self.assertEqual(res["citation_verifications"], [])
        self.assertFalse(res["unverified_citations_detected"])
        self.assertFalse(res["citation_grounding"]["enabled"])
        self.assertIn("未做事实核验", res["citation_grounding"]["note"])


class TestSearchKnowledgeAutoGrounding(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name) / "OmniLore_Data"
        self.engine = OmniLoreEngine(self.data_dir)
        self.boot = ensure_bootstrapped(self.engine, self.data_dir)
        self.gateway = OmniLoreMCPGateway(self.engine)
        ingest = await self.gateway.ingest_document_or_file(
            self.boot.raw_token,
            content_bytes=DOC_TEXT.encode("utf-8"),
            filename="gateway_spec.md",
            target_space_id=self.boot.space_id,
        )
        self.doc_id = ingest["task"]["result_doc_id"]
        self.assertIsNotNone(self.doc_id)

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_search_auto_grounds_and_keeps_degradation_axes(self) -> None:
        res = await self.gateway.search_knowledge(self.boot.raw_token, "网关超时 500ms")
        self.assertIn("citation_verifications", res)
        self.assertIn("unverified_citations_detected", res)
        self.assertTrue(res["citation_grounding"]["enabled"])
        self.assertGreater(res["citation_grounding"]["total_claims"], 0)
        self.assertFalse(res["unverified_citations_detected"])
        self.assertTrue(any(v["is_grounded"] for v in res["citation_verifications"]))
        # Orthogonal degradation contract must be untouched.
        self.assertIn("retrieval_mode", res)
        self.assertIn("degraded_reason", res)
        self.assertIn("synthesis_degraded", res)
        self.assertIn("budget_governance", res)
        self.assertIn("has_sufficient_evidence", res)

    async def test_guard_can_be_disabled_by_caller(self) -> None:
        res = await self.gateway.search_knowledge(
            self.boot.raw_token, "网关超时 500ms", verify_citations=False
        )
        self.assertEqual(res["citation_verifications"], [])
        self.assertFalse(res["citation_grounding"]["enabled"])
        self.assertFalse(res["unverified_citations_detected"])

        # The MCP protocol layer must actually forward the switch. Without this,
        # `verify_citations` in inputSchema would be a claim with no effect.
        from omnilore.mcp_stdio_server import MCPJsonRpcServer
        rpc = MCPJsonRpcServer(self.gateway)
        for enabled_flag, expected in ((False, False), (True, True)):
            arguments = {"token": self.boot.raw_token, "query": "网关超时 500ms"}
            if not enabled_flag:
                arguments["verify_citations"] = False
            response = await rpc.handle_request({
                "id": 1,
                "method": "tools/call",
                "params": {"name": "search_knowledge", "arguments": arguments},
            })
            self.assertFalse(response["result"]["isError"])
            payload = json.loads(response["result"]["content"][0]["text"])
            self.assertEqual(payload["citation_grounding"]["enabled"], expected)

    async def test_fabricated_synthesis_raises_warning_e2e(self) -> None:
        forged = {
            "answer": "全系统必须使用量子加密通信技术进行同步。",
            "citations": [
                {
                    "doc_id": self.doc_id,
                    "quote": "全系统必须使用量子加密通信技术进行同步",
                }
            ],
            "has_sufficient_evidence": True,
        }
        with patch.object(
            self.engine.model_adapter,
            "generate_structured_resilient",
            return_value=(forged, ""),
        ):
            res = await self.gateway.search_knowledge(self.boot.raw_token, "网关超时 500ms")
        self.assertTrue(res["unverified_citations_detected"])
        self.assertEqual(
            res["citation_verifications"][0]["verification_status"],
            "UNVERIFIED_OR_MISQUOTED",
        )
        # Reporting only: the answer and the sufficiency flag are NOT rewritten.
        self.assertEqual(res["synthesized_answer"], forged["answer"])
        self.assertTrue(res["has_sufficient_evidence"])

    async def test_folded_budget_does_not_create_false_negative(self) -> None:
        # A tiny budget folds chunk_text for the response, but grounding must run
        # on the un-folded original and therefore stay grounded.
        res = await self.gateway.search_knowledge(
            self.boot.raw_token, "网关超时 500ms", token_budget=12
        )
        self.assertTrue(res["budget_governance"]["applied"])
        self.assertFalse(res["unverified_citations_detected"])
        self.assertTrue(any(v["is_grounded"] for v in res["citation_verifications"]))


if __name__ == "__main__":
    unittest.main()
