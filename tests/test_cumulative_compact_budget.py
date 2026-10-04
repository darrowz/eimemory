import copy
import json
from types import SimpleNamespace

import pytest

from eimemory.models.records import RecallBundle, RecordEnvelope, ScopeRef


def oversized_bundle():
    record = RecordEnvelope.create(kind='reflection', title='合成预算测试',
        scope=ScopeRef(), source_id='a' * 128,
        provenance={'project_context': {'schema': 'same_turn_release_context.v1',
                                       'synthetic_context': '合成' * 3000}})
    return RecallBundle([record], [], [], .8, 'fixture')


def test_public_budget_rejects_oversize_without_mutating_parent():
    from eimemory.models.records import CompactRecallBudgetExceeded
    bundle = oversized_bundle()
    before = copy.deepcopy(bundle.to_dict())
    with pytest.raises(CompactRecallBudgetExceeded) as caught:
        bundle.to_compact_dict(limit=1)
    error = caught.value.to_dict()
    assert error['ok'] is False and error['retrieval_status'] == 'unavailable'
    assert error['size_budget']['maximum_bytes'] == 4096
    assert error['size_budget']['payload_bytes'] > 4096
    assert error['size_budget']['selected_count'] == 1
    assert error['size_budget']['delivered_count'] == 0
    assert bundle.to_dict() == before
    assert 'synthetic_context' not in json.dumps(error)


def test_budget_exact_utf8_boundary_preserves_identity_and_proof():
    from eimemory.models.records import _fit_compact_payload
    payload = {'items': [{'record_id': 'mem_synthetic', 'source_id': 'a'*128,
                         'project_context': {'host_text': '合成'}}],
               'rules': [], 'reflections': [],
               'recall_diagnostics': {'caller_assistance': {'proofs': [{'quote_digest': 'a'*64}]}}}
    before = copy.deepcopy(payload)
    size = len(json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode())
    assert _fit_compact_payload(payload, maximum_bytes=size) == before
    from eimemory.models.records import CompactRecallBudgetExceeded
    with pytest.raises(CompactRecallBudgetExceeded):
        _fit_compact_payload(payload, maximum_bytes=size-1)
    assert payload == before


def test_cli_budget_failure_is_json_and_nonzero(capsys):
    from eimemory.cli.main import _dispatch_recall
    bundle = oversized_bundle()
    runtime = SimpleNamespace(memory=SimpleNamespace(recall=lambda **kwargs: bundle))
    args = SimpleNamespace(view=None, compact=True, query='synthetic', limit=1, explain=False)
    assert _dispatch_recall(args, runtime, {}) == 1
    result = json.loads(capsys.readouterr().out)
    assert result['ok'] is False and result['error'] == 'compact_payload_too_large'
    assert result['size_budget']['delivered_count'] == 0


def test_eval_budget_failure_does_not_certify_execution_or_size():
    from eimemory.evaluation.production_recall import run_production_recall_eval
    bundle = oversized_bundle()
    runtime = SimpleNamespace(memory=SimpleNamespace(recall=lambda **kwargs: bundle))
    result = run_production_recall_eval(runtime, {'cases': [{'query': 'synthetic', 'topk': 1}]}, seed=False)
    assert result['ok'] is False and result['execution_ok'] is False
    assert result['payload_ceiling_ok'] is False
    assert result['samples'][0]['passed'] is False
    assert result['errors'][0]['error'] == 'compact_payload_too_large'
    assert result['payload_bytes_top_1'] > 4096
