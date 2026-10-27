"""A native repair must appear as a resolution in the nightly result."""
import json
from hashlib import sha256
from dataclasses import asdict

from eimemory.evaluation import semantic_gap_repair as repair
from eimemory.evaluation import semantic_relevance_monitor as semantic
from eimemory.scheduler import jobs
from eimemory.scheduler.result_contract import nightly_result_diagnostics
from test_semantic_gap_repair import incident
from test_semantic_gap_repair_flow import replay_case


def test_signed_repair_is_reported_as_resolution_with_no_new_off_topic_finding(replay_case, monkeypatch):
    runtime, scope, gap, _, _, _, _, _ = replay_case
    report, findings = repair.collect_semantic_gap_repair_findings(runtime, scope=scope, max_new=2)
    assert len(findings) == 1
    monkeypatch.setattr(semantic, 'monitor_channel_deliveries', lambda *_, **__: (
        {'status': 'observation_only', 'semantic_gap_repair': {**report, 'by_agent': {'current-agent': report}}},
        findings))
    actual = jobs._run_semantic_relevance_monitor(runtime, scope=asdict(scope))
    assert actual['ok'] is True
    assert actual['off_topic_finding_count'] == actual['quality_gap_created_count'] == 0
    assert actual['repair_passing_finding_count'] == actual['quality_gap_resolved_count'] == 1
    resolution = runtime.store.get_by_id(actual['quality_gap_resolved_record_ids'][0], scope=scope)
    assert resolution.status == 'resolved' and resolution.content['resolves_gap_id'] == gap.record_id
    safe = nightly_result_diagnostics({'semantic_relevance_monitor': actual}, [])['recall_semantic_relevance']
    assert safe['off_topic_finding_count'] == 0 and safe['quality_gap_resolved_count'] == 1
    assert safe['quality_gap_resolved_record_ids'] == [resolution.record_id]
    assert safe['semantic_gap_repair']['replay_count'] == safe['semantic_gap_repair']['provider_calls'] == 1
    assert safe['semantic_gap_repair']['natural_benchmark_eligible'] is False
    assert 'PRIVATE' not in json.dumps(safe)


def test_repair_diagnostics_only_expose_bounded_counts_and_native_resolution_refs():
    raw = {'semantic_gap_repair': {'query': 'PRIVATE', 'provider_calls': True,
        'by_agent': {'hongtai': {'query': 'PRIVATE', 'replay_count': 1}, 'PRIVATE name': {'replay_count': 3}}},
        'quality_gap_resolved_count': 1,
        'quality_gap_resolved_record_ids': ['PRIVATE', 'ref_' + 'a' * 12]}
    safe = nightly_result_diagnostics({'semantic_relevance_monitor': raw}, [])['recall_semantic_relevance']
    assert safe['quality_gap_resolved_record_ids'] == ['ref_' + 'a' * 12]
    assert safe['semantic_gap_repair']['provider_calls'] == 0
    assert safe['semantic_gap_repair']['by_agent']['hongtai']['replay_count'] == 1
    assert 'PRIVATE' not in json.dumps(safe)


def test_main_calls_survive_zero_final_global_snapshots():
    scope = dict(tenant_id='t', agent_id='a', workspace_id='w', user_id='u')
    main = {'provider_calls': 0, 'explicit': {'provider_calls': 0},
            'semantic_gap_repair': {'provider_calls': 3, 'replay_count': 5}}
    final = {'provider_calls': 0, 'semantic_gap_repair': {'provider_calls': 0, 'replay_count': 2}}
    raw = {'semantic_relevance_monitor': main, 'recall_quality_per_agent': {
        'producer_reports': [{'scope': {**scope, 'user_id': str(i)},
                              'semantic_relevance_monitor': final} for i in range(4)]}}
    evidence = nightly_result_diagnostics(raw, [])['known_lane_call_evidence']
    assert evidence['coverage'] == 'reported_lanes_only'
    assert evidence['main']['phase'] == 'main'
    assert evidence['main']['scope'] is None
    assert evidence['main']['lanes']['repair_judge'] == 3
    assert evidence['main']['lanes']['repair_recall'] == 5
    assert len(evidence['final']) == 1
    assert evidence['final'][0]['phase'] == 'final'
    assert len(evidence['final'][0]['scopes']) == 4
    assert evidence['final'][0]['lanes']['repair_judge'] == 0
    assert 'system_total' not in evidence


