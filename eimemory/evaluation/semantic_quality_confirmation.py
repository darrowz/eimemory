"""Bounded blind confirmation of non-severe delivery quality observations."""
import json
from dataclasses import asdict

from eimemory.models.records import RecordEnvelope

VERSION = 'semantic-quality-confirmation.v1'
SOURCE = 'eimemory.semantic_quality_confirmation'
MODERATE_KINDS = frozenset({'semantic_mixed', 'semantic_redundancy'})


def issue_kinds(report):
    labels = report.get('relevance') or []
    if report.get('reason') != 'evaluated' or report.get('verdict') == 'unknown':
        return set()
    if report.get('verdict') == 'off_topic':
        return {'semantic_off_topic'}
    kinds = set()
    if report.get('verdict') == 'mixed' and 'relevant' in labels and 'unrelated' in labels:
        kinds.add('semantic_mixed')
    if report.get('duplicates') is True and len(labels) >= 2:
        kinds.add('semantic_redundancy')
    return kinds


def quality_gate(kind):
    if kind not in MODERATE_KINDS | {'semantic_off_topic'}:
        raise ValueError('semantic_quality_kind_invalid')
    return {'ok': False, 'blocked_reason': 'confirmed_' + kind,
            'blocking_metrics': {kind: {'actual': 1, 'threshold': 0, 'operator': '=='}}}


def _binding(observation, scope):
    from .semantic_relevance_monitor import _digest
    return dict(version=VERSION, scope=asdict(scope), observation_ref=observation.record_id,
                observation_digest=_digest(observation.content),
                evaluation_identity=observation.content.get('evaluation_identity'))


def valid_confirmation(record, observation, scope):
    from .semantic_relevance_monitor import SOURCE as OBSERVATION_SOURCE, _digest, _parse_result
    if (observation is None or observation.scope != scope or observation.status != 'active'
            or observation.kind != 'evaluation_packet' or observation.source != OBSERVATION_SOURCE
            or observation.source_id != 'default' or not isinstance(observation.content, dict)
            or observation.meta.get('semantic_monitor_digest') != _digest(observation.content)):
        return False
    if (record is None or record.scope != scope or record.status != 'active' or record.source_id != 'default'
            or record.source != SOURCE or record.kind != 'evaluation_packet'
            or record.meta.get('report_type') != VERSION):
        return False
    body, binding = record.content, _binding(observation, scope)
    if (not isinstance(body, dict) or set(body) != set(binding) | {'result'}
            or any(body.get(k) != v for k, v in binding.items())
            or record.meta.get('confirmation_digest') != _digest(body)
            or record.meta.get('confirmation_identity') != _digest(binding)):
        return False
    result = body.get('result')
    if not isinstance(result, dict) or result.get('reason') != 'evaluated':
        return False
    parsed = _parse_result(json.dumps({k: result.get(k) for k in
        ('relevance', 'off_topic', 'duplicates', 'unanswered')}), len(observation.content.get('relevance') or []))
    return result == parsed


def cached_confirmation(runtime, observation, scope):
    from .semantic_relevance_monitor import _digest
    record = runtime.store.latest_record_by_meta_value_exact_scope(
        kind='evaluation_packet', source=SOURCE, status=None, scope=scope,
        meta_key='confirmation_identity', meta_value=_digest(_binding(observation, scope)))
    return record if valid_confirmation(record, observation, scope) else None


def confirm(runtime, *, observation, scope, query, items):
    from . import semantic_relevance_monitor as monitor
    # A separate call cannot see the first verdict. Reverse the delivery order
    # to reduce position anchoring, then restore labels to the original order.
    system = ('Independently review this delivery. No prior review is provided. '
              'Flag duplication only when items repeat the same answer without adding useful evidence.\n'
              + monitor.SYSTEM)
    try:
        raw = monitor._complete_tool_free(system, json.dumps(
            {'query': query, 'items': list(reversed(items))}, ensure_ascii=False))
        result = monitor._parse_result(raw, len(items))
    except Exception:
        result = monitor._parse_result('', len(items))
        result['reason'] = 'completion_unavailable'
    result['relevance'] = list(reversed(result['relevance']))
    binding = _binding(observation, scope)
    body = dict(binding, result=result)
    record = RecordEnvelope.create(kind='evaluation_packet', title='Semantic quality confirmation',
        summary=result['reason'], content=body, scope=scope, source=SOURCE,
        meta={'report_type': VERSION, 'confirmation_identity': monitor._digest(binding),
              'confirmation_digest': monitor._digest(body)})
    runtime.store.append(record)
    return record


def confirmed_kinds(observation, confirmation, scope):
    if not valid_confirmation(confirmation, observation, scope):
        return set()
    first, second = observation.content, confirmation.content['result']
    if first.get('relevance') != second.get('relevance'):
        return set()
    return issue_kinds(first) & issue_kinds(second) & MODERATE_KINDS
