from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

import pytest

from eimemory.api.runtime import Runtime
from eimemory.identity import HONGTU_AGENT_ID, HONGTU_WORKSPACE_ID
from eimemory.identity_ops import repair_hongtu_identity
from eimemory.knowledge.capabilities import (
    KNOWLEDGE_CAPABILITY_MARKER_KEY,
    KnowledgeCapabilityBridgeError,
    refresh_capability_applicability_marker,
)
from eimemory.knowledge.compiler import compile_paper_knowledge
from eimemory.knowledge.evidence_contracts import versioned_record_ref
from eimemory.knowledge.pages import stable_page_id
from eimemory.knowledge.projectors import _candidate_from_record, _validated_supporting_claims, stable_projection_id
from eimemory.knowledge.refresh import _refresh_reason
from eimemory.models.claim_cards import ClaimCard
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.storage.jsonl import canonical_payload_json


_TEXT = "OpenClaw runtime recall must preserve canonical source provenance for operational decisions."


def _minimal_pdf(text: str) -> bytes:
    """A real local PDF lets the production verifier check its full manifest."""
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT\n/F1 12 Tf\n72 720 Td\n({escaped}) Tj\nET\n".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"endstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    payload = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(payload))
        payload.extend(f"{number} 0 obj\n".encode("ascii") + obj + b"\nendobj\n")
    xref_offset = len(payload)
    payload.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii") + b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        payload.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    payload.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("ascii"))
    return bytes(payload)


@pytest.fixture
def lineage_runtime(tmp_path: Path, monkeypatch):
    pytest.importorskip("pypdf")
    monkeypatch.setenv("EIMEMORY_LEGACY_AGENT_IDS", "lineage-test-legacy")
    runtime = Runtime.create(root=tmp_path / "runtime")
    scope = ScopeRef(agent_id=HONGTU_AGENT_ID, workspace_id=HONGTU_WORKSPACE_ID, user_id="lineage-operator")
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(_minimal_pdf(_TEXT))
    source = runtime.ingest_paper_source({"pdf_file": str(pdf), "title": "OpenClaw provenance"}, scope=asdict(scope))
    claim = ClaimCard(
        claim_card_id="claim_lineage_probe", paper_source_id=source.record_id,
        paper_extract_id="extract_lineage_probe", claim_text=_TEXT, confidence=0.95,
    ).to_record(scope=scope)
    runtime.store.append(claim)
    claim = runtime.store.get_by_exact_ref(claim.record_id, scope=scope, source_id="default")
    page = compile_paper_knowledge(
        paper_source_id=source.record_id, paper_title=source.title, claim_records=[claim],
    ).to_records(scope=scope)[0]
    runtime.store.append(page)
    page = runtime.store.get_by_exact_ref(page.record_id, scope=scope, source_id="default")
    try:
        yield runtime, scope, source, claim, page
    finally:
        runtime.close()


def _snapshot(runtime: Runtime) -> tuple:
    with runtime.store._lock:
        db = runtime.store.sqlite
        records = tuple(tuple(row) for row in db.execute("SELECT * FROM records ORDER BY storage_key").fetchall())
        exports = tuple(tuple(row) for row in db.execute("SELECT * FROM export_outbox ORDER BY operation_id").fetchall())
    return records, exports


def _changed_claim(runtime: Runtime, claim: RecordEnvelope) -> None:
    claim.meta["source_revision"] = "reviewed-upstream-revision-2"
    claim.touch()
    runtime.store.rewrite(claim)


def _exact(runtime: Runtime, record: RecordEnvelope) -> RecordEnvelope:
    value = runtime.store.get_by_exact_ref(record.record_id, scope=record.scope, source_id=record.source_id)
    assert value is not None
    return value


