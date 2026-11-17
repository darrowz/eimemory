"""Independent offline review of delivery-copy and explicit rendering checks.

Real service assembly/rendering, signed captures, exact references and SQLite
are used. Only the kernel collector and unbound release lookup are fixtures;
the receipt key is synthetic and no production key/config is read.
"""
from copy import deepcopy
import socket
import subprocess
from types import SimpleNamespace

import pytest

from eimemory.adapters.runtime.channel import resolve_channel_scope
from eimemory.adapters.runtime.service import AgentRuntimeMemoryService
from eimemory.evaluation import explicit_recall as explicit
from eimemory.governance.tool_receipts import ReceiptKeySet
from eimemory.models.records import RecordEnvelope, RecallBundle, ScopeRef
from eimemory.recall import raw_citation
from eimemory.storage.runtime_store import RuntimeStore

SCOPE = {'tenant_id': 'independent-review', 'agent_id': 'reader',
         'workspace_id': 'lab', 'user_id': 'fixture-owner'}


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    def denied(*args, **kwargs):
        pytest.fail('external network/process forbidden')
    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(subprocess, 'Popen', denied)
    monkeypatch.setenv('EIMEMORY_RAW_ANSWER_SUPPORT_ENABLED', '0')
    monkeypatch.setenv('EIMEMORY_ROOT', str(tmp_path / 'unconfigured'))
    monkeypatch.setenv('EIMEMORY_SYSTEM_SETTINGS_PATH', str(tmp_path / 'absent.json'))


@pytest.fixture
def delivery(monkeypatch, tmp_path):
    store = RuntimeStore(tmp_path / 'synthetic-store')
    scope = ScopeRef.from_dict(resolve_channel_scope('hermes', SCOPE))
    row = store.append(RecordEnvelope.create(kind='memory', title='Independent source',
        summary='The cedar relay uses a local spool.', content={'text': 'The cedar relay uses a local spool.'},
        source='review.synthetic', scope=scope))
    bundle = RecallBundle([row], [], [], 0.8, '', explanation={})
    runtime = SimpleNamespace(store=store, memory=SimpleNamespace(recall=lambda **kwargs: bundle))
    service = AgentRuntimeMemoryService(runtime)
    monkeypatch.setattr(service, '_proactive_release', lambda *args: {})
    key = 'synthetic-review-key-not-production' * 2
    monkeypatch.setattr(explicit, '_receipt_key_set', lambda: ReceiptKeySet('audit', key, {'audit': key}))
    lanes = []
    def collect(memory, **kwargs):
        lane = raw_citation.RawCitationLane(admission={'status': 'unavailable', 'reason': 'synthetic_no_raw'},
            request={'query_digest': raw_citation.digest(kwargs['query'])})
        lanes.append(lane)
        return lane
    monkeypatch.setattr(raw_citation, 'collect_citation', collect)
    yield SimpleNamespace(service=service, runtime=runtime, bundle=bundle, row=row, lanes=lanes)
    store.close()


@pytest.mark.parametrize('initial', [None, {'decision': {'status': 'related'}}])
def test_fallback_copy_does_not_modify_or_reuse_caller_bundle(delivery, initial):
    d = delivery
    d.bundle.raw_citation = deepcopy(initial)
    before = deepcopy(d.bundle)
    first, first_bundle = d.service._prefetch_result(channel='hermes', scope=SCOPE, query='first', limit=1)
    second, second_bundle = d.service._prefetch_result(channel='hermes', scope=SCOPE, query='second', limit=1)
    assert first_bundle is not d.bundle and second_bundle is not d.bundle
    assert first_bundle.raw_citation is d.lanes[0]
    assert second_bundle.raw_citation is d.lanes[1]
    assert d.lanes[0] is not d.lanes[1]
    assert d.bundle == before
    assert first['bundle']['raw_citation']['request'] != second['bundle']['raw_citation']['request']
    if initial is None:
        other = d.service.prefetch(channel='openclaw', scope=SCOPE, query='other', limit=1)
        assert 'raw_citation_presentation' not in other
        assert 'raw_citation' not in other['bundle']


def explicit_call(d, *, request_id='delivery-review'):
    return d.service.prefetch(channel='hermes', scope=SCOPE, query='Where is the cedar relay spool?', limit=1,
        explicit_request={'session_id': 'review-session', 'request_id': request_id, 'acceptance_generated': False})


@pytest.mark.parametrize('tamper', ['none', 'omit', 'replace', 'extra', 'both', 'remove_lane'])
def test_explicit_capture_reconstructs_presentation_and_rejects_tampering(delivery, monkeypatch, tamper):
    d = delivery
    prefetch = d.service._prefetch_result
    def changed(**kwargs):
        result, bundle = prefetch(**kwargs)
        if tamper == 'omit':
            result.pop('raw_citation_presentation')
        elif tamper == 'replace':
            result['raw_citation_presentation'] = {'status': 'forged'}
        elif tamper == 'extra':
            result['raw_citation_presentation'] = {**result['raw_citation_presentation'], 'forged_proof': True}
        elif tamper == 'both':
            result['raw_citation_presentation'] = {'status': 'forged'}
            result['bundle']['raw_citation']['presentation'] = {'status': 'forged'}
        elif tamper == 'remove_lane':
            result.pop('raw_citation_presentation')
            result['bundle'].pop('raw_citation')
        return result, bundle
    monkeypatch.setattr(d.service, '_prefetch_result', changed)
    result = explicit_call(d)
    capture = explicit.load_explicit_capture(d.runtime, result['capture']['record_id'],
        scope=resolve_channel_scope('hermes', SCOPE))
    assert capture.content['release_reference'] == {}  # isolated, not production-qualified
    assert d.bundle.raw_citation is None
    if tamper == 'none':
        assert result['ok'] is True
        assert result['raw_citation_presentation'] == result['bundle']['raw_citation']['presentation']
        assert [ref['record_ref'] for ref in capture.content['references']] == [d.row.record_id]
        assert capture.content['error'] == ''
    else:
        assert result['ok'] is False and result['error'] == 'ValueError'
        assert capture.content['error'] == 'ValueError'
        assert capture.content['references'] == []


@pytest.mark.parametrize('forged', [False, True])
def test_no_lane_explicit_contract_still_rejects_invented_presentation(delivery, monkeypatch, forged):
    d = delivery
    # Keep the exact record owner authorized for this transport.
    d.bundle.items.clear()
    prefetch = d.service._prefetch_result
    def changed(**kwargs):
        result, bundle = prefetch(**kwargs)
        assert 'raw_citation_presentation' not in result
        if forged:
            result['raw_citation_presentation'] = {'status': 'delivered'}
        return result, bundle
    monkeypatch.setattr(d.service, '_prefetch_result', changed)
    result = d.service.prefetch(channel='codex', scope=SCOPE, query='query', limit=1,
        explicit_request={'session_id': 'codex-session', 'request_id': 'codex-request', 'acceptance_generated': False})
    assert result['ok'] is (not forged)
    if forged:
        assert result['error'] == 'ValueError'
    else:
        assert 'raw_citation_presentation' not in result
