"""Known legacy reader defaults cannot authorize unrelated envelope changes."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from eimemory import identity
from eimemory import identity_ops
from eimemory.identity_ops import _RepairSkipped, _repair_snapshot, repair_hongtu_identity
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.storage.jsonl import payload_digest
from eimemory.storage.runtime_store import RuntimeStore


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    monkeypatch.setattr(identity, "HONGTU_AGENT_ID", "current-owner")
    monkeypatch.setattr(identity, "legacy_agent_ids", lambda: ("legacy-owner",))
    store = RuntimeStore(tmp_path)
    record = RecordEnvelope.create(
        kind="entity_record", title="Synthetic legacy entity", summary="Unchanged summary",
        content={"aliases": ["Example Alias"]},
        scope=ScopeRef(agent_id="legacy-owner", workspace_id="embodied", user_id="owner"),
        source="eimemory.knowledge.relations",
    )
    with store._lock:
        store.sqlite.upsert(record)
    scope = ScopeRef(agent_id="current-owner", workspace_id="embodied", user_id="owner")
    try:
        yield store, record, scope
    finally:
        store.close()


def _rows(store):
    with store._lock:
        return [dict(row) for row in store.sqlite.conn.execute("SELECT * FROM records ORDER BY storage_key")]


def _set_payload(store, payload, *, raw=None):
    with store._lock:
        store.sqlite.conn.execute("UPDATE records SET payload_json=?,payload_digest=?",
                                  (json.dumps(payload) if raw is None else raw, payload_digest(payload)))
        store.sqlite.conn.commit()


def _repair(store, scope, *, apply=True):
    return repair_hongtu_identity(SimpleNamespace(store=store), scope=scope, apply=apply)


@pytest.mark.parametrize("legacy_aliases", ["Example Alias", ["Example Alias"]])
def test_all_three_missing_new_fields_preserve_derived_legacy_aliases_and_business_content(legacy, legacy_aliases):
    store, record, scope = legacy
    raw = json.loads(_rows(store)[0]["payload_json"])
    for key in ("aliases", "aliases_version", "source_id"):
        raw.pop(key)
    raw["content"]["aliases"] = legacy_aliases
    # Open business content remains open JSON; this is preserved verbatim.
    raw["content"]["unknown_business_key"] = {"flag": True, "weight": 1.0}
    _set_payload(store, raw)
    before = _rows(store)

    preview = _repair(store, scope, apply=False)
    assert preview["ok"] is True and preview["eligible_in_place_count"] == 1
    assert preview["legacy_normalization_counts"] == {
        "missing_aliases": 1, "missing_aliases_version": 1, "missing_source_id": 1,
    }
    assert _rows(store) == before

    report = _repair(store, scope)
    assert report["outcome_counts"] == {"applied": 1}
    after = _rows(store)[0]
    payload = json.loads(after["payload_json"])
    assert payload["aliases"] == ["example alias"] == record.aliases
    assert payload["source_id"] == "default"
    assert payload["content"] == raw["content"]
    assert payload["time"]["created_at"] == raw["time"]["created_at"]
    assert payload["time"]["occurred_at"] == raw["time"]["occurred_at"]
    assert payload_digest(payload) == after["payload_digest"]


@pytest.mark.parametrize("legacy_aliases", [None, True, 1, [True], ["valid", 1]])
def test_legacy_alias_defaults_cannot_materialize_malformed_values_as_text(legacy, legacy_aliases):
    store, _, scope = legacy
    raw = json.loads(_rows(store)[0]["payload_json"])
    raw.pop("aliases")
    raw["content"]["aliases"] = legacy_aliases
    _set_payload(store, raw)
    before = _rows(store)

    report = _repair(store, scope)

    assert report["ok"] is False and report["repaired_count"] == 0
    assert report["mismatch_field_counts"] == {"payload_roundtrip_digest": 1}
    assert _rows(store) == before


@pytest.mark.parametrize("missing_aliases", [False, True])
def test_non_alias_kind_preserves_open_business_aliases_content(legacy, missing_aliases):
    store, _, scope = legacy
    raw = json.loads(_rows(store)[0]["payload_json"])
    raw["kind"] = "knowledge_page"
    raw["content"]["aliases"] = {"open_business_key": True}
    raw["aliases"] = []
    raw.pop("aliases_version")
    if missing_aliases:
        raw.pop("aliases")
    with store._lock:
        store.sqlite.conn.execute("UPDATE records SET kind='knowledge_page'")
    _set_payload(store, raw)
    before = _rows(store)

    preview = _repair(store, scope, apply=False)
    assert preview["ok"] is True and preview["eligible_in_place_count"] == 1
    assert _rows(store) == before

    report = _repair(store, scope)
    assert report["outcome_counts"] == {"applied": 1}
    after = json.loads(_rows(store)[0]["payload_json"])
    assert after["content"] == raw["content"]
    assert after["aliases"] == []


def test_existing_explicit_aliases_do_not_require_a_new_legacy_alias_derivation(legacy):
    store, _, scope = legacy
    raw = json.loads(_rows(store)[0]["payload_json"])
    raw["content"]["aliases"] = [True]
    raw["aliases"] = ["true"]
    raw.pop("aliases_version")
    _set_payload(store, raw)

    report = _repair(store, scope)

    assert report["outcome_counts"] == {"applied": 1}
    after = json.loads(_rows(store)[0]["payload_json"])
    assert after["content"] == raw["content"] and after["aliases"] == raw["aliases"]


@pytest.mark.parametrize("before_value,after_value", [(True, 1), (1, 1.0)])
def test_identity_transition_cannot_coerce_unrelated_content_types(legacy, monkeypatch, before_value, after_value):
    store, _, scope = legacy
    raw = json.loads(_rows(store)[0]["payload_json"])
    raw.pop("aliases_version")
    raw["content"]["flag"] = before_value
    _set_payload(store, raw)
    before = _rows(store)
    normalize = identity_ops.normalize_hongtu_record

    def unrelated_type_change(record):
        proposed = normalize(record)
        proposed.content["flag"] = after_value
        return proposed

    monkeypatch.setattr(identity_ops, "normalize_hongtu_record", unrelated_type_change)
    report = _repair(store, scope)

    assert report["ok"] is False and report["repaired_count"] == 0
    assert report["outcome_counts"] == {"identity_transition_forbidden": 1}
    assert _rows(store) == before


@pytest.mark.parametrize("mutation,field", [
    ("unknown_top", "unknown_fields"), ("unknown_scope", "scope"),
    ("unknown_time", "time"), ("unknown_link", "links"),
    ("null_aliases", "aliases"), ("null_aliases_version", "aliases_version"),
    ("explicit_alias_normalization", "aliases"), ("typed_tags", "tags"),
    ("explicit_source_normalization", "source_id"),
    ("typed_title", "title"), ("null_content", "content"),
    ("missing_summary", "summary"), ("missing_clock", "time"),
])
def test_missing_new_fields_do_not_bless_lossy_hydration(legacy, mutation, field):
    store, _, scope = legacy
    raw = json.loads(_rows(store)[0]["payload_json"])
    raw.pop("aliases_version")
    if mutation == "unknown_top":
        raw["private_unknown_envelope_key"] = "private unknown value"
    elif mutation == "unknown_scope":
        raw["scope"]["private_unknown_scope_key"] = True
    elif mutation == "unknown_time":
        raw["time"]["private_unknown_time_key"] = "private time value"
    elif mutation == "unknown_link":
        raw["links"] = [{"relation": "related", "target_kind": "entity_record", "target_id": "example",
                         "private_unknown_link_key": "private link value"}]
    elif mutation == "null_aliases":
        raw["aliases"] = None
    elif mutation == "null_aliases_version":
        raw["aliases_version"] = None
    elif mutation == "explicit_alias_normalization":
        raw["aliases"] = ["Example Alias"]
    elif mutation == "typed_tags":
        raw["tags"] = [True, 1, None]
    elif mutation == "explicit_source_normalization":
        raw["source_id"] = "HerMes"
        store.sqlite.conn.execute("UPDATE records SET source_id='hermes'")
    elif mutation == "typed_title":
        raw["title"] = 1
        store.sqlite.conn.execute("UPDATE records SET title='1'")
    elif mutation == "null_content":
        raw["content"] = None
    elif mutation == "missing_summary":
        raw.pop("summary")
        store.sqlite.conn.execute("UPDATE records SET summary=''")
    elif mutation == "missing_clock":
        raw["time"].pop("occurred_at")
    _set_payload(store, raw)
    before = _rows(store)

    report = _repair(store, scope)

    assert report["ok"] is False and report["repaired_count"] == 0
    assert report["outcome_counts"] == {"source_projection_or_digest_mismatch": 1}
    assert report["mismatch_field_counts"] == {"payload_roundtrip_digest": 1}
    assert report["roundtrip_field_counts"][field] == 1
    assert report["legacy_normalization_counts"] == {}
    diagnostics = json.dumps({key: value for key, value in report.items() if key.endswith("counts")})
    assert "private_unknown" not in diagnostics and "private unknown value" not in diagnostics
    assert _rows(store) == before


@pytest.mark.parametrize("ambiguity", ["duplicate_key", "nonfinite"])
def test_loose_json_hydration_cannot_bypass_strict_original_payload_validation(legacy, ambiguity):
    store, _, scope = legacy
    raw = json.loads(_rows(store)[0]["payload_json"])
    raw.pop("aliases_version")
    if ambiguity == "duplicate_key":
        raw_text = json.dumps(raw)[:-1] + ',"title":' + json.dumps(raw["title"]) + '}'
    else:
        raw["content"]["untrusted_number"] = float("nan")
        raw_text = json.dumps(raw)
    _set_payload(store, raw, raw=raw_text)
    before = _rows(store)

    report = _repair(store, scope)

    assert report["ok"] is False and report["repaired_count"] == 0
    assert report["mismatch_field_counts"] == {"payload_json": 1}
    assert _rows(store) == before


def test_metadata_projection_cannot_change_bool_to_integer(legacy):
    store, _, scope = legacy
    raw = json.loads(_rows(store)[0]["payload_json"])
    raw.pop("aliases_version")
    raw["meta"]["flag"] = True
    _set_payload(store, raw)
    projected = dict(raw["meta"], flag=1)
    with store._lock:
        store.sqlite.conn.execute("UPDATE records SET meta_json=?", (json.dumps(projected),))
        store.sqlite.conn.commit()
    before = _rows(store)

    report = _repair(store, scope)

    assert report["ok"] is False and report["repaired_count"] == 0
    assert report["mismatch_field_counts"] == {"meta_json": 1}
    assert _rows(store) == before


def test_explicit_null_source_id_is_not_a_missing_field_compatibility_default(legacy):
    store, record, _ = legacy
    raw = json.loads(_rows(store)[0]["payload_json"])
    raw["source_id"] = None
    _set_payload(store, raw)
    before = _rows(store)
    ref = (record.scope.tenant_id, record.scope.agent_id, record.scope.workspace_id,
           record.scope.user_id, "default", record.record_id)

    with pytest.raises(_RepairSkipped, match="source_projection_or_digest_mismatch"):
        store.read_consistent(lambda sqlite: _repair_snapshot(sqlite, ref))
    assert _rows(store) == before


@pytest.mark.parametrize("source_evidence", ["missing", "conflicting", "invalid_type"])
def test_sql_source_column_cannot_authorize_legacy_overlay_without_matching_source_evidence(legacy, source_evidence):
    store, record, _ = legacy
    raw = json.loads(_rows(store)[0]["payload_json"])
    raw["kind"] = "knowledge_page"
    if source_evidence == "conflicting":
        raw["content"]["source_ids"] = ["hermes"]
        raw["provenance"]["source_ids"] = ["other-source"]
    elif source_evidence == "invalid_type":
        raw["content"]["source_ids"] = [True]
    _set_payload(store, raw)
    with store._lock:
        store.sqlite.conn.execute("UPDATE records SET kind='knowledge_page',source_id='hermes'")
        store.sqlite.conn.commit()
    before = _rows(store)
    ref = (record.scope.tenant_id, record.scope.agent_id, record.scope.workspace_id,
           record.scope.user_id, "hermes", record.record_id)

    with pytest.raises(_RepairSkipped, match="source_projection_or_digest_mismatch"):
        store.read_consistent(lambda sqlite: _repair_snapshot(sqlite, ref))
    assert _rows(store) == before


@pytest.mark.parametrize("concurrent_change", ["payload", "other_sql_projection"])
def test_compatible_legacy_snapshot_still_binds_original_digest_and_entire_sql_row(legacy, monkeypatch, concurrent_change):
    store, _, scope = legacy
    raw = json.loads(_rows(store)[0]["payload_json"])
    raw.pop("aliases_version")
    _set_payload(store, raw)
    read = store.read_consistent
    concurrent_rows = []

    def change_after_discovery(callback):
        result = read(callback)
        if concurrent_change == "payload":
            changed = dict(raw, tags=["concurrent writer"])
            _set_payload(store, changed)
        else:
            with store._lock:
                store.sqlite.conn.execute("UPDATE records SET embedding_json='[1.0]'")
                store.sqlite.conn.commit()
        concurrent_rows.extend(_rows(store))
        return result

    monkeypatch.setattr(store, "read_consistent", change_after_discovery)
    report = _repair(store, scope)

    assert report["ok"] is False and report["repaired_count"] == 0
    assert report["outcome_counts"] == {"source_version_changed": 1}
    assert _rows(store) == concurrent_rows
