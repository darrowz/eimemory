"""Synthetic exact-identity projector contract; intentionally no Runtime/providers."""
from __future__ import annotations

from dataclasses import asdict, replace
import hashlib
import json

import pytest

from eimemory.knowledge.compiler import compile_paper_knowledge
from eimemory.knowledge.extract import extract_paper_memory
from eimemory.knowledge.projectors import (
    PROJECTOR_SOURCE, PROJECTION_TYPE, PROJECTION_LINEAGE_SCHEMA, SUPPORT_LINEAGE_SCHEMA,
    project_operational_knowledge, stable_projection_id,
)
from eimemory.models.claim_cards import ClaimCard
from eimemory.models.records import RecordEnvelope, ScopeRef, TimeRef
from eimemory.storage.runtime_store import RuntimeStore

SCOPE = ScopeRef(tenant_id="audit", agent_id="agent", workspace_id="knowledge", user_id="owner")
TEXT = "The runtime memory policy must prefer tenant scoped verified operational records and enforce explicit source contracts."


@pytest.fixture
def store(tmp_path):
    value = RuntimeStore(tmp_path / "synthetic")
    try:
        yield value
    finally:
        value.close()


def claim(record_id="claim_native", *, source_id="default", scope=None, confidence=.95, status="active"):
    value = ClaimCard(claim_card_id=record_id, paper_source_id="paper_test", paper_extract_id="pex_test",
                      claim_text=TEXT, confidence=.95).to_record(scope=scope or SCOPE)
    value.source_id = source_id
    value.content["confidence"] = confidence
    value.meta["confidence"] = confidence
    value.status = status
    return value


def version_ref(record):
    # Independent producer-side reference construction; do not use the
    # implementation's helper to make both sides share the same mistake.
    digest = hashlib.sha256(json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True,
        default=str, separators=(",", ":")).encode()).hexdigest()
    return {"record_id": record.record_id, "kind": record.kind, "scope": asdict(record.scope),
            "source_id": record.source_id, "version_digest": digest}


def page(claims, *, record_id="page_explicit", source_id=None, scope=None, confidence=None):
    first = claims[0]
    content = {"summary": TEXT, "supporting_claim_ids": [c.record_id for c in claims],
               "supporting_claim_refs_schema": SUPPORT_LINEAGE_SCHEMA,
               "supporting_claim_refs": [version_ref(c) for c in claims]}
    if confidence is not None:
        content["confidence"] = confidence
    value = RecordEnvelope.create(kind="knowledge_page", title="Synthetic operational page", summary=TEXT,
        scope=scope or first.scope, source_id=source_id or first.source_id, content=content,
        source="synthetic.explicit-lineage")
    value.record_id = record_id
    return value


def memories(store, scope=SCOPE, source_ids=None):
    return store.list_records(kinds=["memory"], scope=scope, source_ids=source_ids, limit=100)


def projection(store, source):
    return store.get_by_exact_ref(stable_projection_id(source), scope=source.scope, source_id=source.source_id)


def test_native_default_legacy_id_and_idempotency(store):
    src = claim()
    store.append(src)
    expected = "mem_proj_" + hashlib.sha256("\x1f".join([PROJECTION_TYPE, src.kind, src.record_id]).encode()).hexdigest()[:16]
    assert stable_projection_id(src) == expected
    first = project_operational_knowledge(store, scope=SCOPE)
    output = projection(store, src)
    assert first["projected_count"] == 1
    assert output.source_id == "default" and output.scope == SCOPE
    assert output.meta["projection_lineage_schema"] == PROJECTION_LINEAGE_SCHEMA
    assert output.provenance["projection_lineage_schema"] == PROJECTION_LINEAGE_SCHEMA
    assert output.meta["source_record_ref"] == version_ref(src)
    assert output.provenance["source_record_ref"] == version_ref(src)
    assert output.meta["source_confidence"] == .95
    second = project_operational_knowledge(store, scope=SCOPE)
    assert second["projected_count"] == 0 and second["skipped"][0]["reason"] == "already_projected"
    assert len(memories(store)) == 1


def test_existing_legitimate_legacy_default_is_not_rewritten(store):
    src = claim()
    store.append(src)
    project_operational_knowledge(store, scope=SCOPE)
    legacy = projection(store, src)
    for container in (legacy.meta, legacy.provenance):
        for key in ("source_record_ref", "supporting_claim_refs", "projection_lineage_schema"):
            container.pop(key)
    store.append(legacy)
    before = legacy.to_dict()
    report = project_operational_knowledge(store, scope=SCOPE)
    assert report["projected_count"] == 0
    assert projection(store, src).to_dict() == before


