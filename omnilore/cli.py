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
* `audit-bundle` — export a verifiable evidence bundle (`audit-bundle.json` +
  `audit-bundle.verify.md`) for a document or a space (R-05m). Every SHA-256 in
  the bundle is **recomputed from the bytes on disk**; a mismatch makes the
  process exit non-zero instead of printing a bundle that merely *looks* fine.
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
from .audit_bundle import build_audit_bundle, bundle_exit_code, write_audit_bundle
from .bootstrap import (
    BootstrapResult,
    ensure_bootstrapped,
    render_mcp_config_snippet,
    render_sovereignty_notice,
)
from .dashscope_client import ModelConfigError
from .embedding_provider import EmbeddingConfigurationError
from .engine import OmniLoreEngine, OmniLoreMCPGateway, OmniLoreError, ReviewAction, RLSDatabaseEngine
from .pathutil import resolve_data_dir
from .runtime_index import load_runtime_index
from .telemetry import (
    TELEMETRY_ENV,
    TRANSPORT_UNAVAILABLE,
    build_payload,
    consent_path,
    is_enabled,
    record_command,
    set_consent,
)
from .textio import pin_utf8_std_streams

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
#: `audit-bundle` 的「已验证但结论为否」退出码 —— 与 `tools/verify_rls_pg.py` 的语义一致：
#: 0 = 通过，3 = 结论为否，1 = 跑不起来。刻意不复用 1：把「内容被改过」和「命令打错了」
#: 混成同一个码，会让脚本化的校验失去区分能力。
EXIT_INTEGRITY_NOT_VERIFIED = 3


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


def _print_err(text: str = "") -> None:
    """Write a line to **stderr** —— 诊断/回显专用，不污染 stdout 的机器可读输出。"""
    sys.stderr.write(text + "\n")


def _emit_sovereignty_notice() -> None:
    """把「当前真正生效」的主权配置打到 stderr（每次启动一次）。

    Why stderr: stdout 是「60 秒验证」的可抄录输出（`--json` 时更是机器可读 JSON），
    诊断信息混进去会破坏逐字一致与脚本解析。两个开关只在进程环境变量里生效
    （本进程不读 `.env`），容器模式下还要靠 compose 转发 —— 任何一环断了，用户都会
    得到「静默无效」，这是主权承诺最坏的失效方式。打出来，失效就变成可见的。
    """
    _print_err()
    _print_err("  [主权配置] 你的笔记你做主 —— 以下是本次启动实际生效的值：")
    for line in render_sovereignty_notice():
        _print_err(line)
    _print_err()


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

    # R-16 / P1-1：把「当前真正生效」的主权配置打出来（走 stderr，见 helper 注释）。
    # 放在最后一段，不影响 README 里「60 秒验证」那段 stdout 节选。
    _print()  # 保留 stdout 结尾的空行，与加入回显之前逐字一致
    _emit_sovereignty_notice()


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
        # `--json` 的消费者同样需要知道「配置到底生效了没」；stderr 是诊断通道，
        # 不会破坏 stdout 上的 JSON。这里仍然只打印一次。
        _emit_sovereignty_notice()
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


FULL_DEMO_VERSION_KEY = "vk_demo_governance"
FULL_DEMO_RECLASSIFY_CATEGORY = "02_Life"


async def _demo_ingest_version(
    gateway: OmniLoreMCPGateway,
    token: str,
    *,
    space_id: str,
    category: str,
    version_label: str,
    content: str,
    filename: str,
) -> Dict[str, Any]:
    """Ingest one version into a *fixed* series, approving it if routing asks for review."""
    ingest = await gateway.ingest_document_or_file(
        token,
        content_bytes=content.encode("utf-8"),
        filename=filename,
        target_space_id=space_id,
        target_category=category,
        version_key=FULL_DEMO_VERSION_KEY,
        version_label=version_label,
    )
    task = ingest["task"]
    doc_id = task.get("result_doc_id") or task.get("doc_id")
    if task["task_state"] == "NEEDS_REVIEW":
        approved = await gateway.review_and_fix_knowledge(
            token,
            action=ReviewAction.APPROVE_REVIEW,
            target_id=task["task_id"],
            payload={"version_key": FULL_DEMO_VERSION_KEY, "version_label": version_label},
        )
        doc_id = approved.get("result_doc_id") or doc_id
    if not doc_id:
        raise OmniLoreError("DEMO_INTEGRITY_FAILED", f"{version_label} did not reach a published doc_id")
    return {"doc_id": doc_id, "task_state": task["task_state"]}


