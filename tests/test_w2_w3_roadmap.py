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

为什么这些也要写成测试：本项目的头号风险是「宣称与证据不匹配」。
靠人肉同步文档与代码必然失败 —— 所以把它变成测试。
"""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import os
from pathlib import Path
import re
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
    ProfileType,
    RoleType,
    SpaceRecord,
    SpaceType,
    build_conflict_copy_rel_path,
    get_conflict_copy_dir,
    parse_conflict_copy_dir,
    parse_protected_frontmatter_keys,
)
from omnilore.cli import main as cli_main

REPO_ROOT: Path = Path(__file__).resolve().parent.parent
README_PATH: Path = REPO_ROOT / "README.md"
CHANGELOG_PATH: Path = REPO_ROOT / "CHANGELOG.md"
MATRIX_PATH: Path = REPO_ROOT / "docs" / "FEATURE_STATUS.md"
THREAT_MODEL_PATH: Path = REPO_ROOT / "docs" / "THREAT_MODEL.md"
MAKEFILE_PATH: Path = REPO_ROOT / "Makefile"

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

    def test_readme_and_changelog_report_the_same_statistics(self) -> None:
        """README 与 CHANGELOG 都必须复述矩阵里的同一组数字 —— 三处一致。"""
        rows = self._matrix_rows()
        for label, text in (("README", self.readme), ("CHANGELOG", self.changelog)):
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
            # RLS 在 2026-10-24 的真实 PG 验证后升为 Tested-real；但只能以「人工一次性
            # 证据」的身份出现，不许写成「已由 CI 验证」。
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

        # ①b RLS 升级必须自带「人工 / CI 待接入」这层限定，不许变成持续证据的口吻
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
        self.assertIn("手动验证", rls_claim, "RLS 的 Tested-real 必须写明是「手动验证」")
        self.assertIn("CI", rls_claim, "RLS 的 Tested-real 必须写明 CI 尚未接入")

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
            f"「{upgraded}」应依据 2026-10-24 真实 PG 验证标 Tested-real",
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
            rows.get("真实 PostgreSQL 集成 CI"), "Planned",
            "自动化集成 CI 尚未接入，不能标成已实现",
        )

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

    def test_every_make_target_mentioned_in_docs_exists(self) -> None:
        """文档里写给用户跑的 `make xxx` 必须真的存在（不许写假命令）。"""
        makefile = _read(MAKEFILE_PATH)
        mentioned: set[str] = set()
        for text in (self.readme, self.matrix):
            mentioned.update(re.findall(r"`?make\s+([a-z0-9-]+)`?", text))
        self.assertTrue(mentioned, "文档里没有任何 make 验证命令")
        for target in sorted(mentioned):
            self.assertTrue(
                re.search(rf"^{re.escape(target)}:", makefile, re.M),
                f"Makefile 缺少目标 {target}",
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
        proc = subprocess.run(
            [sys.executable, "tools/verify_rls_pg.py"],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 2, f"缺少数据库时应当以 2（未验证）退出：{proc.stdout}")
        self.assertIn("未验证", proc.stdout)
