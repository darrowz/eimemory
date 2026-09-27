from __future__ import annotations

from typing import Any


PRODUCTIVE_MODULES = [
    "memory_retrieval",
    "task_replay",
    "autonomous_patch",
    "safety_gate",
]


def classify_evolution_modules(*, online_evidence: Any) -> dict[str, Any]:
    evidence_items = _normalize_evidence(online_evidence)
    productive = set(PRODUCTIVE_MODULES)
    demote: list[str] = []
    observe: list[str] = []

    for item in evidence_items:
        module = _module_name(item)
        if not module or module in productive:
            continue
        success_count = _int_value(item.get("success_count"), default=0)
        if success_count <= 0:
            _append_unique(demote, module)
        else:
            _append_unique(observe, module)

    return {
        "ok": True,
        "productive_modules": list(PRODUCTIVE_MODULES),
        "keep": list(PRODUCTIVE_MODULES),
        "demote": demote,
        "demoted_modules": list(demote),
        "observe": observe,
        "evidence_count": len(evidence_items),
    }


def _normalize_evidence(online_evidence: Any) -> list[dict[str, Any]]:
    if online_evidence is None:
        return []
    if isinstance(online_evidence, dict):
        nested = online_evidence.get("online_evidence") or online_evidence.get("modules")
        if isinstance(nested, (list, tuple)):
            return _normalize_evidence(nested)
        return [
            {"module": str(module), **(dict(metrics) if isinstance(metrics, dict) else {"success_count": metrics})}
            for module, metrics in online_evidence.items()
        ]
    if isinstance(online_evidence, (list, tuple, set)):
        return [dict(item) for item in online_evidence if isinstance(item, dict)]
    return []


def _module_name(item: dict[str, Any]) -> str:
    for key in ("module", "module_name", "name", "id", "component"):
        value = str(item.get(key) or "").strip()
        if value:
            return value
    return ""


def _append_unique(items: list[str], value: str) -> None:
    if value not in items:
        items.append(value)


def _int_value(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)
