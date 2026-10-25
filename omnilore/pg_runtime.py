"""Safe PostgreSQL runtime connection layer for OmniLore (asyncpg).

为什么有这个模块
----------------
OmniLore 的对外核心宣称是「PostgreSQL RLS 提供命令级权限」。但应用运行时此前
**未接入** PostgreSQL：``OmniLoreEngine`` 默认实例化内存引擎 ``RLSDatabaseEngine``，
``mcp_stdio_server`` 不读 ``DATABASE_URL``，``AsyncpgRepository`` 定义了却零调用。

接通运行时有一条**绝对安全红线**：PostgreSQL 超级用户与 ``BYPASSRLS`` 角色
默认绕过 RLS。若应用以这类角色连接，结果不是报错而是「一切正常但权限隔离完全
失效」——比不接通更危险。因此本模块：

* 只允许用**非超级用户、无 ``BYPASSRLS``** 的连接角色建立运行时连接
  （``_assert_safe_role``，任一不满足即 ``raise``，fails closed）。
* 事务内 ``SET LOCAL ROLE`` 切到策略目标角色（``omnilore_gateway`` /
  ``omnilore_worker``），并把请求上下文通过 ``app.*`` GUC 注入策略函数；
  注入失败即中止事务（默认拒绝，不放行）。
* 提供 ``verify_rls_three()``：在真实 PostgreSQL 上证明 RLS 生效
  （未授权跨空间 0 行 / 授权后可见 / 连接角色非超用户且无 BYPASSRLS），
  跑不出结论就不宣称「接通完成」。

接入范围（诚实声明，不要误会成「整个 engine 已迁到 PG」）
----------------------------------------------------------
engine 的业务数据层（``RLSTransactionConnection`` 的 ``select_rows/insert_row/...``
接口 + ``self.db._state`` 内存字典直访）**仍然走内存引擎**。把这一整层翻译成真实
SQL 是独立的大工程——engine 的查询谓词是任意 Python lambda，无法靠「替换后端引擎」
无缝切换，必须逐业务方法重写；``AsyncpgRepository`` 是项目已有的真实 SQL 层起点
（已实现事务 GUC 绑定 + 混合检索 + 租约，见 ``omnilore/pg_repository.py``），但尚未
覆盖 engine 的完整数据面。

本模块本次钉住的是**安全红线**：设置 ``DATABASE_URL`` 时，应用以安全角色连接真实
PostgreSQL，并在启动时做一次 RLS 角色自检（fails closed）。engine 业务数据层的
完整 SQL 迁移留作后续独立工程。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, Optional

import asyncpg


class PgRuntimeError(RuntimeError):
    """真实 PostgreSQL 运行时连接/RLS 校验失败。"""


class PgRuntime:
    """以安全应用角色连接真实 PostgreSQL 的运行时（asyncpg）。

    Attributes:
        database_url: ``postgresql://<app_role>:<pwd>@host:port/db``。
        strategy_role: 事务内 ``SET LOCAL ROLE`` 切到的策略目标角色，
            默认 ``omnilore_gateway``（RLS 策略 ``FOR ... TO omnilore_gateway``）。
    """

    def __init__(
        self,
        database_url: str,
        strategy_role: str = "omnilore_gateway",
        connect_timeout: float = 10.0,
    ) -> None:
        self.database_url = database_url
        self.strategy_role = strategy_role
        self.connect_timeout = connect_timeout
        self._conn: Optional[asyncpg.Connection] = None

    async def connect(self) -> "PgRuntime":
        """建立连接并强制校验连接角色安全（fails closed）。"""
        try:
            self._conn = await asyncpg.connect(self.database_url, timeout=self.connect_timeout)
        except Exception as exc:  # 连接失败/认证失败/网络失败等
            raise PgRuntimeError(f"无法连接 PostgreSQL：{exc}") from exc
        await self._assert_safe_role()
        return self

    async def _assert_safe_role(self) -> None:
        """强制：连接角色必须非超级用户、无 BYPASSRLS，且是策略角色的成员。"""
        if self._conn is None:
            raise PgRuntimeError("尚未建立连接")
        row = await self._conn.fetchrow(
            "SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
        )
        if row is None:
            raise PgRuntimeError("无法确定连接角色身份")
        if row["rolsuper"]:
            raise PgRuntimeError(
                f"连接角色 {row['rolname']} 是超级用户，会绕过 RLS——拒绝在运行时使用"
            )
        if row["rolbypassrls"]:
            raise PgRuntimeError(
                f"连接角色 {row['rolname']} 具有 BYPASSRLS，会绕过 RLS——拒绝在运行时使用"
            )
        member = await self._conn.fetchval(
            "SELECT pg_has_role(current_user, $1, 'member')", self.strategy_role
        )
        if not member:
            raise PgRuntimeError(
                f"连接角色 {row['rolname']} 不是 {self.strategy_role} 的成员，"
                f"无法 SET LOCAL ROLE 到策略目标角色"
            )

    async def role_attrs(self) -> Dict[str, Any]:
        """当前连接角色的权限属性（用于 P-c 佐证）。"""
        if self._conn is None:
            raise PgRuntimeError("尚未建立连接")
        return dict(
            await self._conn.fetchrow(
                "SELECT rolname, rolsuper, rolbypassrls, rolcanlogin, "
                "rolcreaterole, rolcreatedb FROM pg_roles WHERE rolname = current_user"
            )
        )

    @asynccontextmanager
    async def transaction_guc(
        self,
        *,
        user_id: str,
        read_spaces: list[str],
        read_scopes: list[str],
        write_spaces: list[str],
        write_scopes: list[str],
        role: Optional[str] = None,
    ) -> AsyncIterator[asyncpg.Connection]:
        """事务内 SET LOCAL ROLE + 注入 RLS GUC（fails closed：任一步失败即中止）。"""
        if self._conn is None:
            raise PgRuntimeError("尚未建立连接")
        role = role or self.strategy_role
        async with self._conn.transaction():
            # 先清空再注入，避免残留上一次请求的 GUC。
            await self._conn.execute(
                "SELECT set_config('app.current_user_id','',true),"
                " set_config('app.allowed_read_spaces','',true),"
                " set_config('app.allowed_read_scopes','',true),"
                " set_config('app.allowed_write_spaces','',true),"
                " set_config('app.allowed_write_scopes','',true)"
            )
            await self._conn.execute("SET LOCAL ROLE " + role)
            await self._conn.execute("SELECT set_config('app.current_user_id',$1,true)", user_id)
            await self._conn.execute(
                "SELECT set_config('app.allowed_read_spaces',$1,true)", ",".join(read_spaces)
            )
            await self._conn.execute(
                "SELECT set_config('app.allowed_read_scopes',$1,true)", ",".join(read_scopes)
            )
            await self._conn.execute(
                "SELECT set_config('app.allowed_write_spaces',$1,true)", ",".join(write_spaces)
            )
            await self._conn.execute(
                "SELECT set_config('app.allowed_write_scopes',$1,true)", ",".join(write_scopes)
            )
            yield self._conn

    async def verify_rls_three(
        self,
        *,
        probe_space_id: str,
        authorized_space_id: str,
        user_id: str,
        category: str = "01_Work",
    ) -> Dict[str, Any]:
        """在真实 PostgreSQL 上验证 RLS 三条（需预置探针空间的行）。

        P-a：未授权另一空间 → 0 行；P-b：授权后同一条 → 可见（排除空表假阳性）；
        P-c：连接角色非超用户、无 BYPASSRLS。
        """
        async with self.transaction_guc(
            user_id=user_id,
            read_spaces=[authorized_space_id],
            read_scopes=[f"{authorized_space_id}:{category}"],
            write_spaces=[],
            write_scopes=[],
        ):
            pa = await self._conn.fetchval(
                "SELECT count(*) FROM documents WHERE space_id = $1", probe_space_id
            )
        async with self.transaction_guc(
            user_id=user_id,
            read_spaces=[authorized_space_id, probe_space_id],
            read_scopes=[f"{authorized_space_id}:{category}", f"{probe_space_id}:{category}"],
            write_spaces=[],
            write_scopes=[],
        ):
            pb = await self._conn.fetchval(
                "SELECT count(*) FROM documents WHERE space_id = $1", probe_space_id
            )
        attrs = await self.role_attrs()
        return {
            "P_a_unauthorized_rows": pa,
            "P_b_authorized_rows": pb,
            "role_attrs": attrs,
        }

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None
