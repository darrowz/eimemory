"""Independent synthetic semantic contracts; no provider or real data."""
from copy import deepcopy
import pytest
from eimemory.models.records import RecordEnvelope, ScopeRef, TimeRef
from eimemory.api.memory import MemoryAPI
from eimemory.recall.dedupe import preference_paraphrase_key

STAMP='2026-10-02T00:00:00Z'
def record(text, rid):
    return RecordEnvelope(record_id=rid,kind='memory',status='active',title=text,
        summary=text,detail='',content={'text':text,'memory_type':'preference'},
        tags=[],links=[],evidence=[],source='synthetic',scope=ScopeRef(tenant_id='synthetic'),
        time=TimeRef(STAMP,STAMP,STAMP),provenance={},meta={'memory_type':'preference'},source_id='synthetic')

OPPOSITES=[
 ('negation_synonym','默认不检查电源','默认检查电源'),
 ('unparsed_role_constraint','默认提供摘要；需要保留原件','默认提供摘要；需要删除原件'),
 ('unparsed_step_constraint','先检查电源，再读取日志；必须保留原件','先检查电源，再读取日志；必须删除原件'),
 ('step_marker_inside_noun','先检查后台日志，再重启服务','先检查后台配置，再重启服务'),
 ('english_permission_binding','Allow read deny write','Allow write deny read'),
 ('negated_article_constraint','非文章链接默认提供摘要','非链接默认提供摘要'),
 ('address_url_constraint','默认检查收货地址','默认检查收货网址'),
]
@pytest.mark.parametrize('reverse',[False,True],ids=['forward','reverse'])
@pytest.mark.parametrize('label,left,right',OPPOSITES,ids=[x[0] for x in OPPOSITES])
def test_conflicting_preferences_do_not_disappear(label,left,right,reverse):
    rows=[record(left,'mem_a_left'),record(right,'mem_z_right')]
    if reverse: rows.reverse()
    result=MemoryAPI._dedupe_records(rows)
    assert {r.record_id for r in result} == {'mem_a_left','mem_z_right'}, {
      'case':label,'input':[(r.record_id,r.content['text']) for r in rows],
      'actual':[(r.record_id,r.content['text']) for r in result]}

@pytest.mark.parametrize('left,right,count',[
 ('公众号链接默认先评估；只有明确要求摘要才提供摘要。','默认收到公众号文章链接后先给评估，明确要求摘要时才摘要。',2),
 ('先关闭电源再卸下镜头','先卸下镜头再关闭电源',2),
 ('默认先关闭电源再卸下镜头','默认先卸下镜头再关闭电源',2),
 ('公众号链接默认先评估；只有明确要求摘要才提供摘要。','公众号链接默认先摘要；只有明确要求评估才提供评估。',2),
 ('默认不复核电源','默认复核电源',2),
],ids=['article_constraint_preserved','ordinary_step_order','default_step_order_fixed','opposite_roles','canonical_negation'])
def test_semantic_and_structural_contracts(left,right,count):
    assert len(MemoryAPI._dedupe_records([record(left,'mem_a_left'),record(right,'mem_z_right')]))==count


@pytest.mark.parametrize('verb',['检查','复核','查看','核对','评估','评价','摘要','总结','概括'])
def test_synonym_canonicalization_keeps_negation(verb):
    left=record('默认不'+verb+'方案','mem_negative')
    right=record('默认'+verb+'方案','mem_positive')
    for items in ([left,right],[right,left]):
        assert len(MemoryAPI._dedupe_records(items))==2

