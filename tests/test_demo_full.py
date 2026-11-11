"""R-05f: the complete governance demo must be real, reproducible and honest.

Covers two versions of one series, REVERT_VERSION, RECLASSIFY, a fresh search,
a cross-process DAG read, and CAS hash reconciliation against bytes on disk.
It also pins the fix for the fabricated `REVERT(...)` causal edge: a REVERTED
version is the version that *was undone*, never the version that did the undo.
"""

from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from omnilore.cli import main


REPO_ROOT = Path(__file__).resolve().parents[1]


class TestFullGovernanceDemo(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name).resolve() / "OmniLore_Data"

    def _run_main(self, *argv: str) -> tuple[int, str]:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(list(argv))
        return rc, buf.getvalue()

    def _payload(self, *argv: str) -> dict:
        rc, out = self._run_main(*argv)
        self.assertEqual(rc, 0, out[-1200:])
        start = out.find("{")
        self.assertGreaterEqual(start, 0, "no JSON object was printed")
        return json.loads(out[start:])

    def test_full_demo_end_to_end_and_cas_reconciliation(self) -> None:
        p = self._payload("demo", "--full", "--data-dir", str(self.data_dir), "--json")

        self.assertNotEqual(p["doc_v1"], p["doc_v2"])
        self.assertEqual(p["version_key"], "vk_demo_governance")
        self.assertEqual(p["versions_before_revert"], 2)
        self.assertEqual(p["current_doc_id_after_revert"], p["doc_v1"])
        self.assertEqual(p["category_after_reclassify"], "02_Life")
        self.assertEqual(p["versions_after_revert"], 2)
        self.assertTrue(p["found_current_document"])
        self.assertGreaterEqual(p["search_hits"], 1)

        # The lineage is real, but must not invent a reverse causal pointer.
        graph = p["ascii_lineage"]
        self.assertTrue(graph.strip())
        self.assertIn("REVERTED", graph)
        self.assertNotIn("REVERT(", graph)
        self.assertIn("已回退", graph)

        # `file_hash` must match the actual bytes on disk, not a copied value.
        disk_hashes = {
            hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (self.data_dir / "02_Vault").rglob("*.md")
        }
        self.assertIn(p["wiki_sha256_after_reclassify"], disk_hashes)

    def test_full_demo_text_mode_prints_every_step(self) -> None:
        rc, out = self._run_main("demo", "--full", "--data-dir", str(self.data_dir))
        self.assertEqual(rc, 0, out[-1200:])
        for marker in ("[1/5]", "[2/5]", "REVERT_VERSION", "RECLASSIFY", "[5/5]", "完整治理链路通过"):
            self.assertIn(marker, out)
        self.assertNotIn("REVERT(", out)

    def test_cross_process_history_reads_the_same_dag(self) -> None:
        p = self._payload("demo", "--full", "--data-dir", str(self.data_dir), "--json")
        env = {**os.environ, "PYTHONUTF8": "1"}
        proc = subprocess.run(
            [sys.executable, "-m", "omnilore.cli", "history", p["doc_v1"],
             "--data-dir", str(self.data_dir), "--json"],
            cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[-1200:])
        dag = json.loads(proc.stdout[proc.stdout.index("{"):])
        self.assertEqual(dag["asset_type"], "EVOLUTION_DAG")
        self.assertEqual(dag["total_versions"], 2)
        self.assertEqual(len(dag["nodes"]), 2)
        self.assertIn(p["doc_v1"], dag["nodes"])
        self.assertIn(p["doc_v2"], dag["nodes"])
        for node in dag["nodes"].values():
            self.assertIsNone(node["reverts_version_key"])
        self.assertNotIn("REVERT(", dag["ascii_graph"])

    def test_default_demo_is_unchanged_by_full_mode(self) -> None:
        p = self._payload("demo", "--data-dir", str(self.data_dir), "--json")
        self.assertTrue(p["found_demo_document"])
        self.assertNotIn("doc_v2", p)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
