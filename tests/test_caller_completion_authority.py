"""Controlled boundary tests, not real-model quality evidence."""
import copy
from types import SimpleNamespace
import pytest
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.retrieval import authority_gate as gate
from eimemory.adapters.runtime.service import AgentRuntimeMemoryService as Service


@pytest.mark.parametrize('change', ['', 'revoked', 'missing', 'read_timeout', 'unfinished'])
def test_finished_caller_gets_one_fresh_bounded_authority_read(monkeypatch, change):
    clock = [100.0]
    monkeypatch.setattr(gate, 'perf_counter', lambda: clock[0])
    monkeypatch.setattr(gate, 'recall_budget_seconds', lambda: 8.0)
    row = RecordEnvelope.create(kind='memory', title='Fact', summary='supported fact', scope=ScopeRef())
    reads = []
    def hydrate(rows, *, deadline_at):
        reads.append(deadline_at)
        if len(reads) == 2:
            if change == 'read_timeout':
                clock[0] = deadline_at + 1
            if change == 'missing':
                return {}
            if change == 'revoked':
                altered = copy.deepcopy(row); altered.status = 'deprecated'
                return {row.record_id: altered}
        return {row.record_id: row}
    engine = SimpleNamespace(_record_key=lambda r: r.record_id, _hydrate_records_batch=hydrate)
    def select(self, rows, **kwargs):
        clock[0] = 110.0  # retrieval expired while a bounded verifier completed
        return rows, {'status': 'evidence_found', 'caller_assistance': {
            'calls': 0 if change == 'unfinished' else 1,
            'status': 'evidence_found', 'outcome': 'supported'}}
    selected, state = gate.enforce_selection_authority(select)(engine, [row], limit=1, deadline_at=108.0)
    if change:
        assert selected == []
        assert state['status'] == 'unavailable'
    else:
        assert selected == [row]
        assert reads == [108.0, 118.0]
        assert state['status'] == 'evidence_found'


def supported_result():
    return {'ok': True, 'result': {'ok': True, 'bundle': {
        'retrieval_status': 'evidence_found', 'items': [{'record_id': 'memory-1'}],
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
