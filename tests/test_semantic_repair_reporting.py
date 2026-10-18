"""A native repair must appear as a resolution in the nightly result."""
import json
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
