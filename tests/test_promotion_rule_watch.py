"""Local-only direct rule observations; no production attribution is inferred."""
from __future__ import annotations

import json
import os
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from release_report_fixtures import promotion_health_receipt
from eimemory.api.runtime import Runtime
from eimemory.governance.promotion import promotion_watch as pw
from eimemory.governance.promotion.policy_rollout import policy_version
from eimemory.governance.promotion.rollout_lifecycle import is_executed_rollback_ledger_record
from eimemory.models.records import RecordEnvelope, ScopeRef

SCOPE = ScopeRef(tenant_id="audit", agent_id="synthetic", workspace_id="local", user_id="user")


@pytest.fixture(autouse=True)
def local_only(monkeypatch, tmp_path):
    def reject(*args, **kwargs):
        raise AssertionError("External operations are outside this synthetic contract")
    monkeypatch.setattr(subprocess, "Popen", reject)
    monkeypatch.setattr(os, "system", reject)
    monkeypatch.setattr(socket, "create_connection", reject)
    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "bind", reject)
    monkeypatch.setenv("EIMEMORY_ROOT", str(tmp_path))
    monkeypatch.setenv("EIMEMORY_CONFIG_DIR", str(tmp_path / "empty-config"))
    monkeypatch.delenv("EIMEMORY_CONFIG_PATH", raising=False)


@pytest.fixture
def runtime(tmp_path):
    value = Runtime.create(root=tmp_path)
    try:
        yield value
    finally:
        value.close()


def append(runtime, kind, *, status="shadow", scope=SCOPE, source_id="default", record_id=None):
    record = RecordEnvelope.create(kind=kind, title="Synthetic watch", scope=scope,
                                   source_id=source_id, status=status,
                                   content={"preserve": "content"}, meta={"preserve": "meta"})
    if record_id:
        record.record_id = record_id
    return runtime.store.append(record)


def setup_watch(runtime, kind="rule", *, scope=SCOPE, source_id="default", record_id="artifact-local"):
    candidate = append(runtime, "capability_candidate", status="shadow_observe", scope=scope, source_id="candidate-import")
    request = append(runtime, "promotion_request", status="shadow_observe", scope=scope, source_id="request-import")
    if kind == "intent_pattern":
        runtime.store.upsert_intent_pattern({"id": record_id, "pattern": "synthetic watch", "status": "shadow"}, scope=scope)
        artifact = None
    else:
        artifact = append(runtime, kind, scope=scope, source_id=source_id, record_id=record_id)
    result = pw.initialize_promotion_watch(runtime, candidate=candidate, scope=scope,
        promotion_request_id=request.record_id, applied_pattern_ids=[record_id])
    assert result["ok"], result
    return candidate, request, artifact


def observe(runtime, event="event-0", *, record_id="artifact-local", scope=SCOPE, **kwargs):
    values = dict(hit=True, improved=True, outcome="good")
    values.update(kwargs)
    return pw.record_promotion_observation(runtime, pattern_id=record_id, scope=scope, event_id=event, **values)


def exact(runtime, record):
    return runtime.store.get_by_exact_ref(record.record_id, scope=record.scope, source_id=record.source_id)


def stored_watch(runtime, artifact, *, record_id="artifact-local", scope=SCOPE):
    if artifact is not None:
        record = exact(runtime, artifact)
        return record.status, record.content["post_promotion_watch"]
    payload = pw._load_pattern(runtime, pattern_id=record_id, scope=scope)
    return payload["status"], payload["post_promotion_watch"]


def state(runtime):
    with runtime.store.locked() as sql:
        return {table: [tuple(row) for row in sql.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()]
                for table in ("records", "intent_patterns", "policy_rollout_ledger", "export_outbox")}


