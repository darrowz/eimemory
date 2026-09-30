"""One positive-label authority contract, independent of retrieval results."""
from hashlib import sha256
import json

from eimemory.governance.evidence_contract import same_scope


def label_authority_error(evidence, *, scope, source_id, pending_id, record_ref,
                          grade, labeler):
    from .real_query_schema import (
        _secure_dataset_evidence,
        PRODUCTION_REAL_QUERY_AUTO_REVIEW_LABELER,
        production_real_query_trusted_labelers,
    )
    if evidence is None:
        return "label_evidence_missing"
    if (evidence.status != "active" or evidence.kind != "evaluation_packet"
            or evidence.source != "eimemory.production_recall.label_evidence"
            or evidence.source_id != source_id or not same_scope(evidence.scope, scope)):
        return "label_evidence_boundary_invalid"
    content = evidence.content
    identity = dict(pending_record_id=pending_id, record_ref=record_ref, grade=grade, labeler=labeler)
    digest = sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":")).encode()).hexdigest()
    if (labeler not in production_real_query_trusted_labelers()
            or isinstance(grade, bool) or not isinstance(grade, int) or not 1 <= grade <= 3
            or any(content.get(key) != value for key, value in identity.items())
            or evidence.record_id != "prle_" + digest[:32]
            or list(evidence.evidence) != [pending_id, record_ref]):
        return "label_evidence_identity_invalid"
    if labeler == PRODUCTION_REAL_QUERY_AUTO_REVIEW_LABELER:
        return _auto_review_label_error(evidence, scope=scope, source_id=source_id)
    packet = content.get("operator_packet_evidence")
    if (content.get("evidence_class") != "operator_relevance_label"
            or evidence.meta.get("authoritative") is not True
            or evidence.meta.get("report_type") != "production_recall_label_evidence"
            or _secure_dataset_evidence(packet)[1]
            or evidence.meta.get("operator_packet_digest") != packet.get("digest")):
        return "label_evidence_packet_invalid"
    return verify_operator_label(content, scope=scope, source_id=source_id)


# --- Public unified label-authority API (operator + dataset) ---

def verify_label_authority(evidence, *, scope, source_id, pending_id, record_ref,
                           grade, labeler) -> str:
    """Single public entry: empty string when authoritative, else a stable reason code."""
    return label_authority_error(
        evidence,
        scope=scope,
        source_id=source_id,
        pending_id=pending_id,
        record_ref=record_ref,
        grade=grade,
        labeler=labeler,
    )


def verify_case_authority(runtime, case) -> str:
    """Dataset-case authority (collector → label → pending → candidate)."""
    from eimemory.evaluation.dataset_authority import validate_case_authority
    return validate_case_authority(runtime, case)


def verify_dataset_authority(runtime, dataset) -> dict:
    """Build a live authority manifest; raises ValueError when stale."""
    from eimemory.evaluation.dataset_authority import dataset_authority_manifest
    return dataset_authority_manifest(runtime, dataset)


OPERATOR_LABEL_SCHEMA = "production_recall_operator_label.v1"


def sign_operator_label(body: dict) -> dict:
    """HMAC-sign an operator label authority body using receipt key infra.

    Fail-closed when the evidence receipt keyring is unavailable — never invent
    or soft-default a signing secret.
    """
    from hashlib import sha256
    import hmac
    from eimemory.evaluation.real_query_schema import _stable_digest
    from eimemory.governance.tool_receipts import receipt_key_set

    keys = receipt_key_set()
    if keys is None:
        raise ValueError("operator_label_attestation_key_unavailable")
    payload = {**dict(body or {}), "schema": OPERATOR_LABEL_SCHEMA, "key_id": keys.active_id}
    payload.pop("signature", None)
    signature = hmac.new(
        keys.active_key.encode(),
        (OPERATOR_LABEL_SCHEMA + ":" + _stable_digest(payload)).encode(),
        sha256,
    ).hexdigest()
    return {**payload, "signature": signature}


def verify_operator_label(content, *, scope, source_id) -> str:
    """Validate operator label HMAC when present; fail-closed if missing/invalid."""
    from hashlib import sha256
    import hmac
    from dataclasses import asdict
    from eimemory.evaluation.real_query_schema import _stable_digest
    from eimemory.governance.tool_receipts import receipt_key_set

    body = dict(content.get("operator_authority") or {})
    if not body:
        return "operator_label_signature_missing"
    signature = body.pop("signature", "")
    keys = receipt_key_set()
    key = keys.verification_keys.get(body.get("key_id"), "") if keys else ""
    if not key or not isinstance(signature, str) or not hmac.compare_digest(
        signature,
        hmac.new(
            key.encode(),
            (OPERATOR_LABEL_SCHEMA + ":" + _stable_digest(body)).encode(),
            sha256,
        ).hexdigest(),
    ):
        return "operator_label_signature_invalid"
    if body.get("schema") != OPERATOR_LABEL_SCHEMA:
        return "operator_label_schema_invalid"
    packet = content.get("operator_packet_evidence") if isinstance(content.get("operator_packet_evidence"), dict) else {}
    if (
        body.get("scope") is not None
        and body.get("scope") != asdict(scope)
    ):
        return "operator_label_scope_mismatch"
    if source_id and body.get("source_id") not in (None, source_id):
        return "operator_label_source_mismatch"
    if body.get("label") not in (
        None,
        {k: content.get(k) for k in ("pending_record_id", "record_ref", "grade", "labeler")},
    ):
        return "operator_label_identity_mismatch"
    if body.get("operator_packet_evidence") not in (None, packet):
        return "operator_label_packet_mismatch"
    return ""


