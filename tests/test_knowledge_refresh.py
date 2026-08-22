from __future__ import annotations

import ast
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import hashlib
from pathlib import Path
from threading import Barrier

import pytest

from eimemory.api.runtime import Runtime
from eimemory.intake.papers import artifacts as paper_artifacts
from eimemory.knowledge.compiler import compile_paper_knowledge
from eimemory.knowledge import refresh as knowledge_refresh
from eimemory.knowledge import projectors as knowledge_projectors
from eimemory.knowledge.projectors import stable_projection_id
from eimemory.models.claim_cards import ClaimCard
from eimemory.models.entity_records import EntityRecord
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


def _refresh_fixture(
    runtime: Runtime,
    *,
    scope: ScopeRef,
    source_id: str,
    canonical_text: bool = True,
    include_entity: bool = False,
    project: bool = True,
) -> tuple[RecordEnvelope, RecordEnvelope, RecordEnvelope | None, RecordEnvelope, str]:
    source = _source(
        runtime,
        source_id=source_id,
        scope=scope,
        canonical_text=canonical_text,
    )
    claim = _claim(
        claim_id=f"claim_{source_id}",
        source_id=source_id,
        text="OpenClaw runtime recall must preserve canonical source provenance for operational decisions.",
        scope=scope,
    )
    runtime.store.append(claim)
    entity = None
    if include_entity:
        entity = runtime.store.append(
            EntityRecord(
                entity_record_id=f"entity_{source_id}",
                paper_source_id=source_id,
                name="OpenClaw Runtime",
                entity_type="system",
            ).to_record(scope=scope)
        )
    compiled = compile_paper_knowledge(
        paper_source_id=source_id,
        paper_title="OpenClaw knowledge refresh evidence",
        claim_records=[claim],
        entity_records=[entity] if entity is not None else None,
    )
    page = compiled.to_records(scope=scope)[0]
    runtime.store.append(page)
    if project:
        runtime.project_operational_knowledge(scope=asdict(scope))
    projection_id = stable_projection_id(page)
    page.status = "needs_refresh"
    runtime.store.rewrite(page)
    return source, claim, entity, page, projection_id


def _projection_from_stale_page(page: RecordEnvelope) -> RecordEnvelope:
    active_page = RecordEnvelope.from_dict(page.to_dict())
    active_page.status = "active"
    candidate, reason = knowledge_projectors._candidate_from_record(active_page)
    assert reason == ""
    assert candidate is not None
    projection = knowledge_projectors._memory_from_candidate(candidate)
    projection.meta["race_marker"] = "latest-projection-version"
    return projection


def _assert_retry_required_without_writes(
    runtime: Runtime,
    *,
    source_id: str,
    page_id: str,
    projection_id: str,
    report: dict,
) -> None:
    assert report["ok"] is False
    assert report["retry_required"] is True
    assert report["refresh_status"] == "retry_required"
    assert report["stale_source_ids"] == [source_id]
    for key in (
        "marked_for_refresh_count",
        "recompiled_page_count",
        "blocked_page_count",
        "retired_projection_count",
        "deprecated_page_count",
    ):
        assert report[key] == 0
    for key in (
        "recompiled_page_ids",
        "blocked_page_ids",
        "retired_projection_ids",
        "blocked",
    ):
        assert report[key] == []
    page = runtime.store.get_by_id(page_id)
    projection = runtime.store.get_by_id(projection_id)
    assert page is not None and page.status == "needs_refresh"
    assert projection is not None and projection.status == "active"


def test_refresh_pagination_exhausts_records_beyond_every_previous_boundary() -> None:
    class PagedRepository:
        def __init__(self, total: int) -> None:
            self.records = list(range(total))
            self.offsets: list[int] = []

        def list_records(self, *, kinds, scope, limit, offset):
            del kinds, scope
            self.offsets.append(offset)
            return self.records[offset : offset + limit]

    repository = PagedRepository(10_001)

    records = knowledge_refresh._list_all_records(
        repository,
        kinds=["knowledge_page", "claim_card", "entity_record", "paper_source"],
        scope=ScopeRef(),
    )

    assert records == repository.records
    assert repository.offsets[0] == 0
    assert repository.offsets[-1] == 10_000


