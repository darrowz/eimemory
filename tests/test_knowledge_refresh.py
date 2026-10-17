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


def _projection_from_stale_page(page: RecordEnvelope, *, store) -> RecordEnvelope:
    active_page = RecordEnvelope.from_dict(page.to_dict())
    active_page.status = "active"
    candidate, reason = knowledge_projectors._candidate_from_record(active_page, store=store)
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
        assert repositories == [runtime.store]
    finally:
        runtime.close()


def test_refresh_write_transaction_does_not_reread_canonical_text(
    tmp_path,
    verified_canonical_artifact,
    monkeypatch,
) -> None:
    runtime = Runtime.create(root=tmp_path)
    scope = ScopeRef.from_dict({"agent_id": "knowledge", "workspace_id": "no-io-in-txn"})
    try:
        _refresh_fixture(runtime, scope=scope, source_id="paper_no_io_in_txn")
        reads = {"count": 0}
        original = paper_artifacts.load_verified_canonical_text

        def counting(*args, **kwargs):
            reads["count"] += 1
            return original(*args, **kwargs)

        monkeypatch.setattr(paper_artifacts, "load_verified_canonical_text", counting, raising=False)
        original_mutate = runtime.store.mutate_records_atomically

        def guarded(mutation):
            before = reads["count"]
            result = original_mutate(mutation)
            assert reads["count"] == before
            return result

        runtime.store.mutate_records_atomically = guarded
        report = runtime.refresh_knowledge_pages(scope=asdict(scope))
        assert report["retry_required"] is False
        assert report["recompiled_page_count"] == 1
        assert reads["count"] >= 1
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
                other.store.append(_projection_from_stale_page(page, store=other.store))
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


@pytest.mark.parametrize("tamper", ["content", "run_id"])
def test_refresh_replacement_rejects_changed_retired_projection(tmp_path, verified_canonical_artifact, tamper):
    runtime = Runtime.create(root=tmp_path)
    scope = ScopeRef(agent_id="knowledge", workspace_id="replacement-fence")
    try:
        _, _, _, _, projection_id = _refresh_fixture(runtime, scope=scope, source_id="paper_replacement")
        refresh = runtime.refresh_knowledge_pages(scope=asdict(scope))
        assert refresh["recompiled_page_count"] == 1
        retired = runtime.store.get_by_id(projection_id, scope=scope)
        assert retired.status == "deprecated"
        if tamper == "content":
            retired.summary = "Changed after retirement"
        else:
            retired.meta["refresh_run_id"] = "another-refresh"
        runtime.store.rewrite(retired)
        before = retired.to_dict()
        projected = runtime.project_operational_knowledge(scope=asdict(scope))
        assert any(item["reason"] == "projection_identity_conflict" for item in projected["skipped"])
        assert runtime.store.get_by_id(projection_id, scope=scope).to_dict() == before
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


def _refresh_authority_scopes(monkeypatch) -> tuple[ScopeRef, ScopeRef, ScopeRef]:
    from eimemory.identity import HONGTU_AGENT_ID, HONGTU_WORKSPACE_ID

    legacy_agent = "refresh-authority-legacy"
    monkeypatch.setenv("EIMEMORY_LEGACY_AGENT_IDS", legacy_agent)
    scope = ScopeRef(agent_id=HONGTU_AGENT_ID, workspace_id=HONGTU_WORKSPACE_ID, user_id="refresh-operator")
    legacy = ScopeRef(agent_id=legacy_agent, workspace_id=scope.workspace_id, user_id=scope.user_id)
    shared = ScopeRef(agent_id=scope.agent_id, workspace_id=scope.workspace_id)
    return scope, legacy, shared


def _refresh_authority_snapshot(runtime: Runtime) -> tuple[tuple, tuple]:
    """Include durable export operations when checking a rejected refresh."""

    with runtime.store._lock:
        records = runtime.store.sqlite.conn.execute(
            "SELECT * FROM records ORDER BY storage_key"
        ).fetchall()
        exports = runtime.store.sqlite.conn.execute(
            "SELECT * FROM export_outbox ORDER BY operation_id"
        ).fetchall()
    return tuple(tuple(row) for row in records), tuple(tuple(row) for row in exports)