@pytest.mark.parametrize("kind,source", [("rule", "default"), ("rule", "imported"), ("intent_pattern", "default")])
def test_three_direct_observations_activate_and_export_linked_records(runtime, kind, source):
    candidate, request, artifact = setup_watch(runtime, kind, source_id=source)
    for index in range(3):
        result = observe(runtime, f"event-{index}")
        assert result["status"] == ("active" if index == 2 else "shadow_observe")
        assert stored_watch(runtime, artifact)[1]["observed_count"] == index + 1
    assert result["activated"] is True
    assert exact(runtime, candidate).status == "promoted"
    assert exact(runtime, request).status == "active"
    assert exact(runtime, request).content["post_promotion_status"] == "active"
    for rec in (candidate, request, artifact):
        if rec:
            assert exact(runtime, rec).content["preserve"] == "content"
            assert exact(runtime, rec).meta["preserve"] == "meta"
    ledgers = runtime.get_policy_rollout_ledger(scope=SCOPE, action="promoted_active", limit=10)
    assert ledgers[0]["details"]["observed_count"] == 3
    with runtime.store.locked() as sql:
        assert not sql.in_transaction
        assert not sql.pending_exports()
    assert len(stored_watch(runtime, artifact)[1]["observations"]) == 3


@pytest.mark.parametrize("kind", ["rule", "intent_pattern"])
@pytest.mark.parametrize("outcome,hit,improved,expected", [("bad", True, False, "rolled_back"), ("uncertain", False, False, "quarantined")])
def test_rule_and_native_pattern_failure_transitions(runtime, kind, outcome, hit, improved, expected):
    candidate, request, artifact = setup_watch(runtime, kind)
    for index in range(3):
        result = observe(runtime, f"event-{index}", outcome=outcome, hit=hit, improved=improved)
    assert result["status"] == expected
    assert stored_watch(runtime, artifact)[0] == expected
    assert exact(runtime, candidate).status == exact(runtime, request).status == expected
    ledgers = runtime.get_policy_rollout_ledger(scope=SCOPE, action=expected, limit=10)
    proof = ledgers[0]["details"]["rollback"]
    assert proof["execution_type"] == ("record_status_transition" if artifact else "intent_pattern_status_transition")
    # Rule rollback accounting is deliberately still outside this patch.
    assert is_executed_rollback_ledger_record(ledgers[0]) is (artifact is None)
    before = state(runtime)
    assert observe(runtime, "terminal-repeat")["status"] == expected
    assert state(runtime) == before


@pytest.mark.parametrize("kind", ["rule", "intent_pattern"])
def test_failure_rate_waits_for_three_and_continues_after_activation(runtime, kind):
    _, _, artifact = setup_watch(runtime, kind)
    assert observe(runtime, "a", outcome="bad")["status"] == "shadow_observe"
    assert observe(runtime, "b")["status"] == "shadow_observe"
    assert observe(runtime, "c")["status"] == "rolled_back"
    assert stored_watch(runtime, artifact)[1]["failure_count"] == 1
    setup_watch(runtime, kind, record_id="another")
    for index in range(3):
        result = observe(runtime, f"good-{index}", record_id="another")
    assert result["status"] == "active"
    result = observe(runtime, "active-failure", record_id="another", outcome="bad")
    assert result["status"] == "rolled_back"
    assert result["watch"]["failure_rate"] == 0.25


@pytest.mark.parametrize("observed,failures,expected", [(19, 1, "active"), (9, 1, "quarantined"), (4, 1, "rolled_back")])
def test_rule_threshold_boundaries(runtime, observed, failures, expected):
    _, _, artifact = setup_watch(runtime)
    current = exact(runtime, artifact)
    watch = current.content["post_promotion_watch"]
    watch.update(observed_count=observed, hit_count=observed, improvement_count=observed,
                 failure_count=failures, required_observations=observed + 1)
    current.meta["post_promotion_watch"] = dict(watch)
    runtime.store.rewrite(current)
    result = observe(runtime)
    assert result["status"] == expected
    assert result["watch"]["failure_rate"] == round(failures / (observed + 1), 6)


