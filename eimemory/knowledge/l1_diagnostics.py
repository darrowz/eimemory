"""Bounded extraction observations bound to an authoritative L0 source.

This module projects actual extractor and persistence observations. It does not
search for related records, infer a model call from an empty result, or certify
the semantic quality of a memory.
"""
from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
import json
from typing import Any, Iterable

from eimemory.contracts.l1_source_order import l1_source_material_digest
from eimemory.core.record_ids import is_valid_record_id
from eimemory.metadata import business_metadata
from eimemory.models.records import RecordEnvelope


SCHEMA = "l1_extraction_diagnostics.v1"
MAX_REFS = 16
_MAX_COUNT = 1_000_000
_EXTRACTORS = frozenset({"llm", "heuristic", "image_observation"})
_INPUT_REASONS = frozenset({
    "accepted", "empty_user", "input_too_short", "input_oversized",
    "input_cron_wrapper", "input_sensitive", "input_question_only",
    "input_non_memory", "device_identifier",
})
_FILTER_REASONS = frozenset({
    "priority_below_threshold", "content_rejected", "task_scope",
    "instruction_prefix", "persona_prefix",
})
_COMPLETIONS = frozenset({"stored", "no_change", "no_memory", "unavailable", "blocked"})
_FAILURE_REASONS = frozenset({
    "l1_source_order_stale", "l1_source_order_unknown", "l1_parent_changed_during_extraction",
    "l1_conflict_llm_unavailable", "l1_llm_unavailable", "l1_llm_invalid_output",
    "l1_parent_partition_mismatch", "l1_source_binding_invalid",
    "l1_source_parent_unavailable", "l1_source_parent_changed",
    "l1_legacy_source_binding_unknown", "l1_existing_atom_unavailable", "l1_existing_atom_conflict",
    "l1_conflict_target_not_found", "l1_conflict_target_kind_mismatch", "l1_conflict_target_inactive",
    "l1_conflict_action_missing", "l1_conflict_action_invalid", "l1_conflict_llm_invalid",
    "l1_conflict_invalid_output", "l1_conflict_invalid_target_ids", "l1_conflict_invalid_merged_content",
    "l1_conflict_unauthorized_target", "l1_conflict_update_without_target", "l1_conflict_missing_decision",
    "l1_conflict_candidates_incomplete",
})
_ADJUDICATION_COUNTS = (
    "store_count", "skip_count", "update_count", "source_order_stale_count",
)


def _digest(value: Any) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")).hexdigest()


def _count(value: Any) -> int | None:
    return min(value, _MAX_COUNT) if type(value) is int and value >= 0 else None


def _label(value: Any, allowed: frozenset[str] | set[str]) -> str:
    return value if isinstance(value, str) and value in allowed else "unknown"


def _source_binding(parent: RecordEnvelope) -> dict[str, str]:
    return {"record_id": parent.record_id,
            "source_id_digest": _digest(parent.source_id),
            "scope_digest": _digest(asdict(parent.scope)),
            "source_material_digest": l1_source_material_digest(parent)}


def _reference(record: RecordEnvelope, parent: RecordEnvelope, *, child: bool) -> dict[str, str] | None:
    if (not isinstance(record, RecordEnvelope)
            or not isinstance(record.record_id, str) or not is_valid_record_id(record.record_id)
            or record.scope != parent.scope or record.source_id != parent.source_id
            or record.kind != "memory" or record.record_id == parent.record_id):
        return None
    if child:
        has_parent = (parent.record_id in record.evidence
                      or any(link.relation == "derived_from" and link.target_kind in {"memory", "record"}
                             and link.target_id == parent.record_id for link in record.links))
        if not has_parent or business_metadata(record.meta).get("memory_layer") != "l1":
            return None
    return {"record_id": record.record_id, "source_id_digest": _digest(record.source_id),
            "scope_digest": _digest(asdict(record.scope)),
            "payload_digest": _digest(record.to_dict())}


def _references(records: Iterable[RecordEnvelope] | None, parent: RecordEnvelope, *, child: bool) -> dict[str, Any]:
    # The caller supplies this invocation's actual writes/targets. No scan or
    # source inference takes place here. Counts retain omissions explicitly.
    refs: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    if not isinstance(records, (list, tuple)):
        return {"observed_count": None, "inspected_count": None, "unique_count": None,
                "recorded_count": None, "refs": [], "truncated": None, "missing_ref_count": None}
    observed, missing, unique = len(records), 0, 0
    for record in records[:512]:
        ref = _reference(record, parent, child=child)
        if ref is None:
            missing += 1
            continue
        key = (ref["record_id"], ref["source_id_digest"], ref["scope_digest"])
        if key in seen:
            continue
        seen.add(key)
        unique += 1
        if len(refs) < MAX_REFS:
            refs.append(ref)
    return {"observed_count": min(observed, _MAX_COUNT), "inspected_count": min(observed, 512),
            "unique_count": min(unique, _MAX_COUNT) if observed <= 512 else None,
            "recorded_count": len(refs), "refs": refs,
            "truncated": unique > len(refs) or observed > 512, "missing_ref_count": min(missing, _MAX_COUNT)}


