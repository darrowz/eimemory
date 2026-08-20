from __future__ import annotations

from hashlib import sha256
from typing import Any, Mapping

from eimemory.adapters.openclaw.hooks import OpenClawMemoryHooks
from eimemory.api.runtime import Runtime
from eimemory.models.records import RecordEnvelope, ScopeRef


def run_openclaw_e2e_check(
    runtime: Runtime,
    *,
    scope: dict[str, Any],
    query: str = "eimemory openclaw e2e",
    capability_advertisement: Mapping[str, Any] | None = None,
    capability_outcome: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the operator-only OpenClaw memory lifecycle diagnostic.

    This intentionally writes an isolated marker record and completes a
    synthetic terminal event.  It is therefore an operator/CI diagnostic, not
    a model-facing OpenClaw tool.
    """
    hooks = OpenClawMemoryHooks(runtime)
    normalized_query = str(query or "eimemory openclaw e2e").strip() or "eimemory openclaw e2e"
    event_scope = dict(scope or {})
    session_id = f"eimemory-e2e-{sha256((normalized_query + repr(sorted(event_scope.items()))).encode('utf-8')).hexdigest()[:12]}"
    event_base = {
        **event_scope,
        "session_id": session_id,
    }
    canonical_scope = hooks._scope_from_event(event_base)
    marker = f"{normalized_query} marker {session_id}"
    stored = runtime.memory.ingest(
        text=f"EIMemory OpenClaw E2E test memory: {marker}",
        memory_type="preference",
        title="EIMemory OpenClaw E2E marker",
        scope=canonical_scope,
        source="openclaw.memory_e2e_check",
        force_capture=True,
    )
    before = hooks.before_prompt_build(
        {
            **event_base,
            "query": normalized_query,
            "raw_query": normalized_query,
            "task_context": {
                "task_type": "eimemory.openclaw_e2e",
                "candidate_limit": 24,
            },
        }
    )
    bundle = dict(before.get("memory_bundle") or {})
    items = [dict(item) for item in list(bundle.get("items") or []) if isinstance(item, dict)]
    injection_plan = dict(before.get("injection_plan") or {})
    injection_composition = dict(injection_plan.get("lane_composition") or {})
    recall_hit = any(str(item.get("record_id") or "") == stored.record_id for item in items)
    audit = _latest_recall_audit(runtime, session_id=session_id, scope=canonical_scope)
    terminal = hooks.on_task_end(
        {
            **event_base,
            "query": normalized_query,
            "raw_query": normalized_query,
            "task_context": dict(before.get("task_context") or {}),
            "user_messages": [normalized_query],
            "assistant_messages": [{"role": "assistant", "content": f"E2E check observed {marker}"}],
            "outcome": {
                "success": True,
                "outcome": "good",
                "notes": "OpenClaw memory E2E check passed",
                "verification": "stored marker was available to prompt recall",
            },
        }
    )
    event = dict(terminal.get("event") or {})
    outcome = dict(terminal.get("outcome") or {})
    outcome_trace = dict(terminal.get("outcome_trace") or {})
    outcome_trace_id = str(outcome_trace.get("record_id") or "")
    evidence_id = str(audit.record_id if audit else event.get("id") or outcome.get("id") or "")
    ok = bool(stored.record_id and recall_hit and audit and event and outcome)
    result = {
        "ok": ok,
        "verdict": "pass" if ok else "fail",
        "scope": canonical_scope,
        "session_id": session_id,
        "store": {"ok": bool(stored.record_id), "record_id": stored.record_id},
        "recall": {
            "hit": bool(recall_hit),
            "audit_record_id": audit.record_id if audit else "",
            "selected_count": len(items),
        },
        "policy": {
            "hit": bool(before.get("task_context", {}).get("policy_suggestion_ids")),
            "policy_suggestion_ids": list(before.get("task_context", {}).get("policy_suggestion_ids") or []),
        },
        "injection": {
            "withheld_count": int(injection_composition.get("withheld") or injection_plan.get("withheld_count") or 0),
            "lane_composition": injection_composition,
        },
        "outcome": {
            "event_id": str(event.get("id") or ""),
            "outcome_id": str(outcome.get("id") or ""),
            "trace_id": outcome_trace_id,
        },
        "ledger": {
            "evidence_id": evidence_id,
            "audit_record_id": audit.record_id if audit else "",
            "outcome_trace_id": outcome_trace_id,
        },
    }
    # Capability advertisement/normalization is deliberately opt-in for this
    # operator diagnostic.  The default E2E probe remains a lifecycle hook
    # check; it never guesses a semantic capability from an OpenClaw host.
    if capability_advertisement is not None:
        result["capability_advertisement"] = hooks.advertise_capabilities(
            capability_advertisement,
            event=event_base,
        )
    if capability_outcome is not None:
        result["capability_outcome"] = hooks.normalize_capability_outcome(
            "e2e",
            {
                **event_base,
                "capability_outcome": dict(capability_outcome),
            },
        )
    return result


def _latest_recall_audit(runtime: Runtime, *, session_id: str, scope: dict[str, Any]) -> RecordEnvelope | None:
    scope_ref = ScopeRef.from_dict(scope)
    lookup = getattr(runtime.store, "list_recall_audits_compact_by_session", None)
    if not callable(lookup):
        return None
    try:
        records = lookup(
            scope=scope_ref,
            session_id=str(session_id).strip(),
            limit=10,
        )
    except Exception:
        return None
    if not isinstance(records, list):
        return None
    for record in records:
        if record.scope != scope_ref:
            continue
        if str(record.source or "") != "openclaw.before_prompt_build":
            continue
        content = record.content if isinstance(record.content, dict) else {}
        meta = record.meta if isinstance(record.meta, dict) else {}
        if str(content.get("session_id") or meta.get("session_id") or "").strip() == str(session_id).strip():
            return record
    return None
