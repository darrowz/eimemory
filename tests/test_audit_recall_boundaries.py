from copy import deepcopy
import json

import pytest

from eimemory.api.runtime import Runtime
from eimemory.adapters.runtime.service import AgentRuntimeMemoryService
from eimemory.adapters.runtime.channel import resolve_channel_scope
from eimemory.models.records import LinkRef, RecordEnvelope, ScopeRef


SCOPE = dict(tenant_id="default", agent_id="hongtu", workspace_id="embodied", user_id="alice")


def test_personal_ingest_only_supersedes_exact_scope_and_source(tmp_path):
    runtime = Runtime.create(root=tmp_path)
    def ingest(user, source="alpha"):
        return runtime.memory.ingest(text="Orchid research must cite primary sources.", title="Orchid research policy",
            memory_type="rule", scope={**SCOPE, "user_id": user}, source_id=source, force_capture=True)
    shared = ingest("")
    bob = ingest("bob")
    other_source = ingest("alice", "beta")
    previous = ingest("alice")
    successor = ingest("alice")
    for record in (shared, bob, other_source):
        assert runtime.store.get_by_exact_ref(record.record_id, scope=record.scope, source_id=record.source_id).status == "active"
    assert runtime.store.get_by_exact_ref(previous.record_id, scope=previous.scope, source_id="alpha").status == "superseded"
    assert [link.target_id for link in successor.links if link.relation == "supersedes"] == [previous.record_id]
    runtime.close()


@pytest.mark.parametrize("conflict", ["", "payload", "scope", "source"])
def test_real_explicit_rule_capture_deduplicates_bundle_sections(tmp_path, monkeypatch, conflict):
    monkeypatch.setenv("EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY", "fixture-only-0123456789-abcdefghijklmnopqrstuvwxyz")
    runtime = Runtime.create(root=tmp_path)
    rule = RecordEnvelope.create(kind="rule", title="reply style preference", summary="The user prefers concise replies and primary source citations.",
        scope=ScopeRef.from_dict(resolve_channel_scope("codex", SCOPE)), source="fixture.verified", source_id="shared", meta={"force_capture": True})
    runtime.store.append(rule)
    service = AgentRuntimeMemoryService(runtime)
    if conflict:
        recall = runtime.memory.recall
        def conflicting_recall(**kwargs):
            bundle = recall(**kwargs)
            duplicate = deepcopy(rule)
            if conflict == "payload":
                duplicate.summary = "Conflicting private payload"
            elif conflict == "scope":
                duplicate.scope.user_id = "bob"
            else:
                duplicate.source = "conflicting.authority"
            bundle.rules.append(duplicate)
            return bundle
        monkeypatch.setattr(runtime.memory, "recall", conflicting_recall)
    result = service.prefetch(channel="codex", scope=SCOPE, query="reply style preference", limit=3,
        explicit_request={"session_id": "audit", "request_id": "rule", "acceptance_generated": True})
    capture = runtime.store.get_by_id(result["capture"]["record_id"], scope=result["scope"])
    if conflict:
        assert result["ok"] is False
        assert result["reason"] == "explicit_delivered_reference_conflict"
        assert capture.content["references"] == []
        runtime.close()
        return
    assert result["ok"] is True, result
    assert [ref["record_ref"] for ref in capture.content["references"]] == [rule.record_id]
    runtime.close()


@pytest.mark.parametrize("admission", [False, True])
@pytest.mark.parametrize("boundary", ["source", "scope"])
def test_episode_backrefs_obey_source_allowlist_independent_of_admission(tmp_path, admission, monkeypatch, boundary):
    monkeypatch.setenv("EIMEMORY_LIGHTWEIGHT_ADMISSION_ENABLED", str(int(admission)))
    runtime = Runtime.create(root=tmp_path)
    if not admission:
        runtime.memory.recall_engine.relevance_admission = None
    episode = RecordEnvelope.create(kind="raw_chunk", title="FORBIDDEN_EPISODE_TITLE", summary="Private Orchid conversation",
        scope=ScopeRef.from_dict({**SCOPE, "user_id": "" if boundary == "scope" else "alice"}),
        source="FORBIDDEN_EPISODE_SOURCE", source_id="forbidden" if boundary == "source" else "allowed")
    runtime.store.append(episode)
    memory = runtime.memory.ingest(text="Orchid service connection uses rpc.example:7443.", title="Orchid service connection",
        memory_type="durable_fact", scope=SCOPE, source_id="allowed", force_capture=True,
        links=[LinkRef(relation="derived_from", target_kind="record", target_id=episode.record_id)])
    bundle = runtime.memory.recall(query="Orchid service connection", scope=SCOPE,
        task_context={"source_ids": ["allowed"], "exact_scope_only": boundary == "scope"})
    assert memory.record_id in [item.record_id for item in bundle.items]
    evidence = json.dumps(bundle.explanation.get("cascade_evidence", []))
    assert episode.record_id not in evidence
    assert episode.title not in evidence
    assert episode.source not in evidence
    compact_evidence = json.dumps(bundle.to_compact_dict().get("evidence", []))
    assert episode.record_id not in compact_evidence
    runtime.close()
