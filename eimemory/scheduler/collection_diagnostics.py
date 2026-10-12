"""Text-free collection cohort and exact same-run review associations."""
import re

from eimemory.evaluation.production_query_review_reasons import PRODUCTION_REVIEW_REASON_CODES

CASE_LIMIT = 50


def _count(value):
    return value if type(value) is int and 0 <= value <= 1_000_000 else None


def _code(value, allowed):
    return value if isinstance(value, str) and value in allowed else None if value is None else 'not_allowlisted'


def _id(value, pattern):
    return value if isinstance(value, str) and re.fullmatch(pattern, value) else None


def production_collection_diagnostics(collection, reviews):
    source = collection if isinstance(collection, dict) else {}
    reviews = reviews if isinstance(reviews, dict) else {}
    raw_ids = source.get('new_pending_record_ids')
    ids = list(dict.fromkeys(value for value in raw_ids[:1500]
                            if _id(value, r'prqp_[0-9a-f]{32}'))) if isinstance(raw_ids, list) else []
    ids = ids[:CASE_LIMIT]
    total = _count(source.get('new_count'))
    upstream = source.get('new_pending_record_ids_truncated')
    truncated = None
    if isinstance(raw_ids, list):
        truncated = (True if upstream is True or len(raw_ids) > CASE_LIMIT
                     or (total is not None and total > len(ids)) else
                     upstream if type(upstream) is bool else False if total == len(ids) else None)
    metadata = {}
    for case in source.get('new_case_refs', [])[:CASE_LIMIT] if isinstance(source.get('new_case_refs'), list) else []:
        if isinstance(case, dict) and case.get('pending_record_id') in ids:
            metadata.setdefault(case['pending_record_id'], []).append(case)
    raw_results = reviews.get('review_results')
    matches = {}
    for result in raw_results[:1500] if isinstance(raw_results, list) else []:
        if isinstance(result, dict) and result.get('pending_record_id') in ids:
            matches.setdefault(result['pending_record_id'], []).append(result)
    cases = []
    for pending in ids:
        meta_rows = metadata.get(pending, [])
        meta = meta_rows[0] if len(meta_rows) == 1 else {}
        rows = matches.get(pending, [])
        row = rows[0] if len(rows) == 1 else {}
        receipt = _id(row.get('review_receipt_id'), r'(?:pret|prar)_[0-9a-f]{32}')
        accepted = _id(row.get('accepted_record_id'), r'prqa_[0-9a-f]{32}')
        status = (_code(row.get('review_verdict'), {'pass', 'fail'}) if row else
                  'ambiguous' if rows else 'not_reported')
        if status == 'pass' and accepted and (row.get('reused_accepted') is True or not receipt):
            status = 'already_accepted'
        semantic = row.get('semantic_quality') if isinstance(row.get('semantic_quality'), dict) else {}
        signals = row.get('signal_counts') if isinstance(row.get('signal_counts'), dict) else {}
        reasons = row.get('reasons')
        cases.append({
            'pending_record_id': pending,
            'capture_ref': _id(meta.get('capture_ref'), r'pd:[0-9a-f]{32}'),
            'candidate_count': (_count(meta.get('candidate_count'))
                                if _count(meta.get('candidate_count')) in range(6) else None),
            'review_status': status, 'review_receipt_id': receipt, 'accepted_record_id': accepted,
            'review_complete': row.get('review_complete') if type(row.get('review_complete')) is bool else None,
            'reasons': sorted({reason if isinstance(reason, str) and reason in PRODUCTION_REVIEW_REASON_CODES
                               else 'reason_not_allowlisted' for reason in reasons[:100]})
                       if isinstance(reasons, list) else None,
            'reasons_truncated': len(reasons) > 100 if isinstance(reasons, list) else None,
            'recall_status': _code(row.get('recall_status'), {'recalled', 'no_recall', 'not_evaluable'}),
            'signal_checks_complete': (row.get('signal_checks_complete')
                                       if type(row.get('signal_checks_complete')) is bool else None),
            'signal_counts': {key: (_count(signals.get(key)) if row.get('signal_checks_complete') is True
                                   or (key == 'candidates' and row.get('candidate_refs_checked') is True) else None)
                              for key in (
                'candidates', 'delivered', 'semantic_relevant', 'verified_proof', 'host_used', 'host_rejected')},
            'semantic_quality': {
                'status': _code(semantic.get('status'), {'evaluated', 'unknown', 'missing', 'not_delivered', 'not_evaluable'}),
                'verdict': _code(semantic.get('verdict'), {'relevant', 'off_topic', 'mixed', 'unknown'}),
            },
        })
    skipped = source.get('skipped')
    skip_counts = {}
    for key, value in list(skipped.items())[:100] if isinstance(skipped, dict) else []:
        key = (key if isinstance(key, str) and key in {
            'non_exact_source', 'query_digest_invalid', 'candidate_boundary_invalid', 'task_type_unclassified'}
            else 'reason_not_allowlisted')
        count = _count(value)
        if count is not None:
            skip_counts[key] = skip_counts.get(key, 0) + count
    resolution = source.get('scope_resolution') if isinstance(source.get('scope_resolution'), dict) else {}
    pins = source.get('retention_pins') if isinstance(source.get('retention_pins'), dict) else {}
    policy = source.get('policy') if isinstance(source.get('policy'), dict) else {}
    return {
        'schema': 'production_collection_diagnostics.v1',
        'status': _code(source.get('status'), {'completed', 'blocked', 'disabled'}) or 'not_reported',
        'enabled': policy.get('enabled') if type(policy.get('enabled')) is bool else None,
        'blocked_reason': ('collection_failed' if isinstance(source.get('blocked_reason'), str)
                           and source['blocked_reason'].startswith('collection_failed:') else
                           _code(source.get('blocked_reason'), {'proactive_audit_store_unavailable', 'collection_failed'})),
        **{key: _count(source.get(key)) for key in ('decision_count', 'new_count', 'existing_count',
            'empty_result_count', 'new_nonempty_count', 'new_empty_count')},
        'skipped_reason_counts': skip_counts if isinstance(skipped, dict) else None,
        'skipped_reasons_truncated': len(skipped) > 100 if isinstance(skipped, dict) else None,
        'new_pending_record_ids': ids if isinstance(raw_ids, list) else None,
        'new_pending_record_ids_truncated': truncated,
        'invalid_new_pending_id_count': (sum(not bool(_id(value, r'prqp_[0-9a-f]{32}')) for value in raw_ids[:1500])
                                         if isinstance(raw_ids, list) else None),
        'review_results_status': 'reported' if isinstance(raw_results, list) else 'not_reported',
        'review_results_truncated': (reviews.get('review_results_truncated') is True or len(raw_results) > 1500
                                     if isinstance(raw_results, list) else None),
        'review_dry_run': reviews.get('dry_run') if type(reviews.get('dry_run')) is bool else None,
        'new_case_reviews': cases,
        'scope_resolution': {
            'rewritten': resolution.get('rewritten') if type(resolution.get('rewritten')) is bool else None,
            'reason': _code(resolution.get('reason'), {'unchanged', 'explicit_user_preserved', 'bindings_unreadable',
                'binding_user_ambiguous', 'binding_user_missing', 'already_logical_user',
                'host_user_replaced_by_bound_logical_user'}),
        },
        'retention_pins': {
            'status': _code(pins.get('status'), {'applied', 'unavailable'}),
            **{lane: {key: _count((pins.get(lane) or {}).get(key)) for key in (
                'requested_count', 'pinned_count', 'decision_missing_count', 'total_pinned',
                'expired_count', 'released_over_cap_count')}
               for lane in ('pending_case', 'accepted_case') if isinstance(pins.get(lane), dict)},
        },
    }
