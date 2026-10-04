from types import SimpleNamespace

import pytest

from eimemory.governance.capability.correction_replay import record_user_correction_replay
from eimemory.models.records import ScopeRef
from eimemory.storage.runtime_store import RuntimeStore

SCOPE = ScopeRef(tenant_id="synthetic", agent_id="agent", workspace_id="workspace", user_id="user")
INPUT = {"text": "Check evidence before answering", "context": "Synthetic stale answer", "expected_behavior": "Query synthetic evidence"}


def run(store):
    return record_user_correction_replay(SimpleNamespace(store=store), INPUT, scope=SCOPE)


def test_repeated_correction_uses_canonical_lesson(tmp_path):
    store = RuntimeStore(tmp_path)
    first, second = run(store), run(store)
    assert second["lesson_record_id"] == first["lesson_record_id"]
    for result in (first, second):
        assert result["ok"] is True
        assert result["replay"]["verdict"] == "not_run"
        for key in ("lesson_record_id", "replay_record_id", "ground_truth_rule_id"):
            assert store.get_by_exact_ref(result[key], scope=SCOPE, source_id="default") is not None
        replay = store.get_by_exact_ref(result["replay_record_id"], scope=SCOPE, source_id="default")
        assert replay.content["lesson_record_id"] == result["lesson_record_id"]


@pytest.mark.parametrize("stage,prefix", [("reflection", 0), ("replay_result", 1), ("rule", 2)])
def test_unknown_append_does_not_continue_or_publish_unverified_id(tmp_path, monkeypatch, stage, prefix):
    store = RuntimeStore(tmp_path)
    original = store.append
    calls = []
    def append(row):
        calls.append(row.kind)
        persisted = original(row)
        return None if row.kind == stage else persisted
    monkeypatch.setattr(store, "append", append)
    result = run(store)
    assert result["ok"] is False
    ids = [result[key] for key in ("lesson_record_id", "replay_record_id", "ground_truth_rule_id")]
    assert all(ids[:prefix]) and not any(ids[prefix:])
    assert len(calls) == prefix + 1
    assert result["persistence"]["may_have_additional_writes"] is True


def test_edge_export_after_commit_is_partial_and_keeps_committed_edges(tmp_path, monkeypatch):
    store = RuntimeStore(tmp_path)
    original = store.upsert_memory_edges
    def fail_after_commit(edges):
        original(edges)
        raise OSError("SYNTHETIC_PRIVATE_DIAGNOSTIC")
    monkeypatch.setattr(store, "upsert_memory_edges", fail_after_commit)
    result = run(store)
    assert result["ok"] is False
    assert result["persistence"]["status"] == "partial"
    assert result["persistence"]["stages"]["edges"] == "unknown"
    assert len(store.list_memory_edges(scope=SCOPE, record_ids=[result["lesson_record_id"]])) == 4
    assert "SYNTHETIC_PRIVATE_DIAGNOSTIC" not in str(result)
