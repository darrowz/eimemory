"""Durable queue identity and paper promotion remain authoritative on retry."""
from copy import deepcopy
import json

import pytest

from eimemory.api.runtime import Runtime
from eimemory.cli.l1_worker import drain_l1
from eimemory.intake.loop import candidates_to_records
from eimemory.intake.review import review_candidate
from eimemory.knowledge.l1_queue import L1ExtractQueue
from eimemory.metadata import business_metadata
from eimemory.models.records import RecordEnvelope, ScopeRef


PAPER_SCOPE = {"tenant_id": "review2", "agent_id": "fixture", "workspace_id": "paper", "user_id": "owner"}
PAPER_KINDS = ["paper_source", "paper_extract", "claim_card", "entity_record", "relation_record",
               "knowledge_page", "replay_result"]


def paper_candidate(runtime):
    payload = {"source_id": "synthetic-paper", "source_kind": "arxiv", "title": "Synthetic pipeline evidence",
        "uri": "https://arxiv.org/abs/2601.00001", "decision": "candidate", "fingerprint": "review2-paper",
        "summary": "Independent sources are required for the synthetic paper pipeline.",
        "content_excerpt": "Independent sources are required for the synthetic paper pipeline. "
                           "A candidate review must remain authoritative during extraction and compilation."}
    candidate = candidates_to_records([payload], PAPER_SCOPE)[0]
    return runtime.store.append(candidate)


@pytest.mark.parametrize("field, value", [
    ("channel_scope", []), ("channel_scope", False), ("channel_scope", None),
    ("channel_scope", {"tenant_id": False}), ("channel_scope", {"user_id": []}),
    ("channel_scope", {"user_id": 0}), ("channel_scope", {"userId": "other-owner"}),
    ("channel_id", []), ("episode_id", None), ("session_id", []),
])
def test_worker_rejects_malformed_identity_without_defaulting_or_spending_attempts(tmp_path, field, value):
    with Runtime.create(root=tmp_path) as runtime:
        text = "User: 以后回答先给结论，少解释。\nAssistant: 收到。"
        parent = RecordEnvelope.create(kind="memory", title="Synthetic captured turn", summary=text,
            content={"text": text, "memory_type": "conversation"}, scope=ScopeRef(),
            source="hermes.memory", source_id="hermes", meta={"memory_layer": "l0",
                "memory_type": "conversation", "capture_origin": "turn_sync", "runtime_channel": "hermes",
                "session_id": "session", "turn_id": "turn"})
        parent.time.occurred_at = "2026-10-23T01:00:00Z"
        runtime.store.append(parent)
        path = tmp_path / "state" / "l1_extract_queue.json"
        job = {"job_id": "synthetic-job", "status": "queued", "attempts": 0,
               "episode_id": parent.record_id, "channel_scope": {}, "channel_id": "hermes",
               "session_id": "session", "turn_id": "turn", field: value}
        raw = json.dumps({"jobs": [job], "dead": []})
        path.write_text(raw)
        report = drain_l1(root=str(tmp_path))
        current = runtime.store.get_by_exact_ref(parent.record_id, scope=ScopeRef(), source_id="hermes")
        assert report.get("error") == "queue_schema_invalid", report
        assert report["ok"] is False and report["queue_state"] == "unavailable"
        assert path.read_text() == raw
        assert not business_metadata(current.meta).get("l1_extracted_at")
        assert runtime.store.list_records(kinds=["memory"], scope=ScopeRef(), limit=10) == [current]


