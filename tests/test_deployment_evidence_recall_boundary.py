"""Declared deployment receipts stay accessible without becoming business memory."""
from dataclasses import asdict

import pytest

from eimemory.api.runtime import Runtime
from eimemory.contracts.recall_boundary import default_recall_blocked_lane
from eimemory.evaluation.controlled_memory_benchmark import build_controlled_memory_benchmark
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.recall import build_recall_index_document


SCOPE = ScopeRef(tenant_id="test", agent_id="primary", workspace_id="channel", user_id="owner")


@pytest.mark.parametrize("metadata", ["legacy", "business_layer", "content"])
@pytest.mark.parametrize("legacy_projection", [False, True])
def test_typed_deployment_evidence_has_one_operational_contract(tmp_path, monkeypatch, metadata, legacy_projection):
    monkeypatch.setenv("EIMEMORY_LIGHTWEIGHT_ADMISSION_ENABLED", "0")
    memory_type = "deployment_evidence"
    content = {"text":"Receipt checksum is eight hundred and ninety one."}
    meta = {"force_capture": True}
    if metadata == "content":
        content["memory_type"] = memory_type
    elif metadata == "business_layer":
        meta["business_meta"] = {"memory_type":memory_type}
    else:
        meta["memory_type"] = memory_type
    with Runtime.create(root=tmp_path) as runtime:
        record = runtime.store.append(RecordEnvelope.create(kind="memory", title="Receipt checksum reference",
            summary=content["text"], content=content, meta=meta, source="hermes.memory", source_id="hermes", scope=SCOPE))
        if legacy_projection:
            # Reproduce existing installations: the immutable record declares
            # its correct type, while the old derived index called it primary.
            with runtime.store.locked() as sql:
                sql.execute("UPDATE recall_index SET lane='primary',visibility='default',source_class='default' "
                            "WHERE record_id=? AND source_id='hermes'", (record.record_id,))
                sql.commit()
        document = build_recall_index_document(record)
        assert document.source_class == "deployment" and document.lane == "operational"
        assert document.visibility == "evidence_only"
        assert default_recall_blocked_lane(record.meta, record.content) == "audit_record"
        context = {"source_ids":["hermes"], "target_source_id":"hermes", "exact_scope_only":True}
        default = runtime.memory.recall(query=record.title, scope=asdict(SCOPE), task_context=context, limit=5)
        assert record.record_id not in {item.record_id for item in default.items}
        explicit = runtime.memory.recall(query=record.title, scope=asdict(SCOPE), limit=5,
            task_context={**context, "include_operational_recall":True, "include_evidence_only":True})
        assert record.record_id in {item.record_id for item in explicit.items}
        dataset = build_controlled_memory_benchmark(runtime, scope=SCOPE, source_ids=["hermes"])
        assert dataset["cases"] == []
        assert dataset["dataset_generation"]["skipped_reason_counts"] == {"source_record_evidence_only":1}


def test_deployment_words_do_not_reclassify_a_real_operator_fact(tmp_path, monkeypatch):
    monkeypatch.setenv("EIMEMORY_LIGHTWEIGHT_ADMISSION_ENABLED", "0")
    with Runtime.create(root=tmp_path) as runtime:
        record = runtime.store.append(RecordEnvelope.create(kind="memory", title="Hermes deployment replay",
            summary="The customer reviews deployment logs on the first Monday of each month.",
            content={"memory_type":"fact", "text":"The customer reviews deployment logs on the first Monday of each month."},
            meta={"memory_type":"fact", "force_capture":True}, source="operator.memory", source_id="hermes", scope=SCOPE))
        document = build_recall_index_document(record)
        assert document.source_class == "default" and document.visibility == "default"
        dataset = build_controlled_memory_benchmark(runtime, scope=SCOPE, source_ids=["hermes"])
        assert [case["source_binding"]["record_id"] for case in dataset["cases"]] == [record.record_id]
