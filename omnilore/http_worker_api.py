"""
Internal HTTP Worker API for n8n Workflow Orchestration
=======================================================
Implements Section 2.5, 6.4.3 & 7.1 of OmniLore PRD v2.3:
  - n8n acts strictly as an orchestration layer and NEVER holds LLM API keys
    or BYPASSRLS database credentials.
  - All n8n HTTP Request nodes call this internal gateway using a scoped
    OmniLore access token (`X-OmniLore-Token`).
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Dict

from .daemons import ObsidianVaultWatcher, OutboxRecoveryDaemon
from .engine import OmniLoreEngine, OmniLoreError, OmniLoreMCPGateway, OperationType


class InternalN8nWorkerGateway:
    """
    Handles structured JSON requests from n8n workflows:
      - POST /internal/v1/ingest
      - POST /internal/v1/outbox/recover
      - POST /internal/v1/lint_and_dashboard
      - POST /internal/v1/backup
    """

    def __init__(self, engine: OmniLoreEngine):
        self.engine = engine
        self.mcp = OmniLoreMCPGateway(engine)
        self.recovery_daemon = OutboxRecoveryDaemon(engine)
        self.watcher = ObsidianVaultWatcher(engine)

    async def handle_http_post(self, path: str, headers: Dict[str, str], body: Dict[str, Any]) -> Dict[str, Any]:
        token = headers.get("X-OmniLore-Token") or headers.get("x-omnilore-token") or body.get("token", "")
        try:
            if path == "/internal/v1/ingest":
                content_b64 = body.get("content_base64")
                raw_bytes = (
                    base64.b64decode(content_b64)
                    if content_b64
                    else str(body.get("content_text", "")).encode("utf-8")
                )
                res = await self.mcp.ingest_document_or_file(
                    token,
                    content_bytes=raw_bytes,
                    filename=body.get("filename", "n8n_ingest.md"),
                    target_space_id=body.get("target_space_id", "AUTO"),
                    target_category=body.get("target_category"),
                    version_key=body.get("version_key"),
                    version_label=body.get("version_label", "v1.0"),
                    operation_type=OperationType(body.get("operation_type", "INGEST_FILE")),
                    explicit_external_approval=bool(body.get("explicit_external_approval", False)),
                    extract_health_metrics=bool(body.get("extract_health_metrics", False)),
                )
                return {"status_code": 200, "data": res}

            if path == "/internal/v1/outbox/recover":
                space_ids = body.get("space_ids", ["sp_personal_alice"])
                tick_res = await self.recovery_daemon.tick(token, space_ids)
                sync_events = await self.watcher.scan_once(token, space_ids)
                return {
                    "status_code": 200,
                    "data": {
                        **tick_res,
                        "obsidian_sync_events": sync_events,
                    },
                }

            if path == "/internal/v1/lint_and_dashboard":
                space_id = body["space_id"]
                lint_report = await self.engine.run_knowledge_lint(token, space_id)
                dashboard_md = await self.engine.generate_obsidian_dashboard(token, space_id)
                return {
                    "status_code": 200,
                    "data": {
                        "lint_report": lint_report,
                        "dashboard_updated": True,
                        "dashboard_preview": dashboard_md[:240],
                    },
                }

            if path == "/internal/v1/backup":
                backup_dir = Path(body.get("backup_dir", str(self.engine.storage.base_dir / "ops" / "backups" / "latest")))
                manifest = self.engine.ops.create_backup_snapshot(backup_dir)
                reconcile = self.engine.ops.reconcile_consistency()
                return {
                    "status_code": 200,
                    "data": {
                        "backup_manifest": manifest,
                        "reconcile_report": reconcile,
                    },
                }

            return {"status_code": 404, "error": {"code": "ROUTE_NOT_FOUND", "message": f"Unknown route: {path}"}}
        except OmniLoreError as exc:
            status = 403 if exc.code == "PERMISSION_DENIED" else 400
            return {"status_code": status, "error": {"code": exc.code, "message": exc.message}}
