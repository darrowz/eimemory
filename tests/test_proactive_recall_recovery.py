from __future__ import annotations

import pytest

from eimemory.adapters.runtime.channel import resolve_channel_scope
from eimemory.api.runtime import Runtime
from eimemory.retrieval.proactive import ProactiveRecallService
from eimemory.retrieval.lightweight_admission import LightweightAdmission, LightweightConfig
from eimemory.retrieval.evidence_fragments import POLICY, evidence_fragments
from eimemory.retrieval.postgres_vector import candidate_record_keyword_text


SCOPE = {"tenant_id": "audit", "agent_id": "audit", "workspace_id": "audit", "user_id": "audit"}
RELEASE = {"release_commit": "a" * 40, "release_version": "audit",
           "deployment_receipt_id": "audit-receipt", "release_session_id": "audit-release"}
QUERY = "What does project Borealis require?"


@pytest.fixture
def runtime(tmp_path):
    with Runtime.create(root=tmp_path) as instance:
        instance.proactive = ProactiveRecallService(instance, release_identity=RELEASE, control_percent=0)
        yield instance


def _remember(runtime):
    return runtime.memory.ingest(
        text="Project Borealis requires primary-source citations.", memory_type="preference",
        title="Borealis requirements", scope=resolve_channel_scope("codex", SCOPE),
        source="codex.memory", source_id="codex",
    )


def _decide(runtime, turn="1"):
    return runtime.proactive.decide(channel="codex", scope=SCOPE, source_ids=["codex"],
                                    query=QUERY, session_id=f"session-{turn}", query_id=f"turn-{turn}")


@pytest.mark.parametrize("revision_status", ["available", "missing", "failed"])
def test_new_memory_invalidates_empty_candidate_cache(runtime, monkeypatch, revision_status):
    source = runtime.memory.recall_engine.candidate_source
    if revision_status == "missing":
        monkeypatch.setattr(source, "authority_revision", None)
    elif revision_status == "failed":
        def unavailable():
            raise RuntimeError("revision unavailable")
        monkeypatch.setattr(source, "authority_revision", unavailable)
    assert _decide(runtime)["items"] == []

    record = _remember(runtime)
    second = _decide(runtime, "2")

    assert [item["record_id"] for item in second["items"]] == [record.record_id]


def test_modified_memory_is_reranked_instead_of_reusing_its_old_score(runtime):
    record = _remember(runtime)
    first = _decide(runtime)
    assert [item["record_id"] for item in first["items"]] == [record.record_id]
    record.title = "Cinnamon toast recipe"
    record.summary = "Bake cinnamon toast for breakfast."
    record.detail = record.summary
    record.content["text"] = record.summary
    runtime.store.append(record)

    second = _decide(runtime, "2")

    assert second["items"] == []
    assert "cinnamon" not in second["context"].lower()


def test_unchanged_empty_cache_and_completed_no_evidence_decision_remain_reusable(runtime, monkeypatch):
    original = runtime.memory.recall
    calls = []

    def counted(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(runtime.memory, "recall", counted)
    first = _decide(runtime)
    second = _decide(runtime, "2")
    retry = _decide(runtime)

    assert first["items"] == second["items"] == retry["items"] == []
    assert first["bypassed"] is False and first["decision_id"]
    assert retry["idempotent"] is True and retry["decision_id"] == first["decision_id"]
    assert len(calls) == 1


@pytest.mark.parametrize(("failure", "reason"), [
    (RuntimeError("temporary engine failure"), "recall_engine_failed"),
    (TimeoutError("proactive recall timed out"), "recall_deadline_exceeded"),
    (TimeoutError("proactive recall worker capacity exhausted"), "recall_capacity_exhausted"),
])
def test_transient_failure_can_recover_on_the_same_host_turn(runtime, monkeypatch, failure, reason):
    record = _remember(runtime)
    original = runtime.memory.recall
    calls = []

    def recovering(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise failure
        return original(**kwargs)

    monkeypatch.setattr(runtime.memory, "recall", recovering)
    failed = _decide(runtime)
    recovered = _decide(runtime)
    replay = _decide(runtime)

    assert failed["bypassed"] is True and failed["decision_id"] == ""
    assert failed["retrieval_diagnostics"]["engine"]["reason"] == reason
    assert recovered["bypassed"] is False
    assert [item["record_id"] for item in recovered["items"]] == [record.record_id]
    assert replay["idempotent"] is True and replay["decision_id"] == recovered["decision_id"]
    assert len(calls) == 2


@pytest.mark.parametrize("first_outcome", ["unavailable", "no_evidence"])
def test_real_relevance_gate_failure_retries_but_no_evidence_stays_idempotent(runtime, first_outcome, monkeypatch):
    from eimemory.evaluation import query_input_vault as vault
    monkeypatch.setenv('EIMEMORY_CAPTURE_ORIGINAL_QUERY', '1')
    monkeypatch.delenv('EIMEMORY_CAPTURE_QUERY_SCOPES', raising=False)
    record = _remember(runtime)

    class RecoveringGate(LightweightAdmission):
        calls = 0

        def select(self, items, **kwargs):
            self.calls += 1
            # Isolated index fixture; the real gate still validates authority,
            # original fragments, coverage and cosine on both attempts.
            kwargs['backend_available'] = not (self.calls == 1 and first_outcome == 'unavailable')
            def hints(item):
                fragment = evidence_fragments(candidate_record_keyword_text(item, max_text_chars=16000))[0]
                return {'fragment_policy': POLICY, 'evidence_fragment_id': fragment['id'],
                        'dense_vector_score': .1 if first_outcome == 'no_evidence' else .9}
            kwargs['hints_for'] = hints
            return super().select(items, **kwargs)

    gate = RecoveringGate(LightweightConfig(enabled=True))
    runtime.memory.recall_engine.relevance_admission = gate
    first = _decide(runtime)
    captures = []
    original_capture = vault._capture_query_input
    def counted_capture(*args, **kwargs):
        captures.append(kwargs['decision_id'])
        return original_capture(*args, **kwargs)
    monkeypatch.setattr(vault, '_capture_query_input', counted_capture)
    retry = _decide(runtime)

    assert first["retrieval_diagnostics"]["retrieval_status"] == first_outcome
    if first_outcome == "unavailable":
        assert first["bypassed"] is True and first["decision_id"] == ""
        assert first["retrieval_diagnostics"]["selector"]["dropped_reasons"]["fragment_index_unavailable"] == 1
        assert retry["bypassed"] is False
        assert [item["record_id"] for item in retry["items"]] == [record.record_id]
        assert gate.calls == 2 and captures == [retry['decision_id']]
    else:
        assert first["bypassed"] is False and first["items"] == []
        assert retry["idempotent"] is True and retry["decision_id"] == first["decision_id"]
        assert gate.calls == 1 and captures == []
        assert retry['input_capture'] == first['input_capture']
        assert runtime.store.sqlite.conn.execute(
            "SELECT count(*) FROM records WHERE kind='evaluation_packet'").fetchone()[0] == 0
