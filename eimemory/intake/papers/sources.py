from __future__ import annotations

from copy import deepcopy
from typing import Any

from eimemory.intake.papers.artifacts import (
    ARTIFACT_SCHEMA_VERSION,
    PaperArtifactError,
    materialize_paper_artifacts,
)
from eimemory.intake.papers.normalize import normalize_paper_source_payload
from eimemory.intake.papers.metadata import build_paper_metadata, deep_merge_dicts
from eimemory.models.paper_sources import PaperSource
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.storage.runtime_store import RuntimeStore, _stamp_canonical_hongtu_identity

def _paper_source_id_from_hash(source_hash: str) -> str:
    return f"psrc_{source_hash[:12]}"


def paper_source_from_payload(payload: dict[str, Any]) -> PaperSource:
    source_hash = str(payload.get("source_hash") or "")
    paper_source_id = str(payload.get("paper_source_id") or _paper_source_id_from_hash(source_hash))
    authors = tuple(str(item) for item in (payload.get("authors") or []))
    return PaperSource(
        paper_source_id=paper_source_id,
        source_kind=str(payload.get("source_kind") or ""),
        title=str(payload.get("title") or ""),
        authors=authors,
        abstract=str(payload.get("abstract") or ""),
        venue=str(payload.get("venue") or ""),
        published_at=str(payload.get("published_at") or ""),
        doi=str(payload.get("doi") or ""),
        arxiv_id=str(payload.get("arxiv_id") or ""),
        canonical_url=str(payload.get("canonical_url") or ""),
        pdf_blob_ref=str(payload.get("pdf_blob_ref") or ""),
        normalized_text_ref=str(payload.get("normalized_text_ref") or ""),
        source_hash=source_hash,
        metadata=dict(payload.get("metadata") or {}),
        provenance=dict(payload.get("provenance") or {}),
    )


