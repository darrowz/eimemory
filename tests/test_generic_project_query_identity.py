import pytest
from eimemory.retrieval.answer_requirements import explicit_project, supports_answer_requirements

@pytest.mark.parametrize('query,evidence', [
    ('系统故障、项目人员和项目信息分别找谁负责？', '团队事项分工：系统类故障交甲；项目相关人员信息找乙；项目信息查询找丙。'),
    ('项目预算与项目进度由谁管理？', '预算由财务负责，进度由项目经理负责。'),
    ('项目材料及项目资料归谁管理？', '材料和资料由项目经理负责。'),
])
def test_generic_coordinated_project_nouns_are_not_named_entities(query,evidence):
    assert explicit_project(query) == ''
    assert supports_answer_requirements(query,evidence)

@pytest.mark.parametrize('query,project', [('青禾项目预算多少钱？','青禾'), ('项目 Apollo 的预算多少钱？','Apollo'), ('project Helios cost','Helios')])
def test_real_project_identity_is_preserved(query,project):
    assert explicit_project(query) == project
    assert not supports_answer_requirements(query,'另一个项目预算为100万元。')


def test_missing_verifier_does_not_change_local_empty_selection(monkeypatch):
    from eimemory.retrieval import caller_assistance as ca
    from test_memory_core_v1_repair import select
    monkeypatch.setenv('EIMEMORY_CALLER_ASSISTED_RECALL_ENABLED', '1')
    monkeypatch.setattr(ca, 'configured_client', lambda: None)
    chosen, report = select('甲的手机是什么型号？', ['甲在读法律。'])
    assert chosen == []
    assert report['status'] == 'no_evidence'
    assert report['quality_evaluation'] == 'post_delivery'
    assert 'caller_assistance' not in report


@pytest.mark.parametrize('query', [
    'Alpha v1.14.29的项目现在进展如何？',
    'Orion v2.3.40的项目现在进展如何？',
])
def test_version_tail_cannot_become_project_identity(query):
    from eimemory.recall.task_queries import task_project_scope, task_recall_mode
    from eimemory.retrieval.answer_requirements import requested_attribute
    project, version = query.split()[:2]
    version = version.split('的')[0]
    assert explicit_project(query) == project
    assert task_project_scope(query, {}) == ('project', project)
    assert task_recall_mode(query) == 'status'
    assert requested_attribute(query) == 'task_status'
    assert supports_answer_requirements(query, f'{project} {version}\n已完成回归测试。')
    assert not supports_answer_requirements(query, f'Other {version}\n已完成回归测试。')
    assert not supports_answer_requirements(query, f'{project} v9.8.7\n已完成回归测试。')
    assert not supports_answer_requirements(query, f'{project} {version}\nOther项目已完成回归测试。')
