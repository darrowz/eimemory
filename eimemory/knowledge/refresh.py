from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from eimemory.intake.papers import artifacts as paper_artifacts
from eimemory.intake.papers.artifacts import PaperArtifactError
from eimemory.knowledge.capabilities import (
    KNOWLEDGE_CAPABILITY_MARKER_KEY,
    refresh_capability_applicability_marker,
)
from eimemory.knowledge.compiler import compile_paper_knowledge
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.storage.runtime_store import RuntimeStore


REFRESH_SOURCE = "eimemory.knowledge.refresh"
PROJECTION_TYPE = "operational_knowledge"
_BLOCKED_SOURCE_STATUSES = {"rejected", "deprecated", "conflicted", "needs_refresh"}
_REFRESH_INPUT_KINDS = ["knowledge_page", "claim_card", "entity_record", "paper_source"]
_RECORD_PAGE_SIZE = 1_000


@dataclass(slots=True)
class _RefreshInputs:
    pages: list[RecordEnvelope]
    pages_by_id: dict[str, RecordEnvelope]
    pages_by_source: dict[str, list[RecordEnvelope]]
    claims_by_source: dict[str, list[RecordEnvelope]]
    entities_by_source: dict[str, list[RecordEnvelope]]
    sources_by_id: dict[str, RecordEnvelope]


@dataclass(slots=True)
class _PlanRecords:
    source_record: RecordEnvelope | None
    source_pages: list[RecordEnvelope]
    source_claims: list[RecordEnvelope]
    source_entities: list[RecordEnvelope]


@dataclass(slots=True)
class _RefreshPlan:
    source_id: str
    stale_pages: list[RecordEnvelope]
    source_pages: list[RecordEnvelope]
    source_record: RecordEnvelope | None
    source_claims: list[RecordEnvelope]
    source_entities: list[RecordEnvelope]
    invalidated_record_ids: set[str]
    contradiction_audit_ids: tuple[str, ...] = ()
    blocked_reason: str = ""
    source_version_digest: str = ""
    compile_input_digest: str = ""
    refresh_run_id: str = ""
    compiled_records: list[RecordEnvelope] | None = None


