"""
Citation Grounding & Anti-Hallucination Verification Module
===========================================================
Verifies claims and verbatim citations against authentic CAS raw assets or
compiled Wiki Markdown to eliminate hallucinated references.
Inspired by memobase's strict citation verification and swarmvault's evidence class discipline.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple


def normalize_text(text: str) -> str:
    """Normalize text by lowercasing and stripping non-alphanumeric punctuation and whitespaces."""
    if not text:
        return ""
    # Strip whitespace, common markdown syntax (#, *, _, `, >, [), and punctuation (ASCII + CJK)
    cleaned = re.sub(r"[\s\r\n\t#\*_`>\[\]\(\)\-—.,!?:;，。！？：；、“”‘’（）《》【】]+", "", text.lower())
    return cleaned


def extract_best_matching_snippet(
    needle: str,
    haystack: str,
    window_padding: int = 50,
) -> Tuple[Optional[str], Optional[int], float]:
    """
    Find best matching snippet of needle inside haystack.
    Returns (matched_snippet, line_number, similarity_score).
    """
    if not needle or not haystack:
        return None, None, 0.0

    # 1. Exact match attempt
    pos = haystack.find(needle)
    if pos != -1:
        line_num = haystack[:pos].count("\n") + 1
        start = max(0, pos - window_padding)
        end = min(len(haystack), pos + len(needle) + window_padding)
        snippet = haystack[start:end].strip()
        return snippet, line_num, 1.0

    # 2. Normalized character stream matching
    norm_needle = normalize_text(needle)
    if not norm_needle:
        return None, None, 0.0

    # Find line-by-line where normalized overlap is highest
    lines = haystack.splitlines()
    best_score = 0.0
    best_line = None
    best_snippet = None

    # Slide across lines with a 3-line window
    for idx in range(len(lines)):
        window_lines = lines[idx : min(len(lines), idx + 3)]
        window_text = " ".join(window_lines)
        norm_window = normalize_text(window_text)

        if not norm_window:
            continue

        if norm_needle in norm_window:
            return window_text.strip(), idx + 1, 0.95

        # Character bi-gram overlap
        needle_bigrams = set(norm_needle[i : i + 2] for i in range(len(norm_needle) - 1)) or {norm_needle}
        win_bigrams = set(norm_window[i : i + 2] for i in range(len(norm_window) - 1)) or {norm_window}

        intersection = len(needle_bigrams & win_bigrams)
        union = len(needle_bigrams | win_bigrams)
        score = intersection / union if union > 0 else 0.0

        if score > best_score:
            best_score = score
            best_line = idx + 1
            best_snippet = window_text.strip()

    return best_snippet, best_line, round(best_score, 4)


def verify_citation_content(
    claim_text: str,
    source_content: str,
    verbatim_quote: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Verifies claim or verbatim quote against authentic source content.
    Returns:
    - verification_status: "VERIFIED" | "PARTIALLY_VERIFIED" | "UNVERIFIED_OR_MISQUOTED"
    - similarity_score: float (0.0 to 1.0)
    - matched_snippet: str | None
    - line_number: int | None
    - analysis: diagnostic note
    """
    target = verbatim_quote if verbatim_quote else claim_text
    snippet, line_num, score = extract_best_matching_snippet(target, source_content)

    if score >= 0.85:
        status = "VERIFIED"
        analysis = "引述事实与底座文档高度一致，通过防幻觉真实性核验。"
    elif score >= 0.45:
        status = "PARTIALLY_VERIFIED"
        analysis = "引述存在局部改写或词汇差异，建议复核原文确切表述。"
    else:
        status = "UNVERIFIED_OR_MISQUOTED"
        analysis = "在声明的源文档中未找到对应事实或语句，疑似大模型幻觉生成。"

    return {
        "verification_status": status,
        "is_grounded": status in ("VERIFIED", "PARTIALLY_VERIFIED"),
        "similarity_score": score,
        "line_number": line_num,
        "matched_snippet": snippet,
        "analysis": analysis,
    }
