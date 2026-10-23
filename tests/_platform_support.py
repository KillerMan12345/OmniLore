"""
Test-only platform capability probes (W0 / A-3 prerequisites)
=============================================================
Some acceptance scenarios (notably **AC-S05 symlink defence**) can only be
exercised when the *host operating system / sandbox* is actually able to
materialise the hostile artifact. Windows without `SeCreateSymbolicLinkPrivilege`
(and several CI sandboxes) cannot create real symlinks at all — `os.symlink()`
there either raises `OSError` or silently does nothing.

When the host cannot create a symlink, the corresponding assertion is **SKIPPED
with an explicit reason**. This is a *host capability limitation*, **NOT** a
OmniLore platform defect:

* On every POSIX CI image (ubuntu / macOS runners) the full check runs unmodified.
* The shipped guard itself (`SecureVaultStorage._verify_safe_subpath` and
  `_reject_symlinks_in_chain`) is unchanged and still rejects any symlink that
  the OS *is* able to produce.
* We deliberately DO NOT use a silent `try/except` that would mask a real bug:
  the probe *verifies* the symlink really exists afterwards, and any unexpected
  exception is re-raised.

Usage::

    from _platform_support import ensure_symlink_or_skip

    ensure_symlink_or_skip(self, target=secret_outside, link_path=symlink_path)
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
from typing import Optional, Tuple, Union
import unittest


_SYMLINK_PROBE_CACHE: Optional[Tuple[bool, str]] = None


def probe_symlink_support(probe_dir: Union[str, Path, None] = None) -> Tuple[bool, str]:
    """Return ``(supported, reason)`` describing whether real symlinks can be created.

    Args:
        probe_dir: Directory used for the throwaway probe. A temporary directory
            is created automatically when omitted.

    Returns:
        A tuple ``(supported, reason)``. `reason` explains either the failure or
        confirms support, so it can be embedded verbatim into a skip message.
    """
    import tempfile

    tmp_ctx: Optional[tempfile.TemporaryDirectory] = None
    probe_path: Path
    if probe_dir is None:
        tmp_ctx = tempfile.TemporaryDirectory()
        probe_path = Path(tmp_ctx.name)
    else:
        probe_path = Path(probe_dir)

    try:
        # Ensure the probe directory really exists (it may be a vault subdir).
        probe_path.mkdir(parents=True, exist_ok=True)
        target_file = probe_path / "_omnilore_symlink_probe_target.tmp"
        link_file = probe_path / "_omnilore_symlink_probe_link.tmp"
        result: Tuple[bool, str]
        try:
            target_file.write_bytes(b"probe")
            os.symlink(target_file, link_file)
        except OSError as exc:
            # Expected on unprivileged Windows hosts: missing symlink privilege.
            result = (
                False,
                f"宿主环境禁止创建符号链接（{type(exc).__name__}: {getattr(exc, 'winerror', exc)}）。"
                "此为宿主能力限制，非 OmniLore 平台缺陷；POSIX CI 上该断言完整执行。",
            )
        else:
            if os.path.islink(str(link_file)):
                result = (True, "宿主环境支持创建符号链接。")
            else:
                result = (
                    False,
                    "宿主环境不支持创建符号链接：os.symlink() 未报错但实际未生成链接 "
                    "（Windows 缺少 SeCreateSymbolicLinkPrivilege 或运行于受限沙箱）。"
                    "此为宿主能力限制，非 OmniLore 平台缺陷；POSIX CI 上该断言完整执行。",
                )
        # Always remove probe artifacts so they can never leak into a vault scan.
        _silent_unlink(link_file)
        _silent_unlink(target_file)
        return result
    except Exception as exc:  # pragma: no cover - unexpected, must never be swallowed
        raise RuntimeError(f"Unexpected probe failure while testing symlink support: {exc!r}") from exc
    finally:
        if tmp_ctx is not None:
            tmp_ctx.cleanup()


def _silent_unlink(path: Path) -> None:
    """Remove a probe artifact when possible; probe cleanup failures are irrelevant."""
    try:
        if os.path.islink(str(path)) or path.exists():
            path.unlink()
    except OSError:
        pass


def symlink_supported(probe_dir: Union[str, Path, None] = None) -> bool:
    """Cached boolean form of :func:`probe_symlink_support`."""
    global _SYMLINK_PROBE_CACHE
    if _SYMLINK_PROBE_CACHE is None:
        _SYMLINK_PROBE_CACHE = probe_symlink_support(probe_dir)
    return _SYMLINK_PROBE_CACHE[0]


def symlink_skip_reason(probe_dir: Union[str, Path, None] = None) -> str:
    """Cached human-readable reason describing why symlinks cannot be created."""
    global _SYMLINK_PROBE_CACHE
    if _SYMLINK_PROBE_CACHE is None:
        _SYMLINK_PROBE_CACHE = probe_symlink_support(probe_dir)
    return _SYMLINK_PROBE_CACHE[1]


def ensure_symlink_or_skip(testcase: unittest.TestCase, target: Path, link_path: Path) -> None:
    """Create a real symlink, or skip the calling test with an explicit reason.

    Args:
        testcase: The running `unittest.TestCase` (used to emit `skipTest`).
        target: Existing filesystem path the symlink should point at.
        link_path: Desired symlink location.
    """
    if not create_symlink(target, link_path):
        testcase.skipTest(f"[宿主能力限制] {symlink_skip_reason(link_path.parent)}")


def create_symlink(target: Path, link_path: Path) -> bool:
    """Attempt to create a real symlink; return True only if one really exists.

    Args:
        target: Existing target path.
        link_path: Desired symlink location.

    Returns:
        True when `link_path` is verifiably a symlink afterwards.
    """
    if not probe_symlink_support(link_path.parent)[0]:
        return False
    os.symlink(target, link_path)
    return os.path.islink(str(link_path))


JUNCTION_LINK_NAME = "evil_dir_junction"


def create_junction(target_dir: Path, link_path: Path) -> bool:
    """Create a Windows directory junction (`mklink /J`), which needs no privilege.

    Junctions are not symlinks (`os.path.islink()` is False for them), but
    `Path.resolve()` follows them — so a junction whose target lies outside the
    vault is still rejected by `SecureVaultStorage._verify_safe_subpath` through the
    "escapes expected root directory boundary" check. That makes it a valid — and
    privilege-free — stand-in for exercising the traversal defence on Windows.

    Args:
        target_dir: Existing **directory** to point the junction at.
        link_path: Desired junction location (must not exist yet).

    Returns:
        True when the junction was created and really resolves to `target_dir`.
    """
    if os.name != "nt" or not target_dir.is_dir():
        return False
    try:
        proc = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link_path), str(target_dir)],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if proc.returncode != 0:
        return False
    return link_path.is_dir() and link_path.resolve() == target_dir.resolve()


@dataclass(frozen=True)
class HostileLinkSpec:
    """What :func:`establish_hostile_link` built, so the test knows what to assert.

    Attributes:
        kind: `"symlink"` or `"junction"`.
        rel_path: Vault-relative path that must be **rejected** by the file gateway.
        link_path: The artifact created (useful for diagnostics/cleanup).
    """

    kind: str
    rel_path: str
    link_path: Path


def establish_hostile_link(
    testcase: unittest.TestCase,
    *,
    wiki_dir: Path,
    space_id: str,
    outside_file: Path,
) -> HostileLinkSpec:
    """Create a privileged-free hostile link and report the path to request.

    Tries, in order:
      1. A real **file symlink** (POSIX, and Windows with developer mode/admin).
      2. A **directory junction** via `mklink /J` (Windows; no privilege needed).
         In this mode the returned path points *through* the junction to a file
         outside the vault.
      3. Otherwise `skipTest` with an explicit, auditable reason.

    Args:
        testcase: Running test case, used to emit `skipTest` when neither works.
        wiki_dir: The space's `wiki/` directory inside the vault.
        space_id: Space id, used to build the vault-relative request path.
        outside_file: A file **outside** the vault that must never be readable.

    Returns:
        A :class:`HostileLinkSpec` describing what was created.
    """
    link_file = wiki_dir / "evil_link.md"
    if create_symlink(outside_file, link_file):
        return HostileLinkSpec("symlink", f"{space_id}/wiki/{link_file.name}", link_file)

    junction_dir = wiki_dir / JUNCTION_LINK_NAME
    outside_dir = outside_file.parent
    if create_junction(outside_dir, junction_dir):
        return HostileLinkSpec(
            "junction",
            f"{space_id}/wiki/{junction_dir.name}/{outside_file.name}",
            junction_dir,
        )

    testcase.skipTest(
        "[宿主能力限制] 宿主既无法创建符号链接（symlink）也无法创建目录联接（junction），"
        "无法构造路径穿越场景。此为宿主能力限制，非 OmniLore 平台缺陷。"
    )
    raise AssertionError("unreachable: skipTest 未抛出")