def test_support_change_rebuilds_exact_refs_and_operational_projection(lineage_runtime) -> None:
    runtime, scope, _source, claim, page = lineage_runtime
    runtime.project_operational_knowledge(scope=asdict(scope))
    old_projection = runtime.store.get_by_exact_ref(stable_projection_id(page), scope=scope, source_id=page.source_id)
    assert old_projection is not None and old_projection.status == "active"
    before = page.to_dict()
    refs_bytes = canonical_payload_json(page.content["supporting_claim_refs"])
    _changed_claim(runtime, claim)
    assert _validated_supporting_claims(page, store=runtime.store)[1] == "support_changed"

    reconciled = runtime.evolution.reconcile_knowledge(scope=asdict(scope))

    assert reconciled["support_lineage_marked_count"] == reconciled["page_refresh_count"] == 1
    assert reconciled["new_contradiction_count"] == 0
    marked = _exact(runtime, page)
    assert marked.status == "needs_refresh"
    assert marked.meta["refresh_reason"] == "support_lineage_changed"
    assert canonical_payload_json(marked.content["supporting_claim_refs"]) == refs_bytes
    after = marked.to_dict()
    for field in set(before) - {"status", "meta", "time"}:
        assert after[field] == before[field], field
    assert after["meta"] == {**before["meta"], "refresh_reason": "support_lineage_changed"}
    assert after["time"]["created_at"] == before["time"]["created_at"]
    assert after["time"]["occurred_at"] == before["time"]["occurred_at"]

    refreshed = runtime.refresh_knowledge_pages(scope=asdict(scope))

    assert refreshed["ok"] and refreshed["recompiled_page_count"] == 1
    assert refreshed["retired_projection_count"] == 1
    rebuilt = _exact(runtime, page)
    assert rebuilt.status == "active"
    assert rebuilt.content["refresh"]["reason"] == "support_lineage_changed"
    assert rebuilt.meta["refresh_reason"] == "support_lineage_changed"
    assert _validated_supporting_claims(rebuilt, store=runtime.store)[1] == ""
    assert rebuilt.content["supporting_claim_refs"] == [versioned_record_ref(_exact(runtime, claim))]
    marker = rebuilt.content[KNOWLEDGE_CAPABILITY_MARKER_KEY]
    assert marker["applicability"] == "candidate"
    assert marker["contradiction_state"] == "none"
    assert marker["resolved_contradiction_ids"] == []
    assert "contradiction_ids" not in rebuilt.content
    retired = _exact(runtime, old_projection)
    assert retired.status == "deprecated"
    runtime.project_operational_knowledge(scope=asdict(scope))
    projection = _exact(runtime, old_projection)
    assert projection.status == "active"
    assert projection.meta["source_record_ref"] == versioned_record_ref(rebuilt)
    assert projection.meta["supporting_claim_refs"] == rebuilt.content["supporting_claim_refs"]
    again = runtime.evolution.reconcile_knowledge(scope=asdict(scope))
    assert again["support_lineage_marked_count"] == 0


@pytest.mark.parametrize("bad", [
    "missing_schema", "empty_refs", "malformed_after_changed", "ids_mismatch",
    "missing_after_changed", "cross_scope_after_changed", "cross_source_after_changed",
    "unsafe_claim", "low_confidence_claim", "replaced_kind",
])
def test_incomplete_or_unsafe_support_never_auto_recompiles(lineage_runtime, bad: str) -> None:
    runtime, scope, _source, claim, page = lineage_runtime
    _changed_claim(runtime, claim)
    if bad == "missing_schema":
        page.content.pop("supporting_claim_refs_schema")
    elif bad == "empty_refs":
        page.content["supporting_claim_refs"] = []
    elif bad == "malformed_after_changed":
        page.content["supporting_claim_refs"].append({"record_id": "malformed"})
    elif bad == "ids_mismatch":
        page.content["supporting_claim_ids"].append("claim_unknown")
    elif bad.endswith("after_changed"):
        ref = deepcopy(page.content["supporting_claim_refs"][0])
        ref["record_id"] = "claim_other"
        if bad == "cross_scope_after_changed":
            ref["scope"]["user_id"] = "other-user"
        elif bad == "cross_source_after_changed":
            ref["source_id"] = "another-source"
        page.content["supporting_claim_refs"].append(ref)
        page.content["supporting_claim_ids"].append(ref["record_id"])
    elif bad == "unsafe_claim":
        claim.status = "rejected"
        runtime.store.rewrite(claim)
    elif bad == "low_confidence_claim":
        claim.content["confidence"] = 0.1
        claim.meta["confidence"] = 0.1
        runtime.store.rewrite(claim)
    elif bad == "replaced_kind":
        claim.kind = "memory"
        runtime.store.rewrite(claim)
    runtime.store.rewrite(page)
    before = _snapshot(runtime)

    report = runtime.evolution.reconcile_knowledge(scope=asdict(scope))

    assert report["support_lineage_marked_count"] == report["page_refresh_count"] == 0
    assert report["support_lineage_blocked"]
    assert _snapshot(runtime) == before
    assert _exact(runtime, page).status == "active"


