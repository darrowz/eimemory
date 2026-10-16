"""Rejected identity snapshots expose reasons without relaxing their authority."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from eimemory import identity
from eimemory import identity_ops
from eimemory.identity_ops import repair_hongtu_identity
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.storage.jsonl import payload_digest
from eimemory.storage.runtime_store import RuntimeStore


@pytest.fixture
def owner(tmp_path, monkeypatch):
    monkeypatch.setattr(identity, "HONGTU_AGENT_ID", "current-owner")
    monkeypatch.setattr(identity, "legacy_agent_ids", lambda: ("legacy-owner",))
    store = RuntimeStore(tmp_path)
    scope = ScopeRef(agent_id="current-owner", workspace_id="embodied", user_id="owner")
    try:
        yield store, scope
    finally:
        store.close()


def _append(store, *, kind="entity_record", content=None):
    record = RecordEnvelope.create(
        kind=kind, title="private source title", summary="private source summary",
        source="eimemory.knowledge.relations", content=content,
        scope=ScopeRef(agent_id="legacy-owner", workspace_id="embodied", user_id="owner"),
    )
    with store._lock:
        store.sqlite.upsert(record)
    return record


def _rows(store):
    with store._lock:
        return [dict(row) for row in store.sqlite.conn.execute("SELECT * FROM records ORDER BY storage_key")]


def _repair(store, scope):
    return repair_hongtu_identity(SimpleNamespace(store=store), scope=scope, apply=True)


def test_projection_diagnostics_aggregate_all_rejections_before_sample_truncation(owner):
    store, scope = owner
    for _ in range(101):
        _append(store)
    with store._lock:
        store.sqlite.conn.execute(
            "UPDATE records SET created_at='2000-01-01T00:00:00Z', summary='changed projection'"
        )
        store.sqlite.conn.commit()
    before = _rows(store)

    report = _repair(store, scope)

    assert report["ok"] is False and report["repaired_count"] == 0
    assert report["outcome_counts"] == {"source_projection_or_digest_mismatch": 101}
    assert report["mismatch_field_counts"] == {"created_at": 101, "summary": 101}
    assert len(report["repair_outcomes"]) == 100 and report["repair_outcomes_truncated"] is True
    assert all(set(item["mismatch_fields"]) == {"created_at", "summary"} for item in report["repair_outcomes"])
    assert _rows(store) == before


@pytest.mark.parametrize("legacy_change", ["missing_alias_fields", "source_partition_overlay"])
def test_valid_legacy_payload_is_distinguished_from_stored_digest_failure(owner, legacy_change):
    store, scope = owner
    record = _append(store, kind="knowledge_page", content={"source_ids": ["hermes"]})
    with store._lock:
        if legacy_change == "missing_alias_fields":
            payload = json.loads(store.sqlite.conn.execute("SELECT payload_json FROM records").fetchone()[0])
            payload.pop("aliases")
            payload.pop("aliases_version")
            store.sqlite.conn.execute(
                "UPDATE records SET payload_json=?,payload_digest=?", (json.dumps(payload), payload_digest(payload))
            )
        else:
            # The existing legacy source migration overlays this partition on
            # hydration without rewriting or re-signing the original payload.
            store.sqlite.conn.execute("UPDATE records SET source_id='hermes'")
        store.sqlite.conn.commit()
    before = _rows(store)
    assert payload_digest(json.loads(before[0]["payload_json"])) == before[0]["payload_digest"]

    report = _repair(store, scope)

    assert report["ok"] is False and report["repaired_count"] == 0
    assert report["outcome_counts"] == {"source_projection_or_digest_mismatch": 1}
    assert report["mismatch_field_counts"] == {"payload_roundtrip_digest": 1}
    assert report["repair_outcomes"][0]["mismatch_fields"] == ["payload_roundtrip_digest"]
    assert report["repair_outcomes"][0]["source_ref"]["record_id"] == record.record_id
    assert _rows(store) == before


def test_corrupted_payload_after_discovery_stays_rejected_without_apply_writes(owner, monkeypatch):
    store, scope = owner
    _append(store)
    snapshot = store.read_consistent
    corrupted = []

    def corrupt_after_discovery(callback):
        result = snapshot(callback)
        with store._lock:
            payload = json.loads(store.sqlite.conn.execute("SELECT payload_json FROM records").fetchone()[0])
            payload["summary"] = "changed without a matching checksum"
            store.sqlite.conn.execute("UPDATE records SET payload_json=?", (json.dumps(payload),))
            store.sqlite.conn.commit()
        corrupted.extend(_rows(store))
        return result

    monkeypatch.setattr(store, "read_consistent", corrupt_after_discovery)
    report = _repair(store, scope)

    assert report["ok"] is False and report["repaired_count"] == 0
    assert report["outcome_counts"] == {"source_projection_or_digest_mismatch": 1}
    assert report["mismatch_field_counts"] == {"hydration": 1, "payload_stored_digest": 1}
    assert _rows(store) == corrupted


@pytest.mark.parametrize("meta_json", ['{"unexpected":"projection value"}', 'null'])
def test_metadata_projection_mismatch_remains_blocked(owner, meta_json):
    store, scope = owner
    _append(store)
    with store._lock:
        store.sqlite.conn.execute("UPDATE records SET meta_json=?", (meta_json,))
        store.sqlite.conn.commit()
    before = _rows(store)

    report = _repair(store, scope)

    assert report["ok"] is False and report["repaired_count"] == 0
    assert report["outcome_counts"] == {"source_projection_or_digest_mismatch": 1}
    assert report["mismatch_field_counts"] == {"meta_json": 1}
    assert _rows(store) == before


def test_diagnostic_exception_does_not_replace_original_rejection(owner, monkeypatch):
    store, scope = owner
    _append(store)
    with store._lock:
        store.sqlite.conn.execute("UPDATE records SET source='changed source projection'")
        store.sqlite.conn.commit()
    before = _rows(store)
    digest = identity_ops.payload_digest

    def unavailable_roundtrip(payload):
        # Hydration checks the stored payload in sqlite_store, independently
        # of this optional round-trip diagnostic after the source rejection.
        raise ValueError("private diagnostic exception detail")

    monkeypatch.setattr(identity_ops, "payload_digest", unavailable_roundtrip)
    report = _repair(store, scope)
    monkeypatch.setattr(identity_ops, "payload_digest", digest)

    assert report["outcome_counts"] == {"source_projection_or_digest_mismatch": 1}
    assert report["mismatch_field_counts"] == {"source": 1, "diagnostic_unavailable": 1}
    assert "private diagnostic exception detail" not in json.dumps(report)
    assert _rows(store) == before
