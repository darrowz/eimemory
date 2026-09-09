"""One positive-label authority contract, independent of retrieval results."""
from hashlib import sha256
import json

from eimemory.governance.evidence_contract import same_scope


def label_authority_error(evidence, *, scope, source_id, pending_id, record_ref,
                          grade, labeler):
    from .real_query_gate import _secure_dataset_evidence, PRODUCTION_REAL_QUERY_TRUSTED_LABELERS
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
        from .delegated_label_authority import authority_error
        packet = content.get('delegation_packet_evidence')
        if (content.get('evidence_class') != 'delegated_ai_relevance_label'
                or evidence.meta.get('authoritative') is not True
                or evidence.meta.get('report_type') != 'production_recall_label_evidence'
                or _secure_dataset_evidence(packet)[1]):
            return 'delegated_label_packet_invalid'
        return authority_error(content, scope=scope, source_id=source_id)
    packet = content.get("operator_packet_evidence")
    if (content.get("evidence_class") != "operator_relevance_label"
            or evidence.meta.get("authoritative") is not True
            or evidence.meta.get("report_type") != "production_recall_label_evidence"
            or _secure_dataset_evidence(packet)[1]
            or evidence.meta.get("operator_packet_digest") != packet.get("digest")):
        return "label_evidence_packet_invalid"
    return ""