def test_unchanged_support_does_not_create_refresh_work(lineage_runtime) -> None:
    runtime, scope, _source, _claim, _page = lineage_runtime
    before = _snapshot(runtime)
    report = runtime.evolution.reconcile_knowledge(scope=asdict(scope))
    assert report["support_lineage_marked_count"] == 0
    assert _snapshot(runtime) == before


@pytest.mark.parametrize("foreign", ["shared_user", "legacy_agent"])
def test_visible_foreign_page_is_never_a_write_target(lineage_runtime, foreign: str) -> None:
    runtime, scope, _source, _claim, page = lineage_runtime
    copy = RecordEnvelope.from_dict(page.to_dict())
    foreign_scope = {**asdict(scope), **({"user_id": ""} if foreign == "shared_user" else {"agent_id": "lineage-test-legacy"})}
    copy.scope = ScopeRef.from_dict(foreign_scope)
    runtime.store.append(copy)
    before = _snapshot(runtime)
    report = runtime.evolution.reconcile_knowledge(scope=asdict(scope))
    assert report["support_lineage_marked_count"] == 0
    assert _snapshot(runtime) == before
    assert _exact(runtime, copy).status == "active"


@pytest.mark.parametrize("raced", ["page", "claim"])
def test_marking_uses_full_page_and_support_version_cas(lineage_runtime, monkeypatch, raced: str) -> None:
    runtime, scope, _source, claim, page = lineage_runtime
    _changed_claim(runtime, claim)
    owner = runtime.store.mutate_records_atomically
    observed = {}
    other = Runtime.create(root=runtime.store.root)
    def concurrent_change(callback):
        current = _exact(other, page if raced == "page" else claim)
        current.meta["concurrent_writer"] = "preserve this revision"
        current.touch()
        other.store.rewrite(current)
        observed["snapshot"] = _snapshot(runtime)
        return owner(callback)
    monkeypatch.setattr(runtime.store, "mutate_records_atomically", concurrent_change)

    try:
        report = runtime.evolution.reconcile_knowledge(scope=asdict(scope))
    finally:
        other.close()

    assert report["support_lineage_marked_count"] == 0
    assert report["support_lineage_retry_required_count"] == 1
    assert _snapshot(runtime) == observed["snapshot"]
    assert _exact(runtime, page).status == "active"


def test_rebuild_preserves_an_independent_occupied_physical_target(lineage_runtime) -> None:
    runtime, scope, source, claim, page = lineage_runtime
    _changed_claim(runtime, claim)
    assert runtime.evolution.reconcile_knowledge(scope=asdict(scope))["support_lineage_marked_count"] == 1
    source.title = "New canonical paper title"
    source.content["title"] = source.title
    source.touch()
    runtime.store.rewrite(source)
    occupied = RecordEnvelope.create(kind="memory", title="Independent record", scope=scope, source="lineage.test.unrelated")
    occupied.record_id = stable_page_id("paper", source.record_id, source.title)
    runtime.store.append(occupied)
    before = _snapshot(runtime)

    report = runtime.refresh_knowledge_pages(scope=asdict(scope))

    assert not report["ok"]
    assert report["mutation_count"] == 0
    assert {item["reason"] for item in report["blocked"]} == {"compiled_target_conflict"}
    assert _snapshot(runtime) == before


def test_changed_lineage_does_not_bypass_canonical_artifact_gate(lineage_runtime) -> None:
    runtime, scope, source, claim, page = lineage_runtime
    _changed_claim(runtime, claim)
    report = runtime.evolution.reconcile_knowledge(scope=asdict(scope))
    assert report["support_lineage_marked_count"] == 1
    source.content["normalized_text_ref"] = ""
    source.touch()
    runtime.store.rewrite(source)

    refreshed = runtime.refresh_knowledge_pages(scope=asdict(scope))

    assert refreshed["recompiled_page_count"] == 0
    assert refreshed["blocked_page_count"] == 1
    assert {item["reason"] for item in refreshed["blocked"]} == {"blocked_missing_canonical_text"}
    assert _exact(runtime, page).status == "needs_refresh"


