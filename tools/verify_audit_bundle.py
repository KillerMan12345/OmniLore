#!/usr/bin/env python
"""
离线端到端探针：证据包（`audit-bundle`）到底有没有在校验？
=========================================================
`make verify-audit` 跑的就是本脚本。它回答一个**只能靠实测回答**的问题：

> `audit-bundle.json` 里的 SHA-256，是「读磁盘字节重算出来的」，
> 还是「把数据库里的值抄了一遍」？

做法：先把链路跑通、断言全绿；然后**改掉 CAS 里一个字节**，再导出一次，断言：

1. 该资产被标成 `mismatch`（不是 `verified`，也不是被跳过）；
2. 整包结论变成 `mismatch`；
3. 进程退出码为 **3**（结论为否），不是 0。

最后把字节改回去、再导出一次，断言又回到 `verified` / 退出码 0 —— 这一步排除
「无论如何都失败」的假象（一个永远报错的校验器和没有校验器一样没用）。

设计约束
--------
* 全程离线：不需要数据库、不需要 API Key（`DASHSCOPE_API_KEY` 会被临时摘掉）。
* 全部跑在临时目录里，跑完删掉 —— 不碰用户数据目录。
* 退出码：`0` = 探针全部通过；`1` = 有断言不成立。

单独执行：

    python tools/verify_audit_bundle.py
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys
import tempfile
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from omnilore import OmniLoreEngine, OmniLoreError, OmniLoreMCPGateway  # noqa: E402
from omnilore.audit_bundle import (  # noqa: E402
    EXIT_AUDIT_NOT_VERIFIED,
    EXIT_AUDIT_VERIFIED,
    INTEGRITY_MISMATCH,
    INTEGRITY_VERIFIED,
    STATUS_MISMATCH,
    STATUS_MISSING,
    build_audit_bundle,
    bundle_exit_code,
    scan_text_of_data_sections,
    write_audit_bundle,
)
from omnilore.bootstrap import ensure_bootstrapped  # noqa: E402

DEMO_FILENAME = "audit_probe_note.md"
DEMO_CONTENT = (
    "# 证据包探针文档\n\n"
    "这段文字的用途只有一个：让 CAS 里出现一个真实的字节序列，"
    "这样「改一个字节」的篡改测试才有东西可改。\n"
)


class ProbeFailure(RuntimeError):
    """探针断言不成立（不是环境缺失，而是结论为否）。"""


def _require(condition: bool, message: str) -> None:
    """断言工具：不成立就抛 :class:`ProbeFailure`。

    Args:
        condition: 断言结果。
        message: 失败时打印的中文说明。
    """
    if not condition:
        raise ProbeFailure(message)


def _first_asset_check(bundle: Dict[str, Any]) -> Dict[str, Any]:
    """取出证据包里第一条 `source_asset` 检查（篡改测试的目标）。"""
    for check in bundle["integrity"]["checks"]:
        if check["kind"] == "source_asset":
            return check
    raise ProbeFailure("证据包里没有任何 source_asset 检查 —— 这条链路根本没校验原始证据")


def _asset_path(data_dir: Path, check: Dict[str, Any]) -> Path:
    """把检查记录里的相对路径还原成绝对路径。"""
    return data_dir / check["relative_path"]


async def _ingest(engine: OmniLoreEngine, token: str, space_id: str) -> str:
    """入库一份文档并返回 `doc_id`。"""
    gateway = OmniLoreMCPGateway(engine)
    result = await gateway.ingest_document_or_file(
        token,
        content_bytes=DEMO_CONTENT.encode("utf-8"),
        filename=DEMO_FILENAME,
        target_space_id=space_id,
        target_category="01_Work",
        version_key="audit_probe_series",
        version_label="v1.0",
    )
    doc_id = result["task"].get("result_doc_id")
    _require(bool(doc_id), "入库没有产出 doc_id，后续步骤无从进行")
    return str(doc_id)


async def run_probe() -> List[str]:
    """执行完整探针，返回 `[PASS]` 行列表。

    Returns:
        已通过的断言说明（英文 `[PASS]` 前缀沿用 `tools/verify_rls_pg.py` 的格式，
        便于 CI 用同一套 grep 断言）。

    Raises:
        ProbeFailure: 任一条断言不成立。
    """
    passes: List[str] = []
    saved_key = os.environ.pop("DASHSCOPE_API_KEY", None)
    try:
        with tempfile.TemporaryDirectory(prefix="omnilore_audit_probe_") as tmp:
            data_dir = Path(tmp).resolve() / "OmniLore_Data"
            engine = OmniLoreEngine(data_dir)
            boot = ensure_bootstrapped(engine, data_dir)
            token = boot.raw_token
            space_id = boot.space_id
            doc_id = await _ingest(engine, token, space_id)

            # ---- P1：正常导出，字段完整、哈希一致、退出码 0 --------------------
            bundle = build_audit_bundle(engine, token, doc_id=doc_id)
            document = bundle["documents"][0]
            for field in ("doc_id", "space_id", "version_key", "version_label", "content_revision"):
                _require(field in document, f"文档条目缺少字段 {field}")
            _require(
                bundle["integrity"]["status"] == INTEGRITY_VERIFIED,
                f"正常导出的结论应为 verified，实际 {bundle['integrity']['status']}",
            )
            _require(
                bundle_exit_code(bundle) == EXIT_AUDIT_VERIFIED,
                "正常导出的退出码应为 0",
            )
            asset_check = _first_asset_check(bundle)
            _require(
                asset_check["status"] == "verified" and asset_check["computed_sha256"] == asset_check["recorded_sha256"],
                "原始资产的哈希没有被核对上",
            )
            passes.append(
                f"P1 正常导出：字段完整、{len(bundle['integrity']['checks'])} 条检查全部 verified、退出码 0"
            )

            # ---- P2：改一个字节 → 必须 mismatch 且退出码非 0 -------------------
            cas_path = _asset_path(data_dir, asset_check)
            original_bytes = cas_path.read_bytes()
            _require(len(original_bytes) > 0, "CAS 资产为空，无法做篡改测试")
            cas_path.write_bytes(bytes([original_bytes[0] ^ 0xFF]) + original_bytes[1:])
            tampered = build_audit_bundle(engine, token, doc_id=doc_id)
            tampered_check = next(
                c for c in tampered["integrity"]["checks"] if c["check_id"] == asset_check["check_id"]
            )
            _require(
                tampered_check["status"] == STATUS_MISMATCH,
                f"改动一个字节后，检查状态应为 mismatch，实际 {tampered_check['status']}"
                " —— 说明 bundle 很可能只是把库里的值抄了一遍",
            )
            _require(
                tampered["integrity"]["status"] == INTEGRITY_MISMATCH,
                f"篡改后整包结论应为 mismatch，实际 {tampered['integrity']['status']}",
            )
            _require(
                bundle_exit_code(tampered) == EXIT_AUDIT_NOT_VERIFIED,
                f"篡改后退出码应为 {EXIT_AUDIT_NOT_VERIFIED}",
            )
            out_dir = Path(tmp) / "bundle_tampered"
            json_path, verify_path = write_audit_bundle(tampered, out_dir)
            _require(json_path.is_file() and verify_path.is_file(), "篡改后的证据包没有落盘")
            passes.append(
                "P2 篡改一个字节：该资产标为 mismatch、整包 mismatch、退出码 3（证明真的在校验）"
            )

            # ---- P3：改回去 → 回到 verified（排除「永远报错」的假象） ----------
            cas_path.write_bytes(original_bytes)
            restored = build_audit_bundle(engine, token, doc_id=doc_id)
            _require(
                restored["integrity"]["status"] == INTEGRITY_VERIFIED,
                "字节改回后应恢复为 verified —— 否则前面的 mismatch 不能归因于篡改",
            )
            _require(bundle_exit_code(restored) == EXIT_AUDIT_VERIFIED, "恢复后退出码应为 0")
            passes.append("P3 字节改回：结论恢复 verified、退出码 0（排除「永远失败」的假阳性）")

            # ---- P4：文件缺失 → 如实标 missing，仍非零退出 ---------------------
            original_for_missing = cas_path.read_bytes()
            cas_path.unlink()
            try:
                missing = build_audit_bundle(engine, token, doc_id=doc_id)
                missing_check = next(
                    c for c in missing["integrity"]["checks"] if c["check_id"] == asset_check["check_id"]
                )
                _require(
                    missing_check["status"] == STATUS_MISSING,
                    f"资产文件被删后应标为 missing，实际 {missing_check['status']}",
                )
                _require(
                    bundle_exit_code(missing) == EXIT_AUDIT_NOT_VERIFIED,
                    "资产缺失时退出码必须非零 —— 不能默默输出一个看起来正常的包",
                )
                passes.append("P4 资产缺失：如实标为 missing，退出码仍为 3（不静默跳过）")
            finally:
                cas_path.write_bytes(original_for_missing)

            # ---- P5：bundle 不含 token / 绝对路径 ------------------------------
            rendered = json_path.read_text(encoding="utf-8") + verify_path.read_text(encoding="utf-8")
            _require(token not in rendered, "证据包里出现了原始 token")
            data_text = scan_text_of_data_sections(restored)
            _require("token" not in data_text.lower(), "证据包数据区出现了 token 相关字段")
            _require(str(data_dir) not in data_text, "证据包数据区出现了绝对路径")
            passes.append("P5 敏感信息：数据区不含 token / 绝对路径（与测试同一套扫描口径）")

            # ---- P6：范围外/未授权时 fail-closed -------------------------------
            try:
                build_audit_bundle(engine, token, doc_id="00000000-0000-4000-8000-0000000000ff")
            except OmniLoreError:
                passes.append("P6 指定不存在的 doc_id：明确报错（AUDIT_TARGET_NOT_FOUND），不产出空证据包")
            else:
                raise ProbeFailure("指定不存在的 doc_id 竟然导出了证据包")
    finally:
        if saved_key is not None:
            os.environ["DASHSCOPE_API_KEY"] = saved_key
    return passes


def main() -> int:
    """执行探针并按 `tools/verify_rls_pg.py` 的格式打印结果。"""
    print("=" * 70)
    print("OmniLore 证据包探针（audit-bundle）")
    print("=" * 70)
    try:
        passes = asyncio.run(run_probe())
    except ProbeFailure as exc:
        print(f"[FAIL] {exc}")
        print("\n结论：证据包链路未通过校验。请勿据此宣称「可审计」已可用。")
        return 1
    except Exception as exc:  # noqa: BLE001 - 探针自身崩溃也要明确报出来
        print(f"[ERROR] 探针执行失败：{type(exc).__name__}: {exc}")
        return 1

    for line in passes:
        print(f"[PASS] {line}")
    print()
    print("结论：证据包里的哈希确实来自磁盘字节重算（改一个字节立刻 mismatch + 退出码 3）。")
    print("注意：本探针跑在内存仿真引擎上的一次性链路里，不等于「长期持续取证」。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
