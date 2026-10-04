from __future__ import annotations

from dataclasses import asdict
from types import SimpleNamespace

import pytest

from eimemory.api.memory import MemoryAPI
from eimemory.living.operations import _record_digest, enrich_memory_records
from eimemory.living.posture import compile_living_posture_report
from eimemory.living.schema import has_living_memory_meta
from eimemory.models.records import LinkRef, RecordEnvelope, ScopeRef
from eimemory.storage.runtime_store import RuntimeStore


@pytest.fixture
def store(tmp_path):
    result = RuntimeStore(tmp_path / "synthetic-store")
    try:
        yield result
    finally:
        result.close()


@pytest.fixture
def scope():
    return ScopeRef(tenant_id="fixture", agent_id="fixture", workspace_id="fixture", user_id="owner")


def memory(scope, *, record_id="mem_fixture", memory_type="preference", status="active", source_id="default"):
    item = RecordEnvelope.create(
        kind="memory", title="Zephyr handling preference",
        summary="Zephyr wait and hold off until the operator approves the next step.",
        content={"text": "Zephyr wait and hold off until the operator approves the next step.", "memory_type": memory_type},
        scope=scope, source="manual", source_id=source_id, status=status,
        meta={"memory_type": memory_type, "force_capture": True},
    )
    item.record_id = record_id
    return item


def posture(store, scope):
    return compile_living_posture_report(SimpleNamespace(store=store), "Zephyr", scope, 10)


def pending_rows(store):
    return store.run_locked(lambda sqlite: [tuple(row) for row in sqlite.execute(
        "SELECT operation_id, payload_digest, state FROM export_outbox ORDER BY operation_id"
    ).fetchall()])


def test_posture_does_not_resurrect_superseded_preference(store, scope):
    api = MemoryAPI(store)
    common = dict(memory_type="preference", title="Zephyr handling preference", scope=asdict(scope), force_capture=True)
    old = api.ingest(text="For Zephyr repair broken trust before proceeding with the project.", **common)
    new = api.ingest(text="For Zephyr wait and hold off until explicitly approved by the operator.", **common)
    assert store.get_by_exact_ref(old.record_id, scope=scope, source_id=old.source_id).status == "superseded"
    result = posture(store, scope)
    assert result["profile"]["source_record_ids"] == [new.record_id]
    assert result["profile"]["recommended_action"] == "wait"


@pytest.mark.parametrize("status", ["superseded", "archived", "rejected", "inactive", "", "ACTIVE"])
def test_posture_supplement_and_fallback_require_active(store, scope, status):
    item = memory(scope, status=status)
    store.append(item)
    assert posture(store, scope)["record_count"] == 0


@pytest.mark.parametrize("audit_kind", ["type", "source", "title"])
def test_posture_fallback_never_resurrects_internal_audit(store, scope, audit_kind):
    item = memory(scope, memory_type="audit" if audit_kind == "type" else "preference")
    if audit_kind == "source":
        item.source = "ei_bridge.openclaw_feishu"
    if audit_kind == "title":
        item.title = "ei-bridge openclaw command audit"
    store.append(item)
    result = posture(store, scope)
    assert result["record_count"] == 0
    assert result["profile"]["confidence"] == 0.0
    assert result["profile"]["recommended_action"] == "wait"


def test_posture_keeps_safe_generic_fallback_and_shared_reader_visibility(store, scope):
    shared = ScopeRef(**{**asdict(scope), "user_id": ""})
    item = memory(shared, memory_type="fact")
    item.summary = "Zephyr has a blue display with a stable configuration."
    item.content["text"] = item.summary
    store.append(item)
    result = posture(store, scope)
    assert result["profile"]["source_record_ids"] == [item.record_id]


@pytest.mark.parametrize("field,value", [
    ("memory_type", "audit"),
    ("source", "ei_bridge.openclaw_feishu"),
    ("source_channel", "ei_bridge.openclaw_feishu"),
    ("communication_channel", "ei_bridge.openclaw_feishu"),
])
def test_posture_imported_nested_business_audit_stays_ineligible(store, scope, field, value):
    item = memory(scope)
    payload = item.to_dict()
    payload["meta"] = {"business_meta": {field: value}}
    payload["content"]["memory_type"] = "preference"
    imported = RecordEnvelope.from_dict(payload)
    store.append(imported)
    assert field not in imported.meta
    result = posture(store, scope)
    assert result["record_count"] == 0
    assert result["profile"]["source_record_ids"] == []


@pytest.mark.parametrize("change", ["summary", "content", "meta", "source", "provenance", "links", "evidence", "timestamp"])
def test_enrichment_competing_full_payload_update_is_not_overwritten(store, scope, monkeypatch, change):
    item = memory(scope)
    store.append(item)
    real_owner = store.mutate_records_atomically
    competing_digest = []

    def owner_with_competing_update(mutation):
        current = store.get_by_exact_ref(item.record_id, scope=scope, source_id=item.source_id)
        if change == "summary":
            current.summary = "A concurrent update without a touch call."
        elif change == "content":
            current.content["text"] = "A concurrent content update."
        elif change == "meta":
            current.meta["concurrent"] = True
        elif change == "source":
            current.source = "concurrent-source"
        elif change == "provenance":
            current.provenance["concurrent"] = True
        elif change == "links":
            current.links.append(LinkRef("related", "memory", "mem_other"))
        elif change == "evidence":
            current.evidence.append("synthetic evidence")
        else:
            current.time.occurred_at = "2024-01-02T00:00:00Z"
        store.rewrite(current, previous_scope=scope)
        competing_digest.append(_record_digest(current))
        return real_owner(mutation)

    monkeypatch.setattr(store, "mutate_records_atomically", owner_with_competing_update)
    report = enrich_memory_records(SimpleNamespace(store=store), scope=scope, limit=1)
    final = store.get_by_exact_ref(item.record_id, scope=scope, source_id=item.source_id)
    assert report["enriched_count"] == 0
    assert report["skipped_reasons"] == {"changed": 1}
    assert _record_digest(final) == competing_digest[0]
    assert not has_living_memory_meta(final)