def _assert_refresh_authority_blocked(runtime: Runtime, report: dict, before: tuple, *, reason: str) -> None:
    assert report["ok"] is False
    assert report["refresh_status"] == "blocked"
    assert report["retry_required"] is False
    assert report["recompiled_page_count"] == 0
    assert report["retired_projection_count"] == 0
    assert report["deprecated_page_count"] == 0
    assert report["blocked"]
    assert {item["reason"] for item in report["blocked"]} == {reason}
    assert _refresh_authority_snapshot(runtime) == before


@pytest.mark.parametrize("duplicate_kind", ["claim_card", "memory"])
def test_refresh_authority_unrelated_visible_duplicate_does_not_block_independent_plan(
    tmp_path, verified_canonical_artifact, monkeypatch, duplicate_kind: str,
) -> None:
    scope, legacy, _ = _refresh_authority_scopes(monkeypatch)
    runtime = Runtime.create(root=tmp_path)
    try:
        _, _, _, page, _ = _refresh_fixture(runtime, scope=scope, source_id="paper_independent_authority")
        unrelated = []
        for index, duplicate_scope in enumerate((scope, legacy)):
            if duplicate_kind == "claim_card":
                duplicate = _claim(
                    claim_id="unrelated_visible_duplicate", source_id="unrelated_paper",
                    text=f"Unrelated verified claim from authority {index}.", scope=duplicate_scope,
                )
            else:
                duplicate = RecordEnvelope.create(
                    kind="memory", title="Unrelated visible memory", scope=duplicate_scope,
                    summary=f"Unrelated memory variant {index}.", source="refresh.test.unrelated",
                )
                duplicate.record_id = "unrelated_visible_duplicate"
            runtime.store.append(duplicate)
            unrelated.append(duplicate)
        before = [runtime.store.get_by_exact_ref(r.record_id, scope=r.scope, source_id=r.source_id).to_dict()
                  for r in unrelated]

        report = runtime.refresh_knowledge_pages(scope=asdict(scope))

        assert report["retry_required"] is False
        assert report["recompiled_page_count"] == 1
        assert runtime.store.get_by_exact_ref(page.record_id, scope=scope, source_id=page.source_id).status == "active"
        assert [runtime.store.get_by_exact_ref(r.record_id, scope=r.scope, source_id=r.source_id).to_dict()
                for r in unrelated] == before
    finally:
        runtime.close()


def test_refresh_authority_preserves_unique_shared_paper_and_distinct_kind_source_partitions(
    tmp_path, verified_canonical_artifact, monkeypatch,
) -> None:
    scope, _, shared = _refresh_authority_scopes(monkeypatch)
    runtime = Runtime.create(root=tmp_path)
    try:
        source_id = "paper_shared_read_authority"
        source = _source(runtime, source_id=source_id, scope=shared, canonical_text=True)
        claim = _claim(
            claim_id="claim_shared_read_authority", source_id=source_id,
            text="OpenClaw runtime recall must preserve independently reviewed source provenance.", scope=scope,
        )
        claim.source_id = "research"
        runtime.store.append(claim)
        entity = EntityRecord(
            entity_record_id="entity_shared_read_authority", paper_source_id=source_id,
            name="OpenClaw Runtime", entity_type="system",
        ).to_record(scope=scope)
        entity.source_id = "entity-extractor"
        runtime.store.append(entity)
        page = compile_paper_knowledge(
            paper_source_id=source_id, paper_title=source.title, claim_records=[claim], entity_records=[entity],
        ).to_records(scope=scope)[0]
        assert page.source_id == "research"
        page.status = "needs_refresh"
        runtime.store.append(page)
        source_before = runtime.store.get_by_exact_ref(source.record_id, scope=shared, source_id="default").to_dict()
        entity_before = runtime.store.get_by_exact_ref(entity.record_id, scope=scope, source_id=entity.source_id).to_dict()

        report = runtime.refresh_knowledge_pages(scope=asdict(scope))

        assert report["retry_required"] is False
        assert report["recompiled_page_count"] >= 1
        refreshed = runtime.store.get_by_exact_ref(page.record_id, scope=scope, source_id="research")
        assert refreshed is not None and refreshed.status == "active"
        assert refreshed.content["supporting_claim_refs"][0]["source_id"] == "research"
        assert refreshed.content["supporting_claim_refs"][0]["scope"] == asdict(scope)
        assert runtime.store.get_by_exact_ref(source.record_id, scope=shared, source_id="default").to_dict() == source_before
        assert runtime.store.get_by_exact_ref(entity.record_id, scope=scope, source_id=entity.source_id).to_dict() == entity_before
    finally:
        runtime.close()


