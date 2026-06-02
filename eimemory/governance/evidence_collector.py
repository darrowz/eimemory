from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from eimemory.metadata import business_metadata
from eimemory.models.records import ScopeRef


@dataclass(slots=True)
class Evidence:
    tier: str
    kind: str
    ref: str
    summary: str
    confidence: float = 0.5

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def collect(task: dict[str, Any], *, runtime: Any | None = None, scope: dict[str, Any] | ScopeRef | None = None) -> list[dict[str, Any]]:
    return [item.to_dict() for item in collect_evidence(runtime, task=task, scope=scope)]


def collect_evidence(
    runtime: Any | None,
    *,
    task: dict[str, Any],
    scope: dict[str, Any] | ScopeRef | None = None,
) -> list[Evidence]:
    task_type = str(task.get("task_type") or "")
    if bool(task.get("network")):
        return [Evidence(tier="T6", kind="disabled_network_adapter", ref=task_type, summary="Network collector disabled by source policy.", confidence=0.2)]
    if runtime is None:
        return []
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    if task_type == "local_history_review":
        return _collect_local_history(runtime, scope=scope_ref)
    if task_type == "benchmark_review":
        return _collect_eval_history(runtime, scope=scope_ref)
    if task_type == "repo_scan":
        return _collect_repo_files(runtime, task=task)
    if task_type == "tool_comparison":
        return _collect_tool_inventory(runtime)
    return _collect_local_history(runtime, scope=scope_ref)[:3]


def _collect_local_history(runtime: Any, *, scope: ScopeRef) -> list[Evidence]:
    evidence: list[Evidence] = []
    for record in runtime.store.list_records(kinds=["reflection", "incident", "unknown"], scope=scope, limit=20):
        tier = "T0" if str(business_metadata(record.meta).get("report_type") or "") == "outcome_trace" else "T2"
        evidence.append(Evidence(tier=tier, kind="record", ref=record.record_id, summary=record.summary or record.title, confidence=0.75 if tier == "T0" else 0.6))
    return evidence


def _collect_eval_history(runtime: Any, *, scope: ScopeRef) -> list[Evidence]:
    return [
        Evidence(tier="T1", kind="replay_result", ref=record.record_id, summary=record.summary or record.title, confidence=0.8)
        for record in runtime.store.list_records(kinds=["replay_result"], scope=scope, limit=20)
    ]


def _collect_repo_files(runtime: Any, *, task: dict[str, Any]) -> list[Evidence]:
    root = Path(str(task.get("repo_root") or getattr(runtime.store, "root", Path.cwd()))).resolve()
    files: list[Evidence] = []
    if not root.exists():
        return files
    for path in list(root.rglob("*.py"))[:80]:
        if any(part in {".git", ".venv", "__pycache__", "state"} for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "TODO" in text or "FIXME" in text or "autonomous" in path.name:
            files.append(Evidence(tier="T2", kind="file", ref=str(path), summary=f"Repo evidence from {path.name}", confidence=0.55))
        if len(files) >= 10:
            break
    return files


def _collect_tool_inventory(runtime: Any) -> list[Evidence]:
    root = Path(getattr(runtime.store, "root", Path.cwd()))
    return [
        Evidence(tier="T2", kind="tool_inventory", ref=str(root), summary="Local runtime store and configured eimemory tools are available for offline learning.", confidence=0.55)
    ]
