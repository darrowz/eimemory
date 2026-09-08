"""Live authority manifest for immutable natural-query dataset snapshots.

The file digest proves bytes, not current label validity. Validate again at
activation and evaluation; never reactivate old labels or rewrite old reports.
"""
from dataclasses import asdict
from hashlib import sha256
import json

from eimemory.models.records import ScopeRef
from .label_authority import label_authority_error


def _digest(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                             separators=(",", ":")).encode()).hexdigest()


def validate_case_authority(runtime, case):
    from .production_query_dataset import pending_production_query_capture_validation_error
    scope = ScopeRef.from_dict(case.get("scope") or {})
    source_id = str(case.get("source_id") or "")
    capture = str(case.get("provenance", {}).get("capture_ref") or "")
    if case.get("provenance", {}).get("collector") != "proactive_audit_capture":
        return "natural_capture_contract_required"
    for label in case.get("labels", []):
        evidence = runtime.store.get_by_id(label.get("provenance", {}).get("evidence_ref", ""), scope=scope)
        if evidence is None:
            return "accepted_label_evidence_missing"
        pending_id = str(evidence.content.get("pending_record_id") or "")
        reason = label_authority_error(evidence, scope=scope, source_id=source_id,
            pending_id=pending_id, record_ref=label.get("record_ref"), grade=label.get("grade"),
            labeler=label.get("provenance", {}).get("labeler"))
        if reason:
            return reason
        pending = runtime.store.get_by_id(pending_id, scope=scope)
        if pending is None or pending.content.get("capture_ref") != capture or pending.content.get("case_id") != case.get("case_id"):
            return "accepted_capture_identity_mismatch"
        reason = pending_production_query_capture_validation_error(runtime, pending,
            exact_scope=scope, channel=case.get("channel"))
        if reason:
            return reason
        record = runtime.store.get_by_id(label.get("record_ref", ""), scope=scope)
        if record is None or record.status != "active" or record.source_id != source_id or asdict(record.scope) != asdict(scope):
            return "accepted_candidate_boundary_invalid"
    if not case.get("labels"):
        return "accepted_labels_invalid"
    return ""


def dataset_authority_manifest(runtime, dataset):
    entries = []
    cases = dataset.get("cases")
    if not isinstance(cases, list) or not 1 <= len(cases) <= 1500:
        raise ValueError("dataset_authority_cases_invalid")
    for case in cases:
        reason = validate_case_authority(runtime, case)
        if reason:
            raise ValueError("dataset_authority_stale:" + reason)
        scope = ScopeRef.from_dict(case["scope"])
        refs = []
        for label in case["labels"]:
            evidence = runtime.store.get_by_id(label["provenance"]["evidence_ref"], scope=scope)
            for ref in (label["record_ref"], evidence.record_id, evidence.content["pending_record_id"]):
                record = runtime.store.get_by_id(ref, scope=scope)
                if record is None:
                    raise ValueError("dataset_authority_stale:record_disappeared")
                refs.append({"record_id":ref, "digest":_digest(record.to_dict())})
        case_identity = {key:case.get(key) for key in (
            'case_id','scope','source_id','channel','query_digest','provenance','collection_window')}
        case_identity['labels'] = [{key:label.get(key) for key in ('record_ref','grade','provenance')}
                                   for label in case['labels']]
        entries.append({"case_id":case["case_id"], "case_digest":_digest(case_identity), "scope":asdict(scope),
                        "source_id":case["source_id"], "records":refs})
    manifest = {"schema":"production-dataset-authority.v1", "entries":entries}
    return {**manifest, "digest":_digest(manifest)}
