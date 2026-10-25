"""
PG 运行时接线回归测试（钉住两条红线）
=====================================
红线一（用户约束 5）：**不设 DATABASE_URL 时，应用行为必须与现在完全一致**——
``OmniLoreEngine`` 默认实例化**内存引擎** ``RLSDatabaseEngine``，``mcp_stdio_server``
不接入任何 PostgreSQL 运行时。

红线二：**PG 运行时绝不允许以会绕过 RLS 的角色连接**——超级用户或 ``BYPASSRLS``
连接角色必须在 ``PgRuntime.connect()`` 阶段被拒绝（fails closed）。

完全离线：不依赖真实 PostgreSQL 实例。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

from omnilore import OmniLoreEngine
from omnilore.engine import RLSDatabaseEngine
from omnilore.pg_runtime import PgRuntime, PgRuntimeError


class _FakeConn:
    """模拟 asyncpg 连接：fetchrow 返回角色属性，fetchval 返回成员资格。"""

    def __init__(self, role_attrs, is_member=True):
        self._attrs = role_attrs
        self._member = is_member

    async def fetchrow(self, _sql, *_a):
        return self._attrs

    async def fetchval(self, _sql, *_a):
        return self._member

    async def close(self):
        pass

    async def transaction(self):
        raise NotImplementedError("本测试不进入事务路径")


class TestPgRuntimeWiring(unittest.TestCase):
    def setUp(self):
        self._data_dir = Path(tempfile.mkdtemp(prefix="omnilore-pgrt-"))
        self.addCleanup(lambda: shutil.rmtree(self._data_dir, ignore_errors=True))
        # 隔离环境变量：测试不依赖外部 DATABASE_URL。
        self._saved_db_url = os.environ.pop("DATABASE_URL", None)

    def tearDown(self):
        if self._saved_db_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = self._saved_db_url

    def test_engine_defaults_to_memory_db_engine(self):
        """红线一：不传 db_engine（等价于不设 DATABASE_URL）→ 默认内存引擎。"""
        engine = OmniLoreEngine(self._data_dir)
        self.assertIsInstance(engine.db, RLSDatabaseEngine)
        self.assertFalse(hasattr(engine, "pg_runtime"))

    def test_mcp_attach_pg_runtime_is_noop_without_database_url(self):
        """红线一：mcp 无 DATABASE_URL 时不接入任何 PG 运行时。"""
        from omnilore.mcp_stdio_server import _attach_pg_runtime

        os.environ.pop("DATABASE_URL", None)
        engine = OmniLoreEngine(self._data_dir)

        async def _run():
            await _attach_pg_runtime(engine)

        asyncio.run(_run())
        self.assertFalse(hasattr(engine, "pg_runtime"))
        self.assertIsInstance(engine.db, RLSDatabaseEngine)

    def test_pg_runtime_rejects_superuser_connection_role(self):
        """红线二：连接角色是超级用户 → connect() 必须拒绝。"""
        superuser_attrs = {
            "rolname": "omnilore_migrator",
            "rolsuper": True,
            "rolbypassrls": True,
        }
        with mock.patch(
            "omnilore.pg_runtime.asyncpg.connect",
            new=mock.AsyncMock(return_value=_FakeConn(superuser_attrs)),
        ):
            rt = PgRuntime("postgresql://ignored@localhost/db")
            with self.assertRaises(PgRuntimeError):
                asyncio.run(rt.connect())

    def test_pg_runtime_rejects_bypassrls_connection_role(self):
        """红线二：连接角色有 BYPASSRLS → connect() 必须拒绝。"""
        bypass_attrs = {
            "rolname": "omnilore_migrator",
            "rolsuper": False,
            "rolbypassrls": True,
        }
        with mock.patch(
            "omnilore.pg_runtime.asyncpg.connect",
            new=mock.AsyncMock(return_value=_FakeConn(bypass_attrs)),
        ):
            rt = PgRuntime("postgresql://ignored@localhost/db")
            with self.assertRaises(PgRuntimeError):
                asyncio.run(rt.connect())

    def test_pg_runtime_accepts_safe_connection_role(self):
        """红线二：非超用户、无 BYPASSRLS、且是策略角色成员 → connect() 通过。"""
        safe_attrs = {
            "rolname": "omnilore_app",
            "rolsuper": False,
            "rolbypassrls": False,
        }
        with mock.patch(
            "omnilore.pg_runtime.asyncpg.connect",
            new=mock.AsyncMock(return_value=_FakeConn(safe_attrs, is_member=True)),
        ):
            rt = PgRuntime("postgresql://ignored@localhost/db")
            asyncio.run(rt.connect())  # 不应抛出
            asyncio.run(rt.close())


if __name__ == "__main__":
    unittest.main()
