"""
Audit Bundle Export (R-05m)
===========================
把「可审计」从**内部机制**变成**可交付的证据包**。

为什么需要这个模块
------------------
在本模块之前，OmniLore 的可审计能力全部停在系统内部：CAS 用 SHA-256 内容寻址、对账能跑、
RLS 探针能验证。但用户**带不走任何证据** —— 想证明「这条答案来自这份原件、中间没被动过」，
只能自己去翻数据库和文件系统。没有证据包，「可审计」对外只能讲原理，讲不了证据。

本模块导出一份 `audit-bundle.json`（+ 一份给人看的 `audit-bundle.verify.md`），包含：

1. 文档本体信息（`doc_id` / `space_id` / `version_key` / `version_label` / `content_revision`）；
2. 原始证据（每个 `source_asset` 的 SHA-256、原始文件名、字节数、入库时间）；
3. 版本链（同一 series 的版本演进 + 每版内容哈希）；
4. 可复核性（`how_to_verify`：拿哪个文件、用什么算法、得到什么哈希、与哪个字段比对）；
5. 生成信息（导出时间、工具版本、数据目录**名**）；
6. **不含**敏感信息（token / token_hash / 密码 / 绝对路径）。

红线：哈希必须真的重新算过
--------------------------
导出时**一定从磁盘读取真实字节重算 SHA-256**，再与数据库里记录的值比对：

* 不一致 → 该条检查标 `mismatch`，整包 `integrity.status=mismatch`，进程以 **非零码（3）** 退出；
* 文件不存在 → 标 `missing`，整包 `integrity.status=incomplete`，同样非零退出。

**绝不**把数据库里的值抄一遍就输出一个「看起来正常」的 bundle —— 那正是本模块要防的
「看起来可信但实际没校验」。调用方可以用
`make verify-audit`（`tools/verify_audit_bundle.py`）亲眼看到「改一个字节 → 立刻 mismatch」。

为什么需要结构化元数据（fail-closed）
------------------------------------
`document_assets`（文档 ↔ 资产的归属关系）只存在于数据库里。默认的内存仿真引擎
（`RLSDatabaseEngine`）**不跨进程持久化**，所以在一个全新进程里跑 `omnilore audit-bundle`
拿不到归属关系。这种情况下本模块**拒绝**基于残缺信息生成 bundle，抛
`AUDIT_BUNDLE_NO_METADATA` 并给出可执行的下一步 —— 一个字段缺失的「证据包」比没有更糟，
因为它会让人以为已经验证过。详见 `docs/THREAT_MODEL.md` 与 `docs/FEATURE_STATUS.md`。
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .engine import OmniLoreEngine, OmniLoreError, ResolvedScope
from .runtime_index import load_runtime_index
from .textio import write_text_canonical
from .version import __version__, __version_display__

#: bundle 自身的结构版本。字段增删必须升版本号 —— 消费方据此判断能否解析。
AUDIT_BUNDLE_SCHEMA_VERSION: str = "1.0"

#: 默认输出文件名（两个文件写进同一个目录）。
AUDIT_BUNDLE_FILENAME: str = "audit-bundle.json"
AUDIT_VERIFY_FILENAME: str = "audit-bundle.verify.md"

#: 单条检查的四种结果。`verified` 是唯一「通过」的状态。
STATUS_VERIFIED: str = "verified"
STATUS_MISMATCH: str = "mismatch"
STATUS_MISSING: str = "missing"
STATUS_UNREADABLE: str = "unreadable"

#: 整包结论。`mismatch` = 有字节被改过；`incomplete` = 有文件缺失/读不了。
INTEGRITY_VERIFIED: str = "verified"
INTEGRITY_MISMATCH: str = "mismatch"
INTEGRITY_INCOMPLETE: str = "incomplete"

#: 进程退出码：0 = 全部核对通过；3 = 已验证但结论为否（与 `tools/verify_rls_pg.py`
#: 的「结论为否 = 3」保持同一套语义）；1 = 导出本身失败（由 CLI 统一处理）。
EXIT_AUDIT_VERIFIED: int = 0
EXIT_AUDIT_NOT_VERIFIED: int = 3

#: 明确写入 bundle 的「不导出清单」，让读者知道哪些东西被刻意排除了。
EXCLUDED_FIELDS: Tuple[str, ...] = (
    "access_tokens.token_hash",
    "owner / member 原始 token",
    "ops/bootstrap.json 内容",
    "任何绝对路径（改用相对数据目录的 POSIX 路径）",
    "wiki 正文（只给字节哈希，证据包不复制内容；正文请直接读 vault）",
)

#: 计算哈希时的分块大小（大文件不会一次性读进内存）。
_READ_CHUNK_BYTES: int = 1 << 16

#: 元数据的来源。两者都只是「定位信息」，校验一律以磁盘字节为准。
METADATA_SOURCE_DATABASE: str = "database"
METADATA_SOURCE_RUNTIME_INDEX: str = "runtime_index"

#: 运行时索引里 CAS 路径带的前缀（便于消费方无需知道 CAS 根目录）。
_CAS_REL_PREFIX: str = "ops/cas/"


def _utc_now_iso() -> str:
    """返回秒级精度的 UTC 时间戳（`2026-10-24T12:00:00Z`）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def sha256_of_file(path: Path) -> Tuple[str, int]:
    """从磁盘读取**真实字节**计算 SHA-256，并返回文件字节数。

    刻意不做换行规范化：证据包校验的是「磁盘上那串字节有没有被动过」，
    任何文本层面的转换都会让校验失去意义（CRLF 事故就是这么发生的）。

    Args:
        path: 待校验文件的绝对路径。

    Returns:
        `(十六进制小写 sha256, 字节数)`。
    """
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_READ_CHUNK_BYTES), b""):
            digest.update(chunk)
            total += len(chunk)
    return digest.hexdigest(), total


