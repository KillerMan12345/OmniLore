"""
OmniLore v0.3.0-alpha — Open-Source Launch W0 Regression Tests
================================================================
The pre-existing suites (`test_omnilore_phase0_acceptance.py`,
`test_full_omnilore_suite.py`) verify the Phase 0~3 acceptance criteria, but they
were written **before** W0 and therefore touch none of the newly added launch surface.

This module closes that gap (QA finding P1-5). It covers exactly the things a W0
regression must catch:

  1. Bootstrap idempotency & token reuse
  2. Canonical LF-only text I/O (the cross-platform CRLF fix)
  3. Version identifier consistency across pyproject / package / MCP handshake
  4. Zero-API-key degradation (`retrieval_mode` / `degraded_reason`)
  5. Graceful degradation when the remote provider fails (invalid/expired/unreachable)
  6. `OMNILORE_DATA_DIR` resolution agreement between the CLI and the MCP server
  7. CLI entry point wiring + end-to-end zero-key `demo`

All tests are fully offline: no network, no real API key, no PostgreSQL.
"""

from __future__ import annotations

from contextlib import redirect_stdout
import importlib
import inspect
import io
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import tomllib
import unittest
import warnings
from unittest import mock

from omnilore import (
    OmniLoreEngine,
    OmniLoreMCPGateway,
    SpaceRecord,
    SpaceType,
    MembershipRecord,
    ModelAdapter,
    ModelAdapterConfig,
    RoleType,
    ProfileType,
    __version__,
    __version_display__,
    __version_label__,
    read_text_canonical,
    write_text_canonical,
)
from omnilore.bootstrap import ensure_bootstrapped, load_bootstrap_state
from omnilore.cli import main
from omnilore.dashscope_client import DashScopeHTTPTransport
from omnilore.mcp_stdio_server import SERVER_VERSION
from omnilore.pathutil import resolve_data_dir