async def _async_full_demo(args: Namespace) -> Dict[str, Any]:
    """R-05f: the complete governance walkthrough (R-05f / T9).

    Two versions of one document series are committed, the series is reverted,
    the restored current version is reclassified, and the result is verified by
    both a causal-DAG asset read and a fresh search.  Every step asserts a real
    state transition; a broken link fails the demo instead of printing a
    reassuring summary.
    """
    data_dir = resolve_data_dir(args.data_dir)
    engine = OmniLoreEngine(data_dir)
    gateway = OmniLoreMCPGateway(engine)
    result = ensure_bootstrapped(engine, data_dir, token=args.token, space_id=args.space_id)
    space_id = args.space_id or result.space_id
    token = args.token or result.raw_token
    category = args.category

    _print()
    _print("=" * 68)
    _print("OmniLore 完整治理 Demo：双版本 → 撤销 → 重分类 → 复核")
    _print("=" * 68)
    _print(f"  数据目录  : {data_dir}")
    _print(f"  目标空间  : {space_id}")
    _print(f"  版本链    : {FULL_DEMO_VERSION_KEY}")
    _print()

    v1_text = (
        "# 网关设计规范 v1\n\n"
        "## 1. 超时策略\n\n"
        "系统采用高可用分布式架构，单节点请求超时设置为 500ms。\n\n"
        "## 2. 认证协议\n\n"
        "客户端必须使用 HMAC-SHA256 签名传输请求头。\n"
    )
    v2_text = (
        "# 网关设计规范 v2\n\n"
        "## 1. 超时策略\n\n"
        "系统采用高可用分布式架构，单节点请求超时调整为 800ms。\n\n"
        "## 2. 认证协议\n\n"
        "客户端必须使用 HMAC-SHA256 签名传输请求头。\n\n"
        "## 3. 限流降级\n\n"
        "超出配额时返回 429 并启用本地缓存兜底。\n"
    )

    _print("[1/5] 收录 v1.0")
    v1 = await _demo_ingest_version(
        gateway, token, space_id=space_id, category=category,
        version_label="v1.0", content=v1_text, filename="governance_spec.md",
    )
    _print(f"      doc_id = {v1['doc_id']}  ({v1['task_state']})")

    _print("[2/5] 收录 v2.0（同一版本链）")
    v2 = await _demo_ingest_version(
        gateway, token, space_id=space_id, category=category,
        version_label="v2.0", content=v2_text, filename="governance_spec.md",
    )
    if v2["doc_id"] == v1["doc_id"]:
        raise OmniLoreError("DEMO_INTEGRITY_FAILED", "v2.0 reused the v1.0 doc_id; no version chain was created")
    dag_before = await gateway.get_document_asset(token, doc_id=v2["doc_id"], asset_type="EVOLUTION_DAG")
    series_id = dag_before["series_id"]
    if dag_before["total_versions"] < 2:
        raise OmniLoreError("DEMO_INTEGRITY_FAILED", "causal DAG does not contain both committed versions")
    _print(f"      doc_id = {v2['doc_id']}  series = {series_id}")
    _print(f"      因果链节点数 = {dag_before['total_versions']}")

    _print("[3/5] REVERT_VERSION：撤销 v2.0，恢复 v1.0 为当前版本")
    reverted = await gateway.review_and_fix_knowledge(
        token,
        action=ReviewAction.REVERT_VERSION,
        target_id=series_id,
        payload={"expected_current_doc_id": v2["doc_id"], "rollback_to_doc_id": v1["doc_id"]},
    )
    if reverted["current_doc_id"] != v1["doc_id"]:
        raise OmniLoreError("DEMO_INTEGRITY_FAILED", "REVERT_VERSION did not restore v1.0 as current")
    _print(f"      当前版本 = {reverted['current_doc_id']}  revision = {reverted['revision_number']}")

    _print(f"[4/5] RECLASSIFY：把恢复后的文档迁到 {FULL_DEMO_RECLASSIFY_CATEGORY}")
    reclassified = await gateway.review_and_fix_knowledge(
        token,
        action=ReviewAction.RECLASSIFY,
        target_id=v1["doc_id"],
        payload={"new_space_id": space_id, "new_category": FULL_DEMO_RECLASSIFY_CATEGORY},
    )
    if reclassified["category"] != FULL_DEMO_RECLASSIFY_CATEGORY:
        raise OmniLoreError("DEMO_INTEGRITY_FAILED", "RECLASSIFY did not update the category")
    _print(f"      category = {reclassified['category']}  revision = {reclassified['content_revision']}")
    _print(f"      wiki SHA-256 = {reclassified['file_hash'][:16]}…")

    _print("[5/5] 复核：DAG 拓扑 + 检索当前版本")
    dag_after = await gateway.get_document_asset(token, doc_id=v1["doc_id"], asset_type="EVOLUTION_DAG")
    search = await engine.search_knowledge(token, "网关超时 500ms", top_k=args.top_k)
    hits = search.get("results", [])
    found = v1["doc_id"] in {h.get("doc_id") for h in hits}
    _print(f"      DAG 节点数 = {dag_after['total_versions']}；检索命中 = {len(hits)}；命中当前版本 = {found}")
    if not dag_after["ascii_graph"].strip():
        raise OmniLoreError("DEMO_INTEGRITY_FAILED", "causal DAG rendered an empty lineage")

    payload: Dict[str, Any] = {
        "space_id": space_id,
        "series_id": series_id,
        "version_key": FULL_DEMO_VERSION_KEY,
        "doc_v1": v1["doc_id"],
        "doc_v2": v2["doc_id"],
        "versions_before_revert": dag_before["total_versions"],
        "current_doc_id_after_revert": reverted["current_doc_id"],
        "series_revision_after_revert": reverted["revision_number"],
        "category_after_reclassify": reclassified["category"],
        "wiki_sha256_after_reclassify": reclassified["file_hash"],
        "versions_after_revert": dag_after["total_versions"],
        "ascii_lineage": dag_after["ascii_graph"],
        "search_hits": len(hits),
        "found_current_document": found,
    }

    if args.json:
        _print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        _print()
        _print("  版本因果拓扑：")
        for line in dag_after["ascii_graph"].splitlines():
            _print(f"      {line}")
        _print()
        _print("  ✅ 完整治理链路通过：双版本提交 → 撤销回滚 → 重分类 → DAG/检索复核。")
        _print()
    return payload


