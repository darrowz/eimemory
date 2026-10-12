"""Native extraction observations and ref-only readback; no remote providers."""
from contextlib import closing
import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from eimemory.api.memory import MemoryAPI
from eimemory.knowledge.l1_diagnostics import (
    MAX_REFS, SCHEMA, normalize_l1_extraction_diagnostics, read_l1_extraction_diagnostics,
)
from eimemory.knowledge.l1_pipeline import extract_l1_from_l0_record
from eimemory.knowledge.l1_queue import L1ExtractQueue
from eimemory.knowledge.sediment import L1ExtractorUnavailable, extract_l1_atoms
from eimemory.metadata import business_metadata
from eimemory.models.records import LinkRef, RecordEnvelope, ScopeRef
from eimemory.storage.runtime_store import RuntimeStore


SCOPE = ScopeRef("synthetic", "audit", "isolated", "synthetic-owner")
TEXT = "Synthetic project Falcon account belongs to Alice."


class FakeLLM:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def complete(self, **kwargs):
        self.calls += 1
        return SimpleNamespace(text=json.dumps(self.payload))


def parent_record():
    text = "User: " + TEXT + "\nAssistant: Acknowledged."
    parent = RecordEnvelope.create(kind="memory", title="Synthetic captured turn", summary=text,
        content={"text": text, "memory_type": "conversation"}, scope=SCOPE,
        source="synthetic.turn", source_id="synthetic-private-source",
        meta={"memory_type": "conversation", "memory_layer": "l0", "capture_origin": "turn_sync"})
    parent.record_id = "mem_synthetic_parent"
    return parent


def child_record(parent, index=0):
    child = RecordEnvelope.create(kind="memory", title="Synthetic durable fact", summary=TEXT,
        content={"text": TEXT, "memory_type": "fact"}, scope=parent.scope,
        source="synthetic.l1", source_id=parent.source_id,
        meta={"memory_layer": "l1", "capture_origin": "l1_extract"},
        evidence=[parent.record_id],
        links=[LinkRef("derived_from", "memory", parent.record_id)])
    child.record_id = f"mem_synthetic_child_{index}"
    return child


@pytest.fixture(autouse=True)
def prohibit_real_model(monkeypatch):
    def prohibited(*args, **kwargs):
        raise AssertionError("Remote LLM execution prohibited")
    monkeypatch.setattr("eimemory.llm.command_client.CommandLLMClient.complete", prohibited)


@pytest.mark.parametrize("payload,raw,filters", [
    ([], 0, {}),
    ([{"content": TEXT, "type": "fact", "priority": 1}], 1, {"priority_below_threshold": 1}),
    ([{"content": "Where is Falcon?", "type": "fact", "priority": 90}], 1, {"content_rejected": 1}),
])
def test_explicit_empty_and_filtered_output_have_distinct_actual_observations(payload, raw, filters):
    client, sink = FakeLLM(payload), {}
    assert extract_l1_atoms(user_text=TEXT, source_message_ids=["mem_synthetic_parent"],
        use_llm=True, llm=client, fallback_heuristic=False, diagnostics_sink=sink) == []
    result = normalize_l1_extraction_diagnostics(parent=parent_record(), extraction=sink,
        completion_status="no_memory")
    assert client.calls == 1 and result["model_called"] is True
    assert result["extractor"] == "llm"
    assert result["input_admission"] == "accepted"
    assert result["raw_candidate_count"] == raw and result["admitted_count"] == 0
    assert result["filter_reason_counts"] == filters
    assert result["filter_counts_complete"] is True


@pytest.mark.parametrize("user,assistant,reason", [
    ("Where is Falcon?", "", "input_question_only"),
    ("", TEXT, "empty_user"),
])
def test_input_exclusion_records_no_model_call(user, assistant, reason):
    client, sink = FakeLLM([]), {}
    assert extract_l1_atoms(user_text=user, assistant_text=assistant, use_llm=True, llm=client,
        fallback_heuristic=False, diagnostics_sink=sink) == []
    result = normalize_l1_extraction_diagnostics(parent=parent_record(), extraction=sink,
        completion_status="no_memory")
    assert client.calls == 0 and result["model_called"] is False
    assert result["input_admission"] == "rejected" and result["input_reason"] == reason
    assert result["raw_candidate_count"] is None and result["admitted_count"] == 0
    assert result["extractor"] == "unknown"