def test_enrichment_rechecks_active_under_transaction(store, scope, monkeypatch):
    item = memory(scope)
    store.append(item)
    real_owner = store.mutate_records_atomically

    def owner_with_status_change(mutation):
        changed = store.get_by_exact_ref(item.record_id, scope=scope, source_id=item.source_id)
        changed.status = "superseded"
        store.rewrite(changed, previous_scope=scope)
        return real_owner(mutation)

    monkeypatch.setattr(store, "mutate_records_atomically", owner_with_status_change)
    report = enrich_memory_records(SimpleNamespace(store=store), scope=scope, limit=1)
    final = store.get_by_exact_ref(item.record_id, scope=scope, source_id=item.source_id)
    assert report["enriched_count"] == 0
    assert report["skipped_reasons"] == {"inactive": 1}
    assert final.status == "superseded"
    assert not has_living_memory_meta(final)


def test_enrichment_same_id_shared_scope_is_readable_but_not_writable(store, scope):
    shared_scope = ScopeRef(**{**asdict(scope), "user_id": ""})
    owned = memory(scope, source_id="owned-source")
    shared = memory(shared_scope, source_id="shared-source")
    store.append(owned)
    store.append(shared)
    shared_digest = _record_digest(shared)
    report = enrich_memory_records(SimpleNamespace(store=store), scope=scope, limit=10)
    assert report["enriched_count"] == 1
    assert report["skipped_reasons"] == {"scope_mismatch": 1}
    assert has_living_memory_meta(store.get_by_exact_ref(owned.record_id, scope=scope, source_id=owned.source_id))
    assert _record_digest(store.get_by_exact_ref(shared.record_id, scope=shared_scope, source_id=shared.source_id)) == shared_digest


def test_enrichment_missing_exact_ref_is_explicit_no_write(store, scope, monkeypatch):
    item = memory(scope, source_id="source-a")
    store.append(item)
    before = pending_rows(store)
    requests = []

    def missing(record_id, *, scope, source_id):
        assert store.sqlite.in_transaction
        assert store._write_lock_owned()
        requests.append((record_id, asdict(scope), source_id))
        return None

    monkeypatch.setattr(store.sqlite, "get_by_exact_ref", missing)
    report = enrich_memory_records(SimpleNamespace(store=store), scope=scope, limit=1)
    assert requests == [(item.record_id, asdict(scope), "source-a")]
    assert report["enriched_count"] == 0
    assert report["skipped_reasons"] == {"missing_or_invalid": 1}
    assert report["digests"] == []
    assert pending_rows(store) == before


def test_enrichment_rollback_includes_record_and_outbox(store, scope, monkeypatch):
    item = memory(scope)
    store.append(item)
    before_digest = _record_digest(item)
    before_outbox = pending_rows(store)
    real_enqueue = store._enqueue_record_exports

    def failing_enqueue(record):
        assert store.sqlite.in_transaction
        real_enqueue(record)
        raise RuntimeError("synthetic outbox failure")

    monkeypatch.setattr(store, "_enqueue_record_exports", failing_enqueue)
    with pytest.raises(RuntimeError, match="synthetic outbox failure"):
        enrich_memory_records(SimpleNamespace(store=store), scope=scope, limit=1)
    final = store.get_by_exact_ref(item.record_id, scope=scope, source_id=item.source_id)
    assert _record_digest(final) == before_digest
    assert pending_rows(store) == before_outbox
    assert not store.sqlite.in_transaction


def test_enrichment_commits_record_with_outbox_and_is_idempotent(store, scope, monkeypatch):
    item = memory(scope)
    store.append(item)
    before = _record_digest(item)
    real_rewrite = store.sqlite.rewrite
    boundaries = []

    def checked_rewrite(candidate, *, previous_scope=None, commit=True):
        boundaries.append((store.sqlite.in_transaction, store._write_lock_owned(), commit))
        return real_rewrite(candidate, previous_scope=previous_scope, commit=commit)

    monkeypatch.setattr(store.sqlite, "rewrite", checked_rewrite)
    first = enrich_memory_records(SimpleNamespace(store=store), scope=scope, limit=1)
    final = store.get_by_exact_ref(item.record_id, scope=scope, source_id=item.source_id)
    assert boundaries == [(True, True, False)]
    assert first["enriched_count"] == 1
    assert first["digests"] == [{"record_id": item.record_id, "before_digest": before, "after_digest": _record_digest(final)}]
    outbox = store.run_locked(lambda sqlite: [dict(row) for row in sqlite.execute(
        "SELECT payload_digest, state FROM export_outbox WHERE stream='records'"
    ).fetchall()])
    assert any(row["payload_digest"] == _record_digest(final) for row in outbox)
    second = enrich_memory_records(SimpleNamespace(store=store), scope=scope, limit=1)
    assert second["enriched_count"] == 0
    assert second["skipped_reasons"] == {"already_enriched": 1}