@pytest.mark.parametrize("kind", ["rule", "intent_pattern"])
def test_event_dedup_keeps_bounded_three_event_guarantee(runtime, kind):
    _, _, artifact = setup_watch(runtime, kind)
    observe(runtime, "event-0")
    result = observe(runtime, "event-0", outcome="bad")
    assert result["watch"]["observed_count"] == 1
    assert result["watch"]["failure_count"] == 0
    for index in range(1, 4):
        observe(runtime, f"event-{index}")
    assert stored_watch(runtime, artifact)[1]["observed_count"] == 4
    result = observe(runtime, "event-0")
    assert result["watch"]["observed_count"] == 5  # evicted event is outside the guarantee
    assert len(result["watch"]["observations"]) == 3


def test_concurrent_duplicate_rule_events_count_once(runtime):
    _, _, artifact = setup_watch(runtime)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: observe(runtime), range(2)))
    assert all(item["status"] == "shadow_observe" for item in results)
    assert stored_watch(runtime, artifact)[1]["observed_count"] == 1


@pytest.mark.parametrize("mismatch", ["scope", "global_fallback", "source", "stored_source_marker"])
def test_wrong_rule_partition_does_not_write_anything(runtime, mismatch):
    scope = replace(SCOPE, user_id="") if mismatch == "global_fallback" else SCOPE
    _, _, artifact = setup_watch(runtime, scope=scope, source_id="imported")
    kwargs = {}
    if mismatch == "scope":
        kwargs["scope"] = replace(SCOPE, tenant_id="other")
    elif mismatch == "source":
        kwargs["source_id"] = "other"
    elif mismatch == "stored_source_marker":
        current = exact(runtime, artifact)
        current.content["post_promotion_watch"]["artifact_source_id"] = "other"
        runtime.store.rewrite(current)
    before = state(runtime)
    assert observe(runtime, **kwargs)["status"] == ("blocked" if mismatch == "stored_source_marker" else "not_found")
    assert state(runtime) == before


@pytest.mark.parametrize("field,value", [("candidate_source_id", "wrong"), ("promotion_request_source_id", "wrong"), ("candidate_id", "missing"), ("promotion_request_id", "missing")])
def test_wrong_link_identity_rolls_back_terminal_observation(runtime, field, value):
    _, _, artifact = setup_watch(runtime)
    observe(runtime, "first"); observe(runtime, "second")
    current = exact(runtime, artifact)
    current.content["post_promotion_watch"][field] = value
    runtime.store.rewrite(current)
    before = state(runtime)
    result = observe(runtime, "third")
    assert result["status"] == "blocked"
    assert result["reason"] == "watch_rule_link_identity_conflict"
    assert state(runtime) == before


def test_explicit_source_selects_rule_without_changing_colliding_pattern(runtime):
    _, _, artifact = setup_watch(runtime, source_id="imported")
    runtime.store.upsert_intent_pattern({"id": artifact.record_id, "pattern": "collision", "status": "shadow"}, scope=SCOPE)
    before = pw._load_pattern(runtime, pattern_id=artifact.record_id, scope=SCOPE)
    for index in range(3):
        result = observe(runtime, f"event-{index}", source_id="imported")
    assert result["status"] == "active"
    assert pw._load_pattern(runtime, pattern_id=artifact.record_id, scope=SCOPE) == before