@pytest.mark.parametrize("foreign_kind", ["claim_card", "entity_record", "knowledge_page"])
@pytest.mark.parametrize("foreign_scope_type", ["legacy_agent", "shared_user"])
def test_refresh_authority_rejects_related_foreign_scope_with_distinct_record_id_without_writes(
    tmp_path, verified_canonical_artifact, monkeypatch, foreign_kind: str, foreign_scope_type: str,
) -> None:
    scope, legacy, shared = _refresh_authority_scopes(monkeypatch)
    foreign_scope = legacy if foreign_scope_type == "legacy_agent" else shared
    runtime = Runtime.create(root=tmp_path)
    try:
        source_id = "paper_foreign_authority"
        _refresh_fixture(runtime, scope=scope, source_id=source_id)
        if foreign_kind == "claim_card":
            foreign = _claim(
                claim_id="foreign_authority_claim", source_id=source_id,
                text="Foreign scope claims must not enter the local compiled knowledge page.", scope=foreign_scope,
            )
        elif foreign_kind == "entity_record":
            foreign = EntityRecord(
                entity_record_id="foreign_authority_entity", paper_source_id=source_id,
                name="Foreign Scope Entity", entity_type="concept",
            ).to_record(scope=foreign_scope)
        else:
            foreign = RecordEnvelope.create(
                kind="knowledge_page", title="Foreign scope page", scope=foreign_scope,
                content={"source_ids": [source_id], "page_type": "topic"},
                source="eimemory.knowledge.compiler",
            )
        runtime.store.append(foreign)
        visible = runtime.store.list_records(kinds=[foreign_kind], scope=scope, limit=100)
        assert any(record.record_id == foreign.record_id and record.scope == foreign_scope for record in visible)
        before = _refresh_authority_snapshot(runtime)

        report = runtime.refresh_knowledge_pages(scope=asdict(scope))

        reason = "entity_scope_authority_mismatch" if foreign_kind == "entity_record" else "ambiguous_source_authority"
        _assert_refresh_authority_blocked(runtime, report, before, reason=reason)
    finally:
        runtime.close()


def test_refresh_authority_rejects_related_claims_from_multiple_runtime_sources_without_writes(
    tmp_path, verified_canonical_artifact, monkeypatch,
) -> None:
    scope, _, _ = _refresh_authority_scopes(monkeypatch)
    runtime = Runtime.create(root=tmp_path)
    try:
        source_id = "paper_claim_partition_authority"
        _refresh_fixture(runtime, scope=scope, source_id=source_id)
        foreign = _claim(
            claim_id="foreign_partition_claim", source_id=source_id,
            text="A distinct runtime source must not be merged into this compiled page.", scope=scope,
        )
        foreign.source_id = "research-other"
        runtime.store.append(foreign)
        before = _refresh_authority_snapshot(runtime)

        report = runtime.refresh_knowledge_pages(scope=asdict(scope))

        _assert_refresh_authority_blocked(runtime, report, before, reason="ambiguous_source_authority")
    finally:
        runtime.close()


def test_refresh_authority_rejects_legacy_only_plan_retargeted_by_canonical_caller_without_writes(
    tmp_path, verified_canonical_artifact, monkeypatch,
) -> None:
    scope, legacy, _ = _refresh_authority_scopes(monkeypatch)
    runtime = Runtime.create(root=tmp_path)
    try:
        _refresh_fixture(runtime, scope=legacy, source_id="paper_legacy_only_authority")
        before = _refresh_authority_snapshot(runtime)

        report = runtime.refresh_knowledge_pages(scope=asdict(scope))

        _assert_refresh_authority_blocked(runtime, report, before, reason="page_scope_authority_mismatch")
    finally:
        runtime.close()