@pytest.mark.parametrize("corrupt", ["page_projection", "claim_projection", "page_storage_key", "claim_storage_key"])
def test_marking_requires_verified_physical_snapshots(lineage_runtime, corrupt: str) -> None:
    runtime, scope, _source, claim, page = lineage_runtime
    _changed_claim(runtime, claim)
    target = page if corrupt.startswith("page") else claim
    pending = RecordEnvelope.create(kind="reflection", title="Unrelated pending export", scope=scope)
    with runtime.store._lock:
        db = runtime.store.sqlite
        if corrupt.endswith("storage_key"):
            db.execute("UPDATE records SET storage_key=storage_key||'-drift' WHERE record_id=?", (target.record_id,))
        else:
            db.execute("UPDATE records SET title='wrong SQL projection' WHERE record_id=?", (target.record_id,))
        db.commit()
        db.enqueue_export(stream="records", payload=pending.to_dict(), operation_id="preserve-lineage-pending")
    before = _snapshot(runtime)

    report = runtime.evolution.reconcile_knowledge(scope=asdict(scope))

    assert report["support_lineage_marked_count"] == 0
    if corrupt.endswith("projection"):
        assert {item["reason"] for item in report["support_lineage_blocked"]} == {"support_snapshot_mismatch"}
    else:
        assert report["support_lineage_blocked"] or report["support_lineage_retry_required_count"]
    assert _snapshot(runtime) == before
    with runtime.store._lock:
        assert runtime.store.sqlite.execute(
            "SELECT state FROM export_outbox WHERE operation_id='preserve-lineage-pending'",
        ).fetchone()[0] == "pending"


def test_real_identity_migration_leaves_cross_scope_old_refs_blocked(lineage_runtime) -> None:
    runtime, scope, source, claim, page = lineage_runtime
    legacy = ScopeRef.from_dict({**asdict(scope), "agent_id": "lineage-test-legacy"})
    for record in (source, claim):
        moved = RecordEnvelope.from_dict(record.to_dict())
        moved.scope = legacy
        runtime.store.rewrite(moved, previous_scope=scope)
    legacy_claim = runtime.store.get_by_exact_ref(claim.record_id, scope=legacy, source_id=claim.source_id)
    legacy_page = compile_paper_knowledge(
        paper_source_id=source.record_id, paper_title=source.title, claim_records=[legacy_claim],
    ).to_records(scope=legacy)[0]
    runtime.store.rewrite(legacy_page, previous_scope=scope)

    migrated = repair_hongtu_identity(runtime, apply=True, scope=scope)

    assert migrated["ok"] and migrated["repaired_count"] == 3
    migrated_page = _exact(runtime, page)
    assert _validated_supporting_claims(migrated_page, store=runtime.store)[1] == "support_scope_source_mismatch"
    old_refs = canonical_payload_json(migrated_page.content["supporting_claim_refs"])
    before = _snapshot(runtime)
    report = runtime.evolution.reconcile_knowledge(scope=asdict(scope))
    assert report["support_lineage_marked_count"] == 0
    assert {item["reason"] for item in report["support_lineage_blocked"]} == {"support_scope_source_mismatch"}
    assert _snapshot(runtime) == before
    assert canonical_payload_json(_exact(runtime, page).content["supporting_claim_refs"]) == old_refs


def test_lineage_refresh_preserves_existing_resolved_contradiction_audit(lineage_runtime) -> None:
    runtime, scope, _source, claim, page = lineage_runtime
    audit_ids = ["relation_historical_contradiction"]
    for container in (page.content, page.meta, page.provenance):
        container["resolved_contradiction_ids"] = list(audit_ids)
    runtime.store.rewrite(page)
    _changed_claim(runtime, claim)

    report = runtime.evolution.reconcile_knowledge(scope=asdict(scope))
    assert report["support_lineage_marked_count"] == 1
    refreshed = runtime.refresh_knowledge_pages(scope=asdict(scope))

    assert refreshed["ok"] and refreshed["recompiled_page_count"] == 1
    rebuilt = _exact(runtime, page)
    marker = rebuilt.content[KNOWLEDGE_CAPABILITY_MARKER_KEY]
    assert rebuilt.content["refresh"]["reason"] == "support_lineage_changed"
    assert marker["contradiction_state"] == "resolved"
    assert marker["resolved_contradiction_ids"] == audit_ids
    assert rebuilt.provenance["refresh_audit"]["resolved_contradiction_ids"] == audit_ids
    assert marker["applicability"] == "candidate"
    assert "contradiction_ids" not in rebuilt.content


