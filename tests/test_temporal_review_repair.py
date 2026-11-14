"""Focused review-repair controls; run only in the parent's OS containment."""
from copy import deepcopy
from dataclasses import fields

import pytest

from eimemory.api.memory import MemoryAPI
from eimemory.contracts.temporal_facts import TemporalFactAccess, digest, family_id_for
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.models.source_partitions import normalize_source_id
from eimemory.storage.record_replay import scope_move_payload
from test_temporal_facts import (
    FEB, JAN, SCOPE, access, counts, env, fact, read, temporal, values, write,
)


def table_state(db):
    return tuple(tuple(tuple(row) for row in db.execute(
        "SELECT * FROM " + table + " ORDER BY 1").fetchall())
        for table in ("records", "memory_edges", "export_outbox"))


@pytest.mark.parametrize("protected_location", ["previous", "destination"])
def test_scope_rewrite_rejection_preserves_caller_transaction(env, protected_location):
    api, store, evidence = env
    a = write(api, evidence, "transaction-a")
    r = write(api, evidence, "transaction-r", None, operation="retraction",
              targets=[a.record_id], expected=a.record_id)
    moved_scope = ScopeRef.from_dict({**SCOPE, "user_id": "moved"})
    changed = deepcopy(r)
    changed.meta, changed.kind, changed.status = {}, "memory", "active"
    if protected_location == "previous":
        previous_scope, changed.scope = r.scope, moved_scope
    else:
        changed.scope = moved_scope
        store.append(changed)
        previous_scope, changed.scope = moved_scope, r.scope
    with store._lock:
        db = store.sqlite
        db.execute("BEGIN IMMEDIATE")
        try:
            before = table_state(db)
            with pytest.raises(ValueError, match="temporal_evidence_append_only"):
                db.rewrite(changed, previous_scope=previous_scope, commit=False)
            assert db.in_transaction  # The caller still owns its transaction.
            assert table_state(db) == before
        finally:
            db.rollback()
    assert values(read(api, FEB)) == []


def test_legacy_scope_rewrite_retry_and_recovery_remain_supported(env):
    _, store, evidence = env
    original = store.get_by_id(evidence, scope=ScopeRef.from_dict(SCOPE))
    changed = deepcopy(original)
    changed.scope.user_id = "legacy-moved"
    changed.summary = "A deliberate legacy scope move."
    store.rewrite(changed, previous_scope=original.scope)
    assert store.get_by_id(evidence, scope=original.scope) is None
    assert store.get_by_id(evidence, scope=changed.scope).to_dict() == changed.to_dict()
    store.rewrite(changed, previous_scope=original.scope)
    assert store.flush_exports()["remaining"] == 0
    report = store.rebuild_sqlite_from_jsonl(replace=True)
    assert report["ok"], report
    assert store.get_by_id(evidence, scope=original.scope) is None
    assert store.get_by_id(evidence, scope=changed.scope).to_dict() == changed.to_dict()


@pytest.mark.parametrize("replace", [False, True])
@pytest.mark.parametrize("protected_location", ["previous", "destination"])
def test_scope_move_recovery_cannot_mutate_temporal_revision(env, replace, protected_location):
    api, store, evidence = env
    a = write(api, evidence, "recovery-a")
    r = write(api, evidence, "recovery-r", None, operation="retraction",
              targets=[a.record_id], expected=a.record_id)
    changed = deepcopy(r)
    changed.meta, changed.kind, changed.status = {}, "memory", "active"
    changed.scope.user_id = "recovery-moved"
    if protected_location == "previous":
        previous_scope = r.scope
    else:
        store.append(changed)
        previous_scope, changed.scope = changed.scope, deepcopy(r.scope)
    assert store.flush_exports()["remaining"] == 0
    store.log.append_payload(scope_move_payload(changed, previous_scope))
    before = store.read_consistent(table_state)
    report = store.rebuild_sqlite_from_jsonl(replace=replace)
    assert report["ok"] is False
    assert "temporal_evidence_append_only" in str(report["errors"])
    assert store.read_consistent(table_state) == before
    assert values(read(api, FEB)) == []


@pytest.mark.parametrize("source_id", ["SYNTHETIC", "ｓynthetic", "Key", "straß"],
                         ids=["casefold", "nfkc", "compatibility-casefold", "expanding-casefold"])
def test_binding_rejects_every_normalization_change(source_id):
    assert normalize_source_id(source_id) != source_id
    with pytest.raises(ValueError, match="source"):
        access(source_id=source_id)