def test_refresh_authority_rejects_compiled_target_occupied_by_other_kind_without_writes(
    tmp_path, verified_canonical_artifact, monkeypatch,
) -> None:
    from eimemory.knowledge.pages import stable_page_id

    scope, _, _ = _refresh_authority_scopes(monkeypatch)
    runtime = Runtime.create(root=tmp_path)
    try:
        source_id = "paper_target_authority"
        source, _, _, _, _ = _refresh_fixture(runtime, scope=scope, source_id=source_id)
        source.title = "Changed canonical source title"
        source.content["title"] = source.title
        runtime.store.rewrite(source)
        occupied = RecordEnvelope.create(
            kind="memory", title="Existing unrelated memory", summary="Existing occupied physical target.",
            scope=scope, source="refresh.test.occupied",
        )
        occupied.record_id = stable_page_id("paper", source_id, source.title)
        runtime.store.append(occupied)
        before = _refresh_authority_snapshot(runtime)

        report = runtime.refresh_knowledge_pages(scope=asdict(scope))

        _assert_refresh_authority_blocked(runtime, report, before, reason="compiled_target_conflict")
    finally:
        runtime.close()


@pytest.mark.parametrize("conflict_field", ["scope", "source_id", "kind"])
def test_refresh_authority_rejects_projection_parent_typed_ref_conflict_without_writes(
    tmp_path, verified_canonical_artifact, monkeypatch, conflict_field: str,
) -> None:
    scope, legacy, _ = _refresh_authority_scopes(monkeypatch)
    runtime = Runtime.create(root=tmp_path)
    try:
        _, _, _, _, projection_id = _refresh_fixture(runtime, scope=scope, source_id="paper_projection_authority")
        projection = runtime.store.get_by_exact_ref(projection_id, scope=scope, source_id="default")
        assert projection is not None
        for container in (projection.meta, projection.provenance):
            parent_ref = dict(container["source_record_ref"])
            parent_ref[conflict_field] = asdict(legacy) if conflict_field == "scope" else (
                "research-other" if conflict_field == "source_id" else "claim_card"
            )
            container["source_record_ref"] = parent_ref
        runtime.store.rewrite(projection)
        before = _refresh_authority_snapshot(runtime)

        report = runtime.refresh_knowledge_pages(scope=asdict(scope))

        _assert_refresh_authority_blocked(runtime, report, before, reason="projection_authority_mismatch")
    finally:
        runtime.close()


def test_refresh_authority_rechecks_projection_parent_fence_after_memory_race(
    tmp_path, verified_canonical_artifact, monkeypatch,
) -> None:
    scope, legacy, _ = _refresh_authority_scopes(monkeypatch)
    runtime = Runtime.create(root=tmp_path)
    try:
        _, _, _, _, projection_id = _refresh_fixture(runtime, scope=scope, source_id="paper_projection_fence_race")
        original_mutate = runtime.store.mutate_records_atomically
        after_race = []

        def interleave(mutation):
            projection = runtime.store.get_by_exact_ref(projection_id, scope=scope, source_id="default")
            assert projection is not None
            for container in (projection.meta, projection.provenance):
                parent_ref = dict(container["source_record_ref"])
                parent_ref["scope"] = asdict(legacy)
                container["source_record_ref"] = parent_ref
            projection.meta["race_marker"] = "changed-parent-authority"
            runtime.store.rewrite(projection)
            after_race.append(_refresh_authority_snapshot(runtime))
            return original_mutate(mutation)

        monkeypatch.setattr(runtime.store, "mutate_records_atomically", interleave)

        report = runtime.refresh_knowledge_pages(scope=asdict(scope))

        assert len(after_race) == 1
        _assert_refresh_authority_blocked(runtime, report, after_race[0], reason="projection_authority_mismatch")
    finally:
        runtime.close()


