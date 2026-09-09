"""Bounded, text-free retrieval stage audit shared by raw captures and replay."""
import math
import re


def retrieval_stage_diagnostics(explanation):
    def label(value):
        return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,96}', value) else 'unknown'

    def number(value):
        if type(value) in (int, float) and math.isfinite(value):
            return max(0, min(1_000_000, value))
        return 0

    def stage(value):
        if not isinstance(value, dict):
            return {}
        result = {}
        for key in ('name', 'mode', 'status', 'reason', 'error_type', 'error_reason', 'fallback_reason'):
            if value.get(key):
                result[key] = label(value[key])
        for key in ('candidate_count', 'candidate_limit', 'selected_count', 'retrieved_count',
                    'search_limit', 'query_scope_count', 'elapsed_ms', 'calls',
                    'proposed_count', 'delivered_count', 'context_chars'):
            if key in value:
                result[key] = number(value[key])
        for key in ('drops', 'dropped_reasons', 'blocked_counts'):
            if isinstance(value.get(key), dict):
                result[key] = {label(k): number(v) for k, v in list(value[key].items())[:24]}
        if isinstance(value.get('source_names'), list):
            result['source_names'] = [label(x) for x in value['source_names'][:8]]
        return result

    selector = explanation.get('relevance_selector') or {}
    pipeline = explanation.get('pipeline')
    phases = pipeline.get('phases') if isinstance(pipeline, dict) else []
    assistance = selector.get('caller_assistance') if isinstance(selector, dict) else None
    return {'schema':'retrieval_stage_diagnostics.v1',
        'retrieval_status':label(explanation.get('retrieval_status', 'unknown')),
        'engine':stage(explanation.get('engine_diagnostics')),
        'pipeline':[stage(x) for x in phases[:8]] if isinstance(phases, list) else [],
        'online_gate':stage(explanation.get('online_recall_gate')),
        'delivery':stage(explanation.get('delivery_diagnostics')),
        'selector':stage(selector),
        'assistance':(stage(assistance) if assistance else
                      {'status':'not_run','calls':0} if assistance == {} else {'status':'not_reported'})}
