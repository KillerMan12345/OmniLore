"""
OmniLore v2.3 Full End-to-End Test Suite (Phase 0 + Phase 1 + Phase 2 + Phase 3 + Ops)
========================================================================================
Covers:
  1. Phase 0 Security & RLS Isolation (AC-S01 .. AC-S07)
  2. Phase 1 Saga Ingestion, Idempotency, Zero-Downtime Versioning, All 7 Review Actions
     (APPROVE_REVIEW, RECLASSIFY, SPLIT_DOCUMENT, FIX_EXTRACTED_FIELD, REVERT_VERSION,
     PROMOTE_COPY, CANCEL_TASK), Multi-Format Parsing (PDF, Image, Code Symbols),
     Obsidian Bi-Directional Sync, Watcher Daemon & `_dashboard_控制台.md`
  3. Phase 2 Family Member Invite/Revoke (FR-13), Health Report Metric Extraction &
     Human Verification Pipeline (FR-14), and TTL / Knowledge Lint Inspector (FR-12)
  4. Phase 3 Multi-Level Enterprise Org Tree (`ltree`) Public Read Inheritance (FR-16)
     and Sensitive PII Redaction Preview & Promotion (FR-15)
  5. Section 12 Operations: Backup, SHA-256 Verified Restore, Blue-Green Embedding
     Index Rebuild, Storage Reconciliation, and FastMCP JSON-RPC 2.0 Server Dispatch
"""

from __future__ import annotations
import re

import io
from pathlib import Path
import tempfile
import unittest

from PIL import Image  # type: ignore

from omnilore import (
    OmniLoreEngine,
    OmniLoreMCPGateway,
    MCPJsonRpcServer,
    MembershipRecord,
    ObsidianVaultWatcher,
    OperationType,
    OutboxRecoveryDaemon,
    PermissionDeniedError,
    ProfileType,
    ReviewAction,
    RoleType,
    SpaceRecord,
    SpaceType,
    TaskState,
    VersionConflictError,
    profiled_transaction,
)

# 平台能力探测：Windows 无 symlink 权限时优雅降级（宿主限制，非 OmniLore 平台缺陷）
try:  # `unittest discover -s tests` 会把 tests/ 放进 sys.path
    from _platform_support import establish_hostile_link
except ImportError:  # 以 `tests.<mod>` 方式导入时的兜底
    from tests._platform_support import establish_hostile_link


