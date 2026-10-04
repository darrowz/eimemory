import pytest
from eimemory.recall.lexical import _clean_text, analyze_lexical_signal

@pytest.mark.parametrize('text', ['v1.14.42', '发布v1.14.42版本', '(v1.14.42)。', '版本：V1.14.42，'])
def test_dotted_version_survives_cjk_and_punctuation(text):
    assert 'v1.14.42' in _clean_text(text)
    assert analyze_lexical_signal('v1.14.42', text).version_hits == ('v1.14.42',)

@pytest.mark.parametrize('text', ['v1.14.43', 'v1.14.420', 'v1.14.42beta', 'MIPROv1.14.42', 'v1.14.42-other'])
def test_other_version_and_embedded_suffix_do_not_match(text):
    assert not analyze_lexical_signal('v1.14.42', text).version_hits


def test_legacy_entity_path_cleaning_is_preserved():
    assert _clean_text('Some.Path / foo-bar MIPROv2') == 'some path foo bar miprov2'


def test_native_sqlite_version_query_ranks_full_version_first(tmp_path):
    from eimemory.models.records import RecordEnvelope, ScopeRef
    from eimemory.storage.runtime_store import RuntimeStore
    scope = ScopeRef(user_id='fixture-owner')
    store = RuntimeStore(tmp_path / 'runtime')
    try:
        for version in ['v1.14.42', 'v1.14.43', 'v1.14.420', 'v1.14.42beta']:
            record = RecordEnvelope.create(kind='memory', title='版本说明',
                summary='发布'+version+'版本', detail='发布'+version+'版本',
                content={'text':'发布'+version+'版本'}, scope=scope, source='synthetic.version')
            record.record_id = 'synthetic-'+version
            store.append(record)
        records, diagnostics = store.search_with_diagnostics(
            query='v1.14.42', kinds=['memory'], scope=scope, limit=4)
        assert records[0].record_id == 'synthetic-v1.14.42'
        scored = {row['record_id']: row for row in diagnostics['scored_items']}
        assert tuple(scored['synthetic-v1.14.42']['lexical_signal']['version_hits']) == ('v1.14.42',)
        for ref, row in scored.items():
            if ref != 'synthetic-v1.14.42':
                assert not row['lexical_signal']['version_hits']
    finally:
        store.close()