def test_collision_blocks_default_dispatch_and_native_version_contract_still_works(runtime):
    _, _, rule = setup_watch(runtime)
    runtime.store.upsert_intent_pattern({"id": rule.record_id, "pattern": "collision", "status": "shadow"}, scope=SCOPE)
    before = state(runtime)
    result = observe(runtime)
    assert result["status"] == "blocked"
    assert result["reason"] == "watch_artifact_identity_ambiguous"
    assert state(runtime) == before
    pattern_id = "native-only"
    setup_watch(runtime, "intent_pattern", record_id=pattern_id)
    payload = pw._load_pattern(runtime, pattern_id=pattern_id, scope=SCOPE)
    version = policy_version(payload)
    result = observe(runtime, record_id=pattern_id, details={"policy_version_ids": {pattern_id: version}})
    assert result["status"] == "shadow_observe"
    assert pw._load_pattern(runtime, pattern_id=pattern_id, scope=SCOPE)["post_promotion_watch"]["observed_count"] == 1
    before = state(runtime)
    result = observe(runtime, "stale", record_id=pattern_id, details={"policy_version_ids": {pattern_id: "wrong"}})
    assert result["reason"] == "event_policy_version_conflict"
    assert state(runtime) == before


def test_uninitialized_rule_and_pattern_version_evidence_fail_closed(runtime):
    artifact = append(runtime, "rule", record_id="uninitialized")
    before = state(runtime)
    assert observe(runtime, record_id=artifact.record_id)["reason"] == "rule_watch_not_initialized"
    assert state(runtime) == before
    _, _, artifact = setup_watch(runtime)
    before = state(runtime)
    result = observe(runtime, details={"policy_version_ids": {artifact.record_id: "not-a-rule-version"}})
    assert result["reason"] == "rule_policy_version_unbound"
    assert state(runtime) == before


@pytest.mark.parametrize("failure", ["rule_write", "linked_write", "lifecycle_insert", "shadow_ledger_insert", "record_outbox", "commit"])
@pytest.mark.parametrize("outcome", ["good", "bad"])
def test_failure_rolls_back_rule_links_ledgers_outbox_and_retries(runtime, monkeypatch, failure, outcome):
    candidate, request, artifact = setup_watch(runtime)
    observe(runtime, "first"); observe(runtime, "second")
    before = state(runtime)
    with monkeypatch.context() as patch:
        if failure in {"rule_write", "linked_write"}:
            target, name = runtime.store.sqlite, "rewrite"
        elif failure in {"lifecycle_insert", "shadow_ledger_insert"}:
            target, name = runtime.store.sqlite, "_record_policy_rollout_ledger"
        elif failure == "record_outbox":
            target, name = runtime.store, "_enqueue_record_exports"
        else:
            target, name = runtime.store.sqlite, "commit"
        original = getattr(target, name)
        count = 0
        def fail(*args, **kwargs):
            nonlocal count
            count += 1
            if failure != "commit":
                result = original(*args, **kwargs)
            fail_at = 2 if failure in {"linked_write", "shadow_ledger_insert"} else 1
            if count == fail_at:
                raise RuntimeError("synthetic_injection")
            return result
        patch.setattr(target, name, fail)
        with pytest.raises(RuntimeError, match="synthetic_injection|watch_rule_lifecycle_write_failed"):
            observe(runtime, "third", outcome=outcome)
    assert state(runtime) == before
    assert not runtime.store.sqlite.in_transaction
    result = observe(runtime, "third", outcome=outcome)
    assert result["status"] == ("active" if outcome == "good" else "rolled_back")
    assert stored_watch(runtime, artifact)[1]["observed_count"] == 3


def test_caller_transaction_is_neither_committed_nor_rolled_back(runtime):
    setup_watch(runtime)
    with runtime.store.locked() as sql:
        sql.execute("CREATE TABLE synthetic_sentinel(value TEXT)"); sql.commit()
        sql.execute("BEGIN"); sql.execute("INSERT INTO synthetic_sentinel VALUES ('caller')")
        changes = sql.conn.total_changes
        with pytest.raises(RuntimeError, match="record_mutation_requires_own_transaction"):
            observe(runtime)
        assert sql.in_transaction and sql.conn.total_changes == changes
        assert sql.execute("SELECT value FROM synthetic_sentinel").fetchone()[0] == "caller"
        sql.rollback()
        assert sql.execute("SELECT COUNT(*) FROM synthetic_sentinel").fetchone()[0] == 0


