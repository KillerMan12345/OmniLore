"""Real-PDF ingestion verification (direction 4).

Unlike the existing Markdown / PNG parser tests, this suite constructs a genuine
PDF byte stream (correct xref offsets, two pages, a real Type1 font) and pushes
it through both parser branches plus the full ingestion → CAS → chunk pipeline.
No mocks stand in for the PDF itself: `pdfplumber` and `pypdf` really decode it.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from omnilore.bootstrap import ensure_bootstrapped
from omnilore.engine import OmniLoreEngine, OmniLoreMCPGateway, OmniLoreError
from omnilore.parsers import parse_document_bytes

PAGE_1 = [
    "OmniLore Gateway Specification",
    "Single node request timeout is 500ms.",
    "All services use high availability.",
]
PAGE_2 = [
    "Authentication Protocol",
    "Clients must send HMAC-SHA256 signed headers.",
    "Sliding window rate limiting applies.",
]


def _escape_pdf_literal(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def build_minimal_pdf(pages: list[list[str]]) -> bytes:
    """Build a spec-compliant multi-page PDF with a real xref table.

    Deliberately hand-assembled so the test has zero extra dependencies and the
    bytes are reproducible. Offsets are recomputed from the actual output.
    """
    page_count = len(pages)
    font_obj_num = 3 + 2 * page_count
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(page_count))

    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {page_count} >>".encode("ascii"),
    ]
    for i, lines in enumerate(pages):
        content_obj_num = 4 + 2 * i
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 {font_obj_num} 0 R >> >> "
                f"/Contents {content_obj_num} 0 R >>"
            ).encode("ascii")
        )
        commands = ["BT", "/F1 12 Tf", "72 720 Td"]
        for j, line in enumerate(lines):
            if j:
                commands.append("0 -18 Td")
            commands.append(f"({_escape_pdf_literal(line)}) Tj")
        commands.append("ET")
        stream = ("\n".join(commands) + "\n").encode("latin-1")
        objects.append(
            b"<< /Length "
            + str(len(stream)).encode("ascii")
            + b" >>\nstream\n"
            + stream
            + b"endstream"
        )
    objects.append(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
    )

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for idx, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{idx} 0 obj\n".encode("ascii") + body + b"\nendobj\n"

    xref_pos = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF\n"
    ).encode("ascii")
    return bytes(out)


class TestRealPdfParser(unittest.TestCase):
    def test_pdfplumber_extracts_both_pages_with_page_numbers(self) -> None:
        pdf = build_minimal_pdf([PAGE_1, PAGE_2])
        self.assertTrue(pdf.startswith(b"%PDF-"))
        parsed = parse_document_bytes(pdf, "gateway_spec.pdf")
        self.assertEqual(parsed.page_count, 2)
        self.assertEqual(len(parsed.chunks), 2)
        self.assertIn("OmniLore", parsed.raw_text)
        self.assertIn("500ms", parsed.raw_text)
        self.assertIn("HMAC-SHA256", parsed.raw_text)
        self.assertEqual([c.page_number for c in parsed.chunks], [1, 2])
        self.assertEqual([c.chunk_index for c in parsed.chunks], [0, 1])
        self.assertEqual(parsed.chunks[0].heading_path, "Page 1")
        self.assertIn("500ms", parsed.chunks[0].text)
        self.assertIn("HMAC-SHA256", parsed.chunks[1].text)

    def test_pypdf_fallback_when_pdfplumber_raises(self) -> None:
        pdf = build_minimal_pdf([PAGE_1, PAGE_2])
        with patch("pdfplumber.open", side_effect=RuntimeError("simulated pdfplumber failure")):
            parsed = parse_document_bytes(pdf, "gateway_spec.pdf")
        self.assertEqual(parsed.page_count, 2)
        self.assertIn("OmniLore", parsed.raw_text)
        self.assertIn("HMAC-SHA256", parsed.raw_text)
        self.assertEqual(len(parsed.chunks), 2)

    def test_content_detection_works_without_pdf_extension(self) -> None:
        pdf = build_minimal_pdf([PAGE_1])
        parsed = parse_document_bytes(pdf, "no_extension_asset")
        self.assertIn("500ms", parsed.raw_text)

    def test_textless_pdf_fails_closed(self) -> None:
        pdf = build_minimal_pdf([[]])
        with self.assertRaisesRegex(ValueError, "EMPTY_DOCUMENT"):
            parse_document_bytes(pdf, "blank.pdf")


class TestRealPdfIngestion(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name) / "OmniLore_Data"
        self.engine = OmniLoreEngine(self.data_dir)
        self.boot = ensure_bootstrapped(self.engine, self.data_dir)
        self.gateway = OmniLoreMCPGateway(self.engine)
        self.pdf = build_minimal_pdf([PAGE_1, PAGE_2])

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_ingest_real_pdf_and_reconcile_cas_evidence(self) -> None:
        ingest = await self.gateway.ingest_document_or_file(
            self.boot.raw_token,
            content_bytes=self.pdf,
            filename="gateway_spec.pdf",
            target_space_id=self.boot.space_id,
        )
        doc_id = ingest["task"]["result_doc_id"]
        self.assertIsNotNone(doc_id)

        # 1. The immutable raw PDF is stored under its real SHA-256 digest.
        expected_hash = hashlib.sha256(self.pdf).hexdigest()
        assets = list(self.engine.db._state["assets"].values())
        matches = [a for a in assets if a["sha256_hash"] == expected_hash]
        self.assertEqual(len(matches), 1, "raw PDF must be CAS-addressed by its SHA-256")
        asset = matches[0]

        # 2. Reading the CAS asset back verifies the hash and returns the exact bytes.
        restored = self.engine.storage.read_cas_asset(
            asset["storage_rel_path"], expected_hash
        )
        self.assertEqual(restored, self.pdf)
        self.assertTrue(restored.startswith(b"%PDF-"))

        # 3. The document is linked to that asset, and real PDF text reached the chunks.
        links = [
            d for d in self.engine.db._state["document_assets"].values()
            if d["doc_id"] == doc_id
        ]
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["asset_id"], asset["asset_id"])
        chunks = [
            c for c in self.engine.db._state["document_chunks"].values()
            if c["doc_id"] == doc_id
        ]
        self.assertTrue(chunks, "PDF text must be indexed as searchable chunks")
        joined = "\n".join(str(c["chunk_text"]) for c in chunks)
        self.assertIn("500ms", joined)
        self.assertIn("HMAC-SHA256", joined)

        # 4. Tampering with the expected digest is rejected (fail-closed).
        with self.assertRaises(OmniLoreError) as ctx:
            self.engine.storage.read_cas_asset(asset["storage_rel_path"], "0" * 64)
        self.assertEqual(ctx.exception.code, "ASSET_CORRUPTED")


if __name__ == "__main__":
    unittest.main()
