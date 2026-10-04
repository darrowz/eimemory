from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any

from eimemory.adapters.runtime.service import AgentRuntimeMemoryService
from eimemory.api.runtime import Runtime
from eimemory.knowledge.l1_queue import L1QueueStateError


FORBID = ("completed turn", "[paper]", "arxiv", "locomo")
STYLE_TYPES = ("preference", "operator_preference", "user_profile", "instruction", "persona")

def _plane_cases() -> list[dict[str, Any]]:
    from eimemory.identity import operator_display_name

    name = operator_display_name()
    return [
    {
        "id": "style",
        "query": f"{name}沟通风格",
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
        "query": f"{name}和钊哥记忆分开",
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


PLANE_CASES = _plane_cases()


def _queue_state_failure_report(
    error: L1QueueStateError,
    *,
    stats: dict[str, Any],
    completed_report: dict[str, Any] | None = None,
    phase: str = "drain",
) -> dict[str, Any]:
    known = {**(completed_report or {}), **error.context}
    return {
        "ok": False,
        "error": error.code,
        "queue_state": "unavailable",
        "phase": known.get("phase") or phase,
        "retryable": error.retryable,
        "processed": known.get("processed"),
        "failed": known.get("failed"),
        "newly_dead": known.get("newly_dead"),
        "pending": None,
        "dead": None,
        "dead_jobs": None,
        "errors": [*(known.get("errors") or []), error.code][-5:],
        "last_claimed_job_id": known.get("last_claimed_job_id"),
        "handler_completed": known.get("handler_completed"),
        "completion_recorded": known.get("completion_recorded"),
        "handler_error": known.get("handler_error"),
        **stats,
    }


def drain_l1(*, root: str, limit: int = 5) -> dict[str, Any]:
    runtime = Runtime.create(root=root)
    try:
        service = AgentRuntimeMemoryService(runtime)

        stats = {"atoms_written": 0, "zero_write_jobs": 0}

        def _handle(job: dict[str, Any]) -> None:
            written = service._extract_l1_inline(
                user_text=str(job.get("user_text") or ""),
                assistant_text=str(job.get("assistant_text") or ""),
                turn_text=str(job.get("turn_text") or ""),
                episode_id=str(job.get("episode_id") or ""),
                channel_id=str(job.get("channel_id") or "hermes"),
                channel_scope=dict(job.get("channel_scope") or {}),
                session_id=str(job.get("session_id") or ""),
                turn_id=str(job.get("turn_id") or ""),
            )
            stats["atoms_written"] += len(written)
            if not written:
                stats["zero_write_jobs"] += 1

        report: dict[str, Any] = {}
        phase = "drain"
        try:
            queue = service._l1_queue()
            report = queue.drain_report(_handle, limit=limit)
            report.update(stats)
            report["ok"] = (
                int(report.get("failed") or 0) == 0
                and int(report.get("newly_dead") or 0) == 0
            )
            phase = "recent_dead"
            report["dead_jobs"] = queue.recent_dead(limit=5)
        except L1QueueStateError as exc:
            report = _queue_state_failure_report(
                exc, stats=stats, completed_report=report, phase=phase
            )
        if report.get("queue_state") == "unavailable":
            try:
                _append_worker_log(root, report)
            except (OSError, UnicodeError) as exc:
                # Logging must not hide the queue failure or prior handler error.
                report["log_error"] = type(exc).__name__
        else:
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
        "atoms_written": report.get("atoms_written"),
        "zero_write_jobs": report.get("zero_write_jobs"),
        "errors": report.get("errors") or [],
        "dead_jobs": report.get("dead_jobs"),
        "error": report.get("error"),
        "queue_state": report.get("queue_state"),
        "phase": report.get("phase"),
        "retryable": report.get("retryable"),
        "last_claimed_job_id": report.get("last_claimed_job_id"),
        "handler_completed": report.get("handler_completed"),
        "completion_recorded": report.get("completion_recorded"),
        "handler_error": report.get("handler_error"),
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, ensure_ascii=False) + "\n")


def main() -> int:
    action = (os.environ.get("EIMEMORY_L1_WORKER_ACTION") or "drain").strip()
    if action not in {"drain", "eval", "repair"}:
        print(json.dumps({"ok": False, "error": "invalid_worker_action", "action": action}, ensure_ascii=False))
        return 2
    root = (os.environ.get("EIMEMORY_ROOT") or "").strip()
    if not root:
        print(json.dumps({"ok": False, "error": "EIMEMORY_ROOT is required"}, ensure_ascii=False))
        return 1
    if action in {"eval", "repair"}:
        runtime = Runtime.create(root=root)
        try:
            service = AgentRuntimeMemoryService(runtime)
            scope = {
                "tenant_id": os.environ.get("EIMEMORY_DEPLOY_SCOPE_TENANT") or "default",
                "agent_id": os.environ.get("EIMEMORY_DEPLOY_SCOPE_AGENT") or "hongtu",
                "workspace_id": "embodied",
                "user_id": os.environ.get("EIMEMORY_DEPLOY_SCOPE_USER")
                or os.environ.get("EIMEMORY_USER_ID")
                or os.environ.get("USER")
                or "operator",
            }
            if action == "eval":
                report = evaluate_plane(service, scope=scope)
            else:
                continuation = {}
                if "EIMEMORY_L1_REPAIR_CURSOR" in os.environ:
                    continuation["cursor"] = os.environ["EIMEMORY_L1_REPAIR_CURSOR"]
                if "EIMEMORY_L1_REPAIR_SCAN_LIMIT" in os.environ:
                    continuation["scan_limit"] = int(os.environ["EIMEMORY_L1_REPAIR_SCAN_LIMIT"])
                report = service.backfill_l1(
                    channel=os.environ.get("EIMEMORY_L1_REPAIR_CHANNEL") or "hermes",
                    scope=scope,
                    limit=int(os.environ.get("EIMEMORY_L1_REPAIR_LIMIT") or 200),
                    **continuation,
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