def test_export_failure_retains_committed_record_and_ledger_outbox(runtime, monkeypatch, tmp_path):
    candidate, request, artifact = setup_watch(runtime)
    observe(runtime, "first"); observe(runtime, "second")
    with monkeypatch.context() as patch:
        def fail(*args, **kwargs):
            raise OSError("synthetic export failure")
        patch.setattr(runtime.store, "_flush_committed_exports", fail)
        assert observe(runtime, "third")["status"] == "active"
        with runtime.store.locked() as sql:
            pending = sql.pending_exports()
            assert [item["stream"] for item in pending].count("records") == 3
            assert [item["stream"] for item in pending].count("policy_rollout_ledger") == 2
            assert not sql.in_transaction
    runtime.store.flush_exports()
    with runtime.store.locked() as sql:
        assert not sql.pending_exports()
    ledger_path = runtime.store.auxiliary_log_dir / "policy_rollout_ledger.jsonl"
    exports = [json.loads(line) for line in ledger_path.read_text().splitlines()]
    assert any(item["payload"]["action_type"] == "promoted_active" for item in exports)
    records = [json.loads(line) for line in (runtime.store.root / "records.jsonl").read_text().splitlines()]
    for original in (candidate, request, artifact):
        exported = [item for item in records if item.get("record_id") == original.record_id][-1]
        exported.pop("_operation_id", None)
        exported.pop("_payload_digest", None)
        assert exported == exact(runtime, original).to_dict()
    runtime.close()
    reopened = Runtime.create(root=tmp_path)
    try:
        assert stored_watch(reopened, artifact)[0] == "active"
        assert exact(reopened, candidate).status == "promoted"
        assert exact(reopened, request).status == "active"
    finally:
        reopened.close()


@pytest.mark.parametrize("target", ["rule", "candidate", "request"])
@pytest.mark.parametrize("status", ["disabled", "rejected", "blocked", "rolled_back", "quarantined"])
def test_terminal_review_is_not_overwritten(runtime, target, status):
    candidate, request, artifact = setup_watch(runtime)
    observe(runtime, "first"); observe(runtime, "second")
    record = exact(runtime, {"rule": artifact, "candidate": candidate, "request": request}[target])
    record.status = status
    runtime.store.rewrite(record)
    before = state(runtime)
    result = observe(runtime, "third")
    if target == "rule" and status in {"rolled_back", "quarantined"}:
        assert result["status"] == status
    else:
        assert result["ok"] is False and result["status"] == "blocked"
        assert "state_conflict" in result["reason"]
    assert state(runtime) == before


@pytest.mark.parametrize("fault", ["request_content", "request_meta", "request_artifacts", "candidate_artifacts"])
def test_linkage_evidence_conflict_cannot_update_unrelated_records(runtime, fault):
    candidate, request, artifact = setup_watch(runtime)
    observe(runtime, "first"); observe(runtime, "second")
    record = exact(runtime, candidate if fault == "candidate_artifacts" else request)
    if fault == "candidate_artifacts":
        record.meta["applied_artifact_ids"] = ["unrelated-rule"]
    elif fault == "request_artifacts":
        record.content["side_effect"] = {"applied_artifact_ids": ["unrelated-rule"]}
    elif fault == "request_content":
        record.content["candidate_id"] = "unrelated-candidate"
    else:
        record.meta["candidate_id"] = "unrelated-candidate"
    runtime.store.rewrite(record)
    before = state(runtime)
    assert observe(runtime, "third")["reason"] == "watch_rule_link_identity_conflict"
    assert state(runtime) == before


def test_wrong_watch_binding_is_not_observed(runtime):
    _, _, artifact = setup_watch(runtime)
    current = exact(runtime, artifact)
    current.content["post_promotion_watch"]["pattern_id"] = "unrelated-rule"
    runtime.store.rewrite(current)
    before = state(runtime)
    assert observe(runtime)["status"] == "blocked"
    assert state(runtime) == before