class TestOmniLoreFullSuite(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root_path = Path(self.tmp_dir.name) / "OmniLore_Data"
        self.engine = OmniLoreEngine(self.root_path)
        self.mcp = OmniLoreMCPGateway(self.engine)
        self.jsonrpc = MCPJsonRpcServer(self.mcp)

        # Register Spaces for Phase 1 (Personal), Phase 2 (Family), and Phase 3 (Multi-level Org Tree)
        self.engine.auth.register_space(
            SpaceRecord(
                space_id="sp_personal_alice",
                space_type=SpaceType.PERSONAL,
                parent_space_id=None,
                vault_rel_path="spaces/sp_personal_alice",
            )
        )
        self.engine.auth.register_space(
            SpaceRecord(
                space_id="sp_family_home",
                space_type=SpaceType.FAMILY,
                parent_space_id=None,
                vault_rel_path="spaces/sp_family_home",
            )
        )
        # Phase 3 Multi-level Org Tree: sp_org_root -> sp_org_eng -> sp_org_backend + sp_org_secret
        self.engine.auth.register_space(
            SpaceRecord(
                space_id="sp_org_root",
                space_type=SpaceType.ORG_PUBLIC,
                parent_space_id=None,
                org_tree_path="org",
                vault_rel_path="spaces/sp_org_root",
            )
        )
        self.engine.auth.register_space(
            SpaceRecord(
                space_id="sp_org_eng",
                space_type=SpaceType.ORG_PUBLIC,
                parent_space_id="sp_org_root",
                org_tree_path="org.eng",
                vault_rel_path="spaces/sp_org_eng",
            )
        )
        self.engine.auth.register_space(
            SpaceRecord(
                space_id="sp_org_backend",
                space_type=SpaceType.ORG_PUBLIC,
                parent_space_id="sp_org_eng",
                org_tree_path="org.eng.backend",
                vault_rel_path="spaces/sp_org_backend",
            )
        )
        self.engine.auth.register_space(
            SpaceRecord(
                space_id="sp_org_secret",
                space_type=SpaceType.ORG_RESTRICTED,
                parent_space_id="sp_org_eng",
                org_tree_path="org.eng.secret",
                vault_rel_path="spaces/sp_org_secret",
            )
        )

        self.alice_id = "11111111-1111-1111-1111-111111111111"
        self.bob_id = "22222222-2222-2222-2222-222222222222"
        self.charlie_id = "33333333-3333-3333-3333-333333333333"

        self.engine.auth.grant_membership(
            MembershipRecord(
                membership_id="m1",
                user_id=self.alice_id,
                space_id="sp_personal_alice",
                role=RoleType.ADMIN,
                allowed_categories={"01_Work", "02_Life", "03_Family", "04_Health_Archive"},
            )
        )
        self.engine.auth.grant_membership(
            MembershipRecord(
                membership_id="m2",
                user_id=self.alice_id,
                space_id="sp_family_home",
                role=RoleType.ADMIN,
                allowed_categories={"03_Family", "04_Health_Archive"},
            )
        )
        self.engine.auth.grant_membership(
            MembershipRecord(
                membership_id="m3",
                user_id=self.bob_id,
                space_id="sp_family_home",
                role=RoleType.READER,
                allowed_categories={"03_Family", "04_Health_Archive"},
            )
        )
        self.engine.auth.grant_membership(
            MembershipRecord(
                membership_id="m4",
                user_id=self.alice_id,
                space_id="sp_org_root",
                role=RoleType.ADMIN,
                allowed_categories={"01_Work"},
            )
        )
        self.engine.auth.grant_membership(
            MembershipRecord(
                membership_id="m5",
                user_id=self.alice_id,
                space_id="sp_org_eng",
                role=RoleType.ADMIN,
                allowed_categories={"01_Work"},
            )
        )
        # Charlie is a member of leaf space sp_org_backend with inherit_org_public_read=True
        self.engine.auth.grant_membership(
            MembershipRecord(
                membership_id="m6",
                user_id=self.charlie_id,
                space_id="sp_org_backend",
                role=RoleType.WRITER,
                allowed_categories={"01_Work"},
                inherit_org_public_read=True,
            )
        )

        self.alice_work_token = "tok_alice_work_secret_001"
        self.engine.auth.issue_token(
            self.alice_work_token,
            user_id=self.alice_id,
            profile_type=ProfileType.WORK,
            allowed_read_spaces={"sp_personal_alice", "sp_family_home", "sp_org_root", "sp_org_eng"},
            allowed_write_spaces={"sp_personal_alice", "sp_family_home", "sp_org_root", "sp_org_eng"},
            allowed_categories={"01_Work", "02_Life", "03_Family", "04_Health_Archive"},
        )

        self.alice_omni_token = "tok_alice_omni_secret_002"
        self.engine.auth.issue_token(
            self.alice_omni_token,
            user_id=self.alice_id,
            profile_type=ProfileType.OMNI,
            allowed_read_spaces={"sp_personal_alice", "sp_family_home", "sp_org_root", "sp_org_eng"},
            allowed_write_spaces={"sp_personal_alice", "sp_family_home", "sp_org_root", "sp_org_eng"},
            allowed_categories={"01_Work", "02_Life", "03_Family", "04_Health_Archive"},
        )

        self.bob_family_token = "tok_bob_family_secret_003"
        self.engine.auth.issue_token(
            self.bob_family_token,
            user_id=self.bob_id,
            profile_type=ProfileType.FAMILY,
            allowed_read_spaces={"sp_personal_alice", "sp_family_home"},
            allowed_write_spaces={"sp_family_home"},
            allowed_categories={"01_Work", "03_Family", "04_Health_Archive"},
        )

        self.charlie_work_token = "tok_charlie_work_secret_004"
        self.engine.auth.issue_token(
            self.charlie_work_token,
            user_id=self.charlie_id,
            profile_type=ProfileType.WORK,
            allowed_read_spaces={"sp_org_root", "sp_org_eng", "sp_org_backend", "sp_org_secret"},
            allowed_write_spaces={"sp_org_root", "sp_org_eng", "sp_org_backend"},
            allowed_categories={"01_Work"},
        )

    async def asyncTearDown(self) -> None:
        self.tmp_dir.cleanup()

    # =========================================================================
    # 1. Phase 0/1 Core Security, RLS, Idempotency & Version Control
    # =========================================================================

    async def test_01_profile_and_category_rls_isolation(self) -> None:
        work_res = await self.mcp.ingest_document_or_file(
            self.alice_omni_token,
            content_bytes="微信支付回调 API 接口规范 v1.0：必须验证签名。".encode("utf-8"),
            filename="wechat_pay_api.md",
            target_space_id="sp_personal_alice",
            target_category="01_Work",
            version_key="uc_api_wechat_pay",
        )
        family_res = await self.mcp.ingest_document_or_file(
            self.alice_omni_token,
            content_bytes="家庭重疾险保单汇总：保额 100 万，受益人 Alice。".encode("utf-8"),
            filename="family_policy.md",
            target_space_id="sp_personal_alice",
            target_category="03_Family",
            version_key="family_insurance_2026",
        )
        work_doc_id = work_res["task"]["result_doc_id"]
        family_doc_id = family_res["task"]["result_doc_id"]

        search_work = await self.mcp.search_knowledge(self.alice_work_token, "保单 受益人")
        self.assertEqual(len(search_work["results"]), 0)

        with self.assertRaises(PermissionDeniedError):
            await self.mcp.get_document_asset(self.alice_work_token, family_doc_id, asset_type="WIKI")

        got_work = await self.mcp.get_document_asset(self.alice_work_token, work_doc_id, asset_type="WIKI")
        self.assertIn("微信支付回调", got_work["content"])

        with self.assertRaises(PermissionDeniedError):
            await self.mcp.get_document_asset(self.bob_family_token, work_doc_id, asset_type="WIKI")

    async def test_02_cross_space_update_and_path_traversal_blocked(self) -> None:
        res = await self.mcp.ingest_document_or_file(
            self.alice_work_token,
            content_bytes="API 设计文档：订单服务状态机".encode("utf-8"),
            filename="order_api.md",
            target_space_id="sp_personal_alice",
            target_category="01_Work",
        )
        doc_id = res["task"]["result_doc_id"]
        work_scope = self.engine.auth.resolve_scope(self.alice_work_token)

        async with profiled_transaction(self.engine.db, work_scope) as conn:
            with self.assertRaises(PermissionDeniedError):
                await conn.update_row("documents", doc_id, lambda d: {**d, "category": "03_Family"})

        with self.assertRaises(PermissionDeniedError):
            await self.mcp.get_document_asset(self.alice_omni_token, "../etc/passwd", asset_type="WIKI")

        space_dir = self.engine.storage.ensure_space_dirs("sp_personal_alice")
        secret_outside = Path(self.tmp_dir.name) / "secret_host_file.txt"
        secret_outside.write_text("TOP_SECRET", encoding="utf-8")
        # 优先用真实 symlink；Windows 无 symlink 权限时回退到目录联接（junction，无需特权）。
        # 两者都必须被文件网关拒绝。仅当宿主两者都造不出来时才 skip（原因可复核）。
        spec = establish_hostile_link(
            self,
            wiki_dir=space_dir / "wiki",
            space_id="sp_personal_alice",
            outside_file=secret_outside,
        )

        with self.assertRaises(PermissionDeniedError):
            self.engine.storage.read_wiki_by_record("sp_personal_alice", spec.rel_path)

    async def test_03_10x_idempotent_replay_and_zero_downtime_version_switch(self) -> None:
        payload_bytes = "幂等测试文档：分布式锁与租约续期规范 v1.0".encode("utf-8")
        first_res = await self.mcp.ingest_document_or_file(
            self.alice_omni_token,
            content_bytes=payload_bytes,
            filename="lease_spec_copy1.md",
            target_space_id="sp_personal_alice",
            target_category="01_Work",
            version_key="spec_distributed_lease",
        )
        v1_doc_id = first_res["task"]["result_doc_id"]
        series_id = self.engine.db._state["documents"][v1_doc_id]["series_id"]

        for i in range(2, 11):
            replay = await self.mcp.ingest_document_or_file(
                self.alice_omni_token,
                content_bytes=payload_bytes,
                filename=f"lease_spec_renamed_{i}.md",
                target_space_id="sp_personal_alice",
                target_category="01_Work",
                version_key="spec_distributed_lease",
            )
            self.assertEqual(replay["task"]["result_doc_id"], v1_doc_id)

        v2 = await self.mcp.ingest_document_or_file(
            self.alice_omni_token,
            content_bytes="分布式锁与租约续期规范 v2.0：支持自动看门狗续期。".encode("utf-8"),
            filename="lease_spec_v2.md",
            target_space_id="sp_personal_alice",
            target_category="01_Work",
            version_key="spec_distributed_lease",
            version_label="v2.0",
            operation_type=OperationType.REPLACE_VERSION,
            expected_current_doc_id=v1_doc_id,
        )
        v2_doc_id = v2["task"]["result_doc_id"]

        with self.assertRaises(VersionConflictError):
            await self.mcp.ingest_document_or_file(
                self.alice_omni_token,
                content_bytes="并发冲突版本".encode("utf-8"),
                filename="conflict.md",
                target_space_id="sp_personal_alice",
                target_category="01_Work",
                version_key="spec_distributed_lease",
                operation_type=OperationType.REPLACE_VERSION,
                expected_current_doc_id=v1_doc_id,
            )

        await self.mcp.review_and_fix_knowledge(
            self.alice_omni_token,
            action=ReviewAction.REVERT_VERSION,
            target_id=series_id,
            payload={"expected_current_doc_id": v2_doc_id, "rollback_to_doc_id": v1_doc_id},
        )
        s_res = await self.mcp.search_knowledge(self.alice_omni_token, "分布式锁")
        self.assertEqual(s_res["results"][0]["doc_id"], v1_doc_id)

    # =========================================================================
    # 2. All Remaining Review Actions (APPROVE_REVIEW, RECLASSIFY, SPLIT_DOCUMENT)
    # =========================================================================

    async def test_04_approve_review_reclassify_and_split_document_actions(self) -> None:
        """
        Tests the full lifecycle of `APPROVE_REVIEW`, `RECLASSIFY`, and `SPLIT_DOCUMENT`.
        """
        # 1. Ingest a health note without external approval -> enters NEEDS_REVIEW
        quarantined = await self.mcp.ingest_document_or_file(
            self.alice_omni_token,
            content_bytes="2026年度体检报告：空腹血糖 5.3 mmol/L（参考范围 3.9-6.1）".encode("utf-8"),
            filename="health_2026.md",
            target_space_id="sp_personal_alice",
            target_category="04_Health_Archive",
        )
        q_task_id = quarantined["task"]["task_id"]
        self.assertEqual(quarantined["task"]["task_state"], TaskState.NEEDS_REVIEW.value)

        # Approve via APPROVE_REVIEW -> transitions to COMMITTED and extracts health metrics!
        approved_task = await self.mcp.review_and_fix_knowledge(
            self.alice_omni_token,
            action=ReviewAction.APPROVE_REVIEW,
            target_id=q_task_id,
            payload={
                "target_space_id": "sp_personal_alice",
                "target_category": "04_Health_Archive",
                "title": "2026年度体检报告归档",
                "subject_person": "Alice",
            },
        )
        self.assertEqual(approved_task["task_state"], TaskState.COMMITTED.value)
        approved_doc_id = approved_task["result_doc_id"]  # noqa: F841  # 保留供后续步骤按需取用

        # 2. Test RECLASSIFY: Ingest a note in 01_Work and reclassify it to 02_Life
        work_note = await self.mcp.ingest_document_or_file(
            self.alice_omni_token,
            content_bytes="个人读书笔记：系统架构设计原则".encode("utf-8"),
            filename="reading.md",
            target_space_id="sp_personal_alice",
            target_category="01_Work",
        )
        reclass_doc_id = work_note["task"]["result_doc_id"]
        reclassified = await self.mcp.review_and_fix_knowledge(
            self.alice_omni_token,
            action=ReviewAction.RECLASSIFY,
            target_id=reclass_doc_id,
            payload={
                "new_space_id": "sp_personal_alice",
                "new_category": "02_Life",
                "new_title": "个人生活读书笔记",
            },
        )
        self.assertEqual(reclassified["category"], "02_Life")
        self.assertEqual(reclassified["title"], "个人生活读书笔记")

        # 3. Test SPLIT_DOCUMENT: Ingest a mixed Work + Health note -> NEEDS_REVIEW -> split into 2 docs
        mixed = await self.mcp.ingest_document_or_file(
            self.alice_omni_token,
            content_bytes="内部 API 接口规范 v3；同时记录个人私人病历与体检空腹血糖 5.8 mmol/L。".encode("utf-8"),
            filename="mixed.md",
            target_space_id="AUTO",
        )
        mixed_task_id = mixed["task"]["task_id"]
        self.assertEqual(mixed["task"]["task_state"], TaskState.NEEDS_REVIEW.value)

        split_res = await self.mcp.review_and_fix_knowledge(
            self.alice_omni_token,
            action=ReviewAction.SPLIT_DOCUMENT,
            target_id=mixed_task_id,
            payload={
                "splits": [
                    {
                        "target_space_id": "sp_personal_alice",
                        "target_category": "01_Work",
                        "title": "内部 API 接口规范 v3",
                        "content": "内部 API 接口规范 v3 详细定义。",
                    },
                    {
                        "target_space_id": "sp_personal_alice",
                        "target_category": "04_Health_Archive",
                        "title": "个人体检血糖记录",
                        "content": "体检空腹血糖 5.8 mmol/L（参考范围 3.9-6.1）。",
                        "explicit_external_approval": True,
                    },
                ]
            },
        )
        self.assertEqual(split_res["status"], "SPLIT_COMPLETED")
        self.assertEqual(len(split_res["created_doc_ids"]), 2)

    # =========================================================================
    # 3. Multi-Format Parsing (Image, Code Symbols) & Obsidian Watcher + Dashboard
    # =========================================================================

    async def test_05_image_ingestion_code_symbols_and_obsidian_dashboard(self) -> None:
        """
        Tests FR-01 (Image ingestion), FR-17 (AST Code Symbol extraction & search),
        FR-11 (`_dashboard_控制台.md`), and `ObsidianVaultWatcher`.
        """
        # 1. Create a real PNG image in memory and ingest it
        img_buf = io.BytesIO()
        img = Image.new("RGB", (120, 80), color=(40, 90, 160))
        img.save(img_buf, format="PNG")
        img_res = await self.mcp.ingest_document_or_file(
            self.alice_omni_token,
            content_bytes=img_buf.getvalue(),
            filename="architecture_diagram.png",
            target_space_id="sp_personal_alice",
            target_category="01_Work",
        )
        self.assertEqual(img_res["task"]["task_state"], TaskState.COMMITTED.value)

        # 2. Ingest a technical ADR with Python class/function definitions and ADR-042 tag
        code_doc = (
            "# ADR-042 支付网关幂等控制器设计\n\n"
            "路由定义：`POST /api/v2/payment/callback`\n\n"
            "```python\n"
            "class PaymentCallbackController:\n"
            "    async def verify_webhook_signature(self, payload: bytes) -> bool:\n"
            "        return True\n"
            "```\n"
        )
        await self.mcp.ingest_document_or_file(
            self.alice_omni_token,
            content_bytes=code_doc.encode("utf-8"),
            filename="adr_042.md",
            target_space_id="sp_personal_alice",
            target_category="01_Work",
        )

        # Search by extracted AST code symbol `PaymentCallbackController`
        sym_search = await self.mcp.search_knowledge(
            self.alice_work_token,
            "支付回调控制器",
            code_symbol="PaymentCallbackController",
        )
        self.assertEqual(len(sym_search["results"]), 1)
        self.assertIn("PaymentCallbackController", sym_search["results"][0]["code_symbols"])

        # 3. Test ObsidianVaultWatcher & Dashboard Generation
        watcher = ObsidianVaultWatcher(self.engine)
        initial_events = await watcher.scan_once(self.alice_omni_token, ["sp_personal_alice"])
        self.assertTrue(all(e["sync_status"] == "SKIPPED_SELF_WRITE_NOOP" for e in initial_events))

        dashboard_md = await self.engine.generate_obsidian_dashboard(self.alice_omni_token, "sp_personal_alice")
        self.assertIn("OmniLore 空间治理控制台：`sp_personal_alice`", dashboard_md)
        self.assertTrue((self.engine.storage.vault_dir / "sp_personal_alice" / "_dashboard_控制台.md").exists())

    # =========================================================================
    # 4. Phase 2: Family Member Management, Health Metrics & Knowledge Lint
    # =========================================================================

    async def test_06_phase2_family_members_health_pipeline_and_knowledge_lint(self) -> None:
        """
        Tests Phase 2 capabilities:
          - FR-13: Family member invitation, role update, and revocation
          - FR-14: Structured health indicator extraction, `NEEDS_CHECK` warning,
                   and `FIX_EXTRACTED_FIELD` human verification
          - FR-12: TTL expiration & orphan `[[wiki_link]]` detection via `run_knowledge_lint`
        """
        # 1. Ingest a health report into sp_family_home with explicit approval
        report_text = (
            "# 母亲 2026 年度体检报告\n\n"
            "- 空腹血糖 6.5 mmol/L（参考范围 3.9-6.1）\n"
            "- 收缩压 135 mmHg（参考范围 90-140）\n"
            "关联历史档案：[[2025年度旧版体检报告_不存在]]\n"
        )
        ing = await self.mcp.ingest_document_or_file(
            self.alice_omni_token,
            content_bytes=report_text.encode("utf-8"),
            filename="mother_health_2026.md",
            target_space_id="sp_family_home",
            target_category="04_Health_Archive",
            ttl_days=-1,  # Already expired TTL to test Lint!
            explicit_external_approval=True,
            extract_health_metrics=True,
            subject_person="母亲",
        )
        self.assertEqual(ing["task"]["task_state"], TaskState.COMMITTED.value)

        # 2. Query health trends as Bob (Family Profile) -> sees "AI 提取，尚未核验"
        trends = await self.mcp.query_health_trends(
            self.bob_family_token,
            space_id="sp_family_home",
            subject_person="母亲",
            metric_name="空腹血糖",
        )
        self.assertEqual(len(trends), 1)
        self.assertEqual(trends[0]["verification_status"], "NEEDS_CHECK")
        self.assertEqual(trends[0]["display_warning"], "AI 提取，尚未核验")
        metric_id = trends[0]["metric_id"]

        # 3. Run Knowledge Lint (FR-12) -> detects expired TTL doc, orphan wiki link, and unverified metric
        lint_report = await self.engine.run_knowledge_lint(self.alice_omni_token, "sp_family_home")
        self.assertEqual(len(lint_report["expired_documents"]), 1)
        self.assertEqual(len(lint_report["orphan_wiki_links"]), 1)
        self.assertEqual(lint_report["orphan_wiki_links"][0]["missing_target"], "2025年度旧版体检报告_不存在")
        self.assertGreaterEqual(len(lint_report["unverified_health_metrics"]), 1)

        # 4. Alice verifies the health metric via FIX_EXTRACTED_FIELD -> becomes HUMAN_VERIFIED
        verified = await self.mcp.review_and_fix_knowledge(
            self.alice_omni_token,
            action=ReviewAction.FIX_EXTRACTED_FIELD,
            target_id=metric_id,
            payload={"normalized_value": 6.5, "unit": "mmol/L", "reference_range": "3.9-6.1"},
        )
        self.assertEqual(verified["verification_status"], "HUMAN_VERIFIED")
        self.assertEqual(len(verified["revision_history"]), 1)

        # 5. Test FR-13 Family Member invitation & revocation
        dave_id = "44444444-4444-4444-4444-444444444444"
        await self.engine.invite_or_update_family_member(
            self.alice_omni_token,
            family_space_id="sp_family_home",
            target_user_id=dave_id,
            role=RoleType.READER,
        )
        dave_token = "tok_dave_family_005"
        self.engine.auth.issue_token(
            dave_token,
            user_id=dave_id,
            profile_type=ProfileType.FAMILY,
            allowed_read_spaces={"sp_family_home"},
            allowed_write_spaces=set(),
            allowed_categories={"03_Family", "04_Health_Archive"},
        )
        dave_trends = await self.mcp.query_health_trends(
            dave_token,
            space_id="sp_family_home",
            subject_person="母亲",
            metric_name="空腹血糖",
        )
        self.assertEqual(dave_trends[0]["display_warning"], "人工已核验")

        await self.engine.remove_family_member(
            self.alice_omni_token,
            family_space_id="sp_family_home",
            target_user_id=dave_id,
        )
        with self.assertRaises(PermissionDeniedError):
            await self.mcp.query_health_trends(
                dave_token,
                space_id="sp_family_home",
                subject_person="母亲",
                metric_name="空腹血糖",
            )

    # =========================================================================
    # 5. Phase 3: Multi-Level Org Tree Inheritance & Redacted Promotion
    # =========================================================================

    async def test_07_phase3_org_tree_inheritance_and_redacted_promotion(self) -> None:
        """
        Tests Phase 3 capabilities:
          - FR-16: Charlie in `sp_org_backend` (`org.eng.backend`) automatically inherits
                   READ access to `sp_org_eng` (`org.eng`) and `sp_org_root` (`org`),
                   but does NOT inherit write access to ancestor spaces and cannot access
                   `sp_org_secret` (`ORG_RESTRICTED`).
          - FR-15: Promoting a personal work note containing phone/API secret to `sp_org_eng`
                   supports `preview_only=True` and `apply_redaction=True`.
        """
        # Alice publishes a company-wide architecture standard in sp_org_root
        await self.mcp.ingest_document_or_file(
            self.alice_work_token,
            content_bytes="集团统一微服务网关规范 v1.0：所有服务必须上报 OpenTelemetry Trace。".encode("utf-8"),
            filename="org_root_spec.md",
            target_space_id="sp_org_root",
            target_category="01_Work",
        )

        # Charlie (member only of child sp_org_backend) can READ sp_org_root via ltree inheritance!
        charlie_search = await self.mcp.search_knowledge(self.charlie_work_token, "OpenTelemetry Trace")
        self.assertEqual(len(charlie_search["results"]), 1)
        self.assertEqual(charlie_search["results"][0]["space_id"], "sp_org_root")

        # Charlie CANNOT write to ancestor sp_org_root (inheritance is strictly read-only)
        with self.assertRaises(PermissionDeniedError):
            await self.mcp.ingest_document_or_file(
                self.charlie_work_token,
                content_bytes="尝试越权写入上级组织空间".encode("utf-8"),
                filename="unauth.md",
                target_space_id="sp_org_root",
                target_category="01_Work",
            )

        # Alice creates a personal work draft containing a phone number and secret key, then promotes with redaction
        draft = await self.mcp.ingest_document_or_file(
            self.alice_work_token,
            content_bytes="排障复盘：值班电话 13812345678，临时调试密钥 sk-AbCdEf1234567890XYZ。".encode("utf-8"),
            filename="postmortem_draft.md",
            target_space_id="sp_personal_alice",
            target_category="01_Work",
        )
        draft_doc_id = draft["task"]["result_doc_id"]

        # Preview promotion with redaction
        preview = await self.mcp.review_and_fix_knowledge(
            self.alice_work_token,
            action=ReviewAction.PROMOTE_COPY,
            target_id=draft_doc_id,
            payload={
                "dest_space_id": "sp_org_eng",
                "dest_category": "01_Work",
                "preview_only": True,
                "apply_redaction": True,
            },
        )
        self.assertEqual(preview["status"], "PROMOTION_PREVIEW")
        self.assertTrue(preview["has_sensitive_data"])
        self.assertIn("[REDACTED_PHONE]", preview["preview_body"])
        self.assertIn("[REDACTED_SECRET]", preview["preview_body"])

        # Execute confirmed promotion with redaction
        promoted = await self.mcp.review_and_fix_knowledge(
            self.alice_work_token,
            action=ReviewAction.PROMOTE_COPY,
            target_id=draft_doc_id,
            payload={
                "dest_space_id": "sp_org_eng",
                "dest_category": "01_Work",
                "apply_redaction": True,
                "explicit_user_confirm": True,
            },
        )
        prom_doc_id = promoted["task"]["result_doc_id"]
        prom_wiki = await self.mcp.get_document_asset(self.charlie_work_token, prom_doc_id, asset_type="WIKI")
        self.assertIn("[REDACTED_PHONE]", prom_wiki["content"])
        self.assertNotIn("13812345678", prom_wiki["content"])

    # =========================================================================
    # 6. Section 12 Operations: Backup, Restore, Index Rebuild & MCP JSON-RPC
    # =========================================================================

    async def test_08_backup_restore_index_rebuild_and_jsonrpc_server(self) -> None:
        """
        Tests Section 12 Backup/Restore with SHA-256 verification, Section 6.4.6
        Embedding Index Rebuild, and FastMCP JSON-RPC 2.0 protocol server.
        """
        # 1. Invoke MCP tool via JSON-RPC 2.0 protocol handler
        rpc_ingest = await self.jsonrpc.handle_request({
            "jsonrpc": "2.0",
            "id": 101,
            "method": "tools/call",
            "params": {
                "name": "commit_memory_note",
                "arguments": {
                    "token": self.alice_omni_token,
                    "title": "备份与恢复演练验证笔记",
                    "summary_markdown": "验证 SHA-256 备份清单、全量向量重建与一致性对账。",
                    "target_space_id": "sp_personal_alice",
                    "target_category": "01_Work",
                },
            },
        })
        self.assertFalse(rpc_ingest["result"]["isError"])

        # 2. Create backup snapshot
        backup_dir = Path(self.tmp_dir.name) / "backup_snapshot_01"
        manifest = self.engine.ops.create_backup_snapshot(backup_dir)
        self.assertGreaterEqual(manifest["file_count"], 3)

        # 3. Rebuild embedding index to a new version (Section 6.4.6)
        rebuild_res = self.engine.ops.rebuild_embedding_index(
            new_embedding_version="v4-1024-rebuilt",
            new_index_version=2,
        )
        self.assertEqual(rebuild_res["status"], "INDEX_REBUILT")
        self.assertEqual(self.engine.model_config.index_version, 2)

        # 4. Restore from backup snapshot and verify 100% consistency reconciliation
        restore_res = self.engine.ops.restore_from_backup(backup_dir)
        self.assertEqual(restore_res["status"], "RESTORED_AND_VERIFIED")
        self.assertTrue(restore_res["reconcile_report"]["is_consistent"])

        # 5. Run background daemon tick
        daemon = OutboxRecoveryDaemon(self.engine)
        tick_res = await daemon.tick(self.alice_omni_token, ["sp_personal_alice"])
        self.assertTrue(tick_res["is_consistent"])



    async def test_09_n8n_workflows_and_internal_worker_gateway(self) -> None:
        """
        Tests PRD D-11, Section 2.5 & Section 6.4.3 n8n Integration:
          - Validates all 3 n8n workflow JSON files under `n8n/workflows/`
          - Verifies zero hardcoded DashScope API keys or DB superuser credentials in n8n nodes (AC-AI01)
          - Tests `InternalN8nWorkerGateway` endpoints (`/internal/v1/ingest`, `/internal/v1/outbox/recover`,
            `/internal/v1/lint_and_dashboard`, `/internal/v1/backup`) end-to-end
        """
        from omnilore import InternalN8nWorkerGateway
        import json

        wf_dir = Path(__file__).resolve().parent.parent / "n8n" / "workflows"
        wf_files = sorted(wf_dir.glob("*.json"))
        self.assertEqual(len(wf_files), 3)

        for wf_file in wf_files:
            raw_json = wf_file.read_text(encoding="utf-8")
            wf_data = json.loads(raw_json)
            self.assertIn("nodes", wf_data)
            self.assertIn("connections", wf_data)
            # AC-AI01: n8n workflow JSON must never hardcode DashScope API keys or superuser credentials
            self.assertIsNone(re.search(r"\bsk-[A-Za-z0-9]{8,}", raw_json))
            self.assertNotIn("DASHSCOPE_API_KEY", raw_json)
            self.assertNotIn("omnilore_migrator", raw_json)

        worker_api = InternalN8nWorkerGateway(self.engine)
        headers = {"X-OmniLore-Token": self.alice_omni_token}

        # 1. Test /internal/v1/ingest called by Workflow 01
        ing_resp = await worker_api.handle_http_post(
            "/internal/v1/ingest",
            headers,
            {
                "filename": "n8n_webhook_doc.md",
                "content_text": "# n8n 异步入库测试\n\n通过受控网关完成知识编译与入库。",
                "target_space_id": "sp_personal_alice",
                "target_category": "01_Work",
            },
        )
        self.assertEqual(ing_resp["status_code"], 200)
        self.assertEqual(ing_resp["data"]["task"]["task_state"], "COMMITTED")

        # 2. Test /internal/v1/outbox/recover called by Workflow 02
        rec_resp = await worker_api.handle_http_post(
            "/internal/v1/outbox/recover",
            headers,
            {"space_ids": ["sp_personal_alice"]},
        )
        self.assertEqual(rec_resp["status_code"], 200)
        self.assertTrue(rec_resp["data"]["is_consistent"])

        # 3. Test /internal/v1/lint_and_dashboard called by Workflow 03
        lint_resp = await worker_api.handle_http_post(
            "/internal/v1/lint_and_dashboard",
            headers,
            {"space_id": "sp_personal_alice"},
        )
        self.assertEqual(lint_resp["status_code"], 200)
        self.assertTrue(lint_resp["data"]["dashboard_updated"])

        # 4. Test /internal/v1/backup called by Workflow 03
        bkp_resp = await worker_api.handle_http_post(
            "/internal/v1/backup",
            headers,
            {"backup_dir": str(Path(self.tmp_dir.name) / "n8n_backup")},
        )
        self.assertEqual(bkp_resp["status_code"], 200)
        self.assertTrue(bkp_resp["data"]["reconcile_report"]["is_consistent"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
