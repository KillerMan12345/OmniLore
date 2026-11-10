"""
Answer-Synthesis Citation Grounding Guard
=========================================
Turns citation verification from a passive, opt-in tool into a default
post-processing step of answer synthesis.

Design boundaries (do not widen without updating docs/FEATURE_STATUS.md):
  * Evidence is the **un-folded** retrieved chunks of THIS request only. It is
    deliberately not the full corpus: a claim that is true somewhere in the
    vault but not in the evidence actually shown to the model is still an
    ungrounded answer.
  * This module never changes `retrieval_mode` / `degraded_reason` /
    `synthesis_degraded` / `has_sufficient_evidence`. Retrieval degradation and
    synthesis degradation stay orthogonal (see engine.py search_knowledge).
  * It reports; it does not silently rewrite or drop the answer. Callers decide
    what to do with `unverified_citations_detected`.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from .citation import normalize_text, verify_citation_content

MAX_CLAIMS = 8
MAX_CLAIM_CHARS = 300
MIN_CLAIM_CHARS = 4

# Sentence terminators for both CJK and Latin prose.
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?；;\n])")
# [ref: doc_xxx] / [citation: doc_xxx] style markers.
_REF_MARKER = re.compile(r"\[(?:ref|citation|cite)\s*:\s*([^\]]+)\]", re.I)
# Quoted spans: CJK brackets, curly quotes, straight quotes.
_QUOTED_SPAN = re.compile(r"[「“\"']([^「」“”\"']{6,})[」”\"']")


def _clip(text: str) -> str:
    text = (text or "").strip()
    return text[:MAX_CLAIM_CHARS]


def _collect_from_citations(citations: Any) -> List[Dict[str, Any]]:
    """Normalize the heterogeneous `citations` payload into claim dicts."""
    items: List[Any] = []
    if isinstance(citations, dict):
        for value in citations.values():
            if isinstance(value, (list, tuple)):
                items.extend(value)
            elif isinstance(value, (str, dict)):
                items.append(value)
    elif isinstance(citations, (list, tuple)):
        items.extend(citations)
    elif isinstance(citations, str):
        items.append(citations)

    claims: List[Dict[str, Any]] = []
    for item in items:
        if isinstance(item, str):
            text = item
            declared_doc_id: Optional[str] = None
            quote: Optional[str] = None
        elif isinstance(item, dict):
            text = str(
                item.get("quote")
                or item.get("verbatim_quote")
                or item.get("text")
                or item.get("snippet")
                or item.get("claim")
                or ""
            )
            raw_doc_id = item.get("doc_id")
            declared_doc_id = str(raw_doc_id) if raw_doc_id else None
            quote = item.get("verbatim_quote")
            quote = str(quote) if quote else None
        else:
            continue
        text = _clip(text)
        if len(text) < MIN_CLAIM_CHARS:
            continue
        claims.append({
            "claim_text": text,
            "source": "citations",
            "declared_doc_id": declared_doc_id,
            "verbatim_quote": quote,
        })
    return claims


def _collect_from_answer(answer: str) -> List[Dict[str, Any]]:
    """Extract explicit citation markers and quoted spans from the answer text."""
    claims: List[Dict[str, Any]] = []
    if not answer:
        return claims

    for marker in _REF_MARKER.finditer(answer):
        before = answer[: marker.start()]
        parts = _SENTENCE_SPLIT.split(before)
        sentence = parts[-1].strip() if parts else ""
        if len(sentence) < MIN_CLAIM_CHARS:
            continue
        claims.append({
            "claim_text": _clip(sentence),
            "source": "answer_marker",
            "declared_doc_id": marker.group(1).strip(),
            "verbatim_quote": None,
        })

    for match in _QUOTED_SPAN.finditer(answer):
        claims.append({
            "claim_text": _clip(match.group(1)),
            "source": "answer_quote",
            "declared_doc_id": None,
            "verbatim_quote": None,
        })

    return claims


def _sentence_fallback(answer: str) -> List[Dict[str, Any]]:
    """Last resort when the model emitted NO citations and NO explicit markers.

    Only then do we inspect prose sentence by sentence. Otherwise the boilerplate
    prefix of a deterministic answer ("根据《…》（版本 …）：") would be treated
    as a standalone claim and dilute the similarity into a false UNVERIFIED.
    """
    claims: List[Dict[str, Any]] = []
    for part in _SENTENCE_SPLIT.split(answer):
        sentence = _clip(part)
        if len(sentence) >= MIN_CLAIM_CHARS:
            claims.append({
                "claim_text": sentence,
                "source": "answer_sentence",
                "declared_doc_id": None,
                "verbatim_quote": None,
            })
    return claims


def extract_answer_claims(answer: str, citations: Any) -> List[Dict[str, Any]]:
    """Deduplicate and cap the candidate claims extracted from a synthesis result."""
    collected = _collect_from_citations(citations) + _collect_from_answer(answer)
    if not collected:
        collected = _sentence_fallback(answer)
    seen: set[str] = set()
    out: List[Dict[str, Any]] = []
    for claim in collected:
        key = normalize_text(claim["claim_text"])
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(claim)
        if len(out) >= MAX_CLAIMS:
            break
    return out


def _best_evidence(
    claim: Dict[str, Any],
    evidence_chunks: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Pick the highest-scoring evidence chunk for one claim."""
    best: Dict[str, Any] = {
        "verification_status": "UNVERIFIED_OR_MISQUOTED",
        "is_grounded": False,
        "similarity_score": 0.0,
        "matched_snippet": None,
        "line_number": None,
        "analysis": "本次检索未返回任何可比对的证据块。",
        "matched_doc_id": None,
        "matched_chunk_id": None,
        "evidence_scope": "retrieved_chunks",
    }
    for chunk in evidence_chunks:
        text = str(chunk.get("chunk_text") or "")
        report = verify_citation_content(
            claim["claim_text"],
            text,
            verbatim_quote=claim.get("verbatim_quote"),
        )
        if report["similarity_score"] > best["similarity_score"]:
            best.update(report)
            best["matched_doc_id"] = chunk.get("doc_id")
            best["matched_chunk_id"] = chunk.get("chunk_id")
    return best


