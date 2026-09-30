"""Sanitized failure shapes; synthetic scores are not a production replay."""
from dataclasses import replace

import pytest

from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.retrieval.answer_requirements import supports_answer_requirements
from eimemory.retrieval.evidence_fragments import POLICY, evidence_fragments
from eimemory.retrieval.lightweight_admission import LightweightAdmission, LightweightConfig
from eimemory.retrieval.postgres_vector import candidate_record_keyword_text


def item(text, occurred):
    record = RecordEnvelope.create(kind='memory', title='Hermes completed turn',
        summary=text, content={'text': text, 'memory_type': 'conversation'},
        meta={'memory_type': 'conversation'}, source_id='hermes',
        scope=ScopeRef(user_id='synthetic-owner'))
    return replace(record, time=replace(record.time, occurred_at=occurred))


@pytest.mark.parametrize('query', ['Atlas项目最新进展', 'Atlas项目最近任务进展'])
def test_recent_weak_evidence_cannot_displace_relevant_history(query):
    strong = item('Atlas项目进展：检索修复已完成，测试通过。', '2026-09-01T00:00:00Z')
    weak = item('Atlas项目进展：文档封面已完成。', '2026-09-30T00:00:00Z')
    evidence = {}
    for record, cosine in [(strong, .94), (weak, .66)]:
        fragment = evidence_fragments(candidate_record_keyword_text(record, max_text_chars=16000))[0]
        evidence[record.record_id] = dict(dense_vector_score=cosine,
            fragment_policy=POLICY, evidence_fragment_id=fragment['id'])
    selected, report = LightweightAdmission(LightweightConfig(enabled=True)).select(
        [weak, strong], query=query, limit=1, validate=lambda _: True,
        hints_for=lambda r: evidence[r.record_id], backend_available=True)
    assert selected == [strong]
    assert report['dropped_reasons']['evidence_score_gap'] == 1


@pytest.mark.parametrize('query', ['Atlas项目现在进展如何？', 'Atlas项目之前任务历史'])
def test_project_notification_cannot_borrow_another_subjects_assertion(query):
    text = 'Atlas项目。ASYNC DELEGATION。Boreal项目已完成。'
    assert not supports_answer_requirements(query, text)


@pytest.mark.parametrize('text', [
    'User: Delegated task:\nAtlas项目已完成检索修复，测试通过。',
    'Atlas项目进展：旧版已部署。',
])
def test_delegated_results_and_explicit_history_remain_evidence(text):
    assert supports_answer_requirements('Atlas项目之前任务历史', text)


def test_model_preference_is_not_project_state():
    assert not supports_answer_requirements('Atlas项目现在进展如何？',
        'Atlas项目使用代码助手；用户偏好高能力模型。')


def test_time_only_orders_within_relevant_band_and_authority_is_rechecked():
    old = item('Atlas项目进展：检索已完成。', '2026-09-01T00:00:00Z')
    new = item('Atlas项目进展：页面已完成。', '2026-09-30T00:00:00Z')
    def hints(record):
        fragment = evidence_fragments(candidate_record_keyword_text(record, max_text_chars=16000))[0]
        return dict(dense_vector_score=.8, fragment_policy=POLICY,
                    evidence_fragment_id=fragment['id'])
    gate = LightweightAdmission(LightweightConfig(enabled=True))
    kwargs = dict(query='Atlas项目最新进展', limit=1, hints_for=hints, backend_available=True)
    selected, _ = gate.select([old, new], validate=lambda _: True, **kwargs)
    assert selected == [new]
    checks = 0
    def revoked(record):
        nonlocal checks
        checks += 1
        return checks <= 2
    selected, report = gate.select([old, new], validate=revoked, **kwargs)
    assert not selected
    assert report['dropped_reasons']['authority_changed_during_selection'] == 1


@pytest.mark.parametrize('heading', ['Atlas项目', '## Atlas项目', 'project Atlas:'])
@pytest.mark.parametrize('query', ['Atlas项目现在进展如何？', 'Atlas项目之前任务历史'])
def test_heading_binds_immediately_following_subjectless_fact(heading, query):
    assert supports_answer_requirements(query, heading + '\n已完成检索修复，测试通过。')


@pytest.mark.parametrize('text', [
    'Atlas项目\nBoreal项目已完成。',
    'Atlas项目\nASYNC DELEGATION\n已完成。',
    'Atlas项目\n\n已完成。',
    'Atlas项目。\n已完成。',
    'Atlas项目\nBoreal项目\n已完成。',
    'Atlas项目通知：Boreal项目已完成。',
    'Atlas项目\nBoreal已完成。',
    'Atlas项目\n张三已完成。',
    'Atlas项目\n' + '说明' * 260 + '已完成。',
])
def test_heading_identity_does_not_cross_subject_or_context_boundary(text):
    assert not supports_answer_requirements('Atlas项目现在进展如何？', text)


