"""
Single Source of Truth for Version Identifiers
==============================================
Kept in its own tiny module so that submodules (notably `omnilore.cli` and
`omnilore.mcp_stdio_server`) can read the version **without importing the
package root**. Importing `omnilore` from within a submodule makes `python -m
omnilore.mcp_stdio_server` emit:

    RuntimeWarning: 'omnilore.mcp_stdio_server' found in sys.modules after
    import of package 'omnilore', but prior to execution ...

because runpy imports the parent package first, and the package eagerly imports
the submodule back. See QA finding P2-9.

Version duality (documented in `docs/DECISIONS.md` D2 and `CHANGELOG.md`):
  * `__version__`          — PEP 440 machine-readable (`0.3.0a0`), required by pip/setuptools.
  * `__version_display__`  — advertised release name (`v0.3.0-alpha`).
  * `__version_label__`    — advertised name without the `v` prefix, used by MCP `serverInfo`.
"""

from __future__ import annotations

__version__ = "0.3.0a0"
__version_display__ = "v0.3.0-alpha"
__version_label__ = "0.3.0-alpha"

__all__ = ["__version__", "__version_display__", "__version_label__"]