def test_refresh_uses_one_shared_snapshot_loader_for_plan_and_transaction(
    tmp_path,
    verified_canonical_artifact,
    monkeypatch,
) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = ScopeRef.from_dict({"agent_id": "knowledge", "workspace_id": "shared-snapshot"})
    try:
        _refresh_fixture(runtime, scope=scope, source_id="paper_shared_snapshot")
        original_loader = knowledge_refresh._load_refresh_inputs
        repositories: list[object] = []

        def tracked_loader(repository, *, scope):
            repositories.append(repository)
            return original_loader(repository, scope=scope)

        monkeypatch.setattr(knowledge_refresh, "_load_refresh_inputs", tracked_loader)

        report = runtime.refresh_knowledge_pages(scope=asdict(scope))

        assert report["recompiled_page_count"] == 1
        assert repositories == [runtime.store, runtime.store.sqlite]
    finally:
        runtime.close()


@pytest.mark.parametrize("race_kind", ["create", "update", "reactivation"])
def test_refresh_retires_transaction_current_projection_without_losing_updates(
    tmp_path,
    verified_canonical_artifact,
    race_kind: str,
) -> None:
    runtime = Runtime.create(root=tmp_path)
    other = Runtime.create(root=tmp_path)
    scope = ScopeRef.from_dict(
        {"agent_id": "knowledge", "workspace_id": f"projection-race-{race_kind}"}
    )
    try:
        _source_record, _claim_record, _entity, page, projection_id = _refresh_fixture(
            runtime,
            scope=scope,
            source_id=f"paper_projection_race_{race_kind}",
            project=race_kind != "create",
        )
        if race_kind == "reactivation":
            projection = runtime.store.get_by_id(projection_id, scope=scope)
            assert projection is not None
            projection.status = "deprecated"
            projection.touch()
            runtime.store.rewrite(projection)

        original_mutate = runtime.store.mutate_records_atomically

        def race(mutation):
            if race_kind == "create":
                other.store.append(_projection_from_stale_page(page))
            else:
                projection = other.store.get_by_id(projection_id, scope=scope)
                assert projection is not None
                projection.status = "active"
                projection.meta["race_marker"] = "latest-projection-version"
                projection.summary = f"Latest {race_kind} projection must survive retirement metadata."
                projection.touch()
                other.store.rewrite(projection)
            return original_mutate(mutation)

        runtime.store.mutate_records_atomically = race
        report = runtime.refresh_knowledge_pages(scope=asdict(scope))
        final_projection = runtime.store.get_by_id(projection_id, scope=scope)

        assert report["retired_projection_count"] == 1
        assert final_projection is not None
        assert final_projection.status == "deprecated"
        assert final_projection.meta["race_marker"] == "latest-projection-version"
    finally:
        other.close()
        runtime.close()


@pytest.mark.parametrize("changed_input", ["source", "claim", "entity"])
def test_refresh_revalidates_source_claim_and_entity_inputs_before_writes(
    tmp_path,
    verified_canonical_artifact,
    changed_input: str,
) -> None:
    runtime = Runtime.create(root=tmp_path)
    other = Runtime.create(root=tmp_path)
    scope = ScopeRef.from_dict({"agent_id": "knowledge", "workspace_id": f"cas-{changed_input}"})
    source_id = f"paper_refresh_cas_{changed_input}"
    try:
        source, claim, entity, page, projection_id = _refresh_fixture(
            runtime,
            scope=scope,
            source_id=source_id,
            include_entity=changed_input == "entity",
        )
        original_mutate = runtime.store.mutate_records_atomically

        def race(mutation):
            changed = other.store.get_by_id(
                {"source": source, "claim": claim, "entity": entity}[changed_input].record_id,
                scope=scope,
            )
            assert changed is not None
            if changed_input == "source":
                changed.content["metadata"]["artifact"]["text_sha256"] = "race-source-version"
            elif changed_input == "claim":
                changed.summary = "OpenClaw runtime recall must preserve changed source provenance for decisions."
            else:
                changed.content["name"] = "OpenClaw Runtime Changed"
                changed.title = "OpenClaw Runtime Changed"
            changed.touch()
            other.store.rewrite(changed)
            return original_mutate(mutation)

        runtime.store.mutate_records_atomically = race
        report = runtime.refresh_knowledge_pages(scope=asdict(scope))
        _assert_retry_required_without_writes(
            runtime,
            source_id=source_id,
            page_id=page.record_id,
            projection_id=projection_id,
            report=report,
        )

        runtime.store.mutate_records_atomically = original_mutate
        retry = runtime.refresh_knowledge_pages(scope=asdict(scope))
        assert retry["retry_required"] is False
        assert retry["recompiled_page_count"] >= 1
        assert retry["retired_projection_count"] >= 1
    finally:
        other.close()
        runtime.close()


