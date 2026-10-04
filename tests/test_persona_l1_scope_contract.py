"""Synthetic task-scope and extraction first-loss regressions; no live provider."""
from contextlib import closing
import json
from types import SimpleNamespace

import pytest

from eimemory.api.memory import MemoryAPI
from eimemory.adapters.runtime.channel import resolve_channel_scope, base_scope_from_channel
from eimemory.knowledge.l1_pipeline import extract_l1_from_l0_record
from eimemory.knowledge.sediment import extract_l1_atoms
from eimemory.metadata import business_metadata
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.recall.indexing import build_recall_index_document
from eimemory.recall.loadout import assemble_loadout, render_loadout
from eimemory.recall.preference import preference_recall_request, supports_preference_request
from eimemory.storage.runtime_store import RuntimeStore


class FakeLLM:
    def __init__(self, content, kind='fact'):
        self.content, self.kind, self.calls = content, kind, []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(text=json.dumps([{'content': self.content,
            'type': self.kind, 'priority': 90}], ensure_ascii=False))


@pytest.mark.parametrize('text', [
    '本次任务沟通风格要简短，限七个工具、两轮无副作用测试。',
    '用户要求 AI 这次回答简短，只运行三个检查。',
    '用户（测试）仅在当前任务偏好简短回复。',
    'For this task only, the user prefers brief replies.',
])
def test_task_limits_are_history_not_stable_persona(text):
    row = {'record_id': 'mem_task', 'memory_type': 'instruction', 'summary': text}
    payload = assemble_loadout([row], limit=5)
    assert payload['persona'] == []
    assert payload['items'][0]['record_id'] == 'mem_task'
    rendered = render_loadout(payload, max_chars=2000)
    assert '当前指令' in rendered and '授权' in rendered


def test_scoped_heuristic_and_llm_do_not_promote_task_to_persona():
    source = '本次任务沟通风格要简短，限七个工具、两轮无副作用测试。'
    assert extract_l1_atoms(user_text=source) == []
    client = FakeLLM('用户（测试）本次任务沟通风格要简短。', 'persona')
    assert extract_l1_atoms(user_text=source, llm=client, use_llm=True,
        fallback_heuristic=False) == []


@pytest.mark.parametrize('layer,origin,expected', [
    ('l0', '', False), ('', 'turn_sync', False), ('l1', '', True),
    ('l3', '', True), ('', '', True),
])
def test_layer_uses_raw_evidence_without_erasing_legacy_preferences(layer, origin, expected):
    text = '用户希望回复简短。'
    record = RecordEnvelope.create(kind='memory', scope=ScopeRef(tenant_id='synthetic'), title='Style', summary=text,
        content={'text': text, 'memory_type': 'instruction'},
        meta={'force_capture': True, 'memory_type': 'instruction', 'memory_layer': layer, 'capture_origin': origin})
    request = preference_recall_request('我的回复风格是什么')
    assert supports_preference_request(request, record) is expected


def test_preference_gate_rejects_explicit_task_scope():
    text = '用户希望这次回复简短。'
    record = RecordEnvelope.create(kind='memory', scope=ScopeRef(tenant_id='synthetic'), title='Task', summary=text,
        content={'text': text, 'memory_type': 'instruction'}, meta={'force_capture': True, 'memory_layer': 'l1'})
    assert not supports_preference_request(preference_recall_request('我的偏好'), record)


