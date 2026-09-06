from __future__ import annotations

import json
import os
from typing import Any

from eimemory.adapters.runtime.service import AgentRuntimeMemoryService
from eimemory.api.runtime import Runtime


PLANE_CASES = [
    {
        "id": "style",
        "query": "鸿哥沟通风格",
        "task_type": "operator.preference",
        "require_loadout": True,
        "forbid_title_substrings": ("completed turn", "[paper]", "arxiv", "locomo"),
        "require_memory_types": ("preference", "operator_preference", "user_profile", "instruction", "persona"),
    },
    {
        "id": "isolation",
        "query": "鸿哥和钊哥记忆分开",
        "task_type": "operator.preference",
        "require_loadout": True,
        "forbid_title_substrings": ("completed turn", "[paper]"),
        "require_any_title_substrings": ("隔离", "钊哥", "沟通风格", "极简"),
    },
]


def drain_l1(*, root: str, limit: int = 5) -> dict[str, Any]:
    runtime = Runtime.create(root=root)
    try:
        service = AgentRuntimeMemoryService(runtime)
        processed = service.drain_l1_queue(limit=limit)
        pending = service._l1_queue().pending_count()
        return {"ok": True, "processed": processed, "pending": pending}
    finally:
        runtime.close()


def evaluate_plane(service: AgentRuntimeMemoryService, *, scope: dict[str, str]) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    failed = 0
    for case in PLANE_CASES:
        recalled = service.prefetch(
            channel="hermes",
            scope=scope,
            query=str(case["query"]),
            task_type=str(case["task_type"]),
            limit=5,
        )
        bundle = recalled.get("bundle") or {}
        items = list(bundle.get("items") or [])
        titles = [str(item.get("title") or "") for item in items]
        types = [str(item.get("memory_type") or "") for item in items]
        reasons: list[str] = []
        if case.get("require_loadout") and bundle.get("loadout") != "l3_persona+l1_query":
            reasons.append("missing_loadout")
        for needle in case.get("forbid_title_substrings") or ():
            if any(needle.lower() in title.lower() for title in titles):
                reasons.append(f"forbidden:{needle}")
        required_types = tuple(case.get("require_memory_types") or ())
        if required_types and not any(memory_type in required_types for memory_type in types):
            reasons.append("missing_required_type")
        required_titles = tuple(case.get("require_any_title_substrings") or ())
        if required_titles and items and not any(
            any(needle in title for needle in required_titles) for title in titles
        ):
            reasons.append("missing_required_title")
        ok = not reasons
        if not ok:
            failed += 1
        results.append({"id": case["id"], "ok": ok, "reasons": reasons, "titles": titles[:5]})
    return {"ok": failed == 0, "failed": failed, "cases": results}


def main() -> int:
    action = (os.environ.get("EIMEMORY_L1_WORKER_ACTION") or "drain").strip()
    root = (os.environ.get("EIMEMORY_ROOT") or "").strip()
    if not root:
        print(json.dumps({"ok": False, "error": "EIMEMORY_ROOT is required"}, ensure_ascii=False))
        return 1
    if action == "eval":
        runtime = Runtime.create(root=root)
        try:
            report = evaluate_plane(
                AgentRuntimeMemoryService(runtime),
                scope={
                    "tenant_id": os.environ.get("EIMEMORY_DEPLOY_SCOPE_TENANT") or "default",
                    "agent_id": os.environ.get("EIMEMORY_DEPLOY_SCOPE_AGENT") or "hongtu",
                    "workspace_id": "embodied",
                    "user_id": os.environ.get("EIMEMORY_DEPLOY_SCOPE_USER") or "darrow",
                },
            )
        finally:
            runtime.close()
        print(json.dumps(report, ensure_ascii=False))
        return 0 if report.get("ok") else 1
    report = drain_l1(root=root, limit=int(os.environ.get("EIMEMORY_L1_DRAIN_LIMIT") or 5))
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