def _add_topic_page(runtime: Runtime, scope: ScopeRef, source: RecordEnvelope, claim: RecordEnvelope) -> RecordEnvelope:
    pages = compile_paper_knowledge(
        paper_source_id=source.record_id, paper_title=source.title,
        claim_records=[claim], entities=["OpenClaw Runtime"],
    ).to_records(scope=scope)
    topic = next(page for page in pages if page.content["page_type"] == "topic")
    runtime.store.append(topic)
    return _exact(runtime, topic)


@pytest.mark.parametrize("other_reason", [None, "legacy_refresh_reason", {"malformed": "reason"}])
def test_mixed_same_paper_stale_reasons_preserve_existing_contradiction_semantics(
    lineage_runtime, monkeypatch, other_reason,
) -> None:
    runtime, scope, source, claim, page = lineage_runtime
    topic = _add_topic_page(runtime, scope, source, claim)
    _changed_claim(runtime, claim)
    reconciled = runtime.evolution.reconcile_knowledge(scope=asdict(scope))
    assert reconciled["support_lineage_marked_count"] == 2
    topic = _exact(runtime, topic)
    if other_reason is None:
        topic.meta.pop("refresh_reason")
    else:
        topic.meta["refresh_reason"] = other_reason
    runtime.store.rewrite(topic)
    assert _refresh_reason([_exact(runtime, page), topic], source_records=[claim]) == "claim_contradiction"
    before = _snapshot(runtime)
    def no_owner(_callback):
        pytest.fail("mixed stale reasons must be blocked before opening the mutation owner")
    monkeypatch.setattr(runtime.store, "mutate_records_atomically", no_owner)

    refreshed = runtime.refresh_knowledge_pages(scope=asdict(scope))

    assert not refreshed["ok"]
    assert refreshed["persisted"] is False and refreshed["mutation_count"] == 0
    assert {item["reason"] for item in refreshed["blocked"]} == {"support_lineage_peer_unsafe_page"}
    assert _snapshot(runtime) == before


@pytest.mark.parametrize("conflict", [
    "status", "content_contradiction_ids", "meta_contradiction_claim_ids", "provenance_contradiction_ids",
])
def test_current_source_claim_conflict_prevents_no_contradiction_refresh_semantics(
    lineage_runtime, conflict: str,
) -> None:
    runtime, scope, source, claim, page = lineage_runtime
    other = ClaimCard(
        claim_card_id="claim_other_source_constraint", paper_source_id=source.record_id,
        paper_extract_id="extract_lineage_probe", claim_text="The runtime deployment graph uses a separate source constraint.",
        confidence=0.95,
    ).to_record(scope=scope)
    if conflict == "status":
        other.status = "conflicted"
    else:
        container, key = conflict.split("_", 1)
        getattr(other, container)[key] = ["relation_existing_contradiction"]
    runtime.store.append(other)
    _changed_claim(runtime, claim)
    reconciled = runtime.evolution.reconcile_knowledge(scope=asdict(scope))
    assert reconciled["support_lineage_marked_count"] == 1
    marked = _exact(runtime, page)
    assert marked.meta["refresh_reason"] == "support_lineage_changed"

    refreshed = runtime.refresh_knowledge_pages(scope=asdict(scope))

    assert refreshed["ok"] and refreshed["recompiled_page_count"] == 1
    rebuilt = _exact(runtime, page)
    assert rebuilt.content["refresh"]["reason"] == "claim_contradiction"
    assert rebuilt.meta["refresh_reason"] == "claim_contradiction"
    assert rebuilt.content[KNOWLEDGE_CAPABILITY_MARKER_KEY]["contradiction_state"] == "resolved"


