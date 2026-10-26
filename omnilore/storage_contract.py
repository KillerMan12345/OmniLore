"""
Storage Backend Minimal Capability Contract
===========================================
Defines the minimal interface and capabilities protocol that all OmniLore storage
backends must fulfill, decoupling high-level MCP & governance logic from specific
database engines (PostgreSQL, in-memory simulated, or local SQLite/Git).
Derived from design draft 2026-10-24_存储后端抽象设计草案.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class StorageCapabilities:
    """Honest declaration of backend capabilities without overpromising."""
    name: str
    search_mode: str               # "hybrid" | "vector" | "bm25"
    permission_granularity: str    # "row_level_rls" | "space_level" | "none"
    has_audit_log: bool
    is_persistent: bool
    supports_vector: bool = False
    supports_fulltext: bool = True


@runtime_checkable
class StorageBackend(Protocol):
    """Minimal capability protocol required for an OmniLore storage layer."""

    @property
    def capabilities(self) -> StorageCapabilities:
        """Declared honest capabilities."""
        ...

    async def select_rows(
        self,
        table_name: str,
        predicate: Optional[Callable[[Dict[str, Any]], bool]] = None,
    ) -> List[Dict[str, Any]]:
        """Query rows matching predicate."""
        ...

    async def insert_row(
        self,
        table_name: str,
        row: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Insert a single row."""
        ...

    async def update_row(
        self,
        table_name: str,
        row_id: str,
        updater: Callable[[Dict[str, Any]], Dict[str, Any]],
        id_field: str = "",
    ) -> Dict[str, Any]:
        """Update a row by ID."""
        ...

    async def delete_row(
        self,
        table_name: str,
        row_id: str,
        id_field: str = "",
    ) -> None:
        """Delete a row by ID."""
        ...


class EngineStorageAdapter:
    """
    Adapts existing database engines (OmniLoreRLSDatabaseEngine or PgDatabaseEngine)
    to the uniform StorageBackend contract.
    """

    def __init__(self, raw_db_engine: Any, is_pg: bool = False):
        self._raw = raw_db_engine
        self._is_pg = is_pg

        if is_pg:
            self._caps = StorageCapabilities(
                name="postgresql_rls",
                search_mode="hybrid",
                permission_granularity="row_level_rls",
                has_audit_log=True,
                is_persistent=True,
                supports_vector=True,
                supports_fulltext=True,
            )
        else:
            self._caps = StorageCapabilities(
                name="in_memory_simulated",
                search_mode="bm25",
                permission_granularity="row_level_rls",
                has_audit_log=True,
                is_persistent=False,
                supports_vector=False,
                supports_fulltext=True,
            )

    @property
    def capabilities(self) -> StorageCapabilities:
        return self._caps

    async def select_rows(
        self,
        table_name: str,
        predicate: Optional[Callable[[Dict[str, Any]], bool]] = None,
    ) -> List[Dict[str, Any]]:
        # Delegates to the raw engine transaction or state
        if self._is_pg and hasattr(self._raw, "acquire"):
            async with self._raw.acquire() as conn:
                return await conn.select_rows(table_name, predicate)
        # In-memory engine fallback
        rows = list(self._raw._state.get(table_name, {}).values())
        if predicate:
            return [r for r in rows if predicate(r)]
        return rows

    async def insert_row(
        self,
        table_name: str,
        row: Dict[str, Any],
    ) -> Dict[str, Any]:
        if self._is_pg and hasattr(self._raw, "acquire"):
            async with self._raw.acquire() as conn:
                return await conn.insert_row(table_name, row)
        pk_field = getattr(self._raw, "_table_pks", {}).get(table_name, "id")
        pk_val = str(row.get(pk_field, row.get("doc_id", row.get("task_id", "row_1"))))
        self._raw._state.setdefault(table_name, {})[pk_val] = dict(row)
        return dict(row)

    async def update_row(
        self,
        table_name: str,
        row_id: str,
        updater: Callable[[Dict[str, Any]], Dict[str, Any]],
        id_field: str = "",
    ) -> Dict[str, Any]:
        if self._is_pg and hasattr(self._raw, "acquire"):
            async with self._raw.acquire() as conn:
                return await conn.update_row(table_name, row_id, updater, id_field=id_field)
        table = self._raw._state.get(table_name, {})
        if row_id not in table:
            raise KeyError(f"Row {row_id} not found in {table_name}")
        old_val = table[row_id]
        new_val = updater(old_val)
        table[row_id] = new_val
        return new_val

    async def delete_row(
        self,
        table_name: str,
        row_id: str,
        id_field: str = "",
    ) -> None:
        if self._is_pg and hasattr(self._raw, "acquire"):
            async with self._raw.acquire() as conn:
                await conn.delete_row(table_name, row_id, id_field=id_field)
                return
        table = self._raw._state.get(table_name, {})
        if row_id in table:
            del table[row_id]
