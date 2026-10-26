"""PostgreSQL-backed database engine for OmniLore (asyncpg).

Provides a PG-native implementation of the RLSDatabaseEngine interface:
  - select_rows / insert_row / update_row / delete_row via parameterized SQL
  - RLS enforced by PostgreSQL itself (GUC + SET LOCAL ROLE, no Python _can_read)
  - _state kept in sync for compatibility with existing direct-access code

Honest scope (this is NOT a full migration yet)
-----------------------------------------------
Engine business methods still use arbitrary Python lambdas as query predicates.
PgTransactionConnection.select_rows() fetches the full result set and applies
the lambda in Python — correct but not efficient for large tables.  Future work
can replace simple equality lambdas with SQL WHERE clauses.

ledger_entries is memory-only (no PG table).  All other tables are backed by PG.
"""

from __future__ import annotations

import copy
import json
import re
from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Set

# Import error types from the main engine module
from .engine import PermissionDeniedError

# Table → primary-key column(s)
TABLE_PRIMARY_KEYS: Dict[str, str | tuple[str, ...]] = {
    "documents": "doc_id",
    "document_series": "series_id",
    "document_chunks": "chunk_id",
    "assets": "asset_id",
    "document_assets": ("doc_id", "asset_id"),
    "document_relations": "relation_id",
    "ingestion_tasks": "task_id",
    "outbox_events": "event_id",
    "health_metrics": "metric_id",
    "audit_events": "audit_id",
    "model_runs": "run_id",
    # "ledger_entries" is intentionally omitted — memory-only, no PG table.
}

# Tables that exist in _state but are NOT in PG (memory-only fallback)
MEMORY_ONLY_TABLES: Set[str] = {"ledger_entries"}


def _serialize_for_pg(value: Any) -> Any:
    """Serialize values for asyncpg:
      - dict → JSON string (JSONB)
      - list of strings → keep as list (PG text array)
      - list of non-strings → JSON string (JSONB array)
      - ISO-8601 date → datetime.date
      - ISO-8601 datetime → datetime.datetime
      - primitives → pass through
    """
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        if value and all(isinstance(x, str) for x in value):
            return value  # PG text array (tags, code_symbols, outbound_links, ...)
        return json.dumps(value, ensure_ascii=False)  # JSONB
    if isinstance(value, str):
        # DATE: 2026-10-26
        if re.match(r"^\d{4}-\d{2}-\d{2}$", value):
            return date.fromisoformat(value)
        # TIMESTAMPTZ: 2026-10-26T06:25:35.424392+00:00
        if re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:\d{2}|Z)$", value):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value


def _prepare_row_for_insert(row: Dict[str, Any]) -> tuple[List[str], List[Any]]:
    cols = list(row.keys())
    values = [_serialize_for_pg(row[c]) for c in cols]
    return cols, values