def normalize_l1_extraction_diagnostics(
    *, parent: RecordEnvelope, extraction: dict[str, Any] | None = None,
    adjudication: dict[str, Any] | None = None,
    child_records: Iterable[RecordEnvelope] | None = None,
    completion_status: str = "unknown",
    failure_reason: str = "",
) -> dict[str, Any]:
    """Normalize observations; missing fields remain unknown, never inferred."""
    if not isinstance(parent, RecordEnvelope) or not is_valid_record_id(parent.record_id):
        raise ValueError("l1_diagnostic_source_invalid")
    extraction = extraction if isinstance(extraction, dict) else {}
    adjudication = adjudication if isinstance(adjudication, dict) else {}
    raw_filters = extraction.get("filter_reason_counts")
    raw_filters = raw_filters if isinstance(raw_filters, dict) else None
    filters = {key: _count(raw_filters[key]) for key in sorted(_FILTER_REASONS)
               if raw_filters is not None and key in raw_filters and _count(raw_filters[key]) is not None}
    filter_complete = (None if raw_filters is None else
                       len(filters) == len(raw_filters))
    return {"schema": SCHEMA, "measurement_origin": "native_l0_extraction",
            "source_binding": _source_binding(parent),
            "extractor": _label(extraction.get("extractor"), _EXTRACTORS),
            "input_admission": _label(extraction.get("input_admission"), {"accepted", "rejected"}),
            "input_reason": _label(extraction.get("input_reason"), _INPUT_REASONS),
            "model_called": extraction.get("model_called")
                if type(extraction.get("model_called")) is bool else None,
            "raw_candidate_count": _count(extraction.get("raw_candidate_count")),
            "admitted_count": _count(extraction.get("admitted_count")),
            "filter_reason_counts": filters, "filter_counts_complete": filter_complete,
            "adjudication": {**{key: _count(adjudication.get(key)) for key in _ADJUDICATION_COUNTS},
                "skipped_targets": _references(adjudication.get("skipped_target_records"), parent, child=False)},
            "children": _references(child_records, parent, child=True),
            "completion_status": _label(completion_status, _COMPLETIONS),
            "failure_reason": _label(failure_reason, _FAILURE_REASONS) if failure_reason else ""}


def _stored_references(value: Any, parent: RecordEnvelope) -> dict[str, Any]:
    value = value if isinstance(value, dict) else {}
    result = {key: _count(value.get(key)) for key in (
        "observed_count", "inspected_count", "unique_count", "recorded_count", "missing_ref_count")}
    result["truncated"] = value.get("truncated") if type(value.get("truncated")) is bool else None
    refs = value.get("refs")
    refs = refs if isinstance(refs, list) else []
    result["refs"] = []
    binding = _source_binding(parent)
    for ref in refs[:MAX_REFS]:
        if (not isinstance(ref, dict) or not isinstance(ref.get("record_id"), str)
                or not is_valid_record_id(ref["record_id"])
                or ref.get("source_id_digest") != binding["source_id_digest"]
                or ref.get("scope_digest") != binding["scope_digest"]
                or not isinstance(ref.get("payload_digest"), str)
                or len(ref["payload_digest"]) != 64
                or any(char not in "0123456789abcdef" for char in ref["payload_digest"])):
            continue
        result["refs"].append({key: ref[key] for key in (
            "record_id", "source_id_digest", "scope_digest", "payload_digest")})
    return result


def read_l1_extraction_diagnostics(parent: RecordEnvelope) -> dict[str, Any]:
    """Read the persisted native observation, without recreating old linkage.

    A missing old-v2 report deliberately does not reuse l1_extractor or infer
    model_called from completion status or atom counts.
    """
    report = business_metadata(parent.meta).get("l1_extraction_diagnostics")
    status = "not_recorded"
    if isinstance(report, dict) and report.get("schema") == SCHEMA:
        if report.get("source_binding") == _source_binding(parent):
            # Reapply the privacy/type projection. A caller can supply metadata
            # on a source; this observation never grants retrieval authority.
            safe = normalize_l1_extraction_diagnostics(parent=parent, extraction=report,
                adjudication=report.get("adjudication"),
                completion_status=report.get("completion_status"),
                failure_reason=report.get("failure_reason"))
            safe["adjudication"]["skipped_targets"] = _stored_references(
                (report.get("adjudication") or {}).get("skipped_targets")
                if isinstance(report.get("adjudication"), dict) else None, parent)
            safe["children"] = _stored_references(report.get("children"), parent)
            # This is a readback of the original report, not a new measured run.
            safe["measurement_origin"] = "persisted_native_l0_extraction"
            if report.get("filter_counts_complete") is False:
                safe["filter_counts_complete"] = False
            return safe
        status = "source_binding_changed"
    return {"schema": SCHEMA, "status": status, "input_admission": "unknown",
            "extractor": "unknown", "model_called": None,
            "raw_candidate_count": None, "admitted_count": None,
            "filter_reason_counts": {}, "filter_counts_complete": None,
            "completion_status": "unknown", "source_binding": _source_binding(parent)}
