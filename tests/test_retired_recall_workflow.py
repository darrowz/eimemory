"""Retirement preserves operator evidence and never manufactures release gold."""
from copy import deepcopy
import inspect
import sys
from types import SimpleNamespace

import pytest

from eimemory.api.runtime import Runtime
from eimemory.cli.main import _build_parser
from eimemory.evaluation.production_query_dataset import (
    ACCEPTED_QUERY_SCHEMA, accept_pending_production_query,
    build_production_query_dataset, collect_pending_production_queries,
)
from eimemory.evaluation.real_query_gate import _freeze_real_query_case, _stable_digest
from eimemory.models.records import ScopeRef
from test_production_query_dataset import BASE_SCOPE, LABEL_PACKET_EVIDENCE, _seed_decision


@pytest.mark.parametrize('args', [
    ['auto-label', 'propose'], ['auto-label', 'queue'],
    ['auto-label', 'promote', 'proposal', '--operator-id', 'operator'],
    ['review-pending', '--channel', 'codex', '--review-delegation-json', 'unused'],
    ['collect', '--review-delegation-json', 'unused'],
])
def test_retired_cli_rejected_before_runtime(args):
    with pytest.raises(SystemExit) as exc:
        _build_parser().parse_args(['eval', 'production-query', *args])
    assert exc.value.code == 2


def test_no_delegated_acceptance_backdoor():
    assert '_delegated_labels' not in inspect.signature(accept_pending_production_query).parameters
    with pytest.raises(ValueError, match='trusted operator labeler required'):
        accept_pending_production_query(None, pending_record_id='unused', query_features={},
            labels=[], labeler='delegated_ai', operator_scope=BASE_SCOPE, label_packet_evidence={})


def test_operator_history_readable_delegated_history_excluded_without_rewrite(tmp_path, monkeypatch):
    monkeypatch.setenv('EIMEMORY_EVIDENCE_RECEIPT_HMAC_KEY', 'retirement-fixture-only-key')
    runtime = Runtime.create(root=tmp_path)
    try:
        gold = _seed_decision(runtime, channel='codex', index=901)
        pending = collect_pending_production_queries(runtime, scope=BASE_SCOPE)['pending_record_ids'][0]
        result = accept_pending_production_query(runtime, pending_record_id=pending,
            query_features={'terms': ['archive', 'routing', 'destination'], 'intent': 'memory recall'},
            labels=[{'record_ref': gold.record_id, 'grade': 3}], labeler='operator',
            operator_scope=BASE_SCOPE, label_packet_evidence=LABEL_PACKET_EVIDENCE)
        operator = runtime.store.get_by_id(result['record_id'])
        before = operator.to_dict()
        historical = deepcopy(operator)
        case = historical.content['case']
        case['labels'][0]['provenance']['labeler'] = 'delegated_ai'
        historical.record_id = 'prqa_' + _stable_digest({'schema': ACCEPTED_QUERY_SCHEMA, 'case': case})[:32]
        runtime.store.append(historical)
        historical_before = historical.to_dict()
        built = build_production_query_dataset(runtime, scope=BASE_SCOPE)
        assert built['progress']['accepted_case_count'] == 1
        assert built['dataset']['cases'] == [operator.content['case']]
        assert built['ready'] is False
        assert runtime.store.get_by_id(operator.record_id).to_dict() == before
        assert runtime.store.get_by_id(historical.record_id).to_dict() == historical_before
        frozen, reasons = _freeze_real_query_case(case, index=0, base_scope=ScopeRef.from_dict(BASE_SCOPE))
        assert 'accepted_labeler_untrusted' in reasons
        assert frozen['labels'] == []
    finally:
        runtime.close()


def test_unconfigured_gate_never_reviews_or_claims_acceptance(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setitem(sys.modules, 'eimemory.evaluation.delegated_recall_review',
        SimpleNamespace(collect_and_review_configured=lambda runtime: calls.append(runtime)))
    monkeypatch.setattr('eimemory.scheduler.jobs._production_recall_dataset',
        lambda runtime, *, scope: ({}, False, '', 'production_recall_dataset_unconfigured'))
    runtime = Runtime.create(root=tmp_path)
    try:
        report = runtime.run_configured_production_recall_gate(scope=BASE_SCOPE)
        assert calls == []
        assert report['accepted'] is False
        assert report['gate_status'] == 'not_run'
        assert 'machine_review' not in report
        built = build_production_query_dataset(runtime, scope=BASE_SCOPE)
        assert built['ready'] is False
        assert built['progress']['accepted_case_count'] == 0
    finally:
        runtime.close()


def test_quality_report_explicit_about_observation_only_evolution_seam(tmp_path):
    runtime = Runtime.create(root=tmp_path)
    try:
        signal = runtime.evolution.memory_quality_report(scope=BASE_SCOPE)['recall_relevance_evolution']
        assert signal == {
            'status': 'observation_only', 'producer': 'proactive_delivery_audit',
            'detector': 'duplicate_delivered_record', 'handoff': 'quality_gap_intake',
            'automatic_promotion': False, 'semantic_relevance': 'not_assessed',
        }
    finally:
        runtime.close()