def test_refresh_authority_shared_owner_same_ids_are_blocked_instead_of_false_snapshot_retry(
    tmp_path, verified_canonical_artifact, monkeypatch,
) -> None:
    scope, _, shared = _refresh_authority_scopes(monkeypatch)
    runtime = Runtime.create(root=tmp_path)
    try:
        source, claim, _, page, _ = _refresh_fixture(
            runtime, scope=scope, source_id="paper_owner_shared_same_ids", project=False,
        )
        for original in (source, claim, page):
            duplicate = RecordEnvelope.from_dict(original.to_dict())
            duplicate.scope = shared
            duplicate.meta["duplicate_authority_marker"] = "shared-version"
            runtime.store.append(duplicate)
        visible = runtime.store.list_records(
            kinds=["paper_source", "claim_card", "knowledge_page"], scope=scope, limit=100,
        )
        assert len(visible) == 6
        assert len({(record.kind, record.record_id) for record in visible}) == 3
        before = _refresh_authority_snapshot(runtime)

        report = runtime.refresh_knowledge_pages(scope=asdict(scope))

        _assert_refresh_authority_blocked(runtime, report, before, reason="ambiguous_page_authority")
    finally:
        runtime.close()


def test_refresh_authority_rejects_target_page_owned_by_another_business_paper_without_writes(
    tmp_path, verified_canonical_artifact, monkeypatch,
) -> None:
    from eimemory.knowledge.pages import stable_page_id

    scope, _, _ = _refresh_authority_scopes(monkeypatch)
    runtime = Runtime.create(root=tmp_path)
    try:
        source_id = "paper_target_business_authority"
        source, _, _, _, _ = _refresh_fixture(runtime, scope=scope, source_id=source_id)
        source.title = "Recompiled target title owned by another paper"
        source.content["title"] = source.title
        runtime.store.rewrite(source)
        occupied = RecordEnvelope.create(
            kind="knowledge_page", title="Another paper's existing page", scope=scope,
            source="eimemory.knowledge.compiler", content={"source_ids": ["another_business_paper"]},
        )
        occupied.record_id = stable_page_id("paper", source_id, source.title)
        runtime.store.append(occupied)
        before = _refresh_authority_snapshot(runtime)

        report = runtime.refresh_knowledge_pages(scope=asdict(scope))

        _assert_refresh_authority_blocked(runtime, report, before, reason="compiled_target_conflict")
    finally:
        runtime.close()


@pytest.mark.parametrize("insert_before_transaction", [False, True])
def test_refresh_authority_fences_target_source_partition_including_late_memory_occupancy(
    tmp_path, verified_canonical_artifact, monkeypatch, insert_before_transaction: bool,
) -> None:
    from eimemory.knowledge.pages import stable_page_id

    scope, _, _ = _refresh_authority_scopes(monkeypatch)
    runtime = Runtime.create(root=tmp_path)
    try:
        source_id = "paper_target_partition_authority"
        source, _, _, _, _ = _refresh_fixture(runtime, scope=scope, source_id=source_id)
        source.title = "Changed source title with occupied target partition"
        source.content["title"] = source.title
        runtime.store.rewrite(source)
        occupied = RecordEnvelope.create(
            kind="memory", title="Other runtime source owns this target", scope=scope,
            source="refresh.test.occupied", source_id="other-runtime-source",
            summary="The compiled target must retain its existing runtime source.",
        )
        occupied.record_id = stable_page_id("paper", source_id, source.title)
        if insert_before_transaction:
            original_mutate = runtime.store.mutate_records_atomically
            after_race = []

            def interleave(mutation):
                runtime.store.append(occupied)
                after_race.append(_refresh_authority_snapshot(runtime))
                return original_mutate(mutation)

            monkeypatch.setattr(runtime.store, "mutate_records_atomically", interleave)
        else:
            runtime.store.append(occupied)
            before = _refresh_authority_snapshot(runtime)

        report = runtime.refresh_knowledge_pages(scope=asdict(scope))

        if insert_before_transaction:
            assert len(after_race) == 1
            before = after_race[0]
        _assert_refresh_authority_blocked(runtime, report, before, reason="compiled_target_conflict")
    finally:
        runtime.close()