class TestBootstrapIdempotency(unittest.TestCase):
    """Bootstrap must be repeatable and must reuse the persisted token."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name) / "OmniLore_Data"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _engine(self) -> OmniLoreEngine:
        return OmniLoreEngine(self.data_dir)

    def test_bootstrap_is_idempotent_and_reuses_token(self) -> None:
        first = ensure_bootstrapped(self._engine(), self.data_dir)
        self.assertTrue(first.created_space)
        self.assertTrue(first.created_token)
        self.assertEqual(first.retrieval_mode, "keyword_only")
        self.assertNotEqual(first.degraded_reason, "")

        second_engine = self._engine()
        second = ensure_bootstrapped(second_engine, self.data_dir)
        self.assertEqual(second.raw_token, first.raw_token, "token must be reused on re-run")
        self.assertTrue(second.reused_state)
        self.assertFalse(second.created_token, "no new token should be minted when one exists")
        # A fresh process always re-registers the space in the in-memory engine; that is
        # expected, since only the bootstrap state file survives across processes.
        self.assertTrue(second.created_space)

        # The reused token must actually authorize against a fresh engine.
        scope = second_engine.auth.resolve_scope(second.raw_token)
        self.assertEqual(scope.user_id, second.user_id)
        self.assertIn(second.space_id, scope.read_spaces)
        self.assertIn(second.space_id, scope.write_spaces)

    def test_bootstrap_state_file_is_persisted_and_loadable(self) -> None:
        """引导状态必须真的落盘并可被另一个「进程」读回（P1-1 的根因）。

        注意：本用例只承诺**持久化与可加载**，不承诺权限收紧（后者见下一条）。
        名字曾写作 `..._readable_back`，暗示了并未断言的权限语义，已按实际断言改名。
        """
        result = ensure_bootstrapped(self._engine(), self.data_dir)
        self.assertTrue(result.state_path.exists(), "bootstrap 状态必须落盘")
        state = load_bootstrap_state(self.data_dir)
        self.assertIsNotNone(state)
        self.assertEqual(state["raw_token"], result.raw_token)
        self.assertEqual(state["space_id"], result.space_id)

    def test_permissions_tightening_never_locks_out_owner(self) -> None:
        """收紧权限后 owner 必须仍能读写。

        这是 P1-3 修复过程中踩到的真实反向 bug：直接用 `%USERNAME%` 授权会被 icacls
        解析成空账户，随后 `/inheritance:r` 清掉继承 ACE → 文件谁都读不了。
        当前实现有「授权 → 验证 → 断继承 → 再验证 → 失败 /reset 回滚」保护，
        因此这里断言的是**不变量**：收紧之后 owner 仍然**可读且可写**。
        """
        from omnilore.bootstrap import restrict_secret_file_permissions

        secret = self.data_dir / "ops" / "secret.json"
        secret.parent.mkdir(parents=True, exist_ok=True)
        secret.write_text('{"k": "v"}', encoding="utf-8")

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            restrict_secret_file_permissions(secret)

        # 收紧失败时应当只是 warning，绝不能抛异常、也不能让文件不可用。
        self.assertTrue(secret.exists())
        self.assertEqual(json.loads(secret.read_text(encoding="utf-8")), {"k": "v"})
        # 若宿主不支持收紧，应当给出可诊断的 warning 而不是静默失败。
        if os.name == "nt":
            unsup = [w for w in caught if "无法收紧" in str(w.message)]
            self.assertTrue(unsup or not caught, f"出现无法解释的 warning: {[str(w.message) for w in caught]}")

    def _assert_file_is_tightened(self, path: Path, unsupported_ok: bool = True) -> None:
        """共用断言：确认 `path` 的权限**真的**被收紧了。

        Windows 锚点必须是「收紧前存在、收紧后消失」的 ACE —— `tempfile` 目录的
        **默认** ACL 里没有 Everyone/BUILTIN\\Users（断言它们为空是空转断言，
        QA 实测指出），但**确实**有 NT AUTHORITY\\SYSTEM 与 BUILTIN\\Administrators
        的继承 ACE，只有 `/inheritance:r` 真正生效后它们才会消失。

        Args:
            path: 被收紧的文件。
            unsupported_ok: 宿主明确不支持收紧时是否跳过 ACE 断言（读写断言仍然生效）。
        """
        listing = subprocess.run(["icacls", str(path)], capture_output=True, check=False, timeout=30)
        text = (listing.stdout or b"").decode("utf-8", "replace")
        if os.name == "nt":
            if unsupported_ok and not text.strip():
                self.skipTest("宿主无法提供 icacls 输出，跳过 ACE 断言")
            self.assertNotIn("NT AUTHORITY\\SYSTEM", text, "收紧后不应残留继承的 SYSTEM ACE")
            self.assertNotIn("BUILTIN\\Administrators", text, "收紧后不应残留继承的 Administrators ACE")
            self.assertNotIn("Everyone", text, "收紧后不应残留 Everyone ACE")
            self.assertNotIn("BUILTIN\\Users", text, "收紧后不应残留 BUILTIN\\Users ACE")
        else:
            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(mode & 0o777, 0o600, f"POSIX 下应为 0600，实为 {oct(mode)}")

    def test_bootstrap_state_file_is_tightened_end_to_end(self) -> None:
        """端到端守卫：bootstrap 真正产出的 token 文件必须是被收紧过的。

        补上一个真实缺口：`test_permissions_are_actually_tightened` 是**直接调用**
        收紧函数，因此把 `save_bootstrap_state()` 里的调用整行删掉时它照样绿
        （QA 变异 M18）。这里改从 `ensure_bootstrapped()` 的真实产物上断言，
        保证「调用点」本身也在守卫范围内。
        """
        result = ensure_bootstrapped(self._engine(), self.data_dir)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            # 再跑一次以确保当前文件的收紧状态是本次调用的结果
            ensure_bootstrapped(self._engine(), self.data_dir)
        unsupported = any("无法收紧" in str(w.message) for w in caught)
        if unsupported and os.name == "nt":
            self.skipTest("宿主不支持收紧权限，跳过 ACE 断言")
        self._assert_file_is_tightened(result.state_path, unsupported_ok=False)
        # 读写双向可用（跨进程重写场景）
        state = json.loads(result.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["space_id"], result.space_id)

    def test_permissions_are_actually_tightened(self) -> None:
        """回归守卫（QA P2-1）：确认权限**真的被收紧**，而不只是「没搞坏」。

        QA 的变异测试发现：把 `restrict_secret_file_permissions(path)` 整行删掉后
        所有测试依然全绿 —— 也就是说这个唯一「会造成真实破坏」的改动当时是裸奔的。
        本用例直接断言收紧效果：
          * POSIX    → mode 含 0600（owner 读写）
          * Windows  → ACE 不再含 Everyone / BUILTIN\\Users，且 owner 可读**可写**
                       （可写很关键：`ensure_bootstrapped` 每次启动都会重写该文件，
                        写不进去 MCP 服务就起不来了）
        宿主明确不支持收紧时（有 warning 且未断言 ACE）跳过 ACE 检查，但读写检查仍生效。
        """
        from omnilore.bootstrap import restrict_secret_file_permissions

        secret = self.data_dir / "ops" / "secret.json"
        secret.parent.mkdir(parents=True, exist_ok=True)
        secret.write_text('{"k": "v"}', encoding="utf-8")

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            restrict_secret_file_permissions(secret)

        unsupported = any("无法收紧" in str(w.message) for w in caught)

        if os.name == "nt":
            if not unsupported:
                listing = subprocess.run(
                    ["icacls", str(secret)], capture_output=True, check=False, timeout=30
                )
                text = (listing.stdout or b"").decode("utf-8", "replace")
                # 锚点必须是「收紧前存在、收紧后消失」的 ACE。
                # 注意：`tempfile` 目录下的**默认** ACL 里根本没有 Everyone / BUILTIN\Users，
                # 所以断言「不含 Everyone」在完全没收紧时也会通过 —— 那是空转断言（QA 实测）。
                # 该目录默认**确实**带 NT AUTHORITY\SYSTEM 与 BUILTIN\Administrators 的
                # 继承 ACE，它们只有在 `/inheritance:r` 真正生效后才会消失，
                # 因此用它们做锚点，no-op / 提前 return / 删掉断继承 三种变异都会被抓到。
                self.assertNotIn("NT AUTHORITY\\SYSTEM", text, "收紧后不应残留继承的 SYSTEM ACE")
                self.assertNotIn("BUILTIN\\Administrators", text, "收紧后不应残留继承的 Administrators ACE")
                self.assertNotIn("Everyone", text, "收紧后不应残留 Everyone ACE")
                self.assertNotIn("BUILTIN\\Users", text, "收紧后不应残留 BUILTIN\\Users ACE")
        else:
            if not unsupported:
                mode = stat.S_IMODE(secret.stat().st_mode)
                self.assertEqual(mode & 0o777, 0o600, f"POSIX 下应为 0600，实为 {oct(mode)}")

        # 读写双向都必须可用（跨进程重写场景）
        self.assertEqual(json.loads(secret.read_text(encoding="utf-8")), {"k": "v"}, "收紧后 owner 必须仍可读")
        secret.write_text('{"k": "v2"}', encoding="utf-8")
        self.assertEqual(json.loads(secret.read_text(encoding="utf-8"))["k"], "v2", "收紧后 owner 必须仍可写")

    def test_explicit_token_is_honoured_and_survives(self) -> None:
        explicit = "tok_explicitly_provided_by_user"
        result = ensure_bootstrapped(self._engine(), self.data_dir, token=explicit)
        self.assertEqual(result.raw_token, explicit)
        reloaded = ensure_bootstrapped(self._engine(), self.data_dir)
        self.assertEqual(reloaded.raw_token, explicit, "explicit token must be persisted too")


class TestCanonicalTextIO(unittest.IsolatedAsyncioTestCase):
    """The CRLF regression guard (A-3)."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    def test_write_text_canonical_emits_lf_only_bytes(self) -> None:
        target = self.dir / "nested" / "note.md"
        text = "# 标题\r\n\r\n第一行。\r\n第二行。\n"
        write_text_canonical(target, text)

        raw = target.read_bytes()
        self.assertNotIn(b"\r", raw, "canonical writes must contain no CR byte at all")
        self.assertIn(b"\n", raw)
        # The bytes on disk must equal exactly what we hash.
        import hashlib

        self.assertEqual(hashlib.sha256(raw).hexdigest(), hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest())

    async def test_read_and_write_roundtrip_is_stable(self) -> None:
        target = self.dir / "roundtrip.md"
        original = "第一行\n第二行\n"
        write_text_canonical(target, original)
        self.assertEqual(read_text_canonical(target), original)

        # Even if someone hand-edits the file with CRLF, reading canonicalizes back.
        target.write_bytes("第一行\r\n第二行\r\n".encode("utf-8"))
        self.assertEqual(read_text_canonical(target), original)

    async def test_storage_consistency_after_ingest(self) -> None:
        """End-to-end: ingested wiki bytes hash-match `documents.file_hash`."""
        root = self.dir / "OmniLore_Data"
        engine = OmniLoreEngine(root)
        space_id = "sp_w0_crlf"
        engine.auth.register_space(
            SpaceRecord(space_id=space_id, space_type=SpaceType.PERSONAL, parent_space_id=None, vault_rel_path=f"spaces/{space_id}")
        )
        engine.auth.grant_membership(
            MembershipRecord(membership_id="m1", user_id="u1", space_id=space_id, role=RoleType.ADMIN, allowed_categories={"01_Work"})
        )
        token = "tok_w0_crlf"
        engine.auth.issue_token(
            token,
            user_id="u1",
            profile_type=ProfileType.OMNI,
            allowed_read_spaces={space_id},
            allowed_write_spaces={space_id},
            allowed_categories={"01_Work"},
        )
        gw = OmniLoreMCPGateway(engine)
        await gw.ingest_document_or_file(
            token,
            content_bytes="# 标题\n\n第一行。\n第二行。\n".encode("utf-8"),
            filename="crlf.md",
            target_space_id=space_id,
            target_category="01_Work",
        )

        report = engine.ops.reconcile_consistency()
        self.assertTrue(report["is_consistent"], f"reconcile must be consistent, got {report}")
        self.assertEqual(report["hash_mismatches"], [])

        md_files = list(root.rglob("*.md"))
        self.assertTrue(md_files, "expected at least one wiki markdown file")
        for md in md_files:
            self.assertNotIn(b"\r\n", md.read_bytes(), f"{md} must be LF-only")


