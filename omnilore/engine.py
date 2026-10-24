"""
OmniLore v2.3 Complete Core Engine (Phase 0 + Phase 1 + Phase 2 + Phase 3)
============================================================================
Implements 100% of the functional & architectural requirements in PRD v2.3:
  - Phase 0: Multi-layer Permission Intersection, RLS Transaction Context,
             CAS File Gateway, Unified ModelAdapter (Live DashScope + Local Fallback)
  - Phase 1: Multi-Format Ingestion (PDF/Image/Markdown/Code Symbols), Saga Outbox,
             Zero-Downtime Version Switch, All 7 Review Actions (APPROVE_REVIEW,
             RECLASSIFY, SPLIT_DOCUMENT, FIX_EXTRACTED_FIELD, REVERT_VERSION,
             PROMOTE_COPY, CANCEL_TASK), Obsidian Sync & `_dashboard_控制台.md`
  - Phase 2: Family Member Management (FR-13), Health Report Metric Extraction &
             Verification Pipeline (FR-14), TTL & Knowledge Lint Inspector (FR-12),
             Backup / Restore / Index Rebuild / Storage Reconciliation (Section 12)
  - Phase 3: Multi-Level Enterprise Org Tree (`ltree`) Public Read Inheritance (FR-16)
             and Sensitive Data Redaction Preview & Promotion (FR-15)
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import copy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Set, Tuple
import uuid
import yaml

from .dashscope_client import DashScopeHTTPTransport, ModelAdapterConfig, estimate_cost_usd
from .embedding_provider import (
    EmbeddingConfigurationError,
    EmbeddingProvider,
    LocalDeterministicEmbeddingProvider,
    build_embedding_provider,
    validate_embedding_dimensions,
)
from .textio import (
    lexical_tokens,
    read_text_canonical,
    write_text_canonical,
)
from .ops_manager import OperationsManager
from .parsers import (
    extract_code_symbols,
    extract_health_metrics_from_text,
    extract_outbound_wiki_links,
    parse_document_bytes,
    scan_and_redact_sensitive_text,
)
from .runtime_index import TRACKED_TABLES as RUNTIME_INDEX_TRACKED_TABLES
from .runtime_index import persist_runtime_index


# =============================================================================
# 1. Enums, Constants & State Machine Rules (Section 2, 4.2, 4.3, 9.4)
# =============================================================================

class SpaceType(str, Enum):
    PERSONAL = "PERSONAL"
    FAMILY = "FAMILY"
    ORG_PUBLIC = "ORG_PUBLIC"
    ORG_RESTRICTED = "ORG_RESTRICTED"


class ProfileType(str, Enum):
    WORK = "WORK"
    FAMILY = "FAMILY"
    OMNI = "OMNI"


class RoleType(str, Enum):
    READER = "READER"
    WRITER = "WRITER"
    ADMIN = "ADMIN"


class TaskState(str, Enum):
    RECEIVED = "RECEIVED"
    PRECHECKING = "PRECHECKING"
    ROUTING = "ROUTING"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    STAGED = "STAGED"
    COMMITTING = "COMMITTING"
    COMMITTED = "COMMITTED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_FINAL = "FAILED_FINAL"
    CANCELLED = "CANCELLED"
    REVERTED = "REVERTED"


class DocStatus(str, Enum):
    PENDING_PUBLISH = "PENDING_PUBLISH"
    PUBLISHED_CURRENT = "PUBLISHED_CURRENT"
    SUPERSEDED = "SUPERSEDED"
    SUPPLEMENT_ACTIVE = "SUPPLEMENT_ACTIVE"
    ARCHIVED = "ARCHIVED"
    REVERTED = "REVERTED"


class OperationType(str, Enum):
    INGEST_FILE = "INGEST_FILE"
    COMMIT_NOTE = "COMMIT_NOTE"
    REPLACE_VERSION = "REPLACE_VERSION"
    SUPPLEMENT_DOC = "SUPPLEMENT_DOC"
    REPROCESS = "REPROCESS"


class ReviewAction(str, Enum):
    APPROVE_REVIEW = "APPROVE_REVIEW"
    RECLASSIFY = "RECLASSIFY"
    SPLIT_DOCUMENT = "SPLIT_DOCUMENT"
    FIX_EXTRACTED_FIELD = "FIX_EXTRACTED_FIELD"
    REVERT_VERSION = "REVERT_VERSION"
    PROMOTE_COPY = "PROMOTE_COPY"
    CANCEL_TASK = "CANCEL_TASK"


VALID_CATEGORIES: Set[str] = {
    "01_Work",
    "02_Life",
    "03_Family",
    "04_Health_Archive",
}

SPACE_ID_PATTERN = re.compile(r"^[a-z0-9_]{3,64}$")


# =============================================================================
# 1.1 Canonical Text I/O Guard (Cross-Platform LF)
# -----------------------------------------------------------------------------
# All markdown / dashboard / manifest writes MUST use `write_text_canonical` and
# all markdown reads MUST use `read_text_canonical` (imported from
# `omnilore.textio` and re-exported here). `Path.write_text()` without
# `newline=""` translates `\n` -> `\r\n` on Windows, which desynchronises
# "bytes on disk" from "bytes we hashed" (`documents.file_hash`) and breaks the
# Section 12.3 reconciliation. See `omnilore/textio.py` and `docs/DECISIONS.md`.
# =============================================================================

VERSION_KEY_PATTERN = re.compile(r"^[a-zA-Z0-9_.\-]{3,128}$")

ALLOWED_STATE_TRANSITIONS: Dict[TaskState, Set[TaskState]] = {
    TaskState.RECEIVED: {TaskState.PRECHECKING},
    TaskState.PRECHECKING: {
        TaskState.ROUTING,
        TaskState.NEEDS_REVIEW,
        TaskState.FAILED_RETRYABLE,
        TaskState.FAILED_FINAL,
        TaskState.CANCELLED,
    },
    TaskState.ROUTING: {
        TaskState.STAGED,
        TaskState.NEEDS_REVIEW,
        TaskState.FAILED_RETRYABLE,
        TaskState.FAILED_FINAL,
        TaskState.CANCELLED,
    },
    TaskState.NEEDS_REVIEW: {
        TaskState.ROUTING,
        TaskState.STAGED,
        TaskState.CANCELLED,
    },
    TaskState.STAGED: {
        TaskState.COMMITTING,
        TaskState.FAILED_RETRYABLE,
        TaskState.CANCELLED,
    },
    TaskState.COMMITTING: {
        TaskState.COMMITTED,
        TaskState.FAILED_RETRYABLE,
    },
    TaskState.FAILED_RETRYABLE: {
        TaskState.PRECHECKING,
        TaskState.ROUTING,
        TaskState.STAGED,
        TaskState.COMMITTING,
        TaskState.FAILED_FINAL,
        TaskState.CANCELLED,
    },
    TaskState.COMMITTED: {
        TaskState.REVERTED,
    },
    TaskState.FAILED_FINAL: set(),
    TaskState.CANCELLED: set(),
    TaskState.REVERTED: set(),
}


class OmniLoreError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


class PermissionDeniedError(OmniLoreError):
    def __init__(self, message: str = "Access denied by OmniLore authorization policy"):
        super().__init__("PERMISSION_DENIED", message)


class IllegalStateTransitionError(OmniLoreError):
    def __init__(self, current: TaskState, target: TaskState):
        super().__init__(
            "ILLEGAL_STATE_TRANSITION",
            f"Cannot transition ingestion task from {current.value} to {target.value}",
        )


class VersionConflictError(OmniLoreError):
    def __init__(self, message: str):
        super().__init__("VERSION_CONFLICT", message)


def validate_state_transition(current: TaskState, target: TaskState) -> None:
    if target not in ALLOWED_STATE_TRANSITIONS.get(current, set()):
        raise IllegalStateTransitionError(current, target)


def compute_idempotency_key(
    user_id: str,
    canonical_file_hash: str,
    target_or_route_context: str,
    operation_type: OperationType,
    reprocess_generation: int = 0,
) -> str:
    payload = "|".join([
        user_id.strip(),
        canonical_file_hash.strip().lower(),
        target_or_route_context.strip(),
        operation_type.value,
        str(reprocess_generation),
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# =============================================================================
# 2. Markdown Frontmatter Contract & Validator (Section 5.5)
# =============================================================================

# ---------------------------------------------------------------------------
# R-16（P2 主权）：受保护字段与冲突副本目录**都归用户所有，可以关掉**
# ---------------------------------------------------------------------------
# 设计前提：Obsidian 重度用户（P2）的笔记是他们的资产。系统为了可审计性需要往
# frontmatter 里写若干系统字段，但「我能不能改我自己的笔记」这个问题上，主权在
# 用户手里，不在我们手里。因此：
#   1. 受保护字段集合**可配置**，甚至可以整体关闭（环境变量 `OMNILORE_PROTECTED_FRONTMATTER`）；
#   2. 冲突副本不再散落在用户的 vault 里，统一落到隐藏目录（环境变量 `OMNILORE_CONFLICT_DIR`）。
# 唯一不可关闭的是 `doc_id`：它是同步时定位目标文档的身份锚点，放开等于让文本
# 编辑改写身份，这不是主权问题而是正确性问题。
DEFAULT_PROTECTED_FRONTMATTER_KEYS: Set[str] = {
    "doc_id",
    "space_id",
    "series_id",
    "version_key",
    "version_label",
    "source_asset_ids",
    "source_task_id",
    "content_revision",
    "review_status",
}

# 身份锚点：永远受保护，不参与用户配置。
IMMUTABLE_PROTECTED_FRONTMATTER_KEYS: Set[str] = {"doc_id"}

# 可与数据库当前值逐字段比对的「归属类」字段：其余受保护字段（如 source_asset_ids）
# 只做「必须存在」的契约校验，不做值比对 —— 放开它们不会产生歧义。
COMPARABLE_PROTECTED_FRONTMATTER_KEYS: Set[str] = {"space_id", "series_id", "version_key", "version_label"}

EDITABLE_FRONTMATTER_KEYS: Set[str] = {
    "title",
    "summary",
    "tags",
}

# 环境变量名（同步到 .env.example / README）
PROTECTED_FRONTMATTER_ENV: str = "OMNILORE_PROTECTED_FRONTMATTER"
CONFLICT_COPY_DIR_ENV: str = "OMNILORE_CONFLICT_DIR"

# 默认冲突目录：以 `.` 开头，因此 **Obsidian 默认忽略**它（点号开头的目录不进
# Obsidian 的文件树），从而不污染 P2 用户的 vault。
# 注意：Windows 资源管理器**仍然可见** —— NTFS 不因点号隐藏目录，别对 Windows
# 用户说「文件系统层也隐藏」（那是事实错误，本项目 CI 含 windows-latest）。
DEFAULT_CONFLICT_COPY_DIR: str = ".omnilore/conflicts"

# 关闭保护的写法（大小写不敏感）
_PROTECTION_OFF_TOKENS: Set[str] = {"none", "off", "0", "false", "disabled"}


def _with_identity_anchor(keys: Set[str]) -> Set[str]:
    """把不可关闭的身份锚点（`doc_id`）并入受保护集合。

    Why a helper instead of inlining `keys | IMMUTABLE_PROTECTED_FRONTMATTER_KEYS`:
    `parse_protected_frontmatter_keys` 有三条返回路径（未设置 / 整体关闭 / 白名单），
    只有前两条写过并集。「doc_id 永不放开」是正确性红线，不能只靠每个分支各自记得
    写一遍 —— 一旦有人新增第四条分支忘了并集，红线就静默失守。因此**所有**返回
    路径都过这个函数，测试也直接对三条分支逐一断言。

    Args:
        keys: 某个分支解析出的受保护字段集合（可能不含 `doc_id`）。

    Returns:
        保证含 `doc_id` 的受保护字段集合。
    """
    return set(keys) | IMMUTABLE_PROTECTED_FRONTMATTER_KEYS


def parse_protected_frontmatter_keys(raw_value: Optional[str]) -> Set[str]:
    """把 `OMNILORE_PROTECTED_FRONTMATTER` 的取值解析为受保护字段集合。

    Args:
        raw_value: 环境变量原始取值。逗号分隔的字段名；`none` / `off` / 空串表示
            **整体关闭保护**（仅保留不可关闭的 `doc_id`）；`None` 表示未设置，
            使用默认集合。

    Returns:
        受保护的 frontmatter 字段名集合，必含 `doc_id`。

    Raises:
        OmniLoreError: 取值中出现未知字段名时抛出（配置错误必须响亮失败，
            而不是被静默忽略成「保护已关闭」）。
    """
    if raw_value is None:
        return _with_identity_anchor(set(DEFAULT_PROTECTED_FRONTMATTER_KEYS))
    value = raw_value.strip()
    if not value or value.lower() in _PROTECTION_OFF_TOKENS:
        return _with_identity_anchor(set())
    keys = {part.strip() for part in value.split(",") if part.strip()}
    unknown = keys - DEFAULT_PROTECTED_FRONTMATTER_KEYS
    if unknown:
        raise OmniLoreError(
            "CONFIG_ERROR",
            f"{PROTECTED_FRONTMATTER_ENV} 含未知字段名 {sorted(unknown)}；"
            f"可选项：{sorted(DEFAULT_PROTECTED_FRONTMATTER_KEYS)}，"
            "或设为 none 整体关闭保护",
        )
    return _with_identity_anchor(keys)


def get_protected_frontmatter_keys() -> Set[str]:
    """读取当前进程生效的受保护 frontmatter 字段集合（每次都重新读环境变量，便于测试）。"""
    return parse_protected_frontmatter_keys(os.environ.get(PROTECTED_FRONTMATTER_ENV))


def get_editable_frontmatter_keys(protected_keys: Optional[Set[str]] = None) -> Set[str]:
    """返回当前可自由编辑的字段集合：原生可编辑字段 + 用户主动放开的系统字段。"""
    protected = protected_keys if protected_keys is not None else get_protected_frontmatter_keys()
    return EDITABLE_FRONTMATTER_KEYS | (DEFAULT_PROTECTED_FRONTMATTER_KEYS - protected)


def get_allowed_frontmatter_keys(protected_keys: Optional[Set[str]] = None) -> Set[str]:
    """返回 schema 允许出现的全部字段：受保护字段 + 可编辑字段。"""
    protected = protected_keys if protected_keys is not None else get_protected_frontmatter_keys()
    return protected | get_editable_frontmatter_keys(protected)


def get_required_frontmatter_keys(protected_keys: Optional[Set[str]] = None) -> Set[str]:
    """返回解析磁盘 Markdown 时**必须存在**的字段：即当前受保护的字段。"""
    protected = protected_keys if protected_keys is not None else get_protected_frontmatter_keys()
    return set(protected)


def parse_conflict_copy_dir(raw_value: Optional[str]) -> str:
    """解析冲突副本隐藏目录（相对 `spaces/<space_id>/wiki/`）。

    Args:
        raw_value: 环境变量原始取值；`None` 或空串使用默认值 `.omnilore/conflicts`。

    Returns:
        规范化后的相对目录（不含首尾斜杠）。

    Raises:
        OmniLoreError: 取值是绝对路径或含 `..` 时抛出 —— 冲突目录同样受文件网关约束。
    """
    value = (raw_value or "").strip().strip("/\\")
    if not value:
        return DEFAULT_CONFLICT_COPY_DIR
    if "\x00" in value or ".." in value.split("/") or value.startswith(("/", "\\")):
        raise OmniLoreError(
            "CONFIG_ERROR",
            f"{CONFLICT_COPY_DIR_ENV} 必须是 vault 内的相对目录且不含 '..'：{raw_value!r}",
        )
    return value


def get_conflict_copy_dir() -> str:
    """读取当前进程生效的冲突副本隐藏目录。"""
    return parse_conflict_copy_dir(os.environ.get(CONFLICT_COPY_DIR_ENV))


def build_conflict_copy_rel_path(
    wiki_rel_path: str,
    content_revision: int,
    conflict_dir: Optional[str] = None,
) -> str:
    """构造冲突副本的相对路径：`<space>/wiki/<隐藏目录>/<原名>.conflict_r<N>.md`。

    Args:
        wiki_rel_path: 原文档的相对路径，形如 `<space_id>/wiki/<name>.md`。
        content_revision: 磁盘上编辑器基于的（过期的）内容版本号。
        conflict_dir: 覆盖隐藏目录；为 None 时取当前配置。

    Returns:
        冲突副本的相对路径，仍然满足 `write_wiki_by_record` 的
        `<space_id>/wiki/...` 前缀约束，因此**继续受文件网关保护**。

    Raises:
        OmniLoreError: `wiki_rel_path` 不是合法的 `<space_id>/wiki/...` 形态时抛出。
    """
    space_id, sep, tail = wiki_rel_path.partition("/wiki/")
    if not sep or not space_id or not tail:
        raise OmniLoreError("INVALID_WIKI_REL_PATH", f"非法的 wiki 相对路径：{wiki_rel_path!r}")
    stem = tail[:-3] if tail.endswith(".md") else tail
    hidden_dir = (conflict_dir or get_conflict_copy_dir()).strip().strip("/\\")
    return f"{space_id}/wiki/{hidden_dir}/{stem}.conflict_r{content_revision}.md"


# 兼容既有引用：这两个名字在 v0.3.0-alpha 早期就是模块级常量，保留其语义
# （「默认集合」），但**动态生效**的值一律走上面的 getter。
PROTECTED_FRONTMATTER_KEYS: Set[str] = set(DEFAULT_PROTECTED_FRONTMATTER_KEYS)
ALLOWED_FRONTMATTER_KEYS: Set[str] = DEFAULT_PROTECTED_FRONTMATTER_KEYS | EDITABLE_FRONTMATTER_KEYS


@dataclass
class ParsedWikiMarkdown:
    frontmatter: Dict[str, Any]
    body: str


def render_wiki_markdown(frontmatter: Dict[str, Any], body: str) -> str:
    unknown_keys = set(frontmatter.keys()) - get_allowed_frontmatter_keys()
    if unknown_keys:
        raise OmniLoreError(
            "FRONTMATTER_SCHEMA_ERROR",
            f"Unknown frontmatter keys rejected: {sorted(unknown_keys)}",
        )
    ordered_keys = [
        "doc_id",
        "space_id",
        "series_id",
        "version_key",
        "version_label",
        "source_asset_ids",
        "source_task_id",
        "content_revision",
        "review_status",
        "title",
        "summary",
        "tags",
    ]
    ordered_dict = {k: frontmatter[k] for k in ordered_keys if k in frontmatter}
    yaml_str = yaml.safe_dump(ordered_dict, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{yaml_str}\n---\n\n{body.strip()}\n"


def parse_and_validate_wiki_markdown(raw_markdown: str) -> ParsedWikiMarkdown:
    if not raw_markdown.startswith("---\n"):
        raise OmniLoreError("FRONTMATTER_SCHEMA_ERROR", "Missing YAML frontmatter header")
    parts = raw_markdown.split("\n---\n", 1)
    if len(parts) != 2:
        raise OmniLoreError("FRONTMATTER_SCHEMA_ERROR", "Unclosed YAML frontmatter block")
    yaml_block = parts[0][4:]
    body = parts[1].lstrip("\n")
    try:
        parsed = yaml.safe_load(yaml_block)
    except Exception as exc:
        raise OmniLoreError("FRONTMATTER_SCHEMA_ERROR", f"Invalid YAML syntax: {exc}") from exc

    if not isinstance(parsed, dict):
        raise OmniLoreError("FRONTMATTER_SCHEMA_ERROR", "Frontmatter must be a YAML mapping")

    # R-16：schema 边界随用户配置收缩/放开。放开的系统字段从「受保护」变为「可编辑」，
    # 因此不会出现「既不在白名单、又被拒绝」的死区。
    allowed = get_allowed_frontmatter_keys()
    unknown = set(parsed.keys()) - allowed
    if unknown:
        raise OmniLoreError(
            "FRONTMATTER_SCHEMA_ERROR",
            f"Arbitrary unknown frontmatter fields are prohibited: {sorted(unknown)}",
        )

    required = get_required_frontmatter_keys()
    missing = required - set(parsed.keys())
    if missing:
        raise OmniLoreError(
            "FRONTMATTER_SCHEMA_ERROR",
            f"Missing required system frontmatter fields: {sorted(missing)}",
        )

    if "content_revision" in required and (
        not isinstance(parsed.get("content_revision"), int) or parsed["content_revision"] < 1
    ):
        raise OmniLoreError("FRONTMATTER_SCHEMA_ERROR", "content_revision must be a positive integer")
    if not isinstance(parsed.get("tags", []), list):
        raise OmniLoreError("FRONTMATTER_SCHEMA_ERROR", "tags must be a list of strings")

    return ParsedWikiMarkdown(frontmatter=parsed, body=body)


# =============================================================================
# 3. Security, Authorization & Phase 3 Multi-Level Org Tree Inheritance
# =============================================================================

@dataclass(frozen=True)
class ResolvedScope:
    user_id: str
    token_id: str
    profile_type: ProfileType
    read_spaces: Set[str]
    write_spaces: Set[str]
    read_scopes: Set[str]
    write_scopes: Set[str]
    expires_at: float

    def can_read(self, space_id: str, category: str) -> bool:
        return (
            space_id in self.read_spaces
            and (f"{space_id}:{category}" in self.read_scopes or f"{space_id}:*" in self.read_scopes)
        )

    def can_write(self, space_id: str, category: str) -> bool:
        return (
            space_id in self.write_spaces
            and (f"{space_id}:{category}" in self.write_scopes or f"{space_id}:*" in self.write_scopes)
        )


@dataclass
class SpaceRecord:
    space_id: str
    space_type: SpaceType
    parent_space_id: Optional[str]
    vault_rel_path: str
    org_tree_path: Optional[str] = None  # e.g. "org.engineering.backend"
    default_ttl_days: Optional[int] = None
    allowed_categories: Set[str] = field(default_factory=lambda: set(VALID_CATEGORIES))
    external_llm_policy: Dict[str, str] = field(
        default_factory=lambda: {
            "01_Work": "ALLOW",
            "02_Life": "ALLOW",
            "03_Family": "REQUIRE_REVIEW",
            "04_Health_Archive": "DENY_UNLESS_APPROVED",
        }
    )


@dataclass
class MembershipRecord:
    membership_id: str
    user_id: str
    space_id: str
    role: RoleType
    allowed_categories: Set[str]
    inherit_org_public_read: bool = False
    granted_by: Optional[str] = None
    revoked_at: Optional[datetime] = None


@dataclass
class AccessTokenRecord:
    token_id: str
    token_hash: str
    user_id: str
    profile_type: ProfileType
    allowed_read_spaces: Set[str]
    allowed_write_spaces: Set[str]
    allowed_categories: Set[str]
    expires_at: datetime
    revoked_at: Optional[datetime] = None


class AuthorizationService:
    """
    Computes effective permissions per request with active cache invalidation (SLO <= 5s)
    and supports Phase 3 multi-level organization tree (`ltree`) ancestor read inheritance.
    """

    def __init__(self, cache_ttl_seconds: float = 4.0):
        self.cache_ttl_seconds = min(cache_ttl_seconds, 5.0)
        self._spaces: Dict[str, SpaceRecord] = {}
        self._memberships: Dict[Tuple[str, str], MembershipRecord] = {}
        self._tokens_by_hash: Dict[str, AccessTokenRecord] = {}
        self._scope_cache: Dict[str, ResolvedScope] = {}

    @staticmethod
    def hash_token(raw_token: str) -> str:
        return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    def register_space(self, space: SpaceRecord) -> None:
        if not SPACE_ID_PATTERN.match(space.space_id):
            raise OmniLoreError("INVALID_SPACE_ID", f"Invalid space_id format: {space.space_id}")
        self._spaces[space.space_id] = space
        self.invalidate_all_caches()

    def grant_membership(self, membership: MembershipRecord) -> None:
        self._memberships[(membership.user_id, membership.space_id)] = membership
        self.invalidate_user_cache(membership.user_id)

    def revoke_membership(self, user_id: str, space_id: str) -> None:
        key = (user_id, space_id)
        if key in self._memberships:
            self._memberships[key].revoked_at = datetime.now(timezone.utc)
        self.invalidate_user_cache(user_id)

    def issue_token(
        self,
        raw_token: str,
        user_id: str,
        profile_type: ProfileType,
        allowed_read_spaces: Set[str],
        allowed_write_spaces: Set[str],
        allowed_categories: Set[str],
        ttl_seconds: int = 3600,
    ) -> AccessTokenRecord:
        token_hash = self.hash_token(raw_token)
        rec = AccessTokenRecord(
            token_id=str(uuid.uuid4()),
            token_hash=token_hash,
            user_id=user_id,
            profile_type=profile_type,
            allowed_read_spaces=set(allowed_read_spaces),
            allowed_write_spaces=set(allowed_write_spaces),
            allowed_categories=set(allowed_categories),
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds),
        )
        self._tokens_by_hash[token_hash] = rec
        return rec

    def revoke_token(self, raw_token: str) -> None:
        token_hash = self.hash_token(raw_token)
        rec = self._tokens_by_hash.get(token_hash)
        if rec:
            rec.revoked_at = datetime.now(timezone.utc)
        self._scope_cache.pop(token_hash, None)

    def invalidate_user_cache(self, user_id: str) -> None:
        to_remove = [k for k, v in self._scope_cache.items() if v.user_id == user_id]
        for k in to_remove:
            self._scope_cache.pop(k, None)

    def invalidate_all_caches(self) -> None:
        self._scope_cache.clear()

    def _find_org_public_ancestors(self, space: SpaceRecord) -> List[SpaceRecord]:
        """
        Phase 3 (FR-16): Multi-level organization tree (`ltree` or `parent_space_id` chain)
        ancestor lookup. Only `ORG_PUBLIC` spaces are eligible for read inheritance;
        `ORG_RESTRICTED`, `FAMILY`, and `PERSONAL` spaces never inherit across boundaries.
        """
        ancestors: List[SpaceRecord] = []
        seen: Set[str] = {space.space_id}

        # 1. Walk parent_space_id chain
        curr_parent_id = space.parent_space_id
        while curr_parent_id and curr_parent_id not in seen:
            seen.add(curr_parent_id)
            parent_sp = self._spaces.get(curr_parent_id)
            if not parent_sp:
                break
            if parent_sp.space_type == SpaceType.ORG_PUBLIC:
                ancestors.append(parent_sp)
            curr_parent_id = parent_sp.parent_space_id

        # 2. Check ltree prefix match (e.g. "org.eng" is an ancestor of "org.eng.backend")
        if space.org_tree_path:
            for cand in self._spaces.values():
                if (
                    cand.space_id not in seen
                    and cand.space_type == SpaceType.ORG_PUBLIC
                    and cand.org_tree_path
                    and space.org_tree_path.startswith(cand.org_tree_path + ".")
                ):
                    seen.add(cand.space_id)
                    ancestors.append(cand)
        return ancestors

    def _profile_allowed_categories(self, profile: ProfileType, space: SpaceRecord) -> Set[str]:
        if profile == ProfileType.WORK:
            if space.space_type == SpaceType.FAMILY:
                return set()
            if space.space_type in (SpaceType.PERSONAL, SpaceType.ORG_PUBLIC, SpaceType.ORG_RESTRICTED):
                return {"01_Work"} & space.allowed_categories
            return set()
        if profile == ProfileType.FAMILY:
            if space.space_type != SpaceType.FAMILY:
                return set()
            return {"03_Family", "04_Health_Archive"} & space.allowed_categories
        if profile == ProfileType.OMNI:
            return set(space.allowed_categories)
        return set()

    def resolve_scope(self, raw_token: Optional[str]) -> ResolvedScope:
        if not raw_token or not isinstance(raw_token, str):
            raise PermissionDeniedError("Missing or empty authentication token")

        token_hash = self.hash_token(raw_token)
        now_mono = time.monotonic()
        cached = self._scope_cache.get(token_hash)
        if cached and cached.expires_at > now_mono:
            return cached

        token_rec = self._tokens_by_hash.get(token_hash)
        now_utc = datetime.now(timezone.utc)
        if not token_rec:
            raise PermissionDeniedError("Invalid authentication token")
        if token_rec.revoked_at is not None:
            raise PermissionDeniedError("Authentication token has been revoked")
        if token_rec.expires_at <= now_utc:
            raise PermissionDeniedError("Authentication token has expired")

        read_spaces: Set[str] = set()
        write_spaces: Set[str] = set()
        read_scopes: Set[str] = set()
        write_scopes: Set[str] = set()

        for (uid, space_id), mem in self._memberships.items():
            if uid != token_rec.user_id or mem.revoked_at is not None:
                continue
            space = self._spaces.get(space_id)
            if not space:
                continue

            profile_cats = self._profile_allowed_categories(token_rec.profile_type, space)
            effective_cats = (
                token_rec.allowed_categories
                & mem.allowed_categories
                & space.allowed_categories
                & profile_cats
            )
            if not effective_cats:
                continue

            if space_id in token_rec.allowed_read_spaces:
                read_spaces.add(space_id)
                for cat in effective_cats:
                    read_scopes.add(f"{space_id}:{cat}")

            if (
                space_id in token_rec.allowed_write_spaces
                and mem.role in (RoleType.WRITER, RoleType.ADMIN)
            ):
                write_spaces.add(space_id)
                for cat in effective_cats:
                    write_scopes.add(f"{space_id}:{cat}")
                    read_spaces.add(space_id)
                    read_scopes.add(f"{space_id}:{cat}")

            # Phase 3 (FR-16): Multi-level ORG_PUBLIC ancestor read inheritance (strictly read-only!)
            if mem.inherit_org_public_read:
                for anc_space in self._find_org_public_ancestors(space):
                    if anc_space.space_id in token_rec.allowed_read_spaces:
                        anc_cats = (
                            token_rec.allowed_categories
                            & anc_space.allowed_categories
                            & self._profile_allowed_categories(token_rec.profile_type, anc_space)
                        )
                        if anc_cats:
                            read_spaces.add(anc_space.space_id)
                            for cat in anc_cats:
                                read_scopes.add(f"{anc_space.space_id}:{cat}")

        if not read_scopes and not write_scopes:
            raise PermissionDeniedError("Resolved permission scope is empty")

        resolved = ResolvedScope(
            user_id=token_rec.user_id,
            token_id=token_rec.token_id,
            profile_type=token_rec.profile_type,
            read_spaces=frozenset(read_spaces),  # type: ignore[arg-type]
            write_spaces=frozenset(write_spaces),  # type: ignore[arg-type]
            read_scopes=frozenset(read_scopes),  # type: ignore[arg-type]
            write_scopes=frozenset(write_scopes),  # type: ignore[arg-type]
            expires_at=now_mono + self.cache_ttl_seconds,
        )
        self._scope_cache[token_hash] = resolved
        return resolved


@asynccontextmanager
async def profiled_transaction(pool: Any, resolved_scope: ResolvedScope) -> AsyncIterator[Any]:
    if resolved_scope is None or not resolved_scope.user_id:
        raise PermissionDeniedError("Cannot open database transaction without resolved scope")

    for sp in resolved_scope.read_spaces | resolved_scope.write_spaces:
        if not SPACE_ID_PATTERN.match(sp):
            raise PermissionDeniedError(f"Malformed space_id in scope: {sp}")

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.current_user_id', '', true), "
                "set_config('app.allowed_read_spaces', '', true), "
                "set_config('app.allowed_read_scopes', '', true), "
                "set_config('app.allowed_write_spaces', '', true), "
                "set_config('app.allowed_write_scopes', '', true)"
            )
            await conn.execute("SELECT set_config('app.current_user_id', $1, true)", resolved_scope.user_id)
            await conn.execute(
                "SELECT set_config('app.allowed_read_spaces', $1, true)",
                ",".join(sorted(resolved_scope.read_spaces)),
            )
            await conn.execute(
                "SELECT set_config('app.allowed_read_scopes', $1, true)",
                ",".join(sorted(resolved_scope.read_scopes)),
            )
            await conn.execute(
                "SELECT set_config('app.allowed_write_spaces', $1, true)",
                ",".join(sorted(resolved_scope.write_spaces)),
            )
            await conn.execute(
                "SELECT set_config('app.allowed_write_scopes', $1, true)",
                ",".join(sorted(resolved_scope.write_scopes)),
            )
            yield conn


# =============================================================================
# 4. Transactional RLS Database Engine
# =============================================================================

class RLSTransactionConnection:
    def __init__(self, db: "RLSDatabaseEngine"):
        self._db = db
        self.guc: Dict[str, str] = {}
        self._snapshot: Optional[Dict[str, Any]] = None

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator["RLSTransactionConnection"]:
        async with self._db._lock:
            self._snapshot = copy.deepcopy(self._db._state)
            dirty_snapshot = set(self._db._dirty_tables)
            self.guc.clear()
            try:
                yield self
            except Exception:
                if self._snapshot is not None:
                    self._db._state = self._snapshot
                # 回滚时脏标记也要回滚，否则下一次无关提交会白刷一次索引。
                self._db._dirty_tables = dirty_snapshot
                raise
            else:
                self._flush_runtime_index()
            finally:
                self._snapshot = None
                self.guc.clear()

    def _flush_runtime_index(self) -> None:
        """提交成功后把「文档 ↔ 资产」映射刷到 `<data_dir>/ops/index/`。

        为什么放在事务出口而不是某个具体方法里：入库、版本替换、Obsidian 回写、
        审核动作都会改这几张表，逐个加刷新点必然漏一个（漏掉的代价是证据包缺数据）。
        这里只有一个入口，且只在**真正写过**被跟踪的表时才落盘。

        失败**不抛出**：索引是派生物，写不进去不该让入库失败；但必须留下可见痕迹
        （stderr），否则用户会在导出时才不明所以。
        """
        tracked = self._db._dirty_tables.intersection(RUNTIME_INDEX_TRACKED_TABLES)
        if not tracked or self._db.on_commit is None:
            return
        self._db._dirty_tables.clear()
        try:
            self._db.on_commit()
        except Exception as exc:  # pragma: no cover - 只在磁盘异常时触发
            sys.stderr.write(
                f"[omnilore] 运行时索引写入失败（不影响入库，但会影响 audit-bundle 的跨进程导出）："
                f"{type(exc).__name__}: {exc}\n"
            )
            sys.stderr.flush()

    async def execute(self, sql: str, *args: Any) -> str:
        if "set_config('app.current_user_id', ''" in sql:
            for k in [
                "app.current_user_id",
                "app.allowed_read_spaces",
                "app.allowed_read_scopes",
                "app.allowed_write_spaces",
                "app.allowed_write_scopes",
            ]:
                self.guc[k] = ""
            return "SET"

        match = re.search(r"set_config\('([^']+)',\s*\$1,\s*true\)", sql)
        if match and args:
            self.guc[match.group(1)] = str(args[0])
            return "SET"
        return "OK"

    def _can_read(self, space_id: Optional[str], category: Optional[str]) -> bool:
        if not space_id or not category:
            return False
        read_spaces = set(filter(None, self.guc.get("app.allowed_read_spaces", "").split(",")))
        read_scopes = set(filter(None, self.guc.get("app.allowed_read_scopes", "").split(",")))
        write_spaces = set(filter(None, self.guc.get("app.allowed_write_spaces", "").split(",")))
        write_scopes = set(filter(None, self.guc.get("app.allowed_write_scopes", "").split(",")))
        in_read = space_id in read_spaces and (
            f"{space_id}:{category}" in read_scopes or f"{space_id}:*" in read_scopes
        )
        in_write = space_id in write_spaces and (
            f"{space_id}:{category}" in write_scopes or f"{space_id}:*" in write_scopes
        )
        return in_read or in_write

    def _can_write(self, space_id: Optional[str], category: Optional[str]) -> bool:
        if not space_id or not category:
            return False
        write_spaces = set(filter(None, self.guc.get("app.allowed_write_spaces", "").split(",")))
        write_scopes = set(filter(None, self.guc.get("app.allowed_write_scopes", "").split(",")))
        return space_id in write_spaces and (
            f"{space_id}:{category}" in write_scopes or f"{space_id}:*" in write_scopes
        )

    def select_rows(self, table: str, predicate: Optional[Callable[[Dict[str, Any]], bool]] = None) -> List[Dict[str, Any]]:
        rows = self._db._state[table].values()
        visible: List[Dict[str, Any]] = []
        current_uid = self.guc.get("app.current_user_id", "")
        for row in rows:
            if table in ("documents", "document_series", "document_chunks", "document_assets", "document_relations"):
                if not self._can_read(row.get("space_id"), row.get("category")):
                    continue
            elif table == "health_metrics":
                if row.get("category") != "04_Health_Archive" or not self._can_read(row.get("space_id"), "04_Health_Archive"):
                    continue
            elif table == "ingestion_tasks":
                is_owner = bool(current_uid and row.get("user_id") == current_uid)
                can_space = self._can_read(row.get("space_id"), row.get("category"))
                if not (is_owner or can_space):
                    continue
            elif table == "assets":
                if not current_uid:
                    continue
            if predicate is None or predicate(row):
                visible.append(copy.deepcopy(row))
        return visible

    def insert_row(self, table: str, pk: str, row: Dict[str, Any]) -> Dict[str, Any]:
        current_uid = self.guc.get("app.current_user_id", "")
        if not current_uid:
            raise PermissionDeniedError(f"RLS INSERT rejected on {table}: missing app.current_user_id")

        if table in ("documents", "document_series", "document_chunks", "document_assets", "document_relations", "outbox_events"):
            if not self._can_write(row.get("space_id"), row.get("category")):
                raise PermissionDeniedError(
                    f"RLS INSERT WITH CHECK violation on {table} for ({row.get('space_id')}, {row.get('category')})"
                )
        elif table == "health_metrics":
            if row.get("category") != "04_Health_Archive" or not self._can_write(row.get("space_id"), "04_Health_Archive"):
                raise PermissionDeniedError("RLS INSERT WITH CHECK violation on health_metrics")
        elif table == "ingestion_tasks":
            if row.get("user_id") != current_uid:
                raise PermissionDeniedError("RLS INSERT violation on ingestion_tasks: user_id mismatch")
            if row.get("space_id") is not None and not self._can_write(row.get("space_id"), row.get("category")):
                raise PermissionDeniedError("RLS INSERT violation on ingestion_tasks: unauthorized target space/category")

        if table == "document_series":
            for existing in self._db._state["document_series"].values():
                if existing["space_id"] == row["space_id"] and existing["version_key"] == row["version_key"]:
                    raise OmniLoreError("UNIQUE_VIOLATION", "Duplicate (space_id, version_key) in document_series")
        if table == "documents" and row.get("is_current") and row.get("doc_status") == DocStatus.PUBLISHED_CURRENT.value:
            for existing in self._db._state["documents"].values():
                if (
                    existing["series_id"] == row["series_id"]
                    and existing.get("is_current")
                    and existing.get("doc_status") == DocStatus.PUBLISHED_CURRENT.value
                ):
                    raise OmniLoreError("UNIQUE_VIOLATION", "Multiple PUBLISHED_CURRENT documents in series")

        stored = copy.deepcopy(row)
        self._db._state[table][stored[pk]] = stored
        self._db._dirty_tables.add(table)
        return copy.deepcopy(stored)

    def update_row(self, table: str, pk_val: str, updater: Callable[[Dict[str, Any]], Dict[str, Any]]) -> Dict[str, Any]:
        existing = self._db._state[table].get(pk_val)
        if not existing:
            raise PermissionDeniedError(f"RLS UPDATE target not found or not visible on {table}")

        current_uid = self.guc.get("app.current_user_id", "")
        if table in ("documents", "document_series", "document_chunks", "health_metrics", "outbox_events"):
            if not self._can_write(existing.get("space_id"), existing.get("category")):
                raise PermissionDeniedError(
                    f"RLS UPDATE USING violation on {table} for existing scope ({existing.get('space_id')}, {existing.get('category')})"
                )
        elif table == "ingestion_tasks":
            is_owner = bool(current_uid and existing.get("user_id") == current_uid)
            can_space = self._can_write(existing.get("space_id"), existing.get("category"))
            if not (is_owner or can_space):
                raise PermissionDeniedError("RLS UPDATE USING violation on ingestion_tasks")

        updated = updater(copy.deepcopy(existing))

        if table in ("documents", "document_series", "document_chunks", "health_metrics", "outbox_events"):
            if not self._can_write(updated.get("space_id"), updated.get("category")):
                raise PermissionDeniedError(
                    f"RLS UPDATE WITH CHECK violation on {table} for new scope ({updated.get('space_id')}, {updated.get('category')})"
                )
        elif table == "ingestion_tasks":
            if updated.get("space_id") is not None and not self._can_write(updated.get("space_id"), updated.get("category")):
                raise PermissionDeniedError("RLS UPDATE WITH CHECK violation on ingestion_tasks")

        if table == "documents" and updated.get("is_current") and updated.get("doc_status") == DocStatus.PUBLISHED_CURRENT.value:
            for other_id, other in self._db._state["documents"].items():
                if (
                    other_id != pk_val
                    and other["series_id"] == updated["series_id"]
                    and other.get("is_current")
                    and other.get("doc_status") == DocStatus.PUBLISHED_CURRENT.value
                ):
                    raise OmniLoreError("UNIQUE_VIOLATION", "Multiple PUBLISHED_CURRENT documents in series")

        self._db._state[table][pk_val] = updated
        self._db._dirty_tables.add(table)
        return copy.deepcopy(updated)

    def delete_row(self, table: str, pk_val: str) -> None:
        existing = self._db._state[table].get(pk_val)
        if not existing:
            raise PermissionDeniedError(f"RLS DELETE target not found or not visible on {table}")
        if not self._can_write(existing.get("space_id"), existing.get("category")):
            raise PermissionDeniedError(f"RLS DELETE USING violation on {table}")
        del self._db._state[table][pk_val]
        self._db._dirty_tables.add(table)


class RLSDatabaseEngine:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._state: Dict[str, Dict[str, Dict[str, Any]]] = {
            "documents": {},
            "document_series": {},
            "document_chunks": {},
            "assets": {},
            "document_assets": {},
            "document_relations": {},
            "ingestion_tasks": {},
            "outbox_events": {},
            "health_metrics": {},
            "audit_events": {},
            "model_runs": {},
            "ledger_entries": {},
        }
        # 哪些表在本次/最近事务里被写过。只用于「要不要把运行时索引刷到磁盘」这一个判断，
        # 不参与任何权限或一致性的判定（见 omnilore/runtime_index.py 的四条红线）。
        self._dirty_tables: Set[str] = set()
        # 提交成功后的回调（由 OmniLoreEngine 注入）。缺失时引擎纯内存、不落盘。
        self.on_commit: Optional[Callable[[], None]] = None

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[RLSTransactionConnection]:
        conn = RLSTransactionConnection(self)
        yield conn


# =============================================================================
# 5. Content-Addressable File Gateway & Path Protection (Section 5.1, 7.5)
# =============================================================================

class SecureVaultStorage:
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir.resolve()
        self.inbox_dir = self.base_dir / "00_Inbox"
        self.processing_dir = self.base_dir / "01_Processing"
        self.vault_dir = self.base_dir / "02_Vault" / "spaces"
        self.review_dir = self.base_dir / "99_Review_Required"
        self.cas_dir = self.base_dir / "ops" / "cas"

        for d in (self.inbox_dir, self.processing_dir, self.vault_dir, self.review_dir, self.cas_dir):
            d.mkdir(parents=True, exist_ok=True)

    def ensure_space_dirs(self, space_id: str) -> Path:
        if not SPACE_ID_PATTERN.match(space_id):
            raise OmniLoreError("INVALID_SPACE_ID", f"Invalid space_id: {space_id}")
        space_root = self._verify_safe_subpath(self.vault_dir, space_id)
        for sub in ("raw", "wiki", "_archive"):
            (space_root / sub).mkdir(parents=True, exist_ok=True)
        return space_root

    def _reject_symlinks_in_chain(self, root: Path, target: Path) -> None:
        if os.path.islink(root):
            raise PermissionDeniedError("Symlink root directory is prohibited")
        rel_parts = target.relative_to(root).parts if target != root else ()
        curr = root
        for part in rel_parts:
            curr = curr / part
            if os.path.islink(curr):
                raise PermissionDeniedError(f"Symlink detected in path chain: {curr.name}")

    def _verify_safe_subpath(self, expected_root: Path, rel_path: str) -> Path:
        if not rel_path or "\x00" in rel_path:
            raise PermissionDeniedError("Empty or null-byte path rejected")
        if os.path.isabs(rel_path) or rel_path.startswith(("/", "\\")):
            raise PermissionDeniedError("Absolute client path is prohibited")

        root_resolved = expected_root.resolve()
        candidate_unres = expected_root / rel_path

        curr = expected_root
        for part in Path(rel_path).parts:
            if part in ("..", "."):
                raise PermissionDeniedError("Path traversal segment ('..' or '.') is prohibited")
            curr = curr / part
            if os.path.lexists(curr) and os.path.islink(curr):
                raise PermissionDeniedError("Symlink access is prohibited by file gateway")

        resolved = candidate_unres.resolve()
        if not resolved.is_relative_to(root_resolved) or resolved == root_resolved:
            raise PermissionDeniedError("Path escapes expected root directory boundary")

        self._reject_symlinks_in_chain(root_resolved, resolved)
        return resolved

    def stage_raw_bytes(self, task_id: str, content: bytes) -> Tuple[str, str]:
        sha256_hash = hashlib.sha256(content).hexdigest()
        rel_name = f"task_{task_id}_{sha256_hash[:12]}.staged"
        staged_path = self._verify_safe_subpath(self.processing_dir, rel_name)
        staged_path.write_bytes(content)
        return sha256_hash, rel_name

    def read_staged_raw_bytes(self, staged_raw_rel: str) -> bytes:
        staged_path = self._verify_safe_subpath(self.processing_dir, staged_raw_rel)
        return staged_path.read_bytes()

    def stage_wiki_draft(self, task_id: str, markdown_text: str) -> str:
        rel_name = f"task_{task_id}_wiki.md.staged"
        staged_path = self._verify_safe_subpath(self.processing_dir, rel_name)
        write_text_canonical(staged_path, markdown_text)
        return rel_name

    def promote_staged_to_cas_and_wiki(
        self,
        staged_raw_rel: str,
        staged_wiki_rel: str,
        sha256_hash: str,
        space_id: str,
        wiki_filename: str,
    ) -> Tuple[str, str]:
        staged_raw_path = self._verify_safe_subpath(self.processing_dir, staged_raw_rel)
        staged_wiki_path = self._verify_safe_subpath(self.processing_dir, staged_wiki_rel)

        raw_bytes = staged_raw_path.read_bytes()
        if hashlib.sha256(raw_bytes).hexdigest() != sha256_hash:
            raise OmniLoreError("INTEGRITY_ERROR", "Staged raw file SHA-256 mismatch before publish")

        cas_rel = f"{sha256_hash[:2]}/{sha256_hash}.bin"
        cas_target = self._verify_safe_subpath(self.cas_dir, cas_rel)
        cas_target.parent.mkdir(parents=True, exist_ok=True)
        if not cas_target.exists():
            os.replace(staged_raw_path, cas_target)
        else:
            staged_raw_path.unlink(missing_ok=True)

        self.ensure_space_dirs(space_id)
        safe_wiki_name = re.sub(r"[^a-zA-Z0-9_.\-\u4e00-\u9fa5]", "_", wiki_filename)
        if not safe_wiki_name.endswith(".md"):
            safe_wiki_name += ".md"
        wiki_rel = f"{space_id}/wiki/{safe_wiki_name}"
        wiki_target = self._verify_safe_subpath(self.vault_dir, wiki_rel)
        os.replace(staged_wiki_path, wiki_target)
        return cas_rel, wiki_rel

    def read_wiki_by_record(self, expected_space_id: str, wiki_rel_path: str) -> str:
        if not wiki_rel_path.startswith(f"{expected_space_id}/wiki/"):
            raise PermissionDeniedError("Document wiki_rel_path does not match authorized space_id")
        target = self._verify_safe_subpath(self.vault_dir, wiki_rel_path)
        if not target.exists():
            raise OmniLoreError("FILE_NOT_FOUND", "Authorized wiki file missing on disk")
        return read_text_canonical(target)

    def write_wiki_by_record(self, expected_space_id: str, wiki_rel_path: str, content: str) -> Path:
        if not wiki_rel_path.startswith(f"{expected_space_id}/wiki/"):
            raise PermissionDeniedError("Document wiki_rel_path does not match authorized space_id")
        target = self._verify_safe_subpath(self.vault_dir, wiki_rel_path)
        write_text_canonical(target, content)
        return target

    def move_wiki_across_spaces(
        self,
        old_space_id: str,
        old_wiki_rel: str,
        new_space_id: str,
        new_content: str,
    ) -> str:
        old_target = self._verify_safe_subpath(self.vault_dir, old_wiki_rel)
        self.ensure_space_dirs(new_space_id)
        new_rel = f"{new_space_id}/wiki/{old_target.name}"
        new_target = self._verify_safe_subpath(self.vault_dir, new_rel)
        write_text_canonical(new_target, new_content)
        if old_target != new_target and old_target.exists():
            old_target.unlink()
        return new_rel

    def read_cas_asset(self, storage_rel_path: str, expected_sha256: str) -> bytes:
        target = self._verify_safe_subpath(self.cas_dir, storage_rel_path)
        data = target.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected_sha256:
            raise OmniLoreError("ASSET_CORRUPTED", "Stored CAS asset failed SHA-256 verification")
        return data


# =============================================================================
# 6. Unified Model Adapter (Live DashScope + Local Deterministic Fallback)
# =============================================================================

# Exceptions that mean "we received a response but could not read it" — i.e. OUR
# parsing/shape assumption is wrong, NOT that the provider failed. Keeping them
# separate from transport failures stops a KeyError from ever being reported as
# "大模型调用失败" (QA finding P2-B).
_RESPONSE_SHAPE_ERRORS = (
    KeyError,
    IndexError,
    TypeError,
    ValueError,  # includes json.JSONDecodeError
    AttributeError,
)


def _log_response_shape_error(exc_type: str, exc: BaseException) -> None:
    """Emit a diagnostic line for a model-response parsing failure.

    Args:
        exc_type: Name of the exception class.
        exc: The exception instance.
    """
    sys.stderr.write(
        f"[omnilore] 模型响应解析失败（非供应商故障），已回退本地确定性逻辑："
        f"{exc_type}: {exc}\n"
    )
    sys.stderr.flush()


class ModelAdapter:
    def __init__(
        self,
        config: ModelAdapterConfig,
        telemetry_sink: List[Dict[str, Any]],
        custom_llm_handler: Optional[Callable[[str, Dict[str, Any], str], Dict[str, Any]]] = None,
    ):
        self.config = config
        self._telemetry_sink = telemetry_sink
        self._custom_llm_handler = custom_llm_handler
        self._http = DashScopeHTTPTransport(config)
        # R-04b：向量来源可替换（dashscope / openai 兼容含 Ollama / none）。
        # 远程 provider 与本地确定性兜底**同时存在**：前者不可用时立刻走后者，
        # 这条降级链是 W0/A-6 的契约，抽象化不允许把它弄丢。
        self.embedding_provider: EmbeddingProvider = build_embedding_provider(config, self._http)
        self._local_embedding_provider = LocalDeterministicEmbeddingProvider(config.embedding_dimension)

    def generate_structured_resilient(
        self,
        *,
        task_type: str,
        space: SpaceRecord,
        category: str,
        payload: Dict[str, Any],
        required_schema: Dict[str, type],
        escalate: bool = False,
        explicit_external_approval: bool = False,
        task_id: Optional[str] = None,
        doc_id: Optional[str] = None,
    ) -> Tuple[Dict[str, Any], str]:
        """Generate structured output, never failing because of the remote provider.

        Wraps :meth:`generate_structured` so that a misconfigured / expired / rate-limited
        / unreachable provider degrades to the deterministic local model instead of
        aborting the request. This is the LLM-side counterpart of the zero-key
        retrieval degradation (Open-Source Launch W0 / A-6, QA finding P1-2).

        Args:
            task_type: Which deterministic fallback routine should produce output.
            space / category: Outbound privacy policy coordinates.
            payload: Structured input for the model.
            required_schema: Expected `{field: type}` shape enforced post-hoc.
            escalate: Use the escalated (more expensive) chat model.
            explicit_external_approval: Whether data export was explicitly approved.
            task_id / doc_id: Telemetry correlation ids.

        Returns:
            A tuple `(result, note)`. `note` is `""` when the provider answered,
            otherwise it explains why the local deterministic fallback was used.
        """
        fallback = None
        try:
            result = self.generate_structured(
                task_type=task_type,
                space=space,
                category=category,
                payload=payload,
                required_schema=required_schema,
                escalate=escalate,
                explicit_external_approval=explicit_external_approval,
                task_id=task_id,
                doc_id=doc_id,
            )
            return result, ""
        except _RESPONSE_SHAPE_ERRORS as exc:
            # 供应商返回了响应、但我们在解析/取值时炸了 —— 这**不是**供应商故障。
            # 若笼统报成「大模型调用失败」，会把排查方向彻底带偏（QA P2-B），
            # 因此文案分开，并把细节写到 stderr 便于定位。
            _log_response_shape_error(type(exc).__name__, exc)
            note = (
                f"模型响应解析失败（{type(exc).__name__}），"
                "本次答案已由本地确定性逻辑生成（详见 stderr 日志）"
            )
            fallback = self._default_deterministic_model(task_type, payload, category)
            return fallback, note
        except Exception as exc:  # transport failure, quota, auth, schema/field guard
            note = (
                f"大模型调用失败（{type(exc).__name__}），本次答案已由本地确定性逻辑生成"
            )
            fallback = self._default_deterministic_model(task_type, payload, category)
            return fallback, note

    @property
    def retrieval_mode(self) -> str:
        """Return `hybrid` when a live embedding provider is configured, else `keyword_only`.

        Zero-API-key installs silently use the deterministic local embedder, so we
        must report the effective mode to every caller (Open-Source Launch W0 / A-6).

        R-04b：判断依据从「有没有 DASHSCOPE_API_KEY」升级为「**当前 provider 是否可用**」——
        provider 可以是 DashScope、任意 OpenAI 兼容端点（含本地 Ollama），或显式 `none`。
        """
        return "hybrid" if self.embedding_provider.is_available() else "keyword_only"

    @property
    def degraded_reason(self) -> str:
        """Human-readable explanation when running degraded; empty string when healthy.

        文案由 provider 自己给出：默认 DashScope 无 key 的那句话与抽象之前**逐字一致**
        （README 与测试都引用它），只有换了 provider 才会出现新的说明。
        """
        if self.embedding_provider.is_available():
            return ""
        return self.embedding_provider.unavailable_reason()

    def _record_telemetry(
        self,
        *,
        task_id: Optional[str],
        doc_id: Optional[str],
        space_id: Optional[str],
        model_id: str,
        task_type: str,
        input_tokens: int,
        output_tokens: int,
        latency_ms: int,
        status: str,
        error_code: Optional[str] = None,
    ) -> None:
        self._telemetry_sink.append({
            "run_id": str(uuid.uuid4()),
            "task_id": task_id,
            "doc_id": doc_id,
            "space_id": space_id,
            "provider": self.config.provider,
            "model_id": model_id,
            "task_type": task_type,
            "prompt_version": self.config.prompt_version,
            "schema_version": self.config.output_schema_version,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "latency_ms": latency_ms,
            "estimated_cost_usd": estimate_cost_usd(model_id, input_tokens, output_tokens),
            "status": status,
            "error_code": error_code,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })

    def check_outbound_policy(
        self,
        space: SpaceRecord,
        category: str,
        explicit_external_approval: bool = False,
        task_id: Optional[str] = None,
    ) -> None:
        policy = space.external_llm_policy.get(category, "DENY_UNLESS_APPROVED")
        if policy == "DENY_UNLESS_APPROVED" and not explicit_external_approval:
            self._record_telemetry(
                task_id=task_id,
                doc_id=None,
                space_id=space.space_id,
                model_id=self.config.primary_chat_model,
                task_type="OUTBOUND_POLICY_CHECK",
                input_tokens=0,
                output_tokens=0,
                latency_ms=1,
                status="POLICY_BLOCKED",
                error_code="SENSITIVE_CATEGORY_EXTERNAL_BLOCKED",
            )
            raise OmniLoreError(
                "SENSITIVE_CATEGORY_EXTERNAL_BLOCKED",
                f"Space '{space.space_id}' policy blocks unapproved external model calls for category '{category}'",
            )

    def generate_structured(
        self,
        *,
        task_type: str,
        space: SpaceRecord,
        category: str,
        payload: Dict[str, Any],
        required_schema: Dict[str, type],
        escalate: bool = False,
        explicit_external_approval: bool = False,
        task_id: Optional[str] = None,
        doc_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        self.check_outbound_policy(
            space,
            category,
            explicit_external_approval=explicit_external_approval,
            task_id=task_id,
        )
        model_id = self.config.escalated_chat_model if escalate else self.config.primary_chat_model
        t0 = time.perf_counter()

        api_key = self.config.get_api_key()
        raw_output: Dict[str, Any]
        if self._custom_llm_handler is not None:
            raw_output = self._custom_llm_handler(task_type, payload, model_id)
        elif api_key:
            url = f"{self.config.api_endpoint.rstrip('/')}/chat/completions"
            resp = self._http.post_json(
                url,
                {
                    "model": model_id,
                    "response_format": {"type": "json_object"},
                    "max_tokens": self.config.max_output_tokens,
                    "messages": [
                        {
                            "role": "system",
                            "content": f"You are OmniLore v2.3 ({self.config.prompt_version}). Output strict JSON matching keys: {list(required_schema.keys())}.",
                        },
                        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                    ],
                },
                api_key,
            )
            content_str = resp["choices"][0]["message"]["content"]
            raw_output = json.loads(content_str)
        else:
            raw_output = self._default_deterministic_model(task_type, payload, category)

        latency_ms = max(1, int((time.perf_counter() - t0) * 1000))
        in_tok = max(16, len(json.dumps(payload, ensure_ascii=False)) // 4)
        out_tok = max(16, len(json.dumps(raw_output, ensure_ascii=False)) // 4)

        if not isinstance(raw_output, dict):
            self._record_telemetry(
                task_id=task_id,
                doc_id=doc_id,
                space_id=space.space_id,
                model_id=model_id,
                task_type=task_type,
                input_tokens=in_tok,
                output_tokens=out_tok,
                latency_ms=latency_ms,
                status="SCHEMA_INVALID",
                error_code="OUTPUT_NOT_DICT",
            )
            raise OmniLoreError("MODEL_SCHEMA_INVALID", "Model output must be a JSON object")

        forbidden_model_fields = {
            "space_id",
            "doc_id",
            "token",
            "allowed_read_spaces",
            "allowed_write_spaces",
            "task_state",
        }
        injected_forbidden = set(raw_output.keys()) & forbidden_model_fields
        if injected_forbidden:
            self._record_telemetry(
                task_id=task_id,
                doc_id=doc_id,
                space_id=space.space_id,
                model_id=model_id,
                task_type=task_type,
                input_tokens=in_tok,
                output_tokens=out_tok,
                latency_ms=latency_ms,
                status="SCHEMA_INVALID",
                error_code="FORBIDDEN_PRIVILEGE_FIELD_IN_MODEL_OUTPUT",
            )
            raise OmniLoreError(
                "MODEL_SCHEMA_INVALID",
                f"Model attempted to emit restricted privilege/state fields: {sorted(injected_forbidden)}",
            )

        extra_fields = set(raw_output.keys()) - set(required_schema.keys())
        if extra_fields:
            self._record_telemetry(
                task_id=task_id,
                doc_id=doc_id,
                space_id=space.space_id,
                model_id=model_id,
                task_type=task_type,
                input_tokens=in_tok,
                output_tokens=out_tok,
                latency_ms=latency_ms,
                status="SCHEMA_INVALID",
                error_code="UNEXPECTED_SCHEMA_FIELDS",
            )
            raise OmniLoreError(
                "MODEL_SCHEMA_INVALID",
                f"Model output contains unexpected fields: {sorted(extra_fields)}",
            )

        for field_name, expected_type in required_schema.items():
            if field_name not in raw_output or not isinstance(raw_output[field_name], expected_type):
                self._record_telemetry(
                    task_id=task_id,
                    doc_id=doc_id,
                    space_id=space.space_id,
                    model_id=model_id,
                    task_type=task_type,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    latency_ms=latency_ms,
                    status="SCHEMA_INVALID",
                    error_code=f"INVALID_FIELD_{field_name.upper()}",
                )
                raise OmniLoreError(
                    "MODEL_SCHEMA_INVALID",
                    f"Field '{field_name}' missing or failed type check ({expected_type.__name__})",
                )

        self._record_telemetry(
            task_id=task_id,
            doc_id=doc_id,
            space_id=space.space_id,
            model_id=model_id,
            task_type=task_type,
            input_tokens=in_tok,
            output_tokens=out_tok,
            latency_ms=latency_ms,
            status="SUCCESS",
        )
        return raw_output

    def _default_deterministic_model(self, task_type: str, payload: Dict[str, Any], default_cat: str) -> Dict[str, Any]:
        text = str(payload.get("raw_text", ""))
        if task_type == "ROUTE_AND_COMPILE":
            has_mixed_signals = ("私人病历" in text or "体检" in text) and ("接口规范" in text or "API" in text)
            inferred_cat = default_cat
            if "体检" in text or "血糖" in text:
                inferred_cat = "04_Health_Archive"
            elif "保单" in text or "家庭" in text:
                inferred_cat = "03_Family"
            elif "API" in text or "接口" in text or "架构" in text:
                inferred_cat = "01_Work"

            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            title = payload.get("title") or (lines[0].lstrip("# ").strip() if lines else "Untitled Knowledge Note")
            quote_snippet = lines[-1] if lines else text[:80]
            compiled_md = (
                f"# {title}\n\n"
                f"## 核心摘要\n{text[:240]}\n\n"
                f"## 原始证据引用\n"
                f"> {quote_snippet}\n"
            )
            return {
                "suggested_category": inferred_cat,
                "title": title,
                "summary": text[:120],
                "compiled_markdown": compiled_md,
                "citations": [quote_snippet],
                "has_conflict_or_ambiguity": bool(has_mixed_signals),
                "review_reason": "检测到个人健康与工作规范混合信号，需人工拆分或确认" if has_mixed_signals else "",
                "tags": ["omnilore", inferred_cat.lower()],
            }
        if task_type == "SYNTHESIZE_ANSWER":
            chunks = payload.get("chunks", [])
            if not chunks:
                return {
                    "answer": "当前授权范围内未找到可支持该问题的有效证据，无法给出断言。",
                    "citations": [],
                    "has_sufficient_evidence": False,
                }
            top = chunks[0]
            return {
                "answer": f"根据《{top['title']}》（版本 {top['version_label']}）：{top['chunk_text'][:120]}",
                "citations": [
                    {
                        "doc_id": top["doc_id"],
                        "version_label": top["version_label"],
                        "page_number": top.get("page_number", 1),
                        "heading_path": top.get("heading_path", "Root"),
                        "quote": top["chunk_text"][:100],
                    }
                ],
                "has_sufficient_evidence": True,
            }
        return {}

    def embed_texts(self, texts: List[str], *, task_id: Optional[str] = None,
                    space_id: Optional[str] = None) -> List[List[float]]:
        """把文本转成向量：优先用已配置的 provider，不可用则走本地确定性兜底（R-04b）。

        三条约定（抽象化之后仍必须成立）：
          1. provider 可用 → 调远程；返回维度与 `OMNILORE_EMBEDDING_DIM` 不一致时
             **抛** :class:`EmbeddingConfigurationError`，不截断不补零（错误相似度更危险）；
          2. provider 不可用（无 key / 本地端点未配 / `none`）→ 本地确定性向量，
             调用方通过 `retrieval_mode` 与 `degraded_reason` 得知降级；
          3. 网络/鉴权/超时异常**照常抛出**，由 `search_knowledge` 那条降级链处理
             （保持 W0/A-6 的「不要因为 provider 挂了就整体失败」）。
        """
        # `_custom_llm_handler` 存在时（测试 / 嵌入方注入）沿用旧语义：不打扰外部服务。
        if self.embedding_provider.is_available() and self._custom_llm_handler is None:
            vectors = self.embedding_provider.embed(texts)
            validate_embedding_dimensions(
                vectors,
                self.config.embedding_dimension,
                provider=self.embedding_provider.name,
                model_id=self.embedding_provider.model_id,
            )
            self._record_telemetry(
                task_id=task_id,
                doc_id=None,
                space_id=space_id,
                model_id=self.embedding_provider.model_id,
                task_type="EMBEDDING",
                input_tokens=sum(len(one) // 2 + 1 for one in texts),
                output_tokens=0,
                latency_ms=10,
                status="SUCCESS",
            )
            return vectors

        vectors = self._local_embedding_provider.embed(texts)
        self._record_telemetry(
            task_id=task_id,
            doc_id=None,
            space_id=space_id,
            model_id=self.config.embedding_model,
            task_type="EMBEDDING",
            input_tokens=sum(len(one) // 2 + 1 for one in texts),
            output_tokens=0,
            latency_ms=2,
            status="SUCCESS",
        )
        return vectors


# =============================================================================
# 7. Complete OmniLore Engine (Phase 0 + Phase 1 + Phase 2 + Phase 3)
# =============================================================================

class OmniLoreEngine:
    def __init__(
        self,
        storage_root: Path,
        auth_service: Optional[AuthorizationService] = None,
        db_engine: Optional[RLSDatabaseEngine] = None,
        model_config: Optional[ModelAdapterConfig] = None,
        custom_llm_handler: Optional[Callable[[str, Dict[str, Any], str], Dict[str, Any]]] = None,
    ):
        self.storage = SecureVaultStorage(storage_root)
        self.auth = auth_service or AuthorizationService()
        self.db = db_engine or RLSDatabaseEngine()
        self.model_config = model_config or ModelAdapterConfig()
        self.model_adapter = ModelAdapter(
            self.model_config,
            telemetry_sink=[],
            custom_llm_handler=custom_llm_handler,
        )
        self.ops = OperationsManager(self)
        self._last_synced_hash_by_doc: Dict[str, str] = {}
        self.last_backup_manifest: Optional[Dict[str, Any]] = None
        self.last_restore_drill: Optional[Dict[str, Any]] = None
        # 提交成功后把「文档 ↔ 资产」最小映射刷到 `<data_dir>/ops/index/`，让
        # `omnilore audit-bundle` 能在**另一个进程**里重建导出范围（R-05m）。
        # 索引是派生物：写失败不影响入库，但会写到 stderr（见 RLSTransactionConnection）。
        self.db.on_commit = self.persist_runtime_index

    def persist_runtime_index(self) -> None:
        """把当前状态投影成运行时索引并落盘（`ops/index/assets.json` + `document_assets.json`）。

        这个索引**只解决定位问题**（asset_id → CAS 相对路径、doc → 元数据），
        不承担真相：`audit-bundle` 校验时仍然读真实字节重算 SHA-256，
        再与这里的 `sha256_hash` 比对。详见 `omnilore/runtime_index.py` 的四条红线。
        """
        persist_runtime_index(
            Path(self.storage.base_dir),
            self.db._state,
            updated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

    def _record_audit(
        self,
        actor_user_id: str,
        space_id: Optional[str],
        category: Optional[str],
        action_type: str,
        target_object_type: str,
        target_object_id: str,
        before_summary: Dict[str, Any],
        after_summary: Dict[str, Any],
    ) -> None:
        audit_id = str(uuid.uuid4())
        self.db._state["audit_events"][audit_id] = {
            "audit_id": audit_id,
            "actor_user_id": actor_user_id,
            "space_id": space_id,
            "category": category,
            "action_type": action_type,
            "target_object_type": target_object_type,
            "target_object_id": target_object_id,
            "before_summary": before_summary,
            "after_summary": after_summary,
            "request_id": str(uuid.uuid4()),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    def _transition_task(self, conn: RLSTransactionConnection, task_id: str, new_state: TaskState, **updates: Any) -> Dict[str, Any]:
        def _upd(row: Dict[str, Any]) -> Dict[str, Any]:
            current = TaskState(row["task_state"])
            validate_state_transition(current, new_state)
            row["task_state"] = new_state.value
            row["updated_at"] = datetime.now(timezone.utc).isoformat()
            for k, v in updates.items():
                row[k] = v
            return row

        return conn.update_row("ingestion_tasks", task_id, _upd)

    async def ingest_document(
        self,
        raw_token: str,
        *,
        raw_bytes: bytes,
        original_filename: str,
        target_space_id: Optional[str] = None,
        target_category: Optional[str] = None,
        version_key: Optional[str] = None,
        version_label: str = "v1.0",
        operation_type: OperationType = OperationType.INGEST_FILE,
        expected_current_doc_id: Optional[str] = None,
        parent_doc_id: Optional[str] = None,
        code_symbols: Optional[List[str]] = None,
        source_page: int = 1,
        source_heading: str = "Section 1",
        ttl_days: Optional[int] = None,
        explicit_external_approval: bool = False,
        extract_health_metrics: bool = False,
        subject_person: str = "本人",
        reprocess_generation: int = 0,
        simulate_crash_after_stage: bool = False,
    ) -> Dict[str, Any]:
        scope = self.auth.resolve_scope(raw_token)
        canonical_hash = hashlib.sha256(raw_bytes).hexdigest()

        is_auto_route = target_space_id is None or target_space_id == "AUTO"
        resolved_category = target_category or "01_Work"
        if not is_auto_route:
            resolved_space_id = target_space_id
        else:
            candidate_spaces = sorted(
                scope.write_spaces,
                key=lambda s: (0 if self.auth._spaces[s].space_type == SpaceType.PERSONAL else 1, s),
            )
            resolved_space_id = next(
                (s for s in candidate_spaces if scope.can_write(s, resolved_category)),
                next(iter(candidate_spaces), None),
            )

        if not is_auto_route:
            if not scope.can_write(resolved_space_id, resolved_category):  # type: ignore[arg-type]
                raise PermissionDeniedError(
                    f"Explicit target ({resolved_space_id}, {resolved_category}) is outside caller write scope"
                )
        else:
            if not resolved_space_id:
                raise PermissionDeniedError("Caller has no authorized write spaces for AUTO routing")

        route_ctx = f"{resolved_space_id}:{resolved_category}:{version_key or 'auto'}"
        idem_key = compute_idempotency_key(
            scope.user_id,
            canonical_hash,
            route_ctx,
            operation_type,
            reprocess_generation=reprocess_generation,
        )

        async with profiled_transaction(self.db, scope) as conn:
            existing_tasks = conn.select_rows(
                "ingestion_tasks",
                lambda r: r["idempotency_key"] == idem_key,
            )
            if existing_tasks:
                return existing_tasks[0]

            if any(r["idempotency_key"] == idem_key for r in self.db._state["ingestion_tasks"].values()):
                raise PermissionDeniedError("Idempotency key belongs to an unauthorized principal")

            task_id = str(uuid.uuid4())
            _, staged_raw_rel = self.storage.stage_raw_bytes(task_id, raw_bytes)
            conn.insert_row(
                "ingestion_tasks",
                "task_id",
                {
                    "task_id": task_id,
                    "idempotency_key": idem_key,
                    "user_id": scope.user_id,
                    "space_id": resolved_space_id,
                    "category": resolved_category,
                    "operation_type": operation_type.value,
                    "task_state": TaskState.RECEIVED.value,
                    "canonical_file_hash": canonical_hash,
                    "staging_rel_path": staged_raw_rel,
                    "result_doc_id": None,
                    "candidate_route": {},
                    "review_reason": None,
                    "error_code": None,
                    "error_detail": None,
                    "retry_count": 0,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            self._transition_task(conn, task_id, TaskState.PRECHECKING)

        # Parse multi-format document (PDF, Image, Markdown, Code)
        try:
            parsed_doc = parse_document_bytes(raw_bytes, original_filename)
            raw_text = parsed_doc.raw_text
        except ValueError as exc:
            err_msg = str(exc)
            err_code = "EMPTY_DOCUMENT" if "EMPTY_DOCUMENT" in err_msg else "UNREADABLE_BINARY"
            async with profiled_transaction(self.db, scope) as conn:
                return self._transition_task(
                    conn,
                    task_id,
                    TaskState.FAILED_FINAL,
                    error_code=err_code,
                    error_detail=err_msg,
                )

        space_rec = self.auth._spaces[resolved_space_id]  # type: ignore[index]

        async with profiled_transaction(self.db, scope) as conn:
            self._transition_task(conn, task_id, TaskState.ROUTING)

        try:
            model_res = self.model_adapter.generate_structured(
                task_type="ROUTE_AND_COMPILE",
                space=space_rec,
                category=resolved_category,
                payload={"raw_text": raw_text, "filename": original_filename},
                required_schema={
                    "suggested_category": str,
                    "title": str,
                    "summary": str,
                    "compiled_markdown": str,
                    "citations": list,
                    "has_conflict_or_ambiguity": bool,
                    "review_reason": str,
                    "tags": list,
                },
                explicit_external_approval=explicit_external_approval,
                task_id=task_id,
            )
        except OmniLoreError as exc:
            async with profiled_transaction(self.db, scope) as conn:
                if exc.code == "SENSITIVE_CATEGORY_EXTERNAL_BLOCKED":
                    return self._transition_task(
                        conn,
                        task_id,
                        TaskState.NEEDS_REVIEW,
                        review_reason=exc.message,
                        candidate_route={
                            "space_id": resolved_space_id,
                            "category": resolved_category,
                            "original_filename": original_filename,
                        },
                    )
                return self._transition_task(
                    conn,
                    task_id,
                    TaskState.FAILED_RETRYABLE,
                    error_code=exc.code,
                    error_detail=exc.message,
                    retry_count=1,
                )

        suggested_cat = model_res["suggested_category"]
        if (
            model_res["has_conflict_or_ambiguity"]
            or (is_auto_route and target_category is None and suggested_cat != resolved_category)
            or not scope.can_write(resolved_space_id, suggested_cat if is_auto_route else resolved_category)
        ):
            async with profiled_transaction(self.db, scope) as conn:
                return self._transition_task(
                    conn,
                    task_id,
                    TaskState.NEEDS_REVIEW,
                    review_reason=model_res["review_reason"]
                    or f"路由候选类别 {suggested_cat} 与默认类别不一致或涉及跨边界内容，需人工确认",
                    candidate_route={
                        "suggested_space_id": resolved_space_id,
                        "suggested_category": suggested_cat,
                        "title": model_res["title"],
                        "summary": model_res["summary"],
                        "original_filename": original_filename,
                    },
                )

        for cit in model_res["citations"]:
            if str(cit).strip() and str(cit).strip() not in raw_text:
                async with profiled_transaction(self.db, scope) as conn:
                    return self._transition_task(
                        conn,
                        task_id,
                        TaskState.NEEDS_REVIEW,
                        review_reason="AI 生成的引用片段未在原文中精确命中，需人工复核",
                    )

        merged_symbols = list(dict.fromkeys((code_symbols or []) + parsed_doc.code_symbols))
        return await self._stage_and_commit_task(
            raw_token=raw_token,
            scope=scope,
            task_id=task_id,
            resolved_space_id=resolved_space_id,  # type: ignore[arg-type]
            resolved_category=resolved_category,
            original_filename=original_filename,
            canonical_hash=canonical_hash,
            raw_bytes_len=len(raw_bytes),
            raw_text=raw_text,
            staged_raw_rel=f"task_{task_id}_{canonical_hash[:12]}.staged",
            model_res=model_res,
            version_key=version_key,
            version_label=version_label,
            operation_type=operation_type,
            expected_current_doc_id=expected_current_doc_id,
            parent_doc_id=parent_doc_id,
            code_symbols=merged_symbols,
            outbound_links=parsed_doc.outbound_wiki_links,
            source_page=source_page,
            source_heading=source_heading,
            ttl_days=ttl_days,
            extract_health_metrics=extract_health_metrics or (resolved_category == "04_Health_Archive"),
            subject_person=subject_person,
            simulate_crash_after_stage=simulate_crash_after_stage,
        )

    async def _stage_and_commit_task(
        self,
        *,
        raw_token: str,
        scope: ResolvedScope,
        task_id: str,
        resolved_space_id: str,
        resolved_category: str,
        original_filename: str,
        canonical_hash: str,
        raw_bytes_len: int,
        raw_text: str,
        staged_raw_rel: str,
        model_res: Dict[str, Any],
        version_key: Optional[str],
        version_label: str,
        operation_type: OperationType,
        expected_current_doc_id: Optional[str],
        parent_doc_id: Optional[str],
        code_symbols: List[str],
        outbound_links: List[str],
        source_page: int,
        source_heading: str,
        ttl_days: Optional[int],
        extract_health_metrics: bool,
        subject_person: str,
        simulate_crash_after_stage: bool,
    ) -> Dict[str, Any]:
        doc_id = str(uuid.uuid4())
        asset_id = str(uuid.uuid4())
        effective_version_key = version_key or f"vk_{doc_id.replace('-', '')[:16]}"

        eff_ttl_days = ttl_days if ttl_days is not None else self.auth._spaces[resolved_space_id].default_ttl_days
        ttl_expires_at = (
            (datetime.now(timezone.utc) + timedelta(days=eff_ttl_days)).isoformat()
            if eff_ttl_days is not None
            else None
        )

        async with profiled_transaction(self.db, scope) as conn:
            existing_series = conn.select_rows(
                "document_series",
                lambda s: s["space_id"] == resolved_space_id and s["version_key"] == effective_version_key,
            )
            if operation_type == OperationType.REPLACE_VERSION:
                if not existing_series:
                    raise OmniLoreError("SERIES_NOT_FOUND", f"Series '{effective_version_key}' not found for replacement")
                series_row = existing_series[0]
                if expected_current_doc_id and series_row["current_doc_id"] != expected_current_doc_id:
                    raise VersionConflictError(
                        f"Expected current_doc_id={expected_current_doc_id}, actual={series_row['current_doc_id']}"
                    )
                series_id = series_row["series_id"]
            elif existing_series:
                series_row = existing_series[0]
                series_id = series_row["series_id"]
            else:
                series_id = str(uuid.uuid4())
                conn.insert_row(
                    "document_series",
                    "series_id",
                    {
                        "series_id": series_id,
                        "space_id": resolved_space_id,
                        "category": resolved_category,
                        "version_key": effective_version_key,
                        "current_doc_id": None,
                        "revision_number": 1,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    },
                )

            frontmatter = {
                "doc_id": doc_id,
                "space_id": resolved_space_id,
                "series_id": series_id,
                "version_key": effective_version_key,
                "version_label": version_label,
                "source_asset_ids": [asset_id],
                "source_task_id": task_id,
                "content_revision": 1,
                "review_status": "APPROVED",
                "title": model_res["title"],
                "summary": model_res["summary"],
                "tags": model_res["tags"],
            }
            full_wiki_md = render_wiki_markdown(frontmatter, model_res["compiled_markdown"])
            wiki_hash = hashlib.sha256(full_wiki_md.encode("utf-8")).hexdigest()
            staged_wiki_rel = self.storage.stage_wiki_draft(task_id, full_wiki_md)
            wiki_filename = f"{effective_version_key}_{version_label}_{doc_id[:8]}.md"

            conn.insert_row(
                "documents",
                "doc_id",
                {
                    "doc_id": doc_id,
                    "space_id": resolved_space_id,
                    "category": resolved_category,
                    "series_id": series_id,
                    "version_label": version_label,
                    "title": model_res["title"],
                    "summary": model_res["summary"],
                    "wiki_rel_path": f"{resolved_space_id}/wiki/{wiki_filename}",
                    "file_hash": wiki_hash,
                    "content_revision": 1,
                    "doc_status": DocStatus.PENDING_PUBLISH.value,
                    "review_status": "APPROVED",
                    "is_current": False,
                    "tags": model_res["tags"],
                    "outbound_links": outbound_links,
                    "ttl_expires_at": ttl_expires_at,
                    "source_task_id": task_id,
                    "created_by": scope.user_id,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
            )

            outbox_id = str(uuid.uuid4())
            conn.insert_row(
                "outbox_events",
                "event_id",
                {
                    "event_id": outbox_id,
                    "task_id": task_id,
                    "space_id": resolved_space_id,
                    "category": resolved_category,
                    "event_type": "PUBLISH_STAGED_FILES",
                    "payload": {
                        "task_id": task_id,
                        "doc_id": doc_id,
                        "series_id": series_id,
                        "asset_id": asset_id,
                        "space_id": resolved_space_id,
                        "category": resolved_category,
                        "original_filename": original_filename,
                        "sha256_hash": canonical_hash,
                        "byte_size": raw_bytes_len,
                        "staged_raw_rel": staged_raw_rel,
                        "staged_wiki_rel": staged_wiki_rel,
                        "wiki_filename": wiki_filename,
                        "compiled_body": model_res["compiled_markdown"],
                        "raw_text": raw_text,
                        "operation_type": operation_type.value,
                        "expected_current_doc_id": expected_current_doc_id,
                        "parent_doc_id": parent_doc_id,
                        "code_symbols": code_symbols,
                        "source_page": source_page,
                        "source_heading": source_heading,
                        "extract_health_metrics": extract_health_metrics,
                        "subject_person": subject_person,
                        "ledger_event_id": f"ledger_{task_id}",
                    },
                    "delivery_status": "PENDING",
                    "retry_count": 0,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "processed_at": None,
                },
            )
            self._transition_task(
                conn,
                task_id,
                TaskState.STAGED,
                result_doc_id=doc_id,
                space_id=resolved_space_id,
                category=resolved_category,
            )

        if simulate_crash_after_stage:
            raise RuntimeError("SIMULATED_PROCESS_CRASH_AFTER_STAGE")

        await self.process_outbox_event(raw_token, outbox_id)

        async with profiled_transaction(self.db, scope) as conn:
            final_task = conn.select_rows("ingestion_tasks", lambda r: r["task_id"] == task_id)[0]
            return final_task

    async def process_outbox_event(self, raw_token: str, event_id: str) -> None:
        scope = self.auth.resolve_scope(raw_token)
        async with profiled_transaction(self.db, scope) as conn:
            events = conn.select_rows("outbox_events", lambda e: e["event_id"] == event_id)
            if not events:
                return
            ev = events[0]
            if ev["delivery_status"] == "COMPLETED":
                return
            payload = ev["payload"]
            task_id = payload["task_id"]
            task_rows = conn.select_rows("ingestion_tasks", lambda t: t["task_id"] == task_id)
            if task_rows and task_rows[0]["task_state"] == TaskState.STAGED.value:
                self._transition_task(conn, task_id, TaskState.COMMITTING)

        cas_rel = f"{payload['sha256_hash'][:2]}/{payload['sha256_hash']}.bin"
        wiki_rel = f"{payload['space_id']}/wiki/{payload['wiki_filename']}"
        staged_raw_path = self.storage.processing_dir / payload["staged_raw_rel"]
        staged_wiki_path = self.storage.processing_dir / payload["staged_wiki_rel"]

        if staged_raw_path.exists() and staged_wiki_path.exists():
            cas_rel, wiki_rel = self.storage.promote_staged_to_cas_and_wiki(
                payload["staged_raw_rel"],
                payload["staged_wiki_rel"],
                payload["sha256_hash"],
                payload["space_id"],
                payload["wiki_filename"],
            )

        wiki_full_text = self.storage.read_wiki_by_record(payload["space_id"], wiki_rel)
        self._last_synced_hash_by_doc[payload["doc_id"]] = hashlib.sha256(
            wiki_full_text.encode("utf-8")
        ).hexdigest()

        embeddings = self.model_adapter.embed_texts(
            [payload["compiled_body"]],
            task_id=payload["task_id"],
            space_id=payload["space_id"],
        )

        async with profiled_transaction(self.db, scope) as conn:
            doc_id = payload["doc_id"]
            series_id = payload["series_id"]
            space_id = payload["space_id"]
            category = payload["category"]
            op_type = OperationType(payload["operation_type"])

            existing_assets = [
                a for a in self.db._state["assets"].values()
                if a["sha256_hash"] == payload["sha256_hash"]
            ]
            if existing_assets:
                asset_id = existing_assets[0]["asset_id"]
            else:
                asset_id = payload["asset_id"]
                conn.insert_row(
                    "assets",
                    "asset_id",
                    {
                        "asset_id": asset_id,
                        "sha256_hash": payload["sha256_hash"],
                        "storage_rel_path": cas_rel,
                        "mime_type": "text/markdown",
                        "byte_size": payload["byte_size"],
                        "created_by": scope.user_id,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    },
                )

            conn.insert_row(
                "document_assets",
                "doc_id",
                {
                    "doc_id": doc_id,
                    "asset_id": asset_id,
                    "space_id": space_id,
                    "category": category,
                    "original_filename": payload["original_filename"],
                    "source_locator": {
                        "page": payload["source_page"],
                        "heading": payload["source_heading"],
                    },
                },
            )

            if op_type == OperationType.SUPPLEMENT_DOC and payload.get("parent_doc_id"):
                parent_id = payload["parent_doc_id"]
                conn.update_row(
                    "documents",
                    doc_id,
                    lambda d: {**d, "doc_status": DocStatus.SUPPLEMENT_ACTIVE.value, "is_current": True},
                )
                conn.insert_row(
                    "document_relations",
                    "relation_id",
                    {
                        "relation_id": str(uuid.uuid4()),
                        "space_id": space_id,
                        "category": category,
                        "source_doc_id": doc_id,
                        "target_doc_id": parent_id,
                        "relation_type": "SUPPLEMENT",
                        "created_by": scope.user_id,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
            else:
                series_row = conn.select_rows("document_series", lambda s: s["series_id"] == series_id)[0]
                old_current_id = series_row.get("current_doc_id")
                expected_old = payload.get("expected_current_doc_id")
                if expected_old and old_current_id != expected_old:
                    raise VersionConflictError(
                        f"Concurrent version update detected on series {series_id}: expected {expected_old}, found {old_current_id}"
                    )

                if old_current_id and old_current_id != doc_id:
                    conn.update_row(
                        "documents",
                        old_current_id,
                        lambda d: {**d, "doc_status": DocStatus.SUPERSEDED.value, "is_current": False},
                    )
                    for ch in conn.select_rows("document_chunks", lambda c: c["doc_id"] == old_current_id):
                        conn.update_row(
                            "document_chunks",
                            ch["chunk_id"],
                            lambda c: {**c, "is_current": False},
                        )
                    conn.insert_row(
                        "document_relations",
                        "relation_id",
                        {
                            "relation_id": str(uuid.uuid4()),
                            "space_id": space_id,
                            "category": category,
                            "source_doc_id": doc_id,
                            "target_doc_id": old_current_id,
                            "relation_type": "SUPERSEDES",
                            "created_by": scope.user_id,
                            "created_at": datetime.now(timezone.utc).isoformat(),
                        },
                    )

                conn.update_row(
                    "documents",
                    doc_id,
                    lambda d: {**d, "doc_status": DocStatus.PUBLISHED_CURRENT.value, "is_current": True},
                )
                conn.update_row(
                    "document_series",
                    series_id,
                    lambda s: {
                        **s,
                        "current_doc_id": doc_id,
                        "revision_number": s["revision_number"] + (1 if old_current_id else 0),
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    },
                )

            chunk_id = str(uuid.uuid4())
            conn.insert_row(
                "document_chunks",
                "chunk_id",
                {
                    "chunk_id": chunk_id,
                    "doc_id": doc_id,
                    "series_id": series_id,
                    "space_id": space_id,
                    "category": category,
                    "is_current": True,
                    "chunk_index": 0,
                    "heading_path": payload["source_heading"],
                    "page_number": payload["source_page"],
                    "chunk_text": payload["compiled_body"],
                    "code_symbols": payload.get("code_symbols", []),
                    "embedding": embeddings[0],
                    "embedding_provider": self.model_config.provider,
                    "embedding_model": self.model_config.embedding_model,
                    "embedding_version": self.model_config.embedding_version,
                    "index_version": self.model_config.index_version,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
            )

            # Phase 2 (FR-14): Extract structured health indicators if in 04_Health_Archive
            if payload.get("extract_health_metrics") and category == "04_Health_Archive":
                candidates = extract_health_metrics_from_text(
                    payload.get("raw_text", payload["compiled_body"]),
                    default_subject=payload.get("subject_person", "本人"),
                    page_number=payload["source_page"],
                )
                for cand in candidates:
                    metric_id = str(uuid.uuid4())
                    conn.insert_row(
                        "health_metrics",
                        "metric_id",
                        {
                            "metric_id": metric_id,
                            "space_id": space_id,
                            "category": "04_Health_Archive",
                            "source_doc_id": doc_id,
                            "subject_person": cand["subject_person"],
                            "metric_name": cand["metric_name"],
                            "raw_extracted_value": cand["raw_extracted_value"],
                            "normalized_value": cand["normalized_value"],
                            "unit": cand["unit"],
                            "reference_range": cand["reference_range"],
                            "measured_date": cand["measured_date"],
                            "source_locator": cand["source_locator"],
                            "verification_status": "NEEDS_CHECK",
                            "verified_by": None,
                            "verified_at": None,
                            "revision_history": [],
                            "created_at": datetime.now(timezone.utc).isoformat(),
                        },
                    )

            ledger_event_id = payload["ledger_event_id"]
            if ledger_event_id not in self.db._state["ledger_entries"]:
                self.db._state["ledger_entries"][ledger_event_id] = {
                    "event_id": ledger_event_id,
                    "space_id": space_id,
                    "doc_id": doc_id,
                    "task_id": payload["task_id"],
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }

            conn.update_row(
                "outbox_events",
                event_id,
                lambda e: {
                    **e,
                    "delivery_status": "COMPLETED",
                    "processed_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            self._transition_task(conn, payload["task_id"], TaskState.COMMITTED)
            self._record_audit(
                scope.user_id,
                space_id,
                category,
                "INGEST_COMMITTED",
                "document",
                doc_id,
                {},
                {"doc_id": doc_id, "series_id": series_id, "status": "PUBLISHED_CURRENT"},
            )

    async def run_recovery_worker(self, raw_token: str) -> int:
        scope = self.auth.resolve_scope(raw_token)
        async with profiled_transaction(self.db, scope):
            pending_events = [
                e for e in self.db._state["outbox_events"].values()
                if e["delivery_status"] == "PENDING" and scope.can_write(e["space_id"], e["category"])
            ]
        recovered = 0
        for ev in pending_events:
            await self.process_outbox_event(raw_token, ev["event_id"])
            recovered += 1
        return recovered

    # =========================================================================
    # 8. Hybrid Search with Authorization & Current-Version Filtering (Sec 6)
    # =========================================================================

    async def search_knowledge(
        self,
        raw_token: str,
        query: str,
        *,
        space_filter: Optional[Set[str]] = None,
        category_filter: Optional[Set[str]] = None,
        code_symbol_filter: Optional[str] = None,
        include_history: bool = False,
        top_k: int = 5,
    ) -> Dict[str, Any]:
        scope = self.auth.resolve_scope(raw_token)
        # A-6 zero-key degradation + QA P1-2 resilience: with no usable embedding
        # provider we must neither fake vector recall nor fail outright. Any provider
        # error (missing key, expired key, quota, network) downgrades THIS request to
        # BM25-style keyword ranking and is reported honestly to the caller.
        degraded_reason = ""
        degraded = self.model_adapter.retrieval_mode == "keyword_only"
        q_vec: List[float] = []
        if degraded:
            degraded_reason = self.model_adapter.degraded_reason
        else:
            try:
                q_vec = self.model_adapter.embed_texts(
                    [query], space_id=next(iter(scope.read_spaces))
                )[0]
            except EmbeddingConfigurationError:
                # 配置错（典型：OMNILORE_EMBEDDING_DIM 与 provider 实际维度不符）**必须响亮失败**。
                # 若在这里降级成 keyword_only，用户会以为「向量检索一直好好的」，
                # 而索引里其实已经按错误维度写进去了 —— 那是最坏的一种静默损坏。
                raise
            except _RESPONSE_SHAPE_ERRORS as exc:
                # 与下方 generate_structured_resilient 保持同一套归因（QA P2-B）：
                # 响应拿到了但读不出来，是我们自己的解析假设错误，不是供应商故障。
                _log_response_shape_error(type(exc).__name__, exc)
                degraded = True
                degraded_reason = (
                    f"向量响应解析失败（{type(exc).__name__}），"
                    "本次检索已降级为本地 BM25 / 关键词（keyword_only）（详见 stderr 日志）"
                )
            except Exception as exc:  # provider unreachable / auth / quota / timeout
                degraded = True
                degraded_reason = (
                    f"向量召回不可用（{type(exc).__name__}: {exc}），"
                    "本次检索已降级为本地 BM25 / 关键词（keyword_only）"
                )
        q_tokens = lexical_tokens(query)

        async with profiled_transaction(self.db, scope) as conn:
            candidate_chunks = conn.select_rows("document_chunks")
            docs_by_id = {d["doc_id"]: d for d in conn.select_rows("documents")}
            relations = conn.select_rows("document_relations")

        scored_results: List[Dict[str, Any]] = []
        for ch in candidate_chunks:
            if not include_history and not ch.get("is_current", False):
                continue
            if space_filter and ch["space_id"] not in space_filter:
                continue
            if category_filter and ch["category"] not in category_filter:
                continue
            if code_symbol_filter and code_symbol_filter not in ch.get("code_symbols", []):
                continue

            doc = docs_by_id.get(ch["doc_id"])
            if not doc:
                continue
            if not include_history and doc["doc_status"] not in (
                DocStatus.PUBLISHED_CURRENT.value,
                DocStatus.SUPPLEMENT_ACTIVE.value,
            ):
                continue

            ch_tokens = lexical_tokens(ch["chunk_text"])
            kw_score = len(q_tokens & ch_tokens) / max(1, len(q_tokens))
            if degraded:
                # Pure BM25 / keyword-only ranking: no pseudo-vector component.
                if kw_score == 0.0 and not code_symbol_filter:
                    continue
                cos_sim = 0.0
                total_score = round(kw_score, 6)
            else:
                cos_sim = sum(a * b for a, b in zip(q_vec, ch["embedding"]))
                if kw_score == 0.0 and cos_sim < 0.15 and not code_symbol_filter:
                    continue
                total_score = round(0.5 * kw_score + 0.5 * max(0.0, cos_sim), 6)

            supplements: List[Dict[str, Any]] = []
            for rel in relations:
                if rel["target_doc_id"] == doc["doc_id"] and rel["relation_type"] == "SUPPLEMENT":
                    sup_doc = docs_by_id.get(rel["source_doc_id"])
                    if sup_doc and sup_doc["doc_status"] == DocStatus.SUPPLEMENT_ACTIVE.value:
                        supplements.append({
                            "doc_id": sup_doc["doc_id"],
                            "title": sup_doc["title"],
                            "version_label": sup_doc["version_label"],
                        })

            scored_results.append({
                "chunk_id": ch["chunk_id"],
                "doc_id": doc["doc_id"],
                "series_id": doc["series_id"],
                "space_id": doc["space_id"],
                "category": doc["category"],
                "title": doc["title"],
                "version_label": doc["version_label"],
                "doc_status": doc["doc_status"],
                "heading_path": ch["heading_path"],
                "page_number": ch["page_number"],
                "chunk_text": ch["chunk_text"],
                "code_symbols": ch.get("code_symbols", []),
                "score": total_score,
                "supplements": supplements,
            })

        scored_results.sort(key=lambda x: x["score"], reverse=True)
        top_chunks = scored_results[:top_k]

        first_space = self.auth._spaces[next(iter(scope.read_spaces))]
        synth, synth_note = self.model_adapter.generate_structured_resilient(
            task_type="SYNTHESIZE_ANSWER",
            space=first_space,
            category="01_Work",
            payload={"query": query, "chunks": top_chunks},
            required_schema={
                "answer": str,
                "citations": list,
                "has_sufficient_evidence": bool,
            },
        )
        return {
            "query": query,
            "results": top_chunks,
            "synthesized_answer": synth["answer"],
            "citations": synth["citations"],
            "has_sufficient_evidence": synth["has_sufficient_evidence"],
            # A-6 zero-key / provider-failure contract: callers must be able to tell a
            # genuine hybrid search apart from a keyword-only fallback.
            # 两个维度必须正交（team-lead 裁决 / P2-D）：
            #   * `retrieval_mode` + `degraded_reason` 只描述**检索**是否降级，
            #     调用方可以只用「degraded_reason 是否为空」判断检索能力；
            #   * `synthesis_degraded` + `synthesis_note` 只描述**答案合成**是否回退。
            # 曾经把两者并进 degraded_reason，会出现 `retrieval_mode=hybrid`
            # 却 `degraded_reason` 非空的自相矛盾组合。
            "retrieval_mode": "keyword_only" if degraded else "hybrid",
            "degraded_reason": degraded_reason,
            "synthesis_degraded": bool(synth_note),
            "synthesis_note": synth_note,
        }

    # =========================================================================
    # 9. Obsidian Markdown Sync & Governance Dashboard (Section 10.1 & 10.2)
    # =========================================================================

    async def sync_obsidian_markdown_edit(
        self,
        raw_token: str,
        space_id: str,
        wiki_rel_path: str,
        edited_markdown_on_disk: str,
    ) -> Dict[str, Any]:
        scope = self.auth.resolve_scope(raw_token)
        if space_id not in scope.write_spaces:
            raise PermissionDeniedError("Caller lacks write permission to sync Markdown edits")

        current_disk_hash = hashlib.sha256(edited_markdown_on_disk.encode("utf-8")).hexdigest()
        parsed = parse_and_validate_wiki_markdown(edited_markdown_on_disk)
        fm = parsed.frontmatter
        doc_id = str(fm["doc_id"])

        if self._last_synced_hash_by_doc.get(doc_id) == current_disk_hash:
            return {"status": "SKIPPED_SELF_WRITE_NOOP", "doc_id": doc_id}

        async with profiled_transaction(self.db, scope) as conn:
            docs = conn.select_rows("documents", lambda d: d["doc_id"] == doc_id)
            if not docs:
                raise PermissionDeniedError("Target document for Markdown sync not found or unauthorized")
            doc = docs[0]
            series = conn.select_rows("document_series", lambda s: s["series_id"] == doc["series_id"])[0]

            # R-16：只比对**当前仍受保护**的归属字段。用户把某个字段从白名单里去掉，
            # 就等于把这个字段的主权收回去了 —— 我们不再因为「你改了自己的笔记」而拒绝同步。
            protected_keys = get_protected_frontmatter_keys()
            expected_values = {
                "space_id": doc["space_id"],
                "series_id": doc["series_id"],
                "version_key": series["version_key"],
                "version_label": doc["version_label"],
            }
            comparable = COMPARABLE_PROTECTED_FRONTMATTER_KEYS & protected_keys
            mismatched = sorted(
                key for key, value in expected_values.items()
                if key in comparable and fm.get(key) != value
            )
            if mismatched:
                self._record_audit(
                    scope.user_id,
                    doc["space_id"],
                    doc["category"],
                    "SYNC_PROTECTED_FIELD_REJECTED",
                    "document",
                    doc_id,
                    {"space_id": doc["space_id"], "series_id": doc["series_id"], "version_key": series["version_key"]},
                    {"attempted_frontmatter": fm},
                )
                return {
                    "status": "REJECTED_PROTECTED_FIELD_MODIFICATION",
                    "doc_id": doc_id,
                    "reason": "System-controlled frontmatter fields cannot be altered via text edits",
                    "rejected_fields": mismatched,
                    # 告诉调用方（与用户）：想改这些字段，改配置就能改。
                    "hint": f"可通过 {PROTECTED_FRONTMATTER_ENV} 调整受保护字段白名单",
                }

            # 乐观并发检测：仅在磁盘 frontmatter 里带了可解析的 content_revision 时生效。
            # 用户把 content_revision 也放开保护并删掉该字段，等于主动放弃并发检测 ——
            # 我们不静默丢他的改动，只是不再拦。
            disk_revision = fm.get("content_revision")
            if isinstance(disk_revision, int) and disk_revision != doc["content_revision"]:
                # R-16：冲突副本落隐藏目录，不再散落在用户的 vault 里。
                conflict_rel = build_conflict_copy_rel_path(wiki_rel_path, disk_revision)
                self.storage.write_wiki_by_record(doc["space_id"], conflict_rel, edited_markdown_on_disk)
                self._record_audit(
                    scope.user_id,
                    doc["space_id"],
                    doc["category"],
                    "SYNC_CONFLICT_COPY_CREATED",
                    "document",
                    doc_id,
                    {"expected_revision": doc["content_revision"]},
                    {"editor_revision": fm["content_revision"], "conflict_path": conflict_rel},
                )
                return {
                    "status": "CONFLICT_COPY_CREATED",
                    "doc_id": doc_id,
                    "conflict_rel_path": conflict_rel,
                }

            new_rev = doc["content_revision"] + 1
            fm["content_revision"] = new_rev
            normalized_md = render_wiki_markdown(fm, parsed.body)
            new_hash = hashlib.sha256(normalized_md.encode("utf-8")).hexdigest()

            self.storage.write_wiki_by_record(doc["space_id"], doc["wiki_rel_path"], normalized_md)
            self._last_synced_hash_by_doc[doc_id] = new_hash

            conn.update_row(
                "documents",
                doc_id,
                lambda d: {
                    **d,
                    "title": fm.get("title", d["title"]),
                    "summary": fm.get("summary", d["summary"]),
                    "tags": fm.get("tags", d["tags"]),
                    "outbound_links": extract_outbound_wiki_links(parsed.body),
                    "content_revision": new_rev,
                    "file_hash": new_hash,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
            )

            new_emb = self.model_adapter.embed_texts([parsed.body], space_id=doc["space_id"])[0]
            new_syms = extract_code_symbols(parsed.body)
            for ch in conn.select_rows("document_chunks", lambda c: c["doc_id"] == doc_id):
                conn.update_row(
                    "document_chunks",
                    ch["chunk_id"],
                    lambda c: {
                        **c,
                        "chunk_text": parsed.body,
                        "code_symbols": new_syms,
                        "embedding": new_emb,
                    },
                )

            self._record_audit(
                scope.user_id,
                doc["space_id"],
                doc["category"],
                "OBSIDIAN_MARKDOWN_SYNCED",
                "document",
                doc_id,
                {"content_revision": new_rev - 1},
                {"content_revision": new_rev},
            )
            return {
                "status": "SYNCED",
                "doc_id": doc_id,
                "content_revision": new_rev,
            }

    async def generate_obsidian_dashboard(self, raw_token: str, space_id: str) -> str:
        """
        Section 10.1 (FR-11): Generates `_dashboard_控制台.md` inside the target space
        displaying all 6 required governance views.
        """
        scope = self.auth.resolve_scope(raw_token)
        if space_id not in scope.read_spaces:
            raise PermissionDeniedError("Caller lacks read access to generate space dashboard")

        async with profiled_transaction(self.db, scope) as conn:
            tasks = conn.select_rows("ingestion_tasks", lambda t: t.get("space_id") == space_id)
            docs = conn.select_rows("documents", lambda d: d["space_id"] == space_id)
            chunks = conn.select_rows("document_chunks", lambda c: c["space_id"] == space_id and c.get("is_current"))
            audits = [
                a for a in self.db._state["audit_events"].values()
                if a.get("space_id") == space_id
            ]

        pending_reviews = [t for t in tasks if t["task_state"] == TaskState.NEEDS_REVIEW.value]
        failed_tasks = [
            t for t in tasks
            if t["task_state"] in (TaskState.FAILED_RETRYABLE.value, TaskState.FAILED_FINAL.value)
        ]
        current_docs = [d for d in docs if d.get("is_current")]
        backup_info = self.last_backup_manifest["created_at"] if self.last_backup_manifest else "尚未执行备份"
        restore_info = self.last_restore_drill["restored_at"] if self.last_restore_drill else "尚未执行恢复演练"

        lines = [
            f"# OmniLore 空间治理控制台：`{space_id}`",
            "",
            "## 1. 空间概览与索引健康状态",
            f"- 文档总数：{len(docs)}（当前有效版本数：{len(current_docs)}）",
            f"- 有效检索切片数：{len(chunks)}（Embedding 版本：`{self.model_config.embedding_version}`，索引批次：`v{self.model_config.index_version}`）",
            f"- 待人工确认任务数：{len(pending_reviews)}",
            f"- 失败/可重试任务数：{len(failed_tasks)}",
            "",
            "## 2. 待人工确认任务（NEEDS_REVIEW）",
        ]
        if pending_reviews:
            lines.append("| 任务 ID | 候选类别 | 待确认原因 |")
            lines.append("|---|---|---|")
            for t in pending_reviews:
                cand_cat = t.get("candidate_route", {}).get("suggested_category", t.get("category", "-"))
                lines.append(f"| `{t['task_id']}` | `{cand_cat}` | {t.get('review_reason') or '-'} |")
        else:
            lines.append("- 当前无待确认任务。")

        lines.extend(["", "## 3. 失败与可重试任务"])
        if failed_tasks:
            lines.append("| 任务 ID | 状态 | 错误码 | 错误详情 |")
            lines.append("|---|---|---|---|")
            for t in failed_tasks:
                lines.append(f"| `{t['task_id']}` | `{t['task_state']}` | `{t.get('error_code')}` | {t.get('error_detail')} |")
        else:
            lines.append("- 当前无失败任务。")

        lines.extend(["", "## 4. 当前有效知识版本列表"])
        if current_docs:
            lines.append("| 文档 ID | 标题 | 类别 | 版本 | 状态 | 修订号 |")
            lines.append("|---|---|---|---|---|---|")
            for d in current_docs:
                lines.append(
                    f"| `{d['doc_id']}` | {d['title']} | `{d['category']}` | `{d['version_label']}` | `{d['doc_status']}` | `r{d['content_revision']}` |"
                )
        else:
            lines.append("- 当前无已发布文档。")

        lines.extend(["", "## 5. 最近人工修改、同步冲突与审核记录"])
        for a in sorted(audits, key=lambda x: x["created_at"], reverse=True)[:10]:
            lines.append(f"- `{a['created_at']}` **{a['action_type']}** -> `{a['target_object_type']}:{a['target_object_id']}`")

        lines.extend([
            "",
            "## 6. 备份与恢复演练状态",
            f"- 最近备份时间：{backup_info}",
            f"- 最近恢复演练：{restore_info}",
            "",
        ])
        dashboard_md = "\n".join(lines)
        space_dir = self.storage.ensure_space_dirs(space_id)
        write_text_canonical(space_dir / "_dashboard_控制台.md", dashboard_md)
        return dashboard_md

    # =========================================================================
    # 10. Phase 2: Family Member Management & Knowledge Lint (FR-12, FR-13)
    # =========================================================================

    def invite_or_update_family_member(
        self,
        raw_token: str,
        *,
        family_space_id: str,
        target_user_id: str,
        role: RoleType,
        allowed_categories: Optional[Set[str]] = None,
    ) -> MembershipRecord:
        """Section 3.1 FR-13: Invite or update role of a member in a Family Hub space."""
        scope = self.auth.resolve_scope(raw_token)
        caller_mem = self.auth._memberships.get((scope.user_id, family_space_id))
        if not caller_mem or caller_mem.revoked_at is not None or caller_mem.role != RoleType.ADMIN:
            raise PermissionDeniedError("Only an active ADMIN of the Family Hub can manage family members")

        cats = allowed_categories or {"03_Family", "04_Health_Archive"}
        mem = MembershipRecord(
            membership_id=str(uuid.uuid4()),
            user_id=target_user_id,
            space_id=family_space_id,
            role=role,
            allowed_categories=set(cats),
            granted_by=scope.user_id,
        )
        self.auth.grant_membership(mem)
        self._record_audit(
            scope.user_id,
            family_space_id,
            "03_Family",
            "FAMILY_MEMBER_GRANTED",
            "membership",
            f"{target_user_id}:{family_space_id}",
            {},
            {"role": role.value, "allowed_categories": sorted(cats)},
        )
        return mem

    def remove_family_member(self, raw_token: str, *, family_space_id: str, target_user_id: str) -> None:
        scope = self.auth.resolve_scope(raw_token)
        caller_mem = self.auth._memberships.get((scope.user_id, family_space_id))
        if not caller_mem or caller_mem.revoked_at is not None or caller_mem.role != RoleType.ADMIN:
            raise PermissionDeniedError("Only an active ADMIN of the Family Hub can revoke family members")

        self.auth.revoke_membership(target_user_id, family_space_id)
        self._record_audit(
            scope.user_id,
            family_space_id,
            "03_Family",
            "FAMILY_MEMBER_REVOKED",
            "membership",
            f"{target_user_id}:{family_space_id}",
            {"status": "ACTIVE"},
            {"status": "REVOKED"},
        )

    async def run_knowledge_lint(self, raw_token: str, space_id: str) -> Dict[str, Any]:
        """
        Section 3.1 FR-12: TTL & Knowledge Lint.
        Detects expired documents, orphan `[[wiki_links]]`, unverified health metrics,
        and potential series conflicts. Generates suggestions without silently rewriting content.
        """
        scope = self.auth.resolve_scope(raw_token)
        if space_id not in scope.read_spaces:
            raise PermissionDeniedError("Caller lacks read access to run Knowledge Lint on space")

        now_iso = datetime.now(timezone.utc).isoformat()
        async with profiled_transaction(self.db, scope) as conn:
            docs = conn.select_rows("documents", lambda d: d["space_id"] == space_id and d.get("is_current"))
            health_rows = conn.select_rows("health_metrics", lambda m: m["space_id"] == space_id)

        known_titles = {d["title"] for d in docs}
        known_doc_ids = {d["doc_id"] for d in docs}

        expired_docs: List[Dict[str, Any]] = []
        orphan_links: List[Dict[str, Any]] = []
        for d in docs:
            ttl_exp = d.get("ttl_expires_at")
            if ttl_exp and ttl_exp <= now_iso:
                expired_docs.append({
                    "doc_id": d["doc_id"],
                    "title": d["title"],
                    "ttl_expires_at": ttl_exp,
                    "suggestion": "文档已超过设定 TTL，建议复核有效性或归档",
                })
            for link_target in d.get("outbound_links", []):
                if link_target not in known_titles and link_target not in known_doc_ids:
                    orphan_links.append({
                        "source_doc_id": d["doc_id"],
                        "source_title": d["title"],
                        "missing_target": link_target,
                        "suggestion": f"未找到链接目标 [[{link_target}]]，建议补充文档或修正链接",
                    })

        unverified_metrics = [
            {
                "metric_id": m["metric_id"],
                "subject_person": m["subject_person"],
                "metric_name": m["metric_name"],
                "raw_extracted_value": m["raw_extracted_value"],
            }
            for m in health_rows
            if m["verification_status"] == "NEEDS_CHECK"
        ]

        return {
            "space_id": space_id,
            "expired_documents": expired_docs,
            "orphan_wiki_links": orphan_links,
            "unverified_health_metrics": unverified_metrics,
            "total_suggestions": len(expired_docs) + len(orphan_links) + len(unverified_metrics),
        }


# =============================================================================
# 11. FastMCP Tool Gateway (All 7 Tools + All 7 Review Actions Implemented)
# =============================================================================

class OmniLoreMCPGateway:
    def __init__(self, engine: OmniLoreEngine):
        self.engine = engine

    @staticmethod
    def render_task_receipt(task: Dict[str, Any], doc: Optional[Dict[str, Any]] = None) -> str:
        state = task["task_state"]
        space_display = task.get("space_id") if state == TaskState.COMMITTED.value else (task.get("space_id") or "待确认")
        if state == TaskState.NEEDS_REVIEW.value:
            space_display = f"{task.get('space_id', '待确认')}（待确认）"
        title_display = doc["title"] if doc else "尚未生成"
        asset_ref = f"doc://{doc['doc_id']}/raw" if doc else "尚未生成"
        review_note = task.get("review_reason") or "无"
        next_step_map = {
            TaskState.COMMITTED.value: "检索 / 纠错 / 版本演进",
            TaskState.NEEDS_REVIEW.value: "批准 (APPROVE_REVIEW) / 重分类 (RECLASSIFY) / 拆分 (SPLIT_DOCUMENT) / 取消 (CANCEL_TASK)",
            TaskState.FAILED_RETRYABLE.value: "系统自动重试或手动触发重试",
            TaskState.FAILED_FINAL.value: "修正输入后重新提交或取消",
            TaskState.CANCELLED.value: "已取消",
        }
        next_action = next_step_map.get(state, "查询任务状态")
        return (
            "## 知识任务回执\n"
            f"- 任务 ID：{task['task_id']}\n"
            f"- 状态：{state}\n"
            f"- 目标空间：{space_display}\n"
            f"- 文档：{title_display}\n"
            f"- 原始资产：{asset_ref}\n"
            f"- 来源：第 1 页 / 默认章节\n"
            f"- 处理摘要：{doc['summary'] if doc else (task.get('error_detail') or '等待处理')}\n"
            f"- 需要复核：{review_note}\n"
            f"- 下一步：{next_action}\n"
        )

    async def search_knowledge(
        self,
        raw_token: str,
        query: str,
        space_ids: Optional[List[str]] = None,
        categories: Optional[List[str]] = None,
        code_symbol: Optional[str] = None,
        include_history: bool = False,
    ) -> Dict[str, Any]:
        return await self.engine.search_knowledge(
            raw_token,
            query,
            space_filter=set(space_ids) if space_ids else None,
            category_filter=set(categories) if categories else None,
            code_symbol_filter=code_symbol,
            include_history=include_history,
        )

    async def get_document_asset(
        self,
        raw_token: str,
        doc_id: str,
        asset_type: str = "WIKI",
    ) -> Dict[str, Any]:
        if asset_type not in ("WIKI", "RAW_ASSET"):
            raise OmniLoreError("INVALID_ASSET_TYPE", "asset_type must be 'WIKI' or 'RAW_ASSET'")
        if "/" in doc_id or "\\" in doc_id or ".." in doc_id:
            raise PermissionDeniedError("Client bare paths are prohibited in get_document_asset")

        scope = self.engine.auth.resolve_scope(raw_token)
        async with profiled_transaction(self.engine.db, scope) as conn:
            docs = conn.select_rows("documents", lambda d: d["doc_id"] == doc_id)
            if not docs:
                raise PermissionDeniedError("Resource not found or access denied")
            doc = docs[0]
            if asset_type == "WIKI":
                wiki_content = self.engine.storage.read_wiki_by_record(doc["space_id"], doc["wiki_rel_path"])
                return {
                    "doc_id": doc_id,
                    "asset_type": "WIKI",
                    "content": wiki_content,
                    "version_label": doc["version_label"],
                }

            doc_assets = conn.select_rows("document_assets", lambda da: da["doc_id"] == doc_id)
            if not doc_assets:
                raise PermissionDeniedError("Resource not found or access denied")
            asset_id = doc_assets[0]["asset_id"]
            asset_rec = self.engine.db._state["assets"][asset_id]
            raw_bytes = self.engine.storage.read_cas_asset(
                asset_rec["storage_rel_path"],
                asset_rec["sha256_hash"],
            )
            return {
                "doc_id": doc_id,
                "asset_id": asset_id,
                "asset_type": "RAW_ASSET",
                "sha256_hash": asset_rec["sha256_hash"],
                "raw_bytes": raw_bytes,
                "source_locator": doc_assets[0]["source_locator"],
            }

    async def ingest_document_or_file(
        self,
        raw_token: str,
        *,
        content_bytes: bytes,
        filename: str,
        target_space_id: Optional[str] = "AUTO",
        target_category: Optional[str] = None,
        version_key: Optional[str] = None,
        version_label: str = "v1.0",
        operation_type: OperationType = OperationType.INGEST_FILE,
        expected_current_doc_id: Optional[str] = None,
        parent_doc_id: Optional[str] = None,
        code_symbols: Optional[List[str]] = None,
        ttl_days: Optional[int] = None,
        explicit_external_approval: bool = False,
        extract_health_metrics: bool = False,
        subject_person: str = "本人",
    ) -> Dict[str, Any]:
        task = await self.engine.ingest_document(
            raw_token,
            raw_bytes=content_bytes,
            original_filename=filename,
            target_space_id=target_space_id,
            target_category=target_category,
            version_key=version_key,
            version_label=version_label,
            operation_type=operation_type,
            expected_current_doc_id=expected_current_doc_id,
            parent_doc_id=parent_doc_id,
            code_symbols=code_symbols,
            ttl_days=ttl_days,
            explicit_external_approval=explicit_external_approval,
            extract_health_metrics=extract_health_metrics,
            subject_person=subject_person,
        )
        scope = self.engine.auth.resolve_scope(raw_token)
        doc = None
        if task.get("result_doc_id"):
            async with profiled_transaction(self.engine.db, scope) as conn:
                docs = conn.select_rows("documents", lambda d: d["doc_id"] == task["result_doc_id"])
                doc = docs[0] if docs else None
        return {
            "task": task,
            "receipt_markdown": self.render_task_receipt(task, doc),
        }

    async def commit_memory_note(
        self,
        raw_token: str,
        *,
        title: str,
        summary_markdown: str,
        target_space_id: str,
        target_category: str = "01_Work",
        version_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        note_bytes = f"# {title}\n\n{summary_markdown}".encode("utf-8")
        return await self.ingest_document_or_file(
            raw_token,
            content_bytes=note_bytes,
            filename=f"{title}.md",
            target_space_id=target_space_id,
            target_category=target_category,
            version_key=version_key,
            operation_type=OperationType.COMMIT_NOTE,
        )

    async def get_ingestion_task(self, raw_token: str, task_id: str) -> Dict[str, Any]:
        scope = self.engine.auth.resolve_scope(raw_token)
        async with profiled_transaction(self.engine.db, scope) as conn:
            tasks = conn.select_rows("ingestion_tasks", lambda t: t["task_id"] == task_id)
            if not tasks:
                raise PermissionDeniedError("Task not found or access denied")
            return tasks[0]

    async def review_and_fix_knowledge(
        self,
        raw_token: str,
        *,
        action: ReviewAction,
        target_id: str,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Section 9.4 Controlled Review & Correction Enum Operations:
          1. APPROVE_REVIEW
          2. RECLASSIFY
          3. SPLIT_DOCUMENT
          4. FIX_EXTRACTED_FIELD
          5. REVERT_VERSION
          6. PROMOTE_COPY (supports redaction preview & confirmed copy)
          7. CANCEL_TASK
        """
        if not isinstance(action, ReviewAction):
            raise OmniLoreError("INVALID_REVIEW_ACTION", f"Unsupported action: {action}")

        scope = self.engine.auth.resolve_scope(raw_token)

        # ---------------------------------------------------------------------
        # 1. CANCEL_TASK
        # ---------------------------------------------------------------------
        if action == ReviewAction.CANCEL_TASK:
            async with profiled_transaction(self.engine.db, scope) as conn:
                updated = self.engine._transition_task(conn, target_id, TaskState.CANCELLED)
                self.engine._record_audit(
                    scope.user_id,
                    updated.get("space_id"),
                    updated.get("category"),
                    "CANCEL_TASK",
                    "ingestion_task",
                    target_id,
                    {},
                    {"task_state": TaskState.CANCELLED.value},
                )
                return updated

        # ---------------------------------------------------------------------
        # 2. APPROVE_REVIEW (Approves a NEEDS_REVIEW task and commits it)
        # ---------------------------------------------------------------------
        if action == ReviewAction.APPROVE_REVIEW:
            async with profiled_transaction(self.engine.db, scope) as conn:
                tasks = conn.select_rows("ingestion_tasks", lambda t: t["task_id"] == target_id)
                if not tasks:
                    raise PermissionDeniedError("Task not found or unauthorized")
                task_row = tasks[0]
                if task_row["task_state"] != TaskState.NEEDS_REVIEW.value:
                    raise OmniLoreError("INVALID_TASK_STATE", "Only NEEDS_REVIEW tasks can be approved")

            approved_space = payload.get("target_space_id") or task_row["candidate_route"].get("suggested_space_id") or task_row["space_id"]
            approved_cat = payload.get("target_category") or task_row["candidate_route"].get("suggested_category") or task_row["category"]
            if not scope.can_write(approved_space, approved_cat):
                raise PermissionDeniedError(f"Caller lacks write permission for approved target ({approved_space}, {approved_cat})")

            staged_raw_bytes = self.engine.storage.read_staged_raw_bytes(task_row["staging_rel_path"])
            parsed_doc = parse_document_bytes(staged_raw_bytes, task_row["candidate_route"].get("original_filename", "approved.md"))
            title = payload.get("title") or task_row["candidate_route"].get("title") or "Approved Knowledge Document"
            summary = payload.get("summary") or task_row["candidate_route"].get("summary") or parsed_doc.raw_text[:120]
            compiled_md = f"# {title}\n\n## 核心摘要\n{summary}\n\n## 原始证据引用\n> {parsed_doc.raw_text.splitlines()[-1]}\n"

            committed_task = await self.engine._stage_and_commit_task(
                raw_token=raw_token,
                scope=scope,
                task_id=target_id,
                resolved_space_id=approved_space,
                resolved_category=approved_cat,
                original_filename=task_row["candidate_route"].get("original_filename", "approved.md"),
                canonical_hash=task_row["canonical_file_hash"],
                raw_bytes_len=len(staged_raw_bytes),
                raw_text=parsed_doc.raw_text,
                staged_raw_rel=task_row["staging_rel_path"],
                model_res={
                    "title": title,
                    "summary": summary,
                    "compiled_markdown": compiled_md,
                    "tags": ["approved", approved_cat.lower()],
                },
                version_key=payload.get("version_key"),
                version_label=payload.get("version_label", "v1.0"),
                operation_type=OperationType(task_row["operation_type"]),
                expected_current_doc_id=None,
                parent_doc_id=None,
                code_symbols=parsed_doc.code_symbols,
                outbound_links=parsed_doc.outbound_wiki_links,
                source_page=1,
                source_heading="Approved Section",
                ttl_days=payload.get("ttl_days"),
                extract_health_metrics=(approved_cat == "04_Health_Archive"),
                subject_person=payload.get("subject_person", "本人"),
                simulate_crash_after_stage=False,
            )
            self.engine._record_audit(
                scope.user_id,
                approved_space,
                approved_cat,
                "APPROVE_REVIEW",
                "ingestion_task",
                target_id,
                {"task_state": "NEEDS_REVIEW"},
                {"task_state": "COMMITTED", "result_doc_id": committed_task["result_doc_id"]},
            )
            return committed_task

        # ---------------------------------------------------------------------
        # 3. RECLASSIFY (Moves an existing published document to a new category/space)
        # ---------------------------------------------------------------------
        if action == ReviewAction.RECLASSIFY:
            new_space_id = payload.get("new_space_id")
            new_category = payload.get("new_category")
            if not new_space_id or not new_category:
                raise OmniLoreError("INVALID_PAYLOAD", "RECLASSIFY requires new_space_id and new_category")
            if not scope.can_write(new_space_id, new_category):
                raise PermissionDeniedError(f"Caller lacks write permission in destination ({new_space_id}, {new_category})")

            async with profiled_transaction(self.engine.db, scope) as conn:
                docs = conn.select_rows("documents", lambda d: d["doc_id"] == target_id)
                if not docs:
                    raise PermissionDeniedError("Document not found or unauthorized")
                doc = docs[0]
                old_space_id, old_cat = doc["space_id"], doc["category"]
                if not scope.can_write(old_space_id, old_cat):
                    raise PermissionDeniedError("Caller lacks write permission on source document")

                old_wiki_text = self.engine.storage.read_wiki_by_record(old_space_id, doc["wiki_rel_path"])
                parsed_md = parse_and_validate_wiki_markdown(old_wiki_text)
                parsed_md.frontmatter["space_id"] = new_space_id
                parsed_md.frontmatter["content_revision"] = doc["content_revision"] + 1
                if payload.get("new_title"):
                    parsed_md.frontmatter["title"] = payload["new_title"]
                new_wiki_text = render_wiki_markdown(parsed_md.frontmatter, parsed_md.body)
                new_wiki_hash = hashlib.sha256(new_wiki_text.encode("utf-8")).hexdigest()

                new_wiki_rel = self.engine.storage.move_wiki_across_spaces(
                    old_space_id,
                    doc["wiki_rel_path"],
                    new_space_id,
                    new_wiki_text,
                )
                self.engine._last_synced_hash_by_doc[target_id] = new_wiki_hash

                conn.update_row(
                    "document_series",
                    doc["series_id"],
                    lambda s: {**s, "space_id": new_space_id, "category": new_category},
                )
                updated_doc = conn.update_row(
                    "documents",
                    target_id,
                    lambda d: {
                        **d,
                        "space_id": new_space_id,
                        "category": new_category,
                        "title": parsed_md.frontmatter.get("title", d["title"]),
                        "wiki_rel_path": new_wiki_rel,
                        "file_hash": new_wiki_hash,
                        "content_revision": d["content_revision"] + 1,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
                for ch in conn.select_rows("document_chunks", lambda c: c["doc_id"] == target_id):
                    conn.update_row(
                        "document_chunks",
                        ch["chunk_id"],
                        lambda c: {**c, "space_id": new_space_id, "category": new_category},
                    )

                self.engine._record_audit(
                    scope.user_id,
                    new_space_id,
                    new_category,
                    "RECLASSIFY_DOCUMENT",
                    "document",
                    target_id,
                    {"space_id": old_space_id, "category": old_cat},
                    {"space_id": new_space_id, "category": new_category},
                )
                return updated_doc

        # ---------------------------------------------------------------------
        # 4. SPLIT_DOCUMENT (Splits a quarantined/multi-intent task into N docs)
        # ---------------------------------------------------------------------
        if action == ReviewAction.SPLIT_DOCUMENT:
            splits: List[Dict[str, Any]] = payload.get("splits", [])
            if len(splits) < 2:
                raise OmniLoreError("INVALID_PAYLOAD", "SPLIT_DOCUMENT requires at least 2 split definitions")

            async with profiled_transaction(self.engine.db, scope) as conn:
                tasks = conn.select_rows("ingestion_tasks", lambda t: t["task_id"] == target_id)
                if not tasks:
                    raise PermissionDeniedError("Quarantined task not found or unauthorized")
                orig_task = tasks[0]
                if orig_task["task_state"] != TaskState.NEEDS_REVIEW.value:
                    raise OmniLoreError("INVALID_TASK_STATE", "Only NEEDS_REVIEW tasks can be split")

            created_docs: List[str] = []
            for idx, sp_item in enumerate(splits, start=1):
                sp_space = sp_item["target_space_id"]
                sp_cat = sp_item["target_category"]
                sp_title = sp_item["title"]
                sp_content = sp_item["content"]
                if not scope.can_write(sp_space, sp_cat):
                    raise PermissionDeniedError(f"Caller lacks write permission for split target ({sp_space}, {sp_cat})")

                sub_res = await self.ingest_document_or_file(
                    raw_token,
                    content_bytes=f"# {sp_title}\n\n{sp_content}".encode("utf-8"),
                    filename=f"split_{idx}_{sp_title}.md",
                    target_space_id=sp_space,
                    target_category=sp_cat,
                    explicit_external_approval=bool(sp_item.get("explicit_external_approval", False)),
                    extract_health_metrics=(sp_cat == "04_Health_Archive"),
                )
                created_docs.append(sub_res["task"]["result_doc_id"])

            async with profiled_transaction(self.engine.db, scope) as conn:
                self.engine._transition_task(conn, target_id, TaskState.CANCELLED, review_reason="已拆分为多个独立文档")

            self.engine._record_audit(
                scope.user_id,
                orig_task.get("space_id"),
                orig_task.get("category"),
                "SPLIT_DOCUMENT",
                "ingestion_task",
                target_id,
                {"task_state": "NEEDS_REVIEW"},
                {"split_doc_ids": created_docs},
            )
            return {"status": "SPLIT_COMPLETED", "source_task_id": target_id, "created_doc_ids": created_docs}

        # ---------------------------------------------------------------------
        # 5. REVERT_VERSION
        # ---------------------------------------------------------------------
        if action == ReviewAction.REVERT_VERSION:
            expected_current_doc_id = payload.get("expected_current_doc_id")
            target_rollback_doc_id = payload.get("rollback_to_doc_id")
            if not expected_current_doc_id or not target_rollback_doc_id:
                raise OmniLoreError("INVALID_PAYLOAD", "REVERT_VERSION requires expected_current_doc_id and rollback_to_doc_id")

            async with profiled_transaction(self.engine.db, scope) as conn:
                series_list = conn.select_rows("document_series", lambda s: s["series_id"] == target_id)
                if not series_list:
                    raise PermissionDeniedError("Series not found or unauthorized")
                series = series_list[0]
                if not scope.can_write(series["space_id"], series["category"]):
                    raise PermissionDeniedError("Caller lacks write permission to revert series version")
                if series["current_doc_id"] != expected_current_doc_id:
                    raise VersionConflictError("Series current_doc_id changed before revert could execute")

                conn.update_row(
                    "documents",
                    expected_current_doc_id,
                    lambda d: {**d, "doc_status": DocStatus.REVERTED.value, "is_current": False},
                )
                for ch in conn.select_rows("document_chunks", lambda c: c["doc_id"] == expected_current_doc_id):
                    conn.update_row("document_chunks", ch["chunk_id"], lambda c: {**c, "is_current": False})

                conn.update_row(
                    "documents",
                    target_rollback_doc_id,
                    lambda d: {**d, "doc_status": DocStatus.PUBLISHED_CURRENT.value, "is_current": True},
                )
                for ch in conn.select_rows("document_chunks", lambda c: c["doc_id"] == target_rollback_doc_id):
                    conn.update_row("document_chunks", ch["chunk_id"], lambda c: {**c, "is_current": True})

                updated_series = conn.update_row(
                    "document_series",
                    target_id,
                    lambda s: {
                        **s,
                        "current_doc_id": target_rollback_doc_id,
                        "revision_number": s["revision_number"] + 1,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
                self.engine._record_audit(
                    scope.user_id,
                    series["space_id"],
                    series["category"],
                    "REVERT_VERSION",
                    "document_series",
                    target_id,
                    {"current_doc_id": expected_current_doc_id},
                    {"current_doc_id": target_rollback_doc_id},
                )
                return updated_series

        # ---------------------------------------------------------------------
        # 6. PROMOTE_COPY (FR-15: Supports Redaction Preview & Confirmed Copy)
        # ---------------------------------------------------------------------
        if action == ReviewAction.PROMOTE_COPY:
            dest_space_id = payload.get("dest_space_id")
            dest_category = payload.get("dest_category", "03_Family")
            preview_only = bool(payload.get("preview_only", False))
            apply_redaction = bool(payload.get("apply_redaction", False))
            explicit_confirm = bool(payload.get("explicit_user_confirm", False))

            if not dest_space_id:
                raise OmniLoreError("INVALID_PAYLOAD", "PROMOTE_COPY requires dest_space_id")
            if not scope.can_write(dest_space_id, dest_category):
                raise PermissionDeniedError(f"Caller lacks write permission in destination ({dest_space_id}, {dest_category})")

            wiki_res = await self.get_document_asset(raw_token, target_id, asset_type="WIKI")
            parsed = parse_and_validate_wiki_markdown(wiki_res["content"])
            scan_report = scan_and_redact_sensitive_text(parsed.body)

            visible_members = [
                uid for (uid, sp), mem in self.engine.auth._memberships.items()
                if sp == dest_space_id and mem.revoked_at is None
            ]

            if preview_only:
                return {
                    "status": "PROMOTION_PREVIEW",
                    "source_doc_id": target_id,
                    "dest_space_id": dest_space_id,
                    "dest_category": dest_category,
                    "has_sensitive_data": scan_report["has_sensitive_data"],
                    "sensitive_findings": scan_report["findings"],
                    "preview_body": scan_report["redacted_text"] if apply_redaction else parsed.body,
                    "target_visible_members": sorted(visible_members),
                }

            if not explicit_confirm:
                raise OmniLoreError("CONFIRMATION_REQUIRED", "PROMOTE_COPY requires explicit_user_confirm=True")

            final_body = scan_report["redacted_text"] if apply_redaction else parsed.body
            promoted = await self.ingest_document_or_file(
                raw_token,
                content_bytes=final_body.encode("utf-8"),
                filename=f"promoted_{target_id[:8]}.md",
                target_space_id=dest_space_id,
                target_category=dest_category,
            )
            promoted_doc_id = promoted["task"]["result_doc_id"]

            async with profiled_transaction(self.engine.db, scope) as conn:
                conn.insert_row(
                    "document_relations",
                    "relation_id",
                    {
                        "relation_id": str(uuid.uuid4()),
                        "space_id": dest_space_id,
                        "category": dest_category,
                        "source_doc_id": promoted_doc_id,
                        "target_doc_id": target_id,
                        "relation_type": "PROMOTED_FROM",
                        "created_by": scope.user_id,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    },
                )

            self.engine._record_audit(
                scope.user_id,
                dest_space_id,
                dest_category,
                "PROMOTE_COPY",
                "document",
                target_id,
                {"source_doc_id": target_id, "redacted": apply_redaction},
                {"promoted_doc_id": promoted_doc_id, "dest_space_id": dest_space_id},
            )
            return promoted

        # ---------------------------------------------------------------------
        # 7. FIX_EXTRACTED_FIELD (Phase 2 Health Metric Human Verification)
        # ---------------------------------------------------------------------
        if action == ReviewAction.FIX_EXTRACTED_FIELD:
            async with profiled_transaction(self.engine.db, scope) as conn:
                updated_metric = conn.update_row(
                    "health_metrics",
                    target_id,
                    lambda m: {
                        **m,
                        "normalized_value": payload.get("normalized_value", m["normalized_value"]),
                        "unit": payload.get("unit", m["unit"]),
                        "reference_range": payload.get("reference_range", m["reference_range"]),
                        "verification_status": "HUMAN_VERIFIED",
                        "verified_by": scope.user_id,
                        "verified_at": datetime.now(timezone.utc).isoformat(),
                        "revision_history": m["revision_history"] + [{
                            "verified_by": scope.user_id,
                            "old_value": m["normalized_value"],
                            "new_value": payload.get("normalized_value", m["normalized_value"]),
                        }],
                    },
                )
                self.engine._record_audit(
                    scope.user_id,
                    updated_metric["space_id"],
                    "04_Health_Archive",
                    "VERIFY_HEALTH_METRIC",
                    "health_metric",
                    target_id,
                    {"verification_status": "NEEDS_CHECK"},
                    {"verification_status": "HUMAN_VERIFIED"},
                )
                return updated_metric

        raise OmniLoreError("NOT_IMPLEMENTED", f"Action {action} handler not configured")

    async def query_health_trends(
        self,
        raw_token: str,
        *,
        space_id: str,
        subject_person: str,
        metric_name: str,
    ) -> List[Dict[str, Any]]:
        scope = self.engine.auth.resolve_scope(raw_token)
        if not scope.can_read(space_id, "04_Health_Archive"):
            raise PermissionDeniedError("Caller lacks 04_Health_Archive read permission for health trends")

        async with profiled_transaction(self.engine.db, scope) as conn:
            rows = conn.select_rows(
                "health_metrics",
                lambda m: (
                    m["space_id"] == space_id
                    and m["subject_person"] == subject_person
                    and m["metric_name"] == metric_name
                ),
            )
            for r in rows:
                r["display_warning"] = (
                    "AI 提取，尚未核验" if r["verification_status"] != "HUMAN_VERIFIED" else "人工已核验"
                )
            return sorted(rows, key=lambda x: str(x["measured_date"]))
