"""
Background Worker & Obsidian File Watcher Daemon Services
=========================================================
Provides:
  1. `OutboxRecoveryDaemon`: Polls pending Outbox events, recovers interrupted
     STAGED/COMMITTING tasks, and refreshes Obsidian `_dashboard_控制台.md` views.
  2. `ObsidianVaultWatcher`: Polls `02_Vault/spaces/<space_id>/wiki/*.md` for local
     user edits in Obsidian, ignoring editor temp files (`.tmp`, `~`, `.swp`) and
     conflict copies (`.conflict_r*.md`), and triggers `sync_obsidian_markdown_edit`.
"""

from __future__ import annotations

import hashlib
from typing import Dict, List

from .engine import OmniLoreEngine
from .textio import read_text_canonical


IGNORED_SUFFIXES = (".tmp", ".swp", ".bak", "~", ".staged")


class ObsidianVaultWatcher:
    """
    Section 3.2 Journey C & Section 10.2 File Listener:
      - Monitors only allowed `wiki/` paths and `.md` files
      - Ignores temporary editor files, `_dashboard_控制台.md`, and `.conflict_r*.md`
      - Detects disk changes and invokes `engine.sync_obsidian_markdown_edit`
    """

    def __init__(self, engine: OmniLoreEngine):
        self.engine = engine
        self._observed_hashes: Dict[str, str] = {}

    async def scan_once(self, raw_token: str, space_ids: List[str]) -> List[Dict[str, str]]:
        events: List[Dict[str, str]] = []
        for space_id in space_ids:
            wiki_dir = self.engine.storage.vault_dir / space_id / "wiki"
            if not wiki_dir.exists():
                continue
            for md_file in sorted(wiki_dir.glob("*.md")):
                name = md_file.name
                if (
                    name.endswith(IGNORED_SUFFIXES)
                    or name.startswith(".")
                    or ".conflict_r" in name
                    or name == "_dashboard_控制台.md"
                ):
                    continue
                content = read_text_canonical(md_file)
                disk_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
                rel_path = f"{space_id}/wiki/{name}"
                if self._observed_hashes.get(rel_path) == disk_hash:
                    continue
                sync_res = await self.engine.sync_obsidian_markdown_edit(
                    raw_token,
                    space_id,
                    rel_path,
                    content,
                )
                # Update observed hash to current on-disk state after sync normalization
                updated_content = read_text_canonical(md_file)
                self._observed_hashes[rel_path] = hashlib.sha256(
                    updated_content.encode("utf-8")
                ).hexdigest()
                events.append({
                    "wiki_rel_path": rel_path,
                    "sync_status": sync_res["status"],
                })
        return events


class OutboxRecoveryDaemon:
    """
    Section 4.5 Step 5 & Section 12.1:
    Periodically processes pending Outbox events, reconciles storage vs DB state,
    and updates Obsidian space dashboards.
    """

    def __init__(self, engine: OmniLoreEngine):
        self.engine = engine

    async def tick(self, raw_token: str, space_ids: List[str]) -> Dict[str, object]:
        recovered = await self.engine.run_recovery_worker(raw_token)
        reconcile = self.engine.ops.reconcile_consistency()
        for sp in space_ids:
            await self.engine.generate_obsidian_dashboard(raw_token, sp)
        return {
            "recovered_outbox_events": recovered,
            "is_consistent": reconcile["is_consistent"],
        }