def test_repeat_initialization_preserves_counts_and_active_status(runtime):
    candidate, request, artifact = setup_watch(runtime)
    for index in range(3):
        observe(runtime, f"event-{index}")
        old_status, old_watch = stored_watch(runtime, artifact)
        result = pw.initialize_promotion_watch(runtime, candidate=candidate, scope=SCOPE,
            promotion_request_id=request.record_id, applied_pattern_ids=[artifact.record_id])
        assert result["ok"]
        assert stored_watch(runtime, artifact) == (old_status, old_watch)


def test_initialization_rejects_cross_scope_candidate(runtime):
    candidate, request, artifact = setup_watch(runtime)
    wrong = replace(candidate, scope=replace(SCOPE, tenant_id="other"))
    before = state(runtime)
    result = pw.initialize_promotion_watch(runtime, candidate=wrong, scope=SCOPE,
        promotion_request_id=request.record_id, applied_pattern_ids=[artifact.record_id])
    assert not result["ok"]
    assert state(runtime) == before


def test_source_selector_uses_existing_normalization(runtime):
    setup_watch(runtime, source_id="imported")
    assert observe(runtime, source_id="IMPORTED")["status"] == "shadow_observe"


def test_synthetic_public_rule_promotion_reaches_direct_observation(runtime):
    from eimemory.governance.promotion import promotion_manager as pm
    candidate = runtime.store.append(RecordEnvelope.create(kind="capability_candidate", title="Synthetic rule", scope=SCOPE,
        status="candidate", content={"promotion_target": "memory_rule", "target_capability": "memory.recall",
        "candidate_patch": {"task_type": "memory.recall", "retrieval_policy": {"strategy": "structured"}}}, meta={"authority_tier": "L1"}))
    result = pm.promote_candidate(runtime, candidate_id=candidate.record_id, scope=SCOPE,
        eval_result={"verdict": "pass", "scores": {"safety": 1.0, "regression": 1.0}},
        health=promotion_health_receipt())
    assert result["ok"] is True, result
    rule_id = result["applied_artifact_ids"][0]
    for index in range(3):
        observation = observe(runtime, f"public-{index}", record_id=rule_id)
    assert observation["status"] == "active"


def test_legacy_unique_watch_sources_are_bound_on_next_observation(runtime):
    candidate, request, artifact = setup_watch(runtime, source_id="imported")
    current = exact(runtime, artifact)
    for field in ("artifact_source_id", "candidate_source_id", "promotion_request_source_id"):
        current.content["post_promotion_watch"].pop(field)
    current.meta["post_promotion_watch"] = dict(current.content["post_promotion_watch"])
    runtime.store.rewrite(current)
    assert observe(runtime)["status"] == "shadow_observe"
    watch = stored_watch(runtime, artifact)[1]
    assert watch["artifact_source_id"] == "imported"
    assert watch["candidate_source_id"] == candidate.source_id
    assert watch["promotion_request_source_id"] == request.source_id


def test_rule_observation_never_uses_expanded_or_pooled_record_lookup(runtime, monkeypatch):
    setup_watch(runtime)
    def reject(*args, **kwargs):
        raise AssertionError("Must use the transaction-local exact reader")
    monkeypatch.setattr(runtime.store, "get_by_id", reject)
    monkeypatch.setattr(runtime.store, "list_records", reject)
    for index in range(3):
        result = observe(runtime, f"event-{index}")
    assert result["status"] == "active"


def test_stale_initialization_cannot_overwrite_newer_watch_counts(runtime):
    setup_watch(runtime)
    stale = pw._load_rule_artifact(runtime, artifact_id="artifact-local", scope=SCOPE)
    observe(runtime)
    before = state(runtime)
    with pytest.raises(RuntimeError, match="watch_rule_state_conflict"):
        pw._write_watch_artifact(runtime, stale, scope=SCOPE)
    assert state(runtime) == before


