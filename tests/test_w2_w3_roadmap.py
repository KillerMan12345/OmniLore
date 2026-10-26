"""
OmniLore W2 / W3 路线图回归测试
================================
三条需求各一组守卫，全部离线（不需要数据库、不需要 API Key）：

* **R-16（P2 主权）** `TestVaultSovereignty` / `TestSovereigntyConfigIsVisible`
  受保护 frontmatter 字段白名单可配（可整体关闭）；冲突副本落隐藏目录而不是散落 vault；
  这两项配置在启动时**回显生效值**（拼错的变量名也会被点名），静默失效变可见。
* **R-13（诚实标注）** `TestHonestFeatureStatus`
  README 的每一条能力都有四态标注，逐条与 `docs/FEATURE_STATUS.md` 对齐；
  徽章口径在 README / 矩阵 / CHANGELOG 三处一致；`make` 验证命令真实存在；
  **README 全文**（不只是「核心能力」小节）不允许出现无证据口径的能力宣称。
* **R-10a（贡献入口）** `TestContributionEntryPoints`
  6 套 Issue 模板以单选下拉为主（可统计），升级标签自动打；六条贡献路径确实绕开 `engine.py`。
* **R-08b（真实 PG 集成 CI）** `TestCiIntegrationConfig`
  `.github/workflows/ci.yml` 的 `integration-rls` job 在**配置层**被守住：镜像 tag 命名、
  `pg_isready` 健康检查、不挂 initdb 种子、以及「退出码 2/3 必须让 job 红」的失败语义。
  本组**只读 YAML 文本**，不执行 docker —— 它证明「配置正确」，不证明「CI 会一直绿」。
  （该 job 已于 2026-10-26 真实运行并连续 2 次绿灯，见 `docs/FEATURE_STATUS.md`。）
* **R-05m（证据包导出）** `TestAuditBundle` / `TestAuditBundleCli`
  `omnilore audit-bundle` 导出的 `audit-bundle.json` 必须**真的在校验**：改掉 CAS 里一个字节，
  结论就要变成 `mismatch`、退出码变成 3。这一组是「可审计」从原理变成证据的那一步。
* **R-08b 补课（21 条 RLS 策略注释）** `TestRlsPolicyComments`
  每条策略上方必须有四段注释（保护什么 / 依赖哪个维度 / 防什么 / 不覆盖什么），
  且策略逻辑本身被钉住不许悄悄改 —— 那 21 条是真实 PG 验证的基线。

为什么这些也要写成测试：本项目的头号风险是「宣称与证据不匹配」。
靠人肉同步文档与代码必然失败 —— 所以把它变成测试。
"""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import asyncio
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Dict, List, Tuple
from unittest import mock
import unittest

import yaml

from omnilore import (
    DEFAULT_PROTECTED_FRONTMATTER_KEYS,
    MembershipRecord,
    OmniLoreEngine,
    OmniLoreError,
    OmniLoreMCPGateway,
    OperationType,
    ProfileType,
    RoleType,
    SpaceRecord,
    SpaceType,
    build_conflict_copy_rel_path,
    get_conflict_copy_dir,
    parse_conflict_copy_dir,
    parse_protected_frontmatter_keys,
)
from omnilore.audit_bundle import (
    AUDIT_BUNDLE_FILENAME,
    AUDIT_VERIFY_FILENAME,
    EXIT_AUDIT_NOT_VERIFIED,
    EXIT_AUDIT_VERIFIED,
    INTEGRITY_MISMATCH,
    INTEGRITY_VERIFIED,
    STATUS_MISSING,
    STATUS_MISMATCH,
    build_audit_bundle,
    bundle_exit_code,
    render_verification_markdown,
    scan_text_of_data_sections,
    write_audit_bundle,
)
from omnilore.bootstrap import ensure_bootstrapped
from omnilore.cli import main as cli_main

REPO_ROOT: Path = Path(__file__).resolve().parent.parent
README_PATH: Path = REPO_ROOT / "README.md"
CHANGELOG_PATH: Path = REPO_ROOT / "CHANGELOG.md"
MATRIX_PATH: Path = REPO_ROOT / "docs" / "FEATURE_STATUS.md"
THREAT_MODEL_PATH: Path = REPO_ROOT / "docs" / "THREAT_MODEL.md"
MAKEFILE_PATH: Path = REPO_ROOT / "Makefile"
CI_WORKFLOW_PATH: Path = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# pgvector 官方镜像的 tag 规则：`<pgvector版本>-pg<PG主版本>`（如 `0.8.0-pg16`）。
# 反过来写（如 `pg16-v0.8.0`）在 Docker Hub 上**不存在**，会让整个 job 卡在 pull 阶段。
PGVECTOR_IMAGE_TAG_PATTERN: str = r"^\d+\.\d+\.\d+-pg\d+(-[a-z0-9]+)*$"

# 四态徽章是**封闭词表**：不允许出现第五种状态（例如 `Stable` / `Beta`）。
FOUR_STATES: Tuple[str, ...] = ("Implemented", "Tested-real", "Simulated", "Planned")

# 裸宣称守卫的能力词表：README 里出现这些词的地方，必须**就地**给出证据口径。
# 这份表刻意只收「能力技术名词」，不收「检索」「审计」这类日常词 —— 后者会让守卫
# 变成噪音，而噪音守卫等于没有守卫。新增一项能力时，第一个技术名词请加进来。
CAPABILITY_CLAIM_KEYWORDS: Tuple[str, ...] = (
    "混合检索",
    "BM25",
    "1024 维",
    "向量",
    "向量召回",
    "RLS",
    "行级安全",
    "CAS",
    "SHA-256",
    "ltree",
    "脱敏",
    "遥测",
    "n8n",
    "零停机",
    "双向同步",
    "pgvector",
    "MCP 工具",
    "加密",
    "多租户",
    "状态机",
    "版本控制",
)

# 「证据口径」：四态徽章，或明确的对冲／否定／边界措辞。
# 只有其中任意一种出现，才算「这条能力有交代」。
EVIDENCE_TOKENS: Tuple[str, ...] = FOUR_STATES + (
    "无证据",
    "没有 CI 证据",
    "没有真实",
    "尚未",
    "未实现",
    "不承诺",
    "不支持",
    "不读",
    "不保证",
    "未验证",
    "无法",
    "不是",
    "只是",
    "没有",
)


def _read(path: Path) -> str:
    """以 UTF-8 读取文本文件。"""
    return path.read_text(encoding="utf-8")


def _markdown_section(text: str, heading: str) -> str:
    """截取 Markdown 中从 `heading` 到下一个同级标题之间的内容。

    Args:
        text: Markdown 全文。
        heading: 起始标题（如 `## 核心能力`）。

    Returns:
        该小节的文本；找不到起始标题时返回空串。
    """
    start = text.find(heading)
    if start == -1:
        return ""
    nxt = text.find("\n## ", start + 1)
    return text[start:nxt] if nxt != -1 else text[start:]


