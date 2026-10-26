"""
Tests for Tiered MCP Tooling Governance (Idea 4)
================================================
Verifies that:
1. Every MCP tool definition explicitly declares its tier ("query" vs "governance").
2. Query tier includes read-only, budget-governed consumption tools.
3. Governance tier includes state-modifying, audited operations.
4. JSON-RPC tools/list handles default full list and filtered tier queries.
"""

from __future__ import annotations

import unittest

from omnilore.mcp_stdio_server import MCP_TOOL_DEFINITIONS, MCP_TOOL_TIERS, MCPJsonRpcServer


class TestTieredMCPTooling(unittest.IsolatedAsyncioTestCase):
    def test_all_tools_declare_tier_metadata(self) -> None:
        self.assertEqual(len(MCP_TOOL_DEFINITIONS), 7)
        for tool in MCP_TOOL_DEFINITIONS:
            self.assertIn("tier", tool, f"Tool {tool['name']} must declare tier metadata")
            self.assertIn(
                tool["tier"],
                (MCP_TOOL_TIERS["QUERY"], MCP_TOOL_TIERS["GOVERNANCE"]),
                f"Tool {tool['name']} has invalid tier {tool['tier']}",
            )

        query_tools = [t["name"] for t in MCP_TOOL_DEFINITIONS if t["tier"] == MCP_TOOL_TIERS["QUERY"]]
        governance_tools = [t["name"] for t in MCP_TOOL_DEFINITIONS if t["tier"] == MCP_TOOL_TIERS["GOVERNANCE"]]

        # Query tier: read-only
        self.assertIn("search_knowledge", query_tools)
        self.assertIn("get_document_asset", query_tools)
        self.assertIn("query_health_trends", query_tools)

        # Governance tier: audited operations
        self.assertIn("ingest_document_or_file", governance_tools)
        self.assertIn("commit_memory_note", governance_tools)
        self.assertIn("get_ingestion_task", governance_tools)
        self.assertIn("review_and_fix_knowledge", governance_tools)

    async def test_tools_list_filtering_by_tier(self) -> None:
        server = MCPJsonRpcServer(gateway=None)  # Gateway not invoked for tools/list

        # 1. Default request returns all 7 tools
        resp_all = await server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        tools_all = resp_all["result"]["tools"]
        self.assertEqual(len(tools_all), 7)

        # 2. Filter by query tier
        resp_query = await server.handle_request(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {"tier": "query"}}
        )
        tools_query = resp_query["result"]["tools"]
        self.assertEqual(len(tools_query), 3)
        self.assertTrue(all(t["tier"] == "query" for t in tools_query))

        # 3. Filter by governance tier
        resp_gov = await server.handle_request(
            {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {"tier": "governance"}}
        )
        tools_gov = resp_gov["result"]["tools"]
        self.assertEqual(len(tools_gov), 4)
        self.assertTrue(all(t["tier"] == "governance" for t in tools_gov))
