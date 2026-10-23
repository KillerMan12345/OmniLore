"""
Shared Filesystem Path Resolution
=================================
Every OmniLore entry point (the `omnilore` CLI, `python -m omnilore`, and the
MCP stdio server) resolves the data directory through **one** function so the three
can never drift apart.

Why this module exists
----------------------
`omnilore bootstrap` runs in one process and prints a token; the MCP server is a
*separate* process that must be able to read that same token back. If the two
processes resolve `OMNILORE_DATA_DIR` differently — for example `~/OmniLore_Data`
expanded in one and treated as a literal `~` directory in the other, or a relative
path resolved against different working directories — they silently end up with two
*different* data directories and two different tokens. No error is raised; the user
just holds a token that never works.

Fixing it once here (rather than duplicating `.expanduser().resolve()` calls) is what
keeps the two sides in sync; QA bug P1-1 was caused by exactly that duplication.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


DEFAULT_DATA_DIR_ENV_VAR = "OMNILORE_DATA_DIR"
DEFAULT_DATA_DIR = "./OmniLore_Data"


def resolve_data_dir(
    raw_value: Optional[str] = None,
    *,
    env: Optional[dict] = None,
    base: Optional[Path] = None,
) -> Path:
    """Resolve the OmniLore data directory to an absolute, normalized path.

    Expands `~` / `~user` and resolves relative paths against the current working
    directory, so that two processes invoking this function with the same
    environment always agree on the same absolute directory.

    Args:
        raw_value: Explicit path to resolve. When omitted, the value of the
            `OMNILORE_DATA_DIR` environment variable is used, falling back to
            `./OmniLore_Data`.
        env: Optional environment mapping override (mainly for tests).
        base: Optional base directory used to resolve relative paths (mainly for
            tests); defaults to the process working directory.

    Returns:
        An absolute `Path` with `..` / `.` segments collapsed.

    Raises:
        TypeError: If `raw_value` is not a string and the environment value is unset.
    """
    source_env = os.environ if env is None else env
    value = raw_value if raw_value is not None else source_env.get(DEFAULT_DATA_DIR_ENV_VAR, DEFAULT_DATA_DIR)
    if not isinstance(value, str):
        raise TypeError(f"{DEFAULT_DATA_DIR_ENV_VAR} must be a string path, got {type(value).__name__}")

    path = Path(value).expanduser()
    if not path.is_absolute() and base is not None:
        path = base / path
    return path.resolve()