def test_second_page_race_rolls_back_entire_marking_batch(lineage_runtime, monkeypatch) -> None:
    runtime, scope, source, claim, page = lineage_runtime
    topic = _add_topic_page(runtime, scope, source, claim)
    _changed_claim(runtime, claim)
    ordered = runtime.store.list_records(kinds=["knowledge_page"], scope=scope, limit=100)
    assert len(ordered) == 2
    raced_page = ordered[1]
    other = Runtime.create(root=runtime.store.root)
    pending = RecordEnvelope.create(kind="reflection", title="Unrelated pending export", scope=scope)
    with runtime.store._lock:
        runtime.store.sqlite.enqueue_export(
            stream="records", payload=pending.to_dict(), operation_id="preserve-lineage-batch-pending",
        )
    owner = runtime.store.mutate_records_atomically
    observed = {}
    def second_page_writer(callback):
        current = _exact(other, raced_page)
        current.meta["concurrent_writer"] = "preserve the second page revision"
        current.touch()
        # A real second SQLite connection commits before the owner starts.
        # Keeping exports pending isolates the aborting owner's flush behavior.
        with other.store._lock:
            other.store.sqlite.upsert(current)
        observed["snapshot"] = _snapshot(runtime)
        return owner(callback)
    monkeypatch.setattr(runtime.store, "mutate_records_atomically", second_page_writer)

    try:
        report = runtime.evolution.reconcile_knowledge(scope=asdict(scope))
    finally:
        other.close()

    assert report["support_lineage_marked_count"] == report["page_refresh_count"] == 0
    assert report["support_lineage_retry_required_count"] == 2
    assert _exact(runtime, page).status == _exact(runtime, topic).status == "active"
    assert _snapshot(runtime) == observed["snapshot"]
    with runtime.store._lock:
        assert runtime.store.sqlite.execute(
            "SELECT state FROM export_outbox WHERE operation_id='preserve-lineage-batch-pending'",
        ).fetchone()[0] == "pending"


@pytest.mark.parametrize("container,key,value", [
    ("meta", "conflict", True),
    ("content", "conflict", True),
    ("provenance", "conflict", True),
    ("content", "contradiction_ids", ["relation_current"]),
    ("meta", "contradiction_claim_ids", ["claim_current"]),
    ("provenance", "deprecated", True),
    ("content", KNOWLEDGE_CAPABILITY_MARKER_KEY, {"contradiction_state": "contradicted"}),
    ("meta", KNOWLEDGE_CAPABILITY_MARKER_KEY, {"contradiction_state": "contradicted"}),
    ("masked", KNOWLEDGE_CAPABILITY_MARKER_KEY, {"contradiction_state": "contradicted"}),
])
def test_changed_support_never_erases_an_active_page_safety_gate(
    lineage_runtime, container: str, key: str, value,
) -> None:
    runtime, scope, _source, claim, page = lineage_runtime
    _changed_claim(runtime, claim)
    if container == "masked":
        page.content[key] = {"contradiction_state": "none"}
        page.meta[key] = deepcopy(value)
    else:
        getattr(page, container)[key] = deepcopy(value)
    runtime.store.rewrite(page)
    if key != KNOWLEDGE_CAPABILITY_MARKER_KEY:
        assert _candidate_from_record(page, store=runtime.store)[1] == "unsafe_source_status"
    before = _snapshot(runtime)

    report = runtime.evolution.reconcile_knowledge(scope=asdict(scope))

    assert report["support_lineage_marked_count"] == report["page_refresh_count"] == 0
    assert {item["reason"] for item in report["support_lineage_blocked"]} == {"unsafe_page"}
    assert _snapshot(runtime) == before
    assert _exact(runtime, page).status == "active"


@pytest.mark.parametrize("container", ["content", "meta", "masked"])
def test_changed_support_with_contradicted_marker_cannot_authorize_refresh(
    lineage_runtime, container: str,
) -> None:
    runtime, scope, _source, claim, page = lineage_runtime
    _changed_claim(runtime, claim)
    if container == "masked":
        claim.content[KNOWLEDGE_CAPABILITY_MARKER_KEY] = {"contradiction_state": "none"}
        claim.meta[KNOWLEDGE_CAPABILITY_MARKER_KEY] = {"contradiction_state": "contradicted"}
    else:
        getattr(claim, container)[KNOWLEDGE_CAPABILITY_MARKER_KEY] = {"contradiction_state": "contradicted"}
    runtime.store.rewrite(claim)
    before = _snapshot(runtime)

    report = runtime.evolution.reconcile_knowledge(scope=asdict(scope))

    assert report["support_lineage_marked_count"] == report["page_refresh_count"] == 0
    assert {item["reason"] for item in report["support_lineage_blocked"]} == {"unsafe_support"}
    assert _snapshot(runtime) == before
    assert _exact(runtime, page).status == "active"