@pytest.mark.parametrize('text,expected', [
    ('Atlas项目\n已完成检索修复，测试通过。', True),
    ('Atlas项目\nBoreal项目已完成。', False),
    ('Atlas项目\nASYNC DELEGATION\n已完成。', False),
])
def test_multiline_binding_through_fragment_admission(text, expected):
    record = item(text, '2026-09-30T00:00:00Z')
    fragment = evidence_fragments(candidate_record_keyword_text(record, max_text_chars=16000))[0]
    selected, _ = LightweightAdmission(LightweightConfig(enabled=True)).select(
        [record], query='Atlas项目现在进展如何？', limit=1,
        validate=lambda _: True, backend_available=True,
        hints_for=lambda _: dict(dense_vector_score=.9, fragment_policy=POLICY,
                                evidence_fragment_id=fragment['id']))
    assert bool(selected) == expected


@pytest.mark.parametrize('text', [
    '项目Alpha\n项目Beta已部署成功。',
    '项目Alpha\n已部署成功。项目Beta v1.2.3已部署成功。',
    '项目Alpha\n通知：已部署成功。',
])
def test_acceptance_requirements_are_not_deployment_facts(text):
    assert not supports_answer_requirements('项目Alpha v1.2.3部署验收要求是什么？', text)


@pytest.mark.parametrize('query', [
    '项目Alpha v1.2.3部署验收要求是什么？',
    'Atlas项目发布验收要求有哪些？',
    'Boreal项目上线需要满足什么条件？',
    'project Orion acceptance requirements?',
])
def test_requirement_attribute_precedes_release_or_task_state(query):
    from eimemory.retrieval.answer_requirements import requested_attribute
    assert requested_attribute(query) == 'constraint'


@pytest.mark.parametrize('text,expected', [
    ('项目Alpha v1.2.3部署已完成。', True),
    ('项目Alpha v9.8.7部署已完成。', False),
    ('项目Alpha部署已完成。', False),
    ('项目Alpha\n项目Beta v1.2.3部署已完成。', False),
])
def test_version_identity_adjacent_to_chinese(text, expected):
    assert supports_answer_requirements('项目Alpha v1.2.3部署情况？', text) == expected


@pytest.mark.parametrize('text,expected', [
    ('Atlas项目发布验收要求：必须通过回归测试。', True),
    ('Atlas项目\n必须通过回归测试。', True),
    ('Atlas项目\nBoreal项目发布要求：必须通过回归测试。', False),
    ('Atlas项目已部署成功。Boreal项目要求通过回归测试。', False),
    ('Atlas项目使用代码助手；用户偏好高能力模型。', False),
    ('Atlas项目发布验收要求是什么？用户偏好高能力模型。', False),
])
def test_constraint_subject_binding(text, expected):
    assert supports_answer_requirements('Atlas项目发布验收要求有哪些？', text) == expected


def test_unknown_attribute_keeps_semantic_path():
    from eimemory.retrieval.answer_requirements import requested_attribute
    query = 'Atlas项目有哪些相关经验？'
    assert requested_attribute(query) == ''
    assert supports_answer_requirements(query, 'Atlas项目采用分阶段沟通。')


@pytest.mark.parametrize('query', ['Atlas项目发布验收要求有哪些？', 'Atlas项目上线需要满足什么条件？'])
def test_natural_requirements_reject_preference_in_authorized_record_shape(query):
    # Sanitized completed-turn container, no production content or identifiers.
    rule = item('Atlas项目发布要求：必须通过回归测试并提供验收证据。', '2026-09-01T00:00:00Z')
    preference = item('Atlas项目使用代码助手；用户偏好高能力模型。', '2026-09-30T00:00:00Z')
    def hints(record):
        fragment = evidence_fragments(candidate_record_keyword_text(record, max_text_chars=16000))[0]
        return dict(dense_vector_score=.9, fragment_policy=POLICY, evidence_fragment_id=fragment['id'])
    selected, report = LightweightAdmission(LightweightConfig(enabled=True)).select(
        [preference, rule], query=query, limit=2, validate=lambda _: True,
        hints_for=hints, backend_available=True)
    assert selected == [rule]
    assert report['dropped_reasons']['requested_attribute_missing'] == 1