def refresh_knowledge_pages(
    store: RuntimeStore,
    *,
    scope: ScopeRef | dict | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Fail closed on conflicted compiled knowledge and rebuild safe projections.

    Reconciliation only marks a page as ``needs_refresh``.  This consumer
    validates that the backing paper has a durable canonical text artifact,
    retires projections made from stale pages or conflicted claims, and
    recompiles pages only from still-active source claims.  An unresolved
    conflict therefore leaves the page blocked instead of silently returning
    its old projection to recall.
    """
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    max_pages = max(1, int(limit))
    inputs = _load_refresh_inputs(store, scope=scope_ref)
    stale_pages = [record for record in inputs.pages if record.status == "needs_refresh"][:max_pages]
    if not stale_pages:
        return _empty_report()

    plans = _prepare_plans(
        store=store,
        scope=scope_ref,
        stale_pages=stale_pages,
        inputs=inputs,
    )
    invalidated_record_ids = {
        record_id
        for plan in plans
        for record_id in plan.invalidated_record_ids
    }

    def mutation(sqlite) -> tuple[dict[str, Any], list[RecordEnvelope], list]:
        changed: list[RecordEnvelope] = []
        retired_projection_ids: list[str] = []
        current_inputs = _load_refresh_inputs(sqlite, scope=scope_ref)
        stale_source_ids, current_records = _revalidate_plans(
            store,
            inputs=current_inputs,
            plans=plans,
        )
        if stale_source_ids:
            return _retry_required_report(stale_source_ids), [], []

        for plan in plans:
            records = current_records[plan.source_id]
            current_pages_by_id = {record.record_id: record for record in records.source_pages}
            plan.stale_pages = [
                current_pages_by_id[page.record_id]
                for page in plan.stale_pages
                if page.record_id in current_pages_by_id
            ]
            plan.source_pages = records.source_pages
            plan.source_record = records.source_record
            plan.source_claims = records.source_claims
            plan.source_entities = records.source_entities

        projection_records = _list_all_records(
            sqlite,
            kinds=["memory"],
            scope=scope_ref,
        )
        projected_to_retire = [
            record
            for record in projection_records
            if _projection_source_id(record) in invalidated_record_ids
            and record.status != "deprecated"
        ]
        for projection in projected_to_retire:
            plan = _first_plan_for_record(plans, _projection_source_id(projection))
            _retire_projection(projection, refresh_run_id=plan.refresh_run_id if plan else "")
            sqlite.upsert(projection, commit=False)
            changed.append(projection)
            retired_projection_ids.append(projection.record_id)

        recompiled_page_ids: list[str] = []
        blocked_page_ids: list[str] = []
        deprecated_page_ids: list[str] = []
        for plan in plans:
            if plan.blocked_reason:
                for page in plan.stale_pages:
                    if _mark_page_blocked(page, plan=plan):
                        sqlite.upsert(page, commit=False)
                        changed.append(page)
                    blocked_page_ids.append(page.record_id)
                continue

            compiled_records = list(plan.compiled_records or [])
            replacement_ids = {record.record_id for record in compiled_records}
            previous_by_id = {record.record_id: record for record in plan.source_pages}
            for previous in plan.source_pages:
                if previous.record_id in replacement_ids:
                    continue
                if _deprecate_page(previous, plan=plan, superseded_by=sorted(replacement_ids)):
                    sqlite.upsert(previous, commit=False)
                    changed.append(previous)
                    deprecated_page_ids.append(previous.record_id)
            for page in compiled_records:
                _annotate_recompiled_page(page, plan=plan, previous=previous_by_id.get(page.record_id))
                sqlite.upsert(page, commit=False)
                changed.append(page)
                recompiled_page_ids.append(page.record_id)

        report = {
            "ok": True,
            "refresh_status": "ok",
            "retry_required": False,
            "stale_source_ids": [],
            "marked_for_refresh_count": len(stale_pages),
            "recompiled_page_count": len(recompiled_page_ids),
            "blocked_page_count": len(blocked_page_ids),
            "retired_projection_count": len(retired_projection_ids),
            "deprecated_page_count": len(deprecated_page_ids),
            "recompiled_page_ids": recompiled_page_ids,
            "blocked_page_ids": blocked_page_ids,
            "retired_projection_ids": retired_projection_ids,
            "blocked": [
                {
                    "source_id": plan.source_id,
                    "page_ids": [page.record_id for page in plan.stale_pages],
                    "reason": plan.blocked_reason,
                }
                for plan in plans
                if plan.blocked_reason
            ],
        }
        return report, changed, []

    return store.mutate_records_atomically(mutation)


def _prepare_plans(
    *,
    store: RuntimeStore,
    scope: ScopeRef,
    stale_pages: list[RecordEnvelope],
    inputs: _RefreshInputs,
) -> list[_RefreshPlan]:
    grouped: dict[str, list[RecordEnvelope]] = defaultdict(list)
    for page in stale_pages:
        source_id = _primary_source_id(page)
        grouped[source_id].append(page)

    plans: list[_RefreshPlan] = []
    for source_id, source_stale_pages in grouped.items():
        records = _records_for_plan(
            inputs,
            source_id=source_id,
            fallback_page_ids=[page.record_id for page in source_stale_pages],
        )
        source_pages = records.source_pages
        source_claims = records.source_claims
        source_entities = records.source_entities
        source_record = records.source_record
        invalidated_record_ids = {page.record_id for page in source_pages}
        invalidated_record_ids.update(
            claim.record_id
            for claim in source_claims
            if _is_conflicted_claim(claim)
        )
        plan = _RefreshPlan(
            source_id=source_id,
            stale_pages=list(source_stale_pages),
            source_pages=source_pages,
            source_record=source_record,
            source_claims=source_claims,
            source_entities=source_entities,
            invalidated_record_ids=invalidated_record_ids,
            contradiction_audit_ids=_contradiction_audit_ids(source_pages),
        )
        canonical_text, blocked_reason = _canonical_source_text(store, source_record)
        if not source_id:
            blocked_reason = "missing_paper_source_id"
        active_claims = [
            claim
            for claim in source_claims
            if claim.status == "active" and not _is_conflicted_claim(claim)
        ]
        active_entities = [
            entity
            for entity in source_entities
            if entity.status not in _BLOCKED_SOURCE_STATUSES
        ]
        plan.source_version_digest = _source_version_digest(
            source_id=source_id,
            source_record=source_record,
            canonical_text=canonical_text,
            source_claims=source_claims,
            active_claims=active_claims,
            source_entities=source_entities,
            active_entities=active_entities,
            source_pages=source_pages,
            contradiction_audit_ids=plan.contradiction_audit_ids,
        )
        plan.compile_input_digest = _compile_input_digest(
            source_id=source_id,
            source_record=source_record,
            canonical_text=canonical_text,
            active_claims=active_claims,
            active_entities=active_entities,
            source_pages=source_pages,
            source_version_digest=plan.source_version_digest,
            contradiction_audit_ids=plan.contradiction_audit_ids,
        )
        plan.refresh_run_id = _refresh_run_id(source_id, plan.compile_input_digest)
        if blocked_reason:
            plan.blocked_reason = blocked_reason
        elif not active_claims:
            plan.blocked_reason = "unresolved_conflict_no_active_claims"
        else:
            try:
                title = str((source_record.content if source_record else {}).get("title") or (source_record.title if source_record else "") or source_id)
                compilation = compile_paper_knowledge(
                    paper_source_id=source_id,
                    paper_title=title,
                    claim_records=active_claims,
                    entity_records=active_entities,
                    provenance={
                        **(dict(source_record.provenance or {}) if source_record is not None else {}),
                        "paper_source_id": source_id,
                        "canonical_text_ref": str((source_record.content if source_record else {}).get("normalized_text_ref") or ""),
                        "refresh_run_id": plan.refresh_run_id,
                    },
                )
                plan.compiled_records = compilation.to_records(scope=scope)
            except Exception as exc:  # pragma: no cover - defensive failure-closed boundary
                plan.blocked_reason = f"recompile_failed_{type(exc).__name__}"
        plans.append(plan)
    return plans


def _revalidate_plans(
    store: RuntimeStore,
    *,
    inputs: _RefreshInputs,
    plans: list[_RefreshPlan],
) -> tuple[list[str], dict[str, _PlanRecords]]:
    stale: list[str] = []
    current_records: dict[str, _PlanRecords] = {}
    for plan in plans:
        records = _records_for_plan(
            inputs,
            source_id=plan.source_id,
            fallback_page_ids=[page.record_id for page in plan.stale_pages],
        )
        current_records[plan.source_id] = records
        current = _current_plan_inputs(store, plan=plan, records=records)
        if (
            current["source_version_digest"] != plan.source_version_digest
            or current["compile_input_digest"] != plan.compile_input_digest
        ):
            stale.append(plan.source_id)
    return sorted({source_id for source_id in stale}), current_records


def _current_plan_inputs(
    store: RuntimeStore,
    *,
    plan: _RefreshPlan,
    records: _PlanRecords,
) -> dict[str, Any]:
    source_record = records.source_record
    source_pages = records.source_pages
    source_claims = records.source_claims
    source_entities = records.source_entities
    canonical_text, _blocked_reason = _canonical_source_text(store, source_record)
    active_claims = [
        claim
        for claim in source_claims
        if claim.status == "active" and not _is_conflicted_claim(claim)
    ]
    active_entities = [
        entity
        for entity in source_entities
        if entity.status not in _BLOCKED_SOURCE_STATUSES
    ]
    contradiction_audit_ids = _contradiction_audit_ids(source_pages)
    source_version_digest = _source_version_digest(
        source_id=plan.source_id,
        source_record=source_record,
        canonical_text=canonical_text,
        source_claims=source_claims,
        active_claims=active_claims,
        source_entities=source_entities,
        active_entities=active_entities,
        source_pages=source_pages,
        contradiction_audit_ids=contradiction_audit_ids,
    )
    return {
        "source_version_digest": source_version_digest,
        "compile_input_digest": _compile_input_digest(
            source_id=plan.source_id,
            source_record=source_record,
            canonical_text=canonical_text,
            active_claims=active_claims,
            active_entities=active_entities,
            source_pages=source_pages,
            source_version_digest=source_version_digest,
            contradiction_audit_ids=contradiction_audit_ids,
        ),
    }


def _load_refresh_inputs(repository, *, scope: ScopeRef) -> _RefreshInputs:
    records = _list_all_records(
        repository,
        kinds=_REFRESH_INPUT_KINDS,
        scope=scope,
    )
    pages: list[RecordEnvelope] = []
    pages_by_id: dict[str, RecordEnvelope] = {}
    pages_by_source: dict[str, list[RecordEnvelope]] = defaultdict(list)
    claims_by_source: dict[str, list[RecordEnvelope]] = defaultdict(list)
    entities_by_source: dict[str, list[RecordEnvelope]] = defaultdict(list)
    sources_by_id: dict[str, RecordEnvelope] = {}
    grouped_by_kind = {
        "knowledge_page": pages_by_source,
        "claim_card": claims_by_source,
        "entity_record": entities_by_source,
    }
    for record in records:
        if record.kind == "knowledge_page":
            pages.append(record)
            pages_by_id.setdefault(record.record_id, record)
        elif record.kind == "paper_source":
            sources_by_id.setdefault(record.record_id, record)
        target = grouped_by_kind.get(record.kind)
        if target is None:
            continue
        for source_id in _record_source_ids(record):
            target[source_id].append(record)
    return _RefreshInputs(
        pages=pages,
        pages_by_id=pages_by_id,
        pages_by_source=dict(pages_by_source),
        claims_by_source=dict(claims_by_source),
        entities_by_source=dict(entities_by_source),
        sources_by_id=sources_by_id,
    )


def _records_for_plan(
    inputs: _RefreshInputs,
    *,
    source_id: str,
    fallback_page_ids: list[str],
) -> _PlanRecords:
    source_pages = list(inputs.pages_by_source.get(source_id, ())) if source_id else []
    if not source_pages:
        source_pages = [
            inputs.pages_by_id[record_id]
            for record_id in fallback_page_ids
            if record_id in inputs.pages_by_id
        ]
    return _PlanRecords(
        source_record=inputs.sources_by_id.get(source_id),
        source_pages=source_pages,
        source_claims=list(inputs.claims_by_source.get(source_id, ())) if source_id else [],
        source_entities=list(inputs.entities_by_source.get(source_id, ())) if source_id else [],
    )


def _canonical_source_text(store: RuntimeStore, source_record: RecordEnvelope | None) -> tuple[str, str]:
    if source_record is None:
        return "", "missing_paper_source"
    source_status = str(source_record.status or "").lower()
    if source_status in _BLOCKED_SOURCE_STATUSES:
        return "", f"blocked_source_status_{source_status}"
    payload = source_record.content if isinstance(source_record.content, dict) else {}
    reference = str(payload.get("normalized_text_ref") or "").strip()
    pdf_blob_ref = str(payload.get("pdf_blob_ref") or "").strip()
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    artifact = dict(metadata.get("artifact") or {})
    if not reference or not pdf_blob_ref:
        return "", "blocked_missing_canonical_text"
    verifier = getattr(paper_artifacts, "load_verified_canonical_text", None)
    if not callable(verifier):
        # A bare root-relative text file is not canonical evidence.  Keep this
        # failure closed until the artifact module can validate its manifest,
        # hashes, and both artifact references together.
        return "", "blocked_artifact_verifier_unavailable"
    try:
        result = verifier(
            store.root,
            pdf_blob_ref=pdf_blob_ref,
            normalized_text_ref=reference,
            artifact=artifact,
        )
        text = str(result[0] if isinstance(result, tuple) else result or "")
        if not text:
            return "", "blocked_canonical_text_invalid"
        return text, ""
    except PaperArtifactError as exc:
        return "", f"blocked_{exc.code}"
    except Exception as exc:  # pragma: no cover - defensive fail-closed boundary
        return "", f"blocked_artifact_verification_failed_{type(exc).__name__}"


def _compile_input_digest(
    *,
    source_id: str,
    source_record: RecordEnvelope | None,
    canonical_text: str,
    active_claims: list[RecordEnvelope],
    active_entities: list[RecordEnvelope],
    source_pages: list[RecordEnvelope],
    source_version_digest: str,
    contradiction_audit_ids: tuple[str, ...],
) -> str:
    payload = {
        "source_id": source_id,
        "source_version_digest": source_version_digest,
        "source_record": _record_payload(source_record),
        "canonical_artifact": _canonical_artifact_identity(source_record),
        "canonical_text_sha256": _sha256(canonical_text),
        "active_claims": _record_payloads(active_claims),
        "active_entities": _record_payloads(active_entities),
        "source_pages": _refresh_page_payloads(source_pages),
        "contradiction_audit_ids": list(contradiction_audit_ids),
    }
    return _sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _source_version_digest(
    *,
    source_id: str,
    source_record: RecordEnvelope | None,
    canonical_text: str,
    source_claims: list[RecordEnvelope],
    active_claims: list[RecordEnvelope],
    source_entities: list[RecordEnvelope],
    active_entities: list[RecordEnvelope],
    source_pages: list[RecordEnvelope],
    contradiction_audit_ids: tuple[str, ...],
) -> str:
    payload = {
        "source_id": source_id,
        "source_record": _record_payload(source_record),
        "canonical_artifact": _canonical_artifact_identity(source_record),
        "canonical_text_sha256": _sha256(canonical_text),
        "source_claims": _record_payloads(source_claims),
        "active_claim_ids": [record.record_id for record in sorted(active_claims, key=lambda item: item.record_id)],
        "source_entities": _record_payloads(source_entities),
        "active_entity_ids": [record.record_id for record in sorted(active_entities, key=lambda item: item.record_id)],
        "source_pages": _refresh_page_payloads(source_pages),
        "contradiction_audit_ids": list(contradiction_audit_ids),
    }
    return _sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _record_payload(record: RecordEnvelope | None) -> dict[str, Any] | None:
    return record.to_dict() if record is not None else None


def _record_payloads(records: list[RecordEnvelope]) -> list[dict[str, Any]]:
    return [
        record.to_dict()
        for record in sorted(records, key=lambda item: (item.record_id, item.kind))
    ]


def _refresh_page_payloads(records: list[RecordEnvelope]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for record in sorted(records, key=lambda item: (item.record_id, item.kind)):
        payload = record.to_dict()
        time_payload = payload.get("time") if isinstance(payload.get("time"), dict) else {}
        time_payload["updated_at"] = ""
        payload["time"] = time_payload
        content = payload.get("content") if isinstance(payload.get("content"), dict) else {}
        for key in (
            "refresh",
            "deprecated",
            KNOWLEDGE_CAPABILITY_MARKER_KEY,
        ):
            content.pop(key, None)
        payload["content"] = content
        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        for key in (
            "deprecated",
            "refresh_state",
            "refresh_blocked_reason",
            "refresh_reason",
            "refresh_run_id",
            "source_version_digest",
            "compile_input_digest",
            "previous_compile_digest",
            "superseded_by",
            "resolved_contradiction_ids",
            KNOWLEDGE_CAPABILITY_MARKER_KEY,
        ):
            meta.pop(key, None)
        payload["meta"] = meta
        provenance = payload.get("provenance") if isinstance(payload.get("provenance"), dict) else {}
        for key in (
            "refresh_run_id",
            "source_version_digest",
            "compile_input_digest",
            "knowledge_capability_refresh",
            "resolved_contradiction_ids",
            "refresh_audit",
            "retired_reason",
            KNOWLEDGE_CAPABILITY_MARKER_KEY,
        ):
            provenance.pop(key, None)
        payload["provenance"] = provenance
        payloads.append(payload)
    return payloads


def _canonical_artifact_identity(record: RecordEnvelope | None) -> dict[str, Any]:
    if record is None or not isinstance(record.content, dict):
        return {}
    metadata = record.content.get("metadata")
    if not isinstance(metadata, dict):
        return {}
    artifact = metadata.get("artifact")
    return dict(artifact) if isinstance(artifact, dict) else {}


def _retry_required_report(stale_source_ids: list[str]) -> dict[str, Any]:
    return {
        "ok": False,
        "refresh_status": "retry_required",
        "retry_required": True,
        "stale_source_ids": list(stale_source_ids),
        "marked_for_refresh_count": 0,
        "recompiled_page_count": 0,
        "blocked_page_count": 0,
        "retired_projection_count": 0,
        "deprecated_page_count": 0,
        "recompiled_page_ids": [],
        "blocked_page_ids": [],
        "retired_projection_ids": [],
        "blocked": [],
    }


def _mark_page_blocked(page: RecordEnvelope, *, plan: _RefreshPlan) -> bool:
    refresh = {
        "state": "blocked",
        "reason": plan.blocked_reason,
        "refresh_run_id": plan.refresh_run_id,
        "source_version_digest": plan.source_version_digest,
        "compile_input_digest": plan.compile_input_digest,
    }
    current = page.content.get("refresh") if isinstance(page.content, dict) else None
    capability_marker = refresh_capability_applicability_marker(
        "blocked",
        reason=plan.blocked_reason,
    )
    if (
        page.status == "needs_refresh"
        and current == refresh
        and page.content.get(KNOWLEDGE_CAPABILITY_MARKER_KEY) == capability_marker
    ):
        return False
    page.status = "needs_refresh"
    page.content["refresh"] = refresh
    page.meta["refresh_state"] = "blocked"
    page.meta["refresh_blocked_reason"] = plan.blocked_reason
    page.meta["refresh_run_id"] = plan.refresh_run_id
    page.meta["source_version_digest"] = plan.source_version_digest
    page.meta["compile_input_digest"] = plan.compile_input_digest
    page.content[KNOWLEDGE_CAPABILITY_MARKER_KEY] = capability_marker
    page.meta[KNOWLEDGE_CAPABILITY_MARKER_KEY] = capability_marker
    page.provenance["knowledge_capability_refresh"] = capability_marker
    page.touch()
    return True


def _deprecate_page(page: RecordEnvelope, *, plan: _RefreshPlan, superseded_by: list[str]) -> bool:
    capability_marker = refresh_capability_applicability_marker(
        "superseded",
        reason="not_present_in_recompile",
    )
    if (
        page.status == "deprecated"
        and page.meta.get("refresh_run_id") == plan.refresh_run_id
        and page.content.get(KNOWLEDGE_CAPABILITY_MARKER_KEY) == capability_marker
    ):
        return False
    page.status = "deprecated"
    page.content["deprecated"] = True
    page.content["refresh"] = {
        "state": "superseded",
        "reason": "not_present_in_recompile",
        "refresh_run_id": plan.refresh_run_id,
        "source_version_digest": plan.source_version_digest,
        "superseded_by": superseded_by,
    }
    page.meta["deprecated"] = True
    page.meta["refresh_state"] = "superseded"
    page.meta["refresh_run_id"] = plan.refresh_run_id
    page.meta["source_version_digest"] = plan.source_version_digest
    page.meta["superseded_by"] = superseded_by
    page.content[KNOWLEDGE_CAPABILITY_MARKER_KEY] = capability_marker
    page.meta[KNOWLEDGE_CAPABILITY_MARKER_KEY] = capability_marker
    page.provenance["knowledge_capability_refresh"] = capability_marker
    page.touch()
    return True


def _annotate_recompiled_page(
    page: RecordEnvelope,
    *,
    plan: _RefreshPlan,
    previous: RecordEnvelope | None,
) -> None:
    previous_digest = _record_digest(previous) if previous is not None else ""
    page.source = REFRESH_SOURCE
    audit_ids = list(plan.contradiction_audit_ids)
    # Direct contradiction fields are a safety gate for the operational
    # projector.  This page is rebuilt solely from active, non-conflicted
    # claims, so preserve the historical relation trail under an explicit
    # resolved-audit namespace instead of reintroducing that gate.
    page.content.pop("contradiction_ids", None)
    page.content.pop("contradiction_claim_ids", None)
    page.meta.pop("contradiction_ids", None)
    page.meta.pop("contradiction_claim_ids", None)
    page.provenance.pop("contradiction_ids", None)
    page.provenance.pop("contradiction_claim_ids", None)
    page.content["refresh"] = {
        "state": "recompiled",
        "reason": "claim_contradiction",
        "refresh_run_id": plan.refresh_run_id,
        "source_version_digest": plan.source_version_digest,
        "compile_input_digest": plan.compile_input_digest,
        "previous_compile_digest": previous_digest,
        "resolved_contradiction_ids": audit_ids,
    }
    if audit_ids:
        page.content["resolved_contradiction_ids"] = audit_ids
        page.meta["resolved_contradiction_ids"] = audit_ids
        page.provenance["resolved_contradiction_ids"] = audit_ids
        page.evidence = _dedupe_strings([*page.evidence, *audit_ids])
    page.meta["refresh_state"] = "recompiled"
    page.meta["refresh_reason"] = "claim_contradiction"
    page.meta["refresh_run_id"] = plan.refresh_run_id
    page.meta["source_version_digest"] = plan.source_version_digest
    page.meta["compile_input_digest"] = plan.compile_input_digest
    page.meta["previous_compile_digest"] = previous_digest
    page.provenance["refresh_run_id"] = plan.refresh_run_id
    page.provenance["source_version_digest"] = plan.source_version_digest
    page.provenance["compile_input_digest"] = plan.compile_input_digest
    capability_marker = refresh_capability_applicability_marker(
        "recompiled",
        reason="claim_contradiction",
        resolved_contradiction_ids=audit_ids,
    )
    page.content[KNOWLEDGE_CAPABILITY_MARKER_KEY] = capability_marker
    page.meta[KNOWLEDGE_CAPABILITY_MARKER_KEY] = capability_marker
    page.provenance["knowledge_capability_refresh"] = capability_marker
    if audit_ids:
        page.provenance["refresh_audit"] = {
            "resolved_contradiction_ids": audit_ids,
        }
    page.touch()


def _retire_projection(record: RecordEnvelope, *, refresh_run_id: str) -> None:
    record.status = "deprecated"
    record.content["deprecated"] = True
    record.content["retired_reason"] = "knowledge_refresh"
    record.meta["deprecated"] = True
    record.meta["retired_reason"] = "knowledge_refresh"
    record.meta["refresh_run_id"] = refresh_run_id
    record.provenance["retired_reason"] = "knowledge_refresh"
    record.provenance["refresh_run_id"] = refresh_run_id
    record.touch()


def _primary_source_id(record: RecordEnvelope) -> str:
    source_ids = _record_source_ids(record)
    return source_ids[0] if source_ids else ""


def _record_source_ids(record: RecordEnvelope) -> list[str]:
    content = record.content if isinstance(record.content, dict) else {}
    provenance = record.provenance if isinstance(record.provenance, dict) else {}
    values = content.get("source_ids")
    if not isinstance(values, (list, tuple)):
        values = provenance.get("source_ids")
    result = [str(value).strip() for value in (values or []) if str(value).strip()]
    for value in (content.get("paper_source_id"), provenance.get("paper_source_id")):
        normalized = str(value or "").strip()
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def _contradiction_audit_ids(records: list[RecordEnvelope]) -> tuple[str, ...]:
    """Carry resolved contradiction relations forward without re-blocking output.

    Reconciliation stores direct ``contradiction_ids`` on a stale page.  A
    recompiled page must retain those relation ids for auditability, but the
    operational projector intentionally treats direct contradiction fields as
    unsafe.  Collect both legacy direct fields and prior resolved audit fields
    so repeated refreshes do not sever the trace.
    """
    values: list[str] = []
    for record in records:
        content = record.content if isinstance(record.content, dict) else {}
        meta = record.meta if isinstance(record.meta, dict) else {}
        provenance = record.provenance if isinstance(record.provenance, dict) else {}
        refresh = content.get("refresh") if isinstance(content.get("refresh"), dict) else {}
        refresh_audit = provenance.get("refresh_audit") if isinstance(provenance.get("refresh_audit"), dict) else {}
        for container, key in (
            (content, "contradiction_ids"),
            (content, "contradiction_claim_ids"),
            (meta, "contradiction_ids"),
            (meta, "contradiction_claim_ids"),
            (provenance, "contradiction_ids"),
            (provenance, "contradiction_claim_ids"),
            (content, "resolved_contradiction_ids"),
            (meta, "resolved_contradiction_ids"),
            (provenance, "resolved_contradiction_ids"),
            (refresh, "resolved_contradiction_ids"),
            (refresh_audit, "resolved_contradiction_ids"),
        ):
            raw = container.get(key) if isinstance(container, dict) else None
            if isinstance(raw, (list, tuple, set)):
                values.extend(str(item).strip() for item in raw if str(item).strip())
    return tuple(sorted(set(values)))


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = str(value or "").strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def _is_conflicted_claim(record: RecordEnvelope) -> bool:
    if record.status in _BLOCKED_SOURCE_STATUSES:
        return True
    return bool(
        record.meta.get("contradiction_claim_ids")
        or record.content.get("contradiction_claim_ids")
        or record.meta.get("contradiction_ids")
        or record.content.get("contradiction_ids")
    )


def _projection_source_id(record: RecordEnvelope) -> str:
    if not _is_operational_projection(record):
        return ""
    return str(
        record.meta.get("source_record_id")
        or record.provenance.get("source_record_id")
        or record.content.get("source_record_id")
        or ""
    )


def _is_operational_projection(record: RecordEnvelope) -> bool:
    return any(
        container.get("projection_type") == PROJECTION_TYPE
        for container in (record.meta, record.provenance, record.content)
        if isinstance(container, dict)
    )


def _first_plan_for_record(plans: list[_RefreshPlan], record_id: str) -> _RefreshPlan | None:
    return next((plan for plan in plans if record_id in plan.invalidated_record_ids), None)


def _record_digest(record: RecordEnvelope | None) -> str:
    if record is None:
        return ""
    payload = {
        "record_id": record.record_id,
        "status": record.status,
        "title": record.title,
        "summary": record.summary,
        "detail": record.detail,
        "content": record.content,
        "meta": record.meta,
    }
    return _sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":")))


def _refresh_run_id(source_id: str, compile_input_digest: str) -> str:
    # Keep the separator outside an f-string expression.  Python 3.11 rejects
    # backslashes within an f-string expression, while newer interpreters
    # accept it; eimemory still supports Python 3.11 in production.
    run_input = f"{source_id}\x1f{compile_input_digest}"
    return f"krefresh_{_sha256(run_input)[:20]}"


def _sha256(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _list_all_records(
    repository,
    *,
    kinds: list[str],
    scope: ScopeRef,
) -> list[RecordEnvelope]:
    records: list[RecordEnvelope] = []
    offset = 0
    while True:
        page = repository.list_records(
            kinds=kinds,
            scope=scope,
            limit=_RECORD_PAGE_SIZE,
            offset=offset,
        )
        records.extend(page)
        if len(page) < _RECORD_PAGE_SIZE:
            break
        offset += len(page)
    return records


def _empty_report() -> dict[str, Any]:
    return {
        "ok": True,
        "refresh_status": "ok",
        "retry_required": False,
        "stale_source_ids": [],
        "marked_for_refresh_count": 0,
        "recompiled_page_count": 0,
        "blocked_page_count": 0,
        "retired_projection_count": 0,
        "deprecated_page_count": 0,
        "recompiled_page_ids": [],
        "blocked_page_ids": [],
        "retired_projection_ids": [],
        "blocked": [],
    }
