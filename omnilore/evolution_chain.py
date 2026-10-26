"""
Causal Evolution Chain & MRP Knowledge Compilation Module
=========================================================
Tracks knowledge version evolution as a Causal Directed Acyclic Graph (DAG)
rather than flat overwrite logs, and provides structured Proposed Change Plans
before merging incoming knowledge updates.
Inspired by arkon's MRP knowledge compilation and Hy-Memory's causal diff chain.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import difflib
from typing import Any, Dict, List, Optional, Set


@dataclass
class VersionNode:
    """Represents a discrete immutable version node in a knowledge series."""
    version_key: str
    version_label: str
    doc_id: str
    file_hash: str
    committed_at: str
    reverts_version_key: Optional[str] = None
    supersedes_version_keys: List[str] = field(default_factory=list)
    diff_summary: Dict[str, int] = field(default_factory=dict)
    summary_note: str = ""


class CausalEvolutionDAG:
    """Manages the causal lineage DAG of a document series."""

    def __init__(self, series_id: str):
        self.series_id = series_id
        self._nodes: Dict[str, VersionNode] = {}
        self._root_key: Optional[str] = None
        self._head_key: Optional[str] = None

    def add_version(
        self,
        version_key: str,
        version_label: str,
        doc_id: str,
        file_hash: str,
        committed_at: str,
        reverts_version_key: Optional[str] = None,
        supersedes_version_keys: Optional[List[str]] = None,
        diff_summary: Optional[Dict[str, int]] = None,
        summary_note: str = "",
    ) -> VersionNode:
        """Register a new immutable version node in the DAG."""
        node = VersionNode(
            version_key=version_key,
            version_label=version_label,
            doc_id=doc_id,
            file_hash=file_hash,
            committed_at=committed_at,
            reverts_version_key=reverts_version_key,
            supersedes_version_keys=supersedes_version_keys or [],
            diff_summary=diff_summary or {"added": 0, "deleted": 0},
            summary_note=summary_note,
        )
        self._nodes[version_key] = node

        if self._root_key is None:
            self._root_key = version_key
        self._head_key = version_key
        return node

    def get_lineage(self, version_key: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return the causal lineage from root to specified head (or latest head)."""
        target = version_key or self._head_key
        if not target or target not in self._nodes:
            return []

        # Topological linear presentation of registered versions
        return [asdict(n) for n in self._nodes.values()]

    def render_ascii_lineage(self) -> str:
        """Render a readable textual summary of the evolution trace."""
        if not self._nodes:
            return f"(Series {self.series_id}: Empty)"

        lines = [f"=== 知识演化链谱: {self.series_id} ==="]
        for vkey, node in self._nodes.items():
            flags = []
            if node.reverts_version_key:
                flags.append(f"REVERT({node.reverts_version_key})")
            if node.supersedes_version_keys:
                flags.append(f"SUPERSEDES({','.join(node.supersedes_version_keys)})")

            diff_str = f"+{node.diff_summary.get('added', 0)} / -{node.diff_summary.get('deleted', 0)}"
            flag_str = f" [{'; '.join(flags)}]" if flags else ""
            lines.append(
                f"  [{node.version_label}] ({node.version_key[:8]}) {diff_str} - {node.summary_note}{flag_str}"
            )
        return "\n".join(lines)


@dataclass
class ProposedChangePlan:
    """Pre-compilation inspection plan for proposed knowledge changes."""
    has_changes: bool
    added_lines: int
    deleted_lines: int
    impacted_headings: List[str]
    unified_diff: str
    risk_level: str  # "LOW" | "MEDIUM" | "HIGH"
    recommendation: str


def compile_proposed_change_plan(
    current_content: str,
    incoming_content: str,
) -> ProposedChangePlan:
    """
    Simulates arkon's MRP knowledge compilation plan.
    Compares existing text against proposed incoming text, calculating diff,
    impacted section headings, and safe merge recommendation.
    """
    current_lines = current_content.splitlines(keepends=True)
    incoming_lines = incoming_content.splitlines(keepends=True)

    diff = list(difflib.unified_diff(current_lines, incoming_lines, fromfile="current", tofile="proposed"))
    if not diff:
        return ProposedChangePlan(
            has_changes=False,
            added_lines=0,
            deleted_lines=0,
            impacted_headings=[],
            unified_diff="",
            risk_level="LOW",
            recommendation="内容完全一致，无需重复落库更新。",
        )

    added = sum(1 for line in diff if line.startswith("+") and not line.startswith("+++"))
    deleted = sum(1 for line in diff if line.startswith("-") and not line.startswith("---"))

    # Identify impacted headings (both heading line changes and content modified under headings)
    impacted_headings: Set[str] = set()

    # Traverse diff lines tracking current heading context
    active_heading = "默认小节"
    for line in diff:
        raw_text = line[1:].strip()
        if raw_text.startswith("#"):
            active_heading = raw_text
            if line.startswith(("+", "-")):
                impacted_headings.add(active_heading)
        elif line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
            if active_heading != "默认小节":
                impacted_headings.add(active_heading)

    # Risk analysis
    if deleted > added * 2 and deleted > 20:
        risk_level = "HIGH"
        rec = "检测到大量章节或语句删除，疑似重大知识撤销，建议人工确认后再合并。"
    elif len(impacted_headings) > 2:
        risk_level = "MEDIUM"
        rec = "跨多个核心章节结构性变动，合并后建议触发引用一致性复检。"
    else:
        risk_level = "LOW"
        rec = "常规增量补充或小幅修订，满足安全合并条件。"

    return ProposedChangePlan(
        has_changes=True,
        added_lines=added,
        deleted_lines=deleted,
        impacted_headings=sorted(list(impacted_headings)),
        unified_diff="".join(diff[:50]),  # Preview first 50 lines
        risk_level=risk_level,
        recommendation=rec,
    )
