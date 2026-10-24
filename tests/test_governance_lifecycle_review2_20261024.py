"""Public quality-gap lifecycle must stay closed across readers and writers."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
import json
from threading import Barrier, Event, Lock

import pytest

from eimemory.api.runtime import Runtime
from eimemory.governance.learning import quality_gap_intake as gaps
from eimemory.governance.learning import self_model as self_models
from eimemory.governance.learning.curiosity import generate_learning_goals
from eimemory.governance.learning.self_model import build_self_model
from eimemory.models.records import RecordEnvelope, ScopeRef
from tests.test_quality_gap_intake import SCOPE, _failed_recall_report, _register_memory_recall


@pytest.fixture
def runtime(tmp_path):
    with closing(Runtime.create(root=tmp_path)) as value:
        _register_memory_recall(value)
        yield value


def ingest(runtime, report):
    return gaps.ingest_quality_gate_reports(runtime, scope=SCOPE, reports={"production_recall": report})


def quality_records(runtime):
    return [record for record in runtime.store.list_records(kinds=["reflection"], scope=SCOPE, limit=100)
            if record.source == gaps.QUALITY_GAP_SOURCE]


def source_refs(model):
    return {ref for weakness in model["weaknesses"] for ref in weakness["source_record_ids"]}


def test_resolved_gap_stays_closed_through_self_model_goals_world_and_restart(tmp_path):
    with closing(Runtime.create(root=tmp_path)) as runtime:
        _register_memory_recall(runtime)
        first = ingest(runtime, _failed_recall_report())["created_record_ids"][0]
        assert first in source_refs(build_self_model(runtime, scope=SCOPE, persist=False))
        resolution = ingest(runtime, {"quality_gate": {"ok": True}})["resolved_record_ids"][0]
        model = build_self_model(runtime, scope=SCOPE, persist=False)
        assert not ({first, resolution} & source_refs(model))
        goals = generate_learning_goals(model, [], goal_registry={}, thoughts=[], max_goals=20)
        assert not any({first, resolution} & set(goal.get("source_record_ids", [])) for goal in goals)
        assert runtime.build_world_model(scope=SCOPE, persist=False)["weaknesses"] == []
    with closing(Runtime.create(root=tmp_path)) as runtime:
        assert not source_refs(build_self_model(runtime, scope=SCOPE, persist=False))
        reopened = ingest(runtime, _failed_recall_report(payload_bytes=300_000))["created_record_ids"][0]
        assert source_refs(build_self_model(runtime, scope=SCOPE, persist=False)) == {reopened}
        assert ingest(runtime, {"quality_gate": {"ok": True}})["resolved_count"] == 1
        assert not source_refs(build_self_model(runtime, scope=SCOPE, persist=False))
        assert len(quality_records(runtime)) == 4  # Preserve the full observation chain.


def test_revoked_resolution_and_fork_do_not_hide_unresolved_failure(runtime):
    first = ingest(runtime, _failed_recall_report())["created_record_ids"][0]
    resolution_id = ingest(runtime, {"quality_gate": {"ok": True}})["resolved_record_ids"][0]
    resolution = runtime.store.get_by_id(resolution_id, scope=SCOPE)
    resolution.status = "quarantined"
    runtime.store.rewrite(resolution)
    assert first in source_refs(build_self_model(runtime, scope=SCOPE, persist=False))
    assert resolution_id not in source_refs(build_self_model(runtime, scope=SCOPE, persist=False))

    resolution.status = "resolved"
    runtime.store.rewrite(resolution)
    original = runtime.store.get_by_id(first, scope=SCOPE)
    fork = gaps._gap_record(gaps._quality_finding("production_recall", _failed_recall_report(payload_bytes=300_000)),
                            scope=ScopeRef.from_dict(SCOPE), supersedes=original)
    runtime.store.append(fork)
    assert fork.record_id in source_refs(build_self_model(runtime, scope=SCOPE, persist=False))
    assert resolution_id not in source_refs(build_self_model(runtime, scope=SCOPE, persist=False))


def test_shared_scope_resolution_cannot_close_private_gap(runtime):
    first = ingest(runtime, _failed_recall_report())["created_record_ids"][0]
    original = runtime.store.get_by_id(first, scope=SCOPE)
    resolution = gaps._resolution_record(gaps._quality_finding("production_recall", {"quality_gate": {"ok": True}}),
                                        scope=replace(original.scope, user_id=""), resolves=original)
    runtime.store.append(resolution)
    assert first in source_refs(build_self_model(runtime, scope=SCOPE, persist=False))
    assert resolution.record_id not in source_refs(build_self_model(runtime, scope=SCOPE, persist=False))


def test_quality_resolution_filter_preserves_unrelated_manual_reflections(runtime):
    manual = runtime.store.append(RecordEnvelope.create(
        kind="reflection", title="Quality gate passed: independent operator lesson",
        summary="Check the source before proposing another workflow.",
        scope=ScopeRef.from_dict(SCOPE), source="operator.review", status="active",
        meta={"tag": "resolved", "target_capability": "memory.recall"},
    ))
    ingest(runtime, _failed_recall_report())
    ingest(runtime, {"quality_gate": {"ok": True}})
    assert source_refs(build_self_model(runtime, scope=SCOPE, persist=False)) == {manual.record_id}


def test_closed_chain_longer_than_legacy_fallback_window_stays_closed(runtime):
    for index in range(51):
        assert ingest(runtime, _failed_recall_report(payload_bytes=300_000 + index))["created_count"] == 1
    assert ingest(runtime, {"quality_gate": {"ok": True}})["resolved_count"] == 1
    assert len(quality_records(runtime)) == 52
    assert not source_refs(build_self_model(runtime, scope=SCOPE, persist=False))
    # The legacy fallback window holds only the newest 50 records and cannot
    # prove this chain complete. The already computed self-model is authority.
    assert runtime.build_world_model(scope=SCOPE, persist=False)["weaknesses"] == []


def test_self_model_user_limit_does_not_truncate_quality_closure_evidence(runtime, monkeypatch):
    monkeypatch.setattr(self_models, "_QUALITY_CHAIN_PAGE_SIZE", 2)
    first = ingest(runtime, _failed_recall_report())["created_record_ids"][0]
    latest = ingest(runtime, _failed_recall_report(payload_bytes=300_000))["created_record_ids"][0]
    resolution = ingest(runtime, {"quality_gate": {"ok": True}})["resolved_record_ids"][0]
    model = build_self_model(runtime, scope=SCOPE, limit=2, persist=False)
    assert model["metrics"]["reflection_count"] == 2
    assert not ({first, latest, resolution} & source_refs(model))
    goals = generate_learning_goals(model, [], goal_registry={}, thoughts=[], max_goals=20)
    assert not any({first, latest, resolution} & set(goal.get("source_record_ids", [])) for goal in goals)


def test_quality_chain_budget_counts_selected_groups_without_expanding_history(runtime, monkeypatch):
    for report in (_failed_recall_report(), {"quality_gate": {"ok": True}}):
        gaps.ingest_quality_gate_reports(runtime, scope=SCOPE, reports={"memory_quality": report})
    for record in quality_records(runtime):
        record.time = replace(record.time, updated_at="2000-01-01T00:00:00Z")
        runtime.store.rewrite(record)
    ingest(runtime, _failed_recall_report())
    ingest(runtime, _failed_recall_report(payload_bytes=300_000))
    ingest(runtime, {"quality_gate": {"ok": True}})

    monkeypatch.setattr(self_models, "QUALITY_CHAIN_EVIDENCE_LIMIT", 3)
    selected = build_self_model(runtime, scope=SCOPE, limit=2, persist=False)
    assert selected["quality_closure"]["ok"] is True
    assert selected["quality_closure"]["record_count"] == 3
    assert selected["quality_closure"]["group_count"] == 1
    assert selected["metrics"]["reflection_count"] == 2
    assert not source_refs(selected)

    monkeypatch.setattr(self_models, "QUALITY_CHAIN_EVIDENCE_LIMIT", 5)
    exact = build_self_model(runtime, scope=SCOPE, limit=5, persist=False)
    assert exact["quality_closure"]["ok"] is True
    assert exact["quality_closure"]["record_count"] == 5
    assert exact["quality_closure"]["group_count"] == 2
    assert not source_refs(exact)

    monkeypatch.setattr(self_models, "QUALITY_CHAIN_EVIDENCE_LIMIT", 4)
    blocked = build_self_model(runtime, scope=SCOPE, limit=5, persist=True)
    assert blocked["ok"] is False
    assert blocked["status"] == "blocked"
    assert blocked["reason"] == "quality_chain_evidence_budget_exceeded"
    assert blocked["persistence"]["status"] == "not_persisted"
    assert generate_learning_goals(blocked, [], goal_registry={}, thoughts=[]) == []


def test_incomplete_quality_evidence_blocks_public_thoughts_and_learning(runtime, monkeypatch):
    ingest(runtime, _failed_recall_report())
    ingest(runtime, _failed_recall_report(payload_bytes=300_000))
    ingest(runtime, {"quality_gate": {"ok": True}})
    monkeypatch.setattr(self_models, "QUALITY_CHAIN_EVIDENCE_LIMIT", 2)
    target_kinds = ["thought", "capability_model", "learning_goal", "learning_candidate"]
    before = runtime.store.count_records(kinds=target_kinds, scope=SCOPE)
    thoughts = runtime.generate_learning_thoughts(scope=SCOPE, persist=True)
    assert thoughts["ok"] is False and thoughts["status"] == "blocked"
    assert thoughts["thought_count"] == 0 and thoughts["persisted_count"] == 0
    assert thoughts["supervisor_summary"]["ok"] is False
    assert "quality_chain_evidence_budget_exceeded" in thoughts["supervisor_summary"]["error"]
    assert thoughts["self_model_persistence"]["status"] == "not_persisted"
    cycle = runtime.run_autonomous_learning_cycle(scope=SCOPE, allow_network=False, legacy_compatibility=True)
    assert cycle["ok"] is False and cycle["status"] == "blocked"
    assert not cycle["goals"] and not cycle["candidate_ids"]
    assert runtime.store.get_by_id(cycle["loop_record_id"], scope=SCOPE).status == "blocked"
    world = runtime.build_world_model(scope=SCOPE, persist=True)
    assert world["ok"] is False and world["status"] == "blocked"
    l5 = runtime.run_l5_cycle(scope=SCOPE, persist=True, allow_network=False)
    assert l5["ok"] is False and l5["status"] == "blocked"
    assert l5["assessment"]["ok"] is False
    assert runtime.store.count_records(kinds=target_kinds, scope=SCOPE) == before


def test_corrupt_quality_ancestor_outside_user_window_blocks_closure(runtime):
    first = ingest(runtime, _failed_recall_report())["created_record_ids"][0]
    ingest(runtime, _failed_recall_report(payload_bytes=300_000))
    ingest(runtime, {"quality_gate": {"ok": True}})
    ancestor = runtime.store.get_by_id(first, scope=SCOPE)
    ancestor.time = replace(ancestor.time, updated_at="2000-01-01T00:00:00Z")
    runtime.store.rewrite(ancestor)
    with runtime.store.locked() as sqlite:
        sqlite.execute("UPDATE records SET payload_digest=? WHERE record_id=?", ("0" * 64, first))
        sqlite.commit()
    model = build_self_model(runtime, scope=SCOPE, limit=1, persist=True)
    assert model["ok"] is False and model["status"] == "blocked"
    assert model["reason"] == "quality_chain_evidence_unavailable"
    assert model["persistence"]["status"] == "not_persisted"


def test_damaged_quality_candidate_cannot_become_no_quality_history(runtime):
    record_id = ingest(runtime, _failed_recall_report())["created_record_ids"][0]
    with runtime.store.locked() as sqlite:
        sqlite.execute("UPDATE records SET payload_json=?,payload_pointer_json='',payload_digest=? WHERE record_id=?",
                       ("not valid json", "f" * 64, record_id))
        sqlite.commit()
    model = build_self_model(runtime, scope=SCOPE, persist=False)
    assert model["quality_closure"]["status"] == "blocked"
    assert model["ok"] is False


def test_metadata_filter_drift_cannot_hide_a_reopened_quality_gap(runtime):
    ingest(runtime, _failed_recall_report())
    ingest(runtime, {"quality_gate": {"ok": True}})
    reopened = ingest(runtime, _failed_recall_report(payload_bytes=300_000))["created_record_ids"][0]
    with runtime.store.locked() as sqlite:
        row = sqlite.execute("SELECT meta_json FROM records WHERE record_id=?", (reopened,)).fetchone()
        metadata = json.loads(row["meta_json"])
        metadata["semantic_key"] = "corrupt-projection-key"
        sqlite.execute("UPDATE records SET meta_json=? WHERE record_id=?", (json.dumps(metadata), reopened))
        sqlite.commit()
    model = build_self_model(runtime, scope=SCOPE, limit=1, persist=False)
    assert model["quality_closure"]["status"] == "blocked"
    assert model["ok"] is False


def test_quality_selection_and_complete_chain_share_one_snapshot(runtime, monkeypatch):
    ingest(runtime, _failed_recall_report())
    ingest(runtime, {"quality_gate": {"ok": True}})
    native = self_models._complete_quality_weakness_records
    selected, continue_read = Event(), Event()

    def pause(reader, records, **kwargs):
        selected.set()
        assert continue_read.wait(timeout=5)
        return native(reader, records, **kwargs)

    with closing(Runtime.create(root=runtime.store.root)) as writer:
        with monkeypatch.context() as fault:
            fault.setattr(self_models, "_complete_quality_weakness_records", pause)
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(build_self_model, runtime, scope=SCOPE, limit=1, persist=False)
                assert selected.wait(timeout=5)
                try:
                    reopened = ingest(writer, _failed_recall_report(payload_bytes=300_000))["created_record_ids"][0]
                finally:
                    continue_read.set()
                model = future.result(timeout=5)
        assert model["quality_closure"]["ok"] is True
        assert not source_refs(model)  # A coherent snapshot before the new failure.
        assert source_refs(build_self_model(runtime, scope=SCOPE, limit=1, persist=False)) == {reopened}


def test_concurrent_identical_failures_have_one_durable_gap(runtime, monkeypatch):
    native = gaps._gap_record
    entered = Event()
    mutex = Lock()
    calls = 0

    def pause_first(*args, **kwargs):
        nonlocal calls
        with mutex:
            calls += 1
            first = calls == 1
        if first:
            entered.wait(0.25)
        else:
            entered.set()
        return native(*args, **kwargs)

    monkeypatch.setattr(gaps, "_gap_record", pause_first)
    barrier = Barrier(2)
    def run():
        barrier.wait(timeout=5)
        return ingest(runtime, _failed_recall_report())
    with ThreadPoolExecutor(max_workers=2) as pool:
        reports = list(pool.map(lambda _: run(), range(2)))
    assert sum(report["created_count"] for report in reports) == 1
    assert sum(report["deduplicated_count"] for report in reports) == 1
    assert len(quality_records(runtime)) == 1
    assert ingest(runtime, {"quality_gate": {"ok": True}})["resolved_count"] == 1


def test_concurrent_pass_and_new_failure_do_not_fork_the_current_chain(runtime, monkeypatch):
    original_id = ingest(runtime, _failed_recall_report())["created_record_ids"][0]
    native = gaps._resolution_record
    resolving = Event()
    allow_resolution = Event()
    failure_finished = Event()

    def pause_resolution(*args, **kwargs):
        resolving.set()
        assert allow_resolution.wait(timeout=5)
        return native(*args, **kwargs)

    monkeypatch.setattr(gaps, "_resolution_record", pause_resolution)
    def fail_again():
        try:
            return ingest(runtime, _failed_recall_report(payload_bytes=300_000))
        finally:
            failure_finished.set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        passed = pool.submit(ingest, runtime, {"quality_gate": {"ok": True}})
        assert resolving.wait(timeout=5)
        failed = pool.submit(fail_again)
        failure_finished.wait(0.25)
        allow_resolution.set()
        pass_report, failure_report = passed.result(timeout=5), failed.result(timeout=5)
    records = quality_records(runtime)
    referenced = {record.content.get(key) for record in records
                  for key in ("supersedes_gap_id", "resolves_gap_id") if record.content.get(key)}
    tips = [record for record in records if record.record_id not in referenced]
    assert len(tips) == 1
    assert tips[0].record_id == failure_report["created_record_ids"][0]
    assert tips[0].content["supersedes_gap_id"] == pass_report["resolved_record_ids"][0]
    assert original_id in referenced
