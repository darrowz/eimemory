from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from itertools import islice
from typing import Any
from urllib.parse import urlparse

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
        return _collect_network_evidence(runtime, task=task, scope=scope)
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
    for path in islice(root.rglob("*.py"), 80):
        if any(part in {".git", ".venv", "__pycache__", "state"} for part in path.parts):
            continue
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as handle:
                text = handle.read(1_048_576)
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


def _collect_network_evidence(
    runtime: Any | None,
    *,
    task: dict[str, Any],
    scope: dict[str, Any] | ScopeRef | None,
) -> list[Evidence]:
    task_type = str(task.get("task_type") or "network")
    if runtime is None:
        return [Evidence(tier="T6", kind="network_collector_unavailable", ref=task_type, summary="Network collector requires a runtime.", confidence=0.1)]
    scout = getattr(runtime, "scout_web_learning", None)
    if not callable(scout):
        return [Evidence(tier="T6", kind="network_collector_unavailable", ref=task_type, summary="Runtime has no web learning scout.", confidence=0.1)]

    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    urls = _research_urls(runtime, task)
    timeout_seconds = max(1, min(30, int(task.get("max_seconds") or 8)))
    try:
        report = scout(scope=asdict(scope_ref), urls=urls, timeout_seconds=timeout_seconds)
    except Exception as exc:
        return [Evidence(tier="T6", kind="web_learning_scout_error", ref=task_type, summary=f"Web scout failed: {type(exc).__name__}: {exc}", confidence=0.1)]

    evidence: list[Evidence] = []
    for hypothesis in list(report.get("hypotheses") or []):
        if not isinstance(hypothesis, dict):
            continue
        policy = hypothesis.get("candidate_policy") if isinstance(hypothesis.get("candidate_policy"), dict) else {}
        summary = _first_text(policy.get("policy_update"), policy.get("title"), hypothesis.get("source_url"), "Web scout hypothesis")
        evidence.append(
            Evidence(
                tier="T3",
                kind="web_learning_scout",
                ref=str(hypothesis.get("source_url") or report.get("reflection_record_id") or task_type),
                summary=summary,
                confidence=0.68,
            )
        )

    for error in list(report.get("errors") or []):
        if isinstance(error, dict):
            evidence.append(
                Evidence(
                    tier="T6",
                    kind="web_learning_scout_error",
                    ref=str(error.get("url") or task_type),
                    summary=str(error.get("detail") or error.get("error") or "Web scout error."),
                    confidence=0.1,
                )
            )
    if evidence:
        return evidence
    return [Evidence(tier="T6", kind="web_learning_scout_empty", ref=task_type, summary="Web scout returned no usable hypotheses.", confidence=0.2)]


def _research_urls(runtime: Any, task: dict[str, Any]) -> list[str]:
    urls = _string_list(task.get("urls") or task.get("source_urls"))
    if urls:
        return urls[:5]
    sources = getattr(runtime, "sources", None)
    list_sources = getattr(sources, "list_sources", None)
    if not callable(list_sources):
        return []
    candidates: list[str] = []
    try:
        source_entries = list_sources(enabled=True)
    except Exception:
        return []
    for entry in source_entries:
        uri = str(getattr(entry, "uri", "") or "").strip()
        if _is_http_url(uri):
            candidates.append(uri)
        if len(candidates) >= 5:
            break
    return candidates


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = []
    deduped: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        deduped.append(text)
    return deduped


def _is_http_url(value: str) -> bool:
    parsed = urlparse(str(value or "").strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _first_text(*values: Any) -> str:
    for value in values:
        text = " ".join(str(value or "").split())
        if text:
            return text
    return ""