def test_refresh_authority_retires_default_legacy_projection_without_versioned_lineage(
    tmp_path, verified_canonical_artifact, monkeypatch,
) -> None:
    scope, _, _ = _refresh_authority_scopes(monkeypatch)
    runtime = Runtime.create(root=tmp_path)
    try:
        _, _, _, page, projection_id = _refresh_fixture(runtime, scope=scope, source_id="paper_legacy_projection_fence")
        projection = runtime.store.get_by_exact_ref(projection_id, scope=scope, source_id="default")
        assert projection is not None
        for container in (projection.meta, projection.provenance):
            for key in ("source_record_ref", "projection_lineage_schema", "supporting_claim_refs"):
                container.pop(key, None)
        runtime.store.rewrite(projection)

        report = runtime.refresh_knowledge_pages(scope=asdict(scope))

        assert report["retry_required"] is False
        assert report["recompiled_page_count"] == 1
        assert report["retired_projection_count"] == 1
        retired = runtime.store.get_by_exact_ref(projection_id, scope=scope, source_id="default")
        refreshed = runtime.store.get_by_exact_ref(page.record_id, scope=scope, source_id="default")
        assert retired is not None and retired.status == "deprecated"
        assert refreshed is not None and refreshed.status == "active"
    finally:
        runtime.close()


def test_refresh_authority_preserves_foreign_projection_with_same_bare_parent_and_projection_ids(
    tmp_path, verified_canonical_artifact, monkeypatch,
) -> None:
    scope, legacy, _ = _refresh_authority_scopes(monkeypatch)
    runtime = Runtime.create(root=tmp_path)
    try:
        _, _, _, _, projection_id = _refresh_fixture(runtime, scope=scope, source_id="paper_foreign_projection_preserved")
        local = runtime.store.get_by_exact_ref(projection_id, scope=scope, source_id="default")
        assert local is not None
        foreign = RecordEnvelope.from_dict(local.to_dict())
        foreign.scope = legacy
        for container in (foreign.meta, foreign.provenance):
            parent_ref = dict(container["source_record_ref"])
            parent_ref["scope"] = asdict(legacy)
            container["source_record_ref"] = parent_ref
        runtime.store.append(foreign)
        before = runtime.store.get_by_exact_ref(foreign.record_id, scope=legacy, source_id="default").to_dict()

        report = runtime.refresh_knowledge_pages(scope=asdict(scope))

        assert report["retry_required"] is False
        assert report["recompiled_page_count"] == 1
        assert report["retired_projection_count"] == 1
        assert runtime.store.get_by_exact_ref(projection_id, scope=scope, source_id="default").status == "deprecated"
        assert runtime.store.get_by_exact_ref(foreign.record_id, scope=legacy, source_id="default").to_dict() == before
    finally:
        runtime.close()


@pytest.mark.parametrize("foreign_dimension", ["scope", "source_id"])
def test_refresh_authority_rejects_single_claim_authority_different_from_page_without_writes(
    tmp_path, verified_canonical_artifact, monkeypatch, foreign_dimension: str,
) -> None:
    scope, legacy, _ = _refresh_authority_scopes(monkeypatch)
    runtime = Runtime.create(root=tmp_path)
    try:
        source_id = "paper_single_claim_authority"
        source = _source(runtime, source_id=source_id, scope=scope, canonical_text=True)
        claim = _claim(
            claim_id="single_foreign_authority_claim", source_id=source_id,
            text="A lone foreign claim authority must not become local compiled evidence.",
            scope=legacy if foreign_dimension == "scope" else scope,
        )
        if foreign_dimension == "source_id":
            claim.source_id = "research-other"
        runtime.store.append(claim)
        page = compile_paper_knowledge(
            paper_source_id=source_id, paper_title=source.title, claim_records=[claim],
        ).to_records(scope=scope)[0]
        # Historical default-origin pages need not have versioned support refs.
        page.source_id = "default"
        page.content.pop("supporting_claim_refs", None)
        page.content.pop("supporting_claim_refs_schema", None)
        page.status = "needs_refresh"
        runtime.store.append(page)
        before = _refresh_authority_snapshot(runtime)

        report = runtime.refresh_knowledge_pages(scope=asdict(scope))

        _assert_refresh_authority_blocked(runtime, report, before, reason="claim_authority_mismatch")
    finally:
        runtime.close()


