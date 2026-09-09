"""Bounded numeric recall diagnostics, separate from evidence and authority."""
from __future__ import annotations

from math import isfinite


STAGES = ('authority_probe', 'sqlite', 'index_read', 'embedding_gate',
          'embedding_wait', 'postgres_search', 'index_recheck', 'row_validation')
ADMISSION_DROPS = ('authority_validation_timeout', 'task_evidence_missing',
                   'authority_changed_or_forbidden', 'admission_deadline_exceeded',
                   'fragment_index_unavailable', 'missing_fragment_evidence',
                   'invalid_fragment_evidence', 'requested_attribute_missing',
                   'insufficient_evidence', 'evidence_score_gap', 'same_partition_duplicate',
                   'authority_changed_during_selection', 'candidate_collection_incomplete')
ENGINE_DROPS = ('recall_budget_exhausted', 'candidate_hydration_timeout',
                'candidate_projection_digest_mismatch', 'candidate_scoring_timeout')


def _number(value, *, maximum=1_000_000):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        return 0
    return round(max(0, min(maximum, value)), 3)


def _counts(value, allowed):
    value = value if isinstance(value, dict) else {}
    return {key: int(_number(value[key])) for key in allowed if key in value}


class SourceTrace:
    """Owned by one search; worker completion never writes to this object."""

    def __init__(self, clock):
        self.clock = clock
        self.started = clock()
        self.stages = {}
        self.failed_stage = ''
        self.cache_hit = False

    def call(self, stage, function, *args, **kwargs):
        started = self.clock()
        try:
            return function(*args, **kwargs)
        except Exception:
            if not self.failed_stage:
                self.failed_stage = stage
            raise
        finally:
            self.stages[stage] = self.stages.get(stage, 0) + (self.clock() - started) * 1000

    def payload(self):
        return {
            'elapsed_ms': _number((self.clock() - self.started) * 1000),
            'stages_ms': {key: _number(value) for key, value in self.stages.items() if key in STAGES},
            'failed_stage': self.failed_stage if self.failed_stage in STAGES else '',
            'cache_hit': self.cache_hit,
        }


def summarize_sources(reports):
    stages = {}
    samples = []
    for index, report in enumerate(reports):
        timing = report.get('timing') or {}
        for key, value in (timing.get('stages_ms') or {}).items():
            if key in STAGES:
                stages[key] = stages.get(key, 0) + _number(value)
        # Prefer nontrivial/failed searches; empty alias probes are counted but
        # do not crowd the bounded sample with dozens of sub-millisecond rows.
        if timing.get('failed_stage') or _number(timing.get('elapsed_ms')) >= 10:
            samples.append({'search_index': index,
                'elapsed_ms': _number(timing.get('elapsed_ms')),
                'failed_stage': timing.get('failed_stage') if timing.get('failed_stage') in STAGES else '',
                'cache_hit': timing.get('cache_hit') is True})
    return {
        'source_searches': len(reports),
        'source_budget_exhausted': sum(
            (report.get('postgres') or {}).get('error_code') == 'recall_budget_exhausted'
            for report in reports),
        'source_stages_ms': {key: _number(value) for key, value in stages.items()},
        'source_samples': sorted(samples, key=lambda row: (not bool(row['failed_stage']), -row['elapsed_ms']))[:4],
    }


def compact_recall_diagnostics(explanation):
    engine = explanation.get('engine_diagnostics')
    if not isinstance(engine, dict):
        return {}
    admission = explanation.get('relevance_selector') or {}
    admission = admission if isinstance(admission, dict) else {}
    stages = engine.get('source_stages_ms') or {}
    stages = stages if isinstance(stages, dict) else {}
    result = {
        'elapsed_ms': _number(engine.get('elapsed_ms')),
        'source_searches': int(_number(engine.get('source_searches'))),
        'source_budget_exhausted': int(_number(engine.get('source_budget_exhausted'))),
        'stages_ms': {key: _number(stages[key]) for key in STAGES if key in stages},
        'engine_drops': _counts(engine.get('drops'), ENGINE_DROPS),
        'admission_elapsed_ms': _number(admission.get('elapsed_ms')),
        'admission_drops': _counts(admission.get('dropped_reasons'), ADMISSION_DROPS),
    }
    if admission.get('status') in ('evidence_found', 'no_evidence', 'unavailable'):
        result['admission_status'] = admission['status']
    return result
