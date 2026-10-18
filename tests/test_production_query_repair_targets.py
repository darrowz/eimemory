from __future__ import annotations

from copy import deepcopy

import pytest

from eimemory.api.runtime import Runtime
from eimemory.evaluation.production_query_dataset import ACCEPTED_SOURCE, LABEL_EVIDENCE_SOURCE, PENDING_SOURCE
from eimemory.evaluation.production_query_repair import repair_production_query_channel_scopes
from eimemory.models.records import RecordEnvelope
from test_production_query_repair import BASE_SCOPE, _flatten_channel_evidence, _seed_accepted_cases, operator_receipt_key


def _packets(runtime):
    return [record for record in runtime.store.list_records(
        kinds=["evaluation_packet"], scope=BASE_SCOPE, status="active", limit=500)
        if record.source in {ACCEPTED_SOURCE, LABEL_EVIDENCE_SOURCE, PENDING_SOURCE}]


def _seed(runtime):
    _seed_accepted_cases(runtime, channels=("openclaw",), total=2)
    packets = _packets(runtime)
    case = deepcopy(next(record.content["case"] for record in packets if record.source == ACCEPTED_SOURCE))
    return case, packets


def test_targeted_repair_preserves_non_target_invalid_packets_and_gold(tmp_path):
    runtime = Runtime.create(root=tmp_path)
    try:
        case, packets = _seed(runtime)
        other = next(record for record in packets if record.source == PENDING_SOURCE
                     and record.content["case_id"] != case["case_id"])
        other.content["scope"]["agent_id"] = "foreign-agent"
        runtime.store.rewrite(other, previous_scope=other.scope)
        before = {record.record_id: record.to_dict() for record in _packets(runtime)}
        gold = runtime.store.get_by_id(case["labels"][0]["record_ref"]).to_dict()
        result = repair_production_query_channel_scopes(
            runtime, scope=BASE_SCOPE, target_cases=[case], persist_receipt=False)
        assert result["ok"], result
        assert result["scanned_count"] == result["target_record_count"] == 3
        assert result["non_target_record_count"] == 3
        assert result["already_correct_count"] == 3
        assert result["quarantined_count"] == result["repaired_count"] == 0
        assert {record.record_id: record.to_dict() for record in _packets(runtime)} == before
        assert runtime.store.get_by_id(case["labels"][0]["record_ref"]).to_dict() == gold
    finally:
        runtime.close()


@pytest.mark.parametrize("source,reason", [
    (ACCEPTED_SOURCE, "repair_target_accepted_case_missing"),
    (LABEL_EVIDENCE_SOURCE, "repair_target_label_reference_missing"),
    (PENDING_SOURCE, "repair_target_pending_reference_missing"),
])
def test_targeted_repair_missing_required_packet_fails_closed(tmp_path, source, reason):
    runtime = Runtime.create(root=tmp_path)
    try:
        case, packets = _seed(runtime)
        target = next(record for record in packets if record.source == source and (
            record.content.get("case_id") == case["case_id"]
            or record.content.get("case", {}).get("case_id") == case["case_id"]
            or record.record_id == case["labels"][0]["provenance"]["evidence_ref"]))
        target.status = "deleted"
        runtime.store.rewrite(target, previous_scope=target.scope)
        before = {record.record_id: record.to_dict() for record in _packets(runtime)}
        result = repair_production_query_channel_scopes(
            runtime, scope=BASE_SCOPE, target_cases=[case], persist_receipt=False)
        assert not result["ok"] and result["blocked_reason"] == reason, result
        assert result["repaired_count"] == result["quarantined_count"] == 0
        assert {record.record_id: record.to_dict() for record in _packets(runtime)} == before
    finally:
        runtime.close()


@pytest.mark.parametrize("field,value", [("user_id", "other-user"), ("agent_id", "other-agent"),
                                        ("workspace_id", "other-workspace"), ("tenant_id", "other-tenant")])
def test_target_dataset_never_authorizes_another_boundary(tmp_path, field, value):
    runtime = Runtime.create(root=tmp_path)
    try:
        case, _packets_before = _seed(runtime)
        case["scope"][field] = value
        result = repair_production_query_channel_scopes(
            runtime, scope=BASE_SCOPE, target_cases=[case], persist_receipt=False)
        assert not result["ok"] and result["blocked_reason"] == "repair_target_case_boundary_invalid"
    finally:
        runtime.close()


def test_targeted_repair_requires_exact_accepted_snapshot(tmp_path):
    runtime = Runtime.create(root=tmp_path)
    try:
        case, packets = _seed(runtime)
        accepted = next(record for record in packets if record.content.get("case") == case)
        accepted.content["case"]["query_features"]["intent"] = "changed event"
        runtime.store.rewrite(accepted, previous_scope=accepted.scope)
        result = repair_production_query_channel_scopes(
            runtime, scope=BASE_SCOPE, target_cases=[case], persist_receipt=False)
        assert not result["ok"] and result["blocked_reason"] == "repair_target_accepted_case_mismatch"
    finally:
        runtime.close()