def test_invalid_model_output_never_becomes_an_observed_zero():
    sink = {}
    with pytest.raises(L1ExtractorUnavailable):
        extract_l1_atoms(user_text=TEXT, use_llm=True, llm=FakeLLM([{"bad": "payload"}]),
            fallback_heuristic=False, diagnostics_sink=sink)
    result = normalize_l1_extraction_diagnostics(parent=parent_record(), extraction=sink,
        completion_status="unavailable", failure_reason="l1_llm_invalid_output")
    assert result["model_called"] is True
    assert result["raw_candidate_count"] is result["admitted_count"] is None
    assert result["completion_status"] == "unavailable"


def test_projection_bounds_private_refs_without_carrying_text_or_identity():
    parent = parent_record()
    children = [child_record(parent, i) for i in range(MAX_REFS + 4)]
    result = normalize_l1_extraction_diagnostics(parent=parent,
        extraction={"model_called": True, "extractor": "llm", "raw_candidate_count": len(children),
            "admitted_count": len(children), "input_admission": "accepted", "input_reason": "accepted"},
        adjudication={"store_count": len(children), "skip_count": 0, "update_count": 0,
            "source_order_stale_count": 0}, child_records=children, completion_status="stored")
    refs = result["children"]
    assert refs["observed_count"] == refs["unique_count"] == len(children)
    assert refs["recorded_count"] == len(refs["refs"]) == MAX_REFS and refs["truncated"] is True
    serialized = json.dumps(result)
    assert all(text not in serialized for text in (TEXT, SCOPE.user_id, SCOPE.workspace_id, parent.source_id))
    assert result["source_binding"]["record_id"] == parent.record_id
    assert all(len(ref["payload_digest"]) == 64 for ref in refs["refs"])


@pytest.mark.parametrize("mutation", ["source", "scope", "backref", "layer"])
def test_child_refs_require_actual_same_partition_parent_link(mutation):
    parent, child = parent_record(), child_record(parent_record())
    if mutation == "source":
        child.source_id = "other-private-source"
    elif mutation == "scope":
        child.scope = ScopeRef("synthetic", "audit", "isolated", "other-owner")
    elif mutation == "backref":
        child.links, child.evidence = [], []
    else:
        child.meta["memory_layer"] = "l0"
    refs = normalize_l1_extraction_diagnostics(parent=parent, child_records=[child])["children"]
    assert refs["observed_count"] == 1 and refs["missing_ref_count"] == 1
    assert refs["refs"] == [] and refs["recorded_count"] == 0


def test_unknown_and_malformed_counts_are_not_zero_or_free_form_details():
    result = normalize_l1_extraction_diagnostics(parent=parent_record(), extraction={
        "model_called": 1, "extractor": {"secret": TEXT}, "input_reason": TEXT,
        "input_admission": [TEXT], "raw_candidate_count": True, "admitted_count": -1,
        "filter_reason_counts": {"priority_below_threshold": 2, TEXT: 7, "task_scope": True}},
        adjudication={"store_count": False, "skip_count": None, "update_count": "0"},
        failure_reason=TEXT)
    assert result["model_called"] is result["raw_candidate_count"] is result["admitted_count"] is None
    assert result["extractor"] == result["input_reason"] == result["input_admission"] == "unknown"
    assert result["filter_reason_counts"] == {"priority_below_threshold": 2}
    assert result["filter_counts_complete"] is False
    assert all(result["adjudication"][key] is None for key in ("store_count", "skip_count", "update_count"))
    assert TEXT not in json.dumps(result)


