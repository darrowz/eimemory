"""One positive-label authority contract, independent of retrieval results."""
from hashlib import sha256
import json

from eimemory.governance.evidence_contract import same_scope


def label_authority_error(evidence, *, scope, source_id, pending_id, record_ref,
                          grade, labeler):
    from .real_query_schema import _secure_dataset_evidence, PRODUCTION_REAL_QUERY_TRUSTED_LABELERS
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
    if (labeler not in (PRODUCTION_REAL_QUERY_TRUSTED_LABELERS | {'delegated_ai'})
            or isinstance(grade, bool) or not isinstance(grade, int) or not 1 <= grade <= 3
            or any(content.get(key) != value for key, value in identity.items())
            or evidence.record_id != ("prdl_" + sha256(json.dumps(content.get('delegated_authority'), ensure_ascii=False,
                sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:32]
                if labeler == 'delegated_ai' else "prle_" + digest[:32])
            or list(evidence.evidence) != [pending_id, record_ref]):
        return "label_evidence_identity_invalid"
    if labeler == 'delegated_ai':
        from .delegated_label_authority import authority_error, packet_evidence_invalid
        packet = content.get('delegation_packet_evidence')
        if (content.get('evidence_class') != 'delegated_ai_relevance_label'
                or evidence.meta.get('authoritative') is not True
                or evidence.meta.get('report_type') != 'production_recall_label_evidence'
                or packet_evidence_invalid(packet)):
            return 'delegated_label_packet_invalid'
        return authority_error(content, scope=scope, source_id=source_id)
    packet = content.get("operator_packet_evidence")
    if (content.get("evidence_class") != "operator_relevance_label"
            or evidence.meta.get("authoritative") is not True
            or evidence.meta.get("report_type") != "production_recall_label_evidence"
            or _secure_dataset_evidence(packet)[1]
            or evidence.meta.get("operator_packet_digest") != packet.get("digest")):
        return "label_evidence_packet_invalid"
    return verify_operator_label(content, scope=scope, source_id=source_id)


# --- Public unified label-authority API (operator + delegated + dataset) ---

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


def sign_delegated_label(body: dict) -> dict:
    """HMAC-sign a delegated AI label authority body (fail-closed without key)."""
    from eimemory.evaluation.delegated_label_authority import sign
    return sign(body)


def verify_delegated_label(content, *, scope, source_id) -> str:
    """Validate delegated AI label HMAC + packet binding."""
    from eimemory.evaluation.delegated_label_authority import authority_error
    return authority_error(content, scope=scope, source_id=source_id)


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
