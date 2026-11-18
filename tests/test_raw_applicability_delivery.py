"""Independent new scenarios through persistent RAW, RPC and delivery paths.

These are source-contract probes, not claims about final model answers.
"""
from datetime import datetime, timezone

import pytest

from eimemory.raw.retrieval import search_raw_chunks
from test_runtime_raw_citation_delivery import (
    runtime, add_raw, call, owner, persist_mutation, configure_proactive,
    proactive, persona, final_context,
)


def search(runtime, query, *, fact):
    context = {'exact_scope_only': True, 'rerank_with_llm': False}
    if fact:
        context['evidence_intent'] = 'fact_citation'
    return search_raw_chunks(runtime.store, query=query, scope=owner(), task_context=context, limit=5)


@pytest.mark.parametrize('version', ['6.2', '11.4'])
def test_unseen_version_mismatch_is_removed_only_from_fact_search(runtime, version):
    query = f'Where are Vesper version {version} manifests stored?'
    good = add_raw(runtime, f'Applies to versions: {version}\nVesper manifests are stored in depot-green.')
    bad = add_raw(runtime, 'Applies to versions: 91.3\nVesper manifests are stored in depot-red.')
    assert {row['record']['record_id'] for row in search(runtime, query, fact=False)} == {good.record_id, bad.record_id}
    assert [row['record']['record_id'] for row in search(runtime, query, fact=True)] == [good.record_id]
    lane = call(runtime, query=query)['bundle']['raw_citation']
    assert [row['ref'][0] for row in lane['evidence']] == [good.record_id]
    assert lane['decision']['status'] == lane['admission']['status'] == 'related'


def test_unknown_applicability_remains_searchable_but_is_not_quoted(runtime):
    query = 'Where are Vesper version 6.2 manifests stored?'
    add_raw(runtime, 'Vesper manifests are stored in depot-unknown.')
    rows = search(runtime, query, fact=True)
    assert rows and rows[0]['applicability']['status'] == 'unknown'
    result = call(runtime, query=query)
    assert not result['bundle']['raw_citation']['evidence']
    assert result['bundle']['raw_citation']['admission']['reason'] == 'applicability_unavailable'
    assert 'depot-unknown' not in result['context']


def test_version_comparison_quote_does_not_include_other_release(runtime):
    query = '流光SDK 6.2应该采用什么接口？'
    source = '流光SDK 6.x采用callback接口；从7.0开始采用async接口。'
    add_raw(runtime, source)
    result = call(runtime, query=query)
    evidence = result['bundle']['raw_citation']['evidence']
    assert evidence
    for row in evidence:
        assert source[slice(*row['span'])] == row['text']
        assert 'async' not in row['text']


def test_partial_version_range_keeps_qualified_material_without_upgrading_support(runtime):
    query = '流光还没升到2.0，用户名字段怎么拼？'
    add_raw(runtime, '流光客户端1.9必须发送旧字段account_name；2.0改为accountName。')
    result = call(runtime, query=query)
    lane = result['bundle']['raw_citation']
    assert lane['evidence']
    assert lane['admission']['status'] == 'related'
    entry = lane['evidence'][0]
    assert entry['applicability']['status'] == 'unknown'
    assert entry['applicability']['reason'] == 'version_range_not_covered'
    assert '1.9' in entry['text'] and '2.0' not in entry['text']
    assert 'applicability to the requested version range is not established' in result['context']
    assert 'Do not use it as a confirmed answer' in result['context']


def test_expiry_is_checked_again_on_normal_and_proactive_requests(runtime, monkeypatch):
    import eimemory.raw.applicability as applicability
    class Clock(datetime):
        value = datetime(2031, 3, 2, 12, tzinfo=timezone.utc)
        @classmethod
        def now(cls, tz=None):
            return cls.value
    monkeypatch.setattr(applicability, 'datetime', Clock)
    query = 'Where are Vesper deployment manifests stored?'
    source = ('Valid from: 2031-03-01\nValid until: 2031-03-03\n'
              'Vesper deployment manifests are stored in depot-expiring.')
    add_raw(runtime, source)
    persona(runtime, text='Prefer concise Vesper deployment status reports with primary evidence.')
    configure_proactive(runtime)
    assert call(runtime, query=query)['bundle']['raw_citation']['evidence']
    first = proactive(runtime, query=query)
    assert 'depot-expiring' in final_context(runtime, first, query=query)
    Clock.value = datetime(2031, 3, 3, tzinfo=timezone.utc)
    assert not call(runtime, query=query)['bundle']['raw_citation']['evidence']
    repeated = proactive(runtime, query=query)
    assert 'depot-expiring' not in final_context(runtime, repeated, query=query)
    assert search(runtime, query, fact=False), 'historical exploration remains available'


def test_withdrawn_metadata_cannot_reenter_fact_delivery(runtime):
    query = 'Where are Vesper manifests stored?'
    record = add_raw(runtime, 'Vesper manifests are stored in depot-retired.')
    assert call(runtime, query=query)['bundle']['raw_citation']['evidence']
    record.meta['mutation_state'] = 'superseded'
    persist_mutation(runtime, record)
    assert not call(runtime, query=query)['bundle']['raw_citation']['evidence']
    assert not search(runtime, query, fact=True)


def test_historical_validity_uses_requested_instant(runtime):
    text = ('Valid from: 2023-01-01\nValid until: 2024-01-01\n'
            'Vesper manifests are stored in depot-historical.')
    add_raw(runtime, text)
    assert search(runtime, 'Where are Vesper manifests stored as of 2023-06-02?', fact=True)
    assert not search(runtime, 'Where are Vesper manifests stored as of 2024-01-01?', fact=True)


def test_intraday_handoff_uses_only_effective_source_in_citation(runtime):
    first = add_raw(runtime, 'Valid from: 2034-12-01\nValid until: 2034-12-06T14:00:00Z\n'
                    'Wren deployment manifests are stored in depot-slate.')
    next_record = add_raw(runtime, 'Valid from: 2034-12-06T14:00:00Z\nValid until: unbounded\n'
                          'Wren deployment manifests are stored in depot-ivory.')
    query = 'Where are Wren deployment manifests stored as of 2034-12-06 09:00:00-05:00?'
    assert {row['record']['record_id'] for row in search(runtime, query, fact=False)} == {
        first.record_id, next_record.record_id}
    assert [row['record']['record_id'] for row in search(runtime, query, fact=True)] == [next_record.record_id]
    lane = call(runtime, query=query)['bundle']['raw_citation']
    assert [row['ref'][0] for row in lane['evidence']] == [next_record.record_id]
