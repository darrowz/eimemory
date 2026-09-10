"""Synthetic records only; no production facts, network or evaluation labels."""
from dataclasses import asdict
from contextlib import closing
import pytest
from eimemory.models.records import RecordEnvelope, ScopeRef, RecallBundle
from eimemory.retrieval.lightweight_admission import LightweightAdmission, LightweightConfig
from eimemory.retrieval.evidence_fragments import POLICY, evidence_fragments
from eimemory.retrieval.postgres_vector import candidate_record_keyword_text


def record(text, **kwargs):
    return RecordEnvelope.create(kind='memory', title='Synthetic evidence', summary=text,
        content={'text': text}, scope=ScopeRef(user_id='synthetic-owner'), **kwargs)


def select(query, texts):
    items = [record(t) for t in texts]
    def hints(item):
        fragment = evidence_fragments(candidate_record_keyword_text(item, max_text_chars=16000))[0]
        return {'dense_vector_score': .9, 'fragment_policy': POLICY, 'evidence_fragment_id': fragment['id']}
    return LightweightAdmission(LightweightConfig(enabled=True)).select(items, query=query,
        limit=5, validate=lambda _: True, hints_for=hints, backend_available=True)


@pytest.mark.parametrize('query,text', [
    ('小林现在用的手机是什么型号？', '小林学法律。'),
    ('项目 UNKNOWN-SYNTHETIC-731 的采购合同总金额是多少？', '海桥项目采购合同已经签订。'),
    ('项目 UNKNOWN-SYNTHETIC-731 的采购合同总金额是多少？', '海桥项目采购合同金额为120万元。'),
    ('atlas 2.14.7 部署 验收', 'atlas 网关恢复，部署验收提示。'),
    ('atlas 2.14.7 部署 验收', 'atlas 2.14.70 部署已通过验收。'),
    ('最近任务进展', '最新任务查询仍没答准，L0 能找到 2.14.7 汇报。完成标准：选出新证据，目前未完成检索验证。'),
    ('小林的手机是什么型号？', '小林手机型号尚未记录。'),
])
def test_unanswerable_high_similarity_is_no_evidence(query, text):
    selected, report = select(query, [text])
    assert selected == []
    assert report['status'] == 'no_evidence'


@pytest.mark.parametrize('query,text', [
    ('小林现在用的手机是什么型号？', '小林现在使用的手机型号是 Nova Q，设备型号 NQ-42。'),
    ('小林用的是哪款电话？', '小林使用的手机是 Nova Q，型号 NQ-42。'),
    ('海桥项目采购合同总金额是多少？', '海桥项目采购合同金额为120万元。'),
    ('atlas 2.14.7 部署 验收', 'atlas 2.14.7 已部署，验收测试通过。'),
    ('海桥项目采用什么供电方案？', '海桥项目供电采用风光专线直连与市场补电。'),
    ('海桥项目供电是不是双直连？', '海桥项目供电采用专线直连与市场补电，不是双直连。'),
])
def test_paraphrases_and_existing_fact_shapes(query, text):
    selected, report = select(query, [text])
    assert len(selected) == 1
    assert report['status'] == 'evidence_found'


def test_version_must_be_in_selected_fragment_not_elsewhere_in_parent():
    item = record('atlas 网关恢复。' * 100 + '\natlas 2.14.7 已部署，验收通过。')
    fragment = evidence_fragments(candidate_record_keyword_text(item, max_text_chars=16000))[0]
    chosen, _ = LightweightAdmission(LightweightConfig(enabled=True)).select([item],
        query='atlas 2.14.7 部署 验收', limit=1, validate=lambda _: True, backend_available=True,
        hints_for=lambda _: {'dense_vector_score': .99, 'fragment_policy': POLICY,
                             'evidence_fragment_id': fragment['id']})
    assert chosen == []