def test_distinct_sources_same_text_preserve_exact_source_filters(store):
    records = [claim("claim_a", source_id="research-a"), claim("claim_b", source_id="research-b"), claim()]
    for src in records:
        store.append(src)
    assert project_operational_knowledge(store, scope=SCOPE)["projected_count"] == 3
    for src in records:
        output = projection(store, src)
        assert output and output.source_id == src.source_id and output.scope == src.scope
        assert output.meta["source_record_ref"] == version_ref(src)
        assert len(memories(store, source_ids=[src.source_id])) == 1
        assert all(m.meta["source_record_id"] == src.record_id for m in memories(store, source_ids=[src.source_id]))
    assert memories(store, source_ids=["default"])[0].meta["source_record_id"] == "claim_native"


def test_same_id_different_source_storage_negative_control(store):
    first = claim(source_id="research-a")
    second = claim(source_id="research-b")
    assert stable_projection_id(first) != stable_projection_id(second)
    store.append(first)
    with pytest.raises(ValueError, match="source_id move"):
        store.append(second)
    assert store.get_by_exact_ref(first.record_id, scope=SCOPE, source_id="research-b") is None
    assert project_operational_knowledge(store, scope=SCOPE)["projected_count"] == 1
    assert projection(store, first).source_id == "research-a"


@pytest.mark.parametrize("different_source", [False, True])
def test_same_id_shared_private_scopes_do_not_suppress_each_other(store, different_source):
    shared = claim(scope=replace(SCOPE, user_id=""), source_id="research-a")
    private = claim(source_id="research-b" if different_source else "research-a")
    shared.time = TimeRef("2026-10-01T01:00:00Z", "2026-10-01T01:00:00Z", "2026-10-01T01:00:00Z")
    private.time = TimeRef("2026-10-01T02:00:00Z", "2026-10-01T02:00:00Z", "2026-10-01T02:00:00Z")
    store.append(shared); store.append(private)
    report = project_operational_knowledge(store, scope=SCOPE)
    assert report["scanned_count"] == 2 and report["projected_count"] == 2
    for src in (shared, private):
        assert projection(store, src).meta["source_record_ref"] == version_ref(src)
    assert project_operational_knowledge(store, scope=SCOPE)["projected_count"] == 0


def test_old_mispartitioned_artifact_remains_untouched_without_suppressing_new_output(store):
    src = claim(source_id="research-a")
    fake_default_parent = replace(src, source_id="default")
    # Model the previous projector's known output format, retaining default.
    legacy = RecordEnvelope.create(kind="memory", title="Old projection", summary=TEXT, scope=SCOPE,
        source=PROJECTOR_SOURCE, source_id="default", meta={"projection_type": PROJECTION_TYPE,
        "source_record_id": src.record_id, "source_record_kind": src.kind})
    legacy.record_id = stable_projection_id(fake_default_parent)
    store.append(src); store.append(legacy)
    before = legacy.to_dict()
    assert project_operational_knowledge(store, scope=SCOPE)["projected_count"] == 1
    assert projection(store, src).source_id == "research-a"
    assert store.get_by_exact_ref(legacy.record_id, scope=SCOPE, source_id="default").to_dict() == before


@pytest.mark.parametrize("status", ["candidate", "quarantined", "superseded", "archived", "deleted", "blocked",
                                    "rejected", "deprecated", "conflicted", "needs_refresh", "unknown", "ACTIVE"])
def test_nonactive_sources_never_project(store, status):
    store.append(claim(status=status))
    report = project_operational_knowledge(store, scope=SCOPE)
    assert report["projected_count"] == 0 and not memories(store)
    assert report["skipped"][0]["reason"] == "unsafe_source_status"


@pytest.mark.parametrize("confidence", [.749, -.1, 1.1, "NaN", "Infinity", "-Infinity", "unknown", True, None])
def test_invalid_or_low_declared_confidence_is_not_masked(store, confidence):
    src = claim()
    src.content["confidence"] = confidence
    if confidence is None:
        src.meta["confidence"] = None
    store.append(src)
    report = project_operational_knowledge(store, scope=SCOPE)
    assert report["projected_count"] == 0 and not memories(store)


def test_original_point_75_threshold_remains_eligible(store):
    src = claim(confidence=.75)
    store.append(src)
    assert project_operational_knowledge(store, scope=SCOPE)["projected_count"] == 1
    assert projection(store, src).meta["source_confidence"] == .75


