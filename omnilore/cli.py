"""
OmniLore Command Line Interface (Open-Source Launch W0 / A-2, A-6, A-7)
========================================================================
Registered as the `omnilore` console script (and runnable as
`python -m omnilore`). Sub-commands:

* `bootstrap` — zero-API-key first-run setup: initialise the data directory,
  create the default space + owner token, and print everything the user needs
  to wire OmniLore into an MCP client.
* `demo`      — the smallest reproducible demo: bootstrap → ingest a document →
  find it with search. Runs end-to-end with **no API key**.
* `version`   — print the external (`v0.3.0-alpha`) and internal (PEP 440) version.

The commands are deliberately dependency-free (stdlib only) so that a fresh
`pip install -e .` can run them immediately.
"""

from __future__ import annotations

import argparse
from argparse import Namespace
import asyncio
import json
from pathlib import Path
import sys
from typing import Any, Callable, Dict, List, Optional

from .version import __version__, __version_display__
from .bootstrap import BootstrapResult, ensure_bootstrapped, render_mcp_config_snippet
from .engine import OmniLoreEngine, OmniLoreMCPGateway, OmniLoreError
from .pathutil import resolve_data_dir

# The sample knowledge used by `omnilore demo`. Kept in-repo so the demo never
# needs network access, an API key, or any external file.
DEMO_FILENAME = "omnilore_welcome_note.md"
DEMO_CONTENT = """# OmniLore 零密钥上手说明

OmniLore 是一个 AI 原生的可审计知识中枢。本次演示在**没有任何云 API Key**的情况下完成
收录与检索，说明：不配置 `DASHSCOPE_API_KEY` 时会自动降级为关键词（BM25）检索。

## 60 秒验证清单

1. `pip install -e .`
2. `omnilore bootstrap`
3. `omnilore demo`
4. 观察搜索结果里的 `retrieval_mode=keyword_only`

## 关键口令

- 数据目录：`OMNILORE_DATA_DIR`
- 知识检索：`omnilore demo`
- 状态：v0.3.0-alpha（非生产就绪）
"""
DEMO_QUERY = "60 秒验证清单"

EXIT_OK = 0
EXIT_ERROR = 1


def _default_data_dir() -> Path:
    """Resolve the data directory.

    Shared helper (:func:`omnilore.pathutil.resolve_data_dir`) is used so that the
    CLI and the MCP stdio server always agree on the same absolute directory — see
    that module's docstring for why this must not be duplicated.
    """
    return resolve_data_dir()


def _print(text: str = "") -> None:
    """Write a line to stdout (single place to redirect in future embedders)."""
    sys.stdout.write(text + "\n")


def _print_bootstrap_result(result: BootstrapResult, *, mcp_config: bool = True) -> None:
    """Pretty-print a bootstrap result, including the token and MCP client snippet."""
    hint_note = "（已复用既有 token）" if result.reused_state and not result.created_token else "（新签发）"
    _print()
    _print("=" * 68)
    _print("OmniLore bootstrap 完成")
    _print("=" * 68)
    _print(f"  数据目录      : {result.data_dir}")
    _print(f"  默认空间      : {result.space_id}")
    _print(f"  Owner user_id : {result.user_id}")
    _print(f"  检索模式      : {result.retrieval_mode}")
    if result.degraded_reason:
        _print(f"  降级说明      : {result.degraded_reason}")
    _print()
    _print(f"  Owner Token   : {result.raw_token}  {hint_note}")
    _print(f"  Token 已持久化: {result.state_path}")
    _print("                  请妥善保存该 token；MCP 客户端首参即为该 token。")
    if mcp_config:
        _print()
        _print("  把下面这段写进 MCP 客户端配置（claude_desktop_config.json / .cursor/mcp.json）；")
        _print("  DASHSCOPE_API_KEY 为可选项，留空即自动降级为关键词检索：")
        _print()
        for line in render_mcp_config_snippet(result).splitlines():
            _print("    " + line)
    _print()