def test_actual_source_order_guard_codes_survive_ref_only_failure_projection():
    from eimemory.contracts import l1_source_order
    from eimemory.knowledge import l1_pipeline
    codes = set()
    for module in (l1_source_order, l1_pipeline):
        source = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        for call in ast.walk(source):
            if (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                    and call.func.id == "L1SourceOrderBlocked" and call.args
                    and isinstance(call.args[0], ast.Constant)
                    and isinstance(call.args[0].value, str)):
                codes.add(call.args[0].value)
    assert {"l1_source_order_unknown", "l1_source_parent_changed",
            "l1_source_parent_unavailable", "l1_legacy_source_binding_unknown"} <= codes
    for code in codes:
        result = normalize_l1_extraction_diagnostics(parent=parent_record(),
            completion_status="blocked", failure_reason=code)
        assert result["failure_reason"] == code
    assert normalize_l1_extraction_diagnostics(parent=parent_record(),
        completion_status="blocked", failure_reason="arbitrary_provider_private_detail")["failure_reason"] == "unknown"


def test_legacy_completion_does_not_infer_a_model_or_recreate_child_refs():
    parent = parent_record()
    parent.meta.update(l1_extract_version="l1_extract.v2", l1_extraction_status="stored",
                       l1_extractor="llm", l1_atom_count=1, l1_candidate_count=1)
    result = read_l1_extraction_diagnostics(parent)
    assert result["status"] == "not_recorded"
    assert result["extractor"] == "unknown" and result["model_called"] is None
    assert result["raw_candidate_count"] is result["admitted_count"] is None
    assert "children" not in result


def test_readback_is_independent_bound_to_material_and_reapplies_privacy_projection():
    parent = parent_record()
    report = normalize_l1_extraction_diagnostics(parent=parent, extraction={"model_called": False},
        child_records=[child_record(parent)], completion_status="stored")
    report["unsafe_detail"] = TEXT
    report["children"]["refs"][0]["unsafe_scope"] = SCOPE.user_id
    parent.meta["l1_extraction_diagnostics"] = report
    result = read_l1_extraction_diagnostics(parent)
    assert TEXT not in json.dumps(result) and SCOPE.user_id not in json.dumps(result)
    result["children"]["refs"].clear()
    assert len(report["children"]["refs"]) == 1
    parent.content["text"] = "Changed source material"
    assert read_l1_extraction_diagnostics(parent)["status"] == "source_binding_changed"