def _safe_join(root: Path, rel_path: str) -> Path:
    """把相对路径拼到 `root` 下，并拒绝任何逃出 `root` 的路径。

    Args:
        root: 允许的根目录（CAS 目录或 vault 目录）。
        rel_path: 相对路径（POSIX 风格）。

    Returns:
        解析后的绝对路径（允许目标不存在 —— 缺失本身就是要如实上报的信息）。

    Raises:
        OmniLoreError: 路径逃出根目录时抛出 `AUDIT_PATH_ESCAPE`。
    """
    root_resolved = Path(root).resolve()
    candidate = (root_resolved / rel_path).resolve()
    if candidate != root_resolved and not candidate.is_relative_to(root_resolved):
        raise OmniLoreError(
            "AUDIT_PATH_ESCAPE",
            "证据包导出被拒绝：记录中的文件路径逃出了允许的根目录边界",
        )
    return candidate


def _to_data_dir_relative(engine: OmniLoreEngine, path: Path) -> str:
    """把绝对路径转成「相对数据目录」的 POSIX 路径（bundle 里绝不出现绝对路径）。"""
    try:
        rel = path.resolve().relative_to(Path(engine.storage.base_dir).resolve())
    except ValueError:
        # 理论上不会发生（CAS 与 vault 都在数据目录内）。真发生了就如实报错，
        # 而不是把绝对路径写进证据包。
        raise OmniLoreError(
            "AUDIT_PATH_OUTSIDE_DATA_DIR",
            "证据包导出被拒绝：待校验的文件不在数据目录内",
        ) from None
    return rel.as_posix()


def _hash_one(path: Path) -> Dict[str, Any]:
    """计算单个文件的哈希与大小，把异常翻译成**可上报的状态**而不是中断导出。

    Args:
        path: 目标文件绝对路径。

    Returns:
        `{"status", "computed_sha256", "computed_size_bytes", "error"}`。
        `status` 为 `verified` 之外的取值时，`computed_sha256` 为 `None`。
    """
    try:
        computed_sha256, size = sha256_of_file(path)
    except FileNotFoundError:
        return {
            "status": STATUS_MISSING,
            "computed_sha256": None,
            "computed_size_bytes": None,
            "error": "文件不存在（资产未落盘或被删除）",
        }
    except OSError as exc:
        return {
            "status": STATUS_UNREADABLE,
            "computed_sha256": None,
            "computed_size_bytes": None,
            "error": f"读取失败：{type(exc).__name__}",
        }
    return {
        "status": STATUS_VERIFIED,
        "computed_sha256": computed_sha256,
        "computed_size_bytes": size,
        "error": "",
    }


def _judge(recorded_sha256: Optional[str], hashed: Dict[str, Any]) -> Dict[str, Any]:
    """把「读到的状态」与「库里记录的值」合成一条最终结论。

    Args:
        recorded_sha256: 数据库中记录的哈希；缺失时视为无法核对。
        hashed: :func:`_hash_one` 的返回值。

    Returns:
        与 `hashed` 同构、但 `status` 已按比对结果修正的字典。
    """
    if hashed["status"] != STATUS_VERIFIED:
        return hashed
    if not recorded_sha256:
        return {
            **hashed,
            "status": STATUS_UNREADABLE,
            "error": "数据库没有记录可比对的哈希（元数据不完整）",
        }
    if hashed["computed_sha256"] != recorded_sha256:
        return {
            **hashed,
            "status": STATUS_MISMATCH,
            "error": "磁盘字节的 SHA-256 与记录的哈希不一致（内容被改动过）",
        }
    return hashed


def _verify_commands(relative_path: str) -> Dict[str, str]:
    """为一条检查生成「第三方脱离本系统自己验」的三条命令。

    命令里只出现**相对路径** —— 证据包不含绝对路径，命令也就不能含。
    """
    windows_path = relative_path.replace("/", "\\")
    return {
        "posix": f"sha256sum '{relative_path}'",
        "windows": f'certutil -hashfile "{windows_path}" SHA256',
        "python": (
            "python -c \"import hashlib,pathlib;"
            f"print(hashlib.sha256(pathlib.Path(r'{relative_path}').read_bytes()).hexdigest())\""
        ),
    }