async def _async_bootstrap(args: Namespace) -> Dict[str, Any]:
    """Run the bootstrap flow and return a summary payload.

    Args:
        args: Parsed `bootstrap` arguments.

    Returns:
        Dict describing the bootstrapped space/user/token (for `--json` output).
    """
    # 走共享 resolver，不要在这里再写一份 `.expanduser().resolve()`（QA P2-C：
    # 同一逻辑两处实现正是 P1-1 的成因，见 omnilore/pathutil.py）。
    data_dir = resolve_data_dir(args.data_dir)
    engine = OmniLoreEngine(data_dir)
    result = ensure_bootstrapped(
        engine,
        data_dir,
        token=args.token,
        user_id=args.user_id,
        space_id=args.space_id,
    )
    payload = result.as_dict()
    payload["token"] = result.raw_token
    payload["mcp_client_config"] = json.loads(render_mcp_config_snippet(result))
    if args.json:
        _print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        _print_bootstrap_result(result, mcp_config=not args.quiet)
    return payload


async def _async_demo(args: Namespace) -> Dict[str, Any]:
    """Bootstrap (idempotent), ingest one sample document, then find it with search."""
    data_dir = resolve_data_dir(args.data_dir)
    engine = OmniLoreEngine(data_dir)
    gateway = OmniLoreMCPGateway(engine)

    result = ensure_bootstrapped(engine, data_dir, token=args.token, space_id=args.space_id)
    space_id = args.space_id or result.space_id

    _print()
    _print("=" * 68)
    _print("OmniLore demo：投一份文档，再搜到它（零 API Key）")
    _print("=" * 68)
    _print(f"  数据目录  : {data_dir}")
    _print(f"  目标空间  : {space_id}")
    _print(f"  检索模式  : {result.retrieval_mode}")
    if result.degraded_reason:
        _print(f"  降级说明  : {result.degraded_reason}")
    _print()

    _print(f"[1/3] 收录文档：{DEMO_FILENAME}")
    ingest = await gateway.ingest_document_or_file(
        result.raw_token,
        content_bytes=DEMO_CONTENT.encode("utf-8"),
        filename=DEMO_FILENAME,
        target_space_id=space_id,
        target_category=args.category,
        version_label="v1.0",
    )
    task = ingest["task"]
    doc_id = task.get("result_doc_id")
    _print(f"      任务状态      : {task['task_state']}")
    _print(f"      结果文档 ID   : {doc_id or '（尚未生成）'}")

    _print(f"[2/3] 检索关键词：{repr(args.query)}")
    # `OmniLoreMCPGateway.search_knowledge` intentionally stays MCP-shaped (no
    # transport-local knobs), so the CLI talks to the engine directly for `top_k`.
    search = await engine.search_knowledge(result.raw_token, args.query, top_k=args.top_k)
    hits = search.get("results", [])
    _print(f"      命中结果数    : {len(hits)}")
    _print(f"      retrieval_mode: {search.get('retrieval_mode')}")
    _print(f"      degraded_reason: {search.get('degraded_reason') or '（无，未降级）'}")
    _print()

    found = False
    for idx, hit in enumerate(hits, start=1):
        if hit.get("doc_id") == doc_id:
            found = True
        # `version_label` already carries its own prefix (e.g. "v1.0") — do not prepend another "v".
        _print(f"      #{idx} score={hit['score']}  《{hit['title']}》 {hit['version_label']}")
        excerpt = " ".join(hit["chunk_text"].split())[:120]
        _print(f"          {excerpt}")

    _print()
    if args.json:
        _print(json.dumps(
            {
                "space_id": space_id,
                "task_state": task["task_state"],
                "doc_id": doc_id,
                "query": args.query,
                "retrieval_mode": search.get("retrieval_mode"),
                "degraded_reason": search.get("degraded_reason"),
                "hits": len(hits),
                "found_demo_document": doc_id in {h.get("doc_id") for h in hits},
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        ))
        return {
            "space_id": space_id,
            "doc_id": doc_id,
            "hits": len(hits),
            "found_demo_document": doc_id in {h.get("doc_id") for h in hits},
        }

    _print("[3/3] 结论")
    if doc_id and found:
        _print(f"      ✅ 已成功检索到刚收录的文档 {doc_id}，60 秒验证通过。")
    elif doc_id:
        _print(f"      ⚠️  文档已收录（{doc_id}）但未进入 Top-{args.top_k}；可增大 --top-k 后重试。")
    else:
        _print("      ❌ 文档未能发布到可检索状态（通常为路由待确认），请检查任务回执。")
    _print()
    _print(f"  提示：Obsidian 知识页位于 {data_dir / '02_Vault' / 'spaces' / space_id / 'wiki'}")
    _print()
    return {"space_id": space_id, "doc_id": doc_id, "hits": len(hits), "found_demo_document": found}


def _cmd_version(_args: Namespace) -> int:
    """Print both the external display version and the internal PEP 440 version."""
    _print(f"OmniLore {__version_display__}  (PEP 440: {__version__})")
    _print("状态：alpha 预发布版本 —— 仅供评估、研究与贡献使用，请勿放入重要业务流程。")
    return EXIT_OK


def _run_bootstrap(args: Namespace) -> int:
    """Execute the `bootstrap` sub-command and return a process exit code."""
    asyncio.run(_async_bootstrap(args))
    return EXIT_OK


def _run_demo(args: Namespace) -> int:
    """Execute the `demo` sub-command and return a process exit code."""
    asyncio.run(_async_demo(args))
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    """Construct the top-level argparse parser and all sub-parsers."""
    parser = argparse.ArgumentParser(
        prog="omnilore",
        description="OmniLore CLI：零密钥首启（bootstrap）与最小可传播演示（demo）",
    )
    parser.add_argument("--version", action="version", version=f"omnilore {__version_display__}")
    subparsers = parser.add_subparsers(dest="command", required=False)

    p_bootstrap = subparsers.add_parser(
        "bootstrap",
        help="初始化数据目录并创建默认空间与 owner token（零 API Key）",
    )
    p_bootstrap.add_argument("--data-dir", default=str(_default_data_dir()), help="数据目录，默认 ./OmniLore_Data")
    p_bootstrap.add_argument("--space-id", default=None, help="默认空间 id，缺省 sp_bootstrap_personal")
    p_bootstrap.add_argument("--user-id", default=None, help="owner user id")
    p_bootstrap.add_argument("--token", default=None, help="指定 owner token（缺省自动随机生成）")
    p_bootstrap.add_argument("--json", action="store_true", help="以 JSON 输出，便于脚本消费")
    p_bootstrap.add_argument("--quiet", action="store_true", help="不打印 MCP 客户端配置片段")
    p_bootstrap.set_defaults(func=_run_bootstrap)

    p_demo = subparsers.add_parser(
        "demo",
        help="投一份文档并搜到它（自动 bootstrap，零 API Key）",
    )
    p_demo.add_argument("--data-dir", default=str(_default_data_dir()), help="数据目录，默认 ./OmniLore_Data")
    p_demo.add_argument("--space-id", default=None, help="目标空间 id，缺省使用 bootstrap 空间")
    p_demo.add_argument("--token", default=None, help="沿用既有 token")
    p_demo.add_argument("--category", default="01_Work", help="目标类别，默认 01_Work")
    p_demo.add_argument("--query", default=DEMO_QUERY, help="检索关键词")
    p_demo.add_argument("--top-k", type=int, default=3, help="返回命中条数，默认 3")
    p_demo.add_argument("--json", action="store_true", help="以 JSON 输出结论")
    p_demo.set_defaults(func=_run_demo)

    p_version = subparsers.add_parser("version", help="打印版本号")
    p_version.set_defaults(func=_cmd_version)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point (also the `omnilore` console script target).

    Args:
        argv: Optional argument list; defaults to `sys.argv[1:]`.

    Returns:
        Process exit code: 0 on success, 1 on handled OmniLore/runtime errors.
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "func", None) is None:
        parser.print_help()
        return EXIT_OK

    runner: Callable[[Namespace], int] = args.func
    try:
        return int(runner(args))
    except OmniLoreError as exc:
        sys.stderr.write(f"[omnilore] {exc.code}: {exc.message}\n")
        return EXIT_ERROR
    except KeyboardInterrupt:  # pragma: no cover - interactive interruption
        sys.stderr.write("\n[omnilore] 已取消\n")
        return EXIT_ERROR
    except Exception as exc:
        sys.stderr.write(f"[omnilore] 未预期的错误：{type(exc).__name__}: {exc}\n")
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover - direct module execution guard
    raise SystemExit(main())