@pytest.mark.parametrize('query,context,expected', [
    ('海桥项目最近任务进展', {}, 'bridge'),
    ('最近任务进展', {'project': '海桥'}, 'bridge'),
    ('全局最近任务进展', {'project': '海桥'}, 'both'),
    ('最近任务进展', {}, 'ambiguous'),
])
def test_project_context_through_engine(tmp_path, query, context, expected):
    from eimemory.api.memory import MemoryAPI
    from eimemory.storage.runtime_store import RuntimeStore
    from eimemory.retrieval.engine import GovernedRecallEngine
    from test_recall_budget_reserve import fragment_source
    with closing(RuntimeStore(tmp_path)) as store:
        bridge = record('海桥项目任务进展：部署已完成，目前待验收。', meta={'memory_type': 'conversation'})
        other = record('山岭项目任务进展：采购已完成，目前待验收。', meta={'memory_type': 'conversation'})
        for item in [bridge, other]:
            store.append(item)
        engine = GovernedRecallEngine(store=store, candidate_source=fragment_source(store))
        engine.relevance_admission = LightweightAdmission(LightweightConfig(enabled=True))
        bundle = MemoryAPI(store, recall_engine=engine).recall(query=query, scope=asdict(bridge.scope),
            task_context={'exact_scope_only': True, **context}, limit=5)
        if context.get('project') and expected != 'both':
            assert all('海桥' in request.query for request in engine.candidate_source.repository.requests)
        compact = bundle.to_compact_dict()
        if expected == 'ambiguous':
            assert not bundle.items
            assert compact['retrieval_status'] == 'ambiguous'
        else:
            assert {i.record_id for i in bundle.items} == ({bridge.record_id} if expected == 'bridge'
                                                         else {bridge.record_id, other.record_id})
            assert compact['task_evidence_scope'] == 'historical_only_latest_state_unverified'


def test_compact_carries_selected_original_fragment_and_source():
    item = record('恢复提示。' * 150 + '\natlas 2.14.7 已部署，验收通过。', source_id='synthetic-source')
    fragments = evidence_fragments(candidate_record_keyword_text(item, max_text_chars=16000))
    fragment = next(f for f in fragments if '2.14.7' in f['text'])
    chosen, report = LightweightAdmission(LightweightConfig(enabled=True)).select([item],
        query='atlas 2.14.7 部署 验收', limit=1, validate=lambda _: True, backend_available=True,
        hints_for=lambda _: {'dense_vector_score': .99, 'fragment_policy': POLICY,
                             'evidence_fragment_id': fragment['id']})
    payload = RecallBundle(chosen, [], [], .9, '', {'relevance_selector': report}).to_compact_dict(limit=1)
    assert payload['items'][0]['record_id'] == item.record_id
    assert payload['items'][0]['source_id'] == 'synthetic-source'
    assert '2.14.7' in payload['items'][0]['evidence_excerpt']
    assert payload['items'][0]['evidence_fragment_id'] == fragment['id']


def test_device_fact_extraction_preserves_source_without_named_user(tmp_path):
    from eimemory.knowledge.sediment import extract_l1_atoms
    from eimemory.knowledge.l1_pipeline import persist_l1_atoms
    from eimemory.api.memory import MemoryAPI
    from eimemory.storage.runtime_store import RuntimeStore
    from eimemory.retrieval.engine import GovernedRecallEngine
    from test_recall_budget_reserve import fragment_source
    text = '我现在使用的手机型号是 Nova Q，设备型号 NQ-42。'
    atoms = extract_l1_atoms(user_text=text, source_message_ids=['synthetic-episode'])
    assert len(atoms) == 1
    assert atoms[0].source_message_ids == ('synthetic-episode',)
    with closing(RuntimeStore(tmp_path)) as store:
        memory = MemoryAPI(store)
        written = persist_l1_atoms(memory, atoms=atoms, episode_id='synthetic-episode',
            scope={'user_id': 'synthetic-owner'}, channel_id='synthetic-source')
        engine = GovernedRecallEngine(store=store, candidate_source=fragment_source(store))
        engine.relevance_admission = LightweightAdmission(LightweightConfig(enabled=True))
        bundle = MemoryAPI(store, recall_engine=engine).recall(query='我的手机是什么型号？',
            scope={'user_id': 'synthetic-owner'}, task_context={'exact_scope_only': True}, limit=1)
        assert [i.record_id for i in bundle.items] == [written[0]['record_id']]
        assert bundle.items[0].evidence == ['synthetic-episode']
        assert bundle.items[0].links[0].target_id == 'synthetic-episode'