@pytest.mark.parametrize("container", ["content", "meta", "provenance"])
@pytest.mark.parametrize("field", ["contradiction_ids", "contradiction_claim_ids", "conflict", "deprecated"])
def test_direct_conflict_flags_fail_closed(store, container, field):
    src = claim()
    getattr(src, container)[field] = ["conflict_test"] if field.endswith("ids") else True
    store.append(src)
    assert project_operational_knowledge(store, scope=SCOPE)["projected_count"] == 0


def test_explicit_page_lineage_uses_weakest_support_and_preserves_references(store):
    supports = [claim("claim_a", source_id="research", confidence=.92), claim("claim_b", source_id="research", confidence=.78)]
    src = page(supports, confidence=.9)
    for item in [*supports, src]: store.append(item)
    assert project_operational_knowledge(store, scope=SCOPE)["projected_count"] == 3
    output = projection(store, src)
    assert output.source_id == "research" and output.meta["source_confidence"] == .78
    assert output.meta["source_record_ref"] == version_ref(src)
    assert output.meta["supporting_claim_refs"] == [version_ref(c) for c in supports]
    assert output.provenance["supporting_claim_refs"] == output.meta["supporting_claim_refs"]
    assert project_operational_knowledge(store, scope=SCOPE)["projected_count"] == 0


def test_page_declared_lower_confidence_cannot_be_upgraded(store):
    support = claim()
    src = page([support], confidence=.72)
    store.append(support); store.append(src)
    project_operational_knowledge(store, scope=SCOPE)
    assert projection(store, src) is None


def test_native_compiler_pages_pause_until_producer_supplies_new_lineage(store):
    extracted = extract_paper_memory(paper_source_id="paper_native", title="Runtime memory", abstract=TEXT.replace("must prefer", "improves"))
    compiled = compile_paper_knowledge(extraction=extracted).to_records(scope=SCOPE)
    for item in [*extracted.to_records(scope=SCOPE), *compiled]: store.append(item)
    before = [store.get_by_exact_ref(p.record_id, scope=SCOPE, source_id="default").to_dict() for p in compiled]
    report = project_operational_knowledge(store, scope=SCOPE)
    assert report["projected_count"] == 0 and not memories(store)
    assert any(s["reason"] == "missing_support_lineage" for s in report["skipped"])
    assert before == [store.get_by_exact_ref(p.record_id, scope=SCOPE, source_id="default").to_dict() for p in compiled]


@pytest.mark.parametrize("defect", ["missing", "changed", "inactive", "conflicted", "low", "invalid", "wrong_source", "wrong_scope"])
def test_page_support_defects_are_rejected(store, defect):
    support = claim(source_id="research")
    src = page([support])
    if defect == "changed": support.summary += " Changed."
    if defect == "inactive": support.status = "quarantined"; src = page([support])
    if defect == "conflicted": support.content["contradiction_claim_ids"] = ["other"]; src = page([support])
    if defect == "low": support.meta["confidence"] = .72; src = page([support])
    if defect == "invalid": support.content["confidence"] = "NaN"; src = page([support])
    if defect == "wrong_source": src.source_id = "different"
    if defect == "wrong_scope": src.scope = replace(SCOPE, user_id="different")
    if defect != "missing": store.append(support)
    store.append(src)
    report = project_operational_knowledge(store, scope=src.scope)
    assert projection(store, src) is None
    assert any(s["record_id"] == src.record_id for s in report["skipped"])


@pytest.mark.parametrize("field", ["tenant_id", "agent_id", "workspace_id", "user_id"])
def test_support_scope_cannot_borrow_equal_id_from_another_exact_scope(store, field):
    wrong = claim(scope=replace(SCOPE, **{field: "another"}), source_id="research")
    intended = claim(source_id="research")
    src = page([intended])
    store.append(wrong); store.append(src)
    project_operational_knowledge(store, scope=SCOPE)
    assert projection(store, src) is None


def test_equal_id_wrong_source_is_not_used_as_support(store):
    wrong = claim(source_id="wrong-source")
    intended = replace(wrong, source_id="expected-source")
    src = page([intended])
    store.append(wrong); store.append(src)
    project_operational_knowledge(store, scope=SCOPE)
    assert projection(store, src) is None