def ingest_paper_source(
    store: RuntimeStore,
    paper_input: dict[str, Any],
    *,
    scope: dict[str, Any] | ScopeRef | None = None,
) -> RecordEnvelope:
    scope_ref = scope if isinstance(scope, ScopeRef) else ScopeRef.from_dict(scope)
    normalized = normalize_paper_source_payload(paper_input)
    source_id = _paper_source_id_from_hash(normalized["source_hash"])
    existing = store.get_by_id(source_id, scope=scope_ref, exact_scope=True)
    snapshot = deepcopy(existing.to_dict()) if existing is not None else None
    existing_artifact: dict[str, Any] = {}
    if existing is not None:
        if (existing.kind != "paper_source" or existing.source_id != "default"
                or existing.source != "eimemory.paper_intake"
                or existing.content.get("source_hash") != normalized["source_hash"]):
            raise ValueError("paper_source_identity_conflict")
        if existing.status not in {"active", "blocked"}:
            raise PaperArtifactError("paper_source_not_eligible", str(existing.status))
        for key in ("title", "authors", "abstract", "venue", "published_at"):
            if key not in paper_input:
                normalized[key] = deepcopy(existing.content.get(key, normalized[key]))
        previous_metadata = dict(existing.content.get("metadata") or {})
        existing_artifact = dict(previous_metadata.get("artifact") or {})
        normalized["metadata"] = build_paper_metadata(normalized, upstream_metadata=deep_merge_dicts(
            previous_metadata, paper_input.get("metadata")))
        provenance = deep_merge_dicts(existing.provenance, normalized["provenance"])
        provenance["input_keys"] = sorted(set(existing.provenance.get("input_keys") or ())
                                          | set(normalized["provenance"].get("input_keys") or ()))
        supplied_provenance = paper_input.get("provenance") or {}
        if not isinstance(supplied_provenance, dict) or "input_kind" not in supplied_provenance:
            provenance["input_kind"] = existing.provenance.get("input_kind", provenance["input_kind"])
        normalized["provenance"] = provenance
    try:
        artifacts = materialize_paper_artifacts(store.root, paper_input)
    except PaperArtifactError as exc:
        # A transient remote fetch or parser failure is evidence about this
        # source, not a reason to abort the rest of a promotion batch.
        artifacts = {
            "pdf_blob_ref": "",
            "normalized_text_ref": "",
            "artifact": {
                "schema_version": ARTIFACT_SCHEMA_VERSION,
                "status": "blocked",
                "error_code": exc.code,
                "error_detail": exc.detail,
            },
        }
    artifact_status = str((artifacts.get("artifact") or {}).get("status") or "")
    if existing is not None and existing_artifact.get("status") == "ready":
        if artifact_status == "not_requested":
            # Metadata-only retries do not withdraw previously verified text.
            # Reuse still runs the canonical manifest/hash verifier.
            artifacts = materialize_paper_artifacts(store.root, {
                "pdf_blob_ref": existing.content.get("pdf_blob_ref"),
                "normalized_text_ref": existing.content.get("normalized_text_ref"),
                "artifact": existing_artifact,
            })
        if str((artifacts.get("artifact") or {}).get("status") or "") != "ready":
            raise PaperArtifactError("paper_source_update_blocked",
                                     str((artifacts.get("artifact") or {}).get("error_code") or ""))
    elif existing is not None and existing.status == "blocked" and artifact_status == "not_requested":
        # An empty retry is not evidence that an earlier artifact failure healed.
        return _persist_source(store, existing, snapshot=snapshot)
    normalized["pdf_blob_ref"] = str(artifacts.get("pdf_blob_ref") or "")
    normalized["normalized_text_ref"] = str(artifacts.get("normalized_text_ref") or "")
    metadata = dict(normalized.get("metadata") or {})
    metadata["pdf_blob_ref"] = normalized["pdf_blob_ref"]
    metadata["normalized_text_ref"] = normalized["normalized_text_ref"]
    metadata["artifact"] = dict(artifacts.get("artifact") or {})
    normalized["metadata"] = metadata
    provenance = dict(normalized.get("provenance") or {})
    manifest_ref = str(metadata["artifact"].get("manifest_ref") or "")
    if manifest_ref:
        provenance["artifact_manifest_ref"] = manifest_ref
    provenance["artifact_status"] = str(metadata["artifact"].get("status") or "not_requested")
    if metadata["artifact"].get("error_code"):
        provenance["artifact_error_code"] = str(metadata["artifact"]["error_code"])
    else:
        provenance.pop("artifact_error_code", None)
    normalized["provenance"] = provenance
    paper_source = paper_source_from_payload(normalized)
    record = paper_source.to_record(scope=scope_ref)
    if str(metadata["artifact"].get("status") or "").lower() == "blocked":
        record.status = "blocked"
        record.detail = "Paper source blocked pending canonical artifact evidence"
        record.meta["artifact_status"] = "blocked"
        record.meta["artifact_error_code"] = str(metadata["artifact"].get("error_code") or "")
    if existing is not None:
        updated = RecordEnvelope.from_dict(deepcopy(snapshot))
        for key in ("status", "title", "summary", "detail", "content", "provenance"):
            setattr(updated, key, deepcopy(getattr(record, key)))
        updated.meta = deep_merge_dicts(existing.meta, record.meta)
        for key in ("artifact_status", "artifact_error_code"):
            if key not in record.meta:
                updated.meta.pop(key, None)
        record = updated
    return _persist_source(store, record, snapshot=snapshot)


def _persist_source(store, record: RecordEnvelope, *, snapshot: dict | None) -> RecordEnvelope:
    mutate = getattr(store, "mutate_records_atomically", None)
    if not callable(mutate):
        raise ValueError("paper_source_requires_atomic_store")

    def mutation(sqlite):
        current = sqlite.get_by_id(record.record_id, scope=record.scope, exact_scope=True)
        if (current.to_dict() if current is not None else None) != snapshot:
            raise ValueError("paper_source_changed_retry_required")
        if current is not None and record.to_dict() == snapshot:
            return current, [], []
        if current is not None:
            record.touch()
        _stamp_canonical_hongtu_identity(record)
        sqlite.upsert(record, commit=False)
        return record, [record], []

    return mutate(mutation)
