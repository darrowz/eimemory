from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.experience import record_outcome_trace
from eimemory.experience.capability_contract import CASE_CONTRACTS
from eimemory.governance.capability_acceptance import run_capability_acceptance
from eimemory.governance.capability_attribution import attribute_capability_outcomes, collect_capability_evidence
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


def test_policy_source_field_does_not_pollute_search_discovery(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "office-source-field"}
    record_outcome_trace(
        runtime,
        _trace_payload("trace-office-source", task_type="meeting_notes", summary="Office daily meeting notes verified"),
        scope=scope,
    )

    report = attribute_capability_outcomes(runtime, scope=scope, loop_id="attr_source_noise")

    assert "search.discovery" not in report["capabilities"]


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


def test_attribution_maps_chinese_search_tasks_to_search_discovery(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "search-cn"}
    record_outcome_trace(
        runtime,
        _trace_payload(
            "trace-search-cn-1",
            task_type="搜索最近热门项目",
            summary="按创建时间范围查找最高星项目，并说明热门趋势口径",
        ),
        scope=scope,
    )

    report = attribute_capability_outcomes(runtime, scope=scope, loop_id="attr_search_cn")
    ledger = build_capability_ledger(runtime, scope=scope)

    assert "search.discovery" in report["capabilities"]
    assert ledger["capabilities"]["search.discovery"]["evidence_sources"] == ["outcome_trace"]


def test_unverified_success_is_not_capability_evidence(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "unverified"}
    record_outcome_trace(
        runtime,
        {
            "trace_id": "trace-unverified-search",
            "idempotency_key": "idem-unverified-search",
            "task_type": "search_discovery",
            "input_summary": "Search GitHub sources",
            "outcome": {"status": "success"},
            "verifier": {"passed": False},
            "feedback": {"summary": "not verified"},
        },
        scope=scope,
    )

    report = attribute_capability_outcomes(runtime, scope=scope, loop_id="attr_unverified")

    assert "search.discovery" not in report["capabilities"]


def test_explicit_contracts_attribute_exactly_one_capability_and_case(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "contract-attribution"}
    try:
        acceptance = run_capability_acceptance(runtime, scope=scope, persist=True)
        evidence_by_capability = collect_capability_evidence(runtime, scope=scope)
    finally:
        runtime.close()

    assert acceptance["all_passed"] is True
    contract_items = [
        item
        for items in evidence_by_capability.values()
        for item in items
        if item["contract_verified"] is True
    ]
    assert len(contract_items) == len(CASE_CONTRACTS) == 12
    assert {item["case_id"] for item in contract_items} == set(CASE_CONTRACTS)
    for item in contract_items:
        expected_capability = CASE_CONTRACTS[item["case_id"]][0]
        assert item["capabilities"] == [expected_capability]
        assert item["capability"] == expected_capability
        assert len(item["source_record_ids"]) == 1
        assert item["source_record_ids"][0] in acceptance["probe_ids"]


def test_legacy_keyword_trace_is_diagnostic_only(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"agent_id": "hongtu", "workspace_id": "legacy-attribution"}
    try:
        stored = record_outcome_trace(
            runtime,
            _trace_payload(
                "legacy-search-trace",
                task_type="search_discovery",
                summary="Recent GitHub source discovery was verified",
            ),
            scope=scope,
        )
        evidence_by_capability = collect_capability_evidence(runtime, scope=scope)
    finally:
        runtime.close()

    legacy = next(
        item
        for item in evidence_by_capability["search.discovery"]
        if item["source_id"] == stored["record_id"]
    )
    assert legacy["contract_verified"] is False
    assert legacy["case_id"] == ""
    assert legacy["source_record_ids"] == []