@pytest.mark.parametrize("defect", ["no_schema", "bad_schema", "missing_field", "bad_digest", "bad_scope", "unhashable_ids", "duplicate", "empty"])
def test_malformed_lineage_is_a_skip_not_a_crash(store, defect):
    support = claim()
    src = page([support])
    if defect == "no_schema": src.content.pop("supporting_claim_refs_schema")
    if defect == "bad_schema": src.content["supporting_claim_refs_schema"] = "guess"
    if defect == "missing_field": src.content["supporting_claim_refs"][0].pop("source_id")
    if defect == "bad_digest": src.content["supporting_claim_refs"][0]["version_digest"] = "old"
    if defect == "bad_scope": src.content["supporting_claim_refs"][0]["scope"].pop("user_id")
    if defect == "unhashable_ids": src.content["supporting_claim_ids"] = [{}]
    if defect == "duplicate": src.content["supporting_claim_refs"] *= 2
    if defect == "empty": src.content["supporting_claim_refs"] = []
    store.append(support); store.append(src)
    project_operational_knowledge(store, scope=SCOPE)
    assert projection(store, src) is None


@pytest.mark.parametrize("mutation", ["changed", "inactivated", "removed", "confidence_invalid", "conflicted"])
def test_support_is_revalidated_inside_write_transaction(store, monkeypatch, mutation):
    support = claim()
    src = page([support])
    store.append(support); store.append(src)
    original = store.mutate_records_atomically
    def race(callback):
        def wrapped(sqlite):
            current = sqlite.get_by_exact_ref(support.record_id, scope=SCOPE, source_id="default")
            if mutation == "removed":
                sqlite.execute("DELETE FROM records WHERE record_id=?", (support.record_id,))
            else:
                if mutation == "changed": current.summary += " changed"
                if mutation == "inactivated": current.status = "quarantined"
                if mutation == "confidence_invalid": current.content["confidence"] = "NaN"
                if mutation == "conflicted": current.meta["contradiction_claim_ids"] = ["other"]
                sqlite.upsert(current, commit=False)
            return callback(sqlite)
        return original(wrapped)
    monkeypatch.setattr(store, "mutate_records_atomically", race)
    report = project_operational_knowledge(store, scope=SCOPE)
    assert projection(store, src) is None
    assert not memories(store)
    assert any(s["record_id"] == src.record_id and s["reason"] in {"support_changed", "support_missing"} for s in report["skipped"])


def test_source_version_is_revalidated_inside_transaction(store, monkeypatch):
    src = claim()
    store.append(src)
    original = store.mutate_records_atomically
    def race(callback):
        def wrapped(sqlite):
            changed = sqlite.get_by_exact_ref(src.record_id, scope=SCOPE, source_id="default")
            changed.summary += " changed"
            sqlite.upsert(changed, commit=False)
            return callback(sqlite)
        return original(wrapped)
    monkeypatch.setattr(store, "mutate_records_atomically", race)
    report = project_operational_knowledge(store, scope=SCOPE)
    assert report["projected_count"] == 0 and report["skipped"][0]["reason"] == "source_changed"


def test_record_and_outbox_rollback_when_second_projection_fails(store, monkeypatch):
    for identifier in ("claim_a", "claim_b"):
        store.append(claim(identifier))
    with store._lock:
        before_records = store.sqlite.conn.execute("SELECT count(*) FROM records").fetchone()[0]
        before_outbox = store.sqlite.conn.execute("SELECT count(*) FROM export_outbox").fetchone()[0]
    before_log = store.log.path.read_bytes()
    original = store.sqlite.upsert
    calls = 0
    def fail_second(record, *, commit=True):
        nonlocal calls
        if record.kind == "memory":
            calls += 1
            if calls == 2:
                raise RuntimeError("synthetic-second-write-failure")
        return original(record, commit=commit)
    monkeypatch.setattr(store.sqlite, "upsert", fail_second)
    with pytest.raises(RuntimeError, match="synthetic-second-write-failure"):
        project_operational_knowledge(store, scope=SCOPE)
    assert calls == 2 and not memories(store)
    with store._lock:
        assert store.sqlite.conn.execute("SELECT count(*) FROM records").fetchone()[0] == before_records
        assert store.sqlite.conn.execute("SELECT count(*) FROM export_outbox").fetchone()[0] == before_outbox
        assert not store.sqlite.in_transaction
    assert store.log.path.read_bytes() == before_log