def scope_reference(scope):
    encoded = json.dumps(scope, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return {"scope_digest": sha256(encoded).hexdigest()}


def test_lane_evidence_keeps_missing_calls_unknown_and_main_scope():
    scope = dict(tenant_id='t', agent_id='a', workspace_id='w', user_id='logical')
    evidence = nightly_result_diagnostics({'scope': scope, 'semantic_relevance_monitor': {
        'provider_calls': True, 'semantic_gap_repair': {'provider_calls': 3}}}, [])['known_lane_call_evidence']
    assert evidence['main']['scope'] == scope_reference(scope)
    assert evidence['main']['lanes'] == {'repair_judge': 3}
    assert evidence['final'] == []


import pytest


@pytest.mark.parametrize('unknown', [{'semantic_gap_repair': {}}, {'provider_calls': True, 'semantic_gap_repair': {'provider_calls': True}}], ids=['missing', 'bool'])
@pytest.mark.parametrize('reverse', [False, True], ids=['unknown-zero', 'zero-unknown'])
def test_final_groups_require_equal_strict_lane_evidence(unknown, reverse):
    zero = {'provider_calls': 0, 'semantic_gap_repair': {'provider_calls': 0}}
    monitors = [unknown, zero][:: -1 if reverse else 1]
    rows = [{'scope': dict(tenant_id='t', agent_id='a', workspace_id='w', user_id=str(i)),
             'semantic_relevance_monitor': monitor} for i, monitor in enumerate(monitors)]
    groups = nightly_result_diagnostics({'recall_quality_per_agent': {'producer_reports': rows}}, [])['known_lane_call_evidence']['final']
    assert len(groups) == 2
    assert groups[0]['snapshot'] == groups[1]['snapshot']
    expected = [{}, {'delivery_and_confirmation': 0, 'repair_judge': 0}][:: -1 if reverse else 1]
    assert [group['lanes'] for group in groups] == expected
    assert [group['scopes'] for group in groups] == [[scope_reference(rows[0]['scope'])], [scope_reference(rows[1]['scope'])]]


@pytest.mark.parametrize('identity_field', ['tenant_id', 'agent_id', 'workspace_id', 'user_id'])
def test_lane_scope_references_hide_identities_without_losing_phases_or_counts(identity_field):
    scope = dict(tenant_id='PRIVATE-tenant', agent_id='PRIVATE-agent',
                 workspace_id='PRIVATE-host', user_id='host-user')
    other = {**scope, identity_field: 'PRIVATE-other'}
    monitor = {'provider_calls': 2, 'explicit': {'provider_calls': 0},
               'semantic_gap_repair': {'provider_calls': 3, 'replay_count': 5}}
    raw = {'scope': scope, 'semantic_relevance_monitor': monitor,
           'recall_quality_per_agent': {'producer_reports': [
               {'scope': item, 'semantic_relevance_monitor': monitor}
               for item in [scope, other, scope]]}}
    evidence = nightly_result_diagnostics(raw, [])['known_lane_call_evidence']
    assert evidence['main']['scope'] == scope_reference(scope)
    assert evidence['main']['phase'] == 'main'
    assert evidence['final'][0]['phase'] == 'final'
    assert evidence['final'][0]['scopes'] == [scope_reference(scope), scope_reference(other)]
    assert evidence['main']['lanes'] == evidence['final'][0]['lanes'] == {
        'delivery_and_confirmation': 2, 'explicit_monitor': 0,
        'repair_judge': 3, 'repair_recall': 5}
    serialized = json.dumps(evidence)
    assert 'PRIVATE' not in serialized and 'host-user' not in serialized
    assert evidence['counts_semantics'] == 'separate_phases_not_additive'


@pytest.mark.parametrize('scope', [None, {}, {'user_id': {'private': 'host-user'}},
    dict(tenant_id='t', agent_id='a', workspace_id='w', user_id=['host-user'])])
def test_lane_scope_references_fail_closed_for_malformed_identity(scope):
    raw = {'scope': scope, 'semantic_relevance_monitor': {'provider_calls': 1},
           'recall_quality_per_agent': {'producer_reports': [
               {'scope': scope, 'semantic_relevance_monitor': {'provider_calls': 0}}]}}
    evidence = nightly_result_diagnostics(raw, [])['known_lane_call_evidence']
    assert evidence['main']['scope'] is None
    assert evidence['main']['lanes'] == {'delivery_and_confirmation': 1}
    assert 'host-user' not in json.dumps(evidence)
    if isinstance(scope, dict):
        assert evidence['final'][0]['scopes'] == [None]
        assert evidence['final'][0]['lanes'] == {'delivery_and_confirmation': 0}
