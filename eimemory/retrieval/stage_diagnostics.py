"""Bounded, text-free retrieval stage audit shared by raw captures and replay."""
import math
import re


def retrieval_stage_diagnostics(explanation, *, post_selection=None, trusted_retrieval=None,
                                local_delivery=None):
    """Use caller-observed delivery only, never delivery claims in a bundle."""
    def label(value):
        return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,96}', value) else 'unknown'

    def number(value):
        # Clamp integers before any float conversion, including arbitrarily
        # large JSON counts. These are saturated diagnostics, not exact counts.
        if type(value) is int:
            return max(0, min(1_000_000, value))
        if type(value) is float and math.isfinite(value):
            return max(0, min(1_000_000, value))
        return 0

    def stage(value):
        if not isinstance(value, dict):
            return {}
        result = {}
        for key in ('name', 'mode', 'status', 'reason', 'error_type', 'error_reason', 'fallback_reason',
                    'gateway_stage', 'verification_session_id'):
            if value.get(key):
                result[key] = label(value[key])
        for key in ('input_count', 'candidate_count', 'candidate_limit', 'selected_count', 'retrieved_count',
                    'search_limit', 'query_scope_count', 'elapsed_ms', 'calls',
                    'proposed_count', 'delivered_count', 'context_chars', 'gateway_elapsed_ms'):
            if key in value:
                result[key] = number(value[key])
        for key in ('drops', 'dropped_reasons', 'blocked_counts', 'fallback_error_codes'):
            if isinstance(value.get(key), dict):
                result[key] = {label(k): number(v) for k, v in list(value[key].items())[:24]}
        if isinstance(value.get('source_names'), list):
            result['source_names'] = [label(x) for x in value['source_names'][:8]]
        return result

    selector = explanation.get('relevance_selector') or {}
    pipeline = explanation.get('pipeline')
    phases = pipeline.get('phases') if isinstance(pipeline, dict) else []
    assistance = selector.get('caller_assistance') if isinstance(selector, dict) else None
    result = {'schema':'retrieval_stage_diagnostics.v1',
        'retrieval_status':label(explanation.get('retrieval_status', 'unknown')),
        'engine':stage(explanation.get('engine_diagnostics')),
        'pipeline':[stage(x) for x in phases[:8]] if isinstance(phases, list) else [],
        'online_gate':stage(explanation.get('online_recall_gate')),
        'delivery':stage(local_delivery),
        'selector':stage(selector),
        'assistance':(stage(assistance) if assistance else
                      {'status':'not_run','calls':0} if assistance == {} else {'status':'not_reported'})}
    if isinstance(assistance, dict):
        from .independent_evidence import safe_report
        local = safe_report(assistance.get('local_evidence'))
        if local:
            result['assistance']['local_evidence'] = local
    # Counts describe visibility, never semantic support or a false rejection.
    # Trust comes from the local retrieval path, not fields in a supplied bundle.
    result['verifier_boundary'] = {'status': 'unknown'}
    if trusted_retrieval is True and isinstance(assistance, dict) and assistance:
        boundary = {}
        for key in ('pool_candidate_count', 'candidate_count', 'visible_candidate_count',
                    'visible_window_count', 'candidate_text_chars', 'visible_text_chars',
                    'windowed_candidate_count', 'calls', 'model_selected_count',
                    'answer_requirement_rejections', 'quote_validation_rejections',
                    'accepted_selection_count'):
            if key in assistance:
                value = assistance[key]
                boundary[key] = value if type(value) is int and 0 <= value <= 1_000_000 else 'unknown'
        if 'verifier_reason' in assistance or 'reason' in assistance:
            # Final authority binding may replace reason; keep this stage's verdict.
            reason = assistance.get('verifier_reason', assistance.get('reason'))
            boundary['reason'] = reason if reason in (
                'no_candidates', 'candidate_evidence_empty', 'model_no_selection',
                'reviewed_original_evidence', 'answer_requirements_rejected',
                'caller_model_unavailable', 'assistance_budget_exhausted',
                'caller_verification_failed', 'caller_model_identity_changed',
                'authority_or_deadline_changed',
            ) else 'unknown'
        result['verifier_boundary'] = boundary or {'status': 'unknown'}
    if trusted_retrieval is False:
        # The supplied bundle is still used for delivery, but its upstream
        # diagnostic claims are not observations made by this service.
        result['retrieval_status'] = 'unknown'
        result['pipeline'] = []
        for key in ('engine', 'online_gate', 'selector', 'assistance'):
            result[key] = {'status': 'unknown'}
    # Trusted caller argument only; never accept bundle claims or detail fields.
    result['post_selection'] = {'status': 'unknown'}
    if isinstance(post_selection, dict):
        result['post_selection'] = {}
        for key in (
            'selected_unique_count', 'revalidation_unknown_count',
            'authorization_input_count', 'authorization_filtered_count',
            'voluntary_confidence_input_count', 'voluntary_confidence_filtered_count',
            'session_dedupe_input_count', 'session_deduped_count',
            'item_limit_input_count', 'item_limit_filtered_count',
            'render_input_count', 'render_empty_count',
            'control_input_count', 'control_suppressed_count',
        ):
            if key in post_selection:
                value = post_selection[key]
                result['post_selection'][key] = (
                    value if type(value) is int and 0 <= value <= 1_000_000 else 'unknown')
    return result
