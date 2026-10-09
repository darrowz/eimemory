"""Closure/deploy self-test records must not be known-item targets.

Prod closure ``closure-llrbm15b`` (1.14.48): known-item 2/10, false recall
0.6.  The generated sampler picked closed-loop ``auto-feedback`` memories
(memory_type ``reflection``, excluded from default recall since 1.14.47) and
SAG event memory projected from capability/live acceptance probes.  The first
are evolution artifacts recall must not return; the second are self-test
traces of those artifacts.  Real experience event memory stays recallable.
"""
from __future__ import annotations

import json

import pytest

from eimemory.contracts.recall_boundary import (
    ACCEPTANCE_EVENT_MEMORY_TYPE,
    default_recall_blocked_lane,
    effective_recall_memory_type,
    is_operational_probe_task_type,
)
from eimemory.models.records import RecordEnvelope, ScopeRef

pytestmark = pytest.mark.usefixtures("local_collection_boundary")

SCOPE = ScopeRef(agent_id="hongtu", workspace_id="embodied", user_id="darrow")


def _event_memory(task_type: str, *, summary: str = "") -> RecordEnvelope:
    text = summary or (
        f"SAG event memory for {task_type} | task: Production live acceptance {task_type} | outcome: good | "
        f"label: success | entities: {task_type}, production, live, acceptance, rehearsal, false, status, success, true"
    )
    meta = {"memory_type": "event_trace", "projection_type": "event_memory", "task_type": task_type, "event_id": f"ev-{task_type}"}
    return RecordEnvelope.create(
        kind="memory",
        title=f"Event memory: {task_type}",
        summary=text,
        content={"text": f"Event memory: {task_type}\n{text}", **meta},
        scope=SCOPE,
        source="eimemory.event_graph",
        meta=meta,
    )


def _auto_feedback(index: int) -> RecordEnvelope:
    text = json.dumps({"evaluation": {"confidence": 0.0, "ok": True, "outcome_status": "good",
                                      "primary_label": "success", "record_id": f"out_{index:016x}",
                                      "source": "closed_loop.evaluate"}})
    return RecordEnvelope.create(
        kind="memory", title="auto-feedback", summary=text,
        content={"text": text, "memory_type": "reflection"}, scope=SCOPE, source="loop",
        meta={"memory_type": "reflection", "report_type": "closed_loop_feedback", "closed_loop_stage": "auto-feedback"},
    )


def test_only_acceptance_probe_event_memory_changes_lane() -> None:
    assert is_operational_probe_task_type("capability.acceptance")
    assert is_operational_probe_task_type("live.acceptance.governance.dashboard_read")
    assert not is_operational_probe_task_type("live.acceptance.")
    assert not is_operational_probe_task_type("ops.health")
    probe = effective_recall_memory_type("event_trace", projection_type="event_memory", task_type="capability.acceptance")
    assert probe == ACCEPTANCE_EVENT_MEMORY_TYPE
    assert effective_recall_memory_type("event_trace", projection_type="event_memory", task_type="ops.health") == "event_trace"
    assert effective_recall_memory_type("fact", projection_type="event_memory", task_type="capability.acceptance") == "fact"
    real = _event_memory("ops.health", summary="SAG event memory for ops.health | task: Verify 8091 health")
    assert default_recall_blocked_lane(real.meta, real.content) == ""
    assert default_recall_blocked_lane(_event_memory("capability.acceptance").meta, {}) == "evolution_artifact"
    assert default_recall_blocked_lane(_auto_feedback(0).meta, {}) == "evolution_artifact"


def test_known_item_sampler_matches_default_recall_on_closure_shaped_data(tmp_path) -> None:
    from dataclasses import asdict

    from eimemory.api.runtime import Runtime
    from eimemory.evaluation.production_recall import _record_recall_lane
    from eimemory.scheduler.jobs import _production_recall_smoke_dataset

    runtime = Runtime.create(root=tmp_path / "runtime")
    try:
        facts = [
            runtime.store.append(RecordEnvelope.create(
                kind="memory", title=f"Dock {index}", summary=f"The charging dock {index} is in lab room {100 + index}.",
                scope=SCOPE, source="operator.preference", source_id=f"pref-{index}", meta={"memory_type": "preference"},
            ))
            for index in range(3)
        ]
        probes = [runtime.store.append(_event_memory(task_type)) for task_type in (
            "capability.acceptance", "live.acceptance.deployment.identity", "live.acceptance.governance.dashboard_read")]
        feedback = [runtime.store.append(_auto_feedback(index)) for index in range(6)]

        dataset = _production_recall_smoke_dataset(runtime, scope=asdict(SCOPE))
        sampled = {case["expected_record_ids"][0] for case in dataset["cases"]}
        assert sampled == {record.record_id for record in facts}
        assert not sampled & {record.record_id for record in probes + feedback}

        report = runtime.run_production_recall_eval(dataset, seed=False, scope=asdict(SCOPE))
        assert report["hit_at_1"] == 1.0
        assert report["false_recall_rate"] == 0.0

        # Gate pollution metrics share the canonical lane map with recall.
        assert _record_recall_lane(feedback[0]) == "evolution_artifact"
        assert _record_recall_lane(probes[0]) == "evolution_artifact"

        # Default recall never returns acceptance traces, even by exact title.
        bundle = runtime.memory.recall(query="Event memory: capability.acceptance", scope=asdict(SCOPE),
                                       task_context={}, limit=5)
        assert not {item.record_id for item in bundle.items} & {record.record_id for record in probes}

        row = runtime.store.sqlite.conn.execute(
            "SELECT memory_type, lane FROM recall_index WHERE record_id = ?", (probes[0].record_id,)).fetchone()
        assert row["memory_type"] == ACCEPTANCE_EVENT_MEMORY_TYPE
    finally:
        runtime.close()