@pytest.mark.parametrize('left,right',[
 ('默认不检查电源且检查纸路','默认检查电源且不检查纸路'),
 ('默认需要检查电源','默认可以检查电源'),
 ('默认 threshold < 5','默认 threshold > 5'),
 ('默认检查电源检查纸路','默认检查电源纸路'),
 ('电源默认检查纸路','电源检查默认纸路'),
 ('只有明确要求检查纸路才检查电源','只有明确要求检查电源才检查纸路'),
 ('先检查电源并读取日志，再关闭机身','先读取日志并检查电源，再关闭机身'),
 ('默认先进模式','默认进模式'),
 ('默认不发送文章链接','默认不发送链接'),
],ids=['negation_object','modality','comparison','repeated_action','default_context_binding','conditional_binding','within_step_order','marker_inside_noun','action_object_modifier'])
def test_every_semantic_fragment_and_binding_survives(left,right):
    a,b=record(left,'mem_a'),record(right,'mem_b')
    for items in ([a,b],[b,a]):
        assert len(MemoryAPI._dedupe_records(items))==2

@pytest.mark.parametrize('left,right',[
 ('主题链接默认先评估；只有明确要求摘要才提供摘要。','默认收到主题链接后先给评估，明确要求摘要时才摘要。'),
 ('先检查电源，再读取日志','先检查电源，然后读取日志'),
])
def test_lossless_grammar_variants_can_still_merge(left,right):
    a,b=record(left,'mem_a'),record(right,'mem_b')
    assert preference_paraphrase_key(a) is not None
    assert preference_paraphrase_key(a)==preference_paraphrase_key(b)
    assert len(MemoryAPI._dedupe_records([a,b]))==1


@pytest.mark.parametrize('left,right',[
 ('默认不检查电源','默认不复核电源'),
 ('先检查电源，再读取日志','先复核电源，然后读取日志'),
 ('默认评估方案','默认评价方案'),
 ('默认提供摘要','默认提供总结'),
 ('默认选择 Variant_A','默认选择 variant_a'),
 ('默认检查联系地址','默认检查联系网址'),
 ('文章链接默认提供摘要','链接默认提供摘要'),
 ('方案内容默认提供摘要','方案默认提供摘要'),
],ids=['negative_verb','step_verb','evaluation_verb','summary_noun','identifier_case','address_context','article_modifier','content_modifier'])
def test_semantic_word_changes_are_not_grammar_equivalence(left,right):
    a,b=record(left,'mem_a'),record(right,'mem_b')
    assert len(MemoryAPI._dedupe_records([a,b]))==2
    assert len(MemoryAPI._dedupe_records([b,a]))==2

@pytest.mark.parametrize('text',[
 'Allow read deny write',
 '默认提供摘要；需要保留原件',
 '先检查电源，再读取日志；必须保留原件',
 '先检查电源再读取日志',
 '先检查最后期限，再读取日志',
 '收到链接后检查电源',
])
def test_incomplete_or_ambiguous_parse_falls_back_to_exact(text):
    a,b=record(text,'mem_a'),record(text,'mem_b')
    assert preference_paraphrase_key(a) is None
    # Conservative near-dedupe changes never disable literal exact dedupe.
    assert len(MemoryAPI._dedupe_records([a,b]))==1

@pytest.mark.parametrize('field,value',[
 ('content',{'text':'默认提供摘要','memory_type':'preference','audience':'other'}),
 ('meta',{'memory_type':'preference','audience':'other'}),
 ('provenance',{'constraint':'other'}),
 ('tags',['other']),
 ('evidence',['other']),
 ('aliases',['other']),
])
def test_same_text_different_structured_constraints_do_not_merge(field,value):
    a=record('默认提供摘要','mem_a')
    b=deepcopy(a);b.record_id='mem_b';setattr(b,field,value)
    assert preference_paraphrase_key(a)!=preference_paraphrase_key(b)
    assert len(MemoryAPI._dedupe_records([a,b]))==2


def test_scoring_metadata_is_not_a_semantic_constraint():
    a=record('默认提供摘要','mem_a')
    b=deepcopy(a);b.record_id='mem_b'
    b.meta['quality']={'salience_score':.9}
    assert preference_paraphrase_key(a)==preference_paraphrase_key(b)
    assert len(MemoryAPI._dedupe_records([a,b]))==1