@pytest.mark.parametrize("raw", [
    '{"jobs": [], "jobs": [{"job_id": "lost", "episode_id": "mem_lost", "status": "queued"}], "dead": []}',
    '{"jobs": [{"job_id": "j", "episode_id": "mem_original", "episode_id": "mem_changed", "status": "queued"}], "dead": []}',
    '{"jobs": [{"job_id": "j", "episode_id": "mem_original", "status": "queued", "channel_scope": {"user_id": NaN}}], "dead": []}',
])
def test_ambiguous_json_is_not_rewritten_as_one_chosen_queue_identity(tmp_path, raw):
    path = tmp_path / "state" / "l1_extract_queue.json"
    path.parent.mkdir(parents=True)
    path.write_text(raw)
    report = drain_l1(root=str(tmp_path))
    assert report["ok"] is False and report["error"] == "queue_corrupt"
    assert report["pending"] is None and report["dead"] is None
    assert path.read_text() == raw


def test_enqueue_rejects_bad_identity_before_changing_existing_work_and_keeps_legacy_defaults(tmp_path):
    queue = L1ExtractQueue(tmp_path / "queue.json")
    queued = queue.enqueue({"episode_id": "legacy-minimal"})
    raw = queue.path.read_bytes()
    with pytest.raises(ValueError, match="queue_job_invalid"):
        queue.enqueue({"episode_id": "another-episode", "channel_scope": {"user_id": []}})
    assert queue.path.read_bytes() == raw
    seen = []
    report = queue.drain_report(lambda job: seen.append(job["episode_id"]), limit=1)
    assert seen == [queued["episode_id"]]
    assert report["processed"] == 1 and report["pending"] == 0


@pytest.mark.parametrize("decision", ["reject", "quarantine", "approve"])
def test_paper_promotion_cannot_overwrite_a_review_committed_during_extraction(tmp_path, monkeypatch, decision):
    with Runtime.create(root=tmp_path) as runtime, Runtime.create(root=tmp_path) as reviewer_runtime:
        candidate = paper_candidate(runtime)
        import eimemory.api.runtime as runtime_module
        extract = runtime_module.extract_paper_memory
        reviewed = None

        def concurrent_review(**kwargs):
            nonlocal reviewed
            reviewed = deepcopy(review_candidate(reviewer_runtime, candidate.record_id, decision, "reviewer",
                note="Decision committed while promotion was preparing its results", scope=PAPER_SCOPE).to_dict())
            return extract(**kwargs)

        monkeypatch.setattr(runtime_module, "extract_paper_memory", concurrent_review)
        result = None
        failure = None
        try:
            result = runtime.promote_paper_candidate(candidate, scope=PAPER_SCOPE)
        except ValueError as exc:
            failure = str(exc)
        current = runtime.store.get_by_exact_ref(candidate.record_id, scope=candidate.scope,
                                                 source_id=candidate.source_id)
        assert current.to_dict() == reviewed
        assert runtime.store.list_records(kinds=PAPER_KINDS, scope=PAPER_SCOPE, limit=100) == []
        assert failure == "paper_promotion_candidate_changed_retry_required" or (result and result.get("ok") is False)
        monkeypatch.setattr(runtime_module, "extract_paper_memory", extract)
        retried = runtime.promote_paper_candidate(current, scope=PAPER_SCOPE)
        assert retried["ok"] is (decision == "approve")
        if decision == "approve":
            after = runtime.store.get_by_exact_ref(candidate.record_id, scope=candidate.scope, source_id=candidate.source_id)
            assert [item["decision"] for item in after.meta["review_history"]] == ["approve", "paper_promote"]
        else:
            assert runtime.store.list_records(kinds=PAPER_KINDS, scope=PAPER_SCOPE, limit=100) == []