def test_latest_uses_event_time_after_answerability_not_write_time():
    items = [record('海桥项目任务进展：部署已完成，目前待验收。') for _ in range(2)]
    items[0].record_id, items[1].record_id = 'mem_a', 'mem_z'
    items[0].time.occurred_at = '2025-01-01T00:00:00Z'
    items[0].time.updated_at = '2026-09-10T00:00:00Z'
    items[1].time.occurred_at = '2026-08-01T00:00:00Z'
    items[1].time.updated_at = '2026-08-01T00:00:00Z'
    def hints(item):
        f = evidence_fragments(candidate_record_keyword_text(item, max_text_chars=16000))[0]
        return {'dense_vector_score': .9, 'fragment_policy': POLICY, 'evidence_fragment_id': f['id']}
    selected, _ = LightweightAdmission(LightweightConfig(enabled=True)).select(items,
        query='海桥项目最近任务进展', limit=1, validate=lambda _: True, backend_available=True, hints_for=hints)
    assert selected == [items[1]]

@pytest.mark.parametrize('query,text', [
    ('小林现在用的手机是什么型号？', '小周现在使用的手机型号是 Nova Q，设备型号 NQ-42。'),
    ('海桥项目采购合同总金额是多少？', '海桥项目采购合同已经签订。山岭项目合同金额是120万元。'),
    ('atlas 2.14.7 部署 验收', 'boreal 2.14.7 已部署，验收通过。'),
])
def test_object_and_property_must_be_supported_together(query, text):
    assert not select(query, [text])[0]


def test_authoritative_chinese_alias_is_valid():
    item = record('海桥工程采购合同金额为120万元。', aliases=['海桥'])
    f = evidence_fragments(candidate_record_keyword_text(item, max_text_chars=16000))[0]
    chosen, _ = LightweightAdmission(LightweightConfig(enabled=True)).select([item],
        query='海桥项目采购合同总金额是多少？', limit=1, validate=lambda _: True, backend_available=True,
        hints_for=lambda _: {'dense_vector_score': .9, 'fragment_policy': POLICY, 'evidence_fragment_id': f['id']})
    assert chosen == [item]


@pytest.mark.parametrize('query', ['上次我授权了什么任务？', 'What is the latest task status?'])
def test_unscoped_last_tasks_are_ambiguous(query):
    from eimemory.recall.task_queries import task_project_scope
    assert task_project_scope(query, {}) == ('ambiguous', '')


def test_rendered_loadout_uses_admitted_span_and_historical_boundary():
    from eimemory.recall.loadout import render_loadout
    payload = {'items': [{'record_id': 'mem_synthetic', 'source_id': 'synthetic-source',
        'summary': '网关恢复提示', 'evidence_excerpt': 'atlas 2.14.7 已部署，待验收。'}],
        'task_evidence_scope': 'historical_only_latest_state_unverified'}
    rendered = render_loadout(payload, max_chars=2000)
    assert '2.14.7' in rendered and 'mem_synthetic' in rendered
    assert '历史' in rendered and '台账' in rendered


def test_rendered_ambiguity_requests_host_clarification():
    from eimemory.recall.loadout import render_loadout
    assert '项目' in render_loadout({'items': [], 'retrieval_status': 'ambiguous'}, max_chars=2000)


def test_question_title_is_not_an_identity_escape_from_property_check():
    item = record('手机型号尚未记录。')
    item.title = '我的手机是什么型号？'
    chosen, _ = LightweightAdmission(LightweightConfig(enabled=True)).select([item],
        query=item.title, limit=1, validate=lambda _: True, backend_available=True)
    assert not chosen


def test_named_software_context_is_explicit():
    from eimemory.recall.task_queries import task_project_scope
    assert task_project_scope('atlas 最近任务进展', {'project': 'boreal'}) == ('project', 'atlas')


def test_chinese_only_device_model_paraphrase():
    assert len(select('小林用的是哪款电话？', ['小林使用的手机是华为畅享。'])[0]) == 1


