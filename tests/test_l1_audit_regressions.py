"""Offline L0-to-L1 closure regressions using only isolated synthetic stores."""
from contextlib import closing
from dataclasses import asdict
import json
from types import SimpleNamespace

import pytest

from eimemory.api.memory import MemoryAPI
from eimemory.knowledge.l1_conflict import ConflictDecision, L1ConflictJudgeUnavailable, adjudicate_l1_atoms
from eimemory.knowledge.l1_pipeline import extract_l1_from_l0_record, persist_l1_atoms
from eimemory.knowledge.l1_queue import L1ExtractQueue, MAX_ATTEMPTS
from eimemory.knowledge.sediment import L1Atom
from eimemory.metadata import business_metadata, normalize_metadata, split_metadata
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.storage.runtime_store import RuntimeStore, SearchResult


SCOPE = ScopeRef(tenant_id="synthetic", agent_id="audit", workspace_id="offline", user_id="owner")
TEXT = "Project Falcon account belongs to Alice."


@pytest.fixture(autouse=True)
def forbid_external_model(monkeypatch):
    monkeypatch.setattr("eimemory.llm.command_client.CommandLLMClient.complete",
                        lambda *a, **k: pytest.fail("External model call prohibited"))


def atom(text=TEXT):
    return L1Atom(text=text, title=text, memory_type="fact", semantic_key="fact:falcon:owner",
                  category="fact", source_message_ids=())


def capture(store):
    text = "User: " + TEXT + "\nAssistant: Acknowledged."
    parent = RecordEnvelope.create(kind="memory", title="Synthetic L0 turn", summary=text,
        content={"text": text, "memory_type": "conversation"}, scope=SCOPE,
        source="synthetic.turn", source_id="synthetic",
        meta={"memory_layer": "l0", "memory_type": "conversation", "capture_origin": "turn_sync"})
    parent.time.occurred_at = "2026-10-01T00:00:00Z"
    return store.append(parent)


class Extractor:
    def complete(self, **kwargs):
        assert not kwargs["user_prompt"].startswith("{"), "No conflict judgment after incomplete retrieval"
        return SimpleNamespace(text=json.dumps([{"content": TEXT, "type": "fact", "priority": 90}]))


@pytest.mark.parametrize("partial", [False, True])
def test_degraded_candidates_cannot_certify_no_conflict_or_complete_l0(tmp_path, monkeypatch, partial):
    with closing(RuntimeStore(tmp_path)) as store:
        parent = capture(store)
        candidate = RecordEnvelope.create(kind="memory", title=TEXT, summary=TEXT,
            content={"text": TEXT, "memory_type": "fact"}, scope=SCOPE,
            source="synthetic.l1", source_id="synthetic", meta={"memory_layer": "l1"})
        monkeypatch.setattr(store, "search", lambda **kwargs: SearchResult(
            [candidate] if partial else [], degraded=True, degraded_reason="recall_budget_exhausted"))
        with pytest.raises(L1ConflictJudgeUnavailable, match="l1_conflict_candidates_incomplete"):
            extract_l1_from_l0_record(MemoryAPI(store), parent, use_llm=True,
                                      llm=Extractor(), fallback_heuristic=False)
        current = store.get_by_exact_ref(parent.record_id, scope=SCOPE, source_id="synthetic")
        assert not business_metadata(current.meta).get("l1_extracted_at")
        assert business_metadata(current.meta)["l1_extraction_diagnostics"]["failure_reason"] == "l1_conflict_candidates_incomplete"
        assert len(store.list_records(kinds=["memory"], scope=SCOPE, limit=10)) == 1


@pytest.mark.parametrize("strict", [False, True])
def test_conflict_target_string_is_one_record_id(strict):
    candidate = RecordEnvelope.create(kind="memory", title=TEXT, summary=TEXT,
        content={"text": TEXT, "memory_type": "fact"}, scope=SCOPE,
        source="synthetic.l1", source_id="synthetic", meta={"memory_layer": "l1"})
    candidate.record_id = "mem_falcon_owner"
    api = SimpleNamespace(store=SimpleNamespace(search=lambda **kwargs: [candidate]))
    class Judge:
        def complete(self, **kwargs):
            return SimpleNamespace(text=json.dumps([{"record_id": "new-0", "action": "update",
                "target_ids": candidate.record_id, "merged_content": "Project Falcon account belongs to Bob."}]))
    decisions = adjudicate_l1_atoms(api, [atom()], scope=asdict(SCOPE), source_id="synthetic",
                                    llm=Judge(), strict=strict)
    assert decisions[0].action == "update"
    assert decisions[0].target_ids == (candidate.record_id,)