class AuditBundleBuilder:
    """按调用方指定的范围（单文档 / 单空间 / 全部可读）导出证据包。

    元数据有两个来源，优先级明确：
    1. **数据库**（同进程内 / 未来的持久化后端）—— 最权威；
    2. **`ops/index/` 运行时索引**（`omnilore/runtime_index.py`）—— 让 `omnilore demo`
       在进程 A 入库后，进程 B 的 `audit-bundle` 仍能重建导出范围。索引是派生物，
       只提供「asset_id → CAS 相对路径」的定位信息，**哈希仍然以磁盘字节为准**。

    Attributes:
        engine: 持有 CAS、vault 与结构化元数据的引擎实例。
        scope: 已解析的调用方权限范围（用于逐行过滤，越权文档不会进包）。
        metadata_source: `database` 或 `runtime_index`。
    """

    def __init__(self, engine: OmniLoreEngine, scope: ResolvedScope):
        self.engine = engine
        self.scope = scope
        self.metadata_source: str = METADATA_SOURCE_DATABASE
        self._documents: Dict[str, Dict[str, Any]] = {}
        self._series: Dict[str, Dict[str, Any]] = {}
        self._assets: Dict[str, Dict[str, Any]] = {}
        self._links_by_doc: Dict[str, List[Dict[str, Any]]] = {}
        self._checks: List[Dict[str, Any]] = []
        self._omitted: List[Dict[str, str]] = []
        self._load_metadata()

    # -- 元数据装载 --------------------------------------------------------

    def _load_metadata(self) -> None:
        """按「数据库 → 运行时索引」的优先级装载元数据（两者都没有则留空）。"""
        db_state = self.engine.db._state
        if db_state["documents"]:
            self.metadata_source = METADATA_SOURCE_DATABASE
            self._documents = dict(db_state["documents"])
            self._series = dict(db_state["document_series"])
            self._assets = dict(db_state["assets"])
            for link in db_state["document_assets"].values():
                self._links_by_doc.setdefault(str(link.get("doc_id")), []).append(dict(link))
            return

        index = load_runtime_index(Path(self.engine.storage.base_dir))
        if index is None:
            return  # 交给 build() 抛 AUDIT_BUNDLE_NO_METADATA

        self.metadata_source = METADATA_SOURCE_RUNTIME_INDEX
        for asset_id, payload in index["assets"].items():
            cas_rel = str(payload.get("cas_rel_path") or "")
            if cas_rel.startswith(_CAS_REL_PREFIX):
                cas_rel = cas_rel[len(_CAS_REL_PREFIX):]
            self._assets[asset_id] = {
                "asset_id": asset_id,
                "sha256_hash": payload.get("sha256_hash"),
                "storage_rel_path": cas_rel,
                "mime_type": payload.get("mime_type"),
                "byte_size": payload.get("size_bytes"),
                "created_at": payload.get("ingested_at"),
            }

        for doc_id, payload in index["documents"].items():
            self._documents[doc_id] = {
                "doc_id": doc_id,
                "space_id": payload.get("space_id"),
                "category": payload.get("category"),
                "series_id": payload.get("series_id"),
                "version_label": payload.get("version_label"),
                "content_revision": payload.get("content_revision"),
                "title": payload.get("title"),
                "doc_status": payload.get("doc_status"),
                "is_current": bool(payload.get("is_current")),
                "created_at": payload.get("created_at"),
                "updated_at": payload.get("updated_at"),
                "wiki_rel_path": payload.get("wiki_rel_path"),
                "file_hash": payload.get("page_sha256"),
            }
            series_id = str(payload.get("series_id") or "")
            if series_id and series_id not in self._series:
                self._series[series_id] = {
                    "series_id": series_id,
                    "space_id": payload.get("space_id"),
                    "category": payload.get("category"),
                    "version_key": payload.get("version_key"),
                    "current_doc_id": payload.get("series_current_doc_id"),
                    "revision_number": payload.get("series_revision_number"),
                }
            source_names = payload.get("source_names") or {}
            source_locators = payload.get("source_locators") or {}
            for asset_id in payload.get("asset_ids") or []:
                self._links_by_doc.setdefault(doc_id, []).append({
                    "doc_id": doc_id,
                    "asset_id": asset_id,
                    "original_filename": source_names.get(asset_id),
                    "source_locator": source_locators.get(asset_id),
                })

    # -- 元数据选择 --------------------------------------------------------

    def _select_documents(self, doc_id: Optional[str], space_id: Optional[str]) -> List[Dict[str, Any]]:
        """挑出本次要导出的文档行（已按权限过滤）。

        Args:
            doc_id: 只导出该文档；None 表示不按文档过滤。
            space_id: 只导出该空间；None 表示不按空间过滤。

        Returns:
            文档行列表，按 `created_at` 升序（便于阅读版本演进）。

        Raises:
            OmniLoreError: 指定了 `doc_id` 但不存在或当前 token 无权读取。
        """
        selected: List[Dict[str, Any]] = []
        for row in self._documents.values():
            if doc_id and row["doc_id"] != doc_id:
                continue
            if space_id and row["space_id"] != space_id:
                continue
            if not self.scope.can_read(row["space_id"], row["category"]):
                self._omitted.append({"doc_id": row["doc_id"], "reason": "当前 token 无权读取该文档"})
                continue
            if row["doc_status"] == "PENDING_PUBLISH":
                # 未提交的文档 CAS 可能还没落盘；如实记入 omitted，不混进证据链。
                self._omitted.append({"doc_id": row["doc_id"], "reason": "PENDING_PUBLISH（尚未提交，无证据可导出）"})
                continue
            selected.append(row)

        if doc_id and not selected:
            # 不区分「不存在」与「无权」，避免用导出接口探测他人文档。
            raise OmniLoreError("AUDIT_TARGET_NOT_FOUND", "文档不存在，或当前 token 无权读取它")
        selected.sort(key=lambda d: (str(d.get("created_at") or ""), str(d["doc_id"])))
        return selected

    def _links_for(self, doc_id: str) -> List[Dict[str, Any]]:
        """取该文档的「文档 ↔ 资产」归属行（可能指向索引里缺失的资产，那是要上报的状态）。"""
        return self._links_by_doc.get(doc_id, [])

    # -- 检查记录 ----------------------------------------------------------

    def _record_check(self, check: Dict[str, Any]) -> None:
        """追加一条检查记录（保持顺序稳定，便于人读）。"""
        self._checks.append(check)

    def _check_asset(self, doc_row: Dict[str, Any], asset: Optional[Dict[str, Any]],
                     link: Dict[str, Any], asset_index: int) -> Dict[str, Any]:
        """校验一个原始资产，并返回 bundle 里的 `source_assets[i]` 条目。

        Args:
            doc_row: 该资产归属的文档行。
            asset: 资产记录；`None` 表示元数据里查不到它（此时如实标 `unreadable`，不跳过）。
            link: 「文档 ↔ 资产」归属行（带原始文件名与定位信息）。
            asset_index: 在 `source_assets` 列表里的下标（用于生成 `compare_to` 字段指针）。
        """
        compare_to = f"source_assets[{asset_index}].asset_hash"
        if asset is None:
            # 归属关系指向一个查不到的资产：不能静默丢掉，否则证据链会「少一块还看不出来」。
            missing_record = {
                "status": STATUS_UNREADABLE,
                "computed_sha256": None,
                "computed_size_bytes": None,
                "error": "元数据里缺少该资产的记录（无法定位 CAS 文件）",
            }
            self._record_check({
                "check_id": f"asset:{link.get('asset_id')}",
                "kind": "source_asset",
                "document_id": doc_row["doc_id"],
                "relative_path": "",
                "recorded_sha256": None,
                "computed_sha256": None,
                "recorded_size_bytes": None,
                "computed_size_bytes": None,
                "status": missing_record["status"],
                "error": missing_record["error"],
                "compare_to": compare_to,
            })
            return {
                "asset_id": link.get("asset_id"),
                "doc_id": doc_row["doc_id"],
                "asset_hash": None,
                "recorded_asset_hash": None,
                "original_filename": link.get("original_filename"),
                "size_bytes": None,
                "recorded_size_bytes": None,
                "ingested_at": None,
                "cas_relative_path": "",
                "mime_type": None,
                "source_locator": link.get("source_locator"),
                "integrity_status": missing_record["status"],
            }

        cas_root = Path(self.engine.storage.cas_dir)
        cas_path = _safe_join(cas_root, asset["storage_rel_path"])
        hashed = _judge(asset.get("sha256_hash"), _hash_one(cas_path))
        relative_path = _to_data_dir_relative(self.engine, cas_path)

        self._record_check({
            "check_id": f"asset:{asset['asset_id']}",
            "kind": "source_asset",
            "document_id": doc_row["doc_id"],
            "relative_path": relative_path,
            "recorded_sha256": asset.get("sha256_hash"),
            "computed_sha256": hashed["computed_sha256"],
            "recorded_size_bytes": asset.get("byte_size"),
            "computed_size_bytes": hashed["computed_size_bytes"],
            "status": hashed["status"],
            "error": hashed["error"],
            "compare_to": compare_to,
        })
        return {
            "asset_id": asset["asset_id"],
            "doc_id": doc_row["doc_id"],
            "asset_hash": hashed["computed_sha256"],
            "recorded_asset_hash": asset.get("sha256_hash"),
            "original_filename": link.get("original_filename"),
            "size_bytes": hashed["computed_size_bytes"],
            "recorded_size_bytes": asset.get("byte_size"),
            "ingested_at": asset.get("created_at"),
            "cas_relative_path": relative_path,
            "mime_type": asset.get("mime_type"),
            "source_locator": link.get("source_locator"),
            "integrity_status": hashed["status"],
        }

    def _check_page(self, doc_row: Dict[str, Any], doc_index: int) -> Dict[str, Any]:
        """校验知识页 Markdown 的字节哈希，并返回文档条目里的两个哈希字段。"""
        vault_root = Path(self.engine.storage.vault_dir)
        page_path = _safe_join(vault_root, doc_row["wiki_rel_path"])
        hashed = _judge(doc_row.get("file_hash"), _hash_one(page_path))
        relative_path = _to_data_dir_relative(self.engine, page_path)
        compare_to = f"documents[{doc_index}].page_sha256_recorded"

        self._record_check({
            "check_id": f"page:{doc_row['doc_id']}",
            "kind": "wiki_page",
            "document_id": doc_row["doc_id"],
            "relative_path": relative_path,
            "recorded_sha256": doc_row.get("file_hash"),
            "computed_sha256": hashed["computed_sha256"],
            "recorded_size_bytes": None,
            "computed_size_bytes": hashed["computed_size_bytes"],
            "status": hashed["status"],
            "error": hashed["error"],
            "compare_to": compare_to,
        })
        return {
            "page_sha256_computed": hashed["computed_sha256"],
            "page_sha256_recorded": doc_row.get("file_hash"),
            "page_relative_path": relative_path,
            "page_integrity_status": hashed["status"],
        }

    # -- 组装 --------------------------------------------------------------

    def build(self, *, doc_id: Optional[str] = None, space_id: Optional[str] = None) -> Dict[str, Any]:
        """导出证据包。

        Args:
            doc_id: 只导出该文档。
            space_id: 只导出该空间内的文档。

        Returns:
            可直接序列化为 JSON 的证据包字典。

        Raises:
            OmniLoreError: 范围内没有任何可导出的结构化元数据
                （`AUDIT_BUNDLE_NO_METADATA`），或目标文档不可见
                （`AUDIT_TARGET_NOT_FOUND`）。
        """
        doc_rows = self._select_documents(doc_id, space_id)
        if not doc_rows:
            raise OmniLoreError("AUDIT_BUNDLE_NO_METADATA", self._no_metadata_message())

        documents: List[Dict[str, Any]] = []
        source_assets: List[Dict[str, Any]] = []
        for index, doc_row in enumerate(doc_rows):
            series = self._series.get(str(doc_row.get("series_id") or ""), {})
            page = self._check_page(doc_row, index)
            documents.append({
                "doc_id": doc_row["doc_id"],
                "space_id": doc_row["space_id"],
                "category": doc_row.get("category"),
                "series_id": doc_row.get("series_id"),
                "version_key": series.get("version_key"),
                "version_label": doc_row.get("version_label"),
                "content_revision": doc_row.get("content_revision"),
                "title": doc_row.get("title"),
                "doc_status": doc_row.get("doc_status"),
                "is_current": bool(doc_row.get("is_current")),
                "created_at": doc_row.get("created_at"),
                "updated_at": doc_row.get("updated_at"),
                **page,
            })
            for link in self._links_for(doc_row["doc_id"]):
                asset = self._assets.get(str(link.get("asset_id") or ""))
                source_assets.append(self._check_asset(doc_row, asset, link, len(source_assets)))

        version_chains = self._build_version_chains(doc_rows)
        integrity = self._summarise_integrity(documents, source_assets)

        return {
            "bundle_schema_version": AUDIT_BUNDLE_SCHEMA_VERSION,
            "generated_at": _utc_now_iso(),
            "generator": {
                "tool": "omnilore",
                "command": "omnilore audit-bundle",
                "version_display": __version_display__,
                "version_pep440": __version__,
                "bundle_schema": f"audit-bundle/{AUDIT_BUNDLE_SCHEMA_VERSION}",
            },
            "data_dir": {
                # 只给目录名：绝对路径可能泄露用户名、内部挂载点等信息。
                "name": Path(self.engine.storage.base_dir).name,
                "absolute_path_omitted": True,
                "note": "bundle 内所有文件路径都相对该数据目录，可直接 `cd <数据目录>` 后复核。",
            },
            "metadata_source": {
                "source": self.metadata_source,
                "note": (
                    "database：元数据取自数据库（同进程或持久化后端）。"
                    "runtime_index：元数据取自 `ops/index/` 运行时索引 —— 它是**派生物**，"
                    "只提供 asset_id → CAS 相对路径的定位信息；哈希一律以磁盘真实字节为准。"
                ),
            },
            "scope": {
                "requested_doc_id": doc_id,
                "requested_space_id": space_id,
                "requester_user_id": self.scope.user_id,
                "profile_type": self.scope.profile_type.value,
                "readable_spaces": sorted(self.scope.read_spaces),
            },
            "documents": documents,
            "source_assets": source_assets,
            "version_chains": version_chains,
            "integrity": integrity,
            "how_to_verify": {
                "algorithm": "SHA-256",
                "algorithm_note": (
                    "对文件的**原始字节**直接计算 SHA-256（十六进制小写），"
                    "不做换行转换、不解码为文本、不排序 JSON。"
                ),
                "path_base": "bundle 内所有路径均相对数据目录，使用 POSIX 分隔符",
                "steps": [
                    {
                        "check_id": check["check_id"],
                        "kind": check["kind"],
                        "document_id": check["document_id"],
                        "relative_path": check["relative_path"],
                        "expected_sha256": check["recorded_sha256"],
                        "compare_to": check["compare_to"],
                        # 元数据里查不到资产的检查没有可执行命令（没有文件可算），如实留空。
                        "commands": _verify_commands(check["relative_path"]) if check["relative_path"] else {},
                        "result_in_this_bundle": check["status"],
                    }
                    for check in self._checks
                ],
                "exit_code_contract": {
                    str(EXIT_AUDIT_VERIFIED): "全部检查为 verified",
                    str(EXIT_AUDIT_NOT_VERIFIED): "至少一条 mismatch / missing / unreadable（结论为否）",
                },
                "what_this_proves": (
                    "① 导出时刻磁盘上的字节，与元数据里记录的 SHA-256（入库时写入）一致；"
                    "② 版本链里每一版的内容哈希可直接复核。"
                ),
                "what_this_does_not_prove": (
                    "① 不证明文件系统与数据库之间的事务原子性（见 docs/THREAT_MODEL.md 第 3 节第 1 条）；"
                    "② 不证明导出之后内容不会被改（请把 bundle 与文件一起带走，或在导出后自行再算一次）；"
                    "③ 不证明授权策略正确（那是 RLS 探针的事）。"
                ),
            },
            "excluded": {
                "fields": list(EXCLUDED_FIELDS),
                "note": "bundle 刻意不含 token / token_hash / 密码 / 绝对路径；也没有把 Markdown 正文复制进来。",
            },
            "omitted": self._omitted,
            "limitations": self._limitations(),
        }

    def _no_metadata_message(self) -> str:
        """「导不出来」时的说明 —— 必须区分「还没入库」与「索引坏了」。

        把两者混成一句「没有元数据」，用户会以为是数据损坏而不敢继续；实际上最常见的情况
        只是还没入库过。提示要能直接照做。
        """
        has_index = load_runtime_index(Path(self.engine.storage.base_dir)) is not None
        if has_index:
            return (
                "范围内没有可导出的文档：`ops/index/` 运行时索引存在，但其中没有匹配 "
                "--doc-id / --space-id 的文档，或当前 token 无权读取它们。"
                "请确认参数，或换一个有权限的 token 再试。拒绝生成空的证据包。"
            )
        return (
            "还没有可导出的证据：`ops/index/` 运行时索引不存在。"
            "先运行 `omnilore demo`（或通过 MCP / API 入库一份文档）再来导出 —— "
            "那一步会写入「文档 ↔ 原始资产」的映射。"
            "（若刚入库过却看到这条提示，请检查数据目录的 `ops/index/` 是否可写。）"
        )

    def _limitations(self) -> List[str]:
        """本包的已知界限（按元数据来源如实给出，不写死成一句套话）。"""
        limitations = [
            "bundle 不含正文：如需核对内容，请按 how_to_verify 直接读 vault 里的 Markdown。",
            "哈希只覆盖字节完整性，不覆盖语义正确性（文档内容是否写对，需要人看）。",
        ]
        if self.metadata_source == METADATA_SOURCE_RUNTIME_INDEX:
            limitations.insert(
                0,
                "元数据来自 `ops/index/` 运行时索引：它是**派生物**（可从 vault frontmatter + "
                "CAS 目录 + 数据库重建），不是权威数据源；哈希仍以磁盘真实字节重算为准。",
            )
        else:
            limitations.insert(
                0,
                "元数据来自数据库，未走 `ops/index/` 运行时索引。",
            )
        return limitations

    def _build_version_chains(self, doc_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """按 series 聚合版本链：版本序列 + 每版内容哈希。"""
        chains: Dict[str, Dict[str, Any]] = {}
        for doc_row in doc_rows:
            series_id = str(doc_row.get("series_id") or "")
            if not series_id:
                continue
            series = self._series.get(series_id, {})
            chain = chains.setdefault(series_id, {
                "series_id": series_id,
                "space_id": doc_row.get("space_id"),
                "category": doc_row.get("category"),
                "version_key": series.get("version_key"),
                "current_doc_id": series.get("current_doc_id"),
                "revision_number": series.get("revision_number"),
                "versions": [],
            })
            chain["versions"].append({
                "doc_id": doc_row["doc_id"],
                "version_label": doc_row.get("version_label"),
                "content_revision": doc_row.get("content_revision"),
                "page_sha256_recorded": doc_row.get("file_hash"),
                "is_current": bool(doc_row.get("is_current")),
                "doc_status": doc_row.get("doc_status"),
                "created_at": doc_row.get("created_at"),
            })
        for chain in chains.values():
            chain["versions"].sort(key=lambda v: (str(v.get("created_at") or ""), str(v["doc_id"])))
        return sorted(chains.values(), key=lambda c: str(c["version_key"] or c["series_id"]))

    def _summarise_integrity(self, documents: List[Dict[str, Any]],
                             source_assets: List[Dict[str, Any]]) -> Dict[str, Any]:
        """把逐条检查汇总成一个整包结论（`verified` / `mismatch` / `incomplete`）。"""
        counts = {status: 0 for status in (STATUS_VERIFIED, STATUS_MISMATCH, STATUS_MISSING, STATUS_UNREADABLE)}
        for check in self._checks:
            counts[check["status"]] = counts.get(check["status"], 0) + 1

        if counts[STATUS_MISMATCH]:
            status = INTEGRITY_MISMATCH
            summary = f"{counts[STATUS_MISMATCH]} 条检查的 SHA-256 与记录的哈希不一致：内容被改动过。"
        elif counts[STATUS_MISSING] or counts[STATUS_UNREADABLE]:
            status = INTEGRITY_INCOMPLETE
            summary = (
                f"{counts[STATUS_MISSING]} 条检查的文件缺失、"
                f"{counts[STATUS_UNREADABLE]} 条无法读取：证据不完整。"
            )
        else:
            status = INTEGRITY_VERIFIED
            summary = f"{counts[STATUS_VERIFIED]} 条检查全部通过：磁盘字节与记录的哈希一致。"

        return {
            "status": status,
            # `overall` 是语义别名（与 `status` 同值）：消费方读 `integrity.overall` 更直观，
            # 既有读 `status` 的代码不受影响。
            "overall": status,
            "summary": summary,
            "is_pass": status == INTEGRITY_VERIFIED,
            "checked_at": _utc_now_iso(),
            "counts": {
                "checks_total": len(self._checks),
                "verified": counts[STATUS_VERIFIED],
                "mismatch": counts[STATUS_MISMATCH],
                "missing": counts[STATUS_MISSING],
                "unreadable": counts[STATUS_UNREADABLE],
                "documents": len(documents),
                "source_assets": len(source_assets),
            },
            "checks": list(self._checks),
        }


def build_audit_bundle(
    engine: OmniLoreEngine,
    raw_token: str,
    *,
    doc_id: Optional[str] = None,
    space_id: Optional[str] = None,
) -> Dict[str, Any]:
    """导出证据包（对外入口）。

    Args:
        engine: 已持有元数据与磁盘文件的引擎实例。
        raw_token: 调用方 token（用于逐行权限过滤，不会被写进 bundle）。
        doc_id: 只导出该文档；与 `space_id` 可同时使用（取交集）。
        space_id: 只导出该空间。

    Returns:
        证据包字典，结构见模块 docstring。

    Raises:
        OmniLoreError: token 无效（`UNAUTHORIZED` 等）或范围内没有可导出的元数据。
    """
    scope = engine.auth.resolve_scope(raw_token)
    builder = AuditBundleBuilder(engine, scope)
    return builder.build(doc_id=doc_id, space_id=space_id)


def bundle_exit_code(bundle: Dict[str, Any]) -> int:
    """把证据包结论映射为进程退出码（0 = 全部通过，3 = 结论为否）。"""
    integrity = bundle.get("integrity") or {}
    return EXIT_AUDIT_VERIFIED if integrity.get("is_pass") else EXIT_AUDIT_NOT_VERIFIED


#: 「数据区」的键：这些小节里出现 token / 绝对路径就是事故，必须被扫到。
#: `excluded` / `how_to_verify` / `limitations` 是**说明文字**（它们本来就要写下
#: 「本包不含 token」这句话），因此不参与敏感词扫描 —— 扫说明文字只会得到假阳性。
SENSITIVE_SCAN_SECTIONS: Tuple[str, ...] = (
    "generator",
    "scope",
    "documents",
    "source_assets",
    "version_chains",
    "integrity",
)


def scan_text_of_data_sections(bundle: Dict[str, Any]) -> str:
    """返回证据包**数据区**的序列化文本，专门用于「不含敏感信息」的自动化断言。

    为什么单独提供这个函数：把扫描口径写在一处，测试与 `tools/verify_audit_bundle.py`
    才不会各扫各的（一处扫说明文字造成假阳性、另一处漏扫数据区造成假阴性）。

    Args:
        bundle: 证据包。

    Returns:
        数据区拼接后的 JSON 文本（`ensure_ascii=False`）。
    """
    payload = {section: bundle.get(section) for section in SENSITIVE_SCAN_SECTIONS}
    return json.dumps(payload, ensure_ascii=False, default=str)


def render_verification_markdown(bundle: Dict[str, Any]) -> str:
    """把证据包渲染成一份「人照着就能自己验」的 Markdown。

    为什么单独出一份 `.md`：JSON 适合机器消费，但「可复核性」的读者是人。
    威胁模型公开了「我们防不住什么」，那也必须公开「你怎么自己验」。

    Args:
        bundle: :func:`build_audit_bundle` 的返回值。

    Returns:
        Markdown 文本（结尾带换行）。
    """
    integrity = bundle["integrity"]
    counts = integrity["counts"]
    lines: List[str] = [
        "# OmniLore 证据包复核说明（audit-bundle.verify.md）",
        "",
        "> 这份文件由 `omnilore audit-bundle` 自动生成，与本目录下的 "
        f"`{AUDIT_BUNDLE_FILENAME}` 一一对应。",
        f"> 导出时间：`{bundle['generated_at']}` ｜ 工具版本：`{bundle['generator']['version_display']}` ｜ "
        f"bundle 结构版本：`{bundle['bundle_schema_version']}`",
        "",
        "## 1. 这份证据包在说什么",
        "",
        f"- 结论：**{integrity['status']}**（{'通过' if integrity['is_pass'] else '未通过'}）",
        f"- 说明：{integrity['summary']}",
        f"- 统计：文档 {counts['documents']} 份 ｜ 原始资产 {counts['source_assets']} 个 ｜ "
        f"检查 {counts['checks_total']} 条（verified {counts['verified']} / mismatch {counts['mismatch']} / "
        f"missing {counts['missing']} / unreadable {counts['unreadable']}）",
        "",
        "它证明的是：**导出这一刻，磁盘上的字节与入库时写进数据库的 SHA-256 一致。**",
        "它不证明的：文件系统与数据库之间的跨系统事务原子性、导出之后内容不再变化、授权策略正确。",
        "",
        "## 2. 怎么自己验（脱离 OmniLore）",
        "",
        "所有路径都**相对数据目录**，先进入该目录：",
        "",
        "```bash",
        f"cd <数据目录>   # 本 bundle 记录的数据目录名：{bundle['data_dir']['name']}",
        "```",
        "",
        "算法：**SHA-256，对文件原始字节直接计算**（不做换行转换、不解码为文本）。",
        "",
        "| # | 对象 | 文件（相对数据目录） | 期望哈希 | 与 JSON 的哪个字段比对 | 本次结论 |",
        "|---|---|---|---|---|---|",
    ]
    for index, step in enumerate(bundle["how_to_verify"]["steps"], start=1):
        expected = step["expected_sha256"] or "—"
        lines.append(
            f"| {index} | {step['kind']} | `{step['relative_path']}` | `{expected}` | "
            f"`{step['compare_to']}` | {step['result_in_this_bundle']} |"
        )

    lines += [
        "",
        "逐条执行的命令（任选其一，结果必须与「期望哈希」逐字相同）：",
        "",
    ]
    for index, step in enumerate(bundle["how_to_verify"]["steps"], start=1):
        lines += [
            f"### {index}. `{step['relative_path']}`",
            "",
            "```bash",
            f"# Linux / macOS / Git Bash\n{step['commands']['posix']}",
            f"# Windows\n{step['commands']['windows']}",
            f"# 不依赖 sha256sum / certutil 的兜底\n{step['commands']['python']}",
            "```",
            "",
            f"把输出与 `{step['compare_to']}` 比对；本 bundle 导出时的结论是 "
            f"`{step['result_in_this_bundle']}`。",
            "",
        ]

    lines += [
        "## 3. 退出码约定",
        "",
        "| 退出码 | 含义 |",
        "|---|---|",
        "| `0` | 全部检查为 `verified` |",
        "| `3` | 至少一条 `mismatch` / `missing` / `unreadable` —— **已验证但结论为否**，不是「程序崩了」 |",
        "| `1` | 导出本身失败（范围内没有元数据、token 无权、参数错） |",
        "",
        "## 4. 这份证据包刻意不含什么",
        "",
    ]
    for field in bundle["excluded"]["fields"]:
        lines.append(f"- {field}")
    lines += ["", bundle["excluded"]["note"], "", "## 5. 已知限制", ""]
    for limitation in bundle["limitations"]:
        lines.append(f"- {limitation}")
    if bundle.get("omitted"):
        lines += ["", "## 6. 被跳过的对象（如实列出，不静默丢弃）", ""]
        for item in bundle["omitted"]:
            lines.append(f"- `{item.get('doc_id', '')}`：{item.get('reason', '')}")
    lines.append("")
    return "\n".join(lines)


def write_audit_bundle(bundle: Dict[str, Any], out_dir: Path) -> Tuple[Path, Path]:
    """把证据包与复核说明写进 `out_dir`（必要时创建目录）。

    Args:
        bundle: :func:`build_audit_bundle` 的返回值。
        out_dir: 输出目录。

    Returns:
        `(json 路径, markdown 路径)`。
    """
    target_dir = Path(out_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    json_path = target_dir / AUDIT_BUNDLE_FILENAME
    verify_path = target_dir / AUDIT_VERIFY_FILENAME
    write_text_canonical(json_path, json.dumps(bundle, ensure_ascii=False, indent=2, default=str) + "\n")
    write_text_canonical(verify_path, render_verification_markdown(bundle))
    return json_path, verify_path