class TestVersionConsistency(unittest.TestCase):  # sync: no async deps
    """PEP 440 internal version vs. advertised release label."""

    def setUp(self) -> None:
        self.pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"

    def test_pyproject_matches_package_version(self) -> None:
        if not self.pyproject.exists():
            self.skipTest("pyproject.toml not present in this checkout")
        version = tomllib.loads(self.pyproject.read_text(encoding="utf-8"))["project"]["version"]
        self.assertEqual(version, __version__)
        # PEP 440 requires the alpha marker be numeric (`a0`), the advertised label may not be.
        self.assertTrue(version.endswith("a0"), f"internal version must be PEP 440, got {version}")

    def test_display_label_matches_mcp_handshake(self) -> None:
        self.assertEqual(__version_display__, "v0.3.0-alpha")
        self.assertEqual(__version_label__, "0.3.0-alpha")
        self.assertEqual(SERVER_VERSION, __version_label__)


class TestZeroKeyDegradation(unittest.IsolatedAsyncioTestCase):
    """A-6: retrieval must never depend on having a working cloud account."""

    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "OmniLore_Data"
        self._saved_key = os.environ.pop("DASHSCOPE_API_KEY", None)
        self.engine = OmniLoreEngine(self.root)
        self.space_id = "sp_w0_degrade"
        self.engine.auth.register_space(
            SpaceRecord(space_id=self.space_id, space_type=SpaceType.PERSONAL, parent_space_id=None, vault_rel_path=f"spaces/{self.space_id}")
        )
        self.engine.auth.grant_membership(
            MembershipRecord(membership_id="m1", user_id="u1", space_id=self.space_id, role=RoleType.ADMIN, allowed_categories={"01_Work"})
        )
        self.token = "tok_w0_degrade"
        self.engine.auth.issue_token(
            self.token,
            user_id="u1",
            profile_type=ProfileType.OMNI,
            allowed_read_spaces={self.space_id},
            allowed_write_spaces={self.space_id},
            allowed_categories={"01_Work"},
        )
        await OmniLoreMCPGateway(self.engine).ingest_document_or_file(
            self.token,
            content_bytes="# 分布式锁与租约续期规范\n\n分布式锁需要租约续期。\n".encode("utf-8"),
            filename="lease.md",
            target_space_id=self.space_id,
            target_category="01_Work",
        )

    async def asyncTearDown(self) -> None:
        if self._saved_key is not None:
            os.environ["DASHSCOPE_API_KEY"] = self._saved_key
        else:
            os.environ.pop("DASHSCOPE_API_KEY", None)
        self._tmp.cleanup()

    async def test_zero_key_reports_keyword_only(self) -> None:
        self.assertIsNone(os.environ.get("DASHSCOPE_API_KEY"))
        res = await self.engine.search_knowledge(self.token, "分布式锁与租约续期规范")
        self.assertEqual(res["retrieval_mode"], "keyword_only")
        self.assertNotEqual(res["degraded_reason"], "")
        self.assertTrue(res["results"], "keyword-only retrieval must still find the document")
        self.assertEqual(res["results"][0]["space_id"], self.space_id)
        # 组合 1（见 README）：未配置 Key 时合成走本地确定性逻辑，属设计路径而非故障，
        # 因此 synthesis_degraded 必须为 False —— 否则调用方会误判为故障。
        self.assertFalse(res["synthesis_degraded"])
        self.assertEqual(res["synthesis_note"], "")

    async def test_invalid_or_unreachable_provider_degrades_gracefully(self) -> None:
        """QA P1-2: a configured-but-broken key must degrade, not raise.

        Simulates an unreachable/401 provider purely locally: no real network call.
        """
        os.environ["DASHSCOPE_API_KEY"] = "invalid-key-simulated"
        self.assertEqual(self.engine.model_adapter.retrieval_mode, "hybrid")

        original_post_json = DashScopeHTTPTransport.post_json

        def failing_post_json(self, url: str, body: dict, api_key: str) -> dict:
            raise RuntimeError("DashScope API call failed after retries: HTTPError")

        DashScopeHTTPTransport.post_json = failing_post_json  # type: ignore[method-assign]
        try:
            res = await self.engine.search_knowledge(self.token, "分布式锁与租约续期规范")
        finally:
            DashScopeHTTPTransport.post_json = original_post_json  # type: ignore[method-assign]

        self.assertEqual(res["retrieval_mode"], "keyword_only")
        self.assertIn("降级", res["degraded_reason"])
        self.assertTrue(res["results"], "graceful degradation must still return keyword hits")
        # 组合 3：检索与合成都降级，正交字段必须各自如实上报。
        self.assertTrue(res["synthesis_degraded"])
        self.assertNotEqual(res["synthesis_note"], "")

    async def test_synthesis_degrade_is_reported_orthogonally(self) -> None:
        """组合 4（P2-D）：向量召回正常、只有合成挂 → hybrid + degraded_reason 为空。

        这条正是促使字段拆分的原因：若把合成失败并进 degraded_reason，就会出现
        `retrieval_mode=hybrid` 却 `degraded_reason` 非空的自相矛盾组合，调用方无法
        再用「degraded_reason 是否为空」判断检索能力。
        """
        os.environ["DASHSCOPE_API_KEY"] = "valid-key-simulated"
        original_post_json = DashScopeHTTPTransport.post_json

        def embeddings_ok_but_chat_fails(self, url: str, body: dict, api_key: str) -> dict:
            # /embeddings 正常返回；/chat/completions 模拟供应商不可达
            if url.endswith("/embeddings"):
                return {"data": [{"embedding": [0.1] * 1024} for _ in body.get("input", [])]}
            raise RuntimeError("DashScope API call failed after retries: HTTPError")

        DashScopeHTTPTransport.post_json = embeddings_ok_but_chat_fails  # type: ignore[method-assign]
        try:
            res = await self.engine.search_knowledge(self.token, "分布式锁与租约续期规范")
        finally:
            DashScopeHTTPTransport.post_json = original_post_json  # type: ignore[method-assign]

        self.assertEqual(res["retrieval_mode"], "hybrid", "向量召回成功，不应被标记为降级")
        self.assertEqual(res["degraded_reason"], "", "检索维度未降级，degraded_reason 必须为空")
        self.assertTrue(res["synthesis_degraded"], "合成失败必须单独上报")
        self.assertNotEqual(res["synthesis_note"], "")

    async def test_response_shape_error_is_not_blamed_on_provider(self) -> None:
        """归因守卫（QA P2-B）：响应结构错误**不得**被说成「供应商不可用」。

        QA 的变异测试发现：把 `_RESPONSE_SHAPE_ERRORS` 置空（归因不再区分）后
        28 个测试全绿 —— 也就是说 P2-B 的行为修复当时没有任何自动化守卫。
        这里构造一个「HTTP 正常返回、但结构不对」的响应，内部必然抛 KeyError：
          * 必须被归因为「解析失败」
          * 且**不得**出现「向量召回不可用」这种把内部 bug 伪装成供应商故障的措辞
        """
        os.environ["DASHSCOPE_API_KEY"] = "valid-key-simulated"
        original_post_json = DashScopeHTTPTransport.post_json

        def structurally_wrong_response(self, url: str, body: dict, api_key: str) -> dict:
            return {"unexpected": {"nope": 1}}  # 触发 resp["data"] / resp["choices"] 的 KeyError

        DashScopeHTTPTransport.post_json = structurally_wrong_response  # type: ignore[method-assign]
        try:
            res = await self.engine.search_knowledge(self.token, "分布式锁与租约续期规范")
        finally:
            DashScopeHTTPTransport.post_json = original_post_json  # type: ignore[method-assign]

        self.assertIn("解析失败", res["degraded_reason"], "结构错误必须归因为解析失败")
        self.assertNotIn(
            "向量召回不可用", res["degraded_reason"],
            "不得把内部解析 bug 伪装成供应商不可用",
        )

    async def test_model_adapter_reports_mode_and_reason(self) -> None:
        no_key = ModelAdapter(ModelAdapterConfig(), telemetry_sink=[])
        self.assertEqual(no_key.retrieval_mode, "keyword_only")
        self.assertNotEqual(no_key.degraded_reason, "")

        os.environ["DASHSCOPE_API_KEY"] = "dummy"
        try:
            with_key = ModelAdapter(ModelAdapterConfig(), telemetry_sink=[])
            self.assertEqual(with_key.retrieval_mode, "hybrid")
            self.assertEqual(with_key.degraded_reason, "")
        finally:
            os.environ.pop("DASHSCOPE_API_KEY", None)


