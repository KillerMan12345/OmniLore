"""
Runtime Index: 让「文档 ↔ 原始资产」的归属关系跨进程存在
=======================================================
为什么需要这个模块
------------------
`audit-bundle` 要证明「这条答案来自这份原件、中间没被动过」，就必须知道
**某个 doc 的原始字节落在 CAS 的哪个文件上**。这条映射（`document_assets`）原本只在数据库里，
而默认引擎是**内存仿真引擎**：进程退出后结构化状态清空。结果就是
`omnilore demo`（进程 A）入库成功后，`omnilore audit-bundle`（进程 B）什么也导不出来 ——
一个只有测试能跑通的 CLI 命令，等于没做。

本模块把**最小必要映射**落盘到 `<data_dir>/ops/index/`：

* `assets.json` —— `asset_id → {sha256_hash, cas_rel_path, mime_type, size_bytes, ingested_at}`
* `document_assets.json` —— `doc_id → {文档元数据, page_sha256, asset_ids, source_names, source_locators}`

四条设计红线（违反任何一条都会让「可审计」变成新的黑盒）
--------------------------------------------------------
1. **索引是派生物，不是权威数据源。** 它可以从
   vault 的 Markdown frontmatter + `ops/cas/` 目录 + 真实 PostgreSQL 重建；
   删掉它不会丢任何原始证据。写坏的索引最多让导出变得保守（fail-closed），
   不会让校验通过。
2. **哈希校验绝不信任索引里的值。** 索引只提供「asset_id → CAS 相对路径」的**定位信息**；
   `audit_bundle` 仍然 `open("rb")` 读真实字节重算 SHA-256，再与索引里的 `sha256_hash` 比对。
   两边不一致 = `mismatch`，不是「以索引为准」。
3. **索引里不得有敏感信息。** 无 token、无 token_hash、无密码、无绝对路径；
   一切路径都是相对数据目录的 POSIX 路径。
4. **缺失索引 ≠ 数据损坏。** 索引不存在时 `audit-bundle` 仍然 fail-closed，但提示改成
   「请先运行 `omnilore demo` 或入库一份文档」，不把「还没入库」误报成「数据坏了」。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .textio import write_text_canonical

#: 索引的结构版本。字段增删必须升版本号 —— 消费方据此判断能否解析。
INDEX_SCHEMA_VERSION: int = 1

#: 索引目录（相对数据目录）。
INDEX_DIR_REL: str = "ops/index"
ASSETS_FILENAME: str = "assets.json"
DOCUMENT_ASSETS_FILENAME: str = "document_assets.json"

#: 只有这些表的变化才值得重写索引（其余事务完全不碰磁盘）。
TRACKED_TABLES: Tuple[str, ...] = ("documents", "document_series", "assets", "document_assets")


def index_dir(data_dir: Path) -> Path:
    """返回索引目录的绝对路径（不创建）。"""
    return Path(data_dir) / INDEX_DIR_REL


def assets_path(data_dir: Path) -> Path:
    """返回 `assets.json` 的绝对路径。"""
    return index_dir(data_dir) / ASSETS_FILENAME


def document_assets_path(data_dir: Path) -> Path:
    """返回 `document_assets.json` 的绝对路径。"""
    return index_dir(data_dir) / DOCUMENT_ASSETS_FILENAME


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    """原子写 JSON（先写 `.tmp` 再 `os.replace`），避免半截文件被读到。

    Args:
        path: 目标文件。
        payload: 可 JSON 序列化的字典。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    write_text_canonical(temporary, json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n")
    os.replace(temporary, path)


def build_index_payloads(
    data_dir: Path,
    state: Dict[str, Dict[str, Any]],
    *,
    updated_at: str,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """把内存引擎的状态投影成两份索引载荷（纯函数，便于测试）。

    Args:
        data_dir: 数据目录（用于把 vault 绝对路径转成相对路径）。
        state: `RLSDatabaseEngine._state` 形状的字典。
        updated_at: ISO 时间戳（由调用方注入，保证可测试）。

    Returns:
        `(assets_payload, document_assets_payload)`。
    """
    assets_payload: Dict[str, Any] = {
        "schema_version": INDEX_SCHEMA_VERSION,
        "updated_at": updated_at,
        "note": "运行时索引：派生物，权威数据在数据库与 vault/CAS 文件本身；删除后可从三者重建。",
        "assets": {},
    }
    for asset_id, row in sorted(state.get("assets", {}).items()):
        assets_payload["assets"][asset_id] = {
            "sha256_hash": row.get("sha256_hash"),
            # `storage_rel_path` 已是相对 CAS 根目录的路径，索引里再补一层 CAS 前缀，
            # 让消费方无需知道 CAS 根在哪。
            "cas_rel_path": f"ops/cas/{row.get('storage_rel_path')}",
            "mime_type": row.get("mime_type"),
            "size_bytes": row.get("byte_size"),
            "ingested_at": row.get("created_at"),
        }

    series_by_id: Dict[str, Dict[str, Any]] = state.get("document_series", {})
    documents_payload: Dict[str, Any] = {
        "schema_version": INDEX_SCHEMA_VERSION,
        "updated_at": updated_at,
        "note": "运行时索引：文档元数据 + 文档↔资产归属。派生物，不是权威数据源。",
        "documents": {},
    }
    for doc_id, row in sorted(state.get("documents", {}).items()):
        series = series_by_id.get(str(row.get("series_id") or ""), {})
        links: List[Dict[str, Any]] = [
            link for link in state.get("document_assets", {}).values() if link.get("doc_id") == doc_id
        ]
        documents_payload["documents"][doc_id] = {
            "doc_id": doc_id,
            "space_id": row.get("space_id"),
            "category": row.get("category"),
            "series_id": row.get("series_id"),
            "version_key": series.get("version_key"),
            "version_label": row.get("version_label"),
            "content_revision": row.get("content_revision"),
            "title": row.get("title"),
            "doc_status": row.get("doc_status"),
            "is_current": bool(row.get("is_current")),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
            "series_current_doc_id": series.get("current_doc_id"),
            "series_revision_number": series.get("revision_number"),
            "wiki_rel_path": row.get("wiki_rel_path"),
            "page_sha256": row.get("file_hash"),
            "asset_ids": [link.get("asset_id") for link in links],
            "source_names": {str(link.get("asset_id")): link.get("original_filename") for link in links},
            "source_locators": {str(link.get("asset_id")): link.get("source_locator") for link in links},
        }
    return assets_payload, documents_payload


def persist_runtime_index(
    data_dir: Path,
    state: Dict[str, Dict[str, Any]],
    *,
    updated_at: str,
) -> Tuple[Path, Path]:
    """把索引写盘（两次原子写）。

    Args:
        data_dir: 数据目录。
        state: 内存引擎状态。
        updated_at: ISO 时间戳。

    Returns:
        `(assets.json 路径, document_assets.json 路径)`。
    """
    assets_payload, documents_payload = build_index_payloads(data_dir, state, updated_at=updated_at)
    a_path = assets_path(data_dir)
    d_path = document_assets_path(data_dir)
    _atomic_write_json(a_path, assets_payload)
    _atomic_write_json(d_path, documents_payload)
    return a_path, d_path


def load_runtime_index(data_dir: Path) -> Optional[Dict[str, Dict[str, Any]]]:
    """读取索引；不存在或不可解析时返回 `None`（调用方据此 fail-closed）。

    Args:
        data_dir: 数据目录。

    Returns:
        `{"assets": {...}, "documents": {...}}`；任一文件缺失/损坏/版本不符时返回 `None`。
    """
    try:
        assets_raw = json.loads(assets_path(data_dir).read_text(encoding="utf-8"))
        documents_raw = json.loads(document_assets_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(assets_raw, dict) or not isinstance(documents_raw, dict):
        return None
    if assets_raw.get("schema_version") != INDEX_SCHEMA_VERSION:
        return None
    if documents_raw.get("schema_version") != INDEX_SCHEMA_VERSION:
        return None
    assets = assets_raw.get("assets")
    documents = documents_raw.get("documents")
    if not isinstance(assets, dict) or not isinstance(documents, dict):
        return None
    return {"assets": assets, "documents": documents}


__all__ = [
    "ASSETS_FILENAME",
    "DOCUMENT_ASSETS_FILENAME",
    "INDEX_DIR_REL",
    "INDEX_SCHEMA_VERSION",
    "TRACKED_TABLES",
    "assets_path",
    "build_index_payloads",
    "document_assets_path",
    "index_dir",
    "load_runtime_index",
    "persist_runtime_index",
]
