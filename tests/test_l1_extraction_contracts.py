"""Synthetic L1 schema, provenance, and edit contracts. No provider/user data."""
from contextlib import closing
from dataclasses import asdict
import json
from types import SimpleNamespace

import pytest

from eimemory.api.memory import MemoryAPI
from eimemory.knowledge.l1_pipeline import edit_l1_atom, extract_l1_from_l0_record
from eimemory.knowledge.l1_queue import L1ExtractQueue
from eimemory.knowledge.sediment import L1ExtractorUnavailable, extract_l1_atoms
from eimemory.metadata import business_metadata, runtime_metadata
from eimemory.models.records import LinkRef, RecordEnvelope, ScopeRef
from eimemory.recall.indexing import is_inactive_or_superseded_record
from eimemory.storage.runtime_store import RuntimeStore

SCOPE = ScopeRef(tenant_id='synthetic', agent_id='audit', workspace_id='isolated', user_id='owner')
TEXT = 'Synthetic project Falcon account belongs to Alice.'

class FakeLLM:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []
    def complete(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(text=json.dumps(self.payload, ensure_ascii=False))

def fact(**extra):
    return {'content': TEXT, 'type': 'fact', 'priority': 90, **extra}

def capture(store):
    text = 'User: ' + TEXT + '\nAssistant: Acknowledged.'
    record = RecordEnvelope.create(kind='memory', title='Synthetic L0', summary=text,
        content={'text': text, 'memory_type': 'conversation'}, scope=SCOPE,
        source='synthetic.turn', source_id='synthetic',
        meta={'memory_type': 'conversation', 'memory_layer': 'l0', 'capture_origin': 'turn_sync'})
    return store.append(record)

@pytest.fixture(autouse=True)
def no_real_provider(monkeypatch):
    from eimemory.llm.command_client import CommandLLMClient
    def prohibited(*args, **kwargs):
        raise AssertionError('Real LLM execution prohibited')
    monkeypatch.setattr(CommandLLMClient, 'complete', prohibited)

@pytest.mark.parametrize('payload', [None, 7, 'provider error', {'error': 'rate limited'}, [7],
    [{'nonsense': 1}], {'memories': {}}, [{'memories': [None]}], [fact(type='unknown')],
    [fact(content=12)], [fact(priority='90')], [fact(priority=True)], [fact(priority=float('nan'))],
    [fact(priority=float('inf'))], [fact(priority=-1)], [fact(priority=101)],
    {'memories': [], 'items': [fact()]}])
def test_invalid_schema_is_retryable_not_no_memory(tmp_path, payload):
    with closing(RuntimeStore(tmp_path)) as store:
        api, parent = MemoryAPI(store), capture(store)
        queue = L1ExtractQueue(tmp_path/'queue.json')
        queue.enqueue({'episode_id': parent.record_id})
        bad = FakeLLM(payload)
        report = queue.drain_report(lambda job: extract_l1_from_l0_record(api, parent,
            use_llm=True, llm=bad, fallback_heuristic=False), limit=1)
        assert report['processed'] == 0 and report['failed'] == report['pending'] == 1
        current = store.get_by_exact_ref(parent.record_id, scope=SCOPE, source_id='synthetic')
        assert not business_metadata(current.meta).get('l1_extracted_at')
        good = FakeLLM([fact()])
        report = queue.drain_report(lambda job: extract_l1_from_l0_record(api, parent,
            use_llm=True, llm=good, fallback_heuristic=False), limit=1)
        assert report['processed'] == 1 and report['pending'] == 0
        current = store.get_by_exact_ref(parent.record_id, scope=SCOPE, source_id='synthetic')
        assert business_metadata(current.meta)['l1_extraction_status'] == 'stored'

@pytest.mark.parametrize('payload', [[], {'memories': []}, {'items': []}, {'scenes': []},
    [{'scene_name': 'no durable facts', 'message_ids': [], 'memories': []}], [fact(priority=1)],
    [fact(content='What is this project?', priority=90)]])
def test_valid_empty_or_filtered_output_is_authoritative(tmp_path, payload):
    with closing(RuntimeStore(tmp_path)) as store:
        api, parent = MemoryAPI(store), capture(store)
        client = FakeLLM(payload)
        assert extract_l1_from_l0_record(api, parent, use_llm=True, llm=client, fallback_heuristic=False) == []
        current = store.get_by_exact_ref(parent.record_id, scope=SCOPE, source_id='synthetic')
        assert business_metadata(current.meta)['l1_extraction_status'] == 'no_memory'
        assert extract_l1_from_l0_record(api, parent, use_llm=True, llm=client, fallback_heuristic=False) == []
        assert len(client.calls) == 1

@pytest.mark.parametrize('ids', ['mem_parent', None, ['mem_unrelated'], [1], [''], ['mem_parent', 'mem_unrelated']])
def test_model_cannot_invent_or_mistype_source_ids(ids):
    with pytest.raises(L1ExtractorUnavailable, match='invalid_output'):
        extract_l1_atoms(user_text=TEXT, source_message_ids=['mem_parent'], use_llm=True,
            llm=FakeLLM([fact(source_message_ids=ids)]), fallback_heuristic=False)

@pytest.mark.parametrize('fields', [{}, {'source_message_ids': []}, {'source_message_ids': ['mem_parent','mem_parent']}])
def test_model_provenance_is_bound_to_supplied_sources(fields):
    client = FakeLLM([fact(**fields)])
    atoms = extract_l1_atoms(user_text=TEXT, source_message_ids=['mem_parent'], use_llm=True,
        llm=client, fallback_heuristic=False)
    assert atoms[0].source_message_ids == ('mem_parent',)
    assert '["mem_parent"]' in client.calls[0]['user_prompt']


def test_no_source_input_never_allows_fabricated_provenance():
    atoms = extract_l1_atoms(user_text=TEXT, use_llm=True, llm=FakeLLM([fact()]), fallback_heuristic=False)
    assert atoms[0].source_message_ids == ()
    with pytest.raises(L1ExtractorUnavailable):
        extract_l1_atoms(user_text=TEXT, use_llm=True,
            llm=FakeLLM([fact(source_message_ids=['mem_invented'])]), fallback_heuristic=False)

@pytest.mark.parametrize('memory_type', ['fact','instruction','persona','episodic'])
def test_active_edit_retires_predecessor_and_keeps_provenance(tmp_path, memory_type):
    with closing(RuntimeStore(tmp_path)) as store:
        api = MemoryAPI(store)
        old = api.ingest(text=TEXT, memory_type=memory_type, title='Synthetic fact', scope=asdict(SCOPE),
            source='synthetic.l1', source_id='synthetic', force_capture=True,
            evidence=['mem_source'], links=[LinkRef(relation='derived_from', target_kind='memory', target_id='mem_source')],
            meta={'memory_layer':'l1', 'runtime_meta':{'host_id':'synthetic-host'},
                  'business_meta':{'source_message_ids':['mem_source'], 'removed_by':'obsolete-marker'}})
        new = edit_l1_atom(api, record_id=old.record_id, scope=SCOPE, text='Synthetic project Falcon account belongs to Bob.')
        prior = store.get_by_exact_ref(old.record_id, scope=SCOPE, source_id='synthetic')
        assert prior.status == 'superseded' and business_metadata(prior.meta)['superseded_by'] == new.record_id
        assert new.status == 'active' and not is_inactive_or_superseded_record(new)
        assert new.evidence == ['mem_source']
        assert any(link.relation=='derived_from' and link.target_id=='mem_source' for link in new.links)
        assert any(link.relation=='supersedes' and link.target_id==old.record_id for link in new.links)
        assert runtime_metadata(new.meta)['host_id'] == 'synthetic-host'
        for key in ('superseded_by','mutation_state','removed_by'):
            assert key not in business_metadata(new.meta)
        assert business_metadata(new.meta)['supersedes'] == [old.record_id]
        before = prior.to_dict()
        with pytest.raises(ValueError, match='l1_atom_inactive'):
            edit_l1_atom(api, record_id=old.record_id, scope=SCOPE, text='Synthetic project Falcon account belongs to Carol.')
        assert store.get_by_exact_ref(old.record_id, scope=SCOPE, source_id='synthetic').to_dict() == before
        assert store.get_by_exact_ref(new.record_id, scope=SCOPE, source_id='synthetic').status == 'active'
        assert len(store.list_records(kinds=['memory'], scope=SCOPE, limit=10)) == 2


def test_nested_inactive_marker_cannot_be_reactivated_by_edit(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        api = MemoryAPI(store)
        old = api.ingest(text=TEXT, memory_type='fact', title='Synthetic fact', scope=asdict(SCOPE),
            source='synthetic.l1', source_id='synthetic', force_capture=True,
            meta={'memory_layer':'l1','business_meta':{'superseded_by':'mem_successor'}})
        assert old.status == 'active' and is_inactive_or_superseded_record(old)
        with pytest.raises(ValueError, match='l1_atom_inactive'):
            edit_l1_atom(api, record_id=old.record_id, scope=SCOPE, text='Correction: Bob.')
