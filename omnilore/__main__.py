"""
`python -m omnilore` entry point.

Mirrors the `omnilore` console script declared in `pyproject.toml`
(`omnilore = "omnilore.cli:main"`), so both invocation styles behave
identically. See `omnilore/cli.py` for the actual sub-commands.
"""

from __future__ import annotations

from .cli import build_parser, main

__all__ = ["build_parser", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
