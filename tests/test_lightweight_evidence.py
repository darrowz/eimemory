from dataclasses import replace

import pytest

from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.retrieval.evidence_fragments import (
    POLICY, evidence_fragments, fts_document, fts_query, search_terms,
)
from eimemory.retrieval.lightweight_admission import LightweightAdmission, LightweightConfig
from eimemory.retrieval.postgres_vector import candidate_record_keyword_text, PostgresVectorConfig, projection_fingerprint
from eimemory.retrieval.postgres_ddl import build_candidate_projection_ddl


def record(text, user='owner'):
    return RecordEnvelope.create(kind='memory', title='User preference', summary=text,
        detail=text, content={'text': text}, scope=ScopeRef(user_id=user))


def hints(item, score=.8):
    fragments = evidence_fragments(candidate_record_keyword_text(item, max_text_chars=16000))
    fragment = next(f for f in fragments if 'article' in f['text'])
    return {'dense_vector_score': score, 'fragment_policy': POLICY,
            'evidence_fragment_id': fragment['id']}


def test_fragments_are_original_spans_and_keep_middle_and_negation():
    text = '前言' * 310 + '。供电采用专线与市场补电；不是双直连。另一个话题。'
    spans = evidence_fragments(text)
    assert all(f['text'] == text[f['start']:f['end']] for f in spans)
    assert any('供电采用专线与市场补电' in f['text'] for f in spans)
    assert any('不是双直连' in f['text'] for f in spans)
    assert spans == evidence_fragments(text)
    assert evidence_fragments(text + '修改。')[-1]['id'] != spans[-1]['id']


def test_many_short_lines_do_not_drop_tail():
    text = '\n'.join(str(i) for i in range(1000))
    assert '999' in evidence_fragments(text)[-1]['text']


def test_repeated_original_body_not_repeated_in_index():
    assert len(evidence_fragments('Read article.\nRead article.\n')) == 1


def test_chinese_document_query_use_same_safe_terms():
    assert '微信' in search_terms('我发微信文章')
    assert '微信' in fts_document('请阅读微信文章正文')
    assert "'微信'" in fts_query('我发微信文章')
    assert fts_query("a' | !:* 微信") == "'a' | '微信'"


def test_fragment_policy_invalidates_index_fingerprint_and_cascades_delete():
    config = PostgresVectorConfig(vector_dimension=3)
    assert projection_fingerprint(config) != projection_fingerprint(replace(config, evidence_fragments=True))
    ddl = '\n'.join(build_candidate_projection_ddl(config))
    assert 'ON DELETE CASCADE' in ddl and 'span_start' in ddl and 'fragment_id' in ddl


def test_admission_uses_original_span_not_rrf_or_hash_and_does_not_pad():
    good, bad = record('Read the article in full.'), record('Different article location.')
    gate = LightweightAdmission(LightweightConfig(enabled=True))
    evidence = {good.record_id: hints(good), bad.record_id: {**hints(bad, .1), 'vector_score': .99}}
    chosen, report = gate.select([bad, good], query='Read article', limit=5, validate=lambda _: True,
        hints_for=lambda r: evidence[r.record_id], backend_available=True)
    assert chosen == [good]
    assert report['status'] == 'evidence_found' and report['scored']


def test_missing_or_changed_fragment_fails_closed():
    item = record('Read article.')
    gate = LightweightAdmission(LightweightConfig(enabled=True))
    chosen, report = gate.select([item], query='article', limit=5, validate=lambda _: True,
        hints_for=lambda _: {**hints(item), 'evidence_fragment_id': 'f' * 64}, backend_available=True)
    assert not chosen and report['dropped_reasons']['invalid_fragment_evidence'] == 1


def test_service_failure_is_not_valid_negative():
    chosen, report = LightweightAdmission(LightweightConfig(enabled=True)).select([], query='article',
        limit=5, validate=lambda _: True, backend_available=False)
    assert not chosen and report['status'] == 'unavailable'


def test_dedup_respects_scope_and_second_authority_check():
    first, second = record('Read article.'), record('Read article.')
    third = record('Read article.', user='other')
    gate = LightweightAdmission(LightweightConfig(enabled=True))
    chosen, _ = gate.select([first, second, third], query='article', limit=5, validate=lambda _: True,
        hints_for=hints, backend_available=True)
    assert len(chosen) == 2 and third in chosen
    checks = 0
    def changed(_):
        nonlocal checks
        checks += 1
        return checks == 1
    chosen, report = gate.select([first], query='article', limit=5, validate=changed,
        hints_for=hints, backend_available=True)
    assert not chosen and report['status'] == 'unavailable'


def test_invalid_config_rejected():
    with pytest.raises(ValueError):
        LightweightConfig(min_cosine=float('nan'))


def test_dense_leader_keeps_reserved_slot_even_when_deep_sqlite_duplicate():
    from eimemory.retrieval.contracts import CandidateHit, CandidateRef, ExactScope
    from eimemory.retrieval.postgres_vector import _merge_hits
    scope = ExactScope.from_scope(ScopeRef(user_id='owner'))
    sqlite = [CandidateHit(CandidateRef(str(i), scope, 'default'), i + 1, .1,
                          component_hints={'vector_score': .1}) for i in range(12)]
    dense = CandidateHit(sqlite[-1].ref, 1, .9,
                         component_hints={'dense_vector_score': .9, 'vector_score': .9})
    merged = _merge_hits(sqlite, [dense], limit=4)
    assert any(hit.ref == dense.ref for hit in merged)
    assert len(merged) == 4 and len({hit.ref for hit in merged}) == 4
