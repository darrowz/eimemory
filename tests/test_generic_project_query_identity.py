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


def test_missing_verifier_is_unavailable_not_a_no_answer_verdict(monkeypatch):
    from eimemory.retrieval import caller_assistance as ca
    from test_memory_core_v1_repair import select
    monkeypatch.setenv('EIMEMORY_CALLER_ASSISTED_RECALL_ENABLED', '1')
    monkeypatch.setattr(ca, 'configured_client', lambda: None)
    chosen, report = select('甲的手机是什么型号？', ['甲在读法律。'])
    assert chosen == []
    assert report['status'] == 'unavailable'
