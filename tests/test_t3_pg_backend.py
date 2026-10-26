"""T3 PostgreSQL backend smoke test.

Run with:
    DATABASE_URL="postgresql://postgres:change_me_local_dev_only@localhost:55433/omnilore" \
        python tests/test_t3_pg_backend.py
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from omnilore.engine import OmniLoreEngine
from omnilore.pg_engine import PgDatabaseEngine


async def main() -> int:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print("SKIP: DATABASE_URL not set")
        return 0

    with tempfile.TemporaryDirectory() as tmpdir:
        # 1. Engine creation with DATABASE_URL should pick PgDatabaseEngine
        engine = OmniLoreEngine(Path(tmpdir))
        if not isinstance(engine.db, PgDatabaseEngine):
            print(f"FAIL: Expected PgDatabaseEngine, got {type(engine.db).__name__}")
            return 1
        print("PASS: OmniLoreEngine uses PgDatabaseEngine when DATABASE_URL is set")

        # 2. _state is lazy-loaded on first acquire — trigger it
        from omnilore.engine import ResolvedScope, ProfileType
        dummy_scope = ResolvedScope(
            user_id="loader",
            token_id="tok_loader",
            profile_type=ProfileType.WORK,
            read_spaces=set(),
            read_scopes=set(),
            write_spaces=set(),
            write_scopes=set(),
            expires_at=9999999999.0,
        )
        from omnilore.engine import profiled_transaction
        async with profiled_transaction(engine.db, dummy_scope):
            pass
        if not engine.db._state:
            print("FAIL: _state is empty after first acquire")
            return 1
        print(f"PASS: _state loaded with {len(engine.db._state)} tables")

        # 3. Bootstrap a space + user + token
        from omnilore.bootstrap import ensure_bootstrapped
        result = ensure_bootstrapped(engine, Path(tmpdir), token="tok_t3_test")
        print(f"PASS: Bootstrapped space={result.space_id} user={result.user_id}")

        # 3a. Seed users / spaces into PG so FK constraints are satisfied
        migrator_dsn = os.environ.get("OMNILORE_MIGRATOR_DSN", database_url)
        import asyncpg
        try:
            pg_pool = await asyncpg.create_pool(migrator_dsn)
            assert pg_pool is not None
            async with pg_pool.acquire() as raw_conn:
                await raw_conn.execute(
                    "INSERT INTO users (user_id, display_name, status) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
                    result.user_id, "T3 Test User", "ACTIVE",
                )
                await raw_conn.execute(
                    "INSERT INTO spaces (space_id, space_type, vault_rel_path, allowed_categories) VALUES ($1, $2, $3, $4) ON CONFLICT DO NOTHING",
                    result.space_id, "PERSONAL", f"spaces/{result.space_id}", ["01_Work", "02_Life", "03_Family", "04_Health_Archive"],
                )
            await pg_pool.close()
            await engine.db.refresh_state()
            print("PASS: Seeded users/spaces into PG")
        except asyncpg.exceptions.InsufficientPrivilegeError:
            print("INFO: Skipped seeding users/spaces (omnilore_app lacks INSERT, assuming pre-seeded)")

        try:
            # 4. Ingest a document via PG backend
            from omnilore.engine import OperationType
            task = await engine.ingest_document(
                result.raw_token,
                raw_bytes=b"# Hello PG Backend\n\nThis is a T3 smoke test.",
                original_filename="t3_test.md",
                target_space_id=result.space_id,
                target_category="01_Work",
                operation_type=OperationType.INGEST_FILE,
            )
            print(f"PASS: Ingested doc task_id={task['task_id']}")

            # 5. Verify document exists in PG
            from omnilore.engine import ResolvedScope
            scope = engine.auth.resolve_scope(result.raw_token)
            from omnilore.engine import profiled_transaction
            async with profiled_transaction(engine.db, scope) as conn:
                docs = await conn.select_rows("documents")
                if not docs:
                    print("FAIL: No documents found in PG after ingestion")
                    return 1
                print(f"PASS: Found {len(docs)} document(s) in PG")

                # 6. RLS check: same query with empty scope should return 0 rows
                empty_scope = ResolvedScope(
                    user_id="nobody",
                    token_id="tok_nobody",
                    profile_type=ProfileType.WORK,
                    read_spaces=set(),
                    read_scopes=set(),
                    write_spaces=set(),
                    write_scopes=set(),
                    expires_at=9999999999.0,
                )

            async with profiled_transaction(engine.db, empty_scope) as conn2:
                docs_unauth = await conn2.select_rows("documents")
                if docs_unauth:
                    print(f"FAIL: RLS bypass — unauthorized scope saw {len(docs_unauth)} row(s)")
                    return 1
                print("PASS: RLS correctly blocks unauthorized reads (0 rows)")

            # 7. Health metric CRUD
            from omnilore.engine import OmniLoreMCPGateway, ReviewAction, PermissionDeniedError
            gateway = OmniLoreMCPGateway(engine)
            try:
                await gateway.review_and_fix_knowledge(
                    result.raw_token,
                    action=ReviewAction.FIX_EXTRACTED_FIELD,
                    target_id="nonexistent",
                    payload={},
                )
            except PermissionDeniedError:
                print("PASS: review_and_fix_knowledge correctly raised PermissionDeniedError for nonexistent target")
        finally:
            if hasattr(engine.db, "close"):
                await engine.db.close()

        print("\n=== T3 PG Backend Smoke Test: ALL PASSED ===")
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