class PgTransactionConnection:
    """Async CRUD connection backed by a real asyncpg connection.

    RLS is enforced by PostgreSQL (GUC context + SET LOCAL ROLE).  The Python
    side does **not** re-implement _can_read / _can_write — that would be
    redundant and risk drift from the 21 SQL policies.
    """

    def __init__(self, pg_conn: Any, engine: "PgDatabaseEngine") -> None:
        self._pg_conn = pg_conn
        self._db = engine
        self.guc: Dict[str, str] = {}

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator["PgTransactionConnection"]:
        """Begin an asyncpg transaction and yield self (mirrors RLSTransactionConnection)."""
        async with self._pg_conn.transaction():
            yield self

    async def execute(self, sql: str, *args: Any) -> str:
        await self._pg_conn.execute(sql, *args)
        return "OK"

    async def select_rows(
        self,
        table: str,
        predicate: Optional[Callable[[Dict[str, Any]], bool]] = None,
    ) -> List[Dict[str, Any]]:
        """SELECT * FROM table — RLS filtering happens inside PostgreSQL."""
        if table in MEMORY_ONLY_TABLES:
            rows = list(self._db._state.get(table, {}).values())
        else:
            pg_rows = await self._pg_conn.fetch(f"SELECT * FROM {table}")
            rows = [dict(r) for r in pg_rows]
        if predicate:
            rows = [r for r in rows if predicate(r)]
        return [copy.deepcopy(r) for r in rows]

    async def insert_row(self, table: str, pk: str, row: Dict[str, Any]) -> Dict[str, Any]:
        if table in MEMORY_ONLY_TABLES:
            stored = copy.deepcopy(row)
            self._db._state.setdefault(table, {})[stored[pk]] = stored
            self._db._dirty_tables.add(table)
            return copy.deepcopy(stored)

        cols, values = _prepare_row_for_insert(row)
        placeholders = [f"${i + 1}" for i in range(len(cols))]
        sql = f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join(placeholders)}) RETURNING *"
        pg_row = await self._pg_conn.fetchrow(sql, *values)
        stored = dict(pg_row)
        pk_val = stored.get(pk)
        if pk_val is not None:
            self._db._state.setdefault(table, {})[str(pk_val)] = copy.deepcopy(stored)
        self._db._dirty_tables.add(table)
        return copy.deepcopy(stored)

    async def update_row(
        self,
        table: str,
        pk_val: str,
        updater: Callable[[Dict[str, Any]], Dict[str, Any]],
    ) -> Dict[str, Any]:
        if table in MEMORY_ONLY_TABLES:
            existing = self._db._state[table].get(pk_val)
            if not existing:
                raise PermissionDeniedError(f"RLS UPDATE target not found on {table}")
            updated = updater(copy.deepcopy(existing))
            self._db._state[table][pk_val] = updated
            self._db._dirty_tables.add(table)
            return copy.deepcopy(updated)

        pk_col = TABLE_PRIMARY_KEYS.get(table)
        if pk_col is None:
            raise RuntimeError(f"Unknown primary key for table {table}")
        if isinstance(pk_col, tuple):
            raise RuntimeError(f"Composite primary key not supported for update: {table}")

        sql = f"SELECT * FROM {table} WHERE {pk_col} = $1"
        pg_row = await self._pg_conn.fetchrow(sql, pk_val)
        if pg_row is None:
            raise PermissionDeniedError(f"RLS UPDATE target not found or not visible on {table}")

        existing = dict(pg_row)
        updated = updater(copy.deepcopy(existing))

        set_cols: List[str] = []
        set_values: List[Any] = []
        for k, v in updated.items():
            if k == pk_col:
                continue
            set_cols.append(f"{k} = ${len(set_values) + 2}")
            set_values.append(_serialize_for_pg(v))

        sql = f"UPDATE {table} SET {','.join(set_cols)} WHERE {pk_col} = $1 RETURNING *"
        pg_row = await self._pg_conn.fetchrow(sql, pk_val, *set_values)
        stored = dict(pg_row)

        self._db._state.setdefault(table, {})[str(pk_val)] = copy.deepcopy(stored)
        self._db._dirty_tables.add(table)
        return copy.deepcopy(stored)

    async def delete_row(self, table: str, pk_val: str) -> None:
        if table in MEMORY_ONLY_TABLES:
            if table in self._db._state:
                self._db._state[table].pop(pk_val, None)
            self._db._dirty_tables.add(table)
            return

        pk_col = TABLE_PRIMARY_KEYS.get(table)
        if pk_col is None:
            raise RuntimeError(f"Unknown primary key for table {table}")
        if isinstance(pk_col, tuple):
            raise RuntimeError(f"Composite primary key not supported for delete: {table}")

        sql = f"DELETE FROM {table} WHERE {pk_col} = $1"
        await self._pg_conn.execute(sql, pk_val)

        if table in self._db._state:
            self._db._state[table].pop(str(pk_val), None)
        self._db._dirty_tables.add(table)


class PgDatabaseEngine:
    """PostgreSQL-backed engine with in-memory _state mirror.

    _state is loaded lazily on first acquire() and kept in sync by write
    operations.  Existing code that reads self.db._state directly continues
    to work (with the caveat that _state reflects the last sync point).

    The asyncpg pool is created lazily on first use so that the engine can be
    instantiated synchronously (e.g. from ``OmniLoreEngine.__init__``).
    """

    def __init__(self, database_url: str) -> None:
        self._database_url = database_url
        self._pool: Optional[Any] = None
        self._state: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._dirty_tables: Set[str] = set()
        self.on_commit: Optional[Callable[[], None]] = None

    async def _ensure_pool(self) -> None:
        if self._pool is None:
            import asyncpg

            self._pool = await asyncpg.create_pool(self._database_url)

    async def refresh_state(self) -> None:
        """Load all PG-backed tables into _state."""
        await self._ensure_pool()
        assert self._pool is not None
        async with self._pool.acquire() as conn:
            for table, pk_col in TABLE_PRIMARY_KEYS.items():
                pg_rows = await conn.fetch(f"SELECT * FROM {table}")
                table_data: Dict[str, Dict[str, Any]] = {}
                for row in pg_rows:
                    row_dict = dict(row)
                    if isinstance(pk_col, tuple):
                        key = ":".join(str(row_dict[k]) for k in pk_col)
                    else:
                        key = str(row_dict[pk_col])
                    table_data[key] = row_dict
                self._state[table] = table_data
            # Memory-only tables start empty
            for mem_table in MEMORY_ONLY_TABLES:
                self._state.setdefault(mem_table, {})

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[PgTransactionConnection]:
        if not self._state:
            await self.refresh_state()
        await self._ensure_pool()
        assert self._pool is not None
        async with self._pool.acquire() as pg_conn:
            yield PgTransactionConnection(pg_conn, self)