def build_citation_verifications(
    answer: str,
    citations: Any,
    evidence_chunks: List[Dict[str, Any]],
    *,
    enabled: bool = True,
) -> Dict[str, Any]:
    """Ground every extracted claim against the un-folded retrieved chunks.

    Returns a dict ready to be merged into `search_knowledge`'s response:
    `citation_verifications`, `unverified_citations_detected`, `citation_grounding`.
    """
    if not enabled:
        return {
            "citation_verifications": [],
            "unverified_citations_detected": False,
            "citation_grounding": {
                "enabled": False,
                "total_claims": 0,
                "verified": 0,
                "partially_verified": 0,
                "unverified": 0,
                "evidence_chunks": len(evidence_chunks),
                "all_grounded": True,
                "note": "调用方显式关闭了答案引用巡检，本响应未做事实核验。",
            },
        }

    claims = extract_answer_claims(answer, citations)
    reports: List[Dict[str, Any]] = []
    counts = {"VERIFIED": 0, "PARTIALLY_VERIFIED": 0, "UNVERIFIED_OR_MISQUOTED": 0}

    for claim in claims:
        evidence = _best_evidence(claim, evidence_chunks)
        declared = claim.get("declared_doc_id")
        matched = evidence.get("matched_doc_id")
        # A claim that names a source document but only matches a different one is
        # a misattribution, which is as dangerous as an invented quote.
        doc_id_mismatch = bool(declared and matched and declared != matched)
        status = evidence["verification_status"]
        if doc_id_mismatch and status != "UNVERIFIED_OR_MISQUOTED":
            status = "UNVERIFIED_OR_MISQUOTED"
            evidence["analysis"] = (
                f"声明引用 {declared}，但最佳匹配落在 {matched}；疑似张冠李戴的错误引用。"
            )
        counts[status] += 1
        reports.append({
            "claim_text": claim["claim_text"],
            "source": claim["source"],
            "declared_doc_id": declared,
            "matched_doc_id": matched,
            "matched_chunk_id": evidence["matched_chunk_id"],
            "doc_id_mismatch": doc_id_mismatch,
            "verification_status": status,
            "is_grounded": status in ("VERIFIED", "PARTIALLY_VERIFIED"),
            "similarity_score": evidence["similarity_score"],
            "matched_snippet": evidence["matched_snippet"],
            "line_number": evidence["line_number"],
            "analysis": evidence["analysis"],
            "evidence_scope": "retrieved_chunks",
        })

    unverified = counts["UNVERIFIED_OR_MISQUOTED"] > 0
    return {
        "citation_verifications": reports,
        "unverified_citations_detected": unverified,
        "citation_grounding": {
            "enabled": True,
            "total_claims": len(reports),
            "verified": counts["VERIFIED"],
            "partially_verified": counts["PARTIALLY_VERIFIED"],
            "unverified": counts["UNVERIFIED_OR_MISQUOTED"],
            "evidence_chunks": len(evidence_chunks),
            "all_grounded": not unverified,
            "note": (
                "仅比对本次检索返回的未折叠证据块；不覆盖整个知识库，不代表真实 PostgreSQL 数据面。"
                if not unverified
                else "检测到未被证据原文支撑的引用，请复核答案后再对外使用。"
            ),
        },
    }
