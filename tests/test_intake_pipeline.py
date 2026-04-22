from __future__ import annotations

from eimemory.api.runtime import Runtime
from eimemory.intake.loop import candidates_to_records
from eimemory.intake.pipeline import promote_paper_candidate
from eimemory.models.records import RecordEnvelope, ScopeRef


def _paper_candidate() -> dict:
    return {
        "source_id": "paper-feed-1",
        "source_kind": "arxiv",
        "title": "Memory-Only Paper Intake",
        "uri": "https://arxiv.org/abs/2601.00001",
        "summary": "Memory-only pipelines convert paper candidates into durable paper memory.",
        "content_excerpt": (
            "Memory-only pipelines convert paper candidates into durable paper memory. "
            "They preserve source identity and produce claim cards for later compilation."
        ),
        "decision": "candidate",
        "fingerprint": "paper-fingerprint-1",
        "provenance": {"source": "unit-test"},
        "quality": {"score": 0.9},
    }


def test_promote_paper_candidate_runs_source_extract_compile_for_dict(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"tenant_id": "tenant-a", "agent_id": "agent-a"}

    report = promote_paper_candidate(runtime, _paper_candidate(), scope)

    assert report["ok"] is True
    assert report["paper_source_id"]
    assert report["extracted_record_count"] > 0
    assert report["compiled_record_count"] > 0
    assert report["skipped_reason"] == ""
    assert report["paper_source_id"] in report["record_ids"]
    assert runtime.store.list_records(kinds=["paper_source"], scope=scope)
    assert runtime.store.list_records(kinds=["claim_card"], scope=scope)
    assert runtime.store.list_records(kinds=["knowledge_page"], scope=scope)


def test_promote_paper_candidate_accepts_knowledge_candidate_record(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"tenant_id": "tenant-a", "agent_id": "agent-a"}
    record = candidates_to_records([_paper_candidate()], scope)[0]

    report = promote_paper_candidate(runtime, record, scope)

    assert report["ok"] is True
    assert runtime.store.list_records(kinds=["paper_source"], scope=scope)[0].record_id == report["paper_source_id"]
    assert runtime.store.list_records(kinds=["claim_card"], scope=scope)
    assert runtime.store.list_records(kinds=["knowledge_page"], scope=scope)


def test_promote_paper_candidate_skips_rejected_or_quarantined_candidates(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"tenant_id": "tenant-a", "agent_id": "agent-a"}
    rejected = {**_paper_candidate(), "decision": "rejected", "reason": "manual_reject"}

    report = promote_paper_candidate(runtime, rejected, scope)

    assert report == {
        "ok": False,
        "paper_source_id": "",
        "extracted_record_count": 0,
        "compiled_record_count": 0,
        "skipped_reason": "rejected_candidate",
        "record_ids": [],
    }
    assert runtime.store.list_records(kinds=["paper_source"], scope=scope) == []


def test_promote_paper_candidate_skips_record_from_different_scope(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    candidate = RecordEnvelope.create(
        kind="knowledge_candidate",
        title="Foreign paper",
        summary="A paper summary with enough content to otherwise promote.",
        detail="A paper body with enough reusable content to otherwise promote.",
        scope=ScopeRef(tenant_id="foreign", agent_id="main"),
        status="candidate",
        content={
            "source_kind": "arxiv",
            "title": "Foreign paper",
            "summary": "A paper summary with enough content to otherwise promote.",
            "content_excerpt": "A paper body with enough reusable content to otherwise promote.",
            "uri": "https://arxiv.org/abs/2501.12345",
        },
    )
    runtime.store.append(candidate)

    report = promote_paper_candidate(runtime, candidate, {"tenant_id": "local", "agent_id": "main"})

    assert report["ok"] is False
    assert report["skipped_reason"] == "scope_mismatch"


def test_promote_paper_candidate_deduplicates_paper_source(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = {"tenant_id": "tenant-a", "agent_id": "agent-a"}
    candidate = _paper_candidate()

    first = promote_paper_candidate(runtime, candidate, scope)
    second = promote_paper_candidate(runtime, dict(candidate), scope)

    paper_sources = runtime.store.list_records(kinds=["paper_source"], scope=scope)
    assert first["ok"] is True
    assert second["ok"] is True
    assert first["paper_source_id"] == second["paper_source_id"]
    assert len(paper_sources) == 1
