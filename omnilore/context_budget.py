"""
Context Budget Governance Module
================================
Provides token budget estimation, equal-share allocation, and structural folding
(folding code blocks, long tables, and oversized text bodies while preserving anchors)
to prevent dumping excessive context into downstream agent reasoning loops.
Inspired by Hy-Memory's quota governance and neurostack's tiered token discipline.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple


def estimate_tokens(text: str) -> int:
    """
    Fast, dependency-free heuristic token estimator.
    Approximates token consumption across mixed CJK and Latin text:
    - CJK characters: ~0.7 tokens per character (roughly 1.4 chars / token).
    - Latin words / numbers / punctuation: ~0.3 tokens per character (~3.5-4 chars / token).
    """
    if not text:
        return 0

    cjk_count = 0
    non_cjk_count = 0

    for ch in text:
        code = ord(ch)
        # CJK Unified Ideographs & common CJK ranges
        if (0x4E00 <= code <= 0x9FFF) or (0x3400 <= code <= 0x4DBF) or (0x20000 <= code <= 0x2A6DF):
            cjk_count += 1
        else:
            non_cjk_count += 1

    estimated = int(cjk_count * 0.75 + non_cjk_count * 0.28)
    return max(1, estimated)


def fold_chunk_content(
    chunk_text: str,
    chunk_id: str,
    target_tokens: int,
) -> Tuple[str, bool]:
    """
    Fold a single chunk's text to fit within target_tokens.
    Priority of folding:
    1. Fold large code blocks (> 4 lines or > 80 tokens) into pointer summaries.
    2. Fold excessive body text while preserving the introductory and topic sentences.
    Returns (governed_text, is_truncated).
    """
    current_tokens = estimate_tokens(chunk_text)
    if current_tokens <= target_tokens:
        return chunk_text, False

    modified_text = chunk_text
    is_truncated = False

    # Step 1: Fold code blocks ```lang ... ```
    code_block_pattern = re.compile(r"```([a-zA-Z0-9_\-\+]*)\n([\s\S]*?)```")

    def _replace_code_block(match: re.Match) -> str:
        nonlocal is_truncated
        lang = match.group(1) or "code"
        code_body = match.group(2)
        lines = code_body.strip().splitlines()
        if len(lines) > 3 or estimate_tokens(code_body) > 60:
            is_truncated = True
            first_preview = lines[0].strip()[:60] if lines else ""
            return (
                f"\n```\n[代码块已折叠: {lang}, 共 {len(lines)} 行. 首行: {first_preview}... "
                f"完整细节请调用 get_asset_chunk(chunk_id=\"{chunk_id}\")]\n```\n"
            )
        return match.group(0)

    folded_code = code_block_pattern.sub(_replace_code_block, modified_text)
    if estimate_tokens(folded_code) <= target_tokens:
        return folded_code, is_truncated

    # Step 2: If still exceeding budget, truncate body text with clear indication
    # Calculate approximate target character length
    # Assuming average ~1.8 chars per token for safe estimation
    char_budget = max(120, int(target_tokens * 1.8))
    if len(folded_code) > char_budget:
        truncated_chars = len(folded_code) - char_budget
        head = folded_code[:char_budget]
        folded_text = (
            f"{head}\n...[已折叠剩余 {truncated_chars} 字符。"
            f"如需完整段落请调用 get_asset_chunk(chunk_id=\"{chunk_id}\")]"
        )
        return folded_text, True

    return folded_code, is_truncated


def govern_search_results(
    results: List[Dict[str, Any]],
    token_budget: Optional[int] = 1500,
) -> Dict[str, Any]:
    """
    Apply token budget governance across search results.
    - If token_budget is None or <= 0: leaves results intact, reports unconstrained.
    - Otherwise: allocates budget fairly across candidate chunks, folds oversized parts,
      preserves structural anchors, and appends governance telemetry.
    """
    if not results:
        return {
            "results": [],
            "budget_governance": {
                "applied": False,
                "token_budget": token_budget,
                "estimated_tokens_before": 0,
                "estimated_tokens_after": 0,
                "truncated_count": 0,
            },
        }

    raw_tokens_before = sum(estimate_tokens(r.get("chunk_text", "")) for r in results)

    if token_budget is None or token_budget <= 0:
        return {
            "results": results,
            "budget_governance": {
                "applied": False,
                "token_budget": token_budget,
                "estimated_tokens_before": raw_tokens_before,
                "estimated_tokens_after": raw_tokens_before,
                "truncated_count": 0,
            },
        }

    count = len(results)
    # Reserve roughly 50 tokens per item for metadata fields (doc_id, title, page, etc.)
    metadata_overhead = count * 40
    effective_budget = max(count * 80, token_budget - metadata_overhead)
    tokens_per_chunk = max(100, effective_budget // count)

    governed_chunks: List[Dict[str, Any]] = []
    truncated_count = 0

    for ch in results:
        new_ch = dict(ch)
        orig_text = ch.get("chunk_text", "")
        chunk_id = ch.get("chunk_id", "")

        folded_text, is_trunc = fold_chunk_content(orig_text, chunk_id, tokens_per_chunk)
        new_ch["chunk_text"] = folded_text
        new_ch["truncated"] = is_trunc
        new_ch["original_char_length"] = len(orig_text)

        if is_trunc:
            truncated_count += 1

        governed_chunks.append(new_ch)

    tokens_after = sum(estimate_tokens(r["chunk_text"]) for r in governed_chunks)

    return {
        "results": governed_chunks,
        "budget_governance": {
            "applied": True,
            "token_budget": token_budget,
            "tokens_per_chunk_target": tokens_per_chunk,
            "estimated_tokens_before": raw_tokens_before,
            "estimated_tokens_after": tokens_after,
            "truncated_count": truncated_count,
        },
    }