def test_refresh_authority_checks_actual_storage_key_when_target_sql_owner_columns_drift(
    tmp_path, verified_canonical_artifact, monkeypatch,
) -> None:
    from eimemory.knowledge.pages import stable_page_id

    scope, _, _ = _refresh_authority_scopes(monkeypatch)
    runtime = Runtime.create(root=tmp_path)
    try:
        source_id = "paper_target_physical_authority"
        source, _, _, _, _ = _refresh_fixture(runtime, scope=scope, source_id=source_id)
        source.title = "A newly compiled target"
        source.content["title"] = source.title
        runtime.store.rewrite(source)
        occupied = RecordEnvelope.create(kind="memory", title="Preserve the actual occupied slot",
                                         scope=scope, source="refresh.test.occupied")
        occupied.record_id = stable_page_id("paper", source_id, source.title)
        runtime.store.append(occupied)
        with runtime.store._lock:
            runtime.store.sqlite.conn.execute("UPDATE records SET user_id=? WHERE record_id=?",
                                               ("different-sql-owner", occupied.record_id))
            runtime.store.sqlite.conn.commit()
        before = _refresh_authority_snapshot(runtime)

        report = runtime.refresh_knowledge_pages(scope=asdict(scope))

        _assert_refresh_authority_blocked(runtime, report, before, reason="compiled_target_conflict")
    finally:
        runtime.close()


@pytest.mark.parametrize("malformed", ["missing_parent_kind", "missing_meta_type", "wrong_producer",
                                      "content_parent_ref", "partial_support_refs"])
def test_refresh_authority_rejects_incomplete_or_conflicting_projection_ownership(
    tmp_path, verified_canonical_artifact, monkeypatch, malformed: str,
) -> None:
    scope, legacy, _ = _refresh_authority_scopes(monkeypatch)
    runtime = Runtime.create(root=tmp_path)
    try:
        _, _, _, _, projection_id = _refresh_fixture(runtime, scope=scope, source_id="paper_partial_projection")
        projection = runtime.store.get_by_exact_ref(projection_id, scope=scope, source_id="default")
        assert projection is not None
        if malformed == "missing_parent_kind":
            for container in (projection.meta, projection.provenance, projection.content):
                container.pop("source_record_kind", None)
        elif malformed == "missing_meta_type":
            projection.meta.pop("projection_type")
        elif malformed == "wrong_producer":
            projection.source = "refresh.test.unrelated_producer"
        elif malformed == "content_parent_ref":
            projection.content["source_record_ref"] = {
                **projection.meta["source_record_ref"], "scope": asdict(legacy),
            }
        else:
            projection.provenance.pop("supporting_claim_refs")
        runtime.store.rewrite(projection)
        before = _refresh_authority_snapshot(runtime)

        report = runtime.refresh_knowledge_pages(scope=asdict(scope))

        _assert_refresh_authority_blocked(runtime, report, before, reason="projection_authority_mismatch")
    finally:
        runtime.close()


@pytest.mark.parametrize("drift", ["projection_storage_key", "projection_sql_owner", "projection_meta_type",
                                  "old_page_storage_key", "page_status_projection"])
def test_refresh_authority_binds_all_existing_write_targets_to_verified_physical_rows(
    tmp_path, verified_canonical_artifact, monkeypatch, drift: str,
) -> None:
    import json

    scope, _, _ = _refresh_authority_scopes(monkeypatch)
    runtime = Runtime.create(root=tmp_path)
    try:
        source, _, _, page, projection_id = _refresh_fixture(runtime, scope=scope, source_id="paper_physical_bindings")
        if drift == "old_page_storage_key":
            source.title = "Changed title produces a separate compiled slot"
            source.content["title"] = source.title
            runtime.store.rewrite(source)
        if drift == "projection_meta_type":
            projection = runtime.store.get_by_exact_ref(projection_id, scope=scope, source_id="default")
            projection.meta["flag"] = True
            runtime.store.rewrite(projection)
        with runtime.store._lock:
            db = runtime.store.sqlite.conn
            if drift.endswith("storage_key"):
                record_id = page.record_id if drift == "old_page_storage_key" else projection_id
                db.execute("UPDATE records SET storage_key=storage_key||'-drift' WHERE record_id=?", (record_id,))
            elif drift == "projection_sql_owner":
                db.execute("UPDATE records SET user_id='' WHERE record_id=?", (projection_id,))
            elif drift == "page_status_projection":
                db.execute("UPDATE records SET status='active' WHERE record_id=?", (page.record_id,))
            else:
                projected_meta = dict(projection.meta, flag=1)
                db.execute("UPDATE records SET meta_json=? WHERE record_id=?", (json.dumps(projected_meta), projection_id))
            db.commit()
        before = _refresh_authority_snapshot(runtime)

        report = runtime.refresh_knowledge_pages(scope=asdict(scope))

        _assert_refresh_authority_blocked(runtime, report, before, reason="refresh_record_snapshot_mismatch")
    finally:
        runtime.close()


