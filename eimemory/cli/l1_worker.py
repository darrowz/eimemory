from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any

from eimemory.adapters.runtime.service import AgentRuntimeMemoryService
from eimemory.api.runtime import Runtime


FORBID = ("completed turn", "[paper]", "arxiv", "locomo")
STYLE_TYPES = ("preference", "operator_preference", "user_profile", "instruction", "persona")

PLANE_CASES = [
    {
        "id": "style",
        "query": "鸿哥沟通风格",
        "task_type": "operator.preference",
        "require_memory_types": STYLE_TYPES,
    },
    {
        "id": "style_paraphrase_brief",
        "query": "别废话，先给结论",
        "task_type": "operator.preference",
        "require_any": ("结论", "极简", "废话", "沟通风格", "少解释"),
    },
    {
        "id": "style_paraphrase_how",
        "query": "以后怎么回答",
        "task_type": "operator.preference",
        "require_any": ("结论", "极简", "沟通风格", "少解释", "基准"),
    },
    {
        "id": "isolation",
        "query": "鸿哥和钊哥记忆分开",
        "task_type": "operator.preference",
        "require_any": ("隔离", "钊哥", "分开", "串号", "两个号"),
    },
    {
        "id": "isolation_paraphrase",
        "query": "两个号别混着说",
        "task_type": "operator.preference",
        "require_any": ("隔离", "钊哥", "分开", "串号", "两个号", "混"),
    },
]


def drain_l1(*, root: str, limit: int = 5) -> dict[str, Any]:
    runtime = Runtime.create(root=root)
    try:
        service = AgentRuntimeMemoryService(runtime)

        def _handle(job: dict[str, Any]) -> None:
            service._extract_l1_inline(
                user_text=str(job.get("user_text") or ""),
                assistant_text=str(job.get("assistant_text") or ""),
                turn_text=str(job.get("turn_text") or ""),
                episode_id=str(job.get("episode_id") or ""),
                channel_id=str(job.get("channel_id") or "hermes"),
                channel_scope=dict(job.get("channel_scope") or {}),
                session_id=str(job.get("session_id") or ""),
                turn_id=str(job.get("turn_id") or ""),
            )

        report = service._l1_queue().drain_report(_handle, limit=limit)
        report["ok"] = int(report.get("newly_dead") or 0) == 0
        report["dead_jobs"] = service._l1_queue().recent_dead(limit=5)
        _append_worker_log(root, report)
        return report
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
            task_type=str(case.get("task_type") or "operator.preference"),
            limit=5,
        )
        bundle = recalled.get("bundle") or {}
        items = list(bundle.get("items") or []) + list(bundle.get("persona") or [])
        blob = " ".join(
            f"{item.get('title') or ''} {item.get('summary') or ''}" for item in items
        )
        titles = [str(item.get("title") or "") for item in items]
        types = [str(item.get("memory_type") or "") for item in items]
        reasons: list[str] = []
        if bundle.get("loadout") != "l3_persona+l1_query":
            reasons.append("missing_loadout")
        for needle in FORBID:
            if needle.lower() in blob.lower():
                reasons.append(f"forbidden:{needle}")
        required_types = tuple(case.get("require_memory_types") or ())
        if required_types and not any(memory_type in required_types for memory_type in types):
            reasons.append("missing_required_type")
        required_any = tuple(case.get("require_any") or ())
        if required_any and not any(needle in blob for needle in required_any):
            reasons.append("missing_required_signal")
        ok = not reasons
        if not ok:
            failed += 1
        results.append({"id": case["id"], "ok": ok, "reasons": reasons, "titles": titles[:5]})
    return {"ok": failed == 0, "failed": failed, "cases": results}


def _append_worker_log(root: str, report: dict[str, Any]) -> None:
    path = Path(root) / "logs" / "l1-extract.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    line = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "ok": report.get("ok"),
        "processed": report.get("processed"),
        "failed": report.get("failed"),
        "newly_dead": report.get("newly_dead"),
        "pending": report.get("pending"),
        "dead": report.get("dead"),
        "errors": report.get("errors") or [],
        "dead_jobs": report.get("dead_jobs") or [],
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, ensure_ascii=False) + "\n")


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
    return 0 if report.get("ok") is not False else 1


if __name__ == "__main__":
    raise SystemExit(main())
