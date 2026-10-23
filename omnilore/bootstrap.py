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
from typing import Any, Callable, Dict, List, Optional
import warnings

from .textio import read_text_canonical, write_text_canonical

# Default identity material used by `omnilore bootstrap`.
DEFAULT_SPACE_ID = "sp_bootstrap_personal"
DEFAULT_USER_ID = "00000000-0000-4000-8000-000000000001"
DEFAULT_SPACE_TYPE = "PERSONAL"
DEFAULT_TTL_SECONDS = 365 * 24 * 3600  # 1 year — deliberately long-lived for local use
DEFAULT_CATEGORIES = ("01_Work", "02_Life", "03_Family", "04_Health_Archive")

BOOTSTRAP_STATE_FILENAME = "bootstrap.json"
SUPPORTED_STATE_VERSION = 1


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
        api_key_present: True when `DASHSCOPE_API_KEY` is set (hybrid retrieval).
        retrieval_mode: `hybrid` when an API key exists, else `keyword_only`.
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
    api_key_present: bool
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
            "api_key_present": self.api_key_present,
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

    api_key_present = has_api_key()
    retrieval_mode = "hybrid" if api_key_present else "keyword_only"
    degraded_reason = (
        ""
        if api_key_present
        else "未配置 DASHSCOPE_API_KEY，自动降级为本地 BM25/关键词检索（零密钥可用）"
    )

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
        api_key_present=api_key_present,
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