async def _async_audit_bundle(args: Namespace) -> int:
    """导出证据包并返回进程退出码（R-05m）。

    退出码语义：
      * `0` — 全部检查 `verified`（磁盘字节与库里记录的哈希一致）；
      * `3` — 已验证但**结论为否**（有 `mismatch` / `missing` / `unreadable`）；
      * `1` — 导出本身失败（由 :func:`main` 统一转换）。

    注意：bundle 的哈希一定是「读磁盘字节重算」的结果，绝不是把库里的值抄一遍。
    这条不做成可选项 —— 一个「看起来正常但没校验」的证据包比没有更糟。

    Args:
        args: Parsed `audit-bundle` arguments.

    Returns:
        进程退出码（0 或 3）。
    """
    data_dir = resolve_data_dir(args.data_dir)
    engine = OmniLoreEngine(data_dir)
    # 复用既有 bootstrap 身份（幂等）：内存引擎在全新进程里是空的，必须先把
    # space / membership / token 重新登记，否则 resolve_scope 直接失败。
    result = ensure_bootstrapped(engine, data_dir, token=args.token, space_id=args.space_id)
    token = args.token or result.raw_token

    bundle = build_audit_bundle(engine, token, doc_id=args.doc_id, space_id=args.space_id)
    out_dir = Path(args.out) if args.out else data_dir / "ops" / "audit_bundles"
    json_path, verify_path = write_audit_bundle(bundle, out_dir)
    exit_code = bundle_exit_code(bundle)

    integrity = bundle["integrity"]
    counts = integrity["counts"]
    _print()
    _print("=" * 68)
    _print("OmniLore audit-bundle：可带走的证据包")
    _print("=" * 68)
    _print(f"  数据目录    : {data_dir}")
    _print(f"  导出范围    : {args.doc_id or args.space_id or '全部可读文档'}")
    _print(f"  文档 / 资产 : {counts['documents']} / {counts['source_assets']}")
    _print(f"  检查条数    : {counts['checks_total']}"
           f"（verified {counts['verified']} / mismatch {counts['mismatch']}"
           f" / missing {counts['missing']} / unreadable {counts['unreadable']}）")
    _print(f"  结论        : {integrity['status']}")
    _print(f"  说明        : {integrity['summary']}")
    _print(f"  证据包      : {json_path}")
    _print(f"  复核说明    : {verify_path}")

    failed = [check for check in integrity["checks"] if check["status"] != "verified"]
    if failed:
        _print()
        _print_err("  [结论为否] 以下检查未通过 —— 证据包已如实标注，进程以非零码退出：")
        for check in failed[:10]:
            _print_err(f"    - {check['check_id']}  {check['status']}  {check['relative_path']}")
        if len(failed) > 10:
            _print_err(f"    … 其余 {len(failed) - 10} 条见 {json_path}")
        _print_err(f"  退出码：{exit_code}（0 = 通过，3 = 已验证但结论为否）")

    if args.json:
        _print(json.dumps(bundle, ensure_ascii=False, indent=2, default=str))
    return exit_code