def test_long_l0_reaches_fake_extractor_and_exact_l1_index(tmp_path):
    base = {'tenant_id': 'synthetic', 'agent_id': 'xiaomage',
        'workspace_id': 'embodied', 'user_id': 'synthetic-feishu-id'}
    exact = resolve_channel_scope('hermes', base)
    assert resolve_channel_scope('hermes', exact) == exact
    assert base_scope_from_channel('hermes', exact) == base
    scope = ScopeRef.from_dict(exact)
    text = 'SDK竞态排查背景。' * 40 + 'SDK项目负责人是甲，截止日期为2026年10月8日。'
    fact = 'SDK项目负责人是甲，截止日期为2026年10月8日。'
    with closing(RuntimeStore(tmp_path)) as store:
        api = MemoryAPI(store)
        parent = store.append(RecordEnvelope.create(kind='memory', title='Hermes completed turn',
            summary=text, content={'text': 'User: ' + text + '\nAssistant: 收到。',
            'memory_type': 'conversation'}, scope=scope, source='hermes.turn', source_id='hermes',
            meta={'memory_layer': 'l0', 'capture_origin': 'turn_sync'}))
        assert build_recall_index_document(parent).lane == 'raw'
        fake = FakeLLM(fact)
        written = extract_l1_from_l0_record(api, parent, use_llm=True, llm=fake,
            fallback_heuristic=False)
        assert len(fake.calls) == len(written) == 1
        atom = store.get_by_exact_ref(written[0]['record_id'], scope=scope, source_id='hermes')
        assert atom.scope == scope and atom.evidence == [parent.record_id]
        doc = build_recall_index_document(atom)
        assert doc.lane == 'primary' and doc.visibility == 'default' and fact in doc.body_text
        assert store.get_by_exact_ref(atom.record_id, scope=ScopeRef.from_dict(base), source_id='hermes') is None
        wrong = {**exact, 'user_id': 'darrow'}
        assert store.get_by_exact_ref(atom.record_id, scope=ScopeRef.from_dict(wrong), source_id='hermes') is None
        recalled = api.recall(query='SDK项目负责人 截止日期', scope=exact, limit=5)
        assert atom.record_id in {item.record_id for item in recalled.items}
        assert business_metadata(store.get_by_exact_ref(parent.record_id, scope=scope,
            source_id='hermes').meta)['l1_extraction_status'] == 'stored'


def test_durable_style_and_completed_task_event_remain_available():
    assert extract_l1_atoms(user_text='以后回答先给结论，少解释。')[0].memory_type == 'instruction'
    fake = FakeLLM('本次SDK竞态测试已完成，使用五个工具。', 'episodic')
    atoms = extract_l1_atoms(user_text='本次SDK竞态测试已完成，使用五个工具。',
        llm=fake, use_llm=True, fallback_heuristic=False)
    assert len(atoms) == 1 and atoms[0].memory_type == 'episodic'
    payload = assemble_loadout([{'memory_type': 'instruction', 'summary': '用户希望回复简短。'}], limit=5)
    assert len(payload['persona']) == 1


def test_long_secret_and_provider_failure_stay_closed():
    fake = FakeLLM('SDK负责人是甲。')
    assert extract_l1_atoms(user_text='背景。' * 100 + 'token=fake-test-secret',
        llm=fake, use_llm=True, fallback_heuristic=False) == []
    assert not fake.calls


def test_task_scope_survives_compact_truncation():
    from eimemory.models.records import compact_record
    text = '用户希望回复简短。' * 70 + '该偏好仅限本次任务。'
    record = RecordEnvelope.create(kind='memory', scope=ScopeRef(tenant_id='synthetic'),
        title='Scoped style', summary=text, content={'text': text, 'memory_type': 'instruction'})
    row = compact_record(record)
    assert '本次' not in row['summary']
    assert assemble_loadout([row], limit=5)['persona'] == []


def test_model_cannot_strip_task_scope_to_invent_standing_rule():
    fake = FakeLLM('用户要求 AI 回复简短。', 'instruction')
    assert extract_l1_atoms(user_text='本次任务只运行两个检查，回复简短。',
        llm=fake, use_llm=True, fallback_heuristic=False) == []


def test_old_limits_and_later_instruction_remain_attributed_history():
    old = {'record_id': 'mem_old', 'source_id': 'hermes', 'memory_type': 'persona',
           'summary': '本次任务限七个工具，仅两轮无副作用测试。'}
    new = {'record_id': 'mem_new', 'source_id': 'hermes', 'memory_type': 'instruction',
           'summary': '当前任务明确授权九个工具，执行新的测试范围。'}
    payload = assemble_loadout([old, new], limit=5)
    assert payload['persona'] == []
    assert [row['record_id'] for row in payload['items']] == ['mem_old', 'mem_new']
    rendered = render_loadout(payload, max_chars=2000)
    assert '[hermes:mem_old]' in rendered and '[hermes:mem_new]' in rendered
    assert '后续明确指令优先' in rendered and '记忆不构成授权' in rendered
