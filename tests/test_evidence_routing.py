"""Local selection and isolated verifier contracts; NOT real model quality validation."""
import json
from types import SimpleNamespace
from time import perf_counter
import pytest
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.retrieval.engine import GovernedRecallEngine
from eimemory.retrieval import caller_assistance as assistance


def record(text='Read the entire document before evaluating its claims.'):
    return RecordEnvelope.create(kind='memory', title='Saved instruction', summary=text,
                                 content={}, scope=ScopeRef())


def engine_fixture(items):
    # Selector contracts still run the real authority decorator; explicitly
    # provide its before/after storage reads instead of an uninitialized store.
    engine = GovernedRecallEngine.__new__(GovernedRecallEngine)
    engine.relevance_admission = None
    engine._relevance_selector_policy_version = 'test'
    engine._hydrate_records_batch = lambda rows, **_: {
        engine._record_key(row): row for row in items if row in rows
    }
    return engine


def select(monkeypatch, payload, *, allowed=True, deadline=0, enabled=True, query='How should I assess this material?', text=None, cosine=.6792134063480267):
    item = record(text) if text is not None else record()
    engine = engine_fixture([item])
    calls = []
    def complete(**kwargs):
        calls.append(json.loads(kwargs['user_prompt']))
        if isinstance(payload, Exception):
            raise payload
        return SimpleNamespace(text=json.dumps(payload))
    monkeypatch.setenv('EIMEMORY_CALLER_ASSISTED_RECALL_ENABLED', str(int(enabled)))
    monkeypatch.setattr(assistance, 'configured_client', lambda: SimpleNamespace(timeout_seconds=9, complete=complete))
    result, report = engine._select_post_fusion_items([item], query=query, limit=1,
        fusion_state={}, component_hints_by_ref={engine._record_key(item): {'dense_vector_score': cosine}},
        validate=lambda _: allowed, deadline_at=deadline)
    return item, result, report, calls


@pytest.mark.parametrize('query,expected', [
    ('How should I assess this material?', False),
    ('Read the entire document', True),
])
def test_dense_signal_requires_local_evidence_without_verifier(monkeypatch, query, expected):
    item, result, report, calls = select(monkeypatch,
        {'selected':[{'id':'0','quote':'Read the entire document'}]}, query=query)
    assert result == ([item] if expected else [])
    assert calls == []
    assert report['status'] == ('evidence_found' if expected else 'no_evidence')
    assert report['collection_complete'] is True
    assert 'caller_assistance' not in report


@pytest.mark.parametrize('payload', [
    {'selected':[]},
    {'selected':[{'id':'99','quote':'Read the entire document'}]},
    {'selected':[{'id':'0','quote':'fabricated quote'}]},
    TimeoutError(),
])
@pytest.mark.parametrize('query,expected', [
    ('How should I assess this material?', False), ('Read the entire document', True),
])
def test_model_payload_cannot_admit_or_veto_local_evidence(monkeypatch, payload, query, expected):
    item, result, report, calls = select(monkeypatch, payload, query=query)
    assert result == ([item] if expected else [])
    assert calls == []
    assert report['status'] == ('evidence_found' if expected else 'no_evidence')
    assert 'caller_assistance' not in report


@pytest.mark.parametrize('kwargs,status', [
    ({'allowed':False}, 'unavailable'),
    ({'deadline':perf_counter()-1}, 'unavailable'),
    ({'enabled':False}, 'evidence_found'),
])
def test_authority_and_deadline_still_gate_local_evidence(monkeypatch, kwargs, status):
    item, result, report, calls = select(monkeypatch, {'selected':[]},
        query='Read the entire document', **kwargs)
    assert result == ([item] if status == 'evidence_found' else [])
    assert calls == [] and report['status'] == status
    assert report['collection_complete'] is (status == 'evidence_found')


@pytest.mark.parametrize('payload,outcome', [
    ({'selected': []}, 'no_support'),
    ({'selected': [{'id': '99', 'quote': 'Read the entire document'}]}, 'unavailable'),
    ({'selected': [{'id': '0', 'quote': 'fabricated quote'}]}, 'unavailable'),
    (TimeoutError(), 'unavailable'),
])
def test_isolated_verifier_still_rejects_invalid_support(monkeypatch, payload, outcome):
    calls = []
    def complete(**kwargs):
        calls.append(json.loads(kwargs['user_prompt']))
        if isinstance(payload, Exception):
            raise payload
        return SimpleNamespace(text=json.dumps(payload))
    monkeypatch.setattr(assistance, 'configured_client', lambda: SimpleNamespace(timeout_seconds=9, complete=complete))
    items = [record() for _ in range(12)]
    chosen, report = assistance.verify_candidates(query='How to assess?',
        candidates=[(item, item.summary) for item in items], limit=1)
    assert chosen == [] and report['outcome'] == outcome
    assert len(calls) == 1 and len(calls[0]['candidates']) == 8


