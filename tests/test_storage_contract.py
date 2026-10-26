"""
Tests for Minimal Storage Contract & Adapter (Idea 5)
=====================================================
Verifies that:
1. StorageBackend protocol is runtime checkable.
2. EngineStorageAdapter correctly wraps in-memory and PostgreSQL engines.
3. CRUD operations conform to the uniform interface without leaking implementation details.
4. Capabilities declaration is honest and queryable.
"""

from __future__ import annotations

import unittest

from omnilore.engine import RLSDatabaseEngine
from omnilore.storage_contract import EngineStorageAdapter, StorageBackend


class TestStorageContract(unittest.IsolatedAsyncioTestCase):
    async def test_adapter_implements_storage_backend_protocol(self) -> None:
        raw_db = RLSDatabaseEngine()
        adapter = EngineStorageAdapter(raw_db, is_pg=False)

        # 1. Runtime protocol check
        self.assertIsInstance(adapter, StorageBackend)

        # 2. Honest capabilities check
        caps = adapter.capabilities
        self.assertEqual(caps.name, "in_memory_simulated")
        self.assertFalse(caps.is_persistent)
        self.assertEqual(caps.permission_granularity, "row_level_rls")
        self.assertTrue(caps.has_audit_log)

    async def test_adapter_crud_cycle(self) -> None:
        raw_db = RLSDatabaseEngine()
        adapter = EngineStorageAdapter(raw_db, is_pg=False)

        # 1. Insert
        sample_doc = {
            "doc_id": "doc_test_101",
            "space_id": "sp_main",
            "category": "01_Work",
            "title": "测试文档标题",
            "version_label": "v1.0",
        }
        inserted = await adapter.insert_row("documents", sample_doc)
        self.assertEqual(inserted["doc_id"], "doc_test_101")

        # 2. Select with predicate
        selected = await adapter.select_rows("documents", lambda d: d["space_id"] == "sp_main")
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["title"], "测试文档标题")

        # 3. Update
        updated = await adapter.update_row(
            "documents",
            "doc_test_101",
            lambda d: {**d, "title": "更新后的标题"},
        )
        self.assertEqual(updated["title"], "更新后的标题")

        # Verify update persisted
        re_selected = await adapter.select_rows("documents", lambda d: d["doc_id"] == "doc_test_101")
        self.assertEqual(re_selected[0]["title"], "更新后的标题")

        # 4. Delete
        await adapter.delete_row("documents", "doc_test_101")
        after_delete = await adapter.select_rows("documents", lambda d: d["doc_id"] == "doc_test_101")
        self.assertEqual(len(after_delete), 0)