@pytest.mark.parametrize("failure", ["lifecycle_rejected", "ledger_rejected", "ledger_invalid"])
def test_rule_ledger_rejected_result_is_atomic_failure(runtime, monkeypatch, failure):
    setup_watch(runtime)
    before = state(runtime)
    with monkeypatch.context() as patch:
        if failure == "lifecycle_rejected":
            patch.setattr(pw, "record_lifecycle_event", lambda *args, **kwargs: {"ok": False})
        else:
            original = runtime.store.sqlite._record_policy_rollout_ledger
            count = 0
            def fail(*args, **kwargs):
                nonlocal count
                count += 1
                result = original(*args, **kwargs)
                return result if count == 1 else ({"ok": False} if failure == "ledger_rejected" else {})
            patch.setattr(runtime.store.sqlite, "_record_policy_rollout_ledger", fail)
        with pytest.raises(RuntimeError, match="watch_rule_(lifecycle|ledger)_write_failed"):
            observe(runtime)
    assert state(runtime) == before


def test_ambiguous_imported_rule_identity_does_not_guess(runtime, monkeypatch):
    _, _, artifact = setup_watch(runtime, source_id="left")
    # Ordinary record writes cannot create same-scope/same-ID source collisions.
    # Model only defensive handling of a noncanonical historical/imported row.
    key = runtime.store.sqlite._storage_key
    with monkeypatch.context() as patch:
        patch.setattr(runtime.store.sqlite, "_storage_key", lambda rec: key(rec) + ":imported" if rec.source_id == "right" else key(rec))
        append(runtime, "rule", source_id="right", record_id=artifact.record_id)
    before = state(runtime)
    assert observe(runtime)["status"] == "blocked"
    assert state(runtime) == before


def test_same_rule_id_in_other_scope_stays_unchanged(runtime):
    _, _, artifact = setup_watch(runtime)
    other_scope = replace(SCOPE, tenant_id="other")
    other_candidate, other_request, other = setup_watch(runtime, scope=other_scope)
    before = [exact(runtime, item).to_dict() for item in (other_candidate, other_request, other)]
    for index in range(3):
        observe(runtime, f"event-{index}")
    assert stored_watch(runtime, artifact)[0] == "active"
    assert [exact(runtime, item).to_dict() for item in (other_candidate, other_request, other)] == before


@pytest.mark.parametrize("fault", ["source_marker", "artifact_marker", "ambiguous_import", "initialize", "pattern_empty", "pattern_corrupt", "rule_projection"])
def test_invalid_rule_identity_never_falls_back_to_colliding_pattern(runtime, monkeypatch, fault):
    candidate, request, artifact = setup_watch(runtime, source_id="left")
    runtime.store.upsert_intent_pattern({"id": artifact.record_id, "pattern": "collision", "status": "shadow"}, scope=SCOPE)
    if fault == "ambiguous_import":
        # Defensive historical/imported fixture; ordinary append rejects it.
        key = runtime.store.sqlite._storage_key
        with monkeypatch.context() as patch:
            patch.setattr(runtime.store.sqlite, "_storage_key", lambda rec: key(rec) + ":imported" if rec.source_id == "right" else key(rec))
            append(runtime, "rule", source_id="right", record_id=artifact.record_id)
    elif fault in {"pattern_empty", "pattern_corrupt", "rule_projection"}:
        with runtime.store.locked() as sql:
            if fault == "rule_projection":
                sql.execute("UPDATE records SET status='inconsistent' WHERE record_id=?", (artifact.record_id,))
            else:
                payload = "{}" if fault == "pattern_empty" else "not-json"
                sql.execute("UPDATE intent_patterns SET payload_json=? WHERE id=?", (payload, artifact.record_id))
            sql.commit()
    elif fault != "initialize":
        current = exact(runtime, artifact)
        field = "artifact_source_id" if fault == "source_marker" else "pattern_id"
        current.content["post_promotion_watch"][field] = "conflicting-identity"
        runtime.store.rewrite(current)
    before = state(runtime)
    if fault == "initialize":
        result = pw.initialize_promotion_watch(runtime, candidate=candidate, scope=SCOPE,
            promotion_request_id=request.record_id, applied_pattern_ids=[artifact.record_id])
    else:
        result = observe(runtime)
    assert result["ok"] is False
    assert state(runtime) == before