def test_quote_must_satisfy_requested_attribute(monkeypatch):
    item = record('Atlas项目合同已签署，金额尚未确定。')
    monkeypatch.setattr(assistance, 'configured_client', lambda: SimpleNamespace(timeout_seconds=9,
        complete=lambda **_: SimpleNamespace(text=json.dumps({'selected':[{'id':'0','quote':item.summary}]}))))
    result, report = assistance.verify_candidates(query='Atlas项目合同总金额是多少', candidates=[(item,item.summary)], limit=1)
    assert result == [] and report['outcome'] == 'no_support'


def test_quote_is_bound_to_selected_candidate(monkeypatch):
    first, second = record('Read the entire document.'), record('Evaluate the complete article.')
    monkeypatch.setattr(assistance, 'configured_client', lambda: SimpleNamespace(timeout_seconds=9,
        complete=lambda **_: SimpleNamespace(text=json.dumps({'selected':[{'id':'0','quote':second.summary}]}))))
    chosen, report = assistance.verify_candidates(query='How to evaluate?', candidates=[(first, first.summary), (second, second.summary)], limit=1)
    assert chosen == [] and report['outcome'] == 'unavailable'


def test_missing_configured_client_is_unavailable(monkeypatch):
    monkeypatch.setattr(assistance, 'configured_client', lambda: None)
    chosen, report = assistance.verify_candidates(query='How to evaluate?', candidates=[(record(), 'Read the entire document.')], limit=1)
    assert chosen == [] and report['outcome'] == 'unavailable' and report['calls'] == 0


def test_lightweight_low_evidence_is_not_rescued_by_verifier(monkeypatch):
    from eimemory.retrieval.lightweight_admission import LightweightAdmission, LightweightConfig
    from eimemory.retrieval.evidence_fragments import evidence_fragments, POLICY
    from eimemory.retrieval.postgres_vector import candidate_record_keyword_text
    item = record()
    text = candidate_record_keyword_text(item, max_text_chars=16000)
    fragment = next(f for f in evidence_fragments(text) if 'Read the entire' in f['text'])
    calls = []
    def complete(**kwargs):
        calls.append(json.loads(kwargs['user_prompt']))
        return SimpleNamespace(text=json.dumps({'selected':[{'id':'0','quote':'Read the entire document'}]}))
    monkeypatch.setenv('EIMEMORY_CALLER_ASSISTED_RECALL_ENABLED', '1')
    monkeypatch.setattr(assistance, 'configured_client', lambda: SimpleNamespace(timeout_seconds=9, complete=complete))
    chosen, report = LightweightAdmission(LightweightConfig(enabled=True)).select([item],
        query='如何判断这些材料？', limit=1, validate=lambda _: True, backend_available=True,
        hints_for=lambda _: {'dense_vector_score':.3, 'fragment_policy':POLICY, 'evidence_fragment_id':fragment['id']})
    assert chosen == [] and calls == []
    assert report['status'] == 'no_evidence'
    assert report['quality_evaluation'] == 'post_delivery'
    assert report['dropped_reasons'] == {'insufficient_evidence': 1}
    assert not report['scored'][0]['admitted']


def test_default_identity_fast_path_does_not_call(monkeypatch):
    item = record()
    engine = engine_fixture([item])
    key = engine._record_key(item)
    monkeypatch.setenv('EIMEMORY_CALLER_ASSISTED_RECALL_ENABLED', '1')
    monkeypatch.setattr(assistance, 'configured_client', lambda: pytest.fail('identity must not call'))
    selected, _ = engine._select_post_fusion_items([item], query=item.title, limit=1,
        fusion_state={'evidence_by_ref':{key:{'exact_title'}}, 'authorized_exact_refs': {key}}, component_hints_by_ref={}, exact_scope_strategy=True)
    assert selected == [item]


def test_disabled_verifier_leaves_local_absence_without_semantic_verdict(monkeypatch):
    _, selected, report, calls = select(monkeypatch, {'selected':[]}, enabled=False)
    assert not selected and not calls
    assert report['status'] == 'no_evidence'
    assert report['collection_complete'] is True
    assert 'caller_assistance' not in report


@pytest.mark.parametrize('mutation', ['valid', 'revoked', 'changed', 'missing'])
def test_final_authority_read_checks_actual_selected_records(monkeypatch, mutation):
    from copy import deepcopy
    items = [record() for _ in range(12)]
    engine = engine_fixture(items)
    reads = []
    def hydrate(rows, **_):
        reads.append(list(rows))
        current = {engine._record_key(row): deepcopy(row) for row in rows}
        if len(reads) == 2 and mutation != 'valid':
            if mutation == 'missing':
                return {}
            for row in current.values():
                if mutation == 'revoked':
                    row.status = 'revoked'
                else:
                    row.summary = 'changed after selection'
        return current
    engine._hydrate_records_batch = hydrate
    monkeypatch.setattr(assistance, 'configured_client', lambda: pytest.fail('synchronous verifier'))
    chosen, report = engine._select_post_fusion_items(items, query='Read the entire document', limit=1,
        fusion_state={}, component_hints_by_ref={}, validate=lambda _: True)
    assert chosen == ([items[0]] if mutation == 'valid' else [])
    assert reads == [items, [items[0]]], 'fresh authority read after bounded selection'
    assert report['status'] == ('evidence_found' if mutation == 'valid' else 'unavailable')
    assert report['collection_complete'] is (mutation == 'valid')
    assert report['dropped_reasons'].get('authority_changed', 0) == (0 if mutation == 'valid' else 1)
    assert 'caller_assistance' not in report


