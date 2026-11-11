"""Opt-in anonymous telemetry (decision D3).

This module implements the *collection and self-audit* half of D3 only.  The
constraints that shaped it are non-negotiable (see ``docs/DECISIONS.md`` §3):

1. Strict opt-in — nothing is collected until the user runs an explicit
   ``omnilore telemetry --enable``.
2. Off by default — a fresh install, a fresh ``bootstrap`` and ``demo`` all run
   without writing a single telemetry byte.
3. Minimal fields — anonymous aggregate counters only.  Document content,
   titles, paths, queries, ``space_id``, user ids, tokens (even hashed), IPs,
   hostnames and any other PII are forbidden.
4. ``--show`` self-audit — the complete raw payload is printable so a user can
   verify "that is all".
5. Offline-safe — any failure (no permission, no disk, no network) is silently
   abandoned; telemetry must never block or confuse the main flow.
6. Global kill switch — ``OMNILORE_TELEMETRY=0`` wins over everything.

**Honest boundary:** this release deliberately implements **no upload
transport**.  There is no endpoint, no HTTP client and no background process.
``build_payload()`` returns exactly the payload that an eventual transport
would be allowed to send; ``transport.implemented`` is ``false``.  Calling
``transmit()`` is a documented no-op that returns ``TRANSPORT_UNAVAILABLE``.
This keeps the privacy claim auditable instead of implying a data flow that
does not exist.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .textio import read_text_canonical, write_text_canonical
from .version import __version_display__

TELEMETRY_ENV = "OMNILORE_TELEMETRY"
SCHEMA_VERSION = 1
TRANSPORT_UNAVAILABLE = "TRANSPORT_UNAVAILABLE"

# Only these sub-command *names* may ever be counted.  An unknown command is
# folded into "other" rather than recorded verbatim, so a future command that
# happens to embed a path or an id can never leak into the payload.
ALLOWED_COMMANDS = frozenset({
    "bootstrap", "demo", "version", "audit-bundle", "plan", "history",
    "verify-citation", "telemetry",
})

# Recursive key-name denylist.  Anything matching is rejected by
# ``validate_payload`` instead of being silently stripped — a rejected payload
# is a visible bug, a stripped field is a hidden one.
FORBIDDEN_KEY_FRAGMENTS = (
    "doc_id", "title", "path", "file_path", "filename", "query", "space_id",
    "category", "user", "token", "ip_addr", "client_ip", "hostname", "email",
    "content", "raw_text", "quote", "chunk", "asset", "secret", "api_key",
    "sha256", "author", "doc_name",
)

# Counters that D3 explicitly permits.  Anything else is refused by
# ``increment`` so a future contributor cannot widen the schema by accident.
ALLOWED_COUNTER_KEYS = frozenset({"degraded_occurrences", "retrieval_modes", "commands"})


@dataclass
class TelemetryState:
    """Locally persisted, anonymous-only state."""

    enabled: bool = False
    commands: Dict[str, int] = field(default_factory=dict)
    degraded_occurrences: int = 0
    retrieval_modes: Dict[str, int] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "enabled": self.enabled,
                "commands": self.commands,
                "degraded_occurrences": self.degraded_occurrences,
                "retrieval_modes": self.retrieval_modes,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n"

    @classmethod
    def from_json(cls, raw: str) -> "TelemetryState":
        obj = json.loads(raw)
        if not isinstance(obj, dict):
            raise ValueError("telemetry state must be a JSON object")
        return cls(
            enabled=bool(obj.get("enabled", False)),
            commands=_sanitise_counter_map(obj.get("commands")),
            degraded_occurrences=max(0, int(obj.get("degraded_occurrences", 0) or 0)),
            retrieval_modes=_sanitise_counter_map(obj.get("retrieval_modes")),
        )


def _sanitise_counter_map(value: Any) -> Dict[str, int]:
    """Keep only short, non-numeric, identifier-like keys and non-negative ints."""
    if not isinstance(value, dict):
        return {}
    clean: Dict[str, int] = {}
    for key, count in value.items():
        if not isinstance(key, str) or not key or len(key) > 40:
            continue
        if any(ch.isspace() for ch in key) or "/" in key or "\\" in key or "@" in key:
            continue
        try:
            clean[key] = max(0, int(count))
        except (TypeError, ValueError):
            continue
    return clean


def consent_path(data_dir: Path) -> Path:
    """Return the consent/state file location (under the data dir, not the repo)."""
    return Path(data_dir) / "ops" / "telemetry.json"


def _env_kill_switch() -> bool:
    return os.environ.get(TELEMETRY_ENV, "").strip() == "0"


def is_enabled(data_dir: Path) -> bool:
    """Whether telemetry is allowed to collect. Never raises, never writes."""
    if _env_kill_switch():
        return False
    path = consent_path(data_dir)
    try:
        if not path.exists():
            return False
        return TelemetryState.from_json(read_text_canonical(path)).enabled
    except Exception:
        # Corrupt state must fail closed, not open.
        return False


def load_state(data_dir: Path) -> TelemetryState:
    """Read state for display purposes. A missing/corrupt file means default-off."""
    path = consent_path(data_dir)
    try:
        if path.exists():
            return TelemetryState.from_json(read_text_canonical(path))
    except Exception:
        pass
    return TelemetryState()


def set_consent(data_dir: Path, enabled: bool) -> TelemetryState:
    """Persist explicit consent. This is the *only* way collection can turn on."""
    state = load_state(data_dir)
    state.enabled = bool(enabled)
    path = consent_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_canonical(path, state.to_json())
    return state


def record_command(data_dir: Path, command: str) -> bool:
    """Count one sub-command invocation, but only when explicitly opted in.

    Returns ``True`` when a counter was written, ``False`` otherwise.  Every
    failure is swallowed: D3 requires telemetry to never block the main flow.
    """
    try:
        if _env_kill_switch() or not is_enabled(data_dir):
            return False
        state = load_state(data_dir)
        safe_name = command if command in ALLOWED_COMMANDS else "other"
        state.commands[safe_name] = int(state.commands.get(safe_name, 0)) + 1
        write_text_canonical(consent_path(data_dir), state.to_json())
        return True
    except Exception:
        return False


def record_event(data_dir: Path, *, key: str, bucket: str = "count") -> bool:
    """Increment one aggregate counter from a strictly closed vocabulary.

    ``key`` must be one of ``ALLOWED_COUNTER_KEYS``; ``bucket`` is only used for
    ``retrieval_modes`` and is itself sanitised.  Never raises.
    """
    try:
        if key not in ALLOWED_COUNTER_KEYS:
            return False
        if _env_kill_switch() or not is_enabled(data_dir):
            return False
        state = load_state(data_dir)
        if key == "degraded_occurrences":
            state.degraded_occurrences += 1
        elif key == "retrieval_modes":
            clean = _sanitise_counter_map({bucket: 1})
            if not clean:
                return False
            name = next(iter(clean))
            state.retrieval_modes[name] = state.retrieval_modes.get(name, 0) + 1
        else:  # commands is managed by record_command only
            return False
        write_text_canonical(consent_path(data_dir), state.to_json())
        return True
    except Exception:
        return False


def validate_payload(payload: Mapping[str, Any]) -> None:
    """Raise ``ValueError`` if a payload contains a forbidden key or a scalar
    that looks like a path, id, query or other PII.

    This is the executable form of D3 constraint 3, and it is deliberately
    *fail loud*: ``--show`` and the test suite call it before showing anything.
    """
    def walk(node: Any, trail: str) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                if not isinstance(key, str):
                    raise ValueError(f"non-string payload key at {trail}")
                low = key.lower()
                for fragment in FORBIDDEN_KEY_FRAGMENTS:
                    if fragment in low:
                        raise ValueError(f"forbidden payload key {key!r} at {trail}")
                walk(value, f"{trail}.{key}")
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item, trail)
        elif isinstance(node, str):
            # Free-form strings are only allowed for a small, fixed vocabulary.
            if any(ch in node for ch in ("/", "\\", "@", ":", "?")) or len(node) > 80:
                raise ValueError(f"free-form string looks like PII at {trail}: {node!r}")
        elif isinstance(node, bool) or node is None or isinstance(node, int):
            return
        else:
            raise ValueError(f"unsupported payload value type at {trail}: {type(node).__name__}")

    walk(payload, "$")


def build_payload(data_dir: Path) -> Dict[str, Any]:
    """Return the complete raw payload an eventual transport may send.

    There is no hidden field: whatever ``--show`` prints is the whole thing.
    ``validate_payload`` is applied so that a future schema widening cannot
    quietly smuggle in a forbidden field.
    """
    state = load_state(data_dir)
    enabled = (not _env_kill_switch()) and state.enabled
    payload: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "release": __version_display__,
        "consent": {
            "enabled": enabled,
            "mode": "opt_in" if enabled else "off",
            "override": "env_kill_switch" if _env_kill_switch() else None,
        },
        "aggregate": {
            "commands": dict(state.commands),
            "degraded_occurrences": state.degraded_occurrences,
            "retrieval_modes": dict(state.retrieval_modes),
        },
        "transport": {
            "implemented": False,
            "endpoint": None,
            "note": TRANSPORT_UNAVAILABLE,
        },
    }
    validate_payload(payload)
    return payload


def transmit(payload: Mapping[str, Any], *, _endpoint: Optional[str] = None) -> str:
    """Documented no-op.  This release has no upload transport (see module docstring).

    Kept as a function (rather than omitted) so the privacy boundary has one
    explicit, testable place: adding a network call here would immediately make
    the ``transport.implemented`` claim a lie and break its guard.
    """
    del payload, _endpoint
    return TRANSPORT_UNAVAILABLE