def _assert_peer_blocked_before_owner(runtime: Runtime, scope: ScopeRef, monkeypatch) -> dict:
    pending = RecordEnvelope.create(kind="reflection", title="Keep the unrelated pending export", scope=scope)
    with runtime.store._lock:
        runtime.store.sqlite.enqueue_export(
            stream="records", payload=pending.to_dict(), operation_id="preserve-peer-fence-pending",
        )
    before = _snapshot(runtime)
    def no_owner(_callback):
        pytest.fail("unsafe source siblings must be rejected before the mutation owner")
    monkeypatch.setattr(runtime.store, "mutate_records_atomically", no_owner)

    report = runtime.refresh_knowledge_pages(scope=asdict(scope))

    assert not report["ok"]
    assert report["persisted"] is False and report["mutation_count"] == 0
    assert report["recompiled_page_count"] == report["retired_projection_count"] == 0
    assert _snapshot(runtime) == before
    with runtime.store._lock:
        assert runtime.store.sqlite.execute(
            "SELECT state FROM export_outbox WHERE operation_id='preserve-peer-fence-pending'",
        ).fetchone()[0] == "pending"
    return report


@pytest.mark.parametrize("bad_kind", ["paper", "topic"])
@pytest.mark.parametrize("bad", ["malformed", "unsafe", "foreign_owner", "missing", "masked_page", "masked_claim"])
def test_lineage_refresh_cannot_rehabilitate_unsafe_paper_or_topic_sibling(
    lineage_runtime, monkeypatch, bad_kind: str, bad: str,
) -> None:
    runtime, scope, source, claim, paper = lineage_runtime
    # A separate support is needed to prove a bad sibling's claim cannot be
    # laundered by a safe page which depends on a different changed claim.
    topic_claim = ClaimCard(
        claim_card_id="claim_topic_support", paper_source_id=source.record_id,
        paper_extract_id="extract_lineage_probe",
        claim_text="OpenClaw runtime should preserve stable agent identity for source provenance and operational decisions.",
        confidence=0.95,
    ).to_record(scope=scope)
    runtime.store.append(topic_claim)
    topic_claim = _exact(runtime, topic_claim)
    topic = _add_topic_page(runtime, scope, source, topic_claim)
    bad_page = paper if bad_kind == "paper" else topic
    bad_claim = claim if bad_kind == "paper" else topic_claim
    safe_claim = topic_claim if bad_kind == "paper" else claim
    marked_sibling = bad in {"unsafe", "missing", "masked_claim"}
    if marked_sibling:
        bad_page.status = "needs_refresh"
        bad_page.meta["refresh_reason"] = "support_lineage_changed"
    if bad == "malformed":
        bad_page.content["supporting_claim_refs"].append({"record_id": "malformed_later_ref"})
    elif bad == "unsafe":
        bad_page.provenance["conflict"] = True
    elif bad == "foreign_owner":
        bad_page.scope = ScopeRef.from_dict({**asdict(scope), "user_id": ""})
    elif bad == "missing":
        ref = deepcopy(bad_page.content["supporting_claim_refs"][0])
        ref["record_id"] = "claim_missing_peer_support"
        bad_page.content["supporting_claim_refs"].append(ref)
        bad_page.content["supporting_claim_ids"].append(ref["record_id"])
    elif bad == "masked_page":
        bad_page.content[KNOWLEDGE_CAPABILITY_MARKER_KEY] = {"contradiction_state": "none"}
        bad_page.meta[KNOWLEDGE_CAPABILITY_MARKER_KEY] = {"contradiction_state": "contradicted"}
    elif bad == "masked_claim":
        bad_claim.content[KNOWLEDGE_CAPABILITY_MARKER_KEY] = {"contradiction_state": "none"}
        bad_claim.meta[KNOWLEDGE_CAPABILITY_MARKER_KEY] = {"contradiction_state": "contradicted"}
        runtime.store.rewrite(bad_claim)
    runtime.store.rewrite(bad_page, previous_scope=scope)
    _changed_claim(runtime, safe_claim)
    reconciled = runtime.evolution.reconcile_knowledge(scope=asdict(scope))
    assert reconciled["support_lineage_marked_count"] == 1

    report = _assert_peer_blocked_before_owner(runtime, scope, monkeypatch)

    assert all(item["reason"].startswith("support_lineage_peer_") for item in report["blocked"])


