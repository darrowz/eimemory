"""Local synthetic regressions for the explicitly authorized business contract."""
import pytest
from eimemory.evaluation.recall_agent_scopes import run_per_agent
from eimemory.evaluation.real_query_schema import production_real_query_label_authority
from eimemory.scheduler import jobs
from test_production_query_auto_review import (
    _keys, runtime, _seed, _collect, BASE_SCOPE, CHANNEL,
    auto_review_pending_production_queries,
)


def _roles(reports):
    scopes = [{**BASE_SCOPE, "agent_id": agent} for agent in reports]
    return run_per_agent(scopes, {}, {}, lambda scope: reports[scope["agent_id"]])


def _report(ok, reason="", n=0):
    return {"sample_count": n, "quality_gate": {"ok": ok, "blocked_reason": reason}}


def test_one_qualified_role_and_other_unevaluated():
    result = _roles({"qualified": _report(True, n=3),
                     "missing": _report(False, "recall_quality_evidence_incomplete")})
    assert result["combined"]["ok"] is True
    assert result["agents"]["missing"]["evaluation_status"] == "not_evaluated"
    assert result["role_coverage_complete"] is False


@pytest.mark.parametrize("reason", ["scope_permission_denied", "false_recall_rate_above_threshold"])
def test_known_zero_sample_role_failure_blocks(reason):
    result = _roles({"qualified": _report(True, n=3), "broken": _report(False, reason)})
    assert result["combined"]["ok"] is False
    assert result["combined"]["blocking_agent"] == "broken"
    assert result["agents"]["broken"]["blocked_reason"] == reason


@pytest.mark.parametrize("labeler, expected", [("operator", "operator"),
    ("release_operator", "operator"), ("auto_review", "auto_review"),
    (None, "unknown"), ("unrecognized", "unknown"), ("human", "unknown")])
def test_authority_is_not_inferred_human(labeler, expected):
    assert production_real_query_label_authority(labeler) == expected


def test_per_role_pipeline_includes_monitor(monkeypatch):
    import eimemory.evaluation.recall_agent_scopes as agents
    calls = []
    monkeypatch.setattr(agents, "recall_agent_scopes", lambda *_: [{**BASE_SCOPE, "agent_id": "other"}])
    import eimemory.evaluation.hermes_profile_coverage as profiles
    monkeypatch.setattr(profiles, "load_topology", lambda: None)
    for name in ("collection", "auto_review", "eval"):
        monkeypatch.setattr(jobs, "_run_production_recall_" + name,
            lambda *a, _name=name, **kw: calls.append(_name) or _report(True, n=3))
    monkeypatch.setattr(jobs, "_run_semantic_relevance_monitor", lambda *a, **kw: calls.append("monitor") or {})
    jobs._run_recall_quality_per_agent(object(), scope=BASE_SCOPE, primary_report={})
    assert calls == ["collection", "monitor", "auto_review", "eval"]


def test_temporary_model_failure_stays_retryable(runtime, monkeypatch):
    import eimemory.evaluation.production_query_auto_review as review
    _seed(runtime, 99001, semantic=None)
    _collect(runtime)
    monkeypatch.setattr(review, "_semantic_observation", lambda *a: {
        "status": "unknown", "reason": "completion_unavailable"})
    report = auto_review_pending_production_queries(runtime, scope=BASE_SCOPE, channel=CHANNEL)
    assert report["accepted_count"] == report["rejected_count"] == 0
    assert report["open_review_count"] == report["pending_count"] == 1
    assert report["closed_without_label_count"] == 0
    row = report["review_results"][0]
    assert row["review_complete"] is False and row["review_verdict"] == "pending"
    receipt = runtime.store.get_by_id(row["review_receipt_id"])
    assert receipt.content["review_complete"] is False
    assert receipt.content["disposition"] == "pending"