def test_real_experience_event_memory_stays_known_item_recallable(tmp_path) -> None:
    from eimemory.api.runtime import Runtime
    from eimemory.governance.closed_loop import post_experience_hook
    from eimemory.scheduler.jobs import _production_recall_smoke_dataset

    runtime = Runtime.create(root=tmp_path / "runtime")
    runtime.generate_learning_thoughts = lambda **kwargs: {"ok": True, "thoughts": []}  # type: ignore[method-assign]
    scope = {"agent_id": "eibrain", "workspace_id": "ops", "user_id": "darrow"}
    try:
        outcome = runtime.record_outcome_trace({
            "trace_id": "real-exp-1", "task_type": "ops.backup",
            "input_summary": "Rotate the nightly sqlite backup on the lab server",
            "expected_tools": ["rsync"], "selected_tools": [], "actions": [{"type": "skip"}],
            "outcome": {"status": "bad"}, "cost": 0.1,
        }, scope=scope)
        event_id = post_experience_hook(runtime, outcome, scope)["event_graph"]["event_record_id"]
        dataset = _production_recall_smoke_dataset(runtime, scope=scope)
        cases = [case for case in dataset["cases"] if case["expected_record_ids"] == [event_id]]
        assert cases
        report = runtime.run_production_recall_eval({**dataset, "cases": cases}, seed=False, scope=scope)
        assert report["hit_at_1"] == 1.0
    finally:
        runtime.close()


def test_repair_recall_lanes_previews_applies_and_reverts_without_touching_records(tmp_path) -> None:
    from eimemory.storage.recall_lane_repair import repair_recall_lane_memory_types
    from eimemory.storage.runtime_store import RuntimeStore

    store = RuntimeStore(tmp_path)
    try:
        probe = store.append(_event_memory("capability.acceptance"))
        real = store.append(_event_memory("ops.health", summary="SAG event memory for ops.health | task: Verify 8091"))
        conn = store.sqlite.conn
        # Rows indexed before 1.14.49 carry the raw payload memory type.
        conn.execute("UPDATE recall_index SET memory_type = 'event_trace' WHERE record_id = ?", (probe.record_id,))
        conn.commit()

        def snapshot():
            return conn.execute(
                "SELECT record_id, payload_json, payload_digest, updated_at FROM records ORDER BY record_id").fetchall()

        def index_type(record_id):
            return conn.execute("SELECT memory_type FROM recall_index WHERE record_id = ?", (record_id,)).fetchone()[0]

        records_before = [tuple(row) for row in snapshot()]
        preview = repair_recall_lane_memory_types(store, scope=SCOPE)
        assert preview["applied"] is False and preview["eligible"] == 1 and preview["unproven"] == []
        assert preview["changes"][0]["record_id"] == probe.record_id
        assert preview["changes"][0]["new_memory_type"] == ACCEPTANCE_EVENT_MEMORY_TYPE
        assert index_type(probe.record_id) == "event_trace"

        applied = repair_recall_lane_memory_types(store, scope=SCOPE, apply=True)
        assert applied["repaired"] == 1 and applied["plan_digest"] == preview["plan_digest"]
        assert index_type(probe.record_id) == ACCEPTANCE_EVENT_MEMORY_TYPE
        assert index_type(real.record_id) == "event_trace"
        assert repair_recall_lane_memory_types(store, scope=SCOPE, apply=True)["repaired"] == 0

        reverted = repair_recall_lane_memory_types(store, scope=SCOPE, apply=True, revert=True)
        assert reverted["repaired"] == 1 and index_type(probe.record_id) == "event_trace"
        assert [tuple(row) for row in snapshot()] == records_before
        assert conn.execute("SELECT COUNT(*) FROM recall_index").fetchone()[0] == 2

        with pytest.raises(ValueError, match="exact_owner_required"):
            repair_recall_lane_memory_types(store, scope=ScopeRef(agent_id="hongtu", workspace_id="embodied"))
    finally:
        store.close()