def _cmd_version(_args: Namespace) -> int:
    """Print both the external display version and the internal PEP 440 version."""
    _print(f"OmniLore {__version_display__}  (PEP 440: {__version__})")
    _print("状态：alpha 预发布版本 —— 仅供评估、研究与贡献使用，请勿放入重要业务流程。")
    return EXIT_OK


def _cmd_telemetry(args: Namespace) -> int:
    """Inspect / change opt-in telemetry consent and self-audit the raw payload (D3)."""
    data_dir = resolve_data_dir(args.data_dir)
    if args.enable or args.disable:
        try:
            set_consent(data_dir, enabled=bool(args.enable))
        except Exception as exc:
            _print_err(f"无法写入遥测开关状态：{type(exc).__name__}: {exc}")
            return EXIT_ERROR

    payload = build_payload(data_dir)
    if args.show:
        _print(json.dumps(payload, ensure_ascii=False, indent=2))
        return EXIT_OK

    _print()
    _print("=" * 68)
    _print("OmniLore 匿名遥测（严格 opt-in，默认关闭）")
    _print("=" * 68)
    _print(f"  当前状态    : {'已开启' if is_enabled(data_dir) else '关闭（默认）'}")
    _print(f"  全局开关    : {TELEMETRY_ENV}=0 一次性关闭，优先级最高")
    _print("  采集字段    : 子命令计数 / 降级次数 / 检索模式计数（仅匿名聚合）")
    _print(f"  外发        : 未实现（{TRANSPORT_UNAVAILABLE}，本版本不上传任何数据）")
    _print(f"  状态文件    : {consent_path(data_dir)}（仅显式开启后生成）")
    _print()
    _print("  自查完整 payload : omnilore telemetry --show")
    _print("  显式开启          : omnilore telemetry --enable")
    _print("  随时关闭          : omnilore telemetry --disable")
    _print()
    return EXIT_OK


def _run_bootstrap(args: Namespace) -> int:
    """Execute the `bootstrap` sub-command and return a process exit code."""
    asyncio.run(_async_bootstrap(args))
    return EXIT_OK


def _run_demo(args: Namespace) -> int:
    """Execute the `demo` sub-command and return a process exit code."""
    if getattr(args, "full", False):
        asyncio.run(_async_full_demo(args))
    else:
        asyncio.run(_async_demo(args))
    return EXIT_OK


def _run_audit_bundle(args: Namespace) -> int:
    """Execute the `audit-bundle` sub-command and return a process exit code."""
    return asyncio.run(_async_audit_bundle(args))