@pytest.mark.parametrize('query,text,expected', [
    ('项目Alpha发布要求是什么？', '项目Alpha\n必须通过测试。', True),
    ('项目Alpha发布要求是什么？', '项目Alpha\n项目Beta必须通过测试。', False),
    ('项目Alpha v1.2.3部署验收要求是什么？', '项目Alpha v1.2.3验收要求：必须通过测试。', True),
    ('项目Alpha v1.2.3部署验收要求是什么？', '项目Alpha v9.8.7验收要求：必须通过测试。', False),
    ('Atlas项目发布要求是什么？', 'Atlas项目必须通过测试吗？', False),
    ('Atlas项目有什么经验？', 'Boreal项目采用分阶段沟通。', False),
    ('Atlas项目完成标准是什么？', 'Atlas项目已完成。', False),
])
def test_constraint_and_unknown_identity_controls(query, text, expected):
    assert supports_answer_requirements(query, text) == expected


@pytest.mark.parametrize('query', ['Atlas项目进度验收标准是什么？', 'project Atlas acceptance requirements?'])
def test_constraints_do_not_route_to_task_fact_filter(query):
    from eimemory.recall.task_queries import task_recall_mode
    from eimemory.retrieval.answer_requirements import requested_attribute
    assert requested_attribute(query) == 'constraint'
    assert task_recall_mode(query) == ''


@pytest.mark.parametrize('heading', ['Alpha', '项目Alpha', 'Alpha项目', '## project Alpha:'])
@pytest.mark.parametrize('version,expected', [('v1.14.29', True), ('v1.14.28', False), ('', False)])
@pytest.mark.parametrize('question,fact', [
    ('验收要求是什么？', '必须先完成回归测试。'),
    ('部署情况？', '已部署成功。'),
    ('任务现在进展如何？', '已完成回归测试。'),
    ('项目现在进展如何？', '已完成回归测试。'),
    ('之前任务历史', '已授权先完成回归测试。'),
])
def test_versioned_heading_attribute_matrix(heading, version, expected, question, fact):
    heading = heading.rstrip(':') + (' ' + version if version else '')
    assert supports_answer_requirements('Alpha v1.14.29的' + question, heading + '\n' + fact) is expected


@pytest.mark.parametrize('text', [
    'Beta v1.14.29\n必须先完成回归测试。',
    'Alpha v1.14.29\nBeta必须先完成回归测试。',
    'Alpha v1.14.29\nBeta v1.14.29必须先完成回归测试。',
    'Alpha v1.14.29\n通知：Beta已完成。\n必须先完成回归测试。',
    'Alpha v1.14.29\n\n必须先完成回归测试。',
    'Alpha v1.14.29。\n必须先完成回归测试。',
    'Alpha v1.14.29\n说明\n必须先完成回归测试。',
    'Alpha v1.14.29\n必须先完成回归测试，适用v1.14.28。',
    'Alpha v1.14.29\n必须先完成Beta项目回归测试。',
    'Alpha v1.14.29\n' + '必须' + '先' * 512 + '完成测试。',
    'Alpha v1.14.29通知\n必须先完成回归测试。',
    '参考v1.14.29。Alpha项目\n必须先完成回归测试。',
])
def test_versioned_heading_cannot_borrow_identity(text):
    assert not supports_answer_requirements('Alpha v1.14.29的验收要求是什么？', text)


def test_review_exact_requirement_same_sentence_control():
    from eimemory.retrieval.answer_requirements import requested_attribute, explicit_project
    query = 'Alpha v1.14.29的验收要求是什么？'
    assert requested_attribute(query) == 'constraint'
    assert explicit_project(query) == 'Alpha'
    assert supports_answer_requirements(query, 'Alpha v1.14.29必须先完成回归测试。')


@pytest.mark.parametrize('query', ['项目预算与项目进度由谁管理？', 'Alpha项目进度由谁管理？',
                                 '谁负责Alpha项目进度？'])
def test_responsibility_is_not_task_status(query):
    from eimemory.recall.task_queries import task_recall_mode
    from eimemory.retrieval.answer_requirements import requested_attribute
    assert task_recall_mode(query) == ''
    assert requested_attribute(query) == ''


@pytest.mark.parametrize('version,expected', [('v1.14.29', True), ('v1.14.28', False), ('', False)])
def test_versioned_constraint_heading_through_fragment_admission(version, expected):
    record = item(f'Alpha {version}\n必须先完成回归测试。', '2026-09-30T00:00:00Z')
    fragment = evidence_fragments(candidate_record_keyword_text(record, max_text_chars=16000))[0]
    selected, _ = LightweightAdmission(LightweightConfig(enabled=True)).select(
        [record], query='Alpha v1.14.29的验收要求是什么？', limit=1,
        validate=lambda _: True, backend_available=True,
        hints_for=lambda _: dict(dense_vector_score=.9, fragment_policy=POLICY,
                                evidence_fragment_id=fragment['id']))
    assert bool(selected) is expected
