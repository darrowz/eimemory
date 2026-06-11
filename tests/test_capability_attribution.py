from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.experience import record_outcome_trace
from eimemory.governance.capability_attribution import attribute_capability_outcomes
from eimemory.governance.capability_ledger import build_capability_ledger


def _trace_payload(trace_id: str, *, task_type: str, summary: str, status: str = "success") -> dict:
    return {
        "trace_id": trace_id,
        "idempotency_key": f"idem-{trace_id}",
        "task_type": task_type,
        "input_summary": summary,
        "outcome": {"status": status},
        "verifier": {"passed": status == "success"},
        "feedback": {"summary": "verified"},
        "policy_attribution": {
            "matched_event_type": task_type,
            "policy_sources": ["event_outcome"],
            "selected_records": [{"record_id": f"policy-{trace_id}"}],
        },
    }


def test_attribute_capability_outcomes_writes_business_evidence_to_ledger(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "business", "user_id": "darrow"}
    event = runtime.record_event(
        {
            "source": "manual",
            "user_phrase": "Find recent GitHub projects for the UUMit delivery plan",
            "event_type": "trending_search",
            "interpreted_intent": "Search and discover sources for a UUMit business task",
            "goal": "Support UUMit operations with verified search results",
            "verification": "returned links were checked",
        },
        scope=scope,
    )
    runtime.record_outcome(
        event["id"],
        {
            "outcome": "good",
            "reason": "Search results were relevant and verified",
            "policy_attribution": {
                "matched_event_type": "trending_search",
                "policy_sources": ["event_outcome"],
                "selected_records": [{"record_id": event["id"]}],
            },
        },
        scope=scope,
    )
    record_outcome_trace(
        runtime,
        _trace_payload("trace-u-1", task_type="uumit_delivery", summary="UUMit office daily task completed"),
        scope=scope,
    )

    report = attribute_capability_outcomes(runtime, scope=scope, loop_id="attr_test")
    ledger = build_capability_ledger(runtime, scope=scope)

    assert report["ok"] is True
    assert {"operations.uumit", "search.discovery", "office.daily_task"} <= set(report["capabilities"])
    assert ledger["capabilities"]["operations.uumit"]["evidence_count"] >= 1
    assert ledger["capabilities"]["operations.uumit"]["status"] != "stale_unverified"
    assert ledger["capabilities"]["search.discovery"]["evidence_tiers"]
    assert ledger["capabilities"]["search.discovery"]["evidence_sources"]


def test_low_evidence_business_capability_triggers_recalculation_gap(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "office"}
    record_outcome_trace(
        runtime,
        _trace_payload("trace-office-1", task_type="meeting_notes", summary="Office daily meeting notes verified"),
        scope=scope,
    )
    record_outcome_trace(
        runtime,
        _trace_payload("trace-office-2", task_type="meeting_notes", summary="Office daily calendar task verified"),
        scope=scope,
    )

    attribute_capability_outcomes(runtime, scope=scope, loop_id="attr_gap")
    item = build_capability_ledger(runtime, scope=scope)["capabilities"]["office.daily_task"]

    assert item["evidence_count"] == 2
    assert item["status"] == "needs_outcome_recalculation"
    assert item["needs_outcome_recalculation"] is True
    assert item["goal_gap_reason"] == "insufficient_outcome_evidence"


def test_attribution_covers_research_synthesis_and_device_control(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "mixed"}
    record_outcome_trace(
        runtime,
        _trace_payload("trace-research-1", task_type="research_synthesis", summary="Synthesized research papers into a brief"),
        scope=scope,
    )
    record_outcome_trace(
        runtime,
        _trace_payload("trace-device-1", task_type="media_playback", summary="Controlled speaker playback and verified audio output"),
        scope=scope,
    )

    report = attribute_capability_outcomes(runtime, scope=scope, loop_id="attr_mixed")
    ledger = build_capability_ledger(runtime, scope=scope)

    assert {"research.synthesis", "device.control"} <= set(report["capabilities"])
    assert ledger["capabilities"]["research.synthesis"]["evidence_count"] == 1
    assert ledger["capabilities"]["device.control"]["evidence_count"] == 1