def test_promotion_compiles_unpublished_claims_and_publishes_their_exact_versions(tmp_path, monkeypatch):
    from eimemory.knowledge.evidence_contracts import versioned_record_ref
    import eimemory.api.runtime as runtime_module
    compile_paper = runtime_module.compile_paper_knowledge
    with Runtime.create(root=tmp_path) as runtime:
        candidate = paper_candidate(runtime)
        observed = []

        def inspect_preparation(**kwargs):
            assert runtime.store.list_records(kinds=PAPER_KINDS, scope=PAPER_SCOPE, limit=100) == []
            claims = kwargs["claim_records"]
            assert claims
            observed.extend(claim.record_id for claim in claims)
            return compile_paper(**kwargs)

        monkeypatch.setattr(runtime_module, "compile_paper_knowledge", inspect_preparation)
        report = runtime.promote_paper_candidate(candidate, scope=PAPER_SCOPE)
        assert report["ok"] and observed
        records = runtime.store.list_records(kinds=PAPER_KINDS, scope=PAPER_SCOPE, limit=100)
        assert {record.record_id for record in records} == set(report["record_ids"])
        pages = [record for record in records if record.kind == "knowledge_page"]
        assert pages
        for page in pages:
            for ref in page.content["supporting_claim_refs"]:
                stored = runtime.store.get_by_exact_ref(ref["record_id"], scope=ScopeRef.from_dict(ref["scope"]),
                                                         source_id=ref["source_id"])
                assert versioned_record_ref(stored) == ref
        with pytest.raises(ValueError, match="cannot review terminal candidate"):
            review_candidate(runtime, candidate.record_id, "reject", "too-late-reviewer", scope=PAPER_SCOPE)


@pytest.mark.parametrize("fault", ["record", "outbox"])
def test_promotion_write_failure_rolls_back_the_entire_batch_and_retry_succeeds(tmp_path, monkeypatch, fault):
    with Runtime.create(root=tmp_path) as runtime:
        candidate = paper_candidate(runtime)
        original_candidate = candidate.to_dict()
        method = "upsert" if fault == "record" else "enqueue_export"
        original = getattr(runtime.store.sqlite, method)
        calls = []

        def fail_second_write(*args, **kwargs):
            result = original(*args, **kwargs)
            calls.append(True)
            if len(calls) == 2:
                raise RuntimeError("synthetic_paper_publication_failure")
            return result

        with monkeypatch.context() as fail:
            fail.setattr(runtime.store.sqlite, method, fail_second_write)
            with pytest.raises(RuntimeError, match="synthetic_paper_publication_failure"):
                runtime.promote_paper_candidate(candidate, scope=PAPER_SCOPE)
        current = runtime.store.get_by_exact_ref(candidate.record_id, scope=candidate.scope, source_id=candidate.source_id)
        assert current.to_dict() == original_candidate
        assert runtime.store.list_records(kinds=PAPER_KINDS, scope=PAPER_SCOPE, limit=100) == []
        assert runtime.store.sqlite.conn.execute("SELECT COUNT(*) FROM export_outbox WHERE state='pending'").fetchone()[0] == 0
        report = runtime.promote_paper_candidate(candidate, scope=PAPER_SCOPE)
        assert report["ok"] is True
        records = runtime.store.list_records(kinds=PAPER_KINDS, scope=PAPER_SCOPE, limit=100)
        assert {record.record_id for record in records} == set(report["record_ids"])
        assert len(records) == len(report["record_ids"])


@pytest.mark.parametrize("bound, value", [("MAX_PROMOTION_RECORDS", 2), ("MAX_PROMOTION_BYTES", 256)])
def test_unpublishable_oversized_batch_does_not_leave_partial_recall_evidence(tmp_path, monkeypatch, bound, value):
    from eimemory.intake import promotion_batch
    with Runtime.create(root=tmp_path) as runtime:
        candidate = paper_candidate(runtime)
        original = candidate.to_dict()
        monkeypatch.setattr(promotion_batch, bound, value)
        with pytest.raises(ValueError, match="paper_promotion_batch_too_large"):
            runtime.promote_paper_candidate(candidate, scope=PAPER_SCOPE)
        assert runtime.store.list_records(kinds=PAPER_KINDS, scope=PAPER_SCOPE, limit=100) == []
        current = runtime.store.get_by_exact_ref(candidate.record_id, scope=candidate.scope, source_id=candidate.source_id)
        assert current.to_dict() == original
