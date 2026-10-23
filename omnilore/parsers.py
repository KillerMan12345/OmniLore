"""
Multi-Format Document Parser, AST Code Symbol Extractor & PII Redactor
======================================================================
Implements:
  - FR-01 & FR-04: PDF (`pdfplumber`/`pypdf`), Image (`Pillow`), and Markdown/Text
    extraction with page numbers, section headings, and Markdown table formatting
  - FR-14: Structured Health Metric Extraction (`health_metrics` candidates with
    value, unit, reference range, measured date, and `NEEDS_CHECK` flagging)
  - FR-15: Sensitive PII / Secret detection and redaction preview for cross-space promotion
  - FR-17: Code Symbol & ADR identifier extraction (`ast` + regex) for code-linked search
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from datetime import date
import io
import re
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class ParsedChunkCandidate:
    chunk_index: int
    heading_path: str
    page_number: int
    text: str
    code_symbols: List[str] = field(default_factory=list)


@dataclass
class ParsedDocumentContent:
    raw_text: str
    page_count: int
    chunks: List[ParsedChunkCandidate]
    code_symbols: List[str]
    outbound_wiki_links: List[str]
    extracted_tables_count: int = 0
    image_metadata: Optional[Dict[str, Any]] = None


# -----------------------------------------------------------------------------
# 1. Code Symbol & ADR Identifier Extractor (FR-17)
# -----------------------------------------------------------------------------

ADR_PATTERN = re.compile(r"\bADR-\d{3,5}\b", re.IGNORECASE)
HTTP_ROUTE_PATTERN = re.compile(r"\b(?:GET|POST|PUT|PATCH|DELETE)\s+(/[a-zA-Z0-9_/\-{}:]+)")
DECL_PATTERN = re.compile(r"\b(?:class|def|interface|struct|func|function)\s+([A-Za-z_][A-Za-z0-9_]{2,64})\b")
BACKTICK_SYMBOL_PATTERN = re.compile(r"`([A-Za-z_][A-Za-z0-9_.]{2,64})`")
WIKI_LINK_PATTERN = re.compile(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")


def extract_code_symbols(text: str) -> List[str]:
    symbols: List[str] = []
    seen = set()

    def _add(sym: str) -> None:
        s = sym.strip()
        if s and s not in seen:
            seen.add(s)
            symbols.append(s)

    # Try Python AST if valid Python snippet
    try:
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                _add(node.name)
    except Exception:
        pass

    for m in ADR_PATTERN.finditer(text):
        _add(m.group(0).upper())
    for m in HTTP_ROUTE_PATTERN.finditer(text):
        _add(m.group(1))
    for m in DECL_PATTERN.finditer(text):
        _add(m.group(1))
    for m in BACKTICK_SYMBOL_PATTERN.finditer(text):
        _add(m.group(1))

    return symbols


def extract_outbound_wiki_links(text: str) -> List[str]:
    links: List[str] = []
    for m in WIKI_LINK_PATTERN.finditer(text):
        target = m.group(1).strip()
        if target and target not in links:
            links.append(target)
    return links


# -----------------------------------------------------------------------------
# 2. Multi-Format File Parser (PDF, Image, Markdown, Code)
# -----------------------------------------------------------------------------

def parse_document_bytes(raw_bytes: bytes, filename: str) -> ParsedDocumentContent:
    lower_name = filename.lower()

    # 2.1 PDF Parsing via pdfplumber / pypdf
    if lower_name.endswith(".pdf") or raw_bytes.startswith(b"%PDF-"):
        return _parse_pdf_bytes(raw_bytes)

    # 2.2 Image Parsing via Pillow
    if lower_name.endswith((".png", ".jpg", ".jpeg", ".webp")):
        return _parse_image_bytes(raw_bytes, filename)

    # 2.3 Text / Markdown / Source Code Parsing
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("UNREADABLE_BINARY: Failed to decode file as UTF-8") from exc

    if not text.strip():
        raise ValueError("EMPTY_DOCUMENT: Document contains no extractable text")

    chunks = split_markdown_into_chunks(text)
    all_symbols = extract_code_symbols(text)
    wiki_links = extract_outbound_wiki_links(text)
    return ParsedDocumentContent(
        raw_text=text,
        page_count=1,
        chunks=chunks,
        code_symbols=all_symbols,
        outbound_wiki_links=wiki_links,
        extracted_tables_count=text.count("|---"),
    )


def _parse_pdf_bytes(raw_bytes: bytes) -> ParsedDocumentContent:
    pages_text: List[str] = []
    chunks: List[ParsedChunkCandidate] = []
    tables_count = 0

    try:
        import pdfplumber  # type: ignore

        with pdfplumber.open(io.BytesIO(raw_bytes)) as pdf:
            for idx, page in enumerate(pdf.pages, start=1):
                p_text = (page.extract_text() or "").strip()
                extracted_tables = page.extract_tables() or []
                for tbl in extracted_tables:
                    if not tbl:
                        continue
                    tables_count += 1
                    md_rows = []
                    for r_i, row in enumerate(tbl):
                        cells = [str(c or "").replace("\n", " ").strip() for c in row]
                        md_rows.append("| " + " | ".join(cells) + " |")
                        if r_i == 0:
                            md_rows.append("| " + " | ".join(["---"] * len(cells)) + " |")
                    p_text = p_text + "\n\n" + "\n".join(md_rows)
                if p_text:
                    pages_text.append(p_text)
                    chunks.append(
                        ParsedChunkCandidate(
                            chunk_index=len(chunks),
                            heading_path=f"Page {idx}",
                            page_number=idx,
                            text=p_text,
                            code_symbols=extract_code_symbols(p_text),
                        )
                    )
    except Exception:
        # Fallback to pypdf
        from pypdf import PdfReader  # type: ignore

        reader = PdfReader(io.BytesIO(raw_bytes))
        for idx, page in enumerate(reader.pages, start=1):
            p_text = (page.extract_text() or "").strip()
            if p_text:
                pages_text.append(p_text)
                chunks.append(
                    ParsedChunkCandidate(
                        chunk_index=len(chunks),
                        heading_path=f"Page {idx}",
                        page_number=idx,
                        text=p_text,
                        code_symbols=extract_code_symbols(p_text),
                    )
                )

    full_text = "\n\n".join(pages_text)
    if not full_text.strip():
        raise ValueError("EMPTY_DOCUMENT: PDF contains no extractable text")

    return ParsedDocumentContent(
        raw_text=full_text,
        page_count=max(1, len(pages_text)),
        chunks=chunks,
        code_symbols=extract_code_symbols(full_text),
        outbound_wiki_links=extract_outbound_wiki_links(full_text),
        extracted_tables_count=tables_count,
    )


def _parse_image_bytes(raw_bytes: bytes, filename: str) -> ParsedDocumentContent:
    from PIL import Image  # type: ignore

    with Image.open(io.BytesIO(raw_bytes)) as img:
        width, height = img.size
        fmt = img.format or "IMAGE"
        info_text = img.info.get("Description") or img.info.get("Comment") or ""

    synthesized_text = (
        f"# 图像资产：{filename}\n\n"
        f"- 图像格式：{fmt}\n"
        f"- 分辨率：{width}x{height}\n"
        f"- 附加说明：{info_text or '待视觉模型或 OCR 进一步核验'}\n"
    )
    chunk = ParsedChunkCandidate(
        chunk_index=0,
        heading_path="Image Metadata",
        page_number=1,
        text=synthesized_text,
        code_symbols=[],
    )
    return ParsedDocumentContent(
        raw_text=synthesized_text,
        page_count=1,
        chunks=[chunk],
        code_symbols=[],
        outbound_wiki_links=[],
        image_metadata={"width": width, "height": height, "format": fmt},
    )


def split_markdown_into_chunks(text: str) -> List[ParsedChunkCandidate]:
    lines = text.splitlines()
    chunks: List[ParsedChunkCandidate] = []
    current_heading = "Root"
    current_lines: List[str] = []

    def _flush() -> None:
        body = "\n".join(current_lines).strip()
        if body:
            chunks.append(
                ParsedChunkCandidate(
                    chunk_index=len(chunks),
                    heading_path=current_heading,
                    page_number=1,
                    text=body,
                    code_symbols=extract_code_symbols(body),
                )
            )

    for ln in lines:
        if ln.startswith("#"):
            _flush()
            current_heading = ln.lstrip("#").strip() or "Section"
            current_lines = [ln]
        else:
            current_lines.append(ln)
    _flush()

    if not chunks:
        chunks.append(
            ParsedChunkCandidate(
                chunk_index=0,
                heading_path="Root",
                page_number=1,
                text=text.strip(),
                code_symbols=extract_code_symbols(text),
            )
        )
    return chunks


# -----------------------------------------------------------------------------
# 3. Health Report Structured Metric Extractor (Section 11 / FR-14)
# -----------------------------------------------------------------------------

# Matches patterns like: "空腹血糖 5.4 mmol/L（参考范围 3.9-6.1）" or "总胆固醇: 6.2 mmol/L (参考值 <5.2)"
HEALTH_METRIC_REGEX = re.compile(
    r"(?P<name>空腹血糖|餐后血糖|总胆固醇|甘油三酯|收缩压|舒张压|血尿酸|心率|白细胞计数)"
    r"[\s:：]*"
    r"(?P<val>\d+(?:\.\d+)?)"
    r"\s*(?P<unit>[a-zA-Z/%μ]+\/[a-zA-Z]+|mmHg|bpm|mmol/L|mg/dL|μmol/L)?"
    r"(?:[^0-9\n]*?(?:参考范围|参考区间|参考值|正常范围)[\s:：]*(?P<ref>[0-9.\-~～<>≤≥\s]+))?",
    re.IGNORECASE,
)


def extract_health_metrics_from_text(
    text: str,
    *,
    default_subject: str = "本人",
    measured_date: Optional[str] = None,
    page_number: int = 1,
) -> List[Dict[str, Any]]:
    """
    Section 11.1 & 11.2: Extracts structured health indicators and flags any
    missing unit, missing reference range, or out-of-range value as `NEEDS_CHECK`.
    Never marks AI extraction as `HUMAN_VERIFIED` automatically.
    """
    extracted: List[Dict[str, Any]] = []
    eff_date = measured_date or date.today().isoformat()

    for m in HEALTH_METRIC_REGEX.finditer(text):
        name = m.group("name").strip()
        raw_val = m.group("val").strip()
        unit = (m.group("unit") or "").strip()
        ref = (m.group("ref") or "").strip()

        try:
            num_val: Optional[float] = float(raw_val)
        except ValueError:
            num_val = None

        needs_check_reasons: List[str] = []
        if not unit:
            needs_check_reasons.append("缺失单位")
        if not ref:
            needs_check_reasons.append("缺失报告参考范围")

        extracted.append({
            "subject_person": default_subject,
            "metric_name": name,
            "raw_extracted_value": f"{raw_val} {unit}".strip(),
            "normalized_value": num_val,
            "unit": unit or "UNKNOWN_UNIT",
            "reference_range": ref or "UNKNOWN_RANGE",
            "measured_date": eff_date,
            "source_locator": {
                "page": page_number,
                "matched_span": m.group(0),
                "check_reasons": needs_check_reasons,
            },
            "verification_status": "NEEDS_CHECK",
        })
    return extracted


# -----------------------------------------------------------------------------
# 4. Sensitive Information Scanner & Redaction Preview (FR-03 & FR-15)
# -----------------------------------------------------------------------------

SENSITIVE_PATTERNS: List[Tuple[str, re.Pattern[str], str]] = [
    ("CN_ID_CARD", re.compile(r"\b\d{17}[\dXx]\b"), "[REDACTED_ID_CARD]"),
    ("PHONE_NUMBER", re.compile(r"\b1[3-9]\d{9}\b"), "[REDACTED_PHONE]"),
    ("API_SECRET_KEY", re.compile(r"\b(?:sk|tok|ak)-[A-Za-z0-9_]{12,64}\b"), "[REDACTED_SECRET]"),
    ("BANK_CARD", re.compile(r"\b\d{16,19}\b"), "[REDACTED_BANK_CARD]"),
]


def scan_and_redact_sensitive_text(text: str) -> Dict[str, Any]:
    findings: List[Dict[str, str]] = []
    redacted = text
    for label, pattern, replacement in SENSITIVE_PATTERNS:
        for match in pattern.finditer(text):
            findings.append({"type": label, "Preview": match.group(0)[:4] + "***"})
        redacted = pattern.sub(replacement, redacted)

    return {
        "has_sensitive_data": len(findings) > 0,
        "findings": findings,
        "redacted_text": redacted,
    }
