"""
Canonical Text I/O (Cross-Platform LF Guard)
============================================
OmniLore persists exactly ONE canonical document representation on disk:
**UTF-8 with ``\\n`` (LF) line endings**.

Why this module exists
----------------------
`Path.write_text()` opens the file with Python's default newline handling, so on
Windows every ``\\n`` is translated into ``\\r\\n`` on the way to disk. Meanwhile
`documents.file_hash` is computed with::

    hashlib.sha256(markdown_text.encode("utf-8")).hexdigest()

…over the *memory* representation, which uses LF. The result is that on Windows
"bytes on disk" != "bytes we hashed", and every reconciliation pass
(`OperationsManager.reconcile_consistency`, Section 12.3) reports
``hash_mismatches`` → ``is_consistent=False``. The same class of bug silently
corrupts the SHA-256 verified restore drill and the Obsidian write-back loop
guard.

The fix: **all** markdown / dashboard / manifest writes go through
:func:`write_text_canonical` and **all** markdown reads through
:func:`read_text_canonical`, so the hashed bytes and the persisted bytes are the
same by construction, on every platform.

See ``docs/DECISIONS.md`` (A-3) and ``CHANGELOG.md`` for the full rationale.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Set

# Token class used consistently across storage hashing and lexical retrieval.
_TOKEN_PATTERN = re.compile(r"[\w\u4e00-\u9fa5]+")


def lexical_tokens(text: str, *, bigram_cjk: bool = True) -> Set[str]:
    """Tokenize `text` for BM25-style lexical retrieval.

    Uses exactly the same vocabulary strategy as the deterministic local fallback
    embedder in `ModelAdapter.embed_texts`: word runs plus sliding **CJK bigrams**.
    Without bigrams a Chinese query such as ``分布式锁`` would never match the
    token ``分布式锁与租约续期规范`` and keyword-only recall would collapse to zero.

    Args:
        text: Arbitrary query or document text.
        bigram_cjk: Emit 2-character sliding windows for CJK-containing tokens.

    Returns:
        A set of lowercase tokens.
    """
    lowered = text.lower() if text else ""
    base_tokens = [tok for tok in _TOKEN_PATTERN.findall(lowered) if tok]
    tokens: Set[str] = set(base_tokens)
    if bigram_cjk:
        for tok in base_tokens:
            if any("\u4e00" <= ch <= "\u9fa5" for ch in tok) and len(tok) > 1:
                tokens.update(tok[i : i + 2] for i in range(len(tok) - 1))
    return tokens


def canonicalize_newlines(text: str) -> str:
    """Normalize CRLF / CR line endings to the canonical LF form used everywhere.

    Args:
        text: Arbitrary text possibly containing CRLF or lone CR line endings.

    Returns:
        The same text with every line terminator normalized to ``\\n``.
    """
    if not text:
        return text
    return text.replace("\r\n", "\n").replace("\r", "\n")


def canonical_text_bytes(text: str) -> bytes:
    """Encode `text` into the exact byte sequence OmniLore stores and hashes.

    Args:
        text: Canonical LF text (any line endings are normalized first).

    Returns:
        UTF-8 encoded bytes ready to be written to disk byte-for-byte.
    """
    return canonicalize_newlines(text).encode("utf-8")


def canonical_file_hash(text: str) -> str:
    """Compute the SHA-256 digest OmniLore stores in `documents.file_hash`.

    Args:
        text: The canonical LF markdown body that will be persisted.

    Returns:
        Lowercase hex SHA-256 digest of the canonical UTF-8 bytes.
    """
    import hashlib

    return hashlib.sha256(canonical_text_bytes(text)).hexdigest()


def write_text_canonical(path: Path, text: str) -> None:
    """Write `text` to `path` as UTF-8/LF bytes without platform newline translation.

    Args:
        path: Destination file; parent directories are created when missing.
        text: Text content; any CRLF/CR endings are normalized to LF first.

    Returns:
        None. The file is always overwritten with the canonical byte sequence.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_text_bytes(text))


def read_text_canonical(path: Path) -> str:
    """Read `path` and normalize any CRLF/CR line endings back to canonical LF text.

    Args:
        path: Source file to read.

    Returns:
        Decoded UTF-8 text with LF-only line terminators.
    """
    return canonicalize_newlines(Path(path).read_bytes().decode("utf-8"))