def test_targeted_repair_rejects_tampered_label_without_quarantine(tmp_path):
    runtime = Runtime.create(root=tmp_path)
    try:
        case, packets = _seed(runtime)
        label = next(record for record in packets if record.record_id == case["labels"][0]["provenance"]["evidence_ref"])
        label.content["grade"] = 0
        runtime.store.rewrite(label, previous_scope=label.scope)
        result = repair_production_query_channel_scopes(
            runtime, scope=BASE_SCOPE, target_cases=[case], persist_receipt=False)
        assert not result["ok"] and result["conflict_count"] > 0
        assert result["quarantined_count"] == result["repaired_count"] == 0
    finally:
        runtime.close()


def test_targeted_repair_accepts_only_configured_agent_rename(tmp_path, monkeypatch):
    import eimemory.identity as identity
    monkeypatch.setattr(identity, "HONGTU_AGENT_ID", "hongtu")
    monkeypatch.setenv("EIMEMORY_LEGACY_AGENT_IDS", "old-hongtu")
    runtime = Runtime.create(root=tmp_path)
    try:
        case, packets = _seed(runtime)
        pending = next(record for record in packets if record.source == PENDING_SOURCE
                       and record.content["case_id"] == case["case_id"])
        pending.content["scope"]["agent_id"] = "old-hongtu"
        runtime.store.rewrite(pending, previous_scope=pending.scope)
        before = pending.to_dict()
        result = repair_production_query_channel_scopes(
            runtime, scope=BASE_SCOPE, target_cases=[case], persist_receipt=False)
        assert result["ok"] and result["already_correct_count"] == 3, result
        assert runtime.store.get_by_id(pending.record_id).to_dict() == before
    finally:
        runtime.close()


def test_targeted_repair_does_not_page_or_overflow_unrelated_history(tmp_path, monkeypatch):
    runtime = Runtime.create(root=tmp_path)
    try:
        case, _packets_before = _seed(runtime)
        sqlite = runtime.store.sqlite
        count = sqlite.count_records_by_meta_value
        monkeypatch.setattr(sqlite, "count_records_by_meta_value", lambda **kwargs: 10001 + count(**kwargs))
        def no_history_paging(**kwargs):
            raise AssertionError("targeted repair must use exact references")
        monkeypatch.setattr(sqlite, "list_records_by_meta_value", no_history_paging)
        result = repair_production_query_channel_scopes(
            runtime, scope=BASE_SCOPE, target_cases=[case], persist_receipt=False)
        assert result["ok"] and result["target_record_count"] == 3 and result["overflow_count"] == 0, result
        empty = repair_production_query_channel_scopes(
            runtime, scope=BASE_SCOPE, target_cases=[], persist_receipt=False)
        assert empty["ok"] and empty["scanned_count"] == empty["target_record_count"] == 0, empty
        assert empty["non_target_record_count"] > 10000
    finally:
        runtime.close()


def test_discovered_accepted_targets_restore_first_adoption_without_unknown_pending(tmp_path):
    runtime = Runtime.create(root=tmp_path)
    try:
        _seed_accepted_cases(runtime, channels=("hermes",), total=3)
        assert len(_flatten_channel_evidence(runtime, channels=("hermes",))) == 9
        pending = next(record for record in _packets(runtime) if record.source == PENDING_SOURCE)
        unknown = RecordEnvelope.from_dict(pending.to_dict())
        unknown.record_id = "unknown-pending-retain-unchanged"
        unknown.content["case_id"] = "unknown-case"
        unknown.content["capture_ref"] = "missing-decision"
        unknown.content["scope"]["user_id"] = "foreign-user"
        runtime.store.append(unknown)
        before = unknown.to_dict()
        result = repair_production_query_channel_scopes(
            runtime, scope=BASE_SCOPE, discover_accepted_targets=True, persist_receipt=False)
        assert result["ok"], result
        assert result["discovered_accepted_case_count"] == result["target_case_count"] == 3
        assert result["target_record_count"] == result["repaired_count"] == 9
        assert result["non_target_record_count"] == 1 and result["quarantined_count"] == 0
        assert runtime.store.get_by_id(unknown.record_id, scope=BASE_SCOPE).to_dict() == before
    finally:
        runtime.close()


def test_discovery_without_existing_accepted_cases_never_repairs_unknown_graph(tmp_path):
    runtime = Runtime.create(root=tmp_path)
    try:
        _case, packets = _seed(runtime)
        for record in packets:
            if record.source == ACCEPTED_SOURCE:
                record.status = "deleted"
                runtime.store.rewrite(record, previous_scope=record.scope)
        before = {record.record_id: record.to_dict() for record in _packets(runtime)}
        result = repair_production_query_channel_scopes(
            runtime, scope=BASE_SCOPE, discover_accepted_targets=True, persist_receipt=False)
        assert result["ok"] and result["target_record_count"] == result["discovered_accepted_case_count"] == 0
        assert result["non_target_record_count"] == 4
        assert {record.record_id: record.to_dict() for record in _packets(runtime)} == before
    finally:
        runtime.close()
