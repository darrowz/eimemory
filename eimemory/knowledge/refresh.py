from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from eimemory.intake.papers import artifacts as paper_artifacts
from eimemory.intake.papers.artifacts import PaperArtifactError
from eimemory.knowledge.compiler import compile_paper_knowledge
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.storage.runtime_store import RuntimeStore


REFRESH_SOURCE = "eimemory.knowledge.refresh"
PROJECTION_TYPE = "operational_knowledge"
_BLOCKED_SOURCE_STATUSES = {"rejected", "deprecated", "conflicted", "needs_refresh"}


@dataclass(slots=True)
class _RefreshPlan:
    source_id: str
    stale_pages: list[RecordEnvelope]
    source_pages: list[RecordEnvelope]
    invalidated_record_ids: set[str]
    contradiction_audit_ids: tuple[str, ...] = ()
    blocked_reason: str = ""
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
    pages = _list_records(store, kinds=["knowledge_page"], scope=scope_ref, limit=max(1_000, max_pages * 10))
    stale_pages = [record for record in pages if record.status == "needs_refresh"][:max_pages]
    if not stale_pages:
        return _empty_report()

    claims = _list_records(store, kinds=["claim_card"], scope=scope_ref, limit=2_000)
    entities = _list_records(store, kinds=["entity_record"], scope=scope_ref, limit=2_000)
    sources = {
        record.record_id: record
        for record in _list_records(store, kinds=["paper_source"], scope=scope_ref, limit=2_000)
    }
    plans = _prepare_plans(
        store=store,
        scope=scope_ref,
        stale_pages=stale_pages,
        all_pages=pages,
        claims=claims,
        entities=entities,
        sources=sources,
    )
    invalidated_record_ids = {
        record_id
        for plan in plans
        for record_id in plan.invalidated_record_ids
    }
    projection_records = _list_records(store, kinds=["memory"], scope=scope_ref, limit=5_000)
    projected_to_retire = [
        record
        for record in projection_records
        if _projection_source_id(record) in invalidated_record_ids and record.status != "deprecated"
    ]

    def mutation(sqlite) -> tuple[dict[str, Any], list[RecordEnvelope], list]:
        changed: list[RecordEnvelope] = []
        retired_projection_ids: list[str] = []
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
    all_pages: list[RecordEnvelope],
    claims: list[RecordEnvelope],
    entities: list[RecordEnvelope],
    sources: dict[str, RecordEnvelope],
) -> list[_RefreshPlan]:
    grouped: dict[str, list[RecordEnvelope]] = defaultdict(list)
    for page in stale_pages:
        source_id = _primary_source_id(page)
        grouped[source_id].append(page)

    plans: list[_RefreshPlan] = []
    for source_id, source_stale_pages in grouped.items():
        source_pages = [page for page in all_pages if source_id and source_id in _record_source_ids(page)]
        if not source_pages:
            source_pages = list(source_stale_pages)
        source_claims = [claim for claim in claims if source_id and source_id in _record_source_ids(claim)]
        source_entities = [entity for entity in entities if source_id and source_id in _record_source_ids(entity)]
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
            invalidated_record_ids=invalidated_record_ids,
            contradiction_audit_ids=_contradiction_audit_ids(source_pages),
        )
        source_record = sources.get(source_id)
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
        plan.compile_input_digest = _compile_input_digest(
            source_id=source_id,
            source_record=source_record,
            canonical_text=canonical_text,
            active_claims=active_claims,
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
    contradiction_audit_ids: tuple[str, ...],
) -> str:
    artifact = {}
    if source_record is not None:
        metadata = source_record.content.get("metadata") if isinstance(source_record.content, dict) else {}
        artifact = dict(metadata.get("artifact") or {}) if isinstance(metadata, dict) else {}
    payload = {
        "source_id": source_id,
        "source_status": str(source_record.status if source_record is not None else "missing"),
        "artifact_status": str(artifact.get("status") or ""),
        "canonical_text_sha256": str(artifact.get("text_sha256") or _sha256(canonical_text)),
        "active_claims": [
            {
                "id": record.record_id,
                "summary": record.summary,
                "confidence": record.content.get("confidence", record.meta.get("confidence", "")),
            }
            for record in sorted(active_claims, key=lambda item: item.record_id)
        ],
        # Do not include the stale page envelope: marking a page blocked
        # mutates its refresh metadata and would create a fresh run id on
        # every retry.  These values are immutable source/claim evidence.
        "contradiction_audit_ids": list(contradiction_audit_ids),
    }
    return _sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _mark_page_blocked(page: RecordEnvelope, *, plan: _RefreshPlan) -> bool:
    refresh = {
        "state": "blocked",
        "reason": plan.blocked_reason,
        "refresh_run_id": plan.refresh_run_id,
        "compile_input_digest": plan.compile_input_digest,
    }
    current = page.content.get("refresh") if isinstance(page.content, dict) else None
    if page.status == "needs_refresh" and current == refresh:
        return False
    page.status = "needs_refresh"
    page.content["refresh"] = refresh
    page.meta["refresh_state"] = "blocked"
    page.meta["refresh_blocked_reason"] = plan.blocked_reason
    page.meta["refresh_run_id"] = plan.refresh_run_id
    page.meta["compile_input_digest"] = plan.compile_input_digest
    page.touch()
    return True


def _deprecate_page(page: RecordEnvelope, *, plan: _RefreshPlan, superseded_by: list[str]) -> bool:
    if page.status == "deprecated" and page.meta.get("refresh_run_id") == plan.refresh_run_id:
        return False
    page.status = "deprecated"
    page.content["deprecated"] = True
    page.content["refresh"] = {
        "state": "superseded",
        "reason": "not_present_in_recompile",
        "refresh_run_id": plan.refresh_run_id,
        "superseded_by": superseded_by,
    }
    page.meta["deprecated"] = True
    page.meta["refresh_state"] = "superseded"
    page.meta["refresh_run_id"] = plan.refresh_run_id
    page.meta["superseded_by"] = superseded_by
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
    page.content["refresh"] = {
        "state": "recompiled",
        "reason": "claim_contradiction",
        "refresh_run_id": plan.refresh_run_id,
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
    page.meta["compile_input_digest"] = plan.compile_input_digest
    page.meta["previous_compile_digest"] = previous_digest
    page.provenance["refresh_run_id"] = plan.refresh_run_id
    page.provenance["compile_input_digest"] = plan.compile_input_digest
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
            (meta, "contradiction_ids"),
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
    return f"krefresh_{_sha256(f'{source_id}\x1f{compile_input_digest}')[:20]}"


def _sha256(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _list_records(
    store: RuntimeStore,
    *,
    kinds: list[str],
    scope: ScopeRef,
    limit: int,
) -> list[RecordEnvelope]:
    return store.list_records(kinds=kinds, scope=scope, limit=max(1, int(limit)))


def _empty_report() -> dict[str, Any]:
    return {
        "ok": True,
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
