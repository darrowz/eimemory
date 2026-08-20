from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest

from eimemory.api.runtime import Runtime
from eimemory.intake.papers import artifacts as paper_artifacts
from eimemory.knowledge.compiler import compile_paper_knowledge
from eimemory.knowledge import refresh as knowledge_refresh
from eimemory.knowledge.projectors import stable_projection_id
from eimemory.models.claim_cards import ClaimCard
from eimemory.models.paper_sources import PaperSource
from eimemory.models.records import RecordEnvelope, ScopeRef


_CANONICAL_TEXT = "Canonical evidence says the OpenClaw runtime must preserve source provenance for recall decisions."


def test_refresh_module_remains_python_311_parseable_and_refresh_ids_stable() -> None:
    """Guard the oldest supported production interpreter against syntax drift."""

    source_path = Path(knowledge_refresh.__file__)
    ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path), feature_version=(3, 11))

    expected_digest = hashlib.sha256(b"paper.source\x1fcompile-digest").hexdigest()[:20]
    assert knowledge_refresh._refresh_run_id("paper.source", "compile-digest") == f"krefresh_{expected_digest}"


@pytest.fixture
def verified_canonical_artifact(monkeypatch):
    """Keep refresh tests focused on the verifier contract, not PDF parsing.

    Artifact integrity itself is covered by the paper PDF pipeline tests.  The
    refresh path only proceeds when this verifier accepts the complete
    PDF/text/manifest tuple.
    """

    def verify(_root, *, pdf_blob_ref: str, normalized_text_ref: str, artifact: dict) -> str:
        assert pdf_blob_ref.startswith("artifacts/papers/")
        assert normalized_text_ref.startswith("artifacts/papers/")
        assert artifact["status"] == "ready"
        return _CANONICAL_TEXT

    monkeypatch.setattr(paper_artifacts, "load_verified_canonical_text", verify, raising=False)


def _source(
    runtime: Runtime,
    *,
    source_id: str,
    scope: ScopeRef,
    canonical_text: bool,
    status: str = "active",
) -> RecordEnvelope:
    normalized_text_ref = ""
    pdf_blob_ref = ""
    artifact = {"status": "not_requested"}
    if canonical_text:
        normalized_text_ref = f"artifacts/papers/{source_id}.txt"
        pdf_blob_ref = f"artifacts/papers/{source_id}.pdf"
        artifact = {
            "status": "ready",
            "pdf_sha256": "fixture-pdf",
            "text_sha256": "fixture-text",
            "manifest_ref": f"artifacts/papers/{source_id}.json",
        }
    record = PaperSource(
        paper_source_id=source_id,
        source_kind="pdf",
        title="OpenClaw knowledge refresh evidence",
        abstract="A canonical source used to test refresh closure.",
        pdf_blob_ref=pdf_blob_ref,
        normalized_text_ref=normalized_text_ref,
        metadata={"artifact": artifact},
    ).to_record(scope=scope)
    record.status = status
    return runtime.store.append(record)


def _claim(
    *,
    claim_id: str,
    source_id: str,
    text: str,
    scope: ScopeRef,
) -> RecordEnvelope:
    return ClaimCard(
        claim_card_id=claim_id,
        paper_source_id=source_id,
        paper_extract_id=f"extract_{source_id}",
        claim_text=text,
        confidence=0.95,
    ).to_record(scope=scope)


def test_refresh_recompiles_safe_claims_and_retires_stale_projections(
    tmp_path,
    verified_canonical_artifact,
) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope_dict = {"agent_id": "knowledge", "workspace_id": "refresh"}
    scope = ScopeRef.from_dict(scope_dict)
    source_id = "paper_refresh_closure"
    try:
        _source(runtime, source_id=source_id, scope=scope, canonical_text=True)
        positive = _claim(
            claim_id="claim_positive",
            source_id=source_id,
            text="OpenClaw runtime recall improves tenant scoped operational decisions.",
            scope=scope,
        )
        negative = _claim(
            claim_id="claim_negative",
            source_id=source_id,
            text="OpenClaw runtime recall does not improve tenant scoped operational decisions.",
            scope=scope,
        )
        safe = _claim(
            claim_id="claim_safe",
            source_id=source_id,
            text="OpenClaw runtime recall must preserve canonical source provenance for operational decisions.",
            scope=scope,
        )
        for claim in (positive, negative, safe):
            runtime.store.append(claim)
        compiled = compile_paper_knowledge(
            paper_source_id=source_id,
            paper_title="OpenClaw knowledge refresh evidence",
            claim_records=[positive, negative, safe],
        )
        page = compiled.to_records(scope=scope)[0]
        runtime.store.append(page)
        first_projection = runtime.project_operational_knowledge(scope=scope_dict)
        page_projection_id = stable_projection_id(page)

        reconciliation = runtime.evolution.reconcile_knowledge(scope=scope_dict)
        stale_page = runtime.store.get_by_id(page.record_id, scope=scope)
        assert stale_page is not None
        contradiction_ids = list(stale_page.content["contradiction_ids"])
        refresh = runtime.refresh_knowledge_pages(scope=scope_dict)
        stale_projection = runtime.store.get_by_id(page_projection_id, scope=scope)
        refreshed_page = runtime.store.get_by_id(page.record_id, scope=scope)
        bundle = runtime.memory.recall(
            query="OpenClaw runtime recall tenant operational decisions",
            scope=scope_dict,
            task_context={"task_type": "chat.reply"},
            limit=10,
        )

        assert first_projection["projected_count"] >= 1
        assert reconciliation["page_refresh_count"] == 1
        assert refresh["recompiled_page_count"] == 1
        assert refresh["retired_projection_count"] >= 3
        assert stale_projection is not None
        assert stale_projection.status == "deprecated"
        assert refreshed_page is not None
        assert refreshed_page.status == "active"
        assert refreshed_page.meta["refresh_state"] == "recompiled"
        assert refreshed_page.content["supporting_claim_ids"] == [safe.record_id]
        assert refreshed_page.content["resolved_contradiction_ids"] == contradiction_ids
        assert refreshed_page.meta["resolved_contradiction_ids"] == contradiction_ids
        assert refreshed_page.provenance["resolved_contradiction_ids"] == contradiction_ids
        assert refreshed_page.content["refresh"]["resolved_contradiction_ids"] == contradiction_ids
        assert not refreshed_page.content.get("contradiction_ids")
        assert not refreshed_page.meta.get("contradiction_ids")
        assert all(item.record_id != page_projection_id for item in bundle.items)

        second_projection = runtime.project_operational_knowledge(scope=scope_dict)
        replacement_projection = runtime.store.get_by_id(page_projection_id, scope=scope)

        assert second_projection["projected_count"] >= 1
        assert replacement_projection is not None
        assert replacement_projection.status == "active"
        assert "does not improve" not in replacement_projection.summary
    finally:
        runtime.close()


