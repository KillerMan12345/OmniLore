"""D3 opt-in anonymous telemetry: the six hard constraints, executable.

Every constraint in ``docs/DECISIONS.md`` §3 has a test here.  In particular,
``test_validate_payload_rejects_*`` are mutation-style guards: they inject a
forbidden field and require a visible failure, so the denylist cannot silently
rot into a no-op.
"""

from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from omnilore.cli import main
from omnilore.telemetry import (
    TELEMETRY_ENV,
    TRANSPORT_UNAVAILABLE,
    build_payload,
    consent_path,
    is_enabled,
    load_state,
    record_command,
    record_event,
    set_consent,
    transmit,
    validate_payload,
)


class TestTelemetryD3Constraints(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name).resolve() / "data"
        self.data_dir.mkdir()
        self.env = patch.dict(os.environ, {}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop(TELEMETRY_ENV, None)

    def _run(self, *argv: str) -> tuple[int, str]:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(list(argv))
        return rc, buf.getvalue()

    # --- constraint 1 & 2: strict opt-in, off by default -------------------
    def test_fresh_install_is_off_and_writes_nothing(self) -> None:
        self.assertFalse(is_enabled(self.data_dir))
        self.assertFalse(consent_path(self.data_dir).exists())
        self.assertFalse(self.data_dir.joinpath("ops").exists())
        self.assertFalse(record_command(self.data_dir, "demo"))
        self.assertFalse(consent_path(self.data_dir).exists())

    def test_demo_runs_with_zero_telemetry_bytes(self) -> None:
        rc, out = self._run("demo", "--data-dir", str(self.data_dir), "--json")
        self.assertEqual(rc, 0)
        payload = json.loads(out[out.index("{"):])
        self.assertTrue(payload["found_demo_document"])
        self.assertFalse(consent_path(self.data_dir).exists(),
                         "demo must not create a telemetry state file")

    def test_env_value_of_one_cannot_bypass_explicit_consent(self) -> None:
        os.environ[TELEMETRY_ENV] = "1"
        self.assertFalse(is_enabled(self.data_dir))
        self.assertFalse(record_command(self.data_dir, "demo"))
        self.assertFalse(consent_path(self.data_dir).exists())

    # --- constraint 3 & 4: minimal fields, --show self-audit --------------
    def test_show_prints_the_complete_pii_free_payload(self) -> None:
        set_consent(self.data_dir, True)
        record_command(self.data_dir, "demo")
        record_event(self.data_dir, key="degraded_occurrences")
        record_event(self.data_dir, key="retrieval_modes", bucket="keyword_only")

        rc, out = self._run("telemetry", "--data-dir", str(self.data_dir), "--show")
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(payload, build_payload(self.data_dir))
        self.assertEqual(payload["schema_version"], 1)
        self.assertTrue(payload["consent"]["enabled"])
        self.assertEqual(payload["aggregate"]["commands"]["demo"], 1)
        self.assertEqual(payload["aggregate"]["degraded_occurrences"], 1)
        self.assertEqual(payload["aggregate"]["retrieval_modes"]["keyword_only"], 1)
        self.assertFalse(payload["transport"]["implemented"])
        self.assertIsNone(payload["transport"]["endpoint"])
        self.assertEqual(payload["transport"]["note"], TRANSPORT_UNAVAILABLE)

        serialised = json.dumps(payload, ensure_ascii=False)
        for forbidden in ("doc_id", "space_id", "token", "title", "query",
                          "file_path", "user_id", "ip_addr", "hostname"):
            self.assertNotIn(forbidden, serialised)
        validate_payload(payload)  # must not raise

    def test_validate_payload_rejects_forbidden_keys(self) -> None:
        clean = build_payload(self.data_dir)
        for mutation in (
            {**clean, "doc_id": "d-1"},
            {**clean, "aggregate": {**clean["aggregate"], "query": "secret"}},
            {**clean, "consent": {**clean["consent"], "user_id": "u-1"}},
            {**clean, "transport": {**clean["transport"], "endpoint": "https://example.invalid/hook"}},
        ):
            with self.subTest(mutation=list(mutation)):
                with self.assertRaises(ValueError):
                    validate_payload(mutation)

    def test_validate_payload_rejects_free_form_pii_looking_strings(self) -> None:
        clean = build_payload(self.data_dir)
        with self.assertRaises(ValueError):
            validate_payload({**clean, "consent": {**clean["consent"], "mode": "C:/Users/someone"}})
        with self.assertRaises(ValueError):
            validate_payload({**clean, "schema_version": 1.5})

    def test_only_whitelisted_command_names_are_recorded(self) -> None:
        set_consent(self.data_dir, True)
        record_command(self.data_dir, "verify-citation")
        record_command(self.data_dir, "C:/Users/someone/secret doc.md")
        state = load_state(self.data_dir)
        self.assertEqual(state.commands.get("verify-citation"), 1)
        self.assertEqual(state.commands.get("other"), 1)
        self.assertEqual(len(state.commands), 2)
        validate_payload(build_payload(self.data_dir))

    def test_counter_vocabulary_is_closed(self) -> None:
        set_consent(self.data_dir, True)
        self.assertTrue(record_event(self.data_dir, key="degraded_occurrences"))
        self.assertFalse(record_event(self.data_dir, key="raw_document_text"))
        self.assertEqual(load_state(self.data_dir).degraded_occurrences, 1)

    # --- constraint 5: offline-safe / never blocks ------------------------
    def test_write_failure_is_silent(self) -> None:
        set_consent(self.data_dir, True)
        blocker = self.data_dir / "ops"
        # Replace the state directory with a regular file so every write fails.
        consent_path(self.data_dir).unlink()
        blocker.rmdir()
        blocker.write_text("not a directory", encoding="utf-8")
        self.assertFalse(record_command(self.data_dir, "demo"))
        self.assertFalse(record_event(self.data_dir, key="degraded_occurrences"))
        self.assertEqual(build_payload(self.data_dir)["consent"]["enabled"], False,
                         "unreadable state must fail closed")

    def test_transmit_is_an_explicit_noop(self) -> None:
        self.assertEqual(transmit(build_payload(self.data_dir)), TRANSPORT_UNAVAILABLE)
        self.assertFalse(build_payload(self.data_dir)["transport"]["implemented"])

    # --- constraint 6: global kill switch ---------------------------------
    def test_env_zero_overrides_explicit_consent(self) -> None:
        set_consent(self.data_dir, True)
        self.assertTrue(is_enabled(self.data_dir))
        os.environ[TELEMETRY_ENV] = "0"
        self.assertFalse(is_enabled(self.data_dir))
        self.assertFalse(record_command(self.data_dir, "demo"))
        self.assertEqual(load_state(self.data_dir).commands, {})
        payload = build_payload(self.data_dir)
        self.assertFalse(payload["consent"]["enabled"])
        self.assertEqual(payload["consent"]["override"], "env_kill_switch")
        validate_payload(payload)

    # --- lifecycle ---------------------------------------------------------
    def test_enable_disable_round_trip_through_cli(self) -> None:
        rc, out = self._run("telemetry", "--data-dir", str(self.data_dir))
        self.assertEqual(rc, 0)
        self.assertIn("关闭（默认）", out)

        rc, _ = self._run("telemetry", "--data-dir", str(self.data_dir), "--enable")
        self.assertEqual(rc, 0)
        self.assertTrue(is_enabled(self.data_dir))
        # The enable command itself was counted *before* consent existed, so
        # collection really starts only on the next invocation.
        self.assertEqual(load_state(self.data_dir).commands, {})

        rc, out = self._run("telemetry", "--data-dir", str(self.data_dir), "--disable", "--show")
        self.assertEqual(rc, 0)
        self.assertFalse(is_enabled(self.data_dir))
        shown = json.loads(out)
        self.assertFalse(shown["consent"]["enabled"])
        self.assertEqual(shown["aggregate"]["commands"].get("telemetry"), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