def test_existing_projection_with_another_parent_ref_cannot_suppress_or_overwrite(store):
    src = claim()
    store.append(src)
    project_operational_knowledge(store, scope=SCOPE)
    output = projection(store, src)
    output.meta["source_record_ref"]["source_id"] = "another-source"
    store.append(output)
    before = output.to_dict()
    report = project_operational_knowledge(store, scope=SCOPE)
    assert report["projected_count"] == 0
    assert report["skipped"][0]["reason"] == "projection_identity_conflict"
    assert projection(store, src).to_dict() == before


def test_all_records_and_first_outbox_entry_rollback_on_export_enqueue_failure(store, monkeypatch):
    for identifier in ("claim_a", "claim_b"):
        store.append(claim(identifier))
    with store._lock:
        before_outbox = store.sqlite.conn.execute("SELECT count(*) FROM export_outbox").fetchone()[0]
    before_log = store.log.path.read_bytes()
    original = store.sqlite.enqueue_export
    calls = 0
    def fail_second(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("synthetic-outbox-failure")
        return original(**kwargs)
    monkeypatch.setattr(store.sqlite, "enqueue_export", fail_second)
    with pytest.raises(RuntimeError, match="synthetic-outbox-failure"):
        project_operational_knowledge(store, scope=SCOPE)
    assert calls == 2 and not memories(store)
    with store._lock:
        assert store.sqlite.conn.execute("SELECT count(*) FROM export_outbox").fetchone()[0] == before_outbox
        assert not store.sqlite.in_transaction
    assert store.log.path.read_bytes() == before_log


def test_page_support_reference_cap_fails_closed(store):
    support = claim()
    src = page([support])
    src.content["supporting_claim_refs"] *= 101
    store.append(support); store.append(src)
    project_operational_knowledge(store, scope=SCOPE)
    assert projection(store, src) is None


@pytest.mark.parametrize("defect", ["partial", "schema", "support_refs"])
def test_partial_new_projection_lineage_is_not_accepted_as_legacy(store, defect):
    src = claim()
    store.append(src)
    project_operational_knowledge(store, scope=SCOPE)
    output = projection(store, src)
    if defect == "partial": output.meta.pop("source_record_ref")
    if defect == "schema": output.meta["projection_lineage_schema"] = "invalid"
    if defect == "support_refs": output.provenance["supporting_claim_refs"] = [{"record_id": "forged"}]
    store.append(output)
    before = output.to_dict()
    report = project_operational_knowledge(store, scope=SCOPE)
    assert report["projected_count"] == 0
    assert report["skipped"][0]["reason"] == "projection_identity_conflict"
    assert projection(store, src).to_dict() == before


@pytest.mark.parametrize("kind", ["claim_card", "knowledge_page"])
@pytest.mark.parametrize("container,key", [("meta", "reliability"), ("meta", "confidence"), ("content", "confidence")])
def test_explicit_null_confidence_rejects_even_with_valid_alternative_or_support(store, kind, container, key):
    support = claim(confidence=.87)
    src = support if kind == "claim_card" else page([support])
    getattr(src, container)[key] = None
    if kind == "knowledge_page":
        store.append(support)
    store.append(src)
    report = project_operational_knowledge(store, scope=SCOPE)
    assert projection(store, src) is None
    assert any(item["record_id"] == src.record_id and item["reason"] == "low_confidence" for item in report["skipped"])


@pytest.mark.parametrize("container,key", [("meta", "reliability"), ("meta", "confidence"), ("content", "confidence")])
def test_absent_confidence_field_can_use_remaining_valid_claim_declaration(store, container, key):
    src = claim(confidence=.87)
    getattr(src, container).pop(key, None)
    store.append(src)
    assert project_operational_knowledge(store, scope=SCOPE)["projected_count"] == 1
    assert projection(store, src).meta["source_confidence"] == .87


def test_page_with_truly_absent_own_confidence_derives_only_from_valid_support(store):
    support = claim(confidence=.87)
    src = page([support])
    assert "confidence" not in src.content and "confidence" not in src.meta and "reliability" not in src.meta
    store.append(support); store.append(src)
    project_operational_knowledge(store, scope=SCOPE)
    assert projection(store, src).meta["source_confidence"] == .87


@pytest.mark.parametrize("container,key", [("meta", "reliability"), ("meta", "confidence"), ("content", "confidence")])
def test_null_in_support_cannot_be_masked_by_other_valid_support_confidence(store, container, key):
    support = claim(confidence=.87)
    getattr(support, container)[key] = None
    src = page([support])
    store.append(support); store.append(src)
    project_operational_knowledge(store, scope=SCOPE)
    assert projection(store, src) is None and projection(store, support) is None
