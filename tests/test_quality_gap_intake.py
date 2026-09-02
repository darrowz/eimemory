from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.capabilities import CapabilityBinding, CapabilityDefinition, CapabilityRevision
from eimemory.governance.quality_gap_intake import (
    QUALITY_GAP_SOURCE,
    ingest_quality_gate_reports,
)
from eimemory.governance.curiosity import generate_learning_goals
from eimemory.governance.self_model import build_self_model
from eimemory.scheduler.jobs import _run_quality_gap_intake


SCOPE = {
    "tenant_id": "default",
    "agent_id": "hongtu",
    "workspace_id": "embodied::channel::hermes",
    "user_id": "darrow",
}
STAMP = "2020-01-01T00:00:00Z"


def _register_memory_recall(runtime: Runtime) -> None:
    capability_id = "memory.recall"
    revision_id = f"{capability_id}:v1"
    runtime.capabilities.register_definition(
        CapabilityDefinition(
            capability_id=capability_id,
            display_name="Memory Recall",
            description="Test-local recall capability.",
            owner="governance",
            created_at=STAMP,
            provenance={"source": "quality-gap-test"},
        ),
        runtime_scope=SCOPE,
    )
    runtime.capabilities.register_revision(
        CapabilityRevision(
            revision_id=revision_id,
            capability_id=capability_id,
            contract={
                "input_schema": {"type": "object"},
                "output_schema": {"type": "object"},
                "success_invariants": ["bounded_recall"],
                "failure_invariants": ["cross_scope_leakage"],
                "evidence_requirements": {"minimum_refs": 1},
                "dependencies": [],
                "composition": [],
                "risk_tier": "low",
                "side_effect_class": "none",
            },
            compatibility="incompatible",
            created_at=STAMP,
            provenance={"source": "quality-gap-test"},
        ),
        runtime_scope=SCOPE,
    )
    runtime.capabilities.bind(
        CapabilityBinding(
            binding_id="binding.memory.recall:v1",
            capability_id=capability_id,
            capability_revision_id=revision_id,
            provider_kind="module",
            provider_instance_id="quality-gap-test",
            implementation_digest="a" * 64,
            operations=("recall",),
            limits={"max_requests": 8},
            environment_fingerprint={"runtime": "test"},
            applicability={"scope": "global"},
            advertisement_evidence_refs=("artifact://quality-gap/test.json",),
            provenance={"source": "quality-gap-test"},
            created_at=STAMP,
        ),
        runtime_scope=SCOPE,
    )


def _failed_recall_report(*, payload_bytes: int = 206_100) -> dict:
    return {
        "report_type": "recall_quality_report",
        "sample_count": 8,
        "quality_gate": {
            "ok": False,
            "blocked_reason": "recall_quality_gate_failed",
            "blocking_metrics": {
                "payload_bytes_top_5": {
                    "actual": payload_bytes,
                    "threshold": 16_384,
                    "operator": "<=",
                },
                "noise_rate": {
                    "actual": 0.5,
                    "threshold": 0.4,
                    "operator": "<=",
                },
            },
        },
    }


def test_failed_machine_gate_becomes_deduplicated_l5_gap(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        first = ingest_quality_gate_reports(
            runtime,
            reports={"production_recall": _failed_recall_report()},
            scope=SCOPE,
        )
        second = ingest_quality_gate_reports(
            runtime,
            reports={"production_recall": _failed_recall_report()},
            scope=SCOPE,
        )
        rows = [
            row
            for row in runtime.store.list_records(kinds=["reflection"], scope=SCOPE, limit=20)
            if row.source == QUALITY_GAP_SOURCE
        ]
    finally:
        runtime.close()

    assert first["created_count"] == 1
    assert second["created_count"] == 0
    assert second["deduplicated_record_ids"] == first["created_record_ids"]
    assert len(rows) == 1
    gap = rows[0]
    assert gap.status == "active"
    assert gap.meta["target_capability"] == "memory.recall"
    assert gap.meta["is_failure"] is True
    assert gap.content["blocking_metrics"]["payload_bytes_top_5"]["actual"] == 206_100
    assert "tenant_acl" in gap.content["candidate_boundary"]["forbidden"]
    assert gap.content["success_criteria"]["cross_scope_leakage_count"] == 0


def test_changed_failure_supersedes_prior_gap_and_passing_gate_resolves_latest(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        first = ingest_quality_gate_reports(
            runtime,
            reports={"production_recall": _failed_recall_report()},
            scope=SCOPE,
        )
        changed = ingest_quality_gate_reports(
            runtime,
            reports={"production_recall": _failed_recall_report(payload_bytes=319_000)},
            scope=SCOPE,
        )
        latest_id = changed["created_record_ids"][0]
        latest = runtime.store.get_by_id(latest_id, scope=SCOPE)
        passed = ingest_quality_gate_reports(
            runtime,
            reports={
                "production_recall": {
                    "report_type": "recall_quality_report",
                    "sample_count": 8,
                    "quality_gate": {"ok": True, "blocking_metrics": {}},
                }
            },
            scope=SCOPE,
        )
        resolution_id = passed["resolved_record_ids"][0]
        resolved = runtime.store.get_by_id(resolution_id, scope=SCOPE)
    finally:
        runtime.close()

    assert changed["created_count"] == 1
    assert latest is not None
    assert latest.content["supersedes_gap_id"] == first["created_record_ids"][0]
    assert len(passed["resolved_record_ids"]) == 1
    assert resolved is not None
    assert resolved.status == "resolved"
    assert resolved.content["resolves_gap_id"] == latest_id
    assert resolved.content["resolution"]["status"] == "passed"


def test_quality_gap_is_visible_to_l5_self_model_in_same_runtime(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        _register_memory_recall(runtime)
        intake = _run_quality_gap_intake(
            runtime,
            scope=SCOPE,
            reports={"production_recall": _failed_recall_report()},
        )
        self_model = build_self_model(runtime, scope=SCOPE, persist=False)
        goals = generate_learning_goals(
            self_model,
            [],
            goal_registry={},
            thoughts=[],
            max_goals=20,
        )
    finally:
        runtime.close()

    gap_id = intake["created_record_ids"][0]
    matching = [
        item
        for item in self_model["weaknesses"]
        if item.get("capability") == "memory.recall"
        and gap_id in (item.get("source_record_ids") or [])
    ]
    assert intake["mutation_boundary"] == {
        "observation_records_only": True,
        "production_policy_changed": False,
        "acl_changed": False,
        "release_gate_changed": False,
    }
    assert matching
    assert any(
        goal.get("target_capability") == "memory.recall"
        and gap_id in (goal.get("source_record_ids") or [])
        for goal in goals
    )


def test_unknown_report_without_machine_gate_is_ignored(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    try:
        result = ingest_quality_gate_reports(
            runtime,
            reports={"unknown": {"ok": False, "error": "plain_error_without_gate"}},
            scope=SCOPE,
        )
    finally:
        runtime.close()

    assert result["created_count"] == 0
    assert result["ignored_reports"] == ["unknown"]
