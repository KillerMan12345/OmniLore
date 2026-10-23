"""
FastMCP JSON-RPC 2.0 Stdio Server Entrypoint
============================================
Exposes all 7 OmniLore v2.3 MCP tools over standard JSON-RPC 2.0 stdio transport
so any MCP client (Cursor, Claude Desktop, Windsurf, custom IDE agents) can invoke
OmniLore directly.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path
import sys
from typing import Any, Dict

from .bootstrap import ensure_bootstrapped, load_bootstrap_state
from .engine import OmniLoreEngine, OmniLoreError, OmniLoreMCPGateway, OperationType, ReviewAction
from .pathutil import resolve_data_dir
from .version import __version_label__


# Advertised to MCP clients via `initialize`. Kept in sync with `omnilore.__version__`.
SERVER_VERSION = __version_label__


MCP_TOOL_DEFINITIONS = [
    {
        "name": "search_knowledge",
        "description": (
            "Authorized retrieval over current knowledge versions: hybrid search (BM25 + 1024d vector + code "
            "symbols) when DASHSCOPE_API_KEY is configured; automatically degrades to BM25/keyword-only when it "
            "is not. Responses carry `retrieval_mode` (`hybrid`|`keyword_only`) and `degraded_reason`."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["token", "query"],
            "properties": {
                "token": {"type": "string"},
                "query": {"type": "string"},
                "space_ids": {"type": "array", "items": {"type": "string"}},
                "categories": {"type": "array", "items": {"type": "string"}},
                "code_symbol": {"type": "string"},
                "include_history": {"type": "boolean", "default": False},
            },
        },
    },
    {
        "name": "get_document_asset",
        "description": "Retrieve full Wiki Markdown or original raw asset by controlled doc_id (no bare file paths).",
        "inputSchema": {
            "type": "object",
            "required": ["token", "doc_id"],
            "properties": {
                "token": {"type": "string"},
                "doc_id": {"type": "string"},
                "asset_type": {"type": "string", "enum": ["WIKI", "RAW_ASSET"], "default": "WIKI"},
            },
        },
    },
    {
        "name": "ingest_document_or_file",
        "description": "Ingest a document, PDF, or image with security precheck, routing, compilation, and version control.",
        "inputSchema": {
            "type": "object",
            "required": ["token", "filename"],
            "properties": {
                "token": {"type": "string"},
                "filename": {"type": "string"},
                "content_text": {"type": "string"},
                "content_base64": {"type": "string"},
                "target_space_id": {"type": "string", "default": "AUTO"},
                "target_category": {"type": "string"},
                "version_key": {"type": "string"},
                "version_label": {"type": "string", "default": "v1.0"},
                "operation_type": {"type": "string", "default": "INGEST_FILE"},
            },
        },
    },
    {
        "name": "commit_memory_note",
        "description": "Commit a structured conclusion or decision note from conversation into a target space.",
        "inputSchema": {
            "type": "object",
            "required": ["token", "title", "summary_markdown", "target_space_id"],
            "properties": {
                "token": {"type": "string"},
                "title": {"type": "string"},
                "summary_markdown": {"type": "string"},
                "target_space_id": {"type": "string"},
                "target_category": {"type": "string", "default": "01_Work"},
                "version_key": {"type": "string"},
            },
        },
    },
    {
        "name": "get_ingestion_task",
        "description": "Query the state, error details, or candidate route of an ingestion task.",
        "inputSchema": {
            "type": "object",
            "required": ["token", "task_id"],
            "properties": {
                "token": {"type": "string"},
                "task_id": {"type": "string"},
            },
        },
    },
    {
        "name": "review_and_fix_knowledge",
        "description": "Execute controlled governance actions: APPROVE_REVIEW, RECLASSIFY, SPLIT_DOCUMENT, FIX_EXTRACTED_FIELD, REVERT_VERSION, PROMOTE_COPY, CANCEL_TASK.",
        "inputSchema": {
            "type": "object",
            "required": ["token", "action", "target_id"],
            "properties": {
                "token": {"type": "string"},
                "action": {
                    "type": "string",
                    "enum": [a.value for a in ReviewAction],
                },
                "target_id": {"type": "string"},
                "payload": {"type": "object", "default": {}},
            },
        },
    },
    {
        "name": "query_health_trends",
        "description": "Query structured health metric trends within an authorized 04_Health_Archive scope.",
        "inputSchema": {
            "type": "object",
            "required": ["token", "space_id", "subject_person", "metric_name"],
            "properties": {
                "token": {"type": "string"},
                "space_id": {"type": "string"},
                "subject_person": {"type": "string"},
                "metric_name": {"type": "string"},
            },
        },
    },
]


class MCPJsonRpcServer:
    def __init__(self, gateway: OmniLoreMCPGateway):
        self.gateway = gateway

    async def handle_request(self, request: Dict[str, Any]) -> Dict[str, Any]:
        req_id = request.get("id")
        method = request.get("method", "")
        params = request.get("params") or {}

        try:
            if method == "initialize":
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "serverInfo": {"name": "omnilore-mcp-gateway", "version": SERVER_VERSION},
                        "capabilities": {"tools": {}},
                    },
                }
            if method == "tools/list":
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"tools": MCP_TOOL_DEFINITIONS},
                }
            if method == "tools/call":
                tool_name = params.get("name")
                args = params.get("arguments") or {}
                result_data = await self._dispatch_tool(tool_name, args)
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": json.dumps(result_data, ensure_ascii=False, default=str)}],
                        "isError": False,
                    },
                }
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }
        except OmniLoreError as exc:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps({"error_code": exc.code, "message": exc.message}, ensure_ascii=False)}],
                    "isError": True,
                },
            }

    async def _dispatch_tool(self, tool_name: str, args: Dict[str, Any]) -> Any:
        token = args["token"]
        if tool_name == "search_knowledge":
            return await self.gateway.search_knowledge(
                token,
                args["query"],
                space_ids=args.get("space_ids"),
                categories=args.get("categories"),
                code_symbol=args.get("code_symbol"),
                include_history=bool(args.get("include_history", False)),
            )
        if tool_name == "get_document_asset":
            res = await self.gateway.get_document_asset(token, args["doc_id"], asset_type=args.get("asset_type", "WIKI"))
            if "raw_bytes" in res:
                res["raw_base64"] = base64.b64encode(res.pop("raw_bytes")).decode("ascii")
            return res
        if tool_name == "ingest_document_or_file":
            if "content_base64" in args and args["content_base64"]:
                raw_bytes = base64.b64decode(args["content_base64"])
            else:
                raw_bytes = str(args.get("content_text", "")).encode("utf-8")
            return await self.gateway.ingest_document_or_file(
                token,
                content_bytes=raw_bytes,
                filename=args["filename"],
                target_space_id=args.get("target_space_id", "AUTO"),
                target_category=args.get("target_category"),
                version_key=args.get("version_key"),
                version_label=args.get("version_label", "v1.0"),
                operation_type=OperationType(args.get("operation_type", "INGEST_FILE")),
            )
        if tool_name == "commit_memory_note":
            return await self.gateway.commit_memory_note(
                token,
                title=args["title"],
                summary_markdown=args["summary_markdown"],
                target_space_id=args["target_space_id"],
                target_category=args.get("target_category", "01_Work"),
                version_key=args.get("version_key"),
            )
        if tool_name == "get_ingestion_task":
            return await self.gateway.get_ingestion_task(token, args["task_id"])
        if tool_name == "review_and_fix_knowledge":
            return await self.gateway.review_and_fix_knowledge(
                token,
                action=ReviewAction(args["action"]),
                target_id=args["target_id"],
                payload=args.get("payload") or {},
            )
        if tool_name == "query_health_trends":
            return await self.gateway.query_health_trends(
                token,
                space_id=args["space_id"],
                subject_person=args["subject_person"],
                metric_name=args["metric_name"],
            )
        raise OmniLoreError("UNKNOWN_TOOL", f"Unknown MCP tool: {tool_name}")


def _stderr(line: str) -> None:
    """Write a diagnostic line to stderr — stdout is reserved for JSON-RPC frames."""
    sys.stderr.write(line + "\n")
    sys.stderr.flush()


def _bootstrap_or_explain(engine: OmniLoreEngine, data_root: Path) -> None:
    """Guarantee the server can serve its first request without any manual setup.

    Every MCP tool takes a mandatory `token`, yet a fresh clone has no space, no
    membership and no token — historically a 0% first-run success rate. We therefore
    reuse the persisted bootstrap identity (written by `omnilore bootstrap`) or,
    when auto-bootstrap is enabled (default), create one on the fly.

    The two decisions are deliberately **orthogonal** (QA P2-1):

    * **load** — whenever `<data_root>/ops/bootstrap.json` already exists its
      identity is loaded into this process's engine, *regardless* of
      `OMNILORE_AUTO_BOOTSTRAP`. Turning auto-bootstrap off means "do not mint a
      new identity for me", **not** "invalidate the token I already have"; the
      CHANGELOG promise is that a CLI-issued token stays usable by the MCP service.
    * **create** — only when no persisted identity exists, and only when
      `OMNILORE_AUTO_BOOTSTRAP` is on (or `OMNILORE_BOOTSTRAP_TOKEN` was supplied).

    Args:
        engine: The freshly constructed engine backing this server.
        data_root: Data directory whose `ops/bootstrap.json` is consulted.
    """
    auto_bootstrap = os.environ.get("OMNILORE_AUTO_BOOTSTRAP", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }
    explicit_token = os.environ.get("OMNILORE_BOOTSTRAP_TOKEN", "").strip() or None
    # 只有「既没有已持久化身份、又关闭了自动引导、还没有显式 token」时才早退。
    # 早退前不加载既有身份会让 AUTO_BOOTSTRAP=0 连带废掉 `omnilore bootstrap` 已签发的
    # token（QA P2-1）：内存引擎是空的，任何 tools/call 都会 PERMISSION_DENIED。
    if load_bootstrap_state(data_root) is None and not auto_bootstrap and explicit_token is None:
        _stderr("[omnilore] OMNILORE_AUTO_BOOTSTRAP=0 且未提供 OMNILORE_BOOTSTRAP_TOKEN，跳过自动引导。")
        _stderr("[omnilore] 如需手动初始化，请运行：omnilore bootstrap")
        return

    try:
        result = ensure_bootstrapped(
            engine,
            data_root,
            token=explicit_token,
            allow_auto_create=auto_bootstrap,
            emit=_stderr,
        )
    except RuntimeError as exc:
        _stderr(f"[omnilore] 引导失败：{exc}")
        return
    if result.created_space or result.created_token:
        _stderr(f"[omnilore] 已自动引导：space={result.space_id} token={result.raw_token}")
        _stderr("[omnilore] 建议改用 `omnilore bootstrap` 显式生成并妥善保存该 token。")


async def main_stdio() -> None:
    # Same resolver as the CLI (`omnilore.pathutil.resolve_data_dir`). Do NOT inline
    # `Path(os.environ[...])` here: bootstrap identity lives under this directory and
    # any drift silently produces a second, useless token. See QA finding P1-1.
    data_root = resolve_data_dir()
    engine = OmniLoreEngine(data_root)
    _bootstrap_or_explain(engine, data_root)
    server = MCPJsonRpcServer(OmniLoreMCPGateway(engine))
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        req = json.loads(line)
        resp = await server.handle_request(req)
        sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def main_stdio_sync() -> None:
    """Synchronous entry point that drives :func:`main_stdio` to completion.

    `[project.scripts]` console scripts are called as plain callables — setuptools
    never awaits them. Pointing `omnilore-mcp` straight at the `async def`
    `main_stdio` therefore produced an un-awaited coroutine and a
    ``RuntimeWarning: coroutine 'main_stdio' was never awaited`` instead of serving
    JSON-RPC, which broke the documented MCP client setup in README. This thin
    wrapper is what the console script targets; `python -m omnilore.mcp_stdio_server`
    runs it through the `__main__` guard below and behaves identically.

    Returns:
        None. Returns only when stdin reaches EOF and the server loop ends.
    """
    asyncio.run(main_stdio())


if __name__ == "__main__":
    main_stdio_sync()