@pytest.mark.parametrize("abort_reason", ["target_conflict", "source_race"])
def test_refresh_authority_aborts_owner_without_flushing_existing_pending_exports(
    tmp_path, verified_canonical_artifact, monkeypatch, abort_reason: str,
) -> None:
    from eimemory.knowledge.pages import stable_page_id

    scope, _, _ = _refresh_authority_scopes(monkeypatch)
    runtime = Runtime.create(root=tmp_path)
    try:
        source_id = "paper_pending_export_abort"
        source, _, _, _, _ = _refresh_fixture(runtime, scope=scope, source_id=source_id)
        if abort_reason == "target_conflict":
            source.title = "Occupied future slot"
            source.content["title"] = source.title
            runtime.store.rewrite(source)
            occupied = RecordEnvelope.create(kind="memory", title="Preserve occupied target", scope=scope)
            occupied.record_id = stable_page_id("paper", source_id, source.title)
            runtime.store.append(occupied)
        pending = RecordEnvelope.create(kind="reflection", title="Pending unrelated export", scope=scope,
                                        source="refresh.test.pending")
        with runtime.store._lock:
            runtime.store.sqlite.enqueue_export(stream="records", payload=pending.to_dict(), operation_id="preserve-pending")
        before = _refresh_authority_snapshot(runtime)
        if abort_reason == "source_race":
            original_mutate = runtime.store.mutate_records_atomically
            after_race = []

            def interleave(mutation):
                source.summary = "Concurrent source change"
                with runtime.store._lock:
                    runtime.store.sqlite.upsert(source)
                after_race.append(_refresh_authority_snapshot(runtime))
                return original_mutate(mutation)

            monkeypatch.setattr(runtime.store, "mutate_records_atomically", interleave)

        report = runtime.refresh_knowledge_pages(scope=asdict(scope))

        if abort_reason == "target_conflict":
            _assert_refresh_authority_blocked(runtime, report, before, reason="compiled_target_conflict")
        else:
            assert report["retry_required"] is True
            assert _refresh_authority_snapshot(runtime) == after_race[0]
        with runtime.store._lock:
            assert runtime.store.sqlite.conn.execute(
                "SELECT state FROM export_outbox WHERE operation_id='preserve-pending'"
            ).fetchone()[0] == "pending"
    finally:
        runtime.close()


@pytest.mark.parametrize("read_input", ["claim_owner", "claim_source", "entity_storage_key", "paper_source"])
def test_refresh_authority_verifies_read_only_input_exact_refs_before_compiling_writes(
    tmp_path, verified_canonical_artifact, monkeypatch, read_input: str,
) -> None:
    scope, _, _ = _refresh_authority_scopes(monkeypatch)
    runtime = Runtime.create(root=tmp_path)
    try:
        source, claim, entity, _, _ = _refresh_fixture(
            runtime, scope=scope, source_id="paper_read_only_authority", include_entity=True,
        )
        with runtime.store._lock:
            db = runtime.store.sqlite.conn
            if read_input == "claim_owner":
                db.execute("UPDATE records SET user_id='' WHERE record_id=?", (claim.record_id,))
            elif read_input == "claim_source":
                db.execute("UPDATE records SET source_id='other-source' WHERE record_id=?", (claim.record_id,))
            elif read_input == "entity_storage_key":
                db.execute("UPDATE records SET storage_key=storage_key||'-drift' WHERE record_id=?", (entity.record_id,))
            else:
                db.execute("UPDATE records SET source_id='other-source' WHERE record_id=?", (source.record_id,))
            db.commit()
        before = _refresh_authority_snapshot(runtime)

        report = runtime.refresh_knowledge_pages(scope=asdict(scope))

        _assert_refresh_authority_blocked(runtime, report, before, reason="refresh_record_snapshot_mismatch")
        assert report["persisted"] is False and report["mutation_count"] == 0
    finally:
        runtime.close()