def test_l0_adapter_keeps_version_fragment_from_authorized_parent(tmp_path):
    from eimemory.api.runtime import Runtime
    from eimemory.adapters.eibrain.rpc import EIBrainRPCBridge
    from eimemory.adapters.runtime.channel import resolve_channel_scope
    from test_recall_budget_reserve import fragment_source
    with closing(Runtime.create(root=tmp_path)) as runtime:
        scope = resolve_channel_scope('hermes', {'user_id': 'synthetic-owner'})
        good = record('网关恢复提示。' * 150 + '\natlas 2.14.7 已部署，验收测试通过。',
            source_id='hermes', meta={'memory_type': 'conversation'})
        good.scope = ScopeRef.from_dict(scope)
        bad = record('atlas 网关恢复，旧部署提示。', source_id='hermes', meta={'memory_type': 'conversation'})
        bad.scope = good.scope
        for item in [good, bad]:
            runtime.store.append(item)
        runtime.memory.recall_engine.candidate_source = fragment_source(runtime.store,
            select_fragment=lambda request, fragments: next((f for f in fragments if '2.14.7' in f['text']), fragments[0]))
        runtime.memory.recall_engine.relevance_admission = LightweightAdmission(LightweightConfig(enabled=True))
        output = EIBrainRPCBridge(runtime).handle({'method': 'adapter.search_l0', 'params': {
            'channel': 'hermes', 'scope': scope, 'query': 'atlas 2.14.7 部署 验收', 'limit': 1}})
        assert output['ok'], output
        items = output['result']['bundle']['items']
        assert [item['record_id'] for item in items] == [good.record_id]
        assert items[0]['source_id'] == good.source_id
        assert '2.14.7' in items[0]['evidence_excerpt']


def test_l0_accepts_inherited_project_context(tmp_path):
    from eimemory.api.runtime import Runtime
    from eimemory.adapters.eibrain.rpc import EIBrainRPCBridge
    from eimemory.adapters.runtime.channel import resolve_channel_scope
    from test_recall_budget_reserve import fragment_source
    with closing(Runtime.create(root=tmp_path)) as runtime:
        scope = resolve_channel_scope('hermes', {'user_id': 'synthetic-owner'})
        item = record('海桥项目任务进展：部署已完成，目前待验收。', source_id='hermes',
            meta={'memory_type': 'conversation'})
        item.scope = ScopeRef.from_dict(scope)
        runtime.store.append(item)
        runtime.memory.recall_engine.candidate_source = fragment_source(runtime.store)
        runtime.memory.recall_engine.relevance_admission = LightweightAdmission(LightweightConfig(enabled=True))
        output = EIBrainRPCBridge(runtime).handle({'method': 'adapter.search_l0', 'params': {
            'channel': 'hermes', 'scope': scope, 'query': '最近任务进展',
            'task_context': {'project': '海桥'}, 'limit': 1}})
        assert output['ok'], output
        assert [r['record_id'] for r in output['result']['bundle']['items']] == [item.record_id]

@pytest.mark.parametrize('text', ['小林的手机电量是80%。', 'The phone battery is at 80 percent.',
                                 '小林的手机是坏的。'])
def test_device_attribute_is_not_any_device_statement(text):
    from eimemory.retrieval.answer_requirements import supports_requested_attribute
    assert not supports_requested_attribute('model_phone', text)
    assert not select('手机是什么型号？', [text])[0]


def test_capture_completed_turn_title_is_not_task_completion():
    item = record('最新任务查询仍没答准，L0 能找到 2.14.7 汇报。完成标准：应选出正确的新证据。',
        meta={'memory_type': 'conversation'})
    item.title = 'Synthetic completed turn'
    fragment = evidence_fragments(candidate_record_keyword_text(item, max_text_chars=16000))[0]
    chosen, _ = LightweightAdmission(LightweightConfig(enabled=True)).select([item],
        query='最近任务进展', limit=1, validate=lambda _: True, backend_available=True,
        hints_for=lambda _: {'dense_vector_score': .9, 'fragment_policy': POLICY, 'evidence_fragment_id': fragment['id']})
    assert not chosen


def test_new_device_extraction_does_not_retain_device_identifiers():
    from eimemory.knowledge.sediment import extract_l1_atoms
    # Placeholder only, never a real device identifier.
    assert extract_l1_atoms(user_text='我现在使用的手机型号是 Nova Q，IMEI: <synthetic-placeholder>。') == []