def test_native_queue_completion_and_reopen_preserve_exact_ref_linkage(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        parent = store.append(parent_record())
        queue = L1ExtractQueue(tmp_path / "queue.json")
        queue.enqueue({"episode_id": parent.record_id})
        client = FakeLLM([{"content": TEXT, "type": "fact", "priority": 90}])
        report = queue.drain_report(lambda job: extract_l1_from_l0_record(MemoryAPI(store), parent,
            use_llm=True, llm=client, fallback_heuristic=False), limit=1)
        assert report["processed"] == 1 and report["pending"] == report["failed"] == 0
    with closing(RuntimeStore(tmp_path)) as store:
        current = store.get_by_exact_ref(parent.record_id, scope=parent.scope, source_id=parent.source_id)
        result = read_l1_extraction_diagnostics(current)
        assert result["schema"] == SCHEMA and result["model_called"] is True
        assert result["raw_candidate_count"] == result["admitted_count"] == 1
        assert result["completion_status"] == "stored"
        assert result["adjudication"]["store_count"] == 1
        assert result["children"]["recorded_count"] == 1
        ref = result["children"]["refs"][0]
        child = store.get_by_exact_ref(ref["record_id"], scope=current.scope, source_id=current.source_id)
        assert child is not None and child.status == "active"
        assert child.time.occurred_at == current.time.occurred_at
        assert current.record_id in child.evidence
        assert any(link.relation == "derived_from" and link.target_id == current.record_id for link in child.links)
        assert store.get_by_exact_ref(child.record_id, scope=ScopeRef(user_id="other"), source_id=current.source_id) is None
        assert store.get_by_exact_ref(child.record_id, scope=current.scope, source_id="other") is None


@pytest.mark.parametrize("payload,raw,filters", [
    ([], 0, {}),
    ([{"content": TEXT, "type": "fact", "priority": 1}], 1, {"priority_below_threshold": 1}),
])
def test_native_empty_completion_persists_observations_without_inventing_children(tmp_path, payload, raw, filters):
    with closing(RuntimeStore(tmp_path)) as store:
        parent = store.append(parent_record())
        assert extract_l1_from_l0_record(MemoryAPI(store), parent, use_llm=True,
            llm=FakeLLM(payload), fallback_heuristic=False) == []
        current = store.get_by_exact_ref(parent.record_id, scope=parent.scope, source_id=parent.source_id)
        result = read_l1_extraction_diagnostics(current)
        assert result["model_called"] is True and result["raw_candidate_count"] == raw
        assert result["completion_status"] == "no_memory" and result["filter_reason_counts"] == filters
        assert result["children"]["refs"] == [] and result["children"]["observed_count"] == 0


def test_native_failed_queue_preserves_failure_observation_without_completion(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        parent = store.append(parent_record())
        queue = L1ExtractQueue(tmp_path / "queue.json")
        queue.enqueue({"episode_id": parent.record_id})
        report = queue.drain_report(lambda job: extract_l1_from_l0_record(MemoryAPI(store), parent,
            use_llm=True, llm=FakeLLM([{"invalid": "provider payload"}]),
            fallback_heuristic=False), limit=1)
        assert report["processed"] == 0 and report["failed"] == report["pending"] == 1
        current = store.get_by_exact_ref(parent.record_id, scope=parent.scope, source_id=parent.source_id)
        assert not business_metadata(current.meta).get("l1_extracted_at")
        result = read_l1_extraction_diagnostics(current)
        assert result["completion_status"] == "unavailable" and result["model_called"] is True
        assert result["failure_reason"] == "l1_llm_invalid_output"
        assert result["raw_candidate_count"] is result["admitted_count"] is None
        assert result["children"]["observed_count"] is None
        assert "provider payload" not in json.dumps(result)


def test_native_conflict_skip_reads_exact_existing_target_and_no_new_child(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        parent = store.append(parent_record())
        api = MemoryAPI(store)
        written = extract_l1_from_l0_record(api, parent, use_llm=True,
            llm=FakeLLM([{"content": TEXT, "type": "fact", "priority": 90}]),
            fallback_heuristic=False)
        old_id = written[0]["record_id"]
        second = parent_record()
        second.record_id = "mem_synthetic_second_parent"
        second = store.append(second)

        class SkipLLM(FakeLLM):
            def complete(self, **kwargs):
                if "candidate_pool" in kwargs["user_prompt"]:
                    self.calls += 1
                    return SimpleNamespace(text=json.dumps([{
                        "record_id": "new-0", "action": "skip", "target_ids": [old_id]}]))
                return super().complete(**kwargs)

        assert extract_l1_from_l0_record(api, second, use_llm=True,
            llm=SkipLLM([{"content": TEXT, "type": "fact", "priority": 90}]),
            fallback_heuristic=False) == []
        current = store.get_by_exact_ref(second.record_id, scope=second.scope, source_id=second.source_id)
        result = read_l1_extraction_diagnostics(current)
        assert result["completion_status"] == "no_change"
        assert result["adjudication"]["skip_count"] == 1
        assert result["adjudication"]["store_count"] == result["adjudication"]["update_count"] == 0
        refs = result["adjudication"]["skipped_targets"]["refs"]
        assert len(refs) == 1 and refs[0]["record_id"] == old_id
        assert result["children"]["observed_count"] == 0 and result["children"]["refs"] == []
        old = store.get_by_exact_ref(old_id, scope=current.scope, source_id=current.source_id)
        assert old.status == "active" and parent.record_id in old.evidence
