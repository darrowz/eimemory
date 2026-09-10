"""Synthetic deterministic adapter captures; no private replay fixtures."""
from copy import deepcopy
from contextlib import closing

import pytest

from eimemory.api.memory import MemoryAPI
from eimemory.knowledge.l1_pipeline import extract_l1_from_l0_record, persist_l1_atoms
from eimemory.knowledge.sediment import extract_l1_atoms
from eimemory.metadata import business_metadata
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.storage.runtime_store import RuntimeStore


def capture(store, episode="mem_synthetic_capture", text="我现在使用的手机型号是 Nova Q，设备型号 NQ-42。"):
    text = "User: " + text + "\nAssistant: 已记录。"
    parent = RecordEnvelope.create(kind="memory", title="Synthetic captured turn", summary=text,
        content={"text": text, "memory_type": "conversation"},
        scope=ScopeRef(user_id="synthetic-owner"), source="synthetic.memory", source_id="synthetic",
        meta={"memory_type": "conversation", "memory_layer": "l0", "capture_origin": "turn_sync",
              "authoritative": True, "idempotency_key": "adapter.synthetic:" + episode,
              "runtime_channel": "synthetic", "session_id": "session", "turn_id": episode})
    parent.record_id = episode
    parent.time.occurred_at = "2025-02-03T04:05:06Z"
    return store.append(parent)