def _hydrate_runtime_state(engine: OmniLoreEngine, data_dir: Path) -> None:
    """把 `ops/index/` 运行时索引回填进内存引擎，让治理命令跨进程可用。

    默认引擎是内存仿真引擎，CLI 每次运行都是**全新进程**：`ensure_bootstrapped`
    只重建身份（space / membership / token），文档元数据仍留在上一次进程里。
    不回填的话 `plan` / `history` / `verify-citation` 只会看到空库，正常文档也会被
    误判为「资源不存在」而失败关闭（见 `INSPECT_PLAN` 的 fail-closed 分支）。
    `audit-bundle` 早已用同一份派生索引解决跨进程定位，这里沿用同一约定：
    索引只负责定位，文档正文仍由 `storage.read_wiki_by_record` 从磁盘真实字节读取。

    仅在内存引擎且 `documents` 为空时生效；PG 后端自行持久化，同进程已有数据时不覆盖。
    """
    if not isinstance(engine.db, RLSDatabaseEngine):
        return
    state = engine.db._state
    if state["documents"]:
        return
    index = load_runtime_index(data_dir)
    if index is None:
        return

    for asset_id, payload in index["assets"].items():
        cas_rel = str(payload.get("cas_rel_path") or "")
        if cas_rel.startswith("ops/cas/"):
            cas_rel = cas_rel[len("ops/cas/"):]
        state["assets"][asset_id] = {
            "asset_id": asset_id,
            "sha256_hash": payload.get("sha256_hash"),
            "storage_rel_path": cas_rel,
            "mime_type": payload.get("mime_type"),
            "byte_size": payload.get("size_bytes"),
            "created_at": payload.get("ingested_at"),
        }

    for doc_id, payload in index["documents"].items():
        series_id = str(payload.get("series_id") or "")
        state["documents"][doc_id] = {
            "doc_id": doc_id,
            "space_id": payload.get("space_id"),
            "category": payload.get("category"),
            "series_id": series_id,
            "version_label": payload.get("version_label"),
            "title": payload.get("title"),
            "summary": "",
            "wiki_rel_path": payload.get("wiki_rel_path"),
            "file_hash": payload.get("page_sha256"),
            "content_revision": payload.get("content_revision"),
            "doc_status": payload.get("doc_status"),
            "is_current": bool(payload.get("is_current")),
            "created_by": "",
            "created_at": payload.get("created_at"),
            "updated_at": payload.get("updated_at"),
        }
        if series_id and series_id not in state["document_series"]:
            state["document_series"][series_id] = {
                "series_id": series_id,
                "space_id": payload.get("space_id"),
                "category": payload.get("category"),
                "version_key": payload.get("version_key"),
                "current_doc_id": payload.get("series_current_doc_id"),
                "revision_number": payload.get("series_revision_number"),
                "created_at": payload.get("created_at"),
                "updated_at": payload.get("updated_at"),
            }
        source_names = payload.get("source_names") or {}
        source_locators = payload.get("source_locators") or {}
        for asset_id in payload.get("asset_ids") or []:
            state["document_assets"][str(asset_id)] = {
                "doc_id": doc_id,
                "asset_id": asset_id,
                "space_id": payload.get("space_id"),
                "category": payload.get("category"),
                "original_filename": source_names.get(str(asset_id)),
                "source_locator": source_locators.get(str(asset_id)),
            }