def test_retry_recovers_only_with_new_independent_observation(runtime, monkeypatch):
    from copy import deepcopy
    from eimemory.models.records import RecordEnvelope
    import eimemory.evaluation.production_query_auto_review as review
    from test_production_query_auto_review import SEM_SOURCE, SEM_VERSION, _digest
    _, decision_id = _seed(runtime, 99002)
    _collect(runtime)
    with runtime.store.locked() as db:
        decision = db.load_proactive_decision(decision_id)
    from eimemory.models.records import ScopeRef
    exact = ScopeRef.from_dict(decision['scope'])
    original = runtime.store.list_records_by_meta_value(kinds=['evaluation_packet'], scope=exact,
        meta_key='report_type', meta_value=SEM_VERSION, status='active', limit=10)[0]
    body = deepcopy(original.content)
    body.update(verdict='unknown', reason='completion_unavailable', relevance=[],
                off_topic=False, duplicates=False, unanswered='unknown')
    runtime.store.append(RecordEnvelope.create(kind='evaluation_packet', title='Temporary failure',
        content=body, scope=exact, source=SEM_SOURCE,
        meta={**original.meta, 'semantic_monitor_digest': _digest(body)}))
    first = auto_review_pending_production_queries(runtime, scope=BASE_SCOPE, channel=CHANNEL)
    assert first['accepted_count'] == 0 and first['open_review_count'] == 1
    runtime.store.append(RecordEnvelope.create(kind='evaluation_packet', title='Fresh independent judgment',
        content=original.content, scope=exact, source=SEM_SOURCE, meta=original.meta))
    second = auto_review_pending_production_queries(runtime, scope=BASE_SCOPE, channel=CHANNEL)
    assert second['accepted_count'] == 1 and second['open_review_count'] == 0
    assert runtime.store.get_by_id(second['accepted_record_ids'][0]).meta['label_authority'] == 'auto_review'


def test_missing_scope_cannot_hide_known_failure_of_same_role():
    scopes = [{**BASE_SCOPE, 'agent_id': 'same', 'user_id': user} for user in ('a', 'b')]
    result = run_per_agent(scopes, {}, {}, lambda s: _report(False,
        'scope_permission_denied' if s['user_id'] == 'a' else 'recall_quality_evidence_incomplete'))
    assert result['combined']['blocked_reason'] == 'scope_permission_denied'


def test_missing_primary_can_use_a_formally_verified_role(monkeypatch):
    from eimemory.evaluation.real_query_schema import PRODUCTION_REAL_QUERY_REPORT_SCHEMA, PRODUCTION_REAL_QUERY_POLICY
    gate = {'ok': True, 'vacuous': False, 'recall_quality_evidence': {
        'evaluation_contract': PRODUCTION_REAL_QUERY_REPORT_SCHEMA,
        'formal_business_evidence': True, 'label_trust_accepted': True,
        'release_authority_verified': True, 'authority_record_id': 'prg_role'}}
    qualified = {'schema': PRODUCTION_REAL_QUERY_REPORT_SCHEMA,
        'policy_schema': PRODUCTION_REAL_QUERY_POLICY, 'dataset_kind': 'production',
        'sample_count': 3, 'accepted': True, 'gate_status': 'accepted',
        'persisted': True, 'persisted_record_id': 'prg_role', 'quality_gate': gate}
    scopes = [{**BASE_SCOPE, 'agent_id': agent} for agent in ('primary', 'qualified')]
    result = run_per_agent(scopes, {}, {}, lambda s: qualified if s['agent_id'] == 'qualified'
        else _report(False, 'recall_quality_evidence_incomplete'))
    assert result['combined']['ok'] is True
    assert result['mechanism_report'] == qualified
    # A successful diagnostic gate alone never supplies formal authority.
    result = _roles({'qualified': _report(True, n=3)})
    assert 'mechanism_report' not in result


def test_final_batch_does_not_hide_earlier_retry_after_restart(tmp_path, monkeypatch):
    from eimemory.api.runtime import Runtime
    import eimemory.evaluation.production_query_auto_review as review
    original = review._semantic_observation
    with Runtime.create(root=tmp_path) as rt:
        _seed(rt, 99100)
        _collect(rt)
        _seed(rt, 99101, with_candidate=False)
        _collect(rt)
        monkeypatch.setattr(review, '_semantic_observation', lambda *a: {
            'status': 'unknown', 'reason': 'completion_unavailable'})
        first = auto_review_pending_production_queries(rt, scope=BASE_SCOPE, channel=CHANNEL, limit=1)
        assert first['open_review_count'] is None
        assert first['review_results'][0]['review_verdict'] == 'pending'
    with Runtime.create(root=tmp_path) as rt:
        monkeypatch.setattr(review, '_semantic_observation', original)
        last = auto_review_pending_production_queries(rt, scope=BASE_SCOPE, channel=CHANNEL, limit=1)
        assert last['coverage']['cycle_complete'] is True
        assert last['open_review_count'] == last['pending_count'] == 1


