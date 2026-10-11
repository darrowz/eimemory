"""Public diagnostic codes for production-query review, never arbitrary text.

These codes describe eligibility/evidence failures, not measured recall scores.
Only exact, producer-owned codes may enter a nightly summary. Unknown strings,
including syntactically plausible exception text, remain redacted.
"""

REASON_CATALOG_VERSION = "production_recall_review_reason_catalog.v1"

CAPTURE_REASONS = frozenset({
    "pending_capture_boundary_invalid",
    "pending_capture_authority_unavailable",
    "pending_capture_decision_missing",
    "maintenance_capture_not_natural",
    "pending_capture_provenance_unknown",
    "pending_capture_source_authority_invalid",
    "pending_capture_item_source_mismatch",
    "pending_capture_decision_mismatch",
    "pending_capture_record_identity_invalid",
})

QUERY_REASONS = frozenset({
    "original_query_input_unavailable",
    "original_query_input_boundary_mismatch",
    "original_query_input_digest_mismatch",
    "original_host_query_digest_mismatch",
    "original_query_input_invalid",
    "query_features_invalid",
    "query_features_not_redacted",
    "query_features_low_signal",
    "query_features_not_loaded",
})

SEMANTIC_REASONS = frozenset({
    "semantic_judgment_unknown", "semantic_judgment_missing",
    "semantic_judgment_not_evaluable", "semantic_judgment_not_delivered", "tool_free_transport_unavailable",
    "completion_unavailable", "malformed_verdict", "unverified_delivery",
    "query_unavailable", "query_unverified", "empty_delivery", "delivery_too_large",
})

APPROVAL_REASONS = frozenset({
    "auto_review_disabled", "auto_review_revoked",
    "auto_review_attestation_key_unavailable", "auto_review_execution_failed",
})

SIGNAL_REASONS = frozenset({
    "independent_signal_agreement_missing", "semantic_off_topic",
    "host_rejected_all_candidates", "host_rejected_candidate",
    "no_candidate_refs", "no_candidate_delivered", "candidate_unavailable",
})

# Native acceptance can fail after assessment, including during idempotent
# creation or label-authority validation. Never admit arbitrary suffixes.
ACCEPTANCE_REASONS = frozenset({
    "accepted_record_invalid", "invalid", "auto_review_disabled",
    "auto_review_packet_fields_invalid", "auto_review_packet_invalid",
    "auto_review_packet_signals_invalid", "auto_review_label_attestation_key_unavailable",
    "auto_review_labels_disabled", "auto_review_label_packet_invalid",
    "auto_review_label_signature_missing", "auto_review_label_signature_invalid",
    "auto_review_label_identity_mismatch", "label_evidence_missing",
    "label_evidence_boundary_invalid", "label_evidence_identity_invalid",
    "label_evidence_packet_invalid",
    "trusted pending production query required", "pending production query schema mismatch",
    "pending query boundary mismatch", "auto_review labels count invalid",
    "auto_review label invalid", "auto_review label boundary mismatch",
}) | CAPTURE_REASONS | QUERY_REASONS

PRODUCTION_REVIEW_REASON_CODES = (
    CAPTURE_REASONS | QUERY_REASONS | SEMANTIC_REASONS | APPROVAL_REASONS | SIGNAL_REASONS
    | frozenset("auto_accept_failed:" + code for code in ACCEPTANCE_REASONS)
)