async def _async_plan(args: Namespace) -> int:
    """执行 MRP 知识变更预审计划（对比待更新内容与当前文档）。"""
    data_dir = resolve_data_dir(args.data_dir)
    file_path = Path(args.file).resolve()
    if not file_path.exists():
        _print_err(f"错误: 待预审文件不存在: {file_path}")
        return EXIT_ERROR

    content = file_path.read_text(encoding="utf-8")
    doc_id = args.doc_id
    if not doc_id:
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                try:
                    import yaml
                    fm = yaml.safe_load(parts[1])
                    if isinstance(fm, dict):
                        doc_id = fm.get("doc_id")
                except Exception:
                    pass
        if not doc_id:
            doc_id = file_path.stem

    engine = OmniLoreEngine(data_dir)
    result = ensure_bootstrapped(engine, data_dir, token=args.token, space_id=args.space_id)
    _hydrate_runtime_state(engine, data_dir)
    token = args.token or result.raw_token
    gw = OmniLoreMCPGateway(engine)

    try:
        res = await gw.review_and_fix_knowledge(
            token,
            action=ReviewAction.INSPECT_PLAN,
            target_id=doc_id,
            payload={"proposed_markdown": content},
        )
    except Exception as e:
        _print_err(f"生成变更预审计划失败: {e}")
        return EXIT_ERROR

    if args.json:
        _print(json.dumps(res, ensure_ascii=False, indent=2))
        return EXIT_OK

    _print()
    _print("=" * 68)
    _print(f"OmniLore MRP 知识变更预审计划: 《{doc_id}》")
    _print("=" * 68)
    _print(f"  目标文档 ID : {doc_id}")
    _print(f"  风险评级    : {res.get('risk_level', 'UNKNOWN')}")
    _print(f"  合并建议    : {res.get('recommendation', '')}")
    _print(f"  增删统计    : +{res.get('added_lines', 0)} 行 / -{res.get('deleted_lines', 0)} 行")
    sections = res.get("sections_affected", [])
    if sections:
        _print("  受影响小节  :")
        for s in sections:
            _print(f"    - {s}")
    else:
        _print("  受影响小节  : （无小节结构变化）")
    _print()
    return EXIT_OK


def _run_plan(args: Namespace) -> int:
    """Execute the `plan` sub-command and return a process exit code."""
    return asyncio.run(_async_plan(args))


async def _async_history(args: Namespace) -> int:
    """查询文档版本因果演化链 DAG 与 ASCII 拓扑谱图。"""
    data_dir = resolve_data_dir(args.data_dir)
    doc_id = args.doc_id
    engine = OmniLoreEngine(data_dir)
    result = ensure_bootstrapped(engine, data_dir, token=args.token, space_id=args.space_id)
    _hydrate_runtime_state(engine, data_dir)
    token = args.token or result.raw_token
    gw = OmniLoreMCPGateway(engine)

    try:
        asset = await gw.get_document_asset(token, doc_id=doc_id, asset_type="EVOLUTION_DAG")
    except Exception as e:
        _print_err(f"获取版本因果拓扑失败: {e}")
        return EXIT_ERROR

    if args.json:
        _print(json.dumps(asset, ensure_ascii=False, indent=2))
        return EXIT_OK

    _print()
    _print("=" * 68)
    _print(f"OmniLore 版本因果演化链: 《{doc_id}》")
    _print("=" * 68)
    _print(f"  版本总数    : {asset.get('total_versions', 0)}")
    _print(f"  根版本      : {asset.get('root_version_key', 'none')}")
    _print(f"  最新版本    : {asset.get('latest_version_key', 'none')}")
    _print()
    _print("  因果演化链谱 (ASCII):")
    for line in asset.get("ascii_graph", "").splitlines():
        _print(f"    {line}")
    _print()
    return EXIT_OK


def _run_history(args: Namespace) -> int:
    """Execute the `history` sub-command and return a process exit code."""
    return asyncio.run(_async_history(args))


async def _async_verify_citation(args: Namespace) -> int:
    """在终端核验引用陈述在知识库目标文档中的真实性。"""
    data_dir = resolve_data_dir(args.data_dir)
    doc_id = args.doc_id
    citation_text = args.text
    engine = OmniLoreEngine(data_dir)
    result = ensure_bootstrapped(engine, data_dir, token=args.token, space_id=args.space_id)
    _hydrate_runtime_state(engine, data_dir)
    token = args.token or result.raw_token
    gw = OmniLoreMCPGateway(engine)

    try:
        report = await gw.review_and_fix_knowledge(
            token,
            action=ReviewAction.VERIFY_CITATION,
            target_id=doc_id,
            payload={"citation_text": citation_text, "verbatim_quote": args.quote},
        )
    except Exception as e:
        _print_err(f"引用真实性比对失败: {e}")
        return EXIT_ERROR

    # 完整性门禁必须在 --json 之前算出退出码：JSON 只是输出格式，不能把
    # 「未通过真实性核验」降级成 EXIT_OK（与 _async_audit_bundle 的既有惯例一致）。
    status = report.get("status")
    score = report.get("overlap_score", 0.0)
    exit_code = (
        EXIT_OK
        if status in ("VERIFIED", "PARTIALLY_VERIFIED")
        else EXIT_INTEGRITY_NOT_VERIFIED
    )

    if args.json:
        _print(json.dumps(report, ensure_ascii=False, indent=2))
        return exit_code

    _print()
    _print("=" * 68)
    _print(f"OmniLore 防幻觉事实引用校验报告: 《{doc_id}》")
    _print("=" * 68)
    _print(f"  待核验陈述  : {report.get('claim_text')}")
    _print(f"  校验判定    : {status}")
    _print(f"  置信匹配分  : {score:.4f}")
    if report.get("best_snippet"):
        _print(f"  原文命中段落: {report.get('best_snippet')}")
    _print()
    if status == "VERIFIED":
        _print("  结论        : ✅ 引用已被证据原文严格证实（VERIFIED）。")
    elif status == "PARTIALLY_VERIFIED":
        _print("  结论        : ⚠️  部分符合原文事实，但存在改写或细节偏差（PARTIALLY_VERIFIED）。")
    else:
        _print("  结论        : ❌ 原文未找到匹配依据或存在错误引用（UNVERIFIED_OR_MISQUOTED）。")
    return exit_code


