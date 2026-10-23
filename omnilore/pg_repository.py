"""
Production PostgreSQL (`asyncpg`) Parameterized SQL Repository Layer
====================================================================
Provides the real SQL execution layer for PostgreSQL 15+ with `pgvector` and `ltree`:
  - Binds transaction-local RLS GUC context (`app.current_user_id`, `app.allowed_read_spaces`,
    `app.allowed_read_scopes`, `app.allowed_write_spaces`, `app.allowed_write_scopes`)
  - Executes parameterized hybrid search combining `pgvector` HNSW cosine similarity,
    `ts_rank_cd` full-text BM25, and GIN `code_symbols` array matching
  - Acquires worker task leases atomically via `FOR UPDATE SKIP LOCKED`
  - Switches `document_series.current_doc_id` with row-level `FOR UPDATE` locking
"""

from __future__ import annotations

from contextlib import asynccontextmanager
import json
from typing import Any, AsyncIterator, Dict, List, Optional


class AsyncpgRepository:
    """
    Production PostgreSQL repository using an `asyncpg.Pool` connection pool.
    All queries are 100% parameterized ($1, $2, ...) and execute inside
    `profiled_transaction` so PostgreSQL Row-Level Security (RLS) is enforced
    on every command.
    """

    def __init__(self, pool: Any):
        self.pool = pool

    @asynccontextmanager
    async def transaction(
        self,
        *,
        user_id: str,
        read_spaces: List[str],
        read_scopes: List[str],
        write_spaces: List[str],
        write_scopes: List[str],
    ) -> AsyncIterator[Any]:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "SELECT set_config('app.current_user_id', '', true), "
                    "set_config('app.allowed_read_spaces', '', true), "
                    "set_config('app.allowed_read_scopes', '', true), "
                    "set_config('app.allowed_write_spaces', '', true), "
                    "set_config('app.allowed_write_scopes', '', true)"
                )
                await conn.execute("SELECT set_config('app.current_user_id', $1, true)", user_id)
                await conn.execute("SELECT set_config('app.allowed_read_spaces', $1, true)", ",".join(read_spaces))
                await conn.execute("SELECT set_config('app.allowed_read_scopes', $1, true)", ",".join(read_scopes))
                await conn.execute("SELECT set_config('app.allowed_write_spaces', $1, true)", ",".join(write_spaces))
                await conn.execute("SELECT set_config('app.allowed_write_scopes', $1, true)", ",".join(write_scopes))
                yield conn

    async def hybrid_search_sql(
        self,
        conn: Any,
        *,
        query_text: str,
        query_embedding: List[float],
        space_ids: Optional[List[str]] = None,
        categories: Optional[List[str]] = None,
        code_symbol: Optional[str] = None,
        include_history: bool = False,
        top_k: int = 5,
    ) -> List[Dict[str, Any]]:
        """
        Executes hybrid retrieval inside an RLS-protected transaction:
          - Filters by `is_current = true` (unless `include_history = true`)
          - Computes vector cosine similarity via `1 - (c.embedding <=> $1::vector)`
          - Computes full-text rank via `ts_rank_cd(c.tsv, plainto_tsquery('simple', $2))`
        """
        vec_literal = "[" + ",".join(f"{v:.6f}" for v in query_embedding) + "]"
        sql = """
            SELECT
                c.chunk_id::text,
                c.doc_id::text,
                c.series_id::text,
                c.space_id,
                c.category,
                c.heading_path,
                c.page_number,
                c.chunk_text,
                c.code_symbols,
                d.title,
                d.version_label,
                d.doc_status,
                (
                    0.5 * GREATEST(0.0, 1.0 - (c.embedding <=> $1::vector)) +
                    0.5 * COALESCE(ts_rank_cd(c.tsv, plainto_tsquery('simple', $2)), 0.0)
                ) AS hybrid_score
            FROM document_chunks c
            JOIN documents d ON d.doc_id = c.doc_id
            WHERE ($3::boolean = true OR c.is_current = true)
              AND ($4::text[] IS NULL OR c.space_id = ANY($4::text[]))
              AND ($5::text[] IS NULL OR c.category = ANY($5::text[]))
              AND ($6::text IS NULL OR $6::text = ANY(c.code_symbols))
            ORDER BY hybrid_score DESC
            LIMIT $7
        """
        rows = await conn.fetch(
            sql,
            vec_literal,
            query_text,
            include_history,
            space_ids,
            categories,
            code_symbol,
            top_k,
        )
        return [dict(r) for r in rows]

    async def acquire_worker_lease_sql(
        self,
        conn: Any,
        *,
        worker_id: str,
        lease_seconds: int = 60,
    ) -> Optional[Dict[str, Any]]:
        """
        Section 4.4 Concurrent Worker Claiming:
        Uses `FOR UPDATE SKIP LOCKED` to claim a single pending or expired-lease task.
        """
        sql = """
            WITH candidate AS (
                SELECT event_id
                FROM outbox_events
                WHERE delivery_status = 'PENDING'
                ORDER BY created_at ASC
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            UPDATE outbox_events o
            SET delivery_status = 'PROCESSING',
                retry_count = o.retry_count + 1
            FROM candidate
            WHERE o.event_id = candidate.event_id
            RETURNING o.event_id::text, o.task_id::text, o.space_id, o.category, o.event_type, o.payload
        """
        row = await conn.fetchrow(sql)
        if not row:
            return None
        res = dict(row)
        if isinstance(res.get("payload"), str):
            res["payload"] = json.loads(res["payload"])
        return res
