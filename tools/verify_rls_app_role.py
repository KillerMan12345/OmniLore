#!/usr/bin/env python3
"""用**应用连接角色**（非超级用户、无 BYPASSRLS）在真实 PostgreSQL 上验证 RLS 三条。

为什么需要这个脚本
------------------
仓库原有的 ``tools/verify_rls_pg.py`` 用超级用户 ``omnilore_migrator`` 连接后
``SET ROLE omnilore_gateway`` 来验证策略本身——但连接角色是超用户，恰好踩中
「应用若以超用户连接会绕过 RLS」的红线，无法证明**应用会用的角色**连接时 RLS 生效。

本脚本用**应用连接角色**（如 ``omnilore_app``，``DATABASE_URL`` 中的用户）连接，并在
同一角色下完成三条验证：
  * P-a：未授权另一空间 → 查询返回 0 行；
  * P-b：授权后 → 同一条变为可见（排除「表本来就是空的」假阳性）；
  * P-c：连接角色既不是超级用户、也没有 BYPASSRLS。

用法
----
.. code-block:: bash

    export DATABASE_URL=postgresql://omnilore_app:<pwd>@<host>:5432/omnilore
    export OMNILORE_MIGRATOR_DSN=postgresql://omnilore_migrator:<pwd>@<host>:5432/omnilore  # 可选：预置探针行
    python tools/verify_rls_app_role.py

退出码：0=三条全部通过；2=未验证（环境不具备/探针无数据）；3=已验证但结论为否。

说明：本脚本不改任何项目代码，只做验证；探针行写入用属主/超用户连接串
（``OMNILORE_MIGRATOR_DSN``），未提供且库中已无探针数据时按「未验证」处理。
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import subprocess
import sys

REPO_ROOT: Path = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from omnilore.pg_runtime import PgRuntime, PgRuntimeError  # noqa: E402

PROBE_SPACE: str = "sp_probe_hidden"
SEED_SPACE: str = "sp_bootstrap_personal"
SEED_USER: str = "00000000-0000-4000-8000-000000000001"
SEED_SERIES: str = "11111111-2222-3333-4444-555555555555"
SEED_DOC: str = "66666666-7777-8888-9999-000000000000"


def _seed_probe_rows(migrator_dsn: str) -> None:
    """用属主/超用户连接串幂等写入探针行（sp_probe_hidden 一条 documents）。"""
    sql = f"""
    INSERT INTO spaces (space_id, space_type, parent_space_id, vault_rel_path)
    VALUES ('{PROBE_SPACE}', 'PERSONAL', NULL, 'spaces/{PROBE_SPACE}')
    ON CONFLICT (space_id) DO NOTHING;
    INSERT INTO document_series (series_id, space_id, version_key, current_doc_id, category)
    SELECT '{SEED_SERIES}', '{PROBE_SPACE}', 'probe_series', NULL, '01_Work'
    WHERE NOT EXISTS (SELECT 1 FROM document_series WHERE series_id = '{SEED_SERIES}');
    INSERT INTO documents (
        doc_id, space_id, category, series_id, version_label, title, summary,
        wiki_rel_path, file_hash, content_revision, doc_status, review_status,
        is_current, created_by
    )
    SELECT '{SEED_DOC}', '{PROBE_SPACE}', '01_Work', '{SEED_SERIES}', 'v1.0',
           'RLS probe (app role)', 'probe', '{PROBE_SPACE}/wiki/probe.md',
           repeat('0', 64), 1, 'PUBLISHED_CURRENT', 'APPROVED', true, '{SEED_USER}'
    WHERE NOT EXISTS (SELECT 1 FROM documents WHERE doc_id = '{SEED_DOC}');
    """
    proc = subprocess.run(
        ["psql", migrator_dsn, "-v", "ON_ERROR_STOP=1", "-q", "-c", sql],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        print(f"[warn] 探针行写入失败（不影响已有数据的验证）：{proc.stderr.strip()}")


def _probe_exists(app_dsn: str, migrator_dsn: str) -> bool:
    """探测探针行是否存在。

    用属主/超用户连接串（若提供）探测：应用角色直连会被 RLS 挡成 0 行，那恰恰是
    RLS 正常工作的表现，不能拿来判断「库里有没数据」。
    """
    probe_dsn = migrator_dsn or app_dsn
    proc = subprocess.run(
        ["psql", probe_dsn, "-tA", "-v", "ON_ERROR_STOP=1", "-c",
         f"SELECT count(*) FROM documents WHERE space_id='{PROBE_SPACE}';"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        print(f"[warn] 探针探测失败（{probe_dsn}）：{proc.stderr.strip()}")
        return False
    try:
        return int(proc.stdout.strip().splitlines()[-1].strip()) >= 1
    except (ValueError, IndexError):
        return False


async def _run(app_dsn: str) -> int:
    pg = await PgRuntime(app_dsn).connect()
    try:
        res = await pg.verify_rls_three(
            probe_space_id=PROBE_SPACE,
            authorized_space_id=SEED_SPACE,
            user_id=SEED_USER,
        )
    finally:
        await pg.close()

    pa = res["P_a_unauthorized_rows"]
    pb = res["P_b_authorized_rows"]
    attrs = res["role_attrs"]

    print("=" * 70)
    print("真实 PostgreSQL RLS（应用角色连接）验证结果")
    print("=" * 70)
    ok_a = pa == 0
    ok_b = bool(pb and pb >= 1)
    ok_c = (not attrs["rolsuper"]) and (not attrs["rolbypassrls"])
    print(f"[{'PASS' if ok_a else 'FAIL'}] P-a 未授权 {PROBE_SPACE} 时可见行数：{pa}（期望 0）")
    print(f"[{'PASS' if ok_b else 'FAIL'}] P-b 授权 {PROBE_SPACE} 后可见行数：{pb}（期望 ≥1）")
    print(
        f"[{'PASS' if ok_c else 'FAIL'}] P-c 连接角色 {attrs['rolname']}："
        f"superuser={attrs['rolsuper']}, bypassrls={attrs['rolbypassrls']}"
        f"（期望均 false）"
    )
    if ok_a and ok_b and ok_c:
        print("\n结论：已用应用角色在真实 PostgreSQL 上证明 RLS 生效。")
        return 0
    print("\n结论：RLS 验证未达预期。若 P-b=0，可能是探针空间无数据（空表假阳性），")
    print("请用 OMNILORE_MIGRATOR_DSN 预置探针行后重跑。")
    return 3


def main() -> int:
    app_dsn = os.environ.get("DATABASE_URL", "").strip()
    mig_dsn = os.environ.get("OMNILORE_MIGRATOR_DSN", "").strip()
    if not app_dsn:
        print("=" * 70)
        print("未验证：未设置 DATABASE_URL（应用角色连接串）。")
        print("示例：export DATABASE_URL=postgresql://omnilore_app:<pwd>@host:5432/omnilore")
        return 2
    if mig_dsn:
        _seed_probe_rows(mig_dsn)
    if not _probe_exists(app_dsn, mig_dsn):
        print("=" * 70)
        print("未验证：探针空间无数据，无法排除空表假阳性。")
        print("请提供 OMNILORE_MIGRATOR_DSN 预置探针行，或先在库里写入 sp_probe_hidden 的 documents。")
        return 2
    try:
        return asyncio.run(_run(app_dsn))
    except PgRuntimeError as exc:
        print("=" * 70)
        print(f"未验证（安全连接失败）：{exc}")
        print("这本身就是一条安全结论：该连接角色不适合作为应用运行时连接。")
        return 2


if __name__ == "__main__":
    sys.exit(main())