def _run_verify_citation(args: Namespace) -> int:
    """Execute the `verify-citation` sub-command and return a process exit code."""
    return asyncio.run(_async_verify_citation(args))


def build_parser() -> argparse.ArgumentParser:
    """Construct the top-level argparse parser and all sub-parsers."""
    parser = argparse.ArgumentParser(
        prog="omnilore",
        description="OmniLore CLI：零密钥首启（bootstrap）、最小可传播演示（demo）与可复核证据包（audit-bundle）",
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
    p_demo.add_argument(
        "--full", action="store_true",
        help="跑完整治理链路（双版本提交 / REVERT_VERSION / RECLASSIFY / DAG 与检索复核）",
    )
    p_demo.set_defaults(func=_run_demo)

    p_audit = subparsers.add_parser(
        "audit-bundle",
        help="导出可独立复核的证据包（audit-bundle.json + audit-bundle.verify.md）",
    )
    p_audit.add_argument("--data-dir", default=str(_default_data_dir()), help="数据目录，默认 ./OmniLore_Data")
    p_audit.add_argument("--doc-id", default=None, help="只导出该文档")
    p_audit.add_argument("--space-id", default=None, help="只导出该空间内的文档")
    p_audit.add_argument("--token", default=None, help="指定 token（缺省复用 ops/bootstrap.json 里的 owner token）")
    p_audit.add_argument("--out", default=None,
                         help="输出目录，缺省 <data-dir>/ops/audit_bundles（同名文件会被覆盖）")
    p_audit.add_argument("--json", action="store_true", help="把完整证据包同时打印到 stdout")
    p_audit.set_defaults(func=_run_audit_bundle)

    p_plan = subparsers.add_parser(
        "plan",
        help="执行 MRP 知识变更预审计划（对比本地文件与库中文档，输出差异、受影响小节与风险评级）",
    )
    p_plan.add_argument("file", help="待预审的本地 Markdown 文件路径")
    p_plan.add_argument("--doc-id", default=None, help="目标文档 ID（缺省从文件 frontmatter 或文件名推断）")
    p_plan.add_argument("--data-dir", default=str(_default_data_dir()), help="数据目录，默认 ./OmniLore_Data")
    p_plan.add_argument("--space-id", default=None, help="目标空间 ID")
    p_plan.add_argument("--token", default=None, help="指定操作 token")
    p_plan.add_argument("--json", action="store_true", help="以 JSON 格式输出预审计划")
    p_plan.set_defaults(func=_run_plan)

    p_history = subparsers.add_parser(
        "history",
        help="打印文档版本因果演化链 DAG 与 ASCII 拓扑谱图",
    )
    p_history.add_argument("doc_id", help="目标文档 ID")
    p_history.add_argument("--data-dir", default=str(_default_data_dir()), help="数据目录，默认 ./OmniLore_Data")
    p_history.add_argument("--space-id", default=None, help="目标空间 ID")
    p_history.add_argument("--token", default=None, help="指定操作 token")
    p_history.add_argument("--json", action="store_true", help="以 JSON 格式输出因果演化拓扑")
    p_history.set_defaults(func=_run_history)

    p_verify_citation = subparsers.add_parser(
        "verify-citation",
        help="防幻觉事实引用校验（比对陈述文本与知识库目标文档原文）",
    )
    p_verify_citation.add_argument("--doc-id", required=True, help="目标文档 ID")
    p_verify_citation.add_argument("--text", required=True, help="待核验的引用/陈述文本")
    p_verify_citation.add_argument("--quote", default=None, help="原文精确引句（可选）")
    p_verify_citation.add_argument("--data-dir", default=str(_default_data_dir()), help="数据目录，默认 ./OmniLore_Data")
    p_verify_citation.add_argument("--space-id", default=None, help="目标空间 ID")
    p_verify_citation.add_argument("--token", default=None, help="指定操作 token")
    p_verify_citation.add_argument("--json", action="store_true", help="以 JSON 格式输出校验报告")
    p_verify_citation.set_defaults(func=_run_verify_citation)

    p_telemetry = subparsers.add_parser(
        "telemetry",
        help="查看/开关 opt-in 匿名遥测，并自查完整原始 payload（默认关闭，本版本不外发）",
    )
    consent_group = p_telemetry.add_mutually_exclusive_group()
    consent_group.add_argument("--enable", action="store_true", help="显式开启匿名计数（唯一允许采集的方式）")
    consent_group.add_argument("--disable", action="store_true", help="关闭匿名计数")
    p_telemetry.add_argument("--show", action="store_true", help="打印将要发送的完整原始 payload 供自查")
    p_telemetry.add_argument("--data-dir", default=str(_default_data_dir()), help="数据目录，默认 ./OmniLore_Data")
    p_telemetry.set_defaults(func=_cmd_telemetry)

    p_version = subparsers.add_parser("version", help="打印版本号")
    p_version.set_defaults(func=_cmd_version)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point (also the `omnilore` console script target).

    Args:
        argv: Optional argument list; defaults to `sys.argv[1:]`.

    Returns:
        Process exit code: 0 on success, 1 on handled OmniLore/runtime errors,
        3 when `audit-bundle` produced a bundle whose integrity check failed
        (「已验证但结论为否」—— deliberately distinct from 1 so scripts can tell
        “the content was tampered with” apart from “the command was wrong”).
    """
    # 先钉住 stdio 编码，再打印任何东西：stdout 被重定向到管道/文件时用的是
    # locale 编码（en-US Windows = cp1252），写中文会直接 UnicodeEncodeError ——
    # `omnilore bootstrap > out.txt` 就是这么崩的，也是 windows-latest CI 变红的根因。
    pin_utf8_std_streams()
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "func", None) is None:
        parser.print_help()
        return EXIT_OK

    # D3 constraint 1/2: strictly opt-in.  When consent is off (the default)
    # this call does zero filesystem writes; it only reads one env var and
    # checks whether a state file exists.  The sub-command is always the first
    # token because this parser has no global options before it.
    command_tokens = list(argv) if argv is not None else sys.argv[1:]
    command_name = command_tokens[0] if command_tokens and not command_tokens[0].startswith("-") else "other"
    try:
        record_command(resolve_data_dir(getattr(args, "data_dir", None)), command_name)
    except Exception:
        pass  # telemetry must never block the main flow

    runner: Callable[[Namespace], int] = args.func
    try:
        return int(runner(args))
    except OmniLoreError as exc:
        sys.stderr.write(f"[omnilore] {exc.code}: {exc.message}\n")
        return EXIT_ERROR
    except (ModelConfigError, EmbeddingConfigurationError) as exc:
        # 配置错**不是**「未预期的错误」：把它归到「配置错误」并原样给出可执行信息，
        # 否则用户会去翻代码而不是改环境变量。
        sys.stderr.write(f"[omnilore] CONFIG_ERROR: {exc}\n")
        return EXIT_ERROR
    except KeyboardInterrupt:  # pragma: no cover - interactive interruption
        sys.stderr.write("\n[omnilore] 已取消\n")
        return EXIT_ERROR
    except Exception as exc:
        sys.stderr.write(f"[omnilore] 未预期的错误：{type(exc).__name__}: {exc}\n")
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover - direct module execution guard
    raise SystemExit(main())