class TestDataDirResolution(unittest.TestCase):
    """QA P1-1: CLI and MCP must resolve OMNILORE_DATA_DIR identically."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name).resolve()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_tilde_is_really_expanded(self) -> None:
        """Mutation-detecting test for QA P1-1's actual trigger.

        An earlier version of this test passed an already-absolute path, so deleting
        `expanduser()` from `resolve_data_dir()` still kept it green — i.e. the very
        behaviour that caused P1-1 was unguarded. We now pass a literal `"~/..."`
        and patch the home env vars so `Path.expanduser()` is genuinely exercised:
        removing `expanduser()` makes this test fail.
        """
        home = self.base / "home" / "user"
        home.mkdir(parents=True, exist_ok=True)
        with mock.patch.dict(os.environ, {"HOME": str(home), "USERPROFILE": str(home)}, clear=False):
            resolved = resolve_data_dir("~/qa_ol_tilde")

        self.assertTrue(resolved.is_absolute())
        self.assertNotIn("~", str(resolved), "字面量 ~ 必须被展开，不能被当普通目录名")
        self.assertEqual(resolved, home / "qa_ol_tilde")

    def test_relative_paths_resolve_against_cwd(self) -> None:
        relative = resolve_data_dir("./Data", env={}, base=self.base / "work")
        self.assertEqual(relative, (self.base / "work" / "Data").resolve())
        # 不同 cwd 会得到不同结果 —— 这正是必须显式解析的原因
        other = resolve_data_dir("./Data", env={}, base=self.base / "other")
        self.assertNotEqual(relative, other)

    def test_cli_and_mcp_share_one_resolver(self) -> None:
        """The two entry points must delegate to the same function, or they will drift."""
        from omnilore import cli as cli_module
        from omnilore import mcp_stdio_server as mcp_module

        self.assertIs(cli_module.resolve_data_dir, resolve_data_dir)
        self.assertIs(mcp_module.resolve_data_dir, resolve_data_dir)

        # Behavioural check: same input -> same absolute output, independent of cwd.
        with_unknown_env = resolve_data_dir(str(self.base / "shared"), env={}, base=self.base)
        self.assertEqual(with_unknown_env, self.base / "shared")

    def test_no_duplicate_data_dir_resolution_in_source(self) -> None:
        """源码级漂移守卫（QA P2-C）。

        只扫 `.expanduser` 属性是不够的：QA 把 `mcp_stdio_server.py` 改回
        `Path(os.environ.get("OMNILORE_DATA_DIR", ...))` —— **不带 expanduser，
        正是 P1-1 当年的原始写法** —— 旧断言完全没抓到。
        这类守卫要防的是「有人图省事再内联一次」，所以这里同时检查两件事：
          1. 包内除 `pathutil.py` 外不得出现 `expanduser` 属性调用；
          2. 包内除 `pathutil.py` 外不得出现 `OMNILORE_DATA_DIR` 字面量的**读取**。
        """
        import ast

        pkg_dir = Path(__file__).resolve().parent.parent / "omnilore"
        offenders = []
        for src in sorted(pkg_dir.glob("*.py")):
            if src.name == "pathutil.py":
                continue
            tree = ast.parse(src.read_text(encoding="utf-8"), filename=str(src))
            for node in ast.walk(tree):
                # AST 而非文本扫描：注释/docstring 里提到 expanduser 是允许的。
                if isinstance(node, ast.Attribute) and node.attr == "expanduser":
                    offenders.append(f"{src.name}:{node.lineno} expanduser")
                # 直接读环境变量（绕过共享 resolver）同样是 P1-1 的成因。
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get"
                    and isinstance(node.func.value, ast.Attribute)
                    and node.func.value.attr == "environ"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value == "OMNILORE_DATA_DIR"
                ):
                    offenders.append(f"{src.name}:{node.lineno} os.environ.get('OMNILORE_DATA_DIR')")
        self.assertEqual(offenders, [], f"数据目录解析逻辑出现第二份实现：{offenders}")

    def test_every_entry_point_calls_shared_resolver(self) -> None:
        """每个入口都必须真正**调用** `resolve_data_dir`，而不只是 import 它。

        补上 `assertIs(...)` 的第二重盲区：模块可以 import 了共享函数、却在实际路径上
        内联另一套解析。这里用 AST 确认 `cli.py` / `mcp_stdio_server.py` 里存在对该
        函数的调用点。
        """
        import ast

        pkg_dir = Path(__file__).resolve().parent.parent / "omnilore"
        for entry in ("cli.py", "mcp_stdio_server.py"):
            tree = ast.parse((pkg_dir / entry).read_text(encoding="utf-8"), filename=entry)
            called = any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "resolve_data_dir"
                for node in ast.walk(tree)
            )
            self.assertTrue(called, f"{entry} 未调用共享的 resolve_data_dir()，存在内联解析风险")


class TestReadmeClaimAccuracy(unittest.TestCase):
    """README 的量化宣称必须与仓库实际一致。

    起因（team-lead P2-10）：README 里的 `engine.py` 行数、表数、用例数都是手写的，
    本轮内它们就**再一次**过期了（我改完 engine.py 和测试后数字立刻失真）。
    「宣称与证据必须匹配」是本项目的红线，靠人肉同步必然失败 —— 所以把它变成测试：
    以后谁改了代码让数字失真，测试会红，而不是让文档继续骗人。
    """

    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parent.parent
        self.readme = self.repo_root / "README.md"
        self.text = self.readme.read_text(encoding="utf-8")

    def _assert_claims(self, label: str, claims: list, actual: int) -> None:
        self.assertTrue(claims, f"README 未找到「{label}」的宣称；若刻意移除请同步删除本断言")
        for claimed in claims:
            self.assertEqual(int(claimed), actual, f"README 宣称 {label}={claimed}，实际为 {actual}")

    def test_engine_is_the_largest_module_claim(self) -> None:
        """断言**相对关系**：engine.py 仍是全库最大的 Python 源文件。

        为什么改成相对关系：早先这里钉死了绝对行数（带 ±15% 容差），结果 QA 的 13 个
        变异里有 6 个是被它**误伤**的 —— 任何一次 engine.py 改动都会让它红，反而制造
        噪音。真正有价值的信息是「engine.py 是全库最大的文件，因此贡献者心理门槛最高」，
        这是个稳定的相对关系，不会因改几行代码而失效。

        README 里的「约 3200 行」量级表述保留（见下方量级断言）。
        """
        pkg = self.repo_root / "omnilore"
        sizes = {
            src.name: len(src.read_text(encoding="utf-8").splitlines())
            for src in pkg.glob("*.py")
        }
        self.assertIn("engine.py", sizes)
        biggest = max(sizes, key=lambda k: sizes[k])
        self.assertEqual(
            biggest, "engine.py",
            f"engine.py 不再是全库最大源文件（当前最大：{biggest}={sizes[biggest]} 行）；"
            "README/CONTRIBUTING 中「绕开核心文件」的贡献路径叙事需要同步更新。",
        )
        # 附加一条宽松的量级断言：至少仍是数量级上的「大文件」，防止叙事彻底失真。
        self.assertGreater(sizes["engine.py"], 1000, "engine.py 已小到不再构成贡献门槛，需更新文档叙事")

    def test_engine_line_count_magnitude_claim(self) -> None:
        """README 的「约 N 行」量级表述需在 ±25% 内（宽松，避免误伤）。"""
        claimed = re.findall(r"engine\.py\s+# 核心引擎（约 (\d+) 行", self.text)
        claimed += re.findall(r"读懂约 (\d+) 行的 `engine\.py`", self.text)
        if not claimed:
            self.skipTest("README 未声明 engine.py 行数量级")
        actual = len((self.repo_root / "omnilore" / "engine.py").read_text(encoding="utf-8").splitlines())
        for value in claimed:
            nominal = int(value)
            self.assertGreaterEqual(actual, int(nominal * 0.75),
                                    f"README 宣称约 {nominal} 行，实际 {actual} 行（低于量级下限）")
            self.assertLessEqual(actual, int(nominal * 1.25),
                                 f"README 宣称约 {nominal} 行，实际 {actual} 行（超出量级上限）")

    def test_sql_table_count_claim(self) -> None:
        claimed = re.findall(r"(\d+) 张表", self.text)
        sql = (self.repo_root / "sql" / "001_omnilore_schema_and_rls.sql").read_text(encoding="utf-8")
        self._assert_claims("sql/001 表数", claimed, len(re.findall(r"CREATE TABLE", sql)))

    def test_mcp_tool_count_claim(self) -> None:
        from omnilore import MCP_TOOL_DEFINITIONS

        self._assert_claims("MCP 工具数", re.findall(r"(\d+) 个 MCP 工具", self.text), len(MCP_TOOL_DEFINITIONS))

    def test_total_case_count_claim(self) -> None:
        claimed = re.findall(r"共 (\d+) 个用例", self.text) + re.findall(r"\*\*(\d+) 个测试用例\*\*", self.text)
        total = 0
        for src in sorted((self.repo_root / "tests").glob("test_*.py")):
            total += len(re.findall(r"^    (?:async )?def test_", src.read_text(encoding="utf-8"), re.M))
        self._assert_claims("测试用例总数", claimed, total)

    def test_per_file_case_count_claims(self) -> None:
        for filename in ("test_omnilore_phase0_acceptance.py", "test_full_omnilore_suite.py", "test_w0_launch.py"):
            claimed = re.findall(rf"{re.escape(filename)}\s+#.*?(\d+) 项", self.text)
            if not claimed:
                continue  # 该文件未在目录树中标注项数，跳过
            actual = len(
                re.findall(
                    r"^    (?:async )?def test_",
                    (self.repo_root / "tests" / filename).read_text(encoding="utf-8"),
                    re.M,
                )
            )
            self._assert_claims(f"{filename} 项数", claimed, actual)

    def test_n8n_workflow_count_claim(self) -> None:
        claimed = re.findall(r"(\d+) 条可导入工作流", self.text)
        if not claimed:
            self.skipTest("README 未标注 n8n 工作流数量")
        self._assert_claims("n8n 工作流数", claimed, len(list((self.repo_root / "n8n" / "workflows").glob("*.json"))))


class TestCliEntryPoint(unittest.TestCase):  # sync: main() owns its own event loop
    """Zero-key `bootstrap` + `demo` through the real argparse entry point."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve() / "OmniLore_Data"
        self._saved_key = os.environ.pop("DASHSCOPE_API_KEY", None)

    def tearDown(self) -> None:
        if self._saved_key is not None:
            os.environ["DASHSCOPE_API_KEY"] = self._saved_key
        else:
            os.environ.pop("DASHSCOPE_API_KEY", None)
        self._tmp.cleanup()

    def test_version_and_demo_subcommands_run_offline(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc_version = main(["version"])
        self.assertEqual(rc_version, 0)
        self.assertIn(__version_display__, buf.getvalue())

        buf.seek(0)
        buf.truncate(0)
        with redirect_stdout(buf):
            rc_bootstrap = main(["bootstrap", "--data-dir", str(self.root), "--quiet"])
        self.assertEqual(rc_bootstrap, 0)
        self.assertIn("Owner Token", buf.getvalue())

        buf.seek(0)
        buf.truncate(0)
        with redirect_stdout(buf):
            rc_demo = main(["demo", "--data-dir", str(self.root), "--json"])
        self.assertEqual(rc_demo, 0)
        raw_demo = buf.getvalue()
        # The JSON payload is printed last; slice from its opening brace, not "last line".
        payload = json.loads(raw_demo[raw_demo.index("{"):])
        self.assertTrue(payload["found_demo_document"])
        self.assertEqual(payload["retrieval_mode"], "keyword_only")
        self.assertNotEqual(payload["degraded_reason"], "")

    def test_default_data_dir_uses_shared_resolver(self) -> None:
        os.environ["OMNILORE_DATA_DIR"] = str(self.root)
        try:
            from omnilore.cli import _default_data_dir

            self.assertEqual(_default_data_dir(), self.root)
        finally:
            os.environ.pop("OMNILORE_DATA_DIR", None)


class TestMcpStdioEntrypoint(unittest.TestCase):
    """P0: `omnilore-mcp` must actually serve JSON-RPC, not emit an un-awaited coroutine.

    起因：`[project.scripts]` 的 console script 是**被调用**、永远不会被 await 的，
    而 `main_stdio()` 是 `async def`。入口点直接指向它时实际只产出一条
    ``RuntimeWarning: coroutine 'main_stdio' was never awaited`` 就退出 ——
    而 README 让用户照抄的 MCP 配置片段写的正是 `"command": "omnilore-mcp"`，
    等于把首发主链路的 MCP 挂载路径挂成了死链。

    这里的三条断言分别守住：入口点目标本身、真实管道上的三轮握手、以及改名后的
    `OMNILORE_AUTO_BOOTSTRAP=0` 开关。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name) / "OmniLore_Data"
        self.repo_root = Path(__file__).resolve().parent.parent
        self._saved_key = os.environ.pop("DASHSCOPE_API_KEY", None)

    def tearDown(self) -> None:
        if self._saved_key is not None:
            os.environ["DASHSCOPE_API_KEY"] = self._saved_key
        else:
            os.environ.pop("DASHSCOPE_API_KEY", None)
        self._tmp.cleanup()

    def _run_stdio(self, payload: list, env_extra: dict):
        """Drive the sync entry point over real pipes and return (frames, stderr).

        Deliberately spawns `python -c "... main_stdio_sync()"` rather than the
        installed `omnilore-mcp` launcher: the launcher's path is platform-specific
        (`Scripts/` vs `bin/`), while this exercises exactly the same callable the
        console script targets.

        Args:
            payload: JSON-RPC request objects written to stdin, one per line.
            env_extra: Environment overrides for the child process.

        Returns:
            Tuple of (parsed stdout frames, raw stderr text).
        """
        env = dict(os.environ)
        env["DASHSCOPE_API_KEY"] = ""
        env.update(env_extra)
        stdin = "".join(json.dumps(item) + "\n" for item in payload)
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                "from omnilore.mcp_stdio_server import main_stdio_sync; main_stdio_sync()",
            ],
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=str(self.repo_root),
            env=env,
            timeout=120,
        )
        frames = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
        return frames, proc.stderr

    def test_console_script_target_is_not_a_coroutine(self) -> None:
        """静态守卫：pyproject 声明的入口点目标不得是协程函数。"""
        scripts = tomllib.loads((self.repo_root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["scripts"]
        module_path, attr_name = scripts["omnilore-mcp"].split(":")
        target = getattr(importlib.import_module(module_path), attr_name)

        self.assertFalse(
            inspect.iscoroutinefunction(target),
            "`omnilore-mcp` 入口点指向了协程函数；console script 不会被 await，"
            "实际只会留下一条 'never awaited' 告警就退出。请指向同步壳 main_stdio_sync。",
        )
        self.assertTrue(callable(target), "入口点目标必须可调用")

        # 同步壳必须真的驱动 asyncio 循环且不留未 await 的协程：
        # 一旦有人把 main_stdio_sync 也改成 async def，这条会红。
        from omnilore import mcp_stdio_server as mcp_module

        self.assertFalse(inspect.iscoroutinefunction(mcp_module.main_stdio_sync))
        self.assertTrue(inspect.iscoroutinefunction(mcp_module.main_stdio), "底层循环仍需保持 async")

    def test_stdio_handshake_serves_jsonrpc_over_real_pipes(self) -> None:
        """端到端守卫：initialize → tools/list → tools/call 三轮都返回合法 JSON-RPC。"""
        token = "tok_w0_mcp_handshake"
        frames, stderr = self._run_stdio(
            [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "search_knowledge",
                        "arguments": {"token": token, "query": "分布式锁与租约续期规范"},
                    },
                },
            ],
            {"OMNILORE_DATA_DIR": str(self.data_dir), "OMNILORE_BOOTSTRAP_TOKEN": token},
        )

        self.assertEqual([f.get("id") for f in frames], [1, 2, 3], "每一帧的 id 必须与请求逐一对应")
        for frame in frames:
            self.assertEqual(frame.get("jsonrpc"), "2.0", "stdout 只允许出现 JSON-RPC 帧")
            self.assertTrue("result" in frame or "error" in frame)

        self.assertEqual(frames[0]["result"]["serverInfo"]["name"], "omnilore-mcp-gateway")
        self.assertEqual(len(frames[1]["result"]["tools"]), 7, "tools/list 必须暴露全部 7 个 MCP 工具")

        payload = json.loads(frames[2]["result"]["content"][0]["text"])
        self.assertEqual(payload["retrieval_mode"], "keyword_only")
        self.assertNotEqual(payload["degraded_reason"], "")

        self.assertNotIn("never awaited", stderr, "同步入口不得再留下未 await 的协程告警")
        self.assertNotIn("RuntimeWarning", stderr)

    def test_auto_bootstrap_switch_is_honoured_after_rename(self) -> None:
        """改名守卫：`OMNILORE_AUTO_BOOTSTRAP=0` 必须仍能关闭首启自动引导。"""
        frames, stderr = self._run_stdio(
            [{"jsonrpc": "2.0", "id": 1, "method": "initialize"}],
            {"OMNILORE_DATA_DIR": str(self.data_dir), "OMNILORE_AUTO_BOOTSTRAP": "0"},
        )
        self.assertIn("OMNILORE_AUTO_BOOTSTRAP=0", stderr, "关闭开关时必须给出可诊断的 stderr 说明")
        self.assertIn("跳过自动引导", stderr)
        self.assertEqual(len(frames), 1, "跳过引导不得影响 JSON-RPC 服务能力")
        self.assertEqual(frames[0]["result"]["serverInfo"]["name"], "omnilore-mcp-gateway")

    def test_auto_bootstrap_zero_still_accepts_persisted_token(self) -> None:
        """QA P2-1：`AUTO_BOOTSTRAP=0` 只禁止**新建**，不得废掉已有 token。

        修复前 `_bootstrap_or_explain()` 在 `OMNILORE_AUTO_BOOTSTRAP=0` 时直接早退，
        从不调用 `ensure_bootstrapped()` —— 于是 `omnilore bootstrap` 已写入
        `ops/bootstrap.json` 的 space/user/token_hash 不会灌进新建的内存引擎，
        `tools/call` 拿到的是 `PERMISSION_DENIED: Invalid authentication token`。
        这直接违背 CHANGELOG「使 CLI 签发的 token 可被独立进程的 MCP 服务复用」
        的承诺，而 README 对 `0` 的措辞是「关闭首启自动引导」，读不出「已有 token 作废」。
        """
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc_bootstrap = main(["bootstrap", "--data-dir", str(self.data_dir), "--json"])
        self.assertEqual(rc_bootstrap, 0)
        persisted_token = json.loads(buf.getvalue()[buf.getvalue().index("{"):])["token"]

        frames, stderr = self._run_stdio(
            [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "search_knowledge",
                        "arguments": {"token": persisted_token, "query": "分布式锁与租约续期规范"},
                    },
                },
            ],
            {"OMNILORE_DATA_DIR": str(self.data_dir), "OMNILORE_AUTO_BOOTSTRAP": "0"},
        )

        # 已有 bootstrap.json → 不应走「跳过自动引导」分支
        self.assertNotIn("跳过自动引导", stderr, "已有持久化身份时 AUTO_BOOTSTRAP=0 也必须照常加载")

        payload = json.loads(frames[1]["result"]["content"][0]["text"])
        self.assertNotIn("error_code", payload, f"既有 token 必须仍能鉴权，实际：{payload}")
        self.assertEqual(payload["retrieval_mode"], "keyword_only")
        self.assertNotEqual(payload["degraded_reason"], "")


if __name__ == "__main__":
    unittest.main()