def test_refresh_revalidates_a_plan_that_was_blocked_before_retirement(
    tmp_path,
    verified_canonical_artifact,
) -> None:
    runtime = Runtime.create(root=tmp_path)
    other = Runtime.create(root=tmp_path)
    scope = ScopeRef.from_dict({"agent_id": "knowledge", "workspace_id": "cas-blocked"})
    source_id = "paper_refresh_blocked_cas"
    try:
        source, _claim_record, _entity, page, projection_id = _refresh_fixture(
            runtime,
            scope=scope,
            source_id=source_id,
            canonical_text=False,
        )
        original_mutate = runtime.store.mutate_records_atomically

        def race(mutation):
            changed = other.store.get_by_id(source.record_id, scope=scope)
            assert changed is not None
            changed.content["normalized_text_ref"] = f"artifacts/papers/{source_id}.txt"
            changed.content["pdf_blob_ref"] = f"artifacts/papers/{source_id}.pdf"
            changed.content["metadata"]["artifact"] = {
                "status": "ready",
                "pdf_sha256": "race-pdf",
                "text_sha256": "race-text",
                "manifest_ref": f"artifacts/papers/{source_id}.json",
            }
            changed.touch()
            other.store.rewrite(changed)
            return original_mutate(mutation)

        runtime.store.mutate_records_atomically = race
        report = runtime.refresh_knowledge_pages(scope=asdict(scope))
        _assert_retry_required_without_writes(
            runtime,
            source_id=source_id,
            page_id=page.record_id,
            projection_id=projection_id,
            report=report,
        )

        runtime.store.mutate_records_atomically = original_mutate
        retry = runtime.refresh_knowledge_pages(scope=asdict(scope))
        assert retry["recompiled_page_count"] == 1
    finally:
        other.close()
        runtime.close()


def test_concurrent_refreshes_have_one_winner_and_one_retry(
    tmp_path,
    verified_canonical_artifact,
) -> None:
    setup = Runtime.create(root=tmp_path)
    scope = ScopeRef.from_dict({"agent_id": "knowledge", "workspace_id": "cas-concurrent"})
    source_id = "paper_refresh_concurrent"
    _source_record, _claim_record, _entity, page, _projection_id = _refresh_fixture(
        setup,
        scope=scope,
        source_id=source_id,
    )
    setup.close()
    first = Runtime.create(root=tmp_path)
    second = Runtime.create(root=tmp_path)
    barrier = Barrier(2)
    original_prepare = knowledge_refresh._prepare_plans

    def synchronized_prepare(*args, **kwargs):
        plans = original_prepare(*args, **kwargs)
        barrier.wait(timeout=10)
        return plans

    knowledge_refresh._prepare_plans = synchronized_prepare
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(runtime.refresh_knowledge_pages, scope=asdict(scope))
                for runtime in (first, second)
            ]
            reports = [future.result(timeout=30) for future in futures]
        assert sum(report["recompiled_page_count"] == 1 for report in reports) == 1
        assert sum(report.get("retry_required") is True for report in reports) == 1
        retry = next(report for report in reports if report.get("retry_required") is True)
        assert retry["stale_source_ids"] == [source_id]
        winner = next(report for report in reports if report["recompiled_page_count"] == 1)
        assert winner["retired_projection_count"] == 1
        final_page = first.store.get_by_id(page.record_id, scope=scope)
        assert final_page is not None and final_page.status == "active"
    finally:
        knowledge_refresh._prepare_plans = original_prepare
        first.close()
        second.close()


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
