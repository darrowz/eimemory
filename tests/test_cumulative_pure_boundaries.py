"""Pure candidate checks against current modules; all fetches are injected."""
from types import SimpleNamespace
from dataclasses import asdict

import pytest

from eimemory.capabilities import applicability, consumer_views
from eimemory.contracts import capability_validators, recall_evidence
from eimemory.intake import connectors, fulltext
from eimemory.scoring import adapters, contract, evaluator, thresholds
from eimemory.governance.learning import replay_dataset as dataset
from eimemory.models.records import ScopeRef


@pytest.mark.parametrize('value', [True, False, float('nan'), float('inf'), 'NaN', ' ', {}, 10**400])
def test_score_contract_rejects_invalid_penalty(value):
    with pytest.raises(ValueError):
        contract.ScoreComponent('risk_penalty', value, .35)


def test_missing_penalty_is_not_a_zero_penalty():
    with pytest.raises(ValueError):
        contract.ScoreComponent.from_dict({'name': 'risk_penalty', 'weight': .35})


@pytest.mark.parametrize('value', [[], 'private', True, {'bad': 'shape'}])
def test_bad_optional_score_hint_allows_recompute(value):
    assert adapters.extract_memory_score({'scoring': value}) is None


def test_invalid_legacy_benefit_gets_zero_but_missing_and_zero_keep_contract():
    assert evaluator._legacy_numeric({'confidence': 'NaN'}, 'confidence', default=.5) == 0
    assert evaluator._legacy_numeric({}, 'confidence', default=.5) == .5
    assert evaluator._legacy_numeric({'confidence': 0}, 'confidence', default=.5) == 0


def test_clamp_does_not_reward_nan_or_bool():
    assert thresholds.clamp_score(float('nan')) == 0
    assert thresholds.clamp_score(True) == 0


def test_script_neutral_body_check_preserves_long_text_and_rejects_repetition():
    text = '系统必须保留完整查询输入并在同一事务中核验来源身份和执行证据'
    assert not evaluator._thin_or_repetitive_body(text, evaluator._normalized_terms(text))
    assert evaluator._thin_or_repetitive_body('abcabcabcabc', ['abcabcabcabc'])


@pytest.mark.parametrize('field', ['lexical_score', 'semantic_score', 'vector_score', 'source_weight', 'modality_boost'])
def test_nonfinite_recall_inputs_do_not_become_positive_evidence(field):
    from eimemory.models.records import RecordEnvelope
    record = RecordEnvelope.create(kind='memory', title='fake', scope=ScopeRef(tenant_id='fake'),
                                   content={'text': 'Preserve exact source evidence'})
    values = dict(lexical_score=0, semantic_score=0, vector_score=0, source_weight=.5, modality_boost=0)
    values[field] = float('nan')
    score = evaluator.evaluate_recall_score(record=record, query='exact source evidence', **values)
    assert score.components['relevance'].value == 0


def test_inline_html_keeps_document_order():
    parser = fulltext._DocumentParser()
    parser.feed('<p>Before <b>middle</b> after <i>end</i>.</p><script>hidden</script>')
    assert 'Before middle after end.' in fulltext._node_text(parser.root)
    assert 'hidden' not in fulltext._node_text(parser.root)


def test_chatpaper_query_category_has_precedence():
    assert connectors._chatpaper_fallback_categories('https://example.invalid/cs.AI?category=cs.CL', []) == ['cs.CL']


def test_legacy_arxiv_id_and_version_remain_complete():
    assert connectors._extract_arxiv_id('https://arxiv.org/pdf/hep-th/9901001v2.pdf') == 'hep-th/9901001v2'


def test_empty_selected_capability_set_is_not_authority():
    view = {'capabilities': [], 'profile': {'provenance': {'capability_aliases': {'legacy': 'memory.recall'}}}}
    assert consumer_views.capability_aliases_from_view(view) == {}
    result = consumer_views.resolve_explicit_capability_attribution(
        [{'target_capability': 'memory.recall'}], allowed_capability_ids=[])
    assert result['status'] == 'unclassified'


@pytest.mark.parametrize('constraints', [
    {'allowed_scopes': []}, {'allowed_environment_digests': []},
    {'allowed_scopes': 'global'}, {'allowed_environment_digests': ['not-a-digest']},
])
def test_explicit_or_malformed_allowlist_fails_closed(constraints):
    result = applicability._environment_constraint_gate(constraints, capability_scope='global', environment_digests=set())
    assert result['status'] == 'blocked'


def test_recall_record_identity_uses_shared_path_safe_contract():
    assert recall_evidence._record_id({'record_id': '../unsafe'}) == ''
    assert recall_evidence._record_id({'record_id': 'mem_' + 'a'*150}) == 'mem_' + 'a'*150


def test_outcome_replay_reads_canonical_payload_not_wrapper(monkeypatch):
    scope = ScopeRef(tenant_id='fake')
    record = SimpleNamespace(kind='reflection', source='eimemory.experience.outcome_trace',
        status='active', record_id='ref_actual', scope=scope, meta={'report_type': 'outcome_trace'},
        title='wrong title', summary='wrong summary', content={
            'schema_version': 'outcome_trace.v1', 'primary_label': 'failure',
            'input_summary': 'wrong wrapper query', 'correction': 'wrong wrapper correction',
            'payload': {'input_summary': 'Inspect the inventory', 'correction_from_user': 'Count all bins',
                        'task_type': 'inventory.check'}})
    monkeypatch.setattr(dataset, '_records_by_meta_value', lambda *args, **kwargs: [record])
    cases = dataset._cases_from_outcome_traces(object(), scope=scope, limit=10)
    assert cases[0]['query'] == 'Inspect the inventory'
    assert cases[0]['source_record_id'] == 'ref_actual'
    assert cases[0]['correction_from_user'] == 'Count all bins'
    assert 'real_provenance_ok' not in cases[0]


@pytest.mark.parametrize('field', ['tenant_id', 'agent_id', 'workspace_id', 'user_id'])
def test_outcome_candidate_rejects_foreign_exact_scope(field):
    scope = ScopeRef(tenant_id='fake')
    other = ScopeRef.from_dict({**asdict(scope), field: 'other'})
    record = SimpleNamespace(kind='reflection', source='eimemory.experience.outcome_trace',
        status='active', record_id='ref_actual', scope=other, meta={'report_type': 'outcome_trace'},
        content={'schema_version': 'outcome_trace.v1', 'payload': {}})
    assert dataset._outcome_trace_source_record_id(record, scope=scope) == ''


def test_report_type_alone_cannot_authorize_outcome_trace(monkeypatch):
    scope = ScopeRef(tenant_id='fake')
    forged = SimpleNamespace(kind='reflection', source='unit.test', status='active',
        record_id='ref_forged', scope=scope, meta={'report_type': 'outcome_trace'},
        title='fake title', summary='fake summary', content={
            'input_summary': 'Inspect inventory', 'correction': 'Count bins', 'primary_label': 'failure'})
    monkeypatch.setattr(dataset, '_records_by_meta_value', lambda *args, **kwargs: [forged])
    assert dataset._cases_from_outcome_traces(object(), scope=scope, limit=10) == []
