#!/usr/bin/env python3
"""真实 PostgreSQL RLS 探针（`make verify-rls-pg`）。

为什么这个脚本存在
------------------
[``docs/FEATURE_STATUS.md``](../docs/FEATURE_STATUS.md) 里除两条例外之外的能力都标注为
``Simulated`` —— 因为仓库的自动化证据只来自**内存仿真引擎**
（``omnilore.engine.RLSDatabaseEngine``），真实 PG 上没有针对这些能力的 CI 证据。
本脚本覆盖的那一条例外（真实 PostgreSQL RLS 策略与跨空间读拦截）**已由 CI 的
``integration-rls`` job 持续复跑**（自 2026-10-26 起连续 2 次绿灯）。

这个脚本就是补上那份证据的入口：它把你本地/自建的真实 PostgreSQL 拉起来做一组
RLS 探针，并且**只在真的跑过之后才说验证过**。环境不具备时它以非零码退出并说明
原因 —— **绝不假装验证过**（fails closed）。

探针设计（三条，前两条必须成对出现才构成证据）
----------------------------------------------
* **P1 策略已挂载**：至少 7 张核心表 ``rowsecurity = true``。
* **P2 越权读被过滤**：以 ``omnilore_gateway`` 角色、只授权 ``sp_bootstrap_personal``
  时，跨空间行**不可见**。
* **P3 同一行授权后可见**：把该空间加入授权后，同一行**变为可见**。
  P2 + P3 成对成立，才能排除「表里本来就没数据」这种假阳性。

用法
----
.. code-block:: bash

    export DATABASE_URL=postgresql://omnilore_gateway:<pwd>@localhost:5432/omnilore
    export OMNILORE_MIGRATOR_DSN=postgresql://omnilore_migrator:<pwd>@localhost:5432/omnilore
    python tools/verify_rls_pg.py      # 或：make verify-rls-pg

``OMNILORE_MIGRATOR_DSN`` 省略时回退到 ``DATABASE_URL``（自建库用同一个超级用户时够用）。

退出码
------
* ``0`` —— 三条探针全部通过（真实 PG 上拿到了 RLS 证据）。
* ``2`` —— **未验证**：缺 ``psql``、缺连接串、目标库**不是空库**、或 DDL/探针无法执行。
  不是失败，是没跑。
* ``3`` —— **已验证但结论为否**：RLS 探针没有给出预期结果。

为什么要求空库
--------------
本脚本会以 ``ON_ERROR_STOP=1`` 应用 ``sql/001`` + ``sql/002``（建表 + 种子）。目标库里
若已有本项目的表，必然在「约束已存在」处失败 —— 那不是 RLS 的结论，而是环境没准备好。
因此脚本先探测一次；发现非空就以 ``2`` 退出并给出「指向空库」的指引，
**绝不**自动 ``DROP SCHEMA`` 或清库（那可能删掉真实数据）。
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import List, Optional, Tuple

REPO_ROOT: Path = Path(__file__).resolve().parent.parent
DDL_FILES: Tuple[str, ...] = (
    "sql/001_omnilore_schema_and_rls.sql",
    "sql/002_omnilore_bootstrap_seed.sql",
)

# 探针专用空间：由 migrator（表属主，不受 RLS 约束）写入，再交给 gateway 角色去读。
PROBE_SPACE_ID: str = "sp_probe_hidden"
SEED_SPACE_ID: str = "sp_bootstrap_personal"
SEED_USER_ID: str = "00000000-0000-4000-8000-000000000001"

EXIT_OK: int = 0
EXIT_NOT_VERIFIED: int = 2
EXIT_VERIFIED_NEGATIVE: int = 3

# 本脚本会建的表：用来判断「目标库是不是空库」。只查名字，不查内容。
TARGET_TABLES: Tuple[str, ...] = (
    "spaces",
    "documents",
    "document_series",
    "document_chunks",
    "ingestion_tasks",
)


def _run_psql(dsn: str, sql: str, *, tuples_only: bool = True) -> Tuple[int, str, str]:
    """用 psql 执行一段 SQL，返回 (returncode, stdout, stderr)。

    Args:
        dsn: PostgreSQL 连接串。
        sql: 待执行的 SQL 文本（可以包含多条语句）。
        tuples_only: 是否加 `-tA` 只输出裸值，便于解析。

    Returns:
        三元组 `(returncode, stdout, stderr)`。任何**进程无法启动**的情况
        （找不到可执行文件、Windows 上命中 `.bat` 包装脚本等）都会返回
        returncode 127，而不是抛出 `OSError` —— 「跑不起来」属于**未验证**，
        不是程序崩溃，必须走同一条退出路径。
    """
    cmd: List[str] = ["psql", dsn, "-v", "ON_ERROR_STOP=1", "-q"]
    if tuples_only:
        cmd += ["-tA"]
    cmd += ["-c", sql]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except OSError as exc:
        return 127, "", f"无法启动 psql（{exc}）；请安装 PostgreSQL 客户端后重试。"
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def _resolve_psql() -> Optional[str]:
    """定位一个**可直接执行**的 psql，找不到或不合法时返回 None。

    Windows 上的坑：`shutil.which("psql")` 会按 `PATHEXT` 命中 `psql.bat` 之类的
    包装脚本，而 `subprocess.run`（CreateProcess）**不能**直接执行 `.bat`，
    结果是 `OSError` 崩溃成 exit 1 + traceback —— 把「没装客户端」伪装成了
    「验证失败」。因此这里额外校验扩展名。

    Returns:
        可执行文件路径；不可用时返回 None。
    """
    found = shutil.which("psql")
    if not found:
        return None
    suffix = Path(found).suffix.lower()
    if suffix not in ("", ".exe"):
        return None
    return found


def _preconditions() -> Optional[str]:
    """检查验证前置条件。

    Returns:
        None 表示条件具备；否则返回一段人读的**未验证**原因。
    """
    # 注意：不能只靠 shutil.which —— Windows 上它会命中无法直接执行的 .bat 包装脚本，
    # 详见 _resolve_psql() 的注释。
    if _resolve_psql() is None:
        return (
            "未找到可直接执行的 psql（PATH 中没有 PostgreSQL 客户端，或只找到了 "
            ".bat 之类的包装脚本）—— 无法在真实 PG 上验证。"
        )
    if not os.environ.get("DATABASE_URL", "").strip():
        return (
            "未设置 DATABASE_URL —— 无法在真实 PG 上验证。\n"
            "  示例：export DATABASE_URL=postgresql://omnilore_gateway:<pwd>@localhost:5432/omnilore"
        )
    missing = [rel for rel in DDL_FILES if not (REPO_ROOT / rel).is_file()]
    if missing:
        return f"缺少 DDL 文件：{missing}"
    return None


def _check_target_database_is_empty(migrator_dsn: str) -> Optional[str]:
    """确认目标库是**空库**（本脚本只会建表，不会、也不该清理既有对象）。

    为什么需要这一步：容器 `docker-entrypoint-initdb.d` 已经建过表时，再以
    ``ON_ERROR_STOP=1`` 应用 ``sql/001``，会在
    ``constraint "fk_document_series_current_doc" ... already exists`` 处失败 ——
    2026-10-24 的真实 PG 验证就撞过这个坑，绕过方式是新建一个空库 ``omnilore_verify``。

    Args:
        migrator_dsn: 具备读 `information_schema` 权限的连接串。

    Returns:
        None 表示目标库里没有本项目的表（可以安全建表）；否则返回一段人读的
        **未验证**原因（含「怎么指向一个空库」的指引）。
    """
    names = ", ".join(f"'{name}'" for name in TARGET_TABLES)
    sql = (
        "SELECT count(*) FROM information_schema.tables "
        f"WHERE table_schema = 'public' AND table_name IN ({names});"
    )
    code, out, err = _run_psql(migrator_dsn, sql)
    if code != 0:
        return (
            f"无法确认目标库是否为空（psql 退出码 {code}）：{err or '无输出'}。\n"
            "  本脚本只在**空库**上建表，宁可停下也不猜。"
        )
    try:
        existing = int(out.splitlines()[-1].strip())
    except (IndexError, ValueError):
        return f"无法解析「目标库是否已有表」的探测结果：{out!r}"
    if existing == 0:
        return None
    return (
        "目标库里已存在本项目的表（spaces / documents / document_series 等），"
        "而本脚本会以 ON_ERROR_STOP=1 重复应用 DDL —— 必然在「约束已存在」处失败。\n"
        "  请把连接串指向一个**空库**，例如：\n"
        "    CREATE DATABASE omnilore_verify;\n"
        "    export DATABASE_URL=postgresql://omnilore_gateway:<pwd>@localhost:5432/omnilore_verify\n"
        "    export OMNILORE_MIGRATOR_DSN=postgresql://omnilore_migrator:<pwd>@localhost:5432/omnilore_verify\n"
        "    make verify-rls-pg\n"
        "  本脚本**不会**自动 DROP SCHEMA 或清库 —— 那可能删掉你的真实数据。"
    )


def _apply_ddl(migrator_dsn: str) -> Optional[str]:
    """以 migrator 身份应用 sql/001 + sql/002（幂等）。

    Args:
        migrator_dsn: 具备 DDL 权限的连接串。

    Returns:
        None 表示成功；否则返回失败原因。
    """
    for rel in DDL_FILES:
        try:
            proc = subprocess.run(
                ["psql", migrator_dsn, "-v", "ON_ERROR_STOP=1", "-q", "-f", str(REPO_ROOT / rel)],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            return f"无法启动 psql 应用 {rel}（{exc}）。"
        if proc.returncode != 0:
            return (
                f"应用 {rel} 失败（psql 退出码 {proc.returncode}）。\n"
                f"  {proc.stderr.strip()}\n"
                "  提示：DDL 需要具备建表权限的连接串，可用 OMNILORE_MIGRATOR_DSN 单独指定。"
            )
    return None


def _seed_probe_rows(migrator_dsn: str) -> Optional[str]:
    """以表属主身份写入探针数据（属主不受 RLS 约束，因此这里绕不过策略是假象）。

    Args:
        migrator_dsn: 具备 DDL 权限的连接串。

    Returns:
        None 表示成功；否则返回失败原因。
    """
    sql = f"""
    INSERT INTO spaces (space_id, space_type, parent_space_id, vault_rel_path)
    VALUES ('{PROBE_SPACE_ID}', 'PERSONAL', NULL, 'spaces/{PROBE_SPACE_ID}')
    ON CONFLICT (space_id) DO NOTHING;

    INSERT INTO document_series (series_id, space_id, version_key, current_doc_id, category)
    SELECT '11111111-2222-3333-4444-555555555555', '{PROBE_SPACE_ID}', 'probe_series', NULL, '01_Work'
    WHERE NOT EXISTS (
        SELECT 1 FROM document_series WHERE series_id = '11111111-2222-3333-4444-555555555555'
    );

    INSERT INTO documents (
        doc_id, space_id, category, series_id, version_label, title, summary,
        wiki_rel_path, file_hash, content_revision, doc_status, review_status,
        is_current, created_by
    )
    SELECT '66666666-7777-8888-9999-000000000000', '{PROBE_SPACE_ID}', '01_Work',
           '11111111-2222-3333-4444-555555555555', 'v1.0', 'RLS 探针文档', '仅用于真实 PG RLS 探针',
           '{PROBE_SPACE_ID}/wiki/probe.md', repeat('0', 64), 1, 'PUBLISHED_CURRENT',
           'APPROVED', true, '{SEED_USER_ID}'
    WHERE NOT EXISTS (
        SELECT 1 FROM documents WHERE doc_id = '66666666-7777-8888-9999-000000000000'
    );
    """
    code, _out, err = _run_psql(migrator_dsn, sql)
    if code != 0:
        return f"写入探针数据失败：{err}"
    return None


def _cleanup_probe_rows(migrator_dsn: str) -> None:
    """删除探针数据（best effort：清理失败不影响已得出的结论，但会明确提示）。"""
    sql = f"""
    DELETE FROM documents WHERE doc_id = '66666666-7777-8888-9999-000000000000';
    DELETE FROM document_series WHERE series_id = '11111111-2222-3333-4444-555555555555';
    DELETE FROM spaces WHERE space_id = '{PROBE_SPACE_ID}';
    """
    code, _out, err = _run_psql(migrator_dsn, sql)
    if code != 0:
        print(f"[warn] 探针数据清理失败，请手动检查 {PROBE_SPACE_ID}：{err}")


def _count_visible(gateway_dsn: str, allowed_spaces: str) -> Optional[int]:
    """在指定授权下统计探针行的可见数量。

    Args:
        gateway_dsn: 以 `omnilore_gateway` 角色连接数据库的连接串。
        allowed_spaces: 写入 `app.allowed_read_spaces` 的空间列表（逗号分隔）。

    Returns:
        可见行数；SQL 执行失败时返回 None。
    """
    sql = f"""
    BEGIN;
    SET LOCAL ROLE omnilore_gateway;
    SET LOCAL app.current_user_id = '{SEED_USER_ID}';
    SET LOCAL app.allowed_read_spaces = '{allowed_spaces}';
    SET LOCAL app.allowed_read_scopes = '{allowed_spaces}:01_Work';
    SELECT count(*) FROM documents WHERE space_id = '{PROBE_SPACE_ID}';
    COMMIT;
    """
    code, out, err = _run_psql(gateway_dsn, sql)
    if code != 0:
        print(f"[error] 探针 SQL 失败：{err}")
        return None
    tail = [line for line in out.splitlines() if line.strip()]
    if not tail:
        print("[error] 探针 SQL 没有返回可解析的结果")
        return None
    try:
        return int(tail[-1].strip())
    except ValueError:
        print(f"[error] 探针结果无法解析为整数：{tail[-1]!r}")
        return None


def main() -> int:
    """执行真实 PG RLS 验证。

    Returns:
        进程退出码：`0` 全部通过；`2` 未验证（环境不具备）；`3` 已验证但结论为否。
    """
    reason = _preconditions()
    if reason is not None:
        print("=" * 70)
        print("真实 PostgreSQL RLS：未验证（NOT VERIFIED）")
        print("=" * 70)
        print(reason)
        print("\n在此之前，docs/FEATURE_STATUS.md 中所有 RLS 相关条目维持 `Simulated`。")
        print("这是环境限制，不是验证通过 —— 本脚本刻意不假装验证过。")
        return EXIT_NOT_VERIFIED

    gateway_dsn = os.environ["DATABASE_URL"].strip()
    migrator_dsn = os.environ.get("OMNILORE_MIGRATOR_DSN", "").strip() or gateway_dsn

    not_empty_error = _check_target_database_is_empty(migrator_dsn)
    if not_empty_error is not None:
        print("=" * 70)
        print("真实 PostgreSQL RLS：未验证（NOT VERIFIED）")
        print("=" * 70)
        print(not_empty_error)
        return EXIT_NOT_VERIFIED

    ddl_error = _apply_ddl(migrator_dsn)
    if ddl_error is not None:
        print("=" * 70)
        print("真实 PostgreSQL RLS：未验证（NOT VERIFIED）")
        print("=" * 70)
        print(ddl_error)
        return EXIT_NOT_VERIFIED

    seed_error = _seed_probe_rows(migrator_dsn)
    if seed_error is not None:
        print("=" * 70)
        print("真实 PostgreSQL RLS：未验证（NOT VERIFIED）")
        print("=" * 70)
        print(seed_error)
        return EXIT_NOT_VERIFIED

    results: List[Tuple[str, bool, str]] = []

    code, out, err = _run_psql(
        gateway_dsn,
        "SELECT count(*) FROM pg_tables t JOIN pg_class c ON c.relname = t.tablename "
        "WHERE c.relrowsecurity = true;",
    )
    if code != 0:
        print(f"[error] P1 无法执行：{err}")
        rls_tables: Optional[int] = None
    else:
        try:
            rls_tables = int(out.splitlines()[-1].strip())
        except (IndexError, ValueError):
            rls_tables = None
    results.append(("P1 至少 7 张核心表已启用 RLS", bool(rls_tables and rls_tables >= 7),
                    f"rowsecurity=true 的表：{rls_tables}"))

    denied = _count_visible(gateway_dsn, SEED_SPACE_ID)
    results.append((
        "P2 未授权时跨空间行不可见",
        denied == 0,
        f"仅授权 {SEED_SPACE_ID} 时可见 {PROBE_SPACE_ID} 行数：{denied}",
    ))

    allowed = _count_visible(gateway_dsn, f"{SEED_SPACE_ID},{PROBE_SPACE_ID}")
    results.append((
        "P3 授权后同一行变为可见（排除假阳性）",
        bool(allowed and allowed >= 1),
        f"追加授权 {PROBE_SPACE_ID} 后可见行数：{allowed}",
    ))

    _cleanup_probe_rows(migrator_dsn)

    print("=" * 70)
    print("真实 PostgreSQL RLS 探针结果")
    print("=" * 70)
    for name, ok, detail in results:
        print(f"[{'PASS' if ok else 'FAIL'}] {name} —— {detail}")

    passed = all(ok for _name, ok, _detail in results)
    if not passed:
        print("\n结论：已在真实 PostgreSQL 上验证，但结果与预期不符。")
        print("请修复后重跑；在此之前 RLS 条目仍只能标 `Simulated`。")
        return EXIT_VERIFIED_NEGATIVE

    print("\n结论：已在真实 PostgreSQL 上取得 RLS 证据。")
    print("请把本次输出贴进 Issue/PR，作为 RLS 条目升级为 `Tested-real` 的依据。")
    print("注意：本脚本已由 CI 的 integration-rls job 每次 push 复跑（持续证据）；")
    print("它只证明 sql/001 的 SQL 策略在真实 PG 上生效，不等于业务运行时已接入 PostgreSQL。")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