def test_deterministic_capture_completion_and_stale_retry(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        parent = capture(store)
        before = deepcopy(parent.to_dict())
        api = MemoryAPI(store)
        written = extract_l1_from_l0_record(api, parent)
        assert written
        current = store.get_by_id(parent.record_id, scope=parent.scope)
        assert business_metadata(current.meta).get("l1_extracted_at")
        assert extract_l1_from_l0_record(api, current) == []
        assert extract_l1_from_l0_record(api, RecordEnvelope.from_dict(before)) == []
        for field in ("content", "summary", "source", "source_id", "scope", "evidence", "links"):
            assert current.to_dict()[field] == before[field]
        assert current.time.occurred_at == before["time"]["occurred_at"]
        atom = store.get_by_id(written[0]["record_id"], scope=parent.scope)
        assert atom.time.occurred_at == parent.time.occurred_at
        assert atom.source_id == parent.source_id
        assert parent.record_id in atom.evidence
        assert parent.record_id in business_metadata(atom.meta)["source_message_ids"]
        assert any(link.target_id == parent.record_id for link in atom.links)


def test_partial_ingest_failure_restart_reuses_successful_atom(tmp_path, monkeypatch):
    text = "我现在使用的手机型号是 Nova Q，设备型号 NQ-42。以后回答先给结论，少解释。"
    with closing(RuntimeStore(tmp_path)) as store:
        parent = capture(store, text=text)
        api = MemoryAPI(store)
        atoms = [*extract_l1_atoms(user_text="我现在使用的手机型号是 Nova Q，设备型号 NQ-42。", source_message_ids=[parent.record_id]),
                 *extract_l1_atoms(user_text="以后回答先给结论，少解释。", source_message_ids=[parent.record_id])]
        monkeypatch.setattr("eimemory.knowledge.l1_pipeline.extract_l1_atoms", lambda **kwargs: atoms)
        original = api.ingest
        successful = []
        def fail_after_first(**kwargs):
            if successful:
                raise RuntimeError("synthetic interruption")
            result = original(**kwargs)
            successful.append(result.record_id)
            return result
        monkeypatch.setattr(api, "ingest", fail_after_first)
        with pytest.raises(RuntimeError, match="synthetic interruption"):
            extract_l1_from_l0_record(api, parent)
        assert not business_metadata(store.get_by_id(parent.record_id, scope=parent.scope).meta).get("l1_extracted_at")
    with closing(RuntimeStore(tmp_path)) as store:
        written = extract_l1_from_l0_record(MemoryAPI(store), parent)
        assert successful[0] in {item["record_id"] for item in written}
        rows = store.list_records(kinds=["memory"], scope=parent.scope, limit=100)
        atoms = [r for r in rows if business_metadata(r.meta).get("memory_layer") == "l1"]
        assert len(atoms) == len(written) == 2
        assert all(r.time.occurred_at == parent.time.occurred_at for r in atoms)


@pytest.mark.parametrize("field,value", [("scope", ScopeRef(user_id="intruder")),
    ("source_id", "other"), ("source", "other.memory")])
def test_unauthorized_parent_cannot_extract(tmp_path, field, value):
    with closing(RuntimeStore(tmp_path)) as store:
        parent = capture(store)
        forged = deepcopy(parent)
        setattr(forged, field, value)
        with pytest.raises(ValueError, match="l1_parent"):
            extract_l1_from_l0_record(MemoryAPI(store), forged)
        assert len(store.list_records(kinds=["memory"], scope=parent.scope, limit=100)) == 1


def test_separate_episodes_and_correction_keep_distinct_identity(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        api = MemoryAPI(store)
        parents = [capture(store, "mem_synthetic_" + str(i), text) for i, text in enumerate([
            "我现在使用的手机型号是 Nova Q，设备型号 NQ-42。",
            "我现在使用的手机型号是 Nova Q，设备型号 NQ-42。",
            "我现在使用的手机型号是 Nova R，设备型号 NR-43。"])]
        results = [extract_l1_from_l0_record(api, parent) for parent in parents]
        assert all(results)
        assert len({r[0]["record_id"] for r in results}) == 3


def test_persist_rejects_source_partition_mismatch(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        parent = capture(store)
        atoms = extract_l1_atoms(user_text=parent.summary, source_message_ids=[parent.record_id])
        with pytest.raises(ValueError, match="l1_parent"):
            persist_l1_atoms(MemoryAPI(store), atoms=atoms, episode_id=parent.record_id,
                scope=parent.scope, channel_id="other")


@pytest.mark.parametrize("failure_stage", ["event_time", "completion"])
def test_atomic_write_failure_restart_repairs_without_duplicate(tmp_path, monkeypatch, failure_stage):
    with closing(RuntimeStore(tmp_path)) as store:
        parent = capture(store)
        original = store.sqlite.upsert
        def interrupted(record, **kwargs):
            is_completion = bool(business_metadata(record.meta).get("l1_extracted_at"))
            is_time = (business_metadata(record.meta).get("memory_layer") == "l1"
                       and record.time.occurred_at == parent.time.occurred_at)
            original(record, **kwargs)
            if (failure_stage == "completion" and is_completion) or (failure_stage == "event_time" and is_time):
                raise RuntimeError("synthetic transaction interruption")
        monkeypatch.setattr(store.sqlite, "upsert", interrupted)
        with pytest.raises(RuntimeError, match="synthetic transaction interruption"):
            extract_l1_from_l0_record(MemoryAPI(store), parent)
        current = store.get_by_id(parent.record_id, scope=parent.scope)
        assert not business_metadata(current.meta).get("l1_extracted_at")
        ids_before = {r.record_id for r in store.list_records(kinds=["memory"], scope=parent.scope, limit=100)}
    with closing(RuntimeStore(tmp_path)) as store:
        written = extract_l1_from_l0_record(MemoryAPI(store), parent)
        assert len(written) == 1
        assert written[0]["record_id"] in ids_before
        assert store.get_by_id(written[0]["record_id"], scope=parent.scope).time.occurred_at == parent.time.occurred_at
        assert extract_l1_from_l0_record(MemoryAPI(store), parent) == []


def test_shared_read_visibility_does_not_authorize_extraction(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        parent = capture(store)
        parent.scope.user_id = ""
        # Write the shared fixture with the state primitive, not insert-once append.
        def shared(sqlite):
            sqlite.upsert(parent, commit=False)
            return None, [parent], []
        store.mutate_records_atomically(shared)
        forged = deepcopy(parent)
        forged.scope.user_id = "different-reader"
        with pytest.raises(ValueError, match="l1_parent"):
            extract_l1_from_l0_record(MemoryAPI(store), forged)
        atoms = extract_l1_atoms(turn_text=parent.summary, source_message_ids=[parent.record_id])
        with pytest.raises(ValueError, match="l1_parent"):
            persist_l1_atoms(MemoryAPI(store), atoms=atoms, episode_id=parent.record_id,
                scope=forged.scope, channel_id=parent.source_id)
