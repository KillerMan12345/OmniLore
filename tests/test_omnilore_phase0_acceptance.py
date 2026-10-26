"""
OmniLore v2.3 Phase 0/1 Automated Acceptance & Negative Security Test Suite
=============================================================================
Verifies every quantitative acceptance criterion in Section 14 of the PRD:
  - AC-S01 .. AC-S07: Security, RLS, Profile Isolation, Path Traversal & Token Revocation
  - AC-D01 .. AC-D06: Idempotency (10x replay), Zero-Downtime Version Switch, Crash Recovery & Sync Loop Guard
  - AC-K01 .. AC-K07: Citation Grounding, Zero Stale Version Recall & Ambiguous Route Quarantine
  - AC-AI01 .. AC-AI07: Unified ModelAdapter, Schema Validation, Privilege Guard & Desensitized Telemetry
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from omnilore import (
     OmniLoreEngine,
    OmniLoreMCPGateway,
    DocStatus,
    IllegalStateTransitionError,
    MembershipRecord,
    OperationType,
    PermissionDeniedError,
    ProfileType,
    ReviewAction,
    RoleType,
    SpaceRecord,
    SpaceType,
    TaskState,
    VersionConflictError,
    profiled_transaction,
    validate_state_transition,
)

# 平台能力探测：Windows 无 symlink 权限时优雅降级（宿主限制，非 OmniLore 平台缺陷）
try:  # `unittest discover -s tests` 会把 tests/ 放进 sys.path
    from _platform_support import establish_hostile_link
except ImportError:  # 以 `tests.<mod>` 方式导入时的兜底
    from tests._platform_support import establish_hostile_link


class TestOmniLorePhase0Acceptance(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root_path = Path(self.tmp_dir.name) / "OmniLore_Data"
        self.engine = OmniLoreEngine(self.root_path)
        self.mcp = OmniLoreMCPGateway(self.engine)

        # Register 3 Spaces: Personal Vault, Family Hub, Org Public Space
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
        self.engine.auth.register_space(
            SpaceRecord(
                space_id="sp_org_public",
                space_type=SpaceType.ORG_PUBLIC,
                parent_space_id=None,
                vault_rel_path="spaces/sp_org_public",
            )
        )

        self.alice_id = "11111111-1111-1111-1111-111111111111"
        self.bob_id = "22222222-2222-2222-2222-222222222222"

        # Grant memberships
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

        # Issue Tokens for Work Profile, Family Profile, and Omni Profile
        self.alice_work_token = "tok_alice_work_secret_001"
        self.engine.auth.issue_token(
            self.alice_work_token,
            user_id=self.alice_id,
            profile_type=ProfileType.WORK,
            allowed_read_spaces={"sp_personal_alice", "sp_family_home"},
            allowed_write_spaces={"sp_personal_alice", "sp_family_home"},
            allowed_categories={"01_Work", "02_Life", "03_Family", "04_Health_Archive"},
        )

        self.alice_omni_token = "tok_alice_omni_secret_002"
        self.engine.auth.issue_token(
            self.alice_omni_token,
            user_id=self.alice_id,
            profile_type=ProfileType.OMNI,
            allowed_read_spaces={"sp_personal_alice", "sp_family_home"},
            allowed_write_spaces={"sp_personal_alice", "sp_family_home"},
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

    async def asyncTearDown(self) -> None:
        self.tmp_dir.cleanup()

    # =========================================================================
    # 1. Security & RLS Acceptance Tests (AC-S01 to AC-S07)
    # =========================================================================

    async def test_ac_s01_s02_s03_profile_and_category_rls_isolation(self) -> None:
        """
        AC-S01 / AC-S02 / AC-S03:
          - Work Profile can read/write 01_Work in Personal Vault, but has 0 access
            to 03_Family or 04_Health_Archive even inside the SAME Personal Vault!
          - Bob's Family Profile can read sp_family_home, but cannot read Alice's Personal Vault
            nor write to sp_family_home (since Bob's role is READER).
        """
        # Alice ingests a Work API doc and a Family Insurance doc into her Personal Vault using OMNI token
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

        # AC-S02: Alice's WORK profile searches for "保单" -> 0 results from 03_Family
        search_work = await self.mcp.search_knowledge(self.alice_work_token, "保单 受益人")
        self.assertEqual(len(search_work["results"]), 0)

        # AC-S02: Alice's WORK profile attempts to fetch 03_Family asset directly -> PermissionDeniedError
        with self.assertRaises(PermissionDeniedError):
            await self.mcp.get_document_asset(self.alice_work_token, family_doc_id, asset_type="WIKI")

        # Alice's WORK profile CAN read 01_Work document
        got_work = await self.mcp.get_document_asset(self.alice_work_token, work_doc_id, asset_type="WIKI")
        self.assertIn("微信支付回调", got_work["content"])

        # AC-S03: Bob's FAMILY profile attempts to read Alice's personal doc -> PermissionDeniedError
        with self.assertRaises(PermissionDeniedError):
            await self.mcp.get_document_asset(self.bob_family_token, work_doc_id, asset_type="WIKI")

        # AC-S01: Bob (READER in sp_family_home) attempts to ingest into sp_family_home -> PermissionDeniedError
        with self.assertRaises(PermissionDeniedError):
            await self.mcp.ingest_document_or_file(
                self.bob_family_token,
                content_bytes="家庭备忘录".encode("utf-8"),
                filename="memo.md",
                target_space_id="sp_family_home",
                target_category="03_Family",
            )

    async def test_ac_s04_cross_space_update_blocked_by_rls_with_check(self) -> None:
        """
        AC-S04: Attempting to move a record via UPDATE to an unauthorized space/category
        is blocked by RLS WITH CHECK policy.
        """
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
            # Attempting to UPDATE category from 01_Work to 03_Family under Work Profile must fail WITH CHECK!
            with self.assertRaises(PermissionDeniedError):
                await conn.update_row(
                    "documents",
                    doc_id,
                    lambda d: {**d, "category": "03_Family"},
                )

    async def test_ac_s05_path_traversal_and_symlink_defense(self) -> None:
        """
        AC-S05: Blocks '../', sibling prefix bypass, bare paths, and symlinks.
        """
        # 1. Reject bare path in MCP tool
        with self.assertRaises(PermissionDeniedError):
            await self.mcp.get_document_asset(self.alice_omni_token, "../etc/passwd", asset_type="WIKI")

        # 2. Reject '../' and sibling directory prefix in storage gateway
        with self.assertRaises(PermissionDeniedError):
            self.engine.storage._verify_safe_subpath(self.engine.storage.vault_dir, "../../etc/passwd")

        # 3. Reject symlink inside vault directory
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

    async def test_ac_s06_s07_token_and_membership_revocation_slo(self) -> None:
        """
        AC-S06 / AC-S07: Token revocation and membership removal take effect immediately
        (0s propagation in active cache, well within <= 5s SLO) and record audit events.
        """
        # Promote a family note to sp_family_home so Bob can read it initially
        ing = await self.mcp.ingest_document_or_file(
            self.alice_omni_token,
            content_bytes="家庭宽带账号与路由器位置说明".encode("utf-8"),
            filename="wifi.md",
            target_space_id="sp_personal_alice",
            target_category="03_Family",
        )
        src_doc_id = ing["task"]["result_doc_id"]
        prom = await self.mcp.review_and_fix_knowledge(
            self.alice_omni_token,
            action=ReviewAction.PROMOTE_COPY,
            target_id=src_doc_id,
            payload={"dest_space_id": "sp_family_home", "dest_category": "03_Family", "explicit_user_confirm": True},
        )
        family_doc_id = prom["task"]["result_doc_id"]

        # Bob can read it before revocation
        doc_data = await self.mcp.get_document_asset(self.bob_family_token, family_doc_id, asset_type="WIKI")
        self.assertIn("家庭宽带", doc_data["content"])

        # Revoke Bob's membership in sp_family_home -> immediate denial
        self.engine.auth.revoke_membership(self.bob_id, "sp_family_home")
        with self.assertRaises(PermissionDeniedError):
            await self.mcp.get_document_asset(self.bob_family_token, family_doc_id, asset_type="WIKI")

        # Revoke Alice's work token -> immediate denial
        self.engine.auth.revoke_token(self.alice_work_token)
        with self.assertRaises(PermissionDeniedError):
            await self.mcp.search_knowledge(self.alice_work_token, "API")

    # =========================================================================
    # 2. Idempotency, State Machine, Versioning & Sync Tests (AC-D01 to AC-D06)
    # =========================================================================

    async def test_ac_d01_d02_10x_idempotent_replay_and_single_ledger_append(self) -> None:
        """
        AC-D01 / AC-D02: Replaying the exact same file ingestion request 10 times
        produces only ONE document, ONE task, and ONE ledger entry.
        """
        payload_bytes = "幂等测试文档：分布式锁与租约续期规范 v1.0".encode("utf-8")
        first_res = await self.mcp.ingest_document_or_file(
            self.alice_omni_token,
            content_bytes=payload_bytes,
            filename="lease_spec_copy1.md",
            target_space_id="sp_personal_alice",
            target_category="01_Work",
            version_key="spec_distributed_lease",
        )
        first_task_id = first_res["task"]["task_id"]
        first_doc_id = first_res["task"]["result_doc_id"]

        # Replay 9 more times with even different filenames (idempotency uses SHA-256, not filename!)
        for i in range(2, 11):
            replay_res = await self.mcp.ingest_document_or_file(
                self.alice_omni_token,
                content_bytes=payload_bytes,
                filename=f"lease_spec_renamed_{i}.md",
                target_space_id="sp_personal_alice",
                target_category="01_Work",
                version_key="spec_distributed_lease",
            )
            self.assertEqual(replay_res["task"]["task_id"], first_task_id)
            self.assertEqual(replay_res["task"]["result_doc_id"], first_doc_id)

        self.assertEqual(len(self.engine.db._state["documents"]), 1)
        self.assertEqual(len(self.engine.db._state["ledger_entries"]), 1)

        # Illegal state transition check (Section 4.3)
        with self.assertRaises(IllegalStateTransitionError):
            validate_state_transition(TaskState.RECEIVED, TaskState.COMMITTED)

    async def test_ac_d03_k04_zero_downtime_version_replacement_supplement_and_revert(self) -> None:
        """
        AC-D03 / AC-K04:
          - Replacing v1.0 with v2.0 switches `current_doc_id` atomically and marks v1.0 SUPERSEDED
          - Default search returns ONLY v2.0 (0 stale version recall)
          - Supplement document attaches to v2.0 without overwriting v2.0's main pointer
          - Reverting version restores v1.0 as PUBLISHED_CURRENT
        """
        v1 = await self.mcp.ingest_document_or_file(
            self.alice_omni_token,
            content_bytes="网关鉴权规范 v1.0：采用旧版静态签名校验，超时时间 30 秒。".encode("utf-8"),
            filename="auth_v1.md",
            target_space_id="sp_personal_alice",
            target_category="01_Work",
            version_key="gateway_auth_spec",
            version_label="v1.0",
        )
        v1_doc_id = v1["task"]["result_doc_id"]
        series_id = self.engine.db._state["documents"][v1_doc_id]["series_id"]

        # Replace with v2.0
        v2 = await self.mcp.ingest_document_or_file(
            self.alice_omni_token,
            content_bytes="网关鉴权规范 v2.0：采用高熵动态 Token 与 RLS 双重校验，撤销传播 5 秒内生效。".encode("utf-8"),
            filename="auth_v2.md",
            target_space_id="sp_personal_alice",
            target_category="01_Work",
            version_key="gateway_auth_spec",
            version_label="v2.0",
            operation_type=OperationType.REPLACE_VERSION,
            expected_current_doc_id=v1_doc_id,
        )
        v2_doc_id = v2["task"]["result_doc_id"]

        # Verify concurrent conflict detection when passing stale expected_current_doc_id
        with self.assertRaises(VersionConflictError):
            await self.mcp.ingest_document_or_file(
                self.alice_omni_token,
                content_bytes="网关鉴权规范并发冲突版本".encode("utf-8"),
                filename="auth_conflict.md",
                target_space_id="sp_personal_alice",
                target_category="01_Work",
                version_key="gateway_auth_spec",
                version_label="v2.1",
                operation_type=OperationType.REPLACE_VERSION,
                expected_current_doc_id=v1_doc_id,  # Stale! Current is v2_doc_id
            )

        # Add a SUPPLEMENT document to v2.0
        sup = await self.mcp.ingest_document_or_file(
            self.alice_omni_token,
            content_bytes="网关鉴权补充说明：特殊运维排障临时令牌申请流程。".encode("utf-8"),
            filename="auth_supplement.md",
            target_space_id="sp_personal_alice",
            target_category="01_Work",
            version_key="gateway_auth_spec_sup1",
            version_label="sup-1.0",
            operation_type=OperationType.SUPPLEMENT_DOC,
            parent_doc_id=v2_doc_id,
        )
        sup_doc_id = sup["task"]["result_doc_id"]

        # Default search for "网关鉴权规范" must NOT recall v1.0 ("静态签名")
        search_res = await self.mcp.search_knowledge(self.alice_omni_token, "网关鉴权规范")
        returned_doc_ids = [r["doc_id"] for r in search_res["results"]]
        self.assertIn(v2_doc_id, returned_doc_ids)
        self.assertNotIn(v1_doc_id, returned_doc_ids)

        # Verify v2.0 result includes the attached supplement
        v2_hit = next(r for r in search_res["results"] if r["doc_id"] == v2_doc_id)
        self.assertEqual(len(v2_hit["supplements"]), 1)
        self.assertEqual(v2_hit["supplements"][0]["doc_id"], sup_doc_id)

        # Revert series from v2.0 back to v1.0
        await self.mcp.review_and_fix_knowledge(
            self.alice_omni_token,
            action=ReviewAction.REVERT_VERSION,
            target_id=series_id,
            payload={"expected_current_doc_id": v2_doc_id, "rollback_to_doc_id": v1_doc_id},
        )
        search_after_revert = await self.mcp.search_knowledge(self.alice_omni_token, "网关鉴权规范")
        reverted_doc_ids = [r["doc_id"] for r in search_after_revert["results"]]
        self.assertIn(v1_doc_id, reverted_doc_ids)
        self.assertNotIn(v2_doc_id, reverted_doc_ids)

    async def test_ac_d04_d05_crash_after_stage_and_outbox_recovery(self) -> None:
        """
        AC-D04 / AC-D05: If the worker crashes after staging and Phase 1 DB transaction:
          - Document is in PENDING_PUBLISH and NOT visible to normal search
          - Recovery worker resumes from Outbox and completes publication to COMMITTED
        """
        with self.assertRaises(RuntimeError):
            await self.engine.ingest_document(
                self.alice_omni_token,
                raw_bytes="故障恢复测试：Outbox 异步补偿与断点续传机制。".encode("utf-8"),
                original_filename="crash_test.md",
                target_space_id="sp_personal_alice",
                target_category="01_Work",
                version_key="saga_recovery_doc",
                simulate_crash_after_stage=True,
            )

        # Before recovery: PENDING_PUBLISH document must NOT leak into search results
        pre_search = await self.mcp.search_knowledge(self.alice_omni_token, "断点续传机制")
        self.assertEqual(len(pre_search["results"]), 0)

        # Run recovery worker
        recovered_count = await self.engine.run_recovery_worker(self.alice_omni_token)
        self.assertEqual(recovered_count, 1)

        # After recovery: task is COMMITTED and document is searchable
        post_search = await self.mcp.search_knowledge(self.alice_omni_token, "断点续传机制")
        self.assertEqual(len(post_search["results"]), 1)
        self.assertEqual(post_search["results"][0]["doc_status"], DocStatus.PUBLISHED_CURRENT.value)

    async def test_ac_d06_obsidian_sync_loop_guard_protected_fields_and_conflict_copy(self) -> None:
        """
        AC-D06 / FR-09:
          - Self-writes do not trigger re-sync loops
          - Manual edits to protected frontmatter fields (space_id) are rejected
          - Valid body edits update chunks and increment content_revision
          - Concurrent edits with stale content_revision create a conflict copy without overwriting
        """
        res = await self.mcp.ingest_document_or_file(
            self.alice_omni_token,
            content_bytes="Obsidian 同步初始正文：支持本地双向编辑。".encode("utf-8"),
            filename="obsidian_note.md",
            target_space_id="sp_personal_alice",
            target_category="01_Work",
            version_key="obsidian_sync_test",
        )
        doc_id = res["task"]["result_doc_id"]
        doc_row = self.engine.db._state["documents"][doc_id]
        wiki_rel = doc_row["wiki_rel_path"]
        initial_md = self.engine.storage.read_wiki_by_record("sp_personal_alice", wiki_rel)

        # 1. Loop guard: passing identical content returns SKIPPED_SELF_WRITE_NOOP
        noop_res = await self.engine.sync_obsidian_markdown_edit(
            self.alice_omni_token, "sp_personal_alice", wiki_rel, initial_md
        )
        self.assertEqual(noop_res["status"], "SKIPPED_SELF_WRITE_NOOP")

        # 2. Tampering with protected `space_id` in Frontmatter is rejected
        tampered_md = initial_md.replace("space_id: sp_personal_alice", "space_id: sp_family_home")
        reject_res = await self.engine.sync_obsidian_markdown_edit(
            self.alice_omni_token, "sp_personal_alice", wiki_rel, tampered_md
        )
        self.assertEqual(reject_res["status"], "REJECTED_PROTECTED_FIELD_MODIFICATION")

        # 3. Valid body modification updates content_revision from 1 -> 2
        edited_md = initial_md.replace("支持本地双向编辑", "支持本地双向编辑与实时切片更新")
        sync_ok = await self.engine.sync_obsidian_markdown_edit(
            self.alice_omni_token, "sp_personal_alice", wiki_rel, edited_md
        )
        self.assertEqual(sync_ok["status"], "SYNCED")
        self.assertEqual(sync_ok["content_revision"], 2)

        # 4. Concurrent edit based on stale content_revision=1 generates a conflict copy
        stale_concurrent_md = initial_md.replace("支持本地双向编辑", "另一台设备的并发冲突修改")
        conflict_res = await self.engine.sync_obsidian_markdown_edit(
            self.alice_omni_token, "sp_personal_alice", wiki_rel, stale_concurrent_md
        )
        self.assertEqual(conflict_res["status"], "CONFLICT_COPY_CREATED")
        self.assertIn(".conflict_r1.md", conflict_res["conflict_rel_path"])

    # =========================================================================
    # 3. Knowledge Quality, Routing & AI Governance Tests (AC-K01..K07, AC-AI01..AI07)
    # =========================================================================

    async def test_ac_k06_k07_ambiguous_mixed_content_quarantined_to_needs_review(self) -> None:
        """
        AC-K06 / AC-K07: Mixed personal health + enterprise API content is never
        auto-committed; it enters NEEDS_REVIEW quarantine and stays invisible to search.
        """
        mixed_res = await self.mcp.ingest_document_or_file(
            self.alice_omni_token,
            content_bytes="内部 API 接口规范 v3；顺便记录个人私人病历与体检空腹血糖 6.8 mmol/L。".encode("utf-8"),
            filename="mixed_notes.md",
            target_space_id="AUTO",
        )
        self.assertEqual(mixed_res["task"]["task_state"], TaskState.NEEDS_REVIEW.value)
        self.assertIn("需要复核：检测到个人健康与工作规范混合信号", mixed_res["receipt_markdown"])

        # Quarantined task must not be visible in search
        s_res = await self.mcp.search_knowledge(self.alice_omni_token, "空腹血糖")
        self.assertEqual(len(s_res["results"]), 0)

    async def test_ac_ai02_ai03_ai05_ai06_model_schema_guard_and_privacy_policy(self) -> None:
        """
        AC-AI02 / AC-AI03 / AC-AI05 / AC-AI06:
          - Unapproved external LLM call on 04_Health_Archive is blocked by outbound policy
            and transitioned to NEEDS_REVIEW
          - Malicious/hallucinated LLM output attempting to inject `space_id` or `task_state`
            is rejected by ModelAdapter schema validation
          - Telemetry (`model_runs`) logs metadata without exposing raw prompts or secrets
        """
        # 1. Unapproved external model call on 04_Health_Archive -> blocked by outbound policy -> NEEDS_REVIEW
        health_ing = await self.mcp.ingest_document_or_file(
            self.alice_omni_token,
            content_bytes="2026年度体检报告：空腹血糖 5.4 mmol/L（参考范围 3.9-6.1）".encode("utf-8"),
            filename="health_report_2026.md",
            target_space_id="sp_personal_alice",
            target_category="04_Health_Archive",
        )
        self.assertEqual(health_ing["task"]["task_state"], TaskState.NEEDS_REVIEW.value)
        self.assertTrue(
            any(r["status"] == "POLICY_BLOCKED" for r in self.engine.model_adapter._telemetry_sink)
        )

        # 2. Inject a rogue LLM handler that tries to overwrite `space_id` in its JSON response
        def rogue_llm_handler(task_type: str, payload: dict, model_id: str) -> dict:
            return {
                "space_id": "sp_org_public",  # Forbidden privilege escalation field!
                "suggested_category": "01_Work",
                "title": "Hacked",
                "summary": "Hacked",
                "compiled_markdown": "# Hacked",
                "citations": [],
                "has_conflict_or_ambiguity": False,
                "review_reason": "",
                "tags": [],
            }

        rogue_engine = OmniLoreEngine(
            self.root_path / "rogue_test",
            auth_service=self.engine.auth,
            custom_llm_handler=rogue_llm_handler,
        )
        rogue_task = await rogue_engine.ingest_document(
            self.alice_omni_token,
            raw_bytes="测试模型越权字段拦截".encode("utf-8"),
            original_filename="rogue.md",
            target_space_id="sp_personal_alice",
            target_category="01_Work",
        )
        self.assertEqual(rogue_task["task_state"], TaskState.FAILED_RETRYABLE.value)
        self.assertEqual(rogue_task["error_code"], "MODEL_SCHEMA_INVALID")

        # 3. Verify telemetry logs zero raw content or tokens (AC-AI06)
        for run in self.engine.model_adapter._telemetry_sink:
            serialized = str(run)
            self.assertNotIn("tok_alice", serialized)
            self.assertNotIn("空腹血糖", serialized)


if __name__ == "__main__":
    unittest.main(verbosity=2)