def test_refresh_fails_closed_without_canonical_text_and_retires_projection(tmp_path) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope_dict = {"agent_id": "knowledge", "workspace_id": "blocked"}
    scope = ScopeRef.from_dict(scope_dict)
    source_id = "paper_refresh_blocked"
    try:
        _source(runtime, source_id=source_id, scope=scope, canonical_text=False)
        positive = _claim(
            claim_id="claim_blocked_positive",
            source_id=source_id,
            text="OpenClaw runtime recall improves tenant scoped operational decisions.",
            scope=scope,
        )
        negative = _claim(
            claim_id="claim_blocked_negative",
            source_id=source_id,
            text="OpenClaw runtime recall does not improve tenant scoped operational decisions.",
            scope=scope,
        )
        runtime.store.append(positive)
        runtime.store.append(negative)
        page = compile_paper_knowledge(
            paper_source_id=source_id,
            paper_title="OpenClaw knowledge refresh evidence",
            claim_records=[positive, negative],
        ).to_records(scope=scope)[0]
        runtime.store.append(page)
        runtime.project_operational_knowledge(scope=scope_dict)
        page_projection_id = stable_projection_id(page)

        runtime.evolution.reconcile_knowledge(scope=scope_dict)
        refresh = runtime.refresh_knowledge_pages(scope=scope_dict)
        blocked_page = runtime.store.get_by_id(page.record_id, scope=scope)
        stale_projection = runtime.store.get_by_id(page_projection_id, scope=scope)

        assert refresh["recompiled_page_count"] == 0
        assert refresh["blocked_page_count"] == 1
        assert refresh["blocked"][0]["reason"] == "blocked_missing_canonical_text"
        assert blocked_page is not None
        assert blocked_page.status == "needs_refresh"
        assert blocked_page.meta["refresh_state"] == "blocked"
        assert stale_projection is not None
        assert stale_projection.status == "deprecated"

        first_updated_at = blocked_page.time.updated_at
        first_refresh = dict(blocked_page.content["refresh"])
        retry = runtime.refresh_knowledge_pages(scope=scope_dict)
        retried_page = runtime.store.get_by_id(page.record_id, scope=scope)

        assert retry["blocked_page_count"] == 1
        assert retried_page is not None
        assert retried_page.time.updated_at == first_updated_at
        assert retried_page.content["refresh"] == first_refresh
    finally:
        runtime.close()


@pytest.mark.parametrize("source_status", ["rejected", "deprecated", "conflicted", "needs_refresh"])
def test_refresh_fails_closed_for_blocked_paper_source(
    tmp_path,
    verified_canonical_artifact,
    source_status: str,
) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope_dict = {"agent_id": "knowledge", "workspace_id": f"blocked-source-{source_status}"}
    scope = ScopeRef.from_dict(scope_dict)
    source_id = f"paper_refresh_{source_status}"
    try:
        _source(
            runtime,
            source_id=source_id,
            scope=scope,
            canonical_text=True,
            status=source_status,
        )
        safe = _claim(
            claim_id=f"claim_{source_status}_safe",
            source_id=source_id,
            text="OpenClaw runtime recall must preserve canonical source provenance for operational decisions.",
            scope=scope,
        )
        runtime.store.append(safe)
        page = compile_paper_knowledge(
            paper_source_id=source_id,
            paper_title="Blocked paper source",
            claim_records=[safe],
        ).to_records(scope=scope)[0]
        page.status = "needs_refresh"
        runtime.store.append(page)

        refresh = runtime.refresh_knowledge_pages(scope=scope_dict)
        blocked_page = runtime.store.get_by_id(page.record_id, scope=scope)

        assert refresh["recompiled_page_count"] == 0
        assert refresh["blocked_page_count"] == 1
        assert refresh["blocked"][0]["reason"] == f"blocked_source_status_{source_status}"
        assert blocked_page is not None
        assert blocked_page.status == "needs_refresh"
        assert blocked_page.meta["refresh_blocked_reason"] == f"blocked_source_status_{source_status}"
    finally:
        runtime.close()