def _load_probe_module():
    """把 `tools/verify_rls_pg.py` 当模块加载进来，便于用 mock 覆盖 `_run_psql`。

    `tools/` 不是包（没有 `__init__.py`），且这个脚本是给人直接执行的，所以不能用
    `import tools.verify_rls_pg`。用 importlib 按路径加载是唯一不需要改动仓库结构的办法。

    Returns:
        已加载的模块对象（每次调用都是新实例，mock 不会互相污染）。
    """
    spec = importlib.util.spec_from_file_location(
        "verify_rls_pg_under_test", REPO_ROOT / "tools" / "verify_rls_pg.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _is_table_separator(row: str) -> bool:
    """判断是否是 Markdown 表格的分隔行（`|---|---|`）。"""
    return bool(re.fullmatch(r"\|[\s\-:|]+\|", row))


def _needs_evidence(text: str) -> bool:
    """判断一段文本是否「提到了能力、却没有给出任何证据口径」。

    Args:
        text: 一行或一个「宣称单元」的正文。

    Returns:
        True 表示这是一条需要补证的裸宣称。
    """
    return (
        any(keyword in text for keyword in CAPABILITY_CLAIM_KEYWORDS)
        and not any(token in text for token in EVIDENCE_TOKENS)
    )


def _claim_blocks(text: str) -> List[Tuple[int, str]]:
    """把 Markdown 切成「宣称单元」，用于全文裸宣称扫描。

    一个宣称单元 = 一个非空段落 + **紧贴其后**的 blockquote 注释。之所以要合并
    blockquote：本仓库的写法是「段落在前、证据/边界注释紧跟在后的 `>` 引用块」，
    两者在语义上是同一句话；不合并就会把「有交代」误判成裸宣称。

    Args:
        text: Markdown 全文。

    Returns:
        `(起始行号, 单元正文)` 列表。代码块（``` 围栏内）与标题行被排除 ——
        代码块里出现 `pgvector` 之类的词是示例，不是宣称；标题只是分节。
    """
    lines = text.splitlines()
    blocks: List[Tuple[int, List[str]]] = []
    current: List[str] = []
    start = 0
    in_fence = False
    for line_no, line in enumerate(lines, start=1):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            if current:
                blocks.append((start, current))
                current = []
            continue
        if in_fence:
            continue
        if not stripped:
            if current:
                blocks.append((start, current))
                current = []
            continue
        if not current:
            start = line_no
        current.append(line)
    if current:
        blocks.append((start, current))

    merged: List[Tuple[int, str]] = []
    index = 0
    while index < len(blocks):
        block_start, block_lines = blocks[index]
        next_index = index + 1
        while next_index < len(blocks) and all(
            candidate.lstrip().startswith(">") for candidate in blocks[next_index][1]
        ):
            block_lines = block_lines + [""] + blocks[next_index][1]
            next_index += 1
        merged.append((block_start, "\n".join(block_lines)))
        index = next_index
    return [(line_no, body) for line_no, body in merged if not body.lstrip().startswith("#")]


class TestVaultSovereignty(unittest.IsolatedAsyncioTestCase):
    """R-16：笔记主权归用户 —— 受保护字段可配、冲突副本不污染 vault。"""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "OmniLore_Data"
        self.engine = OmniLoreEngine(self.root)
        self.mcp = OmniLoreMCPGateway(self.engine)

        self.space_id = "sp_personal_alice"
        self.user_id = "11111111-1111-1111-1111-111111111111"
        self.engine.auth.register_space(
            SpaceRecord(
                space_id=self.space_id,
                space_type=SpaceType.PERSONAL,
                parent_space_id=None,
                vault_rel_path=f"spaces/{self.space_id}",
            )
        )
        self.engine.auth.grant_membership(
            MembershipRecord(
                membership_id="m1",
                user_id=self.user_id,
                space_id=self.space_id,
                role=RoleType.ADMIN,
                allowed_categories={"01_Work", "02_Life", "03_Family", "04_Health_Archive"},
            )
        )
        self.token = "tok_alice_omni_secret"
        self.engine.auth.issue_token(
            self.token,
            user_id=self.user_id,
            profile_type=ProfileType.OMNI,
            allowed_read_spaces={self.space_id},
            allowed_write_spaces={self.space_id},
            allowed_categories={"01_Work", "02_Life", "03_Family", "04_Health_Archive"},
        )

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def _ingest_note(self, filename: str = "sovereignty_note.md") -> Tuple[str, str, str]:
        """收录一份文档并返回 `(doc_id, wiki_rel_path, 磁盘上的 Markdown)`。"""
        res = await self.mcp.ingest_document_or_file(
            self.token,
            content_bytes="主权测试正文：这份笔记属于用户，不属于系统。".encode("utf-8"),
            filename=filename,
            target_space_id=self.space_id,
            target_category="01_Work",
            version_key="sovereignty_test",
        )
        doc_id = res["task"]["result_doc_id"]
        doc_row = self.engine.db._state["documents"][doc_id]
        wiki_rel = doc_row["wiki_rel_path"]
        return doc_id, wiki_rel, self.engine.storage.read_wiki_by_record(self.space_id, wiki_rel)

    async def test_default_protected_fields_match_the_readme_declaration(self) -> None:
        """README 首屏声明的系统字段数量与默认白名单必须逐字一致。"""
        readme = _read(README_PATH)
        claimed = re.findall(r"(\d+) 个系统字段", readme)
        self.assertTrue(claimed, "README 未声明系统字段数量；若刻意移除请同步删除本断言")
        for value in claimed:
            self.assertEqual(
                int(value), len(DEFAULT_PROTECTED_FRONTMATTER_KEYS),
                f"README 宣称 {value} 个系统字段，实际默认白名单 {len(DEFAULT_PROTECTED_FRONTMATTER_KEYS)} 个",
            )
        # 声明里列出的每一个字段名都必须是真实存在的字段（不许写错名字糊弄人）
        for key in DEFAULT_PROTECTED_FRONTMATTER_KEYS:
            self.assertIn(f"`{key}`", readme, f"README 未列出系统字段 {key}")

    async def test_protected_whitelist_can_be_narrowed_and_disabled(self) -> None:
        """白名单可收窄、可整体关闭；`doc_id` 作为身份锚点永不被放开。"""
        self.assertEqual(parse_protected_frontmatter_keys(None), set(DEFAULT_PROTECTED_FRONTMATTER_KEYS))
        self.assertEqual(parse_protected_frontmatter_keys("doc_id,space_id"), {"doc_id", "space_id"})
        for off_token in ("none", "OFF", "0", "false", "  "):
            self.assertEqual(
                parse_protected_frontmatter_keys(off_token), {"doc_id"},
                f"{off_token!r} 应当表示「整体关闭保护」，仅保留 doc_id",
            )
        # 红线守卫：`doc_id` 必须由**每条**分支自动补回，而不是「恰好某个分支写了并集」。
        # 若有人把任意分支的 `keys | {doc_id}` 改成裸 `keys`，下面的断言必须变红。
        for raw_value in ("space_id", "space_id,version_label", "review_status", None, "none"):
            resolved = parse_protected_frontmatter_keys(raw_value)
            self.assertIn(
                "doc_id", resolved,
                f"取值 {raw_value!r} 解析后丢掉了身份锚点 doc_id（放开了等于允许文本编辑改写身份）",
            )

    async def test_unknown_protected_field_name_fails_loudly(self) -> None:
        """配置写错字段名必须响亮失败，而不是被静默当成「关闭保护」。"""
        with self.assertRaises(OmniLoreError):
            parse_protected_frontmatter_keys("doc_id,not_a_real_field")

    async def test_narrowing_the_whitelist_stops_rejecting_user_edits(self) -> None:
        """收窄白名单后，改被放开的字段不再被拒绝 —— 主权真的还给了用户。"""
        _doc_id, wiki_rel, initial_md = await self._ingest_note()
        tampered = initial_md.replace("version_label: v1.0", "version_label: v9.9")
        self.assertNotEqual(tampered, initial_md, "前置条件失败：未能改到 version_label")

        # 默认配置：version_label 受保护 → 拒绝
        default_res = await self.engine.sync_obsidian_markdown_edit(
            self.token, self.space_id, wiki_rel, tampered
        )
        self.assertEqual(default_res["status"], "REJECTED_PROTECTED_FIELD_MODIFICATION")
        self.assertEqual(default_res["rejected_fields"], ["version_label"])

        # 收窄到只剩身份锚点：同一个改动被接受
        with mock.patch.dict(os.environ, {"OMNILORE_PROTECTED_FRONTMATTER": "doc_id"}):
            narrowed_res = await self.engine.sync_obsidian_markdown_edit(
                self.token, self.space_id, wiki_rel, tampered
            )
        self.assertEqual(narrowed_res["status"], "SYNCED")

    async def test_conflict_copy_lands_in_hidden_directory_not_in_the_vault(self) -> None:
        """冲突副本只出现在隐藏目录，vault 里不再散落 `.conflict_r*.md`。"""
        _doc_id, wiki_rel, initial_md = await self._ingest_note()
        wiki_dir = self.engine.storage.vault_dir / self.space_id / "wiki"

        # 先把 revision 推到 2，再拿 revision=1 的过期副本制造冲突
        edited = initial_md.replace("主权测试正文", "主权测试正文（第一次修改）")
        first = await self.engine.sync_obsidian_markdown_edit(self.token, self.space_id, wiki_rel, edited)
        self.assertEqual(first["status"], "SYNCED")
        self.assertEqual(first["content_revision"], 2)

        stale = initial_md.replace("主权测试正文", "另一台设备的并发修改")
        conflict = await self.engine.sync_obsidian_markdown_edit(self.token, self.space_id, wiki_rel, stale)
        self.assertEqual(conflict["status"], "CONFLICT_COPY_CREATED")

        conflict_rel = conflict["conflict_rel_path"]
        hidden_dir = get_conflict_copy_dir()
        self.assertIn(f"/wiki/{hidden_dir}/", conflict_rel)
        self.assertIn(".conflict_r1.md", conflict_rel)
        self.assertTrue(
            (self.engine.storage.vault_dir / conflict_rel).is_file(),
            f"冲突副本未落盘：{conflict_rel}",
        )
        # 旧行为（散落在原文件旁边）必须消失
        sibling = wiki_dir / Path(wiki_rel).name.replace(".md", ".conflict_r1.md")
        self.assertFalse(sibling.exists(), f"冲突副本仍然污染 vault：{sibling}")

    async def test_conflict_directory_is_configurable(self) -> None:
        """隐藏目录可以通过 `OMNILORE_CONFLICT_DIR` 改掉。"""
        self.assertEqual(parse_conflict_copy_dir(None), ".omnilore/conflicts")
        self.assertEqual(parse_conflict_copy_dir("_omnilore/.conflicts"), "_omnilore/.conflicts")

        _doc_id, wiki_rel, initial_md = await self._ingest_note()
        edited = initial_md.replace("主权测试正文", "主权测试正文（第一次修改）")
        await self.engine.sync_obsidian_markdown_edit(self.token, self.space_id, wiki_rel, edited)

        stale = initial_md.replace("主权测试正文", "另一台设备的并发修改")
        with mock.patch.dict(os.environ, {"OMNILORE_CONFLICT_DIR": "_omnilore/.conflicts"}):
            conflict = await self.engine.sync_obsidian_markdown_edit(
                self.token, self.space_id, wiki_rel, stale
            )
        self.assertEqual(conflict["status"], "CONFLICT_COPY_CREATED")
        self.assertIn("/wiki/_omnilore/.conflicts/", conflict["conflict_rel_path"])

    async def test_conflict_directory_rejects_traversal_and_bad_wiki_paths(self) -> None:
        """冲突目录与 wiki 路径仍然受文件网关约束，配置不能拿来穿越。"""
        with self.assertRaises(OmniLoreError):
            parse_conflict_copy_dir("../escape")
        with self.assertRaises(OmniLoreError):
            parse_conflict_copy_dir("\x00evil")
        with self.assertRaises(OmniLoreError):
            build_conflict_copy_rel_path("not-a-wiki-path.md", 1)
        self.assertEqual(
            build_conflict_copy_rel_path("sp_x/wiki/note.md", 3, "hidden/conflicts"),
            "sp_x/wiki/hidden/conflicts/note.conflict_r3.md",
        )


class TestSovereigntyConfigIsVisible(unittest.TestCase):
    """R-16 / P1-1：主权配置必须**可见** —— 启动回显 + 拼错变量名点名。

    为什么单开一组：`omnilore bootstrap` 是唯一的人工启动入口，回显必须走**真实 CLI**，
    而不是直接调 `render_sovereignty_notice()` —— 后者只证明函数会拼字符串，证明不了
    「用户真的看得见」。因此这里走 `omnilore.cli.main()`，分别收集 stdout / stderr。
    """

    def _run_bootstrap(self, overrides: Dict[str, str]) -> Tuple[int, str, str]:
        """在受控环境下跑一次 `omnilore bootstrap --quiet`，返回 `(rc, stdout, stderr)`。

        Args:
            overrides: 本次运行要注入的环境变量（覆盖同名项；其余 OMNILORE_* 一律清空，
                避免开发者本机环境影响断言）。

        Returns:
            三元组 `(returncode, stdout, stderr)`。
        """
        env: Dict[str, str] = {
            key: value for key, value in os.environ.items()
            if not key.startswith("OMNILORE_")
        }
        env.pop("DASHSCOPE_API_KEY", None)
        env.update(overrides)
        out, err = io.StringIO(), io.StringIO()
        # 只摘掉 OMNILORE_* 与 DASHSCOPE_API_KEY，不整表 clear —— clear 掉 PATH/TEMP
        # 会让 bootstrap 内部的路径处理在 Windows 上莫名其妙地失败。
        saved = {key: os.environ.pop(key) for key in list(os.environ) if key.startswith("OMNILORE_")}
        saved_key = os.environ.pop("DASHSCOPE_API_KEY", None)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                data_dir = Path(tmp) / "OmniLore_Data"
                with mock.patch.dict(os.environ, env):
                    with redirect_stdout(out), redirect_stderr(err):
                        rc = cli_main(["bootstrap", "--data-dir", str(data_dir), "--quiet"])
        finally:
            os.environ.update(saved)
            if saved_key is not None:
                os.environ["DASHSCOPE_API_KEY"] = saved_key
        return rc, out.getvalue(), err.getvalue()

    def test_startup_echoes_the_effective_protected_fields_and_conflict_dir(self) -> None:
        """默认配置：回显必须写出 9/9 全保护与默认冲突目录，且**不污染 stdout**。"""
        rc, stdout, stderr = self._run_bootstrap({})
        self.assertEqual(rc, 0, f"bootstrap 失败：{stderr}")
        self.assertIn("[主权配置]", stderr, f"stderr 没有主权配置回显：{stderr!r}")
        self.assertIn("OMNILORE_PROTECTED_FRONTMATTER → 受保护 frontmatter 字段", stderr)
        self.assertIn(
            f"{len(DEFAULT_PROTECTED_FRONTMATTER_KEYS)}/{len(DEFAULT_PROTECTED_FRONTMATTER_KEYS)}",
            stderr,
            "回显没有给出「9/9 默认全保护」这样的可核对形式",
        )
        self.assertIn(
            "OMNILORE_CONFLICT_DIR → 冲突副本目录（相对 wiki/）：.omnilore/conflicts", stderr
        )
        # stdout 是「60 秒验证」的可抄录输出，诊断信息不许混进去
        self.assertNotIn("[主权配置]", stdout, "主权回显污染了 stdout（README 的节选就不可复现了）")

    def test_echo_reflects_narrowed_and_disabled_values(self) -> None:
        """改过的取值必须在回显里**如实出现** —— 否则「静默无效」依然看不见。"""
        rc, _stdout, stderr = self._run_bootstrap({
            "OMNILORE_PROTECTED_FRONTMATTER": "doc_id,space_id",
            "OMNILORE_CONFLICT_DIR": "_omnilore/.conflicts",
        })
        self.assertEqual(rc, 0, f"bootstrap 失败：{stderr}")
        self.assertIn("已自定义", stderr)
        self.assertIn("_omnilore/.conflicts", stderr)
        self.assertIn("doc_id, space_id", stderr)

        rc, _stdout, disabled = self._run_bootstrap({"OMNILORE_PROTECTED_FRONTMATTER": "none"})
        self.assertEqual(rc, 0, f"bootstrap 失败：{disabled}")
        self.assertIn("已整体关闭", disabled, "`none` 的反效果必须在回显里看得见")
        self.assertIn("doc_id", disabled, "整体关闭后仍必须显示身份锚点 doc_id")

    def test_compose_forwards_the_two_sovereignty_switches(self) -> None:
        """容器模式必须把两个开关转发进容器 —— 否则 README 的「详见 .env.example」是空头支票。"""
        compose_path = REPO_ROOT / "docker-compose.yml"
        compose = yaml.safe_load(_read(compose_path))
        environment = compose["services"]["mcp-gateway"]["environment"]
        for name in ("OMNILORE_PROTECTED_FRONTMATTER", "OMNILORE_CONFLICT_DIR"):
            self.assertIn(
                name, environment,
                f"docker-compose.yml 的 mcp-gateway 没有转发 {name}："
                "用户在 .env 里配了它，容器里却静默无效",
            )
            self.assertIn(
                "${" + name, str(environment[name]),
                f"{name} 必须取自宿主机环境（${{{name}}}），不能写死成常量",
            )

    def test_compose_postgres_image_tag_uses_the_published_naming_scheme(self) -> None:
        """镜像 tag 必须符合 Docker Hub 上的真实命名规则，否则 `docker compose up` 直接 pull 失败。

        pgvector 官方镜像的 tag 形如 `<pgvector版本>-pg<PG主版本>`（`0.8.0-pg16`），
        而不是把两者顺序倒过来（`pg16-v0.8.0`）。后者在 Docker Hub 上**不存在** ——
        2026-10-24 的真实验证就是这样卡在 pull 阶段，让容器模式整条路径不可用。
        离线无法查 Docker Hub，但命名规则本身可以离线守住。
        """
        compose = yaml.safe_load(_read(REPO_ROOT / "docker-compose.yml"))
        image = compose["services"]["postgres"]["image"]
        self.assertNotIn(
            ":latest", image,
            f"postgres 镜像不允许浮动 tag：{image}",
        )
        tag = image.split(":")[-1]
        self.assertRegex(
            tag, r"^\d+\.\d+\.\d+-pg\d+(-[a-z]+)?$",
            f"tag {tag!r} 不符合 pgvector 官方命名规则 `<pgvector版本>-pg<PG主版本>`"
            "（例如 0.8.0-pg16）；写成 pg16-v0.8.0 这类顺序会 pull 失败",
        )

    def test_misspelled_omnilore_env_var_is_called_out_at_startup(self) -> None:
        """变量名拼错（不被代码读取）时，启动必须点名它，而不是静默失效。"""
        rc, _stdout, stderr = self._run_bootstrap({
            "OMNILORE_PROTECTED_FRONTMATTRE": "none",
        })
        self.assertEqual(rc, 0, f"bootstrap 失败：{stderr}")
        self.assertIn(
            "OMNILORE_PROTECTED_FRONTMATTRE", stderr,
            "拼错的变量名没有被点名 —— 用户会以为「我已经关掉保护了」",
        )
        self.assertIn("不认识", stderr, "回显没有说明这些变量名当前不生效")
        # 拼错的那个不会被当成生效值：仍然应当是默认全保护
        self.assertIn(
            f"{len(DEFAULT_PROTECTED_FRONTMATTER_KEYS)}/{len(DEFAULT_PROTECTED_FRONTMATTER_KEYS)}",
            stderr,
            "拼错的变量名竟然改变了生效值 —— 说明代码真的读了它",
        )


class TestHonestFeatureStatus(unittest.TestCase):
    """R-13：README ↔ 状态矩阵 ↔ CHANGELOG 三处一致，且无一条裸宣称。"""

    def setUp(self) -> None:
        self.readme = _read(README_PATH)
        self.changelog = _read(CHANGELOG_PATH)
        self.matrix = _read(MATRIX_PATH)

    # -- 解析辅助 ----------------------------------------------------------
    def _capability_section(self) -> str:
        """返回 README「核心能力」小节（含其下的 `###` 子标题）。"""
        section = _markdown_section(self.readme, "## 核心能力")
        self.assertTrue(section, "README 找不到「## 核心能力」小节")
        return section

    def _readme_items(self) -> List[Tuple[str, str]]:
        """解析 README 核心能力小节里的 `(名称, 状态)` 列表。"""
        pattern = re.compile(
            r"^- \*\*\[(" + "|".join(FOUR_STATES) + r")\]\*\* \*\*(.+?)\*\*[：:]", re.M
        )
        return [(name, state) for state, name in pattern.findall(self._capability_section())]

    def _matrix_rows(self) -> List[Dict[str, str]]:
        """解析 `docs/FEATURE_STATUS.md` 的状态矩阵行。"""
        lines = self.matrix.splitlines()
        header_idx = next(
            (i for i, line in enumerate(lines) if line.startswith("| 能力 |")), None
        )
        self.assertIsNotNone(header_idx, "状态矩阵缺少 `| 能力 | ...` 表头")
        rows: List[Dict[str, str]] = []
        for line in lines[header_idx + 2 :]:  # 跳过表头与分隔行
            if not line.startswith("|"):
                break
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            self.assertGreaterEqual(len(cells), 4, f"矩阵行列数不足：{line}")
            rows.append({
                "name": cells[0],
                "state": cells[1].strip("`"),
                "verify": cells[3] if len(cells) > 3 else "",
                "remark": cells[4] if len(cells) > 4 else "",
            })
        return rows

    # -- 守卫 --------------------------------------------------------------
    def test_every_readme_capability_is_badged(self) -> None:
        """核心能力小节里不允许存在没有四态标注的裸宣称。"""
        section = self._capability_section()
        bullets = [line for line in section.splitlines() if line.startswith("- ")]
        self.assertTrue(bullets, "核心能力小节没有任何条目")
        badged = [line for line in bullets if re.match(r"^- \*\*\[[A-Za-z-]+\]\*\*", line)]
        unbadged = [line for line in bullets if line not in badged]
        self.assertFalse(unbadged, f"以下能力条目缺少四态标注（裸宣称）：{unbadged}")
        for line in badged:
            state = re.match(r"^- \*\*\[([A-Za-z-]+)\]\*\*", line).group(1)
            self.assertIn(state, FOUR_STATES, f"出现封闭词表之外的状态：{state}")

    def test_readme_capabilities_match_the_matrix_one_for_one(self) -> None:
        """README 的每条能力与矩阵逐条对应，状态也必须一致。"""
        readme_items = set(self._readme_items())
        matrix_items = {(row["name"], row["state"]) for row in self._matrix_rows()}
        self.assertTrue(readme_items, "README 未解析到任何四态能力条目")
        self.assertEqual(
            len(readme_items), len(self._readme_items()),
            "README 出现重复的能力条目名称",
        )
        self.assertEqual(readme_items, matrix_items, "README 能力条目与状态矩阵不一致")

    def test_every_matrix_row_is_a_real_readme_claim(self) -> None:
        """矩阵里不允许出现 README 上根本不存在的能力（防止虚标）。"""
        readme_names = {name for name, _state in self._readme_items()}
        for row in self._matrix_rows():
            self.assertIn(row["name"], readme_names, f"矩阵条目 {row['name']} 在 README 中不存在")

    def test_simulated_rows_carry_a_verification_command(self) -> None:
        """每条 `Simulated` 都要给出用户能自己复跑的验证命令。"""
        simulated = [row for row in self._matrix_rows() if row["state"] == "Simulated"]
        self.assertTrue(simulated, "矩阵里没有 Simulated 条目（这几乎肯定是标注失真）")
        for row in simulated:
            self.assertTrue(
                re.search(r"`?make\s+[a-z0-9-]+`?", row["verify"]),
                f"Simulated 条目「{row['name']}」没有给出验证命令",
            )

    def test_matrix_statistics_match_the_actual_rows(self) -> None:
        """矩阵页脚的统计数字必须等于表格里的真实行数。"""
        rows = self._matrix_rows()
        for state in FOUR_STATES:
            actual = sum(1 for row in rows if row["state"] == state)
            claimed = re.findall(rf"`{re.escape(state)}` (\d+) 条", self.matrix)
            self.assertTrue(claimed, f"矩阵未声明 {state} 的条数")
            for value in claimed:
                self.assertEqual(int(value), actual, f"矩阵宣称 {state}={value}，实际 {actual}")
        total_claimed = re.findall(r"合计 \*\*(\d+) 条\*\*", self.matrix)
        self.assertEqual([int(v) for v in total_claimed], [len(rows)] * len(total_claimed))

    def test_readme_changelog_and_roadmap_report_the_same_statistics(self) -> None:
        """README / CHANGELOG / ROADMAP_STATUS 都必须复述矩阵里的同一组数字 —— 四处一致。

        ROADMAP 原本不在守卫范围内，于是它悄悄写着「`Implemented` 11 条、`Tested-real` 8 条」
        （实际 10 / 9）而无人察觉 —— 这是**已经公开**的错误口径。缺口补上后，
        任何一处再漂移都会立刻变红。
        """
        rows = self._matrix_rows()
        roadmap = _read(REPO_ROOT / "docs" / "ROADMAP_STATUS.md")
        for label, text in (
            ("README", self.readme),
            ("CHANGELOG", self.changelog),
            ("ROADMAP_STATUS", roadmap),
        ):
            for state in FOUR_STATES:
                actual = sum(1 for row in rows if row["state"] == state)
                claimed = re.findall(rf"`{re.escape(state)}` (\d+) 条", text)
                self.assertTrue(claimed, f"{label} 未声明 {state} 的条数")
                for value in claimed:
                    self.assertEqual(
                        int(value), actual, f"{label} 宣称 {state}={value}，实际 {actual}"
                    )

    def test_readme_declares_the_in_memory_simulation_engine(self) -> None:
        """三处都要说清：测试跑在内存仿真引擎上，不是真实 PostgreSQL。"""
        for text, label in ((self.readme, "README"), (self.matrix, "FEATURE_STATUS"), (self.changelog, "CHANGELOG")):
            self.assertIn("RLSDatabaseEngine", text, f"{label} 未点名内存仿真引擎 RLSDatabaseEngine")
            self.assertIn("内存仿真", text, f"{label} 未说明测试运行在内存仿真引擎上")
        self.assertIn("不是真实 PostgreSQL", self.readme)
        self.assertIn("没有 CI 证据", self.readme)

    def test_readme_links_to_the_status_matrix_and_threat_model(self) -> None:
        """首屏必须能走到状态矩阵与威胁模型。"""
        self.assertIn("docs/FEATURE_STATUS.md", self.readme)
        self.assertIn("docs/THREAT_MODEL.md", self.readme)
        self.assertTrue(THREAT_MODEL_PATH.is_file(), "缺少 docs/THREAT_MODEL.md")

    def test_threat_model_declares_the_cross_system_transaction_boundary(self) -> None:
        """威胁模型必须写明：文件系统与数据库**不承诺**跨系统事务原子性。"""
        model = _read(THREAT_MODEL_PATH)
        self.assertIn("能防什么", model)
        self.assertIn("防不住什么", model)
        self.assertIn("Security Advisory", model)
        # 只断言「跨系统事务原子性」这个词出现过是不够的：把「不承诺」翻成「承诺」
        # 仍然绿。这里要求两个词出现在**同一行**，且落在「防不住什么」小节里。
        boundary = _markdown_section(model, "## 3. 防不住什么")
        self.assertTrue(boundary, "威胁模型缺少「防不住什么」小节")
        self.assertRegex(
            boundary,
            r"不承诺[^\n]{0,20}跨系统事务原子性",
            "「防不住什么」必须明确写成「不承诺跨系统事务原子性」；"
            "写成「承诺」就是把边界反过来宣称了",
        )

    # -- 裸宣称守卫（P2-4）：扫描范围必须覆盖 README 全文 -------------------

    def test_no_unbadged_capability_claim_anywhere_in_the_readme(self) -> None:
        """README **全文**不允许出现无证据口径的能力宣称（表格**逐行**判定）。

        为什么扩到全文：首屏定义句、降级组合表、模式对比表都曾出现「裸宣称」——
        「混合检索索引」「真实 PostgreSQL 策略」都写在 `## 核心能力` 之外。
        只扫一个小节等于只堵住了一个门。

        为什么表格要逐行：块级判定下「一整张表」算一个单元，只要块内**任意一行**带证据词
        （例如「不是故障」「Planned」），中间被插进来的一行裸宣称就会被连带放过。
        QA 用「往表里插一行」的变异抓到过这个漏判，所以这里对表格行单独判定。
        """
        offenders: List[str] = []
        for line_no, body in _claim_blocks(self.readme):
            lines = body.splitlines()
            if lines[0].lstrip().startswith("|"):
                for offset, raw_line in enumerate(lines):
                    row = raw_line.strip()
                    if not row.startswith("|") or _is_table_separator(row):
                        continue
                    if _needs_evidence(row):
                        offenders.append(f"README.md:{line_no + offset}: {row[:100]}")
                continue
            if _needs_evidence(body):
                offenders.append(f"README.md:{line_no}: {lines[0][:100]}")
        self.assertFalse(
            offenders,
            "以下位置提到能力却没有任何证据口径（四态徽章或明确对冲）：\n" + "\n".join(offenders),
        )

    def test_the_regions_qa_flagged_still_carry_their_evidence(self) -> None:
        """QA 点名的三段（首屏 / 降级组合表 / 模式对比表）必须逐段带证据。"""
        # ① 首屏：定义句点到的每条能力，都要在紧随其后的证据块里逐条标注状态
        first_screen = _markdown_section(self.readme, "# OmniLore")
        self.assertTrue(first_screen, "README 找不到首屏")
        for capability, allowed_states in (
            ("不可变原始证据", ("Implemented",)),
            # RLS 起始于 2026-10-24 的真实 PG 人工验证；自 2026-10-26 起改由 CI 的
            # `integration-rls` job 持续验证（连续 2 次绿灯）。升级只改证据性质，
            # **不改覆盖范围** —— 下面 ①b 守住这层限定。
            ("行级安全策略", ("Tested-real",)),
            ("混合检索", ("Simulated",)),
        ):
            badges = [
                line for line in first_screen.splitlines()
                if line.lstrip().startswith(">") and f"**{capability}" in line
            ]
            self.assertTrue(badges, f"首屏缺少「{capability}」的状态标注行")
            self.assertTrue(
                any(state in line for line in badges for state in allowed_states),
                f"首屏「{capability}」未标注 {'/'.join(allowed_states)}",
            )

        # ①b RLS 徽章必须同时交代两件事：证据**来自真实 PostgreSQL 的自动化 CI**，
        #    以及**这不等于业务运行时已接入 PG / 不等于持久化**。
        #
        #    为什么改成这两条：旧断言钉的是「手动验证 + CI 尚未接入」。现在 `integration-rls`
        #    job 已连续 2 次绿灯，旧口径已经不成立，断言必须跟着换成新口径 —— 但守卫的目的
        #    没变：防止把 RLS 说成「业务运行时已接入 / 数据已持久化」。只放宽成空断言是
        #    本项目反复踩过的坑（空转守卫），所以这里仍然要求两件事同时出现。
        screen_lines = first_screen.splitlines()
        rls_index = next(
            (i for i, line in enumerate(screen_lines)
             if line.lstrip().startswith(">") and "**行级安全策略" in line),
            None,
        )
        self.assertIsNotNone(rls_index, "首屏找不到 RLS 状态标注行")
        # 徽章可能跨行书写（本仓库就是），因此把该徽章起算的 3 行算作同一条声明
        rls_claim = "\n".join(screen_lines[rls_index:rls_index + 3])
        self.assertIn("Tested-real", rls_claim)
        # ① 证据来自真实 PostgreSQL 上的自动化 CI（不是内存引擎、也不是一次性人工）。
        #    只写「CI 已接入」不够 —— 必须写成**已经绿灯**，否则「接入但从未跑过」也能过关。
        self.assertIn("CI", rls_claim, "RLS 的 Tested-real 必须写明证据来自 CI")
        self.assertIn("真实 PostgreSQL", rls_claim, "RLS 的 Tested-real 必须写明证据跑在真实 PostgreSQL 上")
        self.assertIn("探针", rls_claim, "RLS 的 Tested-real 必须写明跑的是哪三条探针")
        self.assertIn("绿灯", rls_claim, "RLS 的 Tested-real 必须写明 CI **已经绿灯**，而不是「已接入、待绿灯」")
        self.assertNotIn(
            "待首次绿灯", rls_claim,
            "首屏 RLS 徽章仍写着「待首次绿灯」—— `integration-rls` job 已连续 2 次绿灯，"
            "这是已被真实运行证伪的旧口径",
        )
        # ② 仍然明确「不等于业务运行时已接入 PG / 不等于持久化」——只写「不等于」不够，
        #    必须点名到「业务运行时」或「持久化」，否则把限定删掉照样是绿（空转守卫）。
        self.assertRegex(
            rls_claim,
            r"(不等于|不证明|不代表)[^\n]{0,40}(业务运行时|持久化)",
            "RLS 徽章必须写明「CI 绿灯 ≠ 业务运行时已接入 PG / ≠ 数据已持久化」；"
            "只写「不等于」而不点名到「业务运行时 / 持久化」视为空转守卫",
        )

        # ② 降级组合表：四行必须连成一张表（曾被注释从中间切断），且 `hybrid` 有边界说明
        degrade = _markdown_section(self.readme, "## 降级与上报")
        self.assertTrue(degrade, "README 找不到「降级与上报」小节")
        row_indexes = [
            index for index, line in enumerate(degrade.splitlines()) if re.match(r"^\| \d \|", line)
        ]
        self.assertEqual(len(row_indexes), 4, f"组合态表应有 4 行，实际 {len(row_indexes)} 行")
        self.assertEqual(
            row_indexes,
            list(range(row_indexes[0], row_indexes[0] + 4)),
            "组合态表被注释行切断：四行必须连续，否则渲染出来的表格是残缺的",
        )
        self.assertIn("`Simulated`", degrade, "降级小节未声明 hybrid 路径的 Simulated 状态")
        self.assertIn("没有真实", degrade, "降级小节未说明 hybrid 路径没有真实依赖证据")

        # ③ 模式对比表：RLS 那一行必须自带四态徽章，不能裸说「真实 PostgreSQL 策略」
        modes = _markdown_section(self.readme, "## 我该走哪条路？")
        self.assertTrue(modes, "README 找不到模式对比小节")
        rls_rows = [line for line in modes.splitlines() if "RLS 策略执行方式" in line]
        self.assertTrue(rls_rows, "模式对比表缺少「RLS 策略执行方式」行")
        for line in rls_rows:
            self.assertTrue(
                any(state in line for state in FOUR_STATES),
                f"模式对比表的 RLS 行没有四态徽章（裸宣称）：{line}",
            )

        # ④ 矩阵里必须真有「混合检索」这一行，且状态是 Simulated + 有验证命令
        hybrid_rows = [row for row in self._matrix_rows() if row["name"].startswith("混合检索")]
        self.assertTrue(hybrid_rows, "状态矩阵缺少「混合检索」条目")
        self.assertEqual(hybrid_rows[0]["state"], "Simulated", "混合检索只能标 Simulated（无真实证据）")
        self.assertIn("make ", hybrid_rows[0]["verify"], "混合检索条目没有给出验证命令")

    def test_only_the_probed_rls_row_is_upgraded(self) -> None:
        """RLS 升级红线：只有被三条探针覆盖的那一条能升 `Tested-real`，其余不许跟涨。"""
        rows = {row["name"]: row["state"] for row in self._matrix_rows()}
        upgraded = "真实 PostgreSQL RLS 策略与跨空间读拦截"
        self.assertEqual(
            rows.get(upgraded), "Tested-real",
            f"「{upgraded}」应依据 `integration-rls` job 在真实 PostgreSQL 上的"
            "CI 持续验证（自 2026-10-26 起连续 2 次绿灯）标 Tested-real",
        )
        # 这三条探针只验证了「策略挂载 + 跨空间读拦截」；下面这些都没被覆盖，必须守住
        must_stay_simulated = (
            "四维权限交集",
            "Profile 硬性隔离",
            "撤销即时失效",
            "组织树只读继承",
            "混合检索（BM25 + 向量 + 代码符号）",
            "Obsidian 双向同步",
            "备份恢复与一致性对账",
            "两阶段零停机版本控制",
        )
        for name in must_stay_simulated:
            self.assertEqual(
                rows.get(name), "Simulated",
                f"「{name}」没有被真实 PG 探针覆盖，不得升级（这是标注红线）",
            )
        self.assertEqual(
            rows.get("真实 PostgreSQL 集成 CI"), "Tested-real",
            "自动化集成 CI 已接入 Actions 的 integration-rls job，并已于 2026-10-26 "
            "真实运行、连续 2 次绿灯，达到「连续 ≥2 次绿灯」的升级门槛。"
            "升级**不放宽覆盖范围**：该 job 只跑独立 SQL 探针，不覆盖业务运行时接线"
            "（详见矩阵备注）",
        )

    def test_the_two_real_pg_exceptions_keep_their_own_evidence_kind(self) -> None:
        """两条真实 PG 例外的**证据性质不许混写**：①是 CI 持续验证，②仍是人工一次性、非 CI。

        为什么单独守这一条：CI 绿灯后最容易出现的虚标是「顺手把②也说成已由 CI 验证」——
        而 `tools/verify_rls_app_role.py` 根本没接进 CI。反过来，把①说回「待首次绿灯」
        也是虚标（那是已经被真实运行证伪的旧口径）。
        """
        row_of = {
            row["name"]: row for row in self._matrix_rows()
        }
        rls_row = row_of.get("真实 PostgreSQL RLS 策略与跨空间读拦截") or {}
        self.assertIn(
            "CI", rls_row.get("remark", ""),
            "①「真实 PostgreSQL RLS 策略与跨空间读拦截」必须写明证据来自 CI 持续验证",
        )
        self.assertNotIn(
            "待首次绿灯", rls_row.get("remark", ""),
            "① 仍写着「待首次绿灯」—— 该 job 已连续 2 次绿灯，这是已被证伪的旧口径",
        )
        self.assertRegex(
            rls_row.get("remark", ""),
            r"(不等于|不证明)[^\n]{0,60}(业务运行时|持久化)",
            "① 的备注必须写明「不等于业务运行时已接入 PG / 不等于持久化」",
        )

        app_role_row = row_of.get("PostgreSQL 安全连接层与 RLS 生效验证") or {}
        self.assertIn(
            "非 CI", app_role_row.get("remark", ""),
            "②「PostgreSQL 安全连接层与 RLS 生效验证」的验证脚本没有接进 CI，"
            "不得写成 CI 验证",
        )

    def test_arm64_row_reports_its_actual_green_run_count(self) -> None:
        """arm64 必须写明**实际绿灯次数**（已 1 次、样本尚少），不许停留在「待首次绿灯」。"""
        arm_rows = [
            row for row in self._matrix_rows() if row["name"].startswith("ARM64 架构验证")
        ]
        self.assertTrue(arm_rows, "矩阵缺少 ARM64 条目")
        self.assertEqual(arm_rows[0]["state"], "Implemented", "arm64 只有 1 次绿灯，仍应标 Implemented")
        remark = arm_rows[0].get("remark", "")
        self.assertNotIn(
            "待首次绿灯", remark,
            "arm64 已于 2026-10-26 首次绿灯，「待首次绿灯」是已被证伪的旧口径",
        )
        self.assertIn("1 次绿灯", remark, "arm64 备注必须写明实际绿灯次数（1 次）")
        self.assertIn("样本尚少", remark, "arm64 备注必须写明样本尚少，不得暗示已被充分验证")

    def test_verify_rls_pg_refuses_a_non_empty_database(self) -> None:
        """非空库必须 fail-closed（exit 2 + 可执行指引），且**绝不**自动清库。"""
        module = _load_probe_module()
        with mock.patch.object(module, "_run_psql", return_value=(0, "3", "")):
            reason = module._check_target_database_is_empty("postgresql://example/db")
        self.assertIsNotNone(reason, "目标库已有本项目的表时，脚本必须停下")
        self.assertIn("omnilore_verify", reason, "必须给出「指向空库」的可执行指引")
        self.assertIn("不会", reason, "必须明确写出「不会自动 DROP SCHEMA / 清库」")

        with mock.patch.object(module, "_run_psql", return_value=(0, "0", "")):
            self.assertIsNone(
                module._check_target_database_is_empty("postgresql://example/db"),
                "空库应当放行",
            )
        # 探测本身跑不起来时也要 fail-closed（属于「未验证」，不是「空库」）
        with mock.patch.object(module, "_run_psql", return_value=(127, "", "无法启动 psql")):
            self.assertIsNotNone(module._check_target_database_is_empty("postgresql://example/db"))
        self.assertEqual(module.EXIT_NOT_VERIFIED, 2, "未验证必须是 2，不能混成 1（崩溃）或 3（结论为否）")

    #: 文档里出现 `make <目标>` 时，目标名只允许这些字符（**必须以字母开头**：
    #: 否则 `WSL2_环境备注.md` 里 `GNU Make 4.3` 这种版本号会被当成目标名）。
    MAKE_TARGET_TOKEN = re.compile(r"`?make\s+([A-Za-z][A-Za-z0-9_.-]*)`?")
    #: 扫描范围排除的目录：都在 `.gitignore` 里，不是公开文档（写进去也不会被读者看到）。
    NON_PUBLIC_DOC_DIRS = frozenset(
        {".git", ".venv", "deliverables", "_execution_logs", ".workbuddy", ".workbuddy-ai", ".ruff_cache"}
    )

    def _public_docs(self) -> List[Path]:
        """返回仓库里所有**面向读者**的 Markdown（全部 tracked 文档 + 未来新增的）。

        用遍历而不是写死文件名列表：写死列表的下场就是这次 —— `ROADMAP_STATUS` /
        `THREAT_MODEL` / `CHANGELOG` 三份不在名单里，于是它们写假命令不会有人发现。

        用 `os.walk` 并在**进入前剪枝**，不用 `rglob`：`rglob` 会先钻进 `.venv/` 再过滤，
        而 Windows 上本地 `.venv` 里可能有断链符号链接（例如 WSL 建的 venv），
        `os.scandir` 会抛 `OSError: [WinError 1920]` —— 这在 Python 3.11 上直接让守卫 ERROR
        （3.13 的 pathlib 恰好会吞掉这类错误，所以本机没暴露；CI 矩阵里有 3.11）。
        """
        docs: List[Path] = []
        for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
            dirnames[:] = [d for d in dirnames if d not in self.NON_PUBLIC_DOC_DIRS]
            for name in filenames:
                if name.endswith(".md"):
                    docs.append(Path(dirpath) / name)
        return sorted(docs)

    def test_every_make_target_mentioned_in_docs_exists(self) -> None:
        """文档里写给用户跑的 `make xxx` 必须真的存在（不许写假命令）。

        覆盖范围是**全部公开文档**，不只是 README 与状态矩阵。缺口曾经是真实存在的：
        这条守卫原先只扫那两份，于是另外三份文档里写一个不存在的目标也不会红。

        两类合法的非目标写法必须排除，否则守卫会变成噪声源：
          * 通配写法 `make verify-*`（文档在说「一组验证入口」，不是某一个）；
          * 占位写法 —— 文档应当写 `make <目标>`，写成 `make xxx` 就会被判为假命令。

        变异验证（已实测，三条都会红）：① 在 `ROADMAP_STATUS.md` 写 `` `make verify-does-not-exist` ``；
        ② 在 `CHANGELOG.md` 写同样一行；③ 在 `THREAT_MODEL.md` 写同样一行。
        """
        makefile = _read(MAKEFILE_PATH)
        mentioned: Dict[str, List[str]] = {}
        for path in self._public_docs():
            for token in self.MAKE_TARGET_TOKEN.findall(_read(path)):
                if "*" in token or token.endswith("-") or token.endswith("."):
                    continue  # 通配 / 未写完的写法，不是某个具体目标
                mentioned.setdefault(token, []).append(path.relative_to(REPO_ROOT).as_posix())

        self.assertTrue(mentioned, "全部公开文档里没有任何 make 验证命令 —— 解析或文件发现失效了")
        self.assertGreaterEqual(
            len(self._public_docs()), 8,
            f"只发现 {len(self._public_docs())} 份公开文档，明显少于预期 —— 扫描范围失效，"
            "这条守卫会退化成空转",
        )
        for target in sorted(mentioned):
            self.assertTrue(
                re.search(rf"^{re.escape(target)}:", makefile, re.M),
                f"Makefile 缺少目标 {target}（被 {sorted(set(mentioned[target]))} 引用）",
            )


class TestContributionEntryPoints(unittest.TestCase):
    """R-10a：贡献入口可统计、可上手，且真的绕开了 `engine.py`。"""

    def setUp(self) -> None:
        self.template_dir = REPO_ROOT / ".github" / "ISSUE_TEMPLATE"
        # `config.yml` 是 GitHub 的模板开关，不是一套模板，计数时排除。
        self.templates = sorted(p for p in self.template_dir.glob("*.yml") if p.name != "config.yml")

    def _load(self, name: str) -> Dict[str, object]:
        """读取并解析一个 Issue 表单模板。"""
        return yaml.safe_load(_read(self.template_dir / name))

    def _dropdown_options(self, template: Dict[str, object]) -> List[str]:
        """展平一个模板里所有下拉选项文本。"""
        options: List[str] = []
        for block in template.get("body", []):
            if block.get("type") == "dropdown":
                options.extend(str(opt) for opt in block["attributes"].get("options", []))
        return options

    def test_six_issue_templates_exist_and_prefer_dropdowns(self) -> None:
        """6 套模板，且每套都以单选下拉为主（自由文本无法做分布统计）。"""
        self.assertEqual(
            len(self.templates), 6,
            f"Issue 模板数量应为 6，实际 {[p.name for p in self.templates]}",
        )
        for path in self.templates:
            template = self._load(path.name)
            self.assertIn("name", template, f"{path.name} 缺少 name")
            self.assertTrue(template.get("labels"), f"{path.name} 缺少 labels")
            blocks = template.get("body", [])
            dropdowns = [b for b in blocks if b.get("type") == "dropdown"]
            self.assertGreaterEqual(len(dropdowns), 2, f"{path.name} 下拉选项不足，无法做分布统计")
            for block in dropdowns:
                self.assertGreaterEqual(
                    len(block["attributes"].get("options", [])), 3,
                    f"{path.name} 的 {block.get('id')} 选项过少",
                )
        # 空白 Issue 无法做分布统计，因此必须关掉
        config = yaml.safe_load(_read(self.template_dir / "config.yml"))
        self.assertFalse(config["blank_issues_enabled"], "必须禁用空白 Issue，否则统计会被自由文本稀释")

    def test_install_failure_template_asks_how_long_and_escalates_abandonment(self) -> None:
        """`install-failure` 问「卡住多久」，且「我已放弃」进入 P0 通道。"""
        template = self._load("install-failure.yml")
        labels = [str(x) for x in template.get("labels", [])]
        self.assertIn("install-failure", labels)
        options = self._dropdown_options(template)
        self.assertTrue(
            any("卡住多久" in opt or "我已放弃" in opt for opt in options),
            "install-failure 缺少「卡住多久」/「我已放弃」选项",
        )
        self.assertTrue(
            any("我已放弃" in opt for opt in options),
            "install-failure 缺少「我已放弃」选项",
        )
        # 「我已放弃」必须真的能自动拿到 P0-escape：由专用模板或 triage workflow 保证
        escape_labels = [str(x) for x in self._load("install-escape.yml").get("labels", [])]
        self.assertIn("P0-escape", escape_labels)
        triage = _read(REPO_ROOT / ".github" / "workflows" / "triage.yml")
        self.assertIn("我已放弃", triage)
        self.assertIn("P0-escape", triage)

    def test_retrieval_quality_template_escalates_suspected_privilege_leak(self) -> None:
        """「疑似越权」必须自动进入 `security-triage` 分流。"""
        template = self._load("retrieval-quality.yml")
        options = self._dropdown_options(template)
        self.assertTrue(
            any("疑似越权" in opt for opt in options),
            "retrieval-quality 缺少「疑似越权」选项",
        )
        triage = _read(REPO_ROOT / ".github" / "workflows" / "triage.yml")
        self.assertIn("疑似越权", triage)
        self.assertIn("security-triage", triage)
        # 安全漏洞不走 Issue 模板
        security = _read(REPO_ROOT / "SECURITY.md")
        self.assertTrue(
            "Security Advisory" in security or "security/advisories" in security,
            "SECURITY.md 未把漏洞披露指向 Security Advisory",
        )

    def test_contributing_six_paths_all_avoid_engine_py(self) -> None:
        """六条贡献路径确实绕开 `engine.py`（贡献者心理门槛来源）。"""
        contributing = _read(REPO_ROOT / "CONTRIBUTING.md")
        section = _markdown_section(contributing, "## 3. 六条贡献路径")
        self.assertTrue(section, "CONTRIBUTING 缺少六条贡献路径小节")
        rows = re.findall(r"^\|\s*([1-6])\s*\|(.+)$", section, re.M)
        self.assertEqual(len(rows), 6, f"贡献路径应为 6 条，实际 {len(rows)}")
        for number, row in rows:
            self.assertNotIn(
                "engine.py", row,
                f"第 {number} 条贡献路径仍然要求碰 engine.py，与「绕开核心文件」叙事矛盾",
            )
        self.assertIn("六条贡献路径", _read(README_PATH))

    def test_real_pg_probe_fails_closed_without_a_database(self) -> None:
        """`make verify-rls-pg` 在环境不具备时必须**未验证**，绝不假装验证过。"""
        env = dict(os.environ)
        env.pop("DATABASE_URL", None)
        env.pop("OMNILORE_MIGRATOR_DSN", None)
        # 子进程的 stdout 编码必须与这里的解码方式一致：en-US Windows 的 locale 是 cp1252，
        # 不钉住的话子进程打印中文会 UnicodeEncodeError（CI 的 windows-latest 就是这么红的）。
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.run(
            [sys.executable, "tools/verify_rls_pg.py"],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(proc.returncode, 2, f"缺少数据库时应当以 2（未验证）退出：{proc.stdout}")
        self.assertIn("未验证", proc.stdout)


class TestCiIntegrationConfig(unittest.TestCase):
    """R-08b：真实 PostgreSQL 集成 CI 的**配置**守卫。

    只读 `ci.yml` 的 YAML 文本，**不**执行 docker、**不**连数据库 —— 因此它证明的是
    「配置正确」，不是「CI 会一直绿」。后者由真实运行回答：该 job 已于 2026-10-26
    真实运行并连续 2 次绿灯（Run #1 `8d0ecc3`、Run #2 `5d513fe`）。

    为什么这类测试值得写：CI 配置是「没人跑就等于没写」的典型地带，而且它的失败方式
    往往是**假绿**（跳过了却看起来通过），比不跑更糟。下面每条断言都对应一次真实踩过的
    坑或一条假绿路径：

    * 镜像 tag 写成 `pg16-v0.8.0` → job 在 pull 阶段必挂（Docker Hub 无此 tag）；
    * 健康检查不带 `-U` / `-d` → PG 未就绪就开跑，探针 flaky；
    * 把 DDL 脚本挂进 `docker-entrypoint-initdb.d` → 探针发现非空库，exit 2；
    * `continue-on-error: true` 或吞掉退出码 → 未验证/结论为否都变成绿，纯粹假绿。
    """

    def setUp(self) -> None:
        self.raw = _read(CI_WORKFLOW_PATH)
        self.workflow = yaml.safe_load(self.raw)
        self.assertIsInstance(self.workflow, dict, "ci.yml 无法解析为映射（YAML 语法已坏）")

    # -- 解析辅助 ----------------------------------------------------------
    def _job(self) -> Dict[str, object]:
        """取出 `integration-rls` job，缺失时直接失败（本组其余断言都依赖它）。"""
        jobs = self.workflow.get("jobs") or {}
        self.assertIn(
            "integration-rls", jobs,
            f"ci.yml 缺少 integration-rls job（现有 job：{sorted(jobs)}）—— "
            "真实 PG 的 RLS 证据就又没有自动化来源了",
        )
        job = jobs["integration-rls"]
        self.assertIsInstance(job, dict, "integration-rls 不是映射")
        return job

    def _postgres_service(self) -> Dict[str, object]:
        """取出 postgres service container 定义。"""
        services = self._job().get("services") or {}
        self.assertIn(
            "postgres", services,
            "integration-rls 没有 postgres service container —— 那就不是真实 PG 集成",
        )
        return services["postgres"]

    def _step_texts(self) -> List[Tuple[str, str]]:
        """返回 `(step 名称, run 脚本)` 列表，只保留带 `run` 的步骤。"""
        texts: List[Tuple[str, str]] = []
        for step in self._job().get("steps") or []:
            if isinstance(step, dict) and step.get("run"):
                texts.append((str(step.get("name", "")), str(step["run"])))
        return texts

    def _probe_step_text(self) -> str:
        """返回真正执行探针的那一步的脚本正文。

        判别方式是唯一的命令特征 `tools/verify_rls_pg.py` —— 其它步骤不许出现它，
        否则「探针步骤」的定位会漂移，针对性断言就失去意义。
        """
        matches = [run for _name, run in self._step_texts() if "tools/verify_rls_pg.py" in run]
        self.assertEqual(
            len(matches), 1,
            f"应恰好有 1 个步骤执行 tools/verify_rls_pg.py，实际 {len(matches)} 个",
        )
        return matches[0]

    @staticmethod
    def _collect_continue_on_error(node: object) -> List[str]:
        """递归收集任意层级的 `continue-on-error` 取值（GitHub 允许写在 job / step 两级）。"""
        found: List[str] = []
        stack: List[object] = [node]
        while stack:
            current = stack.pop()
            if isinstance(current, dict):
                for key, value in current.items():
                    if str(key) == "continue-on-error":
                        found.append(str(value))
                    stack.append(value)
            elif isinstance(current, list):
                stack.extend(current)
        return found

    @staticmethod
    def _case_branches(probe: str) -> Dict[str, str]:
        """把 probe 步骤里的 shell `case` 分支切成 `{分支标签: 分支正文}`。

        为什么不能直接对整段脚本做 `assertIn("exit 2")`：分支里的
        `echo "::error::exit 2 = 未验证..."` 也含有 `exit 2` 这串字符，于是「把真正的
        `exit 2` 改成 `exit 0`」这种致命变异会被这句提示语蒙混过去（QA 式变异实测如此）。
        必须按分支切段、逐段断言**语句级**的退出码。

        Args:
            probe: 探针步骤的 shell 脚本正文。

        Returns:
            `{标签: 正文}`，标签为 `"0"` / `"2"` / `"3"` / `"*"` 等。
        """
        marker = re.compile(r"(?m)^[ \t]*(\d+|\*)\)[ \t]*$")
        matches = list(marker.finditer(probe))
        branches: Dict[str, str] = {}
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(probe)
            branches[match.group(1)] = probe[match.end():end]
        return branches

    # -- 守卫 --------------------------------------------------------------
    def test_ci_has_a_real_postgres_integration_job(self) -> None:
        """`integration-rls` 存在，且真的是「真实 PG + 探针」而不是把脚本空跑一遍。"""
        job = self._job()
        self.assertIn("runs-on", job, "integration-rls 没有 runs-on")
        postgres = self._postgres_service()
        self.assertIn("5432", str(postgres.get("ports", "")), "postgres service 未映射 5432")

        service_env = postgres.get("env") or {}
        for key in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"):
            self.assertIn(key, service_env, f"postgres service 缺少 {key}")

        job_env = job.get("env") or {}
        self.assertIn(
            "DATABASE_URL", job_env,
            "integration-rls 没有导出 DATABASE_URL —— 探针会以 exit 2「未验证」结束",
        )
        # 连接串里的库名必须与 service 建出来的库一致，否则探针连的是一个不存在的库
        self.assertIn(
            str(service_env["POSTGRES_DB"]), str(job_env["DATABASE_URL"]),
            "DATABASE_URL 与 service 的 POSTGRES_DB 不一致：探针会连到别的库（或连不上）",
        )
        self.assertIn(
            "tools/verify_rls_pg.py", "".join(run for _name, run in self._step_texts()),
            "integration-rls 没有任何步骤执行 tools/verify_rls_pg.py",
        )

    def test_integration_job_pins_a_published_pgvector_image_tag(self) -> None:
        """镜像 tag 必须符合 `0.8.0-pg16` 这类**真实存在**的命名，且不许浮动。"""
        image = str(self._postgres_service()["image"])
        self.assertTrue(image.startswith("pgvector/pgvector:"), f"镜像来源异常：{image}")
        self.assertNotIn(":latest", image, f"镜像不许浮动 tag：{image}")
        tag = image.split(":")[-1]
        self.assertRegex(
            tag, PGVECTOR_IMAGE_TAG_PATTERN,
            f"tag {tag!r} 不符合 pgvector 官方命名规则 `<pgvector版本>-pg<PG主版本>`"
            "（例如 0.8.0-pg16）；写成 pg16-v0.8.0 会在 pull 阶段直接失败",
        )

    def test_integration_job_never_allows_failure(self) -> None:
        """整条 job 链路上不允许 `continue-on-error`，探针命令后也不许吞退出码。"""
        self.assertEqual(
            self._collect_continue_on_error(self._job()), [],
            "integration-rls 里出现了 continue-on-error —— 未验证/结论为否都会被伪装成绿",
        )
        self.assertNotIn(
            "continue-on-error", str(self._job()),
            "integration-rls 的文本里出现了 continue-on-error",
        )
        probe = self._probe_step_text()
        self.assertNotIn(
            "|| true", probe,
            "探针命令后接了 `|| true`：任何失败都会被吞掉，job 永远绿（典型假绿）",
        )

    def test_integration_job_waits_for_a_usable_database(self) -> None:
        """健康检查必须是带 `-U` / `-d` 的 `pg_isready`，并有足够的重试窗口。"""
        options = str(self._postgres_service().get("options", ""))
        self.assertIn(
            "pg_isready", options,
            "postgres service 没有用 pg_isready 做健康检查 —— job 可能在 PG 未就绪时开跑",
        )
        self.assertIn("-U", options, "pg_isready 未指定 -U：用户名对不上时会被误判为就绪")
        self.assertIn("-d", options, "pg_isready 未指定 -d：目标库不可用时仍可能报就绪")
        self.assertIn("--health-retries", options, "健康检查没有重试次数，冷启动时必然 flaky")
        self.assertIn("--health-start-period", options, "健康检查没有 start-period，镜像初始化会被算作失败")

    def test_integration_job_does_not_pre_seed_the_schema(self) -> None:
        """不许把 DDL 种子挂进 initdb 钩子 —— 探针要求空库并自己建表。"""
        dumped = str(self._job())
        self.assertNotIn(
            "docker-entrypoint-initdb.d", dumped,
            "integration-rls 引用了 docker-entrypoint-initdb.d：预建表会让探针以 exit 2 结束",
        )
        for rel in ("sql/001", "sql/002"):
            self.assertNotIn(
                rel, dumped,
                f"integration-rls 引用了 {rel}：探针要求**空库**并自己应用 DDL，预挂载会导致假红",
            )

    def test_probe_exit_codes_are_mapped_to_a_red_job(self) -> None:
        """0/2/3 三码契约必须在 CI 里被**逐码**处理，且退出码取自 python 而非 tee。"""
        probe = self._probe_step_text()
        self.assertIn(
            "PIPESTATUS", probe,
            "退出码必须取自 python 进程（PIPESTATUS[0]）；直接用 `$?` 会拿到 tee 的状态，"
            "脚本失败也会变绿",
        )

        branches = self._case_branches(probe)
        for label in ("0", "2", "3"):
            self.assertIn(label, branches, f"probe 步骤没有 `case` 的 {label}) 分支")

        # 每个分支都必须以**该分支自己的退出码**结束（语句级判定，不看提示语里的字符串）
        for label in ("0", "2", "3"):
            self.assertRegex(
                branches[label], rf"(?m)^[ \t]*exit {label}[ \t]*$",
                f"{label}) 分支没有以 `exit {label}` 结束 —— 这条路径会让 job 变成错误的颜色",
            )
        # 未验证（2）与结论为否（3）必须写明语义，不许含糊成「失败」
        self.assertIn("未验证", branches["2"], "2) 分支必须写明这是「未验证」而不是通过")
        self.assertIn("结论为否", branches["3"], "3) 分支必须写明「已验证但结论为否」")
        # 兜底分支：非 0/2/3 的退出码（脚本崩溃）也必须红
        self.assertIn("*", branches, "probe 步骤缺少 `*)` 兜底分支：脚本崩溃会被当成通过")
        self.assertRegex(
            branches["*"], r"(?m)^[ \t]*exit [1-9][0-9]*[ \t]*$",
            "*) 兜底分支没有以非零码退出（脚本崩溃会静默变绿）",
        )
        # 成功路径也要有实质断言：exit 0 但日志里没有 3 条 [PASS] 同样算假绿
        self.assertIn("[PASS]", branches["0"], "成功分支未校验探针日志里真的出现了 [PASS] 行")

    def test_workflow_keeps_the_offline_jobs_intact(self) -> None:
        """新增 job 不许挤掉原有离线门禁：test / lint / dco 必须照旧存在。"""
        jobs = set((self.workflow.get("jobs") or {}).keys())
        for name in ("test", "lint", "dco", "integration-rls"):
            self.assertIn(name, jobs, f"ci.yml 缺少 job：{name}")
        test_job = self.workflow["jobs"]["test"]
        matrix = (test_job.get("strategy") or {}).get("matrix") or {}
        self.assertEqual(matrix.get("os"), ["ubuntu-latest", "windows-latest"],
                         "test job 的跨平台矩阵被改动了（windows-latest 是防 CRLF 回归的关键）")
        self.assertEqual(matrix.get("python-version"), ["3.11", "3.12"],
                         "test job 的 Python 矩阵被改动了")
        # 新 job 只能新增，不得让离线 job 依赖它
        for name in ("test", "lint", "dco"):
            self.assertNotIn("needs", self.workflow["jobs"][name],
                             f"{name} 不应依赖任何其他 job（尤其不该被 integration-rls 阻塞）")

        # UTF-8 模式必须开着：runner 是 en-US，stdout 走管道时默认 locale 编码是 cp1252，
        # 而本项目所有面向用户的文案都是中文 —— 关掉它，windows-latest 会因为
        # UnicodeEncodeError 整片变红（历史上就是这样红的）。
        self.assertEqual(
            str((self.workflow.get("env") or {}).get("PYTHONUTF8")), "1",
            "ci.yml 顶层 env 必须设 PYTHONUTF8=1：否则 en-US Windows runner 上"
            "子进程打印中文会 UnicodeEncodeError，windows-latest 两个 job 会再次全红",
        )

    def test_compose_bootstrap_user_never_shadows_a_nologin_role(self) -> None:
        """容器编排的启动超用户必须与 CI 一致，且不得与 `sql/001` 的 `NOLOGIN` 角色同名。

        这是**安全**不变量，不是风格偏好。`docker-compose.yml` 曾把 `POSTGRES_USER` 写成
        `omnilore_migrator`，而 `sql/001` 里有一句 `CREATE ROLE omnilore_migrator NOLOGIN`：
        initdb 会先把这个名字建成**可登录的超级用户**，随后 `DO $$ IF NOT EXISTS $$` 直接
        跳过创建 —— 于是一个本应 `NOLOGIN` 的属主角色静默变成了**可登录超用户**，而超级用户
        会绕过全部 21 条 RLS 策略。CI 的 `integration-rls` 一直用 `postgres`，容器编排却与它
        分叉：分叉意味着「CI 验证过的那条路径」和「用户实际跑的那条路径」不是同一条。

        断言三件事（缺一条都会让上面那个提权悄悄回来）：
          1. compose 的 `POSTGRES_USER` 默认值可**静态推导**（不允许只能运行时才知道）；
          2. 它等于 CI `integration-rls` service 的 `POSTGRES_USER` —— 两条路径不许分叉；
          3. 它不在 `sql/001` 的 `NOLOGIN` 角色集合里 —— 否则就是同名提权；
          4. `sql/001` 自己对「同名且可登录/超用户」的情况 **fail closed**（宁可初始化失败，
             也不留一个看起来正常的提权角色）。

        变异验证（已实测，两条都会红）：把 compose 改回 `omnilore_migrator` → 第 3 条红；
        删掉 `sql/001` 的 fail-closed 块 → 第 4 条红。
        """
        compose = yaml.safe_load(_read(REPO_ROOT / "docker-compose.yml"))
        postgres = compose["services"]["postgres"]
        raw_user = str((postgres.get("environment") or {}).get("POSTGRES_USER", ""))
        match = re.search(r"\$\{POSTGRES_USER:-([^}]+)\}", raw_user)
        self.assertIsNotNone(
            match,
            f"docker-compose.yml 的 POSTGRES_USER 必须给出可推导的默认值（形如 ${{POSTGRES_USER:-postgres}}），"
            f"实际是 {raw_user!r} —— 没有默认值时本守卫无从判断容器会建成哪个超用户",
        )
        compose_user = match.group(1).strip()

        ci_user = str((self._postgres_service().get("env") or {}).get("POSTGRES_USER", "")).strip()
        self.assertTrue(ci_user, "integration-rls 的 postgres service 没有显式 POSTGRES_USER")
        self.assertEqual(
            compose_user, ci_user,
            f"compose 的启动超用户是 {compose_user!r}，CI 是 {ci_user!r} —— 两条路径分叉了。"
            "compose 那条路径没有 CI 覆盖，一旦分叉就没人替它把关",
        )

        # `.env` 会**覆盖** compose 里的默认值，而 `.env.example` 是用户照着 `cp` 的那一份。
        # 只守住 compose 默认值是不够的：模板里写着旧名字，用户一 cp 就把它又改回去了。
        env_example = _read(REPO_ROOT / ".env.example")
        env_match = re.search(r"(?m)^POSTGRES_USER[ \t]*=[ \t]*(\S*)[ \t]*$", env_example)
        self.assertIsNotNone(
            env_match,
            ".env.example 里没有 POSTGRES_USER —— 模板与 docker-compose.yml 漂移了",
        )
        self.assertEqual(
            env_match.group(1).strip(), compose_user,
            f".env.example 的 POSTGRES_USER 是 {env_match.group(1).strip()!r}，"
            f"与 compose 默认值 {compose_user!r} 不一致；用户 `cp .env.example .env` 后"
            "会用模板里的值覆盖默认值，于是同名提权又回来了",
        )

        sql = _read(REPO_ROOT / "sql" / "001_omnilore_schema_and_rls.sql")
        nologin_roles = set(re.findall(r"CREATE ROLE\s+([A-Za-z_][A-Za-z0-9_]*)\s+NOLOGIN", sql))
        self.assertGreaterEqual(
            len(nologin_roles), 5,
            f"只从 sql/001 解析到 {sorted(nologin_roles)} 个 NOLOGIN 角色（期望 ≥5）—— "
            "守卫自身失效了，必须先修解析再谈结论",
        )
        self.assertNotIn(
            compose_user, nologin_roles,
            f"compose 的启动超用户 {compose_user!r} 与 sql/001 的 NOLOGIN 角色同名："
            "initdb 会先把它建成可登录超用户，随后的 IF NOT EXISTS 会跳过创建，"
            "结果是该角色绕过全部 RLS 策略",
        )

        self.assertRegex(
            sql,
            r"rolname\s*=\s*'omnilore_migrator'[\s\S]{0,240}?rolsuper[\s\S]{0,240}?RAISE\s+EXCEPTION",
            "sql/001 缺少「同名提权」的 fail-closed 检查：若某个 POSTGRES_USER 恰好叫 "
            "omnilore_migrator，脚本应当直接报错停下，而不是留下一个可登录超用户",
        )


class TestAuditBundle(unittest.IsolatedAsyncioTestCase):
    """R-05m：`audit-bundle` 导出的证据包必须**真的在校验**，而不是抄一遍库里的值。

    这一组的核心不是「导出了 JSON」，而是三条负向能力：
    ① 改一个字节 → `mismatch` + 退出码 3；② 文件缺失 → `missing` 且仍非零退出；
    ③ 没有元数据 → 拒绝生成（fail-closed），而不是输出一个字段残缺、看起来正常的包。
    """

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve() / "OmniLore_Data"
        self._saved_key = os.environ.pop("DASHSCOPE_API_KEY", None)
        self.engine = OmniLoreEngine(self.root)
        self.boot = ensure_bootstrapped(self.engine, self.root)
        self.token = self.boot.raw_token
        self.space_id = self.boot.space_id
        self.gateway = OmniLoreMCPGateway(self.engine)
        self.doc_id = await self._ingest("v1.0", "第一版正文：这条答案来自这份原件。")

    async def asyncTearDown(self) -> None:
        if self._saved_key is not None:
            os.environ["DASHSCOPE_API_KEY"] = self._saved_key
        else:
            os.environ.pop("DASHSCOPE_API_KEY", None)
        self._tmp.cleanup()

    async def _ingest(self, version_label: str, text: str, *, replace: bool = False) -> str:
        """入库一份文档（可选替换版本），返回 `doc_id`。"""
        result = await self.gateway.ingest_document_or_file(
            self.token,
            content_bytes=text.encode("utf-8"),
            filename="audit_note.md",
            target_space_id=self.space_id,
            target_category="01_Work",
            version_key="audit_series",
            version_label=version_label,
            operation_type=OperationType.REPLACE_VERSION if replace else OperationType.INGEST_FILE,
            expected_current_doc_id=self.doc_id if replace else None,
        )
        doc_id = result["task"]["result_doc_id"]
        self.assertTrue(doc_id, "入库没有产出 doc_id，后续断言无从进行")
        return str(doc_id)

    def _first_asset(self, bundle: Dict[str, object]) -> Dict[str, object]:
        """取出证据包里第一个原始资产条目。"""
        assets = bundle["source_assets"]
        self.assertTrue(assets, "证据包里没有任何原始资产 —— 可审计性无从谈起")
        return assets[0]

    async def test_bundle_has_required_fields_and_verifies_every_hash(self) -> None:
        """正常导出：六个要求字段齐全、每条检查 `verified`、退出码 0。"""
        bundle = build_audit_bundle(self.engine, self.token, doc_id=self.doc_id)

        document = bundle["documents"][0]
        for field in ("doc_id", "space_id", "version_key", "version_label", "content_revision"):
            self.assertIn(field, document, f"文档条目缺少要求字段 {field}")
        self.assertEqual(document["doc_id"], self.doc_id)
        self.assertEqual(document["version_key"], "audit_series")

        asset = self._first_asset(bundle)
        for field in ("asset_hash", "original_filename", "size_bytes", "ingested_at"):
            self.assertIn(field, asset, f"原始证据缺少要求字段 {field}")
        self.assertEqual(asset["original_filename"], "audit_note.md")

        # 哈希必须是「读磁盘字节重算」的结果，且与库里记录一致
        self.assertEqual(bundle["integrity"]["status"], INTEGRITY_VERIFIED)
        self.assertEqual(bundle["integrity"]["counts"]["mismatch"], 0)
        self.assertTrue(bundle["integrity"]["counts"]["checks_total"] >= 2,
                        "至少要校验 1 个原始资产 + 1 个知识页")
        self.assertEqual(bundle_exit_code(bundle), EXIT_AUDIT_VERIFIED)

        # 生成信息与结构版本
        self.assertIn("generated_at", bundle)
        self.assertIn("version_display", bundle["generator"])
        self.assertEqual(bundle["data_dir"]["name"], "OmniLore_Data")

    async def test_tampered_cas_byte_is_reported_as_mismatch_and_exit_code_is_three(self) -> None:
        """**红线用例**：改掉 CAS 里一个字节 → 该资产 `mismatch`、整包 `mismatch`、退出码 3。

        如果实现只是把数据库里的 `sha256_hash` 抄进 bundle，这条用例会绿得毫无意义 ——
        所以它断言的是「重算出来的哈希与记录不一致」这个**差异**被如实报出来。
        """
        bundle = build_audit_bundle(self.engine, self.token, doc_id=self.doc_id)
        asset = self._first_asset(bundle)
        cas_path = self.root / asset["cas_relative_path"]
        original = cas_path.read_bytes()
        self.assertTrue(original, "CAS 资产为空，无法做篡改测试")
        try:
            cas_path.write_bytes(bytes([original[0] ^ 0xFF]) + original[1:])
            tampered = build_audit_bundle(self.engine, self.token, doc_id=self.doc_id)
            check = next(c for c in tampered["integrity"]["checks"] if c["check_id"].startswith("asset:"))
            self.assertEqual(check["status"], STATUS_MISMATCH,
                             "改动一个字节后状态仍不是 mismatch：bundle 很可能没真的校验")
            self.assertNotEqual(check["computed_sha256"], check["recorded_sha256"])
            self.assertEqual(tampered["integrity"]["status"], INTEGRITY_MISMATCH)
            self.assertEqual(bundle_exit_code(tampered), EXIT_AUDIT_NOT_VERIFIED,
                             "结论为否时进程退出码必须非零（3），不能静默输出一个「正常」的包")
        finally:
            cas_path.write_bytes(original)

        # 改回去必须恢复 verified —— 否则前一步的 mismatch 不能归因于篡改
        restored = build_audit_bundle(self.engine, self.token, doc_id=self.doc_id)
        self.assertEqual(restored["integrity"]["status"], INTEGRITY_VERIFIED)
        self.assertEqual(bundle_exit_code(restored), EXIT_AUDIT_VERIFIED)

    async def test_missing_asset_is_reported_as_missing_not_skipped(self) -> None:
        """资产文件不存在时必须如实标 `missing` 并非零退出，不能跳过。"""
        bundle = build_audit_bundle(self.engine, self.token, doc_id=self.doc_id)
        asset = self._first_asset(bundle)
        cas_path = self.root / asset["cas_relative_path"]
        original = cas_path.read_bytes()
        try:
            cas_path.unlink()
            missing = build_audit_bundle(self.engine, self.token, doc_id=self.doc_id)
            check = next(c for c in missing["integrity"]["checks"] if c["check_id"].startswith("asset:"))
            self.assertEqual(check["status"], STATUS_MISSING)
            self.assertIn("不存在", check["error"])
            self.assertEqual(bundle_exit_code(missing), EXIT_AUDIT_NOT_VERIFIED)
            self.assertFalse(missing["integrity"]["is_pass"])
        finally:
            cas_path.write_bytes(original)

    async def test_bundle_carries_no_token_or_absolute_path(self) -> None:
        """bundle 的数据区不得出现 token / token_hash / 绝对路径。"""
        bundle = build_audit_bundle(self.engine, self.token, doc_id=self.doc_id)
        data_text = scan_text_of_data_sections(bundle)
        self.assertNotIn(self.token, data_text, "证据包里出现了原始 token")
        self.assertNotIn("token", data_text.lower(),
                         "证据包数据区出现了 token 相关字段（token / token_hash 一律不得导出）")
        self.assertNotIn(str(self.root), data_text, "证据包里出现了绝对路径")
        self.assertNotIn(str(Path.home()), data_text, "证据包里出现了用户主目录绝对路径")
        # 相对路径必须是相对的（不允许以盘符或 / 开头）
        for asset in bundle["source_assets"]:
            self.assertFalse(Path(asset["cas_relative_path"]).is_absolute(),
                             f"CAS 路径不是相对路径：{asset['cas_relative_path']}")
        for document in bundle["documents"]:
            self.assertFalse(Path(document["page_relative_path"]).is_absolute(),
                             f"知识页路径不是相对路径：{document['page_relative_path']}")

    async def test_version_chain_lists_every_version_with_its_own_hash(self) -> None:
        """版本链必须给出 `version_key` 序列与每版内容哈希，且两版哈希不同。"""
        await self._ingest("v2.0", "第二版正文：内容变了，哈希也必须变。", replace=True)
        bundle = build_audit_bundle(self.engine, self.token, space_id=self.space_id)

        self.assertEqual(len(bundle["version_chains"]), 1, "同一 series 应聚合为一条版本链")
        chain = bundle["version_chains"][0]
        self.assertEqual(chain["version_key"], "audit_series")
        labels = [version["version_label"] for version in chain["versions"]]
        self.assertEqual(labels, ["v1.0", "v2.0"], f"版本链顺序/内容不对：{labels}")
        hashes = {version["page_sha256_recorded"] for version in chain["versions"]}
        self.assertEqual(len(hashes), 2, "两个版本的内容哈希竟然相同（版本链没反映真实内容）")
        self.assertTrue(any(v["is_current"] for v in chain["versions"]), "版本链没有任何 is_current 版本")

    async def test_verify_markdown_tells_a_third_party_how_to_check(self) -> None:
        """`audit-bundle.verify.md` 必须写明：哪个文件、什么算法、与哪个字段比对。"""
        bundle = build_audit_bundle(self.engine, self.token, doc_id=self.doc_id)
        markdown = render_verification_markdown(bundle)

        self.assertIn("SHA-256", markdown)
        self.assertIn("相对数据目录", markdown)
        self.assertIn("sha256sum", markdown, "复核说明没有给出可直接执行的校验命令")
        self.assertIn("certutil", markdown, "复核说明只给了 POSIX 命令，Windows 用户无从复核")
        step = bundle["how_to_verify"]["steps"][0]
        self.assertIn(step["relative_path"], markdown, "复核说明没有写出具体文件路径")
        self.assertIn(step["compare_to"], markdown, "复核说明没有写明「与哪个字段比对」")
        self.assertNotIn(str(self.root), markdown, "复核说明里出现了绝对路径")
        # 边界必须写明：证据包不证明什么
        self.assertIn("what_this_does_not_prove", bundle["how_to_verify"])
        self.assertIn("不证明", markdown)

    async def test_exported_files_land_in_the_output_directory(self) -> None:
        """导出必须落两个文件（JSON + 复核说明），且 JSON 可被独立解析。"""
        bundle = build_audit_bundle(self.engine, self.token, doc_id=self.doc_id)
        out_dir = Path(self._tmp.name) / "bundle_out"
        json_path, verify_path = write_audit_bundle(bundle, out_dir)

        self.assertTrue(json_path.is_file(), f"证据包未落盘：{json_path}")
        self.assertTrue(verify_path.is_file(), f"复核说明未落盘：{verify_path}")
        self.assertEqual(json_path.name, AUDIT_BUNDLE_FILENAME)
        self.assertEqual(verify_path.name, AUDIT_VERIFY_FILENAME)
        reloaded = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(reloaded["integrity"]["status"], INTEGRITY_VERIFIED)
        self.assertEqual(reloaded["bundle_schema_version"], "1.0")


class TestAuditBundleCli(unittest.TestCase):
    """R-05m：CLI 入口的 fail-closed 行为与离线探针（真实子进程）。

    为什么不把它写进上面那组：`cli_main` 内部自己 `asyncio.run()`，在
    `IsolatedAsyncioTestCase` 的事件循环里调用会炸；而且这里要的正是「真实进程边界」。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve() / "OmniLore_Data"
        self._saved_key = os.environ.pop("DASHSCOPE_API_KEY", None)

    async def _ingest(self, engine: OmniLoreEngine, token: str, space_id: str) -> str:
        """在一个已引导的引擎上入库一份文档，返回 `doc_id`（供 CLI 成功路径用例使用）。"""
        result = await OmniLoreMCPGateway(engine).ingest_document_or_file(
            token,
            content_bytes="CLI 成功路径正文：证据包应当落盘。".encode("utf-8"),
            filename="cli_note.md",
            target_space_id=space_id,
            target_category="01_Work",
            version_key="cli_series",
            version_label="v1.0",
        )
        return str(result["task"]["result_doc_id"])

    def tearDown(self) -> None:
        if self._saved_key is not None:
            os.environ["DASHSCOPE_API_KEY"] = self._saved_key
        else:
            os.environ.pop("DASHSCOPE_API_KEY", None)
        self._tmp.cleanup()

    def test_cli_refuses_to_emit_a_partial_bundle_without_metadata(self) -> None:
        """索引缺失（从没入库过）时必须 fail-closed，并给出「先入库」的可执行指引。

        这是本功能最容易被做歪的地方：没有元数据时「尽力导出」会得到一个字段缺失、
        却看起来正常的证据包 —— 那比没有更糟。同时提示不能把「还没入库」
        说成「数据损坏」，否则用户会以为数据坏了而不敢继续。
        """
        # bootstrap 会把 token 与主权回显打到 stdout / stderr，测试里全部收走
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            cli_main(["bootstrap", "--data-dir", str(self.root), "--quiet"])
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            rc = cli_main(["audit-bundle", "--data-dir", str(self.root)])
        self.assertEqual(rc, 1, f"没有元数据时应当以 1 失败：{err.getvalue()}")
        self.assertIn("AUDIT_BUNDLE_NO_METADATA", err.getvalue())
        self.assertIn("omnilore demo", err.getvalue(), "失败信息必须给出可执行的下一步")
        self.assertNotIn("损坏", err.getvalue(), "「还没入库」不能被说成「数据损坏」")
        self.assertFalse((self.root / "ops" / "audit_bundles" / AUDIT_BUNDLE_FILENAME).exists(),
                         "拒绝导出的情况下不应留下任何证据包文件")

    def test_cli_exports_a_bundle_and_exits_zero_on_success(self) -> None:
        """CLI 成功路径：写入两个文件、打印结论、退出码 0。

        这里用 `mock.patch` 把 CLI 内部的引擎换成本测试已经入库过的实例 —— 这是唯一能在
        单进程里跑到 CLI 成功分支的办法（默认内存引擎不跨进程持久化，见上一个用例）。
        它覆盖的是 CLI 自身的「写文件 + 打印 + 退出码」这段胶水代码。
        """
        engine = OmniLoreEngine(self.root)
        boot = ensure_bootstrapped(engine, self.root)
        asyncio.run(self._ingest(engine, boot.raw_token, boot.space_id))
        out_dir = Path(self._tmp.name) / "cli_out"
        out = io.StringIO()
        with mock.patch("omnilore.cli.OmniLoreEngine", return_value=engine):
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                rc = cli_main(["audit-bundle", "--data-dir", str(self.root), "--out", str(out_dir)])

        self.assertEqual(rc, 0, f"成功导出应以 0 退出：\n{out.getvalue()}")
        self.assertTrue((out_dir / AUDIT_BUNDLE_FILENAME).is_file())
        self.assertTrue((out_dir / AUDIT_VERIFY_FILENAME).is_file())
        printed = out.getvalue()
        self.assertIn("结论", printed)
        self.assertIn("verified", printed)
        self.assertIn("复核说明", printed)

    def test_offline_probe_recomputes_hashes_from_disk(self) -> None:
        """`tools/verify_audit_bundle.py` 在真实子进程里跑通（`make verify-audit` 的同一条链路）。"""
        env = dict(os.environ)
        env.pop("DASHSCOPE_API_KEY", None)
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.run(
            [sys.executable, "tools/verify_audit_bundle.py"],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(proc.returncode, 0, f"证据包探针未通过：\n{proc.stdout}\n{proc.stderr}")
        self.assertIn("[PASS]", proc.stdout)
        # 篡改测试必须真的被执行到，否则「通过了」这件事没有意义
        self.assertIn("篡改", proc.stdout)
        self.assertIn("mismatch", proc.stdout)


class TestRlsPolicyComments(unittest.TestCase):
    """R-08b 补课：21 条 RLS 策略必须有注释，且策略逻辑被钉住不许悄悄改。

    为什么值得单开一组：这 21 条策略是真实 PostgreSQL 验证的基线（2026-10-24 三条探针）。
    **注释缺失**会让贡献者不敢改、也看不懂自己在改什么；**逻辑被悄悄改动**则会让
    「已验证」这句话当场过期 —— 而后者不会有任何报错。

    口径与 `docs/FEATURE_STATUS.md` 一致：只有 space + category 两维被真实探针验证过，
    因此每条注释都必须写清「依赖哪个维度」，其余维度要注明未验证。
    """

    #: 冻结的策略语句摘要：注释可以改，**策略逻辑一个字都不许动**。
    #: 改动本常量前请先重跑 `make verify-rls-pg`（真实 PG）并同步
    #: `docs/FEATURE_STATUS.md` 第 6 节的证据记录，否则「已验证」就是过期宣称。
    FROZEN_POLICY_DIGEST: str = "947f8c5b9b74a2e0264887c3f1afc40ada020b66b33bf33b6223d5d470605957"

    #: 四段注释各自的关键词：缺任何一段，贡献者就会漏掉一类边界。
    REQUIRED_MARKERS = ("保护", "依赖维度", "防：", "不覆盖")

    def setUp(self) -> None:
        self.sql_path = REPO_ROOT / "sql" / "001_omnilore_schema_and_rls.sql"
        self.text = _read(self.sql_path)
        self.lines = self.text.splitlines()

    @staticmethod
    def _policy_statements(sql_text: str) -> List[str]:
        """抽出全部 `CREATE POLICY ... ;` 语句（注释不参与），用于比对逻辑是否被改过。"""
        statements = [" ".join(match.group(0).split()) for match in re.finditer(r"CREATE POLICY\b.+?;", sql_text, re.S)]
        return statements

    def _comment_block_above(self, index: int) -> List[str]:
        """向上收集紧贴在第 `index` 行之上的连续 `--` 注释行（按原顺序返回）。"""
        block: List[str] = []
        cursor = index - 1
        while cursor >= 0 and self.lines[cursor].lstrip().startswith("--"):
            block.append(self.lines[cursor])
            cursor -= 1
        return list(reversed(block))

    def _policy_line_indexes(self) -> List[int]:
        """返回每条 `CREATE POLICY` 语句所在的行号（0 基）。"""
        return [i for i, line in enumerate(self.lines) if line.startswith("CREATE POLICY")]

    def test_there_are_exactly_21_policies(self) -> None:
        """策略条数必须仍是 21 —— 改动条数等于改动被验证过的基线范围。"""
        self.assertEqual(len(self._policy_statements(self.text)), 21)
        self.assertEqual(self.text.count("CREATE POLICY"), 21)

    def test_every_policy_has_a_comment_within_three_lines_above(self) -> None:
        """每条 `CREATE POLICY` 上方 3 行内必须有注释（防止以后有人加策略不写注释）。"""
        indexes = self._policy_line_indexes()
        self.assertEqual(len(indexes), 21, f"策略条数应为 21，实际 {len(indexes)}")
        missing = []
        for index in indexes:
            window = self.lines[max(0, index - 3):index]
            if not any(line.lstrip().startswith("--") for line in window):
                missing.append(f"第 {index + 1} 行：{self.lines[index]}")
        self.assertFalse(missing, "以下策略上方 3 行内没有注释：\n" + "\n".join(missing))

    def test_every_policy_comment_explains_the_four_required_things(self) -> None:
        """每条策略的注释必须覆盖四件事：保护什么 / 依赖哪个维度 / 防什么 / 不覆盖什么。"""
        problems: List[str] = []
        for index in self._policy_line_indexes():
            block = "\n".join(self._comment_block_above(index))
            policy_name = self.lines[index].split()[2]
            if f"[{policy_name}]" not in block:
                problems.append(f"{policy_name}: 注释没有点名策略名（读者无法把注释与策略对上）")
            for marker in self.REQUIRED_MARKERS:
                if marker not in block:
                    problems.append(f"{policy_name}: 注释缺少「{marker}」这一段")
        self.assertFalse(problems, "策略注释不完整：\n" + "\n".join(problems))

    def test_policy_logic_is_frozen_against_silent_edits(self) -> None:
        """策略逻辑（去掉注释后）必须与冻结摘要逐字一致 —— 改逻辑会让「已验证」过期。"""
        statements = self._policy_statements(self.text)
        digest = hashlib.sha256("\n".join(statements).encode("utf-8")).hexdigest()
        self.assertEqual(
            digest, self.FROZEN_POLICY_DIGEST,
            "21 条 RLS 策略的逻辑被改动了。注释可以改，逻辑不能 —— 那批策略是 2026-10-24 "
            "真实 PostgreSQL 探针的验证基线。若确实要改：先重跑 `make verify-rls-pg`，"
            "把新证据写进 docs/FEATURE_STATUS.md 第 6 节，再更新本测试里的 FROZEN_POLICY_DIGEST。",
        )

    def test_comments_state_the_probed_dimension_boundary(self) -> None:
        """注释必须写明「只有哪些维度被真实探针验证过」，不许含糊成「权限已全部验证」。"""
        probed = [i for i in self._policy_line_indexes() if "space + category" in "\n".join(self._comment_block_above(i))]
        self.assertEqual(len(probed), 21, "每条策略的注释都应写明它依赖的维度是 space + category")
        # user 维度的三条策略（tasks_*）必须明说「没有真实 PG 探针覆盖」
        task_notes = [i for i in self._policy_line_indexes() if "tasks_" in self.lines[i]]
        self.assertEqual(len(task_notes), 3)
        for index in task_notes:
            block = "\n".join(self._comment_block_above(index))
            self.assertIn("真实 PG 探针覆盖", block,
                          f"{self.lines[index].split()[2]} 用了 authz.current_user_id()，"
                          "注释必须说明该维度未被真实探针覆盖")


class TestAuditBundleCrossProcess(unittest.TestCase):
    """R-05m：证据包必须**跨进程**可用（`omnilore demo` 在进程 A，`audit-bundle` 在进程 B）。

    为什么这一组必须用 `subprocess` 起独立进程：之前几组都跑在同一个进程里，
    内存引擎的结构化状态还在，问题被掩盖了 —— 用户真实用法是「装完 pip，跑完
    `bootstrap` + `demo`，第二天再导证据包」，那时数据库是空的。

    修复方式：内存引擎在提交时把「文档 ↔ 原始资产」最小映射写到 `ops/index/`
    （`omnilore/runtime_index.py`），`audit-bundle` 在全新进程里从这份索引重建范围，
    但**哈希仍然读磁盘真实字节重算**。这一组逐条把这条链路钉住：

    1. 跨进程正常导出 → `integrity.overall == verified`、退出码 0；
    2. 跨进程篡改 1 个字节 → 该资产 `mismatch`、退出码 3（证明校验没退化成「信索引」）；
    3. 删掉 `ops/index/` → 退出码 1，且提示「先入库」，不把「还没入库」说成「数据坏了」；
    4. `ops/index/` 内无 token / 无绝对路径。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.work = Path(self._tmp.name).resolve()
        self.data_dir = self.work / "OmniLore_Data"
        self.index_dir = self.data_dir / "ops" / "index"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run_omnilore(self, *args: str) -> subprocess.CompletedProcess:
        """在**独立进程**里执行 `python -m omnilore ...`（零 API Key）。"""
        env = dict(os.environ)
        env.pop("DASHSCOPE_API_KEY", None)
        env.pop("OMNILORE_DATA_DIR", None)
        # 与上面同理：子进程 stdout 钉成 UTF-8，父进程也按 UTF-8 解码，
        # 这样测试就不再依赖宿主的代码页（cp936 / cp1252 / UTF-8 都一样）。
        env["PYTHONIOENCODING"] = "utf-8"
        return subprocess.run(
            [sys.executable, "-m", "omnilore", *args],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )

    def _bootstrap_and_ingest(self) -> None:
        """进程 A：bootstrap + demo（入库一份文档），断言两步都成功。"""
        boot = self._run_omnilore("bootstrap", "--data-dir", str(self.data_dir), "--quiet")
        self.assertEqual(boot.returncode, 0, f"bootstrap 失败：{boot.stderr}")
        demo = self._run_omnilore("demo", "--data-dir", str(self.data_dir), "--json")
        self.assertEqual(demo.returncode, 0, f"demo 失败：{demo.stderr}")
        self.assertTrue((self.index_dir / "assets.json").is_file(),
                        "入库后没有写出运行时索引：跨进程导出必然不可用")

    def _export(self, name: str) -> tuple:
        """进程 B/C/D：导出证据包，返回 `(CompletedProcess, bundle dict 或 None)`。"""
        out_dir = self.work / name
        proc = self._run_omnilore("audit-bundle", "--data-dir", str(self.data_dir), "--out", str(out_dir))
        json_path = out_dir / "audit-bundle.json"
        bundle = json.loads(json_path.read_text(encoding="utf-8")) if json_path.is_file() else None
        return proc, bundle

    def _first_cas_path(self) -> Path:
        """从运行时索引里取出第一个 CAS 文件的绝对路径（篡改测试的目标）。"""
        payload = json.loads((self.index_dir / "assets.json").read_text(encoding="utf-8"))
        rel = next(iter(payload["assets"].values()))["cas_rel_path"]
        return self.data_dir / rel

    def test_fresh_process_exports_a_verified_bundle(self) -> None:
        """场景 B：进程 A 入库 → 进程 B 导出 → 退出码 0、`integrity.overall == verified`。"""
        self._bootstrap_and_ingest()
        proc, bundle = self._export("bundle_ok")

        self.assertEqual(proc.returncode, 0, f"跨进程导出失败：\n{proc.stdout}\n{proc.stderr}")
        self.assertIsNotNone(bundle, "跨进程导出没有落出 audit-bundle.json")
        self.assertEqual(bundle["integrity"]["overall"], "verified")
        self.assertEqual(bundle["integrity"]["status"], "verified")
        self.assertEqual(bundle["metadata_source"]["source"], "runtime_index")
        self.assertEqual(bundle["integrity"]["counts"]["documents"], 1)
        self.assertEqual(bundle["integrity"]["counts"]["source_assets"], 1)
        self.assertEqual(bundle["integrity"]["counts"]["mismatch"], 0)
        asset = bundle["source_assets"][0]
        self.assertEqual(asset["integrity_status"], "verified")
        self.assertTrue(asset["original_filename"], "跨进程导出丢了原始文件名")
        self.assertTrue(asset["ingested_at"], "跨进程导出丢了入库时间")
        # 索引是派生物这件事必须写进 bundle 自身
        self.assertTrue(any("派生物" in line for line in bundle["limitations"]))

    def test_fresh_process_detects_a_tampered_byte(self) -> None:
        """场景 C：跨进程再改 1 个字节 → 该资产 `mismatch`、整包 `mismatch`、退出码 3。

        这条是「跨进程也真的在校验」的证明 —— 若实现只是照抄索引里的 `sha256_hash`，
        这里会以 0 退出并报 verified。
        """
        self._bootstrap_and_ingest()
        cas_path = self._first_cas_path()
        original = cas_path.read_bytes()
        try:
            cas_path.write_bytes(bytes([original[0] ^ 0xFF]) + original[1:])
            proc, bundle = self._export("bundle_tampered")
            self.assertEqual(proc.returncode, 3, f"篡改后应以 3 退出：\n{proc.stdout}\n{proc.stderr}")
            self.assertIsNotNone(bundle)
            self.assertEqual(bundle["integrity"]["overall"], "mismatch")
            statuses = {asset["integrity_status"] for asset in bundle["source_assets"]}
            self.assertEqual(statuses, {"mismatch"}, f"资产状态应为 mismatch，实际 {statuses}")
            check = next(c for c in bundle["integrity"]["checks"] if c["kind"] == "source_asset")
            self.assertNotEqual(check["computed_sha256"], check["recorded_sha256"])
        finally:
            cas_path.write_bytes(original)

    def test_fresh_process_without_index_says_ingest_first(self) -> None:
        """场景 D：删掉 `ops/index/` → 退出码 1，且提示「先入库」，不冒充「数据损坏」。"""
        self._bootstrap_and_ingest()
        shutil.rmtree(self.index_dir)
        proc, bundle = self._export("bundle_no_index")

        self.assertEqual(proc.returncode, 1, f"没有索引时应 fail-closed：\n{proc.stdout}\n{proc.stderr}")
        self.assertIsNone(bundle, "拒绝导出时不应留下证据包文件")
        self.assertIn("AUDIT_BUNDLE_NO_METADATA", proc.stderr)
        self.assertIn("omnilore demo", proc.stderr, "提示必须告诉用户下一步该干什么")
        self.assertNotIn("损坏", proc.stderr, "「还没入库」不能被说成「数据损坏」")

    def test_runtime_index_contains_no_secrets_or_absolute_paths(self) -> None:
        """`ops/index/` 是派生物，同样不许含 token / token_hash / 绝对路径。"""
        self._bootstrap_and_ingest()
        state = json.loads((self.data_dir / "ops" / "bootstrap.json").read_text(encoding="utf-8"))
        raw_token = state["raw_token"]
        for name in ("assets.json", "document_assets.json"):
            text = (self.index_dir / name).read_text(encoding="utf-8")
            self.assertNotIn(raw_token, text, f"{name} 里出现了原始 token")
            self.assertNotIn("token", text.lower(), f"{name} 里出现了 token 相关字段")
            self.assertNotIn(str(self.data_dir), text, f"{name} 里出现了绝对路径")
            self.assertNotIn(str(Path.home()), text, f"{name} 里出现了用户主目录绝对路径")

        payload = json.loads((self.index_dir / "assets.json").read_text(encoding="utf-8"))
        for entry in payload["assets"].values():
            self.assertTrue(entry["cas_rel_path"].startswith("ops/cas/"),
                            f"索引里的 CAS 路径不是相对路径：{entry['cas_rel_path']}")
        docs = json.loads((self.index_dir / "document_assets.json").read_text(encoding="utf-8"))
        for entry in docs["documents"].values():
            self.assertFalse(Path(entry["wiki_rel_path"]).is_absolute(),
                             f"索引里的 vault 路径不是相对路径：{entry['wiki_rel_path']}")
        # 索引自述「我是派生物」——防止以后有人把它当成权威数据源
        self.assertIn("派生物", payload["note"])
        self.assertIn("派生物", docs["note"])