def test_missing_l0_parent_cannot_create_unbound_l1(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        with pytest.raises(ValueError, match="l1_source_parent_unavailable"):
            persist_l1_atoms(MemoryAPI(store), atoms=[atom()], episode_id="mem_absent_source",
                             scope=SCOPE, channel_id="synthetic")
        assert store.list_records(kinds=["memory"], scope=SCOPE, limit=10) == []


def test_one_transient_failure_is_attempted_once_per_drain_and_other_jobs_progress(tmp_path):
    queue = L1ExtractQueue(tmp_path / "queue.json")
    failing = queue.enqueue({"episode_id": "mem_transient"})
    queue.enqueue({"episode_id": "mem_ready"})
    calls = []
    def handle(job):
        calls.append(job["episode_id"])
        if job["episode_id"] == "mem_transient":
            raise RuntimeError("synthetic transient provider failure")
    report = queue.drain_report(handle, limit=10)
    assert calls == ["mem_transient", "mem_ready"]
    assert report["processed"] == 1 and report["failed"] == 1
    assert report["pending"] == 1 and report["dead"] == report["newly_dead"] == 0
    payload = json.loads(queue.path.read_text())
    assert payload["jobs"][0]["job_id"] == failing["job_id"]
    assert payload["jobs"][0]["attempts"] == 1
    for _ in range(MAX_ATTEMPTS - 1):
        report = queue.drain_report(handle, limit=10)
    assert report["dead"] == report["newly_dead"] == 1 and report["pending"] == 0
    assert calls.count("mem_transient") == MAX_ATTEMPTS


@pytest.mark.parametrize("retry_case", ["resume", "missing_receipt", "atom_digest", "output_digest",
    "decision_digest", "source", "missing_binding", "judge_store", "judge_update",
    "judge_same_text_store", "judge_same_text_targets", "judge_skip", "judge_identical"])
def test_retry_after_merged_update_commit_keeps_final_fact_and_completes_source(tmp_path, monkeypatch, retry_case):
    merged = "Project Falcon account belongs to Alice, replacing Bob's previous ownership."
    with closing(RuntimeStore(tmp_path)) as store:
        api = MemoryAPI(store)
        old = api.ingest(text="Project Falcon account belongs to Bob.", memory_type="fact",
            title="Falcon account owner", scope=asdict(SCOPE), source="synthetic.l1",
            source_id="synthetic", force_capture=True, meta={"memory_layer": "l1"})
        old.time.occurred_at = "2026-09-30T00:00:00Z"
        store.mutate_records_atomically(lambda sqlite: (sqlite.upsert(old, commit=False), [old], []))
        parent = capture(store)
        class MergingLLM:
            decision = "update"
            store_output = TEXT
            def complete(self, **kwargs):
                if kwargs["user_prompt"].startswith("{"):
                    prompt = json.loads(kwargs["user_prompt"])
                    return SimpleNamespace(text=json.dumps([{"record_id": "new-0", "action": self.decision,
                        "target_ids": prompt["new_memories"][0]["related_ids"] if self.decision == "update" else [],
                        "merged_content": merged if self.decision == "update" else self.store_output}]))
                return SimpleNamespace(text=json.dumps([{"content": TEXT, "type": "fact", "priority": 90}]))
        original = store.sqlite.upsert
        def fail_completion(record, **kwargs):
            original(record, **kwargs)
            if record.record_id == parent.record_id and business_metadata(record.meta).get("l1_extracted_at"):
                raise RuntimeError("synthetic completion interruption")
        monkeypatch.setattr(store.sqlite, "upsert", fail_completion)
        with pytest.raises(RuntimeError, match="synthetic completion interruption"):
            extract_l1_from_l0_record(api, parent, use_llm=True, llm=MergingLLM(), fallback_heuristic=False)
        monkeypatch.setattr(store.sqlite, "upsert", original)
        children = [item for item in store.list_records(kinds=["memory"], scope=SCOPE, limit=20)
                    if item.record_id not in {parent.record_id, old.record_id}]
        assert len(children) == 1 and children[0].summary == merged
        child_id = children[0].record_id
    # The receipt must survive process/store restart, not just an in-memory retry.
    with closing(RuntimeStore(tmp_path)) as store:
        api = MemoryAPI(store)
        child = store.get_by_exact_ref(child_id, scope=SCOPE, source_id="synthetic")
        if retry_case in {"missing_receipt", "atom_digest", "output_digest", "decision_digest", "source", "missing_binding"}:
            child_meta, runtime_meta = split_metadata(child.meta)
            if retry_case == "missing_receipt":
                child_meta.pop("l1_write_receipt")
            elif retry_case in {"atom_digest", "output_digest", "decision_digest"}:
                child_meta["l1_write_receipt"][retry_case] = "0" * 64
            elif retry_case == "source":
                child.source = "synthetic.forged"
            else:
                child_meta.pop("source_event_binding")
            child.meta = normalize_metadata({"business_meta": child_meta, "runtime_meta": runtime_meta})
            store.mutate_records_atomically(lambda sqlite: (sqlite.upsert(child, commit=False), [child], []))
        client = MergingLLM()
        if retry_case in {"judge_store", "judge_update", "judge_same_text_store", "judge_same_text_targets", "judge_skip"}:
            candidate = api.ingest(text="Project Falcon account ownership details are documented.",
                title="Falcon owner details", memory_type="fact", scope=asdict(SCOPE),
                source="synthetic.l1", source_id="synthetic", force_capture=True, meta={"memory_layer": "l1"})
            candidate.time.occurred_at = "2026-09-29T00:00:00Z"
            store.mutate_records_atomically(lambda sqlite: (sqlite.upsert(candidate, commit=False), [candidate], []))
            client.decision = ("store" if retry_case in {"judge_store", "judge_same_text_store"}
                               else "skip" if retry_case == "judge_skip" else "update")
            if retry_case in {"judge_same_text_store", "judge_skip"}:
                client.store_output = merged
            if retry_case == "judge_update":
                merged = "Project Falcon account belongs to Carol according to a changed judgment."
            candidate_before = candidate.to_dict()
        if retry_case == "judge_identical":
            # Replay the exact already-authorized decision at the persistence
            # boundary. Duplicate target notation has the same canonical intent.
            monkeypatch.setattr("eimemory.knowledge.l1_pipeline.adjudicate_l1_atoms",
                lambda memory_api, atoms, **kwargs: [ConflictDecision(action="update", atom=atoms[0],
                    target_ids=(old.record_id, old.record_id), merged_content=merged,
                    merged_type="fact", origin="judge")])
        monkeypatch.setattr("eimemory.knowledge.l1_pipeline._reconcile_existing_l1_update",
            lambda *a, **k: pytest.fail("A receipted judgment must never replay supersession"))
        before = store.get_by_exact_ref(child_id, scope=SCOPE, source_id="synthetic").to_dict()
        if retry_case not in {"resume", "judge_identical"}:
            with pytest.raises(ValueError, match="l1_(existing_atom_conflict|source_parent_changed)"):
                extract_l1_from_l0_record(api, parent, use_llm=True, llm=client, fallback_heuristic=False)
            assert store.get_by_exact_ref(child_id, scope=SCOPE, source_id="synthetic").to_dict() == before
            current = store.get_by_exact_ref(parent.record_id, scope=SCOPE, source_id="synthetic")
            assert not business_metadata(current.meta).get("l1_extracted_at")
            if retry_case in {"judge_store", "judge_update", "judge_same_text_store", "judge_same_text_targets", "judge_skip"}:
                assert store.get_by_exact_ref(candidate.record_id, scope=SCOPE, source_id="synthetic").to_dict() == candidate_before
            return
        result = extract_l1_from_l0_record(api, parent, use_llm=True, llm=client, fallback_heuristic=False)
        assert [item["record_id"] for item in result] == [child_id]
        assert result[0]["conflict_action"] == "update"
        assert store.get_by_exact_ref(child_id, scope=SCOPE, source_id="synthetic").to_dict() == before
        assert store.get_by_exact_ref(old.record_id, scope=SCOPE, source_id="synthetic").status == "superseded"
        current = store.get_by_exact_ref(parent.record_id, scope=SCOPE, source_id="synthetic")
        assert business_metadata(current.meta)["l1_extraction_status"] == "stored"
        assert business_metadata(current.meta)["l1_extraction_diagnostics"]["adjudication"]["update_count"] == 1


@pytest.mark.parametrize("status", ["superseded", "archived", "draft"])
def test_committed_inactive_child_is_not_reported_as_written(tmp_path, monkeypatch, status):
    with closing(RuntimeStore(tmp_path)) as store:
        api, parent = MemoryAPI(store), capture(store)
        original = store.sqlite.upsert
        def fail_completion(record, **kwargs):
            original(record, **kwargs)
            if record.record_id == parent.record_id and business_metadata(record.meta).get("l1_extracted_at"):
                raise RuntimeError("synthetic completion interruption")
        monkeypatch.setattr(store.sqlite, "upsert", fail_completion)
        with pytest.raises(RuntimeError, match="synthetic completion interruption"):
            extract_l1_from_l0_record(api, parent, use_llm=True, llm=Extractor(), fallback_heuristic=False)
        monkeypatch.setattr(store.sqlite, "upsert", original)
        child = next(item for item in store.list_records(kinds=["memory"], scope=SCOPE, limit=10)
                     if item.record_id != parent.record_id)
        child.status = status
        store.mutate_records_atomically(lambda sqlite: (sqlite.upsert(child, commit=False), [child], []))
        assert extract_l1_from_l0_record(api, parent, use_llm=True, llm=Extractor(), fallback_heuristic=False) == []
        assert store.get_by_exact_ref(child.record_id, scope=SCOPE, source_id="synthetic").status == status
        current = store.get_by_exact_ref(parent.record_id, scope=SCOPE, source_id="synthetic")
        assert business_metadata(current.meta)["l1_extraction_status"] == "no_change"
        assert business_metadata(current.meta)["l1_atom_count"] == 0
