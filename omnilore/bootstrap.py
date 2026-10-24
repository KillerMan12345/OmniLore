"""
Zero-Key First-Run Bootstrap (Open-Source Launch W0 / A-2)
==========================================================
Problem this module solves
--------------------------
All 7 MCP tools exposed by `omnilore.mcp_stdio_server` take a **mandatory
`token`** first argument, but before this module existed the repository offered
**no method whatsoever** to create a space, a membership, or a token. A brand-new
environment therefore had a first-run success rate of exactly 0%.

This module provides a single reusable entry point — :func:`ensure_bootstrapped` —
that is shared by:

* the `omnilore bootstrap` / `omnilore demo` CLI (`omnilore.cli`), and
* the stdio MCP server entry point (`omnilore.mcp_stdio_server.main_stdio`).

It works **without any API key** (no cloud account required) and is idempotent.

Persistence contract
--------------------
The bundled default database engine (`RLSDatabaseEngine`) is **in-memory**, so a
token issued in one process would otherwise evaporate when the process exits. To
make the token printed by `omnilore bootstrap` genuinely usable by the MCP
server, :func:`ensure_bootstrapped` persists the bootstrap identity (space id,
user id, and raw token) to `<data_dir>/ops/bootstrap.json` and reloads it on every
subsequent start. File permissions are tightened to owner-only where the platform
supports it.

Real PostgreSQL deployments instead apply `sql/002_omnilore_bootstrap_seed.sql`, which
contains the equivalent minimal `INSERT` statements.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
import warnings

from .dashscope_client import MODEL_ADAPTER_ENV_NAMES, ModelAdapterConfig, ModelConfigError
from .embedding_provider import (
    EMBEDDING_API_KEY_ENV,
    EmbeddingConfigurationError,
    describe_effective_embedding,
)
from .textio import read_text_canonical, write_text_canonical

# Default identity material used by `omnilore bootstrap`.
DEFAULT_SPACE_ID = "sp_bootstrap_personal"
DEFAULT_USER_ID = "00000000-0000-4000-8000-000000000001"
DEFAULT_SPACE_TYPE = "PERSONAL"
DEFAULT_TTL_SECONDS = 365 * 24 * 3600  # 1 year — deliberately long-lived for local use
DEFAULT_CATEGORIES = ("01_Work", "02_Life", "03_Family", "04_Health_Archive")

BOOTSTRAP_STATE_FILENAME = "bootstrap.json"
SUPPORTED_STATE_VERSION = 1

# 本项目**认识**的 OMNILORE_* 环境变量（R-16 修复：拼错的变量名必须被看见）。
# 为什么需要这份清单：代码只读 `os.environ`，也不加载 `.env`；把
# `OMNILORE_PROTECTED_FRONTMATTER` 写成 `...FRONTMATTRE` 不会报错，只会静默回落到
# 默认全保护 —— 用户以为自己关掉了保护，其实没有。这份清单让启动回显能点名它。
#: 模型 / 向量相关变量集中由 `omnilore.dashscope_client` 定义（R-04b 起**真的被读取**）。
MODEL_ENV_NAMES: Tuple[str, ...] = MODEL_ADAPTER_ENV_NAMES + (EMBEDDING_API_KEY_ENV,)

KNOWN_OMNILORE_ENV_NAMES: Tuple[str, ...] = (
    "OMNILORE_AUTO_BOOTSTRAP",
    "OMNILORE_BOOTSTRAP_TOKEN",
    "OMNILORE_CONFLICT_DIR",
    "OMNILORE_DATA_DIR",
    "OMNILORE_INTERNAL_API_URL",
    "OMNILORE_MIGRATOR_DSN",
    "OMNILORE_PROTECTED_FRONTMATTER",
    "OMNILORE_TELEMETRY",
    "OMNILORE_WORKER_SCOPED_TOKEN",
) + MODEL_ENV_NAMES


@dataclass(frozen=True)
class BootstrapResult:
    """Structured outcome of :func:`ensure_bootstrapped`.

    Attributes:
        data_dir: Absolute path of the OmniLore data directory.
        space_id: Space id that is now registered (created or reused).
        user_id: Owner user id that is now a member of the space.
        raw_token: The raw (plaintext) token. Shown to the user exactly once by
            `omnilore bootstrap`; afterwards it lives in `bootstrap.json`.
        created_space: True when this call created the space.
        created_token: True when this call issued a brand-new token.
        reused_state: True when the identity was loaded from `bootstrap.json`.
        state_path: Path of the persisted bootstrap state file.
        embedding_provider_available: True when the configured embedding provider
            (R-04b: `dashscope` / `openai` 兼容端点 / Ollama) is usable, i.e. when
            `retrieval_mode == "hybrid"`. Replaces the old `api_key_present` field,
            whose meaning ("DASHSCOPE_API_KEY is set") stopped being the right question
            once the provider became configurable.
        retrieval_mode: `hybrid` when the provider is usable, else `keyword_only`.
        degraded_reason: Human-readable reason when running in degraded mode,
            empty string otherwise.
    """

    data_dir: Path
    space_id: str
    user_id: str
    raw_token: str
    created_space: bool
    created_token: bool
    reused_state: bool
    state_path: Path
    embedding_provider_available: bool
    retrieval_mode: str
    degraded_reason: str

    def as_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable view of this result (never includes secrets)."""
        return {
            "data_dir": str(self.data_dir),
            "space_id": self.space_id,
            "user_id": self.user_id,
            "token_hint": self.token_hint(),
            "created_space": self.created_space,
            "created_token": self.created_token,
            "reused_state": self.reused_state,
            "state_path": str(self.state_path),
            # 旧键保留：它的字面含义（DASHSCOPE_API_KEY 是否设置）仍然是可回答的，
            # 只是不再是「能不能做向量检索」的判据 —— 那个问题看
            # `embedding_provider_available` / `retrieval_mode`。
            "api_key_present": has_api_key(),
            "embedding_provider_available": self.embedding_provider_available,
            "retrieval_mode": self.retrieval_mode,
            "degraded_reason": self.degraded_reason,
        }

    def token_hint(self) -> str:
        """Return a non-secret prefix: `tok_abcd…wxyz` — safe to print in logs."""
        token = self.raw_token
        if len(token) <= 12:
            return token[:2] + "…"
        return f"{token[:4]}…{token[-4:]}"