@pytest.mark.parametrize("source_id", ["bad/source", "a" * 129], ids=["invalid-separator", "overlong"])
def test_binding_uses_existing_source_slug_validation(source_id):
    with pytest.raises(ValueError, match="source"):
        access(source_id=source_id)


@pytest.mark.parametrize("replace", [False, True])
def test_temporal_recovery_rejects_source_literal_normalization(env, replace):
    api, store, evidence = env
    a = write(api, evidence, "source-recovery")
    assert store.flush_exports()["remaining"] == 0
    payload = a.to_dict()
    payload["source_id"] = "ＳYNTHETIC"
    store.log.append_payload(payload)
    before = store.read_consistent(table_state)
    report = store.rebuild_sqlite_from_jsonl(replace=replace)
    assert report["ok"] is False
    assert "temporal_source_binding_invalid" in str(report["errors"])
    assert store.read_consistent(table_state) == before
    assert values(read(api, JAN)) == ["A"]


@pytest.mark.parametrize("source_id", ["synthetic", "team.alpha-1", "tenant_01"])
def test_canonical_binding_preserves_identity_and_pure_admission(env, source_id):
    _, store, _ = env
    evidence = RecordEnvelope.create(kind="memory", title="Canonical source control",
        scope=ScopeRef.from_dict(SCOPE), source="test.evidence", source_id=source_id,
        meta={"force_capture": True})
    store.append(evidence)
    original = store.get_by_id(evidence.record_id, scope=evidence.scope)
    received = []

    def admit(record):
        assert record.to_dict() == original.to_dict()
        assert record is not original and record.scope is not original.scope
        received.append(record)
        return True

    binding = TemporalFactAccess(scope=evidence.scope, source="test.structured",
        source_id=source_id, authority="operator", admit_evidence=admit)
    api = MemoryAPI(store, enable_temporal_facts=True, temporal_fact_access=binding)
    a = write(api, evidence.record_id, "canonical")
    assert a.source_id == binding.source_id == source_id
    assert temporal(a)["family_id"] == family_id_for(binding, fact())
    assert temporal(a)["evidence_digests"] == {evidence.record_id: digest(original.to_dict())}
    before = counts(store)
    assert values(read(api, JAN)) == ["A"]
    assert read(api, JAN)["partition"]["source_id"] == source_id
    assert write(api, evidence.record_id, "canonical").to_dict() == a.to_dict()
    assert counts(store) == before
    assert len(received) >= 3
    assert len({id(record) for record in received}) == len(received)


@pytest.mark.parametrize("cross_partition", [False, True])
@pytest.mark.parametrize("stage", ["recall", "duplicate"])
def test_callback_substitution_rejected_when_recertifying(env, cross_partition, stage):
    api, store, evidence = env
    a = write(api, evidence, "recertify")
    scope = ScopeRef.from_dict({**SCOPE, "user_id": "other"} if cross_partition else SCOPE)
    other = RecordEnvelope.create(kind="memory", title="Other persisted evidence",
        scope=scope, source="test.evidence", source_id="synthetic",
        meta={"force_capture": True})
    store.append(other)

    def substitute(record):
        for field in fields(record):
            setattr(record, field.name, deepcopy(getattr(other, field.name)))
        return True

    binding = TemporalFactAccess(scope=ScopeRef.from_dict(SCOPE), source="test.structured",
        source_id="synthetic", authority="operator", admit_evidence=substitute)
    bad_api = MemoryAPI(store, enable_temporal_facts=True, temporal_fact_access=binding)
    before = store.read_consistent(table_state)
    if stage == "recall":
        result = read(bad_api, JAN)
        assert result["status"] == "unavailable" and result["complete"] is False
        assert result["items"] == []
    else:
        with pytest.raises(ValueError, match="evidence_admission_mutated"):
            write(bad_api, evidence, "recertify")
    assert store.read_consistent(table_state) == before
    assert temporal(a)["evidence_digests"] != {evidence: digest(other.to_dict())}
    assert values(read(api, JAN)) == ["A"]


def test_nested_callback_mutation_is_rejected_even_without_identity_change(env):
    _, store, evidence = env

    def mutate(record):
        record.meta["callback_annotation"] = {"allowed": True}
        return True

    api = MemoryAPI(store, enable_temporal_facts=True, temporal_fact_access=TemporalFactAccess(
        scope=ScopeRef.from_dict(SCOPE), source="test.structured", source_id="synthetic",
        authority="operator", admit_evidence=mutate))
    before = store.read_consistent(table_state)
    with pytest.raises(ValueError, match="evidence_admission_mutated"):
        write(api, evidence, "mutated-meta")
    assert store.read_consistent(table_state) == before