@pytest.mark.parametrize("corrupt_historical", [False, True])
def test_deprecated_non_target_history_allows_repeat_refresh_but_keeps_physical_guard(
    lineage_runtime, corrupt_historical: bool,
) -> None:
    runtime, scope, source, claim, paper = lineage_runtime
    topic = _add_topic_page(runtime, scope, source, claim)
    _changed_claim(runtime, claim)
    assert runtime.evolution.reconcile_knowledge(scope=asdict(scope))["support_lineage_marked_count"] == 2
    first = runtime.refresh_knowledge_pages(scope=asdict(scope))
    assert first["ok"] and first["deprecated_page_count"] == 1
    historical = _exact(runtime, topic)
    assert historical.status == "deprecated"
    historical_refs = canonical_payload_json(historical.content["supporting_claim_refs"])
    claim = _exact(runtime, claim)
    claim.meta["source_revision"] = "reviewed-upstream-revision-3"
    claim.touch()
    runtime.store.rewrite(claim)
    assert runtime.evolution.reconcile_knowledge(scope=asdict(scope))["support_lineage_marked_count"] == 1
    if corrupt_historical:
        with runtime.store._lock:
            db = runtime.store.sqlite
            db.execute("UPDATE records SET title='wrong historical SQL projection' WHERE record_id=?", (topic.record_id,))
            db.commit()
    before = _snapshot(runtime)

    second = runtime.refresh_knowledge_pages(scope=asdict(scope))

    if corrupt_historical:
        assert not second["ok"]
        assert second["persisted"] is False and second["mutation_count"] == 0
        assert {item["reason"] for item in second["blocked"]} == {"refresh_record_snapshot_mismatch"}
        assert _snapshot(runtime) == before
    else:
        assert second["ok"] and second["recompiled_page_count"] == 1
        assert second["recompiled_page_ids"] == [paper.record_id]
        assert _exact(runtime, paper).status == "active"
        assert _exact(runtime, topic).status == "deprecated"
        assert canonical_payload_json(_exact(runtime, topic).content["supporting_claim_refs"]) == historical_refs


def test_deprecated_page_in_current_compile_targets_cannot_be_reactivated(
    lineage_runtime, monkeypatch,
) -> None:
    runtime, scope, source, claim, paper = lineage_runtime
    _add_topic_page(runtime, scope, source, claim)
    paper.status = "deprecated"
    paper.content["deprecated"] = True
    paper.meta["deprecated"] = True
    runtime.store.rewrite(paper)
    _changed_claim(runtime, claim)
    assert runtime.evolution.reconcile_knowledge(scope=asdict(scope))["support_lineage_marked_count"] == 1

    report = _assert_peer_blocked_before_owner(runtime, scope, monkeypatch)

    assert {item["reason"] for item in report["blocked"]} == {"support_lineage_peer_unsafe_page"}
    assert _exact(runtime, paper).status == "deprecated"


def test_refresh_marker_retains_default_and_accepts_explicit_no_contradiction() -> None:
    old = refresh_capability_applicability_marker("recompiled")
    assert old["contradiction_state"] == "resolved" and old["applicability"] == "candidate"
    new = refresh_capability_applicability_marker("recompiled", contradiction_state="none")
    assert new["contradiction_state"] == "none" and new["applicability"] == "candidate"
    audited = refresh_capability_applicability_marker("recompiled", contradiction_state="resolved", resolved_contradiction_ids=["relation_audit"])
    assert audited["resolved_contradiction_ids"] == ["relation_audit"]


@pytest.mark.parametrize("state", ["", "approved", "RESOLVED", 1, False])
def test_refresh_marker_rejects_unbounded_contradiction_state(state) -> None:
    with pytest.raises(KnowledgeCapabilityBridgeError, match="contradiction state"):
        refresh_capability_applicability_marker("recompiled", contradiction_state=state)
