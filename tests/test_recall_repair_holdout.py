"""Synthetic holdouts: no live records, inferred labels, or model changes."""
from contextlib import closing
from dataclasses import asdict

import pytest

from eimemory.api.memory import MemoryAPI
from eimemory.models.records import RecordEnvelope, ScopeRef
from eimemory.storage.runtime_store import RuntimeStore


SCOPE = ScopeRef(user_id='synthetic-holdout-owner')
FACTS = (
    '打印机卡纸后，先断开电源，再检查纸路并取出残纸。',
    '相机更换镜头前，先关闭电源，再卸下镜头，避免灰尘进入机身。',
)


@pytest.mark.parametrize('query,expected', [
    ('打印机卡纸应该如何处理？', 0),
    ('更换镜头之前应该先做什么？', 1),
    ('打印机 卡纸 断开电源 纸路', 0),
    ('相机 更换镜头 关闭电源', 1),
    ('打印机的管理员口令是什么？', None),
    ('相机的购买价格是多少？', None),
    ('月球仓库的门禁密码是什么？', None),
])
def test_procedure_recall_holdout(tmp_path, query, expected):
    with closing(RuntimeStore(tmp_path)) as store:
        records = [store.append(RecordEnvelope.create(
            kind='memory', title=text, summary=text,
            content={'text': text, 'memory_type': 'preference'},
            meta={'memory_type': 'preference'}, scope=SCOPE,
            source='synthetic.fixture', source_id='holdout',
        )) for text in FACTS]
        bundle = MemoryAPI(store).recall(query=query, scope=asdict(SCOPE),
            task_context={'source_ids': ['holdout']}, limit=5)
        assert [r.record_id for r in bundle.items] == (
            [] if expected is None else [records[expected].record_id])


def test_equivalent_preferences_do_not_consume_two_result_slots(tmp_path):
    with closing(RuntimeStore(tmp_path)) as store:
        records = [store.append(RecordEnvelope.create(
            kind='memory', title=text, summary=text,
            content={'text': text, 'memory_type': 'preference'},
            meta={'memory_type': 'preference'}, scope=SCOPE,
            source='synthetic.fixture', source_id='holdout',
        )) for text in (
            '公众号链接默认先评估；只有明确要求摘要才提供摘要。',
            '默认收到公众号文章链接后先给评估，明确要求摘要时才摘要。',
        )]
        bundle = MemoryAPI(store).recall(query='公众号 链接 评估 摘要',
            scope=asdict(SCOPE), task_context={'source_ids': ['holdout']}, limit=5)
        assert len(bundle.items) == 1
        assert bundle.items[0].record_id in {r.record_id for r in records}

def test_preference_paraphrase_key_collapses_wording_not_opposites_or_versions():
    from eimemory.recall.dedupe import preference_paraphrase_key, memory_content_key

    def pref(text, **kwargs):
        content = {'text': text, 'memory_type': 'preference'}
        content.update(kwargs.pop('content', {}))
        meta = {'memory_type': 'preference'}
        meta.update(kwargs.pop('meta', {}))
        return RecordEnvelope.create(
            kind='memory', title=text, summary=text, content=content,
            meta=meta, scope=SCOPE, source='synthetic.fixture', source_id='holdout',
            **kwargs,
        )

    first = pref('公众号链接默认先评估；只有明确要求摘要才提供摘要。')
    paraphrase = pref('默认收到公众号文章链接后先给评估，明确要求摘要时才摘要。')
    opposite = pref('公众号链接默认先摘要；只有明确要求评估才提供评估。')
    versioned = pref(
        '公众号链接默认先评估；只有明确要求摘要才提供摘要。',
        content={'text': '公众号链接默认先评估；只有明确要求摘要才提供摘要。', 'memory_type': 'preference', 'version': 'v2'},
    )
    assert preference_paraphrase_key(first)
    assert preference_paraphrase_key(first) == preference_paraphrase_key(paraphrase)
    assert preference_paraphrase_key(first) != preference_paraphrase_key(opposite)
    assert preference_paraphrase_key(first) != preference_paraphrase_key(versioned)
    assert memory_content_key(first) != memory_content_key(paraphrase)

    read_then_extract = pref('先读取作品标题，再抽文案。')
    extract_then_read = pref('先抽文案，再读取作品标题。')
    negated = pref('先读取作品标题，不抽文案。')
    assert preference_paraphrase_key(read_then_extract) != preference_paraphrase_key(extract_then_read)
    assert preference_paraphrase_key(read_then_extract) != preference_paraphrase_key(negated)
    assert len(MemoryAPI._dedupe_records([first, paraphrase])) == 1
    assert len(MemoryAPI._dedupe_records([first, opposite, versioned])) == 3
