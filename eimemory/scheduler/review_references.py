"""Bounded, ID-only links from review reason counters to native receipts."""
import re

from eimemory.evaluation.production_query_review_reasons import PRODUCTION_REVIEW_REASON_CODES


def review_reason_references(report: dict) -> dict:
    results = report.get('review_results')
    if not isinstance(results, list):
        return {'schema': 'production_review_reason_refs.v1', 'status': 'not_reported',
                'results_scanned_count': None, 'results_truncated': None,
                'result_reasons_truncated': None, 'reason_refs': {}}
    buckets = {}
    seen = {}
    reasons_truncated = False
    for result in results[:1500]:
        if not isinstance(result, dict) or result.get('review_verdict') != 'fail':
            continue
        reasons = result.get('reasons')
        if not isinstance(reasons, list):
            continue
        reasons_truncated |= len(reasons) > 100
        codes = {reason if isinstance(reason, str) and reason in PRODUCTION_REVIEW_REASON_CODES
                 else 'reason_not_allowlisted' for reason in reasons[:100]}
        pending, receipt = result.get('pending_record_id'), result.get('review_receipt_id')
        valid = (isinstance(pending, str) and re.fullmatch(r'prqp_[0-9a-f]{32}', pending)
                 and isinstance(receipt, str) and re.fullmatch(r'(?:pret|prar)_[0-9a-f]{32}', receipt))
        for code in sorted(codes):
            bucket = buckets.setdefault(code, {'observed_result_count': 0, 'missing_reference_count': 0,
                                               'reference_count': 0, 'references_truncated': False, 'refs': []})
            bucket['observed_result_count'] += 1
            if not valid:
                bucket['missing_reference_count'] += 1
                continue
            identity = (pending, receipt)
            identities = seen.setdefault(code, set())
            if identity in identities:
                continue
            identities.add(identity)
            if len(bucket['refs']) < 5:
                bucket['refs'].append({'pending_record_id': pending, 'review_receipt_id': receipt})
                bucket['reference_count'] += 1
            else:
                bucket['references_truncated'] = True
    return {'schema': 'production_review_reason_refs.v1', 'status': 'reported',
            'results_scanned_count': min(len(results), 1500), 'results_truncated': len(results) > 1500,
            'result_reasons_truncated': reasons_truncated,
            'reason_refs': dict(sorted(buckets.items()))}
