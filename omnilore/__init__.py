"""OmniLore: AI 原生可审计知识中枢（v0.3.0-alpha，内部代际 v2.3）。"""

# 版本号来自独立模块 `omnilore.version`：子模块（cli / mcp_stdio_server）直接
# `from .version import ...` 即可，无需回过头来 import 包根 —— 那会让
# `python -m omnilore.mcp_stdio_server` 报 RuntimeWarning（QA P2-9）。
from .version import __version__, __version_display__, __version_label__

from typing import Any, List

from .http_worker_api import InternalN8nWorkerGateway
from .bootstrap import (
    BootstrapResult,
    bootstrap_state_path,
    ensure_bootstrapped,
    generate_raw_token,
    has_api_key,
    load_bootstrap_state,
    save_bootstrap_state,
)
from .cli import build_parser as cli_build_parser
from .cli import main as cli_main
from .dashscope_client import DashScopeHTTPTransport, ModelAdapterConfig, estimate_cost_usd
from .daemons import ObsidianVaultWatcher, OutboxRecoveryDaemon
from .engine import (
    ALLOWED_STATE_TRANSITIONS,
    AccessTokenRecord,
    AuthorizationService,
    OmniLoreEngine,
    OmniLoreError,
    OmniLoreMCPGateway,
    DocStatus,
    IllegalStateTransitionError,
    MembershipRecord,
    ModelAdapter,
    OperationType,
    ParsedWikiMarkdown,
    PermissionDeniedError,
    ProfileType,
    RLSDatabaseEngine,
    RLSTransactionConnection,
    ResolvedScope,
    ReviewAction,
    RoleType,
    SecureVaultStorage,
    SpaceRecord,
    SpaceType,
    TaskState,
    VersionConflictError,
    compute_idempotency_key,
    parse_and_validate_wiki_markdown,
    profiled_transaction,
    render_wiki_markdown,
    validate_state_transition,
)
from .ops_manager import OperationsManager
from .parsers import (
    ParsedChunkCandidate,
    ParsedDocumentContent,
    extract_code_symbols,
    extract_health_metrics_from_text,
    extract_outbound_wiki_links,
    parse_document_bytes,
    scan_and_redact_sensitive_text,
    split_markdown_into_chunks,
)
from .textio import (
    canonical_file_hash,
    canonical_text_bytes,
    canonicalize_newlines,
    read_text_canonical,
    write_text_canonical,
)
from .pg_repository import AsyncpgRepository

__all__ = [
    "InternalN8nWorkerGateway",
    "ALLOWED_STATE_TRANSITIONS",
    "AccessTokenRecord",
    "AsyncpgRepository",
    "AuthorizationService",
    "BootstrapResult",
    "bootstrap_state_path",
    "canonical_file_hash",
    "canonical_text_bytes",
    "canonicalize_newlines",
    "cli_build_parser",
    "cli_main",
    "OmniLoreEngine",
    "OmniLoreError",
    "OmniLoreMCPGateway",
    "DashScopeHTTPTransport",
    "DocStatus",
    "ensure_bootstrapped",
    "generate_raw_token",
    "has_api_key",
    "IllegalStateTransitionError",
    "MCPJsonRpcServer",
    "MCP_TOOL_DEFINITIONS",
    "MembershipRecord",
    "ModelAdapter",
    "ModelAdapterConfig",
    "ObsidianVaultWatcher",
    "OperationType",
    "OperationsManager",
    "OutboxRecoveryDaemon",
    "ParsedChunkCandidate",
    "ParsedDocumentContent",
    "ParsedWikiMarkdown",
    "PermissionDeniedError",
    "ProfileType",
    "RLSDatabaseEngine",
    "RLSTransactionConnection",
    "ResolvedScope",
    "ReviewAction",
    "RoleType",
    "SecureVaultStorage",
    "SpaceRecord",
    "SpaceType",
    "TaskState",
    "VersionConflictError",
    "compute_idempotency_key",
    "estimate_cost_usd",
    "extract_code_symbols",
    "extract_health_metrics_from_text",
    "extract_outbound_wiki_links",
    "load_bootstrap_state",
    "parse_and_validate_wiki_markdown",
    "parse_document_bytes",
    "profiled_transaction",
    "read_text_canonical",
    "render_wiki_markdown",
    "save_bootstrap_state",
    "scan_and_redact_sensitive_text",
    "split_markdown_into_chunks",
    "validate_state_transition",
    "write_text_canonical",
    "__version__",
    "__version_display__",
    "__version_label__",
]


# ---------------------------------------------------------------------------
# PEP 562 惰性再导出 (QA P2-9)
# ---------------------------------------------------------------------------
# `mcp_stdio_server` 会被 `python -m omnilore.mcp_stdio_server` 直接执行。若包根
# 在初始化时就 eager import 它，runpy 会发现子模块已在 sys.modules 中并抛出
# RuntimeWarning。改为按需加载：既消除告警，`from omnilore import MCPJsonRpcServer`
# 这类既有写法也完全不受影响。
_LAZY_RPCEXPORTS = {
    "MCPJsonRpcServer": ("mcp_stdio_server", "MCPJsonRpcServer"),
    "MCP_TOOL_DEFINITIONS": ("mcp_stdio_server", "MCP_TOOL_DEFINITIONS"),
}


def __getattr__(name: str) -> Any:
    """Lazily resolve the MCP stdio re-exports declared in `_LAZY_RPCEXPORTS`."""
    entry = _LAZY_RPCEXPORTS.get(name)
    if entry is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr_name = entry
    from importlib import import_module

    value = getattr(import_module(f".{module_name}", __name__), attr_name)
    globals()[name] = value  # cache so后续访问不再走 import
    return value


def __dir__() -> List[str]:
    """Include the lazy re-exports so `dir(omnilore)` stays complete."""
    return sorted(set(globals()) | set(__all__))