# --- Automated review label authority (distinct from operator labels) ---

AUTO_REVIEW_LABEL_SCHEMA = "production_recall_auto_review_label.v1"
AUTO_REVIEW_PACKET_SCHEMA = "production_recall_auto_review_packet.v1"
AUTO_REVIEW_EVIDENCE_CLASS = "auto_review_relevance_label"
AUTO_REVIEW_CRITERIA_VERSIONS = frozenset({"production-recall-auto-review.v1"})
_AUTO_REVIEW_PACKET_FIELDS = frozenset({
    "schema", "criteria_version", "reviewer", "pending_record_id", "inputs_digest",
    "signals", "query_features_origin", "reviewed_at",
})


def auto_review_packet_error(packet, *, pending_id="") -> str:
    """Shape check for the deterministic auto-review packet (no secrets, no text)."""
    import re
    if not isinstance(packet, dict) or set(packet) != _AUTO_REVIEW_PACKET_FIELDS:
        return "auto_review_packet_fields_invalid"
    if (packet.get("schema") != AUTO_REVIEW_PACKET_SCHEMA
            or packet.get("criteria_version") not in AUTO_REVIEW_CRITERIA_VERSIONS
            or packet.get("reviewer") != "eimemory.auto_review"
            or not isinstance(packet.get("pending_record_id"), str)
            or (pending_id and packet.get("pending_record_id") != pending_id)
            or re.fullmatch(r"[0-9a-f]{64}", str(packet.get("inputs_digest") or "")) is None
            or not isinstance(packet.get("signals"), dict)
            or not 1 <= len(packet["signals"]) <= 5
            or not isinstance(packet.get("query_features_origin"), str)
            or not isinstance(packet.get("reviewed_at"), str)):
        return "auto_review_packet_invalid"
    for ref, signal in packet["signals"].items():
        if (not isinstance(ref, str) or not isinstance(signal, dict)
                or set(signal) != {"semantic_relevant", "verified_proof", "host_used"}
                or signal.get("semantic_relevant") is not True
                or type(signal.get("verified_proof")) is not bool
                or type(signal.get("host_used")) is not bool
                or not (signal["verified_proof"] or signal["host_used"])):
            return "auto_review_packet_signals_invalid"
    return ""


def sign_auto_review_label(body: dict) -> dict:
    """HMAC-sign an auto-review label body; fail closed without the receipt keyring."""
    from hashlib import sha256
    import hmac
    from eimemory.evaluation.real_query_schema import _stable_digest
    from eimemory.governance.tool_receipts import receipt_key_set

    keys = receipt_key_set()
    if keys is None:
        raise ValueError("auto_review_label_attestation_key_unavailable")
    payload = {**dict(body or {}), "schema": AUTO_REVIEW_LABEL_SCHEMA, "key_id": keys.active_id}
    payload.pop("signature", None)
    signature = hmac.new(
        keys.active_key.encode(),
        (AUTO_REVIEW_LABEL_SCHEMA + ":" + _stable_digest(payload)).encode(),
        sha256,
    ).hexdigest()
    return {**payload, "signature": signature}


def _auto_review_label_error(evidence, *, scope, source_id) -> str:
    from hashlib import sha256
    import hmac
    from dataclasses import asdict
    from eimemory.evaluation.real_query_schema import (
        _stable_digest, production_recall_auto_review_enabled,
    )
    from eimemory.governance.tool_receipts import receipt_key_set

    if not production_recall_auto_review_enabled():
        return "auto_review_labels_disabled"
    content = evidence.content if isinstance(evidence.content, dict) else {}
    packet = content.get("auto_review_packet")
    if (content.get("evidence_class") != AUTO_REVIEW_EVIDENCE_CLASS
            or evidence.meta.get("authoritative") is not True
            or evidence.meta.get("report_type") != "production_recall_label_evidence"
            or evidence.meta.get("label_authority") != "auto_review"
            or "operator_packet_evidence" in content
            or auto_review_packet_error(packet, pending_id=str(content.get("pending_record_id") or ""))
            or evidence.meta.get("auto_review_packet_digest") != _stable_digest(packet)
            or str(content.get("record_ref") or "") not in packet["signals"]):
        return "auto_review_label_packet_invalid"
    body = dict(content.get("auto_review_authority") or {})
    if not body:
        return "auto_review_label_signature_missing"
    signature = body.pop("signature", "")
    keys = receipt_key_set()
    key = keys.verification_keys.get(body.get("key_id"), "") if keys else ""
    if not key or not isinstance(signature, str) or not hmac.compare_digest(
        signature,
        hmac.new(key.encode(), (AUTO_REVIEW_LABEL_SCHEMA + ":" + _stable_digest(body)).encode(),
                 sha256).hexdigest(),
    ):
        return "auto_review_label_signature_invalid"
    if (body.get("schema") != AUTO_REVIEW_LABEL_SCHEMA
            or body.get("scope") != asdict(scope)
            or body.get("source_id") != source_id
            or body.get("label") != {k: content.get(k) for k in (
                "pending_record_id", "record_ref", "grade", "labeler")}
            or body.get("auto_review_packet") != packet):
        return "auto_review_label_identity_mismatch"
    return ""
