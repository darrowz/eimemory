"""Local evidence selection, separate from RRF order and neural reranking.

Cosine/lexical signals are measurements, not answer probabilities. Thresholds
must be calibrated on development cases and accepted on an untouched holdout.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math
import os
from time import perf_counter

from eimemory.models.identity_aliases import normalize_identity_text
from .evidence_fragments import POLICY, TOKENIZER, evidence_fragments, lexical_coverage, search_terms
from .postgres_vector import candidate_record_keyword_text
from .answer_requirements import requested_attribute, supports_requested_attribute


@dataclass(frozen=True)
class LightweightConfig:
    enabled: bool = False
    min_cosine: float = 0.65
    min_coverage: float = 0.08
    lexical_weight: float = 0.10
    max_score_gap: float = 0.05
    max_candidates: int = 48
    calibration: str = "unvalidated"

    def __post_init__(self):
        if any(not math.isfinite(v) or not 0 <= v <= 1 for v in
               (self.min_cosine, self.min_coverage, self.lexical_weight, self.max_score_gap)):
            raise ValueError('lightweight_admission_bounds')
        if not 4 <= self.max_candidates <= 96:
            raise ValueError('lightweight_candidate_bounds')

    @classmethod
    def from_env(cls):
        if os.environ.get('EIMEMORY_LIGHTWEIGHT_ADMISSION_ENABLED', '0') != '1':
            return cls()
        return cls(enabled=True,
            min_cosine=float(os.environ.get('EIMEMORY_LIGHTWEIGHT_MIN_COSINE', '.65')),
            min_coverage=float(os.environ.get('EIMEMORY_LIGHTWEIGHT_MIN_COVERAGE', '.08')),
            lexical_weight=float(os.environ.get('EIMEMORY_LIGHTWEIGHT_LEXICAL_WEIGHT', '.10')),
            max_score_gap=float(os.environ.get('EIMEMORY_LIGHTWEIGHT_MAX_SCORE_GAP', '.05')),
            calibration=os.environ.get('EIMEMORY_LIGHTWEIGHT_CALIBRATION', 'unvalidated'))

    def identity(self):
        from .caller_assistance import identity as assistance_identity
        return {**asdict(self), 'policy': 'lightweight-evidence-admission.v2',
                'caller_assistance': assistance_identity(),
                'projection': POLICY, 'tokenizer': TOKENIZER,
                'score_kind': 'cosine_plus_lexical_coverage_not_probability'}


class LightweightAdmission:
    def __init__(self, config: LightweightConfig):
        self.config = config

    def select(self, items, *, query, limit, validate, deadline_at=0.0,
               hints_for=lambda _: {}, backend_available=False):
        started = perf_counter()
        attribute = requested_attribute(query)
        dropped, scored, assistance_candidates = {}, [], []
        assistance = {}
        def drop(reason):
            dropped[reason] = dropped.get(reason, 0) + 1
        def expired():
            return bool(deadline_at and perf_counter() >= deadline_at)
        valid = []
        for item in items:
            if expired():
                break
            if validate(item):
                valid.append(item)
        if len(valid) < len(items):
            dropped['authority_changed_or_forbidden'] = len(items) - len(valid)
        normalized = normalize_identity_text(query)
        exact = [item for item in valid if normalized and normalized in
                 {normalize_identity_text(item.title), normalize_identity_text(item.record_id)}]
        chosen = []
        mode, status = 'lightweight_evidence', 'no_evidence'
        if exact:
            chosen, mode = exact[:max(0, limit)], 'identity_lookup'
        elif not backend_available:
            status = 'unavailable'
            drop('fragment_index_unavailable')
        elif deadline_at and perf_counter() >= deadline_at:
            status = 'unavailable'
            drop('admission_deadline_exceeded')
        else:
            # Reserve space for each actual retrieval arm before scoring; fused
            # order alone must not exclude a top semantic-only candidate.
            dense = sorted(valid, key=lambda r: -float(hints_for(r).get('dense_vector_score') or 0))
            lexical = sorted(valid, key=lambda r: -float(hints_for(r).get('fragment_fts_score') or 0))
            pool, seen = [], set()
            for index in range(len(valid)):
                if expired():
                    break
                for arm in (dense, lexical, valid):
                    item = arm[index]
                    key = (tuple(asdict(item.scope).values()), item.source_id, item.record_id)
                    if key not in seen and len(pool) < self.config.max_candidates:
                        pool.append(item)
                        seen.add(key)
            ranked = []
            for item in pool:
                if expired():
                    break
                hints = hints_for(item)
                fragment_id = hints.get('evidence_fragment_id')
                if (hints.get('fragment_policy') != POLICY or not fragment_id
                        or 'dense_vector_score' not in hints):
                    drop('missing_fragment_evidence')
                    continue
                text = candidate_record_keyword_text(item,
                    max_text_chars=int(hints.get('_candidate_projection_text_chars') or 16000))
                fragment = next((f for f in evidence_fragments(text) if f['id'] == fragment_id), None)
                cosine = float(hints.get('dense_vector_score') or 0)
                if fragment is None or not math.isfinite(cosine) or not 0 <= cosine <= 1:
                    drop('invalid_fragment_evidence')
                    continue
                coverage = lexical_coverage(query, fragment['text'])
                score = cosine + self.config.lexical_weight * coverage
                attribute_supported = supports_requested_attribute(attribute, fragment['text'])
                if attribute_supported:
                    assistance_candidates.append((score, item, fragment['text']))
                admitted = (attribute_supported and cosine >= self.config.min_cosine
                            and coverage >= self.config.min_coverage)
                scored.append({'record_id': item.record_id, 'source_id': item.source_id,
                    'fragment_id': fragment_id, 'span_start': fragment['start'], 'span_end': fragment['end'],
                    'cosine': cosine, 'coverage': coverage, 'score': score, 'admitted': admitted,
                    'requested_attribute_supported': attribute_supported})
                if admitted:
                    ranked.append((score, item, fragment['text']))
                else:
                    drop('requested_attribute_missing' if not attribute_supported else 'insufficient_evidence')
            ranked.sort(key=lambda row: (-row[0], row[1].record_id))
            top = ranked[0][0] if ranked else 0
            representatives = []
            for score, item, text in ranked:
                if expired():
                    break
                if top - score > self.config.max_score_gap:
                    drop('evidence_score_gap')
                    continue
                partition = (tuple(asdict(item.scope).values()), item.source_id)
                terms = set(search_terms(text))
                if any(partition == other_partition and terms and
                       len(terms & other) / max(1, len(terms | other)) >= .90
                       for other_partition, other in representatives):
                    drop('same_partition_duplicate')
                    continue
                representatives.append((partition, terms))
                chosen.append(item)
                if len(chosen) >= max(0, limit):
                    break
            from .caller_assistance import needs_verification, verify_candidates
            if limit > 0 and not expired() and needs_verification(query, chosen):
                assistance_candidates.sort(key=lambda row: (-row[0], row[1].record_id))
                chosen, assistance = verify_candidates(query=query,
                    candidates=[(item, text) for _score, item, text in assistance_candidates[:8]],
                    limit=limit, deadline_at=deadline_at)
                status = assistance['status']
        selected = []
        for item in chosen if limit > 0 else []:
            if expired():
                break
            if validate(item):
                selected.append(item)
        if chosen and len(selected) != len(chosen):
            drop('authority_changed_during_selection')
            status = 'unavailable'
            selected = []
        elif selected:
            status = 'evidence_found'
        if expired():
            drop('admission_deadline_exceeded')
            selected, status = [], 'unavailable'
        return selected, {**self.config.identity(), 'mode': mode, 'status': status,
            'requested_attribute': attribute,
            'caller_assistance': assistance,
            'candidate_count': len(valid), 'selected_count': len(selected), 'scored': scored,
            'dropped_reasons': dropped, 'elapsed_ms': round((perf_counter() - started) * 1000, 3)}