def bootstrap_state_path(data_dir: Path) -> Path:
    """Return the canonical location of the persisted bootstrap state file."""
    return Path(data_dir) / "ops" / BOOTSTRAP_STATE_FILENAME


def hash_token(raw_token: str) -> str:
    """SHA-256 of a raw token — identical to `AuthorizationService.hash_token`."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def generate_raw_token() -> str:
    """Generate a cryptographically random, URL-safe OmniLore token."""
    return "tok_" + secrets.token_urlsafe(32)


def has_api_key() -> bool:
    """Return True when a DashScope API key is configured in the environment."""
    return bool(os.environ.get("DASHSCOPE_API_KEY", "").strip())


def load_bootstrap_state(data_dir: Path) -> Optional[Dict[str, Any]]:
    """Load the persisted bootstrap state, or return None when absent/corrupt.

    Args:
        data_dir: OmniLore data directory root.

    Returns:
        The parsed state dict, or None if the file does not exist or is invalid.
    """
    path = bootstrap_state_path(data_dir)
    if not path.exists():
        return None
    try:
        state = json.loads(read_text_canonical(path))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(state, dict):
        return None
    if state.get("state_version") != SUPPORTED_STATE_VERSION:
        return None
    raw_token = state.get("raw_token")
    if not isinstance(raw_token, str) or not raw_token:
        return None
    return state


TIGHTEN_FAILED_NOTE = (
    "无法收紧 %s 的访问权限（宿主不支持）；该文件含明文 token，请手动检查其权限。"
)


def restrict_secret_file_permissions(path: Path) -> None:
    """Best-effort restrict a secret-bearing file to the current user only.

    POSIX: `chmod 0600`.
    Windows: `os.chmod()` cannot express POSIX mode bits (a file chmod'ed 0o600 still
    reports `stat.S_IMODE == 0o666`), so we drive `icacls` to grant full control to
    the *current* account and then drop inherited ACEs, which typically include
    `Everyone` / `BUILTIN\\Users`.

    Windows correctness notes (these are real failure modes we hit in testing):
      * Granting by bare `%USERNAME%` silently resolves to a bogus `MACHINE\\`-
        style principal and grants **nobody** access — always use the SID from
        `whoami /user` when available.
      * `/inheritance:r` must therefore be preceded by a *verified* grant, otherwise
        stripping inherited ACEs can lock the owner out entirely.
      * Every step is followed by a readable-back verification, and anything that
        breaks is rolled back with `icacls /reset`.

    This function never raises: permission tightening is best-effort and must never
    block first-run bootstrap. See `SECURITY.md` section 6 for the trade-off.

    Args:
        path: The file to restrict.
    """
    try:
        if os.name == "nt":
            _restrict_windows(path)
        else:
            os.chmod(path, 0o600)
    except OSError:
        warnings.warn(TIGHTEN_FAILED_NOTE % path, RuntimeWarning, stacklevel=2)


def _run_icacls(args: List[str]) -> bool:
    """Run a single `icacls` command; return True when it exited successfully."""
    try:
        proc = subprocess.run(
            args,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _current_windows_principal() -> str:
    """Best-effort identify the current Windows account as an unambiguous principal.

    Returns:
        `*"<SID>"` when `whoami /user` yields a SID (most reliable — no name lookup),
        else `DOMAIN\\USER`, else an empty string when nothing can be determined.
    """
    try:
        proc = subprocess.run(
            ["whoami", "/user"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
        out = (proc.stdout or b"").decode("utf-8", "replace")
        match = re.search(r"(S-1-\d[\d-]+)", out)
        if match:
            return f"*{match.group(1)}"
    except (OSError, subprocess.SubprocessError):
        pass

    domain = os.environ.get("USERDOMAIN", "").strip() or os.environ.get("USERDNSDOMAIN", "").strip()
    user = os.environ.get("USERNAME", "").strip()
    if domain and user:
        return f"{domain}\\{user}"
    return ""


def _is_readable(path: Path) -> bool:
    """Return True when `path` can still be opened for reading by this process."""
    try:
        with open(path, "rb"):
            return True
    except OSError:
        return False


def _restrict_windows(path: Path) -> None:
    """Windows branch of :func:`restrict_secret_file_permissions` using `icacls`."""
    principal = _current_windows_principal()
    if not principal:
        # Without a resolvable principal we must NOT touch inheritance — doing so
        # would strip SYSTEM/Administrators and lock everyone out.
        warnings.warn(TIGHTEN_FAILED_NOTE % path, RuntimeWarning, stacklevel=2)
        return

    reset = ["icacls", str(path), "/reset"]

    # 1) Grant full control to this account (replace any existing ACE for it).
    if not _run_icacls(["icacls", str(path), "/grant:r", f"{principal}:(F)"]):
        warnings.warn(TIGHTEN_FAILED_NOTE % path, RuntimeWarning, stacklevel=2)
        return

    # 2) Verify the grant ACTUALLY LANDED before we remove anything.
    #    「文件仍可读」不是有效证据 —— 继承来的 ACE 本来就能让我们读，
    #    所以旧实现在这里会被骗过去。必须确认存在一条**显式**（非继承）ACE。
    #    `icacls /grant` 可能返回 exit 0 而 DACL 尚未提交，紧接着执行
    #    `/inheritance:r` 就会把一切都剥掉 → **空 DACL，谁都读不了**。
    #    因此这里轮询等待显式 ACE 出现，最多 ~0.5s。
    if not _wait_for(lambda: _has_explicit_ace(path), attempts=6, delay=0.05):
        _run_icacls(reset)
        warnings.warn(TIGHTEN_FAILED_NOTE % path, RuntimeWarning, stacklevel=2)
        return

    # 3) Drop inherited ACEs (SYSTEM / Administrators / Everyone / BUILTIN\Users).
    if not _run_icacls(["icacls", str(path), "/inheritance:r"]):
        warnings.warn(TIGHTEN_FAILED_NOTE % path, RuntimeWarning, stacklevel=2)
        return

    # 4) Final invariant: still readable AND still has an explicit ACE.
    #    （空 DACL 会让两者同时为假，旧实现只查了 readable 且被竞态骗过。）
    if not _is_readable(path) or not _has_explicit_ace(path):
        _run_icacls(reset)
        warnings.warn(TIGHTEN_FAILED_NOTE % path, RuntimeWarning, stacklevel=2)


def _icacls_listing(path: Path) -> str:
    """Return the `icacls <path>` output, or `''` when it cannot be obtained."""
    try:
        proc = subprocess.run(
            ["icacls", str(path)],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    return (proc.stdout or b"").decode("utf-8", "replace")


_ACE_RIGHTS_PATTERN = re.compile(r"\((?:F|M|RX|R|W|D|AD|WD|WO|RC)\)")
_ICACLS_SUMMARY_MARKERS = ("已成功处理", "Successfully processed", "処理")


def _has_explicit_ace(path: Path) -> bool:
    """Return True when `path` has at least one **non-inherited** (explicit) ACE.

    Before any tightening, a file in a normal directory only carries inherited ACEs,
    all flagged `(I)`. A successful `/grant` adds an explicit one. This is therefore
    the only reliable evidence that the grant really landed — "the file is still
    readable" proves nothing, because inherited ACEs already allow that.

    Args:
        path: File to inspect.

    Returns:
        True when at least one explicit ACE is present.
    """
    text = _icacls_listing(path)
    if not text:
        return False
    for line in text.splitlines():
        line = line.replace(str(path), "").strip()
        if not line or any(marker in line for marker in _ICACLS_SUMMARY_MARKERS):
            continue
        if "(I)" in line:
            continue
        if _ACE_RIGHTS_PATTERN.search(line):
            return True
    return False


def _wait_for(predicate: Callable[[], bool], *, attempts: int = 5, delay: float = 0.05) -> bool:
    """Poll `predicate` until it is true (bounded wait for a filesystem side-effect).

    Args:
        predicate: Zero-argument callable returning a boolean.
        attempts: Maximum number of evaluations.
        delay: Seconds to sleep between evaluations.

    Returns:
        True as soon as the predicate holds, False when it never did.
    """
    for attempt in range(attempts):
        if predicate():
            return True
        if attempt + 1 < attempts:
            time.sleep(delay)
    return False


def unknown_omnilore_env_names() -> List[str]:
    """返回进程里以 `OMNILORE_` 开头、但本项目不认识的环境变量名。

    Returns:
        排序后的变量名列表；全部认识时返回空列表。

    Notes:
        这是 R-16 / P2-1 的守卫：变量名拼错时不会报错、不会生效，只会静默回落默认
        行为。与其让用户以为「我关掉了保护」，不如启动时直接点名它。
    """
    return sorted(
        name for name in os.environ
        if name.startswith("OMNILORE_") and name not in KNOWN_OMNILORE_ENV_NAMES
    )


def render_sovereignty_notice() -> List[str]:
    """回显**当前进程真正生效**的主权配置（受保护字段 / 冲突副本目录）。

    为什么必须有这段回显：`OMNILORE_PROTECTED_FRONTMATTER`、`OMNILORE_CONFLICT_DIR`
    只在**进程环境变量**里生效（本项目不读 `.env`），容器模式下还要靠 compose 转发。
    任何一环断了，用户都会得到「静默无效」——这正是主权承诺最坏的失效方式。
    把生效值打出来，失效就变成**可见**的。

    Returns:
        若干行可直接打印的文本；永不抛异常（配置错误也会被转成一行提示）。
    """
    from .engine import (  # 局部导入：避免 bootstrap ↔ engine 的循环依赖
        CONFLICT_COPY_DIR_ENV,
        DEFAULT_CONFLICT_COPY_DIR,
        DEFAULT_PROTECTED_FRONTMATTER_KEYS,
        OmniLoreError,
        PROTECTED_FRONTMATTER_ENV,
        get_conflict_copy_dir,
        get_protected_frontmatter_keys,
    )

    lines: List[str] = []
    try:
        protected = get_protected_frontmatter_keys()
    except OmniLoreError as exc:
        protected = set(DEFAULT_PROTECTED_FRONTMATTER_KEYS)
        lines.append(f"  ⚠️ {PROTECTED_FRONTMATTER_ENV} 取值无效：{exc}")
        lines.append("     已回落到默认的全量保护；请修正后重启。")

    total = len(DEFAULT_PROTECTED_FRONTMATTER_KEYS)
    if protected >= DEFAULT_PROTECTED_FRONTMATTER_KEYS:
        headline = f"受保护 frontmatter 字段（{len(protected)}/{total}，默认全保护）"
    elif protected <= {"doc_id"}:
        headline = f"受保护 frontmatter 字段（已整体关闭，仅保留身份锚点 doc_id；默认 {total}/{total}）"
    else:
        headline = f"受保护 frontmatter 字段（{len(protected)}/{total}，已自定义）"
    # 变量名一并打出来：用户才知道「我刚才 export 的是哪一个」，也才看得出拼错。
    lines.append(f"  {PROTECTED_FRONTMATTER_ENV} → {headline}：{', '.join(sorted(protected))}")

    try:
        conflict_dir = get_conflict_copy_dir()
    except OmniLoreError as exc:
        conflict_dir = DEFAULT_CONFLICT_COPY_DIR
        lines.append(f"  ⚠️ {CONFLICT_COPY_DIR_ENV} 取值无效：{exc}")
        lines.append(f"     已回落到默认目录 {DEFAULT_CONFLICT_COPY_DIR}。")
    lines.append(f"  {CONFLICT_COPY_DIR_ENV} → 冲突副本目录（相对 wiki/）：{conflict_dir}")

    # R-04b：向量来源的生效值同样要可见 —— 「我配了 Ollama，怎么还在打阿里云」
    # 是这一类静默失效里最贵的一种（既花钱又出网）。
    try:
        lines.append(describe_effective_embedding(ModelAdapterConfig()))
    except (ModelConfigError, EmbeddingConfigurationError) as exc:
        lines.append(f"  ⚠️ 向量检索配置无效：{exc}")
        lines.append("     该配置不会生效；请修正后重启（未修正时检索会降级为 BM25）。")
    except Exception as exc:  # pragma: no cover - 回显永不抛异常
        lines.append(f"  ⚠️ 无法读取向量检索配置（{type(exc).__name__}）：已按默认值处理")

    unknown = unknown_omnilore_env_names()
    if unknown:
        lines.append(
            "  ⚠️ 以下 OMNILORE_* 环境变量本项目不认识（疑似拼写错误，当前不生效）："
            + "、".join(unknown)
        )
    lines.append(
        "  说明：本进程只读取环境变量，不会自动读取 .env；容器模式请确认 "
        "docker-compose.yml 已转发（已内置），或用 env_file 注入。"
    )
    return lines


def save_bootstrap_state(data_dir: Path, *, space_id: str, user_id: str, raw_token: str) -> Path:
    """Persist the bootstrap identity to `<data_dir>/ops/bootstrap.json`.

    The raw token is stored because the bundled default database is in-memory and
    the MCP server runs as a separate process from the CLI. On POSIX the file is
    chmod'ed to owner-only (`0o600`).

    Args:
        data_dir: OmniLore data directory root.
        space_id: Space id that was created.
        user_id: Owner user id.
        raw_token: The plaintext token that was issued.

    Returns:
        Absolute path of the written state file.
    """
    path = bootstrap_state_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "state_version": SUPPORTED_STATE_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "space_id": space_id,
        "user_id": user_id,
        "raw_token": raw_token,
        "token_hash": hash_token(raw_token),
    }
    write_text_canonical(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    restrict_secret_file_permissions(path)
    return path


def _register_bootstrap_identity(
    engine: Any,
    *,
    space_id: str,
    user_id: str,
    raw_token: str,
    categories: Optional[set] = None,
) -> None:
    """Register the bootstrap space, ADMIN membership and OMNI token on `engine`."""
    from .engine import MembershipRecord, ProfileType, RoleType, SpaceRecord, SpaceType

    cats = set(categories or set(DEFAULT_CATEGORIES))
    if space_id not in engine.auth._spaces:
        engine.auth.register_space(
            SpaceRecord(
                space_id=space_id,
                space_type=SpaceType(DEFAULT_SPACE_TYPE),
                parent_space_id=None,
                vault_rel_path=f"spaces/{space_id}",
                allowed_categories=set(cats),
            )
        )
    membership_key = (user_id, space_id)
    existing = engine.auth._memberships.get(membership_key)
    if existing is None or existing.revoked_at is not None:
        engine.auth.grant_membership(
            MembershipRecord(
                membership_id=f"ms_{space_id}",
                user_id=user_id,
                space_id=space_id,
                role=RoleType.ADMIN,
                allowed_categories=set(cats),
            )
        )
    if hash_token(raw_token) not in engine.auth._tokens_by_hash:
        engine.auth.issue_token(
            raw_token,
            user_id=user_id,
            profile_type=ProfileType.OMNI,
            allowed_read_spaces={space_id},
            allowed_write_spaces={space_id},
            allowed_categories=set(cats),
            ttl_seconds=DEFAULT_TTL_SECONDS,
        )


def ensure_bootstrapped(
    engine: Any,
    data_dir: Optional[Path] = None,
    *,
    token: Optional[str] = None,
    user_id: Optional[str] = None,
    space_id: Optional[str] = None,
    allow_auto_create: bool = True,
    emit: Optional[Callable[[str], None]] = None,
) -> BootstrapResult:
    """Idempotently guarantee that `engine` has a usable space + owner token.

    Called with no arguments (except `engine`) this is a zero-API-key operation:
    it never touches the network.

    Args:
        engine: A `OmniLoreEngine` instance (in-memory or otherwise).
        data_dir: Data directory; defaults to `engine.storage.base_dir`.
        token: Explicit token to use. When omitted, a persisted token is reused
            and otherwise a fresh random token is generated.
        user_id: Owner user id override.
        space_id: Space id override.
        allow_auto_create: When False and no persisted state/token exists, no new
            token is generated (used to avoid surprising writes from the MCP path
            when the operator disabled auto-bootstrap).
        emit: Optional logging callback receiving already-formatted strings.

    Returns:
        A :class:`BootstrapResult` describing what happened.

    Raises:
        RuntimeError: If a space has to be created but cannot be registered, or
            if the resolved token fails to authorize against the engine.
    """
    base_dir = Path(data_dir) if data_dir is not None else Path(engine.storage.base_dir)
    log = emit or (lambda _msg: None)

    # 检索模式由**当前 provider 的实际可用性**决定（R-04b）：DashScope / OpenAI 兼容 /
    # Ollama / 显式 none 都走同一条判定，不再只看 DASHSCOPE_API_KEY。
    # 这句话必须与 `ModelAdapter.degraded_reason` 同源，否则「bootstrap 说降级、搜索说不降级」
    # 这种自相矛盾会再次出现（QA P1-2 的教训）。
    adapter = getattr(engine, "model_adapter", None)
    if adapter is not None:
        retrieval_mode = adapter.retrieval_mode
        degraded_reason = adapter.degraded_reason
    else:  # pragma: no cover - 仅用于鸭子类型的假引擎（测试替身）
        retrieval_mode = "hybrid" if has_api_key() else "keyword_only"
        degraded_reason = "" if has_api_key() else "未配置 DASHSCOPE_API_KEY，自动降级为本地 BM25/关键词检索（零密钥可用）"
    embedding_provider_available = retrieval_mode == "hybrid"

    state = load_bootstrap_state(base_dir)
    resolved_user_id = user_id or (state or {}).get("user_id") or DEFAULT_USER_ID
    resolved_space_id = space_id or (state or {}).get("space_id") or DEFAULT_SPACE_ID
    reused_state = False
    created_token = False

    raw_token = token
    if raw_token is None and state is not None:
        raw_token = state["raw_token"]
        reused_state = True
    if raw_token is None:
        if not allow_auto_create:
            raise RuntimeError(
                "BOOTSTRAP_REQUIRED: OmniLore has no space/token yet. "
                "Run `omnilore bootstrap` first, or set OMNILORE_BOOTSTRAP_TOKEN."
            )
        raw_token = generate_raw_token()
        created_token = True

    created_space = resolved_space_id not in engine.auth._spaces
    _register_bootstrap_identity(
        engine,
        space_id=resolved_space_id,
        user_id=resolved_user_id,
        raw_token=raw_token,
    )

    # Always (re)write the state file so explicit tokens survive restarts too.
    save_bootstrap_state(
        base_dir,
        space_id=resolved_space_id,
        user_id=resolved_user_id,
        raw_token=raw_token,
    )

    # Fail loudly and early if the identity we just built cannot authorize.
    engine.auth.resolve_scope(raw_token)

    if created_space:
        log(f"[bootstrap] 已创建默认空间 {resolved_space_id}")
    if created_token:
        log(f"[bootstrap] 已签发 owner token（{generate_token_hint(raw_token)}）")
    if degraded_reason:
        log(f"[bootstrap] {degraded_reason}")

    return BootstrapResult(
        data_dir=base_dir,
        space_id=resolved_space_id,
        user_id=resolved_user_id,
        raw_token=raw_token,
        created_space=created_space,
        created_token=created_token,
        reused_state=reused_state,
        state_path=bootstrap_state_path(base_dir),
        embedding_provider_available=embedding_provider_available,
        retrieval_mode=retrieval_mode,
        degraded_reason=degraded_reason,
    )


def generate_token_hint(raw_token: str) -> str:
    """Return a non-secret display form of a token, e.g. `tok_ab…xy`."""
    if len(raw_token) <= 12:
        return raw_token[:2] + "…"
    return f"{raw_token[:4]}…{raw_token[-4:]}"


def render_mcp_config_snippet(result: BootstrapResult) -> str:
    """Return the MCP client config block for `result` as a plain dict (valid JSON)."""
    return "\n".join(
        [
            "{",
            '  "mcpServers": {',
            '    "omnilore": {',
            '      "command": "omnilore-mcp",',
            '      "env": {',
            f'        "OMNILORE_DATA_DIR": "{result.data_dir.as_posix()}",',
            f'        "OMNILORE_BOOTSTRAP_TOKEN": "{result.raw_token}",',
            '        "DASHSCOPE_API_KEY": ""',
            "      }",
            "    }",
            "  }",
            "}",
        ]
    )
