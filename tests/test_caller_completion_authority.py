"""Controlled boundary tests, not real-model quality evidence."""
import copy
from types import SimpleNamespace
import pytest
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.retrieval import authority_gate as gate
from eimemory.adapters.runtime.service import AgentRuntimeMemoryService as Service




def supported_result():
    return {'ok': True, 'result': {'ok': True, 'bundle': {
        'retrieval_status': 'evidence_found', 'items': [{'record_id': 'memory-1', 'status': 'active'}],
        'recall_diagnostics': {'admission_status': 'evidence_found', 'caller_assistance': {
            'status': 'evidence_found', 'outcome': 'supported', 'calls': 1,
            'proofs': [{'record_id': 'memory-1', 'quote_digest': 'ab'*32, 'span_start': 0, 'span_end': 8}]}}}}}


@pytest.mark.parametrize('failure', ['', 'outer_failed', 'inner_failed', 'bypassed', 'unavailable', 'empty', 'wrong_record', 'nested_only'])
def test_receipt_requires_successful_final_bundle_and_returned_proof(failure):
    result = supported_result()
    bundle = result['result']['bundle']
    if failure == 'outer_failed': result['ok'] = False
    if failure == 'inner_failed': result['result']['ok'] = False
    if failure == 'bypassed': result['bypassed'] = True
    if failure == 'unavailable': bundle['retrieval_status'] = 'unavailable'
    if failure == 'empty': bundle['items'] = []
    if failure == 'wrong_record': bundle['items'] = [{'record_id': 'other'}]
    if failure == 'nested_only': result = {'ok': True, 'untrusted_content': result}
    assert Service._business_caller_evidence(result) is (not failure)