# Recorded host BGE outputs, rows/columns from probe_host.py (2026-09-20).
# Selections below are controlled contracts, NOT model-quality measurements.
@pytest.mark.parametrize("query,text,cosine,supported", [('抖音链接应该怎么处理', '抖音短链不要停在 ies 分享壳或 curl：先解析 aweme_id，再用浏览器打开作品页面，等待作品标题再抽文案。鸿哥发抖音默认要评估。', 0.6792134063480267, True), ('重启之后应该先做什么', '网关重启或会话恢复后，必须先只读复核前序已授权任务（live 身份、收据、健康、未完成项），再问下一步；不重复执行已发生的部署或重启。', 0.6322752188906197, True), ('收到抖音分享地址后该如何提取文案？', '抖音短链不要停在 ies 分享壳或 curl：先解析 aweme_id，再用浏览器打开作品页面，等待作品标题再抽文案。鸿哥发抖音默认要评估。', 0.674291007995536, True), ('会话恢复时如何检查之前授权的工作？', '网关重启或会话恢复后，必须先只读复核前序已授权任务（live 身份、收据、健康、未完成项），再问下一步；不重复执行已发生的部署或重启。', 0.6891605477110474, True), ('用户的火星基地门禁口令是什么', '网关重启或会话恢复后，必须先只读复核前序已授权任务（live 身份、收据、健康、未完成项），再问下一步；不重复执行已发生的部署或重启。', 0.4889878797402052, False), ('用户的月球仓库门禁口令是什么', '网关重启或会话恢复后，必须先只读复核前序已授权任务（live 身份、收据、健康、未完成项），再问下一步；不重复执行已发生的部署或重启。', 0.4724449959515184, False), ('抖音账号的登录口令是什么', '抖音短链不要停在 ies 分享壳或 curl：先解析 aweme_id，再用浏览器打开作品页面，等待作品标题再抽文案。鸿哥发抖音默认要评估。', 0.5321145349164483, False), ('网关重启的管理员密码是什么', '网关重启或会话恢复后，必须先只读复核前序已授权任务（live 身份、收据、健康、未完成项），再问下一步；不重复执行已发生的部署或重启。', 0.5984362800164648, False)])
def test_host_measurements_use_local_evidence_not_model_verdict(monkeypatch, query, text, cosine, supported):
    payload = {"selected": [{"id":"0", "quote":text}] if supported else []}
    item, chosen, report, calls = select(monkeypatch, payload, query=query, text=text, cosine=cosine)
    assert chosen == ([item] if supported else [])
    assert calls == []
    assert report["status"] == ("evidence_found" if supported else "no_evidence")
    assert "caller_assistance" not in report


def test_lightweight_disabled_verifier_keeps_local_absence(monkeypatch):
    from eimemory.retrieval.lightweight_admission import LightweightAdmission, LightweightConfig
    from eimemory.retrieval.evidence_fragments import evidence_fragments, POLICY
    from eimemory.retrieval.postgres_vector import candidate_record_keyword_text
    item = record()
    fragment = evidence_fragments(candidate_record_keyword_text(item, max_text_chars=16000))[0]
    monkeypatch.setenv('EIMEMORY_CALLER_ASSISTED_RECALL_ENABLED', '0')
    selected, report = LightweightAdmission(LightweightConfig(enabled=True)).select([item],
        query='如何判断这些材料？', limit=1, validate=lambda _: True, backend_available=True,
        hints_for=lambda _: {'dense_vector_score':.3, 'fragment_policy':POLICY, 'evidence_fragment_id':fragment['id']})
    assert not selected and report['status'] == 'no_evidence'
    assert report['quality_evaluation'] == 'post_delivery'
    assert report['dropped_reasons'] == {'insufficient_evidence': 1}
    assert 'caller_assistance' not in report


def test_completion_after_recall_budget_keeps_a_verbatim_answer(monkeypatch):
    now = [1.0]
    monkeypatch.setattr(assistance, 'perf_counter', lambda: now[0])
    client = SimpleNamespace(timeout_seconds=9, complete=None)
    def complete(**kwargs):
        now[0] = 4.0
        return SimpleNamespace(text=json.dumps({'selected':[{'id':'0','quote':'Read the entire document'}]}))
    client.complete = complete
    monkeypatch.setattr(assistance, 'configured_client', lambda: client)
    chosen, report = assistance.verify_candidates(query='How to assess?',
        candidates=[(record(), 'Read the entire document.')], limit=1, deadline_at=3.0)
    assert chosen and report['outcome'] == 'supported' and report['calls'] == 1
    assert client.timeout_seconds == 9
