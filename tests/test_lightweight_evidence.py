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


def test_deadline_exhausted_during_authority_check_cannot_return_identity_hit(monkeypatch):
    from eimemory.retrieval import lightweight_admission as module
    clock = [1.0]
    monkeypatch.setattr(module, 'perf_counter', lambda: clock[0])
    item = record('Read article.')
    def validate(_):
        clock[0] = 4.0
        return True
    selected, report = LightweightAdmission(LightweightConfig(enabled=True)).select(
        [item], query=item.title, limit=1, validate=validate, deadline_at=3.0)
    assert not selected and report['status'] == 'unavailable'
    assert 'admission_deadline_exceeded' in report['dropped_reasons']


def test_missing_dense_evidence_cannot_be_admitted_even_with_diagnostic_zero_threshold():
    item = record('Read article.')
    evidence = hints(item)
    del evidence['dense_vector_score']
    gate = LightweightAdmission(LightweightConfig(enabled=True, min_cosine=0, min_coverage=0))
    selected, report = gate.select([item], query='article', limit=5, validate=lambda _: True,
        hints_for=lambda _: evidence, backend_available=True)
    assert not selected and report['dropped_reasons']['missing_fragment_evidence'] == 1


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


@pytest.mark.parametrize('query,evidence,expected', [
    ('这台风扇多少钱？', '风扇转速是2400转每分钟。', False),
    ('汽车的购买价格是多少？', '汽车必须经车主同意才能借出。', False),
    ('培训费用是多少？', '培训费用为1200元，包含教材。', True),
    ('服务多少钱？', '基础服务免费。', True),
    ('How much did the adapter cost?', 'The adapter cost $12.50.', True),
    ('这项服务价格是多少？', '每次十二元。', True),
    ('这项服务价格是多少？', '价格约20，币种未说明。', True),
])
def test_requested_attribute_is_not_entity_similarity(query, evidence, expected):
    from eimemory.retrieval.answer_requirements import requested_attribute, supports_requested_attribute
    assert requested_attribute(query) == 'money'
    assert supports_requested_attribute('money', evidence) is expected


def test_price_comparison_rule_is_not_a_request_for_a_price_amount():
    from eimemory.retrieval.answer_requirements import requested_attribute
    assert requested_attribute('是否允许自动切换到更贵的模型？') == ''


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


def test_bounded_merge_keeps_authority_contract_before_rich_diagnostics():
    from eimemory.retrieval.contracts import CandidateHit, CandidateRef, ExactScope
    from eimemory.retrieval.postgres_vector import _merge_hits
    scope = ExactScope.from_scope(ScopeRef(user_id='owner'))
    ref = CandidateRef('memory', scope, 'default')
    sqlite = CandidateHit(ref, 1, .1, component_hints={
        'vector_score': .1, **{f'diagnostic_{n}': n for n in range(31)}})
    required = {'_candidate_projection_digest': 'a' * 64,
        '_candidate_projection_digest_schema': 'candidate-projection.v1',
        '_candidate_projection_text_chars': 16000,
        '_candidate_authoritative_updated_at': '2026-09-08T00:00:00.000000Z',
        'dense_vector_score': .9, 'vector_score': .9,
        'evidence_fragment_id': 'b' * 64, 'fragment_policy': POLICY}
    postgres = CandidateHit(ref, 1, .9, component_hints=required)
    combined = _merge_hits([sqlite], [postgres], limit=4)[0].component_dict()
    assert len(combined) <= 32
    assert combined['_candidate_sqlite_authority_duplicate'] is True
    assert all(combined.get(key) == value for key, value in required.items())
