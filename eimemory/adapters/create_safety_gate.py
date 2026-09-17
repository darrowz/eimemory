"""Shared create_safety gate for host adapters (BC-02 / BS-01).

Hosts must not auto-create active memory when recent recall fusion already
classified the same scope/query as exists / probable / unavailable.
"""

from __future__ import annotations

from typing import Any, Mapping

BLOCK_CREATE = frozenset({"exists", "unavailable"})
REQUIRE_FORCE = frozenset({"probable"})
KNOWN_SAFETY = frozenset({"exists", "probable", "unknown", "unavailable"})


def extract_create_safety(source: Any) -> str:
    """Pull create_safety from fusion explanation, recall bundle, or event dict."""
    if source is None:
        return ""
    if isinstance(source, str):
        value = source.strip().lower()
        return value if value in KNOWN_SAFETY else ""
    if not isinstance(source, Mapping):
        explanation = getattr(source, "explanation", None)
        if isinstance(explanation, Mapping):
            return extract_create_safety(explanation)
        return ""
    direct = str(source.get("create_safety") or "").strip().lower()
    if direct in KNOWN_SAFETY:
        return direct
    fusion = source.get("fusion")
    if isinstance(fusion, Mapping):
        nested = str(fusion.get("create_safety") or "").strip().lower()
        if nested in KNOWN_SAFETY:
            return nested
    explanation = source.get("explanation")
    if isinstance(explanation, Mapping):
        return extract_create_safety(explanation)
    task_context = source.get("task_context")
    if isinstance(task_context, Mapping):
        return extract_create_safety(task_context)
    recall = source.get("recall") or source.get("last_recall") or source.get("bundle")
    if recall is not None and recall is not source:
        return extract_create_safety(recall)
    return ""


def decide_create_from_safety(
    create_safety: str,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Return a host create decision for a create_safety label."""
    safety = str(create_safety or "").strip().lower() or "unknown"
    if safety in BLOCK_CREATE:
        return {
            "allow": False,
            "action": "skip",
            "create_safety": safety,
            "reason": f"create_safety_{safety}",
            "force_required": False,
        }
    if safety in REQUIRE_FORCE and not force:
        return {
            "allow": False,
            "action": "require_force",
            "create_safety": safety,
            "reason": "create_safety_probable_requires_force",
            "force_required": True,
        }
    return {
        "allow": True,
        "action": "create",
        "create_safety": safety,
        "reason": "",
        "force_required": False,
    }


def resolve_create_safety_for_text(
    runtime: Any,
    *,
    text: str,
    scope: Mapping[str, Any] | None = None,
    query: str = "",
    fusion_hint: Any = None,
    limit: int = 4,
) -> dict[str, Any]:
    """Prefer an explicit fusion hint; otherwise run a lightweight recall."""
    from_hint = extract_create_safety(fusion_hint)
    if from_hint:
        return {
            "create_safety": from_hint,
            "source": "fusion_hint",
            "query": str(query or text or ""),
            "ok": True,
        }
    recall_query = str(query or text or "").strip()
    if not recall_query or runtime is None:
        return {
            "create_safety": "unknown",
            "source": "unavailable",
            "query": recall_query,
            "ok": False,
        }
    memory = getattr(runtime, "memory", None)
    recall = getattr(memory, "recall", None) if memory is not None else None
    if not callable(recall):
        return {
            "create_safety": "unknown",
            "source": "recall_unavailable",
            "query": recall_query,
            "ok": False,
        }
    try:
        bundle = recall(
            query=recall_query,
            scope=dict(scope or {}),
            limit=max(1, int(limit)),
            task_context={"create_safety_probe": True},
        )
    except Exception as exc:  # noqa: BLE001 - probe failure is not evidence of absence
        # Only fusion-reported unavailable (BC-06) blocks create; probe errors stay unknown.
        return {
            "create_safety": "unknown",
            "source": "recall_error",
            "query": recall_query,
            "ok": False,
            "error": type(exc).__name__,
        }
    safety = extract_create_safety(bundle) or "unknown"
    return {
        "create_safety": safety,
        "source": "recall",
        "query": recall_query,
        "ok": True,
        "bundle_explanation": getattr(bundle, "explanation", None)
        if not isinstance(bundle, Mapping)
        else bundle.get("explanation"),
    }


def gate_host_create(
    runtime: Any,
    *,
    text: str,
    scope: Mapping[str, Any] | None = None,
    force: bool = False,
    query: str = "",
    fusion_hint: Any = None,
    skip_probe: bool = False,
) -> dict[str, Any]:
    """Gate a host create/ingest path using create_safety."""
    if skip_probe and fusion_hint is None:
        decision = decide_create_from_safety("unknown", force=force)
        decision["probe"] = {"create_safety": "unknown", "source": "skipped", "ok": True}
        return decision
    probe = resolve_create_safety_for_text(
        runtime,
        text=text,
        scope=scope,
        query=query,
        fusion_hint=fusion_hint,
    )
    decision = decide_create_from_safety(str(probe.get("create_safety") or "unknown"), force=force)
    decision["probe"] = probe
    return decision