@pytest.mark.parametrize("kind", ["rule", "intent_pattern"])
def test_initialization_rechecks_namespace_inside_write_transaction(runtime, monkeypatch, kind):
    candidate = append(runtime, "capability_candidate", status="shadow_observe")
    request = append(runtime, "promotion_request", status="shadow_observe")
    if kind == "rule":
        append(runtime, "rule", record_id="artifact-local")
    else:
        runtime.store.upsert_intent_pattern({"id": "artifact-local", "pattern": "first", "status": "shadow"}, scope=SCOPE)
    write = pw._write_watch_artifact
    after_insertion = None
    def interleave(*args, **kwargs):
        nonlocal after_insertion
        if kind == "rule":
            runtime.store.upsert_intent_pattern({"id": "artifact-local", "pattern": "concurrent", "status": "shadow"}, scope=SCOPE)
        else:
            append(runtime, "rule", record_id="artifact-local")
        after_insertion = state(runtime)
        return write(*args, **kwargs)
    monkeypatch.setattr(pw, "_write_watch_artifact", interleave)
    result = pw.initialize_promotion_watch(runtime, candidate=candidate, scope=SCOPE,
        promotion_request_id=request.record_id, applied_pattern_ids=["artifact-local"])
    assert result["ok"] is False
    assert result["identity_errors"]["artifact-local"] == "watch_artifact_identity_ambiguous"
    assert state(runtime) == after_insertion
    assert not runtime.store.sqlite.in_transaction


@pytest.mark.parametrize("kind", ["rule", "intent_pattern"])
def test_initialization_does_not_commit_or_rollback_caller_transaction(runtime, kind):
    candidate, request, artifact = setup_watch(runtime, kind)
    with runtime.store.locked() as sql:
        sql.execute("CREATE TABLE init_sentinel(value TEXT)"); sql.commit()
        sql.execute("BEGIN"); sql.execute("INSERT INTO init_sentinel VALUES ('caller')")
        changes = sql.conn.total_changes
        before = state(runtime)
        with pytest.raises(RuntimeError, match="requires_own_transaction"):
            pw.initialize_promotion_watch(runtime, candidate=candidate, scope=SCOPE,
                promotion_request_id=request.record_id, applied_pattern_ids=["artifact-local"])
        assert sql.in_transaction and sql.conn.total_changes == changes
        assert state(runtime) == before
        assert sql.execute("SELECT value FROM init_sentinel").fetchone()[0] == "caller"
        sql.rollback()
        assert sql.execute("SELECT COUNT(*) FROM init_sentinel").fetchone()[0] == 0


@pytest.mark.parametrize("kind", ["rule", "intent_pattern"])
def test_initialization_commit_failure_rolls_back_and_is_not_converted_to_identity_error(runtime, monkeypatch, kind):
    candidate, request, artifact = setup_watch(runtime, kind)
    before = state(runtime)
    with monkeypatch.context() as patch:
        def fail():
            raise OSError("synthetic initialization commit failure")
        patch.setattr(runtime.store.sqlite, "commit", fail)
        with pytest.raises(OSError, match="synthetic initialization commit failure"):
            pw.initialize_promotion_watch(runtime, candidate=candidate, scope=SCOPE,
                promotion_request_id=request.record_id, applied_pattern_ids=["artifact-local"])
    assert state(runtime) == before
    assert not runtime.store.sqlite.in_transaction