def test_empty_successful_gate_does_not_establish_mechanism():
    result = _roles({'empty': _report(True, n=0)})
    assert result['combined']['ok'] is False
    assert result['agents']['empty']['evaluation_status'] == 'not_evaluated'


def test_nightly_uses_verified_role_report_without_claiming_role_coverage(runtime, monkeypatch):
    from eimemory.evaluation.real_query_schema import PRODUCTION_REAL_QUERY_REPORT_SCHEMA, PRODUCTION_REAL_QUERY_POLICY
    from eimemory.scheduler.result_contract import nightly_result_diagnostics
    gate = {'ok': True, 'vacuous': False, 'recall_quality_evidence': {
        'evaluation_contract': PRODUCTION_REAL_QUERY_REPORT_SCHEMA,
        'formal_business_evidence': True, 'label_trust_accepted': True,
        'release_authority_verified': True, 'authority_record_id': 'prg_role'}}
    qualified = {'ok': True, 'schema': PRODUCTION_REAL_QUERY_REPORT_SCHEMA,
        'policy_schema': PRODUCTION_REAL_QUERY_POLICY, 'dataset_kind': 'production',
        'sample_count': 3, 'accepted': True, 'gate_status': 'accepted',
        'persisted': True, 'persisted_record_id': 'prg_role', 'quality_gate': gate}
    primary = {'ok': True, **_report(False, 'recall_quality_evidence_incomplete')}
    per_role = {'ok': True, 'combined': {'ok': True}, 'mechanism_report': qualified,
        'agents': {'missing': {'ok': False, 'evaluation_status': 'not_evaluated'}},
        'role_coverage_complete': False}
    def step(steps, name, fn):
        steps.append({'step': name, 'ok': True})
        return {'production_recall': primary, 'recall_quality_per_agent': per_role}.get(name, {'ok': True})
    monkeypatch.setattr(jobs, '_nightly_step', step)
    report = jobs.run_nightly_jobs(runtime, scope=BASE_SCOPE)
    assert report['recall_quality']['persisted_record_id'] == 'prg_role'
    diagnostics = nightly_result_diagnostics(report, report['step_reports'])
    assert diagnostics['natural_recall_quality_accepted'] is True
    assert diagnostics['natural_recall_quality_per_agent']['role_coverage_complete'] is False


@pytest.mark.parametrize('failed_stage', ['collection', 'monitor', 'auto_review'])
def test_per_role_producer_failure_vetoes_even_existing_good_labels(monkeypatch, failed_stage):
    import eimemory.evaluation.recall_agent_scopes as agents
    import eimemory.evaluation.hermes_profile_coverage as profiles
    monkeypatch.setattr(agents, 'recall_agent_scopes', lambda *_: [{**BASE_SCOPE, 'agent_id': 'other'}])
    monkeypatch.setattr(profiles, 'load_topology', lambda: None)
    for stage, name in [('collection', '_run_production_recall_collection'),
                        ('monitor', '_run_semantic_relevance_monitor'),
                        ('auto_review', '_run_production_recall_auto_review')]:
        monkeypatch.setattr(jobs, name, lambda *a, _stage=stage, **kw: {
            'ok': _stage != failed_stage, 'blocked_reason': 'producer_execution_failed'})
    monkeypatch.setattr(jobs, '_run_production_recall_eval', lambda *a, **kw: _report(True, n=3))
    result = jobs._run_recall_quality_per_agent(object(), scope=BASE_SCOPE, primary_report={})
    assert result['combined']['ok'] is False
    assert result['combined']['blocked_reason'] == 'producer_execution_failed'


def test_execution_error_cannot_be_deferred_by_evidence_wait_gate():
    from eimemory.evaluation.recall_agent_scopes import agent_verdict
    result = agent_verdict({**_report(False, 'recall_quality_evidence_incomplete'),
                            'ok': False, 'error': 'evaluation_execution_failed'})
    assert result['ok'] is False
    assert result['blocked_reason'] == 'evaluation_execution_failed'