@pytest.mark.parametrize("text,expected", [
    ("ORION 项目预付款10万元，合同总金额未知。", False),
    ("ORION 项目预算10万元，采购合同总金额尚未确定。", False),
    ("ORION 项目合同总金额不是10万元。", False),
    ("ORION 项目合同分期付款10万元。", False),
    ("ORION 项目合同总金额为120万元，预付款10万元。", True),
    ("ORION 项目预付款10万元，合同总金额为120万元。", True),
    ("ORION 项目采购合同已签订。该合同总价为人民币120万元。", True),
    ("ORION 项目采购合同已签订。另一项目合同总价为120万元。", False),
    ("ORION 项目合同总金额未知，预算为120万元。", False),
    ("ORION 项目合同总金额为 USD 120000。", True),
])
def test_contract_requested_monetary_role(text, expected):
    from eimemory.retrieval.answer_requirements import supports_answer_requirements
    assert supports_answer_requirements("项目 ORION 的采购合同总金额是多少？", text) is expected


def test_captured_image_device_report_extracts_attributed_episode(tmp_path):
    from eimemory.knowledge.l1_pipeline import extract_l1_from_l0_record
    from eimemory.api.memory import MemoryAPI
    from eimemory.storage.runtime_store import RuntimeStore
    text = ("Capture completed turn\nUser: [1 image] What do you see in this image?\n"
            "[Image attached at: /tmp/example.jpg]\nAssistant: 小林，这是你的手机关于本机页面，配置确认了：\n"
            "- **机型：Nova Q**\n- **型号代码：NQ-42**\n- **运行内存：8 GB**")
    item = record(text, source_id="synthetic-source", meta={"memory_type": "conversation"})
    item.time.occurred_at = "2025-02-03T04:05:06Z"
    with closing(RuntimeStore(tmp_path)) as store:
        store.append(item)
        written = extract_l1_from_l0_record(MemoryAPI(store), item)
        assert len(written) == 1
        atom = store.get_by_id(written[0]["record_id"], scope=item.scope)
        assert "助手根据图片描述" in atom.summary
        assert "小林" in atom.summary and "NQ-42" in atom.summary
        assert "用户（鸿哥）" not in atom.summary
        assert atom.time.occurred_at == item.time.occurred_at
        assert atom.evidence == [item.record_id]
        assert atom.links[0].target_id == item.record_id
        assert extract_l1_from_l0_record(MemoryAPI(store), item) == []

@pytest.mark.parametrize("assistant", [
    "小林，这是你的手机关于本机页面，型号未知。",
    "推荐手机：机型 Nova Q，型号代码 NQ-42。",
    "小周，这是别人的手机关于本机页面。机型：Nova Q",
])
def test_image_device_extraction_does_not_infer_ownership(assistant):
    from eimemory.knowledge.sediment import extract_l1_atoms
    assert extract_l1_atoms(user_text="[1 image] What do you see in this image?", assistant_text=assistant) == []


def test_attributed_device_model_is_retrievable():
    from eimemory.retrieval.answer_requirements import supports_answer_requirements
    assert supports_answer_requirements("小林的手机是什么型号？", "助手根据图片描述：小林的手机机型：Nova Q，型号代码：NQ-42。")


def test_capture_projection_repetition_does_not_duplicate_assistant_fields():
    from eimemory.knowledge.sediment import extract_l1_atoms
    turn = "User: [1 image] What do you see in this image?\nAssistant: 小林，这是你的手机关于本机页面。\n- 机型：Nova Q\n- 型号代码：NQ-42"
    atoms = extract_l1_atoms(turn_text=turn+"\n"+turn)
    assert len(atoms) == 1
    assert atoms[0].text.count("NQ-42") == 1

@pytest.mark.parametrize("text", [
    "atlas L0 找到了 2.14.7 汇报，召回测试通过。",
    "正式验收仍被阻塞。上一轮报告显示，后续观察期未完成。完成标准：先核查并补齐样本。",
])
def test_review_discussion_is_not_release_execution(text):
    from eimemory.retrieval.answer_requirements import supports_answer_requirements
    query = "atlas 2.14.7 部署 验收" if "atlas" in text else "全局最近任务进展"
    assert not supports_answer_requirements(query, text)